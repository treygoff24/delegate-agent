# Configuration

Delegate loads JSON config from embedded defaults plus optional user and workspace files.

## Config locations and precedence

From lowest to highest precedence:

1. Embedded defaults in the package.
2. User config: `~/.delegate/config.json`.
3. Workspace config: `.delegate/config.json` under the resolved workspace root.
4. `DELEGATE_CONFIG=/path/to/config.json`, when set.
5. Internal CLI overrides used by some commands.

If `DELEGATE_CONFIG` is set, the file must exist. Delegate fails closed instead of silently falling back to another config.

Config objects are deep-merged. That means an explicit config can override a
specific key, but nested maps such as `droid.models` are merged with lower layers
rather than cleared. For a deterministic automation run with no user-level
aliases, set `HOME` to a temporary directory before launching Delegate:

```bash
clean_home="$(mktemp -d)"
HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json models
```

Check the active source:

```bash
delegate --json describe | jq .configSource
delegate --json describe | jq .configResolution
delegate --json models
```

`describe` and `models` include `configResolution.layers`, an ordered view of
the embedded defaults plus discoverable user, workspace, and `DELEGATE_CONFIG`
layers. This is read-only observability; inspecting it does not modify
`~/.delegate` or workspace config files.

## Example

Copy `config.example.json` and replace placeholders before real Droid runs:

```json
{
  "cursor": {
    "argvPrefix": ["agent"],
    "defaultModel": "composer-2.5",
    "defaultReasoningEffort": null,
    "reasoningEffortModels": {}
  },
  "droid": {
    "binary": "droid",
    "defaultReasoningEffort": null,
    "models": {
      "reviewer": "replace-with-read-only-model-id",
      "implementer": "replace-with-edit-capable-model-id"
    }
  },
  "reasoning": {
    "capabilities": {}
  },
  "codex": {
    "binary": "codex",
    "defaultModel": null,
    "defaultReasoningEffort": null
  },
  "claude": {
    "binary": "claude",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "workPermissionMode": "auto",
    "noSessionPersistence": true,
    "bare": false
  },
  "kimi": {
    "binary": "kimi",
    "defaultModel": "kimi-code/kimi-for-coding",
    "defaultReasoningEffort": null
  }
}
```

`reviewer` and `implementer` are local aliases. They are intentionally provider-neutral. Put the real provider/model IDs in your private config, not in public docs or shared examples.

Reasoning-effort settings are optional. A per-run `--reasoning-effort LEVEL` or JSON `reasoningEffort` overrides provider defaults. If no effort is requested or defaulted, Delegate emits no reasoning-effort argv and preserves current runtime behavior.

## Sections

### `tracking`

Controls local run recording.

```json
{
  "tracking": {
    "completionReport": {"defaultMode": "markdown"},
    "retention": {"enabled": true, "rawLogDays": 7}
  }
}
```

- `completionReport.defaultMode`: `markdown` or `none`.
- `retention.enabled`: whether raw logs are eligible for archive-only retention.
- `retention.rawLogDays`: non-negative number of days before bulky raw logs may be archived.

### `cursor`

```json
{
  "cursor": {
    "argvPrefix": ["agent"],
    "defaultModel": "composer-2.5",
    "defaultReasoningEffort": null,
    "reasoningEffortModels": {
      "high": "replace-with-thinking-cursor-model"
    }
  }
}
```

- `argvPrefix`: command prefix for Cursor Agent. Use an array so wrappers are possible.
- `defaultModel`: non-empty model name passed to Cursor.
- `defaultReasoningEffort`: optional non-empty effort string. If set, it requires a matching `reasoningEffortModels` entry. When no mapping exists, the run proceeds without reasoning effort and records a warning (an explicit `--reasoning-effort` flag still fails closed).
- `reasoningEffortModels`: map from effort strings to Cursor model names. Cursor currently has no standalone reasoning-effort flag, so Delegate implements Cursor effort by selecting the configured model for that effort.
- `cursor.binary` is not supported; use `argvPrefix`.

