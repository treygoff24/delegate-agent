# model-bench data format

Every lane writes the same shapes, so combining the sweep is concatenation.
All files are UTF-8 JSON Lines: one JSON object per line, no trailing commas,
no comments.

## Layout

```
examples/model-bench/
  manifest.jsonl              # one line per benchmark (the scout writes it)
  models.json                 # canonical model-id alias table (coordinator owns)
  data/<benchmark_id>/rows.jsonl   # measurements, one line per score
  data/<benchmark_id>/NOTES.md     # the lane's written research note
  raw/<benchmark_id>/...           # source snapshots; gitignored, never published
  reports/<date>.md                # analysis output
```

A collection lane writes only inside `data/<its benchmark_id>/` and
`raw/<its benchmark_id>/`.

## manifest.jsonl

| field | type | meaning |
| --- | --- | --- |
| `benchmark_id` | string | lowercase slug, `[a-z0-9-]+`, stable across refreshes |
| `name` | string | display name |
| `owner` | string | organization or people who run it |
| `url` | string | leaderboard or home page |
| `data_url` | string or null | raw data (GitHub repo, CSV, JSON endpoint) when one exists |
| `categories` | array of string | from the category list below |
| `reports_cost` | bool | publishes dollar cost per task or per run |
| `reports_time` | bool | publishes wall-clock or latency per task |
| `reports_effort` | bool | distinguishes reasoning-effort or thinking-budget settings |
| `reporter` | string | `independent`, `vendor`, or `community` (crowd votes) |
| `last_updated` | string or null | `YYYY-MM-DD` of the latest leaderboard update seen |
| `update_cadence` | string or null | e.g. `continuous`, `monthly`, `per-release`, `stale` |
| `license` | string or null | license or terms for the data, verbatim or summarized |
| `access_notes` | string or null | JS-rendered, bot-walled, login, API, etc. |
| `priority` | int | 1 = must collect, 2 = should, 3 = nice to have |
| `notes` | string or null | anything else a collector needs |
| `primary` | object or "mean", optional | the headline metric for reports: `{"subset": ..., "metric": ...}`, or `"mean"` to average every quality metric; omitted means a headline-named metric, then the one covering the most models |
| `superseded_by` | string, optional | benchmark id that now carries the same data; reports skip this benchmark |

## Categories

`agentic-coding`, `coding`, `ui-web`, `knowledge-work`, `legal`, `finance`,
`reasoning`, `math`, `science`, `tool-use`, `long-context`, `instruction-following`,
`writing`, `multimodal`, `speed-cost` (throughput, latency, price), `aggregate`
(an index combining other benchmarks), `human-preference` (arena-style votes),
`factuality` (hallucination rates), `safety` (refusal calibration, honesty under pressure),
`usage` (market share or popularity; context only, never scored as quality).

## rows.jsonl

One line per (model configuration, metric) the source reports.

| field | type | meaning |
| --- | --- | --- |
| `benchmark_id` | string | matches the manifest |
| `benchmark_version` | string or null | version, split, or release the score belongs to |
| `subset` | string or null | e.g. `verified`, `hard`, `overall` |
| `category` | string | one category from the list above |
| `model_raw` | string | the model name exactly as the source writes it, including any effort suffix |
| `model_id` | string | best-guess canonical id, `vendor/model` lowercase, e.g. `openai/gpt-6-sol`, `anthropic/claude-opus-5-5`, `deepseek/deepseek-v4.1-flash`; `unknown/<slug>` when unsure |
| `effort` | string or null | reasoning effort or thinking budget as the source states it; null when not stated |
| `harness` | string or null | agent scaffold or evaluation harness, when stated |
| `metric` | string | e.g. `pass@1`, `accuracy`, `resolved_rate`, `elo`, `win_rate`, `index_score`, `tokens_per_second`, `price_usd_per_mtok_input` |
| `value` | number | the score |
| `unit` | string | `%`, `score`, `elo`, `usd`, `s`, `tok/s`, ... |
| `higher_is_better` | bool | direction of `value` |
| `cost_usd` | number or null | dollar cost the source pairs with this score |
| `cost_basis` | string or null | what `cost_usd` covers: `per_task`, `per_run`, `per_mtok_blended`, ... |
| `time_s` | number or null | seconds the source pairs with this score |
| `time_basis` | string or null | `per_task`, `per_run`, `ttft`, ... |
| `ci` | number or null | half-width of the confidence interval or the stated error, same unit as `value` |
| `n` | int or null | number of tasks or votes behind the score |
| `measured_on` | string or null | `YYYY-MM-DD` the score was produced or posted |
| `reporter` | string | `independent`, `vendor`, or `community` for this row |
| `source_url` | string | the page or file the number came from |
| `retrieved_at` | string | ISO 8601 UTC timestamp of retrieval |
| `license` | string or null | redistribution terms, when known |
| `notes` | string or null | caveats, e.g. "vendor-reported, not reproduced" |

Rules:

- Copy numbers exactly as published; convert only the unit (e.g. 0.712 → 71.2
  when the unit is `%`) and say so in `notes`.
- Keep a score and its paired cost/time on one row, so a Pareto analysis reads
  one line per configuration.
- A model evaluated at several efforts gets one row per effort.
- Never invent a value. A missing field is null.
