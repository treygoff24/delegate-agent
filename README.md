<p align="center">
  <img src="docs/assets/delegate-agent-header.png" alt="Delegate Agent" width="100%">
</p>

# Delegate Agent

Delegate Agent is a small CLI for handing a bounded task to another coding-agent runtime. It normalizes common calls to Cursor Agent, Factory Droid, and OpenAI Codex so humans or other agents can launch review, investigation, and implementation jobs without remembering each tool's flags.

Use it when you want a predictable wrapper around prompts like:

- "Review this diff and report risks. Do not edit."
- "Investigate this failure in an isolated copy."
- "Implement this scoped change, run the named check, and report changed files."

Delegate does **not** commit, push, merge, deploy, publish, or run a background service. It builds the child command, adds safety framing, launches the selected runtime, and records local run metadata for later inspection.

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
command -v agent   # Cursor Agent CLI, used by delegate cursor ...
command -v droid   # Factory Droid CLI, used by delegate droid ...
command -v codex   # OpenAI Codex CLI, used by delegate codex ...
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
delegate --json describe
delegate --json models
```

From this development checkout, use `python3 bin/delegate.py ...` instead of an installed `delegate` shim.

## Quickstart

Preview the command without launching a child runtime:

```bash
delegate --json dry-run codex safe "Review this repository. Do not edit files."
```

Run a read-only review in an isolated temporary workspace:

```bash
delegate codex safe "Review this repository for correctness risks. Do not edit files."
delegate cursor safe "Review the current diff for regressions. Do not edit files."
```

Run an edit-capable task in a workspace you trust:

```bash
delegate cursor work "Fix the parser bug. Run python3 -m unittest tests.test_delegate_parser. Report changed files."
```

Run through JSON input for agent callers after copying an example and setting a real `cwd`:

```bash
cp examples/task.codex.json /tmp/delegate-task.json
$EDITOR /tmp/delegate-task.json
delegate --json run --input-json /tmp/delegate-task.json
```

Inspect tracked output by alias:

```bash
delegate runs --recent
delegate snapshot <alias-or-runId>
delegate run-output <alias-or-runId> --completion-report
```

## Safe mode, work mode, and worktree isolation

Delegate separates three ideas:

| Concept | Meaning |
| --- | --- |
| Mode | `safe` is for review/investigation; `work` is edit-capable. |
| Isolation | The child runtime can run in the source workspace, a temporary copy/worktree, or a persistent Git worktree. |
| Runtime policy | Extra flags passed to the child runtime, such as Codex `--sandbox read-only`. |

Defaults are intentionally conservative for review paths:

- `delegate cursor safe` and `delegate codex safe` run in an isolated temporary workspace.
- `delegate droid ALIAS safe` runs in the real workspace using Droid's default read-oriented behavior.
- `work` mode can edit. By default it runs in the real workspace for backward compatibility.

For edit-capable isolation, use a persistent Git worktree:

```bash
delegate --isolation worktree cursor work "Implement the scoped change and run the named check."
delegate worktree list
delegate worktree show <alias-or-runId>
delegate worktree remove <alias-or-runId>
```

Worktree isolation protects the source checkout from ordinary relative-path edits. It is **not** a full security sandbox; the child process can still use its runtime permissions, credentials, network access, and absolute paths according to the environment and runtime policy.

Temporary safe isolation preserves internal symlinks, but replaces symlinks that point outside the source workspace with inert placeholder files inside the isolated workspace. Delegate reports a warning listing the relative symlink paths it blocked. In Git repositories with no commits yet, Cursor/Codex safe isolation falls back to a directory copy because Git cannot create a detached worktree from an unborn `HEAD`.

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
