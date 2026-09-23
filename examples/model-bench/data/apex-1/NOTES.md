# APEX-1 collection notes

- **What it measures:** APEX-1 evaluates single-turn professional knowledge work across investment banking, management consulting, big law, and primary-care medicine. Mercor says its held-out leaderboard covers 400 tasks (100 per job); its open task slice is separate.
- **Version and capture date:** APEX-v1-extended. Captured 2026-09-22 America/New_York (2026-09-23 UTC). The page did not expose a score-posted or evaluation date, so `measured_on` is null.
- **Retrieval:** Downloaded the live Mercor leaderboard HTML and the Hugging Face dataset README. The leaderboard's embedded Next.js page data contains all 47 global model configurations and their `pass-1` and `mean-score` entries, their `error` values, effort, and harness. The decoded page data is saved in `raw/apex-1/leaderboard-data.json`; the original page capture is `raw/apex-1/leaderboard.html`.
- **Rows:** 47 model configurations × 2 published metrics = 94 rows. Each score has a source `error` copied into `ci` as published (percentage-point error). The source's `nSamples` field is not described as task count, so `n` remains null. No cost or time is published.
- **Metric definitions:** Mercor defines Mean Score as the average percentage of rubric criteria passed across benchmark tasks. Pass@1 is the percentage of tasks completed on one attempt with a 100% rubric score. Scores are percentages and higher is better. The embedded dataset declares the available leaderboard metric as Mean Score, while its `globalLeaderboard` records both Mean Score and Pass@1; both have been collected.
- **Method and caveats:** The page says prompts are graded with expert-authored rubrics and an LM judge; the dataset card describes eight runs per model and Gemini 2.5 Pro (Thinking=On) as judge for this extended version. Results are published by Mercor, the benchmark owner, and were not independently reproduced here. The open dev set is explicitly in-distribution; possible overlap with model training data and the training-data status of the held-out set are not established here. No posting timestamps per model were present, so individual results cannot be classified as stale from this capture. Effort values are recorded separately from the source model names.
- **License / redistribution:** The Hugging Face dataset card declares CC BY 4.0. It also states evaluation-only use, forbids training/fine-tuning/parameter fitting, and asks users not to crawl, scrape, or download the dataset programmatically. Only the README was retrieved; no benchmark task data was downloaded. The page's structured data also identifies CC BY 4.0.
- **Not captured:** No per-domain breakdown or dollar cost / elapsed time values appeared in this leaderboard data. Only the aggregate leaderboard and its two score metrics were available in the embedded data.

Sources: [Mercor APEX-1 leaderboard](https://www.mercor.com/apex/apex-v1-leaderboard/); [Hugging Face APEX-v1-extended dataset card](https://huggingface.co/datasets/mercor/APEX-v1-extended).

## Verification

- Parsed all 94 JSONL lines and checked each against the 24 schema fields and their declared types; all passed.
- Compared every row's model configuration, metric value, and published error against `raw/apex-1/leaderboard-data.json`. All 94 pairs matched. Configurations were matched by model name and effort to distinguish the two Opus 4.6 entries.
- The three highest-scoring rows (Muse Spark 1.3 mean score 74.0, Fable 5.1 mean score 73.8, GPT-5.6 Terra mean score 69.5) match the capture and the live Mercor leaderboard.
- Checked both metrics for the four models with release dates in the past month: GPT-6 Astra, Muse Spark 1.3, Gemini 3.8 Flash, and Fable 5.1. All eight rows match the capture.
- Coverage: Mercor's captured source lists 47 model configurations; the rows contain the same 47 configurations and all 94 metric pairs. No additions, deletions, or corrections were needed.
