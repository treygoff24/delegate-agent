# DeepResearch Bench collection notes

added by coverage audit 2026-09-22

## What it measures

DeepResearch Bench (muset-ai leaderboard, https://huggingface.co/spaces/muset-ai/DeepResearch-Bench-Leaderboard, project site https://deepresearch-bench.github.io) evaluates end-to-end deep-research agents/products (not raw base models in isolation) on producing long-form research reports. Its two published axes, per the underlying paper the project's own naming references, are:

- **RACE** — a composite 0-100 report-quality score judged against a reference, broken into four sub-dimensions: comprehensiveness, insight, instruction-following, readability. The leaderboard's `overall_score` column is this RACE composite.
- **FACT** — citation grounding: `citation_accuracy` (percent of cited sources that actually support the claim) and `effective_citations` (mean count of valid/supporting citations per report). Only reported for models the project ran through its citation-checking pipeline (12 of 45 in this snapshot); the rest show `-` in the source CSV.

Rows in this collection carry both axes: `race_overall_score`, `race_comprehensiveness`, `race_insight`, `race_instruction_following`, `race_readability` (all 45 models), and `fact_citation_accuracy` / `fact_effective_citations` (12 models with FACT data).

## Captured release

- Source file: `data/leaderboard.csv` in the Hugging Face Space repo `muset-ai/DeepResearch-Bench-Leaderboard`.
- Space `lastModified` / last commit: **2026-09-11T06:01:57Z** (the repo history is squashed to a single commit, so this is the only timestamp available; it is treated as the data date).
- Captured at: `2026-09-22T22:00:00Z` UTC.
- Live pages: https://huggingface.co/spaces/muset-ai/DeepResearch-Bench-Leaderboard (app), https://deepresearch-bench.github.io (project).

## Collection method and row count

Fetched `data/leaderboard.csv` (45 model rows, 7 columns) directly from the Space's `resolve/main` path, plus `data/fact_results/<model>/fact_result.txt` for the 7 models whose FACT text files exist in the repo tree (confirmed against the Space's file-listing API; the CSV's `citation_accuracy`/`effective_citations` columns actually cover 12 models — more than the 7 raw `fact_result.txt` files present, so the CSV is treated as the authoritative FACT source and the txt files as corroboration only). Verified for `openai-deepresearch`: `fact_result.txt` gives `valid_rate=0.7501...` and `total_valid_citations=39.7878...`, matching the CSV's `citation_accuracy=75.01` and `effective_citations=39.79` after rounding.

45 models × 5 RACE-family metrics = 225 rows, plus 12 models × 2 FACT metrics = 24 rows. Total: **251 rows, 45 distinct models**. Rows with a `-` placeholder in the source (missing FACT data) were omitted rather than emitted with a value of null, since SCHEMA.md's row unit is (model, metric) and there is no metric to report for those models.

## Metric definitions

- `race_overall_score`: source `overall_score`, 0-100, higher is better.
- `race_comprehensiveness`, `race_insight`, `race_instruction_following`, `race_readability`: the four RACE sub-scores, 0-100, higher is better.
- `fact_citation_accuracy`: source `citation_accuracy` (%), higher is better.
- `fact_effective_citations`: source `effective_citations` (mean valid citations per report), higher is better.

No cost or wall-time figures are published by this leaderboard.

## Caveats

- **Model identity**: this benchmark evaluates deep-research *products/agent configurations* (e.g. `openai-deepresearch`, `gemini-2.5-pro-deepresearch`, `claude-research`, `perplexity-Research`, `kimi-researcher`, `tongyi-deepresearch-30B-A3B`, plus many vendor-specific or unbranded entrants such as `qianfan_deepresearch_0430`, `ZTE-Nebula-DeepResearch-V20260519`, `zhipu_deep_research`, `1688AILab-DeepResearch-*`). These are wrappers/pipelines around underlying models, not the raw frontier LLM releases named in the task brief (no GPT-6 Sol/Astra, Claude Opus 5.5/Fable 5.1, Grok 4.7, etc. appear as named entrants) — the product layer evidently lags the base-model release cadence, or these agent products are simply built on undisclosed or older backends. This is expected for this benchmark's scope (agentic tool-use, not base-model capability) but means it does not give direct base-model comparisons.
- `model_id` values for named products with a clear vendor (OpenAI, Google, Anthropic, Perplexity, Moonshot, ByteDance, xAI, Alibaba, NVIDIA, AI2, Baidu, Zhipu) are set to `vendor/product-slug`; everything else (e.g. `Link`, `xiaoyi`, `WhaleCloud-DocChain`, `cellcog-max`, `octen-deepresearch-0508`, `grep-v5`, `ms_deepresearch*`, `TrajectoryKit`, `onyx`, `deepsynth`, `deepdog`, `RecallRadar`, `MindDR-V1.5`, `tavily-research`, `thinkdepthai-deepresearch`, `salesforce-air-deep-research`, `raaa-deep-research`, `drb_cellcog`, `deepinsight`) is `unknown/<slug>` because the vendor is not confidently inferable from the name alone.
- `reporter` is set to `independent` because this is a third-party (muset-ai) leaderboard aggregating results, not a vendor's own benchmark page; some individual entrants (e.g. `openai-deepresearch`, `gemini-2.5-pro-deepresearch`) are the vendor's own product being scored by this independent harness, not vendor self-reported numbers.
- License: the Space's `README.md` frontmatter states `license: apache-2.0` for the Space code; no separate license is stated for the leaderboard data/CSV itself.
- No confidence intervals, sample sizes, or per-task breakdowns are published at the leaderboard level.

## Raw captures

`raw/deepresearch-bench/leaderboard.csv` (primary source), `space-info.json` (HF Space API metadata, file listing, commit history), `README.md`, `app.py`, `create_leaderboard.py`, `data_viewer_index.json`, and two sampled `fact_results/*/fact_result.txt` files, all under `examples/model-bench/raw/deepresearch-bench/`.

## Verification

Verified 2026-09-22 against the captured `leaderboard.csv`.

- Ran a Python schema check: all 251 lines parse as JSON and every line has all 24 SCHEMA.md fields present.
- Re-extracted 10 randomly sampled rows independently from `leaderboard.csv` via `csv.DictReader` (a separate code path from the generation script) and compared to the collected values: all 10 matched exactly (claude-research race_overall_score, WhaleCloud-DocChain race_instruction_following, ZTE-Nebula-DeepResearch-V20260519 race_comprehensiveness, nvidia-aiq-research-assistant race_insight and race_comprehensiveness, deepinsight race_overall_score, ms_deepresearch_gpt52mixqwen35_09_edit_restart09_think_medium race_insight, 1688AILab-DeepResearch-0325 race_insight, 1688AILab-DeepResearch-0428 race_overall_score, WhaleCloud-DocChain race_comprehensiveness).
- Checked the top 3 models by `race_overall_score` (`qianfan_deepresearch_0430` 58.03, `ZTE-Nebula-DeepResearch-V20260519` 57.27, `Link` 57.08) against the raw CSV's first three data rows: exact match.
- Cross-checked one FACT row (`openai-deepresearch`) against its raw `fact_result.txt`: `valid_rate=0.7501...×100=75.01` matches CSV `citation_accuracy=75.01`; `total_valid_citations=39.7878...` matches CSV `effective_citations=39.79` (rounded).
- No mismatches found; no corrections were needed.
