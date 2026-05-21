from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from delegate_agent import harness_events, run_registry

PROGRESS_PERSIST_LINE_INTERVAL = 10
PROGRESS_PERSIST_TIME_INTERVAL_SEC = 0.5
DRAIN_JOIN_TIMEOUT_SEC = 5.0

COMPLETION_REPORT_SUFFIX = """

## Delegate completion report requirement

When you finish, end with a concise completion report for the parent agent:

- Status: completed / blocked / failed
- What you did or found
- Files changed or reviewed
- Verification run and result
- Remaining risks or follow-ups

Keep it concise. Do not include raw logs unless explicitly relevant.
"""


@dataclass(frozen=True)
class RunContext:
    registry_root: Path
    run_id: str
    alias: str
    harness: str
    engine: str
    mode: str
    model: str
    source_cwd: str
    execution_cwd: str
    workspace_kind: str
    isolated_workspace: bool
    started_at: str


def append_completion_report_instructions(prompt: str) -> str:
    if prompt.rstrip().endswith(COMPLETION_REPORT_SUFFIX.strip()):
        return prompt
    return prompt + COMPLETION_REPORT_SUFFIX


def run_dir(registry_root: Path, run_id: str) -> Path:
    return run_registry.runs_dir(registry_root) / run_id


def write_manifest(run_path: Path, manifest: dict[str, Any]) -> None:
    run_registry.write_json_atomic(run_path / "manifest.json", manifest)


def write_state(run_path: Path, state: dict[str, Any]) -> None:
    run_registry.write_json_atomic(run_path / "state.json", state)


def write_snapshot(run_path: Path, snapshot: dict[str, Any]) -> None:
    run_registry.write_json_atomic(run_path / "snapshot.json", snapshot)


def append_event(run_path: Path, event: dict[str, Any]) -> None:
    path = run_path / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()


def snapshot_command(alias: str) -> str:
    return f"delegate snapshot {alias}"


def completion_report_command(alias: str) -> str:
    return f"delegate run-output {alias} --completion-report"


def completion_report_path(run_id: str) -> str:
    return f".delegate/runs/{run_id}/completion-report.md"


