# Lane liveness: stall watchdog and hang/loop signals

## Bottom line

Most of the watchdog cuts predate dlg-mi2 (merge 37628e4a, 2026-09-25), which added runaway-output, repeated-tool-failure, finished-Devin-report, HEAD-probe and `--stall-minutes`. Those fixes are in the code; the CHANGELOG still lists them under Unreleased. What is left is structural. The watchdog remembers only the previous tool call, so any loop of two or more distinct calls reads as progress. It treats any in-flight tool as proof of life forever. It applies one model-blind idle threshold. It never shows the operator what it knows, so `lastActivityAt` and the heartbeat say "alive" for looping lanes. Separately, the tracked stdout cap still fails a run at the byte limit even when the work was done. The biggest single win is to expose the watchdog's own state in the Snapshot (RC-3), then give tool-loop detection a windowed memory (RC-1). The cuts overlap little with F1 or F3, with a few misfiled ones listed below.

## Root causes

### RC-1: Tool-loop memory is depth 1, so any cycle of 2+ distinct calls counts as progress
- Symptoms: pc2_6ef36c115319d143 (MiMo, 50+ repeats, 09-22, open), pc2_1aeb425dc0de3218 (omp gemini flash, 4.6MB of eval start/complete cycles, 08-27, open), pc2_76e3db7403579570 (Luna jq re-validation 20 min, 08-31, open), pc2_1cd15155b20395d7 (pi gemini discovery loops, 08-16, open). 4 cuts.
- Evidence (verified): `stall_watchdog.py:735-757` `_repeats_locked` compares a call only to `_last_completion`, the single previous completion. `_record_outcomes_locked` (`:759-781`) resets the failure streak on any non-failing or targetless completion. Text deltas get 8-deep memory (`RECENT_DELTA_MEMORY`); tools get 1. I drove `StallWatchdog._apply_locked` in-process with a 60-cycle loop of edit(fail), `true`, `true`, fmt at 21s per step. It never stalled and never tripped. Two-call cycles also never stall. The same failure five times in a row trips at cycle 5. Events with no target are never repeats (`:748`); the omp eval cycles were probably in that class (inferred, target extraction not checked).
- Still live at 0.31.0?: yes. The 09-25 fix covers only the single-call repeat. The MiMo shape (fail, no-op, no-op, formatter) is exactly what escapes. Luna's "equivalent but not identical" jq commands are a different limit: no signature detector catches them.
- Why it recurs: 472f0684 deliberately made a fix loop (patch between identical test runs) read as productive. Without an effect signal the detector cannot tell "patching and retesting" from "cycling", so it errs toward never killing. That is the right bias, but it leaves no middle state.
- Recommended fix: (a) keep a windowed memory of tool completion signatures (tool, target, status), the same idea as the delta deque, and flag a cycle when the last N completions span at most M distinct signatures. (b) Add an effect fingerprint to the existing `progress_probe`: HEAD plus a hash of working-tree status. Treat a cycle as a loop only if the fingerprint is unchanged across the window. A productive fix loop changes the tree; a no-op loop does not. Today `_worktree_head_probe` (`runner.py:2034`) sees commits only, and is consulted only after idle passes the threshold. Prove it with a test that replays the cycle above and asserts a `repeated_tool_cycle` trip. A twin test with a changing tree fingerprint must not trip.
- Alternative: warn only. Record `loopSuspected` in the Snapshot and heartbeat and leave termination to the parent. The MiMo cut itself calls termination "a policy choice". This is cheap and safe, and it pairs with RC-3.
- Beads/rulings: dlg-v0y item 3 (Cursor dedupe window of 8) is subsumed by a windowed design. No deferred.md ruling covers this.

