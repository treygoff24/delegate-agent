# Fun facts

These facts come from tracked files and git history as of 2026-06-18.

## No marker debt

Tracked files had 0 TODO, FIXME, or HACK comments. That does not mean there is no cleanup work. It means the repo does not encode cleanup as source comments today.

## Tests outweigh source

Source LOC is 13,334 and test LOC is 15,716, so tests are about 1.18x the size of source. The largest test files, `tests/test_delegate_execution.py` and `tests/test_delegate_worktree_mgmt.py`, mirror the complexity of runtime execution and worktree management.

## The orchestrator has gravity

`src/delegate_agent/cli.py` is 4,042 lines, the largest Python file, and the top churn hotspot with 56 touches in the last 90 days.

## Worktrees have their own gravity

`src/delegate_agent/worktree_mgmt.py` is 1,712 lines and was touched 29 times in the last 90 days.

## The release train was fast

The repo tagged `v0.1.3` on 2026-06-05 and `v0.5.0` on 2026-06-18, with seven tags in 14 calendar days.

## Runtime dependencies are intentionally tiny

`pyproject.toml` declares `dependencies = []`. Runtime integration happens by launching child CLIs, not by importing provider SDK packages.

See [by the numbers](by-the-numbers.md) for the full stats snapshot.
