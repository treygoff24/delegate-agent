# Model performance journal — delegate-agent

## 2026-07-15 - gpt-5.6-sol via codex - closing judge on desloppify branch

Command and run: `delegate --group cleanup-ship codex safe --model sol --reasoning-effort xhigh --prompt-file <brief>`; alias/variant/effort: sol / xhigh; mode/isolation: safe / isolated snapshot; run handle: codex-514.

Task and expectation: pre-ship adversarial review of the 3-commit cleanup branch (deletion safety, protocol-to-concrete type swap, exception-tuple equivalence, resolve_prompt restructure, boundary regression test, changelog honesty), with the two known deliberate behavior changes excluded from re-flagging. Expected ~0-2 real findings given two prior review rounds.

Outcome and verification: SHIP with exactly one low finding, zero hallucinated: CHANGELOG (and commit 9225f f2's body) credited `cancel` with the wait-overlap dedup fix, but cancel's parser rejects `--latest`/`--group`. Coordinator verified live (`cancel --latest` returns unknown_option) — finding real, folded as a docs-only correction commit.

Performance observations: ~7 min wall clock. Followed the "don't re-run the suite" constraint, ran repo-wide deleted-symbol grep sweeps and `git diff --check` instead, and reviewed the untracked dogfood journal unprompted. The one finding is characteristic Sol: a cross-artifact consistency defect (release note vs parser reality) that both a Claude two-axis review and a Terra fix round missed.

Routing assessment: reconfirms Sol xhigh as the closing-judge lane — asymptote behavior again (~1 real finding on a twice-reviewed diff, no noise). Keep the pattern: author/fix lanes, then Sol xhigh last. Confidence: high.


Command and run: `delegate --group cleanup-ship codex work --model terra --reasoning-effort high --prompt-file <brief>`; alias/variant/effort: terra / high; mode/isolation: work / none (dirty tree, expected); run handle: codex-513.

Task and expectation: clustered 4-item fix round from a two-axis review — restore broad except at the workflow adopt/retry boundary with a regression test, broaden `git_root_for` to OSError, inline the `_public_argv` middle-man, reflow one docstring. Expected near-spec-perfect execution with no scope creep, per Terra's standing fix-lane record.

Outcome and verification: all 4 items delivered correctly; regression test well-shaped (unit-level monkeypatched RecursionError exercising BOTH sites, avoiding the known resume-flake class as briefed). Coordinator gates green after: ruff check/format clean, full suite 1276 OK (skipped=7). One deviation: Terra wrote an unrequested `[Unreleased]` CHANGELOG entry covering the whole branch — accurate and well-structured (kept after verification), but outside its owned-file list.

Performance observations: ~4.5 min wall clock. Honest failure reporting held — reported "blocked" because the exact 4-module aggregate test command outran its window, rather than claiming a pass (targeted modules it did run were OK). The CHANGELOG addition is the first scope-creep-shaped event on Terra's record here, though it was additive documentation, not code.

Routing assessment: Terra high remains the default fix lane — 6/6 findings-shaped batches now. Note for briefs: Terra will helpfully document beyond its file list; say "no CHANGELOG/docs edits" explicitly when that matters. Confidence: high.


Command and run: `delegate --json codex work --model sol --reasoning-effort xhigh --prompt-file <brief>`; alias/variant/effort: sol / xhigh; mode/isolation: work / none (report-file write only); run handle: codex-511 (del_20260710T171806Z_d33eb0).

Task and expectation: review commits 49f36fd + 3e42f87 (~2k-line pure-mode diff) in full product context, no live model calls, answer "ship --pure as a real feature?", write a severity-ranked md report. Expected a design-level read plus a handful of real defects.

Outcome and verification: delivered `docs/reviews/2026-07-10-pure-mode-sol-review.md` in 14.5 min. Found 1 critical (Seatbelt lets the whole confined process tree read the ephemeral auth.json; hardlink aliases the live credential — both PROVEN with dummy-file probes, no network), 3 high (claude tripwire fails open on missing/malformed permission_denials; stdin write outside the timeout clock; opencode pure doesn't meet the contract), 2 medium, 1 low. Honored every constraint: single file written, no source edits, no commits, no live calls (ran the full suite under empty HOME so the 7 live codex tests skipped; flagged that default discovery would have fired them). Gate re-run green (1268 passed).

Performance observations: empirical-probe discipline was the standout — it built dummy-credential sandbox probes and a fake-pipe timing probe instead of asserting from reading. The critical finding was missed by the overnight cross-family review (Cursor/Grok) AND the coordinator. Zero fabrication; self-narration accurate against on-disk state. Verification burden low: claims came with file:line + probe evidence.

Routing assessment: confirms Sol xhigh as the deep security/product-judgment review lane, at or above Cursor/Grok 4.5 for boundary reasoning (Grok found the profile-injection hole earlier; Sol found the process-tree credential hole Grok cleared). Use again for any security-claim review. Next comparison: same brief to Cursor grok-4.5-xhigh to see if the credential finding reproduces cross-family. Confidence: high.

## 2026-07-16 - gpt-5.6 (sol high) via codex - Wave 6 hardening: full 6-item authoring

Command and run: `delegate --group wave6 codex work --model sol --reasoning-effort high --prompt-file docs/plans/2026-07-16-wave6-hardening.md`; mode/isolation: work/none (feature branch); run handle: codex-515.

Task and expectation: author all six revised Wave 6 items (auto include-dirty, source-root guard, empty-retry, tail→stdout, bare-handle UX, devin preflight) as coherent per-item commits, gates green.

Outcome and verification: all six shipped in 35m8s, 8 commits, 39 files; coordinator re-ran the full gate independently — 1337 tests OK, ruff clean. Adversarial pair later found 5 major + 4 minor defects, including a blocker-class product hole (empty-retry re-executing write-capable calls) — the author's own gate and self-review missed all of them.

Performance observations: excellent per-item commit discipline and honest deviation notes (two follow-up commits instead of amends). Signature Sol trait confirmed again: coherent whole-wave build, but blind to its own product-policy edge cases.

Routing assessment: keep Sol high as the multi-item wave author; never skip the independent review pair afterward. Confidence: high.

## 2026-07-16 - gpt-5.6 (sol xhigh) via codex - Wave 6 review rounds 1+2

Command and run: `delegate --group wave6-review codex safe --model sol --reasoning-effort xhigh --prompt-file <brief>`; handles codex-516, codex-520; safe/isolated.

Task and expectation: adversarial review of the wave diff (round 1) and fix commits (round 2) against named hunt areas.

Outcome and verification: round 1: 5 major + 4 minor, probe-confirmed two of them live (APFS case-fold guard bypass; naive-timestamp crash) — both verified real by coordinator. Round 2: 6 findings (3 medium), incl. samefile fail-open race and submodule over-blocking classification; all adopted. Zero fabrication either round; verified-OK checklists accurate.

Performance observations: empirical probing again the differentiator (built local repro probes in safe mode). ~17 min and ~13 min respectively.

Routing assessment: premier review lane confirmed for the nth time. Confidence: high.

## 2026-07-16 - grok-4.5-fast-xhigh via cursor - Wave 6 review rounds 1+2 (no-shell)

Command and run: `delegate --group wave6-review cursor safe --prompt-file <brief with no-shell constraint>`; handles cursor-137, cursor-138; safe/isolated.

Task and expectation: same briefs as Sol xhigh, decorrelated attack; shell probes forbidden per standing gotcha.

Outcome and verification: round 1: blocker (call-retry double side effects — convergent with Sol) + submodule-gate hole + grouped-call retry gap, all real. Round 2: caught the F10 marker fix regressing byte-compat on single-attempt runs AND `safe_isolated_request` re-dropping call_read_only — the exact regression class of the round it was reviewing. ~8.5 min and ~9 min. Overlap with Sol ~40%, uniques decorrelated both rounds.

Performance observations: no-shell constraint held (no deadlock); traced construction paths by grep effectively. Severity self-labels reasonable this time, but coordinator re-ranked per standing rule.

Routing assessment: standing pair validated yet again; keep the no-shell constraint permanently in cursor safe briefs. Confidence: high.

## 2026-07-16 - gpt-5.6 (terra high) via codex - Wave 6 fix rounds 1-3

Command and run: `delegate --group wave6-fix{,2,3} codex work --model terra --reasoning-effort high --prompt-file <findings brief>`; handles codex-517, codex-518, codex-521; work/none.

Task and expectation: findings-shaped fix batches (10, then 4, then 9 items), one commit + pinning test each, full gate green.

Outcome and verification: round 1: 6/10 landed but F1 plumbing read a nonexistent Request field (115 test failures); Terra reported the red gate honestly and stopped rather than thrash — coordinator root-caused and repaired (df6c3dc). Rounds 2-3: all items landed clean, full gate green (1347 → 1360 tests), including an unprompted-quality empirical probe for R2 (gitlink diff/apply verification) that settled an allow-vs-block product question with evidence.

Performance observations: honest-failure behavior is exactly why Terra is the fix lane; the round-1 miss shows focused-test verification is insufficient — coordinator must own the full gate. R2 falsification probe was textbook.

Routing assessment: keep Terra as default fix lane; always run the full suite at coordinator level after its rounds. Confidence: high.

## 2026-07-16 - gpt-5.6 (luna low) via codex - live smoke child for dirty-worktree feature

Command and run: `delegate codex work --model luna --reasoning-effort low --isolation worktree "<read dirty files, write SMOKE.txt>"` in a scratch repo; handle codex-1 (scratch registry).

Task and expectation: real-child smoke proving auto include-dirty end to end.

Outcome and verification: child saw both the tracked edit and untracked file and wrote SMOKE.txt containing them; pre-launch stderr disclosure and envelope warning both correct. ~1 min.

Routing assessment: luna low is the right zero-thought smoke child. Confidence: high.
