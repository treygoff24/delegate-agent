# Worktree Isolation — Post-Ship Followups

Logged 2026-05-25, after landing the bug/test cleanup pass on top of `44586a1`.

This doc captures everything the consolidated review identified that was **not** fixed in the cleanup pass — deferred refactors, latent code-quality smells, partial test coverage, and non-code TODOs. Anything not listed here is either resolved on `main` or out of scope for the worktree-isolation feature.

## Cleanup pass summary

9 commits landed on top of `44586a1`, taking main to `6113a21`. Suite at 344 tests green (was 324 before housekeeping deletions; net +22 new tests, -2 dead tests).

| Commit  | Subject |
|---------|---------|
| `b3fc8ef` | isolation: stop duplicating SKILL_REVIEW_PREFIX from runner |
| `1a103af` | Housekeeping: remove dev artifacts and dedupe cli imports |
| `4cabb7b` | rendering: complete worktree show text + lift cleanup commands |
| `6402fce` | rendering: render current source HEAD ref accurately in worktree show |
| `ab060bb` | worktree-mgmt: route mutations through set_worktree_status; fix prune branch/skip semantics |
| `77614d3` | tests: cover missing worktree-mgmt acceptance criteria from spec |
| `9a51c91` | tests: cover detached-HEAD, real-collision, and pass-through-failure persistent worktree paths |
| `6b4ef81` | docs: log post-ship followups, latent issues, and known gaps |
| `6113a21` | worktree-isolation: address Codex review findings |

Resolved: BUG-1 (single-mutation lock contract), BUG-2 (`prune` clean+unmerged-branch semantics per spec L673), BUG-3 (`prune` skip-reason leak per spec L678), DEV-1 (`worktree show` text completeness per spec L621, including section ordering per Codex finding), DEV-2 (pre-launch failure snapshot now omits unrealized `executionCwd`/`worktreeStatus`/`worktreeCleanupCommands`/`branch` fields + `worktreeCleanupCommands` lifted from manifest per spec L713), DEV-3 (`SKILL_REVIEW_PREFIX` duplication, with subprocess-based import-order regression test), `maybe_auto_prune` now holds the lock continuously across the prune call so it actually returns within ~1s under contention per spec L700, housekeeping (`DEEPSEEK_NOTES.md` deletion, dead test removal, indented `unittest.main()` fix, redundant branch-collision test removal, duplicate cli import dedup), and 22 new acceptance/regression tests across spec L803/L809/L811/L819/L822/L823/L826/L827/L832/L833/L834/L835/L836/L838/L841 plus Wave 4's snapshot-omission, show-text-order, lock-contention, manifest-lift, and lazy-import regression tests.

---

## Section 1 — Deferred refactors (post-ship, not behavior bugs)

Identified by the original code review as "post-ship" because they're behavior-preserving cleanups, not correctness fixes. Worth doing as a follow-up commit when there's time.

### 1.1 `_execute_persistent_worktree` decomposition
- **Where**: `src/delegate_agent/cli.py` around L2125-2398 (273 lines, 13 step-numbered comments).
- **Smell**: One function with `# Step 1`, `# Step 2`, ... `# Step 13` narrative comments — textbook "position markers" the clean-code rubric flags. Two near-identical `RunContext` constructions (pre- and post-launch) differing only in `started_at`. Manifest written twice (L2259, L2357) — second overrides, first is throwaway work.
- **Suggested split**: `_register_pre_launch_run`, `_create_or_record_failure`, `_launch_child_in_worktree`. The numbered-step comments disappear naturally. One `RunContext` build site instead of two.
- **Risk**: Low — pure decomposition, behavior-preserving. Test coverage for persistent-worktree creation is solid (the entire spec L786-805 acceptance set).

