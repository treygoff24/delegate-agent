# Data models

Delegate uses JSON payloads for run metadata, inspection commands, worktree management, and config discovery.

## Run registry files

| File | Writer | Purpose |
| --- | --- | --- |
| `.delegate/index.json` | `src/delegate_agent/run_registry.py` | Registry index with aliases and run entries. |
| `.delegate/aliases/<alias>` | `src/delegate_agent/run_registry.py` | Alias claim pointing to a run ID. |
| `.delegate/runs/<runId>/manifest.json` | `src/delegate_agent/runner.py` | Stable launch metadata and public argv. |
| `.delegate/runs/<runId>/state.json` | `src/delegate_agent/runner.py` | Mutable status, PID, byte counts, and timestamps. |
| `.delegate/runs/<runId>/snapshot.json` | `src/delegate_agent/runner.py` | Bounded assistant text, recent events, and metadata. |
| `.delegate/runs/<runId>/stdout.log` | `src/delegate_agent/runner.py` | Raw child stdout. |
| `.delegate/runs/<runId>/stderr.log` | `src/delegate_agent/runner.py` | Raw child stderr. |
| `.delegate/runs/<runId>/events.jsonl` | `src/delegate_agent/runner.py` | Recorded stream events. |
| `.delegate/runs/<runId>/completion-report.md` | `src/delegate_agent/runner.py` | Parent-facing markdown output when available. |

Schema constants such as `delegate.snapshot.v1`, `delegate.runs.v1`, `delegate.run-output.v1`, and `delegate.worktree-list.v1` live in `src/delegate_agent/run_registry.py` and `src/delegate_agent/worktree_mgmt.py`.

See [run record](../primitives/run-record.md) and [isolation context](../primitives/isolation-context.md).
