# WebArena results

## What it measures

WebArena evaluates language-model-powered agents on interactive, multi-step tasks in realistic self-hosted websites. The canonical benchmark has 812 tasks across shopping, a shopping administration site, Reddit, GitLab, maps, Wikipedia, and a homepage. These leaderboard results combine models with agent software, browser tools, prompts, and other task-specific methods; they do not isolate base-model capability.

## Captured data

- Benchmark task release: WebArena v0.2.0 (the authors describe this as the stable task version).
- Source: the official X-WebArena-Leaderboard Google Sheet linked from the WebArena authors' GitHub README.
- Retrieved: 2026-09-23T01:17:22Z (UTC).
- The sheet dates individual submissions by month. `measured_on` is null because no exact day is supplied. The original month labels are preserved in each row's notes.
- The main leaderboard contains 47 model/agent configurations. This file has 47 rows: one `success_rate` measurement per main-table configuration. Two Reddit-subset results and one human-performance result appear in separate sections and are excluded from the model-configuration count. Cost, time, confidence intervals, and sample sizes are not published in the main table and are null.
- The score is copied as the sheet's percentage (`Success Rate (%)`); no conversion was applied. The source does not define a separate metric formula on the sheet. WebArena's primary paper describes task completion judged by functional correctness.

## Collection and caveats

The sheet was exported directly as CSV from Google's public export endpoint; the untouched export is `raw/webarena/official-leaderboard.csv`. It is the primary source linked by the benchmark repository. `harness` carries the source's `Work` column where present; that column often names an agent, team, or method rather than a standardized harness. `model_raw` preserves the sheet's model entry. For composite agents or entries whose underlying base model is not identified, `model_id` uses `unknown/<slug>` rather than guessing.

The leaderboard is heterogeneous: task setups and agent scaffolds differ, and the sheet contains both open and closed entries (the source's `Open?` marker is retained in row notes). At least some entries are explicitly marked self-reported, and submissions are not independently reproduced here. The authors state that since September 2024 submissions are required to include raw trajectories, but the sheet alone does not let this collector verify trajectory availability for every entry. The source dates are mixed, so many results are stale relative to current model releases. Do not compare scores as if they were same-split, same-environment, same-harness model-only evaluations.

The WebArena GitHub repository is Apache-2.0 licensed. The Google Sheet does not state a data redistribution license; row `license` is therefore null. The repository asks users of the environment or data to cite the WebArena paper.

## Not captured

The separate Reddit subset scores and human-performance figure are visible in the raw CSV but are not language-model configurations in the main leaderboard. No reasoning-effort settings, paired costs/times, or exact per-result dates are stated in the main table.

## Verification

- Checked all 47 JSONL lines: each parses and has all 24 fields in `SCHEMA.md`, with no extra fields.
- Compared the ten highest-scoring rows and the `A3-Qwen3.5-9B` row (11 rows total) against the corresponding model and percentage in the saved CSV; all matched. The `A3-Qwen3.5-9B` result is the newest explicitly named base-model configuration in this capture, dated 04/2026 in the source; no named model in the rows was identified as released during 2026-08-22 through 2026-09-22.
- Fetched the official Google Sheet CSV export and confirmed it exactly matches `raw/webarena/official-leaderboard.csv`. The main table contains 47 configurations, matching the 47 rows; it contains 46 distinct model labels because `gpt-4-0613` is listed for two setups.
- No row values required correction, no source configurations were missing, and no out-of-source rows were found. No rows were changed.
