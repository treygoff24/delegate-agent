# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `runs` and `ps` show how long a running Run has left: a running Run with a
  timeout carries `deadlineAt` (recorded by the runner when its timeout clock
  starts, after workspace setup) and `remainingSeconds`, and the text listing
  prefixes `current` with `12m left`. Terminal Runs carry neither.
- **A live Run shows the tool call it is waiting on.** While a tool call is in
  flight, `runs` and `snapshot` carry `pendingTool` (`name`, optional
  `target`, `startedAt`, `seconds`; the oldest pending call, redacted like
  `current`), and after a minute `current` reads `waiting on tool <name> for
  13m`. It is refreshed whenever the pending set changes, tracked even with
  `--stall-minutes 0`, and dropped from terminal records. The stall watchdog
  still never fires while a tool is pending: a hung tool is surfaced, not
  killed.
- A followup or session resume whose child exits non-zero within 15 seconds
  with no stdout or stderr stays `child_failed`, but the message says the
  saved native session probably could not be loaded and `nextActions` offers
  `delegate resume <handle>`.
- **Early-stop detection.** A succeeded Run whose last sentence announces a
  next step ("Now let me write my report.") carries `degraded:
  ended_announcing_next_step`, and a work Run that parks on an approval
  question with no changed files carries `degraded: ended_awaiting_input`.
  Status stays `succeeded`; the warning names `delegate resume <handle>`. A
  finished deliverable that ends with "Should I proceed?" is not flagged.
- **Unread coordinator mail is surfaced at run end.** A terminal work Run
  records `unreadMail` (count, up to three id/sender/subject rows, and an
  `unreadable` count for damaged or non-regular inbox entries) plus a warning
  on the state, envelope, completion report, snapshot, `wait`, and `cancel`
  reply. Messages a push hook already injected count as read. The mail prompt
  suffix now asks lanes to read their inbox before final verification or
  commit. Launch, dry-run, and manifest carry `mailInbox` (host and mail
  root).
- `followup` and `resume` inherit `--mail-push` from the source Run and accept
  `--no-mail-push`; `followup` also accepts `--mail-push`.
- **One automatic fresh rerun for transient safe-mode failures.** A `safe`
  Run that fails with `stream_disconnected` or `provider_unavailable`, or a
  `safe` launch the broker refuses (`broker_binding_inactive`) with no child
  output, is rerun once as a new Run recorded under `autoResume` with `kind:
  "rerun"` and the first attempt's error. The first broker refusal defers the
  known-bad lane marker (`laneMarkerDeferred: "broker_binding_retry"`). Work
  Runs are never rerun this way. `providerErrors.autoResume: false` turns it
  off.
- **`<engine>.enabled: false`** hides a harness from `models`, `describe`,
  `capabilities`, and `capabilities refresh`, and any launch of it fails with
  `harness_disabled` naming the key.
- **Claude model typos are refused before launch.** A typed `--model` that is
  a family word plus a version (`opus-5.5`, `Sonnet 5`) or names no model
  (`claude-`, an empty `[]`) fails with `invalid_alias` and the closest valid
  values, on the CLI, dry runs, and `run --input-json`. Provider names
  (Bedrock ARNs, Foundry deployments, gateway strings) still launch, and the
  version-typo refusal is skipped when a Bedrock, Vertex, Foundry, or
  `ANTHROPIC_BASE_URL` gateway variable is set.
- Workflow status says why and what next: a paused workflow carries a `pause`
  object (gate, actions, last failure, rejection text) or a `softPark` reason
  naming `delegate workflow resume <wfId>`; agent journal rows always carry
  label and item, `agent_started` records `timeout` and `deadlineAt`,
  `agent_timeout` names `nextEngine`, and status lists recent timeouts.
- Codex structured output: a root-array answer wrapped in a one-key object is
  unwrapped on the prompt path and journaled as `agent_output_unwrapped`.
- Worktree warnings name the files: `dirty_source_auto_included_paths`,
  `dirty_source_included`/`dirty_source_included_paths` for explicit
  `--include-dirty`, `dirty_source_mirrored` for safe worktree launches, and
  `dirtySourcePreview` in dry runs (up to five paths each).
- `workflow resume` and `workflow status` say when a workflow's pinned runtime
  differs from the live one. The notice names both runtimes (digest, delegate
  version, and the pin or promotion date), and JSON carries the same facts as
  `runtimePin` (`differs`, `pinned`, `live`; on a resume the notice text is
  also in `warnings`). `status` prints no notice for a finished (`succeeded`
  or `dry_run`) workflow, and a pin that cannot be read reports `checked:
  false` with the reason instead of blocking the resume.
- `--repin` on `workflow resume`, `workflow run --resume`, and `workflow
  approve` is an opt-in that moves a workflow onto the live runtime; a plain
  resume still keeps the pin. It keeps the journal, step keys, frozen script,
  arguments, config, personas, and profile identity, moves the old runtime
  into `runtimeHistory`, and journals one `runtime_repinned` event. The
  identity check still runs first, and the new pin is written aside and
  validated before it replaces the old one. A failed validation, a failed
  launch, or a resume where no supervisor ever starts on the new pin restores
  the old pin byte for byte and journals `runtime_repin_rolled_back`.
  `--repin` is refused with `repin_children_running` while a child run is
  running or still starting, with `invalid_option_combination` on a new run or
  with `--dry-run`, with `workflow_locked` when another resume holds the lock,
  and when the existing pin cannot be verified. `WORKFLOW_KEY_VERSION` remains
  the guard against step-key drift. A script that branches on `capabilities`
  can take a different branch after a repin, because the map then describes
  the live runtime.
- `workflow check` warns about unkeyed steps in a script that keys others.
  When some `agent()`, `parallel()`, `pipeline()`, or `workflow()` calls pass
  `key=` and others do not, it emits one `keying warning` per primitive kind
  with the source lines of the unkeyed calls (at most eight), because those
  calls still replay by position and prompt and lose settled work when a
  resumed script shifts. A script that keys nothing is left alone, a call with
  a `**kwargs` expansion is assumed to carry its key, and `judges()` and
  `followup()` are never flagged.
- A run or workflow id that is not in the current workspace is looked up in a
  small roster of the other workspaces Delegate has launched in
  (`~/.delegate/registries.json`, private, at most 256 entries, most recently
  used first; workspaces whose `.delegate` is gone are skipped). `snapshot`
  and `run-output` read a run id found in exactly one other workspace directly
  and report `resolutionKind: "cross_registry"`, `resolvedWorkspace`, and a
  `cross_registry:` warning. `wait`, `cancel`, `resume`, `followup`, and
  `worktree show` never act across workspaces: they fail with
  `unknown_handle`, name the workspace, and put the exact `delegate --cwd
  <workspace> ...` command first in `nextActions`. The workflow read actions
  `status`, `events`, `watch`, `wait`, and `result` read a workflow found in
  exactly one other workspace, and `status`, `wait`, and `result` add
  `resolvedWorkspace`; `approve`, `reject`, `kill`, and `run --resume` fail
  with `workflow_not_found` and the exact command instead. An id found in
  several workspaces is listed, never guessed, and a numbered alias is never
  auto-resolved: the error lists every workspace that has it. Only workspaces
  used for a launch after the roster shipped are known.
- OMP `tool_execution_update` records whose `args` or `partialResult` is
  oversized are shrunk to a `{"delegateCompacted": true, "originalBytes": N,
  "head": ..., "tail": ...}` stub. The sub-agent `task` tool re-emits its
  whole shared context on every update; `tool_execution_start` and
  `tool_execution_end` carry the full arguments and result once and are never
  touched. `stdoutCapture` (policy `omp-capture-v2`) reports
  `compactedToolUpdateRecords` and `compactedToolUpdateBytes`, the run gets a
  warning, and `--raw` cannot recover the compacted text.
- Tracked Pi, OMP, and OpenCode runs ingest token `usage` from the stream,
  summed per message or step and not double counted.
- Run and call envelopes, run summaries, and snapshots carry `servedModel`
  and, when the harness reports one, `servedProvider`: what the child's stream
  says actually answered. They sit beside `modelResolved` (the request),
  repeat inside `modelProvenance`, and are absent when the harness reported no
  model. A Claude call reports only a per-run `modelUsage` total, so its
  `servedModel` is set only when exactly one model produced output; with
  several it is absent and a pinned call warns `pinned_continuity_unverified`.
  An ungrouped `call` is now held to the same continuity rule as a tracked
  run: a pinned call whose stream names another model or provider fails with
  `model_continuity_paused` (text mode prints no answer text for it; JSON
  keeps any text beside the error), and a fungible or panel call carries the
  `model_substitution` warning, provider included.
- A tracked run whose child ends its turn mid-job is flagged as degraded. A
  headless child ends when its model stops, and background Bash tasks and
  Monitors die with the session, so a Claude work run that said "Waiting on
  the full gate" used to be recorded `succeeded` with `ok: true` while its
  gate never finished. The status stays `succeeded` so the work can be
  adopted, but the run now carries `degraded: true` (present only when true),
  a `degradedReason`, `degradedEvidence` (bounded, redacted lines saying what
  was seen), and a `degraded=...` warning. They appear on the launch envelope,
  in state.json, on `wait` (JSON, `--structural`, and a `degraded:` line in
  text), `snapshot`, `runs` and `ps` (the text table prefixes `current` with
  `[degraded]`; `--structural` omits them), the `run-output
  --completion-report` view, and, for a workflow child, the `agent_child`
  journal event and `agent_meta()` (`degraded`, `degradedReason`). Nothing
  fails a run or a workflow step, and `wait` still exits 0. `degradedReason`
  is `ended_waiting_on_background_work` or
  `background_work_unfinished_at_exit`. The first is a final-message check
  that works for every engine: a short message that is not shaped like a
  finished report and says the child is waiting on, or will act after,
  unfinished work of its own. It ignores waiting on the requester ("I'll
  commit when you approve"), a third party's independent result ("Waiting for
  CI to post its result"), denials, and quoted text such as a tool's output; a
  short report-shaped message matches only an explicit present-tense statement
  that the child's own job is still running; runs with `--output-schema` are
  not text-checked. The second reads Claude Code's `background_tasks_changed`
  snapshot at the result event and catches a finished-looking report that
  abandoned a running task; only Claude emits that event.
- A provider failure is now data. The failed run's envelope, run state, and
  completion report carry a structured `providerError` (`status`,
  `providerCode`, `message`, `engine`, `signature`, `class`, `hint`); the
  message is bounded, credentials are redacted, and email addresses are
  masked. `class` is `persistent`, `transient`, or `unknown`, and a persistent
  signature is either lane-scoped (credentials, billing, or model access are
  wrong) or request-scoped (this prompt is the problem). One engine-keyed
  signature table classifies by HTTP status first, then provider code, and
  uses text patterns only as the fallback, so `failureReason`, `failureKind`,
  the hint, and the class agree, and an OMP HTTP 400 naming an image count or
  size limit is `request_image_limit`, never an auth failure. The signature
  list is in `docs/troubleshooting.md`. Two new `failureKind` values are
  `lane_known_bad` (a launch refused, below) and `provider_exhausted`
  (workflow only).
- A persistent, lane-scoped provider failure (rejected credentials, no credit,
  usage limit, no access to the model, missing API key) marks its lane
  known-bad for `providerErrors.knownBadLaneMinutes` (default `15`; `0` turns
  markers off). Until the marker expires, another launch on that lane is
  refused in milliseconds, before any worktree or child exists, with
  `lane_known_bad` and the new exit code `4`; the payload carries `signature`,
  `class`, `hint`, `expiresAt`, `secondsLeft`, and `markedRunId`. The global
  `--force-launch` launches anyway (another persistent failure renews the
  marker), a success clears the marker, and `--dry-run` never refuses.
  `delegate doctor` lists live markers as `knownBadLanes`. Transient,
  request-scoped, and unclassified failures never mark a lane, nor do endings
  Delegate established itself (timeout, stall, output cap, cancel), and a
  Codex usage limit with `codex.fallbackProfile` set stays with the profile
  failover. The `providerErrors` config section is new, and an unknown key in
  it fails as `invalid_provider_errors_config`.
- A known-bad lane is one engine on one provider, model, and account (the
  Codex failover identity, `CLAUDE_CONFIG_DIR`, or the auth profile), plus the
  credentials the child will see: API keys, tokens, and broker, realm,
  account, and endpoint settings in the environment, and for OpenCode the
  provider keys, base URLs, and `Authorization` headers inside
  `OPENCODE_CONFIG_CONTENT`. They are folded in as a salted hash (the salt is
  `~/.delegate/state/lane-health.salt`, mode 0600), so a bad key on one
  account never refuses a healthy account on the same model, and no key value
  is stored or shown. `doctor` and the refusal print only a short `[credential
  xxxxxxxx]` tag, an account label shaped like an email address is masked, and
  a success clears its marker even when the marker store's lock is stuck.
- A Codex or Claude work run that fails with a transient provider drop
  (`stream_disconnected` or `provider_unavailable`: Codex's websocket closing
  early, or a 5xx or overloaded Claude reply) and saved its native session is
  continued once, automatically, the way `delegate followup` continues one:
  same session, a new run linked to the first by `followupOf`. The
  continuation's envelope and manifest carry `autoResume` (`automatic`,
  `attempt`, `of`, `trigger`), and the final result is the continuation's.
  There is no second automatic attempt, so a drop in the continuation is
  final. It never applies to safe or call mode, `--pass-through`,
  structured-output runs (the workflow supervisor owns that retry), runs that
  saved no session, or runs in a temporary worktree; a worktree run attaches
  to its own worktree and branch. When the continuation cannot be built the
  first run's result is returned unchanged with `autoResume` set to
  `attempted: false` and a `reason`. It is on by default, and
  `providerErrors.autoResume: false` opts out; workflow children are
  unresumable unless the call passes `resumable=True`, so it reaches them only
  then.
- Workflow `agent()` returns two new typed failure kinds under
  `on_failure="typed"`: `lane_known_bad` (the child refused to launch because
  its lane is marked known-bad; workflows never pass `--force-launch`) and
  `provider_exhausted` (the call was skipped, with no run and a `run_id` of
  `None`, because its stage already stopped launching on the lane).
  `AgentFailure.provider_error` carries the child's `providerError` so a
  script can branch on the signature instead of parsing prose, and
  `capabilities["providerOutcomes"]` advertises the feature. A stage is one
  `phase()` label and a lane is one engine on one model. When the first
  `providerErrors.stageStopAfter` results (default `3`; `0` turns the stop
  off) of a stage on a lane all fail with the same persistent, lane-scoped
  signature, the rest of that stage's calls on that lane are not launched,
  cells already queued behind the agent cap skip too, and a call with an
  engine fallback chain moves on to its next engine. A success or any
  different result among the first results means the lane is not uniformly
  bad, and transient and request-scoped failures never count. The trip is
  journaled once as `stage_lane_stopped` and each skipped call as
  `agent_lane_skipped`; a skipped call still counts against the workflow's
  agent budget.
