# Delegate Agent

Delegate Agent is a small local CLI for handing bounded tasks to agent runtimes such as Cursor Agent, Factory Droid, and OpenAI Codex CLI. It gives operators a consistent command shape for read-only analysis (`safe`) and file-editing execution (`work`) while keeping prompts scoped and auditable.

> Status: early development. The CLI is useful today, but the public API and installation workflow may change before a stable release.

## Why this exists

Agent runtimes have different flags, model names, and safety postures. Delegate Agent wraps those differences so a human or orchestrating agent can say:

```bash
delegate cursor safe "Analyze this workspace and report findings only."
delegate cursor work "Implement the scoped fix, run the named check, and report changed files."

delegate droid minimax safe "Investigate this issue. Do not edit files."
delegate droid minimax work "Implement this bounded change and run the specified tests."

delegate codex safe "Review this workspace. Do not edit files."
delegate codex work "Implement the scoped fix, run the named check, and report changed files."
```

The CLI does **not** commit, push, merge, deploy, run a daemon, or create background jobs. It launches the selected runtime with a bounded prompt and returns the child command's result.

Every Delegate run also receives an in-memory skill-review instruction before the operator prompt. The child agent must review the full list of available skills at task start and load/apply any relevant ones; this is a product invariant, not something the parent agent has to remember.

By default, Delegate also keeps a **workspace-local run registry** under `.delegate/` so you can inspect runs without tailing raw harness streams. Default parent-facing output is bounded; use explicit commands when you need raw logs.

## Safety model

Delegate Agent has two modes:

| Mode | Intent | Cursor flags | Droid flags | Codex flags |
| --- | --- | --- | --- | --- |
| `safe` | Read-only review/analysis | `-p --trust` (no plan/ask/force/MCP auto-approve) | default read-only (no unsafe skip) | isolated workspace + `codex exec --sandbox read-only --ask-for-approval never` |
| `work` | File-writing execution in a trusted workspace | `-p --trust --approve-mcps --force` | `--skip-permissions-unsafe` | real workspace + `codex exec --sandbox workspace-write -c sandbox_workspace_write.network_access=true --ask-for-approval never` |

### Cursor safe

Optimized for code review and investigation. Uses **default Cursor Agent**, not `--mode=plan` or `--mode=ask`.

**Hard boundary — workspace isolation**

- Delegate creates a temporary isolated copy (detached git worktree or directory copy) and passes that path as `--workspace`.
- The original resolved workspace is not modified.
- Isolation is the guarantee that protects your tree; everything below is defense-in-depth inside the copy only.

**Defense-in-depth (isolated copy only)**

- All prompts first receive the mandatory Delegate skill-review instruction.
- Prepends read-only review instructions to the prompt.
- Writes `.cursor/cli.json` in the isolated workspace: allow `Read(**)` and read-oriented shell helpers (`rg`, `grep`, `cat`, etc.); deny `Write(**)`, destructive shell, and reads of common secret paths.

**Excluded Cursor flags:** `--mode=plan`, `--mode=ask`, `--force`, `--approve-mcps`.

### Cursor work

Runs in the real workspace with `--approve-mcps --force`. Review diffs afterward in Git workspaces; outside Git, manually inspect changed files.

### Droid safe

Runs in the real workspace on Droid's default read-only posture. Delegate does not pass `--auto`, `--use-spec`, or `--skip-permissions-unsafe`, so review prompts are not pushed into Droid's implementation-spec workflow.

### Droid work

Uses `--skip-permissions-unsafe` in the real workspace. Treat as intentionally powerful; use only for bounded tasks you trust.

### Codex safe

Runs OpenAI Codex CLI in an isolated temporary workspace (detached git worktree or directory copy), same hard boundary as Cursor safe. Delegate passes `codex exec --cd <isolated-copy> --sandbox read-only --ask-for-approval never`. The original workspace is not modified; JSON output reports `cwd` (source), `executionCwd` (isolated copy), and `isolatedWorkspace: true`.

Codex safe always uses `read-only` sandboxing. There is no `codex.safeSandbox` config field — safe sandbox is not configurable.

Defense-in-depth in the isolated copy: mandatory skill-review instruction, then a Codex safe prompt prefix (`do not edit, create, or delete files`), then the operator prompt.

### Codex work

Runs in the real workspace with `codex exec --sandbox workspace-write` and, when effective work policy enables network access (default), `-c sandbox_workspace_write.network_access=true`. Always passes `--ask-for-approval never` for tracked non-interactive runs unless a dangerous bypass profile is active.

