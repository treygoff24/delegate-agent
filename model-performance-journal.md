# Model performance journal — delegate-agent

## 2026-07-10 - gpt-5.6-sol via codex - product review of `call --pure` (D0 + D0.1b)

Command and run: `delegate --json codex work --model sol --reasoning-effort xhigh --prompt-file <brief>`; alias/variant/effort: sol / xhigh; mode/isolation: work / none (report-file write only); run handle: codex-511 (del_20260710T171806Z_d33eb0).

Task and expectation: review commits 49f36fd + 3e42f87 (~2k-line pure-mode diff) in full product context, no live model calls, answer "ship --pure as a real feature?", write a severity-ranked md report. Expected a design-level read plus a handful of real defects.

Outcome and verification: delivered `docs/reviews/2026-07-10-pure-mode-sol-review.md` in 14.5 min. Found 1 critical (Seatbelt lets the whole confined process tree read the ephemeral auth.json; hardlink aliases the live credential — both PROVEN with dummy-file probes, no network), 3 high (claude tripwire fails open on missing/malformed permission_denials; stdin write outside the timeout clock; opencode pure doesn't meet the contract), 2 medium, 1 low. Honored every constraint: single file written, no source edits, no commits, no live calls (ran the full suite under empty HOME so the 7 live codex tests skipped; flagged that default discovery would have fired them). Gate re-run green (1268 passed).

Performance observations: empirical-probe discipline was the standout — it built dummy-credential sandbox probes and a fake-pipe timing probe instead of asserting from reading. The critical finding was missed by the overnight cross-family review (Cursor/Grok) AND the coordinator. Zero fabrication; self-narration accurate against on-disk state. Verification burden low: claims came with file:line + probe evidence.

Routing assessment: confirms Sol xhigh as the deep security/product-judgment review lane, at or above Cursor/Grok 4.5 for boundary reasoning (Grok found the profile-injection hole earlier; Sol found the process-tree credential hole Grok cleared). Use again for any security-claim review. Next comparison: same brief to Cursor grok-4.5-xhigh to see if the credential finding reproduces cross-family. Confidence: high.
