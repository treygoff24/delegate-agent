# PRBench Legal / Finance (Scale Labs) results collection

added by coverage audit 2026-09-22

## What it measures and data version

Professional Reasoning Benchmark (PRBench) evaluates LLMs on high-stakes
professional reasoning in Finance and Law, using 1,100 real questions
co-designed with 182 domain experts (JDs for Law; CFAs/Master's/6+ years
experience for Finance) — 600 Finance questions, 500 Legal questions. Each
task has an expert-curated rubric of 10-30 weighted criteria (scored -10 to
+10) that penalizes harmful/incorrect advice and rewards high-quality, safe
responses. An LLM judge (o4 Mini) scores responses against each rubric
criterion, producing a final weighted score clipped to 0-1 (displayed here as
a percentage). Paper: https://scale.com/research/prbench. Judge validation:
80.2% agreement with human experts (vs. 79.6% human-human agreement).

The benchmark also defines a separate, harder 550-question "Hard Subset"
(300 Finance / 250 Legal); the page's own text says top models "fail to
breach a 0.40 score" on that subset. The scores captured here (top score
~61.6 Legal / ~59.5 Finance) are well above 0.40, confirming the leaderboard
pages fetched (`prbench-legal`, `prbench-finance`) show the **Full Dataset**
result, not the Hard Subset. The Hard Subset numbers were not captured.

## Collection method and coverage

Per the collection brief, `subset` is used here to mean **domain** (`legal`
vs `finance`), matching the two separate leaderboard pages, not the
Full/Hard split described above.

Fetched both pages with `curl -A 'Mozilla/5.0'`:
- https://labs.scale.com/leaderboard/prbench-legal -> `raw/scale-prbench/prbench-legal.html`
- https://labs.scale.com/leaderboard/prbench-finance -> `raw/scale-prbench/prbench-finance.html`

and extracted each page's embedded `entries` array (RSC flight payload,
same method as the other Scale Labs boards in this batch). Legal: 35 model
configurations (35 rows). Finance: 35 model configurations (35 rows). Total
70 rows across 35 distinct `model_id`s (nearly the same model roster was
evaluated on both domains).

## Freshness

Newest row on both pages: Muse Spark 1.3, dated 2026-09-14. `last_updated` =
**2026-09-14** for both subsets. No explicit "Updated <date>" text was found
on either page. 2026-frontier models present on both boards: Muse Spark 1.3
(2026-09-14), Fable 5.1 (2026-09-03), Gemini 3.8 Flash (2026-09-09), GPT 6
Astra (2026-09-09), gpt-5.6-sol (2026-07-28). This board is live and current.

## Metrics and row interpretation

- `metric` = `weighted_score` (the page's own rubric-weighted, 0-1-clipped
  score, copied here as a percentage point value matching the page's
  display, e.g. 61.56), `unit` = `%`, `higher_is_better` = true.
- `category` and `subset` are `legal` for rows from the Legal page and
  `finance` for rows from the Finance page, per the collection brief.
- `ci` = the JSON `confidenceInterval_upper` field (half-width, page renders
  "score ± ci").
- `measured_on` = the row's `createdAt` date.
- `effort`/`harness`: parsed from suffixes where present (e.g. "(max)",
  "(High)", "-thinking", "(Non-Thinking)"). No PRBench row states a
  separate agent harness from the model itself.
- `contaminationMessage` was empty for every row on both pages; `notes` is
  null throughout.
- Several rows carry high-precision (non-rounded) scores, e.g.
  `48.96127165`; these are copied verbatim from the source JSON, not
  rounded.

## Caveats and license

Footer states only "All rights reserved"; no separate redistribution terms
are stated for the leaderboard's numeric results. `license` is null on every
row. The Hard Subset scores mentioned in the page's methodology text are not
captured — only the Full Dataset numbers shown by default on
`prbench-legal`/`prbench-finance` are in rows.jsonl.

## Verification

Spot-checked 10 of 70 rows (top 3 by `weighted_score` across both files
combined, plus 7 more spread across both files) by independently
re-extracting `score`, `confidenceInterval_upper`, and `createdAt` from the
matching raw HTML file (legal vs finance, by `subset`) with a fresh regex
(separate code path from the JSON-parsing extraction script), matched by
exact model name. All 10 matched exactly on score, CI, and date. Also ran a
full-file schema/parse check confirming all 70 lines parse as JSON with
every SCHEMA.md field present.
