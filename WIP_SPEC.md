# Delegate Run Snapshots WIP Spec

> **Note:** The tool ergonomics and `skill.md` file (which agents use to understand their capabilities) will require significant updates as part of the implementation phase. This will be addressed in the upcoming implementation plan, but is flagged now to inform future work.

## Status

This is a work-in-progress design spec for adding token-efficient visibility into sub-agents launched through the Delegate Agent CLI. It captures decisions made so far and open questions that should be resolved before implementation.

## Problem

Agents currently have poor visibility into sub-agents they launch through Delegate. The practical workaround is to tail raw harness session logs, often JSONL, which is token-expensive, brittle, and unpleasant for both humans and agents.

Delegate should provide a clear, bounded, agent-friendly way to ask:

- What sub-agent did I launch?
- Is it still running?
- What has it been doing recently?
- What is it likely working on now?
- What was its final result, without dumping an entire transcript?

## Goals

- Provide a concise, human-readable snapshot of a sub-agent run.
- Preserve a stable machine-readable JSON contract for agents.
- Work across harnesses such as Cursor Agent, Factory Droid, Claude Code, Codex, and future runtimes.
- Avoid accidental token burn from raw stream/log passthrough.
- Keep Delegate a small local CLI, not a daemon, scheduler, deployer, or long-running service.
- Maintain clear separation between the repo development copy and any live installed Delegate runtime.

## Non-Goals

- Do not build a background-job system.
- Do not create a daemon or central coordination service.
- Do not depend on one harness's private session-log format for v1.
- Do not stream entire sub-agent transcripts back to the parent agent by default.
- Do not implement commits, pushes, deploys, or repository publishing inside Delegate.

## Terminology

The canonical glossary lives in `CONTEXT.md`. The core terms are:

- **Delegate Run**: one child agent invocation launched through Delegate.
- **Run Registry**: project-local records that let Delegate inspect launched runs.
- **Run Alias**: short project-local handle such as `cursor`, `cursor-2`, or `droid`.
- **Snapshot**: bounded view of one run's identity, status, activity, and current focus.
- **Harness**: external agent runtime launched by Delegate.
- **Harness Stream**: raw stdout/stderr emitted by the harness.
- **Parent-Facing Output**: bounded output Delegate returns to the caller.

## Architecture Direction

### Configuration model

Delegate is a CLI package that can be installed as a user/global command while also being runnable from a repository checkout for development. The current live runtime on this machine uses:

```text
~/.local/bin/delegate          # installed shim
~/.delegate/config.json        # global/user config
```

The development checkout can also be run directly:

```bash
python3 bin/delegate.py ...
PYTHONPATH=src python3 -m delegate_agent.cli ...
```

Even when run from a checkout, Delegate can still read global/user config unless an explicit config path is provided. The new tracking defaults should therefore support both global/user config and workspace-local config.

Recommended precedence:

```text
CLI flags
> explicit DELEGATE_CONFIG path
> workspace-local <workspace>/.delegate/config.json
> global/user ~/.delegate/config.json
> built-in defaults
```

Workspace-local config lets a project set defaults such as disabling completion-report injection or changing redaction behavior without requiring every parent agent to remember per-run flags. Global/user config remains useful for installed Delegate defaults such as harness binaries and model aliases.

### Tracking is always on

Delegate should track every launched run by default. Visibility should not depend on agents remembering an optional `--track` flag.

Always-on tracking includes:

- allocating a stable `runId`,
- allocating an agent-friendly Run Alias,
- creating a Run Registry entry,
- capturing stdout/stderr,
- normalizing events when possible,
- maintaining incremental state for `delegate snapshot`,
- and returning a bounded parent-facing completion summary.

Raw harness passthrough should be an explicit escape hatch, not the default:

```bash
delegate --pass-through cursor work "fix the bug"
```

