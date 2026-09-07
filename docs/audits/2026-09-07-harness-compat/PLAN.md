# Plan: make delegate-agent correct against every supported harness at its current release

Bead dlg-cjz · branch `feat/harness-compat-audit` · base main @ 12fe7ac · 2026-09-07 · **v2** after the
Astra medium review (run del_20260907T054027Z_749422 follow-up; punch list applied below, each
disposition recorded under Rulings)
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

### Lane S — structured output (`workflows/runtime.py`, `structured_output.py`, `runner.py`, the
claude branch of the direct `--output-schema` preflight in `request_build.py` via a helper S exports)
- S1 (shared B5/B6, claude B1) One engine-keyed eligibility helper in `structured_output.py`:
  `native_schema_eligible(engine, schema) -> str | None` (None when eligible, else a reason). Claude and
  codex require an explicit root `"type": "object"` (a string, not a union, not inferred from
  `properties`); claude additionally requires the serialized `--json-schema` argv value under
  120 000 bytes (Linux `MAX_ARG_STRLEN`; shared L12). `_is_object_node` stays what it is (a
  normalization predicate) and is not reused as the root rule. `_native_schema` returns `None` with the
  reason journaled as `agent_schema_prompt_path` (mirror `resume_command.py:819-826`; closes shared L14).
  The direct request path (`request_build.py` `--output-schema` for claude) calls the same helper and
  fails clearly (`schema_not_native`) instead of forwarding an argv the API will reject; codex keeps its
  existing preflight.
- S2 (shared B7) In the structured retry loop, an attempt that ends failed with no assistant text
  clears `native_schema` for the remaining attempts and journals `agent_schema_demoted`; the first
  demoted attempt always embeds the original schema in the correction prompt, including on the
  resumable path (`runtime.py:2926-2936, 2980-2982` today skip it). This is a bounded loss of native
  enforcement, never a change to the accepted value (`validate_value` still runs).
- S3 (claude S1/L5/L7) One Claude result-text extractor used by both transports: serialize a present,
  valid `structured_output` value, else the string `result`; `is_error` and pure-mode permission-denial
  checks stay independent of extraction. Lane E owns the stream-json `result` handler in
  `harness_events.py` and exposes the extractor; S calls it from `_parse_claude_call_json`, which also
  accepts a top-level object as well as the list shape.
- Tests: eligibility helper for array root, `{}`, enum-only root, `["object","null"]` union, type-less
  `properties`, `{"type":"array","properties":{}}`, oversize schema — both engines; direct claude
  `--output-schema` with an array root fails at preflight with `schema_not_native`; retry-loop test where
  the first attempt fails with no text and the second attempt's prompt carries the schema; claude parse
  tests for `structured_output` present/absent, list and object payloads, on both transports.

### Lane E — event parsing correctness (`harness_events.py`; sole writer in wave 1; also the cursor
tool-event branch of `stall_watchdog.py`)
- E1 (shared B1/B2) `_ingest_error_event`: read `message` or `error.message`; record a `failed` terminal
  (as grok/pi already do).
- E2 (shared B3) `result` with `is_error: true` and no string `result` → `failed` terminal carrying
  `subtype` as the reason.
- E3 (shared B4/L7) kimi/opencode/pi non-JSON stdout lines: keep the malformed-tool-output protection
  (never promote a raw envelope to an answer), but record a bounded, redacted `stream.malformed`
  diagnostic (count + first 200 chars of the first three) instead of dropping silently. A run that ends
  with no assistant text and malformed lines seen is marked incomplete through the existing
  no-assistant-text quality path (`runner.py:998-1004, 4805-4811`); a later valid assistant text clears
  it. Tests: error→EOF and error→recovery.
- E4 (cursor B1, claude L4) Pinned continuity uses exact identity plus engine-specific evidenced
  equivalents, no generic containment: cursor compares the served display name against the catalog's
  `displayName` for the requested id (`parse_cursor_catalog`, `harness_discovery.py:1037`); claude
  accepts served `<requested>-<8 digits>` for a concrete id, and for the documented family aliases
  (`opus`, `sonnet`, `haiku`, `fable`, with an optional `[…]` suffix stripped) a served id whose family
  segment matches. Aliases with no evidenced mapping (`best`, `opusplan`) fail pinned preflight with a
  clear error (Lane A wires the dry-run check). The mid-run switch check is unchanged.
