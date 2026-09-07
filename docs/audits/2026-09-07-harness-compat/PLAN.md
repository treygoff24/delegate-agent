# Plan: make delegate-agent correct against every supported harness at its current release

Bead dlg-cjz · branch `feat/harness-compat-audit` · base main @ 12fe7ac · 2026-09-07
Evidence: eleven audit reports under `docs/audits/2026-09-07-harness-compat/` (one per harness plus
`shared.md`), each citing vendor docs, the installed binary, or vendor source at the current tag.
Baseline: `python3 -m pytest -q` → 3046 passed, 15 skipped, 429 s.

## Goal

Every BROKEN finding fixed, every LATENT finding that can produce a wrong run outcome fixed, and every
SIMPLIFY finding taken where it deletes code *and* removes a defect, all without adding a capability that
needs a live harness we cannot verify tonight. Plus one product change Trey asked for: the auto-injected
skill-review preamble goes behind a config switch, default off.

Not in scope (recorded so it is a decision, not an omission): new followup/resume support for grok, droid,
opencode, pi, kimi, devin (all verified possible; all additive; all need a live round-trip to prove);
switching Grok to `streaming-messages-json` (needs a captured fixture first); Claude `--restricted`
(ignores settings files, could strip estate env — needs Trey); Droid `--auto` tiers (policy choice);
Devin `--export` (format unpublished); relaxing `validate_schema_subset` keywords (`$defs`, `format`).

## Design principles that shaped the item list

1. **Fix at the engine boundary, not in shared validation.** Array-rooted schemas are legitimate on the
   prompt-and-parse path; only the two native-enforcement engines reject them. So the object-root rule
   lives in `_native_schema`, using the predicate that already exists (`structured_output._is_object_node`).
2. **When a harness now offers stdin, use it and delete the argv carve-out.** Cursor (verified live) and
   Oh My Pi (verified in shipped source) both read a piped prompt. That deletes redaction constants, the
   100 KiB argv guard membership, and the flag-like-prompt rejection, and it closes a real exposure: today
   the full prompt sits in `/proc/<pid>/cmdline`.
3. **Make silent drops loud.** Four parser paths lose an error while keeping the pre-error text, which
   turns a failed run into a clean success. Each fix is a few lines; together they are the most valuable
   change in the plan. Add one counter (`unhandledEventTypes`) so the next vendor rename is visible.
4. **Prefer a table to a copy.** The tool-event lane of `harness_events.py` and the alias/summary blocks of
   `reasoning.py` are the same steps repeated per engine; collapse them only after the correctness fixes
   land and the suite is green, as behavior-preserving commits.
5. **A security gate is loosened only to the range the vendor docs verify, and it stays a gate.**

## Items

Legend: (audit ref) · file · size. Items are grouped by lane; lanes own disjoint files.

### Lane S — structured output (`workflows/runtime.py`, `structured_output.py`, `runner.py`)
- S1 (shared B5/B6, claude B1) `_native_schema`: return `None` for `claude` unless
  `structured_output._is_object_node(schema)` and the serialized schema is under 120 000 bytes (Linux
  `MAX_ARG_STRLEN`, shared L12). For `codex`, `_codex_native_schema` returns `None` when the root is not an
  object node (today the normalizer half-repairs and ships an invalid root). Emit a journal warning
  `agent_schema_prompt_path` naming the reason (mirror `resume_command.py:819-826`, closes shared L14).
- S2 (shared B7) In the structured retry loop, a child attempt that produced no text at all
  (`child.text is None`) clears `native_schema` for the remaining attempts, so a launch-time rejection is
  retried on the prompt path rather than with the identical rejected schema. Journal it.
- S3 (claude S1/L5) `_parse_claude_call_json`: prefer the `structured_output` field when present and
  parseable; fall back to the `result` text. Accept both a top-level list and a top-level object for
  `--output-format json` (claude L7).
- Tests: unit tests on `_native_schema` for array root, `{}`, enum-only root, union-with-object root,
  oversize schema, for both engines; retry-loop test with a first attempt returning no text; claude parse
  test with `structured_output` present/absent and object-shaped payload.