Use `--pass-through` as the flag name. It names the transport behavior: Delegate passes the underlying harness stdout/stderr through to the caller instead of mediating it into bounded parent-facing output. Avoid `--raw` because it is ambiguous about which data is raw, and avoid `--string-raw` because it sounds like an output encoding/format mode.

The important contract is that raw streaming must be opt-in, because default pass-through can burn tokens and expose verbose structured JSONL to the parent agent.

`--pass-through` should be incompatible with `--json`, because JSON mode promises one clean Delegate-owned JSON object:

```bash
delegate --json --pass-through cursor safe "analyze this"  # invalid
```

### Universal substrate: stdout/stderr capture

All harnesses are subprocesses with stdout, stderr, and exit code. Delegate should use this universal substrate, but not treat raw streams as the parent-facing API.

For tracked runs, Delegate should launch the harness with pipes, capture stdout/stderr, and tee them into the run registry:

```text
<workspace>/.delegate/runs/<runId>/
  manifest.json
  state.json
  events.jsonl
  stdout.log
  stderr.log
  completion-report.md
  snapshot.json
```

The run registry should be workspace-local by default:

```text
<workspace>/.delegate/
```

Do not store project run state globally under `~/.delegate` by default. Workspace-local storage keeps aliases project-scoped, makes cleanup obvious, and matches Delegate's existing `--cwd` workspace resolution model.

In Git workspaces, Delegate should keep `.delegate/` out of version control by adding it to `.git/info/exclude`, not by editing tracked `.gitignore` files. This avoids surprising project-file mutations. In non-Git directories, Delegate should create `.delegate/` directly and document that it is local runtime state.

Raw stdout/stderr logs should be captured by default because they are the fallback evidence when structured parsing fails or the child exits before emitting a terminal event. They must remain local and opt-in to view.

Raw log exposure should require explicit commands:

```bash
delegate run-output cursor --stderr --tail 100
delegate run-output cursor --stdout --raw
```

`.delegate/` should be documented as usually gitignored.

### Default-on archival and retention

Delegate should include automatic local archival/retention so heavily used projects do not accumulate unbounded run logs.

The exact thresholds should be implementation-tuned, but the default policy should be enabled and documented. A reasonable starting point:

- keep active/running runs unarchived,
- keep completed raw stdout/stderr logs in `.delegate/runs/` for 7 days by default,
- archive completed runs older than the default age threshold,
- do not immediately archive or delete a run merely because its logs are large,
- surface warnings when a run's raw logs are unusually large,
- preserve manifests, state summaries, completion reports, snapshots, and the alias/run index for 90 days by default,
- allow explicit opt-out or threshold override in config for users who need full local forensics.

Possible layout:

```text
<workspace>/.delegate/runs/<runId>/              # active and recent
<workspace>/.delegate/archive/<runId>.tar.gz     # older raw artifacts
<workspace>/.delegate/index.json                 # alias/run lookup survives archival
```

Use gzip archives for v1 because Python can create and read them through the standard library. More efficient compression can be revisited later if archive size becomes a real problem.

Archival should use both live lightweight summaries and compressed raw-artifact tarballs. Keep lightweight run records available for fast lookup:

```text
.delegate/index.json
.delegate/runs/<runId>/manifest.json
.delegate/runs/<runId>/state.json
.delegate/runs/<runId>/snapshot.json
.delegate/runs/<runId>/completion-report.md
```

After the raw-log retention window, move bulky raw artifacts into:

```text
.delegate/archive/<runId>.tar.gz
```

The archive should contain:

```text
stdout.log
stderr.log
events.jsonl
```

This preserves local forensics without making `delegate snapshot` and `delegate runs` slow or bulky.

Archival must not break alias or run ID lookup. `delegate snapshot cursor` should still work after archival by reading the lightweight index/snapshot. If raw logs have been compacted or archived, the snapshot should say so and point to the explicit retrieval command.

