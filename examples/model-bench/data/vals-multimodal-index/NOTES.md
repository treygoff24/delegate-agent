# vals-multimodal-index — Vals Multimodal Index

added by coverage audit 2026-09-22

## What it measures

Vals AI's second GDP-weighted composite index, distinct from the main Vals
Index. It weights sectors by estimated dollar contribution to U.S. GDP
(rather than percentage share) and specifically selects component benchmarks
that require reading non-text documents (scanned/photographed financial and
tax documents, handwritten student work) alongside pure-text coding tasks:

- Finance (weight 2.0, ~$2T GDP): AVG(CorpFin v2 — "corporate finance document
  analysis", Finance Agent v2 — "multi-step financial reasoning tasks",
  Mortgage Tax — "mortgage and tax document analysis")
- Coding (weight 1.4, ~$1.4T GDP): `0.25*SWE-bench Verified + 0.25*Terminal-Bench
  2.1 + 0.5*Vibe Code Bench`
- Education (weight 0.3, ~$270B GDP): SAGE — "grading handwritten student
  work in mathematics"
- `Vals_Multimodal_Index = (2.0*Finance_avg + 1.4*Coding + 0.3*SAGE) / 3.7`

The "multimodal" framing is about the *inputs* three of the eight component
tasks require (document images/PDFs for CorpFin and Mortgage Tax, handwritten
work images for SAGE), not a vision-QA benchmark in the LMArena/MMMU sense.
SWE-bench, Terminal-Bench 2.1, and Vibe Code Bench (the Coding sector) are
plain text/code tasks included only because Coding is one of the three
GDP-weighted sectors.

Subset-selection methodology (stated on the page): SWE-bench Verified uses 33
randomly sampled instances per difficulty tier (by solution time: <15min,
15min-1hr, 1-4hr, >4hr) plus all 3 hardest-tier instances; CorpFin uses 3
randomly selected questions per unique source document; Finance Agent v2 uses
a "13-model multimodal index subset" run three times per model; Vibe Code
Bench uses 22 representative app-building tasks. All subsets were validated
against full-benchmark scores using held-out models.

## Data captured

- Version: 1.2. Page `updated` date: **2026-08-11** — about 6 weeks stale
  relative to today (2026-09-22) and relative to the main Vals Index (updated
  same-day). Still usable: it carries genuine 2026-generation models
  (anthropic/claude-opus-5, anthropic/claude-fable-5, kimi/kimi-k3,
  grok/grok-4.5, minimax/MiniMax-M3, google/gemini-3.6-flash,
  openai/gpt-5.6-sol/luna/terra), just not the very newest tier (no Opus 5.5,
  no GPT-6 Astra, no DeepSeek V4.1, no GLM 5.3, no Qwen 3.8-max's later
  siblings). Not stale enough to skip per the brief's bar ("no 2026 frontier
  models"), but materially behind the main index — flagged for the lead.
- 33 models, 8 subsets each (`overall` + 7 components) = 264 rows in
  `data/vals-multimodal-index/rows.jsonl`.

## How it was retrieved

Same Astro-island extraction method as `vals-index` (see that NOTES.md for
the mechanism): the `BenchmarkView` island's `props` attribute on
`vals_multimodal_index.html` HTML-unescapes to a devalue-style `[type,
value]`-tagged JSON blob, unwrapped with the same recursive script into
`raw/vals-multimodal-index/BenchmarkView.unwrapped.json`. `ValsIndexBarGraph`
and `ScatterGraph` were captured as cross-checks; `FooterTrendLine`'s payload
on this page is identical to the one on the main Vals Index page (a
site-wide, not benchmark-specific, component) and was not used.

Raw files under `raw/vals-multimodal-index/`: `vals_multimodal_index.html`
(full page), `BenchmarkView.json`/`.unwrapped.json`,
`ValsIndexBarGraph.json`/`.unwrapped.json`, `ScatterGraph.json`, plus the
shared selector/trendline islands.

## Metric definitions

- `overall` subset → `metric = index_score` (0-100, `unit = %`), the
  economic-weighted composite formula above.
