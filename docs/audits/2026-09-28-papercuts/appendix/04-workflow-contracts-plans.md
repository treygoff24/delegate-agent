# Workflow structured output, schemas and result contracts (F4)

Scope note: about 60 cuts. Roughly half of the failing code lives outside this repo, in the writing-plans compiler at `~/Code/writing-plans` (`src/planc/`, read only here). "delegate-agent" below means this repo at 0.31.0; "planc" means that compiler.

## Bottom line

Most of this family has already been fixed in code, mostly between 2026-08-23 and 2026-09-27. The ledger rarely records it. Of about 60 cuts, roughly 40 describe defects I could not reproduce in current code. What still hurts comes from three design gaps.

1. Each place that needs to know whether a schema will work on an engine decides for itself. The runtime, direct `--output-schema`, `workflow check` and planc do not share one decision.
2. planc's lane-facing prompt text and its result validators are two hand-written texts. Every mismatch was patched one sentence at a time, and nothing tests that a lane following the prompt passes the validator.
3. Fixes do not reach workflows that are already pinned or compiled. That makes old symptoms look like they keep recurring.

The best single change is one shared schema-compatibility function, called by the runtime, the direct-run path and `check`, and by planc at compile time. The second is a conformance test in planc that builds a lane result from the prompt's own contract text and runs it through the validator.

## Root causes

### RC-1: Schema compatibility is decided per engine and per entry point, not once

- Symptoms (7 cuts, 07-28 to 09-20): pc_e2a4cd287b02, pc2_7490fcac9201ee8b, pc2_6fc147eeda7eecea, pc2_6a73786c28fa7c23, pc2_3c7f5d97f0ccfbab, pc2_11fe1d1652b83a0f, pc2_5e9e24e244569472 (last one is arguably RC-4). The first two are open, the middle ones resolved or partly resolved.
- Evidence (verified):
  - `structured_output.py:36` `native_schema_eligible` sends any non-object-root schema to the prompt-and-parse path. This fixed the claude root-array case (66821d6d, 09-07).
  - `structured_output.py:145-175` `normalize_codex_schema` repairs a missing `required` and `additionalProperties`. Run on `{"type":"array","items":{"type":"object"}}`, it silently returns `{"type":"object","required":[],"additionalProperties":false}`. A free-form object becomes a closed, empty one and passes preflight (`runtime.py:5317` `_codex_native_schema`). That is the cause of the pc2_7490fcac 400. I could not confirm the exact OpenAI error text. The semantic corruption is verified.
  - `workflows/script.py:161-178` `_validate_literal_schemas` only checks `schema=` values that are literal dicts, and only against `SUPPORTED_KEYS`, which is Delegate's own subset. A schema passed by name (every planc-compiled bundle does this) is never checked. Nothing in `check` models codex strictness or Claude's `--json-schema` limits.
  - No `$schema` handling exists anywhere in `src/` (grep is empty). `request_build.py:588-625` `_preflight_claude_output_schema` never strips or rejects it. So the deslop failure (Claude rejecting draft 2020-12) still reproduces for a direct `--output-schema` run.
  - The root-array `{"items":[...]}` wrapper still fails today. I ran `schema.parse_json_tolerant` on `{"items":[{"a":"x"}]}` against an array schema and got "value must be 'array'". The retry prompt now re-renders the schema (`runtime.py:4248-4259`, fixes pc2_e84ccfc9). planc's own FINDINGS and REVIEW_RESULT are object-rooted, so the compiler no longer triggers this.
