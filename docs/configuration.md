# Configuration

Delegate loads JSON config from embedded defaults plus optional user and explicitly selected files.

## Config locations and precedence

From lowest to highest precedence:

1. Embedded defaults in the package.
2. User config: `~/.delegate/config.json`.
3. Its machine-local overlay: `~/.delegate/config.local.json`.
4. `DELEGATE_CONFIG=/path/to/config.json`, when set.
5. That file's machine-local overlay, if the file is in `~/.delegate`.

If `DELEGATE_CONFIG` is set, the file must exist. Delegate fails closed instead of silently falling back to another config.

### Machine-local overlays

Every config layer admits a `.local` sibling that merges immediately above it
and that no provisioning step writes:

| Layer | Its local overlay |
| --- | --- |
| `~/.delegate/config.json` | `~/.delegate/config.local.json` |
| `~/.delegate/config.work.json` | `~/.delegate/config.work.local.json` |
| a `DELEGATE_CONFIG` file in `~/.delegate` | that file with `.local` before `.json` |

On a managed fleet these config files are *copied* onto each machine by an
installer that has no way to learn about edits made there, so a model alias or
reasoning block tuned in place is reverted on the next apply — silently, since
nothing errors. Put those in the matching overlay instead.

Pair the overlay with the file it defends, not with the base config. Under
`AI_PROFILE`, Delegate runs on `config.work.json` or `config.personal.json`, so
that is the file provisioning replaces and `config.local.json` would sit below
it. A per-base sibling also keeps realms separate: a work overlay cannot reach
into personal. Overlays are optional — absent, nothing changes.

Overlays are honored **only for files in `~/.delegate`**. Pointing
`DELEGATE_CONFIG` at a file anywhere else merges exactly that file: the trust
you extend with `DELEGATE_CONFIG` is trust in the file you read, and a sibling
shipped alongside it was never reviewed. Since config selects provider binaries
and argv prefixes, honoring such a sibling would let a cloned repository choose
what Delegate executes.

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

## Mail

Workspace mail is enabled by default, including when the section or key is
omitted. A strict object with one key controls launch-time mail setup:

```json
{
  "mail": {
    "enabled": true
  }
}
```

`mail.enabled` must be a boolean; unknown keys are rejected. Wrapped work
launches receive a pull-mail instruction suffix, and isolated work launches
receive the harness-specific mailbox grant where supported. Set `enabled` to
`false`, or pass global `--no-mail`, to disable that setup for a launch. The flag
does not change saved configuration or suppress explicit `--notify`.

Mail storage is local to `.delegate/mail`; it needs no daemon, network, or
`post` installation. If mail storage cannot be prepared, the launch continues
with mail setup disabled, one stderr warning, and a recorded `warnings` entry.
The run registry itself must still be writable. An isolated sandbox that
cannot reach mail records a deduplicated manifest warning without stderr noise.
Explicit mail commands remain available even when launch-time mail is disabled.

`--mail-push` and input JSON `mailPush: true` require mail to be enabled and a
wrapped work-mode launch. Push remains opt-in; default mail never installs
hooks. Claude receives launch-scoped settings and both adapters keep cursors
and markers under `.delegate/mail`; Codex private homes are created under
`.delegate/runs/<runId>/` and cleaned at terminal finalization or terminal
launch failure. Codex turns its stop hooks on for the launch with
`--enable hooks` (the same as `-c features.hooks=true`) and
`--dangerously-bypass-hook-trust`; a bare `-c hooks=true` is rejected by Codex,
where `hooks` is a table. All other harnesses remain pull-only until their
stop-hook output is verified and degrade with a recorded warning rather than
guessing.

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
    "resumable": true,
    "disableBackgroundTasks": true,
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

### `personas`

Persona files are workspace-local by design, not VCS-shared configuration:
`.delegate/personas/*.md` shadows the same name under
`~/.delegate/personas/`. The `delegate personas` command lists both scopes
without exposing persona bodies in JSON previews. Because `.delegate` is
normally excluded, teams that intentionally version a persona must force-add
the individual file; the resolution scope remains workspace-local.

```json
{
  "personas": {
    "forceTransport": null
  }
}
```

`forceTransport` is optional and accepts `null`, `prepend`, `native-file`, or
`agent-config`. It is an explicit transport pin, not a capability probe;
`native-file` is appropriate only when the configured Claude binary supports
`--append-system-prompt-file`, and `agent-config` is meaningful for OpenCode.
Safe mode never uses native persona channels. A workspace-local persona is
refused in safe mode unless the launch includes `--allow-repo-persona`.

### `tracking`

Controls local run recording.

