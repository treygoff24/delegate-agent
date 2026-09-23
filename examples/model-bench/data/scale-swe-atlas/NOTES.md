# SWE-Atlas (Scale Labs): Codebase QnA / Refactoring / Test Writing

added by coverage audit 2026-09-22

## What it measures and data version

SWE-Atlas is a family of three agentic-coding benchmarks that evaluate
coding agents against real-world software repositories (large, actively
maintained open-source codebases with non-trivial architectures, deliberately
using strong copyleft licenses such as GPL to reduce contamination risk).
Repos span at least Go, Python, C, and TypeScript. Code:
https://github.com/scaleapi/SWE-Atlas; the QnA slice also has a dedicated
dataset card at https://huggingface.co/datasets/ScaleAI/SWE-Atlas-QnA.

All three slices share one primary metric, **Task Resolve Rate**, though its
exact pass condition differs per slice:

- **Codebase QnA** (`sweatlas-qna`): deep code comprehension /
  question-answering. A task is "resolved" if the agent's answer passes all
  rubric items at the strictest threshold (score 1.0); the page cites a top
  pass rate of ~30% at that threshold. Agent runs inside a sandboxed Docker
  container.
- **Refactoring** (`sweatlas-refactoring`): restructuring code while
  preserving behavior. A task is resolved if it passes all existing tests
  (no regressions) and all must-have rubrics (code maintainability,
  documentation, artifact cleanup, negative-regression checks).
- **Test Writing** (`sweatlas-tw`): writing production-grade tests for
  specific target behaviors. A task is resolved if it passes a Manifest
  Check, a Mutation Test, and the rubric set (an LLM judge grades the test
  patch against must-have/nice-to-have binary-pass rubrics).

## Collection method and coverage

Per the collection brief, `subset` is the slug suffix: `qna`, `refactoring`,
`tw`. Fetched all three pages with `curl -A 'Mozilla/5.0'`:

- https://labs.scale.com/leaderboard/sweatlas-qna -> `raw/scale-swe-atlas/sweatlas-qna.html` (24 rows)
- https://labs.scale.com/leaderboard/sweatlas-refactoring -> `raw/scale-swe-atlas/sweatlas-refactoring.html` (17 rows)
- https://labs.scale.com/leaderboard/sweatlas-tw -> `raw/scale-swe-atlas/sweatlas-tw.html` (24 rows)

and extracted each page's embedded `entries` array (RSC flight payload, same
method as the other Scale Labs boards in this batch). Total 65 rows across
23 distinct `model_id`s (many model+harness combinations are evaluated on
more than one slice).

## Freshness

Newest row across the three slices: Gemini 3.8 Flash (Mini-SWE-Agent),
dated 2026-09-09 (QnA and Refactoring) / 2026-09-09 (Test Writing).
`last_updated` = **2026-09-09** (used for all three subsets under this one
benchmark_id). No explicit "Updated <date>" text was found on any of the
three pages. 2026-frontier models present on all three slices: Gemini 3.8
Flash (2026-09-09), GPT 6 Astra/Codex (2026-09-09), Fable 5.1/Claude Code
(2026-09-02). This board is live and current.

## Metrics and row interpretation

- `metric` = `resolved_rate` (Task Resolve Rate), `unit` = `%`,
  `higher_is_better` = true, on all three subsets.
- `ci` = the JSON `confidenceInterval_upper` field (half-width).
- `measured_on` = the row's `createdAt` date.
- `harness`: many rows explicitly name an agent scaffold in parentheses —
  `Claude Code`, `Codex`, `Mini-SWE-Agent`, `Mini-SWE` (a distinct, shorter
  scaffold name from `Mini-SWE-Agent` — kept as its own literal harness value
  rather than merged, since the source treats them as different
  configurations), and `Gemini CLI`. These are captured in `harness` and
  removed from `model_id`/`model_raw`'s effort parsing; `model_raw` itself
  keeps the full original string including the harness annotation.
- `effort`: parsed as `xhigh` for the many "(xHigh)"/"xHigh"/"xHigh*"
  suffixed rows (trailing `*` markers are cosmetic footnote markers on the
  page — e.g. denoting a specific inference provider — and are stripped for
  `model_id` but the asterisk is preserved verbatim in `model_raw`).
- `contaminationMessage` was empty for every row across all three files;
  `notes` is null throughout.

## Caveats and license

Footer states only "All rights reserved" on all three pages; no separate
redistribution terms given for the leaderboard's numeric results (the
underlying source repos are GPL-licensed for contamination resistance, but
that governs the *code corpus*, not these result rows). `license` is null on
every row.

## Verification

Spot-checked 10 of 65 rows (top 3 by `resolved_rate` across all three files
combined, plus 7 more spread across all three) by independently
re-extracting `score`, `confidenceInterval_upper`, and `createdAt` from the
matching raw HTML file (by `subset`) with a fresh regex (separate code path
from the JSON-parsing extraction script), matched by exact model name,
including two rows with a literal embedded newline in `model_raw`
(`"Opus 5 (Claude Code) xHigh\n"`, `"GPT 5.4 (Codex) xHigh\n"`) — confirmed
present in the source's own JSON payload, not an extraction artifact. All 10
matched exactly on score, CI, and date. Also ran a full-file schema/parse
check confirming all 65 lines parse as JSON with every SCHEMA.md field
present.
