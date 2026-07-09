# Glossary

| Term | Meaning | Source |
| --- | --- | --- |
| Delegate | The wrapper CLI implemented by this repository. | `README.md`, `src/delegate_agent/cli.py` |
| Engine | The selected child runtime family: `cursor`, `droid`, `codex`, `claude`, `grok`, `devin`, `opencode`, or `kimi`. | `src/delegate_agent/cli.py` |
| Harness | Runtime adapter name used in run metadata. | `src/delegate_agent/runner.py`, `src/delegate_agent/run_registry.py` |
| Safe mode | Review/investigation mode. It is framed as no-edit and usually uses temporary isolation. | `docs/security-model.md`, `src/delegate_agent/cli.py` |
| Work mode | Edit-capable mode. It can run in place or in a persistent worktree. | `docs/security-model.md`, `src/delegate_agent/cli.py` |
| Isolation mode | Requested isolation value: `auto`, `none`, or `worktree`. | `src/delegate_agent/config.py`, `src/delegate_agent/isolation.py` |
| Effective isolation | Resolved behavior after mapping `auto` to `none` or `worktree`. | `src/delegate_agent/isolation.py` |
| Isolation lifecycle | `none`, `temporary`, or `persistent`. | `src/delegate_agent/isolation.py`, `src/delegate_agent/run_metadata.py` |
| Temporary safe workspace | Ephemeral detached worktree or directory copy used for safe-mode execution. | `src/delegate_agent/cli.py` |
| Persistent worktree | Preserved Git worktree and branch created for work-mode isolation. | `src/delegate_agent/worktree_execution.py`, `src/delegate_agent/worktree_mgmt.py` |
| Run registry | Workspace-local `.delegate/` metadata store. | `src/delegate_agent/run_registry.py` |
| Run ID | Stable ID shaped like `del_YYYYMMDDTHHMMSSZ_<hex>`. | `src/delegate_agent/run_registry.py` |
| Alias | Human handle such as `codex`, `cursor-2`, or `claude-3`. | `src/delegate_agent/run_registry.py` |
| Prompt transport | How Delegate sends prompt text to the child: argv, stdin, or private file. | `src/delegate_agent/prompt_transport.py` |
| Reasoning effort | Provider-specific model thinking-depth request. | `src/delegate_agent/reasoning.py` |
| Redaction | Display-time masking of secret-like strings in snapshots and run-output. | `src/delegate_agent/redaction.py` |

See [architecture](architecture.md) for how these terms connect.
