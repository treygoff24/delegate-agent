# vectara-hallucination — Vectara Hallucination Leaderboard

## What it measures

Grounded-summarization hallucination rate, computed with Vectara's
proprietary **HHEM-2.3** (Hughes Hallucination Evaluation Model). Each LLM
is fed ~7,700+ curated source documents (news, tech, science, medicine,
legal, sports, business, education; 50 words to 24K words) at temperature 0
and asked to produce a concise, factual summary using only information in
the passage (exact prompt captured below). HHEM-2.3 then scores each
summary for factual consistency with its source. Reported per model:

- `hallucination_rate` (%) — the headline metric this leaderboard is named
  for; lower is better.
- `factual_consistency_rate` (%) — `100 - hallucination_rate`; higher is
  better.
- `answer_rate` (%) — percent of documents actually summarized (the README
  explicitly filters out refusals/very-short non-answers so a model can't
  game a low hallucination rate by declining to answer; only documents all
  models responded to are used for the final evaluation).
- `avg_summary_length_words` — published alongside `answer_rate` to show
  low hallucination scores aren't an artifact of unusually short/empty
  summaries.

Category assigned per instructions: `knowledge-work` (hallucination-in-
grounded-summarization is not a clean fit for any other SCHEMA.md category;
flagged here rather than mis-filed under `reasoning`).

## Version / date captured

- Model version: **HHEM-2.3** (per the README's "## Model" section: "This
  leaderboard uses HHEM-2.3, Vectara's commercial hallucination evaluation
  model"). An open-source variant, HHEM-2.1-Open, is available separately
  on Hugging Face/Kaggle but is not what scored this table.
- Data date: the README states **"Last updated on September 22, 2026"**
  in-page (inside an HTML comment block `<!-- LEADERBOARD_START -->` /
  `<!-- LEADERBOARD_END -->` that the repo appears to regenerate on each
  update), matching the newest chart image in the repo,
  `img/top25_hallucination_rates_2026-09-22.png`, and the repo's GitHub
  `pushed_at` timestamp of 2026-09-23T01:07:11Z (i.e. updated yesterday
  relative to capture time) — this is a live, actively maintained,
  non-stale leaderboard.
- Retrieved: 2026-09-23T01:34:28Z.

## How it was captured

Fetched via the GitHub REST API (`api.github.com/repos/vectara/
hallucination-leaderboard`) to confirm push recency, then
`raw.githubusercontent.com/vectara/hallucination-leaderboard/main/README.md`
via plain `curl`. The leaderboard is a Markdown table embedded directly in
the README (no separate JSON/CSV data file exists in the repo — confirmed
by listing the full git tree, which contains only `README.md`, `LICENSE`,
`CITATION.cff`, and PNG chart images under `img/`). Extracted the table
(108 data rows, lines 27-136 of `README.md`) into
`raw/vectara-hallucination/leaderboard_table.md`.

108 models x 4 metrics = **432 rows** in `rows.jsonl`.

Confirmed current 2026 frontier coverage: `openai/gpt-6-sol`,
`openai/gpt-6-astra`, `openai/gpt-5.6-sol`, `openai/gpt-5.5`,
`anthropic/claude-opus-4-7`, `anthropic/claude-opus-4-6`,
`anthropic/claude-sonnet-4-6`, `deepseek-ai/DeepSeek-V4-Pro`,
`zai-org/glm-5`, `google/gemini-3.1-pro-preview`,
`google/gemini-3-pro-preview`, `qwen/qwen3.5-*`, `moonshotai/kimi-k2.6` —
live and current, not stale.

## Model-name handling

`model_raw` is copied verbatim from the README table's first column
(vendor-prefixed HF/API-style slugs, e.g. `openai/gpt-6-sol`,
`anthropic/claude-opus-4-7`). `model_id` normalizes the vendor prefix to
the schema's convention (e.g. `meta-llama` -> `meta`, `mistralai` ->
`mistral`, `deepseek-ai` -> `deepseek`, `xai-org` -> `xai`, `CohereLabs` ->
`cohere`, `MinMaxAI`/`MiniMaxAI` -> `minimax`, `zai-org` -> `zai`,
`ai21labs` -> `ai21`, `ibm-granite` -> `ibm`, `moonshotai` -> `moonshot`,
`arcee-ai` -> `arcee`, `inceptionlabs` -> `inception`) and lowercases the
rest. Three rows use Hugging-Face-style "p"-for-decimal-point naming
(`zai-org/glm-4p7`, `MiniMaxAI/minimax-m2p5`, `MiniMaxAI/minimax-m2p1`,
`MiniMaxAI/minimax-m2p7`) that were converted to dotted form
(`zai/glm-4.7`, `minimax/minimax-m2.5`, etc.) since the same models appear
with dots elsewhere on this site's/EQ-Bench's naming.

