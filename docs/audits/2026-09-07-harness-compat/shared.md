# Shared cross-harness plumbing compatibility audit

Scope: the modules every harness adapter shares, not any one harness.
`harness_discovery.py`, `model_discovery.py` + `bundled_models.py` + `config.example.json`,
`reasoning.py`, `harness_events.py`, `structured_output.py` + `workflows/runtime.py:_native_schema`
+ `workflows/schema.py:validate_schema_subset`, `prompt_transport.py`, `failover_state.py` /
`terminal_states.py` / `child_failures.py`, and `stall_watchdog.py`.

Repo `delegate-agent` 0.30.0, branch `feat/harness-compat-audit`, at `12fe7ac`.
Installed here: claude `2.1.263`, codex-cli `0.153.4`, cursor-agent `2026.09.02-c22c1a3`,
grok `1.0.13`, omp `18.1.13`, pi `0.85.1`. Not installed: droid, devin, kimi, opencode.

Delegate assumptions checked: 61 · BROKEN: 9 · LATENT: 14 · SIMPLIFY: 9 · OK: 29

## Test baseline

`python3 -m pytest -q` (the command `docs/development.md` prescribes), `TMPDIR=/var/tmp`,
repo checkout, no `-x`, no `-n`:

| result | value |
| --- | --- |
| outcome | 3046 passed, 15 skipped, 2241 subtests passed |
| exit code | 0 |
| wall time | 428.65 s (7 m 09 s) |

Green tree. Every BROKEN item below is therefore a gap the suite does not cover, not a
regression the suite already flags.

## BROKEN

- [B1] **A generic `error` event never sets a terminal status.** `harness_events.py:728-733`
  (`_ingest_error_event`) appends an `error` event and stores `_last_error_message`, but never calls
  `_record_terminal_event`. Grok (`:1055`) and pi (`:1281-1282`) do record one; the generic path used
  by codex, claude, cursor, kimi and droid does not. Repro: feed codex
  `item.completed`(agent_message "Partial answer") → `error`("429 rate limit") → `turn.completed` and
  the accumulator reports `terminal_status=None` with the pre-error preamble promoted as the
  completion report. Whether the run is marked failed then rests entirely on the child exit code, and
  `_capture_failure` (`runner.py:3108-3112`) returns `None` when exit is 0 and the terminal is clean,
  so `child_failures.classify` never sees the child's own error words. Smallest fix: record a `failed`
  terminal in `_ingest_error_event`, as the grok and pi handlers already do.
- [B2] **An `error` event whose message is nested is dropped entirely.** Same function reads only a
  top-level string `message`. Anthropic- and OpenAI-shaped errors put it at `error.message`.
  `{"type":"error","error":{"message":"429 rate_limit_exceeded"}}` produces no event, no terminal, no
  text. `_terminal_error_reason` (`harness_events.py:735-741`) already knows how to read `error.message`;
  the asymmetry between the two is the bug.
- [B3] **A `result` event carrying `is_error` but no string `result` is dropped.**
  `harness_events.py:943-948` consults `is_error` only inside the `isinstance(result, str)` guard.
  `{"type":"result","subtype":"error_during_execution","is_error":true,"num_turns":3}` yields
  `terminal=None` and no events. `error_max_turns` is rescued upstream by `_provider_terminal_state`
  (`:371`), but `error_during_execution`, `error_api` and any unlisted subtype fall straight through.
- [B4] **Non-JSON stdout lines are hard-discarded for kimi, opencode and pi.**
  `harness_events.py:505-529` returns early for those three on `RecursionError`, `ValueError`, and any
  non-dict payload. A CLI banner, an auth error printed to stdout, or a truncated final line from a
  killed child vanishes with no event and no counter. Demonstrated on opencode: a stream of
  `step_start`, a bare-text `Error: 401 authentication_error from provider anthropic`, then
  `text: partial` gives `structured_events_seen=2`, `events=[]`, `terminal=None`, text `"partial"`.
  The non-zero `structured_events_seen` then suppresses the raw-stdout fallback
  (`runner.py:4805-4810`), so an exit-0 run reports a truncated answer as a clean success. The
  opencode fixture README documents that opencode exits 0 on permission-denied runs, so exit-0 with a
  real problem is a documented shape for that engine. `omp` reaches the same pi parser but is absent
  from those three tuples, so omp keeps the text fallback — the inconsistency is unexplained.
