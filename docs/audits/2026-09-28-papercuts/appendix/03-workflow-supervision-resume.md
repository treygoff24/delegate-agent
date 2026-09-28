# F3: Workflow supervision, watchdog, and resume

## Bottom line
Most of this family has already been fixed in source code. Two changes did most of the work. On 09-01, commit 1a74beca deleted the heartbeat watchdog outright. The watchdog now fires only when state.json is confirmed deleted or the run has reached a terminal status. On 09-24, dlg-zfk (d7de840b) added stable `key=` step identity and `park_gate` actions, recorded in the journal, which removes the old "replay by position" design.

The pain kept recurring after the fixes for three reasons that are still live:
1. **Pinned runtime (RC-1).** Each workflow is pinned to the runtime it launched with, for life. Resume silently reuses that pin, so no supervisor fix ever reaches a running workflow. Pins that still contain the old heartbeat watchdog remain on disk on both machines.
2. **Detached supervisor (RC-2).** The supervisor always detaches, so systemd (or anything that owns the launcher's lifetime) reaps it.
3. **Resume kills live children (RC-3).** Resume cancels every in-flight child of the dead attempt, live ones included. Each external supervisor death therefore burns every running lane.

The resume/state model does not need another redesign. The redesign landed on 09-24 (journal authority plus stable keys plus park_gate). What's left are targeted fixes: a way to upgrade pins, a foreground supervisor mode, and adopting live children on resume instead of cancelling them.

## Root causes

### RC-1: The pinned runtime freezes supervisor code for the life of a workflow, so fixes never reach in-flight runs
- **Symptoms (7 cuts, 08-27 to 09-04, most still open):**
  - pc2_1ff944d28288f319, pc2_17cf0e5942793210, pc2_3c2638bfd3ed4ec0, pc2_20db02ea8773414d, pc2_63718c30f4267f81 (Mac/meetrec, 09-04). These report `heartbeat_stale` and a missing watchdog reason three days after both were fixed.
  - pc2_d207dc2eada553e8: "resume reproduces the same failure".
  - pc2_972515aa3967903b: a hotfix reverted by a redeploy.
  - The same mechanism explains why the other 4.0 and 4.1 cuts kept arriving after being marked resolved.
- **Evidence (verified in code):**
  - Resume calls only `workflow_pinning.load_pin(wf_id)` (`workflows/commands.py:402`) and launches `pin.cli_argv` (`commands.py:644-651`). There is no comparison against the live runtime and no warning.
  - `create_pin` refuses when the live runtime differs from an existing pin (`workflow_pinning.py:575-583`).
  - docs/delegate-workflows.md:79 states the rule: "that pin cannot change across a resume … refused as `pin_collision` rather than re-pinned".
  - The only visibility is `delegate doctor`/`promote`: "N active supervisor(s) may still use an older pinned runtime" (`workflow_pinning.py:992`).
- **Evidence (verified on disk):**
  - Devbox `~/.delegate-workflow-pins/runtimes` holds 21 runtimes. The six dated 08-27 to 08-31 still contain `heartbeat_stale`.
  - The Mac holds one such runtime (4ffad736…, 08-30).
  - The installed `~/.delegate` on both machines does not contain `heartbeat_stale`.
- **Inferred, not verified:** that the Mac meetrec workflow wf_1262d7313ea8 was pinned to 4ffad736. Its cuts quote `runtime.py:4048` heartbeat code that no longer exists at HEAD, which fits.
- **Still live at 0.31.0?** Yes, as a design choice. The old watchdog bug itself is gone from HEAD, but any workflow pinned before 09-01 still has it.
- **Why it keeps recurring:** pinning (dlg-44w.5, 43f5f246, 08-27) was the right answer to "a redeploy changes code under a running workflow". But it also means a fix is invisible until a fresh workflow starts. Operators resumed, saw the same death, and logged it again.
- **Recommended fix:**
  - Make resume print the pinned runtime digest and date whenever they differ from the live runtime ("resuming on pinned runtime X from 08-30; live is Y").
  - Add an explicit `workflow run --resume --repin` that re-pins when `WORKFLOW_KEY_VERSION` and the journal schema match. The `_require_current_workflow` check already gates on the version.
  - Touches `workflows/commands.py` (the resume branch) and `workflow_pinning.py` (a re-pin path that writes a new pin revision instead of colliding).
  - **Proof:** create a pin, change a runtime file, then resume. Today the test must show no warning and old-code execution; after the fix, a warning, and with `--repin` the new digest in `attempt_config`.
- **Alternative:** warning only, no re-pin. This keeps pin immutability absolute, and operators restart long runs to pick up fixes. That's cheaper, but a multi-day plan run stays stuck on a known-bad supervisor.
- **Related:** dlg-44w.5 (closed; pinning), dlg-adl (config freeze, cited in docs/evidence/runkillers/2026-09-01-dxd-watchdog.md).

### RC-2: The supervisor always detaches, so it does not live and die with whatever launched it
- **Symptoms (5 cuts, 08-25 to 09-24):**
  - pc2_310f0559a371c2f3 (open), pc2_42fe3cfcf284139e and pc2_55fd929544603293 (archived), pc2_535ffec186cda990 (resolved via a docs recipe).
  - The second half of pc2_e62ec824c2c4acb0 (open, 09-24): "delegate warns but only after it has already run once wrongly".
- **Evidence (verified in code):**
  - `detach_supervisor` always double-forks with `setsid` (`runtime.py:6446-6477`). The launcher returns, so it is never the unit's main PID.
  - Under `KillMode=control-group`, systemd reaps the cgroup, and setsid does not help.
  - The only guard is `_systemd_detach_warning` (`commands.py:110-140`), appended at `commands.py:701`. The detach proceeds regardless (`commands.py:706`), and the warning prints after the fact (`commands.py:744`).
  - There is no foreground mode, although `run_supervisor` already supports in-process use (`dedicated_process=False`, `commands.py:244-247`).
  - **Now diagnosable:** since 09-14, SIGTERM from systemd is caught by `_SignalRelay` (`runtime.py:871`). The status then records `supervisor received SIGTERM` and cancels children. The "unit says success, workflow silently dead" confusion is lessened on the workflow side, but the kill still happens.
- **Still live?** Yes. dlg-jyb (open) also reports the warning firing on every launch and gate children left for systemd to SIGKILL on pause. I did not verify that report.
- **Why it recurs:** the devbox house rule says "run long work as a systemd unit", and the tool always forks away from its unit. The two collide on every launch unless the operator remembers `-p KillMode=process`.
- **Recommended fix:**
  - Add `workflow run|approve --foreground`: run `run_supervisor` in the calling process, which then becomes the unit's main PID, so the unit's lifetime and exit status track the workflow's.
  - When systemd detection finds a reaping KillMode, refuse to detach and point at `--foreground`, rather than warn.
  - Touches `workflows/commands.py` (emit_run/emit_approve), `cli_parser.py`, `command_help.py`.
  - **Proof:** a unit-level test that monkeypatches `_systemd_detach_warning` to report `control-group` and asserts that today the detach happens anyway (red), and after the fix the launch refuses. Plus a test that `--foreground` returns the supervisor's own exit code.
- **Alternative:** dlg-jyb's `--unit NAME` wrapper that does the systemd-run with the right KillMode and environment. It gives better ergonomics for pinned env vars, but it builds systemd knowledge into delegate. Foreground mode is simpler and covers any process supervisor.
- **Related:** dlg-jyb.

### RC-3: Resume treats every child of the dead attempt as an orphan and cancels it, so each supervisor death loses all in-flight lane work
- **Symptoms:**
  - Amplifies every RC-2 death.
  - pc2_6fddf27d4b0e9ca7 recorded the opposite behaviour on 09-03 ("children adopted"). That behaviour was removed on 09-14.
  - pc2_7b2cdcbb9638b437 (orphan-running Opus children).
  - Retry work is lost across a restart: the first attempt's retry worktree is released in `finally` (`runtime.py:6415-6418`). This is the resume-boundary residue of pc2_81ed80a726b36b31 and pc2_89bc30be0526cb4e.
- **Evidence (verified in code):**
  - The resume path calls `runtime.cancel_workflow_children(workspace, wf_id)` for every non-terminal child (`commands.py:466-490`).
  - `_cancel_workflow_runs` selects every run in the group that isn't terminal, whether or not its pid is live (`runtime.py:5622-5642`).
  - docs/delegate-workflows.md:198-201 documents it ("cancels every child the previous attempt still had in flight"). Line 160 still says resume will "adopt running children", which contradicts it.
  - Meanwhile `_adopt_existing_agent_run` already knows how to wait on a live non-terminal run by key (`runtime.py:3896-3899`) and to skip dead ones (`agent_adopt_skipped`).
- **Could not determine:** whether an orphaned `delegate run` child reliably finishes and records its result after its supervisor's stdout pipe closes (a possible EPIPE on the final print). This needs one live test.
- **Still live?** Yes.
- **Why:** dlg-m5w (4e6dd3cf) fixed dead-pid orphans that failed thunks with "already terminal (stale)". It reused the kill path wholesale ("the same child cancellation the kill path uses"), and that path also kills live ones.
- **Recommended fix:**
  - Seal only children whose pid is dead or unlaunched past the grace window, and leave live ones for key-based adoption.
  - `cancel_stale_scope_children` already cancels any the new script no longer reaches.
  - Touches `commands.py` (the resume seal) and `runtime.py` (a `dead_only` filter in `_cancel_workflow_runs`). Fix the docs:160/198 contradiction.
  - **Proof:** launch a workflow whose child sleeps, SIGKILL the supervisor, resume, and assert that the child's pid survives and its result is adopted. Today that test is red (the child is cancelled).
- **Alternative:** keep cancel-all and document it as the price of a clean restart. That's simpler and has no risk of a double child, but together with RC-2 it makes every unit mistake cost hours of lanes.
- **Related:** dlg-m5w (closed), dlg-x0z (open; dry-run stale-scope reap, same seam).

### RC-4: Replay identity was positional while scripts steered by their own mutable state, so resume re-ran finished work and decisions bound to the replay cursor
- **Symptoms:** pc2_2d1bc13937a18ffd, pc2_e62ec824c2c4acb0, pc2_7c82275e75fe4304, pc2_85134ffe3949ff05, pc2_1b77127ac72a49ac (the second half), pc2_61ded5e747e47356, pc2_886208a9c0ab7ad4, pc2_7bc42cc75a00acd1, pc2_471b1c1c13598cc6, pc2_7099d209fb6f61be, pc2_cdb2780ca46cde10. These span 08-30 to 09-24; about half are open.
- **Evidence (verified):**
  - The unkeyed `_agent_key` still hashes `scope_path + prompt + opts` (`runtime.py:5459-5461`), and the scope comes from positional counters (`runtime.py:2896-2908`).
  - dlg-zfk (d7de840b, 09-24) added `key=` on agent, parallel, pipeline, and workflow. A keyed step replays by key inside its named scope, and a changed prompt adopts the recorded result and journals `key_prompt_mismatch` (`runtime.py:3771-3844`).
  - dlg-zfk also added `park_gate(key, actions=)` and `workflow approve --gate/--action/--note/--data` (`commands.py:1069-1098`). That fixes pc2_7099d209fb6f61be and moves human decisions into the journal.
  - Adoption now skips cancelled, stale, or never-launched runs (`runtime.py:3869-3894`), which fixes pc2_cdb2780ca46cde10.
  - writing-plans consumes this: `planc/engine/guardrails.py:1100,1137,1333` use `park_gate` and `has_cap("agentKey")`.
- **Still live?** Partly:
  - Unkeyed calls stay positional, by design for compatibility.
  - Workflows pinned before 09-24 cannot use the new capabilities (see RC-1).
  - writing-plans still falls back to `plan-state.json` plan-unpark decisions when `gateActions` is missing, and that fallback is a second source of truth next to the journal.
- **Why it recurred:** the underlying design had two truths. The delegate journal knew what ran, while the writing-plans `plan-state.json`, edited by outside sessions, decided what should run. Every mismatch between them showed up as a new symptom: counters shift, a rotated route changes opts, a decision is consumed before its gate, or an ack isn't reloaded.
- **Recommended fix:** no new delegate redesign.
  - Have `workflow check` warn on an unkeyed `agent()` inside `parallel`/`pipeline` in a script that uses keys anywhere.
  - Point the docs at `key=` as the default for any resumable script.
  - Retire the `plan-state` decision fallback in writing-plans once pins predating 09-24 have drained.
  - **Proof:** a `workflow check` fixture with a mixed keyed/unkeyed parallel that must emit the warning.
- **Alternative:** make `key=` mandatory for new workflows under a key version bump. That gives the strongest guarantee, but breaks every ad-hoc script.
- **Related:** dlg-zfk and dlg-44w.1 (closed), dlg-b35 (closed; writing-plans save_state clobbering acks).

### RC-5: Leftover diagnostic gaps on non-watchdog exits
- **Symptoms:**
  - The residue of pc2_a1c06f8955e9af22, pc2_63718c30f4267f81, and pc2_747f41007200d4ad (watchdog reason not persisted) is fixed: `_record_watchdog_fire` journals `workflow_watchdog_fired` with the reason and sets `watchdogReason` (`runtime.py:1940-1999`, 1fbed4cf 08-31). The supervisor now records the reason on disk.
  - pc2_fab29db36bf151c2 is fixed on the main path (`runtime.py:4648-4656` carries key, label, and model).
- **Still live (verified in code):**
  - The followup timeout row still has only engine and timeout (`runtime.py:5147`); tracked by dlg-bum.
  - `_SignalRelay` handles only SIGTERM and SIGHUP (`runtime.py:871`). SIGINT or any other `BaseException` goes to the generic branch (`runtime.py:6387-6395`). That branch writes `failed` without cancelling children (they are orphaned until the next resume), and the error text may be empty for `KeyboardInterrupt`.
- **Could not determine:** the mechanism of pc2_f7c64c9262286753 (a Ctrl-C in the Claude Code harness killing a setsid supervisor on 09-03). That message is the watchdog's, and the runtime then was probably pre-09-01. The current code would record the cause either way.
- **Recommended fix:** add SIGINT to `_SignalRelay._SIGNALS` so every signal exit takes the cancel-children path, and add key and label to the followup `agent_timeout`.
  - **Proof:** send SIGINT to a live test supervisor. Today its children stay running and the status has no `signal`; after the fix it matches the SIGTERM test.
- **Alternative:** leave SIGINT unhandled, since a setsid daemon rarely receives it. That's a cheap non-fix, and the orphans still get cleaned up on the next resume.
- **Related:** dlg-bum, dlg-ao0.

## Cuts that are not this family's problem
These belong to the writing-plans engine (`~/Code/writing-plans/src/planc`: plan-state, closes, coordinator acks, fix_owned, compiler):
- pc2_85134ffe3949ff05 and pc2_1b77127ac72a49ac (close receipts): the close is the engine's step. Delegate's part is covered under RC-4.
- pc2_f4c63a08ea91ce5e, pc2_686d4783560613bb, pc2_2ef817414a86d237: ack reload and key shape. dlg-b35 is closed.
- pc2_0cad4a71005be186 (fix stage dropped by wave close).
- pc2_59f6c40c333065a0 (`_prepare_close_worktree`).
- pc2_ddd4c9001bf7b01b (fix_owned not persisted). The delegate side, that `--args` is refused on resume, is deliberate pinning.
- pc2_f2ca04fa5796c2cf (compiler cwd guard).
- pc2_7dc37c43fa3de0e0 (standalone_guardrails globals).
- pc2_a9afcf1948a08592 (fix-exhaustion recovery path).
- pc2_a08895b885fa4a60 (retry policy on blocked_dependency).
- pc2_878ddbe279bd2843 and pc2_a18f673ef68d04a5 (lanes cut from spine or root HEAD). Delegate now offers `agent(base=...)` (docs/delegate-workflows.md:77, workspace_spec.py), so choosing the base is the engine's job.

pc2_256155ca6742aa3d is an operator-probe rule for a shell-footguns doc, not a code defect. Its premise (heartbeat as a liveness signal) is obsolete now that the heartbeat is gone.

## Cuts that are stale / already fixed
- **Heartbeat starvation and park kills** (pc2_365f765b21c3377c, pc2_3af45a97a99e105c, pc2_68779013f86b773a, pc2_a75176e09d6dd6fc, pc2_747f41007200d4ad, pc2_ef0e0c7caa41d64f, pc2_20db02ea8773414d, pc2_3c2638bfd3ed4ec0, pc2_1ff944d28288f319, pc2_17cf0e5942793210, pc2_7b2cdcbb9638b437, pc2_d207dc2eada553e8, pc2_972515aa3967903b): fixed by 1a74beca (09-01). The heartbeat writer, the stale window, and the timeout knob were all removed. `_check` now fires only on a confirmed ENOENT across two samples (`runtime.py:6080-6113`), and an unreadable file never fires. The unit suite `tests.test_workflow_watchdog_unit` passed 27/27 here. These cuts stay live only on old pins (RC-1).
- **Watchdog reason not journaled** (pc2_a1c06f8955e9af22, pc2_63718c30f4267f81): fixed by 1fbed4cf (see RC-5).
- **Bare `_supervise` bypassing the pin** (pc2_b7c5486c2473320d): now refused without an attempt snapshot (`commands.py:223-233`).
- **Resume runs the frozen script silently** (pc2_9a4c9ec8841bd066, the first half of pc2_1b77127ac72a49ac): resume now warns on source or frozen-hash drift (`commands.py:1499-1540`).
- **Missing-pid child blocks resume** (pc2_3734873284abd8cf): resume seals unlaunched `missing_pid` rows after 300s (`wait_cancel_commands.py:603,676`, d7de840b).
- **Stale adoption** (pc2_cdb2780ca46cde10): fixed (see RC-4).
- **No --notify on workflow run** (pc2_5c1a94e9b622cd6c): landed (docs/delegate-workflows.md, the `--notify` section).
- **Retry restarting from base** (pc2_81ed80a726b36b31, pc2_89bc30be0526cb4e): within one supervisor lifetime, a retry re-enters the first attempt's worktree (`runtime.py:4100-4135`). Across a resume it doesn't (RC-3).
- **Busy registry lock** (pc2_c8b9c3368f4b1388, pc2_8378e762fa361f18): b3919ac1 (08-11) moved retention to its own lock and made progress writes non-blocking. There have been no recurrences.
- **Wait client killing the supervisor** (pc2_6fddf27d4b0e9ca7): `emit_wait` is a passive status poller (`commands.py:1029-1039`).

## Open questions for Trey
1. **Re-pinning on resume (RC-1).** Should resume be allowed to re-pin to the live runtime (opt-in `--repin`, guarded by key version)? Or is pin immutability absolute, so we only warn? This decides whether long plan runs can pick up supervisor fixes.
2. **Live orphans on resume (RC-3).** Should resume adopt live orphaned children instead of cancelling them? This reverses part of dlg-m5w's choice, and it's worth one live test first to confirm orphans actually finish and record their results after their supervisor dies.
3. **Foreground mode (RC-2).** `--foreground` plus a refusal under a reaping KillMode, or dlg-jyb's `--unit` wrapper?
