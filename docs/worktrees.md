# Worktrees

Delegate can run edit-capable child agents in persistent Git worktrees. This gives the child a separate execution workspace while leaving the source checkout unchanged by ordinary relative-path edits.

## When to use it

Use persistent worktree isolation when:

- You want a child agent to make edits without touching your current checkout.
- You want to review, cherry-pick, or merge the child work later.
- You want Delegate to keep run metadata tied to a branch and worktree path.

Use the real workspace instead when the task depends on uncommitted local files that you do not want mirrored into a worktree. Work-mode worktree runs automatically mirror a dirty checkout.

For grouped feature waves, commit between waves when they share the real
workspace. If each feature needs its own review or commit, use one persistent
worktree per feature and integrate those worktrees separately.

## Launch

```bash
delegate --isolation worktree cursor work "Implement the scoped change and run the named check."
delegate --isolation worktree codex work "Implement the scoped change and report changed files."
delegate --isolation worktree claude work "Implement the scoped change and report changed files."
delegate --isolation worktree grok work "Implement the scoped change and report changed files."
delegate --isolation worktree devin work "Implement the scoped change and report changed files."
delegate --isolation worktree opencode work "Implement the scoped change and report changed files."
delegate --isolation worktree droid work --model implementer "Implement the scoped change and report changed files."
delegate --isolation worktree kimi work "Implement the scoped change and report changed files."
```

Dirty source checkouts automatically seed the new persistent worktree with
uncommitted tracked edits and untracked non-ignored files. Delegate emits a
`dirty_source_auto_included` warning with both counts, followed by a
`dirty_source_auto_included_paths` warning naming up to five paths (dry runs show
both as `dirtySourcePreview` and a warning). `--include-dirty` remains
available as an explicit request and is a no-op when the source is clean. Safe
runs with worktree isolation (reviews) also mirror the dirty source into their
copy; the launcher gets a `dirty_source_mirrored` warning (envelope `warnings`
and the dry-run output) with the file count and up to five paths, so a review
launched while another lane is mid-edit is visible:

```bash
delegate --isolation worktree cursor work --include-dirty "Implement using my local edits."
```