def format_duration(duration_ms: int) -> str:
    total_seconds = max(duration_ms // 1000, 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def status_from_exit(exit_code: int) -> str:
    return "succeeded" if exit_code == 0 else "failed"


def build_manifest(ctx: RunContext, argv: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "delegate.manifest.v1",
        "runId": ctx.run_id,
        "alias": ctx.alias,
        "harness": ctx.harness,
        "engine": ctx.engine,
        "mode": ctx.mode,
        "model": ctx.model,
        "cwd": ctx.source_cwd,
        "executionCwd": ctx.execution_cwd,
        "workspaceKind": ctx.workspace_kind,
        "startedAt": ctx.started_at,
        "argv": argv,
    }
    if ctx.isolated_workspace:
        payload["isolatedWorkspace"] = True
    return payload


def build_state(
    ctx: RunContext,
    *,
    status: str,
    exit_code: int | None = None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    current: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state: dict[str, Any] = {
        "schema": "delegate.state.v1",
        "runId": ctx.run_id,
        "alias": ctx.alias,
        "status": status,
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "lastActivityAt": now,
    }
    if exit_code is not None:
        state["exitCode"] = exit_code
        state["finishedAt"] = now
    if current:
        state["current"] = current
    if pid is not None:
        state["pid"] = pid
    return state


def build_snapshot(
    ctx: RunContext,
    *,
    status: str,
    accumulator: harness_events.StreamAccumulator,
    exit_code: int | None = None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    completion_report_written: bool = False,
) -> dict[str, Any]:
    _assistant_text, assistant_meta = accumulator.bounded_assistant_text()
    recent_events, events_meta = accumulator.bounded_recent_events()
    snapshot: dict[str, Any] = {
        "schema": "delegate.snapshot.v1",
        "ok": True,
        "alias": ctx.alias,
        "runId": ctx.run_id,
        "harness": ctx.harness,
        "status": status,
        "cwd": ctx.source_cwd,
        "executionCwd": ctx.execution_cwd,
        "mode": ctx.mode,
        "model": ctx.model,
        "startedAt": ctx.started_at,
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "current": accumulator.current,
        "warnings": list(accumulator.warnings),
        "recentEvents": recent_events,
        **assistant_meta,
        **events_meta,
    }
    if ctx.isolated_workspace:
        snapshot["isolatedWorkspace"] = True
    if exit_code is not None:
        snapshot["exitCode"] = exit_code
    if completion_report_written:
        report_path = completion_report_path(ctx.run_id)
        snapshot["completionReport"] = {
            "path": report_path,
            "command": completion_report_command(ctx.alias),
        }
    return snapshot


def persist_progress(
    run_path: Path,
    ctx: RunContext,
    accumulator: harness_events.StreamAccumulator,
    *,
    status: str,
    exit_code: int | None = None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    pid: int | None = None,
    completion_report_written: bool = False,
) -> None:
    write_state(
        run_path,
        build_state(
            ctx,
            status=status,
            exit_code=exit_code,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            current=accumulator.current,
            pid=pid,
        ),
    )
    write_snapshot(
        run_path,
        build_snapshot(
            ctx,
            status=status,
            accumulator=accumulator,
            exit_code=exit_code,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            completion_report_written=completion_report_written,
        ),
    )


def write_completion_report(run_path: Path, text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    path = run_path / "completion-report.md"
    path.write_text(cleaned + "\n", encoding="utf-8")
    return True


def emit_bounded_text_summary(
    ctx: RunContext,
    *,
    status: str,
    duration_ms: int,
    stdout: TextIO,
) -> None:
    harness_label = ctx.harness
    print(
        f"delegate run {harness_label} completed in {format_duration(duration_ms)}",
        file=stdout,
    )
    print(f"alias: {ctx.alias}", file=stdout)
    print(f"status: {status}", file=stdout)
    print(f"snapshot: {snapshot_command(ctx.alias)}", file=stdout)
    print(f"completion report: {completion_report_command(ctx.alias)}", file=stdout)


def completion_json_payload(
    ctx: RunContext,
    *,
    ok: bool,
    status: str,
    exit_code: int,
    duration_ms: int,
    stdout_bytes: int,
    stderr_bytes: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "exitCode": exit_code,
        "alias": ctx.alias,
        "runId": ctx.run_id,
        "status": status,
        "engine": ctx.engine,
        "mode": ctx.mode,
        "model": ctx.model,
        "cwd": ctx.source_cwd,
        "executionCwd": ctx.execution_cwd,
        "workspaceKind": ctx.workspace_kind,
        "durationMs": duration_ms,
        "snapshotCommand": snapshot_command(ctx.alias),
        "completionReportCommand": completion_report_command(ctx.alias),
        "completionReportPath": completion_report_path(ctx.run_id),
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
    }
    if ctx.isolated_workspace:
        payload["isolatedWorkspace"] = True
    if not ok:
        payload["error"] = "child_failed"
        payload["message"] = "Child command failed."
    return payload


def _drain_stream(
    pipe: Any,
    log_path: Path,
    byte_counter: list[int],
    *,
    on_stdout_line: Callable[[str], None] | None,
) -> None:
    with log_path.open("ab") as log_handle:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            byte_counter[0] += len(chunk)
            log_handle.write(chunk)
            log_handle.flush()
            if on_stdout_line is not None:
                with contextlib.suppress(Exception):
                    on_stdout_line(chunk.decode("utf-8", errors="replace"))


def _join_drain_thread(thread: threading.Thread, pipe: Any | None) -> None:
    thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
    if thread.is_alive() and pipe is not None:
        with contextlib.suppress(OSError):
            pipe.close()
        thread.join(timeout=1.0)


def execute_tracked(
    argv: list[str],
    cwd: str,
    ctx: RunContext,
    *,
    json_mode: bool,
    stdout: TextIO,
    stderr: TextIO,
    completion_report_mode: str = "markdown",
) -> tuple[int, dict[str, Any] | None]:
    run_path = run_dir(ctx.registry_root, ctx.run_id)
    run_path.mkdir(parents=True, exist_ok=True)
    write_manifest(run_path, build_manifest(ctx, argv))

    stdout_log = run_path / "stdout.log"
    stderr_log = run_path / "stderr.log"
    stdout_log.write_bytes(b"")
    stderr_log.write_bytes(b"")

    accumulator = harness_events.StreamAccumulator()

    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    persist_progress(run_path, ctx, accumulator, status="running", pid=process.pid)

    line_buffer = ""
    stdout_bytes_counter = [0]
    stderr_bytes_counter = [0]
    lines_since_persist = 0
    last_persist_at = time.monotonic()

    def maybe_persist_running() -> None:
        nonlocal lines_since_persist, last_persist_at
        lines_since_persist = 0
        last_persist_at = time.monotonic()
        persist_progress(
            run_path,
            ctx,
            accumulator,
            status="running",
            pid=process.pid,
            stdout_bytes=stdout_bytes_counter[0],
            stderr_bytes=stderr_bytes_counter[0],
        )

    def handle_stdout_line(chunk_text: str) -> None:
        nonlocal line_buffer, lines_since_persist, last_persist_at
        line_buffer += chunk_text
        while "\n" in line_buffer:
            line, line_buffer = line_buffer.split("\n", 1)
            accumulator.ingest_line(line)
            append_event(run_path, {"kind": "stream.line", "stream": "stdout", "text": line[:500]})
            lines_since_persist += 1
            elapsed = time.monotonic() - last_persist_at
            if (
                lines_since_persist >= PROGRESS_PERSIST_LINE_INTERVAL
                or elapsed >= PROGRESS_PERSIST_TIME_INTERVAL_SEC
            ):
                maybe_persist_running()

    stdout_thread = threading.Thread(
        target=_drain_stream,
        args=(process.stdout, stdout_log, stdout_bytes_counter),
        kwargs={"on_stdout_line": handle_stdout_line},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(process.stderr, stderr_log, stderr_bytes_counter),
        kwargs={"on_stdout_line": None},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    exit_code = process.wait()
    _join_drain_thread(stdout_thread, process.stdout)
    _join_drain_thread(stderr_thread, process.stderr)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    if line_buffer.strip():
        accumulator.ingest_line(line_buffer)
    duration_ms = int((time.monotonic() - started) * 1000)

    stdout_bytes = stdout_log.stat().st_size if stdout_log.exists() else 0
    stderr_bytes = stderr_log.stat().st_size if stderr_log.exists() else 0
    status = status_from_exit(exit_code)
    if accumulator.completion_text:
        report_source = accumulator.completion_text
    elif completion_report_mode == "markdown":
        report_source = accumulator.assistant_text
    else:
        report_source = ""
    report_written = write_completion_report(run_path, report_source)
    persist_progress(
        run_path,
        ctx,
        accumulator,
        status=status,
        exit_code=exit_code,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        completion_report_written=report_written,
    )

    ok = exit_code == 0
    if json_mode:
        payload = completion_json_payload(
            ctx,
            ok=ok,
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
        )
        return exit_code, payload

    emit_bounded_text_summary(ctx, status=status, duration_ms=duration_ms, stdout=stdout)
    return exit_code, None


def execute_passthrough(argv: list[str], cwd: str) -> int:
    """Stream child stdout/stderr to the caller. JSON mode is not supported."""
    completed = subprocess.run(argv, cwd=cwd, text=True, check=False)
    return completed.returncode