```json
{
  "tracking": {
    "completionReport": {"defaultMode": "markdown"},
    "retention": {"enabled": true, "rawLogDays": 7, "scratchDays": 3},
    "registryLockTimeoutSec": 120,
    "processGroupTerminationGraceSec": 3,
    "skillReviewPreamble": {"enabled": false}
  }
}
```

- `completionReport.defaultMode`: `markdown` or `none`.
- `retention.enabled`: whether raw logs are eligible for archive-only retention.
- `retention.rawLogDays`: non-negative number of days before bulky raw logs may be archived.
- `retention.scratchDays`: non-negative number of days after which a terminal
  run's neutral scratch directory, scratch sidecars, and compact temp directory
  are removed by the ambient retention pass (default `3`; `0` reclaims as soon as
  a run is terminal). Run records are kept, running and stale runs are never
  touched, and the run state records `scratchReclaimedAt` and
  `scratchReclaimedBytes`. `delegate runs reclaim [--older-than DAYS] [--dry-run]`
  does the same on demand. The ambient step stops after 20 seconds, including
  inside one large tree, and resumes ten minutes later; `runs reclaim` has no
  such limit. The ambient step only runs while `retention.enabled`
  is true, and it is independent of `delegate runs prune`, which removes the
  records themselves.
- `processGroupTerminationGraceSec`: non-negative, finite number of seconds to
  wait after SIGTERM before escalating a child process group to SIGKILL. The
  default is `3` seconds; `0` escalates immediately.
- `registryLockTimeoutSec`: non-negative, finite number of seconds a launch or
  finalization waits for the workspace Registry lock. The default is `120`.
  `DELEGATE_REGISTRY_LOCK_TIMEOUT_SECONDS` overrides it for one process. A
  completed finalization that exhausts this budget publishes an atomic
  per-run `finalize-wal.json`; the next successful Registry lock holder replays
  it before its own mutation. Until replay, readers see the prior canonical
  state. Malformed WAL records are quarantined, and a cancellation marker wins
  over a WAL success.
- `skillReviewPreamble.enabled`: whether Delegate prepends the skill-review
  requirement (`SKILL_REVIEW_PREFIX`) to a wrapped child prompt. The default is
  `false`. When `true`, every wrapped safe- and work-mode prompt is prefixed
  with it before any persona, safe-mode, or worktree framing is added.
  `--pass-through` launches and `call`-mode prompts never receive the
  preamble, regardless of this setting. The requirement is explicitly bounded:
  the child reads the served skill index once, reads only the skill files it
  judged relevant, and does not run discovery CLIs (`doctor`, `--help` probing,
  directory listings) to enumerate skills, so the mandatory review cannot turn
  into an open-ended discovery loop before the task starts.

