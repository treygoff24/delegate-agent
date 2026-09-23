# GDPval results

GDPval measures performance on economically valuable, real-world knowledge-work deliverables across 44 occupations and 9 U.S. industries. The first released version has 1,320 tasks overall and a 220-task open gold subset. Expert graders make blinded comparisons between AI deliverables and expert-produced work, classifying model output as better, as good as, or worse. The current rows use the latest direct GDPval model comparison found: the `GDPval (wins or ties)` table in OpenAI's GPT-5.5 release post, published 2026-04-23. Six named configurations are listed, so there are six rows (one metric per configuration) and six distinct models/configurations. The table does not specify effort settings or a benchmark subset/version; those fields are left null.

## Metric

`win_or_tie_rate` is the percentage of comparisons where the model output was rated better than or as good as the industry professional deliverable. Higher is better. Values and percent units are copied as published. The release table gives no paired API cost or inference time, so these fields are null. OpenAI's original launch post reports general speed/cost comparisons to human experts but not per-model paired values for the six current rows; those figures are not attached to these rows.

## Collection and source status

Retrieved 2026-09-23T01:19:40Z (2026-09-22 in EDT). The freshest direct GDPval result table found in official OpenAI sources was `Introducing GPT-5.5`, dated 2026-04-23. Its table reports, in column order, GPT-5.5 84.9%, GPT-5.4 83.0%, GPT-5.5 Pro 82.3%, GPT-5.4 Pro 82.0%, Claude Opus 4.7 80.3%, and Gemini 3.1 Pro 67.3%. See the archived extraction in `../raw/gdpval/gpt-5-5-release-extract.txt`.

The OpenAI Evals data site is JS-rendered. Curl retrieved its app shell but not the hydrated data; the official leaderboard search result currently states that the OpenAI-hosted GDPval leaderboard is no longer active. Its fetched shell and a source note are saved under `../raw/gdpval/`. No active downloadable current result file was found. The full GDPval paper (arXiv:2510.04374v1) was downloaded as `../raw/gdpval/paper.html`; it documents the earlier 2025 experimental run, not the latest six-model release table. The official GPT-5.5 post's extracted table was used as the numeric source.

## Caveats and terms

These are vendor-reported results, not independently reproduced here. GDPval tasks are one-shot and well specified, and do not fully represent interactive work or human oversight. The 2025 paper notes possible model-identification cues despite blinded grading. It also reports the original benchmark results and historical effort/scaffolding experiments, but those are an older evaluation release and are not mixed into the latest cross-model comparison rows. Scores and benchmark versions may be stale relative to unpublished internal or future releases. OpenAI/arXiv states CC BY 4.0 for the paper; no separate terms for the live Evals service were verified. The user-supplied manifest also specifies CC-BY-4.0.

## Verification

On 2026-09-22, parsed all six lines in `rows.jsonl` and confirmed that every
row contains all fields listed in `SCHEMA.md`. Checked all six score/model
pairs against both the archived extraction and the live OpenAI release table;
the required three highest-scoring rows were included. The source table lists
six configurations and the file has six corresponding rows and six distinct
model IDs. None of the listed models was released during the month before
verification (the source post is dated 2026-04-23; the compared Claude Opus
4.7 and Gemini 3.1 Pro releases are also dated earlier than that month).
Found no mismatches, so no rows were changed.
