# Safe and work modes

Active contributors: Trey

## Purpose

Delegate separates child-agent execution into `safe` and `work` modes. Safe mode is for review and investigation. Work mode is edit-capable and can run in the source workspace or in a persistent Git worktree.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/cli.py` | Mode parsing, safe prompt framing, and provider flags. |
| `src/delegate_agent/config.py` | Safe isolation restrictions and policy validation. |
| `src/delegate_agent/isolation.py` | Isolation metadata and lifecycle. |
| `docs/security-model.md` | User-facing safety boundaries. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `MODE_SAFE` | `src/delegate_agent/cli.py` | Safe mode constant. |
| `MODE_WORK` | `src/delegate_agent/cli.py` | Work mode constant. |
| `SAFE_REVIEW_PREFIX_BY_ENGINE` | `src/delegate_agent/cli.py` | Provider-specific no-edit prompt framing. |
| `SAFE_ISOLATION_REQUIRED_ENGINES` | `src/delegate_agent/config.py` | Engines that cannot use `none` in safe mode. |

## How it works

Safe mode combines prompt framing, provider flags, and isolation. Codex safe emits a read-only sandbox, Claude safe uses plan permission mode and limited tools, Grok safe uses read-only sandbox plus permission controls, Droid safe avoids work-mode unsafe flags, and Kimi safe relies on temporary isolation as the effective write boundary. Work mode enables edit-capable behavior where the runtime supports it.

## Integration points

Runtime flags are implemented in [runtime harnesses](../systems/runtime-harnesses.md). Workspace behavior is implemented in [isolation and worktrees](../systems/isolation-and-worktrees.md).

## Entry points for modification

Change syntax in parser helpers, prompt framing in `SAFE_REVIEW_PREFIX_BY_ENGINE`, runtime flags in `build_*_argv()`, and safe isolation restrictions in `src/delegate_agent/config.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/cli.py` | Mode parsing, safe prompt framing, and provider flags. |
| `src/delegate_agent/config.py` | Safe isolation restrictions and policy validation. |
| `src/delegate_agent/isolation.py` | Isolation metadata and lifecycle. |
| `docs/security-model.md` | User-facing safety boundaries. |
