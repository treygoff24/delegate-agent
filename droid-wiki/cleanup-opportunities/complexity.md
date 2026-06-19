# Complexity

Complexity is concentrated in CLI orchestration, worktree lifecycle management, and large integration-style test files.

| File | Lines | Last 90 day commit touches | Why it matters |
| --- | ---: | ---: | --- |
| `src/delegate_agent/cli.py` | 4,042 | 56 | Largest Python file and highest churn hotspot. |
| `src/delegate_agent/worktree_mgmt.py` | 1,712 | 29 | Large lifecycle module for cleanup, status, and safety edges. |
| `tests/test_delegate_execution.py` | 3,360 | 28 | Large execution behavior suite. |
| `tests/test_delegate_worktree_mgmt.py` | 2,093 | 27 | Large worktree behavior suite. |

Continue extracting command-specific behavior out of `src/delegate_agent/cli.py` into focused modules, following the pattern in `src/delegate_agent/run_output_commands.py`, `src/delegate_agent/worktree_commands.py`, `src/delegate_agent/capability_commands.py`, and `src/delegate_agent/inspection_commands.py`.
