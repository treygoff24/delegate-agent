from __future__ import annotations

import json
from dataclasses import dataclass, field

from delegate_agent.json_types import JsonObject, JsonValue

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
ASSISTANT_RECOVERY_HARNESSES = frozenset({"cursor", "droid", "kimi", "claude"})


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


@dataclass
class StreamAccumulator:
    assistant_chunks: list[str] = field(default_factory=list)
    events: list[NormalizedEvent] = field(default_factory=list)
    completion_text: str | None = None
    current: str | None = None
    _assistant_text_cache: str | None = field(default=None, repr=False)
    _codex_completion_candidate: str | None = field(default=None, repr=False)
    _last_recoverable_assistant_text: str | None = field(default=None, repr=False)
    _pending_tool_uses: dict[str, tuple[str, str | None]] = field(default_factory=dict, repr=False)

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
        self._ingest_object(payload)

    def _ingest_text_fallback(self, text: str) -> None:
        bounded = text if len(text) <= 500 else text[:500] + "…"
        self.events.append(NormalizedEvent(kind="text", message=bounded))
        if len(self.events) == 1:
            self.current = bounded[:120]

    def _ingest_object(self, payload: JsonObject) -> None:
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            self._ingest_role_content_message(payload)
            return
        if event_type == "reasoning":
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

    def _record_successful_completion_text(self, text: str) -> None:
        self._record_assistant_text(text, completion=True)
        self.events.append(NormalizedEvent(kind="run.completed", status="succeeded"))

    def _ingest_completion(self, payload: JsonObject) -> None:
        final_text = payload.get("finalText")
        if isinstance(final_text, str) and final_text.strip():
            self._record_successful_completion_text(final_text)

    def _ingest_result_event(self, payload: JsonObject) -> None:
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            if payload.get("is_error") is True:
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
            self._assistant_text_cache = "\n\n".join(
                chunk for chunk in self.assistant_chunks if chunk
            ).strip()
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
        return self._last_recoverable_assistant_text

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
            "recentEvents": recent_events,
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
    return (line[:120] + "…") if len(line) > 120 else line
