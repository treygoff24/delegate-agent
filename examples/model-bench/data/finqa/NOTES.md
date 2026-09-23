# FinQA results

FinQA measures numerical reasoning over financial documents, with multi-step computation. The current FinanceBenchmark page lists 8,281 tasks and reports one overall result for each of eight language-model configurations. The FinQA paper describes execution accuracy (whether the generated program yields the correct final answer) and program accuracy; the FinanceBenchmark table publishes only “Overall.” Its methodology says it runs evaluations on arXiv-sourced benchmarks against the public dataset using the original authors’ scoring protocol. I therefore interpret these overall percentage scores as execution accuracy.

## Captured data

- Data page: https://financebenchmark.ai/benchmarks/finqa
- Retrieved: 2026-09-23 UTC (2026-09-22 in America/New_York).
- Latest score evaluation month shown in the data bundle: 2026-03. Individual evaluations are dated 2026-02 or 2026-03; the source gives months, not full dates, so `measured_on` is null and each month is preserved in the row notes.
- Source bundle identifies `evaluatedBy: financebenchmark`; the values and display names were taken from the live page’s data bundle and checked against the page’s rendered result table. The original dataset README, repository metadata/tree, license, and paper are also saved in `raw/finqa/`.
- Version/split: not stated by the result page. The page’s task count is 8,281; it does not specify how many examples are used per reported score.

## Metric and coverage

Scores are percentages (0–100), higher is better. `execution_accuracy` is the exact overall number published by the leaderboard, without unit conversion. There are no cost, time, effort, uncertainty, or per-model sample-count values published for these rows. No reasoning-effort variants are listed. The source displays 8 models and this file contains 8 rows (one overall metric per configuration).

## Caveats and terms

These are independently reported by FinanceBenchmark, not reproduced in this collection. The platform methodology says it performs its own runs for arXiv benchmarks, but the individual FinQA rows do not link to run logs or identify model API versions, prompts, decoding settings, or per-run counts. The leaderboard’s associated attribution URL is the original FinQA paper, not a run artifact; the row `source_url` points to the leaderboard page from which the published values were captured. The test set is fixed and may be susceptible to training-data contamination or saturation; the results page does not address either issue. The scores are stale relative to model releases current at retrieval: all listed evaluation months are February or March 2026.

The FinQA GitHub repository declares the dataset repository under the MIT License (`LICENSE` saved in the raw capture). The FinanceBenchmark page/methodology does not state a separate redistribution license for its scores or data bundle; row-level `license` is therefore null.

No score-level run artifacts, exact evaluation dates, or per-score sample counts were available to capture.

## Verification

- Parsed all 8 JSONL lines; every line is valid JSON and contains all 24 fields in `examples/model-bench/SCHEMA.md`.
- Checked all 8 rows (the file has fewer than 10), including the three highest numeric scores: DeepSeek V4 Pro (87), Claude Opus 4.7 (84), and GPT-5.5 (82). All eight model names and score values match the live FinanceBenchmark results table and the captured page/data bundle. No score value or row was changed.
- Coverage matches: the live FinQA page lists 8 models; the file contains 8 rows.
- Provenance mismatches remain in seven rows: the source assigns evaluation months earlier than official release dates for Claude Opus 4.7 (2026-03 vs. Apr 16), GPT-5.5 (2026-03 vs. Apr 23), DeepSeek V4 Pro (2026-02 vs. Apr 24 preview; Aug 13 GA), Grok 4.3 (2026-03 vs. available by Jun 17), Kimi K2.6 (2026-03 vs. Apr 21), and GLM-5.1 (2026-02 vs. Apr 7), and MiniMax M2.7 (2026-02 vs. Mar 18). The page itself publishes these pairings, so the underlying scores cannot be corrected from the available evidence. Gemini 3.1 Pro was not counted as a timing mismatch because its February release and month-only evaluation date do not establish a contradiction. None of the eight listed models has an official release in the rolling month before this check; the newest cited release among them is DeepSeek V4 Pro GA on Aug 13.
- The rendered ranking labels are also inconsistent with the scores: it marks Claude Opus 4.7 (84) first and DeepSeek V4 Pro (87) fourth, while the table gives DeepSeek the top score. The row values preserve the source's scores; no rank field exists in the schema.
- Source checks: [FinanceBenchmark FinQA](https://financebenchmark.ai/benchmarks/finqa); official release references: [Anthropic](https://www.anthropic.com/claude/opus), [OpenAI](https://openai.com/index/introducing-gpt-5-5/), [DeepSeek preview](https://api-docs.deepseek.com/news/news260424/) and [GA](https://api-docs.deepseek.com/news/news260813/), [xAI](https://x.ai/news/grok-amazon-bedrock), [Moonshot](https://forum.moonshot.ai/t/meet-kimi-k2-6-advancing-open-source-coding/369), and [MiniMax](https://www.minimax.io/news/minimax-m27-en), and [Z.ai GLM research listings](https://www.zhipuai.cn/en/research).
