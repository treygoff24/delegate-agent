# Configuration

Delegate loads JSON config from embedded defaults plus optional user and explicitly selected files.

## Config locations and precedence

From lowest to highest precedence:

1. Embedded defaults in the package.
2. User config: `~/.delegate/config.json`.
3. `DELEGATE_CONFIG=/path/to/config.json`, when set.
4. Internal CLI overrides used by some commands.

If `DELEGATE_CONFIG` is set, the file must exist. Delegate fails closed instead of silently falling back to another config.

Repository-local `.delegate/config.json` is never merged automatically. A cloned
repository must not be able to select provider binaries, environment variables,
profiles, or execution policy. To trust a file deliberately, select it with
`DELEGATE_CONFIG=/path/to/config.json`; `describe` still reports an existing
workspace file as an unapplied layer.

Create an editable user config from an installed Delegate:

```bash
delegate config init
$EDITOR ~/.delegate/config.json
```

`config init` also writes missing `config.work.json` and
`config.personal.json` profile overlays next to the base config. Existing
installs can run `env -u AI_PROFILE delegate config sync-profiles` to create
missing overlays without overwriting ones already present.

For automatic first-run configuration, use `delegate setup`. If no config
exists, setup writes only safe absolute harness selectors. If one exists, setup
validates it and leaves its bytes unchanged. Model catalogs, native defaults,
and observed reasoning menus belong in the discovery cache rather than being
copied into config.

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

## Discovery cache and precedence

Discovery state is separate from config. The implicit auth profile uses
`~/.delegate/cache/discovery/default.json`; every named profile gets a separate
hashed file under `~/.delegate/cache/discovery/`. These files are owner-only,
atomically replaced, and never selected by repository config.

The private cache format is schema `1`. Its root contains only `schema`,
`profile`, `capturedAt`, and `harnesses`; each normalized harness record is
limited to installation/fingerprint data, model scope/default/catalog data,
reasoning evidence, and normalized warnings. Raw provider objects, profile env,
and raw probe output are not cache fields. Do not edit the cache by hand. A
malformed snapshot is treated as absent and can be replaced by the next refresh.

`delegate setup` and `delegate capabilities refresh` probe all supported
harnesses and write the selected profile's cache. Successful harness records
replace their previous records independently. A failed probe retains its
last-known-good record and appears in `staleHarnesses` in the refresh response.
Changing a configured binary selector also marks that harness stale; runtime
resolution ignores the mismatched record without probing during the launch.

`delegate models <engine> --live` is deliberately non-persistent. It shows a
fresh per-engine projection but writes neither config nor cache. Plain `models`,
plain `capabilities`, dry-run, and ordinary launches only read cached state.

Runtime model selection remains explicit: a CLI/JSON model or configured
alias/default wins. Model catalog display precedence is config, profile
discovery (or the one-off live result), the legacy workspace reasoning cache,
then bundled advisory data. Exact reasoning declarations use config, profile
discovery, the legacy workspace cache, then bundled fallback. Harness-wide
compatibility applies only where that harness documents it and exact evidence
is absent.

## Example

`delegate config init` writes a starter config like this. Replace placeholders before real Droid runs:

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
    "defaultReasoningEffort": null,
    "profile": null,
    "fallbackProfile": null
  },
  "profiles": {
    "detectFrom": ["DELEGATE_PROFILE", "AI_PROFILE"],
    "default": null,
    "definitions": {
      "work": {
        "env": {
          "CODEX_HOME": "~/replace-with-work-codex-home"
        }
      }
    }
  },
  "claude": {
    "binary": "claude",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "workPermissionMode": "auto",
    "noSessionPersistence": true,
    "bare": false
  },
  "grok": {
    "binary": "grok",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "workPermissionMode": "auto",
    "safePermissionMode": "dontAsk",
    "safeSandbox": "read-only",
    "workSandbox": null,
    "disableWebSearch": true,
    "noSubagents": false
  },
  "devin": {
    "binary": "devin",
    "defaultModel": "swe-1.7",
    "defaultReasoningEffort": null
  },
  "opencode": {
    "binary": "opencode",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "defaultAgent": null,
    "models": {}
  },
  "pi": {
    "binary": "pi",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {}
  },
  "omp": {
    "binary": "omp",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {}
  },
  "kimi": {
    "binary": "kimi",
    "defaultModel": "kimi-code/k3",
    "defaultReasoningEffort": null
  },
  "workflows": {
    "engineCaps": {},
    "itemThreads": 64,
    "structuredOutputRetries": 2
  }
}
```

`reviewer` and `implementer` are local aliases. They are intentionally provider-neutral. Put the real provider/model IDs in your private config, not in public docs or shared examples.

Reasoning-effort settings are optional. A per-run `--reasoning-effort LEVEL` or JSON `reasoningEffort` overrides provider defaults. If no effort is requested or defaulted, Delegate emits no reasoning-effort argv and preserves current runtime behavior.

Codex Fast is intentionally not a Delegate config default. Use per-run
`--fast`, `--no-fast`, or JSON `fast`; omission inherits the active Codex CLI
configuration. This keeps speed selection independent from model aliases and
reasoning defaults.

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
    "models": {
      "fast": "replace-with-cursor-model-id"
    },
    "reasoningEffortModels": {
      "high": "replace-with-thinking-cursor-model"
    }
  }
}
```

