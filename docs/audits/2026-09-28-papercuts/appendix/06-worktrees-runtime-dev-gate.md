# Worktree lifecycle, shared ~/.delegate runtime, repo dev gate, delegate-export

## Bottom line
Most of this family is already fixed in code at 0.31.0; the ledger just never learned. The live-lane deletion cuts (08-23 to 09-23) were answered by the owner-run liveness check (0cf29a57, 08-27, hardened 08-31) and the launcher lease (2a612a37, 09-24). The dev-gate cuts were absorbed into `tests/__init__.py` one env var at a time. What is still live is structural. First, `prune` and `remove` judge "dirty" and "merged" with cruder tests than the completion-time retirement path uses (the two dirty-file cuts, cb0feb2d and 2b5657b2). Second, worktrees and their run records can be split across Registries or lost, and no command clears an orphan (82e3f1f2, 8d38327e). Third, the sanctioned promote tool has no "did I drop commits" check, and the in-repo `install-local` skill still prescribes the in-place rsync that `docs/live-runtime.md` forbids. Fourth, "the gate" is five different commands. `dlg-4l5` does not explain the test-gate cuts. The items worth doing are RC-2, RC-3 and RC-4, plus closing the stale ledger rows.

## Root causes

### RC-1: Liveness was inferred from git state and child pids, not from an ownership lease (mostly fixed)
- Symptoms: 1c964f09, 199816b3, a8664a8e, d5f44c96, f46a85bd, 9277fd13, d1b07c0e. Seven cuts, 08-23 to 09-23. Only d1b07c0e is open in the ledger; 1c964f09, d5f44c96 and 9277fd13 are marked resolved.
- Evidence (verified):
  - `_merge_base_is_ancestor` (worktree_mgmt.py:751) uses `merge-base --is-ancestor branch HEAD`. A zero-commit branch is trivially an ancestor, so it reads as "merged". That is the mechanism of the 08-25 deletions.
  - `prune_worktrees` (worktree_gc.py:56) now calls `inspect_worktree`, which first calls `_owner_run_block_reason` (worktree_mgmt.py:291). It returns `run_active`, `run_not_terminal`, `process_group_alive` or `worktree_leased`, and prune skips on it.
  - Stale is terminal only if `staleReason` is `dead_pid` and the recorded pgid is proven gone. Permission errors count as alive.
  - Prune, list auto-prune and completion auto-prune all go through this. `remove_worktree` re-inspects under the registry lock (worktree_remove.py:437).
  - 9277fd13 (stale run never prunable) is fixed by the `dead_pid` plus pgid-gone branch (worktree_mgmt.py:310-319).
- Still live at 0.31.0?: mostly no. Two residuals:
  - `--force` bypasses the owner check (`inspect_worktree` line 119, `evaluate_worktree_safety` line 177). `prune --merged --force` will still delete a running lane's worktree. docs/worktrees.md says "Only an explicit --force overrides it", so this is deliberate but unguarded.
  - d1b07c0e (09-23, Devin lanes) is not explained by the pre-08-27 bug alone. Inferred, not verified: a pid-based check misreads a run whose recorded pid or pgid is not the process doing the work (shared Devin ACP process). The 09-24 launcher lease ("even when the child's recorded pid is dead", docs/worktrees.md:~305) reads like the answer to exactly that. I could not confirm which runtime the Loom devbox ran on 09-23. The devbox runtime now has the check (`grep` count 2).
- Why it recurred: the first design inferred "safe to delete" from properties of the branch. Ownership is a fact about a run, and it was added as a filter later. Each new prune entry point (list auto-prune, completion auto-prune, `reap`) had to re-adopt it, and 8a18751f, 23463b7c and a1e26019 were needed to align them.
- Recommended fix: nothing structural. Close 1c964f09, d5f44c96 and 9277fd13 with the commits. Add a test that `prune --merged --force` on a `running` record with a live launcher still refuses, or decide that it should not. Regression proof for the whole class: a running run whose child pid and pgid are dead but whose launcher is alive must be `worktree_leased`. Check that the test exists in tests/test_worktree_safety_policy.py or test_retention.py.
- Alternative: make `--force` never override a live lease and add a separate `--kill-live`. This costs one flag and removes the last way to reproduce the incident by accident.
- Related: docs/plans/deferred.md ("Automated worktree reclamation must be a separate explicit `reap` verb"). It is consistent with the shipped design.

