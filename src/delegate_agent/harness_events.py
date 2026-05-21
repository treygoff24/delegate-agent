from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

ASSISTANT_TEXT_LIMIT = 30_000
ASSISTANT_TEXT_HEAD = 20_000
ASSISTANT_TEXT_TAIL = 10_000
EVENT_LIMIT = 500
EVENT_HEAD = 100
EVENT_TAIL = 400


@dataclass
class NormalizedEvent:
    kind: str
    tool: str | None = None
    target: str | None = None
    path: str | None = None
    status: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
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
    warnings: list[str] = field(default_factory=list)

    def ingest_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
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

    def _ingest_object(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        if not isinstance(event_type, str):
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
        if event_type == "assistant":
            self._ingest_cursor_assistant(payload)
            return
        if event_type in ("tool_call.started", "tool_call.completed"):
            self._ingest_cursor_tool(payload, event_type)
            return
        if event_type == "result":
            self._ingest_cursor_result(payload)

    def _ingest_system(self, payload: dict[str, Any]) -> None:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.current = f"session cwd {cwd}"

    def _ingest_message(self, payload: dict[str, Any]) -> None:
        role = payload.get("role")
        if role not in (None, "assistant"):
            return
        text = _extract_text(payload.get("content"))
        if text:
            self.assistant_chunks.append(text)
            self.current = _current_from_text(text)

    def _ingest_cursor_assistant(self, payload: dict[str, Any]) -> None:
        message = payload.get("message")
        if isinstance(message, dict):
            text = _extract_text(message.get("content"))
            if text:
                self.assistant_chunks.append(text)
                self.current = _current_from_text(text)

    def _ingest_completion(self, payload: dict[str, Any]) -> None:
        final_text = payload.get("finalText")
        if isinstance(final_text, str) and final_text.strip():
            self.completion_text = final_text.strip()
            self.assistant_chunks.append(final_text)
            self.current = _current_from_text(final_text)
            self.events.append(NormalizedEvent(kind="run.completed", status="succeeded"))

    def _ingest_cursor_result(self, payload: dict[str, Any]) -> None:
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            self.completion_text = result.strip()
            self.assistant_chunks.append(result)
            self.current = _current_from_text(result)
            self.events.append(NormalizedEvent(kind="run.completed", status="succeeded"))

    def _ingest_tool_call(self, payload: dict[str, Any]) -> None:
        tool = _string_field(payload, "tool", "name", "toolName") or "tool"
        target = _tool_target(payload)
        kind = "tool.started"
        self.events.append(
            NormalizedEvent(kind=kind, tool=tool, target=target, path=target),
        )
        self.current = _tool_current(tool, target)

    def _ingest_cursor_tool(self, payload: dict[str, Any], event_type: str) -> None:
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

    @property
    def assistant_text(self) -> str:
        return "\n\n".join(chunk for chunk in self.assistant_chunks if chunk).strip()

    def bounded_assistant_text(self) -> tuple[str, dict[str, Any]]:
        text = self.assistant_text
        meta = {
            "assistantTextChars": len(text),
            "assistantTextTruncated": False,
            "assistantTextLimitChars": ASSISTANT_TEXT_LIMIT,
            "assistantTextOmittedMiddleChars": 0,
        }
        if len(text) <= ASSISTANT_TEXT_LIMIT:
            meta["assistantText"] = text
            return text, meta
        head = text[:ASSISTANT_TEXT_HEAD]
        tail = text[-ASSISTANT_TEXT_TAIL:]
        omitted = len(text) - ASSISTANT_TEXT_HEAD - ASSISTANT_TEXT_TAIL
        bounded = f"{head}\n\n… [{omitted} chars omitted] …\n\n{tail}"
        meta.update(
            {
                "assistantText": bounded,
                "assistantTextTruncated": True,
                "assistantTextOmittedMiddleChars": max(omitted, 0),
            }
        )
        return bounded, meta

    def bounded_recent_events(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        serialized = [event.to_dict() for event in self.events]
        total = len(serialized)
        meta = {
            "eventsTotal": total,
            "eventsTruncated": False,
            "eventsLimit": EVENT_LIMIT,
            "eventsOmittedMiddle": 0,
        }
        if total <= EVENT_LIMIT:
            return serialized, meta
        head = serialized[:EVENT_HEAD]
        tail = serialized[-EVENT_TAIL:]
        omitted = total - EVENT_HEAD - EVENT_TAIL
        meta.update(
            {
                "eventsTruncated": True,
                "eventsOmittedMiddle": max(omitted, 0),
                "recentEvents": head + tail,
            }
        )
        return head + tail, meta


def _extract_text(content: Any) -> str:
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


def _string_field(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_target(payload: dict[str, Any]) -> str | None:
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
