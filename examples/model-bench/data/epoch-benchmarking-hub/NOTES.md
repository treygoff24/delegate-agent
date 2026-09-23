# Epoch AI Benchmarking Hub — collection notes

Added by coverage audit 2026-09-22.

## What this is

Epoch AI (https://epoch.ai/benchmarks) runs its own model evaluations on a
handful of benchmarks (using `inspect_ai`) and separately compiles/aggregates
results that other labs and evaluators publish, into one downloadable bundle:
`https://epoch.ai/data/benchmark_data.zip` (one CSV per benchmark, plus
`benchmark_metadata.csv`, `model_metadata.csv`, and an `epoch_capabilities_index/`
subfolder). This lane treats the whole hub as a single `benchmark_id`
(`epoch-benchmarking-hub`) with one `subset` per underlying benchmark, since
that is how the source packages it (one manifest line's worth of provenance,
many CSVs).

**Priority 1 target (FrontierMath) is covered**: all five FrontierMath CSVs in
the zip (`frontiermath.csv`, `frontiermath_tiers_1_3_v2.csv`,
`frontiermath_tier_4.csv`, `frontiermath_tier_4_v2.csv`,
`frontiermath_erdos.csv`) are collected in full, including v1→v2 supersession
relationships from `benchmark_metadata.csv`.

## How obtained

- Downloaded `https://epoch.ai/data/benchmark_data.zip` directly with `curl -A
  'Mozilla/5.0'` (HTTP 200, ~2.3 MB) — no JS rendering or bot-wall involved,
  it's a plain static zip link.
- Extracted to `raw/epoch-benchmarking-hub/extracted/` (kept in full — this is
  the raw capture used for verification; gitignored per repo convention).
- Parsed with a Python script (`build_epoch_rows.py`, run from the scratchpad;
  not checked into this repo) that reads each source CSV, applies a per-file
  column-mapping config, and writes `rows.jsonl`. The script is mechanical and
  reproducible from the raw CSVs alone.

## Own-run vs. external (Epoch's own flag, not ours)

The zip itself distinguishes two families by filename convention and column
schema:

- **Epoch own-run** (13 files, no `_external` suffix, all share the identical
  schema `Model version, mean_score, Best score (across scorers), Release
  date, Organization, Country, Training compute (FLOP), Training compute
  notes, stderr, Log viewer, Logs, Started at, id`): the 5 FrontierMath
  variants, `gpqa_diamond.csv`, `swe_bench_verified.csv`,
  `otis_mock_aime_2024_2025.csv`, `simpleqa_verified.csv`,
  `chess_puzzles.csv`, `ebr_bench.csv`, `mirrorcode.csv`,
  `mystery_game_puzzles.csv`. These have an eval-run timestamp (`Started at`,
  from an `inspect_ai` log) and use `Best score (across scorers)` as the
  canonical metric per `benchmark_metadata.csv`. `reporter` set to
  `independent` with a per-row note "Epoch own-run (inspect_ai) evaluation,
  not vendor-reported."
- **External / compiled** (`*_external.csv`, 40 files collected here, plus
  `epoch_capabilities_index/eci_scores.csv`): Epoch pulls these from other
  evaluators' own leaderboards/sites (e.g. ARC Prize, Scale AI, Cursor,
  FutureSearch, Surge AI, METR, LiveBench-family sites, GitHub repos). Each
  has its own idiosyncratic column schema; score column, units, cost/time
  columns, effort/harness columns, and eval-date columns were mapped
  per-file (see per-row `notes` for caveats). `reporter` is `independent` for
  all of these except `webdev_arena_external.csv` (`reporter: community`,
  since it is a crowd-vote Elo arena).
  - `hle_external.csv` is an edge case: it uses the `_external` naming
    pattern but has no `Source` column at all, so its provenance (Epoch's own
    run vs. a compiled third-party number) could not be confirmed from the
    CSV alone. Flagged low-confidence in its per-row notes.

`Epoch Capabilities Index (ECI)` (from `epoch_capabilities_index/eci_scores.csv`)
is a distinct third thing: Epoch's own composite ability index across all the
above benchmarks (IRT-style, not a 0-100% score), category `aggregate`. Its
companion `edi_scores.csv` is a per-*benchmark* difficulty index (no model
dimension) and does not fit `rows.jsonl`'s per-model-row schema, so it was
**not** collected — noted here instead.

