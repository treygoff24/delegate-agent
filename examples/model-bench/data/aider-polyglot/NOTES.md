# Aider Polyglot capture

Captured 2026-09-22 (retrieval timestamp in `rows.jsonl`: 2026-09-23T01:09:55Z). The current Aider leaderboard page was last updated November 20, 2025; the newest listed Polyglot run is dated 2025-10-03. The raw capture includes the rendered leaderboard HTML, the source YAML, Aider's benchmark metric/pricing notes, contribution instructions, and repository license under `examples/model-bench/raw/aider-polyglot/`.

## What it measures

Aider's Polyglot benchmark evaluates models editing code for 225 challenging Exercism problems across C++, Go, Java, JavaScript, Python, and Rust. The original benchmark description says the problems were selected from 697 exercises using results from seven models; problems solved by three or fewer of those models were selected. Runs use differing Aider versions and edit formats, recorded in each row's `harness` field.

## Rows and metrics

The raw data lists 69 model configurations and the leaderboard publishes two metrics for each, yielding 138 rows:

- `percent_completed_correctly`: source `pass_rate_2`, the percentage of benchmark tasks the model completed successfully. Values are copied directly as percentages.
- `percent_using_correct_edit_format`: source `percent_cases_well_formed`, the percentage of tasks where the model followed the requested edit format. Aider may provide feedback and ask for a corrected edit after an edit-format mistake.

The leaderboard pairs each completion score with the run's `total_cost` and `seconds_per_case`; those fields are attached to its completion row. `cost_basis` is `per_run`; `time_basis` is `per_task`. `n` is the source's `test_cases` count. There is no published confidence interval. Rows keep source model spelling verbatim; `model_id` is inferred from the recorded command/model label and may be approximate, especially for aliases and hosted-compatible endpoints. Reasoning effort/thinking tokens are filled only where the source states them.

## Caveats

These are community-contributed results listed by Aider, not independent re-runs verified by this collection. Aider's notes say run costs reflect provider prices at the time of the run and may be inaccurate or no longer current. Scores span Aider versions and edit formats, so they are not controlled comparisons of model weights alone. Results are stale as of this capture: the leaderboard page's update date is 2025-11-20 and the newest run is from 2025-10-03. The benchmark's public Exercism problems and difficulty selection based on model performance create plausible training-data contamination and selection effects; the source does not report a contamination audit. No listed result was omitted; all 69 configurations and both published leaderboard metrics were captured.

## License and redistribution

The Aider repository's `LICENSE.txt` identifies Apache License 2.0. The leaderboard does not state a separate license for the results data, so this records the repository license as context rather than asserting data-specific redistribution terms.

## Sources

- Leaderboard: https://aider.chat/docs/leaderboards/
- Raw result source: https://github.com/Aider-AI/aider/blob/main/aider/website/_data/polyglot_leaderboard.yml
- Metric and price definitions: https://aider.chat/docs/leaderboards/notes.html
- Polyglot benchmark design: https://aider.chat/2024/12/21/polyglot.html
- Contribution instructions: https://aider.chat/docs/leaderboards/contrib.html
- Repository license: https://github.com/Aider-AI/aider/blob/main/LICENSE.txt

## Verification

Verified 2026-09-22 against `raw/aider-polyglot/polyglot_leaderboard.yml`.
All 138 JSONL lines parse, and every row contains all 24 fields defined in
`SCHEMA.md`. Checked 10 rows; each value matches the corresponding source YAML
number (completion maps to `pass_rate_2`; edit-format maps to
`percent_cases_well_formed`):

- Codestral 25.01, 2025-01-13, `percent_using_correct_edit_format`: 100.0 — match (highest-score tie).
- DeepSeek R1 + claude-3-5-sonnet-20241022, 2025-01-23, `percent_using_correct_edit_format`: 100.0 — match (highest-score tie).
- Gemini 2.0 Pro exp-02-05, 2025-02-25, `percent_using_correct_edit_format`: 100.0 — match (highest-score tie).
- Gemini 2.0 Pro exp-02-05, 2025-02-25, `percent_completed_correctly`: 35.6 — match.
- gpt-4o-mini-2024-07-18, 2024-12-21, `percent_completed_correctly`: 3.6 — match.
- gpt-4o-mini-2024-07-18, 2024-12-21, `percent_using_correct_edit_format`: 100.0 — match.
- claude-3-5-sonnet-20241022, 2025-01-17, `percent_completed_correctly`: 51.6 — match.
- claude-3-5-sonnet-20241022, 2025-01-17, `percent_using_correct_edit_format`: 99.6 — match.
- gpt-4o-2024-11-20, 2024-12-30, `percent_completed_correctly`: 18.2 — match.
- gpt-4o-2024-11-20, 2024-12-30, `percent_using_correct_edit_format`: 95.1 — match.

Coverage: source YAML has 69 model/date configurations and 68 distinct model
labels (Qwen2.5-Coder-32B-Instruct appears in two configurations). Rows cover
all 69 configurations with both reported metrics, for 138 rows. The source
captures no model release dates, but all listed benchmark runs are dated no
later than 2025-10-03; none is a model entry from the month before this check.
No mismatches or missing/extra rows were found; no row changes were needed.
