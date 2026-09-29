# Delegate Workflows

Delegate Workflows are Python workflow scripts executed by a detached supervisor
on top of normal Delegate runs. A workflow gets its own registry at
`.delegate/workflows/<wfId>/`; every `agent()` child is an ordinary run tagged
with `--group <wfId>`, so `runs`, `snapshot`, `run-output`, `wait`, and
`cancel` keep working on child runs.

Workflow registries use this file set as needed:

- `script.py`: pinned workflow source for this run (the frozen copy executed on
  every resume).
- `args.json`: launch arguments supplied with `--args`.
- `journal.jsonl`: append-only workflow events.
- `status.json`: current supervisor/status snapshot; records `sourceScript`
  (the launch-time source path, provenance only, never the resume execution
  input) and `scriptSha256`.
- `result.json`: final workflow result, present only after success.
- `approval.json`: approvals bound to both a gate key and its result hash. A changed result requires a fresh approval; a key-only legacy approval does not authorize it.
- `workflow.lock`: process lock held while a supervisor is active.

## Terminology

Canonical terms — use these in code, docs, and prompts rather than synonyms:

- **Workflow**: one registered execution of a workflow script, identified by
  `wf_<12 hex>` and rooted at `.delegate/workflows/<wfId>/`.
- **Supervisor**: the detached process that executes the script body and owns
  the journal, status snapshot, and workflow lock for its lifetime.
- **Journal**: the append-only `journal.jsonl` event log with monotonic
  sequence numbers; result-bearing events are fsynced and are the durable
  source of truth on resume.
- **Structural key**: the deterministic identity of one `agent()` call —
  `sha256` over its scope path, prompt, and canonical options — used for
  replay, claim idempotency, and adoption across resumes.
- **Child run**: an ordinary Delegate run launched by `agent()`/`judges()`,
  tagged `--group <wfId>` so the standard run commands apply to it.
- **Replay**: returning a journaled result for a structural key instantly on
  resume instead of re-running the child.
