# Run record

Active contributors: Trey

## Purpose

A run record is the durable local representation of a tracked child-agent invocation.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/run_registry.py` | Registry storage and lookup. |
| `src/delegate_agent/runner.py` | File writing and process capture. |
| `src/delegate_agent/snapshot_view.py` | Snapshot merge behavior. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `Run ID` | `src/delegate_agent/run_registry.py` | Generated as `del_YYYYMMDDTHHMMSSZ_<hex>`. |
| `Alias` | `src/delegate_agent/run_registry.py` | Human handle such as `codex` or `claude-2`. |
| `Manifest` | `src/delegate_agent/runner.py` | Stable launch metadata. |
| `State` | `src/delegate_agent/runner.py` | Mutable status and byte counts. |

## How it works

`src/delegate_agent/run_registry.py` creates `.delegate/index.json`, alias files, and run directories. `src/delegate_agent/runner.py` writes manifest, state, snapshot, stdout, stderr, events, and completion report files.

## Integration points

Run records are produced by [tracked execution](../systems/tracked-execution.md) and consumed by [run inspection](../features/run-inspection.md).

## Entry points for modification

Change registry layout and alias behavior in `src/delegate_agent/run_registry.py`. Change file contents in `src/delegate_agent/runner.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/run_registry.py` | Registry storage and lookup. |
| `src/delegate_agent/runner.py` | File writing and process capture. |
| `src/delegate_agent/snapshot_view.py` | Snapshot merge behavior. |
