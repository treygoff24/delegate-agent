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

Narrowest relevant check first, then the four commands CI runs:

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests bin
ruff check .
ruff format --check .
```

`ruff` comes from the `dev` extra (`python3 -m pip install -e ".[dev]"`);
`ruff format .` applies formatting.

## Runtime boundaries

- `safe` mode is for review/investigation and must not edit files; `work` mode
  is edit-capable.
- Temporary safe isolation protects the source checkout from ordinary
  relative-path edits, but it is not a complete host security sandbox.
- Persistent worktree runs are orchestrator-managed: use `delegate worktree ...`,
  never delete or move a Delegate-created worktree by hand.

## Public-repo hygiene

Never commit local runtime state (`.delegate/`), credentials, `.env` files,
private model IDs, machine-specific absolute paths, or private planning notes.
Keep examples provider-neutral and placeholder-only — real model IDs belong in
`~/.delegate/config.json` or a path referenced by `DELEGATE_CONFIG`.
