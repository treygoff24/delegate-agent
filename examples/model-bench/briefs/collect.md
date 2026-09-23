PRE-APPROVED: this is a data-collection task; do it now without asking for design confirmation. A workspace claim held by the model-bench coordinator (for example in `fleet`) covers this run: write your owned paths anyway.

# Task: collect one benchmark's current results

Today is {DATE}. You own exactly one benchmark:

```json
{ITEM}
```

Pull every current result this benchmark publishes for language models, and
save it in the shared format so it can be combined with other benchmarks
without any cleanup.

## Steps

1. Read `examples/model-bench/SCHEMA.md` (the row format and rules).
2. Find the freshest data. Prefer the raw source (`data_url`, a GitHub repo, a
   CSV, or the JSON endpoint the leaderboard page loads) over scraping rendered
   HTML. Use web search, `curl`, and, when a page is JavaScript-rendered or
   bot-walled, the `firecrawl` or `exa-agent` shell tools if available.
3. Save what you downloaded under `examples/model-bench/raw/{ID}/` (the raw
   file, or the page text you read) so every number can be checked later.
4. Write `examples/model-bench/data/{ID}/rows.jsonl`: one line per model
   configuration and metric, following SCHEMA.md exactly. Include every model
   the source lists, not a sample. Where the source pairs a score with dollar
   cost or time, put them on the same row. One row per reasoning-effort setting
   when the source distinguishes them.
5. Write `examples/model-bench/data/{ID}/NOTES.md`: what the benchmark measures,
   the version and date of the data you captured, how you got it, the metric
   definitions, caveats (vendor-reported, contamination concerns, stale
   entries), the license or redistribution terms if stated, and anything you
   could not capture.

## Rules

- Copy numbers exactly; never estimate, interpolate, or fill a gap from memory.
  Unknown fields are null.
- `model_raw` is verbatim from the source. `model_id` is your best canonical
  guess (`vendor/model`, lowercase); use `unknown/<slug>` when unsure.
- Write only inside `examples/model-bench/data/{ID}/` and
  `examples/model-bench/raw/{ID}/`. Do not edit any other file. Do not commit.
- If the benchmark is unreachable or has no usable data, still write NOTES.md
  explaining why, and leave rows.jsonl absent.

## Done when

Every line of rows.jsonl parses as JSON with every schema field present, the
row count matches the number of model configurations times metrics the source
shows (say so in NOTES.md if it differs, and why), and the raw capture exists.

Final answer: a single JSON object, nothing else:
{"benchmark_id": "{ID}", "status": "ok" | "partial" | "blocked", "rows": <int>, "models": <int>, "data_date": "<YYYY-MM-DD or null>", "summary": "<one sentence>"}
