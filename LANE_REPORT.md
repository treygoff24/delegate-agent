# Lane report

## Root cause

`StreamAccumulator._record_assistant_text` appended every non-empty text chunk, including a Claude terminal `result` that replayed the immediately preceding `assistant` message verbatim. That doubled the final answer and could trigger the assistant-text size cap. The method had no terminal-only replay distinction.

## Changes

- `src/delegate_agent/harness_events.py`: when `completion=True` and the stripped text equals the last assistant chunk, retain `completion_text`, `current`, and cache invalidation but skip the duplicate append. Non-terminal repeated messages and different terminal text still append normally.
- `tests/test_harness_events.py`: added realistic Claude stream-json coverage for identical terminal replay, different terminal text, and repeated non-terminal assistant messages.

## Verification

- `python3 -m pytest -q tests/test_harness_events.py` — **154 passed, 41 subtests passed**.
- `python3 -m pytest -q tests/test_harness_events.py -k 'terminal_result_replays_assistant_text_only_once or terminal_result_with_different_text_keeps_both_chunks or repeated_non_terminal_assistant_messages_are_not_deduplicated'` — **3 passed, 151 deselected**.
- Assertion watch: temporarily reverted the dedupe condition; the focused run went **1 failed, 2 passed, 151 deselected**; restored the fix and reran it green.
- `ruff check src/delegate_agent/harness_events.py tests/test_harness_events.py` — **passed**.
- `ruff format --check src/delegate_agent/harness_events.py tests/test_harness_events.py` — **passed**.
- `bash scripts/gate.sh` — pinned Ruff and format checks passed; pytest reported **3281 passed, 15 skipped, 1 error, 2378 subtests passed**. The sole teardown error was the repository's linked-worktree registry-lock guard observing concurrent `ci-burn` Delegate runs; no test failure was reported.

## Not verified

The full gate cannot complete cleanly while concurrent Delegate runs hold the source registry-lock guard. No retry or process termination was attempted because those runs are external to this lane.

## Open questions

None for this change.
