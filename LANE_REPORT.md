# Lane report: dlg-o7i

Status: done-pending-verification.

## Root cause

OMP JSON mode emits tool results as useful stdout records. OMP already had a separate 256 MiB transport ceiling and bounded `thinking_delta` compaction, but every retained stream still used the shared 16 MiB cap, so a legitimate review could hit the retained limit long before the runaway guard. Discounting tool records would lose useful events, so this change preserves them and raises the finite retained budget instead.

## What changed

- Added `<engine>.trackedStreamMaxBytes` as a positive-integer engine setting.
- Set Pi and OMP tracked-stream defaults to 64 MiB; all other engines remain at 16 MiB.
- Resolved the limit before child launch and used it independently for tracked stdout and stderr. Direct runner contexts whose ambient config cannot be resolved fall back to the finite engine default.
- Kept OMP's 256 MiB total stdout transport ceiling, 16 MiB per-record ceiling, and 64 KiB thinking sample unchanged.
- Made output-limit errors name the engine and configured tracked limit. Transport and record failures also identify their distinct hard ceiling.
- Exposed the setting through `describe --full`, validated invalid values, updated the two general cap references in `docs/`, and added runner/config/describe regression coverage.
- Did not change `harness_events.py`; useful tool-output records remain retained and counted.

## Files changed

- `src/delegate_agent/config.py`
- `src/delegate_agent/describe_payload.py`
- `src/delegate_agent/runner.py`
- `tests/test_tracked_output_bounds.py`
- `tests/test_omp_output_capture.py`
- `tests/test_engine_argv.py`
- `docs/cli-reference.md`
- `docs/troubleshooting.md`
- `LANE_REPORT.md`

The pre-existing `.beads/issues.jsonl` modification was not touched or staged.

## Verification

- `uv run --extra dev pytest -q tests/test_tracked_output_bounds.py tests/test_omp_output_capture.py tests/test_call_capture_contract.py tests/test_profiles.py::CodexProfileExecutionTests::test_fallback_child_drops_ambient_delegate_config tests/test_engine_argv.py::EngineArgvTests::test_describe_preserves_safe_read_only_modes`
  - 22 passed, 33 subtests passed in 4.62 seconds.
- `python3 -m compileall -q src tests bin`
  - Passed with exit 0.
- `python3 bin/delegate.py --json describe --full | jq '{codex: .engineDefaults.codex.trackedStreamMaxBytes, pi: .engineDefaults.pi.trackedStreamMaxBytes, omp: .engineDefaults.omp.trackedStreamMaxBytes}'`
  - Read back Codex `16777216`, Pi `67108864`, and OMP `67108864`.
- A local fake-OMP CLI smoke used an explicit OMP limit of 8192 bytes. It exited 1 with `output_limit_exceeded` and the message `Child engine omp stdout exceeded its configured tracked stream limit of 8192 bytes.` This was a local transport smoke, not live-provider evidence.
- `scripts/gate.sh`
  - Ruff 0.15.15 matched the pin; `ruff check` passed; `ruff format --check` passed for 244 files.
  - Pytest completed with 3,281 passed, 15 skipped, 2,383 subtests passed, and one teardown error. The only error was the known linked-worktree guard observing sibling command `delegate --json runs --group ci-burn` lock the source checkout registry during the run. The gate therefore exited nonzero despite no code-test failures.

## Watched failing

I saved and temporarily reverse-applied the implementation diff while leaving the new tests in place, then ran:

`uv run --extra dev pytest -q tests/test_tracked_output_bounds.py::TrackedOutputBoundsTests::test_engine_configured_limit_is_honored_and_named_in_error tests/test_tracked_output_bounds.py::TrackedOutputBoundsTests::test_pi_family_default_differs_from_codex tests/test_tracked_output_bounds.py::TrackedOutputBoundsTests::test_config_rejects_non_positive_or_non_integer_tracked_stream_limit tests/test_engine_argv.py::EngineArgvTests::test_describe_preserves_safe_read_only_modes`

The planted negative produced 8 failures, 1 pass, 26 passing subtests, and the same known external lock teardown error. It specifically showed the configured limit was not enforced, the default resolver was absent, invalid values were accepted, and `describe --full` omitted the setting. I restored the implementation and the focused suite passed as reported above.

No assertions, validators, gates, timeouts, or tolerances were weakened. No tests were skipped or suppressed by this change.

## Not verified

- A clean `scripts/gate.sh` exit could not be observed while sibling lanes were using the source registry; a verifier should rerun it in a quiet window.
- No live OMP provider review was launched, so the original medium-diff workload was not replayed against provider infrastructure.

## Open questions

None.

## Suggested integration

Inspect the lane diff, cherry-pick its commit, rerun the focused command above, and rerun `scripts/gate.sh` when no sibling lane is touching the source registry. Keep the bead open until that independent verification is recorded.