- `argvPrefix`: command prefix for Cursor Agent. Use an array so wrappers are possible.
- `defaultModel`: non-empty model name passed to Cursor.
- `models`: optional map of local aliases to Cursor model IDs. Used by `--model` and JSON `model`. Alias keys must not collide with mode names (`safe`/`work`/`call`), equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: optional non-empty effort string. It needs either a matching `reasoningEffortModels` entry or an exact discovered route for the selected model family. When neither can satisfy a configured default, the run proceeds without reasoning effort and records a warning (an explicit `--reasoning-effort` flag still fails closed).
- `reasoningEffortModels`: map from effort strings to Cursor model names. Cursor currently has no standalone reasoning-effort flag, so Delegate implements Cursor effort by selecting a model. Without an explicit model pin, this map outranks discovered routes. An explicit `--model` blocks the global map; Delegate may still select a different exact same-family selector when discovery corroborates that effort route, and reports the replacement as a warning.
- `cursor.binary` is not supported; use `argvPrefix`.

### `droid`

```json
{
  "droid": {
    "binary": "droid",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {
      "reviewer": "replace-with-read-only-model-id",
      "implementer": "replace-with-edit-capable-model-id"
    }
  }
}
```

- `binary`: child executable for Droid.
- `defaultModel`: optional default model ID used when neither a positional alias nor `--model` is given.
- `models`: map of local aliases to real Droid model IDs. May be empty if you do not use Droid; running a Droid positional alias that is not present fails with `invalid_alias`. Alias keys must not collide with mode names (`safe`/`work`/`call`), equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: optional non-empty effort string validated against the resolved Droid model before launch. When the model has no matching capability declaration, the run proceeds without reasoning effort and records a warning (an explicit `--reasoning-effort` flag still fails closed).
- Placeholder IDs that start with `replace-with-` are rejected for real runs.

### `codex`

```json
{
  "codex": {
    "binary": "codex",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {
      "fast": "replace-with-codex-model-id"
    },
    "profile": null,
    "fallbackProfile": null,
    "workSandbox": "workspace-write",
    "ephemeral": true,
    "ignoreUserConfig": false
  }
}
```

- `defaultModel`: optional model string. `null` lets Codex choose its own default.
- `models`: optional map of local aliases to Codex model IDs for `--model` / JSON `model`. Alias keys must not collide with mode names, equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: optional non-empty effort string. When a Codex model resolves (run input or `codex.defaultModel`) and supports the level, Delegate emits a Codex config override; otherwise the run proceeds without reasoning effort and records a warning. An explicit `--reasoning-effort` flag fails closed for unsupported levels, but can target the Codex harness default model when no model is configured.
- `profile`: optional Codex CLI config overlay name. It is config-only; JSON run input cannot set it.
- `fallbackProfile`: optional top-level `profiles.definitions` name for one quota-limit retry on tracked Codex runs. The fallback profile must define `env.CODEX_HOME`.
- `workSandbox`: `read-only`, `workspace-write`, or `danger-full-access` for Codex work mode when full bypass is not enabled.
- `ephemeral`: include Codex `--ephemeral` in JSON-streaming runs.
- `ignoreUserConfig`: include Codex `--ignore-user-config`.
- Codex safe mode always uses `--sandbox read-only` in v1; `codex.safeSandbox` is rejected.
- `codex.profile` is a Codex CLI config overlay. The top-level `profiles`
  block below is Delegate-injected auth/env and is a separate concept.