Ambient retention is best-effort. Archive I/O is serialized separately from
Registry mutations, so a slow archive cannot block run progress, inspection,
or cancellation; if another retention pass is already active, a concurrent
ambient pass returns immediately. The scratch reclamation step of a pass has a
20-second budget; a pass that runs out of time leaves the rest, and implicit
scratch reclamation then waits ten minutes before trying again (raw-log
archival keeps its once-a-minute cadence).

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
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
- `reasoningEffortModels`: map from effort strings to Cursor model names. Cursor currently has no standalone reasoning-effort flag, so Delegate implements Cursor effort by selecting a model. Without an explicit model pin, this map outranks discovered routes. An explicit `--model` blocks the global map; Delegate may still select a different exact same-family selector when discovery corroborates that effort route, and reports the replacement as a warning. If no exact same-family route exists, an explicit effort fails closed while a configured default is ignored with a warning.
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
- `defaultModel`: optional default model ID used when `--model` is omitted.
- `models`: local aliases resolved by `--model`, as with other engines. The map may be empty. Alias keys must not be mode names, the engine name, or start with `-`.
- `defaultReasoningEffort`: optional non-empty effort string validated against the resolved Droid model before launch. When the model has no matching capability declaration, the run proceeds without reasoning effort and records a warning (an explicit `--reasoning-effort` flag still fails closed).
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
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
    "resumable": true,
    "ignoreUserConfig": false
  }
}
```

- `defaultModel`: optional model string. `null` lets Codex choose its own default.
- `models`: optional map of local aliases to Codex model IDs for `--model` / JSON `model`. Alias keys must not collide with mode names, equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: optional non-empty effort string. When a Codex model resolves (run input or `codex.defaultModel`) and supports the level, Delegate emits a Codex config override; otherwise the run proceeds without reasoning effort and records a warning. An explicit `--reasoning-effort` flag fails closed for unsupported levels, but can target the Codex harness default model when no model is configured.
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
- `profile`: optional Codex CLI config overlay name. Codex reads it as a file:
  `--profile <name>` layers `$CODEX_HOME/<name>.config.toml` on top of the base
  user config. It is not a `[profiles.<name>]` table inside `config.toml`, which
  current Codex no longer consults. A name whose file does not exist is accepted
  silently and resolves no overlay, so `delegate doctor` warns when the file is
  missing. It is config-only; JSON run input cannot set it.
- `fallbackProfile`: optional top-level `profiles.definitions` name for Codex-only quota fallback. The profile must define `env.CODEX_HOME`; a known-blocked credential namespace is not launched.
- `workSandbox`: `read-only`, `workspace-write`, or `danger-full-access` for Codex work mode when full bypass is not enabled.
- `resumable`: defaults to `true`. A Codex **work** Run saves its native session, so `delegate followup <handle>` works without remembering `--resumable` at launch. `false` restores the old opt-in default: a work Run then saves its session only when launched with `--resumable` (or JSON `resumable: true`). A launch can always decide for itself: `--resumable` or `--no-resumable` (JSON `resumable: true` or `false`) beats this key. Safe and call Runs, `--pass-through` Runs, and other engines never default to resumable. See [Native session files](#native-session-files-and-the-resumable-default) for the storage consequence.
- `ephemeral`: include Codex `--ephemeral` in JSON-streaming runs that are **not** resumable, so it now applies to safe and call Runs, `--no-resumable` Runs, and every Run while `resumable` is `false`. It does not switch resumability off: a work Run stays resumable under `ephemeral: true` unless it opts out with `--no-resumable` or `resumable: false`.
- `ignoreUserConfig`: include Codex `--ignore-user-config`.

Temporary usage-limit blocks live in per-user runtime state and key on a hash
of `CODEX_HOME/auth.json` plus `codex.profile`. Default work/personal homes also
mirror compatible legacy alias keys; remapped aliases remain isolated. When
both namespaces are blocked, Delegate keeps the primary attempt and skips the
fallback.

- Codex safe mode always uses `--sandbox read-only` in v1; `codex.safeSandbox` is rejected.
- `codex.profile` names a Codex CLI config overlay file. The top-level `profiles`
  block below is Delegate-injected auth/env and is a separate concept; it is what
  supplies the `CODEX_HOME` the overlay file is looked up under.

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

#### Profile and config precedence

One order applies to every choice: an explicit flag beats the environment, and
the environment beats a config default.

| Layer | Config file (profile shim) | Active profile (Python) |
| --- | --- | --- |
| 1. Flag: `--auth-profile NAME` | `config.NAME.json` when that overlay exists; `AI_PROFILE` and the realm keys follow it for the child | `NAME` |
| 2. Environment | `DELEGATE_CONFIG`, else `config.$AI_PROFILE.json` | first defined `profiles.detectFrom` variable |
| 3. Config default | `~/.delegate/config.json` | `profiles.default` of the loaded config |

A higher layer never wins silently over a lower layer that names something
different. Delegate warns when:

- `--auth-profile NAME` replaces an ambient `DELEGATE_CONFIG` or a different
  `AI_PROFILE` (shim), or a different detected profile (Python);
- `--auth-profile NAME` names `work` or `personal` but `config.NAME.json` is
  missing, so only the definition changes while the environment's config and
  keys stay in effect;
- an inherited detection variable such as `DELEGATE_PROFILE` overrides the
  loaded config's `profiles.default`. Pass `--auth-profile` with the default's
  name to use it.
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
    "resumable": true,
    "disableBackgroundTasks": true,
    "bare": false
  }
}
```

