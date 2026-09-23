# Artificial Analysis Intelligence Index and model leaderboard

## Capture

- **Captured:** 2026-09-22 (America/New_York). The response headers report 2026-09-23 UTC; this is the same instant after midnight UTC.
- **Benchmark version:** not stated in the captured leaderboard data. `benchmark_version` is therefore null.
- **Source:** <https://artificialanalysis.ai/leaderboards/models>.
- **Retrieval:** saved the full public leaderboard HTML, response headers, and the model array parsed from the page's Next.js hydration payload under `raw/artificial-analysis-index/`. The data page documents `GET /api/v2/language/models`; a direct request without an API key returned HTTP 401 (`API key is required`). No credential was used. The public leaderboard HTML nevertheless contains the complete model-level array.
- **Counts:** 673 model configurations in the page payload; 13,019 rows covering 37 numeric metric fields. The source marks 275 configurations non-deprecated and 398 deprecated. Nine configurations have no numeric Intelligence Index value; they remain represented by any other numeric results they publish. Per-field nulls and the source placeholder string `$undefined` do not become measurement rows. The page's static FAQ says “168 models”; it does not match the 275 non-deprecated configuration entries in the captured live payload, so that FAQ appears stale or counts a different unit. All 673 configurations were retained because they occur in the current published payload.
- **Dates:** no model-level measurement or posting dates are included in the captured records. `measured_on` is null; the data date is the capture date, not a claim that all measurements were produced that day.

## What it measures

Artificial Analysis describes the Intelligence Index as an aggregate index across benchmark results. The captured page publishes that score, its component scores, model pricing, and inference performance. The separate methodology page says the benchmark aims to measure real-world performance experienced through inference providers and systems, rather than maximum hardware capability. Model configurations include reasoning-effort variants; the source marks some Index values as estimated.

`model_raw` is the page's `name` string. `effort` contains a recognized reasoning/configuration suffix when the source name supplies one (for example, `Adaptive Reasoning, Max Effort, Default Fallback`); otherwise it is null. `model_id` is a best-effort lowercase `creator/model-name` alias, with recognized effort suffixes removed so effort variants share a model ID. The source's `deprecated` and estimated flags are preserved in row notes. The row file retains every numeric value at the precision in the embedded source data; proportion-like scores remain fractions and are not multiplied into percentages.

## Metrics

| Source field | Row metric | Category and interpretation |
| --- | --- | --- |
| `intelligenceIndex` | `index_score` | `aggregate`; Intelligence Index score. Its paired numeric `intelligenceIndexCostPerTask` is stored in `cost_usd` on this row. |
| `omniscience`, `omniscienceAccuracy`, `omniscienceNonHallucination` | `aa_omniscience`, `aa_omniscience_accuracy`, `aa_omniscience_non_hallucination` | `reasoning`; AA Omniscience score and its accuracy/non-hallucination values. |
| `gdpvalNormalized`, `analystAgent` | `gdpval_normalized`, `analyst_agent` | `knowledge-work`; normalized GDPval and Analyst Agent scores. |
| `terminalbenchHard`, `terminalBench21`, `terminalBench40` | `terminalbench_hard`, `terminalbench_2_1`, `terminalbench_4_0` | `agentic-coding`; Terminal-Bench scores (the field names distinguish Hard, 2.1, and 4.0). |
| `tau2`, `tauBanking` | `tau2_bench`, `tau_banking` | `tool-use`; tau² and tau Banking scores. |
| `lcr` | `aa_lcr` | `long-context`; AA-LCR score. |
| `hle`, `gpqa`, `critpt` | `hle`, `gpqa`, `critpt` | `reasoning`; source score fields. |
| `scicode` | `scicode` | `science`; SciCode score. |
| `ifbench` | `ifbench` | `instruction-following`; IFBench score. |
| `apexAgents`, `itbenchSre` | `apex_agents`, `itbench_sre` | `agentic-coding`; APEX Agents and ITBench SRE scores. |
| `mmmuPro` | `mmmu_pro` | `multimodal`; MMMU-Pro score. |
| `price1mInputTokens`, `price1mOutputTokens`, `cacheHitPrice`, `cacheWritePrice` | `price_usd_per_mtok_input`, `price_usd_per_mtok_output`, `price_usd_per_mtok_cache_hit`, `price_usd_per_mtok_cache_write` | `speed-cost`; USD per million tokens. |
| `medianOutputTokensPerSecond`, `percentile05OutputTokensPerSecond`, `quartile25OutputTokensPerSecond`, `quartile75OutputTokensPerSecond`, `percentile95OutputTokensPerSecond` | `tokens_per_second_median`, `tokens_per_second_p05`, `tokens_per_second_p25`, `tokens_per_second_p75`, `tokens_per_second_p95` | `speed-cost`; output-token throughput statistics, in tokens/second. |
| `medianTimeToFirstTokenSeconds`, `percentile05TimeToFirstTokenSeconds`, `quartile25TimeToFirstTokenSeconds`, `quartile75TimeToFirstTokenSeconds`, `percentile95TimeToFirstTokenSeconds` | `time_to_first_token_median`, `time_to_first_token_p05`, `time_to_first_token_p25`, `time_to_first_token_p75`, `time_to_first_token_p95` | `speed-cost`; time to first token, in seconds. |
| `medianTimeToFirstAnswerTokenSeconds` | `time_to_first_answer_token_median` | `speed-cost`; time to first answer token, in seconds. The methodology distinguishes this from first reasoning-token latency. |
| `medianEndToEndResponseTimeSeconds` | `end_to_end_response_time_median` | `speed-cost`; end-to-end response time, in seconds. |
| `medianReasoningTimeSeconds` | `reasoning_time_median` | `speed-cost`; reasoning time, in seconds. |