- Still live at 0.31.0?: partly. Live: bare-object corruption, no check-time model, `$schema` on direct Claude runs, and root-array wrapper leniency for hand-written scripts. Not live: optional-property codex failure (now demoted to the prompt path when `required` is partial) and root arrays on claude.
- Why it keeps recurring: each new engine limit was fixed at the call site that hit it (codex demotion, claude argv ceiling, claude root-object rule). There is no single "what will this engine do with this schema" answer that other entry points can ask. `check` is an AST literal scan, so it cannot see compiled schemas at all.
- Recommended fix: make `structured_output` return one verdict per (engine, schema): native, prompt-path, or refuse, each with a reason.
  - Make `normalize_codex_schema` raise `SchemaPreflightError` on a free-form object (an object with no `properties`), so the workflow path demotes it to prompt-and-parse.
  - Strip `$schema` at the claude adapter.
  - Have planc call the verdict in `plan-to-workflow` and `plan-lint` over its `contracts.py` schemas and any plan-authored schema. That is where the schema values are actually known.
  - Optionally accept a single-key `{"items": [...]}` wrapper (or any single-key object holding a list) for a root-array schema.
  - Proof: unit tests calling the verdict with a bare-object item, a `$schema` schema and a partial-`required` schema. The bare-object case must currently return "native" and must fail after the change.
- Alternative: keep the per-call-site behavior and just add the two missing rules (free-form object, `$schema`). Cheaper, and probably right for the next release. It leaves `check` blind.
- Beads/deferred: none directly. `docs/plans/deferred.md` has no ruling against this.

### RC-2: Lane prompt contract and validator are separate hand-written texts (planc)

- Symptoms (15 cuts, 08-31 to 09-20): pc2_748fed8eef240bda, pc2_8227d40e58cf4e58, pc2_bfa0383947b63620, pc2_d5de35f73f454b25, pc2_4e58c54ed4989d96, pc2_a977f4802266096d, pc2_4c77830dc57c2c6f, pc2_80a5fdfcfcf70955, pc2_d913ab6375d348dd, pc2_93a64682c3226541, pc2_0501e0ceb8e0b49c, pc2_93484d8bc06d71c1, and related. In every one a fully green lane was rejected for a rule the prompt did not state.
- Evidence (verified in planc):
  - The artifacts rule is now stated twice: `engine/guardrails.py:210-230` `_VERIFY_REPORT_CONTRACT` ("On any other row `artifacts` is `[]` or omitted; ... `artifacts-forbidden`"), and the brief text (f3270ac, 09-19). `contracts.py:585-600` also now treats an empty list as absence.
  - The close prompt now prints the literal declared row ids and states the convention: zero-based per task, `__workflow__` for workflow rows (`engine/close.py:763-780, 1732-1737`).
  - `build_ids` is stated to the lane (`close.py:1824`, "diff_nonempty exactly ...").
  - Blocked results exist as terminal states `blocked_human` and `blocked_dependency` (`contracts.py:20-38`), which answers pc2_93a64682.
  - Still open: the adjudicator persona (`skill/personas/adjudicator.md`) says nothing about the harness command policy or red-proofable verify rows (pc2_0501e0ce; grep for "rm -rf", "policy" and "red" finds nothing). `_ROW_ID_SCHEMA` is still `{"type":"array","minItems":4,"items":{}}` (`contracts.py:320`), tracked as bead dlg-1bg.
- Still live?: mostly no. Dates of the fixes: the rule-and-path surfacing landed in 31b40b7 (09-22), the blocked states and lane-report checks in e85b916/629f810 (09-25). Residual: the adjudicator prompt, the loose row-id schema, and the class itself.
- Why it recurs: prompts are prose assembled in `engine/*.py`, validators are separate functions in `contracts.py`, and no test links them. The 08-31 to 09-20 run of cuts on one workflow (wf_46f02cf62eb3) shows the pattern: each MUST-rule was found by paying for a full failing lane.
- Recommended fix (in planc): a conformance test that renders each lane prompt, builds the minimal compliant result the contract text describes (rows, notes, artifacts, ids), and requires the validator to accept it. Add one mutation per rule (drop the sentence, expect red). Longer term, generate the "MUST" sentences from a rule table that the validator also reads, so a new `ContractError` code cannot ship without prompt text.
- Alternative: keep fixing sentences as cuts arrive. It has worked since 09-19, but each defect costs one full lane.
- Related beads: dlg-1bg (open, P3, close prompt row-id example and tighter schema; still valid for the schema half). Cut ledger entries that say "fixed by a brief patch" (pc2_8227d40e) were workarounds; the real fix is the guardrails text above.

