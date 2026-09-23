# vals-index — Vals AI Index

added by coverage audit 2026-09-22

## What it measures

The Vals Index is Vals AI's GDP-weighted composite of agentic model
performance across Finance, Coding, and Legal work. Version 2 (live as of
2026-09-22) combines seven component benchmarks into sector averages, then
weights the sectors by each sector's approximate share of U.S. GDP (BEA
value-added-by-industry data via FRED):

- Finance (~8.0% weight) = AVG(Finance Agent v2, Excel Modeling Benchmark/EMB)
- Coding (~5.6% weight) = AVG(Terminal-Bench 2.1, Vibe Code Bench, Code Migration)
- Legal (~1.2% weight) = AVG(Legal Research Bench, HLAB — Harvey's Legal Agent Benchmark)
- `Vals_Index = (8.0*Finance + 5.6*Coding + 1.2*Legal) / 14.8`

Each component column on the index page is stated to be "that benchmark's own
published standalone score" under the same methodology as its own leaderboard
page, with one exception: Code Migration on the index uses a fixed 50-of-120
CLI-task + all-10-COBOL-task subset (75/25 weighted) chosen by Monte Carlo
search to reproduce the full 120-task leaderboard at Spearman 0.99 (0.8 pts
mean absolute error on the models used to choose it; 1.1 pts on held-out
models, per the page's own methodology text).

Version history noted on the page: v1 → v1.1 → v1.2 → v2 (8/13/2026), which
replaced CorpFin with EMB, added Code Migration to coding, added Legal
Research Bench and HLAB to a returning Legal sector, and dropped SWE-Bench
Verified from coding ("SWE-Bench has become saturated").

## Data captured

- Version: 2. Page `updated` date: 2026-09-22 (same day as this collection —
  the freshest possible snapshot).
- 63 models, 8 subsets each (`overall` = the index score, plus the 7
  component benchmarks) = 504 rows in `data/vals-index/rows.jsonl`.
- Confirmed 2026-frontier coverage: anthropic/claude-opus-5-5,
  anthropic/claude-fable-5-1, openai/gpt-6-astra, openai/gpt-5.6-sol/luna/terra,
  google/gemini-3.8-flash, xai/grok-4.7 (raw slug `grok/grok-4.7`),
  deepseek/deepseek-v4.1-flash (raw `deepseek/deepseek-v4.1-flash`),
  zai/glm-5.3, moonshotai/kimi-k3 (raw `kimi/kimi-k3`), alibaba/qwen3.8-max,
  minimax/minimax-m3, meta/muse-spark-1-3-max — all present with 2026-09-22
  scores.

## How it was retrieved

The page is built with **Astro** (`<meta name="generator" content="Astro
v4.16.18">`), not Next.js — there is no `__NEXT_DATA__`/RSC payload. Astro
ships each interactive island's props as an HTML-entity-encoded JSON string
in the `props="..."` attribute of `<astro-island>` tags. The relevant island
is `BenchmarkView` (component-url `/_astro/BenchmarkView.*.js`), whose props
attribute, once HTML-unescaped, is a ~514KB JSON blob using a devalue-style
`[type, value]` tagging scheme (`0` = scalar/object, `1` = array). A small
recursive "untag" pass converts it to plain JSON
(`raw/vals-index/BenchmarkView.unwrapped.json`). The `ValsIndexBarGraph`
island carries the same `tasks.overall` data in a lighter per-model shape
(`accuracy`, `latency`, `cost_per_test`, no per-task breakdown) and was used
as a cross-check. `FooterTrendLine`'s props give one flagship-per-generation
trend series per vendor, which supplied the human-readable display names
(e.g. "Claude Opus 5.5", "GPT-6 Astra") used to confirm identity, but not a
full 63-model name map — `model_raw` in rows.jsonl is therefore the
`vendor/slug` key exactly as it appears in the underlying data payload (the
literal identifier the source uses to key each model), not the prose display
name.

Raw files under `raw/vals-index/`: `vals_index.html` (full page, ~1.2MB),
`BenchmarkView.json`/`.unwrapped.json`, `ValsIndexBarGraph.json`/`.unwrapped.json`,
`ScatterGraph.json`/`.unwrapped.json`, `FooterTrendLine.json`/`.unwrapped.json`,
`BenchmarkSelector.json`/`.unwrapped.json`, `BenchmarkVersionSelector.json`/`.unwrapped.json`.

## Metric definitions

- `overall` subset → `metric = index_score`, the GDP-weighted composite
  described above (0-100 scale, `unit = %`).
- Each component subset (`finance_agent`, `emb`, `terminal_bench_2_1`,
  `vibe_code_bench`, `code_migration`, `legal_research`,
  `legal_agent_benchmark`) → `metric = accuracy`, that benchmark's own
  standalone score, unit `%`.
- `cost_usd` = the source's `cost_per_test` field, `cost_basis = per_task`.
  Confirmed unit is USD per test (page metadata flag `use_cost_per_test:
  true`; prose elsewhere on vals.ai, e.g. the Finance Agent v2 page, phrases
  it as "$X per test").
- `time_s` = the source's `latency` field, `time_basis = per_task`. The
  vals_index page itself never labels this field's unit (its only visible
  toggle is Accuracy/Cost), but a companion Vals page (fabv2/Finance Agent
  v2) states in prose "about 3 minutes 19 seconds per task" for a model whose
  raw `latency` value is ~199 — confirming the unit is seconds.
- `ci` = the source's `stderr` field (standard error, same units as
  `value`/accuracy percentage points).
