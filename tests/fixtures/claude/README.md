# Claude Code stream-json fixtures

## structured_output.jsonl

Captured from `claude 2.1.263 (Claude Code)` on 2026-09-07. Model: `haiku`, the
cheapest available.

Command:

```
claude -p --output-format stream-json --model haiku \
  --json-schema '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}' \
  'Reply ok true'
```

Exit code 0. Retained verbatim, including the 54 `system`/`thinking_tokens`
lines and the `rate_limit_event`, because absorbing unmodelled event types
without disturbing extraction is part of what the parser has to do.

The terminal `result` event carries the answer twice: `result` holds the string
`{"ok":true}` and `structured_output` holds the parsed object `{"ok": true}`.
Only the `structured_output` field is documented by the vendor. It also carries
`is_error: false`, `permission_denials: []`, `session_id`, snake_case `usage`
and camelCase `modelUsage` with `canonicalModel`.