- `delegate capabilities refresh` records per-engine auth health. A successful
  refresh runs the read-only probes named in `providerErrors.authProbes` (by
  default `estate-cursor status` for `cursor` and `estate-omp usage` for
  `omp`) and records what they clearly say as `authHealth`, in its JSON and
  text output and in `~/.delegate/state/auth-health.json`: `ok`, `logged_out`
  (Cursor reports it is not signed in), or `limit_reached` (an OMP quota
  window is at 100%). OMP reports quota per provider and account, so its
  reading also carries `lanes` (`<provider>/account <N>` to `ok`,
  `limit_reached`, or `unknown`); the engine reads `limit_reached` only when
  every lane is exhausted, `partial` when some are, and `unknown` for lanes
  the report does not describe. A probe that is missing, slow, exits non-zero
  without a recognisable message, or prints something unexpected records
  `unknown` with a `reason`; `unknown` is never a failure and never refuses a
  launch, and the probe's raw output is not stored. A subset refresh probes
  only the named engines and keeps the others' last reading, and `delegate
  doctor` shows the last reading as `authHealth`. `authProbes` accepts only
  `cursor` and `omp` (each a command and its arguments, or `null` to turn that
  probe off), and `DELEGATE_AUTH_PROBES=off` (or `0`, `false`, `no`) skips
  every probe.
- A conformance test requires the parser to accept every documented command
  line. It extracts each `delegate` line from `docs/cli-reference.md` and each
  usage and example line in `COMMAND_SPECS`, expands the usage synopses into
  concrete argv lists (minimal, maximal, each alternative alone, and every
  pair of optional groups), and fails on any the parser refuses, so a synopsis
  that shows mutually exclusive options as independent cannot drift again. A
  separate test runs the real launcher on `mail send --to coordinator -` with
  a piped multi-byte UTF-8 stdin and compares the delivered bytes.
- The stall watchdog stops runs for named reasons besides idle time. A run
  that streams roughly 100k tokens of model output with no tool activity stops
  as `runaway_output`; only model-output payload characters count, and
  text-stream harnesses such as Devin are exempt. The same tool call failing
  the same way five times in a row, with nothing else completing in between,
  stops as `repeated_tool_failure`; each failure counts in exactly one streak,
  and targets compare whole. A repeated identical call no longer resets the
  idle clock and time spent inside tool calls no longer counts as idle, so a
  fast no-op loop is caught while a slow polling loop is left alone. A changed
  `HEAD` in the execution checkout counts as progress. Every idle stall samples
  the child's process group and records `childActivity` (`cpu_active`,
  `waiting`, or `no_processes`) with a process snapshot, and a pending stall
  kill is re-validated after those blocking diagnostics.
- A Devin child whose output already ends in a Delegate completion report and
  then goes quiet is stopped as finished (`stoppedAfterCompletion`) instead of
  stalled. Only a `Status: completed` report records exit 0; `failed` or
  `blocked` records a failed run, and a child that echoes the prompt's own
  template line (`Status: completed / blocked / failed`) stalls as before.
- Tracked launches take `--stall-minutes`, which pins the stall threshold over
  config, the silent-harness default, and `DELEGATE_STALL_MINUTES`. The
  manifest records a pinned value as `stallMinutes`, and `resume` and
  `followup` pass it to the continuation (config and environment defaults are
  still re-resolved). Resume refuses an invalid recorded value and followup
  ignores one, as each already treats `timeoutSeconds`. `describe` lists the
  flag with the other tracked-only launch options.
- With `DELEGATE_CHILD_NO_PAGE_ASK=1` in the launching environment, each
  tracked child finds a stub `ask` first on its `PATH`. The stub prints a
  refusal and exits 2 (the real `ask`'s relay-down code) instead of paging the
  operator's phone and blocking. It is written into the run's own private temp,
  scratch, or run directory; nothing under `$HOME` is written.
- An `--isolation none` run whose cwd is strictly inside a git work tree
  records the files it changed elsewhere in that repository as
  `outsideCwdChanges` (repository, count, examples) with a warning. The before
  and after snapshots key each path by status, size, and mtime, skip
  Delegate's own registry, and run git with `--no-optional-locks` so they never
  rewrite the user's index. A repo-root cwd, a non-git cwd, and paths outside
  the repository stay undetected, and the warning says so.
- Safe and dirty-synced workspace copies name, in a run warning, the paths
  they withhold only because `.git/info/exclude` ignores them. The files are
  still not copied, since `info/exclude` is where private scratch usually
  lives.
- An omp prompt that names image paths warns that omp will not attach them
  (omp attaches files only from `@path` argv, and Delegate sends the prompt on
  stdin) and points at omp's read tool. The scan runs in linear time.
- `runs --summary` and `ps --summary` print counts by status, harness, and
  group for every matching run, without rows or manifest loads. Each run is
  counted once across registry roots, the listing's empty-scope and
  status-filter warnings apply, and `--limit` and `--structural` are refused.
- `wait --json --structural` reduces each run to identity, terminal status,
  result quality, and failure fields, and moves the handle-resolution warnings
  (`bare_handle_stale`, `bare_handle_ambiguous`, `run_target_stale`) to the top
  level. `ok` and the exit code still come from the full view; text output
  ignores the flag.
- `worktree show` and `worktree remove` accept the worktree path as the
  handle, matched canonically against the records' `executionCwd`; a relative
  path resolves against the `--cwd` workspace. An unknown path fails with
  `unknown_worktree_path` and, like an unknown handle, points at
  `worktree reap --path`. `not_worktree_run` names the run that owns the
  worktree and puts `worktree show` on it first in `nextActions`.
- A bare harness handle warns `bare_handle_ambiguous`, naming up to five other
  runs, when other runs of that harness share its group or are still running.
- A cursor model that is a bare family word such as `grok`, and is neither a
  configured `cursor.models` alias nor a catalog selector, resolves to the
  family's newest selector (highest version, non-fast, balanced tier) from the
  discovered catalog, or from the bundled table when discovery has none, with
  a warning. Configured defaults and routes resolve the same way before effort
  routing.
- Launches warn when the resolved model id is absent from the discovered
  catalog, or from the bundled table when discovery is empty; bundled-table
  warnings fire only for a selector that resembles a listed id. Cursor now
  uses this shared check, so a misspelled id warns without a discovery
  snapshot. A Claude `[1m]` suffix compares by its base, and Claude family
  aliases are exempt. The launch is never refused and the selector never
  rewritten.
- The `--forbid-commit` implied-worktree note and a missing `codex.profile`
  overlay print to stderr when a run launches, not only in the finalization
  report or `doctor`.
- A failed safe-mode run whose child reports resolver or reachability errors
  (`getaddrinfo`, `ENOTFOUND`, `EAI_AGAIN`, and similar) warns that safe mode
  may block network access and that research belongs in work mode. Only a
  generic nonzero exit gets the hint; status and failure kind are unchanged.
- `followup` on a run that a later resume or followup already continued prints
  a note naming that run and the command that continues from it, unless the
  later run shares the source's native session.
- `describe` and `models --json` report `binary` for every engine, droid
  included. The `models` text view omits a retired droid row that has no
  configured or discovered models and no configured default.
- Workspace spec for work lanes in a persistent worktree: `--base REF` cuts the
  worktree from REF, `--env NAME=VALUE`/`--env-file PATH` set child variables
  that are recorded privately and re-applied on resume and followup, and
  `--setup CMD` runs in the fresh worktree before the child. A failed setup is
  a typed `workspace_setup_failed` error (`failureKind: workspace_setup`) and no
  child launches. `run --input-json` takes `base`, `env`, and `setup`, and
  workflow `agent()` takes `base=`, `env=`, and `setup=` (capability
  `workspaceSpec`). `workflow run --env/--env-file` records a launch env that
  every resume re-applies.
- A worktree whose run is not terminal while its launching Delegate process is
  alive is leased: prune, auto-prune, reap, and remove skip it with
  `worktree_leased` even when the child pid is dead.
- `delegate runs` in a repository's main worktree also lists runs registered in
  its linked worktrees, tagged with `registryWorkspace`.
- Workflow steps can carry a stable caller-supplied identity. `agent(..., key="...")`
  replays by that key inside the enclosing named scope instead of by position and
  prompt text, so a resumed script that skips settled work or embeds new git heads
  in its prompts still hits its cache. A changed prompt under the same key adopts
  the recorded result and journals `key_prompt_mismatch`. `parallel`, `pipeline`,
  and `workflow` accept `key=` to name their scope. Reusing a key within one run
  raises `WorkflowKeyConflict`. Unkeyed calls keep their positional keys, so
  existing journals replay unchanged.
- Gate actions: `park_gate(key, result, actions=[...])` pauses for an operator
  decision, and `workflow approve <wfId> --gate KEY --action NAME [--note TEXT]
  [--data JSON]` returns that choice to the script. Undeclared actions are
  refused with the allowed list. A bare `approve` behaves as before. A recorded
  action the resumed script no longer declares is not returned: the gate parks
  again (`gate_action_undeclared`) and asks under the current actions. A second
  live `park_gate()` with the same key in one named scope raises
  `WorkflowKeyConflict`.
- The workflow `capabilities` global adds `agentKey`, `scopeKey`, and
  `gateActions`.
- Tracked runs persist the harness-reported token `usage` on the run record, so
  `snapshot`, `run-output`, and `runs --json` expose it without re-reading the
  child's stream (previously it reached only call mode's launch response).
- A fungible or panel run whose harness reports a served model other than the
  resolved one now carries a `model_substitution` warning naming requested,
  resolved, and served models. Pinned runs already pause on a switch; the
  warning closes the silent case for the default continuity mode.
- `delegate doctor` warns when a configured `<engine>.defaultModel` (resolved
  through `<engine>.models` aliases first) is absent from the selected profile's
  discovery catalog, naming the nearest discovered selectors. It checks the
  unique targets of configured `<engine>.models` aliases the same way, naming the
  alias key, since a selector an operator wrote down is worth knowing about even
  when no current launch selects it. A cursor launch
  adds the same warning for its selected selector, because cursor-agent resolves
  an unknown selector inside the child. All of them are warnings: an empty or
  missing catalog stays silent, and no configured value is rewritten or refused.
- `workflow run` warns when it detects it is running inside a systemd unit whose
  `KillMode` will reap the detached supervisor (`control-group`/`mixed`), naming
  the unit and the required `-p KillMode=process`. Previously the unit reported
  success while the workflow was already dead.

### Fixed
- A stale Run's `nextActions` (in `runs` and `snapshot`) now offers `delegate
  resume <handle>` (not for `call` Runs, which cannot resume). The CLI
  reference and troubleshooting guide say what killing a launch command does:
  the launch is the Run's supervisor, the child keeps running on its own until
  it next writes output and then exits, the Run reads `stale`, and `cancel`,
  `resume`, or `followup` recover it. Launch each lane from its own background
  task.
- The `runs` and `worktree list` text tables head their time-since-activity
  column `idle`; it was labelled `age`, which agents read as run age. JSON is
  unchanged.
- Parallel Runs launched at the same moment (workflow `parallel()` children,
  a fan-out) no longer fail at random with `unsafe_scratch_directory` ("File
  exists: 'run-scratch'"). Two launches racing to create the shared scratch
  root now both use it; the symlink, non-directory, and foreign-owner checks
  still run on the directory either way.
- The Claude model-typo refusal is skipped when `CLAUDE_CODE_USE_BEDROCK`,
  `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, or `ANTHROPIC_BASE_URL`
  comes from `--env` or `--env-file`, not only from the launching
  environment. The env files are read once per launch, so the check and the
  child see the same values.
- A Run's `current` no longer keeps showing a provider error after the
  harness has moved on. Pi and OMP show `retrying after provider error
  (429)` while they retry and then the new activity; a Codex `turn.started`
  or OpenCode `step_start` after an error replaces it the same way.
- `--notify` channel pings work against post 0.9.0, which removed `--anyway`
  from its help. The notifier sends without it and retries once with
  `--anyway` only when post refuses the send as `crossed_send`.
- The setup-command output tail recorded for a worktree Run can no longer
  reconstruct a secret passed through `--env`/`--env-file`: recorded values
  and their fragments are masked, generic redaction runs between masking
  passes, and the whole pass repeats until nothing changes (a tail that never
  settles is replaced by the mask).
- The Cursor model-family warning names the selector the operator typed and
  the routed selector actually passed to Cursor.
- Every harness caps its map of tool calls still waiting on a result at 256
  entries (Claude, Kimi, Grok, and Cursor kept them all), and a completion
  report status line may end in a semicolon (`Status: completed;`).
- `worktree remove` of a parent no longer orphans worktrees of Runs launched
  inside it: clean, merged nested worktrees are removed first, and dirty or
  unmerged ones refuse with `nested_worktrees_block_remove` and a per-nested
  command (`--force` does not reach them). Reap refuses a pool path holding
  nested worktrees (`nested_worktrees_present`), a nested Registry that
  appears mid-removal refuses with `nested_registry_appeared`, and an
  unreadable nested Registry refuses even with `--kill-live`.
- A Codex workflow stage whose schema has an object with no declared
  properties (including through a local `$ref`) no longer fails every
  attempt: it uses the prompt path instead of strict mode, which closed the
  object to zero keys. A direct `codex --output-schema` with such a schema is
  refused at preflight with `invalid_output_schema` naming the path.
- A dry-run `workflow resume` no longer cancels or reaps a stale scope child,
  changes status, persists a budget, or records an approval; it only
  journals. `workflow approve` on a failed workflow points at `workflow
  resume`, and on a running one says the gate is not open yet.
- A relative `--prompt-file` falls back to `--cwd`, and the not-found error
  names both paths tried.
- The unread-mail scan cannot block on a FIFO named like mail; the shared
  bounded record reader opens non-blocking.
- Small surface fixes: the Registry writes `.delegate/.gitignore`;
  `--include-dirty` is a warned no-op in safe mode and reports `includeDirty:
  false`; `runs show` names the real readers; a timed-out `wait --json` warns
  and names `--structural`; `no_matching_worktrees` names the Registry it
  searched.
- A `--mail-push` Codex run no longer dies before the model starts. Delegate
  injected `-c hooks=true`, which Codex 0.157 rejects at config load because
  `hooks` is a table; it now passes `--enable hooks` (the same as `-c
  features.hooks=true`) and does not add it twice when it is already present.
- `--notify` room pings work against post 0.9.0. The notifier no longer passes
  `--allow-self`, which post now rejects as an unexpected argument, and the
  message-id match accepts post's room ids (which have no microsecond field)
  as well as channel ids, so `messageId` is no longer empty for a room ping. A
  room ping is a workspace fan-out that never reaches its own sender, so
  `--notify` naming your own room reaches the room's other participants and
  not the launching session; the troubleshooting guide now says so.