- E5 (grok B3/L1/L2/L3/L4) On each `usage` event seal the current response as the candidate and reset
  only the active buffer; promote the last sealed response at `end` (never an earlier tool preamble; a
  single-response stream whose only `usage` precedes `end` must still deliver its text). Classify
  `max_tokens`, `max_turn_requests`, and the `max_turns_reached` event as incomplete terminals; handle
  `tool_call_update` → `tool.completed` with status; read `rawInput`/`target_file` for tool targets;
  capture `sessionId` and usage/cost from `end`. Rewrite the multiturn test against the real shape.
- E6 (pi B1/L5, omp L6) Accept `compaction_start`/`compaction_end` alongside `auto_compaction_*`, and
  classify an aborted compaction (`aborted: true`, `willRetry: false`) as failed even without a preceding
  provider error (today `_pi_recovery_error` is required first); manual/successful compaction and retry
  recovery unchanged. `stopReason: "deferred"` → incomplete terminal; `notice` with `level: "error"` →
  error event.
- E7 (codex S1/L3) Capture `turn.completed.usage`; clear the completion-report candidate on every
  non-`agent_message` item type.
- E8 (cursor B2, moved from T1) Cursor tool events dispatch on `(type, subtype)`; the tool name comes
  from the single `*ToolCall` key with `args` nested inside it; `tool.completed` carries status. The
  stall watchdog's cursor branch gets the same `(type, subtype)`/`call_id` start–finish handling so
  pending tools resolve.
- E9 (kimi L2) `goal.summary` (no `role` key) is preserved as a bounded terminal reason with
  status/reason/turns/tokens; the non-zero exit stays a failure unless the status is `complete`.
- E10 (shared L9/L10, merges old E8 + I1) Count unhandled event types per run (bounded to 32 distinct
  types) and expose `unhandledEventTypes` on the accumulator; Lane I surfaces it in the manifest and
  snapshot and reuses the existing no-assistant-text warning when structured events were seen but no
  text was extracted.
- Tests: one focused test per item; new fixtures under `tests/fixtures/` captured from the real binaries
  where installed (cursor `tool_call` started/completed payloads, grok multi-response stream, claude
  result object with `structured_output`), recaptured with the cheapest model.

### Lane A — argv, transport, and flags (`argv_builders.py`, `prompt_transport.py`, `request_build.py`,
`describe_payload.py`, `mail_core.py`, `command_help.py`, `cli_parser.py`)
- A1 (cursor S1, omp S1) Move cursor and omp to `PROMPT_TRANSPORT_STDIN`. Delete `CURSOR_PROMPT_REDACTION`,
  `OMP_PROMPT_REDACTION`, both from `ARGV_PROMPT_TRANSPORT_ENGINES`, the omp flag-like prompt guard in
  `_build_pi_family_argv`, the `prompt` parameter threading for omp, the omp/cursor branches in
  `describe_payload.py` and `mail_core.py`. Kimi stays on argv (verified still required). Verify with one
  tiny live call each (`printf … | cursor-agent -p --mode ask …`, `printf … | omp -p --mode json …`).
