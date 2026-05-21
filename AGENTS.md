# Delegate Agent repository instructions

This repository contains the development copy of the `delegate` CLI.

Do not mutate a user's live machine runtime at `~/.delegate` or any installed `delegate` shim unless the user explicitly asks to install or promote a repository change. Other agents may be actively using that live runtime.

Use repo-local tests before proposing promotion:

```bash
python3 -m unittest discover -s tests
```

## Cursor safe mode

`delegate cursor safe` is for read-only code review and investigation. It does **not** use Cursor plan/ask mode.

**Hard boundary (workspace isolation):**

- Runs Cursor Agent in an isolated temporary copy of the workspace (detached git worktree or directory copy).
- The original resolved workspace is never passed as `--workspace`; it is not modified by delegate.
- With `--json`, output reports `cwd` (source), `executionCwd` (isolated copy), and `isolatedWorkspace: true`.

**Argv exclusions:**

- No `--mode=plan`, `--mode=ask`, `--force`, or `--approve-mcps`.
- Safe uses default Cursor Agent: `-p --trust` only.

**Defense-in-depth (isolated copy only):**

- Prepends a read-only review instruction block to the prompt.
- Writes `.cursor/cli.json` in the isolated workspace (allow `Read(**)` and read-oriented shell helpers; deny writes and destructive shell). This does not protect the source workspace if isolation fails—treat isolation as the guarantee.

`delegate cursor work` runs in the real workspace with `--approve-mcps --force`.

**Droid safe** stays on Droid defaults in the real workspace: no `--auto`, `--use-spec`, or `--skip-permissions-unsafe`.

## Run registry (orchestrating agents)

When this checkout tracks Delegate runs under `.delegate/`:

- Default parent-facing output is **bounded**; raw harness streams are not returned unless the operator passes `--pass-through`.
- Inspect runs with `delegate snapshot <alias>`, `delegate runs`, and `delegate run-output` — do **not** tail `stdout.log`, `stderr.log`, or `events.jsonl` from scripts.
- After the raw-log retention window, bulky logs move to `.delegate/archive/<runId>.tar.gz`; snapshots and alias lookup still work. Retention is archive-only (no prune/delete commands).
- Use `python3 bin/delegate.py` from this repo for development; do not overwrite `~/.delegate` or `~/.local/bin/delegate` unless the operator explicitly asks to promote.