### RC-2: An in-flight tool is treated as proof of life with no upper bound, and idle is one model-blind threshold
- Symptoms: pc2_b35a981fe69859f0 (GLM stuck 13 min after an MCP tool, 08-20), pc2_32a5f60a4bd8e4cc (Devin 10 min at "waiting-for-child-output", 08-19), the second half of pc2_59556849280cc8bc (Devin SWE-2 max silent 27 min, zero edits), pc2_0ce41e93ab09da30 (Grok xhigh cancelled while thinking, 09-26, open), pc2_8046adb9f3417794 (Sol xhigh, resolved by the flag). 5 cuts, two directions of error.
- Evidence (verified): `_stalled_idle_locked` returns `None` whenever `_pending_tools` is non-empty (`stall_watchdog.py:942`). A tool that starts and never finishes disables the watchdog until `--timeout` fires, or forever without one. The module docstring calls this deliberate (a 40-minute test suite). Kimi and Devin are exempt from the default threshold when a `--timeout` exists (`effective_stall_seconds`, `:149-163`, called from `cli.py:452`). So a dead Devin child with `--timeout 3600` is invisible for an hour. The default is a flat 8 minutes (`STALL_MINUTES_DEFAULT`) regardless of harness or reasoning effort. `--stall-minutes` exists for direct launches. Workflow `agent()` has `timeout=` but no stall parameter (`docs/delegate-workflows.md:77`, no `stall` in `workflows/*.py`). Workflow children get one global value through the `DELEGATE_STALL_MINUTES` env mapping (`workflow_attempts.py:31`).
- Still live?: partly. Devin finished-but-idle is fixed (`runner.py:2886-2909`, only `Status: completed|blocked|failed` on its own line ends it; dlg-v0y item 5 notes `Status: completed;` and split lines still miss). The HEAD probe is fixed for commits. The hung-tool, silent-thinking and workflow-override gaps are live. Whether the GLM stall was a never-finished tool is inferred from the cut text.
- Why it recurs: "alive" is inferred from stdout events. Silence is ambiguous between thinking, a provider queue, a dead tool session and a dead child. The only disambiguator, `process_group_activity` (`:1075`), runs only after an idle stall fires.
- Recommended fix: bound the in-flight exemption by age (a `toolStallMinutes` ceiling, much larger than the idle threshold). When it is exceeded, sample the process group and stall only if `childActivity` is `waiting` or `no_processes`. That subsumes dlg-v0y items 1 and 2, because a leaked pending entry then expires instead of needing a map cap. Add an optional per-step stall value to workflow `agent()`. Consider raising the default for xhigh Grok and Sol lanes only as a config default, not code.
- Alternative: docs and convention only. Tell callers to pass `--stall-minutes 25` for xhigh reviews. That already works for direct launches, so it is a fine stopgap. It does nothing for workflows or hung tools.
- Beads: dlg-v0y items 1, 2, 4 (Grok usage-only stream reads as stall), 5.

### RC-3: The watchdog's knowledge is never exposed while a run is alive, and the visible signals mean something else
- Symptoms: pc2_1cc04fa74e95e64b and pc2_9f4ab769d0ccd2aa (stdout.log "stops at 500 lines", 09-26, open), pc2_6ef36c115319d143 ("Delegate reported fresh activity"), pc2_32a5f60a4bd8e4cc, pc2_b35a981fe69859f0. Counts as the visibility half of 5 cuts.
- Evidence (verified): `StallWatchdog.last_progress_label` and `tools_in_flight` (`stall_watchdog.py:685-693`) have no callers outside the class. `lastActivityAt` is stamped `now` on every persist (`runner.py:790`). Persist happens on any stdout line, so a looping lane keeps advancing it. The heartbeat prints only the current label or "waiting for child output" (`runner.py:2223-2236`), and heartbeats are off by default. The cut's mechanism is wrong on one point: what stops at 500 is the `stream.line` mirror in `events.jsonl` (`harness_events.EVENT_LIMIT = 500`, `runner.py:2675-2688`, documented at `docs/troubleshooting.md:482`). `stdout.log` keeps growing to the byte cap. But `_drain_stream` writes it through a plain buffered `open("ab")` handle with no flush (`runner.py:1618-1621`). So its mtime and content lag by up to one 8 KiB buffer (inferred from Python buffering semantics, not observed live). `lastActivityAt` is not documented in `docs/*.md`.
- Still live?: yes.
- Why it recurs: there are three notions of "activity" (last stdout byte, `lastActivityAt`, watchdog progress) and only the first two are visible.
- Recommended fix: include a `watchdog` block in the running Snapshot extra (`lastProgressAt`, `lastProgressLabel`, `toolsInFlight`, `idleSeconds`, threshold, `loopSuspected`). Put idle seconds in the heartbeat. Flush `stdout.log` on the existing persist cadence. Document liveness in `docs/troubleshooting.md`. Test: a run with repeated identical deltas must show a growing `idleSeconds` while `lastActivityAt` advances.
- Alternative: docs only ("read events.jsonl, ignore stdout.log mtime"). It resolves the two 09-26 cuts but leaves looping lanes looking healthy.

### RC-4: The tracked stream cap fails the run at the byte limit, so a finished lane can die as a failure
- Symptoms: pc2_d04d2e2a809f46e0 (08-16), pc2_f61fce28683a6236 and pc2_001f87091b6bea00 (08-20, the latter says the candidate was valid and green). All open, all predate 09-05.
- Evidence (verified): `BoundedCapture._retain` raises `CaptureLimit("retained")` once captured bytes exceed `max_bytes` (`stream_capture.py:111-119`). The drain trips `limit_signal`, and the runner terminates the group and finalizes `output_limit_exceeded` with exit 1 (`runner.py:2841-2850, 3000`). Parsing stops at the same point, so a terminal event or Completion Report after the cap is never seen. Since 76fcc28d (09-05) pi/omp default to 64 MiB and omp thinking deltas are compacted (`config.py:default_tracked_stream_max_bytes`). Every other harness, including native kimi, stays at 16 MiB. `docs/troubleshooting.md:471-476` states the fail is intended: "an endlessly verbose child still fails and is terminated".
- Still live?: partly. The omp case is mostly mitigated. Native kimi, claude, codex and the rest are unchanged. Which harness produced 001f87 is not stated in the cut (could not determine).
- Why it recurs: one number does two jobs, log-size guard and runaway detector. Runaway now has a real detector (`runaway_output`), so the cap no longer needs to kill.
- Recommended fix: past the retained cap, stop writing to `stdout.log`, append a `delegate.capture` truncation marker, and keep feeding the accumulator. Keep a hard transport cap as the kill switch. Then a lane that finished its work finishes as a success with a truncation warning. Test: a fake child that emits 17 MiB then a terminal event must end succeeded with `stdoutCapture.truncated`, where today it ends `output_limit_exceeded`.
- Alternative: raise the default, or spool. It is one line, but a mid-run stall only moves later.
- Ruling: the troubleshooting doc calls the current behavior intentional (docs, not deferred.md). See Open Questions.

