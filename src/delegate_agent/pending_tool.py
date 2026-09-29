"""Surface the longest-running in-flight tool call of a live run.

The stall watchdog never fires while a tool is pending (test suites and builds
legitimately run long), so a hung call is otherwise invisible: the run just
looks quiet. `pendingTool` on the run record names the oldest pending call and
how long it has run, and `current` says so once the wait is notable. This is
observation only; nothing here cancels or times out a run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from delegate_agent import record_io, redaction
from delegate_agent.json_types import JsonObject

# The registry's UTC stamp format; run_registry imports run_status, which reads
# this module, so the constant is repeated rather than imported.
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Below this, `current` keeps the ordinary "<tool> <target>" text.
NOTICE_SECONDS = 60
# While a tool stays pending with no output, the record is refreshed this often
# so `current` shows a moving duration instead of the moment it was first noted.
REFRESH_SECONDS = 30


def waiting_text(name: str, seconds: int) -> str:
    if seconds >= 3600:
        elapsed = f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    elif seconds >= 60:
        elapsed = f"{seconds // 60}m"
    else:
        elapsed = f"{seconds}s"
    return f"waiting on tool {name} for {elapsed}"


def record_value(detail: JsonObject) -> JsonObject | None:
    """The persisted `pendingTool` for a watchdog detail, redacted like tool text."""
    name = detail.get("name")
    seconds = detail.get("seconds")
    if not isinstance(name, str) or not isinstance(seconds, int):
        return None
    started = datetime.now(UTC) - timedelta(seconds=seconds)
    value: JsonObject = {
        "name": redaction.redact_string(name),
        "startedAt": started.strftime(_TIMESTAMP_FORMAT),
        "seconds": seconds,
    }
    target = detail.get("target")
    if isinstance(target, str) and target:
        value["target"] = redaction.redact_string(target)
    return value


def current_for(value: JsonObject) -> str | None:
    """`current` text for a persisted value once the wait is long enough to mention."""
    name = value.get("name")
    seconds = value.get("seconds")
    if isinstance(name, str) and isinstance(seconds, int) and seconds >= NOTICE_SECONDS:
        return waiting_text(name, seconds)
    return None


def read_view(state: JsonObject) -> JsonObject | None:
    """`pendingTool` for a reader, with `seconds` recomputed from `startedAt`.

    A record is written only when the run emits or the poll loop refreshes it, so
    the stored `seconds` is stale by the time anyone reads it. A run that is no
    longer live has no pending tool to report.
    """
    value = state.get("pendingTool")
    if not isinstance(value, dict) or state.get("status") != "running":
        return None
    started = value.get("startedAt")
    parsed = record_io.parse_utc_timestamp(started) if isinstance(started, str) else None
    if parsed is None:
        return dict(value)
    fresh = dict(value)
    fresh["seconds"] = max(int((datetime.now(UTC) - parsed).total_seconds()), 0)
    return fresh