### Lane E — event parsing correctness (`harness_events.py`; sole writer in wave 1)
- E1 (shared B1/B2) `_ingest_error_event`: read `message` or `error.message`; record a `failed` terminal
  (as grok/pi already do).
- E2 (shared B3) `result` with `is_error: true` and no string `result` → `failed` terminal carrying
  `subtype` as the reason.
- E3 (shared B4) kimi/opencode/pi: stop hard-discarding non-JSON stdout lines; route them through the
  same recoverable-text path the other engines use (omp already does). Keep `structured_events_seen`
  semantics.
- E4 (cursor B1, claude L4) Pinned continuity: compare requested vs served with a normalizer (lowercase,
  alphanumerics only, strip a trailing `[…]` context suffix) and accept when equal or when one contains
  the other (`composer-2.5` ~ `Composer 2.5`, `haiku` ~ `claude-haiku-4-5-20251001`). The mid-run switch
  check (served changes after first observation) is unchanged. Aliases that cannot match (`best`,
  `opusplan`) produce a dry-run warning in Lane A.
- E5 (grok B3/L1/L2/L3) Reset `_grok_text_buffer` on each `usage` event (the documented per-response
  boundary); classify `max_tokens` and `max_turn_requests` as incomplete terminals; handle
  `tool_call_update` → `tool.completed` with status; read `rawInput` (and `target_file`) for tool targets;
  capture `sessionId` and `usage`/`total_cost_usd` from `end`. Rewrite the multiturn test against the real
  shape (single terminal `end`).
- E6 (pi B1/L5, omp L6) Accept `compaction_start`/`compaction_end` alongside `auto_compaction_*`;
  `stopReason: "deferred"` → incomplete terminal; `notice` with `level: "error"` → error event.
- E7 (codex S1/L3) Capture `turn.completed.usage`; clear the completion-report candidate on every
  non-`agent_message` item type (not only `command_execution`).
- E8 (shared L9/L10) Count unhandled event types per run (`unhandledEventTypes: {type: n}`) and expose it
  through the accumulator; Lane I surfaces it.
- Tests: one focused test per item; new fixtures under `tests/fixtures/` captured from the real binaries
  where installed (cursor tool_call payload, grok multi-response stream, claude result object) — the
  auditors' captures are quoted in their reports; recapture with the cheapest model where needed.

### Lane A — argv, transport, and flags (`argv_builders.py`, `prompt_transport.py`, `request_build.py`,
`describe_payload.py`, `mail_core.py`, `command_help.py`, `cli_parser.py`)
- A1 (cursor S1, omp S1) Move cursor and omp to `PROMPT_TRANSPORT_STDIN`. Delete `CURSOR_PROMPT_REDACTION`,
  `OMP_PROMPT_REDACTION`, both from `ARGV_PROMPT_TRANSPORT_ENGINES`, the omp flag-like prompt guard in
  `_build_pi_family_argv`, the `prompt` parameter threading for omp, the omp/cursor branches in
  `describe_payload.py` and `mail_core.py`. Kimi stays on argv (verified still required). Verify with one
  tiny live call each (`printf … | cursor-agent -p --mode ask …`, `printf … | omp -p --mode json …`).
- A2 (cursor S2/S3) `cursor safe` and `cursor call --read-only` emit `--mode plan` (read-only, per vendor
  docs; implementer runs one tiny review prompt under `plan` and `ask` and keeps the one that returns a
  normal review answer; record the choice). Drop the duplicate `--print`.
- A3 (codex B1/L1) Replace `--search` with `-c web_search="live"` and `--ask-for-approval never` with
  `-c approval_policy="never"` (both emitted after `exec` with the other `-c` overrides). Update the
  describe/security text.
- A4 (kimi L3) Pass-through kimi runs emit `--output-format text` explicitly.
- A5 (omp L2/L3) omp work mode emits `--approval-mode yolo`; pi-family argv passes `--cwd <workspace>`
  (both pi and omp document it).
- A6 (claude S2/L1) Emit `--permission-prompts none` for claude safe and read-only call when the probed
  help text lists the flag; add `ultracode` to `CLAUDE_NATIVE_EFFORTS`.