- [B5] **`_native_schema` hands Claude a non-object-root schema that the Anthropic API rejects.**
  The instance in `docs/issues/2026-09-07-claude-json-schema-non-object.md`. Confirmed at the source:
  the Claude Code 2.1.263 bundle's `--json-schema` preflight only requires a JSON object, runs Ajv
  `validateSchema`+`compile`, and attaches the result verbatim as a custom tool's `inputJSONSchema`.
  The object-root rule is enforced by the API, not the CLI. Probed against a dead endpoint
  (`ANTHROPIC_BASE_URL=http://127.0.0.1:9`, no model call): array root, `{"type":"string"}`, `{}`,
  `format`, `$defs`/`$ref`, root `oneOf`, root `$ref`, `definitions`, and empty `properties` all pass
  preflight and reach the API.
- [B6] **The same acceptance gap exists on four more shapes, and on codex as well as claude.**
  `validate_schema_subset` (`workflows/schema.py`) accepts `{"enum":["a","b"]}`, `{}`,
  a type-less `{"properties":{...}}`, and `{"type":["object","null"]}`. `_native_schema` routes all four
  natively for claude, and `normalize_codex_schema` routes the last three natively for codex —
  `_is_object_node` (`structured_output.py:30-37`) accepts a list containing `"object"` and deliberately
  leaves type-less roots alone (`:18-23`). The codex normalizer injects `required` and
  `additionalProperties: false` but never injects `type: "object"`, so delegate half-repairs the schema
  and still ships an invalid root. Claude returns 400; OpenAI strict mode rejects all four. The
  one-line fix proposed in the issue doc covers the claude side of B5 and B6 but nothing on the codex
  side.
- [B7] **A rejected native schema is retried with the identical schema.** `native_schema` is computed
  once, outside the retry loop (`workflows/runtime.py:2914`), and a hard launch failure is
  indistinguishable from a bad model answer: `child.text is None` → `parse_json_tolerant("")` raises →
  `agent_structured_retry` → relaunch with the same rejected schema (`runtime.py:3031-3080`). That is
  why the field incident burned both attempts in about two seconds. Nothing in the loop can demote a
  native-schema stage to the prompt path after a launch-time rejection.
- [B8] **A valid grok row voids the entire reasoning capability cache.**
  `resolve_grok_reasoning_capability` (`reasoning.py:618`) reads grok declarations from the cache, but
  `validate_cache_payload` (`:1420`) gates harness keys on `TRANSPORT_BY_HARNESS`, which is only
  `{codex, droid, cursor}`. `load_reasoning_capability_cache` (`:780-797`) swallows the resulting error
  and returns `None`. Verified end to end: writing a `harnesses.grok` row makes the loader return
  `None`, so the grok branch is unreachable through the real loader and one grok row also destroys the
  codex and droid rows in the same file.
- [B9] **pi and omp reject effort levels their own CLIs accept.** Live on this box,
  `pi --help` documents `--thinking <level>  Set thinking level: off, minimal, low, medium, high,
  xhigh, max` and `omp --help` adds `auto`. `PI_NATIVE_EFFORTS` (`reasoning.py:78`) stops at
  `low, medium, high, xhigh, max`, so `delegate pi safe --reasoning-effort off` fails with
  `unsupported_reasoning_effort`. The same flag value reaches the child fine through the alias door:
  with `pi.models.<alias> = {model, thinking: "off"}` the built argv is `pi … --thinking off`, because
  `_pi_request_parts` (`request_build.py:3231-3240`) constructs the capability directly on
  `thinking_source == "alias"` with no enum check. `auto` is reachable through no path at all —
  `PI_THINKING_LEVELS` omits it too.

## LATENT

