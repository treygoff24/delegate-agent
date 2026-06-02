# Delegate Agent repository instructions

This repository contains the development copy of the `delegate` CLI.

## Development entry point

Use the repo-local entry point while working in this checkout:

```bash
python3 bin/delegate.py --json describe
```

Do not overwrite an installed `delegate` shim or a user's live `~/.delegate`
runtime/config unless the operator explicitly asks you to install or promote a
repository change.

## Validation

Before proposing or shipping code changes, run the narrowest relevant check and
then the full local unit suite when practical:

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests bin
ruff check .
```

Packaging changes should also build and install a wheel in a clean virtual
environment:

```bash
python3 -m build --sdist --wheel
twine check dist/*
```

## Runtime boundaries

- `safe` mode is for review/investigation and should not edit files.
- `work` mode is edit-capable.
- Temporary safe isolation protects the source checkout from ordinary
  relative-path edits, but it is not a complete host security sandbox.
- Persistent worktree runs are orchestrator-managed. Do not delete or move
  Delegate-created worktrees by hand; use `delegate worktree ...` commands.

## Public-repo hygiene

Do not commit:

- `.delegate/` run logs or local runtime state.
- `.env` files, credentials, API keys, private model IDs, or local config.
- Machine-specific absolute paths.
- Private planning notes or local agent/plugin folders.

Keep examples provider-neutral and placeholder-only. Real model IDs belong in a
private config file such as `~/.delegate/config.json` or a path referenced by
`DELEGATE_CONFIG`.
