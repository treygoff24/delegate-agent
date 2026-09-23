# Arena Vision leaderboard capture

## Measurement

Arena (formerly LMArena) collects blind pairwise human preferences between
model responses to image/vision prompts and reports an aggregate rating and
rank. It fits a Bradley-Terry model and translates scores to an Elo-like
scale, exactly as described for the Text arena (see Arena's [ranking
method](https://arena.ai/blog/ranking-method) and [leaderboard
changelog](https://arena.ai/blog/leaderboard-changelog)). These are crowd
preference results, not direct measures of task success.

This capture reuses the approach documented in the existing `lmarena` lane
(`examples/model-bench/data/lmarena/NOTES.md`, read-only reference): the
public leaderboard page is JS-rendered, but the same underlying dataset is
published on Hugging Face and exposes per-arena configs including `vision`
and `vision_style_control`, fetched via the `datasets-server.huggingface.co`
rows endpoint in pages of 100.

## Data version and retrieval

- Verified the public leaderboard page is live: `https://arena.ai/leaderboard/vision`
  returned HTTP 200 (saved as `raw/arena-vision/arena-vision-page.html`).
- Dataset: [`lmarena-ai/leaderboard-dataset`](https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset),
  snapshot `d25aabda0010d1986a11988e5cb0a748053ce02b` (same snapshot the
  `lmarena` lane captured; confirmed via a fresh `hf-dataset-api.json` fetch
  in this lane's raw directory, `sha` and `lastModified` match).
- Configs captured: `vision` (`latest` split, 1,029 rows) and
  `vision_style_control` (`latest` split, 1,029 rows). The dataset also
  publishes `full` splits for both configs (history), not captured here — only
  `latest` is in scope for a leaderboard snapshot.
- Retrieved **2026-09-23T01:33:20Z**.
- Raw pages saved under `examples/model-bench/raw/arena-vision/splits/<config>/latest/offset-*.json`,
  with `raw/arena-vision/splits/capture-index.json` recording endpoint URLs and
  row counts per config. Also saved: the rendered leaderboard page HTML and a
  fresh Hugging Face dataset API metadata snapshot.

## Rows and fields

`rows.jsonl` contains **2,058 rating rows**: `vision` 1,029 and
`vision_style_control` 1,029. There are 11 source category labels
(`overall`, `captioning`, `chinese`, `creative_writing`,
`creative_writing_vision`, `diagram`, `english`, `entity_recognition`,
`homework`, `humor`, `ocr`); not every model appears in every category. The
union contains **152 distinct source model names** across 22 organizations
(one row per model per category per config).

Each source result becomes one row with `metric: "elo"`, `value` copied from
source `rating`, `unit: "elo"`, `n` copied from `vote_count`, and
`measured_on` copied from `leaderboard_publish_date` (1,990 rows dated
2026-09-13; 68 rows — all in less-trafficked categories — dated 2026-01-09,
meaning those category boards had not refreshed since January). `ci` is the
half-width calculated from the source's `rating_lower` and `rating_upper`,
the same convention the `lmarena` lane used; Arena's methodology pages do not
state a confidence level specific to the vision board. The exact bounds,
published rank, variance, and per-model license are retained in each row's
`notes`. `model_raw` is the source `model_name`; `model_id` uses the source
`organization` and a lowercase slug of the model name, `unknown/<slug>` when
organization is blank. `subset` is `<config>/<category>`. The row `license`
is the dataset-level `CC-BY-4.0` term (per the dataset README, same as the
`lmarena` lane).

The source publishes no paired dollar cost, elapsed time, or reasoning-effort
setting for these results, so those fields are null. No separate rank rows
are emitted; rank is preserved in `notes` alongside the rating.

## Caveats

- Crowd preference depends on Arena's prompt mix, response/image
  presentation, and voting population; style-controlled and unadjusted
  ratings are distinct subsets and should not be treated as interchangeable.
- A minority of category boards (68 of 2,058 rows) carry a stale
  `leaderboard_publish_date` of 2026-01-09 rather than the 2026-09-13 date
  most rows carry; treat comparisons that mix those categories with caution.
- No contamination audit or per-model reproduction protocol is provided by
  the source pages consulted.
- Model identifiers are best-effort canonical guesses derived from source
  organization/model display names (e.g. `anthropic/claude-fable-5.1-max`,
  `openai/gpt-5.4-nano-high`); blank organizations use `unknown/<slug>`.

## Verification

Spot-checked 10 rows by independently re-reading the raw captured JSON pages
under `raw/arena-vision/splits/` with a separate Python snippet (not the
build script), matching on `model_name` + `category` and comparing `rating`,
`vote_count`, and (for the top-3 check) computed `ci`:

- Top 3 by rating in `vision/overall`: `claude-fable-5` (1325.86, n=11304),
  `claude-fable-5.1-max` (1322.34, n=2701), `claude-opus-5-high` (1321.21,
  n=11599) — all matched exactly against raw.
- 7 additional randomly sampled rows across configs/categories
  (`vision/diagram` gemini-3-pro, `vision/overall` glm-4.5v,
  `vision_style_control/overall` mistral-medium-2508, `vision/ocr`
  grok-4-0709, `vision/humor` gpt-5.4-nano-high, `vision/english`
  kimi-k2.5-thinking, `vision/creative_writing_vision` o4-mini-2025-04-16) —
  all matched exactly (rating, vote_count, rank all agreed with raw).

No mismatches found; no corrections were needed.

Added by coverage audit 2026-09-22.
