# METR Time Horizon data capture

- **Benchmark:** METR-Horizon-v1.1 (the current version shown on METR's page).
- **Data date:** 2026-05-08, the page's stated “LAST UPDATED” date. The latest model release date among the captured results is 2026-04-07.
- **Retrieved:** 2026-09-23T01:19:38Z.
- **Source:** [METR time horizons](https://metr.org/time-horizons/) and its linked [raw YAML](https://metr.org/assets/benchmark_results_1_1.yaml). A copy of each is in `raw/metr-time-horizon/`.

METR estimates the human-expert task duration at which an AI agent is predicted to succeed at a specified reliability. The page says the 50% and 80% horizons are fitted from results on over 100 diverse software tasks. The horizon describes task difficulty in human completion time; it is not the agent's wall-clock runtime. METR says the task distribution is primarily software engineering, machine learning, and cybersecurity, and notes that task durations can overestimate professional work because the contracted humans have less context.

The 26 YAML result configurations each contain three numeric metrics, so `rows.jsonl` has 78 rows: `average_score` (source fraction, retained without rescaling), `p50_horizon_length`, and `p80_horizon_length`. The YAML's `is_sota` flag is result metadata, not a numeric metric, and is not represented as a measurement row. Horizon values are copied as published in minutes; the YAML provides asymmetric lower and upper confidence bounds, which are recorded verbatim in each horizon row's `notes` because the shared schema has only one symmetric `ci` field. `measured_on` is the source `release_date`, not the evaluation date. Source scaffold alternatives are joined in `harness`; no cost, runtime, effort setting, sample count, or evaluation date was published in this file.

`model_raw` preserves each result key verbatim. `model_id` is a best-effort canonicalization; version suffixes and `inspect` in raw keys distinguish configurations, not reasoning-effort settings. The source has sparse model coverage: METR states it may not have measurements for some releases and does not promise a complete record of the most capable models. The page lists some recent models without measurements. METR warns that measurements above 16 hours are unreliable with the current task suite. The source does not discuss contamination for these results, so no contamination claim is made here.

The page footer says “© 2026 METR. All rights reserved.” No separate data redistribution license was stated on the page or in the downloaded YAML; see METR terms before redistribution. This is an independent METR evaluation, not a vendor-reported benchmark result.

Capture note: a direct GitHub API listing request returned HTTP 403, but the page and the page-linked raw YAML were retrieved successfully. The current webpage identifies the YAML endpoint as the raw data source, so this did not prevent collection.

## Verification

Checked all 78 JSONL rows against the captured YAML's numeric estimates and release dates; 0 mismatches. The live page and downloaded YAML were re-fetched on 2026-09-22; the YAML is byte-identical to the capture and the page still says it was last updated May 8, 2026. The 10 individual spot-checks below include the three highest-valued rows and seven additional configurations with the latest release dates; each matches the source.

- `claude_mythos_preview_early_inspect` / `p50_horizon_length`: 1044.780145 — match
- `claude_opus_4_6_inspect` / `p50_horizon_length`: 718.80683 — match
- `gemini_3_1_pro` / `p50_horizon_length`: 384.147435 — match
- `gpt_5_4` / `p50_horizon_length`: 341.735276 — match
- `gpt_5_3_codex` / `p50_horizon_length`: 349.530732 — match
- `gpt_5_2` / `p50_horizon_length`: 352.249302 — match
- `claude_opus_4_5_inspect` / `p50_horizon_length`: 292.994594 — match
- `gpt_5_1_codex_max_inspect` / `p50_horizon_length`: 223.714694 — match
- `gemini_3_pro` / `p50_horizon_length`: 224.325884 — match
- `gpt_5_2025_08_07_inspect` / `p50_horizon_length`: 203.012577 — match

All lines parse and include all 24 schema fields. The YAML lists 26 scored configurations, and rows cover the same 26 configurations with all three source metrics (78 rows). No scored configuration is dated within the month before 2026-09-22; the live page lists Claude Opus 4.7 and GPT-5.5 among recent models without published horizons. No row corrections or additions were needed.