- `binary`: child executable for Claude Code.
- `defaultModel`: optional Claude model string. `null` lets Claude Code choose its own default.
- `models`: optional map of local aliases to Claude model IDs for `--model` / JSON `model`. Alias keys must not collide with mode names, equal the engine's own name, or start with `-`.
- `defaultReasoningEffort`: optional Claude Code effort string. Delegate validates it against the selected profile's discovered harness enum when available (with bundled native labels as compatibility fallback) and emits it as `--effort`. This allows a newly advertised Claude effort label without requiring a Delegate release.
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
- `workPermissionMode`: Claude Code permission mode for work runs. Allowed values are `acceptEdits`, `auto`, `default`, `dontAsk`, and `plan`.
- `workPermissionMode` cannot be `bypassPermissions`; use `policy.harness.claude.work.bypassApprovalsAndSandbox` when you explicitly want Delegate to emit Claude `--permission-mode bypassPermissions`.
- `resumable`: defaults to `true`. A Claude **work** Run saves its native session, so `delegate followup <handle>` works without remembering `--resumable` at launch. `false` restores the old opt-in default. `--resumable` or `--no-resumable` (JSON `resumable`) on a launch beats this key. Safe and call Runs and `--pass-through` Runs never default to resumable. See [Native session files](#native-session-files-and-the-resumable-default).
- `noSessionPersistence`: defaults to `true`, adding `--no-session-persistence` to runs that are **not** resumable: safe and call Runs, `--no-resumable` Runs, and every Run while `resumable` is `false`. It does not switch resumability off; a work Run stays resumable under `noSessionPersistence: true` unless it opts out.
- `disableBackgroundTasks`: defaults to `true`. For tracked `work` runs (and followups of them) Delegate sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` in the child environment, which removes Bash `run_in_background` and the automatic backgrounding of long commands, and adds `--disallowedTools Monitor`. Because a long gate then runs in the foreground, it also sets `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` to the run's `--timeout` (never less than two hours) so Claude Code's own 2-minute default and 10-minute maximum for a foreground command do not cut it off; an auth profile's `env` cannot override these three variables (Delegate warns and keeps its own). A headless `claude -p` child ends when the model stops, and background tasks and Monitors die with the session, so a child that backgrounds its test gate and then "waits" abandons it. Set `false` to allow background tasks; a Run that still ends mid-job is marked [`degraded`](cli-reference.md#degraded-runs-the-child-ended-its-turn-mid-job) either way. Safe runs are unchanged: their tool allowlist already excludes Monitor.
- `bare`: opt-in `--bare` mode for runs that should skip Claude Code customizations and auto-discovery. Defaults to `false`, which is consistent with how the other harnesses use their own installed configuration. Be aware of the footprint: with `bare: false`, a delegated run loads the operator's full Claude Code environment — hooks, skills, plugins, output styles, and auto-memory. `--strict-mcp-config` suppresses MCP servers, but nothing else, so each run carries that ambient system-prompt context (extra latency and token cost) and is not hermetic. Set `bare: true` for cost-sensitive or reproducible runs that should ignore local customizations.
- Claude safe mode uses `claude -p`, stdin prompt delivery, `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob, and selected read-only Bash tools.

#### Native session files and the resumable default

Codex and Claude **work** Runs are resumable by default. A resumable Run leaves
the harness's own session on disk (Codex's rollout under `CODEX_HOME/sessions`,
Claude Code's transcript under its `projects` directory) and records the session
id in the Run's manifest, which is what `delegate followup` resumes. What that
means in practice:

- **Storage.** Native session files now persist for every Codex and Claude work
  Run, not only Runs launched with `--resumable`. They hold the prompt, the
  child's output, and its tool results in the harness's own store, outside
  `.delegate/` and outside Delegate's redaction and `runs prune`. Delegate does
  not prune them. See [the security model](security-model.md#native-session-files).
- **Worktrees.** A succeeded persistent-worktree Run that is resumable keeps its
  worktree (`worktreeRetained: "resumable_session"`) instead of retiring it when
  clean, so `followup` and `resume` can re-enter it. See
  [worktrees](worktrees.md#resumable-runs-keep-their-worktree).
- **Precedence.** The default beats `codex.ephemeral: true` and
  `claude.noSessionPersistence: true`. Those keys only apply to Runs that are not
  resumable. Opt out per launch with `--no-resumable` (JSON `resumable: false`),
  or per engine with `codex.resumable: false` / `claude.resumable: false`. A launch
  that says `--resumable` beats the engine key.
- **Unchanged.** Safe and call Runs, `--pass-through` Runs, and every other
  engine stay ephemeral, and `--no-resumable` is a quiet no-op there.
  Workflow `agent()` children stay non-resumable unless the call passes
  `resumable=True`.

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
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
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
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
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
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
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
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
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
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
- `models`: optional alias map. Values may be model strings or objects with `model` and `thinking`; structured aliases may also pin `off` or `minimal` thinking.
- Explicit `--reasoning-effort` overrides alias-pinned thinking, which overrides the configured default.
- Every mode uses `--no-session`. Safe mode and `call --read-only` allow only Pi's `read` tool and disable extensions, skills, prompt templates, and project approval discovery.
- `delegate models pi --live` probes Pi's local model catalog without reading or printing provider credentials.

### Disabling a harness (`<engine>.enabled`)

```json
{ "droid": { "enabled": false } }
```

- Every engine block (`cursor`, `droid`, `codex`, `kimi`, `claude`, `grok`, `devin`, `opencode`, `pi`, `omp`) accepts an optional boolean `enabled`, default `true`. A non-boolean value fails config load with `invalid_<engine>_config`.
- `enabled: false` removes the harness from `delegate models`, `describe`, and `capabilities` listings (JSON and text), skips it in `capabilities refresh`, and makes every launch of it (and `delegate models <name>`) fail fast with `harness_disabled` naming `<engine>.enabled`. Static help text and the `describe` overview still mention the harness. Use it to retire a harness whose binary is still on `PATH`.

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
- `trackedStreamMaxBytes`: retained-stream byte cap for tracked runs. Off by default (`null`): nothing is truncated, hidden from the parser, or killed for being verbose, and `stdout.log` keeps the whole stream. Set a positive integer to opt in; a Run that then exceeds it is stopped as `output_limit_exceeded` with its partial output kept in the completion report. Only an opted-in cap applies the OMP 256 MiB transport and 16 MiB per-record ceilings, which are not configurable.
- `models`: the same string or `{ "model", "thinking" }` alias shape as `pi.models`; model values containing a colon suffix are rejected. An alias object takes exactly `model` and `thinking`, both required (`"quick": { "model": "provider/model-id", "thinking": "minimal" }`); reasoning effort is spelled `thinking`, and any other key (for example `reasoningEffort`) fails config load with the allowed keys named.
- Explicit `--reasoning-effort` overrides alias-pinned thinking, which overrides the configured default.
- Every mode uses `--no-session`. Safe mode and `call --read-only` allow only `read`, disable extensions, skills, rules, and LSP discovery, and add `--approval-mode always-ask` as the load-bearing write/exec denial in headless mode.
- Delegate does not consume `modelRoles` and never emits `--smol`, `--slow`, `--plan`, `--prewalk*`, or `--plan-yolo*`.
- `delegate models omp --live` probes `omp models --json --no-extensions` without reading or printing provider credentials.
- A `provider/model-id` you type yourself (`--model`, input-JSON `model`, or a
  literal `agent(model=...)` in a workflow) is pinned by default: the run behaves
  as `--continuity-mode pinned`. A delegate alias (a key of `omp.models`) and
  `defaultModel` stay `fungible` even though their targets name a provider, so
  aliases keep multi-subscription failover; the run still records and warns
  about which provider answered, and `--continuity-mode pinned` on an alias pins
  it. Delegate launches a pinned OMP run with a private `--config` overlay that sets
  `retry.modelFallback` and `retry.usageAwareFallback` to `false`, so OMP's own
  retry chains (`retry.fallbackChains`) cannot move the run to another model or
  provider. Retrying the same model is unaffected. The overlay beats your
  `config.yml`; the dry-run argv shows it as `--config <omp config overlay>`.
  If a different provider or model is served anyway, the run fails
  (`model_continuity_paused`) with a message naming what OMP tried to serve,
  for example `requested opencode-go/glm-5.3, but omp tried to serve
  fireworks/glm-5p3`.
- Naming `--continuity-mode fungible` or `panel` is the opt-in to failover: the
  overlay is not sent, OMP may fail over, and a served model that differs from
  the request is recorded as a `model_substitution` warning that names the
  served provider. A run with no model, or with a bare model ID, is `fungible`
  unless you name a mode.
- Use the full `provider/model-id` selector, including the provider even when the
  model ID itself contains `/`. OMP reports provider and model separately;
  Delegate compares their exact combined identity. A slash-free bare model ID
  pins the exact model name, then binds the first observed provider for
  subsequent switch detection. Ambiguous slash-bearing bare IDs are not accepted
  as equivalent to provider-qualified selectors.
- `--model` accepts three shapes: a key of `omp.models` (an alias), a
  `provider/model-id` selector (a raw ID, split at the first slash), or a bare
  name. OMP resolves a bare name by exact ID and then by fuzzy match against its
  own bundled catalog, which can land on a different provider, so a retired alias
  such as `kimi` must not reach it. Once a discovered omp catalog exists, a bare
  name that is not an alias and not exactly the model ID of a catalog entry fails
  with `invalid_alias` and lists the configured aliases and the nearest catalog
  selectors. With no catalog the run proceeds with a warning; run
  `delegate capabilities refresh` so bare names can be checked.

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
- Codex `max` support is model-scoped and bundled for the GPT-6 models (`gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`) as of 2026-09. Other Codex models fail closed unless an exact config, profile-discovery, or legacy workspace-cache declaration includes `max`.

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

#### `isolation.safeBackend` and `isolation.bwrapBinds` (Linux)

```json
{
  "isolation": {
    "safeBackend": "bwrap",
    "bwrapBinds": [
      {"path": "/srv/example-agent/runtime", "mode": "ro"},
      {"path": "/var/lib/example-agent/cache", "mode": "rw"}
    ]
  }
}
```

`safeBackend` is `copy` (default, every platform) or `bwrap` (Linux with
bubblewrap): eligible non-Cursor safe runs on Git workspaces then run zero-copy
inside a bubblewrap boundary. The real workspace is read-only-bound, gitignored
paths are hidden by parity masks, `$HOME` and `/tmp` are private tmpfs, and the
workspace `.delegate/` registry is masked. `DELEGATE_SAFE_BACKEND` overrides
the key; an invalid value on either channel fails closed. `bwrapBinds` lists extra host
paths (`{"path", "mode": "ro"|"rw"}`, tilde-expanded) the engine launch needs
inside the boundary (absolute after `~` expansion); a missing path fails the
run (`bwrap_bind_missing`) and a `rw` entry that intersects the workspace in
either direction is refused (`bwrap_bind_conflict`). Cursor safe always uses the copy backend; the
boundary refuses `--pass-through` and initialized submodules. See the
[security model](security-model.md#zero-copy-safe-isolation-linux-isolationsafebackend-bwrap)
for the full boundary and fail-closed conditions.

#### `isolation.writeGuard`

```json
{
  "isolation": {
    "writeGuard": {
      "enabled": true,
      "onUnavailable": "warn",
      "macosSeatbelt": false,
      "codeRoot": null,
      "add": ["~/.config/example-secrets"],
      "remove": ["~/.config/gcloud"],
      "writable": ["~/.ssh/known_hosts.d"],
      "homeCaches": ["~/.cache", "~/.npm"]
    }
  }
}
```

The work write guard keeps work lanes off a named list of irreplaceable paths
(credential stores, `~/.ai-profiles`, and the installed Delegate runtime).
Code roots and sibling checkouts are not protected by default; opt in with
`codeRoot` or `add`. The protect-list backends leave other paths writable;
Codex's native sandbox still applies its own workspace write limits. A lane
started through an estate launcher (`estate-claude`, `estate-codex`, ...) gets
`~/.ai-profiles` re-opened, because the launcher writes account and session state
there on every launch. Every key is optional; an
unknown key or a wrong type fails config validation (`invalid_isolation_config`).
See the [security model](security-model.md#work-write-guard) for the default
protected list, the writable re-opens every run gets, and the backends.

- `enabled` (default `true`): `false` means Delegate adds nothing to work lanes:
  no wrap, no Codex `--add-dir` roots, no prompt note. The environment variable
  `DELEGATE_WRITE_GUARD` overrides it (`off`, `0`, `false`, `no` disable; `on`,
  `1`, `true`, `yes` enable).
- `onUnavailable` (`warn` default, or `refuse`): what happens when the backend
  is missing or fails its preflight: bubblewrap on Linux, and `sandbox-exec` on
  the Mac when `macosSeatbelt` is on (its probe run counts as the preflight).
  `warn` launches unguarded and records a warning; `refuse` fails the launch
  with `write_guard_unavailable`. On Linux with `warn`, a preflight that fails
  because one path will not bind drops only that path, not the whole guard: the
  manifest lists it under `unbound` and the run gets a warning. If a dropped
  path was a protected one, the guard status is `partial` instead of `enforced`.
  `refuse` never retries.
- `macosSeatbelt` (default `false`): opt in to the macOS Seatbelt guard. It
  wraps every engine except a Codex lane whose own sandbox is on (that lane gets
  `--add-dir` roots instead; see the recipe below). A Codex lane running with its
  sandbox bypassed is wrapped, and that combination is not yet live-tested.
  While it is `false` the Mac guard status is `off`, with no warning and no
  refusal.
- `codeRoot` (default `null`): opt in to protecting a directory's checkouts,
  for example `"~/Code"`. The run's own execution root, git common directory
  and registry are re-opened automatically. `null` disables this protection.
- `add`: extra paths to protect (absolute; a leading `~` expands).
- `remove`: default protected paths to drop.
- `writable`: paths re-opened for every run. `delegate ... --writable PATH`
  re-opens an existing path for one run only and is CLI-only (there is no
  `run --input-json` field). This is also how to open an engine's profile
  directory when Delegate has no home variable for the engine, or when the
  engine home was refused: only the selected engine's own home variable
  re-opens anything by itself, and a home that does not itself carry an
  identity file, contains another profile's home, or is too large or unreadable
  to check is listed under `refused` in the manifest and stays read-only.
- `homeCaches`: package-manager cache directories under HOME. Backends that
  protect a list leave them writable by construction; the Codex native sandbox
  does not, so its writable roots include the ones that exist.

Missing paths are skipped silently (there is nothing to lose there). Delegate
records the resolved plan in the run manifest under `writeGuard` and prints it in
`--dry-run` output.

**Recipe: Codex work lanes on the Mac.** The `external-sandbox` profile makes
Codex work lanes run with `--dangerously-bypass-approvals-and-sandbox`. To keep
that profile and put Codex's own `workspace-write` sandbox back on for work
lanes, override only the harness-scoped field:

```json
{
  "policy": {
    "profile": "external-sandbox",
    "harness": {"codex": {"work": {"bypassApprovalsAndSandbox": false}}}
  }
}
```

Delegate then emits `--sandbox workspace-write` (with network access per the
policy), adds the git common directory, registry, run scratch and temp,
`--writable` paths and existing home caches as `--add-dir` roots, and leaves
Codex unwrapped. To guard the other engines on the Mac as well, also set
`isolation.writeGuard.macosSeatbelt` to `true`.

### `worktrees`

```json
{
  "worktrees": {
    "dataHome": null,
    "poolWarnCount": 20,
    "retireWorktreeOnCompletion": true,
    "autoPrune": {
      "enabled": false,
      "mergedOlderThanDays": 7
    }
  }
}
```

- `dataHome`: persistent-worktree root. `null` means `~/.delegate/worktrees`.
- `poolWarnCount`: non-negative worktree-count threshold for a launch-time warning when the shared persistent-worktree pool holds more worktrees. The default is 20. The warning does not block or delete anything.
- `retireWorktreeOnCompletion`: when true (the default), a successful work-lane run retires its clean persistent worktree while preserving the `delegate/*` branch. Unchanged source dirt seeded at launch is treated as clean; failed/cancelled runs, child edits, unverifiable state, and unsafe metadata retain the worktree and are reported in the completion payload. Set false to keep manual cleanup behavior.
- `autoPrune.enabled`: if true, `delegate worktree list` and work-lane completion run the existing opportunistic prune pass for clean, fully merged worktrees older than `mergedOlderThanDays`.
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

### `providerErrors`

```json
{
  "providerErrors": {
    "knownBadLaneMinutes": 15,
    "autoResume": true,
    "stageStopAfter": 3,
    "authProbes": {
      "cursor": ["estate-cursor", "status"],
      "omp": ["estate-omp", "usage"]
    }
  }
}
```

How Delegate reacts to provider errors; see
[troubleshooting](troubleshooting.md#provider-errors-known-bad-lanes-and-automatic-resume)
for the signature table.

- `knownBadLaneMinutes`: how long a lane stays marked known-bad after a
  persistent, lane-scoped provider failure (bad credentials, no credit, no access
  to the model). Launches on a marked lane are refused with `lane_known_bad`
  (exit code 4) unless `--force-launch` is given. Defaults to `15`; `0` turns the
  markers off. Any non-negative number of minutes.
- `autoResume`: defaults to `true`. A Codex or Claude work run that dies on a
  transient stream drop or provider 5xx and saved its session gets exactly one
  automatic continuation. It also gates one fresh rerun of a `safe` run killed by
  a stream drop and one retry of a safe-mode broker `binding_not_active` launch refusal (see
  [troubleshooting](troubleshooting.md#automatic-resume-after-a-transient-drop)).
  `false` opts out of all three.
- `stageStopAfter`: defaults to `3`. In a workflow, when the first this-many
  results of one `phase()` stage on one lane all failed with the same persistent
  lane-scoped signature, the stage stops launching further calls on that lane and
  they return `provider_exhausted`. `0` turns the stop off.
- `authProbes`: the read-only health commands `delegate capabilities refresh`
  runs to record per-engine auth health (`authHealth`, shown by `delegate
  doctor`). Only `cursor` and `omp` are understood; each value is a command and
  its arguments, or `null` to turn that probe off. A missing or unrecognised
  probe records `unknown`, never a failure. An empty `authProbes: {}` in your
  config leaves the defaults in place, so use `null` per engine to disable.
  Set the environment variable `DELEGATE_AUTH_PROBES=off` (or `0`, `false`,
  `no`) to skip every probe, for CI or a sandbox.

Unknown keys under `providerErrors` are rejected with
`invalid_provider_errors_config`.

### `workflows`

#### Operational settings on pinned workflow attempts

New workflow pins support immutable per-attempt operational snapshots. On a
normal launch, resume, or approval, Delegate takes the command's already-loaded
config and copies only these settings over the creation pin:

- `workflows.engineCaps`, `itemThreads`, `structuredOutputRetries`, and
  `stallMinutes` (including its top-level `stallMinutes` fallback).
- `tracking.processGroupTerminationGraceSec` and `registryLockTimeoutSec`
  (including the older `registryLockTimeoutSeconds` spelling).
- `progress.enabled`, `initialDelaySec`, and `intervalSec`.
- `worktrees.poolWarnCount`.

Pin creation completes empty or partial input from creation-time defaults and
preserves explicit scalar nulls. Nullable operational sections are resolved to
their creation defaults. Later attempts never refill model/security identity
from a newer CLI's defaults; verifying an existing pin does not rewrite it.

Models, binaries, profile/account selection configuration, permissions,
isolation, personas, and executable code stay pinned. Cleanup permissions stay
pinned too: changing `retirementIgnoreGlobs`, retirement enablement, or auto-prune
policy does not change an existing workflow's authority to remove files.
There is no restored workflow-timeout/watchdog setting in this allowlist.

New pins also record the resolved Delegate profile identity, not just profile
definitions. `DELEGATE_PROFILE`, custom `profiles.detectFrom` variables, and
default/no-selection cases are resolved at creation. A later selector that
chooses a different profile fails with `workflow_profile_drift` before approval
or a managed child launch. The effective `HOME`, `CODEX_HOME`, and
`CLAUDE_CONFIG_DIR` namespaces are bound; configured primary/fallback home
expansions are frozen as absolute paths. Ambient values hidden by a profile's
explicit override do not cause false drift refusals. Token contents are never
recorded, and rotation within the same namespace remains allowed.

New workflow pins require absolute effective credential-home paths. In particular,
a relative ambient `CLAUDE_CONFIG_DIR` or `CODEX_HOME` is refused instead of binding
an account whose meaning changes with an isolated child's working directory.
This creation-time restriction does not migrate existing pins.

This validator covers ambient selection for the supervisor and managed DSL
children, not arbitrary Python subprocesses, vendor processes, or credential
stores. A workflow script remains trusted executable code: it can manually launch
another command with an explicit `--auth-profile`. External harnesses drop both pin
and attempt bootstrap markers, retaining the effective config data while trusted
mail-push/private/pure home derivations remain free to operate. Older pins without
a `profileIdentity` stamp retain their existing execution behavior and report
`profileIdentityPinned: false` plus an identity-unavailable warning; definitions
alone are not represented as proof of frozen credential selection.

Children inherit the launching PATH, including `~/.local/bin`, so a child can
reach a human-paging helper such as `ask`. Set `DELEGATE_CHILD_NO_PAGE_ASK=1` in
the launching environment to opt out for tracked runs: each child then finds a
stub `ask` first on PATH, in the run's own private directory, that prints a
refusal and exits 2 (ask's relay-down code) without paging anyone. Nested
Delegate launches inherit the setting. Nothing under `$HOME` is written.

The operational environment overrides `DELEGATE_STALL_MINUTES`,
`DELEGATE_PROCESS_GROUP_TERMINATION_GRACE_SEC`,
`DELEGATE_REGISTRY_LOCK_TIMEOUT_SECONDS`, `DELEGATE_PROGRESS_INITIAL_DELAY_SEC`,
and `DELEGATE_PROGRESS_INTERVAL_SEC` are captured once at launch, ahead of config
values. Invalid operational numbers fail before approval or launch-state
mutation. The supervisor and its Delegate children receive the captured values
and the same effective config path; a conflicting inherited override is refused,
not silently applied later. Global and local config overlays are not merged into
a validated attempt snapshot.

The content-addressed artifact lives under
`~/.delegate-workflow-pins/attempts/<wfId>/<digest>/`. It contains read-only
`config.json` and `attempt.json`, bound to the base config and runtime digests.
Identical snapshots may be reused; different concurrent attempts cannot overwrite
each other's settings. No user config file is required when embedded defaults
are sufficient. An explicitly selected missing config remains an error.
Artifacts are staged privately and published only after both files are complete.
A failed staging write does not poison an identical retry; leftover staging
directories and pre-existing partial destinations are retained, never silently
replaced or deleted. A failed resume/approval launch restores the prior approval
bytes (or absence) under the workflow lock, including when detachment fails.

Launch responses, journal `attempt_config` events, and supervisor status expose
`attemptConfig`: `effectiveConfigDigest`, base digests, `opsSource`,
`opsChangedKeys`, `opsEnvironment`, and allowlisted `opsValues`. The launch
response also gives `effectiveConfigPath`. Source is a provenance label, not a
verified source-control revision. Credentials are not included in this projection.

Resumption requires the current workflow format: version-2 structural keys and
a runtime pin supporting version-1 attempt configuration. Pinless workflows and
older formats are rejected before child launch; start a new workflow rather
than migrating old state. Synchronous dry runs use command configuration and
do not establish an operational snapshot for a live attempt. A resume keeps the
pinned runtime; `workflow resume --repin` moves the pin onto the live runtime
first, and the attempt snapshot is then bound to the new runtime digest (see
[Pinned runtime and `--repin`](delegate-workflows.md#pinned-runtime-and---repin)).

#### Workflow defaults

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

Workflow hard caps are not configurable in v1: scripts are limited to 1 MiB,
nested `workflow()` calls to depth 3, lifetime `agent()` calls to 1000, and
`pipeline()`/`parallel()` inputs to 4096 items.
