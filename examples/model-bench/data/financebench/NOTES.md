# FinanceBench results capture

Captured 2026-09-22 (retrieved 2026-09-23 UTC). The benchmark tests open-book financial question answering over real company filings and financial statements. Patronus describes a 10,231-question corpus and publishes an annotated 150-question sample; the published original evaluation files contain 16 model/configuration files with 150 human-reviewed answers each. The current FinanceBenchmark listing describes the same 150-task benchmark and publishes eight additional model entries dated March or April 2026.

## Sources and method

- Patronus source repository: <https://github.com/patronus-ai/financebench>. Its README says that the `results/` directory contains human-annotated completions for configurations evaluated in the paper. We downloaded all 16 JSONL files visible in the repository's `results/` directory. The 16 corresponding rows in `rows.jsonl` report `accuracy` as the exact count of `Correct Answer` labels divided by 150, converted to percent; refusals and incorrect answers are not counted as correct. Per-answer raw files are retained under `raw/financebench/patronus-results/`.
- Current aggregator: <https://financebenchmark.ai/benchmarks/financebench>. We captured the raw HTML and transcribed all eight rows from its Full Results Table. Its listed scores are GPT-5.5 91.0%, Claude Opus 4.7 88.0%, Gemini 3.1 Pro 86.0%, DeepSeek V4 Pro 85.0%, Grok 4.3 84.0%, Kimi K2.6 82.0%, GLM-5.1 79.0%, and MiniMax M2.7 77.0%. The table's dates have month precision only (March or April 2026); exact run dates are unavailable, so `measured_on` is null and the month is recorded in row notes.
- The aggregator's methodology (version 1.2, last updated June 2026; <https://financebenchmark.ai/methodology>) says arXiv scores are rerun using its own evaluation, but the FinanceBench results table cites the 2023 FinanceBench paper for all rows, including model names released in 2026. We preserve the scores as currently published while marking this attribution inconsistency; we could not independently verify the runs or their scoring protocol from linked run artifacts.

## Metrics and limitations

`accuracy` is higher-is-better percent of answers marked correct. In original Patronus rows it is computed from answer-level human labels, not copied from a summary table. The current aggregator supplies percentages directly. No cost or time data are paired with these results. The original 16 configurations and the aggregator's eight entries use different, incompletely documented evaluation contexts; they are preserved as separately versioned result groups and should not be treated as directly comparable. The task set is fixed and retrieval/prompt scaffold choices materially affect scores. The aggregator's newer entries are third-party/aggregator reports; its original-source attribution is inconsistent as described above. Benchmark contamination cannot be ruled out for either the public sample or models with pretraining after the paper's publication.

The capture is dated 2026-09-22; aggregator rows list score months March/April 2026, and the Patronus records are from the original 2023 paper evaluation. No reasoning-effort settings, confidence intervals, or paired dollar costs/times are published in the captured sources.

## License

The GitHub README points users to the repository and dataset but does not state redistribution terms in the captured text. The manifest describes the license as “See dataset license”; therefore the row license fields are null. The raw captures are retained for audit only; confirm the repository's current dataset license before redistribution. The aggregator provides no separate data-license terms on the captured benchmark or methodology pages.

## Count

24 rows total: 16 Patronus model/configuration results plus 8 current FinanceBenchmark listing entries, each with one accuracy metric. There are 24 reported configurations/entries across these two sources. Duplicate vendor/model names are not deduplicated because their configurations and source protocols differ.

## Verification

- Parsed all 24 JSONL lines; each is a JSON object and has all 24 fields listed in `SCHEMA.md`.
- Checked 10 rows against their source records: rows 6 and 7 against their Patronus answer-level captures, and rows 17-24 against the captured FinanceBenchmark results page. Both Patronus counts match exactly (128/150 = 85.333...%; 134/150 = 89.333...%), and all eight aggregator model/score pairs match. This sample includes the three highest-scoring rows (GPT-5.5 91.0%, GPT-4-1106-preview oracle_reverse 89.333...%, Claude Opus 4.7 88.0%). Result: 0 mismatches.
- Checked release dates for the listed models; none were released during Aug 22-Sep 22, 2026. In particular, DeepSeek V4 Pro's official GA announcement is dated Aug 13, 2026. The Patronus configurations are from the 2023 evaluation; the other listed models' releases are earlier than this window.
- Coverage: the captures contain 16 Patronus configuration files and 8 FinanceBenchmark table entries (24 source entries/configurations; 12 distinct model names). The JSONL has 24 rows, matching all listed entries. No omissions or unsupported rows found.
- Changes: none to `rows.jsonl`; no sampled values or coverage entries required correction.