- [L1] **A command-local option typed after the prompt is silently ignored.**
  `delegate --json dry-run claude safe --model claude-opus-4-8 "x"` yields `model: claude-opus-4-8`;
  `delegate --json dry-run claude safe "x" --model claude-opus-4-8` yields `model: null`, exit 0, no
  warning, no `warnings` field. The trailing prompt is variadic, so the flag becomes prompt text. The
  help line says "Use --prompt-file or stdin for long or flag-like prompts" but nothing warns that an
  option after the prompt is absorbed; `docs/cli-reference.md:1162` documents the rule for `resume`
  handles only. A user who reaches for a bigger model gets the default one and never learns.
  `_build_pi_family_argv` (`argv_builders.py`) already rejects a prompt whose first character is `-`
  or `@` — the same guard applied to the first prompt token would close this.
- [L2] **Four of ten engines have no branded version fingerprint.**
  `_CANONICAL_VERSION_PATTERNS` (`harness_discovery.py:83-99`) covers grok, codex, claude, devin, omp
  and cursor. droid, kimi, opencode and pi fall through to `_GENERIC_VERSION` plus a basename match, so
  any executable with the right name printing a bare semver is registered as that harness. Repro: put
  four one-line shell scripts named `droid`, `kimi`, `opencode`, `pi` that `echo "1.2.3"` first on
  PATH, and `resolve_harness_selector` returns each with `version='1.2.3'`, no error and no warning.
  The catalog probe fails afterwards, so the mistake surfaces as `probeStatus: error`, not as a
  correct identification. Real pi does print a bare `0.85.1`, so a branded pattern may not be
  available for it; the other three could not be checked here because they are not installed.
- [L3] **A binary present but unrunnable is reported as not installed.**
  `probe_harness` (`harness_discovery.py:1502-1511`) maps any `resolve_harness_selector` error other
  than `probe_missing` to `installed: false`, and `setup` then prints
  "Install and authenticate the claude CLI, then rerun delegate setup." Reproduced by running
  `delegate --json setup` under a temporary `HOME`, which breaks the `estate-*` wrapper scripts the
  local config pins: every installed harness came back `installed: false, probeStatus: "error"`.
  `shutil.which` had found all of them. The next-action text sends the user to reinstall a CLI that is
  already there.
- [L4] **Codex usage-limit reset parsing matches one English 12-hour phrasing.**
  `failover_state.parse_reset_epoch` matches only `try again at H:MM AM|PM`. Any other wording, a
  24-hour clock, or an ISO timestamp silently returns `None` and the cooldown falls back to the
  30-minute `_DEFAULT_COOLDOWN_SEC`. Nothing records that the reset time was not understood.
- [L5] **Failover cooldown state is codex-only.** `_VALID_TOOLS = frozenset({"codex"})`
  (`failover_state.py:13`) makes `check_blocked`, `write_block` and `clear_block` no-ops for the other
  nine engines. `child_failures.classify` will happily return `usage_limit` for any harness, but only
  codex gets a persisted block, so a rate-limited claude or cursor lane retries into the same wall.
- [L6] **`binding_not_active` is matched as a whole anchored line.**
  `child_failures._BINDING_NOT_ACTIVE_PATTERN` requires
  `^estate-harness: binding_not_active: Broker returned HTTP 403: binding_not_active$` with only
  trailing whitespace allowed. Any change to the broker's wording, an added detail suffix, or a
  colorized banner drops the classification to `codex_thread_lost`/`auth_failed`/`child_failed` and the
  operator is told to fix a token that is fine. This one is estate-local rather than vendor-driven, so
  it will not move on its own, but nothing tests the broker string against the broker.
- [L7] **The raw-stdout fallback is disabled by a single parsed line.**
  `runner.py:4805-4816` falls back to raw stdout only when `structured_events_seen == 0`, and
  `harness_events.py:527` increments that counter for any dict payload, recognized or not. One
  `session` banner or `turn_start` permanently disables the fallback for the run. Tracked runs still
  keep the bytes on disk (`delegate run-output <id> --raw`), `call` mode does not.
