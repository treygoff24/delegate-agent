# SpeechMap collection notes

added by coverage audit 2026-09-22

## What it measures

SpeechMap.AI (https://speechmap.ai/, code/data at https://github.com/xlr8harder) puts the same 2,120 sensitive/controversial prompts (political argument, satire, religion, history, rights advocacy, etc.) to hundreds of models and classifies each response as **Complete** (fully answered), **Evasive** (hedged or redirected), **Denial** (refused), or **Error** (blocked by the provider before a response). It is a refusal/compliance-calibration signal, not a capability or safety-quality benchmark: a high complete-rate means the model answers contentious requests as asked, not that its answers are good, safe, or correct. Category assigned here is `instruction-following` per the coverage-audit brief's guidance, since it measures whether the model does what was asked rather than refuses.

## Captured release

- Source: the fully-rendered model table at https://speechmap.ai/models/, which lists all 399 tracked model releases with per-model release date and Complete/Evasive/Denial/Error percentages as HTML `data-*` attributes (`data-s-model`, `data-s-released`, `data-s-complete`, `data-s-evasive`, `data-s-denial`, `data-s-error`) — this is the site's own pre-computed aggregate, not a scrape of rendered visual bars.
- The site's homepage states results are current "as of 2026-09-17" for the lab leaderboard; the most recent model in the full model table has `released: 2026-09-16` (`stealth/union-alpha`), and several 2026 frontier releases are present (e.g. `openai/gpt-6-astra`, `deepseek/deepseek-v4.1-flash`, `deepseek/deepseek-v4.1-flash-reasoning`).
- Captured at: `2026-09-22T22:30:00Z` UTC.
- The project also publishes raw per-question response/judgment data at https://github.com/xlr8harder/speechmap-data (622+ per-theme-per-model `analysis/compliance_*.jsonl` files plus `model_catalog.jsonl`), which is far more granular than needed for a per-model aggregate row and was not used as the primary source; the site's own pre-aggregated model-table percentages were used instead since they are the leaderboard's own computed summary of that same underlying data.

## Collection method and row count

Fetched `https://speechmap.ai/models/` (static server-rendered HTML, no JS execution needed) and parsed all `<tr data-f="..." data-s-model="..." data-s-released="..." data-s-complete="..." data-s-evasive="..." data-s-denial="..." data-s-error="...">` rows with a regex — 399 rows, one per tracked model release. Each model gets up to 4 rows (`complete_rate`, `evasive_rate`, `denial_rate`, `error_rate`); rows with an empty percentage were skipped (only a small number of models have no measurable error rate contribution). 399 models × 4 metrics = up to 1,596 rows; the collected file has exactly 1,596 (no gaps found).

`model_raw` values are already vendor/model-slug form (e.g. `deepseek/deepseek-v4.1-flash`, `openai/gpt-6-astra`, `x-ai/grok-4-07-09`); `model_id` is the same string lowercased, which is already close to canonical form for this source.

## Metric definitions

- `complete_rate` (%, higher = more compliant/less refusing): share of the 2,120 prompts answered in full.
- `evasive_rate` (%): share hedged or redirected rather than directly answered or refused.
- `denial_rate` (%): share refused outright.
- `error_rate` (%): share blocked by the provider before the model could respond (safety filter upstream of the model, rate limiting, etc.).
- `n` is set to 2,120 for every row (the fixed prompt set size stated on the site); this is the same prompt count for every model, not a per-metric count.
- `measured_on` uses the model's stated release date on SpeechMap, which is the date SpeechMap associates with that model version, not necessarily the exact date the model was evaluated.

## Caveats

- Direction of `evasive_rate` and `denial_rate` is not itself normatively "worse" — SpeechMap explicitly frames this as measuring what models refuse, not whether refusing is right or wrong. `higher_is_better` is set `true` only for `complete_rate` (more speech = the site's own framing of the axis) and `false` for the other three, but this is a modeling choice by this collection, not an explicit SpeechMap value judgment; treat all four as descriptive breakdown shares that sum to ~100%.
- No cost, latency, or confidence-interval data is published per model.
- `reporter` is `independent`: SpeechMap is a third-party project (not vendor self-reported), run by the maintainer of https://github.com/xlr8harder.
- License: the site links to GitHub repos (`speechmap-data`, `speechmap-eval`, `speechmap-site`) and a Hugging Face collection but does not state an explicit license for the aggregate percentages themselves on the pages fetched; not verified further.
- The homepage separately reports a "Free Speech Index" per-lab leaderboard (time-weighted average across recent models) at `/labs/`, which was not collected as rows here since it is a lab-level index rather than a per-model, per-metric row; the per-model table captured here is the finer-grained, more broadly useful source for this schema.

## Raw captures

`raw/speechmap/index.html` (homepage), `raw/speechmap/models.html` (full 399-model table, primary source), `raw/speechmap/models_table.json` (parsed rows), `raw/speechmap/labs.html`, `raw/speechmap/resources.html`, `raw/speechmap/model_catalog.jsonl` (from the data GitHub repo, for reference), all under `examples/model-bench/raw/speechmap/`.

## Verification

Verified 2026-09-22 against the captured `models.html` table.

- Ran a Python schema check: all 1,596 lines parse as JSON and every line has all 24 SCHEMA.md fields present.
- Re-extracted 10 randomly sampled rows independently by re-parsing `raw/speechmap/models_table.json` by column index (a separate lookup path from the generation script) and comparing to the collected rows: all 10 matched exactly (`stealth/aurora-alpha` error_rate, `qwen/qwen3.7-max-reasoning` complete_rate, `allenai/olmo-3-7b-think` complete_rate, `google/gemini-2.0-flash-001` evasive_rate, `deepseek/deepseek-v4-pro-0813-reasoning` denial_rate, `deepseek/deepseek-v4-flash-0731` complete_rate, `mistralai/magistral-medium-2506-thinking` evasive_rate, `meituan/longcat-2.0-reasoning` complete_rate, `openai/gpt-5.2` complete_rate, `microsoft/mai-ds-r1-fp8` evasive_rate).
- Checked the top 3 models by `complete_rate` (`stealth/sherlock-dash-alpha` 100.0, `stealth/sherlock-think-alpha` 98.8, `x-ai/grok-4-07-09` 98.3) against the raw HTML table: exact match.
- No mismatches found; no corrections were needed.