Archival should never delete active run state. V1 should be archive-only: do not include `delegate runs prune`, `delegate runs delete`, or other irreversible cleanup commands. Destructive cleanup can be reconsidered later if real usage shows archival is insufficient.

Large logs are often most valuable immediately after an overnight or failed run. Size should therefore trigger visibility, not immediate archival. For example, a snapshot can warn that `stderr.log` is 250 MB and suggest `delegate runs archive --older-than 7d`, but the raw log should remain directly available until the age-based retention window expires or the user explicitly cleans it up.

Large-log warnings are requester-facing metadata. They should appear in `delegate snapshot`, `delegate runs`, and possibly the final Delegate completion summary when relevant. They should not be proactively pushed to any agent, and they should not be injected into the child harness/sub-agent prompt or context. The warning is about Delegate's capture layer, not task feedback.

Warn when either raw stream file exceeds 50 MB:

```text
stdout.log > 50 MB
stderr.log > 50 MB
```

This threshold is warning-only. It must not trigger immediate archival or deletion.

Cleanup should be opportunistic, not daemon-driven. Delegate should run a cheap retention pass at the start or end of normal invocations:

1. scan the local registry,
2. skip active runs,
3. archive completed runs older than the raw-log retention window,
4. keep lightweight index/snapshot metadata available through the metadata retention window.

### Structured-stream first, text fallback

Where harnesses provide structured non-interactive output, Delegate should request it explicitly and parse it internally:

- Cursor Agent: `--print --output-format stream-json`
- Claude Code: `--print --output-format stream-json`
- Factory Droid: `droid exec --output-format stream-json`
- Codex, if added: `codex exec --json`

If structured output is unavailable or unstable, Delegate should fall back to capped text extraction from stdout/stderr.

### Delegate remains synchronous

Delegate can still run synchronously from the caller's perspective. The registry exists for inspection, not scheduling. A command may block until the harness exits, but snapshots can be requested from another shell/agent while the process is running because state is written incrementally.

### Live snapshots use the file registry

`delegate snapshot <alias-or-runId>` should work while the original Delegate launch process and child harness are still running. The snapshot command should not need IPC with the launching process, a daemon, or the child harness. It should read the workspace-local Run Registry.

The launching Delegate process should update run state incrementally using atomic file replacement:

```text
state.json.tmp -> rename -> state.json
snapshot.json.tmp -> rename -> snapshot.json
events.jsonl append + flush
```

Snapshot readers should tolerate partial or stale state:

- live process with recent state: `status: running`,
- missing terminal state and missing/dead PID: `status: stale` or `unknown`,
- completed process: final status and exit code,
- mid-write or invalid state file: fall back to the last valid state/snapshot when available.

## Identity and Collision Avoidance

### Stable run ID

Each Delegate Run gets a unique stable `runId`, suitable for durable storage and exact machine lookup.

Example:

```text
del_20260520T214233Z_8f3a9c
```

### Agent-friendly alias

Each run also gets a project-local Run Alias based on its harness:

```text
cursor
cursor-2
droid
droid-2
claude
codex
```

The first run for a harness claims the base alias. Later runs increment deterministically.

### Atomic alias allocation

Alias allocation must be race-safe. Do not rely on process timing. Use an atomic filesystem operation such as `O_CREAT | O_EXCL` or atomic directory creation:

```text
try claim cursor
  success -> alias = cursor
  exists  -> try cursor-2
  exists  -> try cursor-3
```

Aliases should not be reused, even after completion, because reuse makes completion-report and snapshot references ambiguous.

### Lookup semantics

Bare arguments are exact handles:

```bash
delegate snapshot cursor
delegate snapshot cursor-2
delegate snapshot del_20260520T214233Z_8f3a9c
```

`delegate snapshot cursor` means "snapshot the run whose alias is exactly `cursor`," not "latest Cursor run."

Latest lookup must be explicit:

```bash
delegate snapshot --latest cursor
```

