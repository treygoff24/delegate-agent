# Worktrees

Delegate can run edit-capable child agents in persistent Git worktrees. This gives the child a separate execution workspace while leaving the source checkout unchanged by ordinary relative-path edits.

## When to use it

Use persistent worktree isolation when:

- You want a child agent to make edits without touching your current checkout.
- You want to review, cherry-pick, or merge the child work later.
- You want Delegate to keep run metadata tied to a branch and worktree path.

Use the real workspace instead when the task depends on uncommitted local files that you do not want to commit or mirror into a worktree. If you do want a persistent worktree that starts from your current dirty checkout, use `--include-dirty`.

## Launch

```bash
delegate --isolation worktree cursor work "Implement the scoped change and run the named check."
delegate --isolation worktree codex work "Implement the scoped change and report changed files."
delegate --isolation worktree claude work "Implement the scoped change and report changed files."
delegate --isolation worktree grok work "Implement the scoped change and report changed files."
delegate --isolation worktree devin work "Implement the scoped change and report changed files."
delegate --isolation worktree opencode work "Implement the scoped change and report changed files."
delegate --isolation worktree droid implementer work "Implement the scoped change and report changed files."
delegate --isolation worktree kimi work "Implement the scoped change and report changed files."
```

Add `--include-dirty` when the new persistent worktree should start with the
source checkout's uncommitted tracked edits and untracked non-ignored files:

```bash
delegate --isolation worktree cursor work --include-dirty "Implement using my local edits."
```

Gitignored files remain excluded. External symlinks are blocked with the same
protections used by Delegate's safe-mode workspace sync. The completion payload
reports `includeDirty: true` and `syncedFiles`.

A few boundaries are worth stating explicitly:

- **Tracked-but-gitignored files sync by design.** A path that is tracked in
  repo history but also matched by a `.gitignore` rule (for example, a file that
  was committed before being added to `.gitignore`) is part of the repository
  and is synced like any other tracked file. `--include-dirty` excludes only
  untracked gitignored paths, not tracked ones.
- **`--include-dirty` skips the submodule-cleanliness portion of the clean
  check.** Without the flag, the preflight requires a clean source checkout
  including submodule state (`git status --ignore-submodules=none`). With the
  flag, the entire clean-source check is skipped, so dirty submodules do not
  block the launch and submodule working-tree state is not mirrored into the
  new worktree (only the source checkout's tracked diff and untracked
  non-ignored files are).
- **Keep secrets out of hardlinks.** A hardlink at a non-ignored path to
  gitignored content is indistinguishable from a regular file to Delegate's
  path-based sync: it is a non-ignored path, so `--include-dirty` syncs it by
  content, and the linked gitignored file's contents travel into the new
  worktree. Path-based exclusion cannot close this; do not place secrets in
  hardlinks at non-ignored paths.

Add `--forbid-commit` when the child should leave only uncommitted edits for
the orchestrator to inspect:

```bash
delegate --isolation worktree cursor work --forbid-commit "Implement the scoped change without creating commits."
```

With `--forbid-commit`, Delegate adds a no-commit prompt note and marks the run
failed if commits remain ahead of the creation base when the child exits.
Without it, commits are allowed but still reported in the work summary with a
warning and suggested review commands.

For work mode, `--isolation worktree` creates a persistent worktree under the Delegate data home. The default is:

```text
~/.delegate/worktrees/<repo-fingerprint>/<label>-<short-run-id>/
```

Delegate also creates a local branch named like:

```text
delegate/<label>-<short-run-id>
```

## Preflight requirements

Persistent worktree work-mode runs require:

- A Git workspace.
- A valid `HEAD` commit.
- A clean source checkout: no staged, unstaged, or untracked files, unless the launch uses `--include-dirty`.
- No `--pass-through`.

Dry-run previews the plan without creating anything:

```bash
delegate --json --isolation worktree dry-run cursor work "Implement only."
```

## Child prompt note

Delegate prepends a note telling the child agent that it is running in a Delegate-created isolated Git worktree, that it should make changes only in that execution workspace, and that the orchestrator manages merge and cleanup.

When `--forbid-commit` is active, the note also tells the child not to run
`git commit` because Delegate will fail the run if commits remain ahead of the
creation base at exit.

## Inspect

```bash
delegate worktree list
delegate worktree list --group wave4
delegate worktree show <handle>
delegate worktree show --latest cursor
```

`worktree show --latest HARNESS` resolves the most recent persistent worktree for that harness. A bare harness handle such as `cursor` does the same in the worktree domain. Both forms intentionally ignore newer non-worktree runs from the same harness. Migration note: old registries that contain a literal bare harness alias remain reachable by run ID for worktree commands.