- A2 (cursor S2/S3) `cursor safe` and `cursor call --read-only` emit `--mode ask` (read-only per vendor
  docs, and the mode the audit's live stdin proof already used). Drop the duplicate `--print`. Verify
  stdin on the existing cursor followup and structured-resume paths, not only a fresh call.
- A3 (codex B1/L1) Replace `--search` with `-c web_search="live"` and `--ask-for-approval never` with
  `-c approval_policy="never"` (both emitted after `exec` with the other `-c` overrides). Update the
  describe/security text.
- A4 (kimi L3) Pass-through kimi runs emit `--output-format text` explicitly.
- A5 (omp L2/L3) omp work mode emits `--approval-mode yolo`; omp (only — pi's parser has no `--cwd`)
  passes `--cwd <workspace>`. Run the existing omp read-only behavioral probe
  (`DELEGATE_OMP_BEHAVIOR_TEST=1 python3 -m pytest tests/test_omp_read_only_behavior.py`) against
  18.1.13 and update the version string it cites; a yolo work smoke does not verify the safe boundary.
- A6 (claude S2) Emit `--permission-prompts none` for claude safe and read-only call when the probed
  help text lists the flag. (`ultracode` deferred: discovery overrides the static enum with a warning
  list that omits it, so an enum edit alone is unreachable; needs a compatibility exception with proof.)
- A7 (shared L1) In `cli_parser.py` warn when any token in `checked_prompt_parts` (excluding content
  after a literal `--`) is a recognized option name from the existing `known_options` set: "option after
  the prompt is treated as prompt text". Tests: `"x" --model …`, a quoted prose mention, explicit `--`.
- A8 (cursor L4, docs) README/agent-setup: verify Cursor with `cursor-agent --version`, not `command -v agent`.
- A9 (grok B1 strings, omp L5) Update the grok effort help strings in `command_help.py` and
  `describe_payload.py` (R1 owns the enum). Warn at request build when a resolved omp selector is absent
  from a fresh account catalog (omp resolves missing ids by fuzzy match); never hard-reject and never
  rewrite operator aliases.
- A10 (E4 support) Dry-run/preflight error for `--continuity-mode pinned` with an unmappable claude alias
  (`best`, `opusplan`).

### Lane R — reasoning and model tables (`reasoning.py`, `bundled_models.py`)
- R1 (grok B1) `GROK_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh")` as its own tuple (no alias of
  the claude set). Help strings are A9.
- R2 (droid B1) Remove `minimal` from `gemini-3.6-flash`.
- R3 (shared B9) `PI_NATIVE_EFFORTS` gains `off`, `minimal`; omp additionally `auto`, including the omp
  alias validation path (`PI_THINKING_LEVELS`, `config.py:1151-1154`) — R owns every effort-vocabulary
  edit. Tests: omp `auto` through the CLI and the alias path; pi still rejects `auto`.
- R4 (shared B8/S8) `validate_cache_payload` accepts a `grok` harness row without touching the routing
  tables (config → exact discovery → cache precedence preserved), and
  `resolve_grok_reasoning_capability` becomes `_lookup_declaration(harness="grok")`. Test a mixed
  codex/droid/grok cache file.
- R5 (codex L5, claude L6, shared S6) Refresh bundled tables from the reconciled live catalogs
  (coordinator ran `codex debug models` and `grok models` on 2026-09-07 05:55Z): codex add `gpt-6-astra`,
  `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.2` (all `visibility: list`), keep `gpt-5.4` and `gpt-5.4-mini`
  (present, hidden), drop `gpt-5.3-codex-spark` (absent); reasoning rows: sol default `low` + `ultra`,
  astra/terra `low`/`medium` + `ultra`, luna `medium` no `ultra`, 5.4-mini default `medium`; claude add
  `claude-opus-5`, `claude-sonnet-5`, `claude-fable-5-1`; grok `grok-4.6`, `grok-4.5`. pi/omp/devin
  untouched (absence on one account is not retirement).

### Lane D — discovery (`harness_discovery.py`, `model_discovery.py`)
- D1 (grok B2) `parse_grok_catalog` accepts `-` bullets; remove the `startswith("-")` break; extend fixture.
- D2 (opencode B1/S2) Selector regex `^([^\s/]+/\S+)`; delete dead `parse_opencode_models_output` and its
  two tests. (opencode L2) Probe env gets the same non-read-only overrides as launches
  (`OPENCODE_DISABLE_AUTOUPDATE=1`).
- D3 (droid B2) Custom-model selectors: `custom:<Display-Name>-<index>`, read `displayName` only.
- D4 (kimi L4) Empty `models` object → empty catalog with a warning, not `ValueError`.
- D5 (A6 support) `_probe_claude` records whether `--permission-prompts` appears in the help text
  (`capabilities.permissionPrompts`), the way `--append-system-prompt-file` is probed today.

### Lane B — boundaries and lifecycle (`cli.py`, `sandbox_bwrap.py`, `stall_watchdog.py` except its
cursor branch, the opencode env overrides in `request_build.py` are handed to Lane A as one named edit)
- B1 (devin B1) **Gate retained.** The `3000.4.x` pin stays until the read-only behavioral probe
  (`DELEGATE_DEVIN_BEHAVIOR_TEST`, `tests/test_devin_read_only_behavior.py`) passes on the target release;
  the docs-level argv compatibility with 3000.6.14 is recorded in `docs/cli-reference.md` as "documented
  compatible, behavior unproven". Blocked on a Devin-equipped machine — for Trey.
- B2 (kimi B1) Resolve the Kimi home as `KIMI_CODE_HOME` if set, else `$HOME/.kimi-code`; add it to
  `_ENGINE_HOME_ENV_VAR`-style handling so it is rw-bound exactly once for the selected engine (mounts are
  deduplicated by destination at `sandbox_bwrap.py:392-397`, so it must be excluded from the read-only
  candidates when writable), included in the writable-root intersection checks (`:486-495`), and never
  bound for other engines. Tests: default and overridden home, workspace intersection, other engines see
  no Kimi home.
- B3 (shared L5) **Deferred.** Non-codex cooldowns need engine/auth-identity-scoped write and check
  integration (`runner.py:3136-3147, 3738-3750` are codex-only); widening `_VALID_TOOLS` alone changes
  nothing. Recorded as a follow-on, not claimed.
- B4 (shared L8, kimi L1) `stall_watchdog`: opencode `tool_use` (status running/completed) and grok
  `tool_call`/`tool_call_update` populate `tools_started`/`tools_finished`. For kimi and devin, whose
  stdout is silent during tool execution by design, the engine-default stall detector is disabled only
  when the run carries a finite timeout (standalone tracked runs have no deadline otherwise,
  `runner.py:3658`); an explicit operator `stallMinutes` is always honored. Documented in Lane I.
- B5 (opencode L1/L2) Read-only opencode overrides add `OPENCODE_DISABLE_CLAUDE_CODE=1` (edit in
  `request_build.py`, made by Lane A on B's behalf); the discovery probe env gets
  `OPENCODE_DISABLE_AUTOUPDATE=1` (Lane D, D2).

### Lane P — skill-review preamble switch (Trey, 2026-09-07) (`prompt_instructions.py`, `config.py`,
`request_build.py:effective_prompt`, `config.example.json`, `docs/configuration.md`)
- P1 New switch `tracking.skillReviewPreamble.enabled` (default false) beside
  `tracking.completionReport`, validated inside the tracking section; helper
  `skill_review_preamble_enabled(config)`. The decision stays at request assembly
  (`request_build.py:3512`), the formatter does not load config. Call, slash pass-through, and
  `--pass-through` prompts never get it; safety prefixes, personas, and mail suffix unchanged. Tests: default off → prefix absent; enabled → present; pass-through → absent
  either way. Update the fixtures in `tests/test_execution_argv_and_prompt.py` and siblings that assert
  the prefix.

### Lane M — agent-to-agent mail (Trey, 2026-09-07 mid-turn) (`mail.py`, `mail_core.py`, `mail_push.py`,
`notify.py`, mail keys in `config.py`, `tests/test_mail*.py`, `docs/configuration.md` `## Mail`)
- M1 Design from the recon report `mail.md` (pending): simplify the seam to one config key
  (`mail.enabled`, default **true**), one CLI override, one prompt suffix, one stop-hook path; a machine
  without `post` must be completely unaffected (silent degrade, one-time warning at most). Exact scope
  is fixed after the recon lands and gets its own short Astra review before implementation.

