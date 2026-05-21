# Development notes

Delegate Agent is intentionally small:

- `src/delegate_agent/cli.py` contains the CLI parser, validation, request builder, and child-process execution. `cursor safe` wraps execution in a temporary isolated workspace (git worktree or directory copy), writes `.cursor/cli.json` only there, and restores JSON `cwd` / `executionCwd` fields for the source vs isolated paths.
- `src/delegate_agent/run_registry.py`, `runner.py`, `rendering.py`, and `retention.py` implement workspace-local run tracking, bounded parent output, snapshots, and archive-only retention.
- `bin/delegate.py` runs the checkout directly without installing it.
- `config.example.json` documents safe default configuration shape, including `tracking.retention` and `tracking.completionReport`.
- `tests/` covers parser, validation, command construction, execution output, snapshots, retention, and static safety guards.

The live runtime used by an operator may be separate from this checkout. Do not update an installed shim or runtime as a side effect of normal development. Verify with `python3 bin/delegate.py` from the repo root; promote to `~/.delegate` only after review and explicit operator request.