`worktree show` reports status, path, branch, dirty state, branch merge vs full integration state, ahead/behind counts, work summary, and suggested review or cleanup commands. `worktree list` is read-only unless an enabled auto-prune pass runs before listing; JSON output includes `summary.autoPruneMode` (`disabled`, `attempted`, or `suppressed`) and `summary.readOnly`. `--group NAME` filters list output to persistent worktrees launched with that group.

`workSummary` includes dirty state, changed file count, diff stat, and commits
created by the child (`commitsCreatedCount` and `commitsCreated`). It is present
on `worktree show` and run completion payloads when Delegate can inspect the
persistent worktree; `worktree list` keeps this deep summary out of overview
entries for responsiveness.

### Integration state semantics

Worktree list/show JSON distinguishes branch merge from full integration:

| Field | Meaning |
| --- | --- |
| `branchMergedIntoSource` | Branch tip is an ancestor of current source `HEAD` (Git graph only). |
| `mergedIntoSource` | Backward-compatible branch-graph merge state; same meaning as `branchMergedIntoSource`. |
| `fullyIntegrated` | Branch merged **and** the worktree has no uncommitted changes. |
| `hasUncommittedChanges` | Same tri-state as `dirty`. |
| `integrationStatus` | Summary enum such as `fully-integrated`, `branch-merged-worktree-dirty`, `branch-unmerged`, or `branch-unmerged-worktree-dirty`. |
| `uncommittedChangesIntegrated` | `false` when uncommitted edits remain; `true` when the worktree is clean. |

Automation that needs safe retirement should require `fullyIntegrated: true` or
inspect `integrationStatus`; use `mergedIntoSource` / `branchMergedIntoSource`
when you only need the branch-graph meaning (for example, matching `worktree
remove` / `worktree prune --merged` behavior).

When a branch is merged but the worktree still has local edits, `worktree show` keeps review/diff guidance but omits no-op `mergeIntoSource` / `cherryPickRange` suggestions (ahead vs current `HEAD` is zero and remaining work is uncommitted files).

Unknown handle suggestions for `worktree show/remove` are scoped to persistent
worktrees and include `delegate worktree list` guidance. Run handles from
non-worktree launches are not suggested for worktree management commands.

Common statuses:

- `present`: worktree path exists and is registered.
- `missing`: registry points to a path that no longer exists.
- `removed`: Delegate has recorded the worktree as removed.
- `unknown`: metadata or Git state is inconsistent; inspect before cleanup.

## Integrate

Delegate does not merge for you. Use normal Git review and integration from the source checkout:

```bash
delegate worktree show <handle>
git diff <base>..<branch>
git merge <branch>       # or cherry-pick selected commits
```

Exact branch and diff suggestions are included in `worktree show` output when available.

## Remove one worktree

```bash
delegate worktree remove <handle>
delegate worktree remove --group wave4
```

Default removal refuses if the worktree has uncommitted changes or the branch is not merged into current source `HEAD`.

Explicit override flags:

```bash
delegate worktree remove <handle> --discard-uncommitted
delegate worktree remove <handle> --force-branch
delegate worktree remove <handle> --force
delegate worktree remove <handle> --keep-branch
```

- `--discard-uncommitted`: remove even if uncommitted edits would be lost.
- `--force-branch`: delete an unmerged branch.
- `--force`: shorthand for both destructive overrides.
- `--keep-branch`: remove the worktree path but keep the branch.

`worktree remove --group NAME` removes all persistent worktrees tagged with the
group, applying the same dirty/unmerged safety checks to each entry.

## Prune many worktrees

```bash
delegate worktree prune --merged --dry-run
delegate worktree prune --merged --older-than 7
delegate worktree prune --merged --include-detached --dry-run
delegate worktree prune --merged --group wave4
```

`prune` requires at least one of `--merged` or `--older-than DAYS`. It skips dirty, unknown, detached-source, and merge-check-failed entries unless you pass explicit override flags. `--group NAME` limits prune candidates to that launch group.

## Repair registry state

```bash
delegate worktree gc --dry-run
delegate worktree gc
```

`gc` reconciles registry metadata with the filesystem and Git worktree list. It does not delete paths by itself.

In dry-run mode, `gc` only reports what it would mark. Without `--dry-run`, it may update Delegate registry status (for example, marking missing paths as `missing` or inconsistent metadata as `unknown`) and may run `git worktree prune` to clean Git administrative metadata for already-missing paths, but it does not delete worktree directories. JSON output includes an `effects` object that makes those mutation boundaries explicit.

## Security boundary

Worktree isolation is source-checkout isolation, not a full sandbox. The child process may still use credentials, network access, external tools, and absolute paths according to its runtime and host permissions. See [Security model](security-model.md).
