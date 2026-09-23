PRE-APPROVED: this is a verification task; do it now without asking for design confirmation.

# Task: spot-check one benchmark's collected rows

Today is {DATE}. Another lane collected benchmark `{ID}` into
`examples/model-bench/data/{ID}/rows.jsonl`, with its source captures under
`examples/model-bench/raw/{ID}/` and a note in `data/{ID}/NOTES.md`. Its report:

```json
{REPORT}
```

The collection is a claim. Check it.

1. Read `examples/model-bench/SCHEMA.md`.
2. Confirm every line of rows.jsonl parses and carries every schema field.
3. Pick at least 10 rows (all of them if fewer), always including the three
   highest-scoring rows and any row for a model released in the last month.
   For each, find the same number in the raw capture or, if the capture lacks
   it, on the live source. Record match or mismatch.
4. Check coverage: count the models the source lists and compare with the rows.
5. Fix what you can prove wrong: correct a mismatched value, add rows the
   collector missed, delete rows that are not in the source. Edit only
   `examples/model-bench/data/{ID}/rows.jsonl`, and append a `## Verification`
   section to `data/{ID}/NOTES.md` listing what you checked and changed.
   Do not commit.

Final answer: a single JSON object, nothing else:
{"benchmark_id": "{ID}", "verdict": "pass" | "fixed" | "fail", "checked": <int>, "mismatches": <int>, "fixed": <int>, "coverage_ok": true | false, "summary": "<one sentence>"}
