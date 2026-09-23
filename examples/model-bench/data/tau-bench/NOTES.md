# τ-bench / τ²-bench collection

Captured 374 score rows for 66 submission configurations listed in the current repository manifest. The manifest separates active text submissions, active voice submissions, and legacy submissions; all three lists are included.

## What it measures

τ-bench evaluates multi-turn conversational agents that use tools to resolve tasks in customer-service environments. The current repository and site have evolved to τ³ naming and add banking and voice evaluations. Rows keep the source domain in `subset` and the source benchmark release in `benchmark_version`.

## Source and capture

- Leaderboard: https://taubench.com/leaderboard/
- Raw source: https://github.com/sierra-research/tau2-bench/tree/main/web/leaderboard/public/submissions
- Capture: GitHub `main` at commit `b7ea9074c1cba482b30687fecdb5c8425fd6f619` (2026-09-17T21:57:12Z); manifest and schema plus every listed `submission.json` are archived under `raw/tau-bench/`.
- Retrieved: 2026-09-23T01:16:56Z. Latest submission date among captured records: 2026-09-09.
- `rows.jsonl` has 374 rows across 66 listed submission configurations. Row count differs from a fixed four-rows-per-model count because each configuration reports results for differing domain subsets and pass metrics; the exported row exists for every numeric pass_1 through pass_4 value. Counts by domain: airline=86, banking_knowledge=116, retail=90, telecom=82.

## Metrics and fields

`pass_1` through `pass_4` are the source's pass-k success rates, copied as percentages without rounding. `cost`, when present alongside a domain result, is copied onto each corresponding score row as `cost_usd` with `cost_basis=per_task`, following the leaderboard's average-cost-per-task presentation. The source publishes no wall-clock time, confidence intervals, or task counts in these result objects, so those fields are null. The reasoning effort is copied when stated; harness is recorded as `tau2-bench`, and retrieval settings, user simulator, submission type, and methodology notes are retained in row notes.

Version values are copied from `methodology.tau2_bench_version`; absent versions remain null. This matters because the repository notes that banking scores changed after grading updates, and older versions are not necessarily comparable. Dates are copied from evaluation date, falling back to submission date.

## Provenance and caveats

The benchmark is maintained by Sierra Research. Individual leaderboard submissions are submitted by Sierra, model vendors, or other organizations; row-level `reporter` reflects that distinction (`independent`, `vendor`, or `community`). The source records verification flags for some submissions, but does not expose confidence bounds or independently reproduce every external submission. Scores should therefore be interpreted with their submission notes and versions. Training contamination is not measured by these result files; no general contamination claim is made. Legacy entries may be stale and are included because the source manifest still publishes them. Voice configurations are included because they are language-model benchmark submissions in the current manifest, but should not be compared directly with text rows.

The repository's `LICENSE` is MIT (archived as `raw/tau-bench/repository-LICENSE`). The submission schema does not state a separate license or redistribution grant for submitted result data, so row-level `license` is null.

## Limitations

The website defaults to τ³-Banking but also publishes τ² and τ³-Voice tabs. The raw manifest is the authoritative captured inventory; the rendered page's default table is not the full inventory. No cost or time value was invented where absent.

## Verification

- Parsed all 374 lines in `rows.jsonl`; every row has all 24 schema fields with the expected types.
- Compared the score value against the archived `submission.json` for 10 rows. All matched: the three highest rows (Qwen3-Max-Thinking / telecom / `pass_1`; Claude-Sonnet-4.5 / telecom / `pass_1`; Gemini 3.0 Pro / telecom / `pass_1`), all seven rows for the two `gpt-live-1` submissions whose source model release date is 2026-09-10, and no additional rows were needed to reach 10.
- Coverage matches: all 66 source manifest submission configurations have rows and no unlisted configuration appears; the 59 distinct source model names match the 59 distinct `model_raw` labels.
- No row changes were needed; no mismatches were found in the checked sample.