- [L8] **Three engines have no in-flight tool protection in the stall watchdog.** The module docstring
  promises "While a tool or command execution is in flight the run is never stalled." That protection
  is `_pending_tools` in `stalled_for` (`stall_watchdog.py:520-524`), and only handlers that emit
  `tools_started` populate it. `_classify_opencode` returns `LineSignals(progress=True)` for `tool_use`
  and never a `tools_started`; `_classify_grok` models only `text`/`thought`/`end`/`error`; devin is a
  plain-text stream. Repro (`stall_seconds=480`, one representative line each, polled at t=600):

  | harness | tools_in_flight | `stalled_for(600)` |
  | --- | --- | --- |
  | opencode | 0 | 600.0 |
  | grok | 0 | 600.0 |
  | devin | 0 | 600.0 |
  | claude | 1 | None |
  | codex | 1 | None |
  | pi | 1 | None |

  So an opencode, grok or devin run sitting silent inside a 20-minute test command is cancelled at the
  8-minute default, which is exactly the false kill the module says it is designed to avoid.
- [L9] **Unhandled event types are an allowlist enforced by omission, with no counter.**
  `_ingest_object` (`harness_events.py:632-636`) documents the drop honestly, but
  `{"type":"stream_error",...}` and `{"type":"system","subtype":"error",...}` both produce no events and
  no terminal. Codex item types other than `agent_message` and `command_execution` are dropped
  (`:950-966`); the pi/omp chain (`:1178-1282`) has no `else`; opencode events without a `part` dict
  return early (`:1073-1075`); `_ingest_system` (`:743-749`) keeps only `cwd` and `session_id`. Nothing
  counts unhandled types, so a vendor adding an error event ships silently.
- [L10] **A field rename in any harness yields empty output, never an error.** `_extract_text`
  (`harness_events.py:1436-1449`) returns `""` for anything outside its three accepted shapes.
  Verified: `{"type":"completion","text":"THE ANSWER"}` (droid), `{"type":"result","subtype":"success",
  "response":"…"}` (claude), `{"type":"assistant","text":"THE ANSWER"}` (cursor) all give
  `assistant_text=""` and `completion_text=None`. Combined with L7 this is the highest-blast-radius
  failure mode on any harness version bump.
- [L11] **claude, cursor, kimi and devin have no captured event fixtures.** codex, opencode and pi have
  real captures; omp's `simple_text.jsonl` is a single `session` line whose test asserts it parses to
  nothing; droid's and grok's READMEs state outright that they are hand-written. The cursor and claude
  parsers own the `assistant`/`user`/`result` envelope (`harness_events.py:844-948`) that carries final
  report extraction for the two most-used engines, and B3 sits squarely inside it.
  `tests/test_harness_events.py:431-437` already argues the case for real captures.
- [L12] **A claude schema over 128 KB fails with `E2BIG`.** Claude's schema rides argv as one string.
  Linux `MAX_ARG_STRLEN` is 131072 bytes regardless of `ARG_MAX` (measured: `/bin/true` with a
  131073-byte argument raises `OSError` errno 7). Delegate's argv-size guard covers prompts only, and
  only for `("cursor","kimi","omp")` (`prompt_transport.py:44-45`, enforced at
  `request_build.py:3529-3532`); there is no guard on `--json-schema`. Claude Code's own guard is
  node-count based (1e5), which a 130 KB schema need not reach. The failure is at least clean —
  `runner.py:2065-2073` turns the `OSError` into `child_launch_failed`.
- [L13] **Codex reasoning vocabulary is declared in three places that disagree.** Bundled
  `gpt-5.6-sol` (`reasoning.py:18-40`) includes `max`; `CODEX_HARNESS_DEFAULT_REASONING_EFFORTS`
  (`request_build.py:146`) omits it; the live config on this box adds `ultra`, which no hardcoded copy
  has. The narrowing is defensible — it gates only the unset-`defaultModel` path
  (`request_build.py:2864-2879`) — but neither site says so. Separately, `GROK_NATIVE_EFFORTS` is a
  bare alias of `CLAUDE_NATIVE_EFFORTS` (`reasoning.py:75`), so editing Claude's tuple silently
  redefines a different vendor's enum, and grok's catalog probe never emits `harnessReasoning`, so
  nothing can ever correct it.
