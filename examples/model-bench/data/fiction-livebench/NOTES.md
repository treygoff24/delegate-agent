# fiction-livebench — Fiction.LiveBench for Long Context Deep Comprehension

## What it measures

Long-context comprehension of fiction, using questions that require actually
reading and understanding subtext rather than searching/retrieving a fact
(explicitly contrasted with "needle in a haystack" tests, which the source
says test search rather than comprehension). Source material comes from a
dozen very long, complex stories on fiction.live plus verified quizzes about
them; each story is cut down to progressively shorter versions retaining
only the information needed to answer, producing a "0"-token (minimal,
easiest) test up through longer contexts where the relevant information is
diluted among more surrounding material. The percentage score at each
context-length bucket is the model's accuracy on that bucket's question set.
Context-length buckets published: 0, 400, 1k, 2k, 4k, 8k, 16k, 32k, 60k,
120k, 192k (tokens); recorded in `subset` per SCHEMA.md's "e.g. verified,
hard, overall" convention (e.g. `"32k_tokens"`).

The benchmark dataset itself is private (per the page: "The actual dataset
comes from our users and will remain private"); an 8k-difficulty example
question and its easier 1k variant are linked from the page as
illustrations (both hosted as public GitHub gists, not fetched here since
they are illustrative, not part of the scored data).

## IMPORTANT: source is a static PNG image, not machine-readable text/JSON

`https://fiction.live/stories/Fiction-liveBench` (the brief's URL) does not
resolve directly — it is a fiction.live "story" post identified by a fixed
ID (`oQdzQvKHw8JyXbN87`) whose title/URL slug is renamed on every benchmark
refresh (e.g. `Fiction-liveBench-April-04-2026`). The page itself is a
heavily client-rendered AngularJS SPA; `firecrawl scrape` (JS rendering)
was tried and returned the page's prose/changelog/comment text but **no
structured table** — because the actual results table is not HTML/JSON at
all, it is **posted as a single PNG chart image**
(`https://cdn6.fiction.live/file/fictionlive/a7be0188-9a0a-4b19-a4b3-97e7deecf52e.png`,
2340x1714px) embedded in the story body.

Given no JSON/CSV/HTML table exists anywhere in the source (confirmed via
the story's underlying JSON API, `api/node/<id>` for metadata and
`api/anonkun/chapters/<id>` for the full chapter body — both fetched and
saved under `raw/fiction-livebench/`), the table was **read directly from
the image** by visual inspection (the image contains sharp, legible
digital text/numerals, not a smooth heatmap without labels — every cell is
an exact printed percentage). To guard against transcription error, the
image was read **twice independently**: once as the full image, and once
as two separate 2x-upscaled crops (top and bottom half of the table,
`crop_top.png` / `crop_bottom.png`, plus a third crop of just the right-hand
60k/120k/192k columns, `crop_right.png`, re-checked against all 19 rows).
All three passes produced identical values for every cell — see
Verification below.

## Version / date captured

- The captured snapshot's title/URL slug and its own changelog's last entry
  both read **April 4, 2026** (`04/04/2026 - Added qwen3.6-plus [reasoning:
  high] [alibaba]`), so `benchmark_version`/`measured_on` = **2026-04-04**.
- **Staleness flag:** this is the most recent Fiction.liveBench snapshot
  found (confirmed via web search across third-party mirrors — llmlearner,
  datalearner, benchleader, epoch.ai — none showed a newer fiction.live
  snapshot date than April 2026), but it is **~5.5 months old** relative to
  capture date (2026-09-23) and does **not** include any of the very latest
  named-in-brief frontier models (no GPT-6 Sol/Luna/Astra, no Claude Opus
  5.5/Fable 5.1, no Grok 4.5+/4.7, no Gemini 3.5/3.6/3.8-flash, no DeepSeek
  V4/V4.1, no GLM-5.1/5.2/5.3, no Kimi K3, no Qwen 3.8, no MiniMax M3, no
  Muse Spark). It does contain multiple **2026** frontier-tier models as of
  its own refresh (`gpt-5.2`/`gpt-5.2-pro`, `claude-opus-4-6`,
  `claude-opus-4-5`, `claude-sonnet-4-5`, `gemini-3-pro-preview`,
  `gemini-3-flash-preview`, `glm-5`, `deepseek-v3.2`, `qwen3.6-plus`,
  `minimax-m2.5`, `kimi-k2.5`), so per the brief's literal test ("stale = no
  2026 frontier models") this was **collected, not skipped** — but flagged
  here as **partially stale**: the benchmark appears to not have been
  refreshed by its maintainer since April 2026, five and a half months
  behind the absolute current frontier. `status: partial` is reported for
  this reason (fully captured, but the underlying source itself is behind).
- Retrieved: 2026-09-23T01:34:28Z.

## How it was captured

1. Located the story ID via web search (`exa-agent search`), since the
   brief's bare URL doesn't resolve to a specific story.
2. `curl -A 'Mozilla/5.0' https://fiction.live/api/node/oQdzQvKHw8JyXbN87`
   — story metadata confirming the title/date
   (`raw/fiction-livebench/current.html`'s companion JSON not saved
   separately but reproduced by this call; see also `page.html` and
   `listing.html` from earlier resolution attempts).
3. `curl -A 'Mozilla/5.0' https://fiction.live/api/anonkun/chapters/oQdzQvKHw8JyXbN87`
   — full chapter body (saved as `raw/fiction-livebench/chapters.json`),
   containing the methodology prose, changelog, and the image URL.
4. `firecrawl scrape` on the rendered story page as a second, independent
   check that no structured table exists client-side either (saved as
   `raw/fiction-livebench/firecrawl_out.json`).
5. Downloaded the results image itself
   (`raw/fiction-livebench/leaderboard_image.png`) and read it twice
   independently (full image, then 2x-upscaled crops) to transcribe all 19
   models x 11 context-length columns.

19 models, up to 11 context-length buckets each; 9 (model, context-length)
cells were blank or `-` in the source (not tested at that length) and were
**omitted as rows** rather than fabricated with a null value, per
SCHEMA.md's "one line per ... metric the source reports" — a cell the
source never populated is not a reported measurement. 19 x 11 = 209
possible cells - 9 missing = **200 rows** in `rows.jsonl`.

## Model-name handling

`model_raw` keeps the source's full annotated string verbatim, including
bracketed reasoning-effort and inference-provider/quantization tags (e.g.
`"deepseek-v3.2 [reasoning: high] [deepseek]"`,
`"minimax-m2.5 [reasoning: high] [minimax/fp8]"`,
`"qwen3-235b-a22b-thinking-2507 [chutes]"`). `effort` is parsed out of the
`[reasoning: high]` tag where present (`null` otherwise). Provider/
quantization annotations that aren't a reasoning-effort dial (`[deepseek]`,
`[z-ai]`, `[moonshotai]`, `[alibaba]`, `[chutes]`, `[minimax/fp8]`,
`:free`) are dropped from `model_id` and instead noted per-row where they
carry meaningful information (e.g. fp8 quantization, a free-tier inference
route, or the Chutes inference provider) — see each row's `notes`.

Three Claude rows (`claude-opus-4-6`, `claude-opus-4-5`,
`claude-sonnet-4-5`) show an exact **0.0** at the 192k column, a sharp drop
from 75-94% at 120k. This is copied faithfully from the source with no
correction — plausibly a total failure/timeout/refusal at the longest
context length, but this cannot be verified from the leaderboard image
alone (no per-item detail is available), so it is flagged in each row's
`notes` rather than silently treated as a normal score or excluded.

## Reporter / license

- `reporter`: `independent` — Fiction.live is a fiction-writing/roleplay
  platform running this benchmark on its own users' story corpus, not a
  model vendor.
- License: not stated anywhere on the page or in the fetched API responses;
  the underlying test dataset is explicitly stated to be private/
  unpublished. Recorded `null`.

## What could not be captured

- Per-question / per-item results are not published, only the aggregated
  accuracy per (model, context-length) cell.
- `n` (number of questions behind each percentage) is not stated per cell;
  the page mentions the dataset overall spans "a dozen very long complex
  stories and many verified quizzes" but gives no per-cell item count.
  Recorded `null`.
- `ci`, `cost_usd`, `time_s`: not published.
- `harness`: not applicable/stated; provider/routing annotations (e.g.
  "[chutes]", ":free") are captured in `notes` instead, since `harness`
  means eval scaffold, not inference routing.
- A newer (post-April-2026) refresh of this benchmark, if the maintainer
  has since posted one under a different story-title slug not surfaced by
  this search pass — flagged as an open gap for the lead, not something
  this collection could resolve with the tools available.

## Verification

Because this source publishes its results as a rendered image rather than
text, "re-extraction from raw via a separate script" (the literal
instruction) is not applicable in the usual grep/jq sense. Instead, **every
one of the 19 models (all 200 rows, not just a 10-row sample)** was
cross-checked by reading the source image through two independent visual
passes:

1. A full-image read of `raw/fiction-livebench/leaderboard_image.png` at
   its native rendered size.
2. A second, independent read of three separate 2x-upscaled crops
   (`crop_top.png`, `crop_bottom.png`, `crop_right.png` — the last isolating
   just the 60k/120k/192k columns for all 19 rows as a targeted
   re-verification of the columns most likely to contain transcription
   errors, including the three 0.0 outliers).

All three passes produced **identical values for every cell**, including
the unusual ones double-checked deliberately (the `glm-5` 2k value of 97.1,
which breaks the table's otherwise-common ".2/.7/.8/.9" ending pattern, and
the three Claude 192k = 0.0 outliers). No mismatches found across any pass;
no fixes needed. Also validated with a small Python script that all 200
lines of `rows.jsonl` parse as JSON with every SCHEMA.md field present.

Added by coverage audit 2026-09-22.
