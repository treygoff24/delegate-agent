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
delegate cursor safe [--prompt-file PATH] [prompt...]
delegate cursor work [--prompt-file PATH] [prompt...]

delegate droid MODEL_ALIAS safe [--prompt-file PATH] [prompt...]
delegate droid MODEL_ALIAS work [--prompt-file PATH] [prompt...]

delegate codex safe [--prompt-file PATH] [prompt...]
delegate codex work [--prompt-file PATH] [prompt...]
```

Prompt sources are direct arguments, `--prompt-file`, or stdin.

### Dry-run

```bash
delegate --json dry-run codex safe "Review only."
delegate --json dry-run cursor work --prompt-file task.md
delegate --json dry-run droid reviewer safe "Investigate only."
```

Dry-run builds the request and child argv but does not launch a child runtime, create a registry run, create a branch, or create a worktree. It does not require the real child binary. It does validate config shape and model aliases. For temporary safe isolation, the dry-run argv is the planned command shape and may still show the source workspace because the temporary isolated workspace is not materialized until a real run.

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
  "argv": ["codex", "--ask-for-approval", "never", "exec", "..."],
  "isolatedWorkspace": true,
  "isolationMode": "auto",
  "effectiveIsolation": "worktree",
  "isolationLifecycle": "temporary",
  "preservedWorkspace": false
}
```

Persistent worktree dry-runs may also include `plannedBranch` and `plannedExecutionCwd`; those are plans, not created resources. Temporary safe dry-runs usually keep `plannedExecutionCwd` unset because no temporary worktree or directory copy has been created.

### JSON input

```bash
delegate --json run --input-json examples/task.codex.json
```

Supported input keys:

```json
{
  "engine": "codex",
  "mode": "work",
  "model": null,
  "cwd": "/path/to/workspace",
  "isolation": "worktree",
  "prompt": "Implement the scoped task and report changed files."
}
```

- `engine`: `cursor`, `droid`, or `codex`.
- `mode`: `safe` or `work`.
- `model`: Droid requires a configured local alias; Codex treats a non-empty string as a model override; Cursor does not accept per-run model aliases in v1.
- `cwd`: optional workspace path. Git directories resolve to the repo root.
- `isolation`: optional `auto`, `none`, or `worktree`. `null` is invalid.
- `prompt`: required task prompt.

`profile` is not accepted in run input JSON. Configure Codex profile in `codex.profile` instead.

### Discovery

```bash
delegate --json describe
delegate --json models
delegate agent-help
```

`describe` reports version, engines, modes, supported isolation values, prompt transforms, effective policy, and representative argv shapes. `models` reports configured Cursor, Droid, and Codex model settings.

### Run registry inspection

Tracked runs return bounded parent-facing output and store local metadata under `.delegate/` in the source workspace.

```bash
delegate runs [--active] [--recent] [--harness HARNESS] [--limit N]
delegate snapshot [--latest HARNESS] [--no-redact] <alias-or-runId>
delegate run-output <alias-or-runId> [--completion-report] [--stdout] [--stderr] [--tail N] [--raw] [--no-redact]
```

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
  "isolatedWorkspace": true,
  "isolationMode": "auto",
  "effectiveIsolation": "worktree",
  "isolationLifecycle": "temporary",
  "preservedWorkspace": false,
  "snapshotCommand": "delegate snapshot codex",
  "completionReportCommand": "delegate run-output codex --completion-report"
}
```

Snapshot JSON uses schema `delegate.snapshot.v1` and includes fields such as `alias`, `runId`, `harness`, `status`, `cwd`, `executionCwd`, `assistantText`, `recentEvents`, `warnings`, `exitCode`, and isolation/worktree metadata when applicable.

Run-output JSON uses schema `delegate.run-output.v1` and returns selected completion report, stdout, and/or stderr content. By default, secret-like strings are redacted unless `--no-redact` is supplied.

### Worktree management

```bash
delegate worktree list [--harness HARNESS] [--status STATUS] [--limit N] [--no-auto-prune]
delegate worktree show <alias-or-runId>
delegate worktree show --latest HARNESS
delegate worktree remove <alias-or-runId> [--discard-uncommitted] [--force-branch] [--force] [--keep-branch]
delegate worktree prune [--merged] [--older-than DAYS] [--harness HARNESS] [--include-detached] [--dry-run] [--discard-uncommitted] [--force-branch] [--force]
delegate worktree gc [--dry-run]
```

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
