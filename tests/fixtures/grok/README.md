# Grok streaming-json fixtures

## tool_read_multi_response.jsonl

Captured from `grok 1.0.13 (5e9a58528b76) [stable]` on 2026-09-07, the current
stable release. Model: the account default, `grok-4.6-build`. Self-reported cost
on the `end` event: `total_cost_usd` 0.01234574.

Command:

```
grok --output-format streaming-json --cwd /var/tmp/lane-E-cap \
  --prompt-file /var/tmp/lane-E-cap/grok_prompt.txt
```

The prompt asked the model to read a one-line `marker.txt` and reply with the
marker string it contains. Exit code 0.

Event sequence, verbatim: `available_commands`, a run of `thought` and `text`
events carrying the preamble, `usage`, `tool_call` (`read_file`, arguments under
`rawInput.target_file`), two `tool_call_update` events (the first with
`status: null` and a `locations` entry, the second with `status: "completed"`),
`available_commands`, a second run of `text` events carrying the answer,
`usage`, and a single terminal `end` with `stopReason: "end_turn"`, `sessionId`,
`usage` and `total_cost_usd`.

This is the shape the multi-response fix turns on. There is exactly one `end`
event for a two-response run, and the per-response boundary is `usage`, which
is what the vendor's headless-mode guide documents ("`end` is always the last
event"). The two older hand-written fixtures in `tests/fixtures/` use CamelCase
`EndTurn`/`MaxTokens` from grok 0.2.73 and contain no `usage`, `tool_call` or
`tool_call_update` events at all.

The `available_commands` payloads list the skills and slash commands installed
on the capture host. They are retained verbatim so the fixture is a real
capture rather than an edited one.