- A7 (shared L1) Warn when the first prompt token begins with `-` and matches a known option name
  (`--model`, `--reasoning-effort`, …): "option after the prompt is treated as prompt text".
- A8 (cursor L4, docs) README/agent-setup: verify Cursor with `cursor-agent --version`, not `command -v agent`.

### Lane R — reasoning and model tables (`reasoning.py`, `bundled_models.py`)
- R1 (grok B1) `GROK_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh")`; fix the two help strings.
- R2 (droid B1) Remove `minimal` from `gemini-3.6-flash`.
- R3 (shared B9) `PI_NATIVE_EFFORTS` gains `off`, `minimal`; omp additionally `auto`.
- R4 (shared B8) `validate_cache_payload` accepts a `grok` harness row (or drop the dead grok
  declaration branches S8 — implementer picks the smaller diff that makes the loader stop returning None).
- R5 (codex L5, claude L6, shared S6) Refresh bundled tables from the live catalogs on this box:
  codex (`codex debug models`: add `gpt-6-astra`, `gpt-5.6-terra`, `gpt-5.6-luna`; sol default `low`, add
  `ultra`; 5.4-mini default `medium`; drop `gpt-5.3-codex-spark`), claude (add `claude-opus-5`,
  `claude-sonnet-5`, `claude-fable-5-1`), grok (`grok-4.6`, `grok-4.5`). Leave pi/omp/devin (account- or
  doc-ambiguous).

### Lane D — discovery (`harness_discovery.py`, `model_discovery.py`)
- D1 (grok B2) `parse_grok_catalog` accepts `-` bullets; remove the `startswith("-")` break; extend fixture.
- D2 (opencode B1/S2) Selector regex `^([^\s/]+/\S+)`; delete dead `parse_opencode_models_output` and its
  two tests. (opencode L2) Probe env gets the same non-read-only overrides as launches
  (`OPENCODE_DISABLE_AUTOUPDATE=1`).
- D3 (droid B2) Custom-model selectors: `custom:<Display-Name>-<index>`, read `displayName` only.
- D4 (kimi L4) Empty `models` object → empty catalog with a warning, not `ValueError`.

### Lane B — boundaries and lifecycle (`cli.py`, `sandbox_bwrap.py`, `failover_state.py`,
`stall_watchdog.py`, `request_build.py` env overrides for opencode)
- B1 (devin B1) Widen `_DEVIN_READ_ONLY_TRANSPORT_VERSION` to `3000\.(?:4|5|6)\.\d+`. The gate stays.
  The behavioral probe (`DELEGATE_DEVIN_BEHAVIOR_TEST`) cannot run here (no binary) — flagged for Trey.
- B2 (kimi B1) `_OPTIONAL_ENGINE_DOT_DIRS["kimi"] = ".kimi-code"`, and since Kimi writes sessions there,
  rw-bind it the way the selected engine's home is handled (implementer inspects `wrap_engine_argv` and
  keeps the "only the selected engine's home is writable" rule).
- B3 (shared L5) `failover_state._VALID_TOOLS` covers every engine; reset-epoch parsing stays codex-only,
  others use the default cooldown.
- B4 (shared L8, kimi L1) `stall_watchdog`: opencode `tool_use` (status running/completed) and grok
  `tool_call`/`tool_call_update` populate `tools_started`/`tools_finished`. For kimi and devin, whose
  stdout is silent during tool execution by design, stall detection is off by default (documented; the run
  timeout remains the backstop).
- B5 (opencode L1) Read-only opencode overrides add `OPENCODE_DISABLE_CLAUDE_CODE=1`.

### Lane P — skill-review preamble switch (Trey, 2026-09-07) (`prompt_instructions.py`, `config.py`,
`request_build.py:effective_prompt`, `config.example.json`, `docs/configuration.md`)
- P1 New config section `"prompt": {"skillReviewPreamble": false}` (default off), validated like the
  other sections; helper `skill_review_preamble_enabled(config)`. `effective_prompt` prepends
  `SKILL_REVIEW_PREFIX` only when enabled and not pass-through. Delete `prepend_skill_review_instructions`
  if no caller remains. Tests: default off → prefix absent; enabled → present; pass-through → absent
  either way. Update the fixtures in `tests/test_execution_argv_and_prompt.py` and siblings that assert
  the prefix.