If exact lookup fails, Delegate should return suggestions rather than guessing.

## Parent-Facing Output Contract

The key safety rule:

> Delegate should be a stream consumer, not a stream passthrough, unless the caller explicitly asks for raw output.

Switching harnesses to structured stream output must not cause raw JSONL events to be sent to the parent agent by default.

### Worker-authored completion report by default

Delegate should not call a separate LLM to summarize runs in v1. Instead, Delegate should append standard completion-report instructions to the effective prompt sent to the child harness, so the model doing the work produces a concise final report while it still has full context.

Do not mutate the user's prompt file on disk. If the user supplies `--prompt-file`, Delegate should read it, append the completion-report section in memory, and send that effective prompt to the harness.

Default appended completion-report section:

```md
## Delegate completion report requirement

When you finish, end with a concise completion report for the parent agent:

- Status: completed / blocked / failed
- What you did or found
- Files changed or reviewed
- Verification run and result
- Remaining risks or follow-ups

Keep it concise. Do not include raw logs unless explicitly relevant.
```

The worker should write the completion report in its final assistant message, not as an arbitrary file. Delegate captures that final report and stores it at:

```text
<workspace>/.delegate/runs/<runId>/completion-report.md
```

The parent agent should be pointed to this deterministic report path or to a command that resolves it:

```bash
delegate run-output cursor --completion-report
```

Delegate should not include the completion report preview in the default parent-facing response. The main agent can read the deterministic report when it needs details, and omitting the preview saves tokens. Live snapshots still rely on deterministic event/log state because the worker has not finished yet. Failed or crashed runs should fall back to deterministic stream/event/error summaries.

Tasks that require exact final output formatting need an escape hatch. Prefer an explicit option such as:

```bash
delegate --completion-report none cursor safe "return only JSON matching this schema"
delegate --no-completion-report cursor safe "return only JSON matching this schema"
```

Support both option forms. Treat `--completion-report none` as the canonical/configurable form and `--no-completion-report` as the ergonomic alias. Both disable completion-report instruction injection for exact-output tasks.

Completion-report injection should remain enabled by default, but users should be able to set a config default so agents do not need to remember an opt-out flag on every invocation in workspaces or workflows where exact-output tasks are common.

Example config shape:

```json
{
  "tracking": {
    "completionReport": {
      "defaultMode": "markdown"
    }
  }
}
```

To disable by default:

```json
{
  "tracking": {
    "completionReport": {
      "defaultMode": "none"
    }
  }
}
```

CLI flags override config. For example, a workspace can default to no completion report while a specific run can opt back in with `--completion-report markdown`.

Expose completion-report support in discovery output:

```json
{
  "completionReportModes": ["markdown", "none"]
}
```

The Delegate skill, README, and agent-facing docs must teach main agents when to keep completion reports on and when to disable them. This is important for scaling the behavior across Codex, Claude Code, and any other orchestrating agents using Delegate.

### Text mode completion output

When a run completes, Delegate should print a bounded summary:

```text
delegate run cursor completed in 8m12s
alias: cursor
status: succeeded
snapshot: delegate snapshot cursor
completion report: delegate run-output cursor --completion-report
```

### JSON mode completion output

`delegate --json ...` should return one concise JSON object, not the full stream:

```json
{
  "ok": true,
  "alias": "cursor",
  "runId": "del_20260520T214233Z_8f3a9c",
  "status": "succeeded",
  "durationMs": 492000,
  "snapshotCommand": "delegate snapshot cursor",
  "completionReportCommand": "delegate run-output cursor --completion-report",
  "completionReportPath": ".delegate/runs/del_20260520T214233Z_8f3a9c/completion-report.md",
  "stdoutBytes": 120394,
  "stderrBytes": 831
}
```

Full raw output should require explicit commands or flags, for example:

```bash
delegate run-output cursor --stdout --tail 200
delegate run-output cursor --completion-report
delegate run-output cursor --raw
```

