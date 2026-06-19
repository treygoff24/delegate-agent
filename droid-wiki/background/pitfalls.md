# Pitfalls

## Treating safe mode as a sandbox

Safe mode is a defensive default, not a proof of no side effects. `docs/security-model.md` says child runtimes may still access credentials, network, absolute paths, and external tools according to their own permissions.

## Updating runtime flags without updating docs and tests

Provider behavior is documented in `docs/cli-reference.md`, `docs/configuration.md`, and `docs/security-model.md`. Runtime flag changes in `src/delegate_agent/cli.py` should update those docs and tests.

## Forgetting public argv redaction

If a runtime changes prompt transport, update `src/delegate_agent/prompt_transport.py`, public argv generation in `src/delegate_agent/cli.py`, and manifest behavior in `src/delegate_agent/runner.py`.

## Removing worktrees outside Delegate

Persistent worktrees are tracked in `.delegate/` registry metadata. Use commands from `src/delegate_agent/worktree_commands.py` and lifecycle logic in `src/delegate_agent/worktree_mgmt.py`.

See [debugging](../how-to-contribute/debugging.md).