### Lane I — integration and docs (after waves 1–2)
- I1 Surface `unhandledEventTypes` and a `no_text_extracted` warning (structured events seen, no text)
  in the run manifest/snapshot (`runner.py`, `snapshot_view.py`).
- I2 Docs: README transport paragraph; `docs/cli-reference.md` (cursor/omp transport, grok efforts,
  codex flags, opencode `--verbose`, claude efforts, kimi/devin stall note, devin version range, skill
  preamble); `docs/security-model.md` (codex approval via config override, droid safe is
  harness-enforced, kimi 0.41 wording, pi `--no-approve` is trust not tool-approval, omp 18.1.13,
  cursor `--mode plan`); `docs/configuration.md` (`prompt.skillReviewPreamble`, `codex.profile` is a
  `<name>.config.toml` file).
- I3 `delegate doctor`: warn when `codex.profile` names a file that does not exist (codex L2).

### Lane T — behavior-preserving collapses (wave 3, one writer per file, after everything above is green)
- T1 (shared S1) Tool-event lane of `harness_events.py` → per-engine spec table + one emitter; cursor
  becomes a row (cursor B2: dispatch on `(type, subtype)`, tool name from the single `*ToolCall` key,
  `args` nested). Delete `_codex_command_status`/`_opencode_tool_status` duplicate, dead codex
  `turn.error`/`turn.cancelled` branches if no other harness emits them (codex S2). Target ≈ −130 lines.
- T2 (shared S2/S7/S8) `reasoning.py`: `functools.partial` for the alias-summary trampolines; merge the
  opencode/pi summary pair; one loop for the structurally identical engine blocks in
  `build_alias_reasoning_summaries` and `build_reasoning_capabilities_payload`;
  `resolve_grok_reasoning_capability` → `_lookup_declaration(harness="grok")`; drop dead cursor cache rows.
  Target ≈ −250 lines. Public payload shapes unchanged (asserted by existing tests).

## Waves and ownership

| Wave | Lanes (parallel, disjoint files) | Engine |
| --- | --- | --- |
| 0 | P (coordinator, in-tree) | Fable |
| 1 | S, E, A, R, D, B — each in its own worktree/branch | native Opus for E and A (largest, most coupled); Sol high via delegate for S, R, D, B |
| 2 | I (after wave 1 merged and gate green) | Opus |
| 3 | T1, T2 (separate worktrees) | Astra high (T1), Sol high (T2) |
| review | each wave: GLM 5.3 (omp) + Cursor Grok + Opus review of the merged diff, verify, fix, re-verify | per wave-review |

Merge order for wave 1: R, D, B, S, A, E (smallest to largest; E last because it is the hottest file and
its tests exercise everything else). Gate after every merge: `python3 -m pytest -q` + `ruff check` +
`tests/acceptance.sh`.

## Verification that must be real

- Every fix lands with a test that fails on the pre-fix code (mutation check by the lane, re-run by the
  coordinator on the merged tree).
- Live smoke, cheapest model, one call each, recorded in `docs/audits/2026-09-07-harness-compat/SMOKE.md`:
  cursor stdin + `--mode plan`; omp stdin + `--approval-mode yolo`; claude array-root workflow stage
  (must now take the prompt path and succeed); codex `-c web_search="live"` argv accepted; grok
  `--effort xhigh` accepted and `max` rejected at preflight; pi `--thinking off` accepted.
- Not verifiable here: devin (no binary), droid (no binary), kimi (no binary), opencode (no binary).
  Their changes are unit-tested against the vendor-documented shapes only; stated in the final report.

## Rulings so far

- Grok stays on `streaming-json`; the wire-format swap is a follow-on with a fixture prerequisite.
- Devin gate widened to the docs-verified range without the behavioral probe; gate retained.
- Followup/resume for the six newly capable harnesses deferred (capability, not defect).
