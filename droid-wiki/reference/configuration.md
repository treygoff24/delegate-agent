# Configuration

`src/delegate_agent/config.py` implements configuration loading, validation, policy, and isolation defaults. `docs/configuration.md` is the full public reference.

## Load order

From lowest to highest precedence: embedded defaults, `~/.delegate/config.json`, workspace `.delegate/config.json`, `DELEGATE_CONFIG`, then internal CLI overrides.

## Sections

| Section | Purpose |
| --- | --- |
| `tracking` | Completion report defaults and raw log retention. |
| `cursor` | Cursor argv prefix, model, and effort model map. |
| `droid` | Droid binary, local model aliases, and default effort. |
| `codex` | Codex binary, model, profile, sandbox, and config behavior. |
| `claude` | Claude binary, model, native effort, permission mode, and session behavior. |
| `kimi` | Kimi binary and default model. |
| `reasoning` | Codex and Droid model capability declarations. |
| `policy` | Mode and harness-scoped runtime policy. |
| `isolation` | Safe/work isolation defaults. |
| `worktrees` | Persistent worktree data home and auto-prune. |

Common validation rules reject safe-mode bypass flags, `codex.safeSandbox`, `claude.workPermissionMode = bypassPermissions`, non-null `kimi.defaultReasoningEffort`, and unsupported `reasoning.capabilities` harness keys.

See [configuration and policy](../features/configuration-and-policy.md).
