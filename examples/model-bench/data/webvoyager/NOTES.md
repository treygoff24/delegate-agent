# WebVoyager results

## What it measures

WebVoyager evaluates browser-control agents on web tasks. These results are author-run reproductions using the browser-control harness, with final outcomes judged from the task instruction, response, and screenshots under the GPT-5.5 Alumnium vanilla `SUCCESS` / `NOT SUCCESS` policy.

## Captured data

- Latest repository results package: 2026-06-17; latest score row is Opus 4.8, generated 2026-06-17.
- Two evaluated model configurations and one headline metric per configuration (two rows). The repository's Current Results table lists fable-5 and Opus 4.8; no other language-model score is presented as a current headline result.
- Both headline scores use 615 included tasks: the original 643-task local set, minus 24 Alumnium removals and four current impossible exclusions.
- `pass_rate` is reported as a fraction in `summary.json` and as a percentage in the headline table. Values are represented as the published percentages: 0.9919 -> 99.19%, 0.9041 -> 90.41%.
- No per-task cost, elapsed time, confidence interval, or reasoning-effort setting is published with these headline results; those fields are null.
- Rows use `ui-web`, `browser-control` as the harness, and the manifest's independent reporter classification. The evaluation judge is GPT-5.5; it is not counted as a scored model configuration.

## Retrieval and raw files

Retrieved directly from the public GitHub repository on 2026-09-23 UTC using its GitHub API and raw-file endpoints. The raw directory preserves repository metadata, the main commit metadata, the project README and task update notes, the benchmark documentation and task data layers, both run summaries and per-task result bundles, and both published reports. The compact summary JSON files and README are the source for the rows; the full result bundles and reports support audit.

Sources: https://github.com/omxyz/webvoyager and https://github.com/omxyz/webvoyager/tree/main/results

## Caveats and redistribution

These are independent reproduction results, not vendor leaderboard claims. The authors warn that live-site tasks are time-sensitive and that anti-automation defenses (including reCAPTCHA, Cloudflare, and Amazon cart blocks) can cause failures. The fable-5 run had one missing screenshot and the authors say fable-5 is gone and cannot be reproduced as-is. Opus 4.8 used a model-specific task layer with 47 prompt edits and 10 reference-answer updates. The authors explicitly state that the headline scores are not strictly apples-to-apples. Task removals and edits also mean these are not directly comparable to the original WebVoyager score.

The GitHub repository API reports no license, and no LICENSE file is present in the repository tree captured for this collection. Redistribution terms are therefore unknown; source files are preserved here for audit, not as a claim of licensed redistribution.

No additional language-model headline results, effort variants, paired costs, or paired times were available in the published Current Results section at retrieval. The repository may contain result artifacts for the two listed model runs, but those are per-task details underlying the same headline metrics, not extra model configurations or metrics.

## Verification

- Checked both rows (the complete two-row file), including the highest-scoring rows. Both parse as JSON and contain all 24 fields defined for rows in `examples/model-bench/SCHEMA.md`.
- Matched `fable-5` 99.19% (610/615) and `Opus 4.8` 90.41% (556/615) to the captured reports and the source repository's Current Results table. The captured raw folder does not contain the referenced `summary.json` files; the report tables and live README provide the matching totals and percentages.
- The source Current Results table lists two scored models, matching the two rows. Both reported runs are dated June 2026, so neither is a model release from the month before this check (2026-08-22 through 2026-09-22).
- No mismatches or missing source rows were found; no row changes were needed.