## Snapshot Command

Primary command:

```bash
delegate snapshot <alias-or-runId>
delegate --json snapshot <alias-or-runId>
```

Discovery:

```bash
delegate runs --active --json
delegate runs --recent
delegate runs --harness cursor
delegate runs --cwd /path/to/workspace
delegate snapshot --latest cursor
```

`delegate runs` is the recovery path when a parent agent loses the alias or run ID. It should be bounded by default and must not include raw logs.

Example text output:

```text
alias      status    harness  age    current
cursor     running   cursor   12m    editing src/delegate_agent/cli.py
droid      done      droid    2h     final: found 3 issues
cursor-2   failed    cursor   1d     stderr available
```

JSON output should include a small list of run summaries with snapshot commands:

```json
{
  "schema": "delegate.runs.v1",
  "ok": true,
  "limit": 20,
  "runs": [
    {
      "alias": "cursor",
      "runId": "del_20260520T214233Z_8f3a9c",
      "status": "running",
      "harness": "cursor",
      "current": "editing src/delegate_agent/cli.py",
      "snapshotCommand": "delegate snapshot cursor"
    }
  ]
}
```

Default `delegate runs` should cap the number of rows, for example at 20. Callers can request more explicitly later if needed.

Potential parent-run launch aids:

```bash
delegate ... --run-id-file /tmp/delegate-run-id
```

This avoids fragile parsing when the launching harness captures output in unusual ways.

## Snapshot Content

Snapshots should be live-progress oriented. They should avoid raw logs and tool-result payloads, but they should include the full visible assistant text captured so far because that text is often the most important context for the parent agent to understand what the sub-agent is doing.

Visible assistant text means normal assistant message content emitted by the harness stream. It does not include hidden chain-of-thought, private reasoning fields, raw tool outputs, or raw stdout/stderr logs.

Use an emergency size guard rather than aggressive snippet caps. A reasonable v1 guard is 30,000 characters of visible assistant text per snapshot. If the captured visible assistant text exceeds the guard, Delegate should not silently dump an unbounded payload. It should truncate with explicit metadata using a head+tail strategy: first 20,000 characters plus last 10,000 characters. The beginning usually contains plan/context; the end usually contains current activity.

Suggested JSON shape:

```json
{
  "schema": "delegate.snapshot.v1",
  "ok": true,
  "alias": "cursor",
  "runId": "del_20260520T214233Z_8f3a9c",
  "harness": "cursor",
  "status": "running",
  "cwd": "/path/to/workspace",
  "mode": "work",
  "model": "composer-2.5",
  "startedAt": "2026-05-20T21:42:33Z",
  "lastActivityAt": "2026-05-20T21:48:01Z",
  "current": "Reading README.md",
  "assistantText": "I am checking the project structure...\n\nI found the parser entrypoint and am now tracing how prompt files are resolved...",
  "assistantTextChars": 1240,
  "assistantTextTruncated": false,
  "assistantTextLimitChars": 30000,
  "assistantTextOmittedMiddleChars": 0,
  "eventsTotal": 3,
  "eventsTruncated": false,
  "eventsLimit": 500,
  "eventsOmittedMiddle": 0,
  "recentEvents": [
    {
      "kind": "tool.started",
      "tool": "read",
      "path": "README.md"
    },
    {
      "kind": "tool.completed",
      "tool": "read",
      "path": "README.md",
      "status": "success"
    },
    {
      "kind": "tool.started",
      "tool": "read",
      "path": "README.md"
    }
  ],
  "warnings": []
}
```

Snapshots should include completion report fields only when the completion report exists. Do not include `completionReport: pending` in normal running snapshots; most snapshot calls are for live progress, and pending-report noise is not useful.

When available, include:

```json
{
  "completionReport": {
    "path": ".delegate/runs/del_20260520T214233Z_8f3a9c/completion-report.md",
    "command": "delegate run-output cursor --completion-report",
    "bytes": 1834
  }
}
```

Text output should be an agent-readable rendering of the same object:

```text
cursor · running · 5m28s elapsed
cwd: /path/to/workspace
model: composer-2.5
current: Reading README.md
assistant text:
I am checking the project structure...

I found the parser entrypoint and am now tracing how prompt files are resolved...
recent:
  - read README.md
  - read README.md
```

## Harness-Specific Observations

### Cursor Agent

Cursor supports non-interactive `--print` and `--output-format text|json|stream-json`. Stream JSON includes system initialization, assistant messages, tool call start/completion events, final result, durations, and session/request IDs.

Nuance: failure cases may emit errors to stderr and exit without a valid JSON terminal event.

### Factory Droid

Droid `exec` is headless and designed for CI/CD. Local empirical testing showed three relevant output modes:

- `--output-format json` works and emits a single final result object with `result`, `duration_ms`, `num_turns`, `session_id`, and usage metadata. This is useful for completion-only capture but not live snapshots.
- `--output-format stream-json` works and emits JSONL events including `system`, `message`, `reasoning`, `tool_call`, `tool_result`, and `completion`. This is the right v1 mode for tracked Delegate runs because it supports live activity snapshots and final completion-report capture.
- JSON-RPC-style mode is not appropriate for v1 one-shot Delegate runs. `--output-format stream-jsonrpc` alone behaved like plain text in local testing, and paired JSON-RPC input/output is a different client protocol path rather than a simple prompt-file execution mode.

Parser policy for Droid stream JSON:

- keep `system` metadata such as cwd, session ID, model, and tool list,
- keep `message` visible assistant text,
- keep `tool_call` metadata such as tool name, command/path target, summary, and risk level,
- keep `completion.finalText` as the completion report source,
- ignore `reasoning` events,
- never expose `tool_result.value` in snapshots because it can contain full command output or file contents,
- retain raw stdout/stderr locally for explicit debugging commands.

Nuance: Droid may still disobey soft prompt constraints, such as reading file contents after being asked to inspect filenames only. Snapshot safety should therefore rely on parser filtering, not only on worker prompt instructions.

### Claude Code

Claude Code supports `--print` with `--output-format text|json|stream-json`. Stream JSON can expose assistant events, partial messages when enabled, `system/init` metadata, API retry events, hook events when included, session IDs, and final result metadata.

Nuance: enabling too many event types can increase stream volume, so Delegate should request only what it needs for snapshots.

### Codex

Codex is not currently a Delegate harness in this repo, but if added later, `codex exec --json` provides JSONL events on stdout and should fit the same internal parsing model.

## Normalized Event Model

Delegate should translate harness-specific events into a small normalized vocabulary for snapshots:

```text
run.started
run.completed
run.failed
tool.started
tool.completed
file.read
file.written
command.started
command.completed
warning
error
```

The normalized event log should be capped and redacted for snapshot use. Raw streams can be stored separately for explicit debugging commands.

Tool activity metadata should include which tools were called and their targets, but never the tool result payload. For example, repeated read calls should be represented as repeated events, listing the file path once per call even when the same file was read multiple times.

Snapshots should include all normalized tool/activity metadata by default until an emergency event-count guard is reached. A reasonable v1 guard is 500 events. If a run exceeds that guard, use head+tail truncation: first 100 events plus last 400 events. Include explicit metadata such as `eventsTotal`, `eventsTruncated`, `eventsLimit`, and `eventsOmittedMiddle`.

Include minimal context:

```json
{
  "kind": "tool.completed",
  "tool": "read",
  "target": "src/delegate_agent/cli.py",
  "status": "success",
  "durationMs": 41
}
```

Allowed metadata examples:

- tool name,
- call status,
- file path or command string when that is the target,
- duration,
- exit code,
- byte/line counts,
- high-level error class.

