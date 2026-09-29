"""Stalled-stream detection for tracked child runs.

A child harness can stay alive and chatty while producing nothing: the observed
case (2026-08-25) was an ``omp`` lane whose stdout carried only
``thinking_delta`` events repeating the same backtick fence for 25+ minutes with
zero tool calls, headed for its 3600 s stage timeout. A ``claude`` lane running a
long re-execute looks superficially similar -- one process, minutes between
interesting output -- but it is doing real work: distinct text arrives, tools
start and finish.

This module answers the one question that separates them: *when did this run
last make progress?* Progress is

- a tool/command execution starting or finishing,
- a turn/message/run boundary or terminal event,
- an assistant text or thinking delta whose content is new.

A delta that repeats content the stream produced recently is not progress, which
is what makes the fence loop detectable. Deltas that normalize to nothing (a
lone newline) are ignored entirely, so a loop alternating ``"```"`` with
whitespace cannot masquerade as alternating content.

Two deliberate asymmetries keep false kills rare, because killing a healthy run
costs far more than letting a stalled one reach its stage timeout:

- While a tool or command execution is in flight the run is never stalled, no
  matter how long it takes. A 40-minute test suite is the engine's problem to
  time out, not this watchdog's.
- The watchdog arms on the first stdout line. A child that writes nothing at all
  (buffered output, a slow launch) is left to the stage timeout rather than
  killed on a guess about a stream we never saw.

Unrecognized structured events fall back to whole-line deduplication, so an
engine whose vocabulary is not modeled here still gets the "identical output
forever" check without any risk of being misread as idle.

Idle time is not the only way a run stops making progress, so the watchdog can
also *trip* on a named reason (``stall_reason`` in the detail):

- ``runaway_output``: novel model output keeps arriving but no tool runs. A
  stream of unique-looking text resets the idle clock forever and used to run
  until the provider rate-limited it; past a generous character budget with no
  tool activity the run is stopped.
- ``repeated_tool_failure``: the same tool call on the same target fails the
  same way several times in a row with nothing else succeeding in between.

A repeated identical tool call (same tool, same target, same outcome as the
call just before it) is also not progress: a model re-running the same failing
or no-op command must not keep resetting the idle clock. And an external probe
(the execution worktree's HEAD) counts as progress when a silent child commits.
"""

from __future__ import annotations

import json
import math
import re
import subprocess  # nosec B404 - fixed ps argv for a stall diagnostic.
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from delegate_agent.json_types import JsonObject, JsonValue

# Minutes of no progress before a tracked run is cancelled. 0 disables.
STALL_MINUTES_DEFAULT = 8
SECONDS_PER_MINUTE = 60
# Runs at these reasoning efforts can think silently for many minutes (a Grok
# xhigh review was killed at 8), so their default silent window is longer. An
# explicit --stall-minutes, config stallMinutes, or DELEGATE_STALL_MINUTES wins.
LONG_THINKING_EFFORTS = frozenset({"xhigh", "max", "ultra"})
STALL_MINUTES_LONG_THINKING_DEFAULT = 20

STALL_MINUTES_ENV = "DELEGATE_STALL_MINUTES"
STALL_SOURCE_DEFAULT = "default"
STALL_SOURCE_EFFORT_DEFAULT = "effort_default"
STALL_SOURCE_FLAG = "flag"
STALL_SOURCE_CONFIG = "config"
STALL_SOURCE_ENV = "env"
STALL_SOURCE_HARNESS_DEFAULT = "harness_default"

_MODEL_EFFORT_SUFFIX = re.compile(r"-(?P<effort>xhigh|max|ultra)(?:-fast)?$")

# How many recent DISTINCT delta contents are remembered. A short repeating
# cycle -- the observed failure alternated an opening and a closing fence -- is
# only detectable if the memory is deeper than the cycle, and comparing against
# the immediately preceding delta alone would have missed it entirely.
RECENT_DELTA_MEMORY = 8
# Deltas are short tokens in every stream shape we model; bounding the retained
# signature keeps a pathological multi-megabyte "delta" from being remembered in
# full eight times over.
DELTA_SIGNATURE_LIMIT = 512

_WHITESPACE = re.compile(r"\s+")

# Characters of novel model output (text and thinking deltas) a run may produce
# with no tool activity at all before it is stopped as runaway output. About
# 100k tokens: far past any real final report, well short of the multi-hour
# unique-looking stream that previously ran until the provider returned 429.
RUNAWAY_OUTPUT_CHARS_DEFAULT = 400_000
# Consecutive identical failures of one tool call on one target, with no other
# tool completing in between, before the run is stopped. A model retrying the
# same broken command a few times is normal; this many is a loop.
REPEATED_TOOL_FAILURE_LIMIT = 5
# In-flight tool calls tracked at once; a stream that never closes its calls is capped.
PENDING_TOOL_LIMIT = 256

STALL_REASON_IDLE = "idle"
STALL_REASON_RUNAWAY_OUTPUT = "runaway_output"
STALL_REASON_REPEATED_TOOL_FAILURE = "repeated_tool_failure"

_TOOL_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})
# `ps` process states that mean "on CPU or runnable" on Linux and macOS.
_RUNNING_PROCESS_STATES = frozenset({"R"})
PROCESS_SNAPSHOT_LIMIT = 16
PROCESS_SAMPLE_INTERVAL_SEC = 1.0

# Codex item types that represent work in flight rather than model output.
# agent_message is the model talking, so it is classified as a delta instead.
_CODEX_MESSAGE_ITEM_TYPES = frozenset({"agent_message", "reasoning"})

# pi/omp assistantMessageEvent suffixes that carry incremental model output.
_PI_DELTA_SUFFIX = "_delta"
_PI_END_SUFFIX = "_end"

