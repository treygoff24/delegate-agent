"""Byte capture, with narrowly recognized OMP diagnostics compacted.

This layer owns bytes and JSON-line framing, never process or terminal state.
Unknown records retain the ordinary byte charge and content unchanged.

The retained-byte cap is opt-in: ``max_bytes=None`` (the tracked-run default)
never fails a capture. Only a configured cap, and the OMP transport and record
ceilings that ride with it, can raise ``CaptureLimit``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from delegate_agent.json_types import JsonObject

OMP_TRANSPORT_MAX_BYTES = 256 * 1024 * 1024
OMP_RECORD_MAX_BYTES = 16 * 1024 * 1024
OMP_THINKING_SAMPLE_BYTES = 64 * 1024
# OMP re-emits a tool's full `args` (the whole shared context, for the sub-agent
# `task` tool) on every `tool_execution_update`, and a streaming tool's whole
# accumulated `partialResult` too. Either field past this size is replaced by a
# short stub; `tool_execution_start` and `tool_execution_end` carry the complete
# arguments and result once.
OMP_TOOL_UPDATE_FIELD_BYTES = 2048
OMP_TOOL_UPDATE_PREVIEW_CHARS = 200
_TOOL_UPDATE_FIELDS = ("args", "partialResult")
OMISSION_MARKER = (
    b'{"type":"delegate.capture","message":"OMP thinking_delta diagnostics omitted; '
    b'see stdoutCapture for byte counts and limits."}\n'
)


class CaptureLimit(Exception):
    def __init__(self, kind: str, limit: int):
        super().__init__(f"{kind} limit of {limit} bytes exceeded")
        self.kind = kind
        self.limit = limit


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _thinking_only(record: bytes) -> bool:
    try:
        payload = json.loads(record.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return False
    if not isinstance(payload, dict) or set(payload) != {"type", "assistantMessageEvent"}:
        return False
    update = payload["assistantMessageEvent"]
    return (
        payload["type"] == "message_update"
        and isinstance(update, dict)
        and set(update) == {"type", "contentIndex", "delta"}
        and update["type"] == "thinking_delta"
        and type(update["contentIndex"]) is int
        and update["contentIndex"] >= 0
        and isinstance(update["delta"], str)
    )


def _compact_tool_update(record: bytes) -> tuple[bytes, int] | None:
    """Shrink an oversized OMP ``tool_execution_update`` record.

    Returns ``(replacement line, bytes saved)``, or ``None`` when the record is
    not a recognized update or has nothing oversized, so it stays byte-for-byte.
    """
    if b"tool_execution_update" not in record:
        return None
    try:
        payload = json.loads(record.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "tool_execution_update":
        return None
    changed = False
    for name in _TOOL_UPDATE_FIELDS:
        if name not in payload:
            continue
        rendered = json.dumps(payload[name], ensure_ascii=False)
        size = len(rendered.encode("utf-8"))
        if size <= OMP_TOOL_UPDATE_FIELD_BYTES:
            continue
        payload[name] = {
            "delegateCompacted": True,
            "originalBytes": size,
            "head": rendered[:OMP_TOOL_UPDATE_PREVIEW_CHARS],
            "tail": rendered[-OMP_TOOL_UPDATE_PREVIEW_CHARS:],
        }
        changed = True
    if not changed:
        return None
    replacement = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    return replacement, max(len(record) - len(replacement), 0)


@dataclass
class CaptureStats:
    transport_bytes: int = 0
    captured_bytes: int = 0
    omitted_thinking_bytes: int = 0
    omitted_thinking_records: int = 0
    thinking_sample_bytes: int = 0
    compacted_tool_update_records: int = 0
    compacted_tool_update_bytes: int = 0
    limit_kind: str | None = None

    def payload(self, retained_limit: int | None) -> JsonObject:
        return {
            "policy": "omp-capture-v2",
            "scope": "final-attempt",
            "transportBytes": self.transport_bytes,
            "capturedBytes": self.captured_bytes,
            "omittedThinkingBytes": self.omitted_thinking_bytes,
            "omittedThinkingRecords": self.omitted_thinking_records,
            "thinkingSampleBytes": self.thinking_sample_bytes,
            "compactedToolUpdateRecords": self.compacted_tool_update_records,
            "compactedToolUpdateBytes": self.compacted_tool_update_bytes,
            "truncated": (
                self.omitted_thinking_records > 0
                or self.compacted_tool_update_records > 0
                or self.limit_kind is not None
            ),
            # The transport ceiling is part of an opted-in cap; with no cap
            # configured neither limit exists.
            "transportLimitBytes": OMP_TRANSPORT_MAX_BYTES if retained_limit is not None else None,
            "recordLimitBytes": OMP_RECORD_MAX_BYTES if retained_limit is not None else None,
            "retainedLimitBytes": retained_limit,
            "limitKind": self.limit_kind,
        }


def capture_warning(payload: JsonObject) -> str | None:
    warnings = []
    if payload.get("omittedThinkingRecords"):
        warnings.append(
            "OMP thinking diagnostics were compacted: "
            f"{payload['omittedThinkingRecords']} records / {payload['omittedThinkingBytes']} bytes "
            "omitted; raw stdout is incomplete (see stdoutCapture)."
        )
    if payload.get("compactedToolUpdateRecords"):
        warnings.append(
            "OMP tool_execution_update records were compacted: "
            f"{payload['compactedToolUpdateRecords']} records lost "
            f"{payload['compactedToolUpdateBytes']} bytes of repeated args or partial output; "
            "the tool_execution_start and tool_execution_end records are complete "
            "(see stdoutCapture)."
        )
    return " ".join(warnings) if warnings else None


@dataclass
class BoundedCapture:
    write: Callable[[bytes], object]
    # None means no retained cap, and with it no OMP transport or record
    # ceiling: nothing this class does can then fail the capture, so a finished
    # lane is never killed for being verbose. Framing memory stays bounded.
    max_bytes: int | None
    initial_bytes: int = 0
    compact_omp: bool = False
    on_omitted: Callable[[str], None] | None = None
    stats: CaptureStats = field(default_factory=CaptureStats)
    _pending: bytearray = field(default_factory=bytearray, repr=False)

    def __post_init__(self) -> None:
        self._transport_hash = hashlib.sha256()

    def payload(self) -> JsonObject:
        return {
            **self.stats.payload(self.max_bytes),
            "transportSha256": self._transport_hash.hexdigest(),
        }

    def _fail(self, kind: str, limit: int) -> None:
        self.stats.limit_kind = kind
        raise CaptureLimit(kind, limit)

    def _retain(self, data: bytes) -> None:
        if self.max_bytes is None:
            if data:
                self.write(data)
                self.stats.captured_bytes += len(data)
            return
        available = max(self.max_bytes - self.initial_bytes - self.stats.captured_bytes, 0)
        captured = data[:available]
        if captured:
            self.write(captured)
            self.stats.captured_bytes += len(captured)
        if len(captured) != len(data):
            self._fail("retained", self.max_bytes)

    def _record(self, record: bytes) -> None:
        if _thinking_only(record):
            if (
                not self.stats.omitted_thinking_records
                and self.stats.thinking_sample_bytes + len(record) <= OMP_THINKING_SAMPLE_BYTES
            ):
                self.stats.thinking_sample_bytes += len(record)
            else:
                first = not self.stats.omitted_thinking_records
                self.stats.omitted_thinking_records += 1
                self.stats.omitted_thinking_bytes += len(record)
                if first:
                    self._retain(OMISSION_MARKER)
                if self.on_omitted is not None:
                    self.on_omitted(record.decode("utf-8"))
                return
        compacted = _compact_tool_update(record)
        if compacted is not None:
            record, saved = compacted
            self.stats.compacted_tool_update_records += 1
            self.stats.compacted_tool_update_bytes += saved
        self._retain(record)

    def feed(self, chunk: bytes) -> None:
        self.stats.transport_bytes += len(chunk)
        if not self.compact_omp:
            self._retain(chunk)
            return
        self._transport_hash.update(chunk)
        capped = self.max_bytes is not None
        if capped and self.stats.transport_bytes > OMP_TRANSPORT_MAX_BYTES:
            self._fail("transport", OMP_TRANSPORT_MAX_BYTES)
        # At most one bounded record plus one read chunk is held between calls.
        self._pending.extend(chunk)
        while (end := self._pending.find(b"\n")) >= 0:
            record = bytes(self._pending[: end + 1])
            del self._pending[: end + 1]
            if len(record) > OMP_RECORD_MAX_BYTES:
                if capped:
                    self._fail("record", OMP_RECORD_MAX_BYTES)
                # Uncapped: a record too large to classify is kept verbatim and
                # uncompacted instead of failing the run.
                self._retain(record)
                continue
            self._record(record)
        if len(self._pending) > OMP_RECORD_MAX_BYTES:
            if capped:
                self._fail("record", OMP_RECORD_MAX_BYTES)
            self._retain(bytes(self._pending))
            self._pending.clear()

    def finish(self) -> None:
        if self._pending:
            self._record(bytes(self._pending))
            self._pending.clear()


def drain_bounded(
    read: Callable[[], bytes], capture: BoundedCapture, *, stop: Callable[[], bool] | None = None
) -> None:
    while stop is None or not stop():
        chunk = read()
        if not chunk:
            capture.finish()
            return
        capture.feed(chunk)
