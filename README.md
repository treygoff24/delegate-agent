<p align="center">
  <img src="docs/assets/delegate-agent-header.png" alt="Delegate Agent" width="100%">
</p>

# Delegate Agent

Delegate Agent is a small CLI for handing a bounded task to another coding-agent runtime. It normalizes common calls to Cursor Agent, Factory Droid, OpenAI Codex, Claude Code, and Kimi Code so humans or other agents can launch review, investigation, and implementation jobs without remembering each tool's flags.

Use it when you want a predictable wrapper around prompts like:

- "Review this diff and report risks. Do not edit."
- "Investigate this failure in an isolated copy."
- "Implement this scoped change, run the named check, and report changed files."

Delegate does **not** commit, push, merge, deploy, publish, or run a background service. It builds the child command, adds safety framing, launches the selected runtime, and records local run metadata for later inspection.

Prompt handling is provider-specific: Codex and Claude prompts are delivered to the child
runtime over stdin; Droid prompts are delivered through a private temporary
prompt file using Droid's `--file` option; Cursor Agent and Kimi Code currently
require prompt argv. Delegate redacts Cursor and Kimi prompt argv in dry-run
output and run manifests, but true process-argv hiding for those harnesses
depends on the child CLIs exposing stdin or prompt-file transport.

## Install from source

Delegate requires Python 3.11 or newer. It is currently documented as a GitHub-source install, not a PyPI package.

```bash
python3 -m pip install "delegate-agent @ git+https://github.com/treygoff24/delegate-agent.git"
```

For local development or a checkout-only smoke test:

```bash
git clone https://github.com/treygoff24/delegate-agent.git
cd delegate-agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python3 bin/delegate.py --json describe
```

CI currently validates on Linux with Python 3.11, 3.12, 3.13, and 3.14. Windows support is not claimed until it is covered by tests.

## Prerequisites

Delegate wraps other CLIs. Install and authenticate only the runtimes you plan to call:

```bash
command -v agent   # Cursor Agent CLI (default model: Cursor Composer), used by delegate cursor ...
command -v droid   # Factory Droid CLI, used by delegate droid ...
command -v codex   # OpenAI Codex CLI, used by delegate codex ...
command -v claude  # Claude Code CLI, used by delegate claude ...
command -v kimi    # Kimi Code CLI, used by delegate kimi ...
```

Runtime authentication is owned by each child CLI. Delegate cannot log in for you. Dry-runs and CI tests do not require the real child binaries.

Copy the example config and replace placeholder Droid model IDs before real Droid runs:

```bash
mkdir -p ~/.delegate
cp config.example.json ~/.delegate/config.json
$EDITOR ~/.delegate/config.json
```

Inspect what Delegate sees:

```bash
delegate --version       # installed version — include this in bug reports
delegate --json describe --summary --redacted
delegate --json models --summary --redacted
delegate --json describe
delegate --json models
delegate --json capabilities
```

Discover commands as you go: `delegate <command> --help` prints focused help for any command path, and `delegate --json <command> --help` returns an agent-friendly spec of its usage, arguments, and options. `delegate --json describe` includes a `commands` catalog of the whole surface. `delegate --json capabilities` reports reasoning-effort support without launching a child runtime.

From this development checkout, use `python3 bin/delegate.py ...` instead of an installed `delegate` shim.

## Quickstart

Preview the command without launching a child runtime:

```bash
delegate --json dry-run codex safe "Review this repository. Do not edit files."
delegate --json dry-run claude safe "Review this repository. Do not edit files."
```

Run a read-only review in an isolated temporary workspace:

```bash
delegate codex safe "Review this repository for correctness risks. Do not edit files."
delegate claude safe "Review this repository for correctness risks. Do not edit files."
delegate cursor safe "Review the current diff for regressions. Do not edit files."
delegate kimi safe "Review this repository for regressions. Do not edit files."
```

Run an edit-capable task in a workspace you trust:

```bash
delegate cursor work "Fix the parser bug. Run python3 -m unittest tests.test_delegate_parser. Report changed files."
delegate claude work "Implement the scoped change and run the named check. Report changed files."
delegate kimi work "Implement the scoped change and run the named check. Report changed files."
```

For long foreground runs, add `--progress` to emit bounded parent heartbeats to
stderr while keeping final stdout machine-readable:

```bash
delegate --json claude safe --progress "Review this repository. Do not edit files."
```

Run through JSON input for agent callers after copying an example and setting a real `cwd`:

