# CLI reference and JSON contracts

Use `delegate --help` for the exact command list from the installed version. Global options must appear before the subcommand, except `--json` and `--isolation` are also accepted in launch and `dry-run` option tails before inline prompt text begins.

## Global options

```text
--cwd PATH                    Resolve and run from PATH. Git directories resolve to the repo root.
--json                        Emit JSON for commands that support it.
--isolation auto|none|worktree
--pass-through                Stream raw child stdout/stderr. Incompatible with --json and persistent worktree runs.
--completion-report MODE      markdown or none.
--no-completion-report        Disable completion-report prompt injection.
--auth-profile NAME           Override detected profiles for launches, dry-run, run --input-json, profiles, models, capabilities, and setup.
--group NAME                  Tag a launch/run-input request with a lightweight group ([A-Za-z0-9._-]{1,64}).
```

## Workspace mail

Mail is a parent-owned pull mailbox under `.delegate/mail`; mail commands are
available even when `mail.enabled` is false. Mail's `--group` is command-local
and must not be confused with launch `--group`:

```text
delegate mail send (--to ALIAS|coordinator | --group NAME) [--reply-to ID] [--subject S] (BODY|--file FILE|-)
delegate mail inbox [--from SENDER]
delegate mail read ID_PREFIX [--peek]
delegate mail status MESSAGE_ID
delegate mail watch [--once] [--from SENDER] [--reply-to ID] [--timeout SEC] [--interval-ms N]
delegate mail prune [--older-than DAYS] [--dry-run]
```

`send` records per-recipient outcomes (`delivered`, `failed`,
`skipped_ineligible`, or `blocked`); `status` may report `pruned` when a
delivered mailbox file is gone. `watch --once` emits metadata-only NDJSON and
returns exit code 124 on timeout. Read-only profile guards allow inbox,
status, watch, and `read --peek`; mutating mail operations remain protected.
`mail prune --dry-run` is byte-preserving and best-effort; under concurrent
mail activity its report may be stale or internally inconsistent and is not
an exact later mutation plan.

Work launches may opt into stop-hook push with `--mail-push` (or input JSON
`mailPush: true`). It is never enabled by `mail.enabled` alone and is refused
for slash-passthrough or `--pass-through` launches. Push reads unread mail
without consuming it, injects at most 50 messages and 512 KiB per boundary,
and advances a per-run cursor only after the hook response is written. Every
injected lane message includes the Tier-2 data-not-prompt framing. Claude and
Codex are the only audited verified adapters; Cursor, Droid, Grok, and other
harnesses keep the pull suffix and record a one-time degraded-to-pull warning.

`delegate mail hook-pump` is an internal command used only by those
launch-scoped adapters. It is not a user-facing way to bypass the mailbox or
launch prompt.

## Personas

Named personas are UTF-8 Markdown files resolved from the source workspace:
`.delegate/personas/<name>.md` shadows `~/.delegate/personas/<name>.md`. Names
must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; files are regular, non-symlink
files no larger than 64 KiB and are rejected on invalid UTF-8 or disallowed C0
controls. The workspace-local directory is not VCS-shared by default; teams
that intentionally version a persona must force-add that individual file.

```bash
delegate personas
delegate --json personas
delegate claude work --persona editor "Review chapter 3"
delegate codex safe --persona editor --allow-repo-persona "Review the diff"
delegate resume --persona editor <handle> "Continue with the requested fixes"
delegate resume --no-persona <handle> "Continue without the recorded persona"
```

`--persona NAME`, `--no-persona`, and `--allow-repo-persona` are shared launch
options. A workspace-local persona is refused in safe mode unless explicitly
allowed. Resolution logs `persona: NAME (workspace|global)` to stderr. Personas
are not accepted for call, read-only call, `--pass-through`, or slash-passthrough
prompts. Input JSON uses `persona` (string or null) and `allowRepoPersona`
(boolean).

`delegate --json personas` returns schema `delegate.personas.v1` with sorted rows
containing `name`, `source`, `sizeBytes`, and an escaped bounded `preview`.
Invalid files remain visible with an `invalid` reason; a workspace row that
shadows a global row also includes `shadowsGlobal: true`.

Work-mode transport is prepend for most engines, Claude native-file only when
the discovery cache proves `--append-system-prompt-file` (otherwise prepend
with a warning), and OpenCode agent-config merge. OpenCode preserves existing
root keys, agent keys, and prompts, appending the persona after an existing
agent prompt. Safe mode always prepends. Claude native-file keeps the persona
body out of argv, dry-run JSON, and the manifest; prepend engines using argv
transport (Cursor, Kimi, and Oh My Pi) expose it in the live process argv.
Tracked runs retain it only in the private
`persona.txt` artifact.

## Commands

### Direct runtime commands

