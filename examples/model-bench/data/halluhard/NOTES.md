# HalluHard collection notes

added by coverage audit 2026-09-22

## What it measures

HalluHard (https://halluhard.com/, code at https://github.com/epfml/halluhard, EPFL — Fan, Delsad, Flammarion, Andriushchenko, arXiv:2602.01031) is a hard multi-turn hallucination benchmark across four domains: `coding`, `legal_cases`, `medical_guidelines`, `research_questions`. Each domain runs multi-turn conversations (with follow-up questions) and judges responses either by claim-based web-grounded verification (research/legal/medical domains) or by direct coding-specific judging (package/import/function-call checks). The reported metric is a **hallucination rate**: the percent of judged claims/responses found unsupported or incorrect, both per conversation turn (1, 3, 5) and as an overall aggregate per domain. Lower is better.

## Captured release

- Source: `https://halluhard.com/data/leaderboard-data.js`, which embeds two JS objects consumed by the site: `LEADERBOARD_DATA` (per domain → per model → per turn `{1, 3, 5}` hallucination rate) and `OVERALL_RATES` (per domain → per model → single aggregate hallucination rate).
- GitHub repo `epfml/halluhard` `pushed_at`: **2026-09-17T13:01:14Z**; treated as the data date since the site does not print a separate "last updated" timestamp on the pages fetched.
- Captured at: `2026-09-22T23:00:00Z` UTC.
- 2026 frontier models are present (`gpt-6-astra`, `gpt-6-astra-websearch`, `claude-fable-5`, `claude-fable-5-websearch`, `claude-sonnet-5`, `claude-opus-4-6`, `claude-opus-4-7`, `deepseek-v4-pro`, `gemini-3.1-pro`, `grok-4.1-thinking-fast`, `kimi-k2.6-thinking`, `glm-5.2`, `gpt-5.5-medium`), so the source is live and current, not stale.

## Collection method and row count

Fetched `leaderboard-data.js` directly (plain JS object literals, valid JSON once the `const NAME = ` / trailing `;` wrapper is stripped) and parsed both `LEADERBOARD_DATA` and `OVERALL_RATES` with a bracket-matching extractor plus `json.loads`. Each domain has 40-42 models (some models are domain-specific, e.g. present in `research_questions` but not `legal_cases`); union across domains is 41 distinct model configurations.

Per (domain, model): up to 3 turn-wise rows (`hallucination_rate_turn1`, `hallucination_rate_turn3`, `hallucination_rate_turn5`) plus 1 overall row (`hallucination_rate_overall`) when present in `OVERALL_RATES` (`legal_cases` is missing one model's overall figure; `research_questions` has one overall entry with no matching turn-wise entry — both asymmetries are preserved as-is from the source, not corrected). Total: **656 rows across 41 distinct models** and 4 domains.

## Metric definitions

- `hallucination_rate_turn{1,3,5}` (%, lower is better): hallucination rate measured at that conversation turn within the domain's multi-turn task.
- `hallucination_rate_overall` (%, lower is better): the source's own aggregate hallucination rate across turns for that domain, from `OVERALL_RATES` (not independently recomputed from the turn-wise numbers by this collection).
- `category` per SCHEMA's category list: `coding` domain → `coding`; `legal_cases` → `legal`; `medical_guidelines` and `research_questions` → `knowledge-work` (closest fit; there is no dedicated "medical" or "legal-research" category in SCHEMA.md).
- No cost or wall-time figures are published.

## Caveats

- **No license file** in the `epfml/halluhard` GitHub repo (`license: None` from the GitHub API) and none stated on the website; redistribution terms for the leaderboard numbers are unclear — treat as research-preview data, not confirmed for redistribution.
- `model_raw` values on this leaderboard are informal short names (e.g. `gpt-6-astra`, `claude-fable-5`, `nemotron-3-ultra`, `kimi-k2.5-thinking`) rather than fully qualified provider IDs; `model_id` was inferred by matching a name prefix to a vendor (`gpt*`→openai, `claude*`→anthropic, `gemini*`→google, `deepseek*`→deepseek, `kimi*`→moonshotai, `glm*`→z-ai, `grok*`→xai, `nemotron*`→nvidia) and is a best-effort guess, not verified against each vendor's official model-naming.
- `-websearch` suffixed entries denote the same base model run with web search enabled as a separate configuration; these are kept as distinct `model_raw`/`model_id` rows (not merged with the non-websearch variant), consistent with SCHEMA's "one row per effort/configuration" rule.
- `measured_on` is left `null`: neither the site nor the JS data file states a per-model evaluation date, only the repo-level push date recorded above.
- The benchmark explicitly targets "hard" claims; the reported rates (many in the 30-95% range even for frontier models) are not comparable in absolute terms to easier hallucination benchmarks — see the paper (arXiv:2602.01031) for task construction details, which were not independently re-verified beyond what the README states.
- `reporter` is `independent` (EPFL academic project, not vendor self-reported).

## Raw captures

`raw/halluhard/index.html`, `raw/halluhard/leaderboard-data.js` (primary source), `raw/halluhard/parsed_leaderboard.json` and `raw/halluhard/parsed_overall.json` (parsed JSON extracted from the JS), `raw/halluhard/repo-info.json` (GitHub API metadata), `raw/halluhard/README.md`, all under `examples/model-bench/raw/halluhard/`.

## Verification

Verified 2026-09-22 against the captured `leaderboard-data.js`.

- Ran a Python schema check: all 656 lines parse as JSON and every line has all 24 SCHEMA.md fields present.
- Re-extracted 10 randomly sampled rows independently from `parsed_leaderboard.json`/`parsed_overall.json` (a separate lookup path from the generation script) and compared to the collected rows: all 10 matched exactly, spanning turn-wise and overall metrics across `legal_cases`, `research_questions`, `coding`, and `medical_guidelines` (e.g. `gpt-5.2`/`legal_cases`/turn1 = 31.1, `kimi-k2.5-thinking`/`research_questions`/turn5 = 98.4, `gpt-5-thinking`/coding/overall = 41.2).
- Checked the top 3 models by `hallucination_rate_overall` within the `coding` domain (lowest rate = best): `gpt-5.4-thinking-websearch` 4.3, `gpt-6-astra-websearch` 5.0, `gpt-6-astra` 5.2 — matched the raw `OVERALL_RATES.coding` values exactly.
- Also computed the top 3 by average `hallucination_rate_overall` across all four domains as a cross-domain headline check: `gpt-6-astra-websearch` (27.23), `claude-opus-4-5-websearch` (29.27), `gpt-5.4-thinking-websearch` (31.88) — consistent with the per-domain rankings (no mismatch).
- No mismatches found; no corrections were needed.
