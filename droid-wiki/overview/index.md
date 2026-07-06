# Delegate Agent overview

Delegate Agent is a Python CLI for handing bounded software-engineering tasks to another coding-agent runtime. It normalizes calls to Cursor Agent, Factory Droid, OpenAI Codex, Claude Code, xAI Grok Build, and Kimi Code, records local run metadata, and gives parent agents stable inspection commands.

## What it does

Delegate takes a task prompt, a runtime choice, and a mode, then builds a child command. The source entry point is `bin/delegate.py`, which imports `delegate_agent.cli:main` from `src/delegate_agent/cli.py`. Package metadata in `pyproject.toml` installs the public `delegate` console script.

It is intentionally a launcher and recorder. `README.md` and `CONTRIBUTING.md` both state that Delegate does not commit, push, merge, deploy, publish, or run a background service. Child runtimes do the actual investigation or implementation work.

## Main capabilities

| Capability | Source files | Notes |
| --- | --- | --- |
| Runtime dispatch | `src/delegate_agent/cli.py` | Parses `cursor`, `droid`, `codex`, `claude`, `grok`, and `kimi` commands. |
| Safe and work modes | `src/delegate_agent/cli.py`, `src/delegate_agent/config.py`, `src/delegate_agent/isolation.py` | Separates review/investigation from edit-capable runs. |
| Run registry | `src/delegate_agent/run_registry.py`, `src/delegate_agent/runner.py` | Stores manifests, state, snapshots, logs, aliases, and completion reports. |
| Inspection commands | `src/delegate_agent/inspection_commands.py`, `src/delegate_agent/run_output_commands.py` | Implements `runs`, `snapshot`, and `run-output`. |
| Persistent worktrees | `src/delegate_agent/isolation.py`, `src/delegate_agent/worktree_execution.py`, `src/delegate_agent/worktree_mgmt.py` | Runs edit-capable child agents in preserved Git worktrees. |
| Reasoning effort | `src/delegate_agent/reasoning.py`, `src/delegate_agent/capability_commands.py` | Validates provider-specific effort labels and reports capabilities. |
| Configuration | `src/delegate_agent/config.py`, `config.example.json` | Layers embedded, user, workspace, and `DELEGATE_CONFIG` settings. |

## How to read this wiki

Start with [architecture](architecture.md) for the control flow. Use [getting started](getting-started.md) to run the local checkout. Then jump into [runtime harnesses](../systems/runtime-harnesses.md), [tracked execution](../systems/tracked-execution.md), or [isolation and worktrees](../systems/isolation-and-worktrees.md) depending on the code you plan to change.

## Key source files

| File | Purpose |
| --- | --- |
| `bin/delegate.py` | Checkout-local executable wrapper. |
| `src/delegate_agent/cli.py` | Parser, request builder, runtime argv builders, temporary safe isolation, and execution dispatch. |
| `src/delegate_agent/config.py` | Config defaults, merge precedence, validation, policy, and isolation resolution. |
| `src/delegate_agent/runner.py` | Child process launch and tracked output capture. |
| `src/delegate_agent/run_registry.py` | Local `.delegate/` registry and alias/run lookup. |
| `src/delegate_agent/isolation.py` | Isolation planning and persistent worktree creation helpers. |