### `profiles`

```json
{
  "profiles": {
    "detectFrom": ["DELEGATE_PROFILE", "AI_PROFILE"],
    "default": null,
    "definitions": {
      "work": {
        "env": {
          "CODEX_HOME": "~/replace-with-work-codex-home",
          "SOME_TOOL_HOME": "~/.config/some-tool/work"
        }
      }
    }
  }
}
```

- `detectFrom`: ordered environment variable names checked for an active profile
  name. The first non-empty defined profile wins.
- `default`: optional profile name used when no detection variable names a
  defined profile.
- `definitions`: map of profile names to `env` maps. The resolved `env` map is
  expanded for `~` and `$VARS`, then injected into every child process
  regardless of engine. Harness-irrelevant pointers are inert.
- Profile `env` is for non-secret routing pointers. Secret-looking keys are
  rejected with `secret_in_profile_env`; export real API keys in the parent
  shell or a harness-native credential store instead. Enforcement is by key
  name only — do not embed credentials in innocuously named values (for example
  a database URL with an embedded password), and do not interpolate a secret via
  `$VAR` (for example `"PROVIDER_REF": "$OPENAI_API_KEY"`): expansion resolves
  the live secret into the value, and an opaque secret would print verbatim in
  `delegate profiles` and dry-run output. Keep secrets in shell env or
  harness-native key files.
- `delegate config sync-profiles` creates missing `config.<profile>.json`
  overlays for the built-in `work` and `personal` profile names. Each overlay
  pins `profiles.default` and carries that profile's `CODEX_HOME` pointer; it
  does not contain secrets.
- `--auth-profile NAME` overrides ambient detection for launches, `dry-run`,
  `run --input-json`, `delegate profiles`, `models`, `capabilities`, and
  `setup`. Unknown names fail closed with `unknown_profile`.
- `delegate profiles` reports the detected profile, source, and resolved env
  keys. JSON output includes redacted values and never emits unredacted
  secret-keyed values.
- Codex active profiles must define `CODEX_HOME`; non-Codex engines simply
  receive the same flat env map.

### `claude`

```json
{
  "claude": {
    "binary": "claude",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {},
    "workPermissionMode": "auto",
    "noSessionPersistence": true,
    "bare": false
  }
}
```

