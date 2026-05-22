---
name: delegate-dev
description: Run the delegate CLI against this dev checkout safely — never touches the installed `~/.delegate/` runtime or `~/.local/bin/delegate` launcher. Use when invoking `delegate` subcommands during development or testing.
---

# delegate-dev

Invoke the delegate CLI from the dev checkout. Strictly preserves the live-runtime separation enforced by `AGENTS.md`.

## Always use this entry point

```bash
python3 bin/delegate.py <args>
```

`bin/delegate.py` prepends `src/` to `sys.path` and calls `delegate_agent.cli:main`. It does not touch `~/.delegate/`, the installed launcher, or any user-site Python install.

Equivalent fallback if `bin/delegate.py` is unavailable for some reason:

```bash
PYTHONPATH=src python3 -m delegate_agent.cli <args>
```

## Never do these

- `delegate …` (bare command) — that hits the installed shim at `~/.local/bin/delegate`, which is being used by other agents.
- `pip install -e .` against the user environment.
- Write to `~/.delegate/`, `~/.local/bin/`, or anywhere outside the repo.
- Overwrite `config.json` at the repo root or in `~/.delegate/`. Edit `config.example.json` if updating the documented default shape.

## Helpful flags

- `--json` — machine-readable output (default-friendly for inspection).
- `describe` — dump effective config, resolved binaries, mode mappings, and policy metadata; safe smoke test.
- `cursor safe …` — isolated read-only review run (workspace gets copied to a temp dir; original is never passed to Cursor).
- `codex safe …` — isolated read-only Codex review run (same source-workspace protection as Cursor safe).
- `cursor work …` / `droid work …` / `codex work …` — edit-capable runs.
- `dry-run codex work …` — inspect effective Codex argv/policy without launching Codex.

## Workspace state

Run registry lives at `.delegate/index.json` + `.delegate/runs/<runId>/` and `.delegate/archive/<runId>.tar.gz`. In git workspaces, these paths get added to `.git/info/exclude` (not `.gitignore`) — don't move them into `.gitignore`.

## Quick smoke

```bash
python3 bin/delegate.py --json describe
```

If that prints resolved Cursor/Droid/Codex metadata and the merged config, the dev checkout is wired up correctly.