### RC-2: `prune` and `remove` use a cruder dirty test than completion retirement
- Symptoms: cb0feb2d (open, 09-25, 15 lanes), 2b5657b2 (open, 09-01, 16 lanes). Two cuts, one mechanism.
- Evidence (verified):
  - Completion retirement calls `inspect_worktree(..., retirement_ignore_globs=...)`, which routes to `_effective_dirty_for_retirement` (worktree_mgmt.py:380). That function discounts launch-seeded files that are byte-identical to the launch digest, plus the globs `.beads/**` and `.papercuts.jsonl` (config.py:33, default added 08-27).
  - `prune_worktrees` (worktree_gc.py:82) calls `inspect_worktree` with no globs. That falls to `dirty_info`, which is raw `git status --porcelain` (worktree_mgmt.py:737).
  - `remove_worktree` defaults `retirement_ignore_globs=None`, and prune does not pass one.
  - So `show`/`list` report `seededOnlyChanges` and a summary, and completion retirement honours the globs. The one command an operator uses for backlog cleanup ignores both. The "docs say it is excluded" complaint in 2b5657b2 is literally true of the completion path only.
- Still live?: yes.
- Why it recurs: the retirement path was built (08-26/27) with seeded-digest filtering, and the explicit verbs were never migrated onto the same inspection call. Two "is this dirty" implementations exist.
- Recommended fix: make `inspect_worktree` compute effective dirt by default from the record's `creationContext` digests plus configured `worktrees.retirementIgnoreGlobs`. Raw porcelain becomes the exception behind `--discard-uncommitted` semantics. `prune` and `remove` need no new flags. Test: a merged worktree whose only dirt is a byte-identical `.beads/issues.jsonl` and a launch-synced untracked file must appear in `planned` under `prune --merged --dry-run`. It fails today with `dirty`. Add a mutation: edit the synced file, and expect `dirty` again.
- Alternative: add `--ignore-synced` to prune only (the cb0feb2d suggestion). Cheaper, but it leaves two dirt definitions and a flag every coordinator must remember.
- Related: docs/configuration.md:893 pins cleanup authority per workflow, so the change must not widen what a pinned workflow may remove.

### RC-3: Worktrees are keyed to whichever Registry launched them, and orphans have no exit
- Symptoms: 82e3f1f2 (open), 8d38327e (open), b0fb2d (open, remove never checks for a process cwd inside). 920ff3d0 is fixed (below).
- Evidence (verified):
  - `worktree list/prune/remove` operate on the single Registry of the workspace they run in (`_list_payload`, worktree_commands.py:33). Only `runs` reads through `linked_registry_roots` (inspection_commands.py:34,198). docs/worktrees.md:~315 says handle commands still resolve against one Registry. So a run launched with `--cwd <delegate-worktree>` registers its own worktree under that worktree's `.delegate/`, and removing the parent hides the child (82e3f1f2).
  - `unknown_handle` and `unknown_worktree_path` errors say "use `reap --path`" (worktree_mgmt.py:1247-1257, 1330). But `reap` classifies a record-less path that is not a backlink-orphan as live and skips it with reason `live_backlink`, even under `--force` (worktree_gc.py:575-583, comment "conservatively treated as live"). This is the 8d38327e dead end. It restates the deferred.md ruling ("no shipped command can clear such a record") for the git-linked case.
  - No `/proc`, `lsof`, or cwd scan exists in worktree_*.py or retention.py (grep empty). b0fb2d is unaddressed.