The methodology defines Index cost per task as a weighted-average USD cost using the Index benchmark weights and token usage; prices may be first-party provider rates or medians across providers. It defines output speed as tokens per second after the first token, first-token latency from request to first response token, first-answer-token latency after reasoning, and end-to-end time through the complete response. Other constituent score definitions and per-benchmark weighting details were not available in the captured page text; scores are therefore recorded under the names and categories shown above without reconstructing a common scale. Source fractions are kept as fractions (`unit: fraction`); the Index and Omniscience aggregate scores use `unit: score`.

## Caveats and terms

- Artificial Analysis identifies itself as an independent benchmarking source. The captured methodology describes tests of hosted model endpoints and says listed prices are provider first-party rates or medians across providers. The page does not provide per-row sample counts, confidence intervals, provider-level provenance, or measurement dates; `n`, `ci`, and `measured_on` are null. Pricing can change independently of benchmark scores.
- The source marks 398 configurations deprecated. They are included because the current payload lists them, with a deprecation note on each row. The page does not supply dates for those entries, and they may be stale.
- The captured methodology and leaderboard did not provide a contamination audit for the individual constituent benchmarks. Contamination risk cannot be assessed from these records.
- The data API page says the free API is for internal use and that redistribution rights require an appropriate commercial package. The captured Data Platform Terms v1.1 (last revised 2026-08-19) define raw data to include scores and metrics and prohibit redistribution of raw data files. The site's Terms of Use also limit site access/content use. This local capture is for internal research; these terms do not grant permission to publish or redistribute the rows. Refer to `raw/artificial-analysis-index/data-api.html`, `terms-of-use.html`, and `data-platform-terms.pdf` for the source terms.
- This capture could not obtain the authenticated API response, a benchmark version identifier, per-model score dates, sample sizes, confidence intervals, or a full model-level test provenance record.

## Verification

Checked on 2026-09-22 against `raw/artificial-analysis-index/models.json` and
`SCHEMA.md`. All 13,019 JSONL lines parse; each row has all 24 schema fields.
Reconciled every row's source metric value, category, and paired Index cost
against the captured model records: no mismatches, missing metrics, duplicates,
or extra metrics.

The required 10-row spot check also matched the same-number values in the raw
capture (row number here is the 1-based JSONL line):

| Row | Model configuration | Metric | Value | Result |
| ---: | --- | --- | ---: | --- |
| 2732 | Claude Opus 5.5 (Adaptive Reasoning, Max Effort, Default Fallback) | `index_score` | 57.6223698102963 | match |
| 11617 | Claude Opus 5.5 (Adaptive Reasoning, Xhigh Effort, Default Fallback) | `index_score` | 55.9873505840139 | match |
| 12369 | Claude Opus 5.5 (Adaptive Reasoning, High Effort, Default Fallback) | `index_score` | 53.5831959822067 | match |
| 2733 | Claude Opus 5.5 (Adaptive Reasoning, Max Effort, Default Fallback) | `aa_omniscience` | 46.416666666666664 | match |
| 2716 | GPT-6 Astra (max) | `price_usd_per_mtok_input` | 10 | match |
| 50 | Gemini 3.5 Flash (high) | `tokens_per_second_median` | 212.725393929573 | match |
| 5 | GLM-4.5V (Non-reasoning) | `terminalbench_hard` | 0.0681818181818182 | match |
| 1 | GLM-4.5V (Non-reasoning) | `index_score` | 6.69669796944385 | match |
| 2 | GLM-4.5V (Non-reasoning) | `aa_omniscience` | -55.583333333333336 | match |
| 3 | GLM-4.5V (Non-reasoning) | `aa_omniscience_accuracy` | 0.18216666666666667 | match |

The top three Index scores are included. Claude Opus 5.5 was released on
2026-09-22, confirmed by [Anthropic's announcement](https://www.anthropic.com/claude-opus-5-5); it is in the required recent-release sample. Coverage: the capture lists 673 model configurations; 669 have at least one numeric field represented by this benchmark's metric mapping, and all 669 occur in the rows. The other four have no numeric benchmark metric (only context-window metadata), so they correctly produce no measurement rows. No row corrections were needed.
