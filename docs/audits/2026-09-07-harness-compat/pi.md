# Pi compatibility audit

Latest version: 0.85.1 (2026-09-05, https://github.com/earendil-works/pi/releases + npm `@earendil-works/pi-coding-agent`) · Installed here: 0.85.1
Delegate assumptions checked: 34 · BROKEN: 1 · LATENT: 8 · SIMPLIFY: 3 · OK: 22

Installed version **is** the latest release, so there is no version-lag diff to report. Every finding
below is against current Pi. Package identity moved: `@mariozechner/pi-coding-agent` is frozen at
0.73.1 and `@earendil-works/pi-coding-agent` carries 0.74.0+; the repo is now `earendil-works/pi`.

Pi is **not authenticated on this cell** (`~/.pi/agent/auth.json` is `{}`, 2 bytes), so every finding
is from `--help`, the shipped `docs/`, the shipped TypeScript declaration files, and delegate
dry-runs. No live model call was made. Items needing a live run are listed under "Could not verify".

## BROKEN

- **[B1] Pi renamed its compaction events; delegate's compaction-failure guard is dead code for pi.**
  delegate: `src/delegate_agent/harness_events.py:1189` and `:1197` branch on `auto_compaction_start`
  and `auto_compaction_end`. Pi 0.85.1 emits `compaction_start` and `compaction_end`
  (`dist/core/agent-session.d.ts:53,65`, and `docs/json.md`: "`compaction_start` and `compaction_end`
  cover both manual and automatic compaction"). The *field* names delegate reads on that branch
  (`aborted`, `willRetry`, `errorMessage`) match `compaction_end` exactly, so this is a missed rename,
  not a redesign.
  Repro (no model call):
  ```
  cd /home/trey-agent/.local/lib/node_modules/@earendil-works/pi-coding-agent/dist
  command grep -ro '"auto_compaction_start"' . | wc -l   # 0
  command grep -n 'compaction_start' core/agent-session.d.ts  # line 53
  ```
  Effect: a pi run whose auto-compaction aborts emits `compaction_end {aborted:true, willRetry:false,
  errorMessage}`, delegate ignores it, and because pi's `--mode json` exits 0 on failure (see OK list)
  the run can be recorded as success. `turn_start` still clears terminal state for pi, so the
  `auto_compaction_start` half is redundant; the `auto_compaction_end` half is the load-bearing loss.
  Smallest fix: accept both spellings in the two branch conditions. The names are still current for
  Oh My Pi (see [L3] and the Oh My Pi note), so do **not** simply rename them.

## LATENT

- **[L1] Pi silently swallows unknown long flags, so delegate's safe-mode lockdown fails open.**
  `dist/cli/args.js:217-233` routes any unrecognized `--foo` into `result.unknownFlags` (the
  extension-flag channel) instead of erroring. Only unknown *short* flags error.
  Repro:
  ```
  pi --offline --this-flag-does-not-exist --list-models ; echo $?   # exit 0, no complaint
  pi --offline -zz --list-models ; echo $?                          # exit 1, "Unknown option: -zz"
  ```
  All five of delegate's safe-mode flags (`argv_builders.py:62-69`) are long flags. If Pi renames or
  drops `--no-prompt-templates`, `--no-skills`, `--no-extensions`, or `--no-approve`, delegate's
  defense-in-depth silently evaporates with a green run. The primary gate `--tools read` is a known
  flag today and is genuinely enforcing (see OK list), so safe mode is sound *now*. Smallest fix: a
  discovery-time acceptance check that each lockdown flag appears in `pi --help`, the way
  `_probe_claude` already proves `--append-system-prompt-file`.

- **[L2] An invalid `--thinking` value is a warning, not an error.**
  `dist/cli/args.js:112-122` pushes a `type: "warning"` diagnostic and leaves `result.thinking`
  unset, so the run proceeds at the default level.
  ```
  pi --offline --thinking bogus-level --list-models ; echo $?
  # Warning: Invalid thinking level "bogus-level". Valid values: off, minimal, low, medium, high, xhigh, max
  # exit 0
  ```
  Delegate's `PI_NATIVE_EFFORTS` matches 0.85.1 exactly, so nothing is wrong today, but a future Pi
  enum change would downgrade reasoning effort silently rather than failing the run.

- **[L3] `errorStatus` does not exist anywhere in Pi 0.85.1.**
  `harness_events.py:1248-1254` reads `message["errorStatus"]` and treats `>= 400` as an HTTP failure.
  `AssistantMessage` in Pi's own types (`node_modules/@earendil-works/pi-ai/dist/types.d.ts:307-328`)
  carries `stopReason`, `errorMessage`, `rawStopReason`, and `diagnostics` — no `errorStatus`. A
  whole-tree grep for the identifier returns 0 hits. The branch is dead for pi. No functional gap:
  `stopReason: "error"` plus `errorMessage` covers the same failures and delegate handles both. The
  field **is** real in Oh My Pi 18.1.12, which is why the shared code has it.

- **[L4] Pi emits no top-level `error` event, so that branch is dead too.**
  `harness_events.py:1280` and `stall_watchdog.py:_PI_BOUNDARY_TYPES` both expect `{"type":"error"}`.
  The complete Pi event vocabulary is `AgentEvent`
  (`node_modules/@earendil-works/pi-agent-core/dist/types.d.ts:377-414`) unioned with the
  `AgentSessionEvent` extensions (`dist/core/agent-session.d.ts:39-100`); neither contains `error`.
  Failures arrive as a failing `turn_end`, which delegate does handle.

- **[L5] `stopReason: "deferred"` and `"pending"` are unhandled.**
  Pi's `StopReason` is `"pending" | "stop" | "length" | "toolUse" | "error" | "aborted" | "deferred"`
  (`pi-ai/dist/types.d.ts:287`). `harness_events.py:1254` returns early for anything outside
  `stop|error|aborted|length`. Ignoring `toolUse` and `pending` is correct (mid-turn). `deferred`
  is a real terminal state for batch/deferred provider responses, and since `--mode json` exits 0
  regardless, a deferred turn would end with no terminal event and an empty completion report scored
  as success rather than failure.

- **[L6] `--resume=<id>` would be silently ignored if pi resume were ever enabled.**
  `argv_builders.py:516` emits `f"--resume={resume_session_id}"` in the shared pi-family builder.
  Pi's parser matches `arg === "--resume"` only (`dist/cli/args.js:49`), and `--resume` takes **no
  value** in pi; the `--resume=x` form falls into the unknown-long-flag bucket from [L1] and is
  dropped. Delegate never reaches this for pi today (`constants.py:88` sets
  `nativeSessionResume: false` and `build_pi_argv` accepts no `resume_session_id`), so this is a trap,
  not a live defect. Flipping the capability without changing the flag would silently start a fresh
  session on every "resumed" turn. See [S3] for the flag that actually works.

- **[L7] `tool_execution_end` carries no `args`, so completed tool events lose their target.**
  `harness_events.py:_ingest_pi_tool` derives `target`/`path` from `payload["args"]`. Pi's
  `tool_execution_end` is `{toolCallId, toolName, result, isError}` — `args` appears only on
  `tool_execution_start` and `tool_execution_update`
  (`pi-agent-core/dist/types.d.ts:404-414`). Every `tool.completed` event delegate records for pi has
  `target: None`. Cosmetic, but progress output is less useful than it looks.

- **[L8] `docs/security-model.md:122` mischaracterizes what `--no-approve` does in Pi.**
  It calls Oh My Pi's `--approval-mode always-ask` "Oh My Pi's analog of Pi's `--no-approve`",
  implying `--no-approve` is Pi's tool-approval gate. It is not. Pi's own security doc is explicit:
  "Project trust controls whether pi loads project-local settings, resources, packages, and
  extensions. It is not a sandbox and it does not restrict what the model can ask tools to do after
  you start working in a directory." (`docs/security.md:7`; `--help`: "Ignore project-local files for
  this run"). `--no-approve` is a config/supply-chain guard worth keeping — it defeats a hostile repo
  plus a global `defaultProjectTrust: "always"` (`docs/security.md:29`) — but in Pi the *only*
  write-blocking flag is `--tools read`. The stated rationale invites a future editor to drop
  `--tools read` as redundant, which would make pi safe mode write-capable.
  Also stale in the same file: it pins Oh My Pi at 17.0.4; 18.1.12 is what is installed here.

## SIMPLIFY

- **[S1] Native persona transport is available and unused.**
  `--append-system-prompt <text>` is repeatable and, per `--help`, appends "text **or file
  contents**". Delegate wires native persona files only for Claude (`argv_builders.py:341`) and
  prepends into the prompt for pi. Adopting it would give pi the same `personaTransports:
  {"native-file": true}` path Claude has and keep the persona out of the user-turn text. Discovery
  evidence is a one-line `--help` substring check next to the existing `_probe_claude` one.

- **[S2] Two dead pi branches can collapse once [B1] is fixed.**
  The `errorStatus`/`http_error` block (`harness_events.py:1246-1253`) and the trailing `error` branch
  (`:1280-1282`) never fire for pi. They are live for Oh My Pi, so the cleanup is to gate them by
  harness rather than delete them — which also makes the pi-vs-omp dialect split explicit instead of
  implicit. Roughly 10 lines plus the synthetic fixtures in `tests/test_provider_outcomes.py:50-56`
  that currently assert pi behavior using omp-shaped events.

- **[S3] Pi has a deterministic headless resume flag; delegate declares it has none.**
  `--session-id <id>` ("Use exact project session ID, creating it if missing") and `--session
  <path|id>` both take values and both work in print mode (`dist/cli/args.js:79-84`). That is a real
  resume-or-create primitive, so `nativeSessionResume: false` for pi (`constants.py:88`) is now
  conservative rather than accurate. Enabling it would let pi join `followup` and the structured-output
  retry path (`workflows/runtime.py` `STRUCTURED_RESUME_ENGINES`) instead of re-sending full context.
  Note this requires dropping `--no-session` for those runs and using `--session-id`, **not** the
  `--resume=` form from [L6].

## OK (verified)

Argv, all confirmed present in `pi --help` on 0.85.1 and in `dist/cli/args.js`:

- `-p` / `--print`, `--mode json` — both accepted; `runPrintMode` serves both (`dist/modes/print-mode.js`).
- `--no-session` — exists; delegate's stateless-child claim holds for all three modes.
- `--model <provider/model>` — `--help`: 'supports "provider/id" and optional ":<thinking>"'.
- `--thinking <level>` — enum is exactly `off, minimal, low, medium, high, xhigh, max`, matching
  `reasoning.py:78-79` (`PI_NATIVE_EFFORTS` + `PI_THINKING_LEVELS`).
- `--tools`, `--no-extensions`, `--no-skills`, `--no-prompt-templates`, `--no-approve`, `--offline`,
  `--list-models`, `--append-system-prompt` — all present and correctly spelled.
- Dry-run argv is exactly what Pi accepts:
  `python3 bin/delegate.py --json dry-run pi safe "Review only."` →
  `["estate-pi","-p","--no-session","--mode","json","--tools","read","--no-extensions","--no-skills","--no-prompt-templates","--no-approve"]`,
  `promptTransport: "stdin"`. Work mode with `--reasoning-effort high` appends `--thinking high`.

Behavior:

- **`--tools read` is genuinely self-enforcing in Pi**, unlike Oh My Pi. `_refreshToolRegistry`
  (`dist/core/agent-session.js:2105-2160`) filters `_baseToolDefinitions` **and** all extension/custom
  tools through `isAllowedTool` before building `_toolDefinitions` and `_toolRegistry`. A disallowed
  tool is never advertised to the model and has no execution path. `docs/security-model.md:121` is
  correct on the mechanism even though :122 is wrong on the rationale ([L8]). Note the registry lets a
  custom tool named `read` overwrite the builtin, which is exactly why `--no-extensions` matters as
  defense-in-depth; delegate passes it.
- **Stdin prompt transport is real and documented.** `docs/usage.md:179`: "In print mode, pi also
  reads piped stdin and merges it into the initial prompt." `dist/cli/initial-message.js` joins
  stdin, `@file` text, and the first positional message. Delegate sends stdin only, so ordering and
  the empty-separator join are moot. `constants.py:91` `promptStdin: true` is correct.
- **`--mode json` exits 0 even when the model errored, and delegate correctly does not trust it.**
  In `runPrintMode` the `exitCode = 1` assignment sits inside `if (mode === "text")`; the json path
  only returns 1 on a thrown exception. Delegate reconciles via the accumulator:
  `runner.py:3109` treats exit 0 plus `terminal_status` failed/cancelled as a failure, and
  `_unclassified_provider_failure` (`runner.py:4577`) is pi/omp-specific. This is the single most
  dangerous Pi behavior and it is already handled.
- Other exit codes: `--help` and `--list-models` always 0 (including "No models available");
  extension-load and no-model-available errors 1; `SIGTERM` 143, `SIGHUP` 129
  (`dist/modes/print-mode.js`); `pi auth check` uses 0/1/2.
- **Event names delegate does consume all exist**: `turn_start`, `turn_end{message, toolResults}`,
  `message_end{message}`, `message_update{usage, assistantMessageEvent}`, `tool_execution_start`,
  `tool_execution_end{isError}`, `auto_retry_start{errorMessage}`, `auto_retry_end{success,
  finalError}`, plus `agent_settled` and `queue_update` used by the stall watchdog.
- **`--list-models` table format matches `parse_pi_catalog` exactly.** `dist/cli/list-models.js`
  emits six `padEnd` columns joined by two spaces: `provider model context max-out thinking images`,
  with `thinking` as literal `yes`/`no`. Delegate indexes by header position with a ragged-row guard
  (`harness_discovery.py:1284-1346`); the column order and the `yes`/`no` vocabulary are correct. No
  ANSI is applied to the table, and delegate strips ANSI anyway.
- **The thinking-enum scrape works against real 0.85.1 help.** The regex `Set thinking level:\s*(...)`
  captures `off, minimal, low, medium, high, xhigh, max` from live `pi --help` output.
- **Version detection works.** `pi --version` prints bare `0.85.1`, matched by `_GENERIC_VERSION`;
  delegate's probe reports `version: "0.85.1"`.
- **Config paths are current.** `~/.pi/agent/` with `auth.json` (`docs/providers.md:26`),
  overridable via `PI_CODING_AGENT_DIR`. `sandbox_bwrap.py:61` maps pi to `.pi` correctly.
  `docs/agent-setup.md` is right that delegate never emits `--api-key`.
- **No native structured output in Pi**, so `structuredOutput: false` and `_native_schema` returning
  `None` for pi (falling back to prompt-and-parse) is correct.
- **`docs/cli-reference.md:455-477` is accurate** on launch line, alias shapes, thinking mapping,
  statelessness, and `models pi --live`.
- The `pi_family_prompt_flag_like` guard (`argv_builders.py:527-537`) is unreachable for pi, which is
  fine — its comment says Oh My Pi does not honor `--` as an end-of-options separator, but Pi does
  (`--help`: "`--` End option parsing"), and pi uses stdin anyway.
- The discovery fixture `tests/fixtures/discovery/pi_models.txt` was captured from 0.80.10, but its
  column layout and thinking enum both still match 0.85.1's renderer, so it is stale-labelled rather
  than wrong.

## Relationship to Oh My Pi

They are **separate forks with a diverged event dialect**, sharing a common ancestor and a very
similar flag surface — and delegate's shared `_build_pi_family_argv` / `_ingest_pi_event` code is
currently written to **Oh My Pi's** dialect, which is the root cause of [B1], [L3], and [L4].

| | Pi 0.85.1 | Oh My Pi 18.1.12 |
|---|---|---|
| npm package | `@earendil-works/pi-coding-agent` | `@oh-my-pi/pi-coding-agent` |
| Compaction events | `compaction_start` / `compaction_end` | `auto_compaction_start` / `auto_compaction_end` (25 occurrences, no bare form) |
| `errorStatus` on assistant message | absent (0 occurrences) | present (`src/session/messages.ts:61`) |
| `message_update` payload | delta-only; `partial` and cumulative `message` stripped by `toJsonEvent` | still carries the accumulating blob |
| `--tools read` | registry-enforcing | not self-enforcing; needs `--approval-mode always-ask` |
| Prompt transport | piped stdin in print mode | positional argv |

No shared config: Pi reads `~/.pi/agent/`, Oh My Pi its own tree. Delegate keeps separate `pi` and
`omp` config sections, which is right.

One consequence worth noting: `stall_watchdog.py:180-190` justifies its delta-only comparison with
"Every `message_update` also carries an accumulating `partial`/`message` blob, so the LINE always
differs". That is **false for Pi 0.85.1** — `docs/json.md` states message_update records "omit both
the cumulative `message` field and `assistantMessageEvent.partial` to keep stream size linear", and
`dist/modes/json-event.js` strips them. The code is still correct (using the delta field is right
either way); only the stated reason no longer applies to pi.

## Could not verify

- **Any live JSON event stream from pi.** `~/.pi/agent/auth.json` is empty, so no provider is
  authenticated on this cell and every model-bearing path is unreachable. All event-schema findings
  are from Pi's shipped `.d.ts` declarations and `docs/json.md`, which are authoritative for the
  installed build but are not a captured stream.
- **Behavioral proof that `--tools read` blocks a write.** Source-verified at the registry level only.
  A write-probe equivalent to the one that produced the Oh My Pi finding needs an authenticated pi.
- **Whether a real compaction abort ([B1]) escapes entirely**, or is also reflected in a failing
  `turn_end` that delegate would still catch. That needs a live long-context run that overflows.
- **Whether `stopReason: "deferred"` ([L5]) occurs in practice** for any provider delegate would route
  to pi; the type permits it, but no shipped adapter was traced to confirm it is emitted.
- Delegate's local config points pi at the `estate-pi` shim, which reports 0.85.1 and forwards flags
  correctly; the shim's own argv handling under unusual quoting was not exercised.

## Sources

- `pi --help`, `pi --version` (0.85.1, installed at `/home/trey-agent/.local/bin/pi`) — flag surface,
  thinking enum, env vars, built-in tool names.
- `npm view @earendil-works/pi-coding-agent version dist-tags time` — 0.85.1 is `latest`, published
  2026-09-05; `@mariozechner/pi-coding-agent` frozen at 0.73.1.
- https://github.com/earendil-works/pi/releases (via `gh api`) — v0.85.1 2026-09-05 is the newest tag,
  confirming installed == latest.
- `.../pi-coding-agent/docs/json.md` — JSON event stream contract, delta-only `message_update`,
  `compaction_start`/`compaction_end` naming.
- `.../pi-coding-agent/docs/usage.md:176-183` and `docs/quickstart.md` — print mode, `--mode json`,
  piped-stdin merge.
- `.../pi-coding-agent/docs/security.md:3-41` — project trust is an input-loading guard, not a
  sandbox; non-interactive trust resolution; `--approve`/`--no-approve` semantics.
- `.../pi-coding-agent/docs/providers.md:26` — `~/.pi/agent/auth.json`.
- `.../dist/core/agent-session.d.ts:39-100` — `AgentSessionEvent` union with exact field names.
- `.../node_modules/@earendil-works/pi-agent-core/dist/types.d.ts:377-414` — base `AgentEvent` union.
- `.../node_modules/@earendil-works/pi-ai/dist/types.d.ts:287,307-328` — `StopReason`,
  `AssistantMessage` fields.
- `.../dist/core/agent-session.js:2105-2160` — `--tools` allowlist enforcement at registry build.
- `.../dist/cli/args.js:49-233` — flag parsing, unknown-flag fallthrough, thinking validation.
- `.../dist/cli/list-models.js` — `--list-models` column layout.
- `.../dist/modes/print-mode.js`, `dist/modes/json-event.js`, `dist/main.js:687-790` — exit codes,
  json serialization, print-mode dispatch.
- `.../dist/cli/initial-message.js` — stdin/`@file`/positional merge order.
- `.../CHANGELOG.md:4658-4659` — historical `auto_compaction_start`/`auto_compaction_end` naming,
  establishing [B1] as a rename rather than a redesign.
- `/home/trey-agent/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent` (18.1.12) — the Oh My
  Pi dialect comparison table.
