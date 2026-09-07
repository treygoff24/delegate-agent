# Grok Build compatibility audit

Latest version: **1.0.13** stable (2026-08-28, https://x.ai/build/changelog; machine-readable channel endpoint https://x.ai/cli/stable returns `1.0.13`). Alpha channel is `1.0.22` (https://x.ai/cli/alpha) with no published notes. · Installed here: **1.0.13** (`grok --version` → `grok 1.0.13 (5e9a58528b76) [stable]`), so the installed binary is the current stable release.

Delegate assumptions checked: 34 · BROKEN: 3 · LATENT: 7 · SIMPLIFY: 4 · OK: 20

**Install path note (requested):** `/home/trey-agent/.local/bin/grok` and `/home/trey-agent/.local/bin/agent` are both symlinks into `~/.grok/bin/`, and both resolve to the identical binary (`md5sum` on each path returns `4d49cfe1f825a1d3d8d6389fcd426a7f`). This is not a local convention: the vendor install script sets `BIN_DIR="${GROK_BIN_DIR:-$HOME/.grok/bin}"` and creates the `agent` symlink itself (`ln -sf "$link_target" "$BIN_DIR/agent"`, https://x.ai/cli/install.sh). Delegate's extra PATH entry `~/.grok/bin` (`src/delegate_agent/cli.py:113`) is correct. The `agent` name is a vendor alias for the same executable, not a second harness.

**Documentation quality:** the public site at https://docs.x.ai/build/cli/reference is thin and omits `--prompt-file`, `--permission-mode`, `--json-schema`, and `--include-partial-messages`. The authoritative reference is the user guide shipped inside the vendor's own Apache-2.0 repo, `https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md`. That file is the only place the exit codes, the full `streaming-json` event table, and the `stopReason` vocabulary exist. Both are vendor-controlled, so both are primary. There are no GitHub releases, no git tags, and no in-repo changelog.

---

## BROKEN

### [B1] Delegate emits `--effort max`, which grok 1.0.13 rejects outright

Delegate declares `GROK_NATIVE_EFFORTS = CLAUDE_NATIVE_EFFORTS` at `src/delegate_agent/reasoning.py:75`, and `CLAUDE_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh", "max")` at `src/delegate_agent/reasoning.py:73`. `max` is not a valid effort for the installed Grok CLI. The same claim is repeated to users in `src/delegate_agent/command_help.py:504` and `src/delegate_agent/describe_payload.py:1211`, both of which state the mapping is "(low, medium, high, xhigh, max)".

Harness truth, from the binary itself:

```
$ grok --output-format streaming-json --effort max --prompt-file ./tiny.txt
{"type":"error","message":"--effort/--reasoning-effort: unknown effort level 'max'; use one of: xhigh, high, medium, low"}
exit=1
```

Delegate accepts it and plans the argv anyway:

```
$ python3 bin/delegate.py --json dry-run grok safe --reasoning-effort max "Review only."
{"argv": [..., "--effort", "max", "--prompt-file", "<prompt file>"], "ok": true,
 "resolvedReasoningEffort": "max",
 "warnings": ["grok reasoning effort was validated against the harness compatibility enum; model-specific support is not known."]}
```

The failure costs a full launch: workspace isolation runs, the prompt file is written, the child starts and dies with exit 1 before any model call.

There is a real subtlety worth recording, because it changes the fix. The vendor guide states the *canonical* level set is broader than what any one model accepts: "Canonical levels: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (each a distinct tier; a model only accepts the levels its menu advertises)" (14-headless-mode.md line 37). So `max` is a real tier in the protocol; it is simply not on `grok-4.6`'s menu, and the CLI validates against the selected model's menu. Delegate's enum is therefore not wrong about the protocol, it is wrong about what is reachable, and it also omits `none` and `minimal`, which the same table lists as valid. This is corroborated by https://docs.x.ai/developers/release-notes, which says Grok 4.6 supports low, medium, high (default), and xhigh.

Smallest fix: change `GROK_NATIVE_EFFORTS` to `("low", "medium", "high", "xhigh")` so it stops aliasing Claude's set, and update the two help strings. The durable fix is to stop treating the Grok effort set as static: the profile at `src/delegate_agent/reasoning.py:111` is declared `"static-enum"`, but effort validity here is per-model and the CLI enumerates the accepted levels in its own error text.

### [B2] Live model discovery silently drops every non-default model

`parse_grok_catalog` (`src/delegate_agent/harness_discovery.py:1249`) parses `grok models` output with `re.match(r"^\*?\s*(\S+?)(?:\s+\(default\))?$", stripped)`. That pattern anchors to end-of-line and allows only a `*` prefix. Grok 1.0.13 marks the default with `*` and every other model with `-`, so the `-` lines match nothing and are skipped without error.

Real output and the resulting parse:

```
$ grok models
You are not authenticated.

Default model: grok-4.6

Available models:
  * grok-4.6 (default)
  - grok-4.5
```

```
$ PYTHONPATH=src python3 -c "import json;from delegate_agent.harness_discovery import parse_grok_catalog;print(json.dumps(parse_grok_catalog(open('grok-models.txt').read())))"
{"probeStatus": "ok", "modelScope": "account", "defaultModel": "grok-4.6",
 "models": {"grok-4.6": {}}, "harnessReasoning": null, "warnings": []}
```

`grok-4.5` is gone, and `probeStatus` is `"ok"` with no warning, so nothing downstream can tell that discovery was lossy. This is why the defect survived: the fixture at `tests/fixtures/discovery/grok_models.txt` contains a single `*` line and no `-` line, so it can never exercise the branch.

Smallest fix: accept `-` as a bullet as well as `*`. The existing `if selector.startswith("-"): break` guard inside the loop was written for a different format and should be removed alongside, or it will terminate the list at the first non-default model. Add a `-` entry to the discovery fixture.

### [B3] Multi-response preamble is concatenated into the final answer

`_ingest_grok_text` accumulates into `_grok_text_buffer` and only `_ingest_grok_end` flushes it (`src/delegate_agent/harness_events.py:992` and `:1004`). The design assumed grok emits an `end` event per assistant turn: `tests/test_harness_events.py:1801` (`test_grok_multiturn_tool_use_end_does_not_promote_preamble`) feeds an intermediate `{"type":"end","stopReason":"ToolUse"}` between two turns and asserts that only the last turn becomes `completion_text`.

Grok 1.0.13 does not emit intermediate `end` events. The vendor guide is explicit: "`end` is always the last event" (14-headless-mode.md line 239), and the per-response boundary is the `usage` line, whose `stopReason` carries the verbatim provider reason such as `tool_use` or `pause_turn` (lines 242-243). My live capture confirms exactly one `end` for a two-response run.

Replaying my captured real 1.0.13 stream through the accumulator:

```
completion_text: 'Hi — I’ll keep this short. Checking the register rule so I greet you in the right voice.Hi.'
terminal_event:  {'event': 'completion', 'status': 'succeeded'}
```

The mid-run preamble is promoted into the delivered answer, glued to the real answer with no separator (`...right voice.Hi.`). Same result on the documented canonical shape:

```
PREAMBLE LEAKED: True
completion_text: "I'll inspect the repo first.Status: completed\n- final answer"
expected       : 'Status: completed\n- final answer'
```

This is BROKEN rather than cosmetic because `completion_text` is the run's published answer, and every Grok run that calls a tool is multi-response. The suppression test passes only because its fixture encodes a stream shape the harness no longer produces.

Smallest fix: reset `_grok_text_buffer` on each `{"type":"usage"}` event, the documented per-response boundary, keeping the last response's text. Delegate already does exactly this for OpenCode steps (`_reset_opencode_step_text_state`), so the pattern exists. Rewrite the multiturn test against the real shape (`usage` boundaries, single terminal `end`).

---

## LATENT

### [L1] `max_tokens` and `max_turn_requests` produce no terminal marker and no answer

The documented `end.stopReason` vocabulary is closed and small: `end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled` (14-headless-mode.md lines 241-242). Delegate classifies three of the five. Matrix run against the accumulator:

| `end.stopReason` | terminal_event | provider state | completion_text |
| --- | --- | --- | --- |
| `end_turn` | completion / succeeded | none | set |
| `max_tokens` | none | none | none |
| `max_turn_requests` | none | none | none |
| `refusal` | end / failed | provider_refusal | none |
| `cancelled` | grok.end / cancelled | provider_cancelled | none |

`max_tokens` normalizes to `maxtokens`, which is in neither `_CANCELLED_REASONS` nor `_FAILED_REASONS` (`src/delegate_agent/harness_events.py:282`), so `_normalize_terminal_status` returns `None`. `max_turn_requests` likewise misses `_PROVIDER_MAX_TURNS_CODES = {"max_turns", "maximum_turns", "error_max_turns", "turn_limit"}` (`:302`). Both fall through to `_record_recoverable_assistant_text`, leaving the run with no terminal event and no completion text while `status_from_exit` (`src/delegate_agent/runner.py:325`) still reports success on exit 0.

The code comment at `harness_events.py:1011` says this conservatism was deliberate because the non-success spellings were unverified against grok 0.2.73. They are now verified and documented, so the guess can become a decision. Adding `max_turn_requests` to `_PROVIDER_MAX_TURNS_CODES` and `maxtokens`/`maxturnrequests` to the truncation handling would classify both as incomplete rather than as silent success.

### [L2] `tool_call_update` is ignored, so no Grok tool is ever recorded as completed

`_ingest_object` has no branch for `tool_call_update`; the only `toolCallId`/`rawOutput` handling in the file is for Pi and Cursor shapes. The documented event carries `status`, `rawOutput`, `content`, `locations` (14-headless-mode.md line 232). Replaying my real capture yields `events: [('tool.started', 'read_file', None), ('run.completed', None, None)]` for a run that completed one read. Tool progress therefore only ever advances, never resolves, and no tool error status is visible.

### [L3] Grok tool events carry no target path

`_tool_target` (`src/delegate_agent/harness_events.py:1527`) looks at `path`, `file`, `command`, `target`, `uri`, then at an `args` dict. Grok puts tool arguments under `rawInput` (ACP leaf naming), so the lookup always misses. My real capture contains `"rawInput":{"target_file":"/home/.../SKILL.md","limit":80}` and the accumulator produced `target=None`. Progress lines read `read_file` with no file. Adding `rawInput` to the dict probe and `target_file` to the key list fixes it.

### [L4] The event-type list is explicitly open and delegate treats it as closed

The vendor states: "Grok may also emit `max_turns_reached` and `auto_compact_*` events; treat the list as non-exhaustive and switch on `type`" (14-headless-mode.md line 247). Delegate ignores all three (`max_turns_reached`, `auto_compact_start`, `plan` all yield `terminal_event: None`). Ignoring `plan` and `auto_compact_*` is harmless. Ignoring `max_turns_reached` is not: it is a second, independent signal of the same truncation `max_turn_requests` describes, and delegate discards both.

### [L5] Grok test fixtures encode a harness version three minor releases old

`tests/fixtures/grok_streaming_json_smoke.jsonl` and `tests/fixtures/grok_streaming_maxtokens.jsonl` use CamelCase `EndTurn`/`MaxTokens`, and the second literally says `"stream shape validated against grok 0.2.73"`. Real 1.0.13 emits snake_case `end_turn`/`max_tokens`, which the guide confirms is the "snake_case ACP/Messages token" (line 134). The normalizer strips non-alpha characters, so the case change alone is absorbed and nothing breaks today. The latent problem is evidentiary: the fixtures no longer resemble live output, they contain none of the `usage`, `tool_call_update`, or `available_commands` events a real run produces, and one of them (via the multiturn test) encodes the stream shape that B3 shows is now wrong. A green run of these fixtures is not evidence about grok 1.0.13.

### [L6] `--verbatim` is never emitted and default prompt handling is undocumented

Grok exposes `--verbatim` ("Send prompt exactly as given", `grok --help`, and 14-headless-mode.md line 43). `build_grok_argv` never emits it. What non-verbatim mode does to a prompt is documented nowhere I could find on either vendor surface. Delegate's wrapped prompts contain a safe-review prefix and a completion-report suffix, and delegate deliberately supports slash pass-through for Grok (`tests/test_slash_passthrough.py:85`). If the default mode expands slash commands or `@`-references, a wrapped prompt containing such text would be transformed before the model sees it. Not emitting `--verbatim` is correct for the slash pass-through path and unverified for the wrapped path. I could not test this without a paid call whose result I could not distinguish from ordinary model behavior.

### [L7] Delegate's sandbox vocabulary and the vendor's disagree on the "no sandbox" name

`GROK_WORK_SANDBOX_VALUES = ("workspace", "devbox", "read-only", "strict")` (`src/delegate_agent/config.py:63`), and `docs/security-model.md:54` classifies `none` as the unsandboxed value. The vendor names the unsandboxed profile `off` and calls it the default (https://docs.x.ai/build/features/sandbox). I confirmed empirically that both `off` and `none` are accepted by the binary, and that all four values delegate allows resolve successfully, so nothing fails today. The risk is in the security-model classification table, which keys off a spelling the vendor does not use: a config or policy that says `off` would not match the `none` branch. Worth aligning the documented vocabulary with the vendor's.

Two further sandbox facts that bear on `docs/security-model.md:54`, both from https://docs.x.ai/build/features/sandbox: child-network restriction is Linux-only and a no-op on macOS for `read-only`/`strict`, and the built-in profiles do not protect `~/.ssh`. Delegate's framing of Grok sandbox flags as "advisory defense-in-depth" behind its own workspace isolation (`describe_payload.py:1206`) is the right posture and remains accurate.

---

## SIMPLIFY

### [S1] Grok supports session resume; delegate refuses it and throws the session id away

`grok --help` documents `-r, --resume [<SESSION_ID_OR_TITLE>]`, `-c, --continue`, `--fork-session`, and `-s, --session-id <SESSION_ID>`, with title-vs-UUID resolution rules also covered in the vendor guide. Every `end` event carries `sessionId` (`"sessionId":"01a07a43-b753-7393-b04a-398da097dfff"` in my capture).

Delegate captures nothing: `_capture_session_id` (`src/delegate_agent/harness_events.py:708`) has branches for codex, claude, cursor, and omp, and none for grok, so `acc.session_id` is `None` after a full real run. `build_grok_argv` has no `resume_session_id` parameter at all, unlike `build_claude_argv`. Grok is in the refusal list at `tests/test_followup_refusals.py:123`.

This is a capability gap rather than dead code, so it adds rather than deletes. The cost is small: one `elif` in `_capture_session_id` reading `payload.get("sessionId")` on the `end` event, and a `--resume` branch in `build_grok_argv` mirroring the Claude one at `argv_builders.py:330`. Removing grok from the followup-refusal set is then the only behavioral change.

### [S2] Spend and turn accounting arrive on every run and are discarded

The `end` event carries `usage` (with `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `reasoning_tokens`, `total_tokens`), `num_turns`, `total_cost_usd`, and a per-model `modelUsage` breakdown. My capture recorded `"total_cost_usd":0.01369146` and `"modelUsage":{"grok-4.6-build":{...}}`. Delegate leaves `acc.usage` as `None` for grok; `_normalize_reported_usage` (`harness_events.py:423`) is only wired to the Cursor `result` event (`:939`). Its camelCase/snake_case dual lookup already handles Grok's field names. One call in `_ingest_grok_end` populates it.

Worth noting for whoever wires this: the guide warns that headless `input_tokens` is uncached-only, while ACP's `_meta.usage.inputTokens` is the full prompt sum. The two are not interchangeable.

### [S3] `--output-format streaming-messages-json` is the Messages wire format delegate already parses

Grok offers four output formats. `streaming-messages-json` is "NDJSON in the Anthropic Messages API wire format" and the guide states "the data-bearing surface matches the Messages shape exactly... A consumer that reconstructs messages, reads spend, or detects errors works without changes" (14-headless-mode.md line 251), including `assistant`/`user` bodies, `usage`, `tool_use`/`tool_result`, `stop_reason`, and a terminal `result` frame with `subtype`, `is_error`, `num_turns`, `total_cost_usd`, and `session_id`.

Delegate already has a full parser for that shape, since it is what Claude emits. Switching Grok to this format would let the entire Grok-specific ingestion path go: `_ingest_grok_text`, `_ingest_grok_end`, `_ingest_grok_error`, `_grok_stop_reason_succeeded`, `_refresh_grok_recovery_text`, the two `_grok_*` buffer fields, and the three grok branches in `_ingest_object` — roughly 90 lines in `harness_events.py` plus the two fixtures. It would also fix B3, L1, L2, and S1/S2 in one move, because the Claude path already handles per-message boundaries, `stop_reason`, tool results, session id, and usage.

I am flagging this as the highest-leverage item in the audit, with one caveat I could not close: the guide says the *data-bearing* surface matches, and lists Grok-specific omissions (`permission_denials` is omitted). Whether delegate's Claude parser tolerates those omissions needs a test against a captured `streaming-messages-json` run before anyone commits to the swap.

### [S4] `--json-schema` exists, and the v1 rejection reason is now only half true

`request_build.py:498-500` rejects `--output-schema` for grok, saying `--json-schema` "forces final json output" and would break streaming snapshots. The flag is real and the reasoning is sound as far as it goes: `grok --help` confirms "Implies `--output-format json`", which is a single terminal object with no stream to snapshot.

What has changed is that the conflict is now a format choice rather than a hard incompatibility, since delegate could run structured-output requests in `json` mode and non-structured requests in a streaming mode. Note that `--json-schema` is the single worst-documented flag in the product: it has no flag-table entry and no example on either vendor surface, and appears only in one passing clause noting that `structured_output` is snake_case. I would not build on it without an empirical schema-shape probe, which is the same class of hazard as the Claude object-root rule in `docs/issues/2026-09-07-claude-json-schema-non-object.md`. Keeping the v1 rejection is defensible; the stated reason should be corrected.

---

## OK (verified)

All flag checks below are against `grok --help` on the installed 1.0.13 binary, cross-checked against the vendor guide's flag table.

- `--cwd <CWD>` — present, "Working directory". `argv_builders.py:360`.
- `--output-format streaming-json` — present; enum is exactly `plain`, `json`, `streaming-json`, `streaming-messages-json`. Both values delegate emits are valid.
- `--prompt-file <PATH>` — present, "Single-turn prompt from a file". The transport delegate mandates at `argv_builders.py:408` is correct and current.
- **Stdin is still not supported, and this is now explicit.** The guide states: "Headless mode does not read piped stdin into the prompt. Pass external content through command substitution or `--prompt-file`" (14-headless-mode.md line 397). I also confirmed `--prompt-file -` is not a stdin idiom: it fails with `Failed to read '-': No such file or directory`. Delegate's hard rejection of any non-file transport (`argv_builders.py:403`) is correct, and the README statement the brief asked me to check is accurate.
- `--permission-mode <MODE>` — present, enum `default, acceptEdits, auto, dontAsk, bypassPermissions, plan`. All values delegate can emit (`dontAsk`, `auto`, `acceptEdits`, `default`, `bypassPermissions`) are valid. Invalid values are rejected by the argument parser with exit 2.
- `--always-approve` — present; delegate pairs it with `bypassPermissions` at `argv_builders.py:376`.
- `--sandbox <PROFILE>` — present, with env override `GROK_SANDBOX`. All four values delegate permits (`read-only`, `strict`, `workspace`, `devbox`) resolve successfully; an unknown profile fails at resolve time with exit 1 before the agent starts.
- `--disable-web-search` — present, "Disable web search and web fetch tools". Delegate's default-on behavior is correct.
- `--no-subagents` — present, "Disable subagent spawning".
- `--model <MODEL>` / `-m` — present. An unknown id fails with a JSON `error` line and exit 1.
- `--effort` — confirmed as a live alias of `--reasoning-effort`, listed as `[aliases: --effort]`. Delegate's use of the short spelling is fine.
- Config default `grok.safePermissionMode` cannot be `plan` (`config.py:861`) — `plan` is a real permission mode, so the restriction is a delegate policy choice, not a harness constraint, and it remains valid.
- **Exit codes**: `0` success, `1` error (auth, network, runtime), `130` SIGINT, `143` SIGTERM (14-headless-mode.md, Exit Codes table). I verified 0 and 1 directly, and 2 for argument-parser rejections, which the vendor table does not list. `status_from_exit` treats every non-zero code as failure, which is correct for 1, 130, and 143.
- **Error event shape**: `{"type":"error","message":"..."}` on stdout, plus the same text on stderr, plus exit 1. This matches the comment at `harness_events.py:1039` exactly. Verified three separate ways (bad effort, bad model, bad sandbox).
- `end.stopReason: "end_turn"` classifies as success. The 0.2.73-era fixtures use `EndTurn`; `_grok_stop_reason_succeeded` strips non-alpha characters, so both spellings normalize to `endturn`. The case change is absorbed.
- `end.stopReason: "cancelled"` → `provider_cancelled`, terminal status cancelled. Correct.
- `end.stopReason: "refusal"` → `provider_refusal`, terminal failed, via `_PROVIDER_REFUSAL_CODES`. Correct.
- Version detection: `_CANONICAL_VERSION_PATTERNS["grok"]` (`harness_discovery.py:86`) matches the real string `grok 1.0.13 (5e9a58528b76) [stable]`. Verified by running the compiled pattern against it.
- Model discovery selector: `_probe_grok` (`harness_discovery.py:1432`) invokes the `models` subcommand, which exists (`grok models`, "List available models and exit"). The subcommand and its invocation are right; only the output parsing is wrong (B2).
- `grok models` exits 0 even when it prints `You are not authenticated.`, and still emits a usable catalog. Delegate's parser ignores the leading banner line, so the auth state does not corrupt discovery. Notably the machine is in fact authenticated for agent runs despite that banner, so the banner is not a reliable auth signal for anything.
- Bundled model ids `grok-4.6` and `grok-4.5` (`bundled_models.py:70-71`) match what `grok models` reports today, and `grok-4.6` is the default in both. The vendor's own user guide is stale here in delegate's favour: `.../user-guide/11-custom-models.md` still says new sessions start with `grok-4.5`.

---

## Could not verify

- **`--effort none` and `--effort minimal`.** The vendor guide lists both as canonical tiers, but grok-4.6's error message enumerates only `xhigh, high, medium, low`. I did not test whether `none`/`minimal` are accepted-then-ignored or rejected, because a rejection is free but an acceptance costs a live model call. This matters only if delegate ever widens its enum; the B1 fix should narrow it, not widen it.
- **What non-`--verbatim` prompt handling actually does** (L6). Untestable without a paid call whose output I could not distinguish from normal model behavior.
- **Whether delegate's Claude Messages parser tolerates Grok's `streaming-messages-json`** (S3). Requires one captured live run in that format.
- **`--json-schema` request and response shape** (S4). Effectively undocumented by the vendor; would need an empirical probe.
- **Anything about alpha 1.0.22.** Nine releases exist beyond stable with no published notes anywhere. If the estate ever runs `grok update --alpha`, this audit does not cover that binary.
- **`streaming-json` behavior under `--include-partial-messages`.** Delegate never passes it, and the flag only affects `streaming-messages-json`, so it is out of scope today.

**Cost disclosure:** establishing the real event schema required one live Grok run, since no fixture or doc could have shown me the actual multi-response shape that B3 turns on. It cost $0.0137 (self-reported `total_cost_usd` on the `end` event). Every other probe in this audit was free: argument-parser rejections, an invalid model id, an invalid effort level, a missing prompt file, and offline replays of the captured stream through delegate's own accumulator.

---

## Sources

- `grok --help`, `grok agent --help`, `grok models --help`, `grok agent stdio|headless --help`, `grok --version`, `grok inspect` on the installed 1.0.13 binary — the complete installed flag surface, subcommand list, `--output-format` enum, `--permission-mode` enum, and the `--effort` alias.
- Live `grok --output-format streaming-json --prompt-file ...` run — the real 1.0.13 event stream: `available_commands`, `thought`, `text`, `tool_call`, `tool_call_update`, `usage`, `end`, with snake_case `stopReason`, a single terminal `end`, and full spend fields. Basis for B3, L2, L3, S1, S2.
- Live rejection probes (`--effort max`, `--effort bogus-effort`, `--model not-a-real-model-xyz`, `--sandbox bogus-profile`, `--permission-mode bogus`, `--max-turns 0`, `--prompt-file -`) — the effort enum, the error-event shape, exit codes 1 and 2, sandbox resolution ordering, and the absence of a stdin idiom.
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md — the authoritative headless reference: flag table, four output formats, full `streaming-json` event-type table, the closed `end.stopReason` vocabulary, the open-list warning, exit codes 0/1/130/143, the "does not read piped stdin" statement, and the canonical effort tier list.
- https://docs.x.ai/build/cli/reference and https://docs.x.ai/build/cli/headless-scripting — the public CLI reference; establishes that the public docs are a subset and that `grok models` is the sanctioned model-discovery path.
- https://docs.x.ai/build/features/sandbox and https://docs.x.ai/build/settings/reference — the five sandbox profiles (`off`, `workspace`, `devbox`, `read-only`, `strict`), `~/.grok/sandbox.toml` custom profiles, the Linux-only child-network restriction, and the `~/.ssh` caveat.
- https://x.ai/build/changelog — latest stable is 1.0.13, dated 2026-08-28; per-release notes for v1.0.0 through v1.0.13 and nothing beyond.
- https://x.ai/cli/stable → `1.0.13` and https://x.ai/cli/alpha → `1.0.22` — the version endpoints the installer itself queries.
- https://x.ai/cli/install.sh — `BIN_DIR="${GROK_BIN_DIR:-$HOME/.grok/bin}"` and the vendor-created `agent` symlink; confirms the install path and that `agent` is an official alias.
- https://docs.x.ai/developers/release-notes — Grok 4.6 reasoning effort supports low, medium, high (default), and xhigh; corroborates B1 independently of the binary.
- https://agentclientprotocol.com — Agent Client Protocol, confirmed by the vendor guide (`15-agent-mode.md`) as what "ACP" means in `grok --help`. `streaming-json` is derived from ACP session updates, not raw ACP.
- Offline replays of `tests/fixtures/discovery/grok_models.txt`, `tests/fixtures/grok_streaming_json_smoke.jsonl`, `tests/fixtures/grok_streaming_maxtokens.jsonl`, and the captured live stream through `delegate_agent.harness_events.StreamAccumulator` and `harness_discovery.parse_grok_catalog` — basis for B2, B3, and the L1 classification matrix.