### 1.2 `remove_worktree` decomposition
- **Where**: `src/delegate_agent/worktree_mgmt.py` around L558-750 (~190 lines).
- **Smell**: Single function handles option normalization, mutual-exclusion validation, locked record resolution, already-removed short-circuit, dirty-check raise, metadata sanity check, unmerged-branch guard, missing-path short-circuit, the `git worktree remove` invocation, branch deletion, state write, and result payload assembly. Two near-identical result payloads at the early-return and end paths (`branchKept`/`pathRemoved`/etc.) cry out for a `_remove_payload(...)` factory.
- **Suggested split**: `_normalize_remove_options`, `_handle_already_removed`, `_assert_branch_merged_or_overridden`, `_handle_missing_worktree`, `_perform_removal`, `_build_remove_payload`.
- **Risk**: Low-medium — wide test coverage in `tests/test_delegate_worktree_mgmt.py`. The `branchKept` override path for prune (Wave 2 added it) needs to ride along.

### 1.3 `parse_worktree` + `emit_worktree` table-driven rewrite
- **Where**: `src/delegate_agent/cli.py` around L812-949 (parse, ~138 lines) and L1147-1238 (dispatch, 5 branches).
- **Smell**: `parse_worktree` is a per-action `if token == "--X"` cascade with substantial duplication — `--harness` under list+prune, `--discard-uncommitted`/`--force-branch`/`--force` under remove+prune, `--dry-run` under prune+gc. `emit_worktree` is five near-identical `if action == "..."` branches each calling a worktree_mgmt function then branching on `json_mode` to pick a renderer.
- **Suggested rewrite**: Per-action option table (`{"prune": {"--merged": ("worktree_merged", "flag"), "--older-than": ("worktree_older_than", "int")}}`) + 15-line driver for parsing; `ACTIONS = {"list": (run_list, render_text), ...}` for dispatch. Net ~140 lines removed without behavior change.
- **Risk**: Medium — the parser is the most-exercised public surface of `cli.py`. Need to keep `misplaced_global_option` and per-action positional rejection working.

---

## Section 2 — Known coverage gaps

Things the spec lists as acceptance criteria but our tests cover incompletely or imprecisely.

### 2.1 L837 covered indirectly as L838 (linked to §3.6)
- **Spec L837**: "`git worktree add` failure with `branch already checked out` surfaces as `worktree_create_failed` with the underlying git stderr."
- **What's tested** (Wave 3B, `test_persistent_worktree_add_collision_fails_and_cleans_up`): pre-creates the predicted branch and checks it out in another worktree, then triggers a delegate run. Asserts the error is in `["worktree_create_failed", "branch_collision"]`.
- **Why the test catches `branch_collision` first** (Codex correction — earlier rationale here was wrong): `create_persistent_worktree` at `src/delegate_agent/isolation.py:344-353` maps any `git worktree add` stderr containing `"already exists"` to `branch_collision` rather than `worktree_create_failed`. So the actual `git worktree add` failure DOES fire — but its error code is reclassified by the stderr grep, not raised as `worktree_create_failed` with the original git stderr. This is the same fragile stderr-grep behavior flagged in §3.6.
- **To trigger L837 cleanly**: fix §3.6 first (replace the stderr grep with `git rev-parse --verify --quiet` probe before `worktree add`). Then the actual `worktree_create_failed` path becomes reachable and the test can assert against it with the real git stderr in the message.

### 2.2 `--no-auto-prune` end-to-end CLI plumbing
- **Spec L827**: "`delegate worktree list --no-auto-prune` skips the opportunistic pass even with config opt-in."
- **What's tested** (`test_worktree_list_no_auto_prune_skips_opportunistic_pass`): calls `maybe_auto_prune(registry_root, config, no_auto_prune=True)` directly and asserts no prune occurred. Wave 4 added `test_maybe_auto_prune_skips_when_lock_contended` which covers the contention path end-to-end.
- **Gap**: full CLI invocation (`delegate worktree list --no-auto-prune` through `main()`) is still not exercised. Plumbing in `cli.py:861` parses the flag and L1174 passes it to `maybe_auto_prune`, but no test asserts the wire is connected.

