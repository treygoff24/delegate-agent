# Kimi Code CLI compatibility audit

Latest version: **0.41.0** (2026-09-04, https://www.npmjs.com/package/@moonshot-ai/kimi-code and https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/release-notes/changelog.md) · Installed here: **not installed** (`command -v kimi` → exit 1; `~/.kimi-code` does not exist)

Delegate assumptions checked: 29 · BROKEN: 1 · LATENT: 6 · SIMPLIFY: 3 · OK: 19

## Method note

The binary is not installed on this machine, so no `kimi --help` or `--version` output exists locally.
Ground truth came from two primary sources: the vendor documentation in `MoonshotAI/kimi-code`
(`docs/en/**`, fetched raw from `main`), and **the published 0.41.0 npm tarball itself**, extracted to
`/var/tmp/kimi-probe/package/dist/main.mjs` (23 MB single-file bundle, `package.json` declares
`"bin": {"kimi": "dist/main.mjs"}`, `"engines": {"node": ">=22.19.0"}`). Every claim below marked
"bundle" is read off the shipped 0.41.0 implementation, not off documentation. Nothing was executed.

Two projects both install a binary named `kimi`: the legacy Python `MoonshotAI/kimi-cli` (being wound
down) and the current TypeScript `MoonshotAI/kimi-code`. Delegate targets kimi-code — `config.example.json`
pins `kimi-code/k3` and `docs/cli-reference.md:516` names `~/.kimi-code/config.toml` — so this audit
treats kimi-code 0.41.0 as the truth. Delegate's in-code comments still cite "kimi 0.26.0"
(`src/delegate_agent/harness_events.py:638`, `:829`); 15 minor releases have shipped since.

## BROKEN

- **[B1] The bwrap safe backend binds the wrong Kimi home, so a bwrapped Kimi run has no credentials, no
  config, and no managed `rg`/`fd`.** delegate: `src/delegate_agent/sandbox_bwrap.py:59` maps
  `"kimi": ".kimi"` in `_OPTIONAL_ENGINE_DOT_DIRS`, and `wrap_engine_argv`
  (`sandbox_bwrap.py:466-475`) only ro-binds that path when `os.path.isdir` succeeds. harness: Kimi Code
  CLI stores **all** runtime data under `~/.kimi-code/` — `config.toml`, `credentials/` (OAuth tokens,
  dir 0700), `sessions/`, and `bin/rg` + `bin/fd`, the managed binaries backing the `Grep` and file-reference
  tools (https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/data-locations.md). There is
  no `~/.kimi`; that was the legacy kimi-cli location. Delegate's own docs already know this
  (`docs/cli-reference.md:516`, `docs/configuration.md:526` both name `~/.kimi-code/config.toml`), so line 59
  is a stale leftover. Effect: under `isolation.safeBackend=bwrap`, nothing Kimi needs is bound, the child
  reaches `requireConfiguredModel` with no config and dies on
  `"No model configured. Run \`kimi\` and use /login to sign in, then retry; or set default_model in config.toml."`
  (bundle, `requireConfiguredModel`).
  Repro (no model call): `python3 -c "from delegate_agent import sandbox_bwrap as s; print(s._OPTIONAL_ENGINE_DOT_DIRS['kimi'])"` → `.kimi`; compare against the documented layout above.
  Smallest fix: change the value to `".kimi-code"`. Note the ro-bind is still only half a fix — Kimi writes
  `sessions/`, `logs/`, and `session_index.jsonl` into that same directory, so a read-only bind will produce
  write errors; the honest options are an rw-bind of `~/.kimi-code` or a `KIMI_CODE_HOME` redirect to the
  scratch dir (the env var is supported and moves *all* data:
  https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/data-locations.md), which would slot
  into `_ENGINE_HOME_ENV_VAR` alongside `CODEX_HOME` and `CLAUDE_CONFIG_DIR`.

## LATENT

- **[L1] Kimi's stream-json buffers the tool-call announcement until the tool has already finished, so the
  stall watchdog's "a tool is in flight" grace never applies to Kimi.** delegate:
  `src/delegate_agent/stall_watchdog.py:254` (`_classify_kimi_role_envelope`) emits `tools_started` from the
  `role:"assistant"` line's `tool_calls` and `tools_finished` from the `role:"tool"` line, and the module
  docstring (`stall_watchdog.py:26-29`) states "While a tool or command execution is in flight the run is never
  stalled". harness: in `PromptJsonWriter` (bundle), `writeToolCall` only appends to an in-memory `toolCalls`
  array; the assistant envelope carrying `tool_calls` is emitted by `flushAssistant()`, and `writeToolResult`
  calls `this.flushAssistant()` **immediately before** writing the tool line. The two lines therefore arrive
  back to back after the tool has completed, and stdout is silent for the entire tool execution.
  `writeThinkingDelta()` is an empty method in JSON mode and `tool.progress` goes to stderr, so a long thinking
  phase is silent too. With `STALL_MINUTES_DEFAULT = 8` (`stall_watchdog.py:50`), a Kimi run executing a build
  or test suite longer than eight minutes is cancelled as stalled. Repro: no live call available; read
  `PromptJsonWriter.writeToolResult` and `dispatchNativeEvent` in the extracted bundle. Smallest fix: treat a
  Kimi `role:"tool"` line as both start and finish of a zero-length window and rely on stage timeout instead,
  or raise the effective stall floor for `kimi`.
- **[L2] `delegate kimi work "/goal ..."` can exit 3 or 6 and delegate has no vocabulary for either.**
  delegate: slash pass-through is allowed for kimi in work mode (`constants.py:150` lists kimi in
  `PROMPT_ENFORCED_SAFE_ENGINES`, which blocks pass-through in **safe** mode only;
  `tests/test_slash_passthrough.py:112` asserts the work-mode prompt reaches argv verbatim). harness: the bundle's
  `parseHeadlessGoalCreate` intercepts a prompt matching `/^\/goal(\s|$)/` in print mode and runs it as a goal;
  `GOAL_EXIT_CODES = {complete: 0, blocked: 3, paused: 6}`, and a non-`complete` terminal status sets
  `process.exitCode = goalExitCode(status)`. It also writes one extra stdout line,
  `{"type":"goal.summary","goalId":…,"status":…,"reason":…,"turnsUsed":…,"tokensUsed":…,"wallClockMs":…}`,
  with **no** `role` key. Delegate's `_ingest_object` (`harness_events.py:540`) dispatches on `type`, finds no
  handler for `goal.summary`, and drops it — including the only turn/token accounting Kimi ever emits. Net
  effect: a paused goal reports as a plain non-zero failure with no reason. Smallest fix: map `goal.summary`
  into a completion/result event for kimi, or refuse `/goal` pass-through for kimi.
- **[L3] `--pass-through` Kimi runs can be flipped to stream-json by an ambient environment variable.**
  delegate: `build_kimi_argv` (`argv_builders.py:235-236`) omits `--output-format` entirely when
  `stream_capture` is false, assuming the default is `text`
  (`tests/test_kimi_commands.py:test_kimi_pass_through_argv`). harness: `resolveOutputFormat` (bundle) is
  `--output-format` → `KIMI_MODEL_OUTPUT_FORMAT` → `text`, and the env var is honoured **in prompt mode only**
  — which is exactly the delegate case. Documented at
  https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/env-vars.md. An invalid value is a
  hard `OptionConflictError` and exit 1. Smallest fix: emit `--output-format text` explicitly in the
  pass-through branch, which is unambiguous and costs one list append.
- **[L4] The model-discovery probe mutates the user's Kimi home and fails hard on a fresh install.**
  delegate: `_probe_kimi` (`harness_discovery.py:1379`) runs `kimi provider list --json`;
  `parse_kimi_catalog` (`:928`) raises `ValueError("Kimi catalog had no parseable models")` when the `models`
  object is empty. harness: `handleProviderList` (bundle) calls `await harness.ensureConfigFile()` before
  reading, so a read-shaped metadata probe **creates** `~/.kimi-code/config.toml` as a side effect. On a
  machine where nobody has run `/login` yet, `config.models` is `{}` and the probe result is a hard parse
  failure rather than "nothing configured". Also worth noting: the OAuth managed account itself does not appear
  in the `providers` table at all
  (https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/providers.md, warning block); only
  the `kimi-code/*` model aliases that `/login` provisions show up, and delegate reads `models` only, so that
  part is fine. Smallest fix: treat an empty `models` object as an empty catalog with a warning rather than a
  `ValueError`.
- **[L5] Print mode's default background policy makes `kimi -p` effectively unbounded.** harness:
  `[background] print_background_mode` defaults to `"steer"`, `print_wait_ceiling_s` to `2147483` (~68 years)
  and `print_max_turns` to `100000`; background `Bash` tasks default to `bash_task_timeout_s = 0` (no timeout)
  and subagents to `timeout_ms = 0` in print mode
  (https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/config-files.md, `background`
  section). The docs state plainly that in print mode "Background work is never killed by a wall-clock cap".
  Delegate has no Kimi-specific timeout and cannot pass one on argv, so the only backstops are delegate's
  `--timeout` and the stall watchdog, and L1 makes the latter unreliable in the opposite direction.
  Smallest fix: none in argv; document that a Kimi lane must carry an explicit `--timeout`.
- **[L6] Delegate's security wording for Kimi safe mode is now stronger than what Kimi enforces.** delegate:
  `docs/security-model.md:117` says "the isolated temporary workspace is the effective boundary and the safety
  prompt is advisory". harness: 0.40.0 shipped "Remove the workspace restriction on the Bash tool's cwd
  parameter", and 0.41.0 shipped "Auto permission mode no longer blocks dangerous commands and commands that
  cannot be statically analyzed"
  (https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/release-notes/changelog.md). Print mode always
  runs at `permission: "auto"` and `installHeadlessHandlers` sets an approval handler that returns
  `{decision: "approved"}` unconditionally (bundle). So at 0.41.0 a Kimi safe run can `Bash` its way to an
  absolute path outside the throwaway worktree, and the dangerous-command guard that briefly existed no longer
  applies. The worktree still protects against ordinary relative-path edits, which is what
  `docs/security-model.md:311` claims correctly. Smallest fix: soften line 117 to match line 311, and note that
  a real boundary for Kimi requires the bwrap backend (which needs B1 fixed first).

## SIMPLIFY

- **[S1] Kimi now supports native session resume in print mode; delegate refuses followup for it.**
  delegate carries: `followup_command.py:169` and `:281` reject every engine outside `("codex", "claude")` with
  `followup-unsupported`; `harness_events._capture_session_id` (`:708`) has no kimi branch and never records a
  Kimi session id. harness now provides: the last stdout line of every stream-json print run is
  `{"role":"meta","type":"session.resume_hint","session_id":"<id>","command":"kimi -r <id>","content":"…"}`
  (bundle, `writeResumeHint`), and `validateOptions` (bundle) rejects only `--session` **without** an id in
  prompt mode — `kimi --session <id> -p "<followup>"` and `kimi -c -p "<followup>"` are both valid.
  `--session`/`-r`/`--continue` are documented at
  https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/reference/kimi-command.md. Wiring this up is
  additive: one `elif` in `_capture_session_id`, one argv branch for `--session`, and kimi added to the
  followup engine tuple. Estimated deletion: none; estimated addition ~15 lines, in exchange for removing a
  documented capability gap (`README.md:281`, `docs/cli-reference.md`).
- **[S2] `kimi session list --json` exists and would give delegate a first-class session enumeration.**
  Added in 0.40.0 ("Add the `kimi session list` command to list sessions from the command line"); the bundle
  registers it with `--cwd`, `--all`, `--archived`, `--limit <n>`, and `--json`. Delegate has no Kimi session
  enumeration today. Only worth doing if S1 lands.
- **[S3] Kimi does have a thinking-effort transport — an environment variable, not a flag — so the blanket
  `unsupported_reasoning_effort` rejection is stricter than necessary.** delegate carries:
  `request_build.py:3347-3357` raises `unsupported_reasoning_effort` for any requested effort;
  `reasoning.py:85` `KIMI_UNSUPPORTED_REASONING_WARNING`; `config.py:809` forces
  `kimi.defaultReasoningEffort` to null; and `docs/cli-reference.md:516` states "the Kimi CLI exposes no effort
  flag". The flag half of that is correct. But `KIMI_MODEL_THINKING_EFFORT` is documented with values
  `low`/`medium`/`high`/`xhigh`/`max`
  (https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/env-vars.md, `KIMI_MODEL_*` table),
  and the bundle's `resolveKimiEnvThinkingEffort` reads it and — per its own comment — "intentionally bypasses
  `support_efforts`". Delegate already has an env-var transport concept, and `parse_kimi_catalog`
  (`harness_discovery.py:945-958`) already reads `supportEfforts`/`defaultEffort` off the model entries, so the
  discovery half is built and currently unused. **Caveat, stated as a caveat:** the docs place this variable in
  the `KIMI_MODEL_*` family whose prose says `KIMI_MODEL_NAME` is "the enable switch", while the bundle
  function reads the effort variable independently of that path. Whether it takes effect with
  `KIMI_MODEL_NAME` unset needs one live run to settle; do not ship on the bundle reading alone.

## OK (verified)

Flags — all verified against the bundle's `createProgram` option list and
https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/reference/kimi-command.md:

- `--prompt <prompt>` / `-p` — still the non-interactive entry point, still takes the prompt as an option value.
- **Argv is still the only prompt transport.** `README.md:19-20` and `docs/cli-reference.md:159` claim Kimi
  "requires prompt argv"; still true at 0.41.0. There is no stdin transport, no prompt-file flag, and
  positional arguments are actively rejected — `program.argument("[args...]")` errors with
  `unknown command '<arg>'` when anything positional is passed (bundle). An empty or whitespace prompt is
  rejected with `"Prompt cannot be empty."`. (Note: the doc example `kimi -p --agent reviewer "Review …"` in
  the kimi-command reference contradicts this and looks wrong; not delegate's problem.)
- `--output-format <format>` with `choices(["text", "stream-json"])`, prompt-mode only, default `text` —
  matches `argv_builders.py:236`. Delegate emits it before `--prompt`; commander is order-insensitive.
- `--model <model>` / `-m` — matches `argv_builders.py:234`. Semantics are "model **alias**"; the bundle's
  `requireConfiguredModel` passes the value through to session creation unvalidated at the CLI layer.
- Delegate emits no `--yolo`, `--auto`, or `--plan`, and must not: `validateOptions` (bundle) rejects all three
  with `--prompt` (`"Cannot combine --prompt with --yolo."` etc.). `docs/cli-reference.md:514` and
  `docs/security-model.md:204` state this correctly.
- `--add-dir <dir>`, repeatable, "Add an additional workspace directory for this session" — matches
  `mail_core.py:386-388` and `docs/security-model.md:50`.
- No `--output-schema` / `--json-schema` equivalent exists, so `workflows/runtime.py:3751` `_native_schema`
  correctly returns `None` for kimi and the prompt-and-parse path is the only option.
- No working-directory flag exists; the bundle uses `process.cwd()` in `runPrompt`, which is what delegate
  relies on by launching with `cwd` set.

Output schema — verified against `PromptJsonWriter` and `dispatchNativeEvent` in the bundle:

- Assistant envelopes are `{"role":"assistant","content":…,"tool_calls":[{"type":"function","id":…,"function":{"name":…,"arguments":"<JSON string>"}}]}`. Matches `harness_events._ingest_kimi_tool_calls` (`:798`), including the
  JSON-encoded-string `arguments` handled by `_kimi_tool_target` (`:1512`).
- Tool results are `{"role":"tool","tool_call_id":…,"content":…}`. Matches `_ingest_kimi_tool_result` (`:823`).
- Tool results still carry **no** `is_error` or `status` field, so `harness_events.py:829` leaving `status=None`
  rather than inventing success remains correct at 0.41.0.
- **No usage or token lines are emitted in stream-json.** `docs/cli-reference.md:518` claims this
  ("verified 0.26.0") and it still holds at 0.41.0: `PromptJsonWriter` has no usage path, and the only numeric
  accounting anywhere is inside the `goal.summary` line covered by L2.
- Meta lines carry a `type` and are correctly dropped by delegate's type-first dispatch
  (`harness_events.py:638` names `session.resume_hint`). Three exist at 0.41.0: `session.resume_hint`,
  `turn.step.retrying` (retry telemetry, added 0.23.5), and `system.version`
  (`{"role":"meta","type":"system.version","version":"0.41.0"}`), emitted as the **first** stdout line by the
  v2 print runner. All three are dropped harmlessly; delegate's comment is just one name out of date.
- Thinking content is never written to stdout in stream-json mode (`writeThinkingDelta()` is empty); tool
  progress and resume notices go to stderr. Consistent with delegate not parsing them.

Models and discovery:

- `kimi provider list --json` is a real, offline, auth-free subcommand (bundle `handleProviderList`), writes
  only JSON to stdout, and routes all errors to stderr with exit 1. `harness_discovery.py:1379` is correct.
- **The JSON is camelCase, matching `parse_kimi_catalog` and `tests/fixtures/discovery/kimi_providers.json`.**
  This was the least obvious thing to check: `config.toml` uses snake_case (`display_name`, `support_efforts`,
  `default_effort`), but `handleProviderList` serializes the *parsed runtime config*, which the bundle converts
  via `snakeToCamel`. Confirmed in the bundle by `config.defaultModel` in the same function and by
  `effortsFor$1` reading `.supportEfforts`. Delegate's `displayName` / `supportEfforts` / `defaultEffort` keys
  (`harness_discovery.py:945-958`) are right.
- Model ids `kimi-code/k3`, `kimi-code/kimi-for-coding`, `kimi-code/kimi-for-coding-highspeed`
  (`bundled_models.py:73-76`, `config.example.json:114`) all still appear as current aliases in
  https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/config-files.md. The docs also confirm
  only the k3 family declares effort levels under `managed:kimi-code`. The stale `kimi-k2.7` entry noted in
  delegate's own CHANGELOG is gone from `bundled_models.py:73-76` (it survives at `:47-48` under a different
  engine, which is out of scope here).
- Embedded `kimi.defaultModel` is `None` (`config.py:106`), deferring to Kimi's own `default_model`, and
  `docs/configuration.md:523` describes this correctly. Dry-run confirms:
  `python3 bin/delegate.py --json dry-run kimi work "Do the thing"` →
  `{"argv": ["kimi", "--output-format", "stream-json", "--prompt", "<prompt redacted: kimi argv transport>"], "model": null, "promptTransport": "argv", …}`.
- `~/.kimi-code/bin` in `cli.py:114` `MISSING_BINARY_PROBE_DIRS` is **correct** — the official install script
  defaults to `KIMI_INSTALL_DIR=$HOME/.kimi-code` and puts the executable in `${KIMI_INSTALL_DIR}/bin`, adding
  that directory to `PATH` (verified by fetching https://code.kimi.com/kimi-code/install.sh, lines 25 and
  215-231). The managed `rg`/`fd` live in the same directory.

Exit codes (bundle):

| Situation | Code |
| --- | --- |
| Success | 0 |
| Flag conflict (`OptionConflictError`) → `error: <message>` on stderr | 1 |
| Startup failure or a failed turn in print mode | 1 |
| `/goal` run ending `blocked` | 3 |
| `/goal` run ending `paused` | 6 |
| SIGINT / SIGHUP / SIGTERM during print mode | 130 / 129 / 143 |

The 0.23.2 fix "Fix `kimi -p` runs exiting with code 0 when a turn fails" means a failed turn is reliably
non-zero on any version delegate would meet today. Delegate has no Kimi-specific exit-code table, so only the
`/goal` codes (L2) are a mismatch.

## Could not verify

- **Anything requiring the binary.** `kimi --version`, `kimi --help`, `kimi provider list --json` on a real
  configured home, and actual stream-json line ordering from a live run were all impossible: `kimi` is not
  installed here and `~/.kimi-code` does not exist. Every behavioural claim above is read from the shipped
  0.41.0 bundle or the vendor docs, and is labelled as such.
- **Whether `KIMI_MODEL_THINKING_EFFORT` is honoured without `KIMI_MODEL_NAME` set** (S3). The bundle's
  resolver reads it unconditionally for a Kimi-protocol provider; the docs table implies the family is gated
  behind `KIMI_MODEL_NAME`. One live run settles it.
- **Whether the 15-second `METADATA_PROBE_TIMEOUT_SEC` (`harness_discovery.py:117`) is enough for
  `kimi provider list --json`.** The entry point is a 23 MB single-file Node bundle requiring Node ≥ 22.19.0,
  and cold start was not measurable here. Flagging it as a thing to time once, not as a defect.
- **The exact stdout interleaving of `system.version` relative to config-diagnostic warnings on stderr.** Read
  from code, not observed.
- **Legacy-engine behaviour.** `KIMI_CODE_LEGACY_FLAG=1` selects the pre-0.33.0 v1 engine, which has its own
  `runPrompt` path. Both paths share `PromptJsonWriter`, so the envelope shape is the same, but the v1 path
  does not emit the `system.version` line. Not separately verified.

## Sources

- https://www.npmjs.com/package/@moonshot-ai/kimi-code and `https://registry.npmjs.org/@moonshot-ai/kimi-code` — latest version 0.41.0, published 2026-09-04; 74 published versions.
- `@moonshot-ai/kimi-code@0.41.0` npm tarball, `package/dist/main.mjs` — commander option table, `validateOptions`, `resolveOutputFormat`, `PromptJsonWriter`, `writeResumeHint`, `writeExperimentalVersion`, `handleProviderList`, `requireConfiguredModel`, `installHeadlessHandlers`, `goalExitCode`, `goalSummaryJson`, `signalExitCode`, `resolveKimiEnvThinkingEffort`. Ground truth for flags, stream-json envelopes, exit codes, and JSON key casing.
- https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/reference/kimi-command.md — flag reference, flag-conflict rules, non-interactive execution, `provider`/`session`/`doctor`/`export` subcommands.
- https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/data-locations.md — `~/.kimi-code` layout; established B1.
- https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/config-files.md — `[models]` fields (`support_efforts`, `default_effort`, `display_name`), `[thinking]`, `[background]` print-mode policy; established L5 and the model-id check.
- https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/env-vars.md — `KIMI_MODEL_*` family, `KIMI_MODEL_OUTPUT_FORMAT`, `KIMI_MODEL_THINKING_EFFORT`; established L3 and S3.
- https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/providers.md — provider types; OAuth accounts absent from the `providers` table.
- https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/release-notes/changelog.md — 0.2.0 resume-hint meta line, 0.23.2 print-mode exit-code fix, 0.24.2 print-mode background semantics, 0.33.0 agent-core-v2 default, 0.40.0 `session list` / Bash cwd restriction removal, 0.41.0 auto-mode dangerous-command guard removal; established L2, L5, L6.
- https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/guides/getting-started.md and https://code.kimi.com/kimi-code/install.sh — install paths; confirmed `cli.py:114`.
- https://github.com/MoonshotAI/kimi-cli — the legacy Python CLI, explicitly being wound down in favour of kimi-code; establishes that the two `kimi` binaries are distinct projects.