- `effort` = the source's `compute_effort` field (falls back to
  `reasoning_effort`/`reasoning` when compute_effort is null); most rows show
  `"max"`.
- Category assignment for rows.jsonl (schema categories, not manifest
  categories): `overall` → `aggregate`; `finance_agent`, `emb` → `finance`;
  `terminal_bench_2_1`, `vibe_code_bench`, `code_migration` → `agentic-coding`
  (the page's own `mode` metadata field is `"agentic"`); `legal_research`,
  `legal_agent_benchmark` → `legal`.

## Caveats

- `dataset_type: "private"` — Vals runs these on held-out, non-public task
  sets; results are not independently reproducible from public data.
- No explicit data license or redistribution terms are stated anywhere on
  the page; only a generic "Copyright © 2026 Vals AI. All rights reserved."
  footer notice. `license` is null in every row.
- `fireworks/nemotron-lightning-3p5-30b-a3b`: the raw vendor slug is
  `fireworks` (a hosting platform), but the model itself is an Nvidia
  Nemotron model; `model_id` was normalized to `nvidia/nemotron-lightning-3p5-30b-a3b`
  as the best canonical guess. Likewise `grok/*` → `xai/*` (xAI is the model
  vendor; Grok is the product/brand) and `kimi/*` → `moonshotai/*` (Moonshot
  AI is the vendor; Kimi is the product line).
- `MiniMax-M2.7`/`MiniMax-M3` and `Muse_Spark_*` slugs used mixed case /
  underscores in the raw data; `model_id` lowercases and hyphenates them
  (`model_raw` is kept verbatim).
- One model, `fireworks/nemotron-lightning-3p5-30b-a3b`, scored `accuracy: 0`
  on `vibe_code_bench` — copied as-is (not an extraction error; verified
  against raw HTML in the Verification section below).

## Other Vals AI benchmark pages that exist (not collected)

Per instructions, `legal_bench` (vals.ai/benchmarks/legal_bench = LegalBench)
was already collected by the `legalbench` lane and is excluded below.
Discovered via the `/benchmarks` landing page and the in-page benchmark
switcher. "2026 frontier?" reflects whether the page's own top-3/mention
count shows current-generation models (Claude Opus 5.5, Fable 5.1, GPT-6
Astra, Grok 4.5+, DeepSeek V4.1 Flash, GLM 5.3, Kimi K3, etc.) as of this
check, not a full re-scrape.

| Name | URL | Updated | 2026 frontier? |
| --- | --- | --- | --- |
| Vals Multimodal Index | /benchmarks/vals_multimodal_index | 2026-08-11 | Partial — has Opus 5, Fable 5, Grok 4.5, Kimi K3, MiniMax-M3; missing Opus 5.5/GPT-6 Astra/DeepSeek V4.1. (being collected as `vals-multimodal-index`) |
| Vals RSI Index | /benchmarks/rsi_index | 2026-09-21 | Yes — Opus 5.5, Fable 5.1, Opus 5 in top 3. "Can a model do the research that builds the next model?" — a third Vals index worth a lane. |
| Web Search Index | /benchmarks/web_search | 2026-07-16 | Partial/stale — top 3 are Fable 5 (x2), GPT 5.6 Sol; no Opus 5.5 or GPT-6 Astra; ~2 months stale relative to vals_index. |
| Code Migration | /benchmarks/code-migration | 2026-09-22 | Yes (feeds vals_index's coding sector; also has its own full 120-task standalone leaderboard) |
| BioMysteryBench | /benchmarks/biomysterybench | 2026-09-21 | Yes |
| CUA-bench | /benchmarks/cua_bench | 2026-09-18 | Yes |
| CyberBench v1.1 | /benchmarks/cyber | 2026-09-18 | Yes |
| Excel Modeling Benchmark (EMB) | /benchmarks/emb | 2026-09-22 | Yes (feeds vals_index) |
| Finance Agent v2 | /benchmarks/fabv2 | 2026-09-22 | Yes (feeds vals_index) |
| Harvey's Legal Agent Benchmark (HLAB) | /benchmarks/hlab | 2026-09-22 | Yes (feeds vals_index) |
| IOI | /benchmarks/ioi | 2026-09-21 | Yes |
| Legal Research Bench | /benchmarks/legal_research | 2026-09-22 | Yes (feeds vals_index) |
| MedCode | /benchmarks/medcode | 2026-09-22 | Partial — top 3 (Opus 5, Gemini 3.1 Pro Preview, Fable 5) skew slightly older; likely has newer models further down the list. |
| MedScribe | /benchmarks/medscribe | 2026-09-22 | Yes |
| MysteryMechanism | /benchmarks/mysterymechanism | 2026-09-21 | Yes |
| Poker Agent | /benchmarks/poker_agent | 2025-12-23 | No — stale, top 3 are GPT 5.2/5, Gemini 3 Flash (12/25); not refreshed since Dec 2025. |
| ProgramBench | /benchmarks/programbench | 2026-09-22 | Yes |
| ProofBench v1.1 | /benchmarks/proof_bench | 2026-09-22 | Yes |
| Public Benefits Bench v1.1 | /benchmarks/public-benefits-bench | 2026-09-21 | Yes |
| SAGE | /benchmarks/sage | 2026-09-21 | Partial — top 3 (Opus 4.7, Gemma 4 31B IT, Opus 4.8) are older; page date is recent but leaders shown are not current-gen — worth checking the full list before assuming coverage. |
| SkillsBench | /benchmarks/skillsbench | 2026-09-11 | Yes |
| SRE Bench | /benchmarks/srebench | 2026-09-21 | Yes |
| Tax Agent Bench | /benchmarks/tax_agent_bench | 2026-09-22 | Yes |
| Terminal-Bench 2.1 | /benchmarks/terminal-bench-2-1 | 2026-09-22 | Yes (feeds vals_index; no card on the /benchmarks grid but page itself has 71 models and current-frontier hits) |
| Terminal-Bench 4.0 | /benchmarks/terminal-bench-4 | 2026-09-22 | Yes |
| Terminal-Bench Science | /benchmarks/terminal-bench-science | 2026-09-22 | Yes |
| Time Horizon Index: KSP | /benchmarks/time_horizon_index | 2026-09-14 | Yes |
| Vibe Code Bench 1-100 | /benchmarks/vcb-1-100 | 2026-09-22 | Yes (feeds vals_index's Code Migration sibling set — a harder 1-100 sequential variant) |
| Vibe Code Bench v1.1 | /benchmarks/vibe-code | 2026-09-22 | Yes (feeds vals_index) |
| VoiceCodeBench | /benchmarks/voice-code-bench | 2026-09-18 | N/A — speech/ASR models (GPT Live Transcribe, Grok Voice Transcribe, Cartesia Ink), not general LLMs. |
| GPQA | /benchmarks/gpqa | 2026-09-01 | Yes — 138 models tested, strong current-frontier mention count; general academic benchmark, not Vals-proprietary. |
| LiveCodeBench (lcb) | /benchmarks/lcb | 2026-09-01 | Yes — 143 models, strong frontier presence. |
| MMLU-Pro | /benchmarks/mmlu_pro | 2026-09-01 | Yes — 138 models, highest frontier-mention density of anything checked. |
| MMMU | /benchmarks/mmmu | 2026-09-01 | Yes (moderate) — 93 models; relevant since it's multimodal and could cross-check `vals-multimodal-index`. |
| Mortgage Tax | /benchmarks/mortgage_tax | 2026-09-01 | Yes (moderate) — 98 models. |
| SWE-bench | /benchmarks/swebench | 2026-09-01 | No clear signal — 88 models but frontier-keyword count matched only the site-wide baseline; consistent with the vals_index methodology note that "SWE-Bench has become saturated" and was dropped from the index. |
| CorpFin v2 | /benchmarks/corp_fin_v2 | 2026-08-12 | No clear signal — 134 models but frontier-keyword count matched only baseline. This is the benchmark vals_index v2 replaced with EMB; likely superseded/deprioritized. |
| Case Law v2 | /benchmarks/case_law_v2 | 2026-05-04 | No — baseline-only frontier signal, ~4.5 months stale. |
| MedQA | /benchmarks/medqa | 2026-04-16 | No — baseline-only frontier signal, ~5 months stale. |
| AIME | /benchmarks/aime | 2026-04-16 | No — baseline-only, ~5 months stale. |
| MATH500 | /benchmarks/math500 | 2026-01-09 | No — stale (~8.5 months). |
| MGSM | /benchmarks/mgsm | 2026-01-09 | No — stale (~8.5 months). |
| Terminal-Bench 2 (superseded by 2.1) | /benchmarks/terminal-bench-2 | 2026-06-04 | No clear signal — baseline-only; superseded by terminal-bench-2-1 above. |
| Public Benefits Bench (v1, superseded) | /benchmarks/public-benefits-bench-v1 | — | Superseded by `public-benefits-bench` (v1.1) above; not separately assessed. |

Method note: "frontier-keyword count" = occurrences of
`claude-opus-5-5|gpt-6-astra|claude-fable-5-1` in each page's raw HTML. A
flat count of 3 recurred identically across several unrelated pages and was
treated as site-wide boilerplate (e.g. a global "trending models" widget),
not evidence of that benchmark's own model list; pages with counts well above
3 (9-53) were treated as genuinely containing current-frontier models. This
is a cheap triage signal, not a benchmark-by-benchmark verification — a
future lane collecting any of these should re-check its own model list
directly, especially `sage` and `medcode` where the top-3 leaders looked
older despite a recent `updated` date.

**Candidates most worth a follow-up lane**: Vals RSI Index (a third
GDP/economic-relevance index, clearly current), GPQA/LiveCodeBench/MMLU-Pro
(large model counts, high frontier density, but these are third-party
academic sets re-hosted by Vals rather than Vals-proprietary agentic
benchmarks — check for overlap with existing non-Vals lanes for the same
underlying benchmarks before adding), and MMMU (multimodal, could
cross-validate `vals-multimodal-index`).

## Verification

Checked 10 rows total against `raw/vals-index/vals_index.html` (the raw page
capture), independently of the Python extraction script — via a fresh
`re.search`/regex pass over the HTML-unescaped `BenchmarkView` island props
string (not the `.unwrapped.json` file the row-builder script reads from),
and separately via a direct `re.findall` grep pattern over the raw
HTML-entity-encoded bytes for the top rows:

- Top 3 by headline metric (`overall`/`index_score`), plus 2 more from the
  same ranking for context:
  - `anthropic/claude-opus-5-5` → 69.689 ✓
  - `anthropic/claude-fable-5-1` → 68.825 ✓
  - `anthropic/claude-opus-5` → 67.213 ✓
  - `openai/gpt-6-astra` → 66.608 ✓
  - `anthropic/claude-fable-5` → 66.036 ✓
- 7 additional randomly sampled rows across different subsets, checked for
  `accuracy`, `cost_usd`, and `time_s` together:
  - `code_migration` / `openai/gpt-5.6-luna`: 44.774 / 2.797004 / 3404.333 ✓
  - `overall` / `ant/ling-3.0-flash-2607`: 21.699 ✓
  - `overall` / `xiaomi/mimo-v2.6-pro`: 59.732 ✓
  - `legal_research` / `anthropic/claude-opus-5`: 55.288 ✓
  - `finance_agent` / `google/gemini-3.6-flash`: 56.296 ✓ (independently
    re-derived via a fresh regex against the decoded raw HTML)
  - `vibe_code_bench` / `fireworks/nemotron-lightning-3p5-30b-a3b`: 0 ✓
    (confirmed this is a genuine zero score in the source, not a null/parse
    failure)
  - `vibe_code_bench` / `thinkingmachines/inkling-small`: 19.052 ✓
    (independently re-derived via a fresh regex against the decoded raw HTML)

All 10 checked values matched exactly (no unit conversion needed — values
copied as published). No mismatches found; no fixes were necessary.

Row count validated: `python3` check confirmed all 504 lines of
`rows.jsonl` parse as JSON and every SCHEMA.md field key is present on every
row (504 = 63 models × 8 subsets, matching `total_models: 63` in the source
metadata).
