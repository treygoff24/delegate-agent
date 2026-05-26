<p align="center">
  <img src="docs/assets/delegate-agent-header.png" alt="Delegate Agent" width="100%">
</p>

# Delegate Agent

Delegate Agent is a small command-line tool for handing a scoped task to another coding agent.

Use it when you want to say: "Cursor, review this diff", "Codex, investigate this bug in a sandbox", or "Droid, make this bounded change", without remembering each tool's flags every time.

Delegate does not commit, push, merge, deploy, or run a daemon. It builds the right command, adds a little safety framing, launches the agent, and records a local run log you can inspect later.

## Why you might want it

Agent CLIs are powerful, but their interfaces do not match. Cursor, Droid, and Codex use different flags for workspaces, models, output modes, safety settings, and non-interactive runs.

Delegate gives them one shape. For Droid, `my-model` means an alias you added to your config:

```bash
delegate cursor safe "Review this repo and report risks. Do not edit."
delegate codex safe "Investigate this bug in an isolated copy."
delegate droid my-model work "Implement the small fix and run the named test."
```

It is most useful if you already use coding agents and want a predictable way to send them small jobs from your terminal, scripts, or another agent.

## Install

Delegate is a Python package. It requires Python 3.11 or newer.

For normal use from the public repo:

```bash
python3 -m pip install git+https://github.com/treygoff24/delegate-agent.git
```

For local development:

```bash
git clone https://github.com/treygoff24/delegate-agent.git
cd delegate-agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

You can also run the checkout without installing it:

```bash
python3 bin/delegate.py --json describe
```

## Prerequisites

Delegate wraps other tools. Install and authenticate the runtimes you plan to use:

```bash
agent --help    # Cursor Agent CLI, for delegate cursor ...
droid --help    # Factory Droid CLI, for delegate droid ...
codex --help    # OpenAI Codex CLI, for delegate codex ...
```

You do not need all three. If you only use Codex, only Codex needs to be installed.

After installing Delegate, check what it sees:

```bash
delegate --json describe
delegate --json models
```

If a runtime is missing, Delegate exits with code `3` and tells you which binary it could not find.

## Quick start

Run a read-only review with Codex in an isolated temporary copy:

```bash
delegate codex safe "Read this project and tell me what looks risky. Do not edit files."
```

Run a read-only review with Cursor in an isolated temporary copy:

```bash
delegate cursor safe "Review the current diff for bugs and regressions."
```

Run a Droid investigation after adding a real model alias to `droid.models`:

```bash
delegate droid my-model safe "Investigate this failure and report likely causes. Do not edit."
```

Run a bounded edit when you trust the workspace:

```bash
delegate cursor work "Fix the parser bug. Run python3 -m unittest tests.test_delegate_parser. Report changed files."
```

Normal runs return a short summary with an alias:

```text
delegate run cursor completed in 1m12s
alias: cursor
status: succeeded
snapshot: delegate snapshot cursor
completion report: delegate run-output cursor --completion-report
```

Use the alias to inspect the run:

```bash
delegate snapshot cursor
delegate run-output cursor --completion-report
delegate run-output cursor --stderr --tail 100
```

## Mode, isolation, and policy

Delegate separates three concepts that are often confused in agent runtimes:

| Concept | Meaning |
| --- | --- |
| **Mode** | `safe` (review/investigation) vs `work` (edit-capable) |
| **Isolation** | Where the child agent runs: source workspace, temporary copy, or a persistent Git worktree |
| **Policy / Sandbox** | Additional runtime-level confinement applied inside the execution workspace (e.g. Codex `--sandbox read-only`, Cursor `-p --trust` only) |

### Default behavior

With no `--isolation` flag, Delegate uses its legacy defaults:

| Command | Where it runs | Additional confinement | What it can do |
| --- | --- | --- | --- |
| `delegate cursor safe` | isolated temporary copy | `-p --trust` only; no plan/ask/force | read-only review intent |
| `delegate codex safe` | isolated temporary copy plus Codex read-only sandbox | `--ask-for-approval never exec --sandbox read-only` | read-only review intent |
| `delegate droid MODEL safe` | real workspace | Droid default read-only; no auto/spec/unsafe | read-only review intent |
| `delegate cursor work` | real workspace | `--approve-mcps --force` | can edit files |
| `delegate codex work` | real workspace with Codex workspace-write sandbox | `--ask-for-approval never exec --sandbox workspace-write` + network access | can edit files |
| `delegate droid MODEL work` | real workspace | `--skip-permissions-unsafe` | can edit files |

Delegate may write its own local run metadata under `.delegate/` in the source workspace for tracked runs. That metadata is ignored by Git. The child agent in Cursor safe or Codex safe still receives the isolated copy, not your source tree.

### Isolation override

The `--isolation` flag lets you control where the child runs, independently of mode:

| Value | Effect |
| --- | --- |
| `auto` | Legacy defaults per the table above (explicit way to say "current behavior") |
| `none` | Force the real source workspace; disables temporary isolation for safe mode |
| `worktree` | Create a separate Git worktree; safe mode gets a temporary worktree, work mode gets a **persistent** worktree retained after the run |

Examples:

```bash
delegate --isolation worktree cursor work "Implement the fix and run the test."
delegate --isolation worktree codex safe "Review in an isolated temp worktree."
delegate --isolation none cursor safe "Review in the real workspace (use sparingly)."
```

`--isolation worktree` for work mode creates a persistent Git worktree under `~/.delegate/worktrees/` and a local branch. The source checkout is not modified by the child agent's relative-path edits.

**Important**: worktree isolation protects the source checkout from ordinary relative-path edits. It does not prevent the child runtime from running commands, using credentials available to the process, accessing the network according to its runtime and Delegate policy, or intentionally writing to absolute paths outside the execution workspace.

#### Pass-through restriction

`--pass-through` is unsupported for any persistent worktree run (work mode + effective `worktree` isolation), including Droid. The combination fails before any artifacts are created. `--pass-through` is still allowed with temporary safe-mode worktree isolation.

### Configurable defaults

Set isolation defaults in `.delegate/config.json` (per-repo) or `~/.delegate/config.json` (per-user):

```json
{
  "isolation": {
    "safe": "auto",
    "work": "none"
  },
  "worktrees": {
    "dataHome": null,
    "autoPrune": {
      "enabled": false,
      "mergedOlderThanDays": 7
    }
  }
}
```

- `isolation.safe` / `isolation.work`: one of `auto`, `none`, or `worktree`.
- `worktrees.dataHome`: override the persistent worktree root (`~/.delegate/worktrees` by default). Must be an absolute or `~/`-prefixed path, or `null`.
- `worktrees.autoPrune.enabled`: when `true`, `delegate worktree list` runs an opportunistic prune pass that removes clean, fully-merged worktrees older than `mergedOlderThanDays`. If that prune pass fails, JSON output includes `autoPrune.ok: false` and the command exits non-zero even when the list entries were rendered successfully.
- The embedded default for `isolation.work` is `none` for backward compatibility. Repos can set `isolation.work = "worktree"` to dogfood the feature.

## Persistent worktree lifecycle

When `--isolation worktree` and `work` mode are combined:

1. Delegate verifies the source is a Git workspace with at least one commit and a clean working tree (no staged, unstaged, or untracked changes).
2. It creates a persistent Git worktree at `~/.delegate/worktrees/<repo-fingerprint>/<label>-<run-id>/` with a local branch named `delegate/<label>-<short-run-id>`.
3. The child agent runs inside the worktree and receives a prompt note explaining the isolation contract.
4. After the child exits (success or failure), the worktree and branch are preserved. Delegate does **not** auto-merge, auto-commit, or auto-push.
5. Inspect, merge, or discard the work using the management commands below.

### Worktree management commands

```bash
delegate worktree list
delegate worktree show <alias-or-runId>
delegate worktree remove <alias-or-runId>
delegate worktree prune --merged --dry-run
delegate worktree gc
```

#### Exit codes

Worktree management commands exit 0 only when the top-level payload reports `ok: true`. Safety refusals and operational failures exit 2 and still emit the structured JSON payload when `--json` is set. Some cleanup failures are intentionally partial: for example, `delegate worktree remove` can return `ok: false` with `removed: true` and `pathRemoved: true` when the worktree path was removed but branch deletion failed. In that case inspect `branchRemoved`, `branchKept`, and `branchRemovalError` before retrying branch cleanup.

#### `delegate worktree list`

List persistent-worktree runs from the current workspace registry. Shows alias, status (present / missing / removed / unknown), harness, age, branch, dirty flag, and whether changes are merged into the source. `unknown` means Delegate could not fully reconcile the worktree metadata, such as a path that still exists but is missing Git metadata or whose branch no longer resolves; inspect with `delegate worktree show` before cleanup.

```bash
delegate worktree list
delegate worktree list --harness cursor --status present --limit 10
delegate worktree list --no-auto-prune
```

#### `delegate worktree show`

Deep view of a single persistent worktree. Shows porcelain status, ahead/behind counts (vs creation base and vs current source HEAD), and structured suggested-commands including review-diff and merge/cherry-pick instructions.

```bash
delegate worktree show cursor-4
delegate worktree show --latest cursor
```

#### `delegate worktree remove`

Remove one persistent worktree and optionally delete its branch.

- Default (no flags): refuses if the worktree has uncommitted changes or the branch is unmerged.
- `--discard-uncommitted`: override the dirty-worktree refusal (data-loss — uncommitted edits are lost).
- `--force-branch`: delete an unmerged branch.
- `--force`: shorthand for both `--discard-uncommitted --force-branch`.
- `--keep-branch`: remove the worktree path but keep the branch.
- If branch deletion fails after the path has been removed, the command reports `ok: false` but preserves the successful path cleanup in `pathRemoved: true`; retry or manually inspect the branch named in `branchRemovalError`.

```bash
delegate worktree remove cursor-4                    # refuses if dirty or unmerged
delegate worktree remove cursor-4 --discard-uncommitted  # discard uncommitted edits
delegate worktree remove cursor-4 --force-branch         # delete unmerged branch
```

#### `delegate worktree prune`

Bulk removal. Requires at least one of `--merged` (run the source-HEAD merge-safety pass) or `--older-than DAYS` (filtered by last activity). With `--merged`, fully merged clean worktrees remove both the path and branch; clean unmerged worktrees can still have the path removed while Delegate keeps the branch (`branchKept: "unmerged"`), and dirty, detached-source, missing, or merge-check-failed entries are skipped unless explicit override flags apply. Options: `--dry-run` to preview, `--harness` to filter by engine, `--include-detached` to include worktrees created from a detached source HEAD, and `--discard-uncommitted` / `--force-branch` for destructive overrides. `prune` skips `unknown` worktrees; inspect and remove those by alias so safety decisions stay explicit.

```bash
delegate worktree prune --merged --older-than 7 --dry-run
delegate worktree prune --merged --include-detached --dry-run
delegate worktree prune --merged --discard-uncommitted
```

#### `delegate worktree gc`

Reconcile registry entries with on-disk reality. Never deletes worktree paths itself — reports orphans for manual review.

```bash
delegate worktree gc
delegate worktree gc --dry-run
```

### Cleanup contract for agents

When an orchestrator agent spawns a persistent worktree run, it must **not** delete or rename `~/.delegate/worktrees/` paths directly. Instead, use:

```bash
delegate worktree show <alias>
delegate worktree remove <alias> [options]
delegate worktree prune --merged
```

This ensures registry metadata stays consistent and prevents orphaned branches.

## Configuration

Delegate reads config from:

```bash
~/.delegate/config.json
```

You can point at another file for one command:

```bash
DELEGATE_CONFIG=/path/to/config.json delegate --json models
```

Start from `config.example.json`. The most common things to configure are:

```json
{
  "cursor": {
    "argvPrefix": ["agent"],
    "defaultModel": "composer-2.5"
  },
  "droid": {
    "binary": "droid",
    "models": {
      "my-model": "your-droid-model-id"
    }
  },
  "codex": {
    "binary": "codex",
    "defaultModel": null
  }
}
```

Codex model selection is optional. If `codex.defaultModel` is `null`, Delegate lets the Codex CLI choose its default.

Droid needs model aliases because Droid model IDs are usually long. Your aliases can be whatever you want. Replace the placeholder in `config.example.json` before running Droid commands.

## JSON input

For longer jobs, put the request in a file:

```json
{
  "engine": "codex",
  "mode": "safe",
  "cwd": "/path/to/workspace",
  "prompt": "Review this project for release blockers. Do not edit files."
}
```

Then run:

```bash
delegate run --input-json task.json
```

For Droid, include `model`:

```json
{
  "engine": "droid",
  "model": "my-model",
  "mode": "safe",
  "cwd": "/path/to/workspace",
  "prompt": "Investigate the failing test. Do not edit files."
}
```

See `examples/` for starter files.

## Output and logs

By default, Delegate keeps parent-facing output short. Raw runtime output is stored in the workspace-local `.delegate/` registry.

Use these commands instead of tailing log files directly:

```bash
delegate runs --active
delegate runs --recent
delegate snapshot <alias-or-run-id>
delegate run-output <alias-or-run-id> --completion-report
delegate run-output <alias-or-run-id> --stdout --tail 100
```

Raw stdout and stderr are redacted by default when printed through Delegate. Use `--no-redact` only when you really need exact output and you are sure it is safe to display.

If you need the old behavior where the child runtime streams directly to your terminal, use:

```bash
delegate --pass-through cursor safe "Review this repo."
```

`--pass-through` is incompatible with `--json`.

## Development

Run the test suite from the repo root:

```bash
python3 -m unittest discover -s tests
```

Useful local checks before a release:

```bash
python3 -m unittest discover -s tests
gitleaks detect --source . --redact
trufflehog filesystem --no-update --only-verified .
```

Build smoke:

```bash
python3 -m pip install build twine
python3 -m build --sdist --wheel
python3 -m twine check dist/*
```

Do not copy this checkout into `~/.delegate` or overwrite an installed `delegate` shim unless you explicitly mean to promote a local build. Other agents may be using the installed runtime.

## Security

Delegate can launch tools that edit files and run commands. Use `work` mode only in repositories you trust.

Never commit provider tokens, API keys, `.env` files, local run logs, or machine-specific config. Report security issues through GitHub Security Advisories for this repository.

## Contributing

Small, well-tested changes are welcome. Please include tests for parser, command-building, execution, or registry behavior when you change those paths.

See `CONTRIBUTING.md` and `SECURITY.md`.

## License

MIT. See `LICENSE`.