### `droid`

```json
{
  "droid": {
    "binary": "droid",
    "defaultReasoningEffort": null,
    "models": {
      "reviewer": "replace-with-read-only-model-id",
      "implementer": "replace-with-edit-capable-model-id"
    }
  }
}
```

- `binary`: child executable for Droid.
- `models`: map of local aliases to real Droid model IDs. May be empty if you do not use Droid; running a Droid alias that is not present fails with `invalid_alias`.
- `defaultReasoningEffort`: optional non-empty effort string validated against the resolved Droid model before launch. When the model has no matching capability declaration, the run proceeds without reasoning effort and records a warning (an explicit `--reasoning-effort` flag still fails closed).
- Placeholder IDs that start with `replace-with-` are rejected for real runs.

### `codex`

```json
{
  "codex": {
    "binary": "codex",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "profile": null,
    "workSandbox": "workspace-write",
    "ephemeral": true,
    "ignoreUserConfig": false
  }
}
```

- `defaultModel`: optional model string. `null` lets Codex choose its own default.
- `defaultReasoningEffort`: optional non-empty effort string. When a Codex model resolves (run input or `codex.defaultModel`) and supports the level, Delegate emits a Codex config override; otherwise the run proceeds without reasoning effort and records a warning. An explicit `--reasoning-effort` flag still fails closed and requires a resolved model.
- `profile`: optional Codex profile name. It is config-only; JSON run input cannot set it.
- `workSandbox`: `read-only`, `workspace-write`, or `danger-full-access` for Codex work mode when full bypass is not enabled.
- `ephemeral`: include Codex `--ephemeral` in JSON-streaming runs.
- `ignoreUserConfig`: include Codex `--ignore-user-config`.
- Codex safe mode always uses `--sandbox read-only` in v1; `codex.safeSandbox` is rejected.

### `claude`

```json
{
  "claude": {
    "binary": "claude",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "workPermissionMode": "auto",
    "noSessionPersistence": true,
    "bare": false
  }
}
```