- [L14] **A codex stage silently loses native schema enforcement.** `_codex_native_schema`
  (`workflows/runtime.py:3764-3777`) swallows `SchemaPreflightError` and returns `None`, dropping to
  prompt-and-parse with no journal event and no warning. The contract still holds because
  `validate_value` runs afterwards, but the degradation is invisible. `resume_command.py:819-826`
  does warn on the equivalent resume path, which is the behavior to copy.

## SIMPLIFY

- [S1] **Tool-event handling in `harness_events.py` is table-shaped.** Four target extractors
  (`_opencode_tool_target:1484`, `_tool_use_target:1501`, `_tool_target:1527`, `_kimi_tool_target:1512`,
  49 lines) differ only in container path and key-priority list; eight tool-event emitters
  (`:798`, `:823`, `:852-869`, `:878-898`, `:968`, `:1105`, `:1307`, `:1325`, `:1334`, 157 lines) run
  the identical five steps; `_codex_command_status:1467` and `_opencode_tool_status:1475` are the same
  function. A per-engine spec table plus one emitter deletes roughly 130 of 1553 lines. The
  text/terminal lane is genuinely irreducible and should be left alone: opencode's step-local text
  reset (`:1057-1066`), pi/omp's revocable terminal (`:1284-1305`), grok's out-of-band delta buffer,
  codex's candidate promotion (`:968-990`, explained at `:196-201`), and devin's line merge
  (`:907-926`) are five different state machines, not five field spellings.
- [S2] **`reasoning.py` has about 250 to 270 collapsible lines of 1504 (~17%).**
  Three 4-line alias-summary trampolines (`:971-986`) that `functools.partial` removes; the
  opencode/pi alias-summary pair (`:1067-1136`) that differs only in three literals;
  `build_alias_reasoning_summaries` (`:1139-1290`) where the claude, grok, kimi, devin and opencode
  blocks are structurally identical and the pi/omp loop at `:1266-1277` already proves the pattern;
  `build_reasoning_capabilities_payload` (`:1293-1409`) where the kimi and devin blocks differ only by
  name and `_unsupported_reasoning_fields` (`:150-155`) already exists; and the capability resolvers
  (`:409-746`), where `resolve_grok_reasoning_capability` (`:613-628`) is provably `_lookup_declaration`
  with `harness="grok"` — verified identical across all five precedence cases. The four resolution
  strategies themselves are four real algorithms and the evidence ladder is public surface in
  `delegate --json capabilities`; that bulk earns its keep.
- [S3] **omp could move off argv prompt transport today.** `omp --help` on 18.1.13 documents
  `MESSAGES   Messages to send (prefix files with @)` and the example `omp @prompt.md @image.png "…"`,
  so a prompt file is a first-class input. omp is a pi fork (its own flags name `PI_SMOL_MODEL`,
  `PI_SLOW_MODEL`, `PI_PLAN_MODEL`) and shares `_build_pi_family_argv` with pi, which delegate already
  drives over stdin. Moving omp to file or stdin deletes `OMP_PROMPT_REDACTION`, omp's entry in
  `ARGV_PROMPT_TRANSPORT_ENGINES`, and the `-`/`@` prompt rejection in `_build_pi_family_argv`, and it
  closes the real process-argv exposure the README already flags. Inferred, not proven: I did not
  confirm that omp reads stdin under `-p`, because doing so needs a live model call.
- [S4] **`validate_schema_subset` is stricter than either harness needs, and looser where it matters.**
  It rejects `$defs`, `definitions` and `format`, all of which Claude accepts and codex passes through,
  while accepting the four root shapes in B6 that both harnesses reject. Tightening the root check and
  relaxing the keyword allowlist would make one function match reality in both directions.
