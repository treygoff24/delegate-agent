from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from delegate_agent.json_types import JsonObject, JsonValue

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

# Harnesses whose streams emit assistant messages that are safe to
# surface as a recovered completion report when a run dies before its final
# completion event. Codex is excluded on purpose: its agent_message events can
# be preamble ("I'll start by..."), and only a message sealed by turn.completed
# is the real answer.
ASSISTANT_RECOVERY_HARNESSES = frozenset({"cursor", "droid", "kimi", "claude", "grok", "devin"})


@dataclass
class NormalizedEvent:
    kind: str
    tool: str | None = None
    target: str | None = None
    path: str | None = None
    status: str | None = None
    message: str | None = None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {"kind": self.kind}
        if self.tool is not None:
            payload["tool"] = self.tool
        if self.target is not None:
            payload["target"] = self.target
        if self.path is not None:
            payload["path"] = self.path
        if self.status is not None:
            payload["status"] = self.status
        if self.message is not None:
            payload["message"] = self.message
        return payload


_CANCELLED_REASONS = {"abort", "aborted", "cancel", "cancelled", "canceled", "interrupted"}
_FAILED_REASONS = {"error", "errored", "fail", "failed", "failure"}


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


@dataclass
class StreamAccumulator:
    harness: str | None = None
    assistant_chunks: list[str] = field(default_factory=list)
    events: list[NormalizedEvent] = field(default_factory=list)
    completion_text: str | None = None
    current: str | None = None
    _assistant_text_cache: str | None = field(default=None, repr=False)
    _codex_completion_candidate: str | None = field(default=None, repr=False)
    _last_recoverable_assistant_text: str | None = field(default=None, repr=False)
    _last_substantive_assistant_text: str | None = field(default=None, repr=False)
    _pending_tool_uses: dict[str, tuple[str, str | None]] = field(default_factory=dict, repr=False)
    _grok_text_buffer: str = field(default="", repr=False)
    _grok_current_line: str = field(default="", repr=False)
    terminal_event: JsonObject | None = None
    terminal_status: str | None = None
    structured_events_seen: int = 0

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
            payload["reason"] = reason
        self.terminal_event = payload
        self.events.append(NormalizedEvent(kind="run.completed", status=status, message=reason))

    def _invalidate_assistant_text_cache(self) -> None:
        self._assistant_text_cache = None

    def ingest_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload: JsonValue = json.loads(stripped)
        except json.JSONDecodeError:
            self._ingest_text_fallback(stripped)
            return
        if not isinstance(payload, dict):
            self._ingest_text_fallback(stripped)
            return
        self.structured_events_seen += 1
        self._ingest_object(payload)

    def _ingest_text_fallback(self, text: str) -> None:
        bounded = text if len(text) <= 500 else text[:500] + "…"
        self.events.append(NormalizedEvent(kind="text", message=bounded))
        self.current = _bounded_current_line(bounded)
        if self.harness == "devin":
            self._record_recoverable_assistant_text(text)

    def _ingest_object(self, payload: JsonObject) -> None:
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            self._ingest_role_content_message(payload)
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
        if event_type == "tool_result":
            return
        if event_type == "system":
            self._ingest_system(payload)
            return
        if event_type == "message":
            self._ingest_message(payload)
            return
        if event_type == "tool_call":
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
            self._ingest_cursor_tool(payload, event_type)
            return
        if event_type == "result":
            self._ingest_result_event(payload)
            return
        if event_type in ("turn.failed", "turn.error"):
            self._record_terminal_event(event=event_type, status="failed")
            return
        if event_type in ("turn.cancelled", "turn.canceled"):
            self._record_terminal_event(event=event_type, status="cancelled")
            return
        if event_type in ("item.started", "item.completed"):
            self._ingest_codex_item(payload, completed=event_type == "item.completed")
            return
        if event_type == "turn.completed":
            self._ingest_codex_turn_completed()
            return
        if event_type == "turn.started":
            self._codex_completion_candidate = None
            return

    def _ingest_system(self, payload: JsonObject) -> None:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.current = f"session cwd {cwd}"

    def _ingest_message(self, payload: JsonObject) -> None:
        role = payload.get("role")
        if role not in (None, "assistant"):
            return
        text = _extract_text(payload.get("content"))
        if text:
            self._record_recoverable_assistant_text(text)

    def _ingest_role_content_message(self, payload: JsonObject) -> None:
        role = payload.get("role")
        if role != "assistant":
            return
        text = _extract_text(payload.get("content"))
        if text:
            self._record_recoverable_assistant_text(text)

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

    def _record_successful_completion_text(self, text: str) -> None:
        self._record_assistant_text(text, completion=True)
        self._record_terminal_event(event="completion", status="succeeded")

    def _ingest_completion(self, payload: JsonObject) -> None:
        final_text = payload.get("finalText")
        if isinstance(final_text, str) and final_text.strip():
            self._record_successful_completion_text(final_text)

    def _ingest_result_event(self, payload: JsonObject) -> None:
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            if payload.get("is_error") is True:
                self._record_terminal_event(event="result", status="failed")
                self._record_recoverable_assistant_text(result)
                return
            self._record_successful_completion_text(result)

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
        if item_type == "command_execution":
            self._ingest_codex_command_execution(item, completed=completed)

    def _ingest_codex_command_execution(self, item: JsonObject, *, completed: bool) -> None:
        command = _string_field(item, "command")
        status = _codex_command_status(_string_field(item, "status"), completed=completed)
        kind = "tool.completed" if completed else "tool.started"
        # Clear the completion candidate: an agent_message followed by a command is
        # preamble/progress, not the turn's final answer. Only a message emitted
        # after the last tool activity (then sealed by turn.completed) is promoted,
        # which is the shape real Codex runs produce. Promoting a pre-command
        # message would surface an intro line ("I'll start by…") as the report.
        self._codex_completion_candidate = None
        self.events.append(
            NormalizedEvent(
                kind=kind,
                tool="command_execution",
                target=command,
                status=status,
            )
        )
        self.current = _tool_current("command_execution", command)

    def _ingest_codex_turn_completed(self) -> None:
        if self._codex_completion_candidate:
            self.completion_text = self._codex_completion_candidate

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

    def _ingest_grok_end(self, payload: JsonObject) -> None:
        text = self._grok_text_buffer.strip()
        self._grok_text_buffer = ""
        self._grok_current_line = ""
        self._invalidate_assistant_text_cache()
        if text:
            # The thought/text/end stream shape and the "EndTurn" success spelling
            # are validated against grok 0.2.73; non-success stopReason spellings
            # (MaxTokens/Refusal/etc.) are best-effort, so classification is
            # conservative — anything not recognized as success stays recoverable.
            stop_reason = payload.get("stopReason")
            terminal_status = _normalize_terminal_status(stop_reason)
            if _grok_stop_reason_succeeded(stop_reason):
                self._record_successful_completion_text(text)
            elif terminal_status in {"cancelled", "failed"}:
                self._record_terminal_event(
                    event="grok.end",
                    status=terminal_status,
                    reason=stop_reason if isinstance(stop_reason, str) else None,
                )
                self._record_recoverable_assistant_text(text)
            else:
                self._record_recoverable_assistant_text(text)
        elif _grok_stop_reason_succeeded(payload.get("stopReason")):
            self._record_terminal_event(event="grok.end", status="succeeded")
        else:
            terminal_status = _normalize_terminal_status(payload.get("stopReason"))
            if terminal_status in {"cancelled", "failed"}:
                reason = payload.get("stopReason")
                self._record_terminal_event(
                    event="grok.end",
                    status=terminal_status,
                    reason=reason if isinstance(reason, str) else None,
                )

    def _ingest_grok_error(self, payload: JsonObject) -> None:
        # Grok streaming-json emits {"type":"error","message":...} on failure and
        # then exits nonzero, so the runner already marks the run failed via exit
        # code. Surface the message (plus any partial buffered text) as recoverable
        # assistant text so it lands in the snapshot instead of being dropped.
        partial = self._grok_text_buffer.strip()
        self._grok_text_buffer = ""
        self._grok_current_line = ""
        self._invalidate_assistant_text_cache()
        if partial:
            self._record_recoverable_assistant_text(partial)
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            text = message.strip()
            self._record_recoverable_assistant_text(text)
            self.current = _bounded_current_line(text)
        self._record_terminal_event(event="grok.error", status="failed")

    def _ingest_tool_call(self, payload: JsonObject) -> None:
        tool = _string_field(payload, "tool", "name", "toolName") or "tool"
        target = _tool_target(payload)
        kind = "tool.started"
        self.events.append(
            NormalizedEvent(kind=kind, tool=tool, target=target, path=target),
        )
        self.current = _tool_current(tool, target)

    def _ingest_cursor_tool(self, payload: JsonObject, event_type: str) -> None:
        tool_call = payload.get("tool_call")
        if not isinstance(tool_call, dict):
            return
        tool = _string_field(tool_call, "name", "tool") or "tool"
        target = _tool_target(tool_call)
        kind = "tool.completed" if event_type.endswith("completed") else "tool.started"
        status = "success" if kind == "tool.completed" else None
        self.events.append(
            NormalizedEvent(kind=kind, tool=tool, target=target, path=target, status=status),
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
            grok = self._grok_text_buffer.strip()
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
        text = self._grok_text_buffer.strip()
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
        total = len(serialized)
        if total <= EVENT_LIMIT:
            meta = {
                "eventsTotal": total,
                "eventsTruncated": False,
                "eventsLimit": EVENT_LIMIT,
                "eventsOmittedMiddle": 0,
            }
            return serialized, meta
        head = serialized[:EVENT_HEAD]
        tail = serialized[-EVENT_TAIL:]
        omitted = total - EVENT_HEAD - EVENT_TAIL
        recent_events = head + tail
        meta = {
            "eventsTotal": total,
            "eventsTruncated": True,
            "eventsLimit": EVENT_LIMIT,
            "eventsOmittedMiddle": max(omitted, 0),
        }
        return recent_events, meta


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
                if isinstance(text, str):
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


def _codex_command_status(status: str | None, *, completed: bool) -> str | None:
    if not completed:
        return status
    if status == "completed":
        return "success"
    return status


def _tool_use_target(block: JsonObject) -> str | None:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "file_path", "path", "pattern", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_target(payload: JsonObject) -> str | None:
    for key in ("path", "file", "command", "target", "uri"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    args = payload.get("args")
    if isinstance(args, dict):
        for key in ("path", "file", "command", "target"):
            value = args.get(key)
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
