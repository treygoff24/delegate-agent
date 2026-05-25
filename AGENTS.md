# Delegate Agent repository instructions

This repository contains the development copy of the `delegate` CLI.

Do not mutate a user's live machine runtime at `~/.delegate` or any installed `delegate` shim unless the user explicitly asks to install or promote a repository change. Other agents may be actively using that live runtime.

Use repo-local tests before proposing promotion:

```bash
python3 -m unittest discover -s tests
```

## Cursor safe mode

`delegate cursor safe` is for read-only code review and investigation. It does **not** use Cursor plan/ask mode.

All Delegate execution prompts are normalized through the CLI prompt middleware before launch. That middleware always prepends a mandatory skill-review instruction requiring the child agent to inspect its full list of available skills and load/apply relevant ones. This invariant applies to direct prompts, prompt files, stdin, JSON input, dry runs, tracked runs, and pass-through runs.

**Hard boundary (workspace isolation):**

- Runs Cursor Agent in an isolated temporary copy of the workspace (detached git worktree or directory copy).
- The original resolved workspace is never passed as `--workspace`; the child agent runs in the isolated copy. Tracked runs may still write Delegate metadata under `.delegate/` in the source workspace.
- With `--json`, output reports `cwd` (source), `executionCwd` (isolated copy), and `isolatedWorkspace: true`.

**Argv exclusions:**

- No `--mode=plan`, `--mode=ask`, `--force`, or `--approve-mcps`.
- Safe uses default Cursor Agent: `-p --trust` only.

**Defense-in-depth (isolated copy only):**

- Includes the mandatory skill-review instruction.
- Prepends a read-only review instruction block to the prompt.
- Writes `.cursor/cli.json` in the isolated workspace (allow `Read(**)` and read-oriented shell helpers; deny writes and destructive shell). This does not protect the source workspace if isolation fails—treat isolation as the guarantee.

`delegate cursor work` runs in the real workspace with `--approve-mcps --force`.

**Droid safe** stays on Droid defaults in the real workspace: no `--auto`, `--use-spec`, or `--skip-permissions-unsafe`.

## Codex modes

`delegate codex safe` runs Codex in an isolated temporary workspace and passes `codex --ask-for-approval never exec --sandbox read-only`.

`delegate codex work` runs in the real workspace and defaults to `codex --ask-for-approval never exec --sandbox workspace-write -c sandbox_workspace_write.network_access=true`.

Policy profiles can enable Codex hook-trust bypass (`trusted-hooks`) or external-sandbox bypass (`external-sandbox`), but the default profile keeps sandboxing on. Workspace containment, approval behavior, network access, hook trust, and full dangerous bypass are separate controls — do not treat `external-sandbox` as a convenience toggle.

Do not promote repo changes into `~/.delegate` unless explicitly asked.

## Run registry (orchestrating agents)

When this checkout tracks Delegate runs under `.delegate/`:

- Default parent-facing output is **bounded**; raw harness streams are not returned unless the operator passes `--pass-through`.
- `--pass-through` is unsupported for any persistent worktree run (`work` mode + effective `worktree` isolation), including Droid; it fails before artifacts are created.
- Launch Delegate normally; do **not** pipe launch commands through `tail` (for example, avoid `delegate ... 2>&1 | tail -20`) just to keep output short. The default launch output is already bounded.
- Inspect runs with `delegate snapshot <alias>`, `delegate runs`, and `delegate run-output` — do **not** tail `stdout.log`, `stderr.log`, or `events.jsonl` from scripts.
- Use `--pass-through` only when raw child streaming is explicitly required. If an operator intentionally pipes Delegate output in a shell script, they should also use `set -o pipefail` so child failures are not hidden by `tail`.
- After the raw-log retention window, bulky logs move to `.delegate/archive/<runId>.tar.gz`; snapshots and alias lookup still work. Retention is archive-only (no prune/delete commands).
- Use `python3 bin/delegate.py` from this repo for development; do not overwrite `~/.delegate` or `~/.local/bin/delegate` unless the operator explicitly asks to promote.

## Persistent worktree workflow

When `--isolation worktree` is combined with `work` mode, Delegate creates a persistent Git worktree that is preserved after the child agent exits. This keeps edit-capable agent runs separate from the source checkout.

### Agent-facing prompt note

Persistent worktree runs prepend a context note after the mandatory skill-review prefix and before the user's prompt. The note tells the child agent:

> You are running in a Delegate-created isolated Git worktree. Make changes in this execution workspace only. Do not attempt to modify, merge into, or clean the source checkout. Do not delete, rename, or `git worktree remove` this workspace; the orchestrator manages worktree lifecycle. Report changed files, verification, and suggested integration steps. Your orchestrator can inspect this run via `delegate worktree show <alias>` and retire it via `delegate worktree remove <alias>` (refuses on dirty or unmerged), `delegate worktree remove <alias> --force-branch` (allow unmerged-branch deletion), `delegate worktree remove <alias> --discard-uncommitted` (DISCARDS uncommitted edits), or `delegate worktree prune --merged` for bulk integrated entries.

### Orchestrator rules for persistent worktrees

1. Use `delegate worktree show <alias>` to inspect the worktree state (porcelain status, ahead/behind both creation base and current source HEAD).
2. Use `delegate worktree remove <alias>` for single-worktree cleanup. The default refuses uncommitted changes and unmerged branches. Pass `--discard-uncommitted` (data-loss) or `--force-branch` (unmerged-branch deletion) explicitly.
3. Use `delegate worktree prune --merged` for bulk cleanup of worktrees whose branches are reachable from the current source HEAD.
4. Do **not** delete `~/.delegate/worktrees/` paths directly — this orphans registry entries and leaves stale metadata. The structured `worktree` commands keep registry state consistent.
5. Do **not** manually run `git worktree remove` or `git branch -D` on Delegate-managed worktrees unless you understand the registry implications.
6. Tests must set `HOME` to a `TemporaryDirectory` to avoid writing to the real `~/.delegate/worktrees/` directory. Subprocess tests that can create worktrees must assert the produced path is under the temporary home.

### Worktree lifecycle summary

| Stage | Action | Who does it |
| --- | --- | --- |
| Creation | `delegate --isolation worktree cursor work "..."` creates branch + worktree | Delegate runner |
| Inspection | `delegate worktree show <alias>` or `delegate worktree list` | Orchestrator / operator |
| Integration | Review diff, cherry-pick, merge — standard Git operations | Orchestrator / operator |
| Cleanup (safe) | `delegate worktree remove <alias>` (refuses if dirty or unmerged) | Orchestrator |
| Cleanup (force) | `delegate worktree remove <alias> --force` (discards edits + force-deletes branch) | Orchestrator |
| Bulk cleanup | `delegate worktree prune --merged` | Orchestrator |
| Repair | `delegate worktree gc` (reconcile registry vs disk, never deletes paths) | Orchestrator |

### Always use the dev entry point

`python3 bin/delegate.py` from this repo. Never use the bare `delegate` command — that hits the installed shim at `~/.local/bin/delegate`, which is being used by other agents. See `.claude/skills/delegate-dev/SKILL.md` for details.