### RC-3: A rejected model result was all-or-nothing and its reason was dropped

- Symptoms (10 cuts, 08-11 to 09-20): pc2_2f63f7b0c9113e58, pc2_d0db3ab5907df745, pc2_e2136f6c357cb7a3, pc2_dd4d86da5ce9de65, pc2_e53d90f2c98fa7d1, pc2_9171c1123302cc64, pc2_3cae78be5a2b80f9, pc2_e82a25db07011656, pc2_2c0bb5a271268633 (blocker), pc2_841e90880fcf45ff.
- Evidence (verified):
  - delegate-agent side, all landed:
    - Tolerant parsing: raw control characters, fences, prose wrappers and JSON-in-string (`workflows/schema.py:191-347`, control characters in 2f7fb0e5, 09-24).
    - A fallback that reads the child's completion report when the structured channel fails (`runtime.py:5949`, 59de4c1e, 08-31).
    - Session-resume structured retries that re-render the schema (`runtime.py:4248-4259`).
    - Provider failures are not retried: only watchdog kinds and `empty_result` are (`runtime.py:137`).
    - `on_failure="typed"` returns `AgentFailure` carrying `lastParsedCandidate`, and planc uses it (`guardrails.py:1336`).
    - `reject(..., by=)` writes `rejectedBy` on `agent_rejected` (`runtime.py:2619`).
  - planc side, landed: `_contract_reason` yields `rule=...; path=...` for close, execute and fix (`engine/coverage.py:194`, `core.py:2012`). Park payloads carry the rejected result (`core.py:2436`).
  - The adjudication `dry` boolean is a rejected key for v2 regimes: `contracts.py:731` raises `legacy-dry-key`, and dryness derives from `fix_tasks` (`core.py:2474`). Only the deprecated non-v2 alias still trusts `value.get("dry")`.
  - The 757KB script problem is fixed by the thin loader plus a content-addressed engine bundle (44335df, 09-25).
- Still live?: partly.
  - Unclassified provider errors: `child_failures.classify` recognizes "usage limit", "insufficient_quota" and rate-limit-plus-account wording. I ran it on four plausible 403 out-of-credits phrasings and none classified. Such a child ends as `no_assistant_text` and is retried as `empty_result`. Whether xAI's real text matches is unknown; I used plausible phrasings, not the actual message (pc2_e82a25db).
  - `on_failure="typed"` is opt-in; the default `agent()` still returns `None` and drops the candidate.
  - "Re-emit to the same lane before rotating" (pc2_dd4d86da) is not present. Retries relaunch on a rotated route with the prior branch as a continuation note (`core.py:2031`).
  - The non-v2 `dry` alias remains.
- Why it recurs: acceptance was a validator verdict on one message. Salvage, classification and re-ask were added per incident, so each new output shape found a gap.
- Recommended fix:
  - Broaden the provider-failure classifier from real captured 403 texts, with a test per captured message.
  - Consider making a schema-valid `lastParsedCandidate` from a timed-out child available by default rather than opt-in.
  - Remove the non-v2 `dry` alias. It is a deprecated path that carries the exact defect in the blocker cut.
  - Proof: an engine test where a non-v2 adjudication returns `dry=true` with adopted dispositions and fix tasks must currently skip the fix round.
- Alternative: leave the alias and add a gate for "adopts exist with no fix task". Smaller, but keeps a second dryness definition.
- Beads: dlg-bum (journal rows carry label/item and timeouts), dlg-oub (name the refusal check), dlg-ao0 (status says why paused). All open, all diagnostics, none blocks correctness.

### RC-4: Compiled plans baked run-environment facts at the wrong time (planc)

