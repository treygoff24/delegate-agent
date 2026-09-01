# dlg-4v1: reachable merge receipt erased on changed replay

## Scope and evidence boundary

The owning code is the writing-plans planc engine, not Delegate Agent's runtime. This diagnosis read Delegate Agent at `c8fcab6640311285cd7ae881b811c7b295506e28` and writing-plans at `779585f493e7b84058c1081376a47979c3f7bc4f`. No relevant writing-plans file changed after the branch-rename follow-up at `90e2458` and before observed HEAD.

The field workflow directory has been removed, so its journal could not be re-read. Sequence numbers below come from the verifier comment on `dlg-4v1`; the surviving babysit log, frozen generated workflow, and state backups corroborate the mechanism. `/home/trey-agent/tmp/plan-state-backup-230249.json` has `merges["B3:execute"] = null`; the later reconstructed backup has merge `c26299e`, base `f6d18f2`, and lane head `b453fdd`.

Current writing-plans HEAD no longer contains the field bug. The exact destructive code remains in Delegate's frozen field-era generator at `docs/plans/burndown-r4/wf.py:1735-1749,1847-1858`. Its owning pre-fix source is `src/planc/engine/merge.py` before `595b8ce`.

## Erasure sequence with file:line

1. B3's first execute result was integrated correctly. Journal seq 117 recorded `merge B3:execute: merged=1`; Git merge `c26299e` has first parent `f6d18f2`, second parent/lane head `b453fdd`, and remains reachable from the spine. The field engine persisted the successful output at `docs/plans/burndown-r4/wf.py:1913-1917`.

2. B3 later parked at REVIEW (seq 151). The operator chose retry (seq 152). At REVIEW, the retry target is `item_attempt_key(item)`, or `B3:review:0`, not `B3:execute`: `docs/plans/burndown-r4/wf.py:2655-2676`. The retry code requests rejection of that review attempt, bumps its counter, and raises `RetryStage` at `:2680-2689`. It does not touch `STATE["merges"]`. This is the verifier's correction to the original report.

3. The supervisor watchdog then cancelled the run (seq 157). Delegate raises the observed `cancellation requested during pipeline` after worker join when its cancel event is set: `src/delegate_agent/workflows/runtime.py:1968-1971`. A resume reuses the frozen script and increments the replay attempt at `src/delegate_agent/workflows/commands.py:158-198,308-340`; `WorkflowState` reloads journal results at `src/delegate_agent/workflows/runtime.py:640-650,661-765`.

4. The field-era plan engine had no durable settled-stage transition. `seed_items()` rebuilt every unseeded task at `stage = "execute"` on every script replay: `docs/plans/burndown-r4/wf.py:1379-1410`. The run therefore revisited B3 execute. Journal seq 172 shows the execute call; seq 184 contains a newly observed result, different from the result that produced `c26299e`.

5. `merge_lane_locked` compared the new result identity with the old receipt. `_cached_success_is_live` first requires exact `recorded_head` and `merged_head` equality at `docs/plans/burndown-r4/wf.py:1735-1741`. Because the replayed result named a different head/branch tip, it returned false before its old-receipt reachability checks at `:1745-1749` could matter.

6. The caller treated "this result does not select that receipt" as "that receipt is invalid." It unconditionally popped `STATE["merges"]["B3:execute"]` and saved state before collecting or classifying facts about the new result: `docs/plans/burndown-r4/wf.py:1847-1858`. This is the information-loss seam.

7. The new result was then refused for an out-of-boundary diff (seqs 185-186). A refused merge is not cached, and `merge_bad` also rejects the new execute agent result at `docs/plans/burndown-r4/wf.py:1681-1685,1913-1918`. That rejection makes later replays run fresh work again, but it occurs after the immutable receipt has already been erased.

8. Close builds `integration.receipts` only from the surviving `STATE["merges"]` rows: `docs/plans/burndown-r4/wf.py:2060-2067`. It has no Git or journal recovery path. With the B3 key gone, seq 230 correctly refused all four B3 rows with `No immutable B3 integration receipt or merge commit`. Re-running close cannot recreate the missing branch, base, merge, and verify binding, so the refusal is deterministic until plan-state is repaired.

## Violated invariant