- Still live?: yes for all three.
- Why: ownership is recorded per Registry, and a Registry is a directory. Nested launches and lost records make the git worktree list the only global truth, and the CLI treats git's view as untrusted for deletion (a deliberate safety choice in deferred.md).
- Recommended fix, smallest that closes the dead end: let `reap --path ... --force --yes` remove a git-linked, record-less pool entry after the existing pool-root validation and dirty check. Reap is already the gated verb with `--older-than`. Test: create a pool worktree, delete its run record, and expect `reap --path P --force --yes` to remove it. It returns `live_backlink` today.
- Also, for the nested-registry case: have `worktree remove` refuse when a linked-worktree Registry under the target has non-terminal runs. `linked_registry_roots` already gives the read path.
- Alternative: register every worktree under the repo's canonical toplevel (82e3f1f2's proposal). This is cleaner conceptually but touches Registry keying, run lookup and `--cwd` semantics. That is a redesign, not a fix.
- For b0fb2d: a Linux `/proc/*/cwd` scan for warn-only is cheap. It is Linux-only; on the Mac use `lsof +D`, which is slow. I would warn on Linux only.

### RC-4: The shared runtime has no "did this promotion drop commits" check, and the install skill contradicts the doc
- Symptoms: 3ad84d6d (open, 08-16), 35057dbf (see below), with context from 20fad7cc, 105033d7, 16bd5c02 (open), 4f672a44, 9f660b97, bb981f00.
- Evidence (verified):
  - docs/live-runtime.md now prescribes versioned `releases/<rev>-<digest>` payloads, an atomic symlink switch and `delegate promote` and `doctor`. `~/.delegate/src` and `bin/delegate.py` are symlinks into `releases/` on this Mac.
  - `~/.local/bin/delegate-promote-checkout` implements it (git archive, refuses under live runs or supervisors, exit 75 to defer, rollback). It has no ancestry check. The stamp's `source` is a free label (`delegate-agent <branch> <commit>`, `sourceVerified:false`). A promotion from a branch that lacks earlier fixes proceeds silently. That is 3ad84d6d.
  - `.claude/skills/install-local/SKILL.md` still prescribes `rsync -a --delete ... ~/.delegate/src/delegate_agent/` and `cp bin/delegate.py ~/.delegate/bin/delegate.py`. Through the current symlinks that writes into an in-use release directory, which docs/live-runtime.md says never to do ("Do not rsync over an in-use package"). It was last changed before the release layout. Live, and dangerous.
  - Persona and config wipes: the estate installer (`~/Code/linux-devbox/40-cells/user-env/install-agent-dev.sh`, outside this repo) now refuses an empty tree over a populated one (line ~613) and respects symlinked runtime paths (lines 415-440). Template convergence of `config.*.json` under running lanes has no supervisor check (grep found none). 16bd5c02 remains open there.
  - For workflows, per-attempt config snapshots (docs/configuration.md:872+) and immutable pins are the answer to "should lanes snapshot config". Ordinary single runs and pre-pin workflows still read config per launch.
- Still live?: 3ad84d6d yes; install-local skill yes; 16bd5c02 partly (workflow lanes are snapshotted, converge-under-live-lanes is not gated); persona wipes no.
- Why: the promote tool, the installer and the skill were written at different times by different lanes. Nothing makes one the single entry point, and the skill in this repo predates the release layout.
- Recommended fix: (1) rewrite or delete the `install-local` skill so it calls `delegate-promote-checkout` and never touches `src/`. Deleting is defensible, since the tool and doc now cover it. (2) In the promote tool, read the previous stamp's commit and refuse unless it is an ancestor of the candidate HEAD, with an explicit `--allow-non-descendant`. Test: stamp commit A, stage a candidate that lacks A, and expect refusal. (3) Have the estate installer defer template convergence when `active-supervisors.json` is non-empty, the same exit-75 pattern.
- Alternative: leave 3ad84d6d as a coordinator ritual. It has cost one live regression, and the fix is a few lines in a script outside this repo. I would do it.
- Note: the promote tool and installer live outside this repo, in linux-devbox scripts and `~/.local/bin`.

