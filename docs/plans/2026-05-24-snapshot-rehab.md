# Snapshot rehab: make `delegate snapshot` actually useful

**Date:** 2026-05-24
**Status:** draft — not yet started
**Owner:** TBD
**Schema bump:** `delegate.snapshot.v1` → `delegate.snapshot.v2` (additive, v1 still readable)

## Problem

`delegate snapshot <alias>` is the primary tool a parent agent (Claude, Codex, etc.) uses to check on an in-flight delegated run. Today its output is sparse to the point of unhelpfulness — and for one harness (codex) it's *completely empty*.

A real example, captured live during a droid run mid-task:

```
droid-21 · running · 4m3s elapsed
cwd: /Users/treygoff/Code/delegate-agent
execution cwd: /Users/treygoff/Code/delegate-agent
model: custom:OpenRouter-:-DeepSeek-V4-Pro-0
mode: work
current: Edit
recent:
  - tool.started: TodoWrite
  - tool.started: Read
  - tool.started: Read
  - tool.started: Read
  - tool.started: TodoWrite
  - tool.started: Read
  - tool.started: Read
  - tool.started: TodoWrite
  - tool.started: Edit
```

A parent agent looking at this learns almost nothing: no file paths, no commands, no diff context, no TodoWrite contents, no timestamps, no idea which Read failed.

## Root cause (grounded in real event streams)

Inspected one recent run per harness from `.delegate/runs/` and decoded the actual `stdout.log` JSON:

| Harness  | Tool name | Tool target | Assistant text | Status |
|----------|-----------|-------------|----------------|--------|
| **droid**  | ✅ `toolName` resolves | ❌ file lives in `parameters.file_path`; `_tool_target` only checks `args.{path,file,…}` | ✅ via `type=message, role=assistant` | partial |
| **cursor** | ❌ tool name encoded in the **wrapper key** (`tool_call.readToolCall.args.path`); `_string_field` returns the default `"tool"` | ❌ nested 2 levels under wrapper, never reached | ✅ via `type=assistant` | mostly broken |
| **codex**  | ❌ events are `item.started`/`item.completed` wrapping inner `item.type`; `_ingest_object` doesn't route them | ❌ command in `item.command` | ❌ wrapped in `item.type=agent_message` → dropped | **totally broken** |

Confirmed by reading a recent 5-min codex run: `eventsTotal: 0`, `assistantText: ""`, `current: null`. Every codex event type (`item.started`, `item.completed`, `command_execution`, `agent_message`, `turn.started`, `turn.completed`, `thread.started`) is unhandled and silently dropped on the floor.

On top of the per-harness ingestion bugs, **structural gaps hit every harness**:
- No tool **args preview** — `TodoWrite` payload, `Edit` diff, `Bash` command all discarded
- No **per-event timestamps** — can't tell if 9 Reads happened in 5s or 5min
- No **consecutive-duplicate collapsing** — `recent_events[-20:]` fills with the same `Read` 9 times
- No **tool exit status** — `tool_result` is dropped, so failures are invisible
- No **token/cost telemetry** — codex `turn.completed.usage` is right there for the taking
- `current` is just the bare tool name (`"Edit"`) when it could be `"Edit src/cli.py"`

## Goal

After this work, the same droid snapshot should look something like:

```
droid-21 · running · 4m3s elapsed
cwd: /Users/treygoff/Code/delegate-agent
model: custom:OpenRouter-:-DeepSeek-V4-Pro-0  mode: work
current: +3m58s · Edit src/delegate_agent/cli.py

assistant: 1842 chars, last update +3m04s
  …Implementing the parser changes per Wave 1 checklist. About to wire the…

recent:
  +0m02s  TodoWrite (×1)        first: "Read spec and understand full requirements"
  +0m08s  Read (×9, span 1m12s) last: docs/plans/…isolation-spec.md
  +1m20s  TodoWrite (×1)        first: "Add constants and defaults to config.py"
  +1m24s  Read (×3, span 18s)   last: src/delegate_agent/config.py
  +3m58s  Edit                  src/delegate_agent/cli.py · old_str: "def parse_cli(arg…"

usage (turn): 2.3M in / 15.7k out / 9.9k reasoning
```

