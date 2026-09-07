from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from delegate_agent.json_types import JsonObject, JsonValue, is_non_negative_int
from delegate_agent.redaction import redact_string
from delegate_agent.run_metadata import clean_harness_session_id
from delegate_agent.terminal_states import (
    PROVIDER_CANCELLED,
    PROVIDER_MAX_TURNS,
    PROVIDER_REFUSAL,
)

RecoveryQuality = Literal[
    "explicit_completion",
    "substantive_assistant_fallback",
    "housekeeping_fallback",
]

_HOUSEKEEPING_PATTERNS = (
    re.compile(r"plan is up-to-date", re.IGNORECASE),
    re.compile(r"final report was delivered", re.IGNORECASE),
    re.compile(r"delivered in the previous message", re.IGNORECASE),
    re.compile(r"nothing (?:else|more) to (?:do|report)", re.IGNORECASE),
    re.compile(
        r"already (?:delivered|sent|provided)(?: the)?(?: final)? report",
        re.IGNORECASE,
    ),
)

_PROGRESS_PATTERNS = (
    re.compile(r"^I'll start by\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Working\.{3}$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Let me (?:check|read|look|investigate|start)\b", re.IGNORECASE | re.MULTILINE),
    re.compile(
        "^I(?: am|'m|\u2019m)\\s+(?:still\\s+)?(?:checking|investigating|reading|working)\\b",
        re.IGNORECASE | re.MULTILINE,
    ),
)

_STATUS_LINE_PATTERN = re.compile(
    r"^Status:\s*(?:completed|failed|blocked)\b",
    re.IGNORECASE | re.MULTILINE,
)
_REPORT_HEADER_PATTERN = re.compile(
    r"^##\s+(?:Summary|What I did|Verification|Files changed)\b",
    re.IGNORECASE | re.MULTILINE,
)


def is_housekeeping_assistant_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if is_substantive_assistant_text(stripped):
        return False
    for pattern in _HOUSEKEEPING_PATTERNS:
        if pattern.search(stripped):
            return True
    return any(pattern.search(stripped) for pattern in _PROGRESS_PATTERNS)


def is_substantive_assistant_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _STATUS_LINE_PATTERN.search(stripped):
        return True
    if stripped.startswith("Verdict:"):
        return True
    if _REPORT_HEADER_PATTERN.search(stripped):
        return True
    bullet_lines = [line for line in stripped.splitlines() if line.strip().startswith("- ")]
    if len(bullet_lines) >= 2:
        if len(stripped) < 80:
            for pattern in _HOUSEKEEPING_PATTERNS:
                if pattern.search(stripped):
                    return False
            for pattern in _PROGRESS_PATTERNS:
                if pattern.search(stripped):
                    return False
        return True
    if len(bullet_lines) == 1 and len(stripped) >= 80:
        return True
    if len(stripped) >= 200 and "\n" in stripped:
        return True
    for pattern in _HOUSEKEEPING_PATTERNS:
        if pattern.search(stripped):
            return False
    for pattern in _PROGRESS_PATTERNS:
        if pattern.search(stripped):
            return False
    return False


def assistant_recovery_quality_for_text(text: str) -> RecoveryQuality:
    if is_substantive_assistant_text(text):
        return "substantive_assistant_fallback"
    return "housekeeping_fallback"


# Result-quality taxonomy shared by the runner (write-time classification) and
# run-output (read-time classification). Centralized here so both channels emit
# identical warning text for the same quality verdict.
RESULT_QUALITY_OK = "ok"
RESULT_QUALITY_HOUSEKEEPING = "housekeeping_noop"
RESULT_QUALITY_EMPTY = "empty"
RESULT_QUALITY_SUSPECT_SHORT = "suspect_short"
RESULT_QUALITY_NO_ASSISTANT_TEXT = "no_assistant_text"

# Qualities that state a FACT about output rather than an OPINION about it.
# "empty" means the child wrote no completion report; "no_assistant_text" means
# the structured stream carried no assistant text at all. Neither can be a false
# positive -- absence is observed, not judged. "housekeeping_noop" and
# "suspect_short" are heuristics about the *content* of real output and can be
# wrong, so they stay warnings; a verdict that fails good runs teaches callers to
# ignore the verdict. This split is what run_status.run_succeeded acts on.
NO_OUTPUT_RESULT_QUALITIES = frozenset({RESULT_QUALITY_EMPTY, RESULT_QUALITY_NO_ASSISTANT_TEXT})


def quality_warning(quality: str, *, harness: str | None = None) -> str | None:
    """Render a human-readable warning for a result-quality verdict.

    Returns None for ``ok`` so callers can use a truthy check to decide whether
    to emit anything. The text is shared by the runner and run-output paths so a
    given verdict produces the same warning regardless of which channel
    classifies it.
    """
    if quality == RESULT_QUALITY_OK:
        return None
    if quality == RESULT_QUALITY_HOUSEKEEPING:
        if harness == "droid":
            return (
                "resultQuality=housekeeping_noop: completion report looks like a Droid "
                "no-op; rerun with a blunter findings-only prompt or reroute to codex/cursor."
            )
        return (
            "resultQuality=housekeeping_noop: completion report looks like housekeeping; "
            "rerun with a blunter findings-only prompt or inspect stdout/stderr."
        )
    if quality == RESULT_QUALITY_EMPTY:
        return (
            "resultQuality=empty: child exited 0 but wrote no completion report; "
            "inspect stdout/stderr or rerun with stricter report instructions."
        )
    if quality == RESULT_QUALITY_SUSPECT_SHORT:
        return (
            "resultQuality=suspect_short: safe-mode completion report is under 200 chars; "
            "inspect stdout/stderr or reroute if it lacks findings."
        )
    if quality == RESULT_QUALITY_NO_ASSISTANT_TEXT:
        return (
            "resultQuality=no_assistant_text: structured stream contained no assistant text; "
            "inspect stdout/stderr or reroute to a different lane."
        )
    return f"resultQuality={quality}"


ASSISTANT_TEXT_LIMIT = 30_000
ASSISTANT_TEXT_HEAD = 20_000
ASSISTANT_TEXT_TAIL = 10_000
EVENT_LIMIT = 500
EVENT_HEAD = 100
EVENT_TAIL = 400
EVENT_TEXT_LIMIT = 500

# kimi, opencode and pi must never have a raw stdout line promoted to an
# answer: their tool results carry arbitrary command output. Dropping such a
# line silently, which is what used to happen, loses a CLI banner, an auth error
# printed to stdout, or the truncated tail of a killed child. These bounds keep
# a diagnostic without turning the event stream into a copy of the child's
# stdout.
MALFORMED_SAMPLE_LIMIT = 3
MALFORMED_SAMPLE_CHARS = 200

# Every parser here is an allowlist enforced by omission: an event type that
# matches no branch is dropped with no trace, so a vendor adding an error event
# or renaming a field ships silently and the run reports empty output rather
# than a problem. Counting the types that reach no handler makes the next
# rename visible. Bounded because the type string comes from the child.
UNHANDLED_EVENT_TYPE_LIMIT = 32
UNHANDLED_EVENT_TYPE_CHARS = 64


def bounded_event_text(text: str, limit: int = EVENT_TEXT_LIMIT) -> tuple[str, bool, int]:
    """Bound retained event text for raw and normalized event surfaces.

    Returns ``(bounded, truncated, original_chars)``. When truncated, the
    bounded string fits within ``limit`` characters and ends with a ``…``
    sentinel so consumers can detect display clipping. Truncation metadata
    belongs on the event payload only when ``truncated`` is true.
    """
    original_chars = len(text)
    if original_chars <= limit:
        return text, False, original_chars
    if limit <= 0:
        return "", True, original_chars
    return text[: limit - 1] + "…", True, original_chars


def _add_bounded_event_field(payload: JsonObject, key: str, value: str) -> None:
    bounded, truncated, original_chars = bounded_event_text(value)
    payload[key] = bounded
    if truncated:
        payload["truncated"] = True
        payload["textChars" if key == "message" else f"{key}Chars"] = original_chars


# Harnesses whose streams emit assistant messages that are safe to
# surface as a recovered completion report when a run dies before its final
# completion event. Codex is excluded on purpose: its agent_message events can
# be preamble ("I'll start by..."), and only a message sealed by turn.completed
# is the real answer.
ASSISTANT_RECOVERY_HARNESSES = frozenset(
    {"cursor", "droid", "kimi", "claude", "grok", "devin", "opencode", "pi", "omp"}
)

