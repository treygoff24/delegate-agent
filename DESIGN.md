# W1-S supervisor lifecycle design

## Scope and base verification

This lane owns the supervisor/gate/retry lifecycle in
`src/delegate_agent/workflows/runtime.py` and
`src/delegate_agent/workflows/commands.py`, with regression coverage in
`tests/test_workflow_commands.py` and new test files.  The required
structured-output persistent-worktree retry was reproduced on the untouched
base with:

```text
python3 -m unittest tests.test_workflow_commands.WorkflowCommandTests.test_structured_retry_in_persistent_worktree_carries_attachment -v
Ran 1 test in 0.024s
OK
```

That confirms the already-healed structured path from `9ab3ca8` (carried into
the current tree by `6ca4f89`) and gives the retry-preservation work a known-good
baseline.  This design does not alter existing journal event keys or their
interpretation; all new replay metadata is additive.

## 1. Watchdog architecture and cancellation

`run_supervisor()` will create a per-run monitor thread after acquiring the
workflow lock and writing the initial running status.  The monitor owns a
`threading.Event` cancellation flag and polls the workflow directory and a
separate heartbeat record.  It never writes terminal workflow state and never
removes a worktree.  On an exit condition it sets the flag and lets the main
supervisor unwind normally.

Child waits become interruptible at the existing `_run_child_command()` seam.
Instead of one unbounded `communicate(timeout=None)`, the wait uses short
bounded intervals, touches the heartbeat, and checks the cancellation flag
between intervals.  A watchdog cancellation terminates the child process
group, reaps it, and raises a dedicated internal cancellation exception.  The
same callback is used by follow-up child waits.  No signal is sent to the
supervisor itself during normal watchdog operation, so Python `finally`
blocks run.

## 2. Independent heartbeat

The supervisor writes `heartbeat.json` beside the workflow status.  It contains
the workflow id, supervisor pid/token, a wall-clock `heartbeatAt`, and a
monotonic-compatible numeric timestamp.  Heartbeats are written at startup,
on ordinary status/event progress, and—most importantly—after every bounded
child-wait interval.  `updatedAt` in `status.json` remains status/event
metadata and is never used as the heartbeat.

The monitor treats a heartbeat as healthy only while its timestamp is newer
than the bounded stale threshold.  A two-sample missing-file grace avoids
mistaking an atomic status/heartbeat replacement for death.  A long child wait
therefore remains healthy because the main thread updates the heartbeat while
blocked; a wedged supervisor whose heartbeat is frozen does not.

## 3. Exit conditions and terminal handling

The monitor requests cooperative shutdown when any of these holds:

1. The supervised workflow status is terminal (`succeeded`, `failed`, or
   `killed`).
2. The workflow state directory/status file (the workflow registry entry) is
   gone.
3. The heartbeat is older than the bounded watchdog timeout.

The monitor is stopped and joined in `run_supervisor()`'s outer `finally`.
Normal terminal/gate paths keep their existing return values.  Watchdog
cancellation is recorded as a failed/stalled lifecycle outcome only after the
workflow body has unwound; it cannot overwrite a durable gate or a terminal
status that won the race.

## 4. Cleanup and retry-worktree ownership

All watchdog-triggered exits travel through the existing `try/finally` layers:
child process groups are terminated and reaped at the wait seam, structured
retry temporary workspaces are cleaned, and
`_release_structured_retry_worktree()` is called for every tracked retry run.
The supervisor-level finally provides a last-resort release pass for retry
handles registered in `WorkflowState`, including errors raised while parsing
or unwinding a gate.  Lock descriptors are still released by
`_held_workflow_lock()`.

Kill-and-leak-to-reap remains crash recovery only (`workflow kill` and the
existing process-group force path); the normal watchdog path never bypasses
Python cleanup by killing the supervisor process.

Timeout and output-cap retry attempts carry the failed child run id through the
existing `structuredRetryRunId`/`structuredRetryWorkspace` protocol.  A retry
is attached to the same persistent worktree/branch (or reuses the same safe
temporary workspace), preserving committed and on-disk WIP.  Output-cap
failures are converted to retry metadata before cleanup so the retry can be
launched; final exhaustion still performs the normal cleanup/release.

## 5. Gate-state durability

Gate persistence is centralized in a state method that records the durable
`gate` journal event and writes `status: "paused"`, `gateKey`, and
`gateResult` before raising `GateExit`.  The gate key is deterministic and
unchanged from the current implementation.  `append_event()` continues to
preserve an already-paused status, so concurrent progress/event writes cannot
clobber the gate.

Every gate path uses that method, including the nested-workflow
`gate=True`/`gate="on-failure"` path and the pipeline/parallel wave-level
propagation paths.  A propagated `GateExit` carries its gate metadata; before
any outer primitive re-raises it, the state method is idempotently re-applied.
Thus a wave-level failure gate can close admission while sibling items are
mid-stage, wait for them to unwind, and still have durable `gateKey`/`paused`
on disk before the supervisor exits.  Approval continues to use the existing
`workflow approve` flow.

## Replay and cache-key compatibility

On journal load, `agent_finished` rows whose result is red/failed (including an
exhausted `None`) are marked non-replayable for a resumed supervisor; existing
rows remain untouched and their original keys remain valid for historical
inspection.  A resumed call executes fresh instead of serving
`agent_cache_hit`.  Successful rows retain current replay behavior.

New agent cache keys include the requested timeout when one is supplied.  For
park-retry resumes, an additive retry-attempt discriminator is included only
when the prior key was marked failed.  The loader/caller keeps a legacy-key
fallback for pre-change journal rows, so a healthy PAUSED production run can
resume without re-keying or misinterpreting its existing events.

## Verification plan

Regression tests will exercise real subprocesses and filesystem state:

- detached supervisor exits after state/registry deletion and after a frozen
  heartbeat, with lock and structured-retry holds released;
- a multi-second child wait remains alive while heartbeats advance;
- red close and concurrent wave-level gate paths persist `paused`/`gateKey`
  and `workflow approve` proceeds;
- resume and park-retry re-execute red results with fresh journal keys, while
  successful legacy rows still replay;
- timeout and output-cap retries observe the same persistent worktree/branch
  and committed files.

The coordinator will run `bash scripts/gate.sh` after the scoped tests pass.