Once a valid merge receipt's `merge_commit` is an ancestor of the current spine head, that receipt is an immutable fact. A later result may be a cache miss, a superseding contribution, or invalid, but it cannot make the earlier integration cease to have happened.

The field code lost this distinction at the interface between `_cached_success_is_live` and `merge_lane_locked`. A cache-selection predicate answered whether the old receipt matched the newly supplied result. The caller used that answer as a receipt-validity predicate and destroyed the old record before checking the old `merge_commit` against the spine. The Git fact survived; the only state binding it to task, branch, base, and verify rows did not.

Current writing-plans HEAD fixes this. `_normalized_receipt` validates old receipts independently, including `merge_commit -> spine` reachability, at `src/planc/engine/merge.py:104-132`; `_receipt_history` retains every valid reachable row at `:135-152`. `merge_lane_locked` selects an exact cached identity without deleting non-selected history at `:367-416`, appends successful changed results at `:518-529`, and restores the cached history after a refused, conflicted, or red changed result at `:530-532`. Commit `90e2458` also prevents a branch rename from filtering out the old task receipt.

## Why the replay result differed

There are two separate questions.

Why execute ran again is structural. The field plan engine restarted B3 at execute and depended on Delegate's agent-result replay cache to make that harmless. Delegate's cache identity is a hash of structural path, prompt, and options: `src/delegate_agent/workflows/runtime.py:2280-2316,3882-3885`. A tombstone or any identity miss reaches a live child. The deleted journal was the only artifact that could distinguish the exact field miss: changed key inputs, an earlier tombstone, or a missing replay row. The surviving evidence proves a live, different result appeared, but not which cache input missed. The REVIEW retry itself does not explain it because its target was `B3:review:0`, not the execute label or merge key.

Why the output differed after a live launch is ordinary fresh-execution variance. The replacement used a different route and produced a different branch/head and file set. The merge engine must tolerate that variance without rewriting history.

Red-result caching is not the initial cause. Delegate journals completed agent results without classifying them red or green at load time (`src/delegate_agent/workflows/runtime.py:744-765`). Planc rejects a result only when its stage predicate declares it invalid (`docs/plans/burndown-r4/wf.py:1657-1661`). The original B3 execute result was green enough to merge. After the changed result was merge-refused, `merge_bad` tombstoned that new result, which amplified the retry loop but did not cause the first receipt pop.

The Delegate r2 gate change, key plus `resultHash`, does not fix or cause dlg-4v1. It binds approval reuse for a nested workflow gate to the exact gate result (`src/delegate_agent/workflows/registry.py:89-149`; `src/delegate_agent/workflows/runtime.py:2173-2185`). B3 execute is a direct `agent()` stage, and its integration receipt lives in planc plan-state. r2 ensures a distinct red result with the same gate key re-parks; it neither stabilizes agent replay identity nor protects `STATE["merges"]`.

Writing-plans `595b8ce..8d6b82c` already changed both relevant seams. `595b8ce` introduced reachable receipt history and durable settled-stage transitions. The other commits in the range do not alter receipt invalidation; `8d6b82c` only removes an unused receipt helper. Current transitions are persisted at `src/planc/engine/core.py:612-628`, validated and restored at `:631-716`, and consulted before agent dispatch at `:2113-2124`. That removes the field-era dependence on Delegate cache identity for an already-settled execute stage.

## Per-design analysis

### A. Immutability guard

Correct and mandatory. Perform it at the invalidation site while the merge lock is held. A valid receipt whose merge commit is reachable from the observed spine must survive every cache miss and every failed changed rerun. This keeps mutation and invariant enforcement local. Current HEAD implements a stronger form by retaining all valid reachable rows, not merely refusing one pop.

The guard must validate task, base, branch, heads, and ancestry before preservation; "reachable SHA" alone must not bless a malformed or forged row. Unreachable receipts may be pruned. A race-free check uses the spine head captured under `MERGE_LOCK`.

### B. Retry refusal

Useful defense and operator feedback, but insufficient alone. It races with direct replay calls and does not protect other callers of `merge_lane_locked`.

