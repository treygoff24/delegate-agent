# Delegate Workflows

Delegate Workflows are Python workflow scripts executed by a detached supervisor
on top of normal Delegate runs. A workflow gets its own registry at
`.delegate/workflows/<wfId>/`; every `agent()` child is an ordinary run tagged
with `--group <wfId>`, so `runs`, `snapshot`, `run-output`, `wait`, and
`cancel` keep working on child runs.

Workflow registries use this file set as needed:

- `script.py`: pinned workflow source for this run.
- `args.json`: launch arguments supplied with `--args`.
- `journal.jsonl`: append-only workflow events.
- `status.json`: current supervisor/status snapshot.
- `result.json`: final workflow result, present only after success.
- `approval.json`: gate approval state, present after `workflow approve`.
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
- **Adoption**: on resume, recognizing a child run that already exists in the
  run registry for a started-without-result key and taking its outcome instead
  of respawning a duplicate.
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
workflow result in `result.json`. Injected globals are `agent`, `pipeline`,
`parallel`, `phase`, `log`, `workflow`, `judges`, `args`, and `budget`.

## Core DSL

- `agent(prompt, engine=None, mode=None, model=None, effort=None, schema=None, label=None, phase=None, isolation=None, passthrough=False, timeout=None, retries=None, fast=None, persona=None, allow_repo_persona=False)` launches a real Delegate child run and returns parent-facing output, a validated schema object, or `None`. `fast=True` requests Codex Fast, `fast=False` requests Standard, and `None` inherits; non-Codex fallback candidates ignore this Codex-only preference. `persona` resolves one named persona from the source workspace; `allow_repo_persona=True` opts into workspace-local personas in safe mode.
- `pipeline(items, stage1, ...)` runs per-item stage chains with no inter-stage barrier. A throwing stage drops that item to `None` and skips later stages for that item.
- `parallel([lambda: ...])` is a barrier and preserves order. Ordinary item failures become `None` slots; gate checkpoints propagate to the supervisor.
- `phase(title)` emits a progress event.
- `log(message)` emits a JSON-safe log event.
- `workflow(name_or_path, args=None, gate=False)` nests another workflow. Use `gate=True` or `gate="on-failure"` for approval checkpoints.
- `judges(prompt, schema, engines=[...])` runs one `call --read-only` judge lane per engine and returns the votes.

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

## Gates and resume

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

This preserves in-flight sibling results for replay while preventing unrelated
siblings from starting after a human checkpoint has requested control.

## Limits

Workflow scripts are intentionally capped:

- Script size: 512 KiB.
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

The supported schema subset includes `minLength` for strings and `minItems` for
arrays. Both take non-negative integers and are enforced recursively. `required`
only requires a property to exist, so use `minLength: 1` or `minItems: 1` when
the contract explicitly requires a non-empty value.

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
- Use `schema=` when a stage must return structured JSON. Codex uses native
  `--output-schema`; every object node must list all properties in `required`,
  and Delegate supplies missing `additionalProperties: false` in the temporary
  schema with a warning. Other engines get schema instructions and validation
  retries.

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
`fast`, `isolation`, and UTF-8 `promptBytes`. Cursor and Kimi calls whose prompts
exceed 102400 bytes also carry a warning because those harnesses transport the
prompt in argv.

## CLI

```bash
python3 bin/delegate.py workflow check review.py
python3 bin/delegate.py --json workflow run review.py --args '{"files":["src/cli.py"]}' --budget 10
python3 bin/delegate.py workflow events wf_0123abcdef45 --since 12
python3 bin/delegate.py workflow watch wf_0123abcdef45 --since 12
python3 bin/delegate.py workflow wait --timeout 60
python3 bin/delegate.py workflow result --field summary
python3 bin/delegate.py workflow approve wf_0123abcdef45
python3 bin/delegate.py workflow kill wf_0123abcdef45
python3 bin/delegate.py workflow save review.py --name review-changes
```
