# Lane report

## Root cause

`_expected_version` accepted a bare semantic version only when the selector basename was one of the harness's ordinary PATH candidates. An explicitly configured `pi.binary=estate-pi` therefore normalized successfully but failed fingerprinting, so capability refresh reported `fingerprint_mismatch` before model/reasoning discovery. The existing `agent` ambiguity guard remains in force.

## Changes

- Threaded the explicit-selector assertion through version identification.
- Allow explicitly configured, non-ambiguous wrapper names to identify a harness from a bare semantic version.
- Added unit and selector-resolution coverage for `estate-pi`, plus an explicit ambiguous `agent` negative.

## Verification

- `python3 -m unittest tests.test_harness_discovery.VersionIdentityTests tests.test_harness_discovery.DetectionTests` — **16 passed**.
- Mutation check: temporarily removed the explicit flag at the resolution call site, then ran the new targeted tests; `test_explicit_pi_wrapper_with_bare_version_resolves` failed, and the fix was restored.
- `python3 -m pytest -q tests/test_harness_discovery.py` — **106 passed, 61 subtests passed**.
- `uv run --extra dev ruff check src/ tests/ bin/` — **passed** (`All checks passed!`).
- `uv run --extra dev ruff format --check src/ tests/ bin/` — **passed** (`244 files already formatted`).
- `python3 -m compileall -q src tests bin` — **passed**.
- `scripts/gate.sh` — ruff checks passed; pytest reached **3281 passed, 15 skipped, 1 error, 2378 subtests passed**. The sole error was the repository's linked-worktree registry-lock teardown guard observing concurrent Delegate CI-burn processes in the shared source checkout, not a test failure in this change.

## Not verified / open questions

The full gate could not reach a clean exit while those concurrent CI-burn processes were active. Per instruction, no live `delegate capabilities refresh` was run; rerun `scripts/gate.sh` after the shared registry-lock activity has stopped, then perform the authorized integration/merge from this branch.
