# Blocked after Phase 0

The required causal reproduction did not occur. Both an in-tree `file_lock()` caller-stack probe and a real `fcntl.flock` `sitecustomize` probe covering subprocesses ran the focused registry tests and the complete suite from this linked worktree; neither observed any open or flock on the source checkout's `.delegate/.registry.lock`. The complete suite passed (2422 passed, 14 skipped, 2001 subtests), so there is no failing escape path to fix or guardrail seam to pin down.

Round 2 supersedes the implementation hold: the escape remains unobserved, so no
registry-resolution change is guessed. The permanent linked-worktree watcher
and bounded fd-inheritance audit now land as the guardrail, while finalization
resilience is implemented independently through the documented WAL contract.
