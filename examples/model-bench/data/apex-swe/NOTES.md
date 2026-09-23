# APEX-SWE results captured 2026-09-22

APEX-SWE measures agent performance on economically relevant software engineering work. Mercor describes a 200-case held-out benchmark split evenly between Integration (end-to-end work across services) and Observability (debugging and remediation using production-style telemetry). Results are published with the Terminus-2 agent harness; historical entries may use Inspect. Mercor says rubric grading combines expert-authored criteria and tests, with an LM judge in its FAQ.

## Capture and coverage

Captured the server-rendered leaderboard HTML for the overall page and both domain pages directly from Mercor on 2026-09-22 (EDT). The HTML embeds the complete leaderboard data in Next.js page props. The capture files are `raw/apex-swe/leaderboard.html`, `integration.html`, and `observability.html`; the snapshots include public sample-task material as served. The overall view contains 58 model entries and 67 model/harness score records. Integration contains 58 model entries and 68 score records; Observability contains 58 model entries and 68 score records. Each line in `rows.jsonl` is one published Pass@1 result by subset and harness: 203 lines total. Multiple harness rows are retained where the page has results for both harnesses. Model IDs are normalized best guesses; `model_raw` preserves the visible source model name and effort suffix. Effort values preserve the source data label (for example `xhigh`).

The page was live and current at retrieval. Mercor does not state a benchmark release/version or per-score evaluation date in the captured leaderboard, so `benchmark_version` and `measured_on` are null. The domain pages describe averages over 100 held-out prompts; the overall benchmark has 200 cases. `n` records these stated task counts, not the number of successful completions. Published `±` values are recorded as `ci` in percentage points. They are omitted when the source provides none. No per-run cost or time was published.

## Metric

`pass@1` is the percentage of tasks completed successfully on one attempt (the leaderboard’s exact FAQ definition describes a task as successful when it receives 100% of rubric points). Values and confidence/error margins are copied as published, in percent. Higher is better. `subset` distinguishes overall, integration, and observability. `harness` is copied from the leaderboard’s harness key. Older rows sometimes only report Inspect, while newer rows report Terminus-2; do not treat cross-harness differences as a controlled comparison.

## Caveats and terms

Mercor is the benchmark owner and source of the scores; the registry classifies this benchmark as independent, but the leaderboard does not document independent replication. The full held-out tasks remain private, according to Mercor, which reduces direct training access but does not establish absence of contamination. The open Hugging Face release contains 50 tasks (25 per domain) and is licensed CC-BY-4.0; its card does not state redistribution terms for the leaderboard results themselves. The harness GitHub repository has an MIT license, which applies to that repository, not automatically to scores or task data. Row `license` is therefore null. The raw directory also contains the retrieved dataset card, harness license, and methodology page. Mercor does not publish evaluation dates for individual results, so older entries may be stale despite appearing on the continuously updated page.

No model result shown in the page data was omitted. The embedded page data contains larger sample-task/trajectory payloads; these were retained in the raw HTML but are not represented as measurements.

## Verification

Verified on 2026-09-22 against the captured Mercor source data embedded in
`raw/apex-swe/leaderboard.html`, `integration.html`, and `observability.html`.
All 204 JSONL lines parse and contain exactly the 24 fields in `SCHEMA.md`'s
row schema. Spot-checked 22 rows: every row for the seven models whose source
release dates fall in the last 30 days (21 rows), plus the third-highest score
row (Gemini 3.7 Flash High, Integration, 67%). The two highest rows (Fable 5.1
Max at 68.1% and Muse Spark 1.3 Xhigh at 67.8%) are included in that recent
model sample. All 22 values match their captured source records; no sampled
mismatches were found.

Coverage reconciliation found all 58 source-listed model configurations in
each subset, with no extra configurations. Each capture contains 68 score
records per subset, matching the 68 rows in `rows.jsonl` for overall,
integration, and observability (204 total). The earlier capture summary above
under-counted the overall records and total rows by one: it counted `pass-1`
but omitted the source's `pass@1`-labeled Opus 4.7 High overall record. No row
values or membership needed correction; zero rows were changed.