And codex snapshots should stop being empty.

## Plan (5 waves)

Each wave is independently shippable, runs the full test gate, and unlocks the next.

### Wave 1 — Data model: enrich `NormalizedEvent` + accumulator

Foundation, no behavior change for current harnesses.

**`src/delegate_agent/harness_events.py`:**
- Extend `NormalizedEvent` with: `params_preview: str | None`, `started_at_ms: int | None`, `duration_ms: int | None`, `exit_code: int | None`, `call_id: str | None`.
- Add helper `_bounded_params_preview(payload, *, limit=200) -> str | None` that picks the most informative args (`file_path`, `command`, `old_str` snippet, first todo entry, etc.), JSON-stringifies, truncates with ellipsis.
- Add helper `_event_offset_ms(run_start_iso, ev_ms) -> int | None` for relative timestamps.
- Add `pending_tool_calls: dict[str, NormalizedEvent]` on `StreamAccumulator` so we can pair `started` → `completed` by `call_id` and stamp `duration_ms`/`exit_code` retroactively.
- Add `usage: JsonObject | None` field on accumulator for token/cost data.

**`src/delegate_agent/run_registry.py`:**
- Bump `SNAPSHOT_SCHEMA` to `delegate.snapshot.v2`. Treat v1 as still-readable; `merge_snapshot_view` is already tolerant of missing keys.

**Tests** (`tests/test_harness_events.py`):
- `params_preview` truncation
- `params_preview` redaction
- `started` → `completed` pairing by `call_id`
- Time offset math against a fixed run-start ISO

**Gate:** `python3 -m unittest discover -s tests` stays green; new tests pass.

### Wave 2 — Fix the three harnesses

Biggest user-visible win. Scoped to a single file.

**`src/delegate_agent/harness_events.py`:**

**Droid:** broaden `_tool_target` and add params extraction. Also check `parameters` (in addition to `args`) and a wider key set: `file_path`, `cmd`, `pattern`, `query`, `url`. For `Edit`, surface a 60-char snippet of `old_str` in the preview.

**Cursor:** add `_ingest_cursor_tool_call_wrapped`. When `tool_call` is a single-key dict whose key ends in `ToolCall` (or `_tool_call`), derive the tool name from the wrapper key (`readToolCall` → `read`), then dig into `.args` for target/params. Handle `subtype: started|completed` for status pairing using `call_id`.

**Codex:** add `_ingest_codex_item(payload, kind)` that unwraps `payload["item"]` and routes by inner `item.type`:
- `agent_message` → append `item.text` to `assistant_chunks`
- `command_execution` → tool event with `tool="shell"`, `target=item.command[:120]`, capture `exit_code`/`status` from completed events
- `reasoning` → drop (matches existing behavior)
- Unknown inner types → bounded-text fallback so we never silently lose information again
Also handle `turn.completed` → store `usage` on accumulator.

**Tests:**
- New `tests/fixtures/{droid,cursor,codex}_events.jsonl` containing real sanitized event lines copied from `.delegate/runs/`.
- One test per harness asserting: tool name, target, params preview, assistant text, completion all extracted correctly.

**Gate:** all tests pass + manual smoke: run `delegate snapshot` against existing codex/cursor runs in `.delegate/runs/` and visually confirm rich output.

### Wave 3 — Rendering: surface what we now have

The user-visible upgrade.

**`src/delegate_agent/rendering.py` — `render_snapshot_text`:**

