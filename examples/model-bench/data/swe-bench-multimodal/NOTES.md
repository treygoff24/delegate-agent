# SWE-bench Multimodal collection notes

## Benchmark and captured data

SWE-bench Multimodal evaluates agent systems on real repository issue-resolution tasks described with text and visual elements. The benchmark page says the original release had 517 issues with visual elements. The current page announces Multimodal v2, dated September 1, 2026, with 480 tasks selected for reproducible evaluation after removing known flaky or ungradeable tests.

Captured on 2026-09-23 UTC (2026-09-22 in the requested America/New_York date) from the official SWE-bench leaderboard root page and its embedded `leaderboard-data` JSON. The JSON was extracted without filtering from the `Multimodal` result group and saved under `raw/swe-bench-multimodal/leaderboard-multimodal.json`; the full downloaded HTML is `official-leaderboard.html`. Hugging Face's dataset API metadata is saved as `huggingface-dataset-api.json`. The dataset API provides task data (100 dev and 480 test examples), not model scores.

The result group contains 22 system configurations and one reported metric per configuration, so `rows.jsonl` contains 22 rows. The latest result record date shown is 2025-11-17. The v2 announcement is newer, but none of the leaderboard records identifies itself as a v2 evaluation; `benchmark_version` is therefore null and the listed scores should be treated as legacy/unspecified-version results, not as v2 scores.

## Metric

`resolved_rate` is the leaderboard's `resolved` percentage: the percentage of task instances solved. Values and displayed precision are copied directly, in percent units. Higher is better. All 22 entries have `cost: null`; the leaderboard does not publish paired cost or elapsed time for these entries. Confidence intervals and evaluated sample counts are not exposed in the captured result records and remain null.

## Coverage, provenance, and caveats

Rows represent complete leaderboard system configurations. `model_raw` preserves the source's result `name`; `harness` is the source's agent/scaffold field. Model IDs are canonicalized from the source `model_display`; two systems disclose only “Undisclosed,” represented as `unknown/undisclosed`. No reasoning-effort setting is stated.

The source has a `checked` flag. The page legend defines checked entries as runs performed or directly checked by the SWE-bench team. Twenty-one captured rows are checked; the Refact.ai Agent row is unchecked and marked `reporter: vendor`. Other rows are marked `reporter: independent`. Entries are dated between 2024-10-06 and 2025-11-17, and none is dated in 2026; they are stale relative to the September 2026 v2 release. Submission coverage for v2 is therefore not established by these results. The leaderboard's entries compare different harnesses and, in some cases, different underlying model versions; they are not controlled model-only comparisons. Benchmark contamination or training overlap is not assessed by the leaderboard data.

## License and redistribution

The leaderboard page carries a 2026 SWE-bench Team copyright notice and does not state a separate data license. The Hugging Face dataset API metadata captured here does not state a license in its card metadata. Redistribution terms are therefore unknown; raw files are retained as audit captures, and this note does not assert an open license.

## Not captured

No v2-specific model submission scores, per-task results for the current v2 split, confidence intervals, cost, or time were found in the current official leaderboard payload. The public dataset API exposes evaluation tasks, not results.

## Verification

Checked on 2026-09-22 against the captured official leaderboard payload and `SCHEMA.md`. All 22 JSONL lines parse, contain all 24 schema fields, and use the declared field types. Compared every row with the corresponding `Multimodal` source record, including the three highest-scoring entries (35.98, 35.98, and 35.59): model/system name, resolved score, date, harness, and checked/reporter status match. The source lists 22 configurations; all 22 appear exactly once in the rows, with no extras. Source release-date fields are null and the model-version tags are dated no later than 2025, so no row is for a model released in the preceding month. No row corrections or additions/removals were needed.
