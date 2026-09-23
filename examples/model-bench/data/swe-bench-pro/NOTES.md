# SWE-bench Pro collection

## What it measures

SWE-bench Pro evaluates long-horizon software engineering agents on real pull-request tasks from open-source repositories. An agent receives a repository at a base commit and an issue/PR-style description; hidden fail-to-pass and pass-to-pass tests grade its patch. The current default dataset is V2, with 642 tasks. Its HARD subset has 51 tasks.

## Captured data

- Dataset release: V2, version `v2.0.0`, dated 2026-09-22.
- Leaderboard captured: 2026-09-22 (local time); source response/PDF metadata dates it 2026-09-22. The Hugging Face dataset API reports last modified 2026-09-22.
- Current official leaderboard: [SWE-Bench Pro V2](https://labs.scale.com/leaderboard/swe_bench_pro_public_v2), with its downloadable [PDF](https://labs.scale.com/api/pdf/leaderboard/swe_bench_pro_public_v2).
- Captured the current page HTML, its PDF and extracted text, the Hugging Face dataset card/API metadata, and the evaluation repository V2 README under `examples/model-bench/raw/swe-bench-pro/`.
- The HTML page contains the result values, `confidenceInterval_upper`, per-entry posted timestamps, model labels, harnesses, and effort settings. The PDF independently confirms the visible model names and scores.
- The older leaderboard route now returns 404. The current page links the legacy board as deprecated, so those historical rows were not combined with current V2 results.

## Rows and metrics

`rows.jsonl` has 21 result rows: 10 entries in the V2 Full table and 11 in V2 HARD. There are 11 distinct model configurations, each represented once per subset where listed. All entries report one metric, displayed as `SCORE`; values and the `confidenceInterval_upper` numbers are copied as published. The page describes the board as comparing resolve rates but does not specify a score formula or unit in the captured table, so rows use `metric: score` and `unit: score` without converting values to percentages. `ci` copies the source's `confidenceInterval_upper` field. Per-entry `measured_on` dates come from the page's `createdAt` timestamps. The source does not report task counts per model result, cost, or time, so `n`, cost, and time fields are null.

## Caveats and terms

- Results are from Scale's benchmark leaderboard. It mixes model/harness configurations (Claude Code, Codex, and mini-swe-agent) and effort settings (`high`, `xhigh`, `max`); these are not controlled model-only comparisons.
- The leaderboard's explanatory text says grayed results use a capped cost limit and 50-turn limit, while other results use uncapped cost and 250 turns. The capture does not provide row-level cap/turn fields, so those settings could not be assigned reliably to individual rows.
- No contamination messages appear in the captured result payload. The V2 card says its open-source tasks use GPL-licensed repositories to reduce contamination risk; this is not a guarantee that every result is contamination-free.
- The dataset card states that benchmark task content remains subject to the licenses of its upstream repositories. It states that the evaluation harness and tooling are MIT-licensed. No separate redistribution license for the leaderboard result table was found; row `license` fields are therefore null.
- Some provider cells are blank or abbreviated on the published table. Canonical `model_id` values are best-effort mappings from the model labels and provider names; model labels, effort, and harness remain verbatim/separate in each row.

## Verification

- Parsed all 21 JSONL rows and confirmed every line has all 24 fields defined for `rows.jsonl` in `examples/model-bench/SCHEMA.md`, with JSON types matching the schema.
- Compared all 21 rows with the captured Scale leaderboard HTML. Every subset and exact source model label has one matching row; all scores, `confidenceInterval_upper` values, and `createdAt` dates match. This includes the three highest scores and both GPT-6 Astra rows; GPT-6 Astra was released on 2026-09-03, within the preceding month.
- The source contains 10 Full and 11 HARD entries. Its labels have 16 distinct raw spellings across the tables; these correspond to 11 model configurations, and all 11 are represented in the rows. No source entries are missing or extra.
- No mismatches were found, so no rows were changed.