Gitignored files remain excluded. An untracked symlink that points outside the
source, or at gitignored content, is replaced by a placeholder file as it is
mirrored in, with the same protections as Delegate's safe-mode workspace sync,
and the launch warning names it. A symlink that is committed to the repository
is left exactly as Git checked it out, absolute or not: replacing it would
start every run with a typechange that the next `git add -A` commits. A
worktree is not a confinement boundary (see [Security boundary](#security-boundary)),
so keeping a committed absolute symlink adds no exposure the worktree did not
already have, while safe mode, which is a boundary, is unchanged and still
blocks every external symlink in its throwaway copy. The completion payload
reports `includeDirty: true` and `syncedFiles`.

By default, a successful work-lane run retires its persistent worktree when the
end-state is clean. Failed or cancelled runs are retained for inspection.
Source dirt copied into the worktree at launch is compared
by content against a launch digest, so unchanged seeded files do not block
retirement while a child edit to a seeded file does. The `delegate/*` branch is
kept as the durable artifact. Dirty, unverifiable, or unsafe worktrees remain
on disk and the completion JSON includes `worktreeRetained` with a reason. Set
`worktrees.retireWorktreeOnCompletion` to `false` to preserve the previous
manual-cleanup behavior. If `worktrees.autoPrune.enabled` is true, its existing
merged-and-age-filtered pass also runs at completion.

Worktree inspection reads the canonical state and immutable manifest once per
run. Older snapshot files remain raw ownership evidence when present. Missing older projections can fall back to
consistent surviving records. Corrupt records, mismatched run IDs, or conflicting
branch/source/execution identities produce warnings and withhold the source
metadata required for destructive cleanup. Even forced removal cannot turn
contradictory ownership records into permission to delete a directory. Completion
retirement retains the worktree when required evidence cannot be verified.
Mutation and GC paths still reload records under their existing registry locks;
an inspection bundle is not a cached authorization to remove anything.

The `runs` listing checks every in-scope run. Terminal rows may use a small
projection in the existing index only while the state file identity matches and
no finalization journal is pending. Other rows read current state. Log sizes and
archive metadata are read only for returned rows; ordering and total counts
still cover the full selection.

A few boundaries are worth stating explicitly:

- **Tracked-but-gitignored files sync by design.** A path that is tracked in
  repo history but also matched by a `.gitignore` rule (for example, a file that
  was committed before being added to `.gitignore`) is part of the repository
  and is synced like any other tracked file. `--include-dirty` excludes only
  untracked gitignored paths, not tracked ones.
- **Dirty submodules fail preflight.** Delegate auto-syncs ordinary tracked and
  untracked non-ignored source changes, but cannot safely reproduce dirty
  submodule state, so it refuses the launch until that submodule is clean.
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

### Workspace spec: base, env, setup

A work lane in a persistent worktree can declare how its workspace is made:

```bash
delegate --isolation worktree codex work \
  --base origin/main \
  --env API_BASE=http://localhost:8080 --env-file .lane.env \
  --setup 'npm ci' \
  "Implement the scoped task."
```

- `--base REF` cuts the worktree from REF (branch, tag, or commit) instead of
  the source checkout's HEAD. The creation base recorded for ahead/behind,
  review diffs, and `--forbid-commit` is that commit. `--base` cannot be
  combined with `--include-dirty`, and dirty source files are not auto-included,
  since they are relative to HEAD rather than to REF.
- `--env NAME=VALUE` (repeatable) and `--env-file PATH` (repeatable; `NAME=VALUE`
  lines, `#` comments, optional `export`, one pair of surrounding quotes) set
  variables for the child. `--env` wins over files. Values are written only to
  the run's private `workspace-env.json`; the manifest and output show names
  (`workspaceSpec.envKeys`). Names Delegate sets itself (`DELEGATE_*`,
  `WORKSPACE_ROOT`, `TMPDIR`/`TMP`/`TEMP`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
  `KIMI_CODE_HOME`) are refused. Delegate and auth-profile variables still take
  precedence. Errors name the file and line, never the content: an env file
  cannot carry a quoted value across lines, and a line that opens a quote it
  does not close on the same line is refused rather than truncated.
- `--setup CMD` runs CMD with `/bin/sh` in the fresh worktree, under the
  launching Delegate process, before the child starts, with the run's env and
  bounded by `--timeout`, which is applied twice: setup gets the timeout, and
  the child then gets it again from its own launch, so one run can take up to
  about twice the timeout. Output goes to the run's `setup.log`. A nonzero exit
  or timeout fails the run with error `workspace_setup_failed` and
  `failureKind: workspace_setup`, keeps the worktree for inspection, and
  launches no child. Setup runs in its own process group: `delegate cancel`
  stops a run whose setup is still running (the record names the setup group
  under `setupPgid`, never as the child `pid`/`pgid`), and a launcher that is
  terminated or interrupted stops that group with it. Delegate never guesses a
  setup command. Setup output embedded in the failure message has every
  recorded `--env` value masked.

`run --input-json` accepts the same spec as `base`, `env` (an object of names
to strings), and `setup`. `resume` and `followup` re-apply the recorded env
when they attach to the worktree, whatever the resuming shell exports; `base`
and `setup` are creation-only and are not re-run. A run that failed in setup is
resumable on those terms: `resume` attaches to the kept worktree without
re-running setup.

### Resumable runs keep their worktree

A succeeded Codex or Claude work Run that used a persistent worktree keeps it
when the Run is resumable (`worktreeRetained: "resumable_session"`), even if the
end-state is clean, so `followup` and `resume` can re-enter the same tree. Codex
and Claude work Runs are resumable by default, so a fan-out of them leaves one
retained worktree per succeeded Run until you clean up (`worktree prune
--merged`, `worktree gc`, or `worktree remove`). Launch with `--no-resumable`
(or set `codex.resumable` / `claude.resumable` to `false`) when a Run's worktree
should retire itself and no native `followup` will be needed. Workflow `agent()`
children stay non-resumable unless the call passes `resumable=True`, so
workflow fan-outs are unaffected.

### Resume attachment

To continue a terminal Run that used a persistent worktree, use the Run handle
rather than launching a second persistent worktree:

```bash
delegate resume <alias-or-runId> "Continue the implementation and run the tests."
```

The resumed Run attaches to the existing execution path and records an
attachment lease. It does not create a new branch, copy dirty files, or derive
a second persistent-worktree record. The lease is released when the attached
Run reaches a terminal state. Worktree removal, pruning, and registry repair
refuse or skip a path with a live attached Run; inspect or wait for that Run
before cleanup. A missing, moved, symlinked, unregistered, or branch-mismatched
source worktree is refused rather than recreated implicitly. `--include-dirty`
is rejected on attached resume because it only controls creation-time
synchronization; ordinary resume drops the flag with a note.

## Preflight requirements

Persistent worktree work-mode runs require:

- A Git workspace.
- A valid `HEAD` commit.
- A source checkout whose tracked edits and untracked non-ignored files can be synced.
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
entries for responsiveness. Completion summaries also expose
`rawChangedFilesCount` and `seededOnlyChanges` so an orchestrator can tell when
the raw Git status consisted only of unchanged source dirt copied at launch.

`fileInspectionStatus` and `commitInspectionStatus` are each `verified` or
`unverified`, saying whether `git status` and the commit count actually ran. A
failed inspection reports zero files or no commit count rather than an error, so
read the status before treating zeros as a clean tree; `noChanges` is true only
when both inspections are `verified` and show nothing.

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

A followup, resume, or attached run does not own the worktree it used. Asking
`worktree show` about one names the run that does, following the lineage
upward through followups and resumes (up to 16 hops, with a cycle guard):
"It followed up codex-5, which followed up codex-2, which owns the
worktree", with `delegate worktree show codex-2` as the first next action. A
handle that is not in this workspace's Registry but is recorded in another
known workspace names that workspace and the exact
`delegate --cwd <workspace> worktree show <handle>` to run; it is never
followed automatically.

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

"Uncommitted changes" has one definition across `remove`, `prune`, `reap`, and
completion retirement: what a lane actually changed. Files the launch seeded
from a dirty source do not count while they still match the launch digest, and
neither do the ledger paths in `worktrees.retirementIgnoreGlobs` (default
`.beads/**` and `.papercuts.jsonl`). A worktree whose only dirt is those is
removed without `--discard-uncommitted`; the refusal, when there is one, names
only the lane's own paths. Git itself still needs its `--force` to delete a
checkout with any dirt, so Delegate applies it after its own check has passed.

### Ledger edits are saved before removal

Discounting the ledger paths keeps them from pinning a worktree, but it must not
lose a real edit to them either. So before any removal of a worktree with
changed ledger files (`.beads/issues.jsonl` edited, `.papercuts.jsonl`
appended to, and so on), Delegate copies each changed file to

```text
<Registry>/salvage/<worktree-name>-<UTC timestamp>/<path relative to the worktree>
```

where `<Registry>` is the source repository's `.delegate/` directory. It checks
the copy against the original byte for byte, and only then removes the
worktree. If a copy fails, nothing is removed and the command reports
`ledger_salvage_failed`. The JSON result of `remove` (and each entry of `prune`
and `reap`) carries `salvagePath` and `salvagedPaths`, the text output prints a
`saved N changed ledger file(s) to ...` line, and completion retirement records
`worktreeSalvagePath` on the run. Launch-seeded files that still match their
digest are not copied (the source checkout holds those bytes). Every
discounted change, including deleted ledger files and the old name of a
renamed one, is also listed in a `MANIFEST.tsv` (status, path, old path) in the
same directory, so a deletion is recorded even though there is nothing to copy;
the JSON names those paths in `salvageRemovedPaths`. If Git cannot report the
worktree's status at this point, the removal is refused
(`ledger_salvage_failed`) rather than treated as having no ledger changes. The
copy is also made with
`--discard-uncommitted`. Nothing in Delegate deletes a salvage directory:
`worktree prune`, `runs prune`, and retention only remove run directories and
worktrees, so clear old ones by hand. A source-gone `reap` deletes the path
without a copy, because Git cannot report what changed there.

Explicit override flags:

```bash
delegate worktree remove <handle> --discard-uncommitted
delegate worktree remove <handle> --force-branch
delegate worktree remove <handle> --force
delegate worktree remove <handle> --keep-branch
delegate worktree remove <handle> --kill-live
```

- `--discard-uncommitted`: remove even if uncommitted edits would be lost.
- `--force-branch`: delete an unmerged branch.
- `--force`: shorthand for both destructive overrides above. It never overrides a
  live run; on a live worktree it refuses and names `--kill-live`.
- `--keep-branch`: remove the worktree path but keep the branch.
- `--kill-live`: also remove a worktree a live run still holds (a running or
  non-terminal run, a live process group, or the launcher's lease). That run
  loses its workspace. It does not discard uncommitted work or force the branch
  by itself, and it never overrides an attached resume.

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

A run holds a lease on its worktree while its record is not terminal and the
Delegate process that launched it (`launcherPid` in the run state) is still the
same live process. Prune, `worktree list` auto-prune, completion auto-prune,
`reap`, and `remove` skip a leased worktree with reason `worktree_leased`,
even when the child's recorded pid is dead. The lease uses the same launcher
check as never-launched run sealing (a live pid that started after the run is
a reused pid, not the launcher). It ends when the record turns terminal, so a
run's own completion retirement is unaffected. `--force` does not override any
live-owner guard (`run_active`, `run_not_terminal`, `process_group_alive`,
`worktree_leased`, `nested_run_active`, `nested_registry_unreadable`): a refused
`remove` or `prune --force`
says so and names `--kill-live`, the only flag that does, on `remove`, `prune`,
and `reap`. A skipped entry in `prune` and `reap` JSON carries the same hint.
Uncommitted work is judged as described under `remove` in every one of these
verbs, so a worktree that `prune` plans is one `remove` and retirement would
also accept.

A run launched from inside a linked worktree (for example, an agent working in
its own Delegate worktree, or `--cwd` pointed at one) is registered in that
worktree's `.delegate/`, not in the parent repository's registry. `delegate
runs` in the repository's main worktree also lists the runs of every linked
worktree that has a registry, tagged with `registryWorkspace`; it reads them
through `git worktree list` and writes nothing across worktrees. Handle
commands such as `wait` and `snapshot` still resolve against the registry of
the workspace they run in, so pass `--cwd <registryWorkspace>` for those.

Because the parent's lease check only reads the parent's registry, removal and
prune also look inside the worktree itself: a `running` run in its own
`.delegate/` blocks removal as `nested_run_active` (override: `--kill-live`).
A nested registry that exists but cannot be read, or holds a run whose state
cannot be told, blocks removal as `nested_registry_unreadable` (same override):
an unreadable answer is not a "no". A worktree with no nested registry is
unaffected. Once a nested run has finished, `worktree remove` of the parent
also removes that run's worktree first, but only when it is clean and its
branch merged (`nestedRemoved` in the result lists them). The parent's
`--force`/`--discard-uncommitted`/`--force-branch` never reach a nested
worktree. If a nested worktree is dirty, unmerged or otherwise refused, nothing
is removed and the error `nested_worktrees_block_remove` names each one in
`nestedWorktrees` with its `reason` and the exact `delegate --cwd <worktree>
worktree remove <alias> [flags]` command in `nextActions`; run those by name,
then remove the parent again. Nested Registries are locked (parent, then
nested, depth first) while they are read and removed, and the running-run check
is repeated right before the parent is deleted. A nested Registry that cannot be
read or locked refuses with `nested_registry_unreadable` (even with
`--kill-live`), naming its path in `nestedRegistry`; a nested removal that fails
part-way (for example branch deletion) stops with `nested_worktree_remove_failed`
and the nested result in `nestedResult`, and the parent stays. The nested run's
record lives in the parent's `.delegate/` and goes away with it, so read or
`snapshot` a nested run's result with `--cwd <worktree>` before removing its
parent.

## Reap old pooled paths

`worktree reap` is the explicit, age-gated cleanup verb for paths retained in
the machine-wide pool. It requires exactly one selector and an age threshold:

```bash
delegate worktree reap --handle cursor-4 --older-than 14 --dry-run
delegate worktree reap --path ~/.delegate/worktrees/abc123def456/cursor-1 --older-than 30 --yes --force
delegate worktree reap --group wave4 --older-than 14 --yes --force
```

`--path` is accepted only for an absolute pool entry exactly one fingerprint
directory below the configured `worktrees.dataHome`; symlinked fingerprint or
worktree components and paths outside the pool are refused. A filesystem alias
above the pool root is allowed when it resolves to that configured root. A dry
run never changes the pool. A mutating run also requires `--yes`, acquires the
pool lock before the registry lock without changing the pool root's mode, and
re-reads the selected entry immediately before removal.

The same owner-liveness check used by `worktree prune` and `worktree remove`
blocks active runs, live process groups, and attached resumes. An effective
`stale` run is eligible only when its child and recorded process group are
provably gone; a stale record with missing PID information remains blocked.
Source-gone paths cannot be checked for uncommitted contents, so they report
unknown dirt. Reaping one requires `--yes` plus either `--force` or
`--discard-uncommitted`; confirmation alone never authorizes deleting unknown
work. For recordless paths, `--older-than` uses the newest mtime across the root
and all descendants without following symlinks; unreadable or over-limit walks
are retained. Their branches are preserved and never touched.

### A path Git still links but no run record owns

A pool entry whose run record is gone (deleted registry, a run recorded in a
different workspace) but which Git still lists as a worktree of its source
repository used to have no exit: `reap` skipped it as `live_backlink` even with
`--force`, and the only way out was `git worktree remove --force` by hand.
`--path P --older-than N --yes --force` now removes it, after these checks, each
re-run under the locks immediately before removal:

- The pool scan has no warning for the entry. An entry whose metadata looks
  broken but was changed within the last 15 minutes (it may still be under
  construction), or whose metadata could not be read, is refused with the
  scan's reason.
- Git still lists the path as a worktree of its source repository.
- No Registry holds a record for the path. A run that registered it after the
  reap was planned is refused as `record_owns_path` (re-run the reap so that
  run's liveness check applies); one in the source repository's own Registry
  is refused as `record_in_other_registry` with the `--cwd` to run the command
  from. This check runs again under the locks, but the source repository's
  Registry is not locked, so a record written there between the check and the
  removal is not caught.
- There is no uncommitted work by the same definition as `remove`, unless
  `--discard-uncommitted` is passed (`dirty`, `dirty_unknown`).
- No process has its current directory inside the path (`process_cwd_inside`,
  with the process ids and command names), unless `--kill-live` is passed. The
  check is one pass over every process (`/proc` on Linux, `lsof -d cwd` on
  macOS), never a recursive `lsof +D`. If the scan cannot run (neither is
  available, or `lsof` fails, prints nothing, or exits 1 without naming a
  process inside the path) or cannot read one of your own
  processes, the entry is refused as `process_scan_unavailable`: finding
  nothing is not the same as looking. `--kill-live` removes it anyway. Other
  users' processes are invisible to any such scan; they are noted in a warning
  and do not block.
- Changed ledger files are saved first (see [Ledger edits are saved before
  removal](#ledger-edits-are-saved-before-removal)); if that fails the entry is
  refused as `ledger_salvage_failed`.

The branch is kept. Without `--force` the entry is still skipped as
`live_backlink`, now with a hint that names `--force`.

## Repair registry state

```bash
delegate worktree gc --dry-run
delegate worktree gc
```

`gc` reconciles registry metadata with the filesystem and Git worktree list. It does not delete paths by itself.

In dry-run mode, `gc` reports `wouldPruneSourceRoots` and classifies un-prunable worktrees with reasons such as `source_root_missing`, `worktree_metadata_missing`, `branch_missing`, and `detached_backlink`. Without `--dry-run`, it may update Delegate registry status (for example, marking missing paths as `missing` or inconsistent metadata as `unknown`) and may run `git worktree prune` to clean Git administrative metadata for already-missing paths, but it does not delete worktree directories. JSON output includes an `effects` object that makes those mutation boundaries explicit.

## Find pooled worktrees whose repository is gone

```bash
delegate worktree gc --all
delegate worktree gc --all --dry-run
delegate worktree gc --pool ~/.delegate/worktrees
```

Persistent worktrees live in a machine-global pool under `worktrees.dataHome` (default `~/.delegate/worktrees`), but they are tracked in a run registry inside the source repository. Delete the source repository and its registry goes with it, leaving a pooled worktree that no per-repository `gc` can reach. `--all` finds these by walking the pool directly and reading each worktree's `.git` backlink file as text — the ordinary Git-based checks cannot classify them, because every `git` command fails inside a worktree whose repository is gone. Because a live worktree always has a live backlink, the walk is safe to run across every repository on the machine at once. `--all` works outside a Delegate registry for the same reason.

The scan adds a `pool` object to the JSON with `dataHome`, `scannedWorktrees`, `orphans`, `emptyFingerprintDirs`, and `warnings`. Each orphan carries `worktreePath`, `fingerprint`, the recovered `sourceGitRoot` (null when the layout does not reveal it), `gitdir` (null when the worktree has no `.git` backlink), a `reason`, and a `safeAction`.

A worktree counts as an orphan only when two things are settled. Its `.git` backlink must be definitely gone or definitely unusable — absent, or readable and holding no usable `gitdir:` line. And its Git admin directory must no longer serve it: the admin directory must exist and hold a `gitdir` backfile naming this worktree. Anything that merely *could not be checked* — a `.git` entry that is a symlink, a directory, a FIFO, oversized, or unreadable; an admin directory that could not be inspected — is reported as live and surfaced in `warnings` with reason `worktree_metadata_unverifiable`. A false orphan invites someone to delete real work by hand, while a false live only under-reports, so every unresolved case resolves toward live.

Absence also has to have lasted. Creating a persistent worktree is several steps — Delegate makes the fingerprint directory, then `git worktree add` creates the worktree directory, its `.git` pointer, and the admin directory's backfile — and a scan that lands inside that window sees exactly what a deleted repository leaves behind. Since the scan runs in a different process from the one creating the worktree, and often for a different repository, it has nothing to wait on and judges by age instead: a pool entry modified within the last 15 minutes is reported in `warnings` (`worktree_unsettled`, or `fingerprint_dir_unsettled` for an empty directory) and classified on a later run. An entry whose age cannot be read at all is treated the same way. This delays reporting a genuine orphan by one run at most, since a real orphan's metadata never comes back.

Only first-level directories named like a repository fingerprint (12 lowercase hex characters, as written by `plan_worktree_path`) are treated as Delegate's. The root is whatever you passed, so anything else is skipped without being descended, and is summarized in `warnings` with reason `not_a_pool_fingerprint_dir` — pointing `--pool` at an ordinary directory yields warnings, never orphans or cleanup candidates.

The pool scan removes nothing. With the source repository gone there is no way to tell whether an orphan holds uncommitted work, so removal is a deliberate manual decision — inspect the reported path, rescue anything you need, then delete it yourself. Empty fingerprint directories, left behind when a fingerprint's last worktree was removed, are reported in `emptyFingerprintDirs` for the same hand-clearing; `gc` does not remove those either. "Empty" means the directory holds nothing at all: a fingerprint directory with loose files in it is reported in `warnings` (`fingerprint_dir_not_empty`) instead. Fingerprint directories that could not be listed also appear in `warnings` rather than being reported as empty.

Use `--pool PATH` to scan a pool root that is no longer the configured `dataHome`. An explicitly named `--pool` path must exist and be a readable directory; anything else is an `invalid_pool_root` error rather than a successful scan of nothing, including a root that becomes unreadable between validation and the walk. A configured `dataHome` that does not exist yet is not an error — it just means no pool has been created — and is reported in `pool.warnings`.

## Security boundary

Worktree isolation is source-checkout isolation, not a full sandbox. The child process may still use credentials, network access, external tools, and absolute paths according to its runtime and host permissions. See [Security model](security-model.md).