```bash
delegate cursor safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate cursor work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate cursor call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate droid [MODEL_ALIAS] safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate droid [MODEL_ALIAS] work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate droid [MODEL_ALIAS] call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate codex safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--fast|--no-fast] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [--output-schema FILE] [prompt...]
delegate codex work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--fast|--no-fast] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [--output-schema FILE] [prompt...]
delegate codex call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--fast|--no-fast] [--prompt-file PATH] [--output-schema FILE] [prompt...]

delegate claude safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate claude work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate claude call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate grok safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate grok work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate grok call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate opencode safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--agent NAME] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate opencode work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--agent NAME] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate opencode call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--agent NAME] [--prompt-file PATH] [prompt...]

delegate pi safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate pi work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate pi call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate omp safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate omp work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate omp call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]

delegate kimi safe [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate kimi work [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate kimi call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

Prompt sources are direct arguments, `--prompt-file`, or Delegate stdin. Raw C0 control characters other than newline, carriage return, and tab are stripped before launch; a prompt that becomes empty fails fast. After
Delegate resolves the prompt, Codex, Claude, OpenCode, and Pi prompts are passed to the child runtime over
stdin. Oh My Pi receives a positional prompt because 17.0.4 did not consume piped stdin in the verified
non-interactive invocation. Droid and Grok prompts are written to a private temporary prompt file and passed
with Droid's documented `--file` option or Grok's `--prompt-file`. Cursor Agent currently only exposes
positional prompt input, and Kimi Code prompt mode currently uses `--prompt`,
so those Harnesses also use argv transport; Delegate redacts Cursor, Oh My Pi, and Kimi
prompt argv in dry-run output and run manifests.
`--prompt-file /dev/stdin` (also `-`, `/dev/fd/0`, and equivalent non-regular
stdin descriptors) is treated as the stdin source itself, so a pipe is read
exactly once rather than rejected as a second prompt source.

Temporary safe isolation also re-roots absolute paths under the source workspace
when it transports the prompt, so `/source/repo/src/app.py` becomes the matching
path inside the isolated copy. Paths outside the workspace and prefix lookalikes
are left unchanged. The safe prompt asks the child to cite workspace-relative
paths in its report; consumers should not depend on temporary isolation paths.
Verbatim slash pass-through prompts are not rewritten.

For launch and `dry-run` commands, `--json` and `--isolation auto|none|worktree`
are unambiguous before inline prompt text starts and may appear with launch
options, such as `delegate codex work --prompt-file task.md --json` or
`delegate codex work --isolation worktree "Implement..."`. After prompt text
begins, a later `--json` or `--isolation` still fails with
`misplaced_global_option`; use `--prompt-file` or stdin for literal flag-like
prompt text.

`--model <alias-or-model>` is optional on every engine and is parsed only before
prompt text begins. The value is resolved against `<engine>.models` when it
matches an alias key; otherwise it is passed through verbatim as a raw model ID
(the harness validates unknown IDs). Droid also accepts an optional positional
`MODEL_ALIAS` (alias-only/strict); give either the positional or `--model`, not
both. With neither, Droid uses `droid.defaultModel` when set. Discover aliases
and advisory catalogs with `delegate models`, `delegate models <engine>`, and
`delegate models <engine> --live`. Every harness except Claude exposes a live
model probe; see [Discovery](#discovery) for the evidence and Devin caveats.

`--reasoning-effort LEVEL` is optional and parsed only before prompt text begins.
Engines with exact capability metadata reject unsupported model/effort pairs
before launch with `unsupported_reasoning_effort`. It affects only model
reasoning depth, cost, or latency; it does not change `safe`/`work`/`call`
permissions, sandboxing, approvals, network policy, or edit capability. Cursor
effort is model-selection based. Without an explicit model pin,
`cursor.reasoningEffortModels` takes precedence over discovered routes. An
explicit `--model` blocks that global map; when discovery has an exact route
family for the pinned selector, Delegate may replace it with the requested
same-family effort selector and records a warning. Without that exact family,
an explicit effort fails closed; a configured default instead degrades with a
warning and preserves the pinned selector. Droid emits
`--reasoning-effort LEVEL`; Codex emits a `model_reasoning_effort` config
override; Claude and Grok emit native `--effort`; OpenCode emits `--variant`
and validates it when exact discovered variants exist; Pi and Oh My Pi emit
`--thinking`. Devin and Kimi expose no Delegate effort transport.

Codex `max` support is model-scoped and, as of 2026-07, bundled only for
`gpt-5.6-sol`. Other Codex models fail closed unless an exact
config, profile-discovery, or legacy workspace-cache declaration explicitly
supports `max`;
other engines retain their independently documented effort sets.

`--fast` and `--no-fast` are Codex-only, mutually exclusive per-run service-tier overrides. `--fast` emits `service_tier="fast"` plus `features.fast_mode=true` (Codex silently drops a Fast tier when that feature flag is off in the ambient config, so Delegate enables it explicitly); `--no-fast` emits `service_tier="default"` so a globally enabled Fast setting can be turned off for one child. Omitting both emits no override and inherits Codex configuration. Fast is orthogonal to model selection, reasoning effort, and Delegate safety policy. Two upstream caveats: Codex strips the service tier when authenticated with an API key (Fast is a ChatGPT-plan feature), and neither Codex nor the API fails on a tier the model does not offer — Delegate's flag validation is the only fail-closed layer, so an unsupported combination degrades silently to standard routing rather than erroring.

`--progress` enables parent progress heartbeats on stderr for tracked foreground
runs. `--no-progress` disables them even when `progress.enabled` is true in
config. When neither flag is set, config `progress.enabled` applies (default
`false`). Heartbeat labels are credential-scrubbed before printing. Timing
resolves as env override > config > built-in default (30s initial / 60s
interval). It is incompatible with `--pass-through`.

`--forbid-commit` is an opt-in launch flag for `work` mode with persistent
worktree isolation; when isolation is omitted, it implies `--isolation worktree`
and launch output prints `note: --forbid-commit implies --isolation worktree`.
It injects a no-commit prompt note and fails the run if
commits remain ahead of the creation base when the child exits. Without it,
Delegate still reports remaining child commits in the work summary, emits a
warning plus suggested review commands, but does not fail solely because commits
exist. Validation rejects `--forbid-commit` outside `work` mode, and explicit
`--isolation none --forbid-commit` remains invalid.

Work-mode persistent worktrees automatically copy uncommitted tracked changes
and untracked non-ignored files from a dirty source checkout before the child
starts. Gitignored files remain excluded, and external symlinks are blocked with
the same protections used by safe-mode workspace sync. Automatic sync emits a
`dirty_source_auto_included` warning with tracked-modified and untracked counts.
`--include-dirty` remains an explicit launch flag and is a no-op when the source
is already clean.
JSON and text completion output report `includeDirty: true` / `syncedFiles`.
`run --input-json` accepts the equivalent boolean field `includeDirty`.

A few boundaries are worth stating explicitly:

- **Tracked-but-gitignored files sync by design.** A path that is tracked in
  repo history but also matched by a `.gitignore` rule is part of the repository
  and is synced like any other tracked file; `--include-dirty` excludes only
  untracked gitignored paths, not tracked ones.
- **Dirty submodules fail preflight.** Delegate auto-syncs ordinary tracked and
  untracked non-ignored source changes, but cannot safely reproduce dirty
  submodule state, so it refuses the launch until that submodule is clean.
- **Keep secrets out of hardlinks.** A hardlink at a non-ignored path to
  gitignored content is indistinguishable from a regular file and will sync by
  content; `--include-dirty` trusts every non-ignored path. Path-based exclusion
  cannot close this; keep secrets out of hardlinks.

`--group NAME` tags launch and `run --input-json` registry entries. It does not
create an orchestration manifest; it only enables selectors such as
`delegate runs --group NAME`, `delegate wait --group NAME`, and worktree
management filters. Group names must match `[A-Za-z0-9._-]{1,64}`.
If grouped feature waves run in the same non-isolated workspace, commit between
waves so later edits do not become interleaved with earlier work. When features
need separate review or commits, launch each in a persistent worktree and
integrate them separately; `wait --group` warns when it sees shared same-tree
work runs.

`--auth-profile NAME` selects a top-level `profiles.definitions` entry. Delegate
injects that profile's flat env map into child launches and metadata probes, and
uses the same profile name to select cached discovery. It overrides ambient
detection for launches, `dry-run`, `run --input-json`, `delegate profiles`,
`models`, `capabilities`, and `setup`. Unknown names fail with
`unknown_profile`. It is rejected for run-inspection, worktree-management, and
unrelated diagnostics where no child env or profile cache selection occurs.

### `delegate codex`

Usage:

```bash
delegate [--json] [--isolation auto|none|worktree] codex {safe,work} [--model <alias-or-model>] [--reasoning-effort LEVEL] [--fast|--no-fast] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [--output-schema FILE] [prompt...]
delegate [--json] codex call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--fast|--no-fast] [--prompt-file PATH] [--output-schema FILE] [prompt...]
```

- Safe mode reviews your **current working tree** — uncommitted tracked edits and untracked, non-ignored files are mirrored into an isolated throwaway copy (only gitignored paths are excluded), so you can review local changes without committing first or pasting a diff. Codex safe always uses `--sandbox read-only`. Under `--isolation auto`, Codex safe is the only safe harness that may opt out with `--isolation none`, because Codex still keeps its read-only sandbox active.
- Prompt text is delivered on stdin to `codex exec`; dry-run argv and tracked run manifests do not contain the prompt.
- Model selection uses `--model` (alias from `codex.models` or a raw model ID), the run-input JSON `model`, or `codex.defaultModel`.
- `--reasoning-effort` maps to a Codex `model_reasoning_effort` config override after the model is resolved. The `max` level is bundled only for `gpt-5.6-sol` as of 2026-07.
- `--fast` requests Codex Fast for one run; `--no-fast` explicitly requests Standard; omission inherits Codex configuration. The selected tier is recorded as `requestedFast` when explicit.
- For Codex, `--output-schema FILE` supplies the JSON Schema OpenAI enforces on the final message. Relative paths resolve against the process launch cwd, the same rule as `--prompt-file`. Delegate recursively preflights strict object schemas: missing `additionalProperties: false` is supplied in a temporary execution copy with a warning, while an incomplete `required` list or `additionalProperties` value other than `false` fails before launch. The source file is never modified. When set, Delegate suppresses completion-report prompt injection so the schema owns the whole final message. Claude also supports `--output-schema` in call mode; other engines reject it.

Examples:

```bash
delegate codex safe "Review this repo for regressions; report file/line/severity."
delegate codex safe --model reviewer "Review this repo for regressions; report file/line/severity."
delegate codex safe --model my-alias --reasoning-effort medium --fast "Explore likely causes."
delegate codex work "Implement the scoped task; report changed files and tests."
delegate codex call "Summarize this context in three bullets."
delegate --json codex safe --output-schema findings.schema.json "Return one record per finding."
delegate --isolation worktree codex work "Implement the feature in a persistent worktree."
```

### `delegate claude`

Usage:

```bash
delegate [--json] [--isolation auto|none|worktree] claude {safe,work} [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate [--json] claude call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

- Safe mode reviews your **current working tree** — uncommitted tracked edits and untracked, non-ignored files are mirrored into an isolated throwaway copy (only gitignored paths are excluded), so you can review local changes without committing first or pasting a diff. Under `--isolation auto`, Claude safe uses `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob, and selected read-only Bash tools such as `git diff`/`git status`.
- Claude safe mode is not hermetic: Delegate does not prove hooks, plugins, user settings, output styles, or other non-MCP customization surfaces are disabled. Use `claude.bare: true` for a more minimal/reproducible Claude invocation, and keep safe-mode work review-only.
- Prompt text is delivered on stdin to `claude -p`; dry-run argv and tracked run manifests do not contain the prompt.
- JSON-streaming runs use `--output-format stream-json --input-format text`; pass-through runs use `--output-format text`.
- Work mode uses `claude.workPermissionMode` from config, unless Delegate policy explicitly enables `policy.harness.claude.work.bypassApprovalsAndSandbox`, which maps to Claude `--permission-mode bypassPermissions`.
- Model selection uses `--model` (alias from `claude.models` or a raw model ID), the run-input JSON `model`, or `claude.defaultModel`.
- `--reasoning-effort` maps to Claude Code `--effort` and accepts `low`, `medium`, `high`, `xhigh`, or `max`.

Examples:

```bash
delegate claude safe "Review this repo for regressions; report file/line/severity."
delegate claude work --model implementer "Implement the scoped task; report changed files and tests."
delegate claude call "Summarize this context in three bullets."
delegate --isolation worktree claude work "Implement the feature in a persistent worktree."
```

### `delegate grok`

Usage:

```bash
delegate [--json] [--isolation auto|none|worktree] grok {safe,work} [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate [--json] grok call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

- Safe mode reviews your **current working tree** in an isolated throwaway copy plus Grok read-only controls (`--sandbox read-only`, `--permission-mode dontAsk` by default). Delegate does not use Grok `plan` mode for safe review.
- Prompt text is delivered via Grok `--prompt-file` from a Delegate temp file; dry-run argv and tracked run manifests do not contain the prompt.
- Work mode uses `grok.workPermissionMode` and `grok.workSandbox` from config, unless Delegate policy explicitly enables `policy.harness.grok.work.bypassApprovalsAndSandbox`, which maps to Grok `--permission-mode bypassPermissions`.
- Model selection uses `--model` (alias from `grok.models` or a raw model ID), the run-input JSON `model`, or `grok.defaultModel`.
- `--reasoning-effort` maps to Grok `--effort` and accepts `low`, `medium`, `high`, `xhigh`, or `max`.
- `--output-schema` is unsupported for Grok in v1 because Grok `--json-schema` forces final JSON output and weakens live snapshot parity.

Examples:

```bash
delegate grok safe "Review this repo for regressions; report file/line/severity."
delegate grok work "Implement the scoped task; report changed files and tests."
delegate grok call "Summarize this context in three bullets."
delegate --isolation worktree grok work "Implement the feature in a persistent worktree."
```

### `delegate opencode`

Wraps OpenCode's non-interactive `run` command.

```bash
delegate [--json] [--isolation auto|none|worktree] opencode {safe,work} [--model <alias-or-model>] [--reasoning-effort LEVEL] [--agent NAME] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate [--json] opencode call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--agent NAME] [--prompt-file PATH] [prompt...]
```

- Safe mode reviews the current working tree in an isolated throwaway copy. It
  injects an `OPENCODE_CONFIG_CONTENT` permission lockdown that allows only
  read, glob, and grep operations; OpenCode merges the override last, so a
  repository config cannot restore write-capable tools. `call --read-only`
  uses the same lockdown. Plain `call` has no permission lockdown.
- Prompt text is delivered on stdin. The child argv starts with `opencode run
  --format json --print-logs --dir <workspace>`; work mode adds `--auto`, while
  safe and call modes do not.
- Model selection uses `--model`, the run-input JSON `model`, or
  `opencode.defaultModel`. IDs use OpenCode's `provider/model` form and pass
  through verbatim. An `opencode.models` alias may be a model string or an
  object that pins both model and variant:

  ```json
  {
    "opencode": {
      "models": {
        "reviewer": "provider/model",
        "reviewer-high": {
          "model": "provider/model",
          "variant": "high"
        }
      }
    }
  }
  ```

- `--reasoning-effort LEVEL` maps directly to OpenCode `--variant LEVEL` and
  overrides an alias-pinned variant. OpenCode silently ignores bogus variant
  names, so a typo can have no effect.
- `--agent NAME` selects an OpenCode agent for one run. With no flag,
  `opencode.defaultAgent` is used when configured.
- `delegate models opencode --live` runs `opencode --pure models`. Live discovery has
  returned 452+ models and includes any provider in OpenCode's models.dev
  catalog, plus configured custom or local providers.
- OpenCode is available to workflow `agent()` calls and
  `workflows.engineCaps` like other engines.
- OpenCode currently buffers stdout until completion, so progress can remain
  silent even though `--print-logs` stderr is visible. Sessions accumulate in
  the user's global OpenCode state. Call mode has no Delegate timeout unless
  `--timeout` is set.

Examples:

```bash
delegate opencode safe "Review this repo for regressions; report file/line/severity."
delegate opencode work --agent build "Implement the scoped task; report changed files and tests."
delegate opencode call --read-only --model reviewer --prompt-file rubric.md
delegate --isolation worktree opencode work "Implement the feature in a persistent worktree."
```

### `delegate pi`

Wraps Pi's headless print mode.

```bash
delegate [--json] [--isolation auto|none|worktree] pi {safe,work} [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate [--json] pi call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

- Delegate launches `pi -p --no-session --mode json` and sends the resolved prompt over stdin.
- Safe mode and `call --read-only` add `--tools read --no-extensions --no-skills --no-prompt-templates --no-approve`; safe mode also uses Delegate's isolated throwaway workspace.
- Model aliases accept either a Pi `provider/model` string or `{ "model": "provider/model", "thinking": "LEVEL" }`.
- `--reasoning-effort` maps directly to `--thinking` for `low`, `medium`, `high`, `xhigh`, and `max`. Structured aliases may select Pi's additional `off` or `minimal` levels.
- All modes are stateless at Pi's session layer; Delegate's run registry remains the durable record.
- JSON call responses retain the standard `text` field and also populate `assistantText`, matching tracked safe/work envelopes.
- `delegate models pi --live` queries Pi's visible provider/model catalog.

Examples:

```bash
delegate pi safe --reasoning-effort high "Review this repo for regressions."
delegate pi work --model reviewer "Implement the scoped task and run its checks."
delegate pi call --read-only --prompt-file rubric.md
```

### `delegate omp`

Wraps Oh My Pi's headless print mode.

```bash
delegate [--json] [--isolation auto|none|worktree] omp {safe,work} [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate [--json] omp call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

- Delegate launches `omp -p --no-session --mode json` and passes the resolved prompt as a positional argument.
- Safe mode and `call --read-only` add `--tools read --no-extensions --no-skills --no-rules --no-lsp --approval-mode always-ask`; the approval mode is the load-bearing write/exec denial in headless mode, and safe mode also uses Delegate's isolated throwaway workspace.
- Model aliases accept either a `provider/model` string or `{ "model": "provider/model", "thinking": "LEVEL" }`.
- `--reasoning-effort` maps directly to `--thinking` for `low`, `medium`, `high`, `xhigh`, and `max`. Structured aliases may select `off` or `minimal`.
- Delegate never emits Oh My Pi's `--smol`, `--slow`, `--plan`, `--prewalk*`, or `--plan-yolo*` role flags.
- `delegate models omp --live` uses `omp models --json --no-extensions`.

Examples:

```bash
delegate omp safe --reasoning-effort high "Review this repo for regressions."
delegate omp work --model reviewer "Implement the scoped task and run its checks."
delegate omp call --read-only --prompt-file rubric.md
```

### `delegate kimi`

Usage:

```bash
delegate [--json] [--isolation auto|none|worktree] kimi {safe,work} [--model <alias-or-model>] [--reasoning-effort LEVEL] [--progress] [--timeout SECONDS] [--forbid-commit] [--prompt-file PATH] [prompt...]
delegate [--json] kimi call [--read-only] [--timeout SECONDS] [--model <alias-or-model>] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]
```

- Safe mode reviews your **current working tree** — uncommitted tracked edits and untracked, non-ignored files are mirrored into an isolated throwaway copy (only gitignored paths are excluded), so you can review local changes without committing first or pasting a diff. Under `--isolation auto`, Kimi safe uses a read-only safety prompt. Delegate intentionally avoids Kimi `--plan` in safe mode. Kimi prompt mode auto-approves tool actions, so the isolation is the effective write boundary; the safety prompt is advisory.
- Work mode uses Kimi prompt mode and runs in the real workspace unless you opt into worktree isolation. Delegate does not emit `--yolo` because Kimi rejects combining `--yolo` with `--prompt`.
- Model selection uses `--model` (alias from `kimi.models` or a raw model ID), the run-input JSON `model`, or `kimi.defaultModel`.
- `--reasoning-effort` is rejected for Kimi: the Kimi CLI exposes no effort flag, and k3 supports effort internally via `~/.kimi-code/config.toml`.
- Kimi prompt text is passed via argv.
- Run JSON reports `usage: unavailable` for Kimi — expected: the Kimi CLI (verified 0.26.0) emits no usage/token lines in stream-json output, so there is nothing to parse.

Examples:

```bash
delegate kimi safe "Review this repo for regressions; report file/line/severity."
delegate kimi work "Implement the scoped task; report changed files and tests."
delegate kimi call "Summarize this context in three bullets."
delegate --isolation worktree kimi work "Implement the feature in a persistent worktree."
```

### `delegate workflow`

Workflows run a Python DSL supervisor that can fan out normal Delegate child
runs, journal progress, pause on approval gates, and resume from cached child
results. Workflow state lives under `.delegate/workflows/<wfId>/`; child runs
are tagged with `--group <wfId>`.

Usage:

```bash
delegate [--json] workflow check <script.py>
delegate [--json] workflow run <script.py> [--args JSON] [--budget N] [--dry-run]
delegate [--json] workflow run --resume <wfId> [--budget N]
delegate [--json] workflow status <wfId>
delegate [--json] workflow events <wfId> [--since SEQ]
delegate [--json] workflow watch <wfId> [--since SEQ]
delegate [--json] workflow wait [<wfId>] [--timeout SEC]
delegate [--json] workflow result [<wfId>] [--field KEY]
delegate [--json] workflow approve <wfId>
delegate [--json] workflow kill <wfId>
delegate [--json] workflow list
delegate [--json] workflow save <script.py> --name NAME
```

- `check` validates the workflow script, including literal preflight checks for
  unsupported `agent()` combinations.
- `run` launches a detached supervisor; `--dry-run` renders planned stubs
  without launching child agents or consuming real budget. Each entry in
  `runTree.calls` includes the resolved `model`, `effort`, `fast`, `isolation`,
  and UTF-8 `promptBytes`; Cursor/Kimi/OMP prompts over 102400 bytes add a warning
  before their argv transport limit can fail a real run.
- `--resume` replays the journal, adopts matching child runs by workflow agent
  key, and continues from missing work.
- `wait` and `result` accept an explicit workflow ID or, when omitted, resolve
  the latest eligible workflow. JSON output for implicit selection includes the
  selected `wfId` and `resolutionKind: "latest"`.
- `result --field KEY` extracts a top-level field from an object result. Text
  mode prints strings directly and JSON-encodes other values; JSON mode returns
  a field/value envelope.
- `approve` releases a paused gate (and resumes). Do not also run
  `run --resume` for the same gate — approve already is that resume. `kill`
  validates the supervisor process group before signaling and always attempts
  child fan-out cancellation.
- JSON-capable workflow commands return the normal `{ok: ...}` envelope. Invalid
  scripts fail with `invalid_workflow_script`.

#### Workflow error codes

Codes raised as `DelegateError` from workflow commands (`workflows/commands.py`):

| Code | Meaning |
| --- | --- |
| `invalid_workflow_args` | `--args` is not valid JSON. |
| `invalid_workflow_id` | `wfId` is not `wf_` + 12 hex digits. |
| `invalid_workflow_name` | Saved-workflow `--name` is not a simple file stem. |
| `invalid_workflow_script` | Script failed `check` / load validation. |
| `missing_workflow` | A verb that needs `<wfId>` was invoked without one. |
| `missing_workflow_result_field` | `result --field` was invoked without a key. |
| `missing_workflow_save_args` | `save` needs both `<script.py>` and `--name`. |
| `missing_workflow_script` | `run`/`check` need `<script.py>` or `--name`. |
| `unknown_workflow_action` | Unrecognized `workflow` subcommand. |
| `workflow_execution_failed` | Dry-run (or in-process) execution raised before detach. |
| `workflow_locked` | Another supervisor already holds the workflow flock. |
| `workflow_not_found` | No workflow directory / status for that `wfId`. |
| `workflow_not_gated` | `approve` on a workflow that is not paused on a gate. |
| `workflow_result_missing` | `result` before `result.json` exists. |
| `workflow_result_field_missing` | The requested top-level result field does not exist. |
| `workflow_result_not_object` | `--field` was requested for a non-object result. |
| `workflow_script_not_found` | Resolved script path is missing or not a file. |

See [Delegate Workflows](delegate-workflows.md) for the DSL, caps, config, and
gate semantics.

### Stateless `call` mode

`call` is the one-hop model-call form of Delegate: "work mode minus a repo." It
gives a child runtime a prompt with no project tree to resolve and captures the
final assistant text, so you can call a model to *do something* (or to *judge
something*) from anywhere, including a non-git directory. Calls are untracked
by default; grouped calls are the narrow exception described below.

```bash
delegate codex call "Write a Python script that finds the 500th prime and run it."
delegate --json grok call --read-only --prompt-file rubric.md
delegate --json codex call --read-only --output-schema verdict.json --prompt-file rubric.md
delegate --json claude call --pure --timeout 60 --output-schema verdict.json < rubric.md
```

Call mode uses an empty temporary cwd instead of resolving the current repo, and
it deletes that cwd after the child exits. It does not create snapshots, inject
safe/work skill or completion-report framing, emit progress heartbeats, or honor
persistent worktree/commit policy options. JSON
output returns fields such as `ok`, `status`, `exitCode`, `engine`, `mode`,
`model`, `pure`, `structuredOutput`, `modelRequested`, `modelResolved`, `usage`,
`text`, `textChars`, `textTruncated`, `stdoutBytes`, `stderrBytes`, reasoning
metadata, and `warnings`. Failed calls include a redacted `stderrTail`.
`textTruncated` is `true` when the returned
`text` was bounded (large outputs keep the head and tail); `textChars` is the
full untruncated character count.

**Default call is work-level.** A bare `call` grants the child the same
capability as `work` (it can write files, run commands, and use the network in
the temporary cwd) — it just skips the git/worktree ceremony. It is **not** a
security sandbox: the harness is not confined to the temp cwd, so treat a
default call like a `work` run and only give it prompts you trust.

**`--read-only` is the stateless judge/completion contract.** It drops the child
to read-only capability (matching each engine's `safe`-mode restriction) and
prepends a short evaluator preamble telling the model there is no repository to
inspect — which stops non-Codex engines from derailing into "let me inspect the
changed files…" on a repo-flavored prompt. Pair it with `--output-schema` (Codex)
for structured verdicts. Use `--read-only` for any LLM-as-judge, grader, or
oracle use where the text is the product and the model must not act.

**`--pure` is the hostile-content completion boundary.** It is supported by
Claude only; other engines fail before launch with `unsupported_pure_call`. Pure
mode sends the prompt verbatim, drops ambient
environment variables outside the documented allowlist, and cannot be combined
with `--read-only`. Claude uses `--safe-mode`, disables every tool, ignores MCP
and ambient customization, disables session persistence, and receives the prompt
only on stdin. Use `--output-schema FILE` with Claude (schema contents inline)
or with ordinary Codex call mode (schema path).

`--timeout SECONDS` is a positive integer accepted in every mode. It bounds
calls and tracked `safe`/`work` runs alike: on expiry Delegate terminates the
whole child process group and returns `call_timeout` (a historical error-code
name kept for API stability) with exit code 1. `--timeout` is not supported
with `--pass-through`, which streams the child without a tracked deadline.

`--read-only` and `--pure` apply only to `call`; passing them with
`safe`/`work` is rejected. Empty-result retry applies to safe runs and read-only
calls when the prompt can be safely extended; write-capable, pure, and verbatim
calls are not retried. Codex thread-loss recovery follows the same safety
boundary: `call --read-only` may retry, but a write-capable call returns
`codex_thread_lost` after its first attempt because external side effects cannot
be proven absent.
Because the child call is stateless, `--isolation`, `--pass-through`,
`--progress`, `--forbid-commit`, and markdown completion reports are rejected.
An ordinary call also rejects `--cwd`.

Use `delegate --cwd /path/to/workspace <engine> work ...` for file deliverables;
`call` is intended for text results. If a child nevertheless creates files in the call cwd,
Delegate moves the cwd to `.delegate/artifacts/<runId>/` at teardown and lists
the retained files in `preservedArtifacts`. The artifacts directory has manual,
bounded-by-operator retention; Delegate does not run artifact GC.

**Grouped calls preserve workflow tracking without exposing the workspace to the
child.** `--group NAME` registers the call in the invocation workspace so
workflow kill/adopt and group selectors can find it. In that one combination,
`--cwd PATH` may select the registry/config workspace:

```bash
delegate --cwd /path/to/project --group wf_0123abcdef45 codex call "Summarize this input."
```

The child still executes in an empty temporary cwd; `--cwd` does not give it the
project tree or leak it through Delegate root metadata. Dry-run accepts the same combination for faithful planning but
creates no run entry. Use `safe` or `work` when the child should see the project
tree.

### Dry-run

```bash
delegate --json dry-run codex safe --reasoning-effort high "Review only."
delegate --json dry-run codex call "Summarize this prompt."
delegate --json dry-run claude safe --reasoning-effort high "Review only."
delegate --json dry-run grok safe --reasoning-effort high "Review only."
delegate --json dry-run cursor work --prompt-file task.md
delegate --json dry-run codex work --mail-push "Run with opt-in stop-hook mail push."
delegate --json dry-run droid reviewer safe "Investigate only."  # needs a configured 'reviewer' alias
```

Dry-run builds the request and child argv but does not launch a child runtime, create a registry run, create a branch, or create a worktree. It does not require the real child binary. It does validate config shape and model aliases, so the Droid example above only succeeds once `reviewer` maps to a real model ID — the shipped `config.example.json` uses `replace-with-` placeholders that dry-run rejects with `unconfigured_model`. For temporary safe isolation, the dry-run argv is the planned command shape and may still show the source workspace because the throwaway copy is not materialized until a real run — and safe mode's working-tree sync (uncommitted tracked edits and untracked, non-ignored files mirrored into the isolated copy; only gitignored paths excluded) happens only on a real launch, not in dry-run.

Typical dry-run JSON fields:

```json
{
  "ok": true,
  "dryRun": true,
  "engine": "codex",
  "mode": "safe",
  "model": null,
  "cwd": "/path/to/workspace",
  "workspaceKind": "git",
  "promptTransport": "stdin",
  "argv": ["codex", "--ask-for-approval", "never", "exec", "..."],
  "requestedReasoningEffort": "high",
  "resolvedReasoningEffort": "high",
  "reasoningEffortSource": "cli",
  "reasoningCapabilitySource": "bundled",
  "reasoningTransport": "codex-config",
  "isolatedWorkspace": true,
  "isolationMode": "auto",
  "effectiveIsolation": "worktree",
  "isolationLifecycle": "temporary",
  "isolation": "worktree temporary",
  "preservedWorkspace": false
}
```

`isolation` is a human-readable summary combining `effectiveIsolation` and `isolationLifecycle` (e.g. `"worktree temporary"`, `"worktree persistent"`, `"none"`). Depend on the structured fields rather than parsing it. A push dry-run also includes `mailPushAdapter` with the audit status and the internal hook command; unverified harnesses include the pull-degradation warning.

When a profile is active, dry-run and completion payloads add `authProfile` (the resolved profile name) and, when a Codex fallback profile is configured, `fallbackProfile`. Dry-run also adds `profileEnv` (the injected env map, with values redacted). These keys are omitted when no profile is active.

For Cursor, Claude, Grok, OpenCode, Pi, Oh My Pi, Droid, and Kimi safe mode, an explicit `--isolation none`
is normalized to `auto` with a warning because those safe contracts depend on
the temporary workspace/config boundary. Codex safe can use `none` because Codex
still runs with its read-only sandbox.

Persistent worktree dry-runs may also include `plannedBranch` and `plannedExecutionCwd`; those are plans, not created resources. Temporary safe dry-runs usually keep `plannedExecutionCwd` unset because no temporary worktree or directory copy has been created.

### Profiles

```bash
delegate profiles
delegate --json --auth-profile work profiles
```

`delegate profiles` is read-only introspection for the top-level `profiles`
auth/env system. It reports the detected active profile, its source
(`flag`, the matching detection variable name, or `default`), and the resolved
env keys. JSON output is pinned to a small shape:

```json
{
  "ok": true,
  "profile": "work",
  "source": "flag",
  "envKeys": ["CODEX_HOME"],
  "env": {"CODEX_HOME": "/redacted-or-expanded/path"},
  "warnings": [],
  "configSource": "/path/to/config.json"
}
```

Values in `env` are routed through the same key-aware redaction used for child
environment diagnostics. Profile env maps are for routing pointers, not
secrets; secret-like keys in `profiles.definitions.*.env` are rejected during
config validation.

### Setup

```bash
delegate setup
delegate --json setup
delegate --json --auth-profile work setup
```

`delegate setup` is the explicit first-run discovery command. It fingerprints
every supported harness it can find, runs bounded metadata probes, and updates
the active auth profile's private discovery cache. It does not install harnesses,
authenticate accounts, edit harness-native config, or submit a model prompt.
`--auth-profile` must name an existing `profiles.definitions` entry; setup does
not create profile definitions.

When the resolved Delegate config does not exist, setup creates a minimal
owner-only JSON file containing safe absolute selectors for the harnesses it
found. It does not invent aliases or model defaults. An existing config is
validated and preserved byte-for-byte while discovery still refreshes. If the
config changes concurrently, setup preserves the other writer's file and asks
the caller to rerun rather than combining old probe results with new config.

The JSON result separates `discoveryReady` (at least one successful metadata
record) from `ready` (at least one harness can launch under the effective
config). Per-harness records include `installed`, `probeStatus`, `catalogCount`,
`reasoningStatus`, `launchable`, `stale`, and `nextAction`. For example, Droid
can be discovered but remain unlaunchable until a model is passed or
`droid.defaultModel` is configured.

### Config init

```bash
delegate config init
delegate --json config init --force
delegate config sync-profiles
```

`delegate config init` writes an editable starter config to
`~/.delegate/config.json`, or to `DELEGATE_CONFIG` when set. It refuses to
overwrite an existing file unless `--force` is passed. It also writes missing
`config.work.json` and `config.personal.json` profile overlays next to the base
config.

`delegate config sync-profiles` reads the base config and creates any missing
profile overlays without overwriting existing ones. `config` is itself a
mutation command, so if `AI_PROFILE=work|personal` is set and the matching
overlay is missing, the profile guard (enforced in the CLI itself, and again
by `bin/delegate-profile-shim` if that's in front of it) blocks it too -- run
it as `env -u AI_PROFILE delegate config sync-profiles`.

### JSON input

```bash
delegate --json run --input-json examples/task.codex.json
```

The shipped example files use a placeholder `cwd` (`/path/to/workspace`); copy one and set a real `cwd` first, otherwise the run fails with `invalid_cwd`.

Supported input keys:

```json
{
  "engine": "codex",
  "mode": "work",
  "model": null,
  "cwd": "/path/to/workspace",
  "isolation": "worktree",
  "reasoningEffort": "high",
  "fast": false,
  "progress": true,
  "forbidCommit": true,
  "includeDirty": true,
  "timeout": 30,
  "outputSchema": "/path/to/schema.json",
  "prompt": "Implement the scoped task and report changed files."
}
```

- `engine`: `cursor`, `droid`, `codex`, `claude`, `grok`, `devin`, `opencode`, `pi`, `omp`, or `kimi`.
- `mode`: `safe`, `work`, or `call`.
- The `devin` engine rejects `safe` with `unsupported_mode` during preflight. Devin filesystem surveys may require generic `exec`, which Delegate cannot allow without weakening the read-only boundary; use another safe Harness for filesystem review.
- `model`: optional alias-or-id for every engine. Resolved against `<engine>.models` when it matches an alias; otherwise passed through as a raw model ID. For Droid, a positional alias remains alias-only/strict; JSON/`--model` is alias-or-id. Cursor honors an explicit model even when it differs from `cursor.defaultModel`.
- `cwd`: optional workspace path. Git directories resolve to the repo root. Omit it for `mode: "call"`, which always uses an empty temporary cwd.
- `isolation`: optional `auto`, `none`, or `worktree`. `null` is invalid. `mode: "call"` rejects isolation. For Cursor, Claude, Grok, OpenCode, Pi, Oh My Pi, Droid, and Kimi safe mode, `none` is normalized to `auto` with a warning.
- `reasoningEffort`: optional non-empty effort string. It overrides provider `defaultReasoningEffort` for that JSON run.
- `fast`: optional Codex-only boolean or `null`. `true` requests Fast, `false` explicitly requests Standard, and `null`/omission inherits Codex configuration.
- `progress`: optional boolean. `true` enables parent progress heartbeats on stderr; `false` disables them even when `progress.enabled` is true in config. When omitted, config `progress.enabled` applies (default `false`). `mode: "call"` rejects progress.
- `forbidCommit`: optional boolean. `true` requires `mode: "work"` with persistent worktree isolation and fails the run if the child creates commits. `mode: "call"` rejects commit policy.
- `includeDirty`: optional boolean. `true` requires `mode: "work"` with persistent worktree isolation and syncs tracked edits plus untracked non-ignored files into the new worktree before launch.
- `timeout`: optional positive integer seconds, with the same semantics as `--timeout`. Non-integer, boolean, or non-positive values fail with `invalid_timeout`; combining it with pass-through is rejected.
- `outputSchema`: optional path to a JSON Schema for the final message. Supported for Codex and Claude call mode (same semantics as `--output-schema`). Other engines fail with `unsupported_output_schema`.
- `prompt`: required task prompt.

`profile` is not accepted in run input JSON. Configure the Codex CLI config
overlay in `codex.profile` instead. That is separate from the top-level
`profiles` block, which Delegate resolves once per request and injects as
auth/env. Use the global `--auth-profile NAME` with `run --input-json` to
override ambient profile detection for that run.

### Discovery

```bash
delegate --json setup
delegate --json describe --summary
delegate --json models --summary
delegate --json describe
delegate --json models
delegate --json models <engine>
delegate --json models <engine> --live
delegate --json capabilities
delegate --json capabilities refresh
delegate --json capabilities refresh <engine> [...]
delegate agent-help
```

`describe` reports version, engines, modes, supported isolation values, prompt
transforms, effective policy, top-level profile config metadata, representative
argv shapes, and a command catalog. Full `describe` is a strict superset of
`describe --summary`, so fields present in summary keep the same names in the
full payload.

`models` reports configured settings and cached discovery for Cursor, Droid,
Codex, Kimi, Claude, Grok, Devin, OpenCode, Pi, and Oh My Pi. A per-engine
catalog merges sources in this order: explicit config, current live or
profile-scoped discovery, the legacy workspace reasoning cache, then bundled
advisory IDs. `models <engine> --live` runs one fresh probe in the selected
profile environment, but does not update Delegate config or cache. Use setup or
`capabilities refresh` when ordinary launches should consume the new record.
Claude is the only harness without a non-interactive live model catalog.

Automatic setup and refresh use these evidence sources:

| Harness | Discovered model scope | Reasoning evidence |
| --- | --- | --- |
| Cursor | Models visible to the active account | Only corroborated catalog routes are recorded as `inferred-route`; explicit config mappings stay authoritative. |
| Droid | Built-in help plus harness-native custom-model settings | Per-model help details are `exact`; missing or ambiguous details remain unknown. |
| Codex | Models visible to the active account | Per-model effort menus from `debug models` are `exact`. |
| Kimi | Models in the active Kimi configuration | Provider metadata may advertise efforts, but Delegate has no Kimi effort transport and still rejects `--reasoning-effort`. |
| Claude | No model enumeration | The native effort enum is harness-wide evidence. |
| Grok | Models visible to the active account | Exact config, discovery, or bundled declarations win; otherwise the native effort enum is harness-wide compatibility evidence and model-specific support remains unknown. |
| Devin | No prompt-free model enumeration | Automatic discovery records only installation/version; Delegate has no Devin effort transport. |
| OpenCode | Harness catalog | Listed variants are `exact` for that model; models without exact variant evidence retain the documented unvalidated pass-through path. |
| Pi | Harness catalog | Thinking support is model-specific, but available labels come from a harness-wide enum and are reported as partial model evidence. |
| Oh My Pi | Harness catalog | A model's explicit `thinking` array is exact; missing arrays use harness-partial compatibility rather than fabricated model evidence. |

`modelScope`/`catalogScope` describes where a list came from, not whether a real
launch is authenticated: `account` is the active account's visible menu,
`configured` is harness-native configuration, `catalog` is a CLI catalog, and
`unknown` means no reliable model list was exposed. Setup does not validate
credentials online beyond whatever the metadata command itself requires.

Reasoning resolution keeps evidence strength explicit. `exact` binds effort
labels to one model selector. `inferred-route` is a Cursor model-selection route
corroborated by the catalog. Harness-wide or model-partial evidence proves a
transport label but not exact per-model support. `unknown` proves neither. An
exact empty menu is authoritative and rejects effort instead of falling through
to a bundled guess.

For a launch, an explicit CLI/JSON model or configured alias/default remains the
model choice. Exact capability precedence is manual config, profile discovery,
the read-only legacy workspace cache, then bundled fallback. Harness-specific
compatibility paths apply only when exact evidence is absent. A discovered
native default may validate capability metadata without forcing a `--model`
argument, so the child harness can still choose its own default.

Discovery is scoped to the resolved auth profile. The implicit profile uses
`~/.delegate/cache/discovery/default.json`; named profiles use separate hashed
files in the same owner-only directory. A refresh replaces successful harness
records independently. If one current probe fails, its last-known-good record
is retained and the refresh response lists that harness in `staleHarnesses`.
If the configured executable selector changes, cached reads mark that harness
stale and ordinary launches ignore only that harness's record until refresh.
Cached reads never run `--version` merely to test staleness, and neither does an
ordinary launch: a cached record can only be short of what the harness now
supports, never claim more, so one that satisfies the run is used as-is.

A launch re-probes `--version` for its own harness only when the cached record
has already cost the run something: it refused an explicit `--reasoning-effort`,
or it silently dropped a configured `defaultReasoningEffort` (a configured
default degrades to a warning instead of failing the launch, so nothing else
would signal it). An in-place upgrade behind an unchanged selector is the reason
either can be wrong, so the probe compares the live banner against the version
stored in the record, and a mismatch drops the record and rebuilds the run
against config and bundled capabilities. If the refreshed picture cannot satisfy
a configured default either, the run proceeds as it already had — a probe never
turns a launch that succeeded into one that fails.

The reverse case is not covered: a harness that *removes* a capability leaves
its record listing more than the harness now supports, which produces no refusal
and therefore no probe. Run `capabilities refresh` after an upgrade that narrows
what a harness accepts. The result carries a warning naming
`capabilities refresh <engine>`, because the superseded record stays on disk
until a refresh rewrites it. The probe fails open — an error, a timeout, or an
unrecognized banner leaves the original refusal standing — except when the
banner positively names a *different* known harness, which fails the run with
`harness_identity_mismatch` rather than handing one harness's argv and sandbox
policy to another. Dry runs never probe at all.

All automatic probes run with `shell=False`, stdin closed, a neutral temporary
working directory, bounded output, and a timeout. They pass the active profile
environment but do not carry a task prompt. Devin is the explicit exception on
the opt-in one-off surface: `models devin --live` uses a bounded invalid-model,
prompt-like invocation to make Devin print its available-model error. Setup and
`capabilities refresh` never use that Devin invocation; they run `--version`
only.

Discovery output applies best-effort credential scrubbing, so secret-shaped
model IDs or diagnostic values may appear as `***`. Exact values should come
from the private config or cache, not a scrubbed public projection. Agents
should start with `models --summary`, then request one per-engine catalog.

Both `describe` and `models` include provenance fields useful for detecting installed-runtime drift:

- `runtime.version`, `runtime.modulePath`, `runtime.packageRoot`, `runtime.executable`, and `runtime.pythonExecutable`.
- `configResolution.source`, `configResolution.effectiveConfigPath`, and ordered `configResolution.layers` showing embedded, user, workspace, and `DELEGATE_CONFIG` layers when discoverable.

`capabilities` reads config, the selected profile's user cache, the legacy
workspace cache, and bundled fallbacks without invoking child binaries.
`capabilities refresh` probes all supported harnesses and atomically writes the
selected profile's user cache when at least one installed harness returns a
valid record. `capabilities refresh <engine> [...]` probes only the named
harnesses — useful when one harness ships a new model — while every other
harness keeps its last-known-good record. It no longer writes `.delegate/capabilities/reasoning.json`; that
workspace file remains a lower-precedence, read-only compatibility source and
should not be committed.

### Help and discovery

Every command and subcommand supports `--help` (and the `-h` alias). It prints focused help for that command path and exits 0:

```bash
delegate cursor --help
delegate cursor safe --help
delegate droid --help
delegate worktree remove --help
```

`delegate help` accepts the same paths positionally. With no arguments it prints the overview:

```bash
delegate help
delegate help worktree remove
delegate help cursor safe
```

For agents, add `--json` to get a machine-readable spec instead of prose. This is the recommended way to learn how to invoke a command without trial and error. The two forms are equivalent:

```bash
delegate --json cursor --help
delegate --json help worktree remove
```

The JSON spec uses these keys:

```json
{
  "ok": true,
  "command": "worktree remove",
  "summary": "Remove one persistent worktree and, by default, its branch.",
  "usage": ["delegate [--cwd PATH] [--json] worktree remove <handle> [--discard-uncommitted] [--force-branch] [--force] [--keep-branch]"],
  "arguments": [{"name": "<handle>", "required": true, "description": "Worktree handle to remove."}],
  "options": [{"flag": "--keep-branch", "argument": null, "description": "Remove the worktree but keep its branch."}],
  "examples": ["delegate worktree remove cursor-1"],
  "notes": ["A --help token anywhere in the args prints help and removes nothing."],
  "seeAlso": ["worktree list", "worktree prune", "worktree gc"]
}
```

The overview JSON (`delegate --json help`) returns `{ok, commands, globalOptions}`, where `commands` is the same `{command, summary}` catalog that `describe` includes.

A `--help` token triggers help only before any prompt free-text is consumed, so help works without supplying a mode, alias, or required argument (`delegate cursor --help`, `delegate droid --help`, `delegate run --help`). Once prompt capture begins, a later `--help` is prompt text: `delegate cursor work explain --help` parses as a run whose prompt is `explain --help`. To send a literal prompt that begins with `--help`, pass it through `--prompt-file` or stdin rather than as a trailing argument.

For worktree actions, a `--help` token anywhere in the args wins and performs no action — `delegate worktree remove cursor --help` prints help and removes nothing.

### Scratch directory for isolated runs

Tracked safe-mode and isolated runs get a per-run scratch directory at
`.delegate/runs/<run-id>/scratch`. Delegate exports `TMPDIR`, `TMP`, and `TEMP`
to that path after applying profile env overrides, so the scratch directory wins
over profile-provided temp variables. The scratch directory persists with the
run directory for inspection and is cleaned up only when the run directory is
cleaned up.

For Codex safe/isolated runs, Delegate also passes `--add-dir <scratch>` along
with `--sandbox read-only`. Verified live 2026-07-04: the Codex read-only sandbox
still denies scratch writes despite `--add-dir` ("operation not permitted"), so
Codex safe children currently cannot use the scratch TMPDIR; the flag is kept so
scratch access starts working automatically if a future Codex honors it. Prompts
for Codex safe runs should not depend on temp-file writes. The scratch export is
verified working on the copy-isolated lanes (Cursor, Droid, Kimi, Grok, Claude).
Cursor, Droid, Kimi, Grok, and Claude receive the scratch path through the temp
environment variables only. This does not change the isolation semantics of the
repo copy or persistent worktree itself.

### Run registry inspection

Tracked runs return bounded parent-facing output and store local metadata under `.delegate/` in the source workspace.

```bash
delegate runs [--active|--running|--stale|--recent] [--harness HARNESS] [--group NAME] [--limit N] [--structural]
delegate runs prune [--older-than DAYS] [--dry-run]
delegate ps [--harness HARNESS] [--group NAME] [--limit N]
delegate snapshot [--latest HARNESS] [--no-redact] <handle>
delegate run-output [--latest HARNESS] <handle> [--completion-report] [--stdout] [--stderr] [--tail N] [--max-chars N] [--raw] [--no-redact]
delegate resume [--engine ENGINE] [--model MODEL] [--reasoning-effort LEVEL] [--fast|--no-fast] [--progress|--no-progress] [--timeout SEC] [--output-schema PATH|--no-output-schema] [--include-dirty] [--persona NAME|--no-persona] [--allow-repo-persona] [--mail-push] [--dry-run] <handle> [extra instructions...]
delegate wait <handle>... [--latest HARNESS] [--group NAME] [--timeout SEC] [--interval SEC] [--completion-report]
delegate cancel <handle>...
```

`delegate resume` relaunches a terminal Run as a new Run. It accepts
`succeeded`, `failed`, `cancelled`, and stale Runs; an effectively running Run
is refused. A successful source requires extra instructions. The continuation
contains the original prompt, then a bounded prior-run Completion Report (or a
Snapshot digest), then the new instructions last. Prior-run content is framed
as untrusted data and is disclosed to the target Harness, including on a
cross-engine resume. Resume options must appear before the handle; trailing
tokens are continuation instructions.

The new Run inherits source settings according to this table. `--engine` and
the listed resume flags must precede the handle; `--group` and
`--auth-profile` are global flags and therefore precede `resume`. A persistent
worktree source is resumed by attaching to its existing worktree rather than
creating a second one. The attachment is a live lease: `worktree remove`,
`worktree prune`, and `worktree gc` refuse or skip the worktree while the
attached Run may still be active. `--dry-run` resolves the continuation without
creating a Run or writing a prompt record.

| Field | Source manifest key | Override flag | Absent or legacy behavior | Cross-engine rule |
| --- | --- | --- | --- | --- |
| Engine | `engine`, falling back to `harness` | `--engine` | A missing or unknown source engine refuses resume. | The selected engine becomes the target; engine-scoped fields below may drop. |
| Mode | `mode` | None; v1 does not override mode. | A missing or invalid mode refuses resume. | Retained unchanged. |
| Model selection | `modelAlias`, then `modelRequested`, `modelResolved`, then `model` | `--model` | No usable key uses the target engine configuration default and emits a note. | Source model selection drops; pass `--model` to pin a target model. |
| Reasoning effort | `requestedReasoningEffort`, then `resolvedReasoningEffort`, together with `reasoningEffortSource` | `--reasoning-effort` | Missing effort uses the target default and emits a note. A source value from configuration is re-resolved through target capability/configuration; only `cli` and `input-json` source intent is inherited directly. | Source effort drops with a note; an explicit override remains valid. |
| Fast tier | `requestedFast` | `--fast` or `--no-fast` | Omitted leaves fast unspecified. | Inherited only for a same-engine Codex resume. Other targets drop it; non-Codex targets emit a drop note when a source value is present. |
| Progress intent | `progressRequested` (`on`, `off`, or omitted) | `--progress` or `--no-progress` | Omitted uses target progress configuration and emits a note; only `on` and `off` are inherited. | Retained; progress intent is not engine-specific. |
| Timeout | `timeoutSeconds` | `--timeout` | Omitted uses the target timeout default and emits a note. A present value must be a positive integer (an integral JSON float is accepted). | Retained. |
| Output schema | Inline `outputSchema` text | `--output-schema PATH` or `--no-output-schema` | Omitted means no schema. Stored text is re-materialized privately only during normal launch. | Only Codex inherits inline schema text. Other engines soft-drop it with a note before output-schema validation. |
| OpenCode agent | `agent` | None | Omitted means no agent selection. | Inherited only for a same-engine OpenCode resume; otherwise dropped with a note. |
| Workspace cwd / execution cwd | `cwd`; persistent records also derive `executionCwd`, branch, and source Git root | None (`--cwd` selects the registry workspace and must match recorded `cwd`) | Missing `cwd` falls back to the selected registry workspace; missing persistent-worktree metadata causes attachment validation to refuse. | Retained as workspace context; an attach uses the validated existing worktree rather than copying `executionCwd` into a new worktree. |
| Isolation / lifecycle | `isolationMode`; persistent detection also reads `isolationLifecycle`, `preservedWorkspace`, `worktreeStatus`, and `worktreeAttachment` | None; `--isolation` is rejected for resume. | Missing ordinary `isolationMode` uses target isolation configuration and emits a note. | Retained. Persistent or attached sources become an `attached` execution lifecycle. |
| Group | `group` | Global `--group` | Omitted uses the ungrouped target default and emits a note. | Retained. |
| Auth profile | `authProfile` | Global `--auth-profile` | Omitted uses target profile detection/default and emits a note. | Retained. |
| Forbid commit | `commitPolicy.forbidCommit` when exactly `true` | None | Omitted or any other value leaves commit prohibition off. | Retained. |
| Include dirty | `includeDirty` | `--include-dirty` | Source and explicit values are creation-only and are dropped for ordinary resume with a note. | Retained only as a drop decision; on a persistent/attached source, explicit `--include-dirty` is rejected because attachment does not create or sync a worktree. |

`delegate runs` defaults to recent runs. `--active` preserves the legacy active view and includes both live `running` runs and `stale` runs. Use `--running` for only live tracked processes and `--stale` for runs recorded as running whose PID is missing or dead. `--active`, `--running`, `--stale`, and `--recent` are mutually exclusive. `--group NAME` filters by launch group and the runs table shows a `group` column when any visible run has one.
`--structural` omits content-bearing run fields and emits only identity, lifecycle, model,
timestamp, group/mode, and `initiatorRoot` metadata. It is intended for local status collectors.
`delegate ps` is the shorter first-class form of `delegate runs --active` and
accepts the same harness, group, limit, and structural selectors.

`delegate runs prune` removes old terminal Run records from the workspace Registry so they stop accumulating forever. Only runs whose effective status is terminal (`succeeded`, `failed`, `cancelled`, or `stale` from a dead child) and whose last Registry activity is older than the threshold (default 30 days; override with `--older-than DAYS`) are eligible. Effectively running runs are always skipped, and persistent-worktree runs are skipped unless their worktree is recorded as removed or missing — pruning the record of a live worktree would orphan it from `worktree list`/`show`/`remove`. Pruning deletes the per-Run directory (Snapshot, Manifest, logs, events, Completion Report) and the retained raw-log archive; worktree paths on disk are never touched. `--dry-run` reports what would be removed without changing anything. JSON output uses schema `delegate.runs-prune.v1` with `planned`, `removed`, `skipped` (each entry carries a `reason`), and `errors` sections.

Run-scoped handles (`snapshot`, `run-output`, `wait`, and `cancel`)
resolve exact run IDs and numbered aliases first. A bare harness name such as
`codex` resolves to that harness's latest run, and `harness:modelAlias` (for
example `droid:glm`) resolves to the latest run for that harness/model alias.
Generated follow-up commands always use the concrete numbered alias.

v0.10.0 migration note: pre-v0.10 runs that were literally aliased with a bare
harness name (for example `codex`) are shadowed by the new latest-selector
semantics, because `delegate snapshot codex` now resolves to the latest `codex`
run rather than the literal first-run alias. Reach those legacy runs by run ID
(`delegate snapshot del_20260520T100000Z_abcdef`).

Common JSON fields for tracked run completion:

```json
{
  "ok": true,
  "exitCode": 0,
  "alias": "codex-1",
  "runId": "...",
  "harness": "codex",
  "engine": "codex",
  "mode": "safe",
  "model": null,
  "cwd": "/path/to/source",
  "executionCwd": "/path/to/execution-workspace",
  "workspaceKind": "git",
  "requestedReasoningEffort": "high",
  "resolvedReasoningEffort": "high",
  "reasoningEffortSource": "cli",
  "reasoningCapabilitySource": "bundled",
  "reasoningTransport": "codex-config",
  "isolatedWorkspace": true,
  "isolationMode": "auto",
  "effectiveIsolation": "worktree",
  "isolationLifecycle": "temporary",
  "preservedWorkspace": false,
  "progressRequested": false,
  "assistantText": "final assistant text when recoverable",
  "resultQuality": "ok",
  "completionReportWritten": true,
  "completionReportSource": "child",
  "assistantTextChars": 37,
  "assistantTextTruncated": false,
  "snapshotCommand": "delegate snapshot codex-1",
  "completionReportCommand": "delegate run-output codex-1 --completion-report"
}
```

Persistent worktree completions also include `branch`, `worktree`, a
`workSummary`, and (when requested) `commitPolicy`. `workSummary` reports dirty
state, changed file count, diff stat, and commits created by the child. When a
Codex usage-limit fallback fires, the completion payload also includes
`codexAuthFallback` metadata (reason, the primary and fallback profile names,
both exit codes, and a redacted primary stderr tail). The fallback is Codex-only
and is enabled only by `codex.fallbackProfile`; Delegate remembers temporary
blocks by hashed credential namespace (`CODEX_HOME/auth.json` plus
`codex.profile`) as canonical state. Default work/personal credential homes
also mirror compatible legacy alias keys so existing launchers share blocks;
remapped aliases remain isolated.

Snapshot JSON uses schema `delegate.snapshot.v1` and includes fields such as `alias`, `runId`, `harness`, `status`, `rawStatus`, `effectiveStatus`, `staleReason`, `nextActions`, `cwd`, `executionCwd`, `workspaceRoot`, `assistantText`, `recentEvents`, `warnings`, `exitCode`, reasoning metadata, terminal metadata, and isolation/worktree metadata when applicable. `workspaceRoot` is also exported to the child as `WORKSPACE_ROOT`, so commands can anchor workspace-relative paths after changing directories. Inspection commands do not rewrite a stale run's recorded state; they expose the raw recorded status plus the effective status computed from the current PID check. Run-output and worktree show output include `requestedHandle`, `resolvedHandle`, and `resolutionKind` (`literal`, `latest`, or `latest_model`) when a handle resolves indirectly. For bare harness handles, snapshot, run-output, and wait also report `resolvedRunId`, `resolvedAlias`, `resolvedWorkspace`, `resolvedAge`, and `resolvedAgeSeconds`. Resolutions older than 24 hours add a `bare_handle_stale` warning suggesting `--cwd` or an explicit handle.

Tracked run envelopes include `completionReportWritten`, `completionReportSource`
(`child`, `delegate_synthesized`, `stdout_recovery`, or `null`), and
`resultQuality` (`ok`, `housekeeping_noop`, `empty`, `suspect_short`, or
`no_assistant_text`). Non-`ok` quality adds a warning rather than changing
exit-code-derived status.

Failed runs classify recognized child output as `usage_limit`, `auth_failed`,
or `codex_thread_lost`; the envelope, persisted effective-status views, and
synthesized completion report include the code and a human message. Usage-limit
messages retain a reset time when the child supplied one. Unknown failures use
`child_failed`, and raw stdout/stderr remain available through `run-output`.
Codex thread loss alone activates a narrow escalation ladder: retry once, then
use an ephemeral `--ignore-user-config` attempt if the same failure repeats.
Work-mode attempts are never replayed after tool activity or a workspace change.
`codexThreadFallback` and an event/warning disclose when that fallback engaged.

Run-output JSON uses schema `delegate.run-output.v1` and returns selected completion report, stdout, and/or stderr content. By default, secret-like strings are redacted unless `--no-redact` is supplied. Tracked runs finish in one of the terminal statuses `succeeded`, `failed`, or `cancelled`; explicit harness cancellation/error terminal events override an exit-zero child status.

Tracked stdout and stderr logs are capped independently at 16 MiB. Exceeding a
cap terminates the child and records `output_limit_exceeded` plus an
`outputLimit` object naming the stream and byte limit. After an explicit
terminal-success event, Delegate gives the harness one second to exit and then
stops a lingering process; successful envelopes disclose this as
`stoppedAfterCompletion: true`.

Safe runs and read-only call runs that exit successfully with `resultQuality=empty`
retry once with the original prompt plus a plain-text final-answer instruction.
Pure and slash pass-through prompts, and write-capable calls, do not retry. Retried
envelopes add `emptyRetry: {attempted, resolved}`; runs that do not retry omit the
field. If the second attempt is also empty, the run remains successful and honest
with `resultQuality=empty`, and emits an
`empty_success_retry` warning. Tracked safe runs retain both attempts in their
stdout/stderr logs.

`delegate wait` blocks until every selected run reaches terminal state. Defaults:
`--interval 3`, minimum interval `1`, and `--timeout 3600`. Exit codes are `0`
when all runs succeeded, `1` when any run failed or was cancelled, and `124` on
timeout. JSON uses schema `delegate.wait.v1` with `timedOut` and per-run merged
snapshot/state envelopes. Effective status is used: a recorded running child
whose pid is dead is reported as terminal failure with `staleReason: dead_pid`,
not left to hang until timeout. Text mode prints one compact line when each run's
status changes, then a final table; `--completion-report` appends each run's
report after the table. The `--timeout` is a floor, not an exact bound: a run
that reaches terminal state just past the deadline can be observed terminal up
to one `--interval` later, because the polling loop checks liveness before
testing the deadline. `--group NAME` waits for all runs tagged with that group.

`delegate cancel` resolves the same run handles, refuses already-terminal runs,
signals the recorded process group with SIGTERM, waits a 5s grace period, then
uses SIGKILL if needed. It never signals pid/pgid `<= 1`. Legacy runs without a
recorded pgid fall back to the recorded pid with a warning. Cancel marks the run
`cancelled` with `failureReason: cancelled_by_user` and records current captured
stdout/stderr byte counts. Ungrouped `call` mode is untracked; grouped calls are
registered and can be selected for cancellation.

Before sending any signal, cancel stamps a `cancelRequested: true` marker (with
a `cancelRequestedAt` timestamp) on the run state under the registry lock, so
that a runner finalizer observing the marker persists `cancelled` even if the
child exits 0 on SIGTERM and finalizes before cancel's post-grace terminal
write. This keeps the synchronous launcher envelope (`ok`/`status`/`exitCode`),
the persisted state, and the eventual reconciled registry entry in agreement
regardless of which side finishes first. The marker is never stamped on an
already-terminal run.

A cancelled run has no child completion report (the child was killed mid-flight),
so Delegate synthesizes one with `completionReportSource: delegate_synthesized`.
The synthesized cancelled report records `Status: cancelled`, the failure reason
(`cancelled_by_user` for an operator cancel, `harness_cancelled` for a harness
terminal cancellation event), a bounded redacted stderr tail when present, and a
next action pointing at `run-output <alias>` for partial output. It is readable
via `run-output <handle> --completion-report` the same way a failed run's
synthesized report is.

With no selector, `run-output` prints the best available parent-facing output:
`completion-report.md` when present, a recovered final assistant message when
possible, otherwise bounded stdout/stderr tails plus diagnostics. Explicit
selectors are preserved. `--stdout` or `--stderr` without `--tail` or `--raw`
defaults to a bounded `--tail 80` and a character cap (default 60000); use
`--max-chars N` to override the cap. `--raw` returns the full stream with no
line or character bounds, includes `rawOutputBytes` in JSON metadata, and cannot
be combined with `--tail` or `--max-chars`. A bare `--tail N` implies
`--stdout` when no output selector is supplied; it never includes stderr.
`--max-chars` still requires `--stdout` or `--stderr`, and Delegate rejects
either bound when only `--completion-report` is selected.

When `completion-report.md` is absent, `run-output --completion-report` makes a
bounded best-effort attempt to recover an explicit final response from the
recorded child stdout stream using the same event parser used during live
tracking. Codex recovery only promotes an `agent_message` after the stream
reaches `turn.completed`, so progress messages are not treated as final reports.
JSON output marks recovered reports with `synthetic: true` and
`source: "stdout.log"`; text output flags them in the section header
(`=== completionReport (synthetic: recovered from stdout.log tail) ===`), and
tailed log sections carry a `(last N lines; full log B bytes)` header cue.
Character-capped text sections also disclose `(last N chars; M omitted)` in text
headers, matching the JSON `charTruncated` metadata.
Synthetic recovery may fail when the stdout stream is
truncated, malformed, or lacks a completed final message. JSON failures for
explicit `--completion-report` include `diagnostics` (run status and stdout /
stderr presence and byte counts) plus `nextActions` with bounded fallback
commands before you read raw `.delegate/` files directly.

### Worktree management

```bash
delegate worktree list [--harness HARNESS] [--group NAME] [--status STATUS] [--limit N] [--no-auto-prune]
delegate worktree show <handle>
delegate worktree show --latest HARNESS
delegate worktree remove <handle|--group NAME> [--discard-uncommitted] [--force-branch] [--force] [--keep-branch]
delegate worktree prune [--merged] [--older-than DAYS] [--harness HARNESS] [--group NAME] [--include-detached] [--dry-run] [--discard-uncommitted] [--force-branch] [--force]
delegate worktree gc [--dry-run] [--all] [--pool PATH]
```

`worktree show --latest HARNESS` selects the latest persistent worktree for the harness, not merely the latest run overall. `worktree list` JSON includes a `summary` with status counts, registry drift counts, warning counts, `autoPruneMode`, and whether the returned operation was read-only; `summary.totalPersistentWorktrees` is always registry-wide, while `allStatusCounts` is scoped to the `--harness` / `--group` filters (pre-status-filter) and `statusCounts` to the visible entries. `worktree remove --group NAME` removes all matching persistent worktrees with the same safety checks as single-handle removal. `worktree prune --group NAME` limits prune candidates to the group. `worktree gc --dry-run` reports `wouldPruneSourceRoots` and structured orphan reasons; `worktree gc` JSON also includes `mode`, `effects`, per-entry `action`, and orphan `safeAction` fields. `gc` never deletes worktree directories.

`worktree gc --all` additionally scans the machine-wide worktree pool (`worktrees.dataHome`, default `~/.delegate/worktrees`) and adds a `pool` object with `dataHome`, `scannedWorktrees`, `orphans`, `emptyFingerprintDirs`, and `warnings`. A pool orphan is a pooled worktree whose Git admin directory no longer serves it, classified `source_root_missing` or `worktree_metadata_missing` with the same `safeAction` vocabulary as registry orphans, plus `worktreePath`, `fingerprint`, `sourceGitRoot`, and `gitdir` (null when the backlink is absent). This is the only way to see worktrees whose source repository was deleted along with its `.delegate/` registry, so `--all` is accepted outside a Delegate registry. The pool scan removes nothing at all: orphans are reported because uncommitted work in them is undetectable once the source repository is gone, and empty fingerprint directories are reported for the same hand-clearing rather than reclaimed. Only a settled absence produces an orphan — metadata that could not be read (a symlinked, non-regular, oversized, or unreadable `.git` entry, or an admin directory that could not be inspected) is treated as live and reported in `pool.warnings`, and so is any pool entry modified within the last 15 minutes, since worktree creation writes the directory before its metadata and a concurrent scan would otherwise call a worktree being born an orphan (`worktree_unsettled`, `fingerprint_dir_unsettled`). `pool.warnings` also covers first-level directories whose names are not repository fingerprints, fingerprint directories that hold loose files rather than being empty, and unlistable fingerprint directories. `--pool PATH` scans a specific pool root instead of the configured one, which finds pools left behind by an earlier `worktrees.dataHome`; an explicit `--pool` path that is missing, not a directory, or unreadable is an `invalid_pool_root` error.

List/show entry fields include `branchMergedIntoSource` (branch graph only), `mergedIntoSource` (backward-compatible branch graph state), `fullyIntegrated` (branch merged and worktree clean), `hasUncommittedChanges`, `integrationStatus`, and `uncommittedChangesIntegrated`. `workSummary` is included on `worktree show` and run completion payloads when Delegate can inspect the worktree; `worktree list` omits the deep summary for responsiveness. Consumers that need safe retirement should require `fullyIntegrated: true` or inspect `integrationStatus`. When `integrationStatus` is `branch-merged-worktree-dirty`, merge/cherry-pick suggestions are suppressed because commit integration is already complete and only uncommitted edits remain.

Unknown persistent-worktree handles return suggestions scoped to persistent
worktrees plus a `listCommand` hint (`delegate worktree list`). Run-output and
snapshot handle suggestions remain scoped to tracked runs.

v0.10.0 migration note: worktree commands (`worktree show/remove/prune`) accept
a bare harness name (resolves to the latest persistent worktree for that harness)
or a concrete alias/run ID only. They do not accept `harness:model` selectors,
unlike `snapshot`/`run-output` which do.

Worktree JSON schemas:

- `delegate.worktree-list.v1`
- `delegate.worktree-show.v1`
- `delegate.worktree-remove.v1`
- `delegate.worktree-prune.v1`
- `delegate.worktree-gc.v1`

Worktree management exits 0 only when top-level `ok` is true. Safety refusals return structured JSON with `ok: false`, `error`, `message`, `exitCode`, and often `nextActions`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Delegate command completed successfully. For child launches, child exit code was 0. |
| 2 | Usage, config, validation, or worktree-management safety failure. JSON mode emits `ok: false`. |
| 3 | Missing child binary for a real launch. Dry-run does not require the binary. |
| Child exit code | For tracked child launches, Delegate returns the child runtime's exit code and includes it in JSON as `exitCode`. |

JSON error payloads use this shape:

```json
{
  "ok": false,
  "error": "missing_binary",
  "message": "Missing binary: codex",
  "exitCode": 3
}
```

Fields may grow over time. Agent callers should check `ok`, `error`, `exitCode`, tracked-run fields such as `alias` and `runId`, call-mode fields such as `text`, and documented schema names rather than depending on object key order.