- Symptoms (8 cuts, 09-02 to 09-20): pc2_8c2990cfc527419c, pc2_0e5fd701e439854b, pc2_8a57204f86ff6cec, pc2_3476925cdffe673d, pc2_4c4dda68d04f4b5d, pc2_95460fd377f8a715, pc2_bab90c47889b12e5, pc2_cdec9e77c1773cac; plus pc2_7974d5181adf1ede and pc2_c8d23f2ada0abe53 (reviewer-preamble path).
- Evidence (verified):
  - The preamble text and its digest now travel in the compiled workflow and are checked before every review dispatch (`engine/core.py:1360-1380`).
  - The build-start marker comes from the plan (`close.py:1665-1670`).
  - Missing acceptance scaffolding refuses at compile time (`core.py:461-468`, `close.py:1698`).
  - The audit row takes a repo-relative plan path with `--relative` (`close.py:1673-1690`).
  - `resolve_item_thread_cap` goes through Delegate's own loader (`guardrails.py:1402`).
  - Workspace env is a launch-time data file that resume replays (docs/delegate-workflows.md, "workspace-env.json"; `workflow resume` refuses `--env`).
  - `MAIN_BRANCH` is bound explicitly with `globals().update(...)` (`loader.py:99`).
- Still live?: mostly no. Residuals:
  - The final-close audit row still asserts exit 0 for a reporting tool (pc2_3476925c second half).
  - The `globals()` pattern remains in the engine (`close.py:1092`, `coverage.py:219`).
  - The delegate-agent docs do not say a workflow script body is wrapped in a function (`workflows/script.py:65` `_workflow_body`), which is why module-level names are not in `globals()` (pc2_95460fd). Verified from the wrapper; the docs grep for "wrapped" and "module-level" is empty.
  - Whether spine-side merge verify rows receive the workspace env is unknown. `runtime.py:5220` uses `state.attempt_environment`; I did not trace what it holds.
- Why it recurs: the first compiler emitted constants and relative paths as literals. The design fix, treating the compiled plan and engine as pinned, self-contained artifacts, is the thin-loader work of 09-25.
- Recommended fix: add one paragraph to docs/delegate-workflows.md on the script namespace. Add a planc test that merge verify rows see plan env if that is the intended contract.
- Alternative: none needed.

### RC-5: Dry-run had no contract with generated plans (mostly fixed)

- Symptoms (6 cuts, 08-27 to 08-31): pc2_f001f3356c7fff98, pc2_1d470a4ddc586743, pc2_b46735891b816aa5, pc2_a7ce0d96fa35e821, pc2_52d99028a656eebe, pc2_612291b84ec39dbe.
- Evidence (verified):
  - Runtime: the script gets a `dry_run` global, dry-run item threads are daemons, `workflows.dryRunTimeoutSeconds` raises `dry_run_timeout` (`workflows/commands.py:788-828`, c73edf5, 08-27). A test asserts a non-zero exit (`tests/test_workflow_commands.py:650`), so pc2_a7ce0d96's "exit 0" is fixed.
  - planc: a nominal-only simulation does not load or save plan state, does not run verifies and does not write receipts (`engine/simulation.py`, fe8e7da, 09-06). The loader also skips the state owner claim when dry (`loader_runtime.py:528`).
- Still live?: only for hand-written gated scripts (they wait until the timeout by design, and the docs say so) and for bead dlg-x0z (dry-run resume can cancel and reap a stale scope child; open P2).
- Recommended fix: fix dlg-x0z with a `state.dry_run` guard, as the bead describes.

### RC-6: Fixes do not reach pinned or compiled workflows, and "resolved" often meant a workaround

