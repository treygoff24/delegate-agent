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
python3 -m unittest discover -s tests -t .
python3 -m compileall -q src tests bin
ruff check .
ruff format --check .
```

`-t .` makes discovery import `tests/__init__.py`, which shims `src` onto
`sys.path` and strips ambient env. Unittest prints its `Ran N tests / OK`
summary to stderr — pipe with `2>&1` when capturing output.

`ruff` comes from the `dev` extra (`python3 -m pip install -e ".[dev]"`);
`ruff format .` applies formatting.

Fast local accelerator (not a gate): the dev extra also ships pytest +
pytest-xdist, so the same suite can run in parallel with

```bash
uv run --extra dev pytest -n 8 --dist loadfile
```

Use an explicit `-n` (never `-n auto`) on shared machines, and treat unittest
as the source of truth when the two disagree.

## Runtime boundaries

- `safe` mode is for review/investigation and must not edit files; `work` mode
  is edit-capable.
- Temporary safe isolation protects the source checkout from ordinary
  relative-path edits, but it is not a complete host security sandbox.
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

## Issue tracking — beads (house rules)

This project uses **bd (beads)** as the work ledger. `bd prime` for commands; `bd ready` on arrival.

- **Beads is the work graph only** — tasks, bugs, dependencies, close-reasons. **Journal, state/sitrep, and memory files are the narrative and continuity layer and we use them heavily.** Beads never replaces them; a close-reason should point at the journal entry or commit that holds the story.
- Model decisions-needed-from-Trey as blocker beads (human-checkpoint-as-blocker-edge), so dependent work can't be picked up by mistake.
- Create the bead before starting substantial work; close with `--reason`.
- `bd remember` is welcome *alongside* memory files, not instead of them.
- Git behavior comes from this room's own rules (commits ungated, pushes gated — global CLAUDE.md), never from beads tooling.

Do not let `bd` tooling re-inject its managed CLAUDE.md/AGENTS.md block; this section replaces it deliberately.