- `binary`: child executable for Claude Code.
- `defaultModel`: optional Claude model string. `null` lets Claude Code choose its own default.
- `defaultReasoningEffort`: optional Claude Code effort string: `low`, `medium`, `high`, `xhigh`, or `max`. Delegate emits it as `--effort`.
- `workPermissionMode`: Claude Code permission mode for work runs. Allowed values are `acceptEdits`, `auto`, `default`, `dontAsk`, and `plan`.
- `workPermissionMode` cannot be `bypassPermissions`; use `policy.harness.claude.work.bypassApprovalsAndSandbox` when you explicitly want Delegate to emit Claude `--permission-mode bypassPermissions`.
- `noSessionPersistence`: defaults to `true`, adding `--no-session-persistence` to headless calls.
- `bare`: opt-in `--bare` mode for runs that should skip Claude Code customizations and auto-discovery. Defaults to `false`, which is consistent with how the other harnesses use their own installed configuration. Be aware of the footprint: with `bare: false`, a delegated run loads the operator's full Claude Code environment — hooks, skills, plugins, output styles, and auto-memory. `--strict-mcp-config` suppresses MCP servers, but nothing else, so each run carries that ambient system-prompt context (extra latency and token cost) and is not hermetic. Set `bare: true` for cost-sensitive or reproducible runs that should ignore local customizations.
- Claude safe mode uses `claude -p`, stdin prompt delivery, `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob, and selected read-only Bash tools.

### `kimi`

```json
{
  "kimi": {
    "binary": "kimi",
    "defaultModel": "kimi-code/kimi-for-coding",
    "defaultReasoningEffort": null
  }
}
```

- `binary`: path to the `kimi` executable.
- `defaultModel`: default Kimi model alias (e.g. `kimi-code/kimi-for-coding`). Set to `null` to let Kimi use its own configured default.
- `defaultReasoningEffort`: not supported in v1; must be `null`.
- Kimi's thinking/effort level is configured in `~/.kimi-code/config.toml`, not through Delegate.
- Kimi safe mode uses Delegate's read-only safety prompt and isolated workspace. Kimi prompt mode auto-approves tool actions, so the isolated workspace is the effective write boundary.
- Kimi work mode emits `--yolo` by default.

### `reasoning`

```json
{
  "reasoning": {
    "capabilities": {
      "codex": {
        "custom-model": {
          "supported": ["low", "medium", "high"],
          "default": "medium"
        }
      },
      "droid": {
        "provider/custom-model": {
          "supported": ["high", "xhigh"],
          "default": "high"
        }
      }
    }
  }
}
```

- `capabilities`: optional map of harness name (`codex` or `droid` only; cursor uses `cursor.reasoningEffortModels`, and Claude uses static native `--effort` labels) to model capability declarations.
- `supported`: non-empty array of exact effort strings. Delegate treats these literally; it does not translate `xhigh` to another provider spelling.
- `default`: optional effort string that must be present in `supported`. It is informational only (shown by `delegate capabilities`); launches apply `<engine>.defaultReasoningEffort`, not per-model defaults.
- Effort strings may not contain whitespace, double quotes, or backslashes.

Capability precedence is config, then workspace cache, then bundled fallback. Use config for private or newly released models. A malformed workspace cache is ignored (treated as absent) and is overwritten by the next `capabilities refresh`. Use `delegate --json capabilities` to inspect the merged view and `delegate --json capabilities refresh` to refresh the workspace-local cache at `.delegate/capabilities/reasoning.json`.

### `policy`

```json
{
  "policy": {
    "profile": "safe",
    "work": {
      "networkAccess": true
    }
  }
}
```

Profiles:

- `safe`: default. No approval/sandbox bypass flags by profile.
- `trusted-hooks`: permits Codex hook-trust bypass for work mode.
- `external-sandbox`: permits Codex approval/sandbox bypass and hook-trust bypass for work mode. Use only inside a separate sandbox you control.
- `custom`: no profile defaults; use explicit per-mode/per-harness settings.

Supported boolean policy keys: `networkAccess`, `webSearch`, `bypassApprovalsAndSandbox`, and `bypassHookTrust`. Only Codex currently consumes all of these fields. Claude consumes `bypassApprovalsAndSandbox` only from the harness-scoped `policy.harness.claude.work` block, mapping it to `--permission-mode bypassPermissions`. Cursor, Droid, and Kimi ignore unsupported policy fields rather than translating them to runtime flags.

`bypassApprovalsAndSandbox` and `bypassHookTrust` are work-mode escalations. Setting either to `true` under a safe-mode policy block (`policy.safe` or `policy.harness.<engine>.safe`) is rejected at config load with `invalid_policy_config`, because safe mode is read-only by contract.

### `isolation`

```json
{
  "isolation": {
    "safe": "auto",
    "work": "none"
  }
}
```

Allowed values are `auto`, `none`, and `worktree`. For Cursor, Claude, Droid, and Kimi
safe mode, an effective value of `none` is rejected because those safe contracts
depend on Delegate's temporary workspace/config boundary. Use `auto` or
`worktree` for those safe-mode harnesses. Codex safe can use `none` because the
Codex read-only sandbox remains active.

Embedded defaults:

- `safe`: `auto`. Cursor, Claude, Droid, Codex, and Kimi safe use temporary workspace isolation by default.
- `work`: `none`. Work mode runs in the real workspace unless you opt into worktree isolation.

### `worktrees`

```json
{
  "worktrees": {
    "dataHome": null,
    "autoPrune": {
      "enabled": false,
      "mergedOlderThanDays": 7
    }
  }
}
```

- `dataHome`: persistent-worktree root. `null` means `~/.delegate/worktrees`.
- `autoPrune.enabled`: if true, `delegate worktree list` opportunistically prunes clean, fully merged worktrees older than `mergedOlderThanDays`.
- `autoPrune.mergedOlderThanDays`: non-negative integer.

See [Worktrees](worktrees.md) for lifecycle details.