## Effort / reasoning parsing

Most `Model version` values in this dataset carry a reasoning-effort suffix
(`_max`, `_xhigh`, `_high`, `_medium`, `_low`, `_minimal`, `_none`, `_unknown`,
`_promax`, or a thinking-token-budget suffix like `_16K`). These were stripped
from the base model slug and mapped into `effort` (lowercased; `_unknown` →
`effort: null`, base model kept). A handful of external files publish an
explicit effort column instead (`Reasoning effort`, `Reasoning level`,
`Reasoning`) — those were used directly when present, overriding the
suffix-derived guess. `model_id` = `<vendor>/<base-model-slug>`, vendor
guessed from each row's own `Organization` column (first org when
comma-separated, e.g. co-authored papers).

## Categories chosen

Most subsets mapped cleanly onto SCHEMA.md's category list. A few are
genuine judgment calls, called out per-row in `notes` with "low confidence":
`CL-Bench` / `CL-Bench Life` (undocumented benchmark name/methodology, no
Source URL in the CSV — categorized `reasoning`/`knowledge-work` from the
sub-score names alone), `RLI` (no documentation at all beyond a bare `Score`
column — categorized `reasoning`), `EBR-Bench` and `MirrorCode` (Epoch
own-run, but full name expansion isn't published anywhere in the zip).

## Duplicates with existing manifest.jsonl entries

Flagging per the task brief — these subsets cover the same underlying
benchmark as an id already in `manifest.jsonl`, just via Epoch's own run or
Epoch's compiled numbers rather than the existing source:

| This subset | Existing manifest id | Note |
| --- | --- | --- |
| `GPQA Diamond` (own-run) | `gpqa` | Same GPQA Diamond dataset; this is Epoch's own inspect_ai run vs. the existing entry's huggingface-sourced numbers. |
| `SWE-bench Verified (Epoch run)` | `swe-bench-verified` | Existing manifest entry is marked stale/legacy; this is a fresher, independently-run number on the same dataset. |
| `FrontierSWE v2` | `frontier-swe` | Same GitHub project (Proximal-Labs/frontier-swe-v2), same benchmark. |
| `OSWorld 2.0 (Epoch-compiled)` | `osworld-2` | Same OSWorld 2.0 dataset/leaderboard. (`OSWorld (v1)` in this collection is a *different*, older benchmark version — not a duplicate.) |
| `SciCode (Epoch-compiled)` | `scicode` | Same SciCode benchmark, compiled via Artificial Analysis rather than the existing GitHub-sourced entry. |
| `Humanity's Last Exam (Epoch-compiled)` | `hle` | Same HLE dataset; see provenance caveat above. |
| `WebDev Arena` | `webdev-arena` | Same lmarena.ai/arena.ai WebDev Arena leaderboard. |
| `METR Time Horizons` | `metr-time-horizon` | Same METR task-time-horizon methodology/leaderboard. |

Related but **not** flagged as exact duplicates (different projects/versions,
worth a human glance): `Terminal-Bench` here vs. manifest's `terminalworld`
and `tua-bench` (different benchmark projects); `APEX Agents` here vs.
manifest's `apex-1`/`apex-swe` (same benchmark family, unclear if identical
dataset revision).

## Pre-2026 rows excluded