### RC-5: Call mode is untracked, so a timeout or hang leaves nothing to inspect or salvage
- Symptoms: pc_faa8d0efc307 (Kimi call_timeout, no artifact, 07-24, open), pc_891c3e465b78 (call ignored `--timeout`, 08-03, open). 2 cuts.
- Evidence (verified): `_bounded_call_communicate` (`runner.py:5080-5240`) does enforce the deadline today. On timeout it raises `RunnerLaunchError("call_timeout")` and drops `stdout_buf`, so a call that produced a draft loses it (`:5209-5212`). Calls write no Registry entry (`docs/cli-reference.md:724`, "Calls are untracked").
- Still live?: yes for faa8d (partial output discarded). For 891c the enforcement is in the code and process-group reaping was rewritten 08-26 (dlg-7te). Whether the 30-minute overrun was a hung terminate path is not determinable from the cut.
- Recommended fix: on `call_timeout`, run the accumulator over the buffered stdout and return `partialText` plus a stdout tail in the error payload. Tracking calls is the bigger redesign; the salvage is the small piece that removes the pain.
- Alternative: leave untracked but require `--timeout` on long calls. Weaker: the draft is still lost.

### RC-6: No resource discipline for what a lane spawns
- Symptoms: pc2_6c06d43fe1508dd0 (vitest ~15 workers, ~55 GB, 08-11, open). 1 cut.
- Evidence: nothing in the runner limits descendants. Workspace spec `env=` exists (docs line 77) but is not a default. Not verified further.
- Recommended fix: a documented convention. Set the test runner's worker cap via a config-level child env default or the workspace spec, rather than adding limit machinery to the runner. Band-aid is right here.

## Cuts that are not this family's problem
- pc_742ba6e57a7a (pi ok:true with empty assistantText): F1 silent success. Call mode now sets `result_quality` empty (`runner.py:5540-5600`), so check F1 for whether `ok` follows it.
- pc2_0efa35ffaf04e97c and pc2_a3de12ceca639200 (Terra "awaiting approval"): the lane exits normally having done nothing. This is work-mode prompt framing plus F1, not a watchdog matter.
- pc2_c1a69c7f7a19d8bb, the second clause ("structured retries should rotate off the failing model"): F3 workflow supervision.
- pc2_d04d2e2a809f46e0, the quota-death half: provider routing, not liveness.

## Cuts that are stale / already fixed
- pc2_c1a69c7f7a19d8bb (fence loop, 08-25): the watchdog was built for this case (`stall_watchdog.py` header, introduced fa408afc 08-26).
- pc2_421c925163c061de (Devin commits ignored): `progress_probe` on worktree HEAD is wired (`runner.py:2595`, `stall_watchdog.py:907-919`). It sees commits only, not uncommitted edits (see RC-1).
- pc2_59556849280cc8bc (Devin done-detector): shipped (`runner.py:2886-2909`, CHANGELOG entry). Residual regex gaps are dlg-v0y item 5.
- pc2_8046adb9f3417794: `--stall-minutes` shipped and is inherited by resume/followup.
- pc2_1b55d59ac578fd56 (leaked test supervisors): fixed by 2ad2993d, `tests/process_guard.py`.
- pc_3ce0a8fecd13, pc_4d0e022e4db5 (08-05/08-09 shell death and hung wrapper): process-group cleanup was rewritten 08-26 (fbefd572, dlg-7te) and `_safe_process_group_id` refuses the caller's own group (`runner.py:4951`). I cannot show the cause was fixed. Treat as unreproduced.
- pc2_1aeb425dc0de3218 and the pi/omp half of the 16 MB cuts are partly mitigated by the 64 MiB default and thinking compaction (RC-4), not by any loop detector (RC-1).

## Open questions for Trey
1. Should loop detection kill, or only warn? Warn-only (Snapshot `loopSuspected`) ships safely with RC-3. Kill needs the working-tree fingerprint to avoid cancelling productive fix loops.
2. Should the tracked stdout cap degrade (truncate the log, keep parsing) instead of failing the run? Troubleshooting currently documents the fail as intended.
