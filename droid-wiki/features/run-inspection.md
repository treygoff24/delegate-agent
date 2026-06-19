# Run inspection

Active contributors: Trey

## Purpose

Run inspection lets callers inspect tracked child-agent runs without reading raw `.delegate/` files.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/run_registry.py` | Registry storage, aliases, and summaries. |
| `src/delegate_agent/inspection_commands.py` | `runs` and `snapshot`. |
| `src/delegate_agent/run_output_commands.py` | `run-output`. |
| `src/delegate_agent/snapshot_view.py` | Snapshot display merge. |
| `src/delegate_agent/redaction.py` | Display redaction. |
| `src/delegate_agent/retention.py` | Raw log archival. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `SnapshotCommand` | `src/delegate_agent/inspection_commands.py` | Parsed snapshot command. |
| `RunsCommand` | `src/delegate_agent/inspection_commands.py` | Parsed runs command. |
| `RunOutputCommand` | `src/delegate_agent/run_output_commands.py` | Parsed output command. |
| `SnapshotView` | `src/delegate_agent/snapshot_view.py` | Merged display view. |

## How it works

Tracked runs write manifest, state, snapshot, stdout, stderr, events, and completion report files. `runs` computes effective status, `snapshot` merges display state and redacts by default, and `run-output` prefers completion reports before bounded log diagnostics.

## Integration points

[Tracked execution](../systems/tracked-execution.md) writes the registry data that inspection reads. [Prompt transport and redaction](prompt-transport-and-redaction.md) covers masking behavior.

## Entry points for modification

Change command parsing in `src/delegate_agent/cli.py`, summaries in `src/delegate_agent/run_registry.py`, snapshot merge behavior in `src/delegate_agent/snapshot_view.py`, and output selection in `src/delegate_agent/run_output_commands.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/run_registry.py` | Registry storage, aliases, and summaries. |
| `src/delegate_agent/inspection_commands.py` | `runs` and `snapshot`. |
| `src/delegate_agent/run_output_commands.py` | `run-output`. |
| `src/delegate_agent/snapshot_view.py` | Snapshot display merge. |
| `src/delegate_agent/redaction.py` | Display redaction. |
| `src/delegate_agent/retention.py` | Raw log archival. |
