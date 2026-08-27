# Blocked after Phase 0

The required causal reproduction did not occur. Both an in-tree `file_lock()` caller-stack probe and a real `fcntl.flock` `sitecustomize` probe covering subprocesses ran the focused registry tests and the complete suite from this linked worktree; neither observed any open or flock on the source checkout's `.delegate/.registry.lock`. The complete suite passed (2422 passed, 14 skipped, 2001 subtests), so there is no failing escape path to fix or guardrail seam to pin down.

Per the task's Phase 0 fail-closed instruction, implementation of dlg-znh and dlg-yjo is intentionally stopped here rather than guessed. Re-run with a capture from the environment that holds the source lock (or with the missing reproduction conditions) before changing registry resolution or finalization behavior.
