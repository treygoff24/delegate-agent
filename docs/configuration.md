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
delegate --json models
```

## Example

Copy `config.example.json` and replace placeholders before real Droid runs:

```json
{
  "cursor": {
    "argvPrefix": ["agent"],
    "defaultModel": "composer-2.5"
  },
  "droid": {
    "binary": "droid",
    "models": {
      "reviewer": "replace-with-read-only-model-id",
      "implementer": "replace-with-edit-capable-model-id"
    }
  },
  "codex": {
    "binary": "codex",
    "defaultModel": null
  }
}
```

`reviewer` and `implementer` are local aliases. They are intentionally provider-neutral. Put the real provider/model IDs in your private config, not in public docs or shared examples.

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
    "defaultModel": "composer-2.5"
  }
}
```

- `argvPrefix`: command prefix for Cursor Agent. Use an array so wrappers are possible.
- `defaultModel`: non-empty model name passed to Cursor.
- `cursor.binary` is not supported; use `argvPrefix`.

### `droid`

```json
{
  "droid": {
    "binary": "droid",
    "models": {
      "reviewer": "replace-with-read-only-model-id",
      "implementer": "replace-with-edit-capable-model-id"
    }
  }
}
```

- `binary`: child executable for Droid.
- `models`: map of local aliases to real Droid model IDs. May be empty if you do not use Droid; running a Droid alias that is not present fails with `invalid_alias`.
- Placeholder IDs that start with `replace-with-` are rejected for real runs.

### `codex`

```json
{
  "codex": {
    "binary": "codex",
    "defaultModel": null,
    "profile": null,
    "workSandbox": "workspace-write",
    "ephemeral": true,
    "ignoreUserConfig": false
  }
}
```

- `defaultModel`: optional model string. `null` lets Codex choose its own default.
- `profile`: optional Codex profile name. It is config-only; JSON run input cannot set it.
- `workSandbox`: `read-only`, `workspace-write`, or `danger-full-access` for Codex work mode when full bypass is not enabled.
- `ephemeral`: include Codex `--ephemeral` in JSON-streaming runs.
- `ignoreUserConfig`: include Codex `--ignore-user-config`.
- Codex safe mode always uses `--sandbox read-only` in v1; `codex.safeSandbox` is rejected.

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

Supported boolean policy keys: `networkAccess`, `webSearch`, `bypassApprovalsAndSandbox`, and `bypassHookTrust`. Only Codex currently consumes all of these fields. Cursor and Droid ignore unsupported policy fields rather than translating them to runtime flags.

### `isolation`

```json
{
  "isolation": {
    "safe": "auto",
    "work": "none"
  }
}
```

Allowed values are `auto`, `none`, and `worktree`.

Embedded defaults:

- `safe`: `auto`. Cursor and Codex safe use temporary workspace isolation; Droid safe remains in the real workspace.
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