```bash
cp examples/task.codex.json /tmp/delegate-task.json
$EDITOR /tmp/delegate-task.json
delegate --json run --input-json /tmp/delegate-task.json
```

Reasoning effort is provider-aware. Unsupported combinations fail before launch. It changes only the requested model thinking depth/cost/latency; it does not change safe/work mode, sandboxing, approvals, or edit capability. Codex/Droid validate effort against a resolved model capability table, Cursor maps effort to configured model selection, and Claude maps directly to Claude Code `--effort` (`low`, `medium`, `high`, `xhigh`, `max`). For example, after configuring `codex.defaultModel`:

```bash
delegate --json dry-run codex safe --reasoning-effort high "Review this repository. Do not edit files."
delegate --json dry-run claude safe --reasoning-effort high "Review this repository. Do not edit files."
```

Inspect tracked output by alias:

```bash
delegate runs --recent
delegate snapshot <alias-or-runId>
delegate run-output <alias-or-runId>
```

`run-output` defaults to the best available parent-facing output: a completion
report when present, a recovered final assistant message when possible, or
bounded stdout/stderr diagnostics. Use `--completion-report` when you want that
selector explicitly.

## Safe mode, work mode, and worktree isolation

Delegate separates three ideas:

| Concept | Meaning |
| --- | --- |
| Mode | `safe` is for review/investigation; `work` is edit-capable. |
| Isolation | The child runtime can run in the source workspace, a temporary copy/worktree, or a persistent Git worktree. |
| Runtime policy | Extra flags passed to the child runtime, such as Codex `--sandbox read-only`. |

Defaults are intentionally conservative for review paths:

- `delegate cursor safe`, `delegate codex safe`, `delegate claude safe`, `delegate droid ALIAS safe`, and `delegate kimi safe` run in an isolated temporary workspace.
- Claude safe mode invokes `claude -p` with prompt text on stdin, `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob plus selected read-only Bash tools, and `--no-session-persistence` by default.
- `work` mode can edit. By default it runs in the real workspace for backward compatibility.

For edit-capable isolation, use a persistent Git worktree:

```bash
delegate --isolation worktree cursor work "Implement the scoped change and run the named check."
delegate --isolation worktree cursor work --forbid-commit "Implement the scoped change without creating commits."
delegate worktree list
delegate worktree show <alias-or-runId>
delegate worktree remove <alias-or-runId>
```

Worktree isolation protects the source checkout from ordinary relative-path edits. It is **not** a full security sandbox; the child process can still use its runtime permissions, credentials, network access, and absolute paths according to the environment and runtime policy.

Persistent worktree completions and `worktree list/show` include a work summary
with dirty state, changed file counts, diff stat, and commits created by the
child. `--forbid-commit` fails the run if the child creates commits.

Temporary safe isolation preserves internal symlinks, but replaces symlinks that point outside the source workspace with inert placeholder files inside the isolated workspace. Delegate reports a warning listing the relative symlink paths it blocked. In Git repositories with no commits yet, Cursor/Codex/Claude/Droid/Kimi safe isolation falls back to a directory copy because Git cannot create a detached worktree from an unborn `HEAD`.

Snapshots and `run-output` redact common credential shapes by default, including authorization headers, bearer/basic tokens, JWT-like strings, and common `token=` / `api_key=` / `password=` values. Use `--no-redact` only when exact output is necessary and safe to display.

## Useful docs

- [Agent setup](docs/agent-setup.md): human and non-interactive setup flows.
- [CLI reference](docs/cli-reference.md): commands, exit codes, and JSON contracts.
- [Configuration](docs/configuration.md): config precedence, sections, and provider-neutral aliases.
- [Security model](docs/security-model.md): boundaries, limitations, and safe usage.
- [Worktrees](docs/worktrees.md): persistent-worktree lifecycle and cleanup.
- [Troubleshooting](docs/troubleshooting.md): common failures and checks.
- [Contributing](CONTRIBUTING.md) and [Security](SECURITY.md).

## Limitations

- Delegate is an alpha CLI wrapper. Child runtimes can change their own flags or behavior.
- Safe mode is a policy and isolation pattern, not a guarantee that a runtime cannot perform side effects outside its execution workspace.
- Persistent worktrees require a Git repository with a valid `HEAD`; work-mode worktree runs require a clean source checkout.
- `--pass-through` is incompatible with `--json` and with persistent worktree runs.
- Delegate stores local run metadata under `.delegate/`; do not commit that directory.