Disallowed by default:

- full file contents,
- full command stdout/stderr,
- full diffs or patch bodies,
- raw JSON tool payloads,
- secrets or auth material.

## Token and Safety Rules

- Never dump full harness streams in default parent-facing output.
- Do not include completion report previews in default parent-facing output.
- Store worker-authored completion reports at deterministic registry paths.
- Include full visible assistant text in snapshots by default, subject only to an emergency size guard.
- Never include hidden chain-of-thought, private reasoning fields, raw tool outputs, or raw logs as assistant text.
- Include all normalized tool/activity metadata by default, including repeated calls, until the emergency event-count guard is reached.
- If event metadata exceeds 500 events, use first-100 plus last-400 head+tail truncation with explicit metadata.
- Redact secrets, auth headers, and obvious credential values in snapshot/run-list metadata by default.
- Treat stderr as diagnostics, not necessarily failure.
- Treat stdout as potentially sensitive because structured tool events can include file contents.
- Avoid raw `readToolCall` content in snapshots by default.
- Avoid raw `writeToolCall.fileText` content in snapshots by default.
- Include byte counts and pointers to explicit retrieval commands instead of dumping logs.
- Capture raw logs locally by default, but archive/compact them automatically only after documented age thresholds. Size thresholds should warn, not immediately archive.
- Large-log warnings are shown to whoever requests a snapshot or run listing, not proactively sent to the harness/sub-agent.

Raw local logs are retained unredacted for local forensic/debug use. Redaction applies to agent-facing metadata surfaces such as `delegate snapshot` and `delegate runs`, because those outputs are likely to be injected into a parent model context. Provide an explicit escape hatch for trusted local inspection:

```bash
delegate snapshot cursor --no-redact
delegate run-output cursor --raw
```

Redaction should also be configurable for users who want a different workspace default, but the safe default remains redacted.

## Agent Roles and Workflows

### Parent agent

The parent agent launches a bounded Delegate Run and receives a concise alias/run ID plus completion summary. It should use `delegate snapshot <alias>` for progress checks rather than reading raw logs.

### Delegate CLI

Delegate owns launch normalization, alias allocation, run registry writes, harness stream capture, event normalization, snapshot rendering, and bounded parent-facing output.

Delegate does not own the harness's native reasoning, execution policy, permission semantics, or internal worker orchestration.

### Harness

The harness performs the actual agent work. It emits stdout/stderr in text or structured form. Delegate consumes this output and normalizes only the monitoring surface.

### Future skill/tool surface

The Delegate agent skill should teach agents:

- how to spawn bounded work,
- how to identify the returned alias,
- how to request snapshots,
- how worker-authored completion report instructions are injected by default,
- when and how to disable completion report injection for exact-output tasks,
- when to use `runs --active`,
- how to retrieve completion reports and raw output safely,
- and when not to request raw streams.

This is a required implementation-phase update, not optional polish.

## Likely Implementation Phases

### Phase 1: Registry and aliases

- Add project-local `.delegate/runs/` registry.
- Allocate unique run IDs and non-reused aliases.
- Add `runs` and exact lookup primitives.
- Preserve current harness argv behavior where possible.

### Phase 2: Streaming runner

- Replace one-shot `subprocess.run` execution with streaming capture.
- Write stdout/stderr logs and incremental state.
- Preserve bounded parent-facing output.

### Phase 3: Snapshot command

- Add `delegate snapshot`.
- Add JSON and text renderers.
- Add capped recent events and completion report retrieval.

### Phase 4: Harness adapters

- Cursor stream JSON parser.
- Claude stream JSON parser.
- Droid structured output parser.
- Optional Codex adapter if/when Codex becomes a Delegate harness.

### Phase 5: Skill and docs update

- Update Delegate skill instructions.
- Update README command examples.
- Add agent-focused usage docs.
- Add tests for token-safe output behavior.
