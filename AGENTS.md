# Delegate Agent repository instructions

Development copy of the `delegate` CLI. `CONTRIBUTING.md` covers contributor
setup, supported platforms, packaging, and issue reporting; this file is the
agent-facing subset.

## Development entry point

Validate through the repo-local entry point, never an installed shim:

```bash
python3 bin/delegate.py --json describe
```

Do not overwrite an installed `delegate` shim or a user's live `~/.delegate`
runtime/config unless the operator explicitly asks you to install or promote a
repository change.

## Validation gate

Pytest is the only test runner. Both entry points run the same four checks
(pytest, compileall, Ruff lint, Ruff format) and report their tool versions:

```bash
scripts/gate.sh                       # uv selects the project dev extra
tests/acceptance.sh                   # existing dev environment; no uv needed
scripts/gate.sh --fast                # quick tests plus all static checks
uv run --extra dev python scripts/test.py -- tests/test_delegate_parser.py
```

Run affected tests while iterating; the coordinator runs the full gate once on the integrated candidate before merge, and lanes never run it (Trey, 2026-09-30). `--fast` is
partial regression coverage; append affected paths after `--` when useful.
The full gate refuses test filters and ignores ambient `PYTEST_ADDOPTS`.

The shared runner uses individual-test scheduling, four workers on Linux and
two on macOS, and low priority. With the estate's `testrun` installed on Linux,
it enters a named scope and caps the entire process tree at two cores. macOS
has low priority and bounded workers, not a hard CPU quota. A Git-common-dir
lock prevents overlapping runs across linked worktrees; a second run exits 75.
Worker and CPU overrides are explicit (`--workers`, `--cpu-limit`); never use
`-n auto`. Each run reports its slowest 25 phases (`--durations` changes this).

Collection imports `tests/__init__.py`, which shims `src` onto `sys.path` and
strips ambient env. `pyproject.toml` sets `testpaths = ["tests"]`, so a test
file placed anywhere else is never collected. Install tooling with the `dev`
extra (`python3 -m pip install -e ".[dev]"`); Ruff is pinned there.

## Runtime boundaries

- `safe` mode is for review/investigation and must not edit files; `work` mode
  is edit-capable.
- Temporary safe isolation protects the source checkout from ordinary
  relative-path edits, but it is not a complete host security sandbox.
- Before changing safe-mode isolation, `isolation.bwrapBinds`, or the Linux bubblewrap backend, read `docs/bwrap-safe-boundary.md`.
- Some harness sandboxes reject `rm` of even freshly created temp files; write
  scratch output to unique `mktemp` paths and skip cleanup rather than retrying
  deletion. Unique names make cleanup unnecessary.
- Persistent worktree runs are orchestrator-managed: use `delegate worktree ...`,
  never delete or move a Delegate-created worktree by hand.

## Public-repo hygiene

Never commit local runtime state (`.delegate/`), credentials, `.env` files,
private model IDs, machine-specific absolute paths, or private planning notes.
Keep examples provider-neutral and placeholder-only — real model IDs belong in
`~/.delegate/config.json` or a path referenced by `DELEGATE_CONFIG`.
