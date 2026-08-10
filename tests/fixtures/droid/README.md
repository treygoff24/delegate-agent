# Droid JSONL fixtures

Synthetic fixtures — hand-written, NOT captured from a real `droid` child
run (unlike `tests/fixtures/opencode/`, whose fixtures document real captures).
There is no recorded droid stream behind these files.

## Why synthetic is sufficient

Droid call-mode text rides the harness-generic message/completion envelope:
the runner reads `{"type":"message", ...}` / `{"type":"completion", ...}`
records the same way it does for every harness, and `harness_events.py` has
no droid-specific ingest branch for that path. `simple_text.jsonl` therefore
pins the generic message/completion extraction staying green for droid call
runs, which is exactly the behavior `tests/test_call_text_fixtures.py`
exercises — no real capture is needed to cover it.
