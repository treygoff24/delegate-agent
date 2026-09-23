# OpenRouter usage rankings + pricing catalog capture

## Measurement

OpenRouter is a model marketplace/router; its public rankings page
(`https://openrouter.ai/rankings`) reports **token throughput by model**
across all traffic routed through the platform — a revealed-preference,
market-usage signal (which models developers actually spend tokens on),
not a benchmark score or a crowd vote. Per this lane's assignment, the
`category` field on the usage rows is set to `human-preference` (the closest
fit in SCHEMA.md's category list) even though this is usage/spend data
rather than pairwise preference votes; this note is that explanation.
Pricing rows (see below) are tagged `speed-cost`, which is the correct fit
(price is one of that category's stated dimensions).

## Data version and retrieval

- Verified the public page is live: `https://openrouter.ai/rankings` returned
  HTTP 200 (saved as `raw/openrouter-rankings/rankings-page.html`, a
  Next.js app that streams data as an RSC payload rather than embedding a
  `__NEXT_DATA__` blob).
- The rankings page's default "week" view embeds only the top ~20 models in
  its RSC payload (extracted to `raw/openrouter-rankings/rankings-models-week.json`
  for reference/comparison). Reverse-engineering the page's own API calls
  found the underlying JSON endpoint the page itself fetches from:
  `GET https://openrouter.ai/api/frontend/v1/rankings/models?view=week`,
  which returns the **full** ranked list (579 model/variant rows, not just
  the top 20 shown on the page) with numerically identical figures for the
  models that appear in both. Saved as
  `raw/openrouter-rankings/rankings-models-week-api.json`.
- Model pricing: fetched the public documented endpoint
  `https://openrouter.ai/api/v1/models` (454 models). Saved as
  `raw/openrouter-rankings/models-api.json`.
- Retrieved **2026-09-23T01:40:56Z**.
- Also saved for reference: the raw rankings page HTML, its extracted RSC
  text payload (`payload5.txt`), and a probe confirming `/rankings/programming`
  and similar per-category URL paths do not exist (404) — see Caveats.

## Rows and fields

`rows.jsonl` contains **2,444 rows**: 1,158 usage rows (579 model/variant
configurations x 2 metrics) plus 1,286 pricing rows (454 models x 2-4
price-tier metrics each, depending on which fields the source publishes).

### Usage rows (`subset: "rankings/week"`)

Each of the 579 model/variant rows in the source's `week` view becomes two
schema rows:

- `metric: "tokens_weekly"` — `value` is the source's
  `total_completion_tokens + total_prompt_tokens` for that model/variant over
  the window, `unit: "tok"`, `n` is the source's request `count`. `cost_usd`
  is paired from the source's `total_usage` field on the same row
  (`cost_basis: "per_week_total_usage_usd"`); this is interpreted as
  aggregate USD spend across all users on that model/variant for the window
  (verified: free-tier variants consistently show `total_usage` near zero,
  e.g. a `:free` DeepSeek variant with 10B+ tokens and `total_usage: 84.231`,
  consistent with near-zero effective pricing — this is **not** a per-task
  or per-million-token price; see the separate pricing rows for that).
- `metric: "token_share"` — a value this lane **derived**, not one OpenRouter
  publishes directly: each model/variant's token total divided by the sum of
  `total_completion_tokens + total_prompt_tokens` across all 579 captured
  rows, times 100, `unit: "%"`. The denominator and method are stated in each
  row's `notes` so the derivation is auditable back to source numbers; no
  value was estimated or invented.

`model_raw` is the source's `variant_permaslug` (includes date suffix and, for
non-standard serving tiers, a `:free`/`:batch` suffix exactly as published).
`model_id` is the canonical id: where the source's dated `model_permaslug`
matched a `canonical_slug` in the `/api/v1/models` catalog, this lane used
that model's own dateless `id` field (OpenRouter's own canonical naming,
e.g. `openai/gpt-6-luna-pro`); otherwise it stripped a trailing `-YYYYMMDD`
date suffix as a best-effort guess. Vendor prefixes `x-ai` and `z-ai` were
normalized to `xai` and `zai` to match this audit's other lanes;
`mistralai`→`mistral`, `moonshotai`→`moonshot`, `meta-llama`→`meta` were
also normalized. Other OpenRouter-specific vendor namespaces (e.g.
`anthracite-org`, `thedrummer`, community fine-tuners) were left as-is —
these are genuinely third-party publishers on OpenRouter, not the frontier
labs' own listings.

