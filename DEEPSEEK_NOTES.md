# DeepSeek V4 Pro / V4 Flash — running notes

Notes Trey asked me to keep while orchestrating Wave 1–6 implementation via DeepSeek brothers exclusively (Codex for review only).

Format per entry:
- Run alias, model, wave, task
- Wall time, exit code
- What I gave it, what came back
- Quality call (correctness, scope discipline, defensive-noise, test additions)
- Failure modes

## Aliases as configured

OpenCode-Go DeepSeek isn't configured locally (`my-model` is still the placeholder). Both DeepSeeks are OpenRouter:
- `delegate droid "deepseek v4 pro" ...` → `custom:OpenRouter-:-DeepSeek-V4-Pro-0`
- `delegate droid "deepseek v4 flash" ...` → `custom:OpenRouter-:-DeepSeek-V4-Flash-0`

## Game plan

- Pro for substantive waves (1, 2, 3, 5)
- Flash for narrow waves (4 metadata plumbing, 6 docs)
- Codex safe for code review after each wave; iterate if findings warrant
- Re-evaluate split if Pro is wildly expensive or Flash is wildly bad

---

## Run log

### Wave 1 — `delegate droid "deepseek v4 pro" work` (in flight)
Fired: 2026-05-24, prompt at `/tmp/delegate-wave1-prompt.md`, ~450 lines of spec context required.