- Each component subset (`finance_agent`, `corp_fin_v2`, `swebench`,
  `terminal_bench_2_1`, `vibe_code_bench`, `sage`, `mortgage_tax`) → `metric
  = accuracy`, that benchmark's own standalone score, `unit = %`.
- `cost_usd` = source `cost_per_test`, `cost_basis = per_task` (page metadata
  flag `use_cost_per_test: true`, same convention as the main Vals Index).
- `time_s` = source `latency`, `time_basis = per_task`; unit inferred as
  seconds by the same cross-page evidence documented in the `vals-index`
  NOTES.md (a sibling Vals page states latency in prose as minutes+seconds
  for a value on the same numeric scale).
- `ci` = source `stderr`.
- `effort` = source `compute_effort`/`reasoning_effort`/`reasoning`; **every
  row in this dataset has `compute_effort: null`** (unlike the main Vals
  Index, which mostly reports `"max"`) — Vals did not record/expose an
  effort setting for this run.
- Row `category` values (schema categories): `overall` → `aggregate`;
  `finance_agent`, `corp_fin_v2`, `mortgage_tax` → `finance`; `swebench` →
  `coding` (page metadata `mode: "agentic"`, but SWE-bench itself is not
  typically framed as multimodal or agentic-scaffold-specific here, kept as
  plain `coding` since the source doesn't distinguish it from `terminal_bench_2_1`);
  `terminal_bench_2_1`, `vibe_code_bench` → `agentic-coding`; `sage` →
  `multimodal` (the one component that is directly a vision/handwriting task).

## Caveats

- `dataset_type: "private"`; no public raw task data.
- No explicit license/redistribution terms found on the page beyond the
  site-wide "Copyright © 2026 Vals AI. All rights reserved." footer.
  `license` is null in every row.
- Data is ~6 weeks older than the main Vals Index snapshot taken the same
  session — if this lane is refreshed later, expect the model roster to grow
  (Opus 5.5, GPT-6 Astra, DeepSeek V4.1, GLM 5.3, Qwen 3.8 family, etc. are
  present on the main index but absent here).
- `swebench` here means **SWE-bench Verified**, not the full SWE-bench suite,
  per the page's own methodology text.
- Same vendor-id normalization notes as `vals-index`: `grok/*` → `xai/*`,
  `kimi/*` → `moonshotai/*`; `MiniMax-M3`/`muse_spark_*` lowercased and
  hyphenated in `model_id` (kept verbatim in `model_raw`).
- See `data/vals-index/NOTES.md` for the full list of other Vals AI
  benchmark pages discovered during this audit (not duplicated here to avoid
  drift between the two files).

## Verification

Checked 10 rows total against `raw/vals-multimodal-index/vals_multimodal_index.html`,
independently of the Python extraction script, via a fresh `re.search` pass
over the HTML-unescaped `BenchmarkView` island props string:

- Top 5 by headline metric (`overall`/`index_score`):
  - `anthropic/claude-fable-5` → 74.154 (cost_usd 7.972213) ✓
  - `anthropic/claude-opus-5` → 73.896 ✓
  - `kimi/kimi-k3` → 73.42 ✓
  - `openai/gpt-5.6-sol` → 72.64 ✓
  - `anthropic/claude-opus-4-8` → 70.889 ✓
- 5 additional randomly sampled rows across different subsets:
  - `mortgage_tax` / `anthropic/claude-opus-5`: accuracy 72.059, latency
    7.978 ✓
  - `corp_fin_v2` / `kimi/kimi-k2.6`: 68.182 ✓
  - `vibe_code_bench` / `anthropic/claude-sonnet-5`: 86.902 ✓
  - `overall` / `alibaba/qwen3.6-plus`: 51.516 ✓
  - `finance_agent` / `anthropic/claude-fable-5`: 56.349 ✓

All 10 checked values matched exactly (no unit conversion needed). No
mismatches found; no fixes were necessary.

Row count validated: a `python3` check confirmed all 264 lines of
`rows.jsonl` parse as JSON and every SCHEMA.md field key is present on every
row (264 = 33 models × 8 subsets, matching `total_models`-equivalent model
count of 33 in the source metadata).
