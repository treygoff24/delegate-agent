# Cleanup opportunities

Data collected on 2026-06-18 from tracked files and git history only.

| Area | Finding | Priority signal |
| --- | --- | --- |
| Complexity | Orchestration and worktree logic are concentrated in a few large, high-churn files. | High |
| Docs drift | Documentation churn is high during a rapid release train, so docs should be checked against current behavior after each feature release. | Medium |
| TODOs | No TODO, FIXME, or HACK comments were found in tracked files. | Low |

Focused findings: [complexity](complexity.md), [docs drift](docs-drift.md), and [TODOs](todos.md). See [by the numbers](../by-the-numbers.md) for the source stats behind these findings.
