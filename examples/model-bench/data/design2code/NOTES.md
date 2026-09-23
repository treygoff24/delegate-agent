# Design2Code results

## Measurement and capture

Design2Code evaluates multimodal models that generate webpage code from a reference screenshot. The original test set has 484 real-world webpages; the paper also reports a separate Design2Code-HARD subset of 80 difficult webpages. This is the newest official result set found in the benchmark's own repository and paper: arXiv v3, posted 2025-02-09 (NAACL 2025). The benchmark repository README was retrieved on 2026-09-23 and links its dataset, predictions, and evaluation assets. I captured the README, arXiv metadata, v3 source archive, paper PDF, LaTeX source, and the human-evaluation and learning-curve figures in `examples/model-bench/raw/design2code/`.

Rows were transcribed from the paper's automatic evaluation tables (main benchmark and HARD), its simulated win-rate tables, the labeled human pairwise-comparison chart, and direct-assessment summary in the paper text. Scores are stored as published percentages; they were not rescaled. The row source URL points to the exact arXiv v3 paper. There are 269 rows for 13 canonical model IDs: 115 main-benchmark automatic scores (23 model/prompt configurations × 5 metrics), 90 HARD automatic scores (18 configurations × 5 metrics), 32 simulated win-rate scores, 30 human pairwise win/tie/loss scores, and 2 direct-assessment results. Multiple rows for one configuration are separate reported metrics, not additional model configurations.

## Metrics

- `block_match`: size-weighted coverage of matched visual blocks, penalizing missing and hallucinated blocks.
- `text_similarity`: character-level text similarity across matched blocks.
- `position_similarity`: normalized spatial similarity of matched blocks.
- `color_similarity`: color similarity of matched blocks.
- `clip_similarity`: CLIP-ViT-B/32 visual similarity of generated and reference screenshots after masking detected text.
- `simulated_win_rate`: learned logistic/linear model prediction of human preference, evaluated over all 484 examples. The paper reports some model/configuration values in separate tables.
- `human_win_plus_tie_rate`: human majority-vote win plus tie share, from 100 examples.
- `human_pairwise_win_rate`, `human_pairwise_tie_rate`, `human_pairwise_lose_rate`: human majority-vote comparison against Gemini Pro Vision direct prompting, from 100 examples.
- `human_replacement_rate`: share judging GPT-4V self-revision output similar enough to replace the reference, 100 examples.
- `human_better_design_rate`: share preferring the GPT-4V self-revision design over the reference design, 100 examples.

All score metrics are higher-is-better. `n` is populated only where the paper states the number of examples. Costs, times, confidence intervals, and reasoning-effort settings are not reported. Prompting methods are retained in `model_raw` and `harness`.

## Caveats and terms

These are independent research results, but proprietary API results are author-reported and the paper does not provide a current independent reproduction. Model/API versions are historical; the paper identifies GPT-4o as `gpt-4o-2024-05-13`, GPT-4V as `gpt-4-1106-vision-preview`, Claude 3 Opus as `claude-3-opus-20240229`, and Gemini 1.0 Pro Vision as `gemini-1.0-pro-vision`. The README describes API access and prompting experiments; do not interpret these entries as current frontier model performance. The authors note comparisons are not apples-to-apples because model sizes and training data differ. Potential benchmark contamination is not quantified in the source.

The repository says its data, code, and checkpoint are licensed/intended for research use only and says the benchmark was built on C4 under ODC-By. No license is declared verbatim for redistribution of the score tables; the per-row license preserves the source's stated terms. Confirm rights before external republication.

The paper also contains a plotted normalized training-learning curve and additional human-evaluation visualizations. The learning-curve plot does not print exact underlying values, so those points were not digitized or estimated. The reported numeric model results and explicitly labeled human comparison rates were captured. Cost/time/effort are unavailable, not zero.

## Verification

- Parsed all 269 JSONL lines; every row has all 24 fields in `examples/model-bench/SCHEMA.md`. No parse or field-set failures.
- Source-checked 15 rows against the captured arXiv v3 LaTeX and figure: rows 7, 12, 2, and 22 (the top three score levels, including the tie at 98.2); rows 116, 120, 202, and 205 (Design2Code-HARD); rows 206 and 207 (simulated and annotated win-plus-tie rates); rows 226-228 (GPT-4o Direct pairwise win/tie/loss); and rows 256-257 (direct-assessment rates). All matched. The direct-assessment values also appear in the captured paper text.
- Counted 13 distinct models in the paper's main and HARD result tables after removing overlap; the rows contain 13 distinct `model_id` values. The paper's evaluated model versions predate the month before 2026-09-22.
- No mismatches or missing source-listed models were found. No rows were changed or added.