### 2.3 Detached creation end-to-end + `worktree prune --include-detached` happy path
- **Spec L836 happy-path**: We test the persistent run succeeds with detached HEAD and `show` surfaces the warning. We don't test that `worktree prune --merged --include-detached` actually includes that entry in selection (only the negative case where it's skipped without the flag is tested in the pre-existing `test_worktree_prune_merged_removes_only_safe_mixed_set`).

### 2.4 Tri-state `dirty`/`mergedIntoSource` warning text shape
- **Spec L833**: "Lazy `dirty` / `mergedIntoSource` fields are tri-state and return `null` (with a warning) when git is unavailable or returns non-zero."
- **What's tested** (`test_dirty_info_returns_null_when_git_unavailable`, `test_merged_into_source_returns_null_when_git_unavailable`): asserts the value is `None` and warnings list is non-empty.
- **Gap**: doesn't pin the warning text shape. If someone refactors the warning string, the assertion silently passes.

---

## Section 3 — Latent code-quality smells (not fixed)

Identified by the consolidated review but deliberately not addressed in the cleanup pass. None are bugs; all are technical-debt items.

### 3.1 `prune` merged-check imprecision (preserved from pre-existing code)
- **Where**: `src/delegate_agent/worktree_mgmt.py` around L815 in `prune_worktrees`.
- **Smell**: `if not merged_value:` catches both `False` (correctly: branch is not merged) and `None` (git error or detached without `--include-detached`). The detached case is filtered earlier, but git errors slip through and get treated as unmerged → routed to `keep_branch_for_prune = True` → path removed, branch silently kept.
- **Why not fixed**: pre-existing imprecision; Wave 2 preserved it rather than changing behavior in an already-substantive commit. Fix: change to `if merged_value is False:` so git errors fall through to a different path (likely a `skipped` entry with reason `merge_check_failed` if we add that reason).

### 3.2 `gc_worktrees` has three near-identical reload-and-write blocks
- **Where**: `src/delegate_agent/worktree_mgmt.py` L894-975.
- **Smell**: The "missing path", "metadata missing", and "branch missing" branches each open a registry lock, reload the record, re-validate the same fields, and call `set_worktree_status`. Extract `_reconcile_under_lock(registry_root, record, predicate, status, append)` and the three branches collapse to one-liners. Any future bugfix must currently be made three times.

### 3.3 Cleanup-hint block duplicated between runner and rendering
- **Where**: `src/delegate_agent/runner.py` L320-369 (`emit_bounded_text_summary`) and `src/delegate_agent/rendering.py` L171-184 (`render_snapshot_text`).
- **Smell**: The `cleanup (refuses dirty / unmerged):` / `cleanup (allow unmerged branch deletion):` / `cleanup (DISCARD uncommitted edits):` / `raw git equivalent:` block exists twice with character-identical formatting. Per the spec's module-ownership lines (L862-864), JSON/text renderers belong in `rendering.py`. Extract `rendering.render_persistent_worktree_cleanup_block(...)` and call from both sites.

### 3.4 `_record_for_run` leaks private keys
- **Where**: `src/delegate_agent/worktree_mgmt.py` L166-168.
- **Smell**: Returns a `JsonObject` with leading-underscore keys (`_state`, `_manifest`, `_snapshot`) as implementation cargo. Either keep them on a private dataclass (`PersistentRecord`) with a `to_public_dict()` method, or stop attaching them and re-load when needed.

### 3.5 `WorktreeManagementError` swallows shape errors
- **Where**: `src/delegate_agent/worktree_mgmt.py` L25-29.
- **Smell**: `payload.get("message", payload.get("code", "worktree_error"))` — if a caller forgets `code`, the exception silently degrades to a generic string. Either validate at construction (`assert "code" in payload`) or take `code` and `message` as separate required args plus `extra: JsonObject`.

### 3.6 Branch-collision detection greps git stderr text (load-bearing for §2.1)
- **Where**: `src/delegate_agent/isolation.py` L347 inside `create_persistent_worktree`.
- **Smell**: `if "already exists" in stderr and "branch" in stderr.lower():` — string-matching git stderr is fragile across git versions and locales (LANG=fr_FR.UTF-8 will defeat it). Either probe with `git rev-parse --verify --quiet refs/heads/<branch>` before `worktree add` (cheap, no race inside the registry lock) or check both branch existence and the failure independently.
- **Bonus impact**: this is what's blocking §2.1's L837 coverage from being properly testable. Fixing this unlocks the L837 test as a side effect.