- [S5] **Two acceptance surfaces, neither modeling the Claude side.** `validate_schema_subset` has
  exactly three callers (`workflows/runtime.py:2912`, `:3541`, `workflows/script.py:176`); the
  CLI and JSON API `--output-schema` path never calls it. codex gets `normalize_codex_schema`, claude
  gets nothing on either surface. One shared preflight keyed by engine would collapse the divergence
  that produced B5.
- [S6] **`bundled_models.py` carries entries the harness no longer lists.** Codex bundled `gpt-5.4` is
  gone from `codex debug models` on this account, which now returns
  `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4-mini`,
  `gpt-5.3-codex-spark`, `codex-auto-review`, `gpt-daybreak-blue-latest`, `gpt-reserve`. Grok bundled
  `swe-1.7` is gone from `grok models`, which returns only `grok-4.6` and `grok-4.5`. All five cursor
  bundled ids are still present in `cursor-agent models`. The file's own docstring calls the tables
  advisory with harness enumeration authoritative, so this is drift in a hint, not a launch failure —
  but `delegate models <engine>` shows those ids to users, and `gpt-6-astra` is the account's priority-1
  model and is absent. The pi and omp bundled ids could not be judged: only cerebras, fireworks,
  google, openrouter, xai and zai providers are authenticated here, so `anthropic/*` and
  `openai-codex/*` are missing from the live catalog for account reasons, not vendor reasons.
- [S7] **Dead cursor rows in the reasoning cache.** `validate_cache_payload` accepts a `cursor` harness
  key, but `_cache_model_declarations` is called only from `_lookup_declaration:394` (codex and droid)
  and `:1307`. Validated, written, never read.
- [S8] **Dead grok branches around a permanently empty bundled table.** `reasoning.py:61-63` documents
  that grok rows will never exist; `:619`, `:1338-1343` and `:1356-1360` branch on it anyway.
- [S9] **`--output-schema` is documented on codex usage lines only.** `delegate help dry-run` lists it
  under codex, though the option description and the code support claude in every mode. Cosmetic.

## OK (verified)

- **The Grok-as-`agent` case is handled correctly.** `/home/trey-agent/.local/bin/agent` is a symlink
  to `/home/trey-agent/.grok/bin/agent` and prints `grok 1.0.13 (5e9a58528b76) [stable]`.
  `_PATH_CANDIDATES["cursor"]` tries `agent` first, `_identify_version` matches the branded grok pattern
  and returns `known-other`, and because the candidate is not explicit the resolver warns and falls
  through. `resolve_harness_selector(config.example.json, "cursor", env=os.environ)` returns selector
  `/home/trey-agent/.local/bin/cursor-agent`, version `2026.09.02-c22c1a3`, error `None`, warning
  `selector '/home/trey-agent/.local/bin/agent' identifies itself as grok, not cursor`. Discovery does
  not register Grok as Cursor.
- **`config.example.json` shipping `cursor.argvPrefix: ["agent"]` is safe.** The `explicit` flag in
  `selector_candidates` (`harness_discovery.py:516-521`) means "differs from the embedded default", and
  the embedded default is the same `["agent"]`, so the shipped config never triggers the hard
  `fingerprint_mismatch` refusal. A user-modified selector that resolves to another harness does refuse,
  which is correct.
- **The other five installed harnesses fingerprint correctly.** claude `2.1.263 (Claude Code)`, codex
  `codex-cli 0.153.4`, grok `grok 1.0.13`, omp `omp/18.1.12` (binary now reports 18.1.13), pi `0.85.1`;
  devin, droid, kimi and opencode return `probe_missing` because they are not installed.
- **The prompt transport matrix matches README lines 17-22 exactly.** From
  `delegate --json dry-run <engine> <mode> "x"`, reading `promptTransport`: stdin for codex, claude,
  opencode and pi; file for droid, grok and devin; argv for cursor, omp and kimi. The three tables that
  encode it — `describe_payload.py:914-923`, the ten `EngineRequestParts` in `request_build.py`, and
  the README prose — agree on all ten engines.
