# Isolation context

Active contributors: Trey

## Purpose

`IsolationContext` is the shared record of where a child run should execute and how that workspace relates to the source checkout.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/isolation.py` | Isolation context and planning. |
| `src/delegate_agent/config.py` | Isolation validation. |
| `src/delegate_agent/run_metadata.py` | Payload metadata emission. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `source_workspace` | `src/delegate_agent/isolation.py` | Original workspace path. |
| `isolation_mode` | `src/delegate_agent/isolation.py` | Requested value. |
| `effective_isolation` | `src/delegate_agent/isolation.py` | Mapped behavior. |
| `isolation_lifecycle` | `src/delegate_agent/isolation.py` | `none`, `temporary`, or `persistent`. |

## How it works

`build_isolation_context()` maps config and CLI choices into stable metadata used by dry-run, manifests, snapshots, and completion payloads. Temporary safe isolation is created later by `src/delegate_agent/cli.py`.

## Integration points

See [isolation and worktrees](../systems/isolation-and-worktrees.md) for full lifecycle behavior.

## Entry points for modification

Change lifecycle mapping in `src/delegate_agent/isolation.py`, validation in `src/delegate_agent/config.py`, and metadata emission in `src/delegate_agent/run_metadata.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/isolation.py` | Isolation context and planning. |
| `src/delegate_agent/config.py` | Isolation validation. |
| `src/delegate_agent/run_metadata.py` | Payload metadata emission. |
