# eqbench-creative-writing — EQ-Bench Creative Writing v3

## What it measures

EQ-Bench's Creative Writing v3 leaderboard scores models on 32 writing
prompts run for 3 iterations each (96 items) at temp 0.7 / min_p 0.1. Two
scores are produced:

- **Rubric score** (`creative_writing_score` in the raw data): each output is
  graded in isolation against a detailed rubric by the judge model
  (`anthropic/claude-sonnet-4` via OpenRouter). Absolute scale, less
  discriminative at the top end. Column header on the site reads "Rubric
  Score".
- **Elo score**: pairwise matchups between neighboring models on the same
  prompt, judged by Claude Sonnet 4.6 (updated from Sonnet-4 per a 2026-03-01
  site note) on 9 named criteria (character authenticity, originality,
  writing quality, coherence, instruction following, world/atmosphere,
  avoiding cliché, avoiding flowery/show-offy vocab, avoiding gratuitous
  metaphor). Win margins ("+" to "+++++") feed a modified Glicko/Trueskill
  solver. Elo is anchored: DeepSeek-R1 = 1500, and (per the CSV)
  `meta-llama/llama-3.2-1b-instruct` = 200 (about.html text says "ministral-3b"
  for the low anchor, but the captured CSV's actual minimum-score row is
  llama-3.2-1b-instruct at exactly 200.0 — the anchor model may have changed
  since that prose was written; flagging the discrepancy rather than
  guessing).

Length-bias mitigation: pairwise judging truncates outputs to 4000 chars;
rubric judging uses full-length outputs.

Additional descriptive columns published alongside the two scores:
- `avg_length`: average output length in characters (not explicitly labeled
  with a unit on the page; inferred from magnitude, e.g. 3400–14929).
