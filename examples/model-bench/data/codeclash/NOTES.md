# CodeClash results capture

## What it measures

CodeClash evaluates language models as adaptive coding agents in multi-round competitive programming games. Agents get a high-level goal and iteratively modify their codebases; those codebases then compete in game arenas. This tests goal-oriented software development rather than isolated issue resolution.

## Data captured

- Benchmark: CodeClash public leaderboard, as displayed on the official homepage.
- Leaderboard data timestamp: 2025-11-03T16:58:27Z (the per-board timestamps differ by milliseconds).
- Captured version/date: leaderboard updated 2025-11-03; retrieved 2026-09-22.
- Seven boards: overall plus BattleSnake, CoreWar, Halite, Poker, RoboCode, and RobotRumble.
- Coverage: 8 listed model configurations on each board, 56 rows total (8 models × 7 boards × one ELO metric). No effort variants, cost, or time are published.
- Raw capture: `raw/codeclash/leaderboard-2026-09-22.html`, the official homepage HTML containing the embedded full leaderboard JSON.

The official page still displays this November 2025 leaderboard. Newer CodeClash articles describe CC:Ladder and CC:Train, but did not publish additional model leaderboard scores in the checked pages. The page's leaderboard is therefore stale relative to the current date; the results below are all results currently published on its public leaderboard, not evidence of current model performance.

## Metrics

Each row records the published Elo rating for the specified board. Higher is better. The source gives a `±` value labeled in the embedded data as `elo_std`; it is copied exactly into `ci` as the schema's field for stated error/uncertainty, without assuming it is a formal confidence interval. Sample counts are not provided. No costs or runtimes are published.

## Collection and caveats

Downloaded `https://codeclash.ai/` with `curl` and extracted its embedded `fullLeaderboardData` JSON. The site itself is not bot-walled or JavaScript-dependent for the leaderboard payload, so this is a direct capture of the rendered page's underlying data. The displayed overall and arena boards each contain eight models. The source identifies models by display names; `model_id` values are canonical-name mappings, with `Grok Code Fast` mapped to `xai/grok-code-fast-1`.

Results are independently published by the CodeClash project, not reproduced by this collection. The leaderboard reflects the project's 2025 evaluation runs and model versions then evaluated. Possible contamination and subsequent model updates cannot be assessed from the leaderboard. No per-row sample count, effort configuration, or evaluation harness details are listed on the page.

The CodeClash GitHub repository is marked MIT, but the leaderboard page does not state a separate license or redistribution terms for these result data; `license` is left null.

## Not captured

No newer model scores were found in the official public leaderboard or the checked newer CodeClash articles. CC:Ladder is mentioned in a January 2026 article, but its model results were marked as forthcoming there; it is not represented as an additional score board in the current leaderboard capture.

## Verification

- Parsed all 56 JSONL rows and confirmed each contains every field in `examples/model-bench/SCHEMA.md`.
- Spot-checked 10 rows against `raw/codeclash/leaderboard-2026-09-22.html`, including the three highest scores (Claude Sonnet 4.5 / CoreWar 1641, GPT-5 / Poker 1599, and o3 / Halite 1577). All model/board pairs, Elo values, and `elo_std` values matched.
- Compared the complete capture with the rows: 7 boards × 8 model configurations = 56 source entries; all 56 are represented once, with no extras or score/uncertainty mismatches. The captured models are from the 2025-11-03 leaderboard, so none was released in the month before this 2026-09-22 check.
- Changes: none; no row was provably wrong or missing.