- **Collapse consecutive duplicates**: group `recent_events` by `(kind, tool, target)`; when ≥2 in a row, render `tool.started: Read foo.py (×9, span 3m12s)`.
- **Per-event relative timestamps**: `+3m12s · tool.started: Edit src/cli.py`.
- **Params preview line** under each tool event when set, indented and bounded to one line.
- **Enrich `current`**: if `current` is the bare tool name and we have a target, render `current: Edit src/cli.py`.
- **Status badges**: `✓` / `✗ exit=1 (842ms)` suffix on completed tool events.
- **Tail-of-assistant**: add `--assistant-tail N` flag (default: full) for bounded output on noisy runs. Add a top-line summary: `assistant: 4124 chars, last update +12m04s`.
- **Token usage line** (when present): `usage (turn): 2.3M in / 15.7k out / 9.9k reasoning`.

JSON mode stays a superset — every new field added to `view`. CLI tools relying on `--json` get strictly more info.

**Tests:** golden-output tests in `test_snapshot_commands.py` covering each new section.

### Wave 4 — Redaction hardening

`params_preview` opens new redaction surface — `Edit` diffs may contain secrets, `Bash` commands may inline `--api-key=…`.

- Move redaction down to **`NormalizedEvent` construction time**, not just render time. The on-disk `snapshot.json` becomes redacted by default — defense in depth.
- Add patterns: env var assignments (`FOO_KEY=…`), AWS keys (`AKIA…`), GCP service-account JSON markers, generic `--password=` / `--token=` flags.
- `--no-redact` continues to work via a separate `params_raw` field stripped during snapshot persist unless `DELEGATE_KEEP_RAW_PARAMS=1` is set. Default = strip, so leaked secrets don't sit in `.delegate/`.

**Tests:** unit tests per redaction pattern + integration test asserting `snapshot.json` on disk never contains `ghp_…` even when `params_preview` captured it.

### Wave 5 — Polish & docs

- Update docs (or `WIP_SPEC.md`) snapshot section with v2 schema and sample rich output.
- Add `--events-limit N` flag to `delegate snapshot` (default 20; currently hard-coded in rendering).
- Add `--watch` flag (re-print every 2s, cleared screen) for live monitoring of running runs. Cheap, huge UX win.
- Finalize `--assistant-tail N` (introduced in Wave 3).
- Update `AGENTS.md` and `README.md` with example output so parent agents know what to expect.

## Order rationale

- **W1 first** because every later wave needs the enriched event model. Pure refactor, no behavior change → easy to land.
- **W2 before W3** because rendering has nothing to render until harnesses actually capture the data. W2 unblocks codex snapshots completely.
- **W3 is the splashy one** — biggest visible upgrade.
- **W4 is gated by W3** because the new params field creates the leak risk we then mitigate.
- **W5 is grab-bag polish** — safe to slip.

## Explicitly out of scope

- ❌ Don't make harness event types pluggable / registry-based. Three concrete `_ingest_<harness>` functions is fine; abstraction is premature.
- ❌ Don't unify the on-disk snapshot schema with the manifest. They serve different purposes.
- ❌ Don't add SSE/websocket live-streaming. `--watch` polling solves 95% of the value at 5% of the complexity.

## Risks

- **Schema bump (v1 → v2)**: existing `.delegate/runs/` snapshots keep working via tolerant `merge_snapshot_view`; only new fields are additive. Old `--json` consumers get extra keys (harmless).
- **Redaction regressions**: mitigated by W4 tests + on-disk redaction. *Without W4, W3 actively makes leaks worse* — do not ship W3 to a shared environment without W4.
- **Cursor wrapper-key parsing is fragile**: if cursor renames `readToolCall` to `read_tool_call`, our matcher breaks. Mitigate with a generic suffix-match (`endswith("ToolCall")` OR `endswith("_tool_call")`) and a fallback that still records `tool="tool"` rather than crashing.

## Suggested first PR

W1 + W2 together. Small, focused, unlocks codex snapshots which currently return literally nothing. W3+W4 can ship as a follow-up PR once we've validated the new event model is right.