A blanket item-level refusal is too broad. Retrying REVIEW for an already-integrated item is legitimate; the field bug was that replay reopened EXECUTE. Refuse only an action that would invalidate or re-enter an execute/fix stage whose valid receipt is spine-reachable. Return a named `already_integrated` error with task, stage, and merge commit. Let review/adjudication retry proceed while preserving the execute transition and receipt.

### C. Receipt archival

Safe if close considers only fully validated, spine-reachable archived rows. It preserves audit history and supports a genuinely changed successful contribution. Costs are schema migration, duplicate identity, ordering, and stale-row selection. A separate `superseded` table is unnecessary at current HEAD: `merged` is already an ordered receipt history, and close projects all its rows at `src/planc/engine/close.py:220-234`.

Archive at the merge seam, not as a close-time salvage path. Close cannot reconstruct the task/base/verify binding from Git alone. It should validate supplied history and fail closed when none is valid, not manufacture a receipt.

### D. Durable settled-stage transitions

This is the missing upstream control. Persist `execute -> review` with the immutable integration receipt and restore it after supervisor restart. Then a review retry stays a review retry, and a Delegate cache miss cannot relaunch integrated execute work. `595b8ce` already implements this at current HEAD. It reduces exposure but does not replace A: merge receipt immutability must hold even if a caller legitimately presents changed output.

## Recommendation

For the field-era engine, the smallest durable repair is A plus D. Add the stage-aware form of B for clear operator behavior. C is optional in a minimal redesign, but current HEAD already implements A and C together as reachable receipt history; retain it rather than adding a second archive.

Do not add close-time Git reconstruction. The primary invariant belongs at the old pop site. Close should remain a verifier of immutable receipts, not a recovery engine.

On observed current HEAD, dlg-4v1's destructive sequence is closed by `595b8ce` plus `90e2458`: settled execute transitions restore without dispatch, reachable receipts survive identity changes and failed reruns, and close sees the retained history. The remaining useful hardening is the narrow named retry refusal, not another receipt store.

## Test plan

1. Add one cross-module field regression: merge execute result A; assert its commit is on the spine; settle and persist `execute -> review`; park at review; apply retry; tear down and reconstruct the workflow state; force a would-be execute result B with a different branch/head and an out-of-boundary diff. Assert no second execute agent is called when transition restore is live. Also call the merge seam directly with B and assert receipt A remains byte-for-byte in plan-state. Build close input and assert B3's receipt is present.

2. Keep the current merge-history cases in `tests/test_engine_merge_sim.py`: exact replay (`:664`), branch rename (`:706`), changed success append (`:735`), malformed-row pruning (`:828`), unreachable-row pruning (`:887`), and red/conflicted/refused changed reruns preserving reachable history (`:957,1008,1059`). The refused changed-rerun case is the narrow reproduction of the destructive field seam.

3. Keep transition tests that reload the next prompt without merging again (`tests/test_engine_coordinator_sim.py:549,601`) and reject incomplete, unreachable, or wrong-task receipts (`:327-396`). Add the operator-review retry to this integration fixture so it pins the exact watchdog/replay ordering.

4. Add retry-guard tests: reachable execute/fix receipt returns `already_integrated` without tombstoning, bumping attempts, or changing plan-state; REVIEW retry remains allowed; an unreachable receipt does not block a fresh execute/fix attempt.

5. Add a close projection test with two historical rows, one reachable and one unreachable. Only valid reachable history may reach `integration.receipts`; absence must refuse, never infer from Git.

Mutation-check each load-bearing assertion. Replace the current cached-restore branch at `src/planc/engine/merge.py:530-532` with a pop; make `_normalized_receipt` ignore or invert `is_ancestor(merge_commit, spine_head)`; reintroduce the old exact-identity pop; filter history by the newly observed branch; remove either `_restore_settled_transition` call at `src/planc/engine/core.py:2116,2123`; and remove the proposed `already_integrated` guard. Each mutation must make its focused test red. A green test under any one of those mutations does not pin dlg-4v1.

## Verification

- `python3 -m unittest` on five focused writing-plans receipt-history tests: 5 passed.
- `python3 -m unittest` on two focused transition-validity tests: 2 passed.
- `python3 -m unittest tests.test_workflow_gate_binding` in Delegate Agent: 4 passed, including same-key/different-result re-park.
- No source, test, or config file was modified.
