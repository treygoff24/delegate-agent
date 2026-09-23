# VisualToolBench (VTB, Scale Labs) results collection

added by coverage audit 2026-09-22

## What it measures and data version

VisualToolBench (VTB) benchmarks how multimodal LLMs dynamically interact
with and reason about visual images — i.e. tool-use tasks whose inputs/state
involve images, not just text. The page states VTB is highly challenging:
all 16 evaluated MLLMs at the time of writing struggled significantly.

Two metrics are defined from the task rubrics: **Average Pass Rate (APR)**,
the primary success metric — a response only "passes" if it satisfies ALL
rubrics weighted 4-5 ("critical") — and **Average Rubric Score (ARS)**, a
granular partial-credit metric over all weighted rubrics. The page notes APR
is "significantly lower than ARS" by construction (strict pass/fail vs.
partial credit).

## Collection method and coverage

Fetched https://labs.scale.com/leaderboard/vtb with `curl -A 'Mozilla/5.0'`
and extracted the embedded `entries` array from the page's RSC flight
payload (same method as the other Scale Labs boards in this batch). Raw
HTML saved under `raw/scale-vtb/vtb.html`. Single leaderboard table (no
subset toggle): 21 model configurations, one score each (21 rows).

**Only Average Pass Rate (APR) is present in this leaderboard's data
payload.** Average Rubric Score (ARS) is defined in the methodology text but
no second metric array was found in the captured page; if the site exposes
ARS, it would need a separate capture path (e.g. a client-side toggle).

## Freshness — flagged as lagging

Newest row: Muse Spark 1.1, dated **2026-07-09**. No explicit "Updated
<date>" text was found. `last_updated` = **2026-07-09**.

Like MultiChallenge in this same batch, VTB has not been refreshed with the
September 2026 frontier wave: no GPT-6, no Fable 5.1, no Gemini 3.8 Flash, no
DeepSeek V4.1, no GLM 5.3, no Kimi K3, no Qwen 3.8, no MiniMax M3, no Grok
4.7. It does carry 2026-vintage models (Muse Spark 1.1, gpt-5.4-2026-03-05,
gemini-3.1-pro-preview, claude-opus-4-6-thinking, all from Q1 2026), clearing
the brief's literal "has 2026 frontier models" bar, but by 2026-09-22 the
newest entry is over two months old. Most of the remaining 15 rows date to
2025-10-08, a single batch capture from over 11 months before this fetch.
Treat this benchmark's coverage as meaningfully stale.

## Metrics and row interpretation

- `metric` = `average_pass_rate`, `unit` = `%`, `higher_is_better` = true.
- `ci` = the JSON `confidenceInterval_upper` field (half-width).
- `measured_on` = the row's `createdAt` date.
- `effort`: parsed from suffixes where present, including the explicit
  `(reasoning effort = high)` form on the `gpt-5.4-2026-03-05` row (captured
  as `effort: "high"`) and bare `-thinking` suffixes on several
  Anthropic/OpenAI rows.
- `contaminationMessage` was empty for every row; `notes` is null
  throughout.

## Caveats and license

Footer states only "All rights reserved"; no separate redistribution terms
given. `license` is null on every row.

## Verification

Spot-checked 10 of 21 rows (top 3 by `average_pass_rate` plus 7 more spread
across the ranking) by independently re-extracting `score`,
`confidenceInterval_upper`, and `createdAt` from the raw HTML with a fresh
regex (separate code path from the JSON-parsing extraction script), matched
by exact model name. All 10 matched exactly on score, CI, and date. Also ran
a full-file schema/parse check confirming all 21 lines parse as JSON with
every SCHEMA.md field present.
