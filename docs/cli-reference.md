# CLI reference and JSON contracts

Use `delegate --help` for the exact command list from the installed version. Global options must appear before the subcommand.

## Global options

```text
--cwd PATH                    Resolve and run from PATH. Git directories resolve to the repo root.
--json                        Emit JSON for commands that support it.
--isolation auto|none|worktree
--pass-through                Stream raw child stdout/stderr. Incompatible with --json and persistent worktree runs.
--completion-report MODE      markdown or none.
--no-completion-report        Disable completion-report prompt injection.
```

## Commands

### Direct runtime commands

```bash
delegate cursor safe [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
delegate cursor work [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate droid MODEL_ALIAS safe [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
delegate droid MODEL_ALIAS work [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate codex safe [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
delegate codex work [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate claude safe [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
delegate claude work [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate kimi safe [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
delegate kimi work [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

Prompt sources are direct arguments, `--prompt-file`, or Delegate stdin. After
Delegate resolves the prompt, Codex and Claude prompts are passed to the child runtime over
stdin. Droid prompts are written to a private temporary prompt file and passed
with Droid's documented `--file` option. Cursor Agent currently only exposes
positional prompt input, and Kimi Code prompt mode currently uses `--prompt`,
so those launches still use argv transport; Delegate redacts Cursor and Kimi
prompt argv in dry-run output and run manifests.

`--reasoning-effort LEVEL` is optional and parsed only before prompt text begins. Unsupported model/effort pairs fail closed before launch with `unsupported_reasoning_effort`. It affects only model reasoning depth, cost, or latency; it does not change `safe`/`work` permissions, sandboxing, approvals, network policy, or edit capability. Cursor effort is model-selection based and requires `cursor.reasoningEffortModels`; Droid emits `--reasoning-effort LEVEL`; Codex emits a `model_reasoning_effort` config override after the model is resolved; Claude emits Claude Code `--effort LEVEL`. Kimi does not support reasoning effort in v1.

### `delegate claude`

Usage:

```bash
delegate [--json] [--isolation auto|none|worktree] claude {safe,work} [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

- Safe mode runs in an isolated temporary copy of the workspace (under `--isolation auto`) and uses Claude Code `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob, and selected read-only Bash tools such as `git diff`/`git status`.
- Prompt text is delivered on stdin to `claude -p`; dry-run argv and tracked run manifests do not contain the prompt.
- JSON-streaming runs use `--output-format stream-json --input-format text`; pass-through runs use `--output-format text`.
- Work mode uses `claude.workPermissionMode` from config, unless Delegate policy explicitly enables `policy.harness.claude.work.bypassApprovalsAndSandbox`, which maps to Claude `--permission-mode bypassPermissions`.
- `--reasoning-effort` maps to Claude Code `--effort` and accepts `low`, `medium`, `high`, `xhigh`, or `max`.

Examples:

```bash
delegate claude safe "Review this repo for regressions; report file/line/severity."
delegate claude work "Implement the scoped task; report changed files and tests."
delegate --isolation worktree claude work "Implement the feature in a persistent worktree."
```

### `delegate kimi`

Usage:

```bash
delegate [--json] [--isolation auto|none|worktree] kimi {safe,work} [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

- Safe mode runs in an isolated temporary copy of the workspace (under `--isolation auto`) and uses a read-only safety prompt. Delegate intentionally avoids Kimi `--plan` in safe mode. Kimi prompt mode auto-approves tool actions, so the isolation is the effective write boundary; the safety prompt is advisory.
- Work mode emits Kimi `--yolo` by default and runs in the real workspace unless you opt into worktree isolation.
- Model selection comes from `kimi.defaultModel` config or the `model` key in JSON run input; there is no CLI model alias.
- `--reasoning-effort` is unsupported for Kimi in v1.
- Kimi prompt text is passed via argv.

Examples:

```bash
delegate kimi safe "Review this repo for regressions; report file/line/severity."
delegate kimi work "Implement the scoped task; report changed files and tests."
delegate --isolation worktree kimi work "Implement the feature in a persistent worktree."
```

### Dry-run

```bash
delegate --json dry-run codex safe --reasoning-effort high "Review only."  # requires codex.defaultModel
delegate --json dry-run claude safe --reasoning-effort high "Review only."
delegate --json dry-run cursor work --prompt-file task.md
delegate --json dry-run droid reviewer safe "Investigate only."  # needs a configured 'reviewer' alias
```

Dry-run builds the request and child argv but does not launch a child runtime, create a registry run, create a branch, or create a worktree. It does not require the real child binary. It does validate config shape and model aliases, so the Droid example above only succeeds once `reviewer` maps to a real model ID — the shipped `config.example.json` uses `replace-with-` placeholders that dry-run rejects with `unconfigured_model`. For temporary safe isolation, the dry-run argv is the planned command shape and may still show the source workspace because the temporary isolated workspace is not materialized until a real run.

Typical dry-run JSON fields:

```json
{
  "ok": true,
  "dryRun": true,
  "engine": "codex",
  "mode": "safe",
  "model": null,
  "cwd": "/path/to/workspace",
  "workspaceKind": "git",
  "promptTransport": "stdin",
  "argv": ["codex", "--ask-for-approval", "never", "exec", "..."],
  "requestedReasoningEffort": "high",
  "resolvedReasoningEffort": "high",
  "reasoningEffortSource": "cli",
  "reasoningCapabilitySource": "bundled",
  "reasoningTransport": "codex-config",
  "isolatedWorkspace": true,
  "isolationMode": "auto",
  "effectiveIsolation": "worktree",
  "isolationLifecycle": "temporary",
  "isolation": "worktree temporary",
  "preservedWorkspace": false
}
```

`isolation` is a human-readable summary combining `effectiveIsolation` and `isolationLifecycle` (e.g. `"worktree temporary"`, `"worktree persistent"`, `"none"`). Depend on the structured fields rather than parsing it.

`--isolation none` is rejected for Cursor, Claude, Droid, and Kimi safe mode because it would remove the temporary workspace/config boundary those safe contracts depend on. Codex safe can use `none` because Codex still runs with its read-only sandbox.

Persistent worktree dry-runs may also include `plannedBranch` and `plannedExecutionCwd`; those are plans, not created resources. Temporary safe dry-runs usually keep `plannedExecutionCwd` unset because no temporary worktree or directory copy has been created.

### JSON input

```bash
delegate --json run --input-json examples/task.codex.json
```

The shipped example files use a placeholder `cwd` (`/path/to/workspace`); copy one and set a real `cwd` first, otherwise the run fails with `invalid_cwd`.

Supported input keys:

```json
{
  "engine": "codex",
  "mode": "work",
  "model": null,
  "cwd": "/path/to/workspace",
  "isolation": "worktree",
  "reasoningEffort": "high",
  "prompt": "Implement the scoped task and report changed files."
}
```

- `engine`: `cursor`, `droid`, `codex`, `claude`, or `kimi`.
- `mode`: `safe` or `work`.
- `model`: Droid requires a configured local alias; Codex, Claude, and Kimi treat a non-empty string as a model override; Cursor does not accept per-run model aliases in v1.
- `cwd`: optional workspace path. Git directories resolve to the repo root.
- `isolation`: optional `auto`, `none`, or `worktree`. `null` is invalid. `none` is rejected for Cursor, Claude, Droid, and Kimi safe mode; use `auto` or `worktree`.
- `reasoningEffort`: optional non-empty effort string. It overrides provider `defaultReasoningEffort` for that JSON run.
- `prompt`: required task prompt.

`profile` is not accepted in run input JSON. Configure Codex profile in `codex.profile` instead.

### Discovery

```bash
delegate --json describe
delegate --json models
delegate --json capabilities
delegate --json capabilities refresh
delegate agent-help
```

`describe` reports version, engines, modes, supported isolation values, prompt transforms, effective policy, and representative argv shapes. It also includes a `commands` catalog (each entry has `command` and `summary`) so an agent can enumerate the whole command surface in one call. `models` reports configured Cursor, Droid, Codex, Claude, and Kimi model settings.

Both `describe` and `models` include provenance fields useful for detecting installed-runtime drift:

- `runtime.version`, `runtime.modulePath`, `runtime.packageRoot`, `runtime.executable`, and `runtime.pythonExecutable`.
- `configResolution.source`, `configResolution.effectiveConfigPath`, and ordered `configResolution.layers` showing embedded, user, workspace, and `DELEGATE_CONFIG` layers when discoverable.

`capabilities` reports reasoning-effort support from config, the workspace cache, and bundled fallback data without invoking child binaries. `capabilities refresh` may invoke child CLIs, validates the discovered data, and writes `.delegate/capabilities/reasoning.json` in the resolved workspace only after a successful refresh. The cache is runtime state and should not be committed.

### Help and discovery

Every command and subcommand supports `--help` (and the `-h` alias). It prints focused help for that command path and exits 0:

```bash
delegate cursor --help
delegate cursor safe --help
delegate droid --help
delegate worktree remove --help
```

`delegate help` accepts the same paths positionally. With no arguments it prints the overview:

```bash
delegate help
delegate help worktree remove
delegate help cursor safe
```

For agents, add `--json` to get a machine-readable spec instead of prose. This is the recommended way to learn how to invoke a command without trial and error. The two forms are equivalent:

```bash
delegate --json cursor --help
delegate --json help worktree remove
```

The JSON spec uses these keys:

```json
{
  "ok": true,
  "command": "worktree remove",
  "summary": "Remove one persistent worktree and, by default, its branch.",
  "usage": ["delegate [--cwd PATH] [--json] worktree remove <alias-or-runId> [--discard-uncommitted] [--force-branch] [--force] [--keep-branch]"],
  "arguments": [{"name": "<alias-or-runId>", "required": true, "description": "Worktree handle to remove."}],
  "options": [{"flag": "--keep-branch", "argument": null, "description": "Remove the worktree but keep its branch."}],
  "examples": ["delegate worktree remove cursor"],
  "notes": ["A --help token anywhere in the args prints help and removes nothing."],
  "seeAlso": ["worktree list", "worktree prune", "worktree gc"]
}
```

The overview JSON (`delegate --json help`) returns `{ok, commands, globalOptions}`, where `commands` is the same `{command, summary}` catalog that `describe` includes.

A `--help` token triggers help only before any prompt free-text is consumed, so help works without supplying a mode, alias, or required argument (`delegate cursor --help`, `delegate droid --help`, `delegate run --help`). Once prompt capture begins, a later `--help` is prompt text: `delegate cursor work explain --help` parses as a run whose prompt is `explain --help`. To send a literal prompt that begins with `--help`, pass it through `--prompt-file` or stdin rather than as a trailing argument.

For worktree actions, a `--help` token anywhere in the args wins and performs no action — `delegate worktree remove cursor --help` prints help and removes nothing.

### Run registry inspection

Tracked runs return bounded parent-facing output and store local metadata under `.delegate/` in the source workspace.

```bash
delegate runs [--active|--running|--stale|--recent] [--harness HARNESS] [--limit N]
delegate snapshot [--latest HARNESS] [--no-redact] <alias-or-runId>
delegate run-output <alias-or-runId> [--completion-report] [--stdout] [--stderr] [--tail N] [--raw] [--no-redact]
```

`delegate runs` defaults to recent runs. `--active` preserves the legacy active view and includes both live `running` runs and `stale` runs. Use `--running` for only live tracked processes and `--stale` for runs recorded as running whose PID is missing or dead. `--active`, `--running`, `--stale`, and `--recent` are mutually exclusive.

Common JSON fields for tracked run completion:

```json
{
  "ok": true,
  "exitCode": 0,
  "alias": "codex",
  "runId": "...",
  "harness": "codex",
  "engine": "codex",
  "mode": "safe",
  "model": null,
  "cwd": "/path/to/source",
  "executionCwd": "/path/to/execution-workspace",
  "workspaceKind": "git",
  "requestedReasoningEffort": "high",
  "resolvedReasoningEffort": "high",
  "reasoningEffortSource": "cli",
  "reasoningCapabilitySource": "bundled",
  "reasoningTransport": "codex-config",
  "isolatedWorkspace": true,
  "isolationMode": "auto",
  "effectiveIsolation": "worktree",
  "isolationLifecycle": "temporary",
  "preservedWorkspace": false,
  "snapshotCommand": "delegate snapshot codex",
  "completionReportCommand": "delegate run-output codex --completion-report"
}
```

Snapshot JSON uses schema `delegate.snapshot.v1` and includes fields such as `alias`, `runId`, `harness`, `status`, `rawStatus`, `effectiveStatus`, `staleReason`, `nextActions`, `cwd`, `executionCwd`, `assistantText`, `recentEvents`, `warnings`, `exitCode`, reasoning metadata, and isolation/worktree metadata when applicable. Inspection commands do not rewrite a stale run's recorded state; they expose the raw recorded status plus the effective status computed from the current PID check.

Run-output JSON uses schema `delegate.run-output.v1` and returns selected completion report, stdout, and/or stderr content. By default, secret-like strings are redacted unless `--no-redact` is supplied.

With no selector, `run-output` prints the best available parent-facing output:
`completion-report.md` when present, a recovered final assistant message when
possible, otherwise bounded stdout/stderr tails plus diagnostics. Explicit
selectors are preserved. `--stdout` or `--stderr` without `--tail` or `--raw`
defaults to a bounded `--tail 80`; use `--raw` only when you intentionally need
the full stream.

When `completion-report.md` is absent, `run-output --completion-report` makes a
bounded best-effort attempt to recover an explicit final response from the
recorded child stdout stream using the same event parser used during live
tracking. Codex recovery only promotes an `agent_message` after the stream
reaches `turn.completed`, so progress messages are not treated as final reports.
JSON output marks recovered reports with `synthetic: true` and
`source: "stdout.log"`; text output flags them in the section header
(`=== completionReport (synthetic: recovered from stdout.log tail) ===`), and
tailed log sections carry a `(last N lines; full log B bytes)` header cue.
Synthetic recovery may fail when the stdout stream is
truncated, malformed, or lacks a completed final message. JSON failures for
explicit `--completion-report` include `diagnostics` (run status and stdout /
stderr presence and byte counts) plus `nextActions` with bounded fallback
commands before you read raw `.delegate/` files directly.

### Worktree management

```bash
delegate worktree list [--harness HARNESS] [--status STATUS] [--limit N] [--no-auto-prune]
delegate worktree show <alias-or-runId>
delegate worktree show --latest HARNESS
delegate worktree remove <alias-or-runId> [--discard-uncommitted] [--force-branch] [--force] [--keep-branch]
delegate worktree prune [--merged] [--older-than DAYS] [--harness HARNESS] [--include-detached] [--dry-run] [--discard-uncommitted] [--force-branch] [--force]
delegate worktree gc [--dry-run]
```

`worktree show --latest HARNESS` selects the latest persistent worktree for the harness, not merely the latest run overall. `worktree list` JSON includes a `summary` with status counts, registry drift counts, warning counts, `autoPruneMode`, and whether the returned operation was read-only; `summary.totalPersistentWorktrees` is always registry-wide, while `allStatusCounts` is scoped to the `--harness` filter (pre-status-filter) and `statusCounts` to the visible entries. `worktree gc` JSON includes `mode`, `effects`, per-entry `action`, and orphan `safeAction` fields to distinguish dry-run inspection from registry reconciliation; `gc` never deletes worktree directories.

Worktree JSON schemas:

- `delegate.worktree-list.v1`
- `delegate.worktree-show.v1`
- `delegate.worktree-remove.v1`
- `delegate.worktree-prune.v1`
- `delegate.worktree-gc.v1`

Worktree management exits 0 only when top-level `ok` is true. Safety refusals return structured JSON with `ok: false`, `error`, `message`, `exitCode`, and often `nextActions`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Delegate command completed successfully. For child launches, child exit code was 0. |
| 2 | Usage, config, validation, or worktree-management safety failure. JSON mode emits `ok: false`. |
| 3 | Missing child binary for a real launch. Dry-run does not require the binary. |
| Child exit code | For tracked child launches, Delegate returns the child runtime's exit code and includes it in JSON as `exitCode`. |

JSON error payloads use this shape:

```json
{
  "ok": false,
  "error": "missing_binary",
  "message": "Missing binary: codex",
  "exitCode": 3
}
```

Fields may grow over time. Agent callers should check `ok`, `error`, `exitCode`, `alias`, `runId`, and documented schema names rather than depending on object key order.