- `binary`: child executable for Claude Code.
- `defaultModel`: optional Claude model string. `null` lets Claude Code choose its own default.
- `models`: optional map of local aliases to Claude model IDs for `--model` / JSON `model`. Alias keys must not collide with mode names, equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: optional Claude Code effort string. Delegate validates it against the selected profile's discovered harness enum when available (with bundled native labels as compatibility fallback) and emits it as `--effort`. This allows a newly advertised Claude effort label without requiring a Delegate release.
- `workPermissionMode`: Claude Code permission mode for work runs. Allowed values are `acceptEdits`, `auto`, `default`, `dontAsk`, and `plan`.
- `workPermissionMode` cannot be `bypassPermissions`; use `policy.harness.claude.work.bypassApprovalsAndSandbox` when you explicitly want Delegate to emit Claude `--permission-mode bypassPermissions`.
- `noSessionPersistence`: defaults to `true`, adding `--no-session-persistence` to headless calls.
- `bare`: opt-in `--bare` mode for runs that should skip Claude Code customizations and auto-discovery. Defaults to `false`, which is consistent with how the other harnesses use their own installed configuration. Be aware of the footprint: with `bare: false`, a delegated run loads the operator's full Claude Code environment — hooks, skills, plugins, output styles, and auto-memory. `--strict-mcp-config` suppresses MCP servers, but nothing else, so each run carries that ambient system-prompt context (extra latency and token cost) and is not hermetic. Set `bare: true` for cost-sensitive or reproducible runs that should ignore local customizations.
- Claude safe mode uses `claude -p`, stdin prompt delivery, `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob, and selected read-only Bash tools.

### `grok`

```json
{
  "grok": {
    "binary": "grok",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {},
    "workPermissionMode": "auto",
    "safePermissionMode": "dontAsk",
    "safeSandbox": "read-only",
    "workSandbox": null,
    "disableWebSearch": true,
    "noSubagents": false
  }
}
```

- `binary`: path to the Grok Build CLI executable. Delegate also searches `~/.grok/bin`.
- `defaultModel`: optional Grok model string. `null` lets Grok choose its own default.
- `models`: optional map of local aliases to Grok model IDs for `--model` / JSON `model`. Alias keys must not collide with mode names, equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: optional Grok effort string. Delegate validates exact model declarations before using the harness-wide compatibility enum and emits accepted values as `--effort`. A manual `reasoning.capabilities.grok` declaration can teach Delegate a newly released exact model/effort pair.
- `workPermissionMode`: Grok permission mode for work runs. Allowed values include `acceptEdits`, `auto`, `default`, and `dontAsk`.
- `workPermissionMode` cannot be `bypassPermissions`; use `policy.harness.grok.work.bypassApprovalsAndSandbox` when you explicitly want Delegate to emit Grok `--permission-mode bypassPermissions`.
- `safePermissionMode`: Grok permission mode for safe runs. Allowed values are `dontAsk`, `default`, and `auto`. Defaults to `dontAsk`.
- `safeSandbox`: Grok sandbox profile for safe runs. Defaults to `read-only`.
- `workSandbox`: optional Grok sandbox profile for work runs: `workspace`, `devbox`, `read-only`, `strict`, or `null` (omit `--sandbox` when `null`).
- `disableWebSearch`: defaults to `true`. Delegate adds `--disable-web-search` only when this is `true` and effective `policy.webSearch` is not `true`. Set `policy.work.webSearch` to `true` or `grok.disableWebSearch` to `false` to allow web search.
- `noSubagents`: defaults to `false`. When `true`, Delegate adds `--no-subagents` to Grok argv.
- Grok safe mode uses prompt-file transport, `--sandbox read-only`, and `--permission-mode dontAsk` by default, plus Delegate's isolated throwaway workspace.

### `kimi`

```json
{
  "kimi": {
    "binary": "kimi",
    "defaultModel": "kimi-code/k3",
    "defaultReasoningEffort": null,
    "models": {}
  }
}
```

- `binary`: path to the `kimi` executable.
- `defaultModel`: optional Kimi model alias. The editable example pins one; the embedded default is `null` so Kimi can use its own configured default.
- `models`: optional map of local aliases to Kimi model IDs for `--model` / JSON `model`. Alias keys must not collide with mode names, equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: not supported in v1; must be `null`.
- Kimi's thinking/effort level is configured in `~/.kimi-code/config.toml`, not through Delegate.
- Kimi safe mode uses Delegate's read-only safety prompt and isolated workspace. Kimi prompt mode auto-approves tool actions, so the isolated workspace is the effective write boundary.
- Kimi work mode uses prompt mode. Delegate does not emit `--yolo` because Kimi rejects combining `--yolo` with `--prompt`.

### `devin`

```json
{
  "devin": {
    "binary": "devin",
    "defaultModel": "swe-1.7",
    "defaultReasoningEffort": null,
    "models": {}
  }
}
```

- `binary`: path to the Devin CLI executable.
- `defaultModel`: optional Devin model ID. The editable example pins one; the embedded default is `null` so Devin can use its own configured default.
- `models`: optional map of local aliases to Devin model IDs for `--model` / JSON `model`. Alias keys must not collide with mode names, equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: not supported in v1; must be `null`.
- Discover live Devin model IDs with `delegate models devin --live`.
- Devin safe mode is rejected during preflight because filesystem surveys may require generic `exec`, which Delegate cannot permit without weakening the read-only boundary. Use another safe Harness for filesystem review.

### `opencode`

```json
{
  "opencode": {
    "binary": "opencode",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "defaultAgent": null,
    "models": {
      "fast": "replace-with-provider/model-id",
      "reviewer": {
        "model": "replace-with-provider/model-id",
        "variant": "high"
      }
    }
  }
}
```

- `binary`: path to the OpenCode executable. The curl installer normally writes
  it under `~/.opencode/bin`, so an absolute path is useful when that directory
  is not on `PATH`.
- `defaultModel`: optional OpenCode `provider/model` ID. `null` lets OpenCode use
  its configured default.
- `defaultReasoningEffort`: optional OpenCode variant. Delegate emits it as
  `--variant` and validates it when exact discovered variants exist for the
  selected model. Without exact evidence, Delegate preserves pass-through
  compatibility and reports `opencode_variant_unvalidated`; OpenCode may then
  silently ignore a bogus variant.
- `defaultAgent`: optional OpenCode agent name used when a run does not pass
  `--agent`.
- `models`: optional map of local aliases. A value may be a model string or an
  object with `model` and `variant`, which pins that variant to the alias.
- Delegate rejects OpenCode model, variant, agent, and alias values that start
  with `-` in config and per-run input.

`delegate models opencode --live` runs `opencode --pure models --verbose`. Live
discovery returns more than 450 `provider/model` IDs and includes any models.dev
provider, including configured custom or local providers.

### `pi`

```json
{
  "pi": {
    "binary": "pi",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {
      "reviewer": "replace-with-provider/model-id",
      "quick": {
        "model": "replace-with-provider/model-id",
        "thinking": "minimal"
      }
    }
  }
}
```

- `binary`: path to the Pi executable.
- `defaultModel`: optional Pi `provider/model` ID. `null` preserves Pi's configured default.
- `defaultReasoningEffort`: optional `low`, `medium`, `high`, `xhigh`, or `max` default.
- `models`: optional alias map. Values may be model strings or objects with `model` and `thinking`; structured aliases may also pin `off` or `minimal` thinking.
- Explicit `--reasoning-effort` overrides alias-pinned thinking, which overrides the configured default.
- Every mode uses `--no-session`. Safe mode and `call --read-only` allow only Pi's `read` tool and disable extensions, skills, prompt templates, and project approval discovery.
- `delegate models pi --live` probes Pi's local model catalog without reading or printing provider credentials.

### `omp`

```json
{
  "omp": {
    "binary": "omp",
    "defaultModel": null,
    "defaultReasoningEffort": null,
    "models": {
      "reviewer": "replace-with-provider/model-id",
      "quick": { "model": "replace-with-provider/model-id", "thinking": "minimal" }
    }
  }
}
```

- `binary`: path to the Oh My Pi executable.
- `defaultModel`: optional `provider/model` ID. `null` preserves Oh My Pi's configured default.
- `defaultReasoningEffort`: optional `low`, `medium`, `high`, `xhigh`, or `max` default.
- `models`: the same string or `{ "model", "thinking" }` alias shape as `pi.models`; model values containing a colon suffix are rejected.
- Explicit `--reasoning-effort` overrides alias-pinned thinking, which overrides the configured default.
- Every mode uses `--no-session`. Safe mode and `call --read-only` allow only `read`, disable extensions, skills, rules, and LSP discovery, and add `--approval-mode always-ask` as the load-bearing write/exec denial in headless mode.
- Delegate does not consume `modelRoles` and never emits `--smol`, `--slow`, `--plan`, `--prewalk*`, or `--plan-yolo*`.
- `delegate models omp --live` probes `omp models --json --no-extensions` without reading or printing provider credentials.

### `reasoning`

```json
{
  "reasoning": {
    "capabilities": {
      "codex": {
        "provider/custom-model": {
          "supported": ["low", "medium", "high", "xhigh", "max"],
          "default": "medium"
        }
      },
      "droid": {
        "provider/custom-model": {
          "supported": ["high", "xhigh"],
          "default": "high"
        }
      },
      "grok": {
        "provider/custom-model": {
          "supported": ["low", "medium", "high"],
          "default": "high"
        }
      }
    }
  }
}
```

- `capabilities`: optional map of harness name (`codex`, `droid`, or `grok`; Cursor uses `cursor.reasoningEffortModels`, and Claude uses native harness-wide `--effort` labels) to exact per-model capability declarations.
- `supported`: non-empty array of exact effort strings. Delegate treats these literally; it does not translate `xhigh` to another provider spelling.
- `default`: optional effort string that must be present in `supported`. It is informational only (shown by `delegate capabilities`); launches apply `<engine>.defaultReasoningEffort`, not per-model defaults.
- Effort strings may not start with `-` or contain whitespace, double quotes, or backslashes.
- Codex `max` support is model-scoped and bundled only for `gpt-5.6-sol` as of 2026-07. Other Codex models fail closed unless an exact config, profile-discovery, or legacy workspace-cache declaration includes `max`.

Use config for private models or a deliberate override. A malformed profile
cache is treated as absent and can be replaced by the next setup or refresh.
The older `.delegate/capabilities/reasoning.json` file is still read at lower
precedence for compatibility, but refresh no longer writes it. Use
`delegate --json capabilities` to inspect the merged view and
`delegate --json capabilities refresh` to refresh the selected profile.

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

Supported boolean policy keys: `networkAccess`, `webSearch`, `bypassApprovalsAndSandbox`, and `bypassHookTrust`. Only Codex currently consumes all of these fields. Claude and Grok consume `bypassApprovalsAndSandbox` only from harness-scoped `policy.harness.<engine>.work` blocks, mapping it to `--permission-mode bypassPermissions`. Grok emits `--disable-web-search` when effective `policy.webSearch` is not `true` and `grok.disableWebSearch` is `true` (the default). Cursor, Droid, and Kimi ignore unsupported policy fields rather than translating them to runtime flags.

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

Allowed values are `auto`, `none`, and `worktree`. For Cursor, Claude, Grok,
OpenCode, Pi, Oh My Pi, Droid, and Kimi safe mode, an effective value of `none` is normalized to `auto`
because those safe contracts depend on Delegate's temporary workspace/config
boundary. Explicit per-run CLI/JSON `none` requests also emit a warning; a
config default is normalized without a separate per-run warning. Codex safe can
use `none` because the Codex read-only sandbox remains active.

Embedded defaults:

- `safe`: `auto`. Cursor, Claude, Grok, OpenCode, Pi, Oh My Pi, Droid, Codex, and Kimi safe use temporary workspace isolation by default. Devin safe is unsupported.
- `work`: `none`. Work mode runs in the real workspace unless you opt into worktree isolation.

### `worktrees`

```json
{
  "worktrees": {
    "dataHome": null,
    "poolWarnCount": 20,
    "autoPrune": {
      "enabled": false,
      "mergedOlderThanDays": 7
    }
  }
}
```

- `dataHome`: persistent-worktree root. `null` means `~/.delegate/worktrees`.
- `poolWarnCount`: non-negative worktree-count threshold for a launch-time warning when the shared persistent-worktree pool holds more worktrees. The default is 20. The warning does not block or delete anything.
- `autoPrune.enabled`: if true, `delegate worktree list` opportunistically prunes clean, fully merged worktrees older than `mergedOlderThanDays`.
- `autoPrune.mergedOlderThanDays`: non-negative integer.

See [Worktrees](worktrees.md) for lifecycle details.

### `progress`

```json
{
  "progress": {
    "enabled": false,
    "initialDelaySec": 30,
    "intervalSec": 60
  }
}
```

Controls parent progress heartbeats for tracked foreground runs. Heartbeats are written to stderr so `--json` stdout stays machine-readable.

- `enabled`: must be a boolean. When `true`, tracked foreground runs emit heartbeats unless a launch passes `--no-progress`. When `false` (the default), runs are silent unless a launch passes `--progress`. The per-launch flag always wins over config.
- `initialDelaySec`: delay before the first heartbeat. Must be a positive, finite number. Default `30`.
- `intervalSec`: spacing between subsequent heartbeats. Must be a positive, finite number. Default `60`.

Timing resolves as environment override, then config, then embedded default. Non-positive, non-finite, or non-numeric `initialDelaySec`/`intervalSec`, and a non-boolean `enabled`, are rejected at config load. See [CLI reference](cli-reference.md) for the `--progress` / `--no-progress` launch flags.

### `workflows`

```json
{
  "workflows": {
    "engineCaps": {"codex": 4, "claude": 2},
    "itemThreads": 64,
    "structuredOutputRetries": 2
  }
}
```

Controls the local workflow supervisor. See
[Delegate Workflows](delegate-workflows.md) for DSL details and limits.

- `engineCaps`: optional per-engine concurrent child-run caps. Keys are
  Delegate engine names; values must be positive integers. Engines without an
  entry are not capped by this setting.
- `itemThreads`: maximum concurrent item worker threads for `pipeline()` and
  `parallel()`. Positive integers override the embedded default of `64`; `0`
  or a missing key falls back to the default.
- `structuredOutputRetries`: non-negative retry count for `agent(schema=...)`
  validation failures. The embedded default is `2`.

Workflow hard caps are not configurable in v1: scripts are limited to 512 KiB,
nested `workflow()` calls to depth 3, lifetime `agent()` calls to 1000, and
`pipeline()`/`parallel()` inputs to 4096 items.
