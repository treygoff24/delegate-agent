# TUA-Bench collection notes

- **What it measures:** 120 real-world executable terminal-use tasks spanning everyday digital workflows and specialized scientific tools. The leaderboard evaluates agent + language model + reasoning-effort configurations; results are not a model-only comparison.
- **Captured:** 2026-09-22 (America/New_York); retrieved at 2026-09-23T01:15:23Z. The source does not give a benchmark-run date, so `data_date` and row `measured_on` are unknown.
- **Version:** Mutable public leaderboard snapshot captured from `https://tuabench.ai/leaderboard.json`. The checked-in README at the GitHub `main` branch reported the same 39 configurations and four scores per configuration. No explicit leaderboard version or score publication date is shown.
- **Retrieval:** Downloaded the leaderboard JSON linked by the official site’s page and saved it at `raw/tua-bench/leaderboard.json`; also saved the page HTML, the GitHub `main` README, and repository `LICENSE`. The site HTML contains stale copy saying results are placeholders, but the loaded JSON contains scores and per-run costs; the repository README’s table corroborates all displayed scores.
- **Metrics:** `success_rate` is reproduced on its published 0–1 scale; `ci` is the exact displayed ± error (half-width) for that metric. `pass@1`, `pass@5`, and `all-5` are copied as percentages. Higher values are better for all four. The source explains these as success/resolution rates and reliability metrics; it does not publish a sample count for the individual scores, so `n` is null.
- **Costs and time:** The JSON publishes `costPerRun`, the spend for one full pass over the 120-task suite. It is attached to each configuration’s `success_rate` row as USD per run; other metric rows leave cost null to avoid duplicating one run’s cost across separate measurements. No time values are published.
- **Coverage:** 39 distinct agent/model/thinking configurations × 4 metrics = 156 rows. All 39 configurations in the JSON and README are included. Cost is included where published; no row was omitted for missing cost.
- **Caveats:** Results are published by the benchmark maintainers, not independently reproduced in this collection. The source does not identify a run date, task-level sample count, or model release dates. Model names and reasoning settings are source labels; canonical IDs are best-effort vendor guesses. Changes to the mutable leaderboard may replace scores, and older model variants may no longer represent current vendor offerings. The page’s placeholder notice conflicts with its populated JSON; the README table independently matches the JSON values.
- **License:** The repository `LICENSE` states Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0). Attribution is required and commercial reuse is not permitted under that license. See `raw/tua-bench/LICENSE`.
- **Not captured:** No per-task breakdown or raw run logs were linked from the leaderboard JSON. The benchmark publishes no model-specific time measurement here.

## Verification

- Parsed all 156 JSONL lines and checked every row against the 24 fields and types in `SCHEMA.md`; all passed.
- Compared every row's configuration and score with `raw/tua-bench/leaderboard.json`, including the paired success-rate confidence interval and run cost. All 156 matched; no rows were added, removed, or corrected.
- This includes the three numerically highest values (`pass@5`: 68.3, 67.5, 67.5) and all four DeepSeek V4-Pro metric rows. DeepSeek announced V4-Pro GA on 2026-08-13 ([official API notice](https://api-docs.deepseek.com/news/news260813/)), within the prior month.
- Coverage matches: the capture lists 39 distinct configurations, and the rows contain those same 39 configurations with four metrics each; no missing or extra configuration/metric pairs were found.
- Result: 156 checked, 0 mismatches, 0 fixes.
