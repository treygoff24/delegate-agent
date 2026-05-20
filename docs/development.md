# Development notes

Delegate Agent is intentionally small:

- `src/delegate_agent/cli.py` contains the CLI parser, validation, request builder, and child-process execution. `cursor safe` wraps execution in a temporary isolated workspace (git worktree or directory copy), writes `.cursor/cli.json` only there, and restores JSON `cwd` / `executionCwd` fields for the source vs isolated paths.
- `bin/delegate.py` runs the checkout directly without installing it.
- `config.example.json` documents safe default configuration shape.
- `tests/` covers parser, validation, command construction, execution output, and static safety guards.

The live runtime used by an operator may be separate from this checkout. Do not update an installed shim or runtime as a side effect of normal development.