**Reasoning-effort variants split into `effort`:** several rows encode an
effort/reasoning-mode setting in the raw model string rather than as a
separate column. These were parsed into the schema's `effort` field with a
shared base `model_id`:
- `openai/gpt-5.2-high-2025-12-11` / `-low-` -> `effort: "high"`/`"low"`,
  `model_id: openai/gpt-5.2-2025-12-11`.
- `openai/gpt-5.1-high-2025-11-13` / `-low-` -> same pattern,
  `model_id: openai/gpt-5.1-2025-11-13`.
- `openai/gpt-5-high-2025-08-07` / `openai/gpt-5-minimal-2025-08-07` ->
  `effort: "high"`/`"minimal"`, `model_id: openai/gpt-5-2025-08-07`.
- `openai/o4-mini-high-2025-04-16` / `-low-` -> `effort: "high"`/`"low"`,
  `model_id: openai/o4-mini-2025-04-16`.
- `xai-org/grok-4-1-fast-reasoning` / `-non-reasoning` -> `effort:
  "reasoning"`/`"non-reasoning"`, `model_id: xai/grok-4.1-fast`.
- `xai-org/grok-4-fast-reasoning` / `-non-reasoning` -> same pattern,
  `model_id: xai/grok-4-fast`.

`openai/gpt-5-mini-2025-08-07` and `openai/gpt-5-nano-2025-08-07` were
**not** treated as effort variants — "mini"/"nano" are separate model-size
SKUs in OpenAI's naming, not a reasoning-effort dial, so `effort: null` for
those. Likewise `qwen/qwen3-next-80b-a3b-thinking` keeps "thinking" as part
of its `model_id` (it is a distinct model variant, not an effort toggle
applied to a shared base model in this table).

## Reporter / license

- `reporter`: `independent` (Vectara runs and publishes this leaderboard
  independently of the model vendors it evaluates, though Vectara is
  itself a commercial vendor of the HHEM model used to judge — noted as a
  conflict-of-interest caveat, not reclassified as `vendor`, since Vectara
  is not a evaluated LLM vendor).
- `license`: **Apache-2.0**, confirmed by fetching the repo's `LICENSE`
  file directly (`raw/vectara-hallucination/LICENSE`).

## What could not be captured

- Per-document summaries and HHEM per-item scores are not published; only
  the aggregate 4 metrics per model.
- `n` (number of documents each score is computed over) is not stated per
  model on the leaderboard, only the aggregate dataset size ("over 7700
  articles"); recorded `null` per SCHEMA.md rather than attaching the
  aggregate figure to every row.
- `cost_usd` / `time_s`: not published.
- API/version details for how each specific model was accessed (e.g. "via
  OpenAI API", "via dashscope API", "via Together AI") are listed in a
  separate README section per-vendor but not merged into the table; not
  captured as structured fields since SCHEMA.md has no field for
  provider-routing details, though the `harness` field would be the
  closest fit and was left `null` since it means eval scaffold, not API
  routing.

## Verification

Spot-checked 8 distinct (model, metric) pairs, re-extracting each value
independently from `raw/vectara-hallucination/leaderboard_table.md` with a
fresh line-split/parse (separate from the row-building script):

- Top 3 by lowest `hallucination_rate` (best): `antgroup/finix_s1_32b`
  (1.8%), `openai/gpt-5.4-nano-2026-03-17` (3.1%),
  `google/gemini-2.5-flash-lite` (3.3%) — all **MATCH**.
- 5 additional rows spread across the 432-line output (indices 0, 100,
  200, 300, 400, 431), covering `deepseek-ai/DeepSeek-V3`,
  `qwen/qwen3-next-80b-a3b-thinking`, `anthropic/claude-opus-4-1-20250805`,
  `mistralai/ministral-3-14b-2512`, `mistralai/ministral-3-3b-2512`
  across `hallucination_rate` and `avg_summary_length_words` — all
  **MATCH**, no mismatches, no fixes needed.
- Validated with a small Python script that all 432 lines parse as JSON
  with every SCHEMA.md field present. All passed.

Added by coverage audit 2026-09-22.
