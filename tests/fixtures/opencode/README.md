# OpenCode NDJSON fixtures

Captured against `opencode` v1.17.17 (`$HOME/.opencode/bin/opencode`) on 2026-07-09,
using `OPENCODE_DISABLE_AUTOUPDATE=1` on every invocation. Model: `opencode/gpt-5-nano`
(cheap model, provider `opencode`/Zen auth).

## simple_text.ndjson

Command:

```
cd /tmp/oc-phase0/simple
OPENCODE_DISABLE_AUTOUPDATE=1 opencode run --format json --model opencode/gpt-5-nano \
  "Reply with exactly: pong"
```

Exit code: 0. Plain text answer, no tool use.

Event sequence: `step_start` -> `text` -> `step_finish`.

- `step_start`: `part.type == "step-start"`. Marks the beginning of an assistant turn/step.
- `text`: `part.type == "text"`, carries the final answer in `part.text`. Includes
  `part.time.{start,end}` and provider metadata under `part.metadata.<provider>.itemId`.
- `step_finish`: `part.type == "step-finish"`, `part.reason == "stop"` for a normal
  completion. Carries `part.tokens.{total,input,output,reasoning,cache.{read,write}}`
  and `part.cost` (USD).

All events share top-level `type`, `timestamp` (ms epoch), `sessionID`, and `part`.

## tool_run.ndjson

Command:

```
cd /tmp/oc-phase0/toolrun
echo "The secret word is banana42." > note.txt
OPENCODE_DISABLE_AUTOUPDATE=1 opencode run --format json --model opencode/gpt-5-nano \
  "Read the file note.txt in the current directory and tell me exactly what the secret word is."
```

Exit code: 0. Model reads the file via the `read` tool, then answers in text.

Event sequence: `step_start` -> `tool_use` -> `step_finish` -> `step_start` -> `text` -> `step_finish`.

- `tool_use`: `part.type == "tool"`, `part.tool` is the tool name (e.g. `"read"`).
  `part.state.status` is `"completed"` (also observed: `"error"` when a denied
  tool is attempted in other runs, see `error_run.ndjson` note below and evidence.md
  check 7). `part.state.input` holds the tool call arguments; `part.state.output` holds
  the tool's result text. Note the file path opencode reports is the realpath-resolved
  form (macOS `/tmp` -> `/private/tmp`), not the path as typed.
- Two full `step_start`/`step_finish` cycles appear: one for the tool call step, one for
  the final text-answer step. `step_finish.part.reason` is `"tool-calls"` for the first
  step and `"stop"` for the final step.

## error_run.ndjson

Command:

```
cd /tmp/oc-phase0/errorrun
OPENCODE_DISABLE_AUTOUPDATE=1 opencode run --format json --model opencode/totally-bogus-model-xyz \
  "Reply with exactly: pong"
```

Exit code: 1. A single `error` event, no `step_start`/`step_finish` pair.

- `error`: top-level `type == "error"`. `error.name` (e.g. `"UnknownError"`),
  `error.data.message` (human-readable), `error.data.ref` (an opaque error reference
  id for support/log correlation). No stack trace or secret leaked to stdout or stderr.

## Notes for the parser design

- All three fixtures are NDJSON: one JSON object per line, no wrapping array, no
  comments. Parse line-by-line; a line failing to parse should not crash the run
  (be tolerant of trailing blank lines).
- `sessionID` is stable across every event in a single `run` invocation; use it to
  correlate multi-step runs.
- There is no explicit "run complete" event distinct from the final `step_finish`;
  end-of-stream (EOF on stdout, process exit) is the actual completion signal.
- Permission-denied tool calls do **not** appear as `tool_use` events with an error
  state in the common case — see evidence.md check 4/5. When a tool is denied via
  config permissions, opencode simply excludes that tool from the model's tool
  schema, so the model never attempts to call it; the run still ends with an
  ordinary `text` + `step_finish` (`reason: "stop"`) and exit code 0.
