# Security

Delegate is a launcher and recorder, not a complete sandbox. The main trust boundary is between the source checkout and the child runtime execution workspace.

## What Delegate controls

`src/delegate_agent/cli.py` controls child argv, prompt framing, prompt transport, and execution dispatch. `src/delegate_agent/config.py` controls config validation, policy, and isolation restrictions. `src/delegate_agent/isolation.py` and worktree modules control temporary and persistent workspace behavior.

Delegate can choose safer defaults, but it does not control provider-side model behavior, child runtime implementation, credentials available on the host, network access granted outside the child CLI, or absolute-path side effects allowed by the runtime.

## Mode and isolation boundaries

Safe mode is review/investigation. Work mode is edit-capable. Safe mode is not proof of zero side effects. Temporary safe isolation protects the source checkout from ordinary relative-path edits inside the execution workspace. Persistent worktree isolation protects the source checkout during edit-capable work by creating a preserved Git worktree and branch.

Neither temporary nor persistent isolation is a host sandbox. Child processes may still use environment variables, credentials, network access, external tools, absolute paths, browser sessions, or MCP servers according to their own runtime permissions.

## Secret hygiene

Keep real config in a private location such as `~/.delegate/config.json` or a private `DELEGATE_CONFIG` path. `config.example.json` should remain placeholder-only. Do not commit `.delegate/` run logs, API keys, private model IDs, `.env` files, or machine-specific paths.

`src/delegate_agent/redaction.py` masks common secret shapes in display output, but raw local logs can still contain sensitive content.

Related pages: [safe and work modes](features/safe-and-work-modes.md), [isolation and worktrees](systems/isolation-and-worktrees.md), [prompt transport and redaction](features/prompt-transport-and-redaction.md).
