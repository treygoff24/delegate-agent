# How to contribute

Delegate Agent favors small, explicit changes with tests. Most changes touch one of five areas: CLI parsing, runtime request building, tracked execution, persistent worktrees, or configuration/policy.

## Work pickup

| Change area | Start with |
| --- | --- |
| Command grammar or help | `src/delegate_agent/cli.py`, `src/delegate_agent/command_help.py` |
| Runtime flags or prompt transport | `src/delegate_agent/cli.py`, `src/delegate_agent/prompt_transport.py` |
| Config, policy, or isolation defaults | `src/delegate_agent/config.py`, `config.example.json` |
| Tracking, snapshots, output recovery | `src/delegate_agent/runner.py`, `src/delegate_agent/run_registry.py`, `src/delegate_agent/run_output_commands.py` |
| Persistent worktrees | `src/delegate_agent/isolation.py`, `src/delegate_agent/worktree_execution.py`, `src/delegate_agent/worktree_mgmt.py` |
| Reasoning effort | `src/delegate_agent/reasoning.py`, `src/delegate_agent/capability_commands.py` |

## Definition of done

A change is ready for review when it uses `python3 bin/delegate.py` for local validation, preserves safe/work mode boundaries from `docs/security-model.md`, adds or updates tests, keeps public examples placeholder-only, and avoids committing `.delegate/` runtime state or secrets.

Read [patterns and conventions](patterns-and-conventions.md), [development workflow](development-workflow.md), and [testing](testing.md) before larger changes.
