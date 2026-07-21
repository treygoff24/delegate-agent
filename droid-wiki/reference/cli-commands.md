# CLI commands

`docs/cli-reference.md` is the public command reference. This page maps command groups to source files.

| Command group | Source files | Notes |
| --- | --- | --- |
| `cursor`, `droid`, `codex`, `claude`, `grok`, `devin`, `opencode`, `pi`, `omp`, `kimi` | `src/delegate_agent/cli.py`, `src/delegate_agent/request_build.py` | Direct runtime launch in safe, work, or call mode where supported. |
| `dry-run` | `src/delegate_agent/cli.py` | Builds a request without launching a child runtime. |
| `run --input-json` | `src/delegate_agent/cli.py` | Programmatic JSON task input. |
| `runs` | `src/delegate_agent/inspection_commands.py`, `src/delegate_agent/run_registry.py` | Lists tracked runs. |
| `snapshot` | `src/delegate_agent/inspection_commands.py`, `src/delegate_agent/snapshot_view.py` | Shows bounded state for one run. |
| `run-output` | `src/delegate_agent/run_output_commands.py` | Shows completion report, stdout, stderr, or fallback diagnostics. |
| `worktree` | `src/delegate_agent/worktree_commands.py`, `src/delegate_agent/worktree_mgmt.py` | Manages persistent worktrees. |
| `setup` | `src/delegate_agent/setup_commands.py`, `src/delegate_agent/harness_discovery.py` | Discovers installed harness metadata, refreshes one profile cache, and creates minimal config only when absent. |
| `models` | `src/delegate_agent/model_discovery.py`, `src/delegate_agent/harness_discovery.py` | Merges config, cached/live discovery, legacy cache, and bundled advisory models. |
| `capabilities` | `src/delegate_agent/capability_commands.py`, `src/delegate_agent/reasoning.py`, `src/delegate_agent/harness_discovery.py` | Reports cached reasoning capabilities or refreshes the profile discovery cache. |
| `describe` | `src/delegate_agent/cli.py` | Reports command catalog, config resolution, policy, and runtime metadata. |
| `help` and `--help` | `src/delegate_agent/command_help.py` | Text and JSON help specs. |

See [data models](data-models.md) for registry and schema details.