**Progress checkpoint at 47m elapsed:**
- cli.py: +39 lines, threading `isolation` field through ParsedCommand and every parser; correct misplaced_global_option detection for `--isolation`; `VALID_ISOLATION_VALUES` import from config; explicit `missing_isolation_value` vs `invalid_isolation` errors.
- config.py: +95 lines (constants + DEFAULT_CONFIG additions + validation, presumably; haven't read yet).
- No test file changes yet.

**Initial impressions:**
- Quality: surgical and correct. Threading is methodical — each parser dispatch site gets the `isolation` param via keyword arg, no shortcuts. This is exactly the wiring discipline the spec asked for.
- Pace: very slow. ~3 line/min effective throughput. Lots of Read→Edit→Read→Edit cycles, suggesting it's re-reading files between edits to anchor each change. Could be DeepSeek Pro just being cautious; could also be OpenRouter rate-limiting between iterations.
- Concern: hasn't started tests yet at 47m. The spec test list is ~10 cases; that's another ~30-45m at this pace. Total Wave 1 wall time could hit 90-100m.
- No defensive noise visible in the diff so far — no spurious try/except, no over-engineered error messages.

Decision: let it finish. The work is real and the quality justifies the wait. If a future wave hits 2h+ I'll switch to Flash for narrower waves.

**Checkpoint at 1h2m:**
- cli.py +111, config.py +95, still no tests. Currently working `request_from_input_json` (the JSON pre-read for run --input-json, prompt item #6/7).
- 20 assistant messages so far, all narrating real next steps. Not stuck.
- This run will likely hit 90-120m total. **Lesson:** for parser-only waves where the work is mostly mechanical (threading a field through N callsites), DeepSeek Pro is probably overqualified. Flash should handle Wave 2 well too since IsolationContext refactor is also mostly threading. Save Pro for actual logic-heavy waves (3, 5).

### Wave 1 — COMPLETED
- **Total wall time:** 1h17m10s.
- **Diff:** cli.py +119/-24, config.py +95, test_delegate_parser.py +187, test_delegate_validation.py +85. **+462/-24 net.**
- **Gate:** `python3 -m unittest tests.test_delegate_parser tests.test_delegate_validation` → 75 tests, all green.
- **Full suite:** 203 tests, all green. Zero regressions.
- **DeepSeek Pro final report:** terse (2 sentences). Did NOT follow my structured-report instruction. Just said "Wave 1 is complete. All 203 tests pass with zero regressions." Won't pre-empt the actual deliverable but I'd lose the judgment-call context a more disciplined model would surface. → Going forward, drop "Report" section from prompts to Pro; rely on diff + my own inspection.

### DeepSeek V4 Pro — first impressions
- **Quality:** very high. Threading discipline matched the spec; no scope leak into Wave 2; no defensive noise visible.
- **Pace:** very slow. ~1h17m for what's mostly mechanical wiring. Effective throughput ~6 lines/min including tests; raw throughput per Read+Edit pair is ~30-60s.
- **Cost intuition:** 1h17m of work-mode at Pro pricing isn't cheap. For mechanical Wave 2 I should try Flash first and only fall back if quality drops.
- **Behavioral notes:** does Read→Edit→Read→Edit cycles aggressively, anchoring each edit by re-reading. Conservative but expensive. Uses TodoWrite to plan, which is good for visibility via `delegate snapshot`.

### Wave 1 Codex review (codex-5, 3m41s)
**5 findings — sent back for rework:**
1. **HIGH** — `run --input-json` loads config from CLI cwd BEFORE JSON pre-read; spec requires the opposite.
2. **MED** — `dry-run --isolation worktree cursor work` errors as `invalid_engine`, should be `misplaced_global_option`.
3. **MED** — explicit `null` accepted for fields the spec types (isolation.safe/work, autoPrune.enabled/mergedOlderThanDays, JSON isolation). `dataHome: null` is the intentional default and should keep being accepted.
4. **MED** — `resolve_isolation` doesn't validate its own inputs; spec says it should.
5. **LOW** — test coverage gaps: misplaced flag only on `cursor work`; missing `isolation.work` invalid, null rejections, non-int `mergedOlderThanDays`, valid `dataHome: null`.

All five are legit. None require Pro for the fix — narrow, precise corrections.

### Wave 1 fixes — `delegate droid "deepseek v4 flash" work` (droid-22, 44m59s)
Fired with prompt at `/tmp/delegate-wave1-fixes-prompt.md`. Testing the hypothesis that Flash can handle narrow correction sets while Pro handles substantive new logic.

**Diff after Flash:** cli.py +178, config.py +131, parser tests +312, validation tests +171. **+772 / -20 net** (vs Pro's +462/-24 starting point, so Flash added net +310 lines).

**Gate after Flash:** 88 tests on the focused gate (up from Pro's 75 = +13 new). Full suite 216/216 (up from 203 = +13 new). Zero regressions.

### DeepSeek V4 Flash — first impressions
- **Speed vs Pro:** 45m vs 77m on comparable work scopes (Pro did initial Wave 1, Flash did 5 fixes + 13 new tests). Flash is **noticeably faster per useful change** — Pro's ~6 lines/min vs Flash's ~7 lines/min, and Flash spent fewer Read cycles per Edit.
- **Quality:** at least as good as Pro on this task. Took initiative to broaden fix #2 to `parse_droid` and `parse_modeless_engine` too (correct call — `--isolation` could be misplaced after any subcommand). Introduced an `InvalidIsolationError` class internally to thread validation errors out of `config.py` without circular imports — clean engineering.
- **Discipline:** stayed in Wave 1 scope. Did not leak into IsolationContext / dry-run payload / execution. TodoWrite-driven so progress was visible in `delegate snapshot`.
- **Cost intuition:** Flash hourly cost is significantly lower than Pro. For a 45m run on broad-but-correction work, Flash is the right tool.

**Provisional model strategy for remaining waves:**
- Pro for new substantive logic where correctness of novel design matters (Wave 3 worktree creation, Wave 5 management commands).
- Flash for everything else (Wave 2 IsolationContext refactor — also mostly threading; Wave 4 metadata plumbing; Wave 6 docs).
- Codex review every wave; iterate fixes via whichever DeepSeek is appropriate to the fix scope.

### Wave 1 Codex re-review (codex-6, 3m14s)
**3/5 RESOLVED, 2/5 PARTIAL:**
- #1 HIGH RESOLVED — pre_read_run_json_for_config helper added, reorder correct.
- #2 MED RESOLVED — parse_dry_run detects option-like first token.
- #3 MED PARTIAL — null caught at pre-read but `request_from_input_json` still collapses missing/null via `raw.get()`.
- #4 MED PARTIAL — `resolve_isolation` validates CLI/JSON but not loaded-config inputs; falls through to defaults on malformed shapes.
- #5 LOW PARTIAL — direct tests for the above paths missing.

Defense-in-depth gaps, not user-facing bugs (`validate_config` catches things at the user-flow layer). But spec is explicit. Tight round-2 fix to Flash.

### Wave 1 round-2 fixes — `delegate droid "deepseek v4 flash" work` (droid-23, 21m21s)
**Diff:** cli.py +185 (was +178, +7), config.py +140 (was +131, +9), parser tests +350 (was +313, +37), validation tests +299 (was +171, +128). +181 net added.

**Gate:** 93/93 (up from 88, +5 new tests on the focused gate). Full suite 221/221 (up from 216, +5 new). Zero regressions.

**Flash again:** 21m for ~181 lines of changes including substantial validation-test additions. Consistent with first run pace. Round-2 was tighter scope than round-1; finished in less than half the time.

### Wave 1 Codex final verification (codex-7, 3m24s)
**3/3 partials RESOLVED. Verdict: ship Wave 1.**

One non-blocking nit: `tests/test_delegate_validation.py:552-612` is shadowed by same-named methods at `616-675` — unittest only runs the latter. Coverage exists either way. Bundling cleanup into Wave 2 prompt.

### Wave 1 — DONE
- 3 delegate runs (Pro initial, Flash round-1, Flash round-2) + 3 Codex reviews
- Total wall: 1h17m + 45m + 21m = 2h23m delegate work + ~10m Codex reviews
- Final stats: cli.py +185, config.py +140, parser tests +350, validation tests +299. **+974 / -20 net.**
- 93/93 focused gate, 221/221 full suite (+18 new tests from baseline 203).

**Aggregate model take after Wave 1:**
- **Pro:** thorough but slow. Best on the first cut where structure matters.
- **Flash:** faster, equally clean on correction work, willing to broaden a fix to obvious adjacent sites without being told. Solid "fix what the reviewer flagged" tool.
- Hypothesis confirmed: Flash is the right default for narrow waves; reserve Pro for greenfield logic-heavy waves (3, 5).

---

## Wave 2 plan
- **Model:** Flash (refactor is mostly threading `SafeIsolationContext` → `IsolationContext`).
- **Scope:** Generalized `IsolationContext`, planned branch/path metadata (no FS artifacts), dry-run payload structured isolation fields, no-artifact dry-run assertions.
- **Hard invariant:** all safe-mode behavior unchanged. Existing tests must still pass without modification.
- **Bundle:** also dedupe the shadowed test methods Codex flagged.
- **Gate:** `python3 -m unittest tests.test_delegate_parser tests.test_delegate_execution tests.test_delegate_commands`.

### Wave 2 — `delegate droid "deepseek v4 flash" work` (droid-24, 1h8m24s)
**Diff:**
- isolation.py NEW, 199 lines.
- cli.py +309 (was +185 at Wave 1 end, so +124 net for refactor + dry-run payload).
- config.py unchanged (still +140).
- test_delegate_execution.py +143 NEW (dry-run + planning tests).
- test_delegate_validation.py +235 (was +299; -64 from dedup of shadowed methods).
- test_delegate_parser.py unchanged.

**Gate:** parser+execution+commands = 94/94. Full suite: 264/264 (+43 from Wave 1 end). Zero regressions, zero modified existing tests (verified via diff; only additions to execution tests). Refactor preserved behavior.

**Flash on Wave 2:** 1h8m total. Broader scope than the 21m round-2 fix, much more refactor work. Quality looked good in real-time observation: built a `build_isolation_context` factory function rather than scattering construction, kept isolation.py creation-side only (didn't leak removal helpers). Also did the test dedup cleanup as bundled.

### Wave 2 Codex review (codex-8, 3m22s)
**5 findings — 1 deferred to Wave 3, 4 to fix now:**
1. **HIGH (Wave 3)** — `cursor work --isolation worktree` silently runs in source workspace (Wave 3 implements actual execution; deferred).
2. **HIGH (fix now)** — `isolated_workspace=True` falsely set for all runs because `make_run_context` treats any `isolation_context` presence as isolated. Regression to existing user-visible JSON metadata for normal droid/cursor/codex work runs.
3. **MED (fix now)** — `isolationMode` vs `effectiveIsolation` semantics swapped; `auto` is collapsed before storage.
4. **MED (fix now)** — dry-run argv/plans use generic placeholders instead of real spec-shaped paths/branches.
5. **LOW (fix now)** — no-artifact dry-run tests only check worktree dir, not branches or registry rows.

### Wave 2 fixes — `delegate droid "deepseek v4 flash" work` (droid-25, 1h16m20s)
- cli.py +389 (was +309, +80 for fixes A/B/C)
- test_delegate_execution +185 (was +143, +42 for Finding D coverage + new field tests)
- Gate: 94/94 focused, full suite 266/266 (+2 net)

**Flash on this round:** noticeably slower than the Wave 2 initial run (1h16m vs 1h8m for similar scope-mass). Lots of Read/Grep cycles between edits. Methodical. 56 assistant messages. Final-mile (test additions for Finding D) was the slowest part.

**Pattern emerging on Flash:** great on broad-but-shallow tasks; struggles on dense-multi-touch fix-sets. Pro might have been faster for this round.

### Wave 2 Codex re-review (codex-9, 3m29s)
- A — STILL PARTIAL (cosmetic: `isolatedWorkspace` omitted when false; should always emit per `preservedWorkspace` pattern)
- B — RESOLVED
- C — STILL PARTIAL (real: `request_from_parsed` doesn't capture git metadata, so CLI dry-run shows placeholders; text dry-run argv shows source path not planned)
- D — RESOLVED

A is cosmetic, C is real. Trying **Pro** this round for the dense fix (Flash struggled on last fix-set at 1h16m). Hypothesis: Pro is more efficient on dense multi-touch work despite slower for broad work.

### Wave 2 round-3 fixes — `delegate droid "deepseek v4 pro" work` (droid-26, 35m33s)
- cli.py +403 (was +389, +14 for git metadata capture + planning)
- runner.py +3 (was untouched! — for isolatedWorkspace explicit-emit fix)
- test_delegate_execution +382 (was +185, +197 for Finding C tests)
- Gate: 103/103 focused (up from 94, +9 new), full suite 275/275 (up from 266, +9)

**Pro vs Flash on dense fix-set: ~3x faster.** Pro: 35m. Flash on prior round (similar dense scope): 1h16m. Hypothesis confirmed — for fix-sets touching 3+ functions across 3+ files with interconnected changes, Pro is materially better. Flash is best on broad-but-shallow (initial wave structure, narrow correction passes).

**Updated model strategy:**
- Pro: greenfield logic (Wave 3 worktree creation, Wave 5 mgmt commands), AND dense multi-file fix-sets.
- Flash: broad straightforward wiring (Wave 4 metadata plumbing), simple corrections, docs (Wave 6).

### Wave 2 Codex final (codex-10, 2m12s)
- Finding A — STILL PARTIAL (runner.py:303-304 and :194-195 omit `isolatedWorkspace` when false; only dry-run path emits explicit). Narrow — 2 conditionals.
- Finding C — RESOLVED. Pro fully addressed it; live dry-run shows real paths/branches.

**Decision: ship Wave 2 with Finding A residual bundled into Wave 4** (snapshot/runner output is Wave 4's explicit territory). Cleaner than another fix-set round for 2 conditional lines.

### Wave 2 — DONE
- 5 delegate runs (Flash initial + Flash fix + Pro round-3) + 3 Codex reviews
- Total: 1h8m (Flash initial) + 1h16m (Flash round-2) + 35m (Pro round-3) ≈ **3h delegate + ~10min Codex**
- Final stats: cli.py +403, isolation.py +199 (NEW), runner.py +3, test_delegate_execution +382 (185 then +197), tests overall ~1015 lines net
- Gate: 103/103 focused, 275/275 full suite (+54 from Wave 1 end of 221)

**Key learnings from Waves 1-2:**
- Pro on dense fix-sets: ~3x faster than Flash for the same scope mass. Use Pro when fixes span 3+ functions across 3+ files with interconnected logic.
- Flash on broad-shallow refactor: comparable speed to Pro, slightly cleaner naming patterns.
- Flash on simple narrow corrections: very fast (Wave 1 round-1 was 45m for 5 fixes).
- Both DeepSeeks are honest reporters — TodoWrite-driven progress; assistant messages narrate real steps; no hallucinated tool calls observed.

---

## Wave 3 plan
- **Model:** Pro (substantive new logic: actual worktree+branch creation, pre-launch state, argv rewrite for cursor/droid/codex, prompt context injection, `--pass-through` restrictions).
- **Scope:** valid-HEAD + clean-source checks; pre-launch `creating_isolation` state; persistent branch/worktree creation under `~/.delegate/worktrees/<fingerprint>/<label>-<short-id>`; preserve on success/failure; child argv workspace argument rewrite; persistent-worktree prompt context injection; `--pass-through` rejection for persistent worktree; safe+worktree+pass-through context-manager dispatch order.
- **Gate:** `python3 -m unittest tests.test_delegate_execution tests.test_delegate_commands tests.test_runner_capture tests.test_run_registry`.

### Wave 3 — `delegate droid "deepseek v4 pro" work` (droid-27, 1h14m23s)
- cli.py +706 (was +403, +303 for persistent worktree execute path)
- runner.py unchanged from Wave 2 (+13) — Pro didn't grow runner; pre-launch state likely lives in cli.py instead
- isolation.py unchanged in diff stat? Let me re-check post-review. (Either Pro added inline to cli.py or already had isolation.py changes recorded as new content.)
- test_delegate_execution +959 (was +382, +577 new tests for worktree creation, pre-launch state, prompt context, safe+worktree, pass-through restrictions, dirty source, missing HEAD)
- Gate: 95/95 focused (parser+execution+commands+runner+registry), full suite 291/291 (+16 new tests).

**Trey-spotted false alarm at 52m:** Trey thought droid-27 was dead. I checked — process 62594 was alive, recently doing TodoWrite. He said "let it cook." It finished 22m later. Pro on substantive logic-heavy waves: 1h+ is normal.

### Wave 3 Codex review (codex-11, 11m1s — longest review yet)
**6 findings; 3 HIGH; spec-blocking:**
1. **HIGH** — Prompt injection NEVER REACHES CHILD. `execution_prompt` computed but stored in unused `exec_request`; `execute_tracked` gets argv with original (non-injected) prompt. The whole worktree-context-note feature is wired but disconnected.
2. **HIGH** — Pre-launch worktree creation failure not inspectable via `delegate snapshot`. Only state.json written; snapshot view drops new fields. Spec § "Persistent worktree pre-launch state" explicitly requires snapshot inspectability.
3. **HIGH** — Droid branch labels use resolved model id (e.g. `custom:OpenRouter-:-Qwen-3.7-Max-0`) instead of alias (`qwen`). Spec violation.
4. **MED** — Transaction boundary after `git worktree add` underhandled; argv rewrite / Popen failure leaves run stuck in `creating_isolation` without `failed` transition.
5. **MED** — Clean-source test only covers untracked; missing staged, unstaged, submodule. `require_clean_source` ignores git nonzero return.
6. **LOW** — `test_branch_collision_fails_before_launch` is a `pass` no-op.

All 6 are real. Pro fix-set fired.

### Wave 3 fixes — `delegate droid "deepseek v4 pro" work` (droid-28, 50m32s)
- cli.py +789 (was +706, +83 for 6 fixes)
- rendering.py +5 (was 0, for Finding 2 — preserve failure fields in merge_snapshot_view)
- test_delegate_execution +1229 (was +959, +270 new tests for the 6 findings)
- Gate: 103/103 focused, full suite 299/299 (+8 new)

**Pro on dense interconnected fix-set: 50min for 6 findings touching 3 files.** Consistent with the Wave 2 round-3 pattern — Pro is much faster than Flash on dense work.

### Wave 3 Codex re-review (codex-12, 8m4s)
- Finding 1 — RESOLVED (argv now contains injected prompt; fake-child tests assert)
- Finding 2 — STILL PARTIAL (impl correct, test doesn't call main() to invoke snapshot — reads files)
- Finding 3 — RESOLVED (Request.model_alias added; dry-run + execution use it; tests verify)
- Finding 4 — STILL PARTIAL (impl correct — wider try/except; no test simulates Popen launch failure)
- Finding 5 — RESOLVED (require_clean_source checks nonzero + tests cover all 4 dirty cases)
- Finding 6 — RESOLVED (collision test implemented)

Two STILL PARTIAL items are test-only gaps on correct implementation. Bundling into Wave 4 (snapshot/registry territory anyway).

### Wave 3 — DONE
- 2 delegate runs (Pro initial 1h14m, Pro fix 50m) + 2 Codex reviews
- Total ~2h delegate + ~19m Codex
- Final stats: cli.py +789, rendering.py +5, test_delegate_execution +1229
- Gate: 103/103 focused, 299/299 full suite (+24 from Wave 2 end of 275)

---

## Wave 4 plan
- **Model:** Flash (metadata plumbing — broad-shallow, plus bundled test-only fixes)
- **Scope (per spec § "Wave 4"):** add sourceGitRoot, worktreeStatus, worktreeRemovedAt, creationContext, worktreeCleanupCommands to manifest/snapshot/runs. Add set_worktree_status registry helper. Retention tests for preserved worktrees. PLUS: bundle Wave 2 Finding A residual (always emit `isolatedWorkspace` in runner JSON) and Wave 3 partial-test fixes (main()-level snapshot assertion for pre-launch failure; Popen-failure-after-create test).
- **Gate:** test_run_registry + test_snapshot_commands + test_retention + test_end_to_end_tracking

### Wave 4 — `delegate droid "deepseek v4 flash" work` (droid-29, 1h8m20s)
- runner.py +119 (was +13, +106 — cleanup hints + always-emit + creationContext propagation)
- rendering.py +59 (was +5, +54 — surface new fields in snapshot view + worktreeCleanupCommands)
- run_registry.py +46 NEW — set_worktree_status helper
- test_run_registry +37 NEW
- test_retention +48 NEW
- test_delegate_execution +69 for bundled Wave 3 fixes
- Gate: 79/79 focused (registry+snapshot+retention+end_to_end), full suite 304/304 (+5 from Wave 3 end of 299)

**Flash on broad metadata plumbing:** 1h8m. Similar pace to Pro on substantive work, but quality assessment pending Codex review.

Codex review firing.