- `vocab_complexity`: proportion of words with 3+ syllables (per
  about.html's "Update 2025-02-25" note), expressed as a value in roughly the
  20–67 range, treated on the site as a metric that can indicate
  judge-flattering "vocab-maxxing" rather than genuine quality.
- `slop_score`: frequency of overused LLM phrases ("GPT-isms"), higher =
  worse (more slop). Computed against a slop word list derived from a large
  story dataset plus community-compiled slop-phrase lists.
- `repetition_score`: sum of top-N word/bigram/trigram frequencies in the
  model's outputs, higher = more repetitive (worse), per the page's own
  metrics-explanation section.

## Version / date captured

- Benchmark version: **creative-writing-v3** (the only version this page
  serves; legacy v2 lives in a separate "Creative Writing (Legacy v2)"
  section of about.html and was NOT collected here since v3 supersedes it).
- Data date: **2026-09-07**, taken from the HTTP `Last-Modified` header on
  both `creative_writing.html` and `creative_writing.js` (no explicit
  "last updated" date is printed on the page itself).
- Retrieved: 2026-09-23T01:34:28Z.

## How it was captured

The leaderboard table on `https://eqbench.com/creative_writing.html` is
populated client-side by `creative_writing.js`, which embeds the full
dataset as a CSV-formatted JS template literal
(`leaderboardDataCreativeWritingV3`, columns: `model_name, elo_score,
creative_writing_score, avg_length, vocab_complexity, slop_score,
repetition_score`). Fetched via `curl -A 'Mozilla/5.0'`, no JS rendering
needed. Raw files saved under `raw/eqbench-creative-writing/`:
`page.html`, `about.html`, `creative_writing.js`, `creative_writing_chartdata.js`,
`creative_writing_chartdata_style.js`, and the extracted
`leaderboard_creative_writing_v3.csv` (lines 5–138 of `creative_writing.js`,
134 lines incl. header, 133 model rows).

133 model configurations × 6 metrics (elo, rubric_score, avg_length,
vocab_complexity, slop_score, repetition_score) = **798 rows** in
`rows.jsonl`, matching the row-count rule in `briefs/collect.md`.

Confirmed current 2026 frontier coverage: `gpt-6-astra`, `claude-fable-5-1`,
`claude-opus-5`, `kimi-k3`, `GLM-5.3`, `gpt-5.6-sol/terra/luna`, `gemini-3.8-flash`,
`grok-4.5`/`grok-4.20-beta`, `DeepSeek-V4-Pro/Flash`, `Qwen3.8-*`, `muse-spark-1.x`
— the source is live and current, not stale.

## Model-name handling

The site prefixes a leading `*` on rows it flags as "new model" and `!` on
rows flagged NSFW-tuned (both stripped for display, per
`creative_writing.js`'s `isNewModel`/`isNsfwModel` logic around line 5841).
`model_raw` in this dataset preserves the **exact source string including
any leading `*`/`!`**, since that is literally what the CSV field contains;
the flag meaning is recorded per-row in `notes` when present, and `model_id`
is derived from the flag-stripped name.

`model_id` is a best-effort canonical guess. Several models are OpenRouter
"stealth"/cloaked codenames with no confirmed public vendor attribution at
capture time (`hunter-alpha`, `ox-alpha`, `optimus-alpha`, `quasar-alpha`,
`openrouter/horizon-alpha`, `openrouter/horizon-beta`, `openrouter/pony-alpha`,
`openrouter/sherlock-dash-alpha`, `openrouter/cypher-alpha`) — these are
recorded as `unknown/<slug>`. Several are unaffiliated community fine-tunes
(`ifable/gemma-2-Ifable-9B`, `sam-paech/Darkest-muse-v1`,
`allura-org/Gemma-3-Glitter-12B`, `ToastyPigeon/Gemma-3-Starshine-12B`) —
also `unknown/<slug>`. `muse-spark-1.1/1.2/1.3` and
`meta-models/Muse-Glimmer-30B` (a "Muse Spark"/"Muse" family named in the
task brief as a 2026 frontier model) have no confirmed vendor org either and
are recorded `unknown/<slug>` rather than guessed. `zai-org/GLM-*` and
`THUDM/GLM-4-32B-0414` are both mapped to vendor `zai` (Z.ai, formerly
Zhipu AI, the GLM developer) since THUDM is GLM's original open-weights
release org under the same lab. `Qwen/*` rows are mapped to vendor `qwen`.

## Direction / unit caveats

- `avg_length` and `vocab_complexity` are descriptive, not scored for
  direction by the source — `higher_is_better: false` is recorded as an
  arbitrary convention for schema completeness, not a claim the source
  makes; see each row's `notes`.
- `slop_score` and `repetition_score` are explicitly lower-is-better per the
  source's own metric descriptions.
- Numbers are copied exactly as published; no unit conversion was needed
  (all columns are the source's native numeric scale already).

## Reporter / license

- `reporter`: `independent` (EQ-Bench is Sam Paech's independent benchmark
  project, not vendor- or crowd-run).
- License / redistribution terms: not stated on the leaderboard page or
  about.html for the v3 dataset specifically. The benchmark's evaluation
  code is open source at `https://github.com/EQ-bench/creative-writing-bench`
  (linked from about.html); the site's own GitHub org also has
  `EQ-bench/EQ-bench-site` (source of this page, last pushed 2026-09-07,
  matching the data date) which was not scraped separately since the JS
  payload already contains the full dataset. No explicit data license
  (e.g. CC-BY) was found; left `null` in rows rather than guessed.

## What could not be captured

- Per-item / per-iteration transcripts and confidence intervals are not on
  the leaderboard page (only the aggregate values per model); `ci` and `n`
  are `null` for every row.
- `harness` (agent/eval scaffold) is not applicable/stated for this
  benchmark; `null`.
- `cost_usd` / `time_s`: about.html states an approximate aggregate cost of
  "$10-15 per full run" and "~$10 in API fees" for the whole benchmark,
  but this is not broken out per model, so it was not attached to
  individual rows (would misrepresent a source that pairs cost with score
  per SCHEMA.md's rule).

## Verification

Spot-checked 12 (model, metric) pairs by independently re-reading the value
from `raw/eqbench-creative-writing/leaderboard_creative_writing_v3.csv` via
a fresh `csv.DictReader` pass (separate from the script that built
`rows.jsonl`) and comparing to the corresponding line in `rows.jsonl`:

- Top 3 by Elo: `*gpt-6-astra` (2163.9), `*claude-fable-5-1` (2152.7),
  `claude-opus-5` (2120.6) — all MATCH.
- 9 additional rows spread across the file (indices 0, 50, 100, 200, 300,
  400, 500, 600, 797 of the 798-line output), covering `muse-spark-1.1`,
  `*muse-spark-1.2`, `moonshotai/Kimi-K2-Instruct`,
  `deepseek-ai/DeepSeek-V4-Pro`, `gemini-2.5-pro-preview-06-05`,
  `openrouter/sherlock-dash-alpha`, `gemini-2.0-flash-001`,
  `meta-llama/Llama-4-Maverick-17B-128E-Instruct`,
  `meta-llama/llama-3.2-1b-instruct`, across all 6 metric types
  (elo, avg_length, slop_score, repetition_score) — all **MATCH**, no
  mismatches found, no fixes needed.
- Separately validated with a small Python script that every one of the 798
  lines parses as JSON and has all 24 SCHEMA.md fields present (`null` where
  unknown). All 798 passed.

Added by coverage audit 2026-09-22.
