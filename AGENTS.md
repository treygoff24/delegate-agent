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

`scripts/test-parity.sh` checks that both runners agree on the executed and
skipped counts (ran == passed + skipped, skips equal) — a drift receipt, not a
proof of identical collection; run it after adding tests that use
process-global state or skip conditions.

Use an explicit `-n` (never `-n auto`) on shared machines, and treat unittest
as the source of truth when the two disagree.

## Runtime boundaries

- `safe` mode is for review/investigation and must not edit files; `work` mode
  is edit-capable.
- Temporary safe isolation protects the source checkout from ordinary
  relative-path edits, but it is not a complete host security sandbox.
- Safe mode optionally runs zero-copy on Linux behind an experimental bubblewrap
  backend (`isolation.safeBackend` / `DELEGATE_SAFE_BACKEND=bwrap`): the workspace
  is read-only-bound with gitignore-parity masks, and every fail-closed condition
  (bubblewrap unavailable, a leaking untracked symlink, mask overflow, a missing
  or workspace-intersecting `isolation.bwrapBinds` entry, an initialized submodule)
  aborts the run instead of falling back to the copy backend. Inside the
  boundary `$HOME` and `/tmp` are tmpfs (emitted before every HOME-relative
  ro-bind, since bwrap mounts in argv order); the workspace `.delegate/`
  registry is masked and only the current run's scratch is rw-bound on top;
  only the selected engine's home override (`CODEX_HOME` / `CLAUDE_CONFIG_DIR`)
  is writable; the engine executable and its resolved target are ro-bound as
  files when they live outside the core roots; a linked worktree's common git dir is ro-bound; and
  `--pass-through` is refused because it execs outside the tracked launcher.
  Site launch surfaces are declared via `isolation.bwrapBinds`.
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
- Model decisions that need the maintainer as blocker beads (human-checkpoint-as-blocker-edge), so dependent work can't be picked up by mistake.
- Create the bead before starting substantial work; close with `--reason`.
- `bd remember` is welcome *alongside* memory files, not instead of them.
- Git behavior (what may be committed or pushed, and when) comes from the maintainer's own rules, never from beads tooling.

Do not let `bd` tooling re-inject its managed CLAUDE.md/AGENTS.md block; this section replaces it deliberately.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
