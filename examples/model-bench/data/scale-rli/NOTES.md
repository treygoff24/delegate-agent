# Remote Labor Index (RLI, Scale Labs) results collection

added by coverage audit 2026-09-22

## What it measures and data version

The Remote Labor Index (RLI) empirically measures the capability of AI
agents to perform real-world, economically valuable remote work. It uses 240
self-contained projects sourced from 358 verified freelancers on Upwork,
spanning many sectors; each project bundles a brief, input files, a
professionally-accepted human deliverable, and economic data (the freelancer's
reported completion time and cost). Paper: https://scale.com/research/rli.

Scale defines four metrics for RLI: **Automation Rate** (% of projects where
a "reasonable client" would accept the AI's deliverable as at least as good
as the human standard — the primary success metric), **Elo Score** (pairwise
comparison score, human baseline fixed at 1,000), **Dollars Earned** (total
human-cost value of successfully completed projects), and **Cost Savings**
(% cost reduction versus the human price; a failed project counts as 0%
savings). All evaluation is manual, by trained experts on a purpose-built
platform, because RLI deliverables (3D models, video, CAD, code, etc.) are
not reliably auto-gradable.

## Collection method and coverage

Fetched https://labs.scale.com/leaderboard/rli with `curl -A 'Mozilla/5.0'`
and extracted the embedded `entries` array from the page's RSC flight
payload (same method as the other Scale Labs boards in this batch). Raw HTML
saved under `raw/scale-rli/rli.html`.

**Only Automation Rate is published in the leaderboard table captured here.**
The page states explicitly: "Score measures Automation Rate. There is no
confidence interval for Automation Rate." Elo Score, Dollars Earned, and Cost
Savings are defined in the methodology text but are not present as
per-model columns/arrays in this page's data payload (no alternate metric
array was found in the HTML; if the site exposes them, it is via a
client-side toggle calling an endpoint not captured by this static fetch).
`cost_usd` and `time_s` are therefore null on every row, even though the
underlying dataset does carry economic data per project.

18 model/agent configurations, one Automation Rate score each (18 rows).

## Freshness

Newest row: GPT 6 Astra and Fable 5.1, both dated 2026-09-17.
`last_updated` = **2026-09-17**. No explicit "Updated <date>" text was found
on this page. 2026-frontier models present: GPT 6 Astra (2026-09-17), Fable
5.1 (2026-09-17), Gemini 3.7 Flash (2026-08-24). This board is live and
current.

## Metrics and row interpretation

- `metric` = `automation_rate`, `unit` = `%`, `higher_is_better` = true.
- `ci` = **null on every row**, not 0. The raw JSON field
  `confidenceInterval_upper` is literally `0` for every entry, but the page's
  own text says "There is no confidence interval for Automation Rate" — a
  stated absence, not a measured zero-width interval — so null more
  accurately represents "not reported" per SCHEMA.md's rule that a missing
  field is null.
- `measured_on` = the row's `createdAt` date.
- Two rows tagged `company: "deepseek"` are the "Manus" family
  (`Manus_1.6 (Max)`, `Manus 1.5`, `Manus 1.0`). Manus is publicly known as a
  product of Monica/Butterfly Effect, not DeepSeek; Scale's `company` tag for
  these three rows appears to be an error on Scale's side. `model_id` for
  these rows uses `unknown/manus-*` rather than trusting the stated vendor.
- `effort`/`harness`: parsed from suffixes where present, e.g. "(Max)",
  "(CoWork)", "-thinking", "(medium)"/"(default)" (the two `gpt-5.2-2025-12-11`
  rows are the same base model at two different settings, one row each, per
  SCHEMA's one-row-per-effort rule).
- `contaminationMessage` was empty for every row; `notes` is null throughout.

## Caveats and license

Footer states only "All rights reserved"; no separate redistribution terms
given for the leaderboard results. `license` is null on every row. Because
only Automation Rate is captured, this data cannot support the RLI paper's
Pareto-style cost/quality comparisons on its own — Dollars Earned and Cost
Savings would need a separate capture if the site exposes them elsewhere.

## Verification

Spot-checked 10 of 18 rows (top 3 by Automation Rate plus 7 more spread
across the ranking) by independently re-extracting `score`,
`confidenceInterval_upper`, and `createdAt` from the raw HTML with a fresh
regex (separate from the JSON-parsing extraction script), matched by exact
model name. All 10 matched on score and date; `confidenceInterval_upper` in
the raw JSON was 0 in all 10 as expected, consistent with this file's rows
carrying `ci: null` by design (see above). Also ran a full-file schema/parse
check confirming all 18 lines parse as JSON with every SCHEMA.md field
present.
