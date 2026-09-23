# MultiChallenge (Scale Labs) results collection

added by coverage audit 2026-09-22

## What it measures and data version

MultiChallenge evaluates frontier LLMs on realistic multi-turn conversations,
assessing instruction retention, inference memory (recalling/using earlier
context), and self-coherence across a conversation. Scale uses an "LLM as
judge with instance-level rubrics" approach rather than a single frontier
judge model directly scoring transcripts, because directly-prompted frontier
judges showed poor alignment with human raters (frontier models themselves
score under 50% accuracy on MultiChallenge, undermining trust in using one as
judge). The page's changelog note (verbatim): "Updated judge model to Gemini
2.5 Pro." Dataset: https://huggingface.co/datasets/ScaleAI/MultiChallenge.

## Collection method and coverage

Fetched https://labs.scale.com/leaderboard/multichallenge with
`curl -A 'Mozilla/5.0'` and extracted the embedded `entries` array from the
page's RSC flight payload (same method as the other Scale Labs boards in
this batch). Raw HTML saved under
`raw/scale-multichallenge/multichallenge.html`. Single leaderboard table (no
subset toggle): 30 model configurations, one score each (30 rows).

## Freshness — flagged as lagging

Newest row: Muse Spark 1.1, dated **2026-07-09**. No explicit "Updated
<date>" text (other than the unrelated judge-model changelog note above) was
found. `last_updated` = **2026-07-09**.

**This board has not been refreshed with the September 2026 frontier wave.**
It has no GPT-6 (Astra/Luna/Sol), no Claude Fable 5.1, no Gemini 3.8 Flash,
no DeepSeek V4.1, no GLM 5.3, no Kimi K3, no Qwen 3.8, no MiniMax M3, and no
Grok 4.7 — every one of the current-wave models present on the other five
Scale boards collected in this batch. It does still carry 2026-vintage
models (Muse Spark 1.1, gemini-3.1-pro-preview, gpt-5.4-pro-2026-03-05,
claude-opus-4-6-thinking-max, kimi-k2.5), so it clears the brief's literal
"has 2026 frontier models" bar and was collected per instructions, but by
2026-09-22 its newest entry is over two months old and it is visibly behind
the other Scale boards in this batch. Treat this benchmark's coverage as
stale relative to the current model generation; a re-check in a few weeks
may find it caught up.

## Metrics and row interpretation

- `metric` = `accuracy`, `unit` = `%`, `higher_is_better` = true.
- `ci` = the JSON `confidenceInterval_upper` field (half-width).
- `measured_on` = the row's `createdAt` date.
- `effort`/`harness`: parsed from suffixes where present (mostly bare
  "-thinking" suffixes and a couple of "(Non-Thinking)"/"reasoning-high"
  patterns baked into the raw model string rather than parenthesized). No
  MultiChallenge row states a separate agent harness from the model itself.
- `contaminationMessage` was empty for every row; `notes` is null
  throughout.

## Caveats and license

Footer states only "All rights reserved"; no separate redistribution terms
given. `license` is null on every row.

## Verification

Spot-checked 10 of 30 rows (top 3 by accuracy plus 7 more spread across the
ranking) by independently re-extracting `score`, `confidenceInterval_upper`,
and `createdAt` from the raw HTML with a fresh regex (separate code path
from the JSON-parsing extraction script), matched by exact model name,
including one row (`Muse Spark 1.1\n`) whose raw model string carries a
literal embedded newline character — confirmed the newline is genuinely
present in the source's own JSON payload (double-escaped as `\\n` in the
page's JS string literal), not an artifact of extraction. All 10 matched
exactly on score, CI, and date. Also ran a full-file schema/parse check
confirming all 30 lines parse as JSON with every SCHEMA.md field present.
