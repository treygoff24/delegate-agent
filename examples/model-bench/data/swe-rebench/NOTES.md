# SWE-rebench leaderboard capture

## What it measures

SWE-rebench is a continuously evolving benchmark of software-engineering models using real GitHub issues. The leaderboard's current table reports resolved rate, Pass@5, cost per problem, and tokens per problem. This capture covers language-model entries marked `Model`; agent products such as Junie, Claude Code, Codex, and Cursor are excluded.

## Data captured

- Captured from the live leaderboard on 2026-09-22 (America/New_York); the raw server-rendered page is `../../raw/swe-rebench/leaderboard-2026-09-22.html` relative to this note's parent data directory.
- The latest month visible in the leaderboard's historical plot is June 2026. The page does not expose a precise as-of date for the current table, so `measured_on` is null and the capture date is not represented as the measurement date.
- The table lists 117 configurations, of which 17 have numeric metrics (13 language models and 4 agent products); the remaining listed entries are N/A. `rows.jsonl` contains 52 rows: 13 scored language-model configurations × 4 numeric table metrics. It omits the four agent-product rows because they are not language-model configurations, and omits N/A entries because they have no result to record.
- Raw capture URL: https://swe-rebench.com/. The Hugging Face dataset link is https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard; its current viewer contains 860 task rows, not model-result rows, so it was not used as the score source.

## Metrics and mapping

- `resolved_rate`: the table's percentage of resolved problems. The displayed ± amount is copied into `ci`; the page capture does not establish its statistical interpretation, so it is not relabeled.
- `pass@5`: the table's Pass@5 percentage.
- `cost_usd` / `cost_basis`: the table's dollar cost per problem, paired with each success metric row; the table shows no time measure.
- `tokens_per_problem`: the displayed integer token count per problem.
- `cached_token_percentage`: the displayed cached-token share.
- Displayed percentages and costs are copied at their published precision. The table identifies effort in brackets for applicable model names; that suffix is kept in `model_raw` and separately recorded in `effort`.
- `reporter` is `independent` because the benchmark is run independently of the model vendors. The table does not identify a harness for each model configuration, exact sample counts, exact run dates, or a result license; those fields are null.

## Caveats and terms

The project describes its task set as fresh real GitHub issues and aims to reduce contamination; that does not establish that every model result is free of contamination. The current table gives no per-model contamination audit or explicit data timestamp. Some listed configurations have no current numeric result (N/A) and are not represented as scored rows. The Hugging Face task dataset page states CC-BY-4.0 for that dataset; the leaderboard page does not state redistribution terms for its result table, so the result rows' `license` is null rather than extending the task-dataset license to the scores.

No wall-clock time, reasoning-effort comparisons beyond the effort labels shown, or per-task result distributions could be captured from the current table.

## Verification

- Parsed all 52 JSONL lines and checked that every row has all 24 schema fields with the required types and category values; no schema errors were found.
- Compared all 52 recorded metric values with the saved 2026-09-22 leaderboard capture, including paired costs, tokens per problem, and cached-token percentages. All matched. This includes the three highest resolved-rate rows (Fable 5, Grok 4.5, Opus 5) and the three highest Pass@5 rows (GLM-5.2, GPT-5.6 Sol, Fable 5).
- The capture lists 117 configurations: 113 model entries and 4 agent entries. Numeric results are present for 13 model configurations and 4 agents; the 13 scored model configurations are all represented in the rows, with four metrics each. The four agent-product configurations are outside this collection's language-model scope; N/A entries have no numeric result to record.
- No represented model was released in the month before the 2026-09-22 capture. No row was added, removed, or corrected.
