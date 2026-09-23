# AA-LCR capture notes

## What it measures

Artificial Analysis Long Context Reasoning measures extraction, cross-document reasoning, and synthesis over long-form documents. Artificial Analysis describes 100 open-answer questions spanning document domains and formats; it scores answers pass/fail, with another LLM judging equivalence to the official answer. It tests long-context reasoning around 100k input tokens, not a model's maximum context window.

## Capture

- Benchmark release: **AA-LCR v1.1**.
- Captured: **2026-09-23 UTC** (retrieval initiated 2026-09-22 local time). The page gives no per-result measurement date or dated leaderboard refresh, so `measured_on` is null.
- Source: [AA-LCR leaderboard](https://artificialanalysis.ai/evaluations/artificial-analysis-long-context-reasoning), fetched as raw HTML in `raw/aa-lcr/leaderboard.html`. Structured Dataset blocks embedded in the page are preserved in `raw/aa-lcr/embedded-datasets.json`.
- The embedded score dataset contains **20 configurations**. `rows.jsonl` has 20 score rows (one metric per exposed configuration), covering **16 distinct canonical model IDs**. Configurations with different effort/fallback labels remain separate rows.
- The public page exposes only a top-20 score dataset in its embedded payload. The Data API docs identify `GET /api/v2/language/models` as a source for evaluation metrics, but direct requests to standard and free endpoints returned HTTP 401, `API key is required`. Docs and response bodies are preserved in `raw/aa-lcr/`. No API credential was available. **Coverage is partial:** the other published AA-LCR configurations could not be captured.

## Metrics and row rules

- `accuracy` is the AA-LCR pass rate. The page provides a fraction; values were multiplied by 100 and stored as percent at source precision (the rendered page rounds headline values). Higher is better.
- Effort is taken from a stated parenthetical setting (`max`, `xhigh`, `medium`, or `high`); full labels, including fallback wording, remain verbatim in `model_raw`.
- Separate top-20 datasets provide cost per task and time per task. Matching configurations are paired onto score rows. Cost is the sum of published answer, reasoning, cache-write, cache-hit, and input USD components, with basis `per_task`. Time is published as weighted average decode time in minutes (excluding time to first token and overhead) and converted to seconds, with basis `per_task`. Unmatched fields are null.
- `n` is 100 based on the stated question count. Confidence intervals and exact per-model test dates are not published and are null.

## Caveats and terms

Artificial Analysis describes the results as independently benchmarked. The captured page does not disclose per-result run dates, contamination controls, or independent replication details; these are unknown. An LLM judges answer equivalence. No specific contamination assessment is stated.

The embedded datasets link to Artificial Analysis' [Terms of Use](https://artificialanalysis.ai/docs/legal/Terms-of-Use.pdf); the Data API docs say API terms apply. This capture does not establish a separate open-data redistribution license. The rows retain source attribution and should be treated as subject to those terms.

## Capture limits

The score data is limited to the 20 configurations in the page's embedded score dataset, while the leaderboard states a larger overall population. The full table was unavailable in the public payload and the documented API required a key. Cost and time are each limited to their corresponding embedded top-20 datasets.

## Verification

- Checked all 20 rows (the required 10-row sample, including the three highest scores); all 20 score labels and values match both the captured score dataset and the live leaderboard's score dataset. No score mismatches were found.
- Confirmed every line parses as JSON and includes all 24 fields in `SCHEMA.md`; no schema-field or type errors were found.
- Checked the recent-release rows: the live Artificial Analysis model pages date GPT-6 Sol and GPT-6 Luna to 2026-09-22, and both configurations are present.
- Coverage is not complete: the live leaderboard reports 32 of 547 models, while its public embedded score dataset and this file expose only 20 configurations (16 distinct canonical model IDs). The documented API endpoints return an API-key-required response without credentials, so the other 12 configurations could not be verified or safely added. No row values were changed.