Each source row's `date` field was copied to `measured_on` even though rows
in a single "week" pull carry six different calendar dates
(2026-09-17 through 2026-09-22) — this reflects OpenRouter's own per-model
data-freshness staggering within the window, not an error in this capture;
flagged in each row's `notes`.

### Pricing rows (`subset: "pricing"`)

For each of the 454 models in `/api/v1/models`, this lane emitted one row per
published price field, converting the source's USD-per-token figure to
USD-per-million-tokens (stated in `notes`):

- `price_usd_per_mtok_input` (source field `pricing.prompt`) — all 454 models.
- `price_usd_per_mtok_output` (source field `pricing.completion`) — all 454
  models.
- `price_usd_per_mtok_input_cache_read` — 287 models that publish this field.
- `price_usd_per_mtok_input_cache_write` — 91 models that publish this field.

`model_raw` is the source's `id` field (OpenRouter's canonical model id,
e.g. `cohere/command-a-plus`); `model_id` applies the same vendor
normalization as above. `reporter` is `vendor` for pricing rows (these are
the per-model provider prices OpenRouter lists), versus `independent` for
the usage rows (OpenRouter's own aggregated platform usage, not a vendor
claim or a crowd vote). `unit` is `usd_per_mtok`; `higher_is_better: false`.

Audio, image, and web-search pricing fields (`pricing.audio`,
`pricing.image`, `pricing.web_search`, etc.) were **not** captured — out of
scope per the brief's "prompt/completion price per model."

## Caveats

- OpenRouter's rankings page has no per-category (e.g. "programming",
  "roleplay", "marketing") breakdown of *model* token usage as of this
  capture. A "Filter by benchmark category" control exists on the page, but
  it filters the separate Artificial-Analysis-sourced Intelligence Index
  scatter plot (an `aaData`/benchmarks block also embedded in the page,
  not captured here — out of scope for this benchmark id), not the token
  rankings table. `category` and `view` (other than `week`) query parameters
  passed to the rankings API had no effect on the returned data (verified:
  identical payload with and without `&category=programming`). A separate
  "Apps" ranking (top applications by token volume, with app-level
  categories like `cli-agent`, `roleplay`, `programming-app`) does exist on
  the same page but ranks applications, not models, and was out of scope for
  this benchmark id.
- Some models publish tiered pricing keyed by prompt-token thresholds (an
  `overrides` array in `pricing`, e.g. `qwen/qwen3-coder-flash` charges more
  above 32K and 128K prompt tokens). This capture recorded only the base-tier
  price; tiered overrides were not captured as separate rows.
- `token_share` is a derived metric (see above), not something OpenRouter
  publishes as a percentage; treat it as informational, computed purely from
  this capture's own source numbers.
- The 579-row "full" list from the JSON API and the ~20-row list embedded in
  the rendered page's RSC payload agree exactly on every model that appears
  in both (spot-checked); the API list is treated as authoritative because
  it is more complete.
- No contamination or audit concerns apply (this is usage telemetry, not a
  benchmark), but usage volume reflects OpenRouter's own traffic mix only —
  it is not representative of total market usage across all inference
  providers.

## Verification

Spot-checked 10 rows by independently re-reading the raw captured JSON files
(`rankings-models-week-api.json`, `models-api.json`) with a separate Python
snippet (not the build script):

- Top 3 by `tokens_weekly`: `z-ai/glm-5.3-flash-20260826` (18,424,365,913,421
  tok, n=414,078,776, cost_usd=959,647.90), `deepseek/deepseek-v4.1-flash-20260910`
  (17,834,842,673,367 tok, n=293,970,473, cost_usd=633,328.27),
  `tencent/hy4-preview-20260827` (12,941,237,226,827 tok, n=113,033,846,
  cost_usd=99,956.88) — all matched exactly against raw.
- 4 additional randomly sampled `tokens_weekly` rows
  (`openai/gpt-5.3-codex-20260224`, `mistralai/mistral-large-2512`,
  `google/lyria-3-pro-preview-20260330`, `google/gemini-3.8-flash-20260902`)
  — token totals, request counts, and cost_usd all matched exactly.
- 3 randomly sampled pricing rows (`qwen/qwen3-coder-flash` cache-write price,
  `aion-labs/aion-rp-llama-3.1-8b` output price, `openai/gpt-5` cache-read
  price) — each recomputed independently from the raw per-token price and
  matched the captured USD-per-mtok value exactly.

No mismatches found; no corrections were needed.

Added by coverage audit 2026-09-22.
