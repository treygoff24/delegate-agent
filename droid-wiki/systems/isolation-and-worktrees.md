# Isolation and worktrees

Active contributors: Trey

## Purpose

Isolation decides where a child runtime executes: the source checkout, a temporary safe workspace, or a persistent Git worktree.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/config.py` | Validates isolation values and safe-mode restrictions. |
| `src/delegate_agent/isolation.py` | Isolation metadata, branch/path planning, persistent creation helpers, and prompt context. |
| `src/delegate_agent/cli.py` | Temporary safe workspace creation and request rewriting. |
| `src/delegate_agent/worktree_execution.py` | Persistent worktree preflight, creation, launch, and failure recording. |
| `src/delegate_agent/worktree_mgmt.py` | Worktree list, show, remove, prune, and gc lifecycle. |
| `docs/worktrees.md` | User-facing persistent worktree guide. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `IsolationContext` | `src/delegate_agent/isolation.py` | Structured metadata for effective isolation and lifecycle. |
| `safe_isolated_request()` | `src/delegate_agent/cli.py` | Creates and cleans temporary safe workspaces. |
| `PersistentWorktreeRecord` | `src/delegate_agent/worktree_mgmt.py` | Registry-derived worktree record. |
| `RemoveWorktreePlan` | `src/delegate_agent/worktree_mgmt.py` | Validated cleanup plan. |

## How it works

`auto` maps to temporary worktree isolation for local safe-mode harnesses and to `none` for work mode. Temporary safe isolation uses a detached Git worktree or directory copy and blocks external symlinks. Persistent worktree execution requires a Git workspace, valid `HEAD`, no dirty submodules, no pass-through mode, and a valid child binary; ordinary tracked and untracked changes auto-sync.

## Integration points

[Safe and work modes](../features/safe-and-work-modes.md) explains mode semantics. [Run inspection](../features/run-inspection.md) exposes execution cwd, branch, and cleanup commands. [Configuration](../reference/configuration.md) documents `isolation` and `worktrees` settings.

## Entry points for modification

Change validation in `src/delegate_agent/config.py`, mapping in `src/delegate_agent/isolation.py`, temporary safe behavior in `src/delegate_agent/cli.py`, persistent launch in `src/delegate_agent/worktree_execution.py`, and lifecycle commands in `src/delegate_agent/worktree_mgmt.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/config.py` | Validates isolation values and safe-mode restrictions. |
| `src/delegate_agent/isolation.py` | Isolation metadata, branch/path planning, persistent creation helpers, and prompt context. |
| `src/delegate_agent/cli.py` | Temporary safe workspace creation and request rewriting. |
| `src/delegate_agent/worktree_execution.py` | Persistent worktree preflight, creation, launch, and failure recording. |
| `src/delegate_agent/worktree_mgmt.py` | Worktree list, show, remove, prune, and gc lifecycle. |
| `docs/worktrees.md` | User-facing persistent worktree guide. |

## Related pages

- [Isolation context](../primitives/isolation-context.md)