### Lane I — integration and docs (after waves 1–2)
- I1 Surface `unhandledEventTypes` and the E10 no-text warning in the run manifest/snapshot
  (`runner.py`, `snapshot_view.py`).
- I2 Docs: README transport paragraph; `docs/cli-reference.md` (cursor/omp transport, grok efforts,
  codex flags, opencode `--verbose`, claude efforts, kimi/devin stall note, devin version range, skill
  preamble, devin "documented compatible, behavior unproven", kimi/devin stall rule); `docs/security-model.md` (codex approval via config override, droid safe is
  harness-enforced, kimi 0.41 wording, pi `--no-approve` is trust not tool-approval, omp 18.1.13,
  cursor `--mode plan`); `docs/configuration.md` (`prompt.skillReviewPreamble`, `codex.profile` is a
  `<name>.config.toml` file).
- I3 `delegate doctor`: warn when `codex.profile` names a file that does not exist (codex L2).

### Lane T — behavior-preserving collapse, optional wave 3 (`reasoning.py` only)
- T1 **Dropped.** The tool-event table conversion of `harness_events.py` hid a required fix (now E8) and
  the shared audit itself warns the text/terminal lanes are five different state machines. The
  demonstrated small deduplications stay (R4 lookup reuse; dead codex `turn.error`/`turn.cancelled`
  branches deleted in E if no other harness emits them; dead `parse_opencode_models_output` in D).
