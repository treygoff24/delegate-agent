# eqbench-longform — EQ-Bench Longform Creative Writing

## What it measures

`https://eqbench.com/creative_writing_longform.html` (URL verified live, HTTP
200) scores models on writing an 8-chapter (8×1000-word-turn) short
story/novella from a minimal prompt, with an initial brainstorm/plan and a
plan revision step first. Source code:
`https://github.com/EQ-bench/longform-writing-bench` (per the page's own
methodology card).

Judge: Claude Sonnet 4.6 (upgraded from Sonnet 4 per the page's "v1.11
2026-02-19" changelog entry shown in the methodology card; Sonnet 4 itself
had replaced Sonnet 3.7 in the "v1.1 2025-08-08" update). Generation
settings: temp 0.7, min_p 0.1, typically via OpenRouter.

Score composition: `Final Score = (sum of other rubric criteria) + (5 x
Forced_Poetry/Metaphor^1.7)`, where Forced Poetry/Metaphor is scaled 0-1 and
deliberately over-weighted because the judge under-detects incoherent
metaphor on its own. 14 rubric dimensions total: 8 positive ("Nuanced
Characters", "Emotionally Engaging", "Compelling Plot", "Coherent",
"Well-earned Lightness or Darkness", "Characters Consistent with Profile",
"Followed Chapter Plan", "Faithful to Writing Prompt") and 6 negative
("Weak Dialogue", "Tell-Don't-Show", "Unsurprising or Uncreative",
"Amateurish", "Purple Prose", "Forced Poetry or Metaphor"). A "Long Context
Degradation Penalty" automatically scales down chapter scores when a model
falls into excessive single-sentence-paragraph structure late in the piece
(detected structurally, since judges often miss it).

Columns captured, one row per model per metric:
- `overall_score_100` -> `overall_score` (0-100 scale; the leaderboard's
  "Score" column; average of the 8 chapter averages plus the final scored
  complete piece).
- `avg_chapter_length` -> `avg_chapter_length` (chars; page states "doesn't
  contribute to the score").
- `vocab_complexity` -> `vocab_complexity`: this specific page does not
  redefine the metric in its own methodology card; treated as the same
  "proportion of words with 3+ syllables" measure defined on the sibling
  creative-writing-v3 about.html page (both are EQ-Bench writing-eval
  pages sharing the same slop/vocab pipeline). Flagged as inferred, not
  independently confirmed on this page.
- `slop_score` -> `slop_score`: page states explicitly "the lower, the
  better."
- `repetition_score` -> `repetition_score`: "measures how strongly a model
  repeats n-grams across its outputs" (direction not stated explicitly on
  this page; recorded lower-is-better by the same convention EQ-Bench uses
  on its other writing leaderboards).
- `chapter1_avg` ... `chapter8_avg` -> 8 rows per model with metric
  `chapter_avg` and a `notes` field identifying which of the 8 chapters
  (these are the per-chapter rubric averages behind the page's degradation
  sparkline).
- `final_judgement_avg` -> `final_judgement_avg` (average score of the
  complete, final scored piece; combined with the 8 chapter averages into
  `overall_score_100`).

The leaderboard table itself only displays Length / Slop / Repetition /
Degradation / Score / Samples columns; the per-chapter breakdown and
final-judgement average are only present in the underlying data payload,
not rendered as leaderboard columns — captured here anyway since
SCHEMA.md asks for "every model, one row per metric" the source publishes,
and the JS payload does publish them.

## Version / date captured

- Dataset label in the JS source: `leaderboardDataLongformV3`. The page's
  own methodology card cites benchmark version "v1.11 (2026-02-19)" as the
  latest changelog entry (judge upgrade to Sonnet 4.6); recorded
  `benchmark_version` as a combination of both since the page does not
  give one single canonical version string.
- Data date: **2026-09-07**, from the HTTP `Last-Modified` header on both
  `creative_writing_longform.html` and `creative_writing_longform.js` (no
  in-page "last updated" date is printed).
- Retrieved: 2026-09-23T01:34:28Z.

## How it was captured

Data is embedded as a CSV-formatted JS template literal
(`leaderboardDataLongformV3`) inside `creative_writing_longform.js`, fetched
with `curl -A 'Mozilla/5.0'` (no JS rendering required). Raw files under
`raw/eqbench-longform/`: `page.html`, `creative_writing_longform.js`, and
the extracted `leaderboard_longform.csv` (134 model rows + header, lines
4-138 of the JS file, with the trailing backtick of the template literal
stripped).

134 models x 14 metrics = **1876 rows** in `rows.jsonl`.

Confirmed current 2026 frontier coverage in this dataset: `claude-sonnet-4-6`,
`claude-opus-4-6`, `claude-opus-5`, `claude-fable-5`/`claude-fable-5-1`,
`gpt-6-astra`, `GLM-5.3`, `gpt-5.6-sol`, `muse-spark-1.2/1.3`,
`gemini-3.8-flash`/`gemini-3.5-flash`, `Qwen3.5-*`/`Qwen3-Max-2025-09-24` —
live and current, not stale.

