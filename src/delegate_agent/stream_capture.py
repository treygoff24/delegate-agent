"""Bounded byte capture, with narrowly recognized OMP thinking diagnostics.

This layer owns bytes and JSON-line framing, never process or terminal state.
Unknown records retain the ordinary byte charge and content unchanged.
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


@dataclass
class CaptureStats:
    transport_bytes: int = 0
    captured_bytes: int = 0
    omitted_thinking_bytes: int = 0
    omitted_thinking_records: int = 0
    thinking_sample_bytes: int = 0
    limit_kind: str | None = None

    def payload(self, retained_limit: int) -> JsonObject:
        return {
            "policy": "omp-thinking-v1",
            "scope": "final-attempt",
            "transportBytes": self.transport_bytes,
            "capturedBytes": self.captured_bytes,
            "omittedThinkingBytes": self.omitted_thinking_bytes,
            "omittedThinkingRecords": self.omitted_thinking_records,
            "thinkingSampleBytes": self.thinking_sample_bytes,
            "truncated": self.omitted_thinking_records > 0 or self.limit_kind is not None,
            "transportLimitBytes": OMP_TRANSPORT_MAX_BYTES,
            "recordLimitBytes": OMP_RECORD_MAX_BYTES,
            "retainedLimitBytes": retained_limit,
            "limitKind": self.limit_kind,
        }


def capture_warning(payload: JsonObject) -> str | None:
    if not payload.get("omittedThinkingRecords"):
        return None
    return (
        "OMP thinking diagnostics were compacted: "
        f"{payload['omittedThinkingRecords']} records / {payload['omittedThinkingBytes']} bytes "
        "omitted; raw stdout is incomplete (see stdoutCapture)."
    )


@dataclass
class BoundedCapture:
    write: Callable[[bytes], object]
    max_bytes: int
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
        self._retain(record)

    def feed(self, chunk: bytes) -> None:
        self.stats.transport_bytes += len(chunk)
        if not self.compact_omp:
            self._retain(chunk)
            return
        self._transport_hash.update(chunk)
        if self.stats.transport_bytes > OMP_TRANSPORT_MAX_BYTES:
            self._fail("transport", OMP_TRANSPORT_MAX_BYTES)
        # At most one bounded record plus one read chunk is held between calls.
        self._pending.extend(chunk)
        while (end := self._pending.find(b"\n")) >= 0:
            if end + 1 > OMP_RECORD_MAX_BYTES:
                self._fail("record", OMP_RECORD_MAX_BYTES)
            record = bytes(self._pending[: end + 1])
            del self._pending[: end + 1]
            self._record(record)
        if len(self._pending) > OMP_RECORD_MAX_BYTES:
            self._fail("record", OMP_RECORD_MAX_BYTES)

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