### RC-5: The dev gate is many commands and env isolation is a growing denylist
- Symptoms: pc_8b4b28ab, pc_a915c04d, pc_cca73554, pc_1c6722e8, pc_e135dd50, pc_5ba47318, pc_9e28a2fa, pc_c76e44c0, 8f4a42f3, c4896d6a, a7e8563a, a0d3d9eb, f2e03283. Thirteen cuts, 08-01 to 09-17. Only 8f4a42f3, a7e8563a and f2e03283 are open.
- Evidence (verified):
  - Import path: `tests/__init__.py` now inserts `src` (fixed ba5a83de, 07-30, i.e. before the 08-01 cuts were logged as new).
  - Ambient env: 17 `os.environ.pop` lines plus a PYTHONPATH scrub (`tests/__init__.py`) handle AI_PROFILE, DELEGATE_RUN_ID, DELEGATE_WORKFLOW_PIN and others. Each was added after a leak: 91dfb7ae, a8f119b4, ba178577, 3f3a10f3. Typical shape is one new production env var, then a red suite in someone's live lane.
  - f2e03283 (mise shim polluting stderr) is fixed: `MISE_DATA_DIR/MISE_CACHE_DIR` are set from the real home (tests/__init__.py ~line 110, commit 0f7ff641, 09-22). The cut is still open.
  - a7e8563a is fixed: `scan_lock(target, suite_pid=...)` checks process ancestry (`_ancestry_contains`), and `tests/__init__.py` passes `--suite-pid`. Landed 0f7ff641, 09-22. The cut is still open. Caveat: the guard reads `/proc/locks`, so on the Mac `scan_lock` returns `()` and the guard is a no-op there.
  - 8f4a42f3 is fixed: `setuptools>=69` and `wheel` are in the dev extra (pyproject.toml:49, 0f7ff641). The cut is still open.
  - c4896d6a: the test now uses a barrier fake `post` and `reap_workflow_now` cleanup (test_workflow_commands.py:328). I did not run it to confirm it is race-free.
  - Swallowed summary: unittest writes its Ran/OK line to stderr. That is documented in CLAUDE.local.md but is not fixable in the repo. The canonical runner is now pytest.
  - There are five "gates": CI (`ci.yml`, pytest), `scripts/gate.sh` (uv, pinned ruff), `tests/acceptance.sh`, docs/development.md (`python3 -m pytest`), and CLAUDE.local.md (`unittest discover -s tests -t .`). They differ on runner and on who selects ruff.
- dlg-4l5: the bead (six Mac-only failures, created 09-24) is a separate incident. It postdates every cut here. Its cause was `/var/tmp` vs `/private/var/tmp` in the suite's temp-root naming, fixed by 70c3203d on 09-25, whose message says the six tests failed before and pass after. The bead is still open. It explains none of the test-gate cuts. Recommend closing it with that commit.
- Still live?: mostly no. Residuals are the multi-gate spread, the Mac no-op lock guard, and the denylist shape.
- Recommended fix: (1) one gate: make `scripts/gate.sh` the only documented command, and point CLAUDE.local.md, docs/development.md and `acceptance.sh` at it. (2) Turn the env denylist into an allowlist. Build the child environment from a short list (PATH, LANG, TERM, MISE_*, the redirected HOME and TMPDIR) and drop everything else. That makes the next `DELEGATE_*` variable safe by default. Proof: set a fake `DELEGATE_FOO=1` and `PYTHONPATH=/nonexistent` in the parent and assert a subprocess-bearing test does not see them.
- Alternative: keep the denylist and add a test that lists every `DELEGATE_*` name read in `src/` and fails if `tests/__init__.py` does not mention it. Cheaper and keeps behaviour, but only catches names, not other ambient state.
- Related: deferred.md "Workflow resume tests: in-process state pollution" (pre-existing, unfixed) and "Test-isolation leak" entries.

