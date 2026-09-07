# Cursor stream-json fixtures

## tool_read.jsonl

Captured from `cursor-agent 2026.09.02-c22c1a3` on 2026-09-07. Model:
`composer-2.5`, the cheapest listed model.

Command:

```
cursor-agent -p --trust --mode ask --model composer-2.5 --output-format stream-json \
  'Read the file marker.txt in the current directory and reply with only the marker string it contains.'
```

Run in `/var/tmp/lane-E-cap`, which held a one-line `marker.txt`. Exit code 0.

Two things in this capture drive parser behaviour:

- The `system`/`init` event reports `"model": "Composer 2.5"` — the catalog
  display name, not the `composer-2.5` id passed to `--model`.
- Tool activity arrives as `{"type": "tool_call", "subtype": "started" |
  "completed", "call_id": ..., "tool_call": {"readToolCall": {"args": {...}}}}`.
  The type is dotless, the subtype carries the phase, and the tool is named by
  the single `*ToolCall` key with its `args` nested one level inside.

`thinking` events appear in print mode despite the vendor docs saying they are
suppressed there.
