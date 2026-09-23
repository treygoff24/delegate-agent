# UIBenchKit collection

- Captured: 2026-09-22 (UTC retrieval timestamp in each row: `2026-09-23T01:10:38Z`).
- Freshness: the live leaderboard payloads identify their last updates as `2026-03-10T06:31:55Z` for DCGen and `2026-03-08T11:48:05Z` for Design2Code. This is the data date reported by the source; the collection was made on 2026-09-22 local time.
- Benchmark version: the leaderboard JSON does not state a benchmark release/version, so `benchmark_version` is null.
- Coverage: 37 published configurations (17 DCGen, 20 Design2Code), spanning 16 distinct model names. The data has 14 percentage metrics and three per-instance token metrics per configuration. One Design2Code `gpt-5` / `layoutcoder` configuration does not publish `vision_prompt_tokens_per_instance` or `text_prompt_tokens_per_instance`, so those two rows are absent: 627 rows total rather than 629 possible rows. Its `response_tokens_per_instance` is included.

## What it measures

UIBenchKit evaluates screenshot-to-code generation on the DCGen and Design2Code datasets. Its source describes code similarity, CLIP visual similarity, and fine-grained Design2Code-style visual metrics. The leaderboard groups results by model and generation method (`direct`, `dcgen`, `latcoder`, `layoutcoder`, or `uicopilot`); method is recorded as `harness` in the shared rows.

## Metrics

The published percentage metrics are `clip`, `code_similarity`, `block_match`, `text`, `position`, `color`, and `fg_clip`, with corresponding `_all` versions. The JSON publishes these as percentage strings; rows preserve the displayed numeric percentage in the value and use `%` as the unit. Metric names are retained verbatim to avoid assigning undocumented definitions. The leaderboard calls CLIP visual similarity, code similarity source-code comparison, and block/text/position/color/foreground CLIP fine-grained measures. The JSON also provides `vision_prompt_tokens_per_instance`, `text_prompt_tokens_per_instance`, and `response_tokens_per_instance`; these are recorded as tokens/instance, with lower values better. Cost and elapsed time are not present in the leaderboard payloads.

## Collection method and source

The homepage is a JavaScript-rendered shell. I downloaded its HTML and JavaScript bundle, followed the bundle's request to the site's `/.netlify/functions/github-proxy` endpoint, then downloaded the raw `leaderboard/dcgen-results.json`, `leaderboard/design2code-results.json`, and `leaderboard/trajectory_links.json` responses. The exact payloads, homepage, bundle, GitHub README, and GitHub license are preserved under `examples/model-bench/raw/uibenchkit/`. The frontend's metric selector includes all numeric keys in the payload, including both base and `_all` metrics and all three token metrics.

Leaderboard results are community submissions with manual checks described on the submission page; the source does not provide confidence intervals, sample counts, or score measurement dates in these JSON rows. The per-run timestamps in `run_id` are retained in the raw payload but are not treated as a measurement date because the source does not define them as such. The displayed model list is old relative to this collection date: newest leaderboard update found is 2026-03-10, and no later result payload was exposed by the live page.

## License and limitations

The GitHub repository's `LICENSE` is MIT (copy preserved in the raw directory). The Hugging Face dataset README/card URL returned 404, and no separate license or redistribution terms for the leaderboard result data or underlying datasets were stated in the captured materials; row-level `license` is therefore null. Do not infer that the result data or datasets inherit the repository-code license. Current frontier model coverage, raw experiment artifacts beyond the trajectory index, cost, time, confidence intervals, and sample counts could not be captured from the leaderboard result files.

## Verification

- Parsed all 627 non-empty JSONL rows; each has all 24 schema fields and values of the documented types. No parse or schema errors.
- Compared 10 score rows against the captured DCGen and Design2Code payloads, including the three highest percentage scores. All 10 values matched exactly: Design2Code Claude-Opus-4.5/direct `text` (98.78%), `text_all` (98.78%), GPT-4.1/direct `text` (98.46%); and DCGen Claude-3.7-Sonnet/direct `code_similarity` (14.51%), `clip` (87.43%), `block_match` (70.31%), `text` (88.71%), `position` (80.89%), `color` (79.18%), and `fg_clip` (90.12%).
- Coverage check: the captures list 37 configurations and 16 distinct model names across both leaderboards; rows contain all 16 names, with no source-listed model missing and no extra row model. Captured result payloads are dated March 8 and March 10, 2026, and their model-date metadata contains no August 22–September 22, 2026 model release.
- Changes: none. No mismatched or unsupported sampled values were found, and the capture-backed model coverage matches the rows.
