# Arena Search leaderboard capture

## Measurement

Arena (formerly LMArena) collects blind pairwise human preferences between
tool-use/research agent responses that ground answers in live web search, and
reports an aggregate rating and rank using the same Bradley-Terry
methodology as the Text and Vision arenas (see Arena's [ranking
method](https://arena.ai/blog/ranking-method) and [leaderboard
changelog](https://arena.ai/blog/leaderboard-changelog)). These are crowd
preference results over search-grounded agent behavior, not direct measures
of factual accuracy or task success, though Arena also publishes a
factuality-weighted composite board for this arena (see below).

This capture reuses the approach documented in the existing `lmarena` lane
(`examples/model-bench/data/lmarena/NOTES.md`, read-only reference) and the
same Hugging Face dataset used for `arena-vision` in this audit: the same
`lmarena-ai/leaderboard-dataset` publishes `search`, `search_style_control`,
and `search_factuality` configs, fetched via the
`datasets-server.huggingface.co` rows endpoint.

## Data version and retrieval

- Verified the public leaderboard page is live: `https://arena.ai/leaderboard/search`
  returned HTTP 200 (saved as `raw/arena-search/arena-search-page.html`). The
  benchmark id assigned for this lane read "verify" for the URL; this URL is
  confirmed correct — the page renders a "Search Arena" leaderboard with
  Overall/Style Control/Factuality tabs matching the three configs captured
  below.
- Dataset: [`lmarena-ai/leaderboard-dataset`](https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset),
  snapshot `d25aabda0010d1986a11988e5cb0a748053ce02b` (same snapshot as the
  `lmarena` and `arena-vision` captures; confirmed via a fresh
  `hf-dataset-api.json` fetch in this lane's raw directory).
- Configs captured: `search` (34 rows), `search_style_control` (34 rows),
  `search_factuality` (32 rows) — all from the `latest` split (small enough
  to fetch in a single page of 100 each; no pagination needed). The dataset
  also publishes `full` splits (history) for each, not captured here.
- Retrieved **2026-09-23T01:33:20Z** (fetch of the vision configs and search
  configs ran back-to-back in this session).
- Raw pages saved under `examples/model-bench/raw/arena-search/splits/<config>/latest/offset-00000.json`,
  with `raw/arena-search/splits/capture-index.json` recording endpoint URLs
  and row counts per config. Also saved: the rendered leaderboard page HTML
  and a fresh Hugging Face dataset API metadata snapshot.

## Rows and fields

`rows.jsonl` contains **100 rating rows**: `search/overall` 34,
`search_style_control/overall` 34, `search_factuality/overall` 32. This
arena has a single `overall` category (no per-topic category split, unlike
Text or Vision). The union contains **34 distinct source model names** across
7 organizations (anthropic, openai, google, xai, perplexity, baidu, diffbot),
covering current frontier search-augmented configurations (e.g.
`gpt-5.6-sol-xhigh`, `claude-sonnet-5-search`, `gemini-3.1-pro-grounding`,
`grok-4.20-beta1`).

Each source result becomes one row with `metric: "elo"`, `value` copied from
source `rating`, `unit: "elo"`, `n` copied from `vote_count`, and
`measured_on` copied from `leaderboard_publish_date` — all 100 rows carry
**2026-08-24**, meaning this arena's leaderboard had not refreshed as
recently as the Text/Vision arenas as of retrieval. `ci` is the half-width
calculated from the source's `rating_lower` and `rating_upper`, the same
convention used in `lmarena` and `arena-vision`. The exact bounds, published
rank, variance, and per-model license are retained in each row's `notes`.
`model_raw` is the source `model_name`; `model_id` uses the source
`organization` and a lowercase slug of the model name. `subset` is
`<config>/overall`. The row `license` is the dataset-level `CC-BY-4.0` term.

Arena's factuality methodology (documented for the Text arena at [Factuality
in the Arena](https://arena.ai/blog/factuality-in-arena/)) describes a
composite board combining human preference and factuality labels with a
default factuality weight of 25%; the same composite approach appears to
apply to `search_factuality`, though the consulted pages describe this
explicitly for text, not search — flagged as unconfirmed for this arena.

The source publishes no paired dollar cost, elapsed time, or
reasoning-effort setting for these results, so those fields are null.

## Caveats

- Crowd preference depends on Arena's prompt mix, response presentation, and
  voting population; style-controlled and factuality-weighted ratings are
  distinct subsets and should not be treated as interchangeable with the raw
  `search` board.
- All captured rows share a single `leaderboard_publish_date` of
  2026-08-24, roughly a month older than the Text/Vision arenas' most recent
  publish date (2026-09-13) as of this same retrieval session — this board
  updates less frequently or had not refreshed at capture time.
- Small n per board (32-34 models) relative to Text/Vision; some models list
  identical vote counts across `search` and `search_style_control` boards
  (e.g. `claude-sonnet-4-6-search`), consistent with style-control being a
  re-weighting of the same vote data rather than a separate poll — this
  matches the design described for the Text arena's style-controlled board.
- No contamination audit or per-model reproduction protocol is provided by
  the source pages consulted.
- Model identifiers are best-effort canonical guesses derived from source
  organization/model display names; `unknown/<slug>` would be used for blank
  organizations, though none occurred in this capture.

## Verification

Spot-checked 10 rows by independently re-reading the raw captured JSON pages
under `raw/arena-search/splits/` with a separate Python snippet (not the
build script), matching on `model_name` and comparing `rating` and
`vote_count`:

- Top 3 by rating in `search/overall`: `gpt-5.6-sol-xhigh` (1257.26,
  n=29663), `claude-opus-4-6-search` (1253.42, n=134699), `gpt-5.5-search`
  (1242.05, n=89873) — all matched exactly against raw.
- 7 additional randomly sampled rows across the three configs
  (`search_style_control` grok-4.5, `search` gpt-5.2-search-non-reasoning,
  `search_style_control` grok-4.3, `search_factuality` grok-4.20-beta1,
  `search` claude-sonnet-4-6-search, `search` gemini-3-pro-grounding,
  `search_factuality` gpt-5.6-sol-xhigh) — all matched exactly (rating and
  vote_count agreed with raw).

No mismatches found; no corrections were needed.

Added by coverage audit 2026-09-22.