- **Cursor still forces argv.** `cursor-agent --help` at 2026.09.02 documents
  `Usage: agent [options] [command] [prompt...]` with no stdin, prompt-file, or `@file` option. The
  README's claim that argv-hiding for cursor depends on the child CLI is still true.
- **`bundled_models.py` and `KNOWN_ENGINES` agree.** Both cover exactly the same ten engines, no
  extras, no gaps. Every alias in `config.example.json` resolves to a model id, and the only aliases
  present are the two droid placeholders the file intends as placeholders.
- **All ten engines are present in `REASONING_PROFILES`**, and none appear there that are outside
  `KNOWN_ENGINES`. Engines outside `TRANSPORT_BY_HARNESS` raise a clean
  `ReasoningCapabilityError("unsupported_reasoning_effort")` (`reasoning.py:422-431`) rather than
  crashing. An unknown harness name would be a bare `KeyError` at several unguarded
  `REASONING_PROFILES[harness]` lookups, but no caller can reach that today.
- **Claude's discovered effort vocabulary matches its fallback.** The live
  `claude --effort __delegate_probe__ --help` probe returns exactly `low, medium, high, xhigh, max`,
  which is `CLAUDE_NATIVE_EFFORTS`.
- **`child_failures.classify` is fed trusted diagnostics only.** Its callers pass a bounded stderr tail
  plus `_accumulator_failure_signal_text` (`runner.py:3071-3095`), which draws from normalized
  `error` and `run.completed` events and redacts before returning. Assistant text is never classified,
  matching the module docstring. Substring matching on raw stdout does not happen anywhere in this path.
  The `API Error: 400` string from the Claude schema incident appears only in documentation, never as a
  matcher.
- **`terminal_states.py` is a clean vocabulary module.** Nine states, one frozenset, one operator-cancel
  override that strips child receipts. No per-harness branching, nothing to drift.
- **The stall watchdog's unknown-event fallback works as documented.** An unparseable line, a non-object
  payload, or a modeled engine's unmodeled event type all become a whole-line delta, so an engine this
  module has never seen still gets the "identical output forever" check and is never read as idle
  (`stall_watchdog.py:400-424`). The deliberate exception is pi/omp `message_update`, which returns no
  signals rather than falling back, because its accumulating blob differs on every line.
- **The watchdog's unmatched-finish handling cannot leak an in-flight tool forever**
  (`stall_watchdog.py:498-505`), so a stream that fails to correlate starts with finishes does not
  disable the watchdog for the rest of the run.
- **`failover_state` file handling is sound.** 0700 root, 0600 state files, `mkdir`-based locking with a
  30-second staleness sweep, atomic `os.replace`, and expiry taken as the max of proposed and existing so
  a shorter cooldown cannot shorten a longer one.
- **No engine silently ignores a structured-output request.** The CLI and JSON API refuse with
  `unsupported_output_schema` for all eight non-codex/claude engines, grok with its own message
  (`request_build.py:493-506`); workflows fall back to prompt embedding and still run `validate_value`.
  Both refusals verified live via `dry-run`.
- **The codex schema path is not the leak I first suspected.** `Request.output_schema_text` carries the
  normalized text (`request_build.py:3611`), and `_materialize_output_schema_argv`
  (`runner.py:1515-1540`, called at `:3702-3712`) rewrites the argv to a private copy under the run
  scratch directory when sandboxed, so a `/tmp` schema path is not invisible under
  `isolation.safeBackend: bwrap`. `docs/cli-reference.md:292` is accurate.
- **`--pure` with `--json-schema` on claude is not a flag conflict.** The StructuredOutput tool is
  appended after the `--tools` filter in the 2.1.263 bundle, and a live preflight of that exact
  combination passes. `--output-format text` with `--json-schema` also passes.
- **Boolean schema `true` is rejected everywhere** — by `validate_schema_subset`, by the codex
  normalizer, and by Claude's own preflight. Empty and duplicate root enums are caught locally by both
  delegate (`workflows/schema.py:96-104`) and Claude's Ajv compile.