# pi/omp top-level events that mark a real boundary in the run.
_PI_BOUNDARY_TYPES = frozenset(
    {
        "turn_start",
        "turn_end",
        "message_start",
        "message_end",
        "agent_end",
        "agent_settled",
        "error",
    }
)

# Harnesses whose stdout is plain text rather than a structured envelope.
_TEXT_STREAM_HARNESSES = frozenset({"devin"})
_SILENT_TOOL_EXECUTION_HARNESSES = frozenset({"devin", "kimi"})
_TOOL_RUNNING_STATUSES = frozenset({"pending", "running", "in_progress", "started"})
_TOOL_FINISHED_STATUSES = frozenset(
    {"cancelled", "canceled", "completed", "error", "failed", "success", "succeeded"}
)


def stall_minutes_value(value: object) -> float | None:
    """A valid stall threshold in minutes (finite, >= 0; 0 disables), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def stall_seconds_from_minutes(minutes: float) -> float:
    """Convert a configured stall threshold in minutes to seconds (0 disables)."""
    if minutes <= 0:
        return 0.0
    return float(minutes) * SECONDS_PER_MINUTE


def is_long_thinking_effort(effort: str | None, model: str | None = None) -> bool:
    """True when the run's reasoning effort (or an effort-suffixed model id) is xhigh or above."""
    if effort:
        return effort.strip().lower() in LONG_THINKING_EFFORTS
    if model:
        return _MODEL_EFFORT_SUFFIX.search(model.strip().lower()) is not None
    return False


def effective_stall_seconds(
    configured_seconds: float,
    *,
    harness: str,
    timeout_seconds: int | None,
    explicitly_configured: bool,
) -> float:
    """Disable only the default detector when a silent harness has a deadline."""
    if (
        harness in _SILENT_TOOL_EXECUTION_HARNESSES
        and timeout_seconds is not None
        and not explicitly_configured
    ):
        return 0.0
    return configured_seconds


def resolve_stall_window(
    configured_seconds: float,
    *,
    harness: str,
    timeout_seconds: int | None,
    pinned: bool,
    config_explicit: bool,
    effort: str | None,
    model: str | None = None,
) -> tuple[float, str]:
    """The launch's silent-window seconds and where they came from.

    Sources: ``flag`` (--stall-minutes), ``config`` (stallMinutes),
    ``harness_default`` (silent harness with a deadline: watchdog off),
    ``effort_default`` (xhigh/max/ultra get a longer default), ``default``.
    DELEGATE_STALL_MINUTES is applied later, at spawn, and reports ``env``.
    """
    if pinned:
        return configured_seconds, STALL_SOURCE_FLAG
    if config_explicit:
        return configured_seconds, STALL_SOURCE_CONFIG
    disabled = effective_stall_seconds(
        configured_seconds,
        harness=harness,
        timeout_seconds=timeout_seconds,
        explicitly_configured=False,
    )
    if disabled != configured_seconds:
        return disabled, STALL_SOURCE_HARNESS_DEFAULT
    if is_long_thinking_effort(effort, model):
        return (
            stall_seconds_from_minutes(STALL_MINUTES_LONG_THINKING_DEFAULT),
            STALL_SOURCE_EFFORT_DEFAULT,
        )
    return configured_seconds, STALL_SOURCE_DEFAULT


def apply_env_override(
    seconds: float, source: str, *, pinned: bool, raw_env: str | None
) -> tuple[float, str]:
    """Apply DELEGATE_STALL_MINUTES (an unpinned run only); junk values are ignored."""
    if pinned or raw_env is None:
        return seconds, source
    try:
        minutes = float(raw_env)
    except ValueError:
        return seconds, source
    if not math.isfinite(minutes) or minutes < 0:
        return seconds, source
    return stall_seconds_from_minutes(minutes), STALL_SOURCE_ENV


def stall_window_record(seconds: float, source: str) -> JsonObject:
    """The manifest/dry-run record of the effective silent window."""
    return {"minutes": seconds / SECONDS_PER_MINUTE, "source": source}


def normalize_delta(text: str) -> str:
    """Collapse whitespace and bound length so repeated content compares equal.

    Returns "" for content that is whitespace-only. An empty result is ignored by
    the watchdog rather than treated as a distinct delta: a stream that emits
    ``"```"`` and ``"\\n"`` in alternation is repeating one thing, not two.
    """
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return collapsed[:DELTA_SIGNATURE_LIMIT]


@dataclass(frozen=True)
class LineSignals:
    """What one stdout line says about progress.

    ``progress`` is unconditional (tool activity, a boundary, a terminal event).
    ``deltas`` are candidate model output: each counts as progress only if its
    normalized content is not already in the recent-delta memory.
    ``delta_chars`` says how many characters of model output each delta
    carries toward the runaway budget, parallel to ``deltas``. It exists
    because a delta is a dedup key, not a payload: pi/omp and grok prefix the
    event type onto it, a pi/omp ``*_end`` event repeats text its deltas
    already carried, and a whole-line fallback is not model output at all.
    ``None`` means each delta is exactly its payload.
    """

    progress: bool = False
    deltas: tuple[str, ...] = ()
    delta_chars: tuple[int, ...] | None = None
    tools_started: tuple[str, ...] = ()
    tools_finished: tuple[str, ...] = ()
    label: str | None = None
    # (tool, target) of a finish that explicitly reported failure but that the
    # stream accumulator does not normalize into a ToolEvent: a codex
    # ``file_change``/``mcp_tool_call`` with status failed, or an ``error``
    # item. It is neither a reset of the failure streak nor progress.
    failed_finish: tuple[str, str] | None = None


_NO_SIGNALS = LineSignals()


