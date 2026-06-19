# Tracked execution

Active contributors: Trey

## Purpose

Tracked execution launches child runtimes, captures stdout and stderr, normalizes streaming events, writes the local run registry, and returns parent-facing inspection commands.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/runner.py` | Child process execution, stream capture, manifests, state, snapshots, and completion reports. |
| `src/delegate_agent/harness_events.py` | Stream JSON normalization and final assistant text recovery. |
| `src/delegate_agent/run_registry.py` | `.delegate/` layout, run IDs, aliases, registry locks, and summaries. |
| `src/delegate_agent/inspection_commands.py` | `runs` and `snapshot` command implementation. |
| `src/delegate_agent/run_output_commands.py` | `run-output` implementation and completion-report fallback. |
| `src/delegate_agent/retention.py` | Raw log archival and archived log reading. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `RunContext` | `src/delegate_agent/runner.py` | Stable run metadata. |
| `StreamAccumulator` | `src/delegate_agent/harness_events.py` | Parses stdout lines into assistant text and normalized events. |
| `NormalizedEvent` | `src/delegate_agent/harness_events.py` | Compact event shape. |
| `register_run()` | `src/delegate_agent/run_registry.py` | Allocates a run ID and alias under a lock. |

## How it works

Execution writes `manifest.json`, `state.json`, `snapshot.json`, `stdout.log`, `stderr.log`, `events.jsonl`, and sometimes `completion-report.md` under `.delegate/runs/<runId>/`. `run-output` uses the completion report when present, tries synthetic final-message recovery from stdout when possible, and otherwise returns bounded diagnostics.

## Integration points

Runtime requests come from [runtime harnesses](runtime-harnesses.md). User-facing inspection is in [run inspection](../features/run-inspection.md). Worktree metadata is explained in [isolation and worktrees](isolation-and-worktrees.md).

## Entry points for modification

Change process launch and capture in `src/delegate_agent/runner.py`. Change stream parsing in `src/delegate_agent/harness_events.py`. Change registry schema or alias lookup in `src/delegate_agent/run_registry.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/runner.py` | Child process execution, stream capture, manifests, state, snapshots, and completion reports. |
| `src/delegate_agent/harness_events.py` | Stream JSON normalization and final assistant text recovery. |
| `src/delegate_agent/run_registry.py` | `.delegate/` layout, run IDs, aliases, registry locks, and summaries. |
| `src/delegate_agent/inspection_commands.py` | `runs` and `snapshot` command implementation. |
| `src/delegate_agent/run_output_commands.py` | `run-output` implementation and completion-report fallback. |
| `src/delegate_agent/retention.py` | Raw log archival and archived log reading. |

## Related pages

- [Run record](../primitives/run-record.md)