- **Nested arrays of objects, missing `required`, and missing `additionalProperties` are all fine.**
  Only the root must be an object; codex repairs the latter two and warns.
- **Malformed-entry skips in the event parsers lose tool decoration only.**
  `_ingest_kimi_tool_calls:802-806`, `_ingest_assistant_event:850-852`, `_ingest_user_event:877-880` and
  `_kimi_tool_target:1520-1522` never discard text or terminal state, and the comment at `:1514-1519`
  correctly explains why the last must not raise on the drain thread.
- **The total-drop case is caught.** `runner.py:1002-1007` and `:4805-4816` set
  `RESULT_QUALITY_NO_ASSISTANT_TEXT` / `RESULT_QUALITY_EMPTY` when exit is 0 with no extracted text, so
  a run that loses everything is flagged rather than reported clean. The dangerous class is the partial
  drop in B1 through B4.
- **`opencode`'s error handler records a terminal even when `error` is a string, not a dict**
  (`harness_events.py:1139-1145`).
- **`model_discovery` source ranking is coherent.** `_SOURCE_RANK` orders bundled below cache below
  discovery/live below config, and `LIVE_UNSUPPORTED_ENGINES = {"claude"}` correctly reflects that
  Claude Code has no model-listing subcommand.

## Could not verify

- **droid, devin, kimi and opencode are not installed here.** Their version banners, catalog probe
  output, and event streams could not be checked against a real binary. L2 rests on the code path plus
  four fake binaries, not on those vendors' actual `--version` output.
- **pi and omp bundled model ids.** Only cerebras, fireworks, google, openrouter, xai and zai providers
  are authenticated on this account, so `anthropic/*` and `openai-codex/*` are absent from the live
  catalogs for account reasons. Whether the three bundled ids per engine are still valid selectors is
  unknown.
- **Whether omp reads a prompt from stdin under `-p`** (S3). Confirming it needs a live model call. The
  `@file` form is documented in `omp --help`, so a prompt-file transport is proven; stdin is inferred
  from the shared pi lineage.
- **Live Anthropic and OpenAI API rejections in B5 and B6.** These were established from the Claude
  Code 2.1.263 bundle's preflight code, from probes against a dead endpoint that show which shapes reach
  the API, and from the 400 recorded in the issue doc. No paid call was made to observe each individual
  rejection.
- **Whether the estate broker still emits the exact `binding_not_active` line in L6.** Not reproducible
  without provoking a 403.

## Sources

- `/home/trey-agent/.local/bin/agent` → `/home/trey-agent/.grok/bin/agent`, `--version` → `grok 1.0.13 (5e9a58528b76) [stable]` — established that the ambiguous `agent` basename is Grok on this machine.
- `cursor-agent --version` → `2026.09.02-c22c1a3`; `cursor-agent --help`; `cursor-agent models` — established Cursor's installed version, the absence of any stdin or prompt-file option, and that all five bundled cursor ids are still listed.
- `claude --version` → `2.1.263 (Claude Code)`; the CLI bundle at `/home/trey-agent/.local/share/claude/versions/2.1.263` — established the `--json-schema` preflight rules and that the object-root constraint is enforced by the API, not the CLI.
- `codex --version` → `codex-cli 0.153.4`; `estate-codex debug models` — established the live codex slug list and the `ultra` effort on `gpt-6-astra`.
- `grok models` — established that the account lists only `grok-4.6` and `grok-4.5`.
- `omp --version` → `omp/18.1.13`; `omp --help`; `estate-omp models --json --no-extensions` — established the `@file` prompt input, the `--thinking` level list including `auto`, and the authenticated provider set.
- `pi --version` → `0.85.1`; `pi --help`; `estate-pi … --list-models` — established the `--thinking` level list including `off` and `minimal`, and the authenticated provider set.
- `python3 -m pytest -q` in the checkout — 3046 passed, 15 skipped, exit 0, 428.65 s.
- `docs/issues/2026-09-07-claude-json-schema-non-object.md` — the recorded 400 that anchors B5.