def _string_field(payload: JsonValue, *names: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _tool_key(payload: JsonValue, *names: str) -> str:
    """Best-effort identity for a tool invocation.

    Streams that carry a call id (claude ``tool_use.id``, pi/omp
    ``toolCallId``, kimi ``tool_call_id``) pair start and finish exactly. Streams
    that carry none (codex command_execution in some versions) fall back to the
    tool/command name, and the watchdog's unmatched-finish handling covers the
    rest.
    """
    identifier = _string_field(payload, *names)
    return identifier or "tool"


def _block_delta(block: JsonObject) -> str | None:
    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text")
        return text if isinstance(text, str) else None
    if block_type == "thinking":
        thinking = block.get("thinking")
        if isinstance(thinking, str):
            return thinking
        text = block.get("text")
        return text if isinstance(text, str) else None
    return None


def _content_deltas(content: JsonValue) -> tuple[str, ...]:
    if isinstance(content, str):
        return (content,)
    if not isinstance(content, list):
        return ()
    deltas: list[str] = []
    for block in content:
        if isinstance(block, str):
            deltas.append(block)
        elif isinstance(block, dict):
            delta = _block_delta(block)
            if delta is not None:
                deltas.append(delta)
    return tuple(deltas)


def _classify_pi(payload: JsonObject, event_type: str) -> LineSignals | None:
    """pi and omp: a session/turn/message envelope with assistantMessageEvent deltas.

    Returns ``None`` for a top-level event type this module does not model, which
    the caller turns into whole-line deduplication. ``message_update`` never
    falls back that way: its line carries an accumulating message blob that
    differs on every event, so a fallback there would read a repeating model as
    healthy.
    """
    if event_type == "message_update":
        update = payload.get("assistantMessageEvent")
        if not isinstance(update, dict):
            return _NO_SIGNALS
        update_type = update.get("type")
        if not isinstance(update_type, str):
            return _NO_SIGNALS
        # Every message_update also carries an accumulating `partial`/`message`
        # blob, so the LINE always differs even when the model is repeating
        # itself. Only the delta field can answer the question.
        if update_type.endswith(_PI_DELTA_SUFFIX):
            delta = update.get("delta")
            if isinstance(delta, str):
                return LineSignals(
                    deltas=(f"{update_type}:{delta}",),
                    delta_chars=(len(delta),),
                    label=update_type,
                )
            return _NO_SIGNALS
        if update_type.endswith(_PI_END_SUFFIX):
            content = update.get("content")
            if isinstance(content, str):
                # The full block its deltas already streamed: novel as a
                # dedup key, but not new output for the runaway budget.
                return LineSignals(
                    deltas=(f"{update_type}:{content}",), delta_chars=(0,), label=update_type
                )
            return LineSignals(progress=True, label=update_type)
        return _NO_SIGNALS
    if event_type == "tool_execution_start":
        return LineSignals(
            tools_started=(_tool_key(payload, "toolCallId", "toolName"),),
            label=_string_field(payload, "toolName") or "tool",
        )
    if event_type == "tool_execution_end":
        return LineSignals(
            tools_finished=(_tool_key(payload, "toolCallId", "toolName"),),
            label=_string_field(payload, "toolName") or "tool",
        )
    if event_type in _PI_BOUNDARY_TYPES:
        return LineSignals(progress=True, label=event_type)
    if event_type in {"session", "agent_start"}:
        return _NO_SIGNALS
    return None


def _classify_opencode(payload: JsonObject, event_type: str) -> LineSignals | None:
    if event_type == "error":
        return LineSignals(progress=True, label="error")
    part = payload.get("part")
    if not isinstance(part, dict):
        return _NO_SIGNALS
    if event_type == "text":
        text = part.get("text")
        if isinstance(text, str):
            return LineSignals(deltas=(text,), label="text")
        return _NO_SIGNALS
    if event_type == "tool_use":
        state = part.get("state")
        status = _string_field(state, "status")
        normalized_status = status.lower() if status else ""
        key = _tool_key(part, "callID", "callId", "id", "tool")
        if normalized_status in _TOOL_RUNNING_STATUSES:
            return LineSignals(tools_started=(key,), label="tool_use")
        if normalized_status in _TOOL_FINISHED_STATUSES:
            return LineSignals(tools_finished=(key,), label="tool_use")
        return LineSignals(progress=True, label="tool_use")
    if event_type in {"step_start", "step_finish"}:
        return LineSignals(progress=True, label=event_type)
    return None


def _classify_grok(payload: JsonObject, event_type: str) -> LineSignals | None:
    if event_type in {"text", "thought"}:
        data = payload.get("data")
        if isinstance(data, str):
            return LineSignals(
                deltas=(f"{event_type}:{data}",), delta_chars=(len(data),), label=event_type
            )
        return _NO_SIGNALS
    if event_type in {"end", "error"}:
        return LineSignals(progress=True, label=event_type)
    if event_type == "usage":
        # Token accounting, which grows on every response: as the whole-line
        # fallback it made a model repeating itself look like progress. Same
        # rule as Claude's thinking-token system events.
        return _NO_SIGNALS
    if event_type == "tool_call":
        return LineSignals(
            tools_started=(_tool_key(payload, "toolCallId", "id", "name"),),
            label="tool_call",
        )
    if event_type == "tool_call_update":
        status = _string_field(payload, "status")
        key = _tool_key(payload, "toolCallId", "id", "name")
        if status and status.lower() in _TOOL_FINISHED_STATUSES:
            return LineSignals(tools_finished=(key,), label="tool_call_update")
        return LineSignals(progress=True, label="tool_call_update")
    return None


def _classify_kimi_role_envelope(payload: JsonObject) -> LineSignals | None:
    """Kimi speaks in untyped role envelopes with OpenAI-style tool_calls."""
    role = payload.get("role")
    if role == "assistant":
        tool_calls = payload.get("tool_calls")
        started: list[str] = []
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if isinstance(call, dict):
                    function = call.get("function")
                    started.append(
                        _string_field(call, "id") or _string_field(function, "name") or "tool"
                    )
        deltas = _content_deltas(payload.get("content"))
        if started or deltas:
            return LineSignals(deltas=deltas, tools_started=tuple(started), label="assistant")
        return _NO_SIGNALS
    if role == "tool":
        return LineSignals(
            tools_finished=(_tool_key(payload, "tool_call_id"),),
            label="tool_result",
        )
    return None


def _classify_cursor(payload: JsonObject, event_type: str) -> LineSignals | None:
    """Cursor's tool events are `(type, subtype)` with the id in `call_id`.

    The shared typed dispatch reads a dotted `tool_call.started`/`.completed`
    type that Cursor does not emit, so every tool event -- start and finish
    alike -- was pushed onto the pending list under the fallback key "tool" and
    never removed. Two unresolved tools disable the watchdog for the rest of
    the run.
    """
    if event_type == "thinking":
        # `{"type":"thinking","subtype":"delta","text":...,"timestamp_ms":N}`:
        # the timestamp makes every line unique, so the whole-line fallback read
        # one thinking token repeated forever as progress and charged nothing.
        if payload.get("subtype") != "delta":
            return _NO_SIGNALS
        text = payload.get("text")
        if isinstance(text, str):
            return LineSignals(
                deltas=(f"thinking:{text}",), delta_chars=(len(text),), label="thinking"
            )
        return _NO_SIGNALS
    if event_type == "tool_call":
        tool_call = payload.get("tool_call")
        key = _string_field(payload, "call_id") or _string_field(tool_call, "toolCallId") or "tool"
        if payload.get("subtype") == "completed":
            return LineSignals(tools_finished=(key,), label="tool_call.completed")
        return LineSignals(tools_started=(key,), label="tool_call.started")
    return _classify_typed(payload, event_type)


def _classify_codex_item(payload: JsonObject, *, completed: bool) -> LineSignals:
    item = payload.get("item")
    if not isinstance(item, dict):
        return _NO_SIGNALS
    item_type = item.get("type")
    if item_type in _CODEX_MESSAGE_ITEM_TYPES:
        if not completed:
            return _NO_SIGNALS
        deltas = _content_deltas(item.get("text")) or _content_deltas(item.get("content"))
        return LineSignals(deltas=deltas, label=str(item_type))
    label = _string_field(item, "type") or "item"
    if completed and item_type == "error":
        # A bare `item.completed` with only a message and no start: an error
        # the model hit, not a tool that finished, so it closes nothing. The
        # whole message is its identity; only the trip record is truncated.
        message = _collapse_whitespace(_string_field(item, "message") or "") or "error"
        return LineSignals(failed_finish=("error", message), label=label)
    key = _tool_key(item, "id", "command", "type")
    if completed:
        status = (_string_field(item, "status") or "").lower()
        if status in _TOOL_FAILURE_STATUSES:
            return LineSignals(
                tools_finished=(key,),
                failed_finish=(label, _codex_item_target(item)),
                label=label,
            )
        return LineSignals(tools_finished=(key,), label=label)
    return LineSignals(tools_started=(key,), label=label)


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _codex_item_target(item: JsonObject) -> str:
    """What a failed codex item was acting on, for repeated-failure identity.

    Untruncated: two commands that share a long prefix are different calls.
    The trip record truncates what it reports, not what it compares.
    """
    changes = item.get("changes")
    if isinstance(changes, list):
        paths = sorted(
            change["path"]
            for change in changes
            if isinstance(change, dict) and isinstance(change.get("path"), str)
        )
        if paths:
            return ",".join(paths)
    named = [_string_field(item, name) for name in ("server", "tool", "command", "query")]
    parts = [part for part in named if part]
    arguments = item.get("arguments")
    if arguments is not None:
        parts.append(json.dumps(arguments, sort_keys=True, default=str))
    return ":".join(parts) or _string_field(item, "type") or "item"


def _classify_claude_assistant(payload: JsonObject) -> LineSignals:
    message = payload.get("message")
    if not isinstance(message, dict):
        return _NO_SIGNALS
    content = message.get("content")
    deltas: list[str] = []
    started: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                started.append(_tool_key(block, "id", "name"))
                continue
            delta = _block_delta(block)
            if delta is not None:
                deltas.append(delta)
    elif isinstance(content, str):
        deltas.append(content)
    if not deltas and not started:
        return _NO_SIGNALS
    return LineSignals(deltas=tuple(deltas), tools_started=tuple(started), label="assistant")


def _classify_claude_user(payload: JsonObject) -> LineSignals:
    message = payload.get("message")
    if not isinstance(message, dict):
        return _NO_SIGNALS
    content = message.get("content")
    if not isinstance(content, list):
        return _NO_SIGNALS
    finished = [
        _tool_key(block, "tool_use_id")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    if not finished:
        return _NO_SIGNALS
    return LineSignals(tools_finished=tuple(finished), label="tool_result")


def _classify_typed(payload: JsonObject, event_type: str) -> LineSignals | None:
    """Shared dispatch for the stream-json family: claude, cursor, codex, droid.

    Returns ``None`` only for an event type this module does not model.
    """
    if event_type == "assistant":
        return _classify_claude_assistant(payload)
    if event_type == "user":
        return _classify_claude_user(payload)
    if event_type == "system":
        # Claude's system events include thinking-token accounting. A model stuck
        # emitting the same fence forever still burns thinking tokens, so a
        # growing counter is evidence of spend, not of progress. Ignored on
        # purpose: counting it would make the stalled case undetectable.
        return _NO_SIGNALS
    if event_type == "message":
        role = payload.get("role")
        if role not in (None, "assistant"):
            return _NO_SIGNALS
        deltas = _content_deltas(payload.get("content"))
        return LineSignals(deltas=deltas, label="message") if deltas else _NO_SIGNALS
    if event_type == "tool_call":
        return LineSignals(
            tools_started=(_tool_key(payload, "id", "tool", "name", "toolName"),),
            label="tool_call",
        )
    if event_type == "tool_result":
        return LineSignals(
            tools_finished=(_tool_key(payload, "tool_call_id", "id", "tool", "name"),),
            label="tool_result",
        )
    if event_type == "tool_call.started":
        return LineSignals(
            tools_started=(_tool_key(payload.get("tool_call"), "id", "name", "tool"),),
            label="tool_call.started",
        )
    if event_type == "tool_call.completed":
        return LineSignals(
            tools_finished=(_tool_key(payload.get("tool_call"), "id", "name", "tool"),),
            label="tool_call.completed",
        )
    if event_type in {"item.started", "item.completed"}:
        return _classify_codex_item(payload, completed=event_type == "item.completed")
    if event_type in {
        "result",
        "completion",
        "error",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "turn.error",
        "turn.cancelled",
        "turn.canceled",
    }:
        return LineSignals(progress=True, label=event_type)
    if event_type in {"reasoning", "thought"}:
        deltas = _content_deltas(payload.get("text")) or _content_deltas(payload.get("content"))
        return LineSignals(deltas=deltas, label=event_type) if deltas else _NO_SIGNALS
    return None


def classify_line(line: str, *, harness: str | None = None) -> LineSignals:
    """Classify one raw stdout line into progress signals.

    An unrecognized line -- unparseable, non-object, or a structured event whose
    shape is not modeled -- becomes a delta keyed on the whole line. Repeating
    an identical unknown line forever is still a stall; varying it is still
    progress. That keeps the watchdog usable on engines this module has never
    seen without letting it guess that silence means idleness.
    """
    stripped = line.strip()
    if not stripped:
        return _NO_SIGNALS
    # Whole-line fallbacks charge nothing to the runaway budget: the line is
    # not known to be model output (omp json mode streams tool output as
    # unmodeled `tool_execution_update` lines, for one), and the idle and
    # repeated-content checks still see it.
    if harness in _TEXT_STREAM_HARNESSES:
        return LineSignals(deltas=(stripped,), delta_chars=(0,), label="text")
    try:
        payload: JsonValue = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return LineSignals(deltas=(stripped,), delta_chars=(0,), label="text")
    if not isinstance(payload, dict):
        return LineSignals(deltas=(stripped,), delta_chars=(0,), label="text")
    event_type = payload.get("type")
    label = event_type if isinstance(event_type, str) else "event"
    signals = _classify_payload(payload, event_type, harness=harness)
    if signals is None:
        # An event shape this module does not model: fall back to line
        # deduplication rather than reading it as idleness.
        return LineSignals(deltas=(stripped,), delta_chars=(0,), label=label)
    return signals


def _classify_payload(
    payload: JsonObject,
    event_type: JsonValue,
    *,
    harness: str | None,
) -> LineSignals | None:
    if harness in {"pi", "omp"}:
        return _classify_pi(payload, event_type) if isinstance(event_type, str) else None
    if harness == "opencode":
        return _classify_opencode(payload, event_type) if isinstance(event_type, str) else None
    if harness == "grok":
        return _classify_grok(payload, event_type) if isinstance(event_type, str) else None
    if harness == "cursor":
        return _classify_cursor(payload, event_type) if isinstance(event_type, str) else None
    if not isinstance(event_type, str):
        return _classify_kimi_role_envelope(payload)
    return _classify_typed(payload, event_type)


@dataclass
class StallWatchdog:
    """Tracks time since the last real progress on one child's stdout stream.

    Thread-safe: ``observe_line`` runs on the stdout drain thread while
    ``stalled_for`` is polled from the capture loop.
    """

    stall_seconds: float
    harness: str | None = None
    recent_delta_memory: int = RECENT_DELTA_MEMORY
    runaway_output_chars: int = RUNAWAY_OUTPUT_CHARS_DEFAULT
    repeated_failure_limit: int = REPEATED_TOOL_FAILURE_LIMIT
    # Returns an opaque token that changes when the child made durable progress
    # outside its stdout (the execution worktree's HEAD), or None when unknown.
    progress_probe: Callable[[], str | None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _armed: bool = field(default=False, repr=False)
    _last_progress_at: float | None = field(default=None, repr=False)
    _recent_deltas: deque[str] = field(default_factory=deque, repr=False)
    _pending_tools: list[str] = field(default_factory=list, repr=False)
    # Parallel to `_pending_tools`: (tool name, target, monotonic start) of each
    # in-flight call, for `oldest_pending_tool`. Never consulted for stall logic.
    _pending_meta: list[tuple[str, str | None, float]] = field(default_factory=list, repr=False)
    # Bumped whenever the set of pending tools changes, so a poller can tell that
    # the oldest pending call changed or cleared without diffing its details.
    _pending_generation: int = field(default=0, repr=False)
    _last_progress_label: str | None = field(default=None, repr=False)
    _lines_seen: int = field(default=0, repr=False)
    _output_chars_since_tool: int = field(default=0, repr=False)
    _last_completion: tuple[str, str, str] | None = field(default=None, repr=False)
    _failure_signature: tuple[str, str] | None = field(default=None, repr=False)
    _failure_count: int = field(default=0, repr=False)
    # The same streak for failed finishes that carry no ToolEvent (see
    # LineSignals.failed_finish). Kept apart so the two kinds interleaving
    # (a failing test run, then a failing patch) do not reset each other.
    _unmatched_failure_signature: tuple[str, str] | None = field(default=None, repr=False)
    _unmatched_failure_count: int = field(default=0, repr=False)
    _trip: JsonObject | None = field(default=None, repr=False)
    # Time spent inside tool calls since the last progress. Idle time excludes
    # it, so a slow polling loop that repeats one call is not killed while a
    # fast loop of instant no-op calls still accumulates idle time.
    _tool_seconds_since_progress: float = field(default=0.0, repr=False)
    _tool_started_at: float | None = field(default=None, repr=False)
    _probe_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._recent_deltas = deque(maxlen=max(self.recent_delta_memory, 1))

    @property
    def enabled(self) -> bool:
        return self.stall_seconds > 0

    @property
    def last_progress_label(self) -> str | None:
        with self._lock:
            return self._last_progress_label

    @property
    def tools_in_flight(self) -> int:
        with self._lock:
            return len(self._pending_tools)

    @property
    def pending_generation(self) -> int:
        """Changes whenever a tool call starts or finishes."""
        with self._lock:
            return self._pending_generation

    def oldest_pending_tool(self, now: float) -> JsonObject | None:
        """The longest-running in-flight tool call, or None when none is pending.

        Observation only: the watchdog never fires while a tool is pending, so a
        hung call is otherwise invisible. `seconds` is how long it has run.
        """
        with self._lock:
            if not self._pending_meta:
                return None
            name, target, started = self._pending_meta[0]
        detail: JsonObject = {"name": name, "seconds": max(int(now - started), 0)}
        if target:
            detail["target"] = target
        return detail

    def prime_probe(self) -> None:
        """Record the external progress baseline before the child can act."""
        if not self.enabled or self.progress_probe is None:
            return
        token = self._run_probe()
        with self._lock:
            self._probe_token = token

    def _run_probe(self) -> str | None:
        probe = self.progress_probe
        if probe is None:
            return None
        try:
            return probe()
        except Exception:  # a diagnostic probe never breaks capture
            return None

    def observe_line(
        self,
        line: str,
        *,
        now: float,
        tool_events: Iterable[ToolEvent] = (),
    ) -> None:
        """Account one stdout line.

        ``tool_events`` are the normalized tool starts/completions the stream
        accumulator derived from this same line; they carry the tool name,
        target, and outcome the raw line classification cannot see.
        """
        # Tool bookkeeping runs even when stall enforcement is off
        # (`--stall-minutes 0`): `oldest_pending_tool` is observation, and
        # `stalled_for`/`confirm_stall` still return None while disabled.
        if not line.strip():
            return
        signals = classify_line(line, harness=self.harness)
        with self._lock:
            self._lines_seen += 1
            if not self._armed:
                self._armed = True
                self._last_progress_at = now
            self._apply_locked(signals, now, tuple(tool_events))

    def _repeats_locked(self, tool_events: tuple[ToolEvent, ...]) -> bool:
        """Whether every tool event on this line repeats the previous call.

        A start repeats when it names the tool and target that last completed;
        a completion repeats when its tool, target, and outcome all match the
        previous completion. Events with no target are never repeats: without
        one, distinct calls of the same tool are indistinguishable.
        """
        if not tool_events:
            return False
        last = self._last_completion
        repeated = True
        for event in tool_events:
            if event.target is None:
                return False
            if event.completed:
                signature = (event.tool, event.target, event.status or "")
                if signature != last:
                    repeated = False
                last = signature
            elif last is None or (event.tool, event.target) != last[:2]:
                repeated = False
        return repeated

    def _record_outcomes_locked(self, tool_events: tuple[ToolEvent, ...]) -> None:
        for event in tool_events:
            if not event.completed:
                continue
            if event.target is not None:
                self._last_completion = (event.tool, event.target, event.status or "")
            else:
                self._last_completion = None
            failed = (event.status or "").lower() in _TOOL_FAILURE_STATUSES
            if not failed:
                self._unmatched_failure_signature = None
                self._unmatched_failure_count = 0
            if not failed or event.target is None:
                self._failure_signature = None
                self._failure_count = 0
                continue
            signature = (event.tool, event.target)
            if signature == self._failure_signature:
                self._failure_count += 1
            else:
                self._failure_signature = signature
                self._failure_count = 1
            self._maybe_trip_repeated_failure_locked(signature, self._failure_count)

    def _record_unmatched_failure_locked(self, signature: tuple[str, str]) -> None:
        if signature == self._unmatched_failure_signature:
            self._unmatched_failure_count += 1
        else:
            self._unmatched_failure_signature = signature
            self._unmatched_failure_count = 1
        self._maybe_trip_repeated_failure_locked(signature, self._unmatched_failure_count)

    def _maybe_trip_repeated_failure_locked(self, signature: tuple[str, str], count: int) -> None:
        if self._trip is None and count >= max(self.repeated_failure_limit, 1):
            self._trip = {
                "stallReason": STALL_REASON_REPEATED_TOOL_FAILURE,
                "tool": signature[0],
                "target": signature[1][:DELTA_SIGNATURE_LIMIT],
                "failures": count,
            }

    def _apply_locked(
        self,
        signals: LineSignals,
        now: float,
        tool_events: tuple[ToolEvent, ...] = (),
    ) -> None:
        tool_activity = bool(signals.tools_started or signals.tools_finished)
        repeated_call = tool_activity and self._repeats_locked(tool_events)
        completed_events = sum(1 for event in tool_events if event.completed)
        # A finish the accumulator normalized into a ToolEvent (a codex
        # `command_execution`) is counted by the command streak alone. Only an
        # error item (no finish at all) or a finish with no ToolEvent (a failed
        # `file_change` or `mcp_tool_call`) belongs to the unmatched streak.
        unmatched_finish = len(signals.tools_finished) > completed_events
        if signals.failed_finish is not None:
            if unmatched_finish or not signals.tools_finished:
                # An explicit failure (a failed patch, an error item) is neither
                # a reset nor the fix loop working: it counts in its own streak.
                self._record_unmatched_failure_locked(signals.failed_finish)
        elif unmatched_finish:
            # A tool finished that the stream accumulator does not normalize
            # into a ToolEvent (codex `file_change` from apply_patch, and its
            # mcp/web-search items). It cannot be the failing call, so it
            # breaks the streak: a fix loop that patches between identical
            # test runs is working, not looping. It also ends "the same call
            # again", so the next identical run counts as progress.
            self._failure_signature = None
            self._failure_count = 0
            self._unmatched_failure_signature = None
            self._unmatched_failure_count = 0
            self._last_completion = None
        self._record_outcomes_locked(tool_events)
        progressed = False
        had_pending = bool(self._pending_tools)
        if signals.tools_started or signals.tools_finished:
            self._pending_generation += 1
        started_events = [event for event in tool_events if not event.completed]
        for index, key in enumerate(signals.tools_started):
            self._pending_tools.append(key)
            event = started_events[index] if index < len(started_events) else None
            if len(self._pending_tools) > PENDING_TOOL_LIMIT:
                # A stream that never finishes its calls must not grow this
                # without bound; the oldest is the one already least trusted.
                del self._pending_tools[0]
                del self._pending_meta[0]
            self._pending_meta.append(
                (
                    event.tool if event else signals.label or "tool",
                    event.target if event else None,
                    now,
                )
            )
            progressed = True
        for key in signals.tools_finished:
            if key in self._pending_tools:
                del self._pending_meta[self._pending_tools.index(key)]
                self._pending_tools.remove(key)
            elif self._pending_tools:
                self._pending_meta.pop(0)
                # A stream that does not correlate starts with finishes (or whose
                # start we missed) must not leak an in-flight tool forever, which
                # would disable the watchdog for the rest of the run.
                self._pending_tools.pop(0)
            # A finish that reported failure is activity, not progress.
            progressed = progressed or signals.failed_finish is None
        if not had_pending and self._pending_tools:
            self._tool_started_at = now
        elif had_pending and not self._pending_tools and self._tool_started_at is not None:
            self._tool_seconds_since_progress += max(now - self._tool_started_at, 0.0)
            self._tool_started_at = None
        if tool_activity:
            self._output_chars_since_tool = 0
        if repeated_call:
            # Re-running the call that just finished, or finishing it the same
            # way again, is activity but not progress: the idle clock and the
            # repeated-content memory both carry on from the previous phase.
            progressed = False
        if signals.progress:
            progressed = True
        if progressed:
            # A new phase began; content the model repeated during the last one
            # is no longer evidence of a loop.
            self._recent_deltas.clear()
        charges = signals.delta_chars
        for index, delta in enumerate(signals.deltas):
            normalized = normalize_delta(delta)
            if not normalized:
                continue
            if normalized in self._recent_deltas:
                continue
            self._recent_deltas.append(normalized)
            if charges is None:
                self._output_chars_since_tool += len(delta)
            elif index < len(charges):
                self._output_chars_since_tool += charges[index]
            progressed = True
        if (
            self._trip is None
            and self.runaway_output_chars > 0
            and self._output_chars_since_tool > self.runaway_output_chars
        ):
            self._trip = {
                "stallReason": STALL_REASON_RUNAWAY_OUTPUT,
                "outputChars": self._output_chars_since_tool,
                "outputCharLimit": self.runaway_output_chars,
            }
        if progressed:
            self._last_progress_at = now
            self._last_progress_label = signals.label
            self._tool_seconds_since_progress = 0.0
            if self._pending_tools:
                self._tool_started_at = now

    def stalled_for(self, now: float) -> float | None:
        """Seconds of no progress if this run is stalled, else None.

        A tripped detector (runaway output, repeated tool failure) is stalled
        immediately, whatever the idle clock says.
        """
        if not self.enabled:
            return None
        with self._lock:
            idle = self._stalled_idle_locked(now)
            if idle is None or self._trip is not None:
                return idle
        # Idle past the threshold. A silent child may still be committing in
        # its worktree; that counts as progress and restarts the clock.
        if self.progress_probe is None:
            return idle
        token = self._run_probe()
        with self._lock:
            if token is not None and token != self._probe_token:
                self._probe_token = token
                self._last_progress_at = max(now, self._last_progress_at or now)
                self._last_progress_label = "worktree_commit"
                self._tool_seconds_since_progress = 0.0
                self._recent_deltas.clear()
                return None
            # The probe blocks; the stdout thread may have seen a tool start or
            # new output meanwhile, so the answer is re-derived, not reused.
            return self._stalled_idle_locked(now)

    def confirm_stall(self, now: float) -> float | None:
        """Re-check, immediately before signalling, that the run is still stalled.

        The caller runs blocking diagnostics (process sampling) between
        ``stalled_for`` and the kill. Progress or a tool start that landed in
        that window cancels the kill. Never probes: this is the last read.
        """
        if not self.enabled:
            return None
        with self._lock:
            return self._stalled_idle_locked(now)

    def _stalled_idle_locked(self, now: float) -> float | None:
        if not self._armed or self._last_progress_at is None:
            return None
        idle = now - self._last_progress_at - self._tool_seconds_since_progress
        if self._trip is not None:
            return max(idle, 0.0)
        if self._pending_tools:
            return None
        if idle < self.stall_seconds:
            return None
        return idle

    def stall_detail(self, idle_seconds: float) -> JsonObject:
        with self._lock:
            detail: JsonObject = {
                "stallReason": STALL_REASON_IDLE,
                "idleSeconds": round(idle_seconds, 3),
                "thresholdSeconds": round(self.stall_seconds, 3),
                "lastProgress": self._last_progress_label,
                "linesSeen": self._lines_seen,
            }
            if self._trip is not None:
                detail.update(self._trip)
            return detail


@dataclass(frozen=True)
class ToolEvent:
    """One normalized tool start or completion, as the stream accumulator saw it."""

    tool: str
    target: str | None
    status: str | None
    completed: bool


def tool_events_from(events: Iterable[object]) -> tuple[ToolEvent, ...]:
    """Pick tool starts/completions out of normalized stream events."""
    picked: list[ToolEvent] = []
    for event in events:
        kind = getattr(event, "kind", None)
        if kind not in {"tool.started", "tool.completed"}:
            continue
        tool = getattr(event, "tool", None)
        target = getattr(event, "target", None)
        status = getattr(event, "status", None)
        picked.append(
            ToolEvent(
                tool=tool if isinstance(tool, str) else "tool",
                target=target if isinstance(target, str) and target.strip() else None,
                status=status if isinstance(status, str) else None,
                completed=kind == "tool.completed",
            )
        )
    return tuple(picked)


# The status word must end its line, followed only by spaces or tabs, closing
# markup, and terminal punctuation (including a trailing semicolon). That rejects prose that merely starts with a
# status word ("Status: completed the inventory") and the prompt's own template
# line ("Status: completed / blocked / failed") a child may echo back. Nothing
# in the pattern crosses a newline, so the next line cannot affect the match.
# Accepted cost: "Status: completed (with caveats)" is not a report.
_COMPLETION_REPORT_STATUS_RE = re.compile(
    r"^[ \t>*_`-]*[*_`]*status[*_`]*[ \t]*:[ \t]*[*_`]*[ \t]*"
    r"(completed|blocked|failed)[*_`.!; \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
# How far from the end of the output a completion report's status line may sit
# and still count as the run's final word.
COMPLETION_REPORT_TAIL_CHARS = 6000


def completion_report_status(text: str) -> str | None:
    """The status a trailing Delegate completion report declares, if any.

    Delegate asks every child to end with a report whose first item is
    ``Status: completed / blocked / failed``. A text-stream harness (Devin)
    has no terminal event, so this line near the end of its output is the only
    evidence that it finished before it went quiet.
    """
    tail = text[-COMPLETION_REPORT_TAIL_CHARS:]
    matches = list(_COMPLETION_REPORT_STATUS_RE.finditer(tail))
    if not matches:
        return None
    return matches[-1].group(1).lower()


def reports_completion_by_text(harness: str | None) -> bool:
    """Harnesses whose only completion evidence is a report in the output text."""
    return harness in _TEXT_STREAM_HARNESSES


def _ps_group_rows(pgid: int) -> list[tuple[int, str, float, str]]:
    try:
        completed = subprocess.run(  # nosec B603 B607 - fixed argv, no shell.
            ["ps", "-A", "-o", "pid=,pgid=,stat=,time=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    rows: list[tuple[int, str, float, str]] = []
    for raw in completed.stdout.splitlines():
        parts = raw.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group != pgid:
            continue
        rows.append((pid, parts[2], _cpu_seconds(parts[3]), parts[4].strip()))
    return rows


def _cpu_seconds(value: str) -> float:
    """Parse ps ``time`` ([[dd-]hh:]mm:ss[.cc]) into seconds; 0 when unparseable."""
    days = 0
    if "-" in value:
        day_text, value = value.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return 0.0
    total = 0.0
    try:
        for part in value.split(":"):
            total = total * 60 + float(part)
    except ValueError:
        return 0.0
    return days * 86400 + total


def process_group_activity(
    pgid: int | None,
    *,
    sample_interval: float | None = None,
    rows: Callable[[int], list[tuple[int, str, float, str]]] = _ps_group_rows,
    sleep: Callable[[float], None] = time.sleep,
) -> JsonObject:
    """What the stalled child's process group was doing when the stall fired.

    Idle stdout alone cannot tell a child waiting on its provider from one
    that is spinning silently or has died, so the group is sampled twice:

    - ``cpu_active``: CPU time grew between samples (working or looping without
      output),
    - ``waiting``: alive with no CPU growth (blocked on I/O: a provider queue,
      a network stall, or a hung read),
    - ``no_processes``: nothing left in the group,
    - ``unknown``: the group could not be inspected.
    """
    if pgid is None or pgid <= 1:
        return {"childActivity": "unknown"}
    first = rows(pgid)
    if not first:
        return {"childActivity": "no_processes"}
    sleep(PROCESS_SAMPLE_INTERVAL_SEC if sample_interval is None else sample_interval)
    second = rows(pgid)
    if not second:
        return {"childActivity": "no_processes"}
    before = {pid: cpu for pid, _stat, cpu, _cmd in first}
    grew = any(cpu > before.get(pid, 0.0) for pid, _stat, cpu, _cmd in second)
    running = any(stat[:1] in _RUNNING_PROCESS_STATES for _pid, stat, _cpu, _cmd in second)
    activity = "cpu_active" if grew or running else "waiting"
    return {
        "childActivity": activity,
        "processes": [
            {"pid": pid, "stat": stat, "cpuSeconds": round(cpu, 2), "command": cmd[:120]}
            for pid, stat, cpu, cmd in second[:PROCESS_SNAPSHOT_LIMIT]
        ],
    }