### 3.7 `dirty_info` and `porcelain_status` overlap
- **Where**: `src/delegate_agent/worktree_mgmt.py` L223-249.
- **Smell**: `dirty_info` calls `porcelain_status(execution_cwd)` then re-derives `bool(lines)` and re-handles the same `executionCwd`-missing case that `detect_worktree_status` already handles. Inline as `_dirty_paths_for(record)` or let callers use `porcelain_status` directly with a small adapter.

### 3.8 Magic `20` cap for dirty paths
- **Where**: `src/delegate_agent/worktree_mgmt.py` L382 and L434-437.
- **Smell**: `dirtyPaths[:20]` appears twice with the `dirtyPathsTotal` companion. Hoist `MAX_DIRTY_PATHS_REPORTED = 20` module constant; one number, two readers.

### 3.9 `_run_git` has no timeout
- **Where**: `src/delegate_agent/worktree_mgmt.py` L40-46.
- **Smell**: Every git invocation can hang indefinitely on a flaky network mount. `gc_worktrees` holds the registry lock while running git — a stuck `git worktree list --porcelain` would block the registry forever. Add `timeout=30` (configurable) and convert `TimeoutExpired` into a `WorktreeManagementError` with a `git_timeout` code.

### 3.10 `repo_fingerprint` does I/O despite its name
- **Where**: `src/delegate_agent/isolation.py` L70.
- **Smell**: `resolve(strict=True)` is a stat call — side effect inside a function that sounds pure. Either rename `compute_repo_fingerprint` or document the I/O cost in the docstring.

### 3.11 Repeated `isinstance(x, dict) and x.get(k)` preamble
- **Where**: throughout `src/delegate_agent/worktree_mgmt.py` — `_branch_from`, `_execution_cwd_from`, `_source_git_root_from`, `_record_for_run`, `_is_persistent_worktree_run`, and several others.
- **Smell**: a `_get_str(source: object, key: str) -> str | None` helper (or `_get(source, key, predicate=None)`) would compress dozens of two-line guards into one-liners.

### 3.12 `_skip_lock` kwarg on `set_worktree_status`, `remove_worktree`, `prune_worktrees`
- **Where**: `src/delegate_agent/run_registry.py` L552; `src/delegate_agent/worktree_mgmt.py` at `remove_worktree` (`_skip_lock` param + `_DummyLockContext` fallback), `prune_worktrees` (passes through), and `maybe_auto_prune` (sole legitimate caller passing `_skip_lock=True`).
- **Smell**: BUG-1's fix routed all mutations through `set_worktree_status` (which holds the lock), but the workraith callers (`remove_worktree`, `gc_worktrees`, and Wave 4's `maybe_auto_prune` lock-and-prune path) already hold the lock for their broader critical section. `_skip_lock=True` avoids nested-flock deadlock. This is correct but the leading-underscore convention suggests "private use only" without enforcement — there's nothing stopping a future caller from passing `_skip_lock=True` without holding the lock, restoring the original bug. Wave 4 widened the surface by adding the kwarg to `remove_worktree` and `prune_worktrees`, plus the `_DummyLockContext` helper class. Consider: a context-manager variant `set_worktree_status_locked(...)` that asserts the caller holds the lock (or a `with locked_mutations(): ...` block that returns a mutator with `_skip_lock` baked in). At minimum, an `assert not _skip_lock or self._has_lock()` guard, even if `_has_lock` is best-effort.

---

## Section 4 — Non-code TODOs

### 4.1 Live runtime is stale
The installed runtime at `~/.local/bin/delegate` (launcher pointing at `~/.delegate/`) does NOT have the worktree feature. Anyone using `delegate` from PATH on this machine cannot pass `--isolation worktree`. Per `CLAUDE.local.md`'s hard rule, the orchestrator did not auto-publish — Trey decides when to do it explicitly ("publish" / "install").