- Evidence: the runtime is pinned per workflow (`workflow_pinning.py`; commits 43f5f246 08-27, 58e64691, 6ad4895e 09-05). planc pins a plan and an engine bundle by digest (44335df). A fix to the parser or the engine therefore affects only workflows launched or recompiled afterward. The recurrence dates fit this but do not prove it: string-payload "value must be object" reappeared on 09-08 (pc2_5e9e24e2) after the 08-27 decoder fix, and a fenced adjudicator failed on 08-29 (pc2_d7907447) after the 08-23 parser. I cannot see which runtime those workflows had pinned, so this is inferred.
- Also verified: the pin's `sitecustomize` imports `delegate_agent.personas` at every Python startup whenever `DELEGATE_WORKFLOW_PIN` is set, and verify-row children deliberately keep the pin env (`runtime.py:5237`). Tests of `delegate_agent` itself inside those rows see the pinned modules (pc2_d58a5de7). This is narrow (it affects only projects that import `delegate_agent`), and it is a consequence of the "nested delegate invocations must run the pinned runtime" ruling, not an oversight.
- Recommended fix: show the pinned runtime digest in `workflow status`, and note in the release notes which fixes need `plan-reload` or a relaunch. If the sitecustomize import matters, install the resolver lazily on first `personas` import.
- Alternative: accept as the price of pinning.

## Cuts that are not this family's problem

- pc2_85998ef165df1f4e, pc2_10951dfa354d61db, pc2_bf4a0323ede10b45, pc2_ff78a4efdc00bdcc: the "cluster prompt" requiring `COMMON.md` in `~/Code`. `~/Code/COMMON.md` does not exist and the string is not in `src/`. This comes from an external cluster-assignment harness, not delegate-agent.
- pc2_6acfeb65c53e5651, pc2_b0c2786ef3520fca, pc2_2ca253dca8ab50db: `spawn_agent` "no thread with id". `docs/troubleshooting.md:570` already documents it as a Claude Code harness bug with a `fork_turns: none` workaround.
- pc2_4f94a25001bbb654 (failover rotates to a seated family) and pc2_954ab0aef849b9a6 (plan-lint prose vs routes) belong to planc. Failover has family-aware panel assignment (`core.py:3724-3750`). I found no lint matching "<role> is A ... with B behind it" in `src/planc/lint/`, so the second may be a resolved-by-workaround entry.

## Cuts that are stale / already fixed

- Control characters, fences, string-wrapped JSON and completion-report fallback: pc2_2f63f7b0, pc2_d7907447, pc2_6fbfc840, pc2_a613dcf2, pc2_5e9e24e2 fixed by `workflows/schema.py` and `runtime.py:5949` (see RC-3). I ran the parser on control-character, fenced and string-wrapped samples and all validated.
- Null result: pc2_c6941976 fixed (`_STRUCTURED_NULL`, `runtime.py:100, 4372`).
- Root array on claude: pc2_6a73786c fixed by 66821d6d (09-07).
- Retry prompt without a schema: pc2_e84ccfc9 fixed (`runtime.py:4248-4259`, comment names the incident).
- Exhausted `lastParsedCandidate` dropped: pc2_9171c112 fixed for scripts using `on_failure="typed"`, which planc does.
- Empty findings after timeout/resume treated as clean: pc2_3cae78be fixed in planc, where a bare array attests no coverage (`contracts.py:715-720`).
- Rejection labels: pc2_e53d90f2 fixed by `rejectedBy` (`runtime.py:2619`).
- Dry-run cluster except dlg-x0z: see RC-5.
- Profile-required static commands: pc_e3a32f6c162f, pc_36705c525042, pc_723eeb8dc64b are stale. `workflow check /nonexistent.py` with `AI_PROFILE`, `DELEGATE_PROFILE` and `DELEGATE_CONFIG` unset reached "workflow_script_not_found" without a profile error; `profile_guard.py:79-92` treats `workflow` read-only actions as read-only.
- Effort enum: pc2_5824d8c8 is fixed at the runtime (`runtime.py:5395`). Whether planc lint now checks it is unknown; not traced.

## Open questions for Trey

1. Root-array schemas in hand-written workflow scripts: accept the `{"items":[...]}` wrapper, or reject root arrays with a message at `check`? Accepting is friendlier but hides a shape error from the author.
2. Should `agent()` return the schema-valid last candidate by default when the only failure is an envelope timeout? That changes the current `None` default that scripts may rely on.