### RC-6: Read-only sealed pin trees are copied into throwaway workspaces (fixed for the main paths)
- Symptoms: 31f95c77 (open), 886bd7eb (resolved).
- Evidence (verified): pins are sealed 0500/0400 by design (workflow_pinning.py:469-481). `_copytree_ignore_safe_workspace` (safe_workspace.py:662) does not exclude `.delegate-workflow-pins`, so a lane whose workspace contains the pin root (for example `$HOME`) copies sealed trees. Commit 3890e42f (08-31) added `_make_temporary_tree_owner_writable` before `rmtree` in `cleanup_safe_isolated_workspace` and the copy-failure path.
- Still live?: partly. `discard_git_safe_workspace` (safe_workspace.py:893) still uses `rmtree(ignore_errors=True)` without the repair, as do cli.py:1310 and request_build.py:1741. Those silently leak if a sealed tree is inside. Not verified that those paths can contain a pin copy. The Mac showed 0 `/tmp/delegate-safe*` dirs when I looked; I did not check the devbox.
- Recommended fix: add `PIN_ROOT_DIRNAME` to the copytree ignore set. Nothing in a throwaway copy needs the sealed runtime, and copying it burned 8.7 GB of tmpfs in 31 hours. Test: workspace containing `.delegate-workflow-pins/x` at 0500, and assert the copy has no such dir.
- Alternative: route the three `ignore_errors` removals through the existing repair helper. This also fixes other read-only content but keeps copying the pins.

## Cuts that are not this family's problem
- 35057dbf (Herdr installer ran live on `--self-test`): the installer is `~/Code/linux-devbox/40-cells/herdr/install.sh`, not delegate-agent. It was first committed 08-20, after the cut, and now accepts only zero args or exactly `--self-test`, and it has a test for unknown args. Stale.
- The delegate-export family (pc_1025605191e1, pc_297a3e7cde96, pc_e59459c84012, pc_2eb06df17eca, pc_8702c5c1d1a1, pc_9489c590000e, pc_9a802472df97) belongs to `~/Code/wade-litigation-discovery/scripts/delegate-export.sh`. The current script uses `trash`, has a pinned grep, and prints `rule=... lines=...` without the matched text (1f6fc24, 07-26). The staging cleanup swallows the trash error (line ~94). `--paths` allowlist mode (pc_297a3e7c) was not built; the fix was a `DISCLOSED_EXCLUSIONS` list. The parallel launcher that should fail closed (pc_2eb06df1) is outside that script and I did not find it.
- 920ff3d0 is a worktree-family cut but fixed (see below).

## Cuts that are stale / already fixed
- 1c964f09, d5f44c96, 9277fd13: RC-1 evidence.
- 920ff3d0: `not_worktree_run` now names the owning run and adds `worktree show <owner>` to next actions (worktree_mgmt.py:1276-1283, b1a90a00, 09-24). Ledger row is still open.
- 886bd7eb, 31f95c77: main cleanup path repaired 08-31 (RC-6). 31f95c77 stays open in the ledger.
- a7e8563a, f2e03283, 8f4a42f3, c4896d6a: RC-5 evidence. Three are open in the ledger though fixed.
- 199816b3, a8664a8e, f46a85bd: archived; the code fix landed 08-27 to 08-31.
- 4f672a44, 9f660b97, bb981f00, 105033d7, 20fad7cc: the doctor and promote stamp, the empty-tree refusal and the release layout all exist. Remaining class is RC-4.
- All eight pc_ (08-01 to 08-06) test-run cuts: `tests/__init__.py` shims `src`, scrubs identity env, and pytest is the documented runner.

## Open questions for Trey
- Should `worktree prune --force` ever override a live lease? Today it does, which reproduces the 08-25 incident on request. Your answer decides whether RC-1 stays as is.
- Delete the `install-local` skill, or rewrite it to wrap `delegate-promote-checkout`? The tool exists and lives outside the repo, so the skill's remaining value is discoverability.