- **Adoption**: on resume, recognizing a child run that already finished in the
  run registry for a started-without-result key and taking its outcome instead
  of respawning a duplicate. A resume cancels children that were still running
  (see [Gates and resume](#gates-and-resume)); it does not adopt them.
- **Gate**: a human checkpoint — the supervisor stops admitting new agents,
  drains in-flight ones to the journal, records a `gate` event, and exits with
  status `paused` until `workflow approve` (or `run --resume`) relaunches it.

## Script shape

```python
meta = {
    "name": "review-changes",
    "description": "Review changed files, then verify findings.",
    "defaults": {"engine": "codex", "mode": "safe"},
}

findings = pipeline(
    args["files"],
    lambda _prev, file, index: agent(f"Review {file}", phase="Review"),
    lambda finding, file, index: agent(f"Verify: {finding}", mode="call"),
)
return {"confirmed": [item for item in findings if item]}
```

`meta` must be a pure top-level dict literal. Top-level `return` becomes the
workflow result in `result.json`. Injected globals are `agent`, `followup`,
`pipeline`, `parallel`, `phase`, `log`, `workflow`, `judges`, `args`, `budget`,
`agent_meta`, `AgentFailure`, `capabilities`, `park_gate`, `reject`,
`soft_park`, `park_item` (aliased as `soft_park_item`, `item_park`, and
`park`), `soft_park_request`, `parked`, `SoftPark`, `structured_attempt`, and
`dry_run` (aliased as `is_dry_run`). The authoritative list is
`workflows.dsl.globals` in `delegate --json describe --full`, which reads the
runtime's `WORKFLOW_DSL_GLOBALS`; the injector refuses to run when its bindings
drift from that tuple.

## Core DSL

- `agent(prompt, engine=None, mode=None, model=None, effort=None, schema=None, label=None, phase=None, isolation=None, passthrough=False, timeout=None, retries=None, fast=None, persona=None, allow_repo_persona=False, resumable=False, on_failure="none", key=None, base=None, env=None, setup=None)` launches a real Delegate child run and returns parent-facing output, a validated schema object, or `None`. `fast=True` requests Codex Fast, `fast=False` requests Standard, and `None` inherits; non-Codex fallback candidates ignore this Codex-only preference. `persona` resolves one named persona from the source workspace; `allow_repo_persona=True` opts into workspace-local personas in safe mode. `resumable=True` preserves the harness session for native session resumption with `followup()`. Workflow children stay non-resumable unless the call passes `resumable=True`, even though standalone Codex and Claude work Runs are resumable by default: a fan-out would otherwise retain every child's native session file and worktree. `on_failure="typed"` makes an exhausted structured call return a falsy `AgentFailure` instead of `None` (see below). `key="..."` gives the call a stable replay identity (see [Stable step keys](#stable-step-keys)). `base=`, `env=` (a dict of names to strings), and `setup=` pass a [workspace spec](worktrees.md#workspace-spec-base-env-setup) to a `mode="work"`, `isolation="worktree"` child; other lanes raise `ValueError`. They join the call's replay identity with env values reduced to a digest. A structured retry that re-enters the first attempt's worktree carries `env` only.
- `agent_meta(key_or_label=None)` returns the latest agent attempt's child outcome (`runId`, `ok`, `status`, `failureKind`, `failureReason`, `degraded`, `degradedReason`, `servedModel`, `servedProvider`), or, with no argument, that of the most recent `agent()` call on the calling thread. `degraded` is `True` when a `succeeded` child [ended its turn with work unfinished](cli-reference.md#degraded-runs-the-child-ended-its-turn-mid-job) (for example, "Waiting on the gate" while its test run was still in the background), and `None` otherwise. The step is not failed and `agent()` still returns the child's text: a script that must not accept unfinished work checks `agent_meta()` itself, and the same fields ride on the `agent_child` journal event.
- `capabilities` maps feature names to versions (`agentFailure`, `agentMeta`, `failureKind`, `agentKey`, `scopeKey`, `gateActions`, `workspaceSpec`, `providerOutcomes`); a script tests membership before relying on a newer feature, for example `key="impl" if capabilities.get("agentKey") else None`. `capabilities` describes the runtime the run was pinned to. A plain resume keeps that pin (a runtime that no longer matches the pin is refused as `pin_collision` rather than silently re-pinned), so a script's keyed/unkeyed choice stays stable for the whole run. `workflow resume --repin` is the one way to move the pin (see [Pinned runtime and `--repin`](#pinned-runtime-and---repin)); the capabilities map then describes the live runtime, so a script that branches on it can take a different branch after a repin.
- `followup(prior_label, prompt, label=None, phase=None, schema=None, timeout=None, retries=None)` continues an earlier resumable child run by its label and returns parent-facing output, a validated schema object, or `None`.
- `pipeline(items, stage1, ..., key=None)` runs per-item stage chains with no inter-stage barrier. A throwing stage drops that item to `None` and skips later stages for that item; a key refusal propagates to the script instead (see [Stable step keys](#stable-step-keys)).
- `parallel([lambda: ...], key=None)` is a barrier and preserves order. Ordinary item failures become `None` slots; gate checkpoints and key refusals propagate to the supervisor.
- `phase(title)` emits a progress event.
- `log(message)` emits a JSON-safe log event.
- `workflow(name_or_path, args=None, gate=False, key=None)` nests another workflow. Use `gate=True` or `gate="on-failure"` for approval checkpoints.
- `park_gate(key, result=None, actions=None)` pauses for an operator decision and, once approved, returns `{"gate", "action", "note", "data"}` (see [Gate actions](#gate-actions)).
- `judges(prompt, schema, engines=[...], *, effort=None)` runs one `call --read-only` judge lane per engine and returns the votes. Pass `effort=` to select a uniform reasoning effort for the panel, or set `"effort"` per engine dict item in `engines` to override it.

Workflow `engine` values and `workflows.engineCaps` keys accept `cursor`,
`droid`, `codex`, `claude`, `grok`, `devin`, `opencode`, `pi`, `omp`, and
`kimi`.

### Persona digest pinning

When `agent(persona="editor")` first resolves `editor.md`, the workflow
structural key and `agent_started` journal event include the resolved bytes'
SHA-256 digest. Replay therefore returns the result only for the same persona
version: editing the file creates a cache miss instead of mixing two versions
under the same name. Dry-runs expose persona name/source/digest/byte count but
never write the persona body. Child input JSON carries `persona` and
`allowRepoPersona`; the normal run manifest and inspection projections expose
non-sensitive persona metadata only.

### Stable step keys

By default a call's replay identity is positional: its scope path
(`root/parallel@1/thunk#0/seq#0`) plus its prompt and options. A resumed script
replays correctly only when it makes the same calls in the same order with the
same prompts. A script that reads mutable state (plan files, git heads) and
skips settled work breaks that: skipping one `parallel()` shifts every later
position, and a new head in a prompt changes the key, so settled steps run
again.

`agent(..., key="impl-task-7")` replaces that identity with the caller's key,
namespaced by the enclosing named scope. Position and prompt text no longer
matter, and a keyed call consumes no positional counter, so skipping it never
shifts an unkeyed sibling. The `agent_started` event records `callerKey`,
`promptDigest`, and `optsDigest`. When a resumed call sends a different prompt
or options under the same key, the recorded result is still adopted and the
journal gets a `key_prompt_mismatch` event (`promptChanged`, `optsChanged`,
both digests).

`parallel(..., key=)`, `pipeline(..., key=)`, and `workflow(..., key=)` name
their scope `<named scope>/<kind>:<key>`. Positional counters inside restart
from that stable parent, and keyed calls inside are namespaced by it. An unkeyed
primitive keeps today's positional scope and passes the enclosing named scope
through. The root, a keyed primitive, and a `soft_park()` item are named
scopes.

A key names one step per run lifetime (one supervisor process). Reusing a
key in the same named scope raises `WorkflowKeyConflict`: while the first call
is live ("held by a live agent() call"), or after it settled ("already used
by an earlier agent() call"), because the second call would silently receive
the first's result. Inside a per-item `pipeline()`, `parallel()`, or
`soft_park()` item, include the item id in the key or key the enclosing
primitive. The refusal reaches the script from inside those handlers, the same
way a gate checkpoint does, rather than being reported as a failed item, so a
script-authoring mistake is never silent. An unusable key or
`park_gate(actions=...)` list is refused the same way. `reject()` of a keyed
step moves it to the next retry identity, so reject-and-rerun works in one
lifetime and replays the same way on resume.

Unkeyed calls keep their positional keys exactly, so existing journals replay
unchanged.

`workflow check` warns about the mixed case: a script that gives some `agent()`,
`parallel()`, `pipeline()`, or `workflow()` calls a `key=` and leaves others
positional gets one `keying warning` per primitive, with the source lines of
the unkeyed calls. Those calls still replay by position and prompt, so the
shifting-position problem described above applies to them. A script that keys
nothing is left alone, and a call that passes `**kwargs` is assumed to carry
its key there.

### Concurrent calls and stale children

Positional scopes and counters are per thread. Two calls from plain Python
threads (not `parallel()`) can therefore share one scope path. On resume, an
unfinished child from an earlier lifetime whose scope now carries a different
key is stale and is cancelled. A child started by the current lifetime is never
treated as stale, whatever its scope: `agent_started` carries the supervisor's
`incarnation` token, and the runtime cancels only children it did not start.

## Gates and resume

Only current-format workflows can resume: version-2 structural keys, a runtime
pin, and version-1 attempt configuration are required. Older or pinless workflow
records are rejected before child launch. Start a new workflow instead of
reusing old state. New workflows replay completed children, relaunch work whose
child was still running when the supervisor died, and preserve result-bound
approvals across resume.

A supervisor is detached, so it pauses, fails, or finishes with nobody watching.
Pass `--notify room:<name>` or `--notify channel:<name>` to `workflow run` and it
sends one metadata line when it pauses at a gate, fails, succeeds, or times an
agent out — the paused case being the one worth having, since the workflow will
sit there correctly and indefinitely until a human acts.

The target is recorded with the workflow and survives resume, dry-run, and
nested sub-workflows; passing `--notify` on a resume replaces it. Delivery goes
through the `post` CLI and therefore requires the workspace to resolve to a
registered room. A notification that cannot be delivered is recorded as a
`notify_degraded` journal event with a reason and never changes the workflow's
outcome — telemetry that can fail a workflow is worse than no telemetry.

```bash
python3 bin/delegate.py workflow run plan.py --notify channel:machineroom
```

A workflow gate checkpoints the whole workflow tree, not just the nested child
that reached the gate. When a gate closes, the runtime stops admitting new
`agent()` calls tree-wide, drains already in-flight agents to the journal, emits
the gate event, writes `status: "paused"`, and exits the supervisor without
writing `result.json`. Release the gate with **one** of these — not both:

```bash
# Normal continuation after a human checkpoint:
python3 bin/delegate.py workflow approve wf_0123abcdef45

# Equivalent lower-level form (approve delegates to the same resume path):
python3 bin/delegate.py workflow run --resume wf_0123abcdef45
```

`workflow approve` already resumes the supervisor (`emit_approve` →
`emit_run(resume=...)`). Running approve and then `run --resume` races a second
resume against the first and fails with `workflow_locked`. Pick one.

A resume can only take the workflow lock because the previous supervisor is
gone. After gate validation and before the new supervisor starts, a live (not
`--dry-run`) resume cancels every child the previous attempt still had in
flight — the same sealing `workflow kill` does — and journals
`attempt_superseded` with the previous supervisor's pid, its last recorded
status, `supervisorLost: true` when that status was still live, and the sealed
children. The event is written before the launch; a launch failure leaves it
without a following `attempt_config`. A child whose process is already dead is
sealed as `cancelled` with `staleReason: dead_pid` rather than left `stale`, so
the replay relaunches it instead of failing the thunk with "already terminal
(stale)". A dead child whose process group still has members is not sealed:
the resume fails with `workflow_children_unsealed` naming the group, and the
operator decides what those survivors are.

Resume does not adopt a child that is still running: it cancels it and the
replay relaunches that step from the start. Letting a resume wait on a live
orphan and take its result instead is deferred work, not current behavior, and
an earlier version of these docs that said resume adopts running children was
wrong. Adoption covers a child that already finished (or that this supervisor
lifetime is itself still launching), not one that a previous supervisor left
running.

### Pinned runtime and `--repin`

A workflow pins the delegate runtime code it was launched with, and every
resume runs on that pin, so a fix shipped after the launch does not reach the
workflow. `workflow resume` and `workflow status` therefore say so plainly. When
the pinned runtime differs from the live one, both print a notice that names the
pinned runtime (digest, delegate version, and the date it was pinned) beside the
live runtime (digest, version, and the date it was promoted when the live
runtime came from a promotion). JSON mode carries the same facts as
`runtimePin` (`differs`, `pinned`, `live`; on a resume the notice text is also
in `warnings`). `status` prints no notice for a finished (`succeeded` or
`dry_run`) workflow, and reports `runtimePin.checked: false` with the reason
when the pin cannot be read, rather than failing. Nothing changes unless you
ask:

```bash
python3 bin/delegate.py workflow resume wf_0123abcdef45           # keeps the pin, prints the notice
python3 bin/delegate.py workflow resume wf_0123abcdef45 --repin   # moves the workflow onto the live runtime
python3 bin/delegate.py workflow approve wf_0123abcdef45 --repin  # same, when releasing a gate
```

`--repin` is opt-in and applies only to a resume (`workflow resume`,
`workflow run --resume`, `workflow approve`); on a new run or with `--dry-run`
it is refused as `invalid_option_combination`. What it changes and what it
keeps:

- It snapshots the live runtime into the content-addressed pin store, rewrites
  the pin's `runtime` section, and moves the old runtime into `runtimeHistory`
  with `pinnedAt` and `supersededAt`. The rest of the pin (the frozen script,
  arguments, config and profile digests, and workflow environment) is left
  exactly as it was, so personas and configuration stay frozen too.
- The journal is untouched apart from one added `runtime_repinned` event
  (`fromDigest`, `fromVersion`, `fromPinnedAt`, `toDigest`, `toVersion`), written
  after `attempt_config` and as late as the resuming command can write: once the
  supervisor starts it owns the journal. If the launch then fails, the old pin
  is put back and a `runtime_repin_rolled_back` event (`reason`,
  `abandonedDigest`, `restoredDigest`) follows it, so the journal never ends
  claiming a move that did not stay. Step keys are not recomputed: settled steps replay
  as before. The existing key-version check still applies, so a workflow saved
  under a different structural key version is refused for that reason with or
  without `--repin`. A change to how keys are derived must bump
  `WORKFLOW_KEY_VERSION`; `tests/test_workflow_key_stability.py` fails first if
  one is changed without it.
- The identity check still runs against the pin before anything moves
  (`workflow_identity.validate`): a workflow whose credential namespace or
  profile no longer matches what it was launched under is refused as
  `workflow_profile_drift`, as on any resume.
- The new pin is written beside the old one as `pin.json.staged`, loaded back
  and validated there, and only then renamed over `pin.json`, so a replacement
  that does not validate never becomes the pin. Before the rename the old pin is
  copied to `pin.json.pre-repin`. The supervisor started on the new pin deletes
  it, durably, before it runs any step; a supervisor that cannot delete it
  fails without running anything. If the resume fails first, the backup's
  bytes are restored over `pin.json`. A backup still on disk therefore always
  means nothing ran on the new runtime: the next resume (not a `--dry-run`)
  puts the old pin back under the workflow lock and, when the journal still
  says the workflow moved, adds `runtime_repin_rolled_back` with
  `reason: interrupted`. A failed `--repin` therefore leaves the workflow as it
  was. Until that recovery runs, `workflow status` reads the pin on disk, which
  is the new one.
- If the pinned runtime already equals the live one, `--repin` changes nothing
  and says so.

`--repin` is refused, with the reason, in these cases: a child of the workflow
is still running (`repin_children_running`, naming the run ids and pids; stop
it with `workflow kill` or wait for it, because a resume would cancel it and
the runtime would change underneath it in the same command). A child that has
not published its pid yet counts as running while its launcher is alive or,
with no launcher on record, for five minutes after its last activity; another resume
holds the workflow lock (`workflow_locked`); the existing pin cannot be
verified (a pin from before the runtime files were recorded fails to load, and
so cannot be resumed either); or the identity check above fails.

The supervisor handles `SIGTERM`, `SIGHUP`, and `SIGINT` the way it handles a
stall watchdog fire. (`SIGINT` used to reach the interpreter's default, which
raised `KeyboardInterrupt` and marked the workflow failed without cancelling its
children; it is relayed now like the others.) The signal handler only records the signal; a relay thread fed
by the process's wakeup pipe journals `supervisor_signalled` with the signal
name and pid and sets the cancel event, so nothing in the handler ever waits on
a lock. Admission closes, in-flight children are cancelled, structured-retry
worktrees are released, and the supervisor exits through its normal cleanup
with `status: failed`, `signal`, `watchdogReason: signal:<NAME>` (a genuine
watchdog reason that started the shutdown keeps both its reason and its error
text), and the `cancelled` list on `status.json`. Before any terminal status is
chosen or published the supervisor drains the relay — a marker byte through the
same pipe, acknowledged once every earlier signal byte has been delivered — so
the status `workflow wait` and `workflow watch` treat as final already carries
the signal. Each drain waits for its own marker, so a marker left behind by an
earlier drain that timed out cannot answer a later one. When the relay does
not acknowledge the drain before publication (it is inside a journal write
that has not returned), the terminal status carries
`signalDrainIncomplete: true` — the signal fields may be missing — rather
than reading as a settled record; `workflow wait` and `workflow watch` repeat
that as a warning line in text mode and as the same field in JSON. The
workflow lock is released only once the relay can no longer write: at exit the
supervisor fences the relay (a fence is taken only when no delivery is in
flight), an in-process supervisor waits for that however long a delivery
takes, and the dedicated supervisor process (only the one `workflow run`
detached, marked by `DELEGATE_WORKFLOW_SUPERVISOR_PROCESS`, which child rows
never inherit) waits a bounded time and then ends itself unconditionally
while it still holds the lock, so the relay dies with it (the journal reader
tolerates an unterminated final line) and no journal writer ever outlives
ownership. Every exit from the supervisor's lock context, exceptional or
not and wherever the exception struck, passes through the relay: it fences
later deliveries (admission and fencing are atomic), restores the handlers,
and hands the lock descriptor to any delivery still writing, which closes it
when it completes — so no interruption can release ownership early. Arming
the relay is transactional too: a failure before its reader thread exists
puts the handlers and wakeup fd back and closes both descriptors, and the
read end is owned by whichever of the reader and the rollback claims it
first, so an exception escaping the thread start never closes it underneath
a reader. A signal that arrives after the last child has completed does not
interrupt anything; the workflow finishes `succeeded` with `signal` and
`signalAfterCompletion: true` recorded. A repeated signal is recorded in
`signalsRepeated`, never re-raised. `workflow kill` still sends `SIGTERM`
first and escalates to `SIGKILL` after five seconds; it notes which children
were already terminal before signalling and completes its `cancelled` report
from the whole group afterwards (`observedIn: registry` on entries it did not
cancel itself — the registry says the child was cancelled during the kill,
not who did it), then writes `killed` with only the signal's own diagnostics
removed. `SIGKILL`
cannot be handled; the next resume records it as `supervisorLost` and seals
whatever was left behind.

This preserves in-flight sibling results for replay while preventing unrelated
siblings from starting after a human checkpoint has requested control.

A child record that never published a pid (a launch whose isolation or
process start never completed, so it sits at `running` or
`creating_isolation` with no pid) is refused while it may still be launching.
Resume seals it as `cancelled` with `staleReason: missing_pid`, instead of
failing with `workflow_children_unsealed`, only when both hold: it has shown no
activity for 300 seconds, and its launcher is verifiably gone. A
`creating_isolation` record names its launcher (`launcherPid`); the launcher
counts as gone when that pid is dead or now belongs to a process that started
after the run. A record that names no launcher relies on the 300-second window
alone. The seal also stamps `cancelRequested`, and the runner re-reads the
record under the registry lock before every process start and before publishing
a pid, so a launcher that outlives the seal refuses to start a child for it
(`cancelled_by_user`) rather than running a second child for the step.
A sealed record reads as `cancelled`, never as `failureKind: runner_lost`;
`runner_lost` is what `wait` reports for a running record whose pid is missing or
dead and that nobody has sealed.

Adoption follows the same rule: when a key's latest child run is cancelled,
stale (dead pid), or sealable as never launched, the call journals
`agent_adopt_skipped` with the reason and relaunches instead of waiting on it or
failing. A never-launched run that this supervisor lifetime started is a
sibling still launching, so adoption waits on it instead of sealing it.

### Gate actions

`park_gate(key, result=None, actions=["retry", "accept"])` records a `gate`
event carrying `gateName` (the key) and `actions`, then pauses the workflow the
same way `workflow(gate=True)` does. The operator chooses:

```bash
python3 bin/delegate.py workflow approve wf_0123abcdef45 \
  --gate task-7-park --action retry --note "fixture fixed" --data '{"attempt": 2}'
```

The resumed script reaches the same call, and it returns
`{"gate": "task-7-park", "action": "retry", "note": "fixture fixed", "data": {"attempt": 2}}`
and journals `gate_decided`. `actions` defaults to `["approve"]`. `approve`
refuses an action the gate did not declare and names the allowed ones, and it
refuses a bare approve on a gate whose actions exclude `approve`. `--gate`
selects an unapproved gate by its `park_gate()` key or journal key; without
it, approve takes the latest unapproved gate as before. A bare approve stores
exactly what approvals always stored, and the call returns action `approve`.
As with every gate, the approval is bound to the gate's result hash: if the
resumed script parks the same key with a different result, it pauses again.
The same holds when the recorded action is one the resumed script no longer
declares: the call journals `gate_action_undeclared` and parks again under a
new question bound to the result and the actions now offered, so
`approve --gate KEY` sees it as pending and validates against the current list.
A recorded answer still offered by the current action list is reused, and the
runtime does not track recency across action-set changes: a gate whose list
changes A, then B, then A again reuses the answer recorded for A rather than
asking again.
Two live `park_gate()` calls with one key in one named scope (for example two
parallel items that both ask `"review"`) would share one answer, so the second
raises `WorkflowKeyConflict` with the same guidance as a reused `agent(key=)`:
include the item id in the key. A call that has returned releases its key, so a
loop may ask the same key again with a new result.
In a dry run, `park_gate()` returns its first declared action with
`dryRun: true`.

A completed dry-run can also become a live run without creating a second
workflow record:

```bash
python3 bin/delegate.py workflow run --resume wf_0123abcdef45
```

Its simulated events remain in `journal.jsonl` for audit, but resume ignores
them as cached results and resets simulated budget before launching live agents.

### Dry-run working directory

A dry run executes the script in the invoking process with its working
directory set to the `--cwd` workspace, the same directory a real run's
detached supervisor uses. Scripts that read plan state or git heads by relative
path see the same files in both modes. The previous directory is restored when
the script finishes. A dry run that exceeds `workflows.dryRunTimeoutSeconds`
fails with `dry_run_timeout`, and its abandoned script thread cannot be stopped,
so the process keeps the workspace as its working directory rather than moving
the script's relative paths out from under it.

### Dry-run write warning

A workflow dry-run stubs **agent calls only**. Filesystem writes made by the
script itself are live and can change the checkout or other state. Scripts that
write state should branch on `dry_run` (or its `is_dry_run` alias), or run from a
disposable checkout. The CLI includes this warning in dry-run output. The
runtime's own dry-run bookkeeping writes nothing but its journal: a predecessor
adoption that a dry run skips, refuses, or completes leaves that run's
temporary structured-retry workspace in place for the live path to reap, so the
scratch tree an operator is reading survives the dry run.

## Limits

Workflow scripts are intentionally capped:

- Script size: 1 MiB.
- Nesting depth: 3 workflow levels.
- Lifetime `agent()` calls per workflow tree: 1000.
- `pipeline()`/`parallel()` item count: 4096.

These caps protect local machines from accidental fan-out and keep resume state
bounded. The caps are hard validation/runtime errors, not warnings.

## Config

The top-level `workflows` config block controls concurrency and schema retries:

```json
{
  "workflows": {
    "engineCaps": {"codex": 4, "claude": 2},
    "itemThreads": 64,
    "structuredOutputRetries": 2
  }
}
```

- `engineCaps`: optional per-engine concurrent child-run caps. Keys are Delegate engine names; values are positive integers. Engines without a cap are unconstrained by this setting.
- `itemThreads`: maximum concurrent workflow item worker threads across `pipeline()` and `parallel()`. Positive integers override the default; `0` or a missing value falls back to the default.
- `structuredOutputRetries`: retry count for `agent(schema=...)` validation failures. Retries include the previous invalid output and validation error as correction context.

Failed child output is diagnostic only, even when its partial text or completion
report validates against the schema. `agent()` and `followup()` return `None`
after a terminal failure or an exhausted retry budget. The child run retains its
partial output; `agent_attempt_failed` journal entries retain the child's exact
`failureReason` and run identity.

With `agent(..., on_failure="typed")`, an exhausted structured call returns an
`AgentFailure` instead of `None`. It is falsy, so `if not result:` still works,
and it carries `failure_kind`, `failure_reason`, `attempts`,
`last_parsed_candidate` (the most recent parsed but invalid value, kept across
a later child failure), `candidate_present`, `validation_error`, `run_id`,
`engine`, `served_model`, and `served_provider`. The default stays `None`
because compiled workflows test `result is None`. The
`agent_structured_exhausted` journal event records the same `attempts`,
`failureKind`, and candidate either way.

### Provider outcomes and the stage stop

`AgentFailure` also carries `provider_error`, the child's structured
`providerError` (`signature`, `class`, `hint`, and the redacted `status` and
`message`; see [troubleshooting](troubleshooting.md#provider-errors-known-bad-lanes-and-automatic-resume)),
so a script can branch on the signature instead of parsing prose. Two failure
kinds are specific to this layer, both returned as an `AgentFailure` with
`on_failure="typed"` (`capabilities["providerOutcomes"]` advertises them):

- `lane_known_bad`: the child refused to launch, with no process started,
  because its lane carries a known-bad marker (`--force-launch` is not passed by
  workflows). `provider_error` is the marker's signature.
- `provider_exhausted`: the call was skipped, with no child run at all
  (`run_id` is `None`), because its stage already stopped launching on the lane.

A stage is one `phase()` label; a lane is one engine on one model. When the
first `providerErrors.stageStopAfter` results (default 3; `0` turns the stop
off) of a stage on a lane all failed with the same persistent, lane-scoped
signature (for example `auth_rejected`), the rest of that stage's calls on that
lane are not launched. Cells already queued behind the agent cap skip as well,
and a call with an engine fallback list moves on to its next engine. A success
or any different result among the first results means the lane is not uniformly
bad, and the stage keeps launching. Transient and request-scoped failures never
count. The journal records the trip once as a durable `stage_lane_stopped`
event (stage, lane, signature, count, hint) and every skipped call as
`agent_lane_skipped`. Because a skipped call is not a run, it still counts
against the workflow's agent budget.

Workflow children run unresumable unless the call passes `resumable=True`, so
the automatic resume after a transient stream drop (see troubleshooting) applies
to a workflow child only in that case.

Structured parsing accepts raw control characters inside JSON strings. When
stray control characters make a document invalid JSON, they are stripped and the
document is parsed again.

Only `timeout`, `agent_timeout`, `call_timeout`, `stall`, and `stalled` are
transient at the workflow layer. Provider errors, refusal, max-turns, usage/auth
failures, cancellation, output caps, and unknown failures stop without relaunch:
provider retries and credential rotation already belong to the child harness.
Successful children with invalid structured output still receive correction
retries, and so does a child that exited 0 without assistant text
(`empty_result`). Each permitted retry waits `min(30, 2**retry_index)` seconds multiplied
by a fresh uniform factor in `[0.75, 1]`, with the first index zero; workflow
cancellation interrupts the wait. `retries=N` is an upper bound of `N+1` child
attempts, not a promise to repeat terminal failures.

A structured retry on Codex or Claude resumes the failed child's native session
to ask for the structured result alone. When that resumed launch cannot find the
session (failure kind `session_lost`, reason `session_expired`; typically the
launcher landed on a different account than the one holding it), the call falls
back to a fresh relaunch in the same worktree instead of ending. The fallback is
journaled as `agent_structured_retry` with `strategy: "relaunch"` and
`fellBackFrom: "resume"`, and it does not spend a retry. A fresh child would redo
the task, so the fallback relaunches only over a worktree known to be untouched;
otherwise the call is refused (`agent_structured_retry_refused`). The reason is
`work_changed_session_missing` when an earlier attempt already changed the
worktree, and `work_state_unverified` when that could not be checked: the work
summary's `fileInspectionStatus` or `commitInspectionStatus` is not `verified`
(for example `git status` failed, which otherwise reads as zero changed files),
its summary is missing or predates `fileInspectionStatus`, or a work-mode child
has no summary at all. Safe and call children run in temporary workspaces with
no summary and are not held to this.

The supported schema subset includes `minLength` for strings and `minItems` for
arrays. Both take non-negative integers and are enforced recursively. `required`
only requires a property to exist, so use `minLength: 1` or `minItems: 1` when
the contract explicitly requires a non-empty value.

`additionalProperties` may be `false` (closed object) or a schema, which types a
map whose keys are not known in advance (`{"type": "object",
"additionalProperties": {"type": "boolean"}}`). Extra keys are validated against
that schema.

Child output is parsed tolerantly: a markdown report whose last fenced block is
the JSON result is fine, and decoy brackets in the prose (a `[T1]` tag, a
`{run, exit, note}` contract line) are skipped in favour of the last value that
validates against the schema.

## Nested workflow references

Explicit CLI script paths and saved workflow names are accepted at launch. Nested
`workflow()` references are narrower: they may name a saved user-library script
under `~/.delegate/workflows/`, use an absolute path inside that same saved
library, or use a path inside the parent workflow's pinned script directory.
Other filesystem paths are rejected at resolution time.

Saved workflows live only in the user library
`~/.delegate/workflows/<name>.py`; project-level workflow libraries are
intentionally deferred.

## Patterns

- Use `safe` or `work` lanes for agents that inspect or modify the tree.
- Use `call --read-only` lanes for judge/verify steps that should not touch files.
- `call` is the default mode for `judges()`, and workflow call-mode children are read-only by default.
- `agent(model=...)` selects the child model per-run; every engine now supports it via `--model`.
- `passthrough=True` is incompatible with `schema=` and with `mode="call"`; slash pass-through needs a work lane or an argv-enforced-safe lane.
- Prefer `agent(phase="...")` under concurrency; global `phase()` is intentionally racy like Claude's workflow primitive.
- Use `--budget N` for run-count control. `budget.spent()` and `budget.remaining()` are available inside scripts. Dry-runs simulate budget ticks but do not consume real budget.
- Use `schema=` when a stage must return structured JSON. Claude receives every
  supported schema natively through `--output-schema`. Codex uses native
  `--output-schema` only when the schema is strict-compatible (every object
  node lists all properties in `required`, `additionalProperties` absent or
  `false`, and no object node is free-form, i.e. every `{"type": "object"}`
  declares `properties`, including array `items`; strict mode would close a
  bare object to zero keys so the model could only emit `{}`); any other Codex
  schema — optional fields, typed maps, bare objects — and every
  other engine take the prompt-and-parse path, with validation retries. A
  schema is never silently rewritten to satisfy strict mode. The journal row
  `agent_schema_prompt_path` carries the reason (for a bare object it names the
  schema path). When the schema root is an array and the child answers with a
  one-key object wrapping a valid array (`{"items": [...]}`), the array is
  unwrapped and an `agent_output_unwrapped` journal row (`wrapperKey`,
  `warning`) records it; a two-key object or a non-validating array still
  fails validation. The subset
  validator rejects empty or duplicate `enum` values and repeated `required`
  or `type` entries, which Claude's own schema preflight refuses at launch.

## Cross-family parallel review

Workflows are user-authored scripts; Delegate does not ship a built-in review
workflow. This compact recipe uses each harness's configured default model, or
model IDs/aliases supplied through `args`, so it does not depend on private
aliases:

```python
meta = {"name": "cross-family-review"}

engines = args.get("engines", ["codex", "claude", "cursor"])
models = args.get("models", {})
prompt = args["prompt"]


def reviewer(engine):
    return lambda: agent(
        prompt,
        engine=engine,
        mode="safe",
        model=models.get(engine),
        phase=f"Review ({engine})",
    )


reviews = parallel([reviewer(engine) for engine in engines])
return {"reviews": dict(zip(engines, reviews))}
```

Save it as (for example) `review.py`, optionally cap each family with
`workflows.engineCaps`, and preview its exact routing before launch:

```bash
python3 bin/delegate.py --json workflow run review.py \
  --args '{"prompt":"Review the current changes; return prioritized findings."}' \
  --dry-run
```

The preview's `runTree.calls` records each call's resolved `model`, `effort`,
`fast`, `isolation`, and UTF-8 `promptBytes` (the raw user prompt plus resolved
persona text). Cursor, Kimi, and OMP calls whose user-plus-resolved-persona input
exceeds 102400 bytes carry a warning; the final materialized prompt is checked
again before exec.

## CLI

```bash
python3 bin/delegate.py workflow check review.py
python3 bin/delegate.py --json workflow run review.py --args '{"files":["src/cli.py"]}' --budget 10
python3 bin/delegate.py workflow events wf_0123abcdef45 --since 12
python3 bin/delegate.py workflow watch wf_0123abcdef45 --since 12
python3 bin/delegate.py workflow watch wf_0123abcdef45 --jsonl
python3 bin/delegate.py workflow wait --timeout 60
python3 bin/delegate.py workflow result --field summary
python3 bin/delegate.py workflow approve wf_0123abcdef45
python3 bin/delegate.py workflow approve wf_0123abcdef45 --gate task-7-park --action retry
python3 bin/delegate.py workflow kill wf_0123abcdef45
python3 bin/delegate.py workflow save review.py --name review-changes
```

A workflow lives in the workspace that launched it. Asking for a `<wfId>` from
another directory used to be a dead end; now the id is looked up on the roster
of workspaces Delegate has launched in (`~/.delegate/registries.json`). The
read-only actions (`status`, `events`, `watch`, `wait`, `result`) read a
workflow found in exactly one other workspace directly, and `status`, `wait`,
and `result` say so with `resolvedWorkspace`; the next actions `status` suggests
already carry the right `--cwd`. `approve`, `reject`, `kill`, and
`run --resume` change state, so they never follow: they fail with
`workflow_not_found`, name the workspace, and print the exact
`delegate --cwd <workspace> workflow <action> <wfId>` to run. An id found in
several workspaces is listed rather than guessed. Child runs are ordinary runs
of the workflow's workspace, so the run-level lookup rules in the CLI reference
apply to them.

`workflow run --env NAME=VALUE` and `--env-file PATH` (both repeatable, launch
only) record a workflow-level env in the workflow directory's private
`workspace-env.json`. Every `agent()` child of every attempt receives it
where a child can take workspace env (mode `work` with
`isolation="worktree"`), beneath the call's own `env=`. It travels as
workspace-spec data in the child's run input, never as the child Delegate
process's own environment, so a lane that cannot take env (safe mode, call
mode, work without a worktree) simply does not receive it; such a call is not
an error, and its replay key is unchanged by a workflow-level env. The launch
values are what a resume replays, rather than whatever the resuming shell
exports: a worktree child records the merged env in its own run, so a manual
`delegate resume` of that child replays workflow-level values that the call's
own `env=` did not override.
`workflow resume` and `workflow run --resume` refuse `--env`. The script's own
process does not see these variables.

`workflow run` detaches its supervisor and returns, so the invoking process ends
immediately. Run it under a systemd unit only with `KillMode=process`
(`systemd-run -p KillMode=process ...`): the default `control-group` reaps the
detached supervisor with the rest of the cgroup about half a second after that
exit, while the unit still reports `Result=success` and `ExecMainStatus=0`, so
`journalctl` looks clean and only `workflow status` reports the stalled
supervisor. A launch that detects it is inside such a unit warns by name at
launch and in JSON `warnings`.

`status`, `list`, and `wait` include the same additive `decision` projection
for each workflow: status, gate key/result hash, error, budget, and suggested
`nextActions`. The full status remains available. Suggestions do not grant
approval or change exit codes: `wait` succeeds for succeeded/paused workflows,
returns 1 for failed/killed/stalled/dry-run workflows, and 124 on timeout.
Implicit latest selection still excludes dry runs; explicit IDs do not.

`watch` reads new journal bytes rather than reparsing the complete journal on
each poll. It retries an incomplete final line when more bytes arrive; malformed
newline-terminated records remain errors. Replacement, shrink, and changed
cursor-boundary bytes restart the reader. The sequence watermark remains in
effect across restarts, so records at or below `--since` or the last emitted
sequence are not replayed. In-place edits far behind the cursor are not an
append operation and are not monitored.

Existing `--json` watch output remains one final envelope containing `events`
and `lastSeq`. For long-running watches, `--jsonl` emits and flushes one record
at a time without retaining an event history:

```json
{"type":"event","schema":"delegate.workflow-command.v1","event":{"seq":13,"type":"agent_started"}}
{"type":"final","schema":"delegate.workflow-command.v1","ok":true,"lastSeq":13,"workflow":{"status":"paused"}}
```

The final record includes the complete status projection (abbreviated above).
As with existing watch behavior, its `ok` is false only for a stalled supervisor;
it is not a claim that a terminal workflow succeeded. JSONL takes precedence
over global `--json`. While a writer is active, an unterminated tail is buffered.
Once settled, valid JSON without a final newline is emitted; an incomplete
JSON or UTF-8 tail is ignored with a warning. Malformed complete records remain
errors.

Approval recovers journal-backed gate evidence under the supervisor lock. If a
supervisor is still draining, a rejected approval attempt does not rewrite its
status or approval file. The existing short post-launch stabilization wait is
unchanged; approval remains bound to the gate's result hash.