# Engines whose stdout may carry arbitrary tool output, so a line that is not a
# JSON object is recorded as a diagnostic rather than becoming a text event.
# `omp` shares pi's parser but not this list: it reaches the same handler
# through `_ingest_pi_event`, and its stdout is a serialized event stream with
# no raw passthrough.
MALFORMED_LINE_PROTECTED_HARNESSES = frozenset({"kimi", "opencode", "pi"})


@dataclass
class NormalizedEvent:
    kind: str
    tool: str | None = None
    target: str | None = None
    path: str | None = None
    status: str | None = None
    message: str | None = None
    truncated: bool | None = None
    text_chars: int | None = None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {"kind": self.kind}
        for key, value in (
            ("tool", self.tool),
            ("target", self.target),
            ("path", self.path),
            ("status", self.status),
        ):
            if value is not None:
                _add_bounded_event_field(payload, key, value)
        if self.message is not None:
            # _ingest_text_fallback pre-bounds plain text before storing it.
            if self.truncated:
                payload["message"] = self.message
            else:
                _add_bounded_event_field(payload, "message", self.message)
        if self.truncated:
            payload["truncated"] = True
            if self.text_chars is not None:
                payload["textChars"] = self.text_chars
        return payload


class EventBuffer:
    def __init__(self) -> None:
        self._head: list[NormalizedEvent] = []
        self._tail: deque[NormalizedEvent] = deque(maxlen=EVENT_TAIL)
        self.last_by_kind: dict[str, NormalizedEvent] = {}
        self.total = 0

    def append(self, event: NormalizedEvent) -> None:
        self.total += 1
        self.last_by_kind[event.kind] = event
        if len(self._head) < EVENT_HEAD:
            self._head.append(event)
        else:
            self._tail.append(event)

    def extend(self, events: Iterable[NormalizedEvent]) -> None:
        for event in events:
            self.append(event)

    def extend_buffer(self, events: EventBuffer) -> None:
        combined_total = self.total + events.total
        self.extend(events)
        self.last_by_kind.update(events.last_by_kind)
        self.total = combined_total

    def __iter__(self) -> Iterator[NormalizedEvent]:
        yield from self._head
        yield from self._tail

    def __len__(self) -> int:
        return len(self._head) + len(self._tail)

    def __getitem__(self, index: int | slice) -> NormalizedEvent | list[NormalizedEvent]:
        return list(self)[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EventBuffer):
            other = list(other)
        return list(self) == other


_CANCELLED_REASONS = {"abort", "aborted", "cancel", "cancelled", "canceled", "interrupted"}
_FAILED_REASONS = {"error", "errored", "fail", "failed", "failure"}

MODEL_PROVENANCE_EVENT_LIMIT = 32
MODEL_PROVENANCE_EVENT_HEAD = 8

_PROVIDER_REFUSAL_CODES = frozenset(
    {
        "refusal",
        "refused",
        "provider_refusal",
        "content_filter",
        "content_filtered",
        "safety_refusal",
        "error_refusal",
    }
)
_PROVIDER_CANCELLED_CODES = frozenset(
    {"cancelled", "canceled", "provider_cancelled", "provider_canceled", "stop_cancelled"}
)
_PROVIDER_MAX_TURNS_CODES = frozenset(
    {
        "max_turns",
        "maximum_turns",
        "error_max_turns",
        "turn_limit",
        # grok 1.0.13's documented `end.stopReason` vocabulary, in both the
        # snake_case the binary emits and the CamelCase the 0.2.73 fixtures use.
        "max_turn_requests",
        "maxturnrequests",
    }
)
# grok emits this as a standalone event type rather than a stop reason. The
# vendor guide names it alongside `max_turn_requests` as the same truncation.
_MAX_TURNS_EVENT_TYPES = frozenset({"max_turns_reached"})


# Pi 0.85.1 renamed its compaction events to the bare spelling and uses it for
# manual and automatic compaction alike; Oh My Pi 18.1.13 still emits the
# `auto_` prefix. The field names on the payload (`aborted`, `willRetry`,
# `errorMessage`) are identical, so both spellings are accepted on both engines.
_PI_COMPACTION_START_TYPES = frozenset({"auto_compaction_start", "compaction_start"})
_PI_COMPACTION_END_TYPES = frozenset({"auto_compaction_end", "compaction_end"})

# Pi's StopReason union. `toolUse` and `pending` are mid-turn and correctly
# ignored; `deferred` is a real terminal state for batch provider responses and
# a deferred turn would otherwise end with no terminal at all on an exit code
# that is always 0.
# A tuple, not a set: a malformed stop reason can be an unhashable dict or
# list, and a membership test must not raise on the drain thread.
_PI_TERMINAL_STOP_REASONS = ("stop", "error", "aborted", "length", "deferred")


