# LMArena Text Arena capture

## Measurement

Arena collects blind pairwise human preferences between model responses and reports an aggregate rating and rank. It fits a Bradley-Terry model and translates scores to an Elo-like scale. These are preference results, not direct measures of task success. The capture includes the current Hugging Face `latest` splits for `text`, `text_style_control`, and `text_factuality`; source category labels are retained in `subset` as `<config>/<category>`.

Arena's August 2026 leaderboard changelog says it computes displayed confidence intervals via a closed-form method based on the Central Limit Theorem for M-estimators. Its factuality article identifies the reported intervals for that board as 95%. For factuality, Arena describes a composite Bradley-Terry leaderboard that combines human preference and factuality labels, with a default factuality weight of 25%. See [Arena ranking method](https://arena.ai/blog/ranking-method), [leaderboard changelog](https://arena.ai/blog/leaderboard-changelog), and [Factuality in the Arena](https://arena.ai/blog/factuality-in-arena/).

## Data version and retrieval

- Dataset: [`lmarena-ai/leaderboard-dataset`](https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset), snapshot `d25aabda0010d1986a11988e5cb0a748053ce02b`.
- All captured rows have `leaderboard_publish_date` **2026-09-13**. The dataset API reported `lastModified` **2026-09-16T03:02:16Z**; that is the repository update time, not the leaderboard publication date.
- Retrieved **2026-09-23T01:25:27Z** (2026-09-22 21:25 EDT).
- Retrieved the official Arena leaderboard page, then used the Hugging Face dataset metadata and rows endpoint, which serves the complete `latest` splits in pages of at most 100 rows. Raw page HTML, dataset metadata/README, endpoint metadata, and every captured page are under `examples/model-bench/raw/lmarena/`.
- Arena's [dataset announcement](https://arena.ai/blog/arena-leaderboard-dataset) says `latest` contains the most recently published data for each selected arena. The dataset README declares `CC-BY-4.0`. The source's per-model license field is preserved in each row's `notes`; it is distinct from the dataset redistribution license.

## Rows and fields

`rows.jsonl` contains **24,794 rating rows**: `text` 10,606, `text_style_control` 10,606, and `text_factuality` 3,582. These counts are source model/category results, not unique models. The union contains **402 distinct source model names**. There are 29 source category labels; not every model appears in every category.

Each source result becomes one row with `metric: "elo"`, `value` copied from source `rating`, `unit: "elo"`, `n` copied from `vote_count`, and `measured_on` copied from `leaderboard_publish_date`. `ci` is the half-width calculated from the source's `rating_lower` and `rating_upper`. Arena describes these as confidence intervals; the factuality article explicitly identifies that board's intervals as 95%, while the dataset README does not specify an interval level for every config. The exact bounds, published rank, variance, and model license are retained in that row's `notes`. `model_raw` is the source `model_name`; `model_id` uses the source organization and a lowercase slug of the model name, with `unknown/` when no organization is given. The row `license` is the dataset-level `CC-BY-4.0` term.

The source publishes no paired dollar cost, elapsed time, or reasoning-effort setting for these results, so those fields are null. No separate rank rows are emitted: rank is preserved alongside the rating in `notes` because the shared schema represents measurements and the leaderboard's primary score is the rating.

## Caveats

- Crowd preference depends on Arena's prompt mix, response presentation, and voting population. Style-controlled, factuality, and unadjusted text ratings are distinct subsets and should not be treated as interchangeable or as task-success scores.
- The latest available publication date is nine days before capture; the dataset repository had been updated more recently, but its publication-date field remained 2026-09-13. Arena describes the leaderboard as continuously updated, so later results may now exist.
- The source pages and dataset description consulted do not provide a contamination audit or a model-by-model reproduction protocol. Public visibility of model identities and benchmark prompts can affect comparisons; this capture does not establish contamination.
- Model identifiers are best-effort canonical guesses derived from source organization/model display names. Blank organizations use `unknown/<slug>`.

## Verification

- Parsed all **24,794** lines in `rows.jsonl`; each has exactly the 24 fields in `SCHEMA.md`, with the declared types and valid category/reporter values.
- Compared every row's `(config, model name, category)` identity to the captured Hugging Face `latest` source, and compared its rating, vote count, publication date, and confidence-interval half-width. All **24,794** identities and values matched; there were no missing or extra source rows.
- The source captures contain **402** distinct model names; `rows.jsonl` contains the same **402**.
- Explicit spot checks (all matched the captured source):

  | JSONL line | subset | model | value (Elo) | result |
  | ---: | --- | --- | ---: | --- |
  | 403 | text/chinese | claude-fable-5.1-max | 1605.2547491006774 | match |
  | 11009 | text_style_control/chinese | claude-fable-5.1-max | 1592.1702145915303 | match |
  | 404 | text/chinese | claude-opus-5-max | 1585.2963797038803 | match |
  | 3000 | text/french | gemma-1.1-7b-it | 1065.056434303816 | match |
  | 8000 | text/korean | gpt-5.2-high | 1369.6177693918048 | match |
  | 12000 | text_style_control/creative_writing | hunyuan-turbo-0110 | 1302.0437691374773 | match |
  | 16000 | text_style_control/industry_life_and_physical_and_social_science | kimi-k2-thinking-turbo | 1440.7410654980813 | match |
  | 20000 | text_style_control/non_english | gpt-5.1-high | 1443.1117713285776 | match |
  | 24000 | text_factuality/longer_query | gpt-5.4 | 1487.432058303913 | match |
  | 24794 | text_factuality/spanish | minimax-m2.5 | 1377.0189475740035 | match |

- The three highest values are lines 403, 11009, and 404 above. The first is Claude Fable 5.1 Max; Anthropic announced Claude Fable 5.1 on 2026-09-01, within the month before this verification ([Anthropic announcement](https://www.anthropic.com/news)).
- No row edits were needed: **0 corrected, 0 added, 0 deleted**.
