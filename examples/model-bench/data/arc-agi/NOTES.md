# arc-agi — ARC Prize Leaderboard

## What it measures

The ARC Prize leaderboard (https://arcprize.org/leaderboard) tracks language
model / AI system performance on three abstraction-and-reasoning benchmarks:

- **ARC-AGI-1** — the original ARC visual puzzle set (accuracy %, cost per
  task).
- **ARC-AGI-2** — a harder revision designed to resist memorization and
  brute-force search (accuracy %, cost per task).
- **ARC-AGI-3** — an interactive game-environment benchmark (accuracy %,
  total cost per run, in two harness modes: "Standard" and "Provider
  Adapter"). ARC-AGI-3 costs are per full evaluation run, not per task; some
  entries cost in the tens of thousands of dollars.

Score and cost are always reported together, one point per (model, effort
setting) pair on a cost-vs-score scatter chart plus a companion breakdown
table.

## How the data was captured

The page (`https://arcprize.org/leaderboard`) is a client-rendered Next.js
app; the static HTML and its `_next` JS chunks do not embed the leaderboard
data as JSON (checked `__NEXT_DATA__`, the RSC `self.__next_f.push` stream,
and all referenced chunk files — no `fetch()`/`.json` endpoint found). Used
`firecrawl scrape --formats markdown --only-main-content` to capture the
fully client-rendered page, which resolves to a complete Markdown table
("Leaderboard Breakdown") pairing every AI system / effort setting with its
ARC-AGI-1, ARC-AGI-2, and ARC-AGI-3 score and cost. Raw capture:
`raw/arc-agi/arc-leaderboard-firecrawl.md` (full rendered page, 898 lines);
also kept the raw HTML and JS chunks (`raw/arc-agi/leaderboard.html`,
`*.js`) as evidence that no separate JSON data source was reachable.

259 table rows parsed into 535 `(model config, ARC-AGI version)` score rows
via `raw/arc-agi/../../..` — actual parsing script was run from the session
scratchpad, not checked into this repo.

## Confirms current data

2026 frontier models are present and lead the board: GPT-6 Astra, GPT-6
Luna, Claude Opus 5.5, Claude Fable 5.1, Grok 4.6/4.20, Gemini 3.7 Flash,
DeepSeek V4 Pro/Flash, GLM-5.2, Kimi K3, Minimax M2.5. The source is live and
current as of retrieval.

## Field notes / caveats

- `model_raw` is the exact "AI System" column label including its
  parenthetical suffix (e.g. `GPT-6 Astra (XHigh)`).
- `effort`: populated only when the parenthetical suffix is a recognizable
  reasoning-effort or thinking-budget token (`Low`/`Medium`/`High`/`XHigh`/
  `Max`/`Minimal`/`None`, any `Thinking ...` token, or a token containing
  "reasoning"). A compound suffix like `(120K, Low)` splits: `effort="Low"`,
  and the other part goes to `notes` as "source parenthetical context: 120K".
  A non-effort suffix (e.g. `(Refine.)`, `(Preview)`, `(Latest)`, a date) is
  left out of `effort` (null) and recorded verbatim in `notes` as "source
  parenthetical variant label: ...".
- `harness`: for ARC-AGI-3, the source sometimes reports two figures per
  model — "Standard" and "Provider Adapter" harness modes — each becomes its
  own row with `harness` set accordingly. Entries whose AI-System label
  itself ends in "- Provider Adapter" (a separate leaderboard entry, distinct
  from the base model's own Provider-Adapter figure) also get
  `harness="Provider Adapter"`.
- `cost_basis`: `"per_task"` for ARC-AGI-1/2 (source states "$X/task");
  `"per_run"` for ARC-AGI-3 (source states a total dollar figure per harness
  mode, not per task — these can run into the tens of thousands of dollars).
- `model_id` is a best-effort canonical guess per vendor naming conventions
  (`openai/gpt-6-astra`, `anthropic/claude-opus-5-5`,
  `anthropic/claude-fable-5-1`, `google/gemini-3.7-flash`, `xai/grok-4.6`,
  `deepseek/deepseek-v4-pro`, `zhipu/glm-5.2`, `moonshot/kimi-k3`,
  `minimax/minimax-m2.5`, etc.). Ambiguous or non-LLM entries (e.g. "Claude
  4.7" without a variant name, human baselines "Human Panel" / "Avg. Mturker"
  / "Stem Grad", non-LLM program-synthesis systems "ARChitects" / "NVARC" /
  "Icecuber" / "Tiny Recursion Model (TRM)" / "Hierarchical Reasoning Model
  (HRM)" / "Inkling" / "Inkling Small" / "Dots3-Note Preview") use
  `unknown/<slug>` (or `dots-studio/dots3-note` for the one attributable
  vendor entry among these). These are still included as rows since the
  source lists them on the same leaderboard, but they are not frontier LLM
  results and should probably be filtered out of any model-vs-model
  comparison.
- `subset` is null throughout — the source does not label a public/semi-
  private/private split distinction on this page (ARC Prize's methodology
  page states scores come from a held-out evaluation set, but the
  leaderboard table itself does not tag individual rows with a split name).
- `measured_on` is null throughout — the source does not date-stamp
  individual rows; the leaderboard is continuously updated as new
  submissions arrive, so a single "last updated" date cannot be attributed to
  every row. `retrieved_at` on every row is the actual capture time.
- Two duplicate-looking labels appear more than once in the raw table with
  different values: "Grok 4 (Refine.)" (rows for two different reported cost
  points) and "GPT-5.5 (High)" (once under ARC-AGI-3 only, once under
  ARC-AGI-1/2 only) — both are the source's own table entries, kept as
  separate rows since collapsing them would drop data the source actually
  publishes.
- Per the page's own footnote: "Only systems which required less than
  $10,000 to run are shown" (for ARC-AGI-1/2); some ARC-AGI-3 entries exceed
  $10,000 because the interactive-game benchmark's cost basis differs.
  "Results marked as 'preview' are unofficial and may be based on incomplete
  testing." One row is flagged by the source itself: "o3 (Preview, Low)" has
  footnote "ARC-AGI-2 score estimate based on partial testing results and
  o1-pro pricing" (not carried into a `notes` field per-row since it wasn't
  in the parenthetical, but noted here).
- License / redistribution terms: not stated on the leaderboard page itself;
  ARC Prize's terms page (https://arcprize.org/terms) was not fetched. Left
  `license: null` throughout.

## What could not be captured

- No machine-readable JSON/CSV data source was found; all data came from the
  rendered HTML table, so exact task counts (`n`) and confidence intervals
  (`ci`) are not available (the source does not publish them on this page).
- Time/latency (`time_s`) is not published by this leaderboard at all —
  every row's `time_s`/`time_basis` is null.

## Verification

Validated all 535 rows parse as JSON and contain every SCHEMA.md field
(`python3` check — 0 missing fields, 0 null `value`s).

Spot-checked 10 rows by independently `grep`-ing the raw markdown capture
(`raw/arc-agi/arc-leaderboard-firecrawl.md`) for the exact `model_raw` label
and comparing the score/cost figures against the corresponding `rows.jsonl`
line (separate command from the row-generation script, per the audit
requirement):

- Top-3 by ARC-AGI-1 accuracy: Claude Opus 5.5 (High) 98.5%/$0.158,
  GPT-6 Astra (XHigh) 98.5%/$0.347, GPT-6 Astra (High) 98.5%/$0.284 — match.
- Top-3 by ARC-AGI-2 accuracy: Human Panel 100%/$17.00, GPT-6 Astra (Max)
  95.0%/$1.12, Claude Opus 5.5 (High) 93.3%/$0.408 — match.
- Top-3 by ARC-AGI-3 accuracy: GPT-6 Astra (High) 99.9%/$18.8K (Provider
  Adapter), GPT-6 Astra (Max) 98.6%/$17.3K (Provider Adapter), GPT-6 Astra
  (XHigh) 98.4%/$18.1K (Provider Adapter) — match.
- Additional spot checks: GPT-5.6 Sol (XHigh) all three versions
  (97.5%/$0.400, 90.0%/$1.04, 7.0%/$19.2K Standard), Kimi K3 (Max)
  (94.5%/$0.770, 60.4%/$1.59), Grok 4.6 (XHigh) all three versions
  (87.0%/$0.348, 67.1%/$0.757, 2.1%/$5.6K Standard), GLM-5.2 both versions
  (77.0%/$0.192, 22.8%/$0.250), Minimax M2.5 both versions (63.7%/$0.070,
  4.9%/$0.170), Deepseek V3.2 both versions (57.0%/$0.080, 4.0%/$0.120),
  Gemini 3.7 Flash (Low) both versions (85.2%/$0.042, 52.9%/$0.079) — all 16
  values match the raw capture exactly.

No mismatches found; no fixes were needed.

Added by coverage audit 2026-09-22.
