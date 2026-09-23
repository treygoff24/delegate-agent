# GPQA collection notes

## What it measures

GPQA is a graduate-level, multiple-choice science question answering benchmark spanning biology, physics, and chemistry. The collected release is the GPQA Diamond Clean results layer: corrected GPQA Diamond membership retains 189 of the original 198 Diamond questions after excluding nine audited defects. The source describes Diamond as the 198-question highest-quality subset of the 448-question GPQA set.

## Captured release

- Results bundle: **0.1.9**, data through **2026-09-22**.
- Benchmark membership: **GPQA-Diamond-Clean 1.2.2** (189 questions); original Diamond comparison: 198 questions.
- Captured at: `2026-09-23T01:19:46Z` UTC.
- Current leaderboard: https://gpqa.ai/
- Versioned release: https://gpqa.ai/data/releases/v0.1.9/
- Dataset card supplied in the task: https://huggingface.co/datasets/Idavidrein/gpqa

## Collection method and row count

Downloaded the release's raw `leaderboard.json`, `configuration-manifests.json`, `uncertainty.json`, `leaderboard-costs.json`, `leaderboard.csv`, `methodology.json`, `release-manifest.json`, `LICENSES.md`, and the published checksums, plus the rendered downloads and methodology pages. The JSON release data is the authoritative capture; the HTML pages preserve the context and live publication version. SHA-256 values for leaderboard, configuration manifests, uncertainty, methodology, CSV, and licenses match the release manifest/checksum list. The supplemental cost JSON is separately linked from the live site and identifies its release binding and pricing scope.

There are **262 model configurations** in the release and **786 normalized rows**: three per configuration (Clean accuracy, Original accuracy, and the source-reported Clean-minus-Original percentage-point change). Those 262 configurations map to **183 distinct canonical model IDs** after collapsing repeated model IDs across dates, efforts, and protocol variants. The configuration manifest's model snapshot is the basis, with route aliases resolved from the named model where needed. The Clean accuracy row carries the paired scalar cost when available: 246 configurations are priced and 16 lack cost. The source's cost file says this is the USD cost for one attempt on each of the 189 Clean questions, with retries excluded. It does not report elapsed time.

The source's `mean@N` is the arithmetic mean across N attempts, not best-of-N, pass@N, or majority voting. The row `effort` comes from the configuration identity; `unspecified` is retained where that is the source's stated value. `measured_on` uses the source configuration date and is not asserted to be the exact evaluation-run date. The release's `configuration_date` can derive from validated source metadata; the release timestamp itself is not used as a measurement date. Cost is recorded as `per_run` for the source's 189-question, one-attempt Clean run. Original score cost and the change metric cost are null; only the Clean score is paired with the quoted cost.

## Metric definitions

- `accuracy`, subset `clean_189`: the source's published Clean percentage, exact release value, higher is better; n=189.
- `accuracy_original`, subset `original_198`: the source's published Original percentage, exact release value, higher is better; n=198.
- `clean_score_delta`, subset `clean_minus_original`: the source's published percentage-point difference between those two percentages. This is a subset correction effect, not a separate model capability measure.
- `cost_usd`: source `clean_run_usd`, where available, for one attempt per retained question. No conversion or estimate was applied.

## Caveats

The release is an independent evidence project, not an official GPQA-author leaderboard or a homogeneous set of new independent runs. It combines attributed Epoch AI evidence with strict, compatibility, derived, recovery, and project-native evidence; each source row's evidence class is preserved in notes. Scores and costs therefore do not imply one uniform prompt/provider setup. The source expressly labels results as configurations, and protocol/attempt count and source class are captured in notes.

The original 198-question Diamond benchmark has contamination and answer-key quality concerns; this project excludes nine questions under its audited clean-membership release. The Clean scores should not be described as scores on the original Diamond set. Source-reported 95% Clean intervals are asymmetric; the shared schema only permits one symmetric CI half-width, so `ci` is null and the full interval remains available in `raw/gpqa/uncertainty.json`. Original intervals are not supplied in that file. Rank intervals and rank are not normalized as benchmark metrics.

Canonical model IDs are best-effort. For cases where the manifest names a hosting route rather than underlying model maker, the model maker is inferred from the raw model name; inspect the captured manifest for provenance. No times were published. The raw release does not include raw completions, reasoning traces, per-question matrices, or all evaluation archives.

## License and redistribution

The release states aggregate results and documentation are **CC BY 4.0**, with upstream GPQA attribution retained. See captured `LICENSES.md`; it says the pipeline code is Apache-2.0 but is not included in the data bundle. The corrected benchmark is an ID-only overlay, and the public release does not distribute question content or answer-level material. The original benchmark dataset's terms are separate; consult the upstream dataset card before redistributing its content.

## Raw captures

All downloaded raw materials are under `examples/model-bench/raw/gpqa/`. Primary row source: `leaderboard.json`; configuration/model identity: `configuration-manifests.json`; costs: `leaderboard-costs.json`; uncertainty: `uncertainty.json`.

## Verification

Verified 2026-09-22 against the captured release artifacts and the live leaderboard at https://gpqa.ai/.

- Parsed all 786 JSONL lines and checked that every row has exactly the 24 schema fields with their declared types; all passed.
- Spot-checked 10 Clean accuracy rows against the same `model_configuration` and exact published Clean score in `raw/gpqa/leaderboard.json`: the three highest scores (GPT-6 Astra (max), GPT-6 Astra Pro (max), GPT-6 Sol (max)) and recent-release examples (GPT-6 Luna (max), Claude Fable 5.1 (max), Gemini 3.8 Flash (high), DeepSeek V4.1 Flash (max), Qwen3.8 Max 0902 (xhigh), Claude Opus 5.5 (max), Grok 4.7 (xhigh)). All 10 matched.
- Compared source and collected configuration names: all 262 source configurations appear in the rows, with no extras. They produce 786 rows (three metrics per configuration) and 183 distinct canonical `model_id` values, matching the collection counts. The live page also reports 262 configurations.
- Changed no data rows: no sampled mismatch or coverage omission was found.
