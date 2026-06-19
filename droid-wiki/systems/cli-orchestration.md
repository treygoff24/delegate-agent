# CLI orchestration

Active contributors: Trey

## Purpose

CLI orchestration parses user commands, resolves workspaces, loads configuration, builds runtime requests, and dispatches to dry-run, execution, inspection, worktree, capabilities, and help flows.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/cli.py` | Main orchestration module. |
| `src/delegate_agent/command_help.py` | Declarative help specs. |
| `src/delegate_agent/command_errors.py` | Shared command error handling. |
| `src/delegate_agent/run_metadata.py` | Shared run metadata fields. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `parse_cli()` | `src/delegate_agent/cli.py` | Parses global options, subcommands, runtime commands, and help tokens. |
| `resolve_workspace()` | `src/delegate_agent/cli.py` | Resolves `--cwd` or JSON cwd and classifies the workspace. |
| `build_request()` | `src/delegate_agent/cli.py` | Resolves reasoning metadata and builds an engine-specific request. |
| `execute_request()` | `src/delegate_agent/cli.py` | Dispatches to tracked, pass-through, temporary isolated, or persistent worktree execution. |

## How it works

`src/delegate_agent/cli.py` remains the largest source file because it owns global option placement, prompt resolution, config-aware request construction, safe prompt framing, temporary safe workspace creation, runtime argv builders, dry-run payloads, and final error rendering.

## Integration points

Runtime request details are explained in [runtime harnesses](runtime-harnesses.md). Execution handoff is explained in [tracked execution](tracked-execution.md). Workspace isolation is explained in [isolation and worktrees](isolation-and-worktrees.md).

## Entry points for modification

Parser changes start in `parse_cli()` and its `parse_*` helpers. Request-building changes start in `request_from_parsed()`, `request_from_input_json()`, and `build_request()`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/cli.py` | Main orchestration module. |
| `src/delegate_agent/command_help.py` | Declarative help specs. |
| `src/delegate_agent/command_errors.py` | Shared command error handling. |
| `src/delegate_agent/run_metadata.py` | Shared run metadata fields. |

## Related pages

- [Delegate CLI](../applications/delegate-cli.md)
