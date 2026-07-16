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
