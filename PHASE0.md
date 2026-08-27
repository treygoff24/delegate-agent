# Phase 0: linked-worktree registry-lock reproduction

Date: 2026-08-27 UTC
Worktree: `/home/trey-agent/Code/burndown-worktrees/w1-registry`
Source checkout checked: `/home/trey-agent/Code/delegate-agent`

## Probe

The in-tree `delegate_agent.run_registry.file_lock()` was temporarily instrumented (gated by `DELEGATE_TRACE_LOCKS`) to record PID, cwd, lock path, resolved lock path, and a caller stack. The focused `tests/test_run_registry.py` run completed with 47 tests passed and 14 subtests passed; every registry lock path was under that test's temporary directory.

Because child processes may use the installed launcher rather than the checkout module, a second non-invasive probe used a temporary `sitecustomize.py` that wrapped the real `fcntl.flock`, read `/proc/<pid>/fd/<fd>`, and recorded every exclusive flock on a `.registry.lock`, including subprocesses. The complete suite was launched from this linked worktree:

```text
DELEGATE_FLOCK_TRACE=<temporary trace> PYTHONPATH=<temporary flock-hook> python3 -m pytest -q
```

Result:

```text
2422 passed, 14 skipped, 2001 subtests passed in 245.67s (0:04:05)
```

## Findings

- Exact caller opening `/home/trey-agent/Code/delegate-agent/.delegate/.registry.lock`: **not observed**.
- Source-lock flock events in the complete all-Python trace: **0**.
- Source-lock flock events in the in-tree `file_lock` trace: **0**.
- Every observed registry flock was under a temporary test workspace or an explicitly-created temporary repository/worktree; no observed path resolved through the linked worktree's Git common directory to the source checkout.
- No source-lock contention was present in `lslocks` after the run.

Therefore this run does not reproduce the reported escape and does not identify a causal caller or registry path beyond the negative result above. The temporary tracing code was removed before this evidence was recorded.