- T2 (shared S2/S7) `reasoning.py`: `functools.partial` for the alias-summary trampolines; merge the
  opencode/pi summary pair; one loop for the structurally identical engine blocks in
  `build_alias_reasoning_summaries` and `build_reasoning_capabilities_payload`; drop dead cursor cache
  rows. Public payload shapes unchanged (existing tests assert them). Only if wave 2 is green with time
  left; no line-count target.

## Waves and ownership

| Wave | Lanes (parallel, disjoint files) | Engine |
| --- | --- | --- |
| 0 | P (coordinator, in-tree, lands first; every lane branches from that checkpoint) | Fable + Sonnet for test edits |
| 1 | S, E, A, R, D, B, M — each in its own worktree/branch | native Opus for E and A; Astra high for M; Sol high via delegate for S, R, D, B |
| 2 | I (after wave 1 merged and gate green) | Opus |
| 3 | T2 (optional) | Sol high |
| review | each wave: GLM 5.3 (omp) + Cursor Grok + Opus review of the merged diff, verify, fix, re-verify | per wave-review |

File ownership (writes): `request_build.py` → A only (including B5's env edit and the claude
`--output-schema` preflight call that S exports as a helper); `command_help.py`, `describe_payload.py`
→ A; `reasoning.py`, `bundled_models.py`, the effort vocabularies in `config.py` → R; `harness_discovery.py`,
`model_discovery.py` → D; `harness_events.py` and the cursor branch of `stall_watchdog.py` → E; the rest of
`stall_watchdog.py`, `cli.py`, `sandbox_bwrap.py` → B; `workflows/runtime.py`, `structured_output.py`,
`runner.py` → S; mail modules → M. Test files follow their module (`tests/test_harness_events*.py` → E,
`tests/test_engine_argv.py`, `tests/test_slash_passthrough.py`, `tests/test_delegate_parser.py` → A,
`tests/test_reasoning*.py` → R, `tests/test_model_discovery.py`, `tests/test_harness_discovery*.py` → D,
`tests/test_workflow_*schema*.py`, `tests/test_pure_call.py` → S, `tests/test_stall*.py`,
`tests/test_sandbox_bwrap*.py`, `tests/test_devin*.py` → B, `tests/test_mail*.py` → M). A lane that needs
a test outside its set adds a new file named for its lane.

Merge order for wave 1 (by dependency): R, D, B, S, E, A, M. Per lane: focused tests + `ruff check`/
`ruff format --check` on touched files. Per merged checkpoint: `scripts/gate.sh` once (it runs pytest,
compileall, ruff check, ruff format); rerun only after further changes or a failure.

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
- Devin gate **not** widened (Astra #1): a security gate is not loosened on documentation alone; the
  behavioral probe is Trey's to run on a Devin-equipped machine. Cost if wrong: `devin call --read-only`
  stays refused on current Devin until then.
- Followup/resume for the six newly capable harnesses deferred (capability, not defect).
- Astra #11: non-codex failover cooldown deferred rather than half-done.
- Astra #13: claude `ultracode` deferred (unreachable through discovery without an exception).
- Astra #20: `harness_events.py` table refactor dropped; `reasoning.py` collapse optional wave 3.
- Astra #18: preamble switch shipped as `tracking.skillReviewPreamble.enabled`.
- Astra #15: cursor safe uses `--mode ask` (evidenced live), no paid mode experiments.
