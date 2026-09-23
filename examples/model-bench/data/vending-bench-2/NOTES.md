# vending-bench-2 — Andon Labs

## What it measures

Vending-Bench 2 (https://andonlabs.com/evals/vending-bench-2) tasks an LLM
agent with running a simulated vending-machine business over a full year
(sourcing suppliers by email/negotiation, restocking, paying a $2/day fee,
handling refunds, adversarial suppliers, delayed deliveries). The agent
starts with a $500 balance and is scored solely on its bank account balance
at the end of the year (higher is better; there is no ceiling — the source
explicitly estimates a "good" strategy could reach roughly $63k vs. the
current best model's ~$15.5k). Each model is run for `n` epochs (typically
4-6 independent yearlong simulations) and the leaderboard reports the mean
final balance plus spread across those runs. A companion chart pairs each
model's mean final balance with the estimated dollar cost of running it
(computed from the LLM provider's own input/output token pricing, no
prompt caching).

## How the data was captured

The page is server-rendered SvelteKit; the static HTML only contains the
top 10 rows (a "Show 53 more" button reveals the rest client-side). The full
dataset backing every chart and the leaderboard table is not a
separately-fetched JSON endpoint — it is embedded as a JS object literal
inside two of the page's own code-split chunks:

- `raw/vending-bench-2/DAnYm5vA.js` → the module-level object `m={vb2:{...}}`
  (the primary results: per-model `time_series` of daily balance averaged
  across epochs, `num_epochs`, `final_value`, `final_value_std`,
  `final_value_sem`, `final_value_ci95`, `final_value_geomean`). Extracted
  the full object with a brace-matching script, evaluated it with Node
  (`node -e '... eval(...) ... JSON.stringify(...)'`) since it uses
  unquoted JS object keys, not valid JSON — saved as
  `raw/vending-bench-2/vb2_data.json` (63 models).
- `raw/vending-bench-2/node27.js` (the eval page's own compiled component) →
  the object `ct={...}` giving per-model API cost: `provider`, `n` (epochs
  costed), `mean_cost`, `min_cost`, `max_cost`, `avg_input_tokens`,
  `avg_output_tokens`, `price_per_mtok_in`, `price_per_mtok_out`. Same
  extraction method, saved as `raw/vending-bench-2/cost_data.json` (60
  models — 3 models present in the results object have no cost entry: Kimi
  K2.7 Code, Kimi K3 (Fireworks), MiniMax-M3).
- Also kept: the raw page HTML (`vending-bench-2.html`, static top-10 rows
  and full page text), and the two source chunk files themselves as
  evidence.

No GitHub repo or public CSV/JSON export was found for Vending-Bench 2
specifically; this in-bundle extraction was the only way to get the full
63-model table (the public UI only shows 10 rows without a client
interaction).

## Confirms current data

The board is led by 2026 frontier models: GPT-6 Astra (released 2026-09-04
per the page's own release-date map), Claude Opus 5, Claude Opus 4.7,
GPT-5.6 Sol/Terra/Luna, Grok 4.6/4.20, GLM-5.2/5.3, Gemini 3.8 Flash, Claude
Fable 5.1, Kimi K3, MiniMax-M3. Source is live and current.

## Field notes / caveats

- `metric` = `final_money_balance`, `unit` = `usd`, `value` = the mean final
  bank balance across all epochs for that model (source's `final_value`).
- `ci` = the source's `final_value_sem` (standard error of the mean), because
  that is the number the leaderboard visually pairs with each score as
  "± $X" (verified: e.g. GPT-6 Astra shows "$15,514.70 ± $1,074" and
  `final_value_sem` = 1074.48; `final_value_ci95` for the same model is
  2105.99 — roughly 1.96× the SEM — so the on-page "±" is the SEM, not a
  95% CI half-width). The true `final_value_ci95` is preserved in `notes`
  for every row, along with `final_value_std` and, when defined,
  `final_value_geomean` (geometric mean of final balances — undefined for
  models with a non-positive run, per the source's own `None` value in
  those cases).
- `n` = the source's `num_epochs` (number of independent yearlong runs
  averaged into the reported score); this can differ slightly from the
  epoch count behind the cost figure (`api_cost` note in `notes` states the
  cost object's own `n`).
- `cost_usd` / `cost_basis="per_run"` = the source's `mean_cost`: the
  estimated dollar cost (from the provider's own token pricing, no caching)
  to run one full yearlong simulation. Null for the 3 models missing a cost
  entry (Kimi K2.7 Code, Kimi K3 (Fireworks), MiniMax-M3).
- `effort`: populated only for the "Claude Fable 5" and "Claude Opus 4.8"
  families, whose source labels are literally `"Claude Fable 5 - High"`,
  `"... - Low"`, `"... - Max"`, `"... - Medium"`, `"... - None"` and
  `"Claude Opus 4.8 - High"`, `"... - Max"` — the suffix after " - " is
  treated as effort. All other models have no effort distinction on this
  leaderboard (null).
- `harness`: populated for models the source itself splits by serving
  provider/tooling — `"Kimi K3 (Moonshot)"` vs `"Kimi K3 (Fireworks)"`
  (same model, different inference provider) and
  `"Gemini 3.1 Pro Custom Tools"` (a distinct entry from plain
  `"Gemini 3.1 Pro"`).
- `model_id` guesses follow vendor naming conventions
  (`openai/gpt-6-astra`, `anthropic/claude-opus-5`,
  `anthropic/claude-fable-5`, `google/gemini-3.8-flash`, `xai/grok-4.6`,
  `zhipu/glm-5.3`, `moonshot/kimi-k3`, `minimax/minimax-m3`,
  `alibaba/qwen-3.6-max`, `deepseek/deepseek-v4-pro`). "Muse Spark 1.1" has
  no identifiable vendor from the page and uses `unknown/muse-spark-1.1`.
- `subset` and `measured_on` are null — the source does not tag rows with a
  split name, and does not date-stamp individual scores (it does give a
  model *release* date used only for the "performance vs. release date"
  trend chart, not a measurement date; not carried into rows since it is
  not when the eval was run).
- Vending-Bench Arena (a multi-agent competitive variant mentioned on the
  same page) was not collected — the page only links to "See the arena
  results" without embedding arena data on this page, and the assignment
  named Vending-Bench 2 as the primary target with Arena "if present on
  this page." Out of scope for this pass; would need a separate fetch of
  the Arena page if wanted later.
- License / redistribution terms: not stated on the page. Left
  `license: null` throughout.
- `reporter` = `independent` for all rows: Andon Labs designs and runs this
  benchmark itself (not vendor-submitted, not a community/crowd metric),
  matching the SCHEMA reporter taxonomy's closest fit, though note Andon
  Labs is also the benchmark's creator (not a fully third-party auditor).

## What could not be captured

- Raw per-epoch time series (daily balance for all 365 days × every epoch)
  exists in the source data but was not flattened into rows.jsonl — SCHEMA
  is one row per (model, metric), and the headline metric is the final
  balance; the full time series is kept in the raw JSON capture
  (`raw/vending-bench-2/vb2_data.json`) if a future pass wants a
  longitudinal view.
- Vending-Bench Arena data (see above).

## Verification

Validated all 63 rows parse as JSON and contain every SCHEMA.md field
(`python3` check — 0 missing fields, 0 null `value`s).

Spot-checked all 10 rows visible in the static HTML (the full top-10 table,
more than the required minimum) by grepping the raw HTML directly for the
`$X,XXX.XX` and `± $X,XXX` figures and comparing to `rows.jsonl`:
GPT-6 Astra ($15,514.70), Claude Opus 5 ($11,181.87), Claude Opus 4.7
($10,936.76), GPT-5.6 Sol ($9,619.37), Grok 4.6 ($9,047.03), GLM-5.2
($8,313.78), GLM-5.3 ($8,163.61), Claude Opus 4.6 ($8,017.59), GPT-5.5
($7,523.84), GPT-5.6 Terra ($7,343.21) — all match `value` exactly. This
comparison also surfaced and corrected the `ci` field's meaning (see above):
the initially-generated rows used `final_value_ci95`, which did not match
the page's displayed "±" figures; re-derived against `final_value_sem`,
which does match exactly for all 10 rows.

Additionally spot-checked 7 more rows (17 total) by independently loading
`raw/vending-bench-2/vb2_data.json` and `raw/vending-bench-2/cost_data.json`
directly in a separate Python one-liner (not the row-generation script) and
comparing `final_value`, `final_value_sem`, and `mean_cost` against the
`rows.jsonl` line: GPT-6 Astra, Claude Opus 5, GLM-5.3, Kimi K2.5, Qwen3 Max,
Gemini 3.1 Pro Custom Tools, MiniMax-M3 (including the one row with a null
`cost_usd`, confirming the null is a genuine missing-source-data case, not
an extraction bug) — all match exactly.

No mismatches remained after the `ci` fix; no other fixes were needed.

Added by coverage audit 2026-09-22.
