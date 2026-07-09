# Delegate CLI

Active contributors: Trey

## Purpose

The Delegate CLI gives humans and parent agents one command surface for launching bounded tasks in Cursor Agent, Factory Droid, OpenAI Codex, Claude Code, xAI Grok Build, Devin, OpenCode, and Kimi Code.

## Directory layout

| File | Purpose |
| --- | --- |
| `bin/delegate.py` | Checkout-local wrapper around `delegate_agent.cli:main`. |
| `src/delegate_agent/cli.py` | Parser, request builder, runtime builders, dry-run, describe/models payloads, and dispatch. |
| `src/delegate_agent/command_help.py` | Declarative help registry used for text and JSON help. |
| `docs/cli-reference.md` | User-facing command and JSON contract reference. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `ParsedCommand` | `src/delegate_agent/cli.py` | Normalized parsed command. |
| `GlobalOptions` | `src/delegate_agent/cli.py` | Global flags such as `--cwd`, `--json`, and `--isolation`. |
| `CommandSpec` | `src/delegate_agent/command_help.py` | Declarative help object. |

## How it works

`parse_cli()` consumes global options before subcommands. Direct runtime commands share a grammar for safe/work mode, optional reasoning effort, optional prompt file, and trailing prompt text. `delegate --json describe`, `models`, `capabilities`, and `help` provide machine-readable discovery for parent agents.

## Integration points

Runtime argv building is in [runtime harnesses](../systems/runtime-harnesses.md). Inspection commands are in [run inspection](../features/run-inspection.md). Persistent worktree commands are in [isolation and worktrees](../systems/isolation-and-worktrees.md).

## Entry points for modification

Change parser behavior in `src/delegate_agent/cli.py`. Change command help in `src/delegate_agent/command_help.py`. Update `docs/cli-reference.md` and tests in `tests/test_delegate_parser.py` and `tests/test_command_help.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `bin/delegate.py` | Checkout-local wrapper around `delegate_agent.cli:main`. |
| `src/delegate_agent/cli.py` | Parser, request builder, runtime builders, dry-run, describe/models payloads, and dispatch. |
| `src/delegate_agent/command_help.py` | Declarative help registry used for text and JSON help. |
| `docs/cli-reference.md` | User-facing command and JSON contract reference. |

## Related pages

- [CLI orchestration](../systems/cli-orchestration.md)
