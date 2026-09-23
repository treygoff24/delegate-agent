# eqbench-3 — EQ-Bench 3 (legacy leaderboard)

## Important: the main page URL now serves EQ-Bench 4, not v3

The task brief's URL for this id, `https://eqbench.com/` (main page), **no
longer serves EQ-Bench 3**. As of this capture, `https://eqbench.com/`
returns a page titled "EQ-Bench 4 Leaderboard" (`<title>EQ-Bench 4
Leaderboard</title>`, indigo/amber theme, `#eqbench4-leaderboard` table,
data from `eqbench4/eqbench4_data.js`). EQ-Bench 4 was **not** collected
here since it is a different benchmark id than the one assigned.

EQ-Bench 3 survives as a separate, explicitly-labeled "legacy" page at
`https://eqbench.com/eqbench3.html` (linked from the main page's "📚Legacy
Leaderboards" dropdown). **This legacy page is not stale**: its data file
(`eqbench3.js`) carries the same `Last-Modified: 2026-09-07` timestamp as
every other current page on the site, and its leaderboard includes current
2026 frontier models (`claude-fable-5`, `claude-opus-4-8`, `claude-opus-4-7`,
`claude-sonnet-4-6`, `gpt-5.5`, `gpt-5.4`, `zai-org/GLM-5.2`, `GLM-5.1`,
`deepseek-ai/DeepSeek-V4-Pro`, `moonshotai/Kimi-K2.6`, `gemini-3-pro-preview`,
`gemini-3.1-pro-preview`). So per the brief's "skip if stale" instruction,
this was **collected, not skipped** — but the lead should be aware the
identity mismatch exists (assigned URL != actual current EQ-Bench-3 URL) and
may want a separate `eqbench-4` id for the new main-page benchmark in a
future pass.

## What it measures

An LLM-judged emotional-intelligence benchmark, judged by **Claude Opus
4.6**, via 45 multi-turn role-play scenarios (most 3 turns) plus several
analytical tasks (identify and analyze compelling aspects of a provided
roleplay transcript). Every assistant reply begins with two introspection
blocks ("I'm thinking & feeling" / "They're thinking & feeling") exposing
reasoning and theory-of-mind understanding.

Two judging passes, both captured here:
- **Rubric pass** (`rubric_0_100`): the judge scores the full transcript +
  self-debrief against several criteria; only six are aggregated into the
  0-100 rubric score (Demonstrated Empathy, Pragmatic EI, Depth of Insight,
  Social Dexterity, Emotional Reasoning, Message Tailoring — the last two
  are not separately broken out as their own leaderboard/CSV columns).
  Rubric-only is about 3x cheaper than a full run but less discriminative
  at the top ability range. **Caveat:** about.html's long-version text
  states "The rubric score is not shown on the leaderboard; it's available
  when running the benchmark" — this appears to be stale documentation,
  since the captured `eqbench3.js` payload for this legacy page does
  publish a `rubric_0_100` value per model. Recorded faithfully as
  published; the discrepancy is flagged rather than resolved by guessing.
- **Pairwise pass / Elo** (`elo_norm` -> metric `elo`): transcripts are
  compared head-to-head on eight criteria via a modified Trueskill/Glicko
  solver, using "+" to "+++++" win-margin scoring. Elo is anchored:
  o3 = 1500, llama-3.2-1b = 200 (both the short-version and long-version
  text on about.html state the same anchors; the captured CSV's minimum
  row, `meta-llama/llama-3.2-1b-instruct`, is exactly 200.0, consistent).

Eleven additional per-model columns are style/personality sub-scores from
the rubric pass's full criteria list (`humanlike, safe, assertive,
social_iq, warm, analytical, insightful, empathy, compliant, moral,
pragmatic`). Four of these (`social_iq`, `insightful`, `empathy`,
`pragmatic`) map to the criteria about.html says are aggregated into
`rubric_0_100` (Social Dexterity, Depth of Insight, Demonstrated Empathy,
Pragmatic EI respectively) and are recorded `higher_is_better: true`. The
other seven (`humanlike, safe, assertive, warm, analytical, compliant,
moral`) map to about.html's "visibility only" style/tendency criteria
(Humanlike, Safety Conscious, Challenging≈assertive, Warmth, Analytical,
Compliant, Moralising) that do **not** count toward the final score;
recorded `higher_is_better: false` as a schema-completeness convention
since the source does not claim a direction for these (see per-row
`notes`).

`ci_low_norm`/`ci_high_norm` (the source's own published confidence-interval
bounds on the Elo score) were converted into the schema's `ci` field on the
`elo` row as `(ci_high_norm - ci_low_norm) / 2` — a half-width in the same
`elo` unit as `value`, matching SCHEMA.md's definition of `ci`.

Bias mitigation per about.html: length truncation for pairwise judging,
position-bias control (both directions averaged). Cost: ~$10-15/full run
(rubric + pairwise) on OpenRouter, ~$1.5/iteration for rubric-only.

## Version / date captured

- Benchmark version: **eqbench3**, the legacy leaderboard at
  `eqbench3.html` (site's dataset variable: `leaderboardDataEQBench3`).
- Data date: **2026-09-07**, from the HTTP `Last-Modified` header on
  `eqbench3.html` and `eqbench3.js` (no in-page "last updated" date is
  printed).
- Retrieved: 2026-09-23T01:34:28Z.

## How it was captured

Data is embedded as a CSV-formatted JS template literal
(`leaderboardDataEQBench3`) inside `eqbench3.js`, fetched with
`curl -A 'Mozilla/5.0'` (no JS rendering needed). Raw files under
`raw/eqbench-3/`: `page.html` (the current eqbench.com main page, EQ-Bench 4
— kept only to document the identity mismatch above), `eqbench3_legacy.html`
(the actual `eqbench3.html` legacy page), `eqbench3.js`, and the extracted
`leaderboard_eqbench3.csv` (79 model rows + header, lines 9-87 of the JS
file).

79 models x 13 metrics (elo, rubric_score, 11 sub-criteria) = **1027 rows**
in `rows.jsonl`.

## Model-name handling

Same `*` ("new model") / `!` (NSFW) leaderboard-display flags as the other
EQ-Bench pages; `model_raw` keeps the exact source string including any
leading flag character, flag meaning recorded per-row in `notes` when
present, `model_id` derived from the flag-stripped name.

Most model names overlap with the creative-writing-v3 / longform datasets
and reused the same canonical mapping. 17 names unique to this dataset were
mapped fresh; one (`HiveLabsAI/hivemind-32b-preview`) has no confidently
identifiable vendor and is recorded `unknown/hivemind-32b-preview`.

## Reporter / license

- `reporter`: `independent` (EQ-Bench project).
- License: not stated on the page. No explicit data redistribution license
  found; left `null`.

## What could not be captured

- Per-scenario transcripts, self-debrief text, and the judge's per-criteria
  "+" to "+++++" win-margin detail are not in the leaderboard data payload.
- `n` (number of scenarios/matchups behind each score) is not published per
  model; `null`.
- `harness`: not applicable/stated; `null`.
- Per-model cost/time: about.html gives only an aggregate benchmark-wide
  cost estimate ("$10-15/full run"), not broken out per model; left `null`
  rather than attach an aggregate figure to individual rows.

## Verification

Spot-checked 9 distinct (model, metric) pairs by independently re-extracting
values from `raw/eqbench-3/leaderboard_eqbench3.csv` with a fresh
`csv.DictReader` pass (separate from the row-building script):

- Top 3 by `elo`: `*claude-fable-5` (2049.7), `*claude-opus-4-8` (2029.8),
  `claude-opus-4-7` (1884.3) — both the elo value and the derived `ci`
  half-width (computed independently from `ci_low_norm`/`ci_high_norm`)
  **MATCH** for all three.
- 6 additional rows spread across the 1027-line output (indices 0, 100,
  300, 500, 700, 900, 1026), covering `demonstrated_empathy`,
  `rubric_score`, `warmth`, `moralising`, `safety_conscious`,
  `pragmatic_ei` across models `*zai-org/GLM-5.2`, `o3`, `claude-sonnet-4`,
  `deepseek-ai/DeepSeek-V3-0324`, `qwen/qwen-2.5-72b-instruct`,
  `meta-llama/llama-3.2-1b-instruct` — all **MATCH**, no mismatches, no
  fixes needed.
- Validated with a small Python script that all 1027 lines parse as JSON
  with every SCHEMA.md field present. All passed.

Added by coverage audit 2026-09-22.
