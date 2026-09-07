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
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from dataclasses import dataclass, field

from delegate_agent.json_types import JsonObject, JsonValue

# Minutes of no progress before a tracked run is cancelled. 0 disables.
STALL_MINUTES_DEFAULT = 8
SECONDS_PER_MINUTE = 60

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


def stall_seconds_from_minutes(minutes: float) -> float:
    """Convert a configured stall threshold in minutes to seconds (0 disables)."""
    if minutes <= 0:
        return 0.0
    return float(minutes) * SECONDS_PER_MINUTE


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
    """

    progress: bool = False
    deltas: tuple[str, ...] = ()
    tools_started: tuple[str, ...] = ()
    tools_finished: tuple[str, ...] = ()
    label: str | None = None


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
                return LineSignals(deltas=(f"{update_type}:{delta}",), label=update_type)
            return _NO_SIGNALS
        if update_type.endswith(_PI_END_SUFFIX):
            content = update.get("content")
            if isinstance(content, str):
                return LineSignals(deltas=(f"{update_type}:{content}",), label=update_type)
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
    """OpenCode reports tools only once they have completed, so there is no in-flight state."""
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
    if event_type in {"tool_use", "step_start", "step_finish"}:
        return LineSignals(progress=True, label=event_type)
    return None


def _classify_grok(payload: JsonObject, event_type: str) -> LineSignals | None:
    if event_type in {"text", "thought"}:
        data = payload.get("data")
        if isinstance(data, str):
            return LineSignals(deltas=(f"{event_type}:{data}",), label=event_type)
        return _NO_SIGNALS
    if event_type in {"end", "error"}:
        return LineSignals(progress=True, label=event_type)
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
    key = _tool_key(item, "id", "command", "type")
    label = _string_field(item, "type") or "item"
    if completed:
        return LineSignals(tools_finished=(key,), label=label)
    return LineSignals(tools_started=(key,), label=label)


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
    if harness in _TEXT_STREAM_HARNESSES:
        return LineSignals(deltas=(stripped,), label="text")
    try:
        payload: JsonValue = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return LineSignals(deltas=(stripped,), label="text")
    if not isinstance(payload, dict):
        return LineSignals(deltas=(stripped,), label="text")
    event_type = payload.get("type")
    label = event_type if isinstance(event_type, str) else "event"
    signals = _classify_payload(payload, event_type, harness=harness)
    if signals is None:
        # An event shape this module does not model: fall back to line
        # deduplication rather than reading it as idleness.
        return LineSignals(deltas=(stripped,), label=label)
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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _armed: bool = field(default=False, repr=False)
    _last_progress_at: float | None = field(default=None, repr=False)
    _recent_deltas: deque[str] = field(default_factory=deque, repr=False)
    _pending_tools: list[str] = field(default_factory=list, repr=False)
    _last_progress_label: str | None = field(default=None, repr=False)
    _lines_seen: int = field(default=0, repr=False)

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

    def observe_line(self, line: str, *, now: float) -> None:
        if not self.enabled or not line.strip():
            return
        signals = classify_line(line, harness=self.harness)
        with self._lock:
            self._lines_seen += 1
            if not self._armed:
                self._armed = True
                self._last_progress_at = now
            self._apply_locked(signals, now)

    def _apply_locked(self, signals: LineSignals, now: float) -> None:
        progressed = False
        for key in signals.tools_started:
            self._pending_tools.append(key)
            progressed = True
        for key in signals.tools_finished:
            if key in self._pending_tools:
                self._pending_tools.remove(key)
            elif self._pending_tools:
                # A stream that does not correlate starts with finishes (or whose
                # start we missed) must not leak an in-flight tool forever, which
                # would disable the watchdog for the rest of the run.
                self._pending_tools.pop(0)
            progressed = True
        if signals.progress:
            progressed = True
        if progressed:
            # A new phase began; content the model repeated during the last one
            # is no longer evidence of a loop.
            self._recent_deltas.clear()
        for delta in signals.deltas:
            normalized = normalize_delta(delta)
            if not normalized:
                continue
            if normalized in self._recent_deltas:
                continue
            self._recent_deltas.append(normalized)
            progressed = True
        if progressed:
            self._last_progress_at = now
            self._last_progress_label = signals.label

    def stalled_for(self, now: float) -> float | None:
        """Seconds of no progress if this run is stalled, else None."""
        if not self.enabled:
            return None
        with self._lock:
            if not self._armed or self._last_progress_at is None:
                return None
            if self._pending_tools:
                return None
            idle = now - self._last_progress_at
            if idle < self.stall_seconds:
                return None
            return idle

    def stall_detail(self, idle_seconds: float) -> JsonObject:
        with self._lock:
            return {
                "idleSeconds": round(idle_seconds, 3),
                "thresholdSeconds": round(self.stall_seconds, 3),
                "lastProgress": self._last_progress_label,
                "linesSeen": self._lines_seen,
            }
