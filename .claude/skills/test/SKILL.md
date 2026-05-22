---
name: test
description: Run the delegate-agent unittest suite. Use to verify changes after edits to `src/` or `tests/`, or whenever you want a fast green/red signal.
---

# test

Runs the unittest suite. Stdlib only — no pytest, no external runner.

## Full suite

```bash
python3 -m unittest discover -s tests
```

Discovers every `tests/test_*.py` module. Takes a few seconds.

## Single module

```bash
python3 -m unittest tests.test_delegate_parser
```

## Single test case or method

```bash
python3 -m unittest tests.test_delegate_parser.ParserTests.test_describe
```

## Verbose

Add `-v` for per-test output:

```bash
python3 -m unittest discover -s tests -v
```

## What to do on failure

Read the traceback, find the failing assertion, then read the module under test and the test file. Tests are the spec — match their expectations rather than relaxing them. If a test looks wrong, surface it before changing it.

## Coverage of modules

- `test_delegate_parser` — CLI argv parsing
- `test_delegate_validation` — input validation
- `test_delegate_commands` — Cursor/Droid/Codex argv construction
- `test_delegate_execution` — execution path, output capture
- `test_runner_capture` — stream capture, progress, completion reports
- `test_run_registry` — registry index, alias allocation, locking
- `test_harness_events` — Cursor/Droid/Codex event stream parsing
- `test_snapshot_commands` — snapshot/run-output rendering
- `test_retention` — archive-only tarball retention
- `test_end_to_end_tracking` — full integration