**Status (2026-05-25 autonomous follow-up):** Documented in `docs/live-runtime.md` with a development-vs-installed-runtime section. Runtime promotion remains an explicit operator action; this implementation does not mutate `~/.delegate` or the installed shim.

### 4.2 Persistent worktrees from cleanup-pass session
Several `~/.delegate/worktrees/<fingerprint>/{cursor,droid}-*` directories from this session linger after cherry-picking commits to main. They're preserved by design (the spec's persistent contract), but a `delegate worktree prune --merged` will clear the ones whose branches were cherry-picked (the cherry-pick gives the merged branch a different OID, so `--merged` may not match — verify before relying on it; may need explicit `worktree remove`).

**Status (2026-05-25 autonomous follow-up):** Inventoried with `python3 bin/delegate.py --json worktree list --no-auto-prune`. Active implementation worktrees at inventory time: `cursor-20`, `droid-53`, `codex-15`, and `droid-54`; each was Delegate-managed, dirty with its subagent diff, and `mergedIntoSource: true` after coordinator integration or pending inspection. Older entries (`droid-31` through `droid-40`, `cursor-16`) were already registry-removed. Cleanup should continue through `python3 bin/delegate.py worktree remove <alias>` after confirming each diff is integrated or intentionally discarded; do not manually delete worktree paths.

### 4.3 README / AGENTS.md / live-runtime docs
The spec (L843-850) lists doc updates as part of the acceptance set. Doc updates landed in the original `44586a1` checkpoint; this followup doc deliberately does NOT modify them. If any of the bug fixes above (especially BUG-1 / BUG-2 / BUG-3 / DEV-1 / DEV-2 / DEV-3) changed user-visible behavior, the docs may need a re-pass. Skim recommended.

**Status (2026-05-25 autonomous follow-up):** Re-passed and updated:
- `config.example.json` now includes `isolation` and `worktrees`.
- README states Droid work uses `--skip-permissions-unsafe`.
- README states `--pass-through` is unsupported for any persistent worktree run, including Droid.
- README no longer advertises invalid bare `delegate worktree prune`; the summary uses `delegate worktree prune --merged --dry-run`.
- AGENTS.md now calls out the same persistent-worktree pass-through restriction for orchestrators.
- `docs/live-runtime.md` keeps the dev-checkout vs installed-runtime boundary explicit.

---

## Section 5 — Out-of-scope items deliberately not touched

For clarity on what was *not* changed during the cleanup pass:

- `cli.py` is now 2,996 lines. The spec explicitly warned against `cli.py` becoming a collision point. Wave 2 left `_execute_persistent_worktree` at 273 lines unchanged because the fix scope was the lock contract, not the function length. See 1.1.
- The full `--include-dirty` follow-up (mentioned in the spec at L1025) remains a non-goal for this feature.
- `delegate worktree list --all` (cross-repo, walking `~/.delegate/worktrees/*/`) remains a non-goal.
- `delegate worktree archive <alias>` remains a non-goal.

---

## Ship recommendation

The bug+coverage scope is resolved through Wave 4. Codex's read came back as "minor findings, no blockers" — all three real findings (pre-launch snapshot semantics, `worktree show` text ordering, `maybe_auto_prune` lock-probe theatre) plus four of his five test-proof gaps landed. The remaining bullets:

- **Section 1** deferred refactors (`_execute_persistent_worktree`, `remove_worktree`, `parse_worktree`/`emit_worktree`) — meaningful next-pass work; none of them block ship.
- **Section 2** gaps — L837 stays a gap until §3.6 is fixed; `--no-auto-prune` CLI plumbing untested; detached `prune --include-detached` happy path untested; tri-state warning text shape unpinned.
- **Section 3** latent smells — all real, none ship-blocking. §3.6 is the most load-bearing because it gates §2.1; §3.9 (`_run_git` no timeout) is the most operationally risky because `gc_worktrees` can park the registry lock indefinitely under network-filesystem flake.

Ship.