Workspace containment, approval behavior (`never` vs bypass), subprocess network access, hook trust, and full dangerous bypass are separate concerns — see [Policy controls](#policy-controls) below.

## Installation for development

From a checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
delegate --json describe
```

You can also run the checkout without installing:

```bash
python3 bin/delegate.py --json describe
PYTHONPATH=src python3 -m delegate_agent.cli --json describe
```

## Configuration

By default, the CLI reads:

```bash
~/.delegate/config.json
```

Override this with:

```bash
DELEGATE_CONFIG=/path/to/config.json delegate --json models
```

Start from [`config.example.json`](config.example.json). Do not commit machine-local `config.json`, `cursor-prereq.json`, API keys, or provider credentials.

Optional workspace-local overrides live at `.delegate/config.json` (see config precedence in [`CONTEXT.md`](CONTEXT.md)).

### Local registry and retention

Each workspace gets `.delegate/` (Git workspaces: added to `.git/info/exclude`, not tracked `.gitignore`):

- `index.json` — alias and run ID lookup
- `runs/<runId>/` — manifest, state, snapshot, completion report, and recent raw logs
- `archive/<runId>.tar.gz` — older `stdout.log`, `stderr.log`, and `events.jsonl` after the raw-log retention window (default 7 days)

Retention is **archive-only**: completed runs older than the window have bulky logs moved into gzip tarballs; lightweight metadata and index entries remain. Active, running, and stale runs are never archived. There are no `prune` or `delete` commands in v1.

Delegate runs a cheap retention pass opportunistically during normal commands that touch the registry. Disable or tune via `tracking.retention` in config (`enabled`, `rawLogDays`).

**Do not** copy this development checkout into `~/.delegate` or `~/.local/bin/delegate` unless you explicitly intend to promote it; other agents may be using the live runtime ([`docs/live-runtime.md`](docs/live-runtime.md)).

### Policy controls

Delegate resolves an **effective policy** per harness and mode before building argv. Precedence (lowest to highest): built-in safe defaults → `policy.profile` expansion → explicit `policy.safe` / `policy.work` → `policy.harness.<engine>.<mode>` overrides.

| `policy.profile` | Effect |
| --- | --- |
| `safe` (default) | Safe mode: no network, no bypasses. Work mode: workspace-write sandbox with network when the harness supports it. |
| `trusted-hooks` | Same as `safe`, but work mode defaults `bypassHookTrust: true` (Codex: `--dangerously-bypass-hook-trust`; Cursor/Droid ignore). |
| `external-sandbox` | Work mode defaults full dangerous bypass plus hook trust bypass for Codex. **High risk** — see warning below. |
| `custom` | No profile expansion; only explicit `policy.safe` / `policy.work` / harness overrides apply. |

Mode policy fields (booleans on `policy.safe`, `policy.work`, or `policy.harness.<engine>.<mode>`):

- `networkAccess` — subprocess/network egress inside Codex sandbox (e.g. package installs). Distinct from native web search.
- `webSearch` — Codex global `--search` when supported.
- `bypassHookTrust` — Codex `--dangerously-bypass-hook-trust` (middle tier).
- `bypassApprovalsAndSandbox` — Codex `--dangerously-bypass-approvals-and-sandbox` (full dangerous bypass; loudest tier).

`approvalPolicy` is not a config field in v1. Tracked Codex runs always use `--ask-for-approval never` unless full bypass is enabled.

> **`external-sandbox` is not a convenience mode.** It disables Codex approvals and sandboxing and should only be used when Delegate itself is already running inside a container, VM, disposable worktree, or similarly hardened environment with controlled filesystem, credentials, and egress.

### Codex configuration

| Field | Purpose |
| --- | --- |
| `codex.binary` | Codex CLI executable (default `codex`). |
| `codex.defaultModel` | Optional default model; omitted from argv when null. |
| `codex.profile` | Optional Codex `--profile` (config-only in v1; not overridable per JSON run). |
| `codex.workSandbox` | Work-mode sandbox: `workspace-write` (default), `read-only`, or `danger-full-access`. Safe mode always uses `read-only` regardless of this setting. |
| `codex.ephemeral` | When true (default), adds `--ephemeral` to tracked JSONL runs. |
| `codex.ignoreUserConfig` | When true, passes `--ignore-user-config` on `codex exec`. |

`codex.workSandbox` behavior:

- `workspace-write` with `networkAccess: true` (work default) emits `-c sandbox_workspace_write.network_access=true`.
- `read-only` for work mode never emits the workspace-write network config.
- `danger-full-access` is an advanced/high-risk sandbox setting, distinct from `bypassApprovalsAndSandbox`; it does not emit the workspace-write network config.

There is no `codex.safeSandbox` field. Codex safe is locked to `read-only`.

### Models discovery

`delegate models` lists Cursor and Droid model aliases from config. Codex does not expose a fixed alias list through Delegate in v1. Use `delegate --json models` to read `codex.defaultModel`, `codex.profile`, and `codex.binary` from your merged config. Use `delegate --json describe` for effective policy metadata and per-harness supported/unsupported policy fields.

## Commands

```bash
delegate [--cwd PATH] [--json] [--pass-through] [--completion-report markdown|none] [--no-completion-report] \
  cursor {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] [--pass-through] [--completion-report markdown|none] [--no-completion-report] \
  droid MODEL_ALIAS {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] [--pass-through] [--completion-report markdown|none] [--no-completion-report] \
  codex {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] dry-run cursor {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] dry-run droid MODEL_ALIAS {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] dry-run codex {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] run --input-json FILE
delegate [--cwd PATH] [--json] snapshot [--latest HARNESS] [--no-redact] <alias-or-runId>
delegate [--cwd PATH] [--json] runs [--active] [--recent] [--harness HARNESS] [--limit N]
delegate [--cwd PATH] [--json] run-output <alias-or-runId> [--completion-report] [--stdout] [--stderr] [--tail N] [--raw] [--no-redact]
delegate [--json] models
delegate [--json] describe
delegate agent-help
```

### Default bounded output vs `--pass-through`

Normal harness runs return a short Delegate-owned summary (alias, status, snapshot command, completion-report command). Raw harness stdout/stderr are captured under `.delegate/runs/<runId>/` but are **not** streamed to the parent by default.

Use `--pass-through` when you need the previous raw streaming behavior. It is incompatible with `--json`.

Do not pipe normal launch commands through `tail` just to suppress noise:

```bash
# Avoid: can hide earlier errors and, without pipefail, can mask Delegate failure.
delegate cursor work --prompt-file task.md 2>&1 | tail -20

# Prefer: launch normally, then inspect the tracked run by alias.
delegate cursor work --prompt-file task.md
delegate snapshot cursor
delegate run-output cursor --completion-report
delegate run-output cursor --stderr --tail 100
```

If a script intentionally pipes Delegate output anyway, enable `set -o pipefail` so the pipeline preserves Delegate failures.

### Run inspection (preferred for orchestrating agents)

```bash
delegate snapshot cursor          # bounded status + recent activity
delegate runs --active            # running/stale runs
delegate run-output cursor --completion-report
delegate run-output cursor --stderr --tail 100
```

Do not tail launch output or `.delegate/runs/*/stdout.log` / `events.jsonl` from orchestration scripts; use `snapshot`, `runs`, and `run-output` instead.

### Completion reports

Work/safe prompts get an in-memory completion-report instruction by default (prompt files on disk are not modified). Disable with `--no-completion-report` or `--completion-report none`. Workspace config can set `tracking.completionReport.defaultMode` to `markdown` or `none`.

The skill-review instruction is separate from completion reports and is not configurable. It is prepended for direct prompts, `--prompt-file`, stdin, JSON input, dry runs, tracked runs, and `--pass-through`; prompt files on disk are not modified.

Global flags must come before the subcommand:

```bash
delegate --json --cwd /path/to/workspace dry-run droid minimax work "hello"
```

Trailing global flags such as `delegate dry-run ... --json` are rejected. Use `--prompt-file` or `run --input-json` when prompt text needs literal flag-looking tokens.

`--cwd` accepts either a Git checkout or an ordinary directory. If the directory is inside a Git repository, Delegate Agent normalizes it to the repository root. If no Git repository is present, Delegate Agent uses the directory directly, which supports document, policy, and shared-folder workspaces.

## JSON input

```json
{
  "engine": "cursor",
  "mode": "work",
  "cwd": "/path/to/workspace",
  "prompt": "Implement the scoped task. Verify with tests. Report changed files."
}
```

For Droid, include a model alias:

```json
{
  "engine": "droid",
  "model": "minimax",
  "mode": "safe",
  "cwd": "/path/to/workspace",
  "prompt": "Investigate this issue. Do not edit files."
}
```

For Codex, `model` is optional (uses `codex.defaultModel` when omitted):

```json
{
  "engine": "codex",
  "mode": "work",
  "cwd": "/path/to/workspace",
  "prompt": "Implement the scoped task. Verify with the named checks. Report changed files."
}
```

Unknown JSON keys are rejected. See [`examples/`](examples/) for starting points.

With `--json`, `cursor safe` and `codex safe` report the original workspace as `cwd`, the temporary isolated copy as `executionCwd`, and `isolatedWorkspace: true`. Other engines and modes keep `cwd` as the execution directory.

## Future enhancements

Codex `--output-last-message` is available in codex-cli 0.133.0 and may be useful for a future completion-extraction improvement. v1 uses JSONL capture to stay aligned with Delegate's existing tracked-run pipeline.

## Development

Run tests from the repository root:

```bash
python3 -m unittest discover -s tests
```

Before publishing changes, also run a secret scan and inspect for machine-local paths or credentials.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | success |
| 2 | usage or validation error |
| 3 | missing dependency/binary |
| child code | Cursor/Droid/Codex launched but failed |

## Contributing

Contributions are welcome once this project is public. Please keep changes small, include tests for CLI behavior, and preserve the core invariant: Delegate Agent should launch bounded tasks, not perform repository publishing or deployment itself.

## License

MIT. See [`LICENSE`](LICENSE).
