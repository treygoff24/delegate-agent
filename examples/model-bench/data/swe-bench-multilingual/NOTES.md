# SWE-bench Multilingual collection notes

Captured 2026-09-22 (America/New_York). The latest dated result in the official leaderboard snapshot is 2026-02-20; the Hugging Face dataset API reported revision `846e647b9f33c0b51b739d005d13d85493c9af09`, last modified 2026-08-17. The dataset contains the `test` split with 300 issue-resolution tasks. No separate benchmark score version is specified by the leaderboard.

## What it measures

SWE-bench Multilingual evaluates an agent's ability to resolve real GitHub issues in repositories written in nine programming languages. A task supplies an issue and a repository snapshot. A patch counts as resolved when the fail-to-pass tests for the issue and pass-to-pass regression tests succeed. The benchmark page says the dataset spans 42 repositories; the Hugging Face card says 41 popular repositories.

## Capture method and scope

Downloaded the official SWE-bench site root page, which embeds JSON in the `leaderboard-data` script, and extracted its `Multilingual` board into `raw/swe-bench-multilingual/official-multilingual-results.json`. Also saved the original benchmark article HTML, Hugging Face dataset metadata API response, and dataset card. The official board contains 13 model configurations; all 13 are represented. The article separately reports the original SWE-agent + Claude 3.7 Sonnet baseline (43%) and appendix breakdowns for 41 repositories, 8 language groups, and 5 year groups. Those are all included as separate `resolved_rate` rows by subset. The resulting 68 rows comprise 13 leaderboard aggregate scores, 1 article baseline aggregate score, and 54 baseline subset scores. Each configuration/subset has one score metric row. The 13 leaderboard records pair their score with average per-instance cost; the original baseline page reports a $2.50 cost limit but no observed cost, so its `cost_usd` is null. No time or reasoning-effort variants beyond the single GPT 5.2 high-effort entry are published in these captures.

## Metrics and provenance

`resolved_rate` is the source's reported percentage of tasks resolved; larger is better. Leaderboard aggregate values are copied directly in percent (for example, 72.7). Appendix percentages are copied at the precision displayed by the article. `cost_usd` uses the official leaderboard's `instance_cost` field, the average dollar cost per task, and is paired with the score on that row. All 13 leaderboard entries are marked as one attempt in the source tags and use mini-SWE-agent version 2.0.0a0 or 2.0.0; the original baseline and its breakdowns use SWE-agent. The board gives per-result dates from 2026-02-13 through 2026-02-20. Its results are team-run leaderboard entries, not vendor self-reports. The article does not give a date for its original baseline.

## Caveats, freshness, and terms

The official leaderboard snapshot was retrieved 2026-09-22 local time, but its newest dated multilingual run is from 2026-02-20, so the published leaderboard scores are stale relative to capture. The benchmark article is older and itself says only Claude 3.7 Sonnet was evaluated; later official leaderboard submissions add the 13 listed model configurations. No confidence intervals are published in these sources. We found no contamination statement or contamination audit in the captured leaderboard/article/dataset card; this should not be read as evidence that contamination is absent. The leaderboard does not state a separate redistribution license for scores. The Hugging Face dataset card labels the task dataset MIT licensed; that is recorded here as the dataset license, not assumed to license leaderboard result data.

The request's supplied dataset URL uses `SWE-bench/SWE-bench_Multilingual`; this is the Hugging Face dataset repository fetched. We could not capture evaluation logs/trajectories from the official board's S3 links, only its published aggregate result records, article appendix tables, and available task dataset metadata/card.

## Verification

- Validated all 68 JSONL lines parse and contain all 24 fields in `examples/model-bench/SCHEMA.md`'s rows schema.
- Checked 19 rows against the captures: all 13 official leaderboard configurations (score, paired per-instance cost, and date), the three highest-scoring repository rows (all 100%), the article's Claude 3.7 Sonnet baseline (43%), the language total (42.67%), and the 2025 year subset (43.8%). All matched; 0 mismatches and no row corrections or additions/deletions were needed.
- The official leaderboard capture lists 13 distinct model configurations; all 13 occur in the rows. The article baseline accounts for the 14th distinct row model. Captured model release dates show none of the listed leaderboard models was released during the month before 2026-09-22.