- On a Python older than 3.11 (Xcode's 3.9, for example), `bin/delegate.py`
  and the `delegate_agent` package now exit `2` with a message naming the
  interpreter version and executable they found, instead of a raw
  `ImportError` from inside the package. The tracked
  `bin/delegate-profile-shim` picks a 3.11+ interpreter itself (`python3`,
  then `python3.14` down to `python3.11`, then Homebrew's, then
  `/usr/local/bin/python3`), honors a validated `DELEGATE_PYTHON` (an override
  older than 3.11 is refused, not bypassed), and exits `2` with a plain
  message when none is found.
- The Codex and `post` argv Delegate builds is now tested against the
  installed binaries (`tests/test_real_binary_contracts.py`), which is how the
  two dead flags above shipped. A `codex` or `post` on `PATH` that fails
  `--version`, times out, or cannot register the throwaway room now fails the
  suite with its command, exit code, and output instead of skipping it; a
  class skips only when its binary is absent or
  `DELEGATE_SKIP_REAL_BINARY_CONTRACTS` is truthy, and says why.
  `pytest-xdist` workers now see the real HOME through
  `DELEGATE_TESTS_ORIGINAL_HOME`.
- A run that fails keeps what the child had produced. A failed, timed-out, or
  (opt-in) capped run's completion report now quotes the child's last
  substantive assistant text, bounded and redacted, under "Partial output
  recovered before the run stopped. This is not a completion report", as a
  cancelled run's already did. Pi, OMP, and OpenCode keep that text across
  turns and steps, so a long review followed by a tool turn and a provider
  error is still recoverable (OpenCode used to clear it at every step). A
  call-mode timeout or output overflow, including a child that had already
  exited before Delegate noticed the overflow, returns the buffered draft as
  `diagnostics` on the error (also flattened into the JSON error):
  `partialText` (redacted and bounded), `stdoutTail`, and `stderrTail`; text
  mode prints the partial text to stderr.
- `followup` errors now say what is true. A source with no recorded native
  session (launched with `--no-resumable`, before Codex and Claude work runs
  saved their session by default, or as a workflow `agent()` call without
  `resumable=True`) is refused with `session-missing` and pointed at `delegate
  resume <handle>`, no longer told to relaunch with `--resumable`. A session
  that was saved but cannot be found when resumed fails as `session_expired`
  (`failureKind: session_lost`); the message names the usual cause, a launch
  under a different account than the one holding the session, and offers
  `delegate resume`.
- `resume` and `followup` refuse a known option of that command that appears
  in the prompt tail (`delegate followup x fix it --dry-run`) with
  `option_after_handle` before anything launches, instead of sending it to the
  child as prompt text while the run launches with the defaults. Put `--`
  before the prompt to send such a token as literal text. `followup` accepts
  options on either side of the handle but only before the prompt text, and
  `resume` only before the handle; an option-shaped token that belongs to
  another command keeps the existing warning.
- A workflow structured retry on Codex or Claude whose resumed session cannot
  be found (`session_lost`, reason `session_expired`) now falls back to a
  fresh relaunch in the same worktree instead of ending, without spending a
  retry; the journal records `agent_structured_retry` with `strategy:
  "relaunch"` and `fellBackFrom: "resume"`. A fresh child redoes the task, so
  the fallback relaunches only over a worktree known to be untouched, and
  otherwise the call is refused (`agent_structured_retry_refused`) as
  `work_changed_session_missing` when an earlier attempt already changed the
  worktree, or `work_state_unverified` when that could not be checked. The
  work summary now records `fileInspectionStatus` next to
  `commitInspectionStatus` (each `verified` or `unverified`), because a failed
  `git status` used to read as zero changed files; `noChanges` is true only
  when both are `verified` and show nothing. In work mode both the prior
  attempt and the resumed attempt must carry a verified summary that reports
  no changes, and a missing summary, or one from a Delegate that predates
  `fileInspectionStatus`, counts as unverified. Safe and call children have no
  summary and keep the fallback.
- `worktree reap --path P --older-than N --yes --force` now removes a pool
  entry that has no run record but that Git still links; `reap` used to skip
  it as `live_backlink` even with `--force`, and the only way out was `git
  worktree remove --force` by hand. Every check is re-run under the locks
  immediately before removal. The pool scan must have no warning for the entry
  (an entry with broken-looking metadata changed within the last 15 minutes,
  or whose metadata cannot be read, is refused); Git must still list the path
  as a worktree of its source repository; no Registry may hold a record for it
  (`record_owns_path`, including a run that claimed it after planning, or
  `record_in_other_registry` with the `--cwd` to run from); there must be no
  uncommitted work by the definition above (`dirty`, `dirty_unknown`) unless
  `--discard-uncommitted` is passed; no process may have its current directory
  inside the path (`process_cwd_inside`, from one pass over `/proc` or `lsof
  -d cwd`, never `lsof +D`) unless `--kill-live` is passed; and changed ledger
  files are saved first. A process scan that cannot run, or cannot read one of
  your own processes, refuses as `process_scan_unavailable`; other users'
  processes are noted in a warning and do not block. The branch is kept, and
  without `--force` the entry is still skipped as `live_backlink`, now with a
  hint naming `--force`.
- Persistent worktrees no longer replace committed symlinks with placeholders.
  The replacement showed up as typechanges that the next `git add -A` would
  commit. A symlink committed to the repository is left as Git checked it out,
  absolute or not; only an untracked symlink that points outside the source,
  or at gitignored content, is replaced by a placeholder as it is mirrored in,
  and the launch warning names it. Safe mode is unchanged and still blocks
  every external symlink in its throwaway copy.
- `worktree show` on a followup, resume, or attached run names the run that
  owns the worktree, following the lineage up through followups and resumes
  (up to 16 hops, with a cycle guard) and putting `delegate worktree show
  <owner>` first in `nextActions`.
- Two false or misleading model warnings are gone. Cursor labels are compared
  after the context-window token (`256K`, `1M`) is dropped, so
  `grok-4.7-xhigh` served as `Grok 4.7 256K Extra High` is the requested model
  and not a `model_substitution`. A pinned call whose stream named a served
  model is no longer told it "could not be verified"; that warning now fires
  only when the stream was silent.
- Usage synopses in `--help`, `describe`, and `docs/cli-reference.md` now
  match what the parser accepts; the parser was right in each case. `claude
  call` takes `[--read-only|--pure]`, not both. `runs` and `ps` take
  `[--summary | [--limit N] [--structural]]`, because `--summary` prints
  counts and no rows and is refused with either. `snapshot` and `run-output`
  take `(<handle>|--latest HARNESS)`. `run-output` takes `[(--stdout|--stderr)
  [--raw | [--tail N] [--max-chars N]]]`, with a second usage line,
  `[--no-redact] (--raw | --tail N)`, for the standalone `--raw` and `--tail
  N` forms. `worktree remove` takes `[--keep-branch | [--force-branch]
  [--force]]`.
- `describe --summary` is now described as the command index plus config
  resolution, launch options, profiles, and workflows, larger than the default
  and `--overview` views and smaller than `--full`, not as compact.
- The workflow supervisor relays `SIGINT` to its children the way it does
  `SIGTERM` and `SIGHUP`, and journals `supervisor_signalled`. `SIGINT` used
  to reach the interpreter's default handler, which raised `KeyboardInterrupt`
  and marked the workflow failed without cancelling its children; they ran on
  unowned until a later resume sealed them.
- The `agent_timeout` journal row written when a `followup()` call times out
  now carries `key`, `label`, and `scope`, as the `agent()` timeout row does,
  so a journal with several follow-ups says which step died.
- The workflow docs no longer say a resume adopts running children. A resume
  cancels every child the previous attempt still had running and relaunches
  that work; adoption covers only children that already finished (or that this
  supervisor lifetime is itself still launching). Waiting on a live orphan and
  taking its result is deferred work, not current behavior.
- `describe --full` documents the workflow DSL the runtime actually injects.
  Its `globals` list is read from the runtime's new `WORKFLOW_DSL_GLOBALS`,
  which the injector also checks, so it now names `park_gate`, `reject`,
  `soft_park` and its helpers, `structured_attempt`, and `dry_run` (aliases in
  `globalAliases`). `capabilities` is derived from the runtime's capability
  table, adding the `agentKey`, `scopeKey`, `gateActions`, and `workspaceSpec`
  entries the old literal lacked, and is also given structured as
  `capabilityVersions`. The `agent()` signature is rendered from the runtime
  method, so it gains `resumable`, `on_failure`, `key`, `base`, `env`, and
  `setup`; the `pipeline`, `parallel`, and `workflow` entries document `key=`,
  and `judges` documents per-entry dicts.
- A stdout or stderr line handler that raises (a registry lock timeout while
  persisting progress, a parser bug) no longer kills the drain thread, which
  stopped consuming the child's pipe and could strand the child. The drain
  keeps reading to EOF, logs a `stream.handler_failed` event, and surfaces a
  run warning that retried attempts carry forward.
- `cancel` no longer fails with a raw `TimeoutError` while another process
  holds the registry lock. When the lock wait runs out it reads the record
  unlocked, re-reads it with any pending finalize WAL overlaid just before
  signalling, and signals only a launched child generation whose start identity
  matches the run; a terminal result refuses as `run_already_terminal` and a
  changed generation as `cancel_target_changed`. It then makes one bounded
  attempt to record the cancelled outcome, reporting `registryLockBypassed`
  with an unrecorded warning if the lock is still held, and refuses with
  `registry_lock_busy` for an unlaunched record, a setup group, or a stale
  seal.
- A bwrap safe child in a uv project without a `.venv` gets
  `UV_PROJECT_ENVIRONMENT` pointed inside the run's own writable temp
  directory, so `uv run` no longer fails creating `.venv` under the read-only
  workspace bind. An existing `.venv` or an explicit `UV_PROJECT_ENVIRONMENT`
  is left alone.
- Resume onto codex or claude keeps a source run's explicit `resumable`
  opt-in, so `followup` on the resumed alias works (any other source now takes
  the resumable-by-default launch setting); a cross-engine resume onto an
  engine that cannot keep it drops the opt-in and says so.
- Resume and followup that reuse the source run's auth profile now say the
  profile was inherited from the source run instead of warning about an
  `--auth-profile` flag the user never typed.
- The suite resolves the production compact-temp root the way runs do. On
  macOS it named `/var/tmp` where runs record `/private/var/tmp`, so six tests
  failed and every subprocess run's temp directory leaked.
- A workflow no longer cancels its own live children as stale. Two `agent()`
  calls from plain script threads could share one positional scope, and the
  second call cancelled the first's running child. Stale-scope cancellation now
  targets only children an earlier supervisor lifetime started (`agent_started`
  records the lifetime's `incarnation`).
- Workflow adoption relaunches instead of waiting on or failing over a key's
  latest child run when that run is cancelled, stale, or never launched
  (`agent_adopt_skipped`).
- Resume seals a workflow child that never published a pid (stuck at `running`
  or `creating_isolation`) once it has been idle for 300 seconds and its
  recorded launcher (`launcherPid`) is dead or its pid was reused, instead of
  refusing with `workflow_children_unsealed` forever. The runner re-reads the
  record under the registry lock before every process start, the first included,
  and publishes the child's pid only over a record that is neither terminal nor
  cancel-requested, so a sealed launch never gains a child.
- `workflow run --dry-run` executes the script with its working directory set to
  the `--cwd` workspace, as a real run's supervisor does. A dry run that times
  out leaves the workspace as the working directory, because its abandoned
  script thread may still be resolving relative paths.
- Persistent-worktree change accounting lists untracked files individually
  (`git status --untracked-files=all`), matching the per-file launch-seeded
  digest map. A seeded untracked directory used to be reported as one collapsed
  entry the seeded filter could never match, so a quiet `work` run claimed a
  change the child never made and reported success, a structured child with
  unparseable output was refused as changed instead of relaunched, and the
  worktree was retained as dirty. `workSummary.changedFiles` and
  `rawChangedFilesCount` count files rather than directories for untracked
  trees.
- A bare `429`/`rate limit` diagnostic classifies as `usage_limit` only when
  account-context wording (`quota`, `usage`, `billing`, `subscription`,
  `account`, `credit`) appears on the same line within 80 characters of it, in
  either order. An unrelated `memory usage: 82%` line or a wrapped command's
  `usage:` help text no longer pairs with a transient 429, which had marked the
  run a quota failure, suppressed the structured correction retry, and pointed
  operators at credential rotation.
- Codex work runs recover the last substantive agent message as the completion
  report when the stream never sealed `turn.completed`, instead of classifying a
  complete report as `resultQuality=empty`. A progress preamble is still not
  adopted.
- `run-output` no longer claims no recoverable final message exists while the
  child wrote stdout; the diagnostic states the byte count and names the command
  that reads it.
- Workflow child launches that fail before publishing a run record record the
  exit code and a bounded, redacted stderr tail on the attempt outcome and on a
  new `child_stdout_unparsed` event (previously only the JSON decode error of
  empty stdout was kept). `agent_structured_retry` carries the tail too. This
  covers a child that exits `0` after writing empty or non-JSON stdout, on both
  the initial `agent()` launch and `followup`.
- An empty `runs`/`ps` listing now adds the workspace-scope warning without
  requiring `--group`/`--harness`, so a bare `runs --running` in a directory
  with no Registry no longer prints an empty table that reads as dead lanes.
  `wait --group` on a group stored in another workspace reports the same hint
  and names the searched Registry.
- `workflow_not_found` errors for `status`, `run --resume`, and id-based verbs
  name the searched workflow root and the `--cwd` form, so "stored elsewhere"
  is distinguishable from "gone". The workflow profile-drift refusal names the
  differing identity keys with their pinned and current values.
- `models <engine> --summary` points at `delegate help models`, and the focused
  help states that `--summary` takes no engine argument.
- The linked-worktree registry-lock guard verifies suite ancestry before
  reporting a violation, so a concurrent unrelated Delegate launcher holding the
  same file is no longer reported as a suite escape.
- Test bootstrap pins `MISE_DATA_DIR`/`MISE_CACHE_DIR` suite-wide, so a
  subprocess-bearing test no longer makes the mise python shim download an
  interpreter into a temporary HOME and pollute stderr.
- The suite removes the compact child temp directory each tracked run manifest
  records before it tears that temporary registry down, and asserts no test
  leaves a net directory under the production owner root. Previously any test
  that launched a real Delegate subprocess created `/var/tmp/dlg-<uid>/<token>`
  that only `runs prune` could ever remove, and the temporary registry those
  paths were derived from was deleted first. The run's own recorded child is
  settled first: a live child whose command line carries one of the paths its
  manifest names gets a bounded grace to reach its own exit (so a fixture child
  writing through `$TMPDIR` finishes), and only then, or after being reaped, is
  the exact recorded directory removed.
- `run-output`, `runs`/`ps`, and `snapshot` reconstruct a persisted run's
  `usage` from one allowlisted normalized shape instead of copying the stored
  object, so a record rewritten after the run (a non-isolated child shares the
  workspace) cannot inject unknown keys, an unknown basis, or credential-shaped
  text into a payload or the `usage:` line. A record whose basis is not one a
  producer emits contributes no usage at all.
- The served-model comparison behind the `model_substitution` warning uses the
  same harness-aware identity rules as the pinned check: OMP's provider-qualified
  `provider/model` observation, a Cursor display name, and a Claude family alias
  are the requested model, not substitutions. A clean OMP lane is no longer
  labelled as substituted.
- Model warnings redact their model values: `model_substitution`,
  `pinned_continuity_unverified`, and the configured-model/alias catalog warnings
  can carry an operator- or provider-supplied selector, and all of them reach
  `state.json`, `delegate.doctor.v1`, or the completion report. A
  credential-shaped selector is masked instead of persisted.
- Workflow child launches that fail before publishing JSON carry the same
  evidence on the *initial* `agent()` attempt as on followups: the outcome
  records the exit code and a bounded, redacted stderr tail, and stdout that
  never parsed emits `child_stdout_unparsed`. An exit-zero child whose envelope
  is unusable is now reported as `invalid_envelope` rather than `nonzero_exit`.
- `workflow run` detects the systemd unit from the cgroup component that owns
  the current process instead of any ancestor `.service` in the path, so an
  ordinary interactive session under `user@<uid>.service` no longer receives the
  severe `KillMode` warning. A unit under the per-user manager is queried through
  `systemctl --user` rather than read as a system-manager lookup failure.
- The track's child `TMPDIR`/`TMP`/`TEMP` now points at a separate per-run
  short-path directory (`/var/tmp/dlg-<uid>/<token>`, recorded as
  `manifest.tempPath`) instead of the run scratch. The scratch path carries the
  registry hash and run id, so a child binding a Unix socket under it overran
  `sun_path`; the new root is short by construction, stays owner-only, is granted
  as a second writable root in the Codex read-only permissions profile, is bound
  read-write inside the optional bwrap boundary, survives failure and
  cancellation like the scratch, and is removed only by the same run-owned prune
  path. Runs recorded without a `tempPath` prune as before.
- The completion report carries a `model_substitution` warning and the run's
  normalized token usage in a labelled delegate-authored block above the child's
  text, and `run-output` (JSON and text) projects the persisted usage. A
  substituted lane no longer reads as the requested model's clean work in the
  report artifact, while the child's report text and its
  `completionReportSource` attribution are unchanged.
- The `dev` extra ships `setuptools` and `wheel`, making the documented
  `python -m build --no-isolation` form work instead of failing with
  `BackendUnavailable`. The isolated build path is unchanged.

### Changed
- Work-mode prompts gain two sentences saying nobody can approve during a Run
  and the task is the approval unless the prompt asks only for a plan,
  review, or read-only answer. Safe and call prompts are unchanged.
- Runs at `xhigh`, `max`, or `ultra` effort default to a 20-minute stall
  window instead of 8, unless `--stall-minutes`, config, or
  `DELEGATE_STALL_MINUTES` sets one; dry run, manifest, and snapshot report
  `stallWindow` with its source.
- The `broker_binding_inactive` hint says one refusal while siblings launch is
  usually launch-slot contention (relaunch once, with `--force-launch` if the
  lane is marked) and a repeat means the binding is inactive.
- OMP alias objects with unknown keys name the allowed keys and point effort
  synonyms at `thinking`; `help omp` carries a valid example. The
  `cursor_auth_required` hint names the auth realm the Run used and the
  `--auth-profile` alternative.
- **The tracked output cap is now off by default.**
  `<engine>.trackedStreamMaxBytes` used to default to 16 MiB (64 MiB for Pi
  and OMP) and stopped a run as `output_limit_exceeded` when a stream passed
  it. It now defaults to `null`: a verbose run is never killed, truncated, or
  hidden from the parser for its size, and `stdout.log` keeps the whole stream
  and is flushed on every write, so its size and modification time follow the
  child. Set a positive integer to opt an engine back into a cap; the
  `output_limit_exceeded` message then names the key to raise or clear. OMP's
  256 MiB transport and 16 MiB per-record ceilings apply only with an opted-in
  cap, and call mode keeps its own 16 MiB caps. Runaway streams remain the
  stall watchdog's job (`runaway_output`). The 500-line limit in the
  `events.jsonl` mirror was never a limit on `stdout.log`; its
  `stream.lines_truncated` marker now says the child keeps running and
  `stdout.log` keeps the full stream.
- A Devin answer is held in memory as a bounded excerpt. Devin prints plain
  lines with no message boundaries, so Delegate treats everything it prints as
  one running answer; past 30,000 characters that answer is the first 20,000
  and the last 10,000 with an `[N chars omitted]` marker between them.
  `assistantText`, the completion report, and a failed run's recovered partial
  output read the excerpt, `assistantTextChars` keeps the full length, and
  `stdout.log` keeps the full stream. The answer used to be rebuilt as one
  string on every line, which was quadratic and, with the cap off, held the
  whole run in memory.
- **Codex and Claude `work` runs are now resumable by default.** They save
  their native session unless the launch says `--no-resumable` (JSON
  `resumable: false`) or the engine config sets `codex.resumable` or
  `claude.resumable` to `false` (both default `true`), so `delegate followup`
  works without remembering `--resumable`. `--resumable` on a launch still
  beats the config key, and `resume` takes the same default and
  `--no-resumable`; a resume source recorded as exactly `true` keeps it, and
  any other source takes the launch default. The default outranks
  `codex.ephemeral` and `claude.noSessionPersistence`, which now apply only to
  runs that are not resumable. Safe runs, call runs, `--pass-through` runs,
  other engines, and workflow `agent()` children (unless the call passes
  `resumable=True`) are unchanged, and `--no-resumable` is a quiet no-op
  there. A succeeded persistent-worktree run that is resumable keeps its
  worktree (`worktreeRetained: "resumable_session"`) instead of retiring it
  when clean, so `followup` and `resume` can re-enter it; a fan-out of these
  runs leaves one retained worktree per succeeded run until `worktree prune
  --merged`, `worktree gc`, or `worktree remove` cleans up. Launch with
  `--no-resumable` when a run's worktree should retire itself.
- **`--force` no longer overrides a live run on the worktree commands.**
  `worktree remove`, `worktree prune`, and `worktree reap` refuse a worktree
  that a live owner still holds (`run_active`, `run_not_terminal`,
  `process_group_alive`, `worktree_leased`) even with `--force`, and the
  refusal names the new `--kill-live`, the only override, which removes the
  worktree out from under the run. `--kill-live` does not discard uncommitted
  work or force the branch by itself. Removal is also refused for a running
  run recorded in the worktree's own `.delegate/` registry
  (`nested_run_active`) and for a nested registry, or a run in it, that cannot
  be read (`nested_registry_unreadable`), each overridable only by
  `--kill-live`. A skipped entry in `prune` and `reap` JSON carries the same
  hint.
- Uncommitted work has one definition across `worktree remove`, `prune`,
  `reap`, and completion retirement: what the lane actually changed. Files the
  launch seeded from a dirty source that still match their digest, and the
  ledger paths in `worktrees.retirementIgnoreGlobs` (default `.beads/**` and
  `.papercuts.jsonl`), do not count, so a worktree that `prune` plans is one
  `remove` accepts and a refusal names only the lane's own paths. Ledger edits
  are saved before any removal: changed ledger files are copied and
  byte-compared into `<Registry>/salvage/<worktree>-<UTC timestamp>/`. The
  result reports `salvagePath` and `salvagedPaths`, the text output prints a
  `saved N changed ledger file(s)` line, and completion retirement records
  `worktreeSalvagePath` on the run. Deleted ledger files and the old name of a
  renamed one are recorded in a `MANIFEST.tsv` (status, path, old path) in the
  same directory and reported as `salvageRemovedPaths`. A copy that fails, or
  a `git status` that cannot run, refuses the removal as
  `ledger_salvage_failed`. Delegate never deletes a salvage directory, and a
  source-gone `reap` deletes the path without a copy.
- **An OMP `provider/model` id you type is now pinned by default.** A
  `provider/model` given as `--model`, input-JSON `model`, a positional raw
  id, or a literal workflow `agent(model=...)` behaves as `--continuity-mode
  pinned` unless you name a mode. Delegate launches it with a private
  `--config` overlay that sets `retry.modelFallback` and
  `retry.usageAwareFallback` to `false`, so OMP's own retry chains cannot move
  the run to another provider (retrying the same model is unaffected, the
  overlay beats your `config.yml`, and a dry run shows `--config <omp config
  overlay>`). If another provider or model is served anyway, the run fails as
  `model_continuity_paused` with a message naming what OMP tried to serve, for
  example `requested opencode-go/glm-5.3, but omp tried to serve
  fireworks/glm-5p3`. A delegate alias (a key of `omp.models`) and
  `omp.defaultModel` stay `fungible`, so multi-subscription failover keeps
  working; the run records and warns (`model_substitution`) which provider
  answered, and `--continuity-mode pinned` on an alias pins it.
  `--continuity-mode fungible` or `panel` on a typed id opts out of the
  overlay. Workflows have no continuity passthrough yet, so there a literal id
  is pinned and an alias is fungible.
- An `omp` model name that is not an `omp.models` alias and not a
  `provider/model` selector fails with `invalid_alias` once a discovered omp
  catalog exists, unless it is exactly a catalog model id; the error lists the
  configured aliases and the nearest catalog selectors. OMP resolves a bare
  name by fuzzy match against its own catalog and can serve a different
  provider (a retired alias, `kimi`, resolved to `fireworks/kimi-k3`), so
  Delegate refuses instead of passing it through. With no catalog the run
  proceeds with a warning; `delegate capabilities refresh` lets bare names be
  checked.
- **Claude `work` runs and their followups now run with background tasks
  disabled.** Delegate sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and adds
  `--disallowedTools Monitor`, because a headless child ends when the model
  stops and a background task or Monitor dies with the session. As a long gate
  must then run in the foreground, Delegate also sets
  `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` to the run's own
  `--timeout`, never below two hours (a run with no `--timeout` gets two
  hours), so Claude Code's 2-minute default and 10-minute maximum do not cut a
  foreground command off. `claude.disableBackgroundTasks: false` turns all of
  it off. An auth profile's `env` or a workspace `--env` cannot undo these
  variables: Delegate keeps its own values and warns `profile env NAME
  ignored` or `workspace env NAME ignored` when the value differed. Every
  framed `work` and `safe` prompt also carries a two-sentence rule that ending
  the turn ends the run and that long jobs must run in the foreground and
  finish before the final message; verbatim slash pass-through prompts are not
  rewritten.
- **A finished run's scratch is now reclaimed on its own age.** Scratch, its
  sidecars (for example mail-push engine homes), and the compact child temp
  used to be removed only when the run record was pruned, so finished runs
  kept their disk until then. The ambient retention pass now removes them for
  terminal runs older than `tracking.retention.scratchDays` (default `3`; `0`
  reclaims as soon as a run is terminal), keeps the run record, and stamps
  `scratchReclaimedAt` and `scratchReclaimedBytes` into the run state;
  `snapshot` shows both, and prints a `scratch reclaimed:` line in text.
  `delegate runs reclaim [--older-than DAYS] [--dry-run]` does the same on
  demand and lists sizes. Running, stale, and already-reclaimed runs are
  skipped, and a run that fails is reported without stopping the others.
  Reclaiming applies the recorded-path and ownership checks that pruning uses,
  removes entries one at a time without following symlinks, and refuses a tree
  that holds another owner's entry or crosses onto another filesystem. The
  ambient pass spends at most 20 seconds, counted inside the walk and the
  removal; a run it could not finish gets no marker, is reported as
  `budget_exhausted`, and implicit reclaim then waits ten minutes before
  continuing. `runs reclaim` has no budget or cooldown, and still works when
  `tracking.retention.enabled` is `false`, which stops the ambient pass.
- `--forbid-commit` now works in every isolation mode, including `--isolation
  none`, which used to be refused; with isolation omitted it still implies
  `--isolation worktree`. Delegate creates run-owned git hooks (`pre-commit`,
  `prepare-commit-msg`, `commit-msg`, and `pre-merge-commit`) that refuse, and
  points `core.hooksPath` at them through one variable,
  `GIT_CONFIG_PARAMETERS`. Codex's default environment filter drops names
  containing `KEY`, `SECRET`, or `TOKEN`, which left the indexed
  `GIT_CONFIG_COUNT` form without its keys and broke every git command;
  `GIT_CONFIG_PARAMETERS` survives it. The post-exit check stays as the
  backstop: it fails the run if commits remain ahead of the creation base (the
  HEAD recorded at launch for `--isolation none`) or if the checkout's own
  HEAD reflog shows a commit made since launch and left behind, on a side
  branch or reset away, for in-place and worktree runs. An unreadable reflog
  with nothing found otherwise leaves the policy unverified, which also fails
  the run. The hooks are a tripwire, not a wall: `git -c
  core.hooksPath=/dev/null commit` or `git commit-tree` bypasses them, and the
  post-exit check can be defeated on purpose too.
- A mail send that reaches no inbox fails with `mail_not_delivered` (exit 1)
  instead of returning `ok` with exit 0. The message names each recipient's
  outcome and reason and states the eligibility rule, the diagnostics carry the
  `msgId` and ledger rows, and the sent ledger is still written.
- `--auth-profile` precedence (flag, then environment, then config default)
  now selects the config file as well as the profile: `--auth-profile
  work|personal` with an existing overlay selects `config.NAME.json`, its keys,
  and the child's `AI_PROFILE`, and warns when that replaces an ambient
  `DELEGATE_CONFIG` or a different `AI_PROFILE`. A flag whose overlay is
  missing warns that the environment's config and keys stay in effect, and the
  resolver warns when an inherited detection variable overrides the config's
  `profiles.default`. The profile shim now finds the flag anywhere before `--`,
  keeps the last occurrence, and strips whitespace, as the Python parser does.
- Errors point at the command that does what was asked. `runs`/`ps` given a
  run id, harness alias, `--id`, or `--alias` point at `snapshot` and
  `run-output`; unknown `show`, `status`, `info`, `inspect`, `logs`, and
  `output` subcommands suggest the per-run commands; `mail send --body`,
  `--message`, and `-m` explain that the body is positional, `--file`, or `-`
  (a bare `-` now reads stdin, as help already promised); and `followup` given
  a resume-only option says the option belongs to `resume` and shows the
  resume form.
- **Exit-code semantics: a child's exit code 0 no longer means success on its
  own.** One outcome function now decides every run's `ok`, `status`, exit code,
  persisted record, `wait` result, and workflow `agent()` result, so these can
  no longer disagree. A run now fails with a nonzero exit, and `ok: false`, in
  three cases where it used to report success. The first is a tracked child
  that exits 0 without assistant text (`failureKind: no_assistant_text`).
  Before this change the tracked envelope said `ok: true` with exit 0 while
  state.json said `ok: false`; call runs already failed this case and still do,
  now with the same `failureKind`. The second is a tracked or call child whose
  provider's last unrecovered error was a quota or 429 refusal
  (`provider_quota`). The third is a tracked run whose `--expect-file PATH`
  deliverable is missing (`deliverable_missing`). Scripts that treated exit 0
  from an empty tracked run as success will now see exit 1. Every failed or
  cancelled run carries `failureKind`, a closed enum listed in
  `docs/cli-reference.md`. A missing `--expect-file` deliverable is reported as
  `deliverable_missing` even when the run also produced no assistant text: the
  verdict the caller asked for outranks the no-output label, and both fail the
  run. Summaries from `runs`/`ps`/`wait` carry `failureKind` whenever the record
  holds the key, `null` included, so a consumer that derives its verdict from a
  summary agrees with the envelope instead of falling back to the older
  result-quality veto (a quiet work run whose changes landed succeeded).
- **A work run that changed files but ended quietly still succeeds.** A
  tracked `work` or `followup` run that exits 0 without assistant text, while
  its work summary shows changed files or commits, is `succeeded` with a
  `no_assistant_text` warning, and the envelope carries the `workSummary` so an
  orchestrator can adopt the work. Without changes the same run fails as
  `no_assistant_text`. Safe and call runs are unaffected. Only persistent or
  attached worktrees record a work summary, so a quiet run in any other
  isolation still fails. Pass `--expect-file` for a stricter check.
- Provider signals are classified from the final attempt only. On the tracked
  and call paths, quota, provider-error, and unrecovered-error verdicts read
  only the last attempt's own error events, last error message, and stderr, so
  an earlier attempt's 429 no longer labels a later attempt that failed for a
  different reason, and a final attempt's unrecovered quota error survives the
  retry merge. The final attempt owns the provider terminal state as well: an
  earlier attempt's `turn.cancelled`, max-turns stop, or refusal-coded failure
  no longer decides a merged run whose retry or failover answered with text and
  exited 0. The call path's empty-success retry now joins the thread-retry and
  auth-fallback merges, so a first attempt's quota line cannot classify a clean
  retry as `provider_quota`.
- `wait` reports a running record whose runner process is gone (`staleReason`
  `dead_pid` or `missing_pid`) as `failureKind: runner_lost` instead of
  `stalled`. `stalled` is kept for the stall watchdog, which workflows retry as
  transient; a lost runner is not retried.
- Workflows: a structured `agent()` call whose work child changed its worktree
  but returned no valid structured result is retried only by resuming the same
  session and asking for the structured result alone. When the engine cannot
  resume, Delegate does not launch a fresh child into that tree; the call fails
  as `structured_invalid` and the `agent_structured_exhausted` journal event
  carries the child's `workSummary`. Safe-mode children and work children
  without changes keep the existing correction retry.
- Tracked `safe`/`work` launches accept a repeatable `--expect-file PATH`. After
  the child exits, Delegate checks the child's process group for members that
  are still alive and records an `orphanedProcesses` warning before it
  terminates them.
- Call-mode JSON carries `assistantText`, `assistantTextChars`, and
  `assistantTextTruncated` on every engine, the same fields tracked envelopes
  use. `text`, `textChars`, and `textTruncated` stay for one release as
  deprecated aliases.
- Workflows: `agent(..., on_failure="typed")` returns a falsy `AgentFailure`
  when structured retries run out. It carries the last parsed candidate, the
  attempt count, `failureKind`, the run id, and the served model and provider;
  the default is still `None`. The new globals are `agent_meta(label)`, which
  exposes the child's outcome and served model and provider, and
  `capabilities`, which lets a script detect these features. A failed child no
  longer erases an earlier attempt's parsed candidate. A child that exits 0
  without text now receives a structured correction retry. Structured parsing
  tolerates raw control characters, including inside a prose-wrapped or fenced
  answer: a candidate the scanner finds is decoded again with the stray
  characters stripped before a quoted fragment of the damaged document can be
  returned in its place.
- Bundled Codex declarations move to the GPT-6 line. `gpt-6-sol` and
  `gpt-6-luna` replace the `gpt-5.6-sol`/`gpt-5.6-luna` rows and `gpt-5.6-terra`
  is dropped outright, verified against live `codex` enumeration on both the
  work and personal profiles (2026-09-22): Sol carries `low`-`ultra` at a
  `medium` default, Luna carries `low`-`max` at `medium`. The `pi` and `omp`
  bundled selectors follow to `openai-codex/gpt-6-sol`, and the Codex discovery
  fixture mirrors the live payload for the new slug. Doc prose that named
  `gpt-5.6-sol` as the single bundled `max`-capable Codex model was already
  stale and now names the bundled set (`gpt-6-astra`, `gpt-6-sol`,
  `gpt-6-luna`).
- Bundled Claude declarations replace `claude-opus-5` with `claude-opus-5-5`,
  and the pinned-continuity error prose names the new id.
- Bundled Grok declarations bumped to 4.7. The Grok CLI now ships `grok-4.7` as
  its upstream default (bundled rows: `grok-4.7`, `grok-4.6`). Cursor's 4.7
  ladder drops the `cursor-` prefix the 4.5/4.6 rows carried, so the bundled
  Cursor rows and the fixed-effort selector table now name `grok-4.7-xhigh` and
  `grok-4.7-xhigh-fast`; the still-served `cursor-grok-4.6-xhigh*` selectors
  stay in the fixed-effort table. The Grok discovery fixture mirrors the live
  `grok models` output, including `grok-4.7-build-fast`.
- The skill-review preamble is explicitly bounded: read the served skill index
  once, read only the skills judged relevant, and do not run discovery CLIs to
  enumerate skills. The requirement stays mandatory; it no longer reads as an
  open-ended discovery task.
- `workflow reject`'s `agent_rejected` event records `rejectedBy: "coordinator"`,
  and the workflow DSL `reject(key, reason, by=...)` accepts `seat`,
  `merge-gate`, or `coordinator`. Emitters that declare nothing keep the legacy
  event shape, so a missing `rejectedBy` means unknown, not seat failure.
- A structured-output resume retry re-renders the target schema into the
  correction prompt instead of asking the model to recall the shape; both GLM
  review retries had rebuilt a root-array schema as an object wrapper and failed
  again.

### Security
- Native Codex and Claude session files now persist for every work run,
  because those runs are resumable by default. Codex's rollout under
  `CODEX_HOME/sessions` and Claude Code's transcript under its `projects`
  directory hold the prompt as sent, the child's replies, and tool inputs and
  results (file contents the child read, command output). They live in the
  harness's own store, outside `.delegate/`, outside Delegate's redaction, and
  outside `runs prune` and the worktree cleanup commands, and Delegate does
  not prune them. A profile with a separate `CODEX_HOME` or Claude config
  directory keeps them in that account's store. Reduce the footprint with
  `--no-resumable`, `codex.resumable: false`, or `claude.resumable: false`.
  The untrusted-input boundaries are unchanged: safe runs, call runs (Claude
  `call --pure` still passes `--no-session-persistence`), `--pass-through`
  runs, other engines, and workflow children without `resumable=True` never
  save a session.
- **Work runs now have a write guard, on by default on Linux.** Work mode runs
  the child with the caller's own filesystem rights, so one confused lane (`rm
  -rf ~`, a stray redirect into a sibling checkout) could destroy things
  nobody can recreate. The guard is a protect-list, not a home allowlist: a
  named set of irreplaceable paths becomes read-only and everything else stays
  writable, and reads are never restricted. By default it protects, each only
  when it exists, the credential stores `~/.ssh`, `~/.gnupg`, `~/.config/gh`,
  `~/.config/gcloud`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.netrc`,
  `~/.git-credentials`, and `~/.password-store`; `~/.ai-profiles`; the
  installed Delegate runtime (`~/.local/bin/delegate` plus `~/.delegate/src`,
  `releases`, `bin`, and `config*.json`); and the code root (`~/Code`), so a
  lane in one checkout cannot write into a sibling. Inside those paths the
  run's own needs are re-opened: the execution root, the git common directory,
  the run registry, run scratch and compact temp, mail-push homes, and the
  selected engine's home. `/tmp`, `TMPDIR`, and home caches such as `~/.cache`
  are not protected. The guard never applies to safe mode, `--pass-through`
  runs are not guarded, and it does not stop reads, network use, or use of
  credentials already in the environment.
- Backends and platform defaults for the write guard. On Linux the child runs
  under bubblewrap (`bwrap --dev-bind / /` with each protected path bound
  read-only), preflighted before launch; the execution root is bound as a
  mount of its own so a lane cannot rename its checkout away, a new hard link
  to a protected file cannot be made, and writes through symlinks to protected
  paths are refused. **On macOS the Seatbelt guard is opt-in:** with
  `isolation.writeGuard.macosSeatbelt` at its default `false`, the guard
  status is `off` with no warning and no refusal, and setting it `true` opts
  in to the `sandbox-exec` guard, which then wraps every engine except a Codex
  lane whose own `workspace-write` sandbox is on (a Codex lane with its sandbox
  bypassed is wrapped, and that combination is not yet live-tested). To keep the `external-sandbox` policy
  profile but put Codex's own sandbox back for work lanes, set
  `policy.harness.codex.work.bypassApprovalsAndSandbox` to `false`; Delegate
  then emits `--sandbox workspace-write` with `--add-dir` roots for the git
  common directory, registry, scratch, temp, `--writable` paths, and existing
  home caches.
- Configure and inspect the write guard with `isolation.writeGuard`
  (`enabled`, `onUnavailable`, `macosSeatbelt`, `codeRoot`, `add`, `remove`,
  `writable`, `homeCaches`; an unknown key or wrong type fails as
  `invalid_isolation_config`), the `DELEGATE_WRITE_GUARD` environment variable
  (`off`, `0`, `false`, or `no` disables, `on`, `1`, `true`, or `yes` enables,
  and it overrides the config), and `--writable PATH` (repeatable, `work`
  mode, CLI-only), which re-opens an existing path for one run. The plan
  (backend, protected paths, and each re-open with its reason) is recorded in
  the run manifest under `writeGuard` and printed by `--dry-run`, and
  work-mode prompts gain a two-sentence note naming the protected paths so a
  lane reports a blocked write instead of working around it. When no backend
  is usable, `isolation.writeGuard.onUnavailable` decides: `warn` (the
  default) launches unguarded and records a warning, and `refuse` fails the
  launch with `write_guard_unavailable`. On Linux with `warn`, a bwrap
  preflight that fails on one unbindable path drops only that path (listed
  under `writeGuard.unbound`, with a warning) instead of the whole guard; when
  a protected path was dropped the status is `partial` rather than `enforced`
  and the warning starts "work write guard is PARTIAL". `refuse` never
  retries.
- Only the selected engine's own home variable (`CODEX_HOME`,
  `CLAUDE_CONFIG_DIR`, `KIMI_CODE_HOME`, or that engine's default directory)
  can re-open a directory inside a protected path, and only when it is
  provably one profile. The home must itself carry an identity file
  (`.claude.json`, `.credentials.json`, or `auth.json` beside `config.toml`),
  and a bounded scan below it (three levels, no symlinks, at most 2000
  directories) must finish and find no other profile's home. A parent of
  profiles, a home that is itself protected, and a scan that hits the cap or
  cannot list a directory are refused: the directory stays read-only, the
  manifest lists it under `writeGuard.refused` with the reason, and the run
  gets a warning. An engine Delegate has no home variable for (droid, for one)
  gets no automatic re-open; name its profile directory in
  `isolation.writeGuard.writable` or pass `--writable`. Known limits: a second
  profile nested deeper than three levels is not found; a hard link that
  already exists between a protected file and a writable name is the same
  inode, so a write through it changes the protected file; and a protected
  path that is itself a symlink is protected at its target while the link
  stays replaceable.
- A lane started through an estate launcher (`estate-claude`, `estate-codex`,
  `estate-omp`, any program named `estate-*`) gets the profiles root
  (`ESTATE_AI_PROFILES_ROOT`, then `AI_PROFILES_ROOT`, then `~/.ai-profiles`)
  re-opened, recorded as "estate launcher writes profile state". The launcher
  picks an account, refreshes its token, and writes session, plugin, and lock
  state there on every launch, so protecting it stopped `estate-claude` before
  Claude started. Those lanes can therefore write every profile's files; lanes
  started any other way keep `~/.ai-profiles` protected, and a Codex lane with
  its own sandbox on never gets it as a writable root.

## [0.31.0] - 2026-09-14

### Harness compatibility audit (2026-09-07)

Eleven audits, one per supported harness plus a shared pass, checked Delegate's
assumptions against each vendor's current release. Every finding that could
produce a wrong run outcome is fixed below.

#### Per-engine fixes
- Codex: `--search` and `--ask-for-approval` are declared on the interactive TUI
  and never reached `codex exec`, so `policy.webSearch` was a no-op and the
  approval policy was never stated. Both now ride `exec` as `-c
  web_search="live"` and `-c approval_policy="never"`. Codex usage survives a
  patch, and a preamble is no longer promoted to an answer after one.
- Claude: safe and read-only call emit `--permission-prompts none` when
  discovery has seen the flag, so a prompt is denied rather than hanging. A
  pinned run judges the served model by Claude's own naming instead of string
  equality, and `--continuity-mode pinned` refuses an alias that names no family
  (`best`, `opusplan`, `default`) at preflight.
- Cursor: `cursor call --read-only` emits `--mode ask`, which Cursor documents as
  read-only; safe mode deliberately does not, and keeps relying on the isolated
  workspace copy and the safe-review prefix. Tool events are parsed from the
  shape Cursor actually emits, and a pinned run accepts the display name Cursor
  reports in place of the requested selector.
- Grok: a response is sealed on usage rather than on `end`, so a multi-response
  stream keeps every part. The effort vocabulary is `low, medium, high, xhigh`;
  `max` was never accepted.
- Oh My Pi: work mode pins `--approval-mode yolo` and names its working
  directory with `--cwd`. An omp selector missing from a fresh account catalog
  now warns, because omp resolves an unknown id by fuzzy match.
- Pi and Oh My Pi: the `--thinking` vocabulary is `off, minimal, low, medium,
  high, xhigh, max`, and omp additionally accepts `auto`.
- Kimi: the goal summary is kept rather than dropped, pass-through runs pin
  `--output-format text`, and bwrap binds the selected Kimi home.
- OpenCode: read-only runs set `OPENCODE_DISABLE_CLAUDE_CODE=1`, and discovery
  probes with `--pure models --verbose`.
- Devin: the read-only argv is documented-compatible with 3000.6.14, but that
  release's read-only behavior is unproven, so the 3000.4.x gate is retained
  until `DELEGATE_DEVIN_BEHAVIOR_TEST` passes against it.

#### Transports
- Cursor Agent and Oh My Pi prompts move to stdin, verified live against
  2026.09.02-c22c1a3 and 18.1.13. That deletes their argv redaction constants,
  their membership in the 100 KiB argv guard, and the omp flag-like-prompt
  rejection, and it takes the prompt out of `/proc/<pid>/cmdline`. Kimi is now
  the only engine on argv transport and the only one whose prompt Delegate
  redacts.

#### Stream parsing
- An error event with a `message` or `error.message` now records a `failed`
  terminal on every harness, and a `result` carrying only `is_error: true` does
  the same instead of being read as a clean success.
- A non-JSON stdout line from kimi, opencode, or pi is no longer discarded in
  silence: it is counted and sampled, bounded and redacted, while the
  malformed-tool-output protection that stops a raw envelope becoming an answer
  is unchanged.
- Event types no parser branch handles are counted, so the next vendor rename
  is visible rather than silent.
- Run records and snapshots carry `malformedLines`, `malformedSamples`,
  `unhandledEventTypes`, and `unhandledEventTypesTruncated`. The block appears
  only when there is something to report, and it survives a retry or auth
  fallback instead of being reset with the accumulator. A merged usage record
  keeps grok's `costUsd`.
- The stall watchdog tracks opencode and grok tool lifecycles. Kimi and Devin,
  whose stdout is silent by design during tool execution, are exempt from the
  engine-default detector only when the run carries a finite timeout; an
  explicit `stallMinutes` is always honored.

#### Structured output
- One engine-keyed eligibility helper decides whether a schema can be enforced
  natively. Claude and Codex both require an explicit object root, and Claude
  additionally requires the serialized argv value to stay under the Linux
  argument-length limit. An ineligible workflow schema falls back to
  prompt-and-parse with the reason journaled; a direct `claude --output-schema`
  fails at preflight with `schema_not_native` instead of forwarding an argv the
  API will reject.
- In the structured retry loop, an attempt that fails with no assistant text
  clears native enforcement for the remaining attempts and embeds the original
  schema in the correction prompt, including on the resumable path.

#### Deferred, recorded as decisions rather than omissions
- Devin's 3000.4.x read-only version gate stays until a behavioral probe passes
  on 3000.6.x; Devin is not installed on the machine that ran the audit.
- Claude's `ultracode` effort is not added. Discovery overrides the static enum
  with a warning list that omits the level, so an enum edit alone is unreachable
  and would need a compatibility exception with its own proof.
- Usage-limit cooldowns stay Codex-only. Widening the valid-tool set alone
  changes nothing; the write and check paths are engine-specific.
- Followup and resume support for grok, droid, opencode, pi, kimi, and devin is
  verified possible and additive, but each needs a live round trip to prove and
  none was run.
- Grok stays on its current stream format, and `validate_schema_subset` still
  rejects `$defs` and `format`.

### Workflow resume and stale runs (2026-09-14)

Found in the dogfood window after the 0.31.0 candidate was promoted: one
workflow that lost its supervisor 28 times left 10 children listed as `stale`
forever, and every resume that reached one of them failed its thunk with
"already terminal (stale)" instead of relaunching.

- `delegate cancel` seals a `stale` run whose tracked pid is dead as
  `cancelled`, with `staleReason: dead_pid` on the record and a warning that
  nothing was signalled, instead of refusing it with `run_already_terminal`.
  A `stale` run with no pid at all is still refused: under the registry lock
  that can be a launch that has not published its pid yet. A dead leader
  whose recorded process group still has members is refused with
  `run_group_alive`, naming the group, because a dead pid is not proof the
  run's work stopped and the group id may already belong to someone else.
- `workflow run --resume` and `workflow approve` seal the prior attempt's
  in-flight children before the new supervisor starts, the same way `workflow
  kill` does, and journal `attempt_superseded` with the lost supervisor's pid,
  its last status, and the sealed children. The replay then relaunches the
  agent instead of tripping over the orphan. The seal runs after gate
  validation and never on `--dry-run`, which launches no replacement.
- The workflow supervisor handles `SIGTERM` and `SIGHUP` by requesting the
  same cooperative cancellation the stall watchdog uses. The signal handler
  itself only records; a wakeup-pipe relay thread journals
  `supervisor_signalled` and sets the cancel event in ordinary thread context,
  so the handler never takes a lock. Admission closes, in-flight children are
  cancelled, retry worktrees are released, and the supervisor exits through
  its normal cleanup with `status: failed`, `signal`, `watchdogReason:
  signal:<NAME>` (a genuine watchdog reason that started the shutdown is
  kept, and its error text is not replaced by the signal's), and the
  `cancelled` list on `status.json`. Before any terminal status is chosen or
  published the supervisor drains the relay, so a signal already delivered
  to the process is on the record consumers read as final; one that lands
  after the last child completed is recorded on the `succeeded` status as
  `signalAfterCompletion`. Each drain is answered only by its own marker,
  and a drain the relay did not acknowledge in time is published as
  `signalDrainIncomplete: true` on the terminal status instead of as a
  settled record, which `workflow wait` and `workflow watch` repeat in every
  output mode; the workflow lock is released only once the relay can no
  longer write (the supervisor fences it at exit, waiting without bound
  in-process and, as the detached supervisor process, ending itself
  unconditionally while it still holds the lock rather than letting a writer
  outlive ownership; every exit from the lock context, exceptional or not,
  fences the relay and hands the lock to a delivery in flight, which closes
  it when it completes; arming the relay rolls back handlers, wakeup fd, and
  descriptors when it fails before the reader thread exists, and the read
  end is owned by whichever of the reader and the rollback claims it first). A repeated signal is recorded, not re-raised. `workflow kill` notes which
  children were already terminal before it signals and completes its
  `cancelled` report from the whole group afterwards (`observedIn: registry`
  on entries it did not cancel itself), so a supervisor sealed by the kill's
  own SIGTERM, a child admitted just before cancellation closed, or a
  supervisor cut short by the SIGKILL escalation still yield a full list; the
  `killed` status drops only the diagnostics the signal itself produced. A signal used to end the supervisor with the lock released,
  `status.json` still `running`, and no record of why.
- A `--continuity-mode pinned` run that completes without any harness model
  observation carries a `pinned_continuity_unverified` warning naming the
  harness, on tracked runs and on ungrouped `call` payloads alike. Claude and Oh My Pi were observed reporting the served model in
  the dogfood window; Codex and Grok streams carry no model field, so on those
  engines a pinned run is pinned in name only and now says so.
- `delegate models` marks a configured binary that does not resolve from the
  Delegate process (`binaryMissing: true`, `(missing)` in text) instead of
  printing the configured path as if it were installed. The check is the
  launch check: `shutil.which` on the configured value, no `~` expansion.

### CI and runtime hardening (2026-09-07)

- Pinned runtime snapshots and workflow attempt directories are published while
  still writable and sealed to `0o500` only after the rename. macOS `rename(2)`
  refuses to move a directory that lacks owner write permission, which made
  every pin-store test fail on the macOS CI runner while Linux passed. A stale
  temporary snapshot that cannot be removed now raises a typed
  `runtime_snapshot_collision` error, and the writable pre-pass walks the tree
  through directory descriptors so a swapped symlink cannot redirect the chmod.
- Test process guards read `ps -ww` and pin `COLUMNS=80`; narrow runner
  terminals truncated the argument column and left children unreaped.
- `trackedStreamMaxBytes` is a per-engine config value: 16 MiB by default and
  64 MiB for Pi and Oh My Pi. The limit is resolved at request build and
  pinned into the run context and manifest, so a config edit does not change
  an already-launched run. Limit errors name the engine and which limit fired.
- Harness discovery fingerprints an explicitly configured wrapper whose
  basename is not another harness's binary name (an `estate-pi` style wrapper
  now discovers), while still refusing ambiguous basenames and any basename
  that names a different harness.
- Harness event capture no longer duplicates the final assistant text when a
  completion event repeats the last streamed chunk.

### Added
- `describe` provides a compact command index; `describe --full` expands the
  command/config catalog. Help is generated from the same command specifications
  used to validate supported global options.
- `workflow resume` aliases `workflow run --resume`; focused reject help,
  workflow typo suggestions, and additive error recovery fields reduce guesswork.
  `workflow watch --jsonl` streams events without retaining the full response.
- Workflow pins use immutable per-attempt operational settings with effective
  values, origins, and digests, and bind the selected profile and credential
  namespaces.

### Fixed
- `delegate doctor` warns when `codex.profile` names an overlay file that does
  not exist. Codex reads the setting as `$CODEX_HOME/<name>.config.toml` and
  accepts a missing file silently, so a stale profile cost every run its overlay
  without a word.
- A recognized option name typed after the prompt warns that it is being treated
  as prompt text.
- A terminal reason is redacted before it reaches the run record.
- Persistent-worktree launches preserve requested continuity and process-group
  termination grace through the shared request-to-run mapping.
- Doctor distinguishes executing, installed, and last-promoted artifacts and
  verifies the outer launcher chain instead of equating matching version strings.
- PI/OMP failed, aborted, and exhausted turns no longer become successful merely
  because the process exits zero. Later retry/compaction recovery remains supported.
- Noisy OMP thinking deltas use bounded diagnostic sampling with explicit capture
  receipts. Useful output, individual records, and total ingress retain finite caps.
- Safe Codex runs can write their designated scratch directory without granting
  source or metadata write access. Unsupported native permission configuration
  fails closed rather than falling back to broader workspace permissions.
- Workflow replay reads the journal once; watch tails new bytes and handles
  interrupted final records. Approval/resume handles draining supervisors and
  rolls back approval when launch publication fails.
- Attempt publication atomically refuses an existing destination, including empty
  directories. Conflicting worktree identity retains files instead of guessing a
  removal target, and limited run listings avoid probing logs for excluded rows.

### Changed
- The auto-injected skill-review preamble is behind
  `tracking.skillReviewPreamble.enabled`, default `false`. Call, slash
  pass-through, and `--pass-through` prompts never receive it either way.
- Workspace mail setup is on by default; global `--no-mail` or
  `mail.enabled: false` opts out without suppressing `--notify`. Push remains
  opt-in.
- Mail storage failures warn and disable launch-time mail instead of refusing
  the run; unreachable isolated mailboxes record manifest-only warnings.
- The wrapped work prompt gains 303 UTF-8 bytes, which count toward the argv
  size guard; `--no-mail` avoids that overhead near the boundary.
- Droid uses the same `--model <alias-or-model>` grammar as other engines. The
  positional model-alias form is retired.
- Droid custom-model selectors use Factory's documented
  `custom:<Display-Name>-<index>` form. The previous id-based selectors no longer
  resolve: re-read `delegate models droid` and update any `droid.models` alias
  that pinned one.
- Workflow resume requires current-format pins, attempt configuration and
  version-2 structural keys. Legacy formats are refused before child launch;
  start a new workflow instead of migrating old state. Gate approvals require
  the matching result hash.
- Run progress, finalization and cancellation share one bounded mutable record.
  Snapshot output is computed from that record; new runs do not maintain a
  second mutable `snapshot.json`. Keep old and new runtime writers in separate
  workspace registries during development or cutover.
- Routine progress updates recover only their own pending finalization record.
  Inspection remains lock-free and can observe a valid pending completion.
  Automatic retention is throttled and is not on the cancellation path.
- Nested workflows share runtime state without replaying the journal again.
  New-agent and followup execution share locked replay and lifecycle decisions,
  including concurrent rejection and interrupted-result recovery.
- Worktree maintenance shares safety predicates while retaining command-specific
  policies, retirement ignore-globs, and fresh checks before destructive changes.
- CLI and input JSON normalize into one launch builder for tracked and call
  modes, retaining input-specific validation and workflow-session checks.
  Invalid JSON instruction modes return usage errors; Droid raw model IDs no
  longer become spurious worktree-planning aliases.
- Shared metadata projections, typed isolation plans and retirement inputs, and
  a read-only record module reduce duplicated policy and circular imports.
- Unit tests use owning modules while CLI contracts retain entrypoint coverage.
  Acceptance selects the pinned Ruff toolchain. Pytest uses private temporary
  state and ownership-checked process cleanup; runner parity retains complete
  logs and cancellation regressions.

### Fixed
- Claude `--output-schema` is accepted in tracked `safe`/`work` modes, not only
  `call`, and is passed natively as `--json-schema`, recorded in the manifest,
  and inherited on resume. Unblocks structured Claude review lanes such as
  deslop's `--runner delegate --lane claude`.
- Workflow `schema=` validation now matches Claude's schema preflight: empty or
  duplicate `enum` values (JSON equality, integers beyond 2**53 and non-string
  object keys refused), repeated `required` or `type` entries are rejected
  before launch, and runtime enum membership uses the same JSON equality.
  Claude workflow children receive every supported schema natively.
- Worktree retirement no longer treats the shared `.beads/` and
  `.papercuts.jsonl` ledgers as lane work, so a run that filed a papercut or
  closed a bead can retire its worktree instead of stranding it. Configurable
  via `worktrees.retirementIgnoreGlobs`.
- `worktree prune` (and the auto-prune that runs at completion) no longer
  selects a worktree whose owning Run is still alive, reporting `run_active`,
  `run_not_terminal`, or `process_group_alive` instead. `--force` overrides.
- `workflow run --dry-run` can no longer hang indefinitely on a script that
  waits for a human gate: scripts now receive a `dry_run` global, dry-run item
  threads are disposable, and `workflows.dryRunTimeoutSeconds` (default 300)
  bounds the run with a `dry_run_timeout` error.

### Added
- `delegate doctor` (read-only, `delegate.doctor.v1`) reports the live runtime
  digest against the last promotion stamp and warns when the installed runtime
  changed without a promotion, when the installed launcher drifted, and which
  workflow supervisors are still pinned to older runtimes. `delegate promote
  --actor WHO --source TEXT [--runtime-digest HEX]` writes the stamp
  (`~/.delegate/last-promotion.json`) under a lock, reading the live digest
  inside it.
- Structured-output retries now resume in place when the harness supports it,
  retaining a safe temporary workspace across attempts and reaping it from
  durable child snapshots on timeout, kill, crash, or supervisor resume.

- `delegate workflow run --notify room:<name>|channel:<name>` rings a detached
  supervisor's owner when it pauses at a checkpoint, fails, succeeds, or times
  an agent out. The target survives resume and dry-run, and a notification that
  cannot be delivered is recorded as a `notify_degraded` journal event without
  changing the workflow's outcome.

### Fixed
- `agent_timeout` and `agent_structured_retry` journal rows carry the task key
  and label, so which lane died no longer has to be inferred by cross-referencing
  `agent_started` timestamps.
- A persistent-worktree run warns at completion when its base is N commits behind
  the source branch — the child never saw work that landed after it was
  dispatched.
- Parse-time option errors name `delegate dry-run`, which already validated an
  invocation without launching it. The hint appears only for launch-shaped
  commands, never when the caller has already typed `dry-run`, and a correction
  for a dry-run stays a dry-run instead of handing back a live launch.
- `workflow approve` now accumulates approvals (`approvedKeys` in `approval.json`) instead of overwriting the single `gateKey`, so a resume that replays an earlier approved gate no longer re-pauses the run there.
- Workflow `agent(schema=...)` on Codex no longer dies before launch when the
  schema has optional fields or a typed `additionalProperties` map: native
  `--output-schema` is used only for strict-compatible schemas, everything else
  falls back to the prompt-and-parse path the other engines use.
- `parse_json_tolerant` now returns the last JSON value that validates against
  the schema instead of decoding from the first `{`/`[` in the text, so a
  markdown completion report with decoy brackets (`[T1]`, `{run, exit, note}`)
  no longer burns structured-output retries.
- Workflow child failures report the child's JSON `error`/`message`/`runId`
  instead of only stderr (which was often just the persona preface).
- `workflow run --resume` treats a key whose retries were exhausted as
  retryable: a finished child run is adopted, otherwise the agent re-runs,
  instead of replaying the cached failure straight back into the same gate.

- The workflow journal no longer sorts event keys: a structured result's key
  order is part of the data (scripts embed prior results via `repr()` in later
  prompts, which the agent cache key hashes), so a sorted replay missed every
  downstream key after a resume and re-ran the whole wave.

### Added

- Workflow schema subset: `additionalProperties` may be a schema, typing a map
  with unknown keys.

## [0.30.0] - 2026-08-22

### Added

- Added an opt-in bubblewrap safe backend for eligible Linux safe runs. Set
  `isolation.safeBackend` to `"bwrap"` or use `DELEGATE_SAFE_BACKEND` to run
  zero-copy against the real workspace, bound read-only inside a bubblewrap
  boundary with gitignore-parity masks. The workspace `.delegate/` registry is
  hidden except for the current run's writable scratch, and `$HOME` is a private
  tmpfs where only the selected engine's config home is made writable by
  default. `isolation.bwrapBinds` adds explicit `{path, mode}` host paths. Each
  run preflights its final mount plan and launches with the exact probed binary.
  The backend fails closed when bubblewrap is unavailable, an untracked symlink
  could expose a host path, parity needs more than 2000 masks, a submodule is
  initialized, a writable bind covers or sits inside the workspace, or
  `--pass-through` is requested. Linked Git worktrees are supported. See the
  [security model](docs/security-model.md#zero-copy-safe-isolation-linux-isolationsafebackend-bwrap)
  and [configuration guide](docs/configuration.md#isolationsafebackend-and-isolationbwrapbinds-linux).
- Added `--notify room:<name>|channel:<name>` for one metadata-only completion
  line through the optional `post` CLI. A missing, refused, or timed-out send
  degrades to a manifest record without changing the run result. Notifications
  also fire for child launch failures and persistent-worktree setup failures,
  and the option works with `resume`; `call` and `--pass-through` reject it.
- Cursor dry-run and terminal JSON now include a stable `accountFingerprint`
  derived from `cursor-agent status --format json`. Raw account fields and
  tokens are never emitted or persisted; unauthenticated or malformed status
  output omits the fingerprint so consumers can fail closed.

### Changed

- The dev extra now ships `pytest` and `pytest-xdist`; `scripts/test-parity.sh`
  checks that the parallel accelerator and the unittest gate agree on executed
  and skipped counts.
- Bundled Grok declarations move to Grok 4.6: `grok-4.6` is the Grok CLI
  default, and Cursor entries use the canonical `cursor-grok-*` IDs with the
  full `cursor-grok-4.6` effort ladder (cursor-agent no longer accepts the
  legacy `grok-4.5-fast-*` style).
- Devin runs use the current noninteractive workspace-trust flow.
- Release hygiene: CI also runs the suite on macOS (Python 3.12), the
  repository has one canonical pull-request template, and the sdist ships
  `scripts/test-parity.sh`.

### Fixed

- `workflow run --resume` turns a completed workflow dry-run into live
  execution under the same workflow ID. Simulated agent and budget events stay
  in the journal for audit but never satisfy replay or consume live budget.
  (Listed under 0.29.1 in earlier drafts; it landed after that tag.)
- Workflow child runs keep their identity (workflow ID and child name)
  through registration, journal events, and snapshots, including children
  launched in isolated worktrees.
- Safe mode never places its scratch copy inside the source workspace. When
  the system temp directory is the source (for example `delegate ... safe` run
  from `/tmp`), the copy goes to `$XDG_CACHE_HOME/delegate/safe-workspaces`
  or `~/.cache/delegate/safe-workspaces`; a source that contains every
  candidate is refused with `safe_workspace_source_too_broad`. Previously the
  copy recursed into its own output until `ENAMETOOLONG`.
- Safe-mode copies skip directories the caller cannot read (other users'
  `/tmp/systemd-private-*`, for example) with a bounded warning, and any
  residual copy failure is reported as `safe_workspace_copy_failed` instead of
  a traceback.

## [0.29.1] - 2026-08-11

### Changed

- The PyPI artifact handoff now uses the official Node 24 upload and download
  actions, removing the deprecation warnings emitted during the 0.29.0 publish.

## [0.29.0] - 2026-08-11

### Added

- `delegate runs` and `delegate ps` now report listing honesty: the JSON
  payload gains `total` (pre-limit match count) and `truncated`, text mode
  prints `showing N of M runs (raise --limit to see more)`, and a zero-row
  result under `--group`/`--harness` explains itself — naming the status
  filter to drop when `--running`/`--stale`/`--active` emptied the result, or
  noting the run Registry is workspace-scoped otherwise.
- Retained event text now carries truncation metadata on both surfaces: raw
  `events.jsonl` `stream.line` records and normalized Snapshot `recentEvents`
  add `truncated: true` plus `textChars` (original length) only when the
  500-character bound actually clipped, and the final unterminated child
  stdout line is now written to `events.jsonl` instead of silently dropped.
  All child-controlled normalized-event strings (including tool targets,
  `error`, and `run.completed`) are bounded through the same helper.
- The tracked launcher shim template (`bin/delegate-profile-shim`) passes
  through the read-only workflow actions
  (`check|status|watch|events|result|wait|list`) without a selected profile,
  mirroring the Python CLI's guard; a parity test fails on drift.
- Tracked Cursor completion JSON now includes the CLI-reported token `usage`
  object when present.

### Changed

- Devin model discovery now uses the prompt-free, fingerprinted
  `devin models list --format json` catalog for setup, capability refresh, and
  one-off live queries. Older or unauthenticated CLIs degrade to a version-only
  record instead of receiving a prompt-like invalid-model probe.
- Child-launch failures with `EPERM` now hint that sandboxed parent shells can
  forbid launching harness binaries; `prompt_too_large` names the engines
  whose prompt rides a file/stdin transport, derived from the transport table.
- The test suite hardens hermeticity against ambient delegate-lane
  environment (`DELEGATE_RUN_ID`, mail identity, root-provenance and profile
  variables), and the documented discovery command is
  `python3 -m unittest discover -s tests -t .` everywhere, including CI and
  the contributor wiki.
- CI and publishing now use the official Node 24 GitHub Action releases, and
  the public publishing checklist builds exported source in isolated temporary
  environments instead of modifying the maintainer's ambient Python install.

### Fixed

- An explicit `DELEGATE_CONFIG` now survives profile selection: the profile
  shim and profile env resolution load the selected profile's credentials but
  keep the incoming `DELEGATE_CONFIG` as the runtime-policy config instead of
  replacing it, and codex quota fallback no longer forwards `DELEGATE_CONFIG`
  into the fallback child environment.
- Tracked runs now stop after an explicit harness terminal event and fail closed
  when either raw output stream exceeds 16 MiB, preventing a completed or
  runaway child from filling disk with cumulative stream payloads.
- Tracked stream decoding preserves UTF-8 characters split across read chunks;
  normalized events retain a bounded head/tail with an exact total, and raw
  `events.jsonl` mirrors at most 500 stdout lines plus a truncation marker so
  many short lines cannot amplify a bounded child stream into an unbounded
  event journal or in-memory event list.
- Tracked Cursor retries now report cumulative token usage, transient Devin
  catalog failures retain the last-known-good catalog, and Devin help no longer
  advertises unsupported reasoning effort.
- Slow raw-log archival no longer holds the Registry mutation lock, concurrent
  ambient retention passes skip instead of waiting, and transient Registry-lock
  contention can no longer crash a tracked run's stdout drain thread.

## [0.28.0] - 2026-08-05

### Added

- Added `delegate runs --structural` and `delegate ps --structural` for
  content-free local status collection, including lifecycle, model, group,
  status, timestamp, and root initiator provenance.
- Tracked runs now retain a validated root initiator across nested Delegate
  launches and persistent-worktree execution.

### Changed

- Codex quota fallback is now explicitly configured with
  `codex.fallbackProfile`, supports arbitrary profile definitions, and keeps
  temporary blocks by resolved credential identity rather than profile alias.
  Default work/personal credential homes continue to interoperate with legacy
  profile launchers without allowing remapped aliases to share state.
- Codex-only failover no longer depends on private Claude shim names or hidden
  runtime controls. Retry and persistent-worktree paths retain the account
  that is actually serving the run.

### Fixed

- Structural run listings now exclude raw terminal/provider events and apply
  normal secret redaction to retained fields.
- Fallback failures persist the correct account block and provider-reported
  reset time; a blocked fallback is skipped, and retries no longer return to a
  blocked primary after a preflight swap.
- Failover tests isolate their state from the user's home directory, and
  `delegate ps --help` now advertises its supported structural view.

## [0.27.0] - 2026-08-01

### Changed

- Split the mail module into `mail_core` (mailbox) and `mail_push` (hook
  protocol) behind a stable `mail` facade; extracted the runner's mail
  sentinel capture and push finalization; unified the three launch seams'
  mail preparation in one helper with ownership transfer kept at the seams;
  `parse_prompt_tail` returns a named, tuple-compatible `PromptTail`.
- Mail-disabled launches no longer touch the filesystem for mail (lanes keep
  their identity and storage materializes lazily on first send); workspace
  resolution runs once per launch; the discovery cache is memoized per
  process; the directory retry baseline is captured only when a codex
  fallback profile is configured; watch sleeps are bounded by the remaining
  deadline; non-once watches dedupe emitted messages and skip unchanged
  inboxes; mail bodies stream with a bounded read.
- All mail subcommands render prose in human mode (watch keeps its
  documented NDJSON contract); JSON output is byte-unchanged.

### Removed

- Dead code from the framer migration and mail rounds: unused safe-prefix
  helpers, the write-only `prompt_framed` field, and unreferenced mail
  helpers; development-history comments replaced with durable behavior
  statements.

## [0.26.0] - 2026-08-01

### Added

- Added workspace-local Wave 1 pull mail: storage outside run records,
  lock-serialized per-recipient delivery ledgers, inbox/read/status/watch, and
  mail-only prune with versioned JSON contracts.
- Added strict `mail.enabled` configuration, work-run identity binding, the
  opt-in `mailPush` plumbing seam, and audited harness-specific scoped or
  workspace-writable mail behavior.
- Added opt-in Wave 2 stop-hook push for audited Claude and Codex launches with
  bounded Tier-2-framed batches, per-run cursors, launch-scoped settings, and
  one-time pull-degradation events. Unverified harnesses remain pull-only.

### Fixed

- Mail launch wiring now applies to the actual child argv and matching manifest
  argv after persistent or attached worktree rewriting. Isolated grants are
  limited to evidenced harness mechanisms; Cursor no longer receives a new
  sandbox, non-isolated work launches receive no redundant grant, and Codex
  follows its effective sandbox policy. Read-only mail commands no longer
  create a registry, mailbox, lock, or Git exclude entry; status reconciles a
  published inbox envelope after a crash before its sender-ledger rewrite.
- Mail sandbox reporting now derives Codex and Grok classification from the
  emitted argv: unsandboxed policies are labelled honestly, constrained and
  unknown isolated policies warn, and only Codex `workspace-write` receives
  the scoped writable-root grant. Delivery reconciliation now validates the
  no-follow recipient envelope against the ledger before treating a crash or
  collision as delivered.

## [0.25.0] - 2026-08-01

### Added

- Named personas: `--persona` / `--no-persona` / `--allow-repo-persona` on
  every engine's safe/work launch (and dry-run), input-json `persona` /
  `allowRepoPersona` keys, and workflow `agent(persona=...)` with the resolved
  persona digest pinned into the structural cache key so an edited persona is
  a cache miss by design.
- `delegate personas [--json]` listing (schema `delegate.personas.v1`) with
  workspace-shadowing metadata and per-file invalid rows; read-only allowlisted
  in both the profile guard and the shell shim.
- Persona transports: claude work uses `--append-system-prompt-file` with a
  private 0600 file when discovery positively proves current support (probe
  recognizes both the long and the collapsed `--append-system-prompt[-file]`
  help spellings); opencode merges a synthetic `delegate-persona` agent into
  the effective config without clobbering profile keys; every other engine
  prepends. Safe mode always prepends, with the persona placed before the
  safe policy so the safety text keeps recency.
- `persona.txt` as a private durable run artifact with manifest, snapshot,
  and runs-summary projection; resume replays recorded persona bytes through
  the target engine's transport, `--persona` re-resolves, `--no-persona`
  drops.

### Changed

- The central framer is now the sole prompt composer: cursor/droid/kimi argv
  builders no longer self-prefix safe policy text, user and persona bytes are
  joined verbatim, and persistent/attached worktree notes are tracked
  structurally on the request rather than inferred from prompt content.
- Slash-passthrough prompts on persistent or attached worktree runs carry the
  Delegate-owned worktree and forbid-commit notes ahead of the slash command,
  matching v0.24.0 behavior; all other slash payloads remain byte-verbatim.

### Security

- Persona resolution walks `.delegate/personas` component-by-component over
  `O_NOFOLLOW` descriptors anchored at the source workspace or home, refusing
  symlinked parents and leaves; strict UTF-8, C0 refusal, and a 64 KiB cap.
- Resume persona replay reads only the fixed `persona.txt` artifact with a
  required, verified sha256 digest; manifest data cannot redirect the read.
- The claude native persona gate fails closed: probe errors, timeouts, and
  unrecognized banners fall back to prepend so an older binary never receives
  an unknown flag. Dry-run never probes.
- Workflow persona digests travel across the child seam and both sides fail
  closed on mismatch without caching.

## [0.24.0] - 2026-07-31

### Added

- Added `delegate resume` for relaunching terminal Runs with a bounded,
  untrusted continuation built from the original prompt and prior result.
  Resume preserves compatible launch settings, supports explicit cross-engine
  resumes, and attaches to an existing persistent worktree with a live cleanup
  lease rather than creating a second worktree.
- Added private `prompt.txt` capture for new tracked Runs so their original
  prompts can be resumed without placing prompt text in the Manifest or
  Snapshot. Legacy records without that capture fail closed with a clear
  `resume_prompt_unavailable` error.

## [0.23.0] - 2026-07-30

### Added

- Added `delegate runs prune` with a 30-day default age threshold, an explicit `--older-than` override, and `--dry-run` preview output. It prunes terminal Run records and their associated Registry artifacts (per-Run directory, logs, Completion Report, retained raw-log archive) without touching genuinely running Runs. Persistent-worktree Runs are skipped unless their worktree is recorded as removed or missing — unknown or unrecorded worktree status is treated as possibly present, mirroring `worktree gc` — so live worktrees never lose their Registry records. The ambient retention pass is bypassed on the prune command path so a dry run never mutates state.

### Fixed

- Focused unittest invocations (`python3 -m unittest tests.<module>`) no longer fail on the src layout import: `tests/__init__.py` now puts `src` on `sys.path` before any test module import runs.

## [0.22.0] - 2026-07-26

### Changed

- Consolidated pure-call validation in `constants.validate_pure_call`, collapsed repeated `attempt`/`call` invocations in `runner.py` into local closures, eliminated `typing.Any` from `src`, and removed defensive `getattr`/`isinstance` hedges where the contract guarantees the type.

### Removed

- Removed the write-only `ReasoningCapability.supported_efforts` field and unused `STATUS_FILTER_ACTIVE`/`STATUS_FILTER_RECENT` constants.
- Removed dead back-compat re-exports from `delegate_agent.cli`: `build_kimi_argv`, `build_opencode_argv`, `build_pi_argv`, `prefix_droid_safe_prompt`, `redacted_prompt_argv`, `KIMI_PROMPT_REDACTION`, `_grok_harness_bypass_enabled`, `_resolve_default_model`, and `block_external_symlinks`. These had no known consumers; the supported import surface is the CLI and `delegate --json` contracts.

## [0.21.0] - 2026-07-26

### Added

- Added `delegate worktree gc --all`, which scans the machine-wide worktree
  pool for worktrees whose source repository no longer exists. Such worktrees
  were previously unreachable by any `gc` invocation, because reaping is driven
  by the run registry inside each source repository and that registry dies with
  the repository. The scan adds no catalog or persistent state: it reads each
  worktree's Git backlink directly, and treats a backlink whose target is gone
  as the sole orphan signal. The scan removes nothing at all, at any depth: a
  missing source repository makes a worktree's uncommitted state impossible to
  inspect, so orphans and empty pool directories alike are reported for
  deliberate manual cleanup. Because that output invites manual deletion, every
  uncertainty resolves away from an orphan verdict: a pointer file that cannot
  be read, a path comparison the filesystem cannot answer, an entry too recently
  modified to have settled, and a directory whose name does not match the pool's
  own fingerprint contract are all reported as warnings rather than orphans.
  That last rule is what keeps a scan of an arbitrary root from labelling
  somebody else's directories. `--pool PATH` scans a different pool root, which
  reaches pools stranded by an earlier `worktrees.dataHome`.
- `capabilities refresh` and `setup` now name each harness on stderr as it is
  probed, so a harness that hangs is identifiable rather than anonymous
  silence. Progress output is suppressed under `--json`.

### Changed

- A launch now fails instead of proceeding when the configured binary for a
  harness identifies itself as a *different* known harness. Delegate re-probes
  `--version` when a cached capability record has refused a run, and during
  `capabilities refresh` and `setup` (see Fixed); when that banner positively
  names another harness — a Codex path replaced in place by Grok, say — the run
  stops with `harness_identity_mismatch` and exit code 2, naming the configured
  harness, the harness that answered, and the selector path.
  Previously the launch continued against that same binary with the same Codex
  argv, sandbox flags, and approval policy, and only the cached capability
  metadata was discarded. This fires solely on a banner that NAMES another known
  harness, never on uncertainty: a probe that errors, times out, exceeds its
  output bound, or prints an unbranded banner still fails open and launches on
  the cached record. An unbranded shape — a bare date-and-hash build ID is the
  clearest case — is deliberately not identification, because wrapper prefixes
  are a supported configuration and refusing a legitimate launch costs more than
  carrying a stale record until the next refresh. The selector is scrubbed of
  credentials before it reaches the message or the diagnostics, since a wrapper
  prefix can carry a token inline or after a flag. Dry runs do not probe and are
  unaffected. The refusal also names the discovery cache file for the active
  auth profile, in its `cachePath` diagnostic and in a next action, because
  `capabilities refresh` cannot clear this state: a refresh keeps the
  last-known-good record whenever a probe fails, which is correct for a flaky
  probe and wrong here, where the record itself is the problem. Without the
  path, a wrong identification would refuse every launch with no documented way
  out short of editing configuration.
- A run whose cached capability record is discarded for version drift now says
  so, naming `delegate capabilities refresh <engine>`. The record is dropped for
  that run only and the superseded copy stays on disk, so every later launch
  repeats the drop and loses the same discovery-sourced models and reasoning
  levels — previously with no indication that a stale cache was the cause.
  Selector drift stays silent, since `capabilities` already reports it under
  `driftedHarnesses`.

### Fixed

- Discovery now detects a harness that was upgraded in place. Cached capability
  records were invalidated only by comparing the configured selector, so an
  upgrade behind an unchanged selector left stale capabilities cached
  indefinitely and a newly supported reasoning level was rejected until a manual
  `capabilities refresh`. Delegate now re-probes `--version` for the single
  harness it is about to use and compares it against the version already stored
  in the record, then rebuilds the run against the refreshed picture. The probe
  runs only once the cached record has already cost the run something, never on
  the ordinary path, which therefore spawns no extra process at all — and that
  matters because the probe costs 26-668ms, worst on the Node and Bun harnesses
  that are already slow to start. Two things trigger it: an explicit
  `--reasoning-effort` the record refuses, and a `defaultReasoningEffort` the
  record silently drops. The second is the one most likely to go unnoticed,
  since a configured default degrades to a warning rather than failing the
  launch, and it is set once and then never typed again. A probe that cannot
  repair the default leaves the run exactly as it was: a launch that succeeded
  is never turned into a failure. The check fails open — a probe that errors,
  times out, or prints a banner nothing recognizes keeps the cached record, so a
  failing probe can never prevent a launch. A banner that positively identifies
  a *different* known harness is the exception, and it aborts the launch rather
  than merely invalidating the record — see Changed. Each banner is matched
  against the expected harness's own pattern before anyone else's, so a banner
  mentioning more than one tool is read as the harness that was asked for
  instead of whichever pattern happens to be declared earlier. Banners are read
  with escape sequences stripped, so a colorized version line no longer freezes
  a record permanently. A cached selector whose binary no longer exists is also
  treated as drift.
- Not covered, deliberately: a harness that *removes* a capability leaves its
  cached record listing more than the harness now supports, which produces no
  refusal and so no probe. The child rejects the effort it was handed, which is
  loud; the one quiet case is Cursor's route table aiming a run at a superseded
  model id. Catching either would cost a probe on every launch, which is the
  trade this path exists to refuse. `capabilities refresh` remains the cure.
- A dry run never probes the harness for its version. Dry run documents that it
  neither launches a child runtime nor requires the real child binary, and the
  drift probe is a child runtime; it now runs only on a real launch, leaving dry
  run with the two filesystem-local checks.
- A discovery cache carrying a newer schema than the running build is no longer
  discarded and overwritten. It is left intact while the run degrades to
  probing without persisting, so an older Delegate can no longer destroy a newer
  cache. Losing that race mid-probe degrades the same way rather than failing the
  command: `capabilities refresh` and `setup` both report `cacheWriteSkipped`,
  `setup` additionally leaving `cacheState` at `unchanged`, and each exits
  successfully, since only persistence was lost. The
  schema is re-read immediately before the atomic replacement rather than before
  the snapshot is serialized, which narrows the window in which a newer cache
  could be published and then overwritten down to the `os.replace` call itself.
  POSIX offers no compare-and-swap for a rename, so that last window remains.
- The test suite no longer writes into the developer's real
  `~/.delegate/worktrees`. Tests exercising persistent-worktree runs created
  real pooled worktrees over temporary source repositories and orphaned them
  permanently, because `worktrees_data_home` falls through to the user's home
  directory with no seam a test could redirect. `HOME` is now redirected at test
  collection time, which reaches every such resolver at once.

## [0.20.0] - 2026-07-25

### Added

- Added typed child-failure reporting for usage/quota limits (including reset
  details when present), expired or rejected auth tokens, and Codex thread/state
  lookup loss. Run state, snapshots, listings, completion envelopes, and
  synthesized reports now carry the specific error and message. The existing
  generic public error remains `child_failed`; `usage_limit`, `auth_failed`, and
  `codex_thread_lost` are additive typed codes. Classification reads only stderr
  and normalized harness error/terminal events, never assistant/model output.
- Added `delegate ps` as the first-class active-run view, equivalent to
  `delegate runs --active`.
- Added `delegate setup` for first-run harness discovery. It fingerprints all
  supported installed harnesses, records available model/reasoning metadata in
  a profile-scoped user cache, creates a minimal selector-only config when no
  config exists, and reports discovery readiness separately from launchability.
- Cached `models`/`capabilities` views now project profile discovery with source
  and evidence metadata. `models <engine> --live` provides a non-persistent
  one-off probe; `capabilities refresh` persists a full refresh for the selected
  profile, and `capabilities refresh <engine> [...]` re-probes only the named
  harnesses while the rest keep their last-known-good records.
- Runtime model and reasoning metadata now distinguish the requested model,
  argv-resolved model, capability-validation model, requested effort, resolved
  effort, source, evidence strength, and transport.

### Changed

- Codex output schemas are now linted before launch. Delegate recursively
  supplies missing `additionalProperties: false` on object schemas with a loud
  warning, rejects partial `required` lists precisely, and passes a normalized
  temporary copy without modifying the source schema. Object-bearing `allOf`,
  `patternProperties`, and external or unresolvable `$ref` values fail closed
  rather than being rewritten unsafely. Workflow Codex stages and direct
  `--output-schema`/`outputSchema` launches share the same preflight. The
  normalized copy preserves the source schema's property order, so
  think-before-answer schemas keep their intended generation order.
- On retry-safe paths, Codex thread/state lookup failures now retry once and,
  only if the same typed failure repeats, retry through an ephemeral
  `--ignore-user-config` launch. Retry-safe paths are read-only calls and
  pre-tool attempts whose workspace remains unchanged; work mode also requires
  a clean captured baseline. Envelopes and events record fallback engagement.
- `--prompt-file /dev/stdin` and equivalent stdin descriptors now consume the
  piped prompt once instead of conflicting with stdin as a second source.
- Model and reasoning resolution now prefers explicit CLI/config choices and
  exact discovered evidence before legacy workspace or bundled fallbacks.
  Exact negative menus fail closed; partial harness evidence and Cursor inferred
  routes remain visibly labeled rather than being promoted to model-exact facts.
- Discovery refresh is last-known-good per harness. Successful records update
  independently, failed probes retain the prior record and report staleness,
  and selector drift invalidates only the affected harness without probing on
  an ordinary launch.
- `capabilities refresh` now writes the selected profile's private discovery
  cache. The old workspace reasoning cache remains a lower-precedence read-only
  compatibility source. The cached `capabilities` read reports the discovery
  cache location in `cachePath`; the previous workspace path, when present, is
  reported under `legacyWorkspaceCachePath`.
- `reasoning.capabilities` config entries are now accepted for `grok` in
  addition to `codex` and `droid`.
- `--auth-profile` is now accepted on cached `capabilities` reads (previously
  rejected as an invalid option combination) and on `models` and `setup`.
- Cursor reasoning-effort routing now defers to a pinned selector when discovery
  has no same-family route for it: the run proceeds on the pinned model and
  records a bypass warning. When discovery does carry exact same-family routes,
  the requested effort still routes to the matching selector, and a family that
  omits the requested effort still fails closed.
- Grok reasoning-effort support is once again governed by the documented CLI
  enum (low, medium, high, xhigh, max); a bundled `grok-4.5` declaration that
  narrowed it to low/medium/high was removed, since `grok --help` enumerates no
  per-model efforts. Config, discovery-exact, and cached grok declarations
  still take precedence.
- Embedded Devin and Kimi `defaultModel` values are now `null`, deferring model
  selection to each harness. The editable `config init` example may still pin
  an explicit default.
- Claude and Grok `defaultReasoningEffort` config values are now only
  format-checked at load time instead of validated against the static enum; an
  unsupported configured default degrades to a launch-time warning so that
  discovery, not a hardcoded table, decides which levels are accepted.

### Security

- Discovery probes use fingerprinted allowlisted selectors, fixed argv with no
  shell, closed stdin, neutral temporary working directories, bounded output,
  timeouts, normalized cache schemas, atomic owner-only writes, and scrubbed
  model/warning projections. Config and cache source paths remain intentionally
  visible diagnostics. Automatic Devin discovery remains prompt-free; only the
  explicitly requested `models devin --live` path uses its bounded prompt-like
  invalid-model probe.
- Setup never overwrites an existing config and refuses unsafe selectors,
  final-component symlinks, and concurrent config races rather than mixing
  discovery results across config states.

### Fixed

- Credentials appearing in a child harness quota or reset message are now
  redacted before they reach `state.json`, `snapshot.json`, the completion
  report, or the run envelope; Codex reports quota walls as stdout events,
  which bypassed the stderr-tail redaction. The in-progress activity label is
  likewise redacted when persisted, matching the progress heartbeat.
- A Codex output schema that omits `required` entirely is now repaired to list
  every property (in declaration order) instead of being rejected, matching how
  a missing `additionalProperties` is handled. A partial `required` is still an
  error.
- The `primary` attempt banner is written once in the run's stderr log even
  when several retry paths run in sequence.
- `delegate ps` now reports argument errors in its own terms — unknown options,
  `--limit`, `--harness`, and `--group` failures name `ps` instead of the
  `runs` command it delegates to, and `ps --recent` gets the same tailored
  redirect as `--running` and `--stale`.
- `delegate capabilities refresh` exits 3 (missing binary) when no supported
  harness is installed — including when an engine subset such as
  `capabilities refresh codex` names only uninstalled harnesses — matching
  `delegate setup` on identical machine state instead of returning the generic
  usage code 2.
- `delegate capabilities` no longer advertises harnesses whose configured
  selector has drifted from the cached record, on both the cached read and
  after a refresh — a subset refresh returns the full snapshot, so engines it
  never probed could otherwise surface stale catalogs the launch path would
  refuse. Excluded harnesses are named in a new `driftedHarnesses` payload
  field and in the text output.
- Pi catalog rows whose column count does not match the header are skipped
  instead of being read by shifted column position, which could cache a
  thinking-capable model as having no reasoning support (unrecoverable, since
  Pi has no config override) or hand a non-thinking model the full effort enum.
- A configured harness binary or argv prefix that no longer resolves while the
  CLI is still on PATH is now reported as a stale config setting rather than an
  uninstalled harness, and `delegate setup` names the config key and file to
  fix instead of telling you to install a CLI you already have.
- An unreadable `~/.factory/settings.json` (permissions or I/O error) is
  reported as a read failure instead of being misreported as invalid JSON.
- Two Cursor selectors that normalize to the same route family and effort now
  drop just the ambiguous routes with a warning, instead of turning the entire
  Cursor discovery record into an error record.
- OpenCode catalog parsing resumes past a malformed entry's whole object, so a
  `provider/model`-shaped line nested inside the rejected body can no longer be
  re-read as a real selector.
- `capabilities refresh` now hints "global options before the subcommand" for a
  misplaced global option instead of mislabeling it `invalid_engine`, and a
  subset refresh whose requested harnesses are all uninstalled reports
  `requested_harnesses_not_installed` instead of the misleading
  `no_harnesses_installed`.

## [0.19.0] - 2026-07-20

### Added

- Codex `--reasoning-effort max` support for `gpt-5.6-sol`; other Codex models
  still fail closed unless config or workspace capability-cache overrides opt them in.

## [0.18.0] - 2026-07-19

### Added

- `worktree gc --dry-run` now reports `wouldPruneSourceRoots` and classifies
  un-prunable worktrees with structured orphan reasons (`source_root_missing`,
  `worktree_metadata_missing`, `branch_missing`, `detached_backlink`), each
  with a `safeAction`. GC remains non-destructive and never deletes worktree
  directories.
- New `worktrees.poolWarnCount` config knob (default 20): persistent
  work-mode launches emit a stderr warning when the shared worktree pool
  holds more worktrees than the threshold. Warn-only; nothing is blocked or
  deleted.

### Changed

- Text rendering for `worktree gc --dry-run` prints
  `would prune source roots` instead of a constant zero, and orphan lines now
  include the worktree path alongside the reason.

## [0.17.0] - 2026-07-18

### Added

- Pi is now a first-class `safe`/`work`/`call` engine with stateless JSON
  execution, stdin prompt transport, model aliases, reasoning-effort mapping,
  live model discovery, normalized JSON events, and a native read-only policy
  for safe mode and `call --read-only`.
- Oh My Pi is now a first-class `omp` engine with matching mode, model alias,
  reasoning, discovery, and event support. Its verified 17.0.4 prompt path uses
  a redacted positional argument because piped stdin exits without processing.
- Child processes now receive an authoritative `WORKSPACE_ROOT`; run metadata
  exposes the same path as `workspaceRoot`, with source and isolated execution
  roots available through `DELEGATE_SOURCE_ROOT` and
  `DELEGATE_EXECUTION_ROOT` where applicable.

### Changed

- Repository-local `.delegate/config.json` is no longer loaded automatically.
  Delegate reports it as an unapplied layer; operators must explicitly select
  a trusted file with `DELEGATE_CONFIG`.
- Stateless calls now preserve child-created files under
  `.delegate/artifacts/<runId>/` instead of deleting successful deliverables,
  and report `artifactsPath` plus `preservedArtifacts` in the response.
- Unknown launch flags now fail as `unknown_option` before they can be absorbed
  as prompt text, and corrected-command suggestions are emitted only after the
  real parser validates them.

### Security

- Oh My Pi safe mode and `omp call --read-only` now add
  `--approval-mode always-ask`, which denies write and exec tools in headless
  mode even when project config requests `yolo`. A gated real-binary behavioral
  test verifies writes and shell commands are denied while reads remain
  available, and positional prompt guards reject flag- and include-shaped
  injection prefixes.
- Private `.delegate` run, workflow, and cache state I/O now traverses from the
  workspace anchor with descriptor-relative, no-follow opens, rejecting
  symlinked path components to prevent symlink-swap races.

## [0.16.0] - 2026-07-16

### Added

- `--timeout` is now accepted and enforced on tracked `safe` and `work` runs
  across engines. It remains rejected with `--pass-through`, and deadline
  failures keep the historical `call_timeout` error code.
- Kimi tool-call activity is normalized into `tool.started`/`tool.completed`
  events with id correlation; `status` stays unset because Kimi tool results
  carry no result-status signal.

### Changed

- Persistent-worktree runs that fail after the worktree is realized
  (child-launch errors, timeouts) now keep `worktreeStatus: present`,
  `executionCwd`, `branch`, and `worktreeCleanupCommands` in the failed-run
  record, so the preserved worktree stays inspectable and removable via
  `delegate worktree show|remove` instead of being orphaned.
- The Kimi default model is now `kimi-code/k3`, and the bundled Kimi catalog
  lists exactly the three real model IDs (`kimi-code/k3`,
  `kimi-code/kimi-for-coding`, `kimi-code/kimi-for-coding-highspeed`); the
  stale `kimi-k2.7` entry was removed.
- Docs clarifications: Kimi reasoning-effort wording now states that the Kimi
  CLI exposes no effort flag (k3 supports effort internally via
  `~/.kimi-code/config.toml`) and that Delegate rejects `--reasoning-effort`
  for Kimi; the CLI reference notes that `usage: unavailable` in Kimi run JSON
  is expected because the Kimi CLI emits no usage/token lines in stream-json
  output.
- Kimi work-mode docs restate current behavior: Delegate emits no `--yolo` —
  Kimi prompt mode auto-approves tool actions, and work mode edits the real
  workspace by default. Temporary safe isolation or explicit worktree
  isolation provides a workspace boundary, not a complete host sandbox.

## [0.15.0] - 2026-07-16

### Added

- Work-mode worktree isolation now automatically carries tracked edits and
  untracked, non-ignored files into the new worktree, with tracked/untracked
  counts in human and JSON warnings and fail-clean teardown on sync errors.
- Child environments expose authoritative source/execution root metadata, and
  Delegate-managed snapshot and worktree cleanup refuses targets that are or
  contain the source workspace.
- Empty successful safe and read-only call results retry once when the prompt
  can be safely extended; tracked runs retain both attempts and envelopes report
  `emptyRetry` only when a retry was attempted.
- Bare Harness resolution in Snapshot, run-output, and wait now reports the
  resolved Run, Alias, workspace, and age, with a stale-resolution warning after
  24 hours.

### Changed

- `run-output --tail N` now selects stdout when no output stream or Completion
  Report was requested; stderr remains opt-in.
- Devin safe mode now fails during preflight with `unsupported_mode` rather than
  launching without an enforceable read-only boundary.
- Call children use their throwaway cwd as `DELEGATE_SOURCE_ROOT`; the
  Registry/config workspace is not disclosed to a stateless call.

### Fixed

- Empty-result retries now preserve cumulative duration, byte counts, usage,
  truncation, stdin diagnostics, timeout budgets, and tracked launch-failure
  state across primary, authentication-fallback, and retry attempts.
- Source-root cleanup guards now handle relative, symlinked, case-variant,
  unresolved, and malformed paths conservatively without blocking safe cleanup
  of source descendants.
- Dirty worktree sync preserves arbitrary filename bytes, rejects unsyncable
  dirty submodules with escaped diagnostics, and documents the same behavior
  consistently across the CLI reference and worktree guidance.
- Devin help and discovery surfaces now derive supported modes consistently and
  reject unknown modes before constructing executable arguments.
## [0.14.0] - 2026-07-15

### Added

- `workflow result --field` for extracting a single field from a workflow
  result, reliable latest-workflow selection, immutable creation ordering,
  dry-run visibility, and orphan detection (PR #14).
- Schema validation for `agent()` results supports `minLength` and
  `minItems`, including in retries and dry-run placeholders (PR #14).
- Grouped work runs sharing one non-isolated workspace now emit a warning
  with guidance for commit boundaries and persistent worktrees (PR #13).

### Changed

- Focused call help (`delegate <engine> call --help`) now reflects
  call-mode option boundaries and documents the grouped-call tracking
  exception (`--cwd` accepted only with `--group`); explicit `agent` input
  is rejected on engines that do not support it (PR #13).
- Overview, dry-run, agent-help, README, CLI reference, and worktree docs
  corrected from user feedback (PR #13).

### Fixed

- Structured Codex workflows consume the child-authored completion report
  (including on resume) and fail closed when it is missing; workflow resume
  transitions atomically to `starting`, hides stale results, rolls back
  failed launches, and releases locks on preparation errors (PR #14).
- Safe isolation re-roots exact source-workspace paths while preserving
  slash-command payloads verbatim (PR #14).
- `wait`: a run matched by both `--latest HARNESS` and `--group NAME`
  was emitted twice in the resolved-target list; it now appears once, with
  first-selection precedence and ordering preserved.
- `git_root_for` degrades any `OSError` from the git probe (not just
  `FileNotFoundError`) to the non-git fallback, so e.g. a `PermissionError`
  spawning git no longer crashes workspace resolution.

### Changed

- Internal cleanup pass across `src/` and `tests/`: removed dead code (unused
  protocol types, private helpers, stale fixtures), collapsed redundant
  exception tuples into their `OSError` base, replaced a hand-rolled
  path-containment helper with `Path.is_relative_to`, and stripped stale
  review-artifact comments. Behavior-preserving; the workflow supervisor's
  broad exception boundary around child-model output is now explicitly
  commented and regression-tested.

## [0.13.1] - 2026-07-09

### Added

- Codex-only `--fast` / `--no-fast` per-run service-tier overrides (and a
  `fast` boolean in run-input JSON). `--fast` emits `service_tier="fast"` plus
  `features.fast_mode=true` so the tier cannot be silently dropped by ambient
  Codex config; `--no-fast` emits the explicit `service_tier="default"`
  standard-routing sentinel; omitting both inherits Codex configuration. The
  explicit choice is recorded as `requestedFast` in run metadata. The flag and
  JSON key fail closed on every other engine, including a bare `"fast": null`.

## [0.13.0] - 2026-07-09

### Added

- Added `opencode` as a first-class engine across `safe`, `work`, `call`, and
  workflow runs. OpenCode drives any provider/model in the models.dev catalog
  (75+ providers, plus custom/local providers) through one harness. Safe mode
  and `call --read-only` enforce a deny-all-but-read permission lockdown via
  `OPENCODE_CONFIG_CONTENT` (merges last, beating hostile repo config; verified
  live against v1.17.17 including a git-worktree write-barrier test). Live
  model discovery via `opencode models`, string or `{"model", "variant"}`
  object aliases that can pin a reasoning variant, `--reasoning-effort` mapped
  to OpenCode `--variant`, and per-run `--agent` selection with
  `opencode.defaultAgent` fallback.

## [0.12.0] - 2026-07-09

Three features in one train: Delegate Workflows (multi-agent orchestration with durable resume), per-run model selection + discovery across all seven engines, and the Devin engine. Model selection was built as the inaugural Delegate Workflows dogfood: three implementation waves plus a twelve-round two-lane adversarial review loop driven through `workflow run --resume`.

### Added

- Per-run model selection: every engine accepts `--model <alias-or-id>` (alias
  from `<engine>.models`, or a raw model ID passed through verbatim). Alias maps
  are generalized beyond Droid to all engines; Droid keeps its optional
  positional alias and gains optional `droid.defaultModel`. `delegate models
  <engine>` merges bundled + config catalogs; `--live` probes cursor/droid/devin
  when available. Non-droid aliases also appear in `models` / `models --summary`.
- Delegate Workflows: a Python DSL supervisor for multi-agent fan-out, durable
  journaling, nested workflow calls, schema-validated `agent()` results,
  approval gates, resume, kill, saved workflows, and workflow discovery in
  `describe`/help/docs. Status/list report `stalled` when a supervisor died
  without finalizing (non-blocking lock probe; `wait`/`watch` exit instead of
  hanging), and supervisor failures record a traceback tail in the journal,
  result, and status.
- Slash pass-through: launch prompts that intentionally start with a harness
  slash command can be sent verbatim when the target mode's safety boundary does
  not depend on Delegate's prompt preamble. `--pass-through` also suppresses the
  skill preamble and completion-report suffix while preserving safe-mode
  boundaries.
- Added `devin` as a first-class engine for Cognition's Devin CLI across
  `safe`, `work`, and `call` modes. Devin uses prompt-file transport,
  config-driven model selection (`devin.defaultModel`, default `swe-1.7`),
  read-only safe/call enforcement through a Delegate-generated
  `--agent-config`, and `--permission-mode dangerous` for work/default-call
  print-mode runs.

### Changed

- Config migration: a `<engine>.models` alias key named after a mode (`safe`,
  `work`, or `call`) or after its own engine is now a config validation error —
  rename the alias (aliases naming a *different* engine, like a `droid.models`
  alias called `grok`, remain valid). Cursor input-JSON `"model"` values that differ from
  `cursor.defaultModel` are now honored instead of rejected. Droid run-input
  JSON `"model"` is now alias-or-id like `--model`: a `droid.models` key keeps
  alias semantics, anything else passes through verbatim to the harness
  (previously a hard `invalid_alias` error).

## [0.11.0] - 2026-07-05

Profile-guard calibration fix for issue #9: a shell carrying `AI_PROFILE=work|personal` with no matching `~/.delegate/config.<profile>.json` no longer presents a half-configured install as a total CLI outage.

### Added

- `delegate config sync-profiles` materializes missing `~/.delegate/config.<profile>.json` overlays (`work`, `personal`) from the validated base config. It never clobbers an overlay you already edited, validates the merged effective config before any write, and writes each overlay with private-file permissions. `delegate config init` now writes the same overlays alongside the base config. Both share one path convention (`config.profile_config_path`, `config.PROFILE_CONFIG_NAMES`) so the writer and the guard cannot disagree on overlay naming.

### Fixed

- `AI_PROFILE=work|personal` with a missing or unreadable profile overlay no longer hard-blocks every command, including the read-only diagnostics you would reach for to debug it. Recognized-but-configless profiles now block only launch and mutation commands — with remediation text that points at `delegate config sync-profiles` and the `env -u AI_PROFILE` / `DELEGATE_CONFIG=` bypasses — while read-only diagnostics (`profiles`, `runs`, `run-output`, `snapshot`, cached `capabilities`, `worktree show`/`list`, `describe`, `models`, `help`, `version`) pass with a stderr warning. An unrecognized non-empty `AI_PROFILE` now warns and continues on the base account instead of silently falling through.

### Security

- The fail-closed profile-crossover guarantee is now enforced inside the Python CLI (`delegate_agent.cli:main` via the new `delegate_agent.profile_guard`), classifying from the real parsed command rather than positional argv guessing. This closes a gap where the guarantee lived only in the optional shell shim: the pip console script, `python -m delegate_agent.cli`, and `bin/delegate.py` all reach `main` with no shim in front, and previously fell through to the base account on a missing overlay. The guard no-ops when `DELEGATE_CONFIG` is already exported (shim precedence) so the two layers compose without a double check. The tracked `bin/delegate-profile-shim` template applies the same check before Python starts, as an additional early gate; it scans all args for `capabilities refresh` so a mutation is never misclassified as a read-only probe.

### Packaging

- Published to PyPI as `delegate-agent-cli`: PyPI's separator-stripped name-similarity rule blocked the shorter `delegate-agent` (an existing `DelegateAgent` project collides). The installed console script is still `delegate`; only the `pip install` name changed.
- Hardened two tests (`test_execution_argv_and_prompt.py`, `test_wait_cancel_commands.py`) that raced child-process teardown against the isolation and cleanup assertions they were checking, which showed up as intermittent failures on the publish gate.

## [0.10.0] - 2026-07-04

Usage-audit fix wave: 82 sessions and 1,241 delegate invocations from one week of agent usage were mined for friction and failure modes, and the whole Tier 1–3 backlog was built across four decorrelated implementation waves plus live acceptance.

### Added

- `delegate wait` blocks on one or more runs until they reach a terminal state, using effective status so a dead child is a terminal failure rather than a hang. Supports bare-harness and `harness:model` latest selectors, `--group`, an optional timeout, and completion-report output; exits 0 (all succeeded), 1 (any failed/cancelled), or 124 (timeout). Replaces the ~100 hand-rolled polling loops agents wrote in the audit week.
- `delegate cancel` signals a run's recorded process group (SIGTERM → 5s grace judged by group liveness → SIGKILL) with layered safety: workspace-scoped resolution, terminal/stale refusal, `pid`/`pgid <= 1` guards, and a `ps`-lstart start-identity check that refuses to signal a pid older than the run (soft-degrades when `ps` is unavailable). A `cancelRequested` marker is stamped under the registry lock before any signal so the runner finalizer can never record a cancelled run as succeeded.
- `cancelled` is a first-class terminal status with normalized per-harness `terminalEvent`/`terminalStatus` mapping (e.g. Grok `stopReason: Cancelled` overrides an exit-0 success). Cancelled and failed runs without a child report now get a synthesized completion report (status, failure reason, redacted stderr tail, harness-appropriate remediation) so `run-output --completion-report` never dead-ends.
- Always-numbered aliases (`codex-1`, `cursor-2`, …). Bare harness names become latest-run selectors and `harness:modelAlias` selectors resolve the newest matching run; envelopes and text banners expose `requestedHandle`/`resolvedHandle`/`resolutionKind`, and generated follow-up commands always use the concrete numbered alias. `run-output` gains `--latest`.
- `resultQuality` classification on tracked runs (`ok` / `housekeeping_noop` / `empty` / `suspect_short` / `no_assistant_text`), computed at finalization and preferred from stored state at read time — closing the hole where an on-disk Droid "Plan is up-to-date." report surfaced as a clean success. Envelopes always carry `completionReportWritten` and `completionReportSource` (`child` / `delegate_synthesized` / `stdout_recovery`); auth failures classify as `auth_failed` from stderr-only patterns.
- `--include-dirty` on worktree work launches syncs uncommitted tracked changes and untracked non-ignored files through the same primitives as the safe-mode snapshot, replacing the stash-launch-pop dance; sync failures tear the worktree down before any child launches.
- `--group NAME` tags launches and selects on `runs`, `wait`, and `worktree remove`/`prune`.
- A per-run scratch `TMPDIR` is exported to safe and isolated runs after profile env; the codex lane is granted it via `--add-dir`.
- `did-you-mean` suggestions on unknown handles, a `list` → `runs` alias, corrected-command text on flag-order errors, and copy-paste command forms on invalid-mode errors.

### Changed

- `--forbid-commit` now implies `--isolation worktree` on both the CLI and JSON paths.
- Work-mode `noChanges` and quality warnings ride the top-level `warnings` array (including the runs table). Text `run-output` discloses char truncation like JSON, and `--tail`/`--max-chars` without a stream selection is now rejected instead of silently ignored.
- `describe` full output is a strict superset of `--summary`, both derived from `COMMAND_SPECS`.

### Fixed

- Registry first-init race fixed with locked init and unique atomic temp names; `write_json_atomic` cleans up temp files on failure. Run-latest tie-breaks use an explicit `registrationOrdinal` stamped under the registry lock (in-memory insertion order degrades through `save_index` on reload).
- Plaintext progress advances every line instead of freezing on the first; `call` mode returns a redacted `stderrTail` on failure and an empty-text warning instead of raw event noise; `droid call` prevalidates model aliases before creating the workspace and cleans up on every failure path.

### Security

- The native boundary read empirically reproduced a leak inherited from the safe-mode mirror: an untracked symlink with an absolute target pointing at the repo's own gitignored secret was recreated verbatim — readable and writable-through in an edit-capable worktree. The shared sync now recreates untracked symlinks only when the link is relative, resolves inside the repo, and the target is not gitignored (batched `git check-ignore -z --stdin`, failing closed to placeholders on unexpected exit codes), hardening safe mode and `--include-dirty` together. The hardlink variant cannot be closed by path-based exclusion and is documented as a caveat.

## [0.9.0] - 2026-07-01

### Added

- Stateless `call` mode for every engine (`delegate <engine> call "prompt"`, plus `droid MODEL call`). Call mode sends a one-hop prompt to a model and returns its output without a project tree, run registry, snapshot, or completion report — the generic "call a model, get the answer back" path for agents that don't need repo context. It runs in an empty temporary cwd that is always cleaned up, even on build-time failure.
- Call mode is **write-capable by default**, inheriting work-level harness permissions (the equivalent of `work` mode minus a repo). A new `--read-only` flag (and `readOnly` JSON input field) opts into the LLM-as-judge/grader contract: it drops the child to each engine's `safe`-mode read-only capability and prepends a neutralizing preamble telling the model there is nothing to inspect or mutate. `--read-only` applies only to `call` and is rejected with `safe`/`work`. Pairs with Codex `--output-schema` for structured verdicts. See `examples/task.judge.json`.
- Call JSON output includes `textChars` and `textTruncated` so callers can detect when bounded assistant text kept only the head and tail of a large response. Human-mode `dry-run` now prints `warning:` lines.

### Security

- Call mode is not a security sandbox. The effective harness policy resolves through the work/safe tiers (default call keeps work-tier web-search/network access; `--read-only` call gets the safe policy), and work-mode approval/sandbox bypass never leaks into call mode. On engines without a native read-only sandbox (Cursor, Droid, Kimi) the neutralizing preamble is the only restriction under `--read-only`; `docs/security-model.md` documents the boundary.

## [0.8.1] - 2026-07-01

### Fixed

- Launch and `dry-run` commands now accept `--json` in the launch option tail before inline prompt text begins, so agent callers can append it after flags such as `--prompt-file` without tripping the misplaced-global guard. A later `--json` after prompt text still fails closed as ambiguous flag-like prompt text.

## [0.8.0] - 2026-06-29

### Added

- First-class `delegate grok {safe,work}` for xAI Grok Build CLI: prompt-file transport, tracked `streaming-json` snapshots, safe isolation required, worktree `--cwd` rewrite, harness-scoped bypass at `policy.harness.grok.work.bypassApprovalsAndSandbox`, and Grok `--effort` reasoning mapping. Safe mode pairs Delegate's isolated worktree copy with Grok's kernel-enforced `--sandbox read-only` profile, and the streaming `error` event is surfaced into the snapshot. Grok `--output-schema` is unsupported in this release because Grok `--json-schema` forces final JSON output, which breaks tracked streaming snapshots.
- `delegate config init` command to write an editable starter config from an installed package, so users no longer need a source checkout just to copy `config.example.json`.

### Changed

- WSL setup is now documented explicitly: install Python/Git/child CLIs inside WSL, prefer `/home/<user>/...`, and convert Windows paths with `wslpath -u`.

### Fixed

- Windows-style paths now fail with actionable WSL guidance instead of turning into confusing POSIX relative paths, and WSL runs fail loudly when `git` resolves to Windows `git.exe`. Workspaces under `/mnt/<drive>` now emit a warning about WSL filesystem performance and private-file semantics.

## [0.7.0] - 2026-06-29

### Added

- Profile-aware auth and environment switching. A new top-level `profiles` config block (`detectFrom`, `default`, `definitions.<name>.env`) lets one session run under a chosen credential/environment profile and have every spawned harness inherit it. The active profile is detected from an environment variable (`profiles.detectFrom`, e.g. `DELEGATE_PROFILE`/`AI_PROFILE`) or pinned explicitly with the new global `--auth-profile NAME` flag. Delegate resolves the profile once per request and injects its env into every child across tracked, pass-through, safe-isolation, and persistent-worktree paths. Profile `env` holds non-secret routing pointers only — secret-shaped keys are rejected at config load with `secret_in_profile_env`.
- `delegate profiles` command (with `--json`): read-only introspection of the resolved profile, its source (`flag`, a detection variable name, or `default`), and the non-secret env keys it injects. It never mutates config.
- `codex.fallbackProfile`: when a Codex run hits a classified usage limit on a clean work-mode baseline with no tool events, Delegate retries once under the fallback profile's account (same env, `CODEX_HOME` swapped). A fallback that resolves to the same account is a no-op. Completions record `codexAuthFallback` metadata.
- Codex `--output-schema FILE` flag and `outputSchema` run-input field for structured final output. Codex-only; OpenAI enforces the JSON Schema on Codex's final message. Relative paths resolve against the launch cwd and are locked absolute before isolation. When set, the completion-report prompt injection is suppressed so the schema owns the final message. Other engines reject it with `unsupported_output_schema`, and `delegate --json describe` advertises `engineCapabilities.<engine>.outputSchema` for feature detection.
- `delegate --json <command> --help` payloads now include `globalOptions` and `unsupportedGlobalOptions`, so agents can discover which global options (such as `--auth-profile`) apply to a command without parsing usage strings.

### Changed

- Safe-mode runs over a dirty Git tree now inject a bounded changed-file note into the prompt before the per-engine transport split, so the synced working-tree state reaches the child regardless of stdin/prompt-file/argv transport. Documentation across help, `describe`, `agent-help`, README, security-model, and the CLI reference now states plainly that safe mode mirrors uncommitted tracked edits and untracked non-ignored files into the isolated copy.
- `--auth-profile` is accepted only where a child auth/env selection actually happens: launches, `dry-run`, `run --input-json`, `delegate profiles`, and `capabilities refresh`. It is rejected for the cached `capabilities` report and for run-inspection, worktree-management, and discovery commands.

### Fixed

- Run retrieval hints (trailer, JSON payload, snapshot, run summary) are now workspace-qualified: the registry is per-workspace, so commands are rendered through `shlex.join` with the source `--cwd`, and the unknown-handle error explains that runs are per-workspace.
- `--forbid-commit` and reasoning-effort/model preflight errors are now actionable — they name the corrective flag or config key (`codex.defaultModel` / `droid.models`) and, in a non-Git workspace, explain that no-commit enforcement requires Git rather than demanding an impossible `--isolation worktree`.
- The codex usage-limit fallback retry now injects the active profile's full env with only `CODEX_HOME` swapped, instead of dropping the profile's other pointers onto a bare environment.
- The internal "codex auth attempt" delimiter is no longer written to a non-Codex run's stderr log when a profile happens to define both a `CODEX_HOME` and a `codex.fallbackProfile`.

## [0.6.0] - 2026-06-23

### Added

- Always-on best-effort credential scrubbing on `describe`/`models` discovery output.
- Heartbeat opt-in via config: `progress.enabled`, `progress.initialDelaySec`, and `progress.intervalSec`, plus `--no-progress` to override config for one launch.
- Foreground launch progress via `--progress`, emitted to stderr so JSON stdout remains machine-readable.
- Persistent worktree `workSummary` metadata, including dirty state, changed file counts, diff stat, and child-created commits. `--forbid-commit` now fails persistent worktree work runs if the child creates commits.
- `run-output --max-chars` for bounded non-raw stdout/stderr sections, plus `rawOutputBytes` metadata for intentional `--raw` reads.

### Changed

- Removed discovery `--redacted` cosmetic masking; `--summary` remains the compact discovery surface.
- `worktree list/show` now distinguish `branchMergedIntoSource` from `mergedIntoSource`; `mergedIntoSource` means the branch is merged and the worktree has no uncommitted changes.
- Kimi help and docs now match actual argv behavior: Delegate uses Kimi prompt mode and does not emit `--yolo` with `--prompt`.

### Fixed

- Heartbeat path scrubbing is URL-safe and covers additional container/CI absolute paths without corrupting `https://…` URLs.
- Global `--json` inference now applies consistently before more subcommands.
- Completion-report recovery now prefers substantive final assistant output over housekeeping/progress output when no explicit completion report exists.
- Worktree handle suggestions are scoped to persistent worktrees, and safe-mode prompts more clearly allow read-only investigation and text-only patch proposals.
- Shared fake-agent test harness output is quiet by default.

## [0.5.0] - 2026-06-18

### Added

- New `claude` engine wrapping Claude Code headless mode (`claude -p`). `delegate claude safe` and `delegate claude work` deliver the prompt on stdin and parse Claude Code's `stream-json` output, the same way the other harnesses are normalized. Requires Claude Code 2.1.x or newer (verified on 2.1.181) for `--effort`, `--permission-mode auto`, and `--no-session-persistence`.

- Claude safe mode runs in a temporary isolated workspace (detached worktree or directory copy) with `--permission-mode plan`, `--strict-mcp-config`, a Read/Grep/Glob tool set, and a read-only Bash allowlist (`git diff/status/show/log`, `rg`, `grep`, `ls`). Safe mode is added to the engines that require real isolation, so `--isolation none` is rejected for it.

- Claude work mode uses `claude.workPermissionMode` (default `auto`). Delegate only emits `--permission-mode bypassPermissions` when `policy.harness.claude.work.bypassApprovalsAndSandbox` is explicitly set; `workPermissionMode` itself rejects `bypassPermissions` so a global sandbox profile can never silently broaden Claude's permissions.

- Reasoning effort for Claude maps directly to Claude Code's native `--effort` (`low`, `medium`, `high`, `xhigh`, `max`), validated before launch and kept independent of the Codex/Droid model-capability cache.

- New `claude` config section (`binary`, `defaultModel`, `defaultReasoningEffort`, `workPermissionMode`, `noSessionPersistence`, `bare`) with validation, and Claude coverage across `describe`, `models`, `reasoning-capabilities`, and `dry-run`. Tool activity is surfaced as `tool.started` / `tool.completed` events parsed from Claude's `tool_use` / `tool_result` content blocks.

## [0.4.0] - 2026-06-15

### Changed

- Safe mode now requires real isolation for Cursor, Droid, and Kimi: `--isolation none` (or `isolation.safe = "none"` in config) is rejected for these engines with `invalid_isolation`, so safe runs always execute in a temporary isolated workspace. **Migration:** remove any `{"isolation": {"safe": "none"}}` override for cursor/droid/kimi and use `auto` or `worktree`. Codex safe still permits `none` because it enforces its own `--sandbox read-only`.

- `delegate droid safe` now defaults to a temporary isolated worktree instead of running in place in the source tree, matching Cursor, Codex, and Kimi safe.

- `delegate snapshot` now surfaces `creationContext` (`sourceHeadOid`, `sourceBranch`, `sourceGitCommonDir`, `plannedExecutionCwd`) for persistent-worktree runs, sourced from the run manifest when the snapshot omits it.

- `run-output` stdout/stderr section `truncated` now reflects whether the tail actually cut content, instead of always reporting `true` for live and archived tails. Parent agents that branch on `truncated` to decide whether to fetch `--raw` should re-check.

### Fixed

- A single corrupt or partially written per-run `state.json` / `manifest.json` no longer aborts whole-registry commands (`runs`, `snapshot`, `run-output`, `worktree list`) or blocks launching new runs. Bulk readers skip the bad file and degrade for that one run, while commands targeting a specific run still fail loud.

- `delegate kimi work` no longer passes `--yolo` together with `--prompt`; current Kimi CLI rejects that combination, and Kimi prompt mode already auto-approves tool actions.

## [0.3.1] - 2026-06-12

### Changed

- Troubleshooting guidance now covers Kimi binary checks, active config layer inspection, safe-mode dry-run limits, pass-through option placement, exact-payload completion-report behavior, and worktree cleanup flags.
- Delegate completion-report instructions now tell child agents to put exact operator-requested payloads, such as bare JSON, after the concise parent-facing report instead of wrapping them inside it.
- Local planning documents under `docs/plans/` are ignored as private working artifacts.

### Fixed

- `missing_binary` errors now include actionable JSON diagnostics (`configPath`, `configKey`, and optional `suggestedBinaryPath`) for configured child runtimes, including persistent worktree preflight and launch paths.
- Launch and dry-run parsing now reject misplaced global completion/pass-through options after the subcommand or mode, matching the documented option placement rules.

## [0.3.0] - 2026-06-12

### Added

- Kimi Code harness (`delegate kimi`). Safe mode runs in an isolated temporary workspace with Delegate's read-only safety prompt; work mode emits Kimi `--yolo` by default for edit-capable prompt-mode runs. Delegate intentionally does not use Kimi `--plan` for safe mode. Kimi prompt mode auto-approves tool actions, so safe mode's effective write boundary is the isolated workspace and the safety prompt is advisory. Model selection uses `kimi.defaultModel` config or the `model` field in JSON run input. Reasoning effort is not supported for Kimi in v1.

## [0.2.0] - 2026-06-09

### Added

- Provider-aware `--reasoning-effort LEVEL` for Codex, Droid, and Cursor runs (plus `reasoningEffort` in JSON run input). Values are literal and validated against per-model capability declarations resolved from config (`reasoning.capabilities`), a refreshable workspace cache, or bundled fallback data. Explicit requests fail closed with `unsupported_reasoning_effort`; an engine `defaultReasoningEffort` config default that cannot be satisfied is skipped with a recorded warning instead of failing every run.

- `delegate capabilities` reports the merged reasoning capability matrix; `delegate capabilities refresh` probes `codex debug models` and writes `.delegate/capabilities/reasoning.json` atomically with owner-only permissions. A malformed cache file is ignored at run time and overwritten by the next refresh.

- Prompt text no longer travels in child argv: Codex prompts are delivered via stdin and Droid prompts via a private temp file. Dry-run payloads and manifests show redaction placeholders plus a `promptTransport` field, and stdin delivery failures surface as a run warning on stderr and in the snapshot.

- `run-output --completion-report` now recovers the last interim assistant message for dead Droid runs as well as Cursor runs (marked `synthetic`); Codex recovery still requires a completed final turn.

### Changed

- `run-output --stdout`/`--stderr` without `--tail` or `--raw` defaults to a bounded 80-line tail instead of erroring with `missing_tail`. Text output marks tailed sections (`last N lines; full log B bytes`) and synthetic completion reports in the section header; JSON output carries the equivalent flags.

- `worktree show --latest HARNESS` resolves the most recent persistent worktree for that harness, intentionally ignoring newer non-worktree runs. `worktree list` JSON gains a `summary`; `totalPersistentWorktrees` is registry-wide while `allStatusCounts` is scoped to the `--harness` filter.

- Run listings probe each run's pid once per entry, so `effectiveStatus` and `staleReason` can no longer disagree about a process that exits mid-listing.

- `reasoning.capabilities` config keys are restricted to `codex` and `droid` (Cursor uses `cursor.reasoningEffortModels`), and effort strings reject whitespace, double quotes, and backslashes.

## [0.1.4] - 2026-06-08

### Fixed

- Child agent processes now receive EOF on stdin instead of inheriting Delegate's own
  stdin. This prevents Codex runs launched from orchestrators with open stdin pipes
  from hanging before they emit output.

## [0.1.3] - 2026-06-05

### Added

- Codex streaming events are now parsed by the run tracker. `item.started`, `item.completed`, `turn.started`, and `turn.completed` events surface `agent_message` text and `command_execution` tool activity from `codex` runs in snapshots and completion reports.

- `run-output --completion-report` recovers a completion report from the recorded child stdout stream when `completion-report.md` is absent and the run has finished. Recovered reports are reconstructed with the same event parser used during live tracking, and are marked `synthetic: true` with `source: "stdout.log"` in JSON output. Codex recovery only promotes an `agent_message` once the stream reaches `turn.completed`, so in-progress messages are never treated as a final report.

- Synthetic completion-report recovery is bounded and best-effort. Delegate reads only a limited stdout tail during recovery, including archived stdout, and reports a clean `missing_completion_report` error when no completed final message is available inside that recovery window.

- Display-side redaction now covers common credential shapes such as authorization headers, bearer/basic tokens, JWT-like strings, and common secret key-values in snapshots and run-output views.

- Prompt input from delayed stdin pipes is now accepted when no direct prompt or prompt file is supplied.

### Notes

- Releases before 0.1.3 predate this changelog.

[Unreleased]: https://github.com/treygoff24/delegate-agent/compare/v0.31.0...HEAD
[0.31.0]: https://github.com/treygoff24/delegate-agent/compare/v0.29.1...v0.31.0
[0.30.0]: https://github.com/treygoff24/delegate-agent/compare/v0.29.1...v0.30.0
[0.29.1]: https://github.com/treygoff24/delegate-agent/compare/v0.29.0...v0.29.1
[0.29.0]: https://github.com/treygoff24/delegate-agent/compare/v0.28.0...v0.29.0
[0.28.0]: https://github.com/treygoff24/delegate-agent/compare/v0.27.0...v0.28.0
[0.27.0]: https://github.com/treygoff24/delegate-agent/compare/v0.26.0...v0.27.0
[0.26.0]: https://github.com/treygoff24/delegate-agent/compare/v0.25.0...v0.26.0
[0.25.0]: https://github.com/treygoff24/delegate-agent/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/treygoff24/delegate-agent/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/treygoff24/delegate-agent/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/treygoff24/delegate-agent/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/treygoff24/delegate-agent/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/treygoff24/delegate-agent/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/treygoff24/delegate-agent/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/treygoff24/delegate-agent/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/treygoff24/delegate-agent/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/treygoff24/delegate-agent/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/treygoff24/delegate-agent/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/treygoff24/delegate-agent/compare/v0.13.1...v0.14.0
[0.13.1]: https://github.com/treygoff24/delegate-agent/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/treygoff24/delegate-agent/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/treygoff24/delegate-agent/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/treygoff24/delegate-agent/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/treygoff24/delegate-agent/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/treygoff24/delegate-agent/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/treygoff24/delegate-agent/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/treygoff24/delegate-agent/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/treygoff24/delegate-agent/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/treygoff24/delegate-agent/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/treygoff24/delegate-agent/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/treygoff24/delegate-agent/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/treygoff24/delegate-agent/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/treygoff24/delegate-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/treygoff24/delegate-agent/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/treygoff24/delegate-agent/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/treygoff24/delegate-agent/releases/tag/v0.1.3
