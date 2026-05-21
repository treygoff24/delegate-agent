from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from delegate_agent import retention as delegate_retention
from delegate_agent import run_registry
from delegate_agent.run_registry import parse_utc_timestamp as parse_timestamp

REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(api[_-]?key|apikey)(\s*[:=]\s*)\S+"), r"\1\2***"),
    (re.compile(r"(?i)(authorization|bearer)(\s*[:=]\s*)\S+"), r"\1\2***"),
    (re.compile(r"(?i)(password|passwd|secret|token)(\s*[:=]\s*)\S+"), r"\1\2***"),
    (re.compile(r"sk-[A-Za-z0-9]{8,}"), "sk-***"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_***"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "gho_***"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "xox***"),
]


def redact_string(value: str) -> str:
    redacted = value
    for pattern, replacement in REDACT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


def format_age(started_at: str | None, *, now: datetime | None = None) -> str:
    start = parse_timestamp(started_at)
    if start is None:
        return "unknown"
    moment = now or datetime.now(UTC)
    delta = moment - start
    total_seconds = max(int(delta.total_seconds()), 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def merge_snapshot_view(
    registry_root: Path,
    run_id: str,
    snapshot: dict[str, Any] | None,
    *,
    redact: bool,
) -> dict[str, Any]:
    state = run_registry.load_run_state(registry_root, run_id)
    manifest = run_registry.load_run_manifest(registry_root, run_id)
    stdout_bytes, stderr_bytes = delegate_retention.effective_log_byte_sizes(registry_root, run_id)
    view: dict[str, Any] = dict(snapshot or {})
    if not view:
        view = {
            "schema": "delegate.snapshot.v1",
            "ok": True,
            "runId": run_id,
        }
    view.setdefault("ok", True)
    view["runId"] = run_id
    view["status"] = run_registry.effective_status(state)
    view["stdoutBytes"] = stdout_bytes
    view["stderrBytes"] = stderr_bytes
    if state:
        for key in ("lastActivityAt", "current", "exitCode", "finishedAt"):
            if key in state and key not in view:
                view[key] = state[key]
    if manifest:
        for key in ("alias", "harness", "cwd", "executionCwd", "mode", "model", "startedAt"):
            if key in manifest and key not in view:
                view[key] = manifest[key]
    warnings = list(view.get("warnings") or [])
    for warning in run_registry.large_log_warnings(stdout_bytes, stderr_bytes):
        if warning not in warnings:
            warnings.append(warning)
    alias = view.get("alias")
    if delegate_retention.raw_logs_archived(registry_root, run_id):
        archive_warning = delegate_retention.archived_log_warning(
            alias if isinstance(alias, str) else None,
            run_id,
        )
        if archive_warning not in warnings:
            warnings.append(archive_warning)
    if warnings:
        view["warnings"] = warnings
    if isinstance(alias, str):
        view.setdefault("snapshotCommand", run_registry.snapshot_command(alias))
        if "completionReport" in view and isinstance(view["completionReport"], dict):
            view["completionReport"].setdefault(
                "command",
                run_registry.run_output_command(alias, completion_report=True),
            )
    if redact:
        view = redact_value(view)
    return view


def snapshot_json_payload(view: dict[str, Any]) -> dict[str, Any]:
    return view


def render_snapshot_text(view: dict[str, Any], stdout: TextIO) -> None:
    alias = view.get("alias", view.get("runId", "?"))
    status = view.get("status", "unknown")
    started_at = view.get("startedAt")
    age = format_age(started_at if isinstance(started_at, str) else None)
    print(f"{alias} · {status} · {age} elapsed", file=stdout)
    for key, label in (
        ("cwd", "cwd"),
        ("executionCwd", "execution cwd"),
        ("model", "model"),
        ("mode", "mode"),
    ):
        value = view.get(key)
        if isinstance(value, str) and value:
            print(f"{label}: {value}", file=stdout)
    current = view.get("current")
    if isinstance(current, str) and current:
        print(f"current: {current}", file=stdout)
    assistant_text = view.get("assistantText")
    if isinstance(assistant_text, str) and assistant_text:
        print("assistant text:", file=stdout)
        print(assistant_text, file=stdout)
    recent_events = view.get("recentEvents")
    if isinstance(recent_events, list) and recent_events:
        print("recent:", file=stdout)
        for event in recent_events[-20:]:
            if not isinstance(event, dict):
                continue
            kind = event.get("kind", "event")
            tool = event.get("tool")
            path = event.get("path") or event.get("target")
            if tool and path:
                print(f"  - {kind}: {tool} {path}", file=stdout)
            elif tool:
                print(f"  - {kind}: {tool}", file=stdout)
            else:
                print(f"  - {kind}", file=stdout)
    warnings = view.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("warnings:", file=stdout)
        for warning in warnings:
            if isinstance(warning, str):
                print(f"  - {warning}", file=stdout)
    completion = view.get("completionReport")
    if isinstance(completion, dict):
        command = completion.get("command")
        if isinstance(command, str):
            print(f"completion report: {command}", file=stdout)


def runs_json_payload(
    summaries: list[dict[str, Any]],
    *,
    limit: int,
    mode: str,
) -> dict[str, Any]:
    return {
        "schema": "delegate.runs.v1",
        "ok": True,
        "mode": mode,
        "limit": limit,
        "runs": summaries,
    }


def render_runs_text(summaries: list[dict[str, Any]], stdout: TextIO, *, mode: str) -> None:
    print(f"mode: {mode}", file=stdout)
    print("alias      status    harness  age      current", file=stdout)
    for summary in summaries:
        alias = summary.get("alias") or summary.get("runId") or "?"
        status = summary.get("status", "unknown")
        harness = summary.get("harness", "?")
        activity = summary.get("activityAt")
        age = format_age(activity if isinstance(activity, str) else None)
        current = summary.get("current", "")
        if isinstance(current, str) and len(current) > 40:
            current = current[:37] + "..."
        print(f"{alias:<10} {status:<9} {harness:<8} {age:<8} {current}", file=stdout)


def tail_file_lines(path: Path, lines: int) -> str:
    if lines < 1:
        raise ValueError("tail lines must be at least 1")
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        if end == 0:
            return ""
        block = 4096
        chunks: list[bytes] = []
        position = end
        newline_count = 0
        while position > 0 and newline_count <= lines:
            read_size = min(block, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.insert(0, chunk)
            newline_count += chunk.count(b"\n")
        text = b"".join(chunks).decode("utf-8", errors="replace")
    split = text.splitlines()
    return "\n".join(split[-lines:]) + ("\n" if split else "")


def read_log_output(path: Path, *, tail: int | None, raw: bool) -> tuple[str, bool]:
    """Return log text and whether the response is a bounded tail (truncated) view."""
    if not path.exists():
        return "", False
    if raw:
        return path.read_text(encoding="utf-8", errors="replace"), False
    if tail is None:
        raise ValueError("add --tail N or --raw to read stdout/stderr log output")
    return tail_file_lines(path, tail), True


def run_output_json_payload(
    *,
    alias: str | None,
    run_id: str,
    sections: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "delegate.run-output.v1",
        "ok": True,
        "runId": run_id,
        "sections": sections,
    }
    if alias:
        payload["alias"] = alias
    return payload


def render_run_output_text(sections: dict[str, str], stdout: TextIO) -> None:
    for name in ("completionReport", "stdout", "stderr"):
        content = sections.get(name)
        if not content:
            continue
        print(f"=== {name} ===", file=stdout)
        print(content, end="" if content.endswith("\n") else "\n", file=stdout)


def print_json(payload: dict[str, Any], stdout: TextIO) -> None:
    print(json.dumps(payload, sort_keys=True), file=stdout)
