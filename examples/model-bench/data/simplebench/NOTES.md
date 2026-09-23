# simplebench — SimpleBench (simple-bench.com)

## What it measures

SimpleBench (https://simple-bench.com/) is a multiple-choice text benchmark
of 200+ questions covering spatio-temporal reasoning, social intelligence,
and "linguistic adversarial robustness" (trick questions designed so a
non-specialist human outperforms frontier LLMs). The headline metric on the
main leaderboard is "Score (AVG@5)" — accuracy averaged over repeated
samples per question — reported as a percentage. Grading is multiple
choice. A separate, much smaller "Open-Ended" leaderboard (3 entries)
scores a free-response variant of the same idea, judged differently from
the main MCQ leaderboard. Benchmark settings stated by the source:
temperature 0.7, top-p 0.95 (except the o1 series).

## How the data was captured

The page's static HTML only contains the intro text and Human
Baseline/Highest-Human-Score callouts; the leaderboard table itself is
rendered client-side by `static/js/index.js` from a plain, non-minified
data file: `https://simple-bench.com/static/js/leaderboard-data.js`. That
file defines three top-level JS values directly (no framework hydration
payload needed):

- `leaderboardData` — 103 entries (101 ranked models + "Highest Human
  Score*" and "Human Baseline*" callout rows), each `{rank, model, score,
  organization, dateAdded}`.
- `openEndedData` — 3 entries for the Open-Ended leaderboard.
- `modelMeta` — 100 entries keyed by model name, each giving OpenRouter (or
  proxy) list pricing (`input`/`output`, USD per 1M tokens), a `released`
  date, an `est` flag (true = delisted/experimental model priced from a
  proxy, not a live quote), and for most current entries `tout` (average
  completion/output tokens the model used over the SimpleBench run) and
  `cpr` (the resulting estimated dollar cost to run the model over the full
  benchmark — "cost per run").

Saved the raw HTML (`raw/simplebench/simplebench.html`) and the leaderboard
data file verbatim (`raw/simplebench/leaderboard-data.js`), then converted
the three JS values to JSON with Node (`eval` + `JSON.stringify`, since the
file is plain JS, not JSON) into
`raw/simplebench/leaderboard_full.json`.

## Confirms current data

The board is led by 2026 frontier models: Claude Fable 5.1 (86.6%, added
2026-09-03), GPT-6 Astra Pro (86.5%, added 2026-09-07), GPT-6 Astra (83.6%),
Gemini 3.8 Flash (82.4%), Claude Fable (81.9%), Muse Spark 1.3 (81.8%),
Claude Opus 5 (80.6%). Source is live and current.

## Field notes / caveats

- `value`/`metric`/`unit`: `accuracy`, the source's `score` percentage,
  `unit="%"`, `higher_is_better=true`.
- `subset`: `"multiple_choice"` for the main 103-row leaderboard,
  `"open_ended"` for the 3-row Open-Ended leaderboard (a different,
  free-response task format the source itself keeps as a separate table).
- `cost_usd`/`cost_basis`: populated from `modelMeta[model].cpr`
  ("cost per run" — the source's own estimate of the dollar cost to run
  that model through the full SimpleBench question set, computed from
  OpenRouter/list token pricing times observed average token usage, no
  caching accounted for). `cost_basis="per_run"` since this is a whole-
  benchmark cost, not a per-question figure. Null when the source has no
  `modelMeta` entry, or the entry lacks `cpr` (older/pre-token-tracking
  models such as GPT-3.5 Turbo, GPT-4 Turbo, and most pre-2026 models —
  the source's own comment says pricing there is "from list price or
  nearest-tier proxy," and several such entries simply have no `tout`/`cpr`
  fields at all).
- The raw `modelMeta` pricing (input/output $/Mtok, whether it's an
  estimated/proxy price, and the average completion-token count behind the
  cost estimate) is preserved verbatim in each row's `notes` field, along
  with the source's own numeric rank string (e.g. "30th") and organization
  name.
- `measured_on` = the source's `dateAdded` (when the model was added to the
  leaderboard), used as the closest available proxy for a measurement
  date since the source does not separately date-stamp when each score was
  produced. Null for the many older/legacy models that predate the site's
  practice of recording an add-date (`dateAdded: null` in the source for
  most models before 2025-10ish).
- One model, "Ox Alpha" (organization stated as "Unknown" by the source
  itself), has no `modelMeta` pricing entry at all — `cost_usd` is null for
  it, consistent with every other model missing from `modelMeta`.
- `model_id`: best-effort canonical vendor/model guesses. The two
  human-baseline rows ("Highest Human Score*", "Human Baseline*", both with
  an empty `organization` string in the source) are not models; they use
  `unknown/highest-human-score` and `unknown/human-baseline` respectively
  and are included because the source lists them directly in the same
  leaderboard table for comparison (same treatment as arc-agi's "Human
  Panel" row). "Muse Spark 1.2"/"1.3" and "Ox Alpha" have no clearly
  identifiable vendor from the page itself and use `unknown/<slug>`.
- License / redistribution terms: not stated on the page itself, though the
  page links to a "Public Dataset" and a "Code" repository (not fetched in
  this pass — out of scope for the leaderboard-scores collection task).
  Left `license: null` throughout.
- `n` (number of tasks/samples behind the score) is left null: the source
  states the headline metric is "AVG@5" (an average over repeated samples,
  likely 5 per question) but does not give an exact total question count or
  sample count per model on the leaderboard page itself.

## What could not be captured

- Per-question or per-category (spatio-temporal / social intelligence /
  adversarial robustness) score breakdowns — the leaderboard page reports
  only a single overall score per model. The source's technical report
  (linked as "Report") likely has this detail but was out of scope for this
  pass.
- Exact sample count `n` behind "AVG@5" (see above).
- Timeline tab data (a chart of score over time) was not separately
  extracted since it is a re-presentation of the same `leaderboardData`
  rows already captured, keyed by `dateAdded`.

## Verification

Validated all 106 rows parse as JSON and contain every SCHEMA.md field
(`python3` check — 0 missing fields, 0 null `value`s).

Spot-checked the top 5 by headline metric plus 6 additional rows (11 total,
exceeding the required minimum) by independently `grep`-ing the raw
`leaderboard-data.js` file for each model's exact table line and comparing
`score` and, where present, `modelMeta.cpr`, against `rows.jsonl` (separate
command from the row-generation script):

- Top 5: Highest Human Score* 95.4%, Claude Fable 5.1 86.6%/$6.25, GPT-6
  Astra Pro 86.5%/$8.29, Human Baseline* 83.7%, GPT-6 Astra 83.6%/$1.92 —
  all match.
- Additional: Kimi K3 (max) 60.7%/$15.41, GLM 5.3 66.2%/$4.13, MiniMax M3
  45.8%/$2.00, GPT-3.5 Turbo 8.0%/null (genuine missing `cpr` in source,
  not an extraction bug — confirmed by grep showing no `tout`/`cpr` keys on
  that `modelMeta` line), DeepSeek V4 Pro 50.9%/$1.99, Qwen 3.8 27B
  60.2%/$8.88 — all match exactly.

No mismatches found; no fixes were needed.

Added by coverage audit 2026-09-22.