def _event_timestamp(payload: JsonObject) -> str:
    for key in ("timestamp", "ts", "created_at", "createdAt"):
        value = payload.get(key)
        if (
            isinstance(value, str)
            and value.strip()
            and len(value) <= 64
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return value.strip()
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _trusted_terminal_reason(payload: JsonObject, event_type: str) -> str:
    values: list[str] = [event_type]
    for key in ("stopReason", "stop_reason", "reason", "subtype", "code"):
        value = payload.get(key)
        if isinstance(value, str):
            values.append(value)
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("type", "name", "code", "message"):
            value = error.get(key)
            if isinstance(value, str):
                values.append(value)
        data = error.get("data")
        if isinstance(data, dict):
            for key in ("code", "message"):
                value = data.get(key)
                if isinstance(value, str):
                    values.append(value)
    if event_type == "error":
        message = payload.get("message")
        if isinstance(message, str):
            values.append(message)
    return " ".join(value.strip() for value in values if value.strip())


def _normalized_terminal_code(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or None


def _structured_terminal_codes(payload: JsonObject) -> set[str]:
    codes: set[str] = set()
    for key in ("stopReason", "stop_reason", "subtype", "code"):
        if (code := _normalized_terminal_code(payload.get(key))) is not None:
            codes.add(code)
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("type", "name", "code"):
            if (code := _normalized_terminal_code(error.get(key))) is not None:
                codes.add(code)
        data = error.get("data")
        if (
            isinstance(data, dict)
            and (code := _normalized_terminal_code(data.get("code"))) is not None
        ):
            codes.add(code)
    return codes


def _provider_terminal_state(payload: JsonObject, event_type: str) -> tuple[str, str] | None:
    codes = _structured_terminal_codes(payload)
    reason = _trusted_terminal_reason(payload, event_type)
    if event_type in _MAX_TURNS_EVENT_TYPES or codes & _PROVIDER_MAX_TURNS_CODES:
        return PROVIDER_MAX_TURNS, reason
    if codes & _PROVIDER_REFUSAL_CODES:
        return PROVIDER_REFUSAL, reason
    if event_type in {"turn.cancelled", "turn.canceled"} or codes & _PROVIDER_CANCELLED_CODES:
        return PROVIDER_CANCELLED, reason
    return None


def append_bounded_model_event(events: list[JsonObject], event: JsonObject) -> None:
    if len(events) < MODEL_PROVENANCE_EVENT_LIMIT:
        events.append(event)
        return
    tail_size = MODEL_PROVENANCE_EVENT_LIMIT - MODEL_PROVENANCE_EVENT_HEAD - 1
    events[:] = [*events[:MODEL_PROVENANCE_EVENT_HEAD], *events[-tail_size:], event]


def _served_model(payload: JsonObject) -> str | None:
    def clean(value: object) -> str | None:
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        return value

    for key in ("servedModel", "served_model", "model", "modelName", "model_name"):
        if (value := clean(payload.get(key))) is not None:
            return value
    message = payload.get("message")
    if isinstance(message, dict):
        for key in ("model", "modelName", "model_name"):
            if (value := clean(message.get(key))) is not None:
                return value
    return None


# Cursor's stream reports the model's DISPLAY NAME, never the id passed to
# `--model`, so a pinned run comparing the two fails 100% of the time. The
# authoritative mapping is the `displayName` that `parse_cursor_catalog` records
# for each selector; `requested_model_display_name` carries it when a caller has
# the catalog to hand. Without one, the selector's own documented shape
# (`<family>-<effort>[-fast]`, rendered as "<Family> <Effort Label>[ Fast]") is
# enough to reconstruct the expected label. Kept in step with
# `harness_discovery._CURSOR_EFFORT_LABELS` by
# test_cursor_effort_labels_match_harness_discovery.
_CURSOR_EFFORT_LABELS = {
    "none": "None",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
}
_CURSOR_SELECTOR_PATTERN = re.compile(
    r"(?P<family>.+)-(?P<effort>none|low|medium|high|xhigh|max)(?P<fast>-fast)?$"
)

# Claude reports the fully dated served id, so every documented alias trips a
# pinned run. The two evidenced equivalences are the dated suffix on a concrete
# id and the family segment of a family alias. `best` and `opusplan` map to no
# single family and are deliberately absent: they fail pinned preflight.
_CLAUDE_FAMILY_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable"})
_CLAUDE_SERVED_FAMILY_PATTERN = re.compile(r"^claude-(?P<family>[a-z]+)-")


def _label_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _cursor_expected_label_keys(requested: str) -> set[str]:
    keys = {_label_key(requested)}
    match = _CURSOR_SELECTOR_PATTERN.fullmatch(requested)
    if match is not None:
        label = _CURSOR_EFFORT_LABELS[match.group("effort")]
        suffix = "fast" if match.group("fast") else ""
        keys.add(_label_key(match.group("family")) + _label_key(label) + suffix)
    keys.discard("")
    return keys


def _cursor_pin_matches(requested: str, served: str, display_name: str | None) -> bool:
    if display_name is not None and served == display_name:
        return True
    served_key = _label_key(served)
    if not served_key:
        return False
    return served_key in _cursor_expected_label_keys(requested)


def _claude_pin_matches(requested: str, served: str) -> bool:
    # `opus[1m]` and `claude-opus-5[1m]` name a context-window variant of the
    # same model, so the documented bracket suffix is stripped before comparing.
    base = requested.split("[", 1)[0].strip()
    if not base:
        return False
    if served == base:
        return True
    if base.lower() in _CLAUDE_FAMILY_ALIASES:
        match = _CLAUDE_SERVED_FAMILY_PATTERN.match(served)
        return match is not None and match.group("family") == base.lower()
    return re.fullmatch(rf"{re.escape(base)}-\d{{8}}", served) is not None


def served_model_matches_requested(
    harness: str | None,
    requested: str,
    served: str,
    *,
    display_name: str | None = None,
) -> bool:
    """Is a served model id the pinned one, under this engine's own naming?

    Exact identity always matches. Beyond that only engine-specific equivalences
    evidenced against the vendor apply; there is deliberately no substring or
    containment rule, which would accept a genuinely different model whose name
    happens to embed the requested one.
    """
    if served == requested:
        return True
    if harness == "cursor":
        return _cursor_pin_matches(requested, served, display_name)
    if harness == "claude":
        return _claude_pin_matches(requested, served)
    return False


def _normalize_terminal_status(value: JsonValue) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = re.sub(r"[^a-z]", "", value.lower())
    if normalized in _CANCELLED_REASONS:
        return "cancelled"
    if normalized in _FAILED_REASONS:
        return "failed"
    if normalized in {"endturn", "stop", "complete", "completed", "done", "success", "succeeded"}:
        return "succeeded"
    return None


def _normalize_reported_usage(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    usage: JsonObject = {"basis": "reported"}
    # Cursor uses the camelCase spellings; grok and codex spell the cache
    # counters out in full and disagree with each other on the prefix.
    for canonical, keys in (
        ("inputTokens", ("inputTokens", "input_tokens")),
        ("outputTokens", ("outputTokens", "output_tokens")),
        (
            "cacheReadTokens",
            (
                "cacheReadTokens",
                "cache_read_tokens",
                "cacheReadInputTokens",
                "cache_read_input_tokens",
                "cached_input_tokens",
            ),
        ),
        (
            "cacheWriteTokens",
            (
                "cacheWriteTokens",
                "cache_write_tokens",
                "cacheCreationInputTokens",
                "cache_creation_input_tokens",
                "cache_write_input_tokens",
            ),
        ),
    ):
        usage[canonical] = next(
            (value[key] for key in keys if is_non_negative_int(value.get(key))), None
        )
    return usage


@dataclass
class StreamAccumulator:
    harness: str | None = None
    requested_model: str | None = None
    # The catalog `displayName` for `requested_model`, when the caller has the
    # discovery catalog to hand. Only cursor reports a display name.
    requested_model_display_name: str | None = None
    continuity_mode: str = "fungible"
    assistant_chunks: list[str] = field(default_factory=list)
    events: EventBuffer = field(default_factory=EventBuffer)
    completion_text: str | None = None
    current: str | None = None
    harness_session_id: str | None = None
    _assistant_text_cache: str | None = field(default=None, repr=False)
    _codex_completion_candidate: str | None = field(default=None, repr=False)
    _last_recoverable_assistant_text: str | None = field(default=None, repr=False)
    _last_substantive_assistant_text: str | None = field(default=None, repr=False)
    _pending_tool_uses: dict[str, tuple[str, str | None]] = field(default_factory=dict, repr=False)
    _grok_text_buffer: str = field(default="", repr=False)
    _grok_sealed_response: str = field(default="", repr=False)
    _grok_current_line: str = field(default="", repr=False)
    _last_error_message: str | None = field(default=None, repr=False)
    _opencode_step_text_chunks: list[str] = field(default_factory=list, repr=False)
    _pi_text_buffer: str = field(default="", repr=False)
    _pi_recovery_error: str | None = field(default=None, repr=False)
    terminal_event: JsonObject | None = None
    terminal_status: str | None = None
    provider_terminal_state: str | None = None
    provider_terminal_reason: str | None = None
    served_model: str | None = None
    model_observations: list[JsonObject] = field(default_factory=list)
    model_fallback_hops: list[JsonObject] = field(default_factory=list)
    model_observations_total: int = 0
    model_fallback_hops_total: int = 0
    sticky_model_turn: int | None = None
    continuity_violation: JsonObject | None = None
    turn_number: int = 0
    usage: JsonObject | None = None
    session_id: str | None = None
    structured_events_seen: int = 0
    malformed_lines: int = 0
    malformed_samples: list[str] = field(default_factory=list)
    unhandled_event_types: dict[str, int] = field(default_factory=dict)
    unhandled_event_types_truncated: bool = False

    def _record_terminal_event(
        self,
        *,
        event: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        self.terminal_status = status
        payload: JsonObject = {"event": event, "status": status}
        if reason:
            _add_bounded_event_field(payload, "reason", reason)
        self.terminal_event = payload
        self.events.append(NormalizedEvent(kind="run.completed", status=status, message=reason))

    def _invalidate_assistant_text_cache(self) -> None:
        self._assistant_text_cache = None

    def ingest_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        # Devin's stdout is plain text, not the structured stream-json envelope
        # the other harnesses emit. A Devin line that happens to look like a
        # standalone JSON object (e.g. `{"retries": 3}` inside a code snippet)
        # must never be parsed and routed through the structured-event path,
        # or it silently drops out of the recovered assistant text.
        if self.harness == "devin":
            self._ingest_text_fallback(stripped)
            return
        try:
            payload: JsonValue = json.loads(stripped)
        except RecursionError:
            # Kimi tool results can contain arbitrary command output. If an
            # excessively nested envelope cannot be classified, exposing the raw
            # line as a text event would let it become the answer.
            if self.harness in MALFORMED_LINE_PROTECTED_HARNESSES:
                self._record_malformed_line(stripped)
                return
            self._ingest_text_fallback(stripped)
            return
        except ValueError:
            # The interpreter's integer-digit limit raises plain ValueError,
            # not JSONDecodeError. Malformed child data must not kill the drain
            # thread and hide a later valid completion.
            if self.harness in MALFORMED_LINE_PROTECTED_HARNESSES:
                self._record_malformed_line(stripped)
                return
            self._ingest_text_fallback(stripped)
            return
        if not isinstance(payload, dict):
            if self.harness in MALFORMED_LINE_PROTECTED_HARNESSES:
                self._record_malformed_line(stripped)
                return
            self._ingest_text_fallback(stripped)
            return
        self.structured_events_seen += 1
        self._ingest_object(payload)

    def _record_malformed_line(self, text: str) -> None:
        """Keep a bounded, redacted trace of a stdout line that is not an event.

        The line is never surfaced as assistant text, so the protection against
        promoting a raw tool envelope to an answer is unchanged. What changes is
        that the run stops looking clean: `structured_events_seen` is what the
        runner reads to decide whether the parser owned this child's stdout, so
        counting a malformed line there is what routes an otherwise textless run
        onto the no-assistant-text quality verdict instead of the raw-stdout
        fallback that must not fire for these engines.
        """
        self.malformed_lines += 1
        self.structured_events_seen += 1
        if len(self.malformed_samples) >= MALFORMED_SAMPLE_LIMIT:
            return
        sample = redact_string(text)[:MALFORMED_SAMPLE_CHARS]
        self.malformed_samples.append(sample)
        self.events.append(NormalizedEvent(kind="stream.malformed", message=sample))

    def _ingest_text_fallback(self, text: str) -> None:
        bounded, truncated, original_chars = bounded_event_text(text)
        event = NormalizedEvent(kind="text", message=bounded)
        if truncated:
            event.truncated = True
            event.text_chars = original_chars
        self.events.append(event)
        self.current = _bounded_current_line(bounded)
        if self.harness == "devin":
            self._record_devin_assistant_text(text)

    def _ingest_object(self, payload: JsonObject) -> None:
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            self._ingest_role_content_message(payload)
            return
        if event_type in {"turn.started", "turn_start", "step_start"}:
            self.turn_number += 1
        self._observe_model(payload, event_type)
        if self.continuity_violation is not None:
            return
        provider_terminal = _provider_terminal_state(payload, event_type)
        # A provider terminal recorded for THIS event already published a
        # run.completed; the generic error/result handlers below must not add a
        # second one for the same line.
        terminal_recorded = provider_terminal is not None
        if provider_terminal is not None:
            self.provider_terminal_state, self.provider_terminal_reason = provider_terminal
            self._record_terminal_event(
                event=event_type,
                status="failed",
                reason=self.provider_terminal_reason,
            )
        self._capture_session_id(payload, event_type)
        if self.harness == "opencode":
            self._ingest_opencode_event(payload, event_type)
            return
        if self.harness in {"pi", "omp"}:
            self._ingest_pi_event(payload, event_type)
            return
        if event_type == "reasoning":
            return
        if event_type == "thought":
            return
        if event_type == "text" and self.harness == "grok":
            self._ingest_grok_text(payload)
            return
        if event_type == "end" and self.harness == "grok":
            self._ingest_grok_end(payload)
            return
        if event_type == "error" and self.harness == "grok":
            self._ingest_grok_error(payload)
            return
        if self.harness == "grok" and event_type == "usage":
            # grok emits exactly one terminal `end` per run; `usage` is the
            # documented per-response boundary. Sealing here is what keeps a
            # tool preamble out of the delivered answer.
            self._seal_grok_response()
            return
        if self.harness == "grok" and event_type == "tool_call_update":
            self._ingest_grok_tool_update(payload)
            return
        if self.harness == "grok" and event_type in _MAX_TURNS_EVENT_TYPES:
            # Already recorded as a provider max-turns terminal above; this
            # branch only keeps it out of the unhandled-type tally.
            return
        if event_type == "error":
            # Codex --json emits {"type":"error","message":...} on stdout for
            # harness-level failures (usage limits, auth). Keep the message: the
            # profile-failover classifier and the synthesized completion report
            # both read it from the accumulator.
            self._ingest_error_event(payload)
            # Text sealed before the error is pre-error output, not the turn's
            # answer. Dropping the candidate is what stops `turn.completed` from
            # promoting a preamble over the top of the failure.
            self._codex_completion_candidate = None
            if not terminal_recorded:
                # An error event is a terminal signal, as the grok and pi
                # handlers already treat it. Without this a child that errors
                # and still exits 0 promotes its pre-error preamble as a clean
                # completion report.
                self._record_terminal_event(
                    event=event_type,
                    status="failed",
                    reason=self._terminal_error_reason(payload),
                )
            return
        if event_type == "goal.summary" and self.harness == "kimi":
            self._ingest_kimi_goal_summary(payload)
            return
        if event_type == "tool_result":
            return
        if event_type == "system":
            self._ingest_system(payload)
            return
        if event_type == "thread.started":
            self._ingest_codex_thread_started(payload)
            return
        if event_type == "message":
            self._ingest_message(payload)
            return
        if event_type == "tool_call":
            if self.harness == "cursor":
                # Cursor's type is dotless; the phase rides on `subtype`.
                self._ingest_cursor_tool(payload, _string_field(payload, "subtype") or "started")
                return
            self._ingest_tool_call(payload)
            return
        if event_type == "completion":
            self._ingest_completion(payload)
            return
        # assistant/user/result are the stream-json envelope shared by the Cursor
        # and Claude Code harnesses; tool_call.* is Cursor-specific (Claude reports
        # tool activity via tool_use/tool_result content blocks instead).
        if event_type == "assistant":
            self._ingest_assistant_event(payload)
            return
        if event_type == "user":
            self._ingest_user_event(payload)
            return
        if event_type in ("tool_call.started", "tool_call.completed"):
            self._ingest_cursor_tool(payload, event_type.rsplit(".", 1)[1])
            return
        if event_type == "result":
            self._ingest_result_event(payload, terminal_recorded=terminal_recorded)
            return
        if event_type in ("turn.failed", "turn.error"):
            self._record_terminal_event(
                event=event_type,
                status="failed",
                reason=self._terminal_error_reason(payload),
            )
            return
        if event_type in ("turn.cancelled", "turn.canceled"):
            self._record_terminal_event(event=event_type, status="cancelled")
            return
        if event_type in ("item.started", "item.completed"):
            self._ingest_codex_item(payload, completed=event_type == "item.completed")
            return
        if event_type == "turn.completed":
            self._ingest_codex_turn_completed(payload)
            return
        if event_type == "turn.started":
            self._codex_completion_candidate = None
            return
        # Anything else with a "type" reached no handler. That includes benign
        # lines such as kimi 0.26.0's {"role":"meta","type":"session.resume_hint"},
        # and it also includes whatever a vendor adds next, so it is counted
        # rather than dropped in silence.
        self._record_unhandled_event_type(event_type)

    def _record_unhandled_event_type(self, event_type: str) -> None:
        name = event_type.strip()
        if not name or any(ord(char) < 32 or ord(char) == 127 for char in name):
            return
        name = name[:UNHANDLED_EVENT_TYPE_CHARS]
        if name in self.unhandled_event_types:
            self.unhandled_event_types[name] += 1
            return
        if len(self.unhandled_event_types) >= UNHANDLED_EVENT_TYPE_LIMIT:
            self.unhandled_event_types_truncated = True
            return
        self.unhandled_event_types[name] = 1

    def _observe_model(self, payload: JsonObject, event_type: str) -> None:
        model = _served_model(payload)
        if model is None:
            return
        turn = max(self.turn_number, 1)
        observed_at = _event_timestamp(payload)
        prior = self.served_model
        if model == prior:
            return
        observation: JsonObject = {
            "model": model,
            "turn": turn,
            "event": event_type,
            "observedAt": observed_at,
        }
        self.model_observations_total += 1
        append_bounded_model_event(self.model_observations, observation)
        if prior is None:
            self.served_model = model
            requested = self.requested_model
            if (
                self.continuity_mode == "pinned"
                and isinstance(requested, str)
                and requested
                and not served_model_matches_requested(
                    self.harness,
                    requested,
                    model,
                    display_name=self.requested_model_display_name,
                )
            ):
                self.continuity_violation = {
                    "reason": "served_model_mismatch",
                    "requestedModel": requested,
                    "servedModel": model,
                    "turn": turn,
                    "observedAt": observed_at,
                }
                self._record_terminal_event(
                    event="model.continuity_paused",
                    status="failed",
                    reason=f"pinned model {requested} was replaced by {model}",
                )
            return
        hop: JsonObject = {
            "fromModel": prior,
            "toModel": model,
            "reason": "harness_reported_model_switch",
            "turn": turn,
            "stickyFromTurn": turn,
            "observedAt": observed_at,
        }
        self.model_fallback_hops_total += 1
        append_bounded_model_event(self.model_fallback_hops, hop)
        self.served_model = model
        self.sticky_model_turn = turn
        if self.continuity_mode == "pinned":
            self.continuity_violation = {
                "reason": "mid_session_model_switch",
                "requestedModel": self.requested_model,
                "fromModel": prior,
                "servedModel": model,
                "turn": turn,
                "observedAt": observed_at,
            }
            self._record_terminal_event(
                event="model.continuity_paused",
                status="failed",
                reason=f"pinned model changed from {prior} to {model} at turn {turn}",
            )

    def _capture_session_id(self, payload: JsonObject, event_type: str) -> None:
        candidate: object = None
        if self.harness == "codex" and event_type == "thread.started":
            candidate = payload.get("thread_id")
        elif self.harness in {"claude", "cursor"} and event_type in {"system", "result"}:
            candidate = payload.get("session_id", payload.get("sessionId"))
            if candidate is None and self.harness == "cursor":
                candidate = payload.get("chat_id", payload.get("chatId"))
        elif self.harness == "omp" and event_type == "session":
            candidate = payload.get("id")
        elif self.harness == "grok" and event_type == "end":
            candidate = payload.get("sessionId")
        if (
            isinstance(candidate, str)
            and candidate
            and candidate == candidate.strip()
            and len(candidate) <= 512
            and candidate[0].isalnum()
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in candidate)
        ):
            self.session_id = candidate

    def _ingest_error_event(self, payload: JsonObject) -> None:
        # Anthropic- and OpenAI-shaped errors nest the text under `error`; codex
        # and grok put it at the top level. `_terminal_error_reason` already read
        # both, so reading only the top level here dropped the whole event.
        message = _string_field(payload, "message")
        if message is None:
            error = payload.get("error")
            if isinstance(error, dict):
                message = _string_field(error, "message")
        if message:
            self._last_error_message = message
            self.events.append(NormalizedEvent(kind="error", message=self._last_error_message))
            self.current = _bounded_current_line(self._last_error_message)

    def _terminal_error_reason(self, payload: JsonObject) -> str | None:
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return self._last_error_message

    def _ingest_system(self, payload: JsonObject) -> None:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.current = f"session cwd {cwd}"
        session_id = payload.get("session_id")
        if session_id is not None:
            self._ingest_harness_session_id(session_id)

    def _ingest_codex_thread_started(self, payload: JsonObject) -> None:
        thread_id = payload.get("thread_id")
        if thread_id is not None:
            self._ingest_harness_session_id(thread_id)

    def _ingest_harness_session_id(self, raw_id: object) -> None:
        clean_id = clean_harness_session_id(raw_id)
        if clean_id is not None:
            self.harness_session_id = clean_id
        else:
            self.events.append(
                NormalizedEvent(kind="session.invalid", message="Invalid harness session ID")
            )

    def _ingest_message(self, payload: JsonObject) -> None:
        role = payload.get("role")
        if role not in (None, "assistant"):
            return
        text = _extract_text(payload.get("content"))
        if text:
            self._record_recoverable_assistant_text(text)

    def _ingest_role_content_message(self, payload: JsonObject) -> None:
        role = payload.get("role")
        if self.harness == "kimi":
            # Kimi's stream-json speaks in untyped role envelopes: tool
            # invocations ride on assistant messages as OpenAI-style
            # tool_calls, and results arrive as role=="tool" lines correlated
            # back by tool_call_id.
            if role == "assistant":
                # Capture prose before tool calls: a combined
                # content+tool_calls envelope must leave `current` on the
                # active tool, not on the stale assistant text.
                text = _extract_text(payload.get("content"))
                if text:
                    self._record_recoverable_assistant_text(text)
                self._ingest_kimi_tool_calls(payload)
                return
            if role == "tool":
                self._ingest_kimi_tool_result(payload)
            return
        if role != "assistant":
            return
        text = _extract_text(payload.get("content"))
        if text:
            self._record_recoverable_assistant_text(text)

    def _ingest_kimi_tool_calls(self, payload: JsonObject) -> None:
        tool_calls = payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            return
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            tool = _string_field(function, "name") or "tool"
            target = _kimi_tool_target(function.get("arguments"))
            tool_id = _string_field(call, "id")
            if tool_id:
                self._pending_tool_uses[tool_id] = (tool, target)
            self.events.append(
                NormalizedEvent(
                    kind="tool.started",
                    tool=tool,
                    target=target,
                    path=target,
                )
            )
            self.current = _tool_current(tool, target)

    def _ingest_kimi_goal_summary(self, payload: JsonObject) -> None:
        """Preserve the only accounting a Kimi `/goal` run ever emits.

        A `/goal` prompt in print mode exits 0 on `complete`, 3 on `blocked` and
        6 on `paused`, and writes one extra `goal.summary` line. The line has a
        `type` but no `role`, so it fell through every branch: a paused goal was
        published as a bare non-zero failure with no reason at all.
        """
        status = _string_field(payload, "status")
        parts: list[str] = []
        if status:
            parts.append(f"status={status}")
        if reason := _string_field(payload, "reason"):
            parts.append(f"reason={reason}")
        for key, label in (("turnsUsed", "turns"), ("tokensUsed", "tokens")):
            value = payload.get(key)
            if is_non_negative_int(value):
                parts.append(f"{label}={value}")
        self._record_terminal_event(
            event="kimi.goal_summary",
            status="succeeded" if status == "complete" else "failed",
            reason=" ".join(parts) or None,
        )

    def _ingest_kimi_tool_result(self, payload: JsonObject) -> None:
        tool_id = _string_field(payload, "tool_call_id")
        tool, target = (
            self._pending_tool_uses.pop(tool_id, (None, None)) if tool_id else (None, None)
        )
        tool = tool or "tool"
        # kimi 0.26.0 tool results carry no is_error/status field, so status
        # stays None (unknown) rather than an invented success. The result
        # content is never read into the event, matching the no-leakage
        # convention of the Claude tool_result path.
        self.events.append(
            NormalizedEvent(
                kind="tool.completed",
                tool=tool,
                target=target,
                path=target,
                status=None,
            )
        )
        self.current = _tool_current(tool, target)

    def _ingest_assistant_event(self, payload: JsonObject) -> None:
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            text = _extract_text(content)
            if text:
                self._record_recoverable_assistant_text(text)
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool = _string_field(block, "name") or "tool"
                        target = _tool_use_target(block)
                        tool_id = _string_field(block, "id")
                        if tool_id:
                            self._pending_tool_uses[tool_id] = (tool, target)
                        self.events.append(
                            NormalizedEvent(
                                kind="tool.started",
                                tool=tool,
                                target=target,
                                path=target,
                            )
                        )
                        self.current = _tool_current(tool, target)

    def _ingest_user_event(self, payload: JsonObject) -> None:
        message = payload.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_id = _string_field(block, "tool_use_id")
            tool, target = (
                self._pending_tool_uses.pop(tool_id, (None, None)) if tool_id else (None, None)
            )
            tool = tool or "tool"
            status = "error" if block.get("is_error") is True else "success"
            self.events.append(
                NormalizedEvent(
                    kind="tool.completed",
                    tool=tool,
                    target=target,
                    path=target,
                    status=status,
                )
            )
            self.current = _tool_current(tool, target)

    def _record_recoverable_assistant_text(self, text: str) -> None:
        stripped = self._record_assistant_text(text)
        if stripped:
            self._last_recoverable_assistant_text = stripped
            if is_substantive_assistant_text(stripped):
                self._last_substantive_assistant_text = stripped

    def _record_devin_assistant_text(self, text: str) -> None:
        # Devin's stdout is delivered one line at a time with no explicit
        # message boundaries, so every line ingested while harness == "devin"
        # belongs to the same running block of output. Merge into the last
        # chunk (joined by "\n") instead of appending a new chunk each line,
        # which would otherwise get "\n\n"-joined into blank-line-separated
        # paragraphs in assistant_text.
        stripped = text.strip()
        if not stripped:
            return
        if self.assistant_chunks:
            self.assistant_chunks[-1] = f"{self.assistant_chunks[-1]}\n{stripped}"
        else:
            self.assistant_chunks.append(stripped)
        self._invalidate_assistant_text_cache()
        self.current = _current_from_text(stripped)
        merged = self.assistant_chunks[-1]
        self._last_recoverable_assistant_text = merged
        if is_substantive_assistant_text(merged):
            self._last_substantive_assistant_text = merged

    def _record_successful_completion_text(self, text: str) -> None:
        self._record_assistant_text(text, completion=True)
        self._record_terminal_event(event="completion", status="succeeded")

    def _ingest_completion(self, payload: JsonObject) -> None:
        final_text = payload.get("finalText")
        if isinstance(final_text, str) and final_text.strip():
            self._record_successful_completion_text(final_text)

    def _ingest_result_event(self, payload: JsonObject, *, terminal_recorded: bool = False) -> None:
        if self.harness == "cursor":
            usage = _normalize_reported_usage(payload.get("usage"))
            if usage is not None:
                self.usage = usage
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            if payload.get("is_error") is True:
                if not terminal_recorded:
                    self._record_terminal_event(event="result", status="failed")
                self._record_recoverable_assistant_text(result)
                return
            self._record_successful_completion_text(result)
            return
        if payload.get("is_error") is True and not terminal_recorded:
            # `error_during_execution`, `error_api` and any unlisted subtype
            # arrive with no string `result`. Only `error_max_turns` is rescued
            # upstream by the provider-terminal table, so without this the whole
            # event -- terminal, reason and all -- was dropped.
            self._record_terminal_event(
                event="result",
                status="failed",
                reason=_string_field(payload, "subtype"),
            )

    def _ingest_codex_item(self, payload: JsonObject, *, completed: bool) -> None:
        item = payload.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "agent_message":
            if not completed:
                return
            text = _extract_text(item.get("text")) or _extract_text(item.get("content"))
            if text:
                stripped = self._record_assistant_text(text)
                self._codex_completion_candidate = stripped
            else:
                self._codex_completion_candidate = None
            return
        # Every other item type is activity, not the turn's answer: an
        # `agent_message` followed by one is preamble. `apply_patch` surfaces as
        # `file_change`, and there are also `mcp_tool_call`, `collab_tool_call`,
        # `web_search`, `todo_list`, `reasoning` and `error` items, so clearing
        # only on `command_execution` let a run that ended in a patch or a
        # search promote its "I'll start by..." intro as the completion report.
        self._codex_completion_candidate = None
        if item_type == "command_execution":
            self._ingest_codex_command_execution(item, completed=completed)

    def _ingest_codex_command_execution(self, item: JsonObject, *, completed: bool) -> None:
        command = _string_field(item, "command")
        status = _codex_command_status(_string_field(item, "status"), completed=completed)
        kind = "tool.completed" if completed else "tool.started"
        self.events.append(
            NormalizedEvent(
                kind=kind,
                tool="command_execution",
                target=command,
                status=status,
            )
        )
        self.current = _tool_current("command_execution", command)

    def _ingest_codex_turn_completed(self, payload: JsonObject) -> None:
        usage = _normalize_reported_usage(payload.get("usage"))
        if usage is not None:
            self.usage = usage
        if not self._codex_completion_candidate:
            return
        self.completion_text = self._codex_completion_candidate
        # `turn.completed` sealing a fresh agent message is codex's success
        # terminal. Recording it is what lets a run that errored and then
        # recovered end clean, while a turn with nothing left to seal keeps
        # whatever failure the stream already reported.
        self._record_terminal_event(event="turn.completed", status="succeeded")

    def _ingest_grok_text(self, payload: JsonObject) -> None:
        data = payload.get("data")
        if not isinstance(data, str) or not data:
            return
        self._grok_text_buffer += data
        if "\n" in data:
            self._grok_current_line = data.rsplit("\n", 1)[1]
        else:
            self._grok_current_line += data
        self.current = _bounded_current_line(self._grok_current_line.strip())
        self._invalidate_assistant_text_cache()

    def _grok_live_text(self) -> str:
        """The current response: the open buffer, else the last sealed one."""
        return self._grok_text_buffer.strip() or self._grok_sealed_response

    def _seal_grok_response(self) -> None:
        text = self._grok_text_buffer.strip()
        if text:
            self._grok_sealed_response = text
        self._grok_text_buffer = ""
        self._grok_current_line = ""
        self._invalidate_assistant_text_cache()

    def _take_grok_text(self) -> str:
        text = self._grok_live_text()
        self._grok_text_buffer = ""
        self._grok_sealed_response = ""
        self._grok_current_line = ""
        self._invalidate_assistant_text_cache()
        return text

    def _ingest_grok_end(self, payload: JsonObject) -> None:
        text = self._take_grok_text()
        usage = _normalize_reported_usage(payload.get("usage"))
        if usage is not None:
            cost = payload.get("total_cost_usd")
            if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
                usage["costUsd"] = float(cost)
            self.usage = usage
        # The event shape, the single terminal `end`, and the snake_case
        # stopReason vocabulary (end_turn, max_tokens, max_turn_requests,
        # refusal, cancelled) are validated against grok 1.0.13. The 0.2.73
        # CamelCase spellings normalize onto the same tokens.
        stop_reason = payload.get("stopReason")
        reason = stop_reason if isinstance(stop_reason, str) else None
        terminal_status = _normalize_terminal_status(stop_reason)
        if (
            terminal_status is None
            and _grok_stop_reason_incomplete(stop_reason)
            and self.provider_terminal_state is None
        ):
            # max_tokens truncates the answer. Without a terminal the run rides
            # on the exit code, which is 0, and a truncated answer is published
            # as a clean success.
            terminal_status = "failed"
        if _grok_stop_reason_succeeded(stop_reason):
            if text:
                self._record_successful_completion_text(text)
            else:
                self._record_terminal_event(event="grok.end", status="succeeded")
            return
        if terminal_status in {"cancelled", "failed"}:
            self._record_terminal_event(event="grok.end", status=terminal_status, reason=reason)
        if text:
            self._record_recoverable_assistant_text(text)

    def _ingest_grok_error(self, payload: JsonObject) -> None:
        # Grok streaming-json emits {"type":"error","message":...} on failure and
        # then exits nonzero, so the runner already marks the run failed via exit
        # code. Surface the message (plus any partial buffered text) as recoverable
        # assistant text so it lands in the snapshot instead of being dropped.
        partial = self._take_grok_text()
        if partial:
            self._record_recoverable_assistant_text(partial)
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            text = message.strip()
            self._record_recoverable_assistant_text(text)
            self.current = _bounded_current_line(text)
        self._record_terminal_event(event="grok.error", status="failed")

    def _reset_opencode_step_text_state(self) -> None:
        # OpenCode emits one assistant turn per step. Mid-step prose from a
        # tool-calling step must not pollute the published assistant surface or
        # recovery fields once the next step begins.
        self._opencode_step_text_chunks = []
        self.assistant_chunks = []
        self.completion_text = None
        self._last_recoverable_assistant_text = None
        self._last_substantive_assistant_text = None
        self._invalidate_assistant_text_cache()

    def _ingest_opencode_event(self, payload: JsonObject, event_type: str) -> None:
        if event_type == "error":
            self._ingest_opencode_error(payload)
            return
        part = payload.get("part")
        if not isinstance(part, dict):
            self._record_unhandled_event_type(event_type)
            return
        part_type = part.get("type")
        if event_type == "step_start" and part_type == "step-start":
            self._reset_opencode_step_text_state()
            return
        if event_type == "text" and part_type == "text":
            self._ingest_opencode_text(part)
            return
        if event_type == "tool_use" and part_type == "tool":
            self._ingest_opencode_tool(part)
            return
        if event_type == "step_finish" and part_type == "step-finish":
            self._ingest_opencode_step_finish(part)
            return
        self._record_unhandled_event_type(event_type)

    def _ingest_opencode_text(self, part: JsonObject) -> None:
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        stripped = text.strip()
        # Keep text step-local until step_finish seals a stop turn. Publish the
        # current step's joined text as the sole assistant_chunks entry so
        # assistant_text / bounded_assistant_text never concatenate prior steps.
        self._opencode_step_text_chunks.append(stripped)
        published = "\n\n".join(self._opencode_step_text_chunks).strip()
        self.assistant_chunks = [published] if published else []
        self._invalidate_assistant_text_cache()
        self.current = _current_from_text(stripped)
        self._last_recoverable_assistant_text = published
        if is_substantive_assistant_text(published):
            self._last_substantive_assistant_text = published

    def _ingest_opencode_tool(self, part: JsonObject) -> None:
        # OpenCode does not emit a "permission denied" event for denied tools:
        # denied tools are removed from the model's schema, and the run usually
        # continues as ordinary text with exit 0. Do not infer denial here.
        tool = _string_field(part, "tool") or "tool"
        state = part.get("state")
        status = _completed_tool_status(state.get("status") if isinstance(state, dict) else None)
        target = _opencode_tool_target(part)
        self.events.append(
            NormalizedEvent(
                kind="tool.completed",
                tool=tool,
                target=target,
                path=target,
                status=status,
            )
        )
        self.current = _tool_current(tool, target)

    def _ingest_opencode_step_finish(self, part: JsonObject) -> None:
        reason = _string_field(part, "reason")
        if reason != "stop":
            return
        text = "\n\n".join(self._opencode_step_text_chunks).strip()
        if text:
            self.assistant_chunks = [text]
            self.completion_text = text
            self._invalidate_assistant_text_cache()
        self._record_terminal_event(
            event="opencode.step_finish",
            status="succeeded",
            reason=reason,
        )

    def _ingest_opencode_error(self, payload: JsonObject) -> None:
        error = payload.get("error")
        if not isinstance(error, dict):
            self._record_terminal_event(event="opencode.error", status="failed")
            return
        name = _string_field(error, "name")
        data = error.get("data")
        message = _string_field(data, "message") if isinstance(data, dict) else None
        reason = ": ".join(part for part in (name, message) if part)
        self._record_terminal_event(
            event="opencode.error",
            status="failed",
            reason=reason or None,
        )
        if reason:
            self.current = _bounded_current_line(reason)

    def _reset_pi_turn_text(self) -> None:
        self._pi_text_buffer = ""
        self.assistant_chunks = []
        self.completion_text = None
        self._last_recoverable_assistant_text = None
        self._last_substantive_assistant_text = None
        self._invalidate_assistant_text_cache()

    def _publish_pi_text(self, text: str, *, completion: bool = False) -> None:
        stripped = text.strip()
        if not stripped:
            return
        self._pi_text_buffer = stripped
        self.assistant_chunks = [stripped]
        self._last_recoverable_assistant_text = stripped
        if is_substantive_assistant_text(stripped):
            self._last_substantive_assistant_text = stripped
        if completion:
            self.completion_text = stripped
        self.current = _current_from_text(stripped)
        self._invalidate_assistant_text_cache()

    def _ingest_pi_event(self, payload: JsonObject, event_type: str) -> None:
        if event_type == "auto_retry_start":
            self._clear_pi_terminal()
            self._pi_recovery_error = (
                _string_field(payload, "errorMessage")
                or self._pi_recovery_error
                or self._last_error_message
                or "Provider retry ended without a terminal result."
            )
            self.completion_text = None
            return
        if event_type in _PI_COMPACTION_START_TYPES:
            if (
                self.terminal_status in {"failed", "cancelled"}
                or self._pi_recovery_error is not None
            ):
                self._clear_pi_terminal()
                self.completion_text = None
            return
        if event_type == "auto_retry_end" or event_type in _PI_COMPACTION_END_TYPES:
            failed = (
                payload.get("success") is False
                if event_type == "auto_retry_end"
                else _pi_compaction_failed(payload, self._pi_recovery_error, self.terminal_status)
            )
            if failed:
                reason = (
                    _string_field(payload, "finalError", "errorMessage")
                    or self._pi_recovery_error
                    or "Provider recovery failed."
                )
                self._pi_recovery_error = reason
                self._ingest_error_event({"message": reason})
                self._record_terminal_event(
                    event=f"{self.harness}.{event_type}", status="failed", reason=reason
                )
            return
        if event_type == "turn_start":
            self._reset_pi_turn_text()
            self._clear_pi_terminal()
            return
        if event_type == "message_update":
            update = payload.get("assistantMessageEvent")
            if isinstance(update, dict) and update.get("type") == "text_delta":
                delta = update.get("delta")
                if isinstance(delta, str):
                    self._publish_pi_text(self._pi_text_buffer + delta)
            return
        if event_type == "message_end":
            message = payload.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                self._publish_pi_text(_extract_text(message.get("content")))
            return
        if event_type == "tool_execution_start":
            self._ingest_pi_tool(payload, completed=False)
            return
        if event_type == "tool_execution_end":
            self._ingest_pi_tool(payload, completed=True)
            return
        if event_type == "turn_end":
            message = payload.get("message")
            role = message.get("role") if isinstance(message, dict) else None
            stop_reason = (
                message.get("stopReason")
                if isinstance(message, dict)
                else payload.get("stopReason")
            )
            if role != "assistant":
                return
            error_status = message.get("errorStatus")
            http_error = (
                isinstance(error_status, int)
                and not isinstance(error_status, bool)
                and error_status >= 400
            )
            if stop_reason not in _PI_TERMINAL_STOP_REASONS and not http_error:
                return
            status = (
                "cancelled"
                if stop_reason == "aborted"
                else "succeeded"
                if stop_reason == "stop" and not http_error
                else "failed"
            )
            text = _extract_text(message.get("content"))
            if text:
                self._publish_pi_text(text, completion=status == "succeeded")
            reason = None
            if status != "succeeded":
                self.completion_text = None
                reason = (
                    _string_field(message, "errorMessage") or f"Provider stopped: {stop_reason}"
                )
                self._ingest_error_event({"message": reason})
            else:
                self._last_error_message = None
            self._pi_recovery_error = reason
            self._record_terminal_event(
                event=f"{self.harness}.turn_end", status=status, reason=reason
            )
            return
        if event_type == "error":
            self._ingest_error_event(payload)
            self._record_terminal_event(event=f"{self.harness}.error", status="failed")
            return
        if event_type == "notice":
            if _string_field(payload, "level") == "error":
                # A session-layer error notice is not a terminal on its own, but
                # its text is what the failover classifier and the synthesized
                # completion report read out of the accumulator.
                self._ingest_error_event(payload)
            return
        self._record_unhandled_event_type(event_type)

    def _clear_pi_terminal(self) -> None:
        # A new turn/compaction suspends terminal shutdown, not the obligation
        # to recover from an observed failure before an exit-zero EOF.
        if self.terminal_status in {"failed", "cancelled"}:
            self._pi_recovery_error = (
                _string_field(self.terminal_event or {}, "reason")
                or self._last_error_message
                or "Provider recovery ended without a successful terminal result."
            )
        self.terminal_status = None
        self.terminal_event = None
        self.provider_terminal_state = None
        self.provider_terminal_reason = None

    def finish_stream(self) -> None:
        """An interrupted harness recovery cannot turn an exit-zero child into success."""
        if self._pi_recovery_error is not None and self.terminal_status is None:
            self._record_terminal_event(
                event=f"{self.harness}.recovery_incomplete",
                status="failed",
                reason=self._pi_recovery_error,
            )

    def _ingest_pi_tool(self, payload: JsonObject, *, completed: bool) -> None:
        tool = _string_field(payload, "toolName") or "tool"
        args = payload.get("args")
        target = _tool_use_target({"input": args}) if isinstance(args, dict) else None
        status = None
        if completed:
            status = "error" if payload.get("isError") is True else "success"
        self.events.append(
            NormalizedEvent(
                kind="tool.completed" if completed else "tool.started",
                tool=tool,
                target=target,
                path=target,
                status=status,
            )
        )
        self.current = _tool_current(tool, target)

    def _ingest_tool_call(self, payload: JsonObject) -> None:
        tool = _string_field(payload, "tool", "name", "toolName") or "tool"
        target = _tool_target(payload)
        tool_id = _string_field(payload, "toolCallId", "call_id", "id")
        if tool_id:
            # grok resolves the call later on a `tool_call_update` that carries
            # only the id, so the name and target have to be remembered here.
            self._pending_tool_uses[tool_id] = (tool, target)
        self.events.append(
            NormalizedEvent(kind="tool.started", tool=tool, target=target, path=target),
        )
        self.current = _tool_current(tool, target)

    def _ingest_grok_tool_update(self, payload: JsonObject) -> None:
        status = _string_field(payload, "status")
        if status is None:
            # grok emits a first update with `status: null` carrying only
            # `locations`. The tool has not resolved, so nothing completes.
            return
        tool_id = _string_field(payload, "toolCallId")
        tool, target = (
            self._pending_tool_uses.pop(tool_id, (None, None)) if tool_id else (None, None)
        )
        tool = tool or "tool"
        if target is None:
            target = _grok_update_target(payload)
        self.events.append(
            NormalizedEvent(
                kind="tool.completed",
                tool=tool,
                target=target,
                path=target,
                status=_completed_tool_status(status),
            )
        )
        self.current = _tool_current(tool, target)

    def _ingest_cursor_tool(self, payload: JsonObject, subtype: str) -> None:
        tool_call = payload.get("tool_call")
        if not isinstance(tool_call, dict):
            return
        named = _cursor_tool_body(tool_call)
        if named is None:
            tool = _string_field(tool_call, "name", "tool") or "tool"
            body = tool_call
        else:
            tool, body = named
        target = _tool_target(body) or _tool_target(tool_call)
        call_id = _string_field(payload, "call_id") or _string_field(tool_call, "toolCallId")
        completed = subtype == "completed"
        if not completed:
            if call_id:
                self._pending_tool_uses[call_id] = (tool, target)
            self.events.append(
                NormalizedEvent(kind="tool.started", tool=tool, target=target, path=target)
            )
            self.current = _tool_current(tool, target)
            return
        pending_tool, pending_target = (
            self._pending_tool_uses.pop(call_id, (None, None)) if call_id else (None, None)
        )
        if named is None and pending_tool:
            tool = pending_tool
        target = target or pending_target
        self.events.append(
            NormalizedEvent(
                kind="tool.completed",
                tool=tool,
                target=target,
                path=target,
                status=_cursor_tool_status(body),
            )
        )
        self.current = _tool_current(tool, target)

    def _record_assistant_text(self, text: str, *, completion: bool = False) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        if completion:
            self.completion_text = stripped
        self.assistant_chunks.append(stripped)
        self._invalidate_assistant_text_cache()
        self.current = _current_from_text(stripped)
        return stripped

    @property
    def assistant_text(self) -> str:
        if self._assistant_text_cache is None:
            base = "\n\n".join(chunk for chunk in self.assistant_chunks if chunk).strip()
            grok = self._grok_live_text()
            if grok:
                base = f"{base}\n\n{grok}" if base else grok
            self._assistant_text_cache = base
        return self._assistant_text_cache

    def bounded_assistant_text(self) -> tuple[str, JsonObject]:
        text = self.assistant_text
        if len(text) <= ASSISTANT_TEXT_LIMIT:
            meta = {
                "assistantText": text,
                "assistantTextChars": len(text),
                "assistantTextTruncated": False,
                "assistantTextLimitChars": ASSISTANT_TEXT_LIMIT,
                "assistantTextOmittedMiddleChars": 0,
            }
            return text, meta
        head = text[:ASSISTANT_TEXT_HEAD]
        tail = text[-ASSISTANT_TEXT_TAIL:]
        omitted = len(text) - ASSISTANT_TEXT_HEAD - ASSISTANT_TEXT_TAIL
        bounded = f"{head}\n\n… [{omitted} chars omitted] …\n\n{tail}"
        meta = {
            "assistantText": bounded,
            "assistantTextChars": len(text),
            "assistantTextTruncated": True,
            "assistantTextLimitChars": ASSISTANT_TEXT_LIMIT,
            "assistantTextOmittedMiddleChars": max(omitted, 0),
        }
        return bounded, meta

    @property
    def recoverable_assistant_text(self) -> str | None:
        self._refresh_grok_recovery_text()
        if self._last_substantive_assistant_text:
            return self._last_substantive_assistant_text
        return self._last_recoverable_assistant_text

    def _refresh_grok_recovery_text(self) -> None:
        text = self._grok_live_text()
        if not text:
            return
        self._last_recoverable_assistant_text = text
        if is_substantive_assistant_text(text):
            self._last_substantive_assistant_text = text

    def assistant_recovery_quality(self) -> RecoveryQuality | None:
        if self.completion_text:
            return "explicit_completion"
        text = self.recoverable_assistant_text
        if not text:
            return None
        return assistant_recovery_quality_for_text(text)

    def bounded_recent_events(self) -> tuple[list[JsonObject], JsonObject]:
        serialized = [event.to_dict() for event in self.events]
        total = self.events.total
        if total <= EVENT_LIMIT:
            meta = {
                "eventsTotal": total,
                "eventsTruncated": False,
                "eventsLimit": EVENT_LIMIT,
                "eventsOmittedMiddle": 0,
            }
            return serialized, meta
        omitted = total - EVENT_HEAD - EVENT_TAIL
        meta = {
            "eventsTotal": total,
            "eventsTruncated": True,
            "eventsLimit": EVENT_LIMIT,
            "eventsOmittedMiddle": max(omitted, 0),
        }
        return serialized, meta


def _pi_compaction_failed(
    payload: JsonObject, recovery_error: str | None, terminal_status: str | None
) -> bool:
    """Did a compaction end in a way the run cannot come back from?

    An abort with no retry queued is terminal on its own: pi's `--mode json`
    exits 0 regardless, so an unclassified abort is published as a success.
    Requiring a preceding provider error, as this once did, made the guard dead
    for exactly the case it was written for.

    Three things are not failures: an abort that will retry, a plain successful
    compaction, and an abort that lands after a turn has already sealed a
    successful answer. Compaction is context housekeeping, and housekeeping
    cannot retract a delivered result.
    """
    aborted = payload.get("aborted") is True
    will_retry = payload.get("willRetry")
    if aborted and will_retry is not True and terminal_status != "succeeded":
        return True
    return recovery_error is not None and (aborted or will_retry is False)


def _extract_text(content: JsonValue) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if item.get("type") in (None, "text") and isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return ""


def _string_field(payload: JsonObject, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _grok_stop_reason_succeeded(value: JsonValue) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"[^a-z]", "", value.lower())
    return normalized in {"endturn", "stop", "complete", "done"}


def _grok_stop_reason_incomplete(value: JsonValue) -> bool:
    """Truncation reasons that neither `_normalize_terminal_status` nor the
    provider-terminal table classifies, leaving the run to ride on exit 0."""
    if not isinstance(value, str):
        return False
    return re.sub(r"[^a-z]", "", value.lower()) in {"maxtokens", "maxturnrequests"}


def _codex_command_status(status: str | None, *, completed: bool) -> str | None:
    if not completed:
        return status
    if status == "completed":
        return "success"
    return status


# Cursor names the tool by the KEY of the single `*ToolCall` member of
# `tool_call` -- `readToolCall`, `writeToolCall` -- with its arguments nested
# one level inside under `args`. There is no `name` field to read.
_CURSOR_TOOL_KEY_SUFFIX = "ToolCall"


def _cursor_tool_body(tool_call: JsonObject) -> tuple[str, JsonObject] | None:
    for key, value in tool_call.items():
        if (
            key.endswith(_CURSOR_TOOL_KEY_SUFFIX)
            and len(key) > len(_CURSOR_TOOL_KEY_SUFFIX)
            and isinstance(value, dict)
        ):
            return key[: -len(_CURSOR_TOOL_KEY_SUFFIX)], value
    return None


def _cursor_tool_status(body: JsonObject) -> str | None:
    """Read the outcome Cursor reports, rather than assuming a success.

    A completed call carries `result: {"success": {...}}` or an error member.
    An unrecognized result shape leaves the status unknown; an unknown outcome
    is not a good one.
    """
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    if "success" in result:
        return "success"
    if result.keys() & {"error", "failure", "failed"}:
        return "error"
    return None


def _completed_tool_status(status: JsonValue) -> str | None:
    """Normalize a harness's completed-tool status onto delegate's vocabulary.

    An unrecognized status is passed through rather than invented into a
    success: an unknown outcome is not a good one.
    """
    if not isinstance(status, str) or not status.strip():
        return None
    stripped = status.strip()
    if stripped == "completed":
        return "success"
    return stripped


def _grok_update_target(payload: JsonObject) -> str | None:
    locations = payload.get("locations")
    if not isinstance(locations, list):
        return None
    for location in locations:
        if isinstance(location, dict) and (path := _string_field(location, "path")):
            return path
    return None


def _opencode_tool_target(part: JsonObject) -> str | None:
    state = part.get("state")
    if not isinstance(state, dict):
        return None
    title = _string_field(state, "title")
    if title:
        return title
    tool_input = state.get("input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("filePath", "file_path", "path", "command", "pattern", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_use_target(block: JsonObject) -> str | None:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "file_path", "path", "pattern", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _kimi_tool_target(arguments: JsonValue) -> str | None:
    # Kimi delivers function arguments as a JSON-encoded string; malformed or
    # missing arguments simply yield no target. The parsed object reuses the
    # same target-key convention as the Claude tool_use path. RecursionError is
    # caught alongside JSONDecodeError because excessively nested (but valid)
    # JSON aborts json.loads with it, and this runs on the runner's stdout
    # drain thread where no exception may escape.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, RecursionError):
            return None
    return _tool_use_target({"input": arguments})


# grok puts tool arguments under `rawInput` and names a file `target_file`
# (ACP leaf naming); cursor and the generic shape use `args` with `path`.
_TOOL_TARGET_KEYS = ("path", "file", "command", "target", "target_file", "uri")
_TOOL_ARGUMENT_CONTAINERS = ("args", "rawInput")


def _tool_target(payload: JsonObject) -> str | None:
    for key in _TOOL_TARGET_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container in _TOOL_ARGUMENT_CONTAINERS:
        arguments = payload.get(container)
        if not isinstance(arguments, dict):
            continue
        for key in _TOOL_TARGET_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _tool_current(tool: str, target: str | None) -> str:
    if target:
        return f"{tool} {target}"
    return tool


def _current_from_text(text: str) -> str:
    line = text.strip().splitlines()[-1] if text.strip() else ""
    return _bounded_current_line(line)


def _bounded_current_line(line: str) -> str:
    return (line[:120] + "…") if len(line) > 120 else line