## Model-name handling

Same `*` ("new model") / `!` (NSFW) leaderboard-display flags as the sibling
creative-writing-v3 page; `model_raw` keeps the exact source string
including any leading flag character, flag meaning recorded per-row in
`notes` when present, `model_id` derived from the flag-stripped name.

One row (`gpt-5-2025-08-07-high-reasoning-high-reasoning`) has a doubled
`-high-reasoning-high-reasoning` suffix in the raw source string (kept
verbatim in `model_raw` — this is the source's own naming, not a
transcription error). `effort` for that row is recorded as `"high-reasoning"`
and `model_id` is derived from the base name
(`openai/gpt-5-2025-08-07`). No other row in this dataset carries an
effort-setting suffix.

49 model names in this dataset do not appear on the creative-writing-v3
leaderboard and were mapped fresh; the following remain genuinely uncertain
and are recorded `unknown/<slug>`: OpenRouter stealth/cloaked codenames
(`horizon-alpha`, `openrouter/bert-nebulon-alpha`, `sonoma-sky-alpha`,
`openrouter/aurora-alpha`, `healer-alpha`, `ftpo-exp501`), unaffiliated
community fine-tunes (`sam-paech/gemma-3-27b-it-antislop`), and a few names
with no confidently identifiable vendor (`nova-2-lite-v1`,
`minimax-m2-her`). Everything else is mapped to the vendor implied by its
path prefix or well-known naming convention (e.g. `tngtech/` -> `tng`,
`inclusionai/` -> `inclusionai`, `meituan-longcat/` -> `meituan`,
`arcee-ai/` -> `arcee`, `solar-pro-3` -> `upstage` per Upstage's known Solar
Pro family, `mimo-v2-pro` -> `xiaomi` by naming continuity with the
`XiaomiMiMo/` rows in the same dataset).

## Direction / unit caveats

- `avg_chapter_length` and `vocab_complexity` are descriptive per the
  source; `higher_is_better: false` is a schema-completeness convention,
  not a source claim (see per-row notes).
- `slop_score`: explicitly lower-is-better per this page's own text.
- `repetition_score`: lower-is-better by convention (not explicitly stated
  on this specific page, unlike slop_score).
- `overall_score`, the 8 `chapter_avg` rows, and `final_judgement_avg` are
  all higher-is-better (rubric scores).
- No unit conversions were needed; all values copied exactly as published.

## Reporter / license

- `reporter`: `independent` (same EQ-Bench project as creative-writing-v3).
- License: not stated on the page. Evaluation code is open source at
  `https://github.com/EQ-bench/longform-writing-bench`. No explicit data
  redistribution license found; left `null`.

## What could not be captured

- Per-chapter transcripts, the "Samples" column's linked sample outputs,
  and the degradation sparkline's underlying detection threshold values are
  not in the leaderboard data payload; not capturable as rows.
- `ci` and `n` are `null` for all rows (no confidence intervals or item
  counts published per model).
- `harness`: not stated; `null`.
- No cost/time figures are published per model on this page (unlike the
  general "~$10-15/run" text on the sibling creative-writing-v3 about.html,
  this page's methodology card gives no cost estimate at all); `cost_usd`/
  `time_s` left `null`.

## Verification

Spot-checked 11 distinct (model, metric) pairs, re-extracting each value
independently from `raw/eqbench-longform/leaderboard_longform.csv` with a
fresh `csv.DictReader` pass (separate from the row-building script) rather
than trusting its own output:

- Top 3 by `overall_score`: `claude-opus-5` (86.3), `*claude-fable-5-1`
  (85.3), `claude-fable-5` (83.0) — verified against a `sort -t, -k2 -nr`
  of the raw CSV independently of the Python check; all **MATCH**.
- 8 additional rows spread across the 1876-line output (indices 0, 200,
  400, 600, 900, 1200, 1500, 1875), covering metrics `overall_score`,
  `repetition_score`, `chapter_avg` (multiple chapter numbers, parsed back
  out of each row's own `notes` field to pick the right raw CSV column),
  `vocab_complexity`, and `final_judgement_avg`, across models
  `*claude-sonnet-4-6`, `moonshotai/Kimi-K2-Instruct`,
  `tngtech/DeepSeek-R1T-Chimera`, `XiaomiMiMo/MiMo-V2-Flash`, `ftpo-exp501`,
  `solar-pro-3`, `deepseek-ai/DeepSeek-V4-Pro` — all **MATCH**, no
  mismatches, no fixes needed.
- Validated with a small Python script that all 1876 lines parse as JSON
  with every SCHEMA.md field present. All passed.

Added by coverage audit 2026-09-22.
