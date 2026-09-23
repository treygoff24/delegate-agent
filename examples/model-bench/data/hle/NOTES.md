# HLE results collection

## What it measures and data version

Humanity's Last Exam (HLE) is a difficult academic question-answering benchmark built from expert-authored questions across mathematics, the natural sciences, humanities, and other fields. The current finalized HLE version contains 2,500 questions and includes multimodal items. Scale's current page distinguishes it from the earlier `HLE-preview` board. This capture does not include the separate HLE-Rolling fork mentioned on the Scale page.

Captured on 2026-09-22 local time (retrieval timestamp in rows: 2026-09-23T01:20:09Z). The newest Scale result record is dated 2026-09-09. The Hugging Face dataset metadata revision captured was `5a81a4c7271a2a2a312b9a690f0c2fde837e4c29`. The Zoom AI tools Space revision was `0cbe9ae3448216898de9c0016ea06b2f257e4fb3`.

## Collection method and coverage

Saved the complete server-rendered Scale Labs HLE and HLE Text Only leaderboard pages, along with the Hugging Face dataset card/API metadata. The model score arrays are embedded in those saved page responses. Saved the current Zoom AI Hugging Face Space page, its `app.py`, its evaluation-reader source, README, and Space metadata. The Space's static data gives both its Full Set and Text-Only tables and links each published score to a report when one is available.

Coverage and row counts:

- Scale Labs full-set board: 53 model configurations, each with accuracy and calibration error (106 rows).
- Scale Labs text-only board: 63 model configurations, each with accuracy and calibration error (126 rows).
- Zoom AI community tool-enabled full-set table: 39 model/agent configurations (39 rows).
- Zoom AI community tool-enabled text-only table: 30 model/agent configurations (30 rows).
- Total: 185 source-configuration/slice entries and 301 metric rows. A configuration appearing in two slices is counted once per slice because it has a separate result; the Scale source publishes two metrics per entry and the tool-enabled source publishes one. Non-numeric `-` placeholders in the tool table mean no result was published and were omitted.

Scale's entries had no `deprecated` flags set at capture. Its full-set page is the multimodal-inclusive overall slice; its separate text-only page excludes multimodal content. The Scale rows do not state tool access per model, so `harness` is left null rather than inferring a no-tools setting. The Zoom AI table explicitly says all listed agents/models have tool-use capabilities. It is a community compilation of scores from official agent/model reports, not a common re-run. The two publishers' measurements are therefore kept in distinct subset and reporter fields.

The scope is the current Scale HLE boards plus the explicitly tool-enabled community tables. The older fixed publication table on the CAIS site is superseded by Scale's current board. Artificial Analysis also publishes its own text-only evaluations; those are third-party evaluations rather than HLE/Scale board entries and are not merged into this collection.

## Metrics and row interpretation

- `accuracy` copies the source accuracy/score value in percentage points; higher is better. Scale's `confidenceInterval_upper` is copied to `ci` because its page renders that number as the half-width of the 95% confidence interval (`±x`). The tool-enabled table publishes no confidence intervals.
- `calibration_error` is Scale's RMS calibration error, copied in percent; lower is better. Its CI is not supplied as a separate value.
- `measured_on` uses Scale's row `createdAt` date. For the community table it uses the listed model `Publish Date`, since that table does not supply an evaluation-run date. Cost, time, and sample size are not published in these captured tables, so those fields are null.
- `effort` is populated only where a configuration suffix states a thinking/reasoning setting. `model_raw` preserves the source label, including whitespace and source markers; `model_id` is a lowercase vendor/model guess.
- HLE has reasoning, science, and multimodal subject matter, but the row schema allows one category; all rows use `reasoning`. The overall/full-set versus text-only distinction is carried in `subset`.

## Caveats, age, and licensing

Scale's page carries per-model contamination notices; those are copied into `notes` on both metric rows for the affected model. Its methodology notes that the exam was publicly released and describes removing searchable items. The community tool table says a checkmark means it found a filtering mention, not that filtering was perfect; absence of the mark does not establish that no filtering was used. Tool-table results are based on each model/agent's official report, with harness details and uncertainty generally absent. Its newest full-set publish date is 2026-07-15 and newest text-only publish date is 2026-06-16, so those entries are older than the Scale capture.

The Hugging Face dataset card states MIT. That license is for the dataset; the captured Scale leaderboard does not state a separate license for result data. The Zoom Space repository declares Apache-2.0, but it does not establish redistribution terms for the linked third-party score reports. Row `license` is therefore null for leaderboard results.

The Hugging Face API metadata and README were reachable, but a direct request for `eval.yaml` returned HTTP 401 stating that the dataset is restricted and requires authenticated access. The response is saved under `raw/hle/`; no question parquet or per-question predictions were downloaded. The captured leaderboards provide aggregate results only: no domain-level breakdowns, per-question results, costs, elapsed times, or separate image-only scores were available in these sources. The Scale page footer says “All rights reserved”; no additional redistribution terms for its leaderboard results were stated.

## Verification

Spot-checked the collected values against the captured source tables on 2026-09-22. All 301 lines parse as JSON and all records contain the 24 schema fields. The 15 checked rows all match: the three highest accuracy values (Claude Mythos Preview 64.7, Claude Fable 5 / Mythos 5 64.5, and GPT-5.4 Pro 58.7; rows 254, 253, and 235) and every row for the three models released in the prior month (GPT 6 Astra, Fable 5.1, and Gemini 3.8 Flash; rows 1–4, 7–8, 107–108, 111–112, and 115–116). Release timing was checked against [OpenAI's GPT-6 Astra announcement](https://openai.com/index/gpt-6-astra/), [Anthropic's Claude Fable 5.1 announcement](https://www.anthropic.com/claude-fable-and-mythos-5-1), and [Google's Gemini 3.8 Flash announcement](https://blog.google/innovation-and-ai/models-and-research/gemini-models/3-8-flash-and-3-8-flash-cyber/).

Coverage also matches exactly by source configuration: Scale has 53 full-set and 63 text-only entries (106 and 126 metric rows); the Zoom AI table has 39 full-set and 30 text-only numeric entries (39 and 30 rows). The source and row-file configuration sets have no missing or extra entries. No mismatches or omissions were found, so no row edits were needed.
