# Patterns and conventions

Delegate is a small stdlib-only Python CLI with strong safety boundaries around child-runtime launch behavior.

## Runtime stays dependency-free

`pyproject.toml` declares `dependencies = []`. Development tools live in the optional `dev` group. Runtime integration is done by launching provider CLIs with `subprocess`, not by adding SDKs.

## Dataclasses and typed payloads

The code uses dataclasses for request and execution context objects, such as `Request` and `ParsedCommand` in `src/delegate_agent/cli.py`, `RunContext` in `src/delegate_agent/runner.py`, and removal option records in `src/delegate_agent/worktree_mgmt.py`.

## Fail closed on safety and config

Config validation in `src/delegate_agent/config.py` rejects invalid policy, isolation, and reasoning settings before launch. Explicit unsupported reasoning-effort requests fail closed in `src/delegate_agent/reasoning.py`. Persistent worktree preflight in `src/delegate_agent/worktree_execution.py` refuses dirty submodules and pass-through mode while auto-syncing ordinary tracked and untracked changes.

## Public argv must not leak prompt text

Prompt transport is declared in `src/delegate_agent/prompt_transport.py`. Dry-run payloads and manifests should show public argv, not secret prompt contents.

## Test behavior, not provider availability

Tests use fake child binaries and temporary homes. Avoid test assumptions that require real Cursor, Droid, Codex, Claude, Grok, Devin, OpenCode, or Kimi installations.

For subsystem boundaries, see [architecture](../overview/architecture.md).