Per source file, rows dated before 2026 (by `Started at` for own-run files,
by the file's own eval-date column when present, otherwise `Release date`)
were **excluded** from `rows.jsonl` — only benchmarks/files with at least one
2026 row were collected at all, per the task's scope. Totals across all 54
collected files: **4,748 total rows examined, 2,756 were 2026-dated, 1,941
were pre-2026 and excluded** (2,755 rows actually written — one 2026-dated
`os_world_external.csv` row had a blank score and was dropped). The two files
with the largest pre-2026 exclusions were `gpqa_diamond.csv` (141 of 313 rows
pre-2026) and `terminalbench_external.csv` (155 of 204 rows pre-2026).
`frontiermath.csv` and `frontiermath_tier_4.csv` (the superseded v1
FrontierMath files) are mostly pre-2026 (73/101 and 43/72 respectively) but
were kept in full since the current-vs-superseded pairing is itself
informative.

Files in the zip that were **excluded entirely** (zero 2026-dated rows, all
model coverage is 2024-2025 or earlier): `adversarial_nli_external.csv`,
`aider_polyglot_external.csv`, `arc_ai2_external.csv`, `bbh_external.csv`,
`bool_q_external.csv`, `cad_eval_external.csv`,
`common_sense_qa_2_external.csv`, `gdpval_external.csv`,
`gsm8k_external.csv`, `hella_swag_external.csv`, `lambada_external.csv`,
`lech_mazur_writing_external.csv`, `live_bench_external.csv`,
`math_level_5.csv` (own-run schema but stale), `mindcube_external.csv`,
`mmlu_external.csv`, `open_book_qa_external.csv`, `piqa_external.csv`,
`science_qa_external.csv`, `spatialviz_bench_external.csv`,
`superglue_external.csv`, `the_agent_company_external.csv`,
`trivia_qa_external.csv`, `video_mme_external.csv`, `vpct_external.csv`,
`wino_grande_external.csv`, `geobench_external.csv` (0 2026 rows).
`edi_scores.csv` was excluded because it's benchmark-level, not model-level
(see above).

## Metric/unit conventions used

- Values reported on a 0-1 scale (`benchmark_metadata.csv` `scale: 1.0`, or
  visually confirmed 0-1 in the file) were multiplied by 100 into `%`, noted
  per-row ("Value converted from 0-1 fraction to %").
- Values already on a 0-100 scale (`scale: 0.01` in `benchmark_metadata.csv`,
  e.g. `os_world_external.csv`, or visually confirmed, e.g.
  `forecastbench_external.csv`) were left as-is.
- A few benchmarks report a genuinely non-percentage metric and were kept in
  their native unit: `ALE-Bench`/`AlgoTune` (`unit: score`, contest
  score / speedup multiplier), `BTF3` (`unit: score`, pooled Brier/RPS,
  **lower is better**), `Vending-Bench 2` (`unit: usd`, simulated ending net
  worth), `WebDev Arena` (`unit: elo`), `METR Time Horizons` (`unit: min`),
  `Epoch Capabilities Index` (`unit: score`, IRT-style composite, not a
  percentage).
- `ci` is always in the same unit as `value` (half-width of a published CI
  when both bounds are given, or the standalone stderr/SE column when that's
  what the source publishes) — several files (`enigma_eval_external.csv`,
  `btf3_external.csv`) required rescaling the raw CI bounds by the same
  factor as the value to keep units consistent; this was caught and fixed
  during verification (see below).
- `measured_on` is the actual evaluation date when the source publishes one
  (own-run files: `Started at`; external files with `Date of evaluation` /
  `Evaluation date` / `Run date` / `Graded at` / `Last updated`-style
  columns). For external files with **no** eval-date column, `measured_on`
  is `null` — using the model's `Release date` there would misrepresent when
  the *evaluation* happened, not just when the model shipped.

## License

CC BY 4.0 per `raw/epoch-benchmarking-hub/extracted/README.md`:
"Epoch AI's data is free to use, distribute, and reproduce provided the
source and authors are credited under the Creative Commons Attribution
license." Citation: `Epoch AI, 'Capabilities & Benchmarking'. Published
online at epoch.ai.`

## What was not captured

- Secondary/companion metric columns on multi-metric files (e.g. AlgoTune's
  correctness-vs-speed breakdown, GSO's OPT@10 and hack-adjusted variants,
  METR's separate 80%-time-horizon and completion-rate columns, ARC-AGI's
  cost-per-task as a standalone series, Fiction.liveBench's other context
  lengths) — only the source's designated canonical/headline column per
  benchmark was captured, per-row notes say which.
- `model_versions` (the ECI's own per-config detail column) and
  `edi_scores.csv` (benchmark-level difficulty index).
- `model_metadata.csv` was used only as a cross-check during model_id
  canonicalization, not copied into rows.jsonl.

## Verification

Spot-checked 19 rows (exceeds the required 10, includes the top 3 by the
headline metric for the priority-1 benchmark) by independently re-extracting
each value from the raw CSV with `grep`/`cut` (a separate path from the
generation script) and comparing:

1. FrontierMath tier 1-3 v2 top 3 by score: `gpt-6-astra_max` (93.6842%),
   `claude-fable-5-1_max` (90.1754%), `gpt-5.6-sol_max` (89.1228%) — all
   matched `frontiermath_tiers_1_3_v2.csv` exactly (value = raw × 100).
2. GPQA Diamond top row `gpt-6-astra_max` (95.7702%, ci 1.3697) — matched.
3. SWE-bench Verified (Epoch run) top row `claude-opus-4-7_max` (83.4711%,
   ci 1.6901) — matched.
4. OTIS Mock AIME top row `qwen3.8-max-0902_xhigh` (100.0%) — matched.
5. Chess Puzzles top row `gpt-6-astra_max` (72.0%, ci 4.5126) — matched.
6. WebDev Arena top row `gpt-6-astra_max` (1800.28 elo, ci 16.475, n=2281
   votes) — matched, including the CI computed from the 95% CI High/Low
   columns and `n` from `Votes`.
7. EnigmaEval `claude-fable-5_high` (39.28%, ci 2.8) — matched. **Caught and
   fixed a bug here**: the CI, computed from `CI Lower Bound`/`CI Upper
   Bound`, was initially left in raw 0-1 units (0.028) while `value` had
   already been converted to %, an inconsistent-units defect. Fixed to scale
   the CI the same way as the value; re-verified after the fix.
8. FrontierSWE v2 top row `gpt-6-astra_max` (65.5076%, cost $1029.65/task) —
   matched.
9. DeepSWE top row `gpt-6-astra_xhigh` (74.115%, cost $6.52/run, effort
   `xhigh` from the explicit `Reasoning effort` column) — matched.
10. GDP.pdf top row `gpt-5.6-sol_unknown` (30.7%) — matched; model_id
    correctly resolved to `openai/gpt-5.6-sol` after fix #2 below.
11. LMCA top row `claude-fable-5-1_unknown` (65.46%, ci 2.935, scale-100
    passthrough) — matched.
12. Vending-Bench 2 top row `gpt-6-astra_unknown` ($15,514.70) — matched.
    **Caught and fixed a second bug**: the effort-suffix stripper's
    known-token list was missing bare `"unknown"` (only `"prounknown"` was
    listed), so every `_unknown`-suffixed model across the *entire* dataset
    kept the literal `_unknown` string glued onto its `model_id`
    (`openai/gpt-6-astra_unknown` instead of `openai/gpt-6-astra`). Fixed
    and regenerated; unique `model_id` count dropped from 231 to 212 as
    duplicate slugs correctly merged.
13. ForecastBench top row `claude-sonnet-4-6_16K` (62.0%, ci 1.3, n=1751) —
    matched, including the token-budget effort suffix (`16k`) and the
    `scale: 100` passthrough (no double-conversion).
14. BTF3 `claude-opus-4-8_xhigh` (0.132 score, ci 0.007, lower-is-better) —
    matched after fixing the config's incorrect `scale: 1.0` (Brier-style
    scores are conventionally left 0-1, not converted to %; this benchmark
    was originally mis-scaled to 13.2 before the fix).
15. GSO top row `claude-opus-4-8_unknown` (47.06%, measured_on
    2026-07-12 from the `Evaluation date` column, harness `OpenHands`) —
    matched.
16. Terminal-Bench top `gpt-5.5_unknown`/harness `NexAU-AHE` (84.7191%, ci
    2.0892, measured_on 2026-04-23 from `Run date`) — matched, including
    correctly keeping separate rows per harness/agent scaffold for the same
    model (5 different agents evaluated `gpt-5.5` on this benchmark).
17. APEX Agents top row `claude-fable-5-1_unknown` (68.6%, ci 4.9) —
    matched; confirmed the source's own stderr column is published already
    in percentage points (not a 0-1 fraction), so it was correctly left
    unscaled per the `ci_is_percent_already` flag.
18. Epoch Capabilities Index top row `GPT-6 Astra` (166.6, ci 4.515,
    measured_on 2026-09-03) — matched against `eci_scores.csv`.

Both bugs found in verification (CI unit mismatch, `_unknown` suffix not
stripped) were fixed in the generation script and the full `rows.jsonl` was
regenerated from scratch before finalizing, so the fixes apply to every row
in the dataset, not just the spot-checked ones. A schema-completeness check
(`python3 -c "..."` reading every line) confirmed all 2,755 lines parse as
JSON with every SCHEMA.md field present (`null` where unknown) both before
and after the fixes.
