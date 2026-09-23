# MCP Atlas (Scale Labs) results collection

added by coverage audit 2026-09-22

## What it measures and data version

MCP Atlas benchmarks how well language models handle real-world tool use
through the Model Context Protocol (MCP). It targets realistic, multi-step
workflows — discovering the right tool from a noisy tool menu, calling tools
with correct parameters, recovering from errors, and synthesizing tool
outputs into a final answer — rather than simple function calling or small
simulated tool sets. Per the page's "Key Metrics at a Glance": 1,000 tasks
(500 public + 500 private), 36 MCP servers, 220 tools, 3-6 tool calls per
task.

The Scale Research team has published a paper
(https://scale.com/research/mcpatlas), a HuggingFace dataset
(https://huggingface.co/datasets/ScaleAI/MCP-Atlas), and a GitHub code
repository (https://github.com/scaleapi/mcp-atlas/tree/main).

## Collection method and coverage

Fetched the server-rendered leaderboard page
(https://labs.scale.com/leaderboard/mcp_atlas) with `curl -A 'Mozilla/5.0'`
and extracted the leaderboard's `entries` array, which is embedded (JSON,
double-escaped) in the page's Next.js RSC flight payload. Saved the raw HTML
under `raw/scale-mcp-atlas/mcp_atlas.html`.

The page has a single leaderboard table (no subset toggle): 34 model
configurations, one score each (34 rows).

## Freshness

The leaderboard's newest entry (Muse Spark family aside, Fable 5.1) is dated
2026-09-04, and the newest addition overall is Qwen3.8-2.4T-A95B (xHigh) and
GLM 5.3, both dated 2026-09-17. `last_updated` in the manifest is set to
**2026-09-17** (the newest `createdAt` among the captured rows).

The page also carries a static methodology note reading verbatim "Updated
April 8, 2026" — but the surrounding paragraph identifies this as the date
Scale revised the *evaluation methodology* (upgraded the scoring judge, added
retry handling for transient tool errors, replaced the 20-turn limit with a
100-tool-call budget, and re-scored all leaderboard models), not the date of
the newest score. Multiple rows are dated well after April 8, 2026 (up to
2026-09-17), confirming the board keeps receiving new model runs after that
methodology revision. I used the newest row date rather than this
methodology-note date for `last_updated` so the manifest reflects actual data
freshness; both dates are recorded here for transparency.

Current 2026-frontier models present: Fable 5.1 (2026-09-04), GLM 5.3
(2026-09-17), Qwen3.8-2.4T-A95B (2026-09-17), GPT-5.6 (sol) (2026-07-15),
Nemotron 3 Ultra (thinking) (2026-09-17), Muse Spark 1.1 (2026-07-09). No
GPT-6 Astra/Luna/Sol, Claude Opus 5.5, Grok 4.7, Gemini 3.8 Flash, DeepSeek
V4.1, Kimi K3, or MiniMax M3 rows are present as of capture, so the very
latest (Sept 2026) frontier wave has not yet been evaluated on this board.

## Metrics and row interpretation

- `metric` = `pass_rate` (the page's own label is "Top Pass Rate"), `unit` =
  `%`, `higher_is_better` = true. The raw JSON `score` field is copied
  directly (already in the page's native percentage-point scale, e.g.
  88.1, not 0-1).
- `ci` = the JSON `confidenceInterval_upper` field, which the page renders as
  "score ± ci" (half-width of the confidence interval).
- The JSON also carries a `maxScore` field per entry (e.g. 92.7515); this is
  a fixed per-benchmark ceiling reference used only to size the leaderboard's
  progress bar (bar width % = score / maxScore), not a per-model quantity, so
  it is not copied into rows.jsonl.
- `measured_on` = the row's `createdAt` date.
- `effort`/`harness`: parsed from parenthetical/suffix qualifiers in the
  model name where present (e.g. "(xHigh)", "(max)", "(thinking)"). No MCP
  Atlas row states an agent harness distinct from the model itself.
- `cost_usd`/`time_s`: not published on this leaderboard; null on every row.
- `contaminationMessage` was empty for every MCP Atlas row (no contamination
  warnings), so `notes` is null throughout.

## Caveats and license

The footer states only "All rights reserved"; no separate redistribution
terms are stated for the leaderboard's numeric results. The linked
HuggingFace dataset and GitHub repo may carry their own terms, not checked
here. `license` is null on every row.

## Cross-check: HLE / SWE-bench Pro freshness on Scale's own boards

Per the collection brief, `humanitys_last_exam*` and `swe_bench_pro_public_v2`
are out of scope for this lane (covered by the existing `hle` and
`swe-bench-pro` data dirs), but I checked whether Scale's own hosted boards
for those two carry newer models than what those other lanes hold, since I
was already fetching Scale pages.

- Scale's HLE page (`humanitys_last_exam`), per the existing `data/hle/`
  capture in this repo (captured 2026-09-22/23), already includes GPT-6
  Astra (2026-09-09) and Fable 5.1 (2026-09-03) — i.e. it is current with
  the same September 2026 frontier wave seen on the boards collected here.
  I did not re-fetch it since it is out of scope; this is a read of the
  already-collected `data/hle/rows.jsonl` in this repo, not a fresh capture.
- I did not independently re-check Scale's `swe_bench_pro_public_v2` board
  in this pass (out of scope and not fetched); no claim is made about its
  freshness relative to the existing `swe-bench-pro` lane.

## Verification

Spot-checked 10 of 34 rows (the top 3 by `pass_rate` plus 7 more spread
across the ranking) by independently re-extracting `score`,
`confidenceInterval_upper`, and `createdAt` from the raw HTML with a fresh
regex search (separate code path from the JSON-parsing script used to build
rows.jsonl — see scratchpad `spotcheck.py`), matched by exact model name.
All 10 matched exactly (score, CI, and date) with no mismatches. Also ran a
full-file schema/parse check confirming all 34 lines parse as JSON with every
SCHEMA.md field present.
