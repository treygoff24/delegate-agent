from __future__ import annotations

import contextlib
import json
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

from delegate_agent import config as delegate_config
from delegate_agent import harness_events, rendering, run_registry
from delegate_agent.json_types import JsonObject

STDOUT_LOG = run_registry.STDOUT_LOG
STDERR_LOG = run_registry.STDERR_LOG
EVENTS_JSONL = run_registry.EVENTS_JSONL
MANIFEST_FILE = run_registry.MANIFEST_FILE
STATE_FILE = run_registry.STATE_FILE
SNAPSHOT_FILE = run_registry.SNAPSHOT_FILE
COMPLETION_REPORT_FILE = run_registry.COMPLETION_REPORT_FILE

PROGRESS_PERSIST_LINE_INTERVAL = 10
PROGRESS_PERSIST_TIME_INTERVAL_SEC = 0.5
DRAIN_JOIN_TIMEOUT_SEC = 5.0


SKILL_REVIEW_PREFIX = """## Delegate sub-agent skill review requirement

Before doing the task, review the full list of skills available in your current agent environment. Load/read and apply any skill instructions that are relevant to the task, workspace, tools, code quality, verification, or final deliverable. If no skill is relevant, proceed normally after explicitly deciding that. This requirement is mandatory for every Delegate Agent run; do not skip it just because the parent prompt did not mention skills.

"""

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
    model: str | None
    source_cwd: str
    execution_cwd: str
    workspace_kind: str
    isolated_workspace: bool
    started_at: str
    creation_context: JsonObject | None = None
    source_git_root: str | None = None
    isolation_mode: str = "none"
    effective_isolation: str = "none"
    isolation_lifecycle: str = "none"
    preserved_workspace: bool = False
    branch: str | None = None
    worktree_status: str | None = None


def prepend_skill_review_instructions(prompt: str) -> str:
    if prompt.startswith(SKILL_REVIEW_PREFIX):
        return prompt
    return SKILL_REVIEW_PREFIX + prompt


def append_completion_report_instructions(prompt: str) -> str:
    if prompt.rstrip().endswith(COMPLETION_REPORT_SUFFIX.strip()):
        return prompt
    return prompt + COMPLETION_REPORT_SUFFIX


def write_manifest(run_path: Path, manifest: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / MANIFEST_FILE, manifest)


def write_state(run_path: Path, state: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / STATE_FILE, state)


def write_snapshot(run_path: Path, snapshot: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / SNAPSHOT_FILE, snapshot)


def open_events_log(run_path: Path) -> TextIO:
    run_path.mkdir(parents=True, exist_ok=True)
    return (run_path / EVENTS_JSONL).open("a", encoding="utf-8")


def append_event(handle: TextIO, event: JsonObject) -> None:
    handle.write(json.dumps(event, sort_keys=True) + "\n")


def completion_report_path(run_id: str) -> str:
    return f".delegate/runs/{run_id}/{COMPLETION_REPORT_FILE}"


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


def build_manifest(ctx: RunContext, argv: list[str]) -> JsonObject:
    payload: JsonObject = {
        "schema": run_registry.MANIFEST_SCHEMA,
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
    payload["isolatedWorkspace"] = ctx.isolated_workspace
    payload["isolationMode"] = ctx.isolation_mode
    payload["effectiveIsolation"] = ctx.effective_isolation
    payload["isolationLifecycle"] = ctx.isolation_lifecycle
    payload["preservedWorkspace"] = ctx.preserved_workspace
    if ctx.source_git_root is not None:
        payload["sourceGitRoot"] = ctx.source_git_root
    if ctx.branch is not None:
        payload["branch"] = ctx.branch
    if ctx.creation_context is not None:
        payload["creationContext"] = ctx.creation_context
    if ctx.worktree_status is not None:
        payload["worktreeStatus"] = ctx.worktree_status
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
    extra: JsonObject | None = None,
) -> JsonObject:
    now = run_registry.utc_now_iso()
    state: JsonObject = {
        "schema": run_registry.STATE_SCHEMA,
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
    if extra is not None:
        state.update(extra)
    return state


def _worktree_cleanup_commands(ctx: RunContext) -> JsonObject | None:
    """Build the worktreeCleanupCommands object for persistent worktree runs.

    Returns None if the run is not a persistent worktree run.
    """
    if ctx.isolation_lifecycle != "persistent" or ctx.branch is None:
        return None
    alias_str = ctx.alias
    source_git = ctx.source_git_root or ""
    exec_cwd = ctx.execution_cwd
    branch = ctx.branch
    remove_argv = ["git", "-C", source_git, "worktree", "remove", "--force", exec_cwd]
    branch_argv = ["git", "-C", source_git, "branch", "-D", branch]
    return {
        "safe": f"delegate worktree remove {alias_str}",
        "forceBranch": f"delegate worktree remove {alias_str} --force-branch",
        "discardUncommitted": f"delegate worktree remove {alias_str} --discard-uncommitted",
        "rawGit": f"{shlex.join(remove_argv)} && {shlex.join(branch_argv)}",
    }


def build_snapshot(
    ctx: RunContext,
    *,
    accumulator: harness_events.StreamAccumulator,
    exit_code: int | None = None,
    completion_report_written: bool = False,
) -> JsonObject:
    _assistant_text, assistant_meta = accumulator.bounded_assistant_text()
    recent_events, events_meta = accumulator.bounded_recent_events()
    snapshot: JsonObject = {
        "schema": run_registry.SNAPSHOT_SCHEMA,
        "ok": True,
        "alias": ctx.alias,
        "runId": ctx.run_id,
        "harness": ctx.harness,
        "cwd": ctx.source_cwd,
        "executionCwd": ctx.execution_cwd,
        "mode": ctx.mode,
        "model": ctx.model,
        "startedAt": ctx.started_at,
        "current": accumulator.current,
        "recentEvents": recent_events,
        **assistant_meta,
        **events_meta,
    }
    # Always emit isolatedWorkspace as explicit boolean.
    snapshot["isolatedWorkspace"] = ctx.isolated_workspace

    # Always emit isolation metadata.
    snapshot["isolationMode"] = ctx.isolation_mode
    snapshot["effectiveIsolation"] = ctx.effective_isolation
    snapshot["isolationLifecycle"] = ctx.isolation_lifecycle
    snapshot["preservedWorkspace"] = ctx.preserved_workspace

    # Surface persistent-worktree fields.
    if ctx.source_git_root is not None:
        snapshot["sourceGitRoot"] = ctx.source_git_root
    if ctx.branch is not None:
        snapshot["branch"] = ctx.branch
    if ctx.creation_context is not None:
        snapshot["creationContext"] = ctx.creation_context
    if ctx.worktree_status is not None:
        snapshot["worktreeStatus"] = ctx.worktree_status

    # Worktree cleanup commands for persistent worktrees.
    cleanup = _worktree_cleanup_commands(ctx)
    if cleanup is not None:
        snapshot["worktreeCleanupCommands"] = cleanup

    if exit_code is not None:
        snapshot["exitCode"] = exit_code
    if completion_report_written:
        report_path = completion_report_path(ctx.run_id)
        snapshot["completionReport"] = {
            "path": report_path,
            "command": run_registry.run_output_command(ctx.alias, completion_report=True),
        }
    if ctx.creation_context is not None:
        snapshot["creationContext"] = ctx.creation_context
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
            accumulator=accumulator,
            exit_code=exit_code,
            completion_report_written=completion_report_written,
        ),
    )


def write_completion_report(run_path: Path, text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    path = run_path / COMPLETION_REPORT_FILE
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
    print(f"source: {ctx.source_cwd}", file=stdout)
    print(f"execution: {ctx.execution_cwd}", file=stdout)
    if ctx.branch:
        print(f"branch: {ctx.branch}", file=stdout)
    lifecycle = ctx.isolation_lifecycle
    if lifecycle == "persistent":
        print("isolation: worktree persistent", file=stdout)
    elif lifecycle == "temporary":
        print("isolation: worktree temporary", file=stdout)
    else:
        print(f"isolation: {lifecycle}", file=stdout)
    print(f"snapshot: {run_registry.snapshot_command(ctx.alias)}", file=stdout)
    print(
        f"completion report: {run_registry.run_output_command(ctx.alias, completion_report=True)}",
        file=stdout,
    )
    if ctx.execution_cwd and (lifecycle == "temporary" or lifecycle == "persistent"):
        print(
            f"inspect: {shlex.join(['git', '-C', ctx.execution_cwd, 'status', '--short'])}",
            file=stdout,
        )
        print(
            f"review diff: {shlex.join(['git', '-C', ctx.execution_cwd, 'diff', '--stat', 'HEAD'])}",
            file=stdout,
        )
    if lifecycle == "persistent" and ctx.branch and ctx.source_git_root:
        cleanup = _worktree_cleanup_commands(ctx)
        if cleanup is not None:
            rendering.render_worktree_cleanup_commands(cleanup, stdout)


def completion_json_payload(
    ctx: RunContext,
    *,
    ok: bool,
    status: str,
    exit_code: int,
    duration_ms: int,
    stdout_bytes: int,
    stderr_bytes: int,
) -> JsonObject:
    payload: JsonObject = {
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
        "snapshotCommand": run_registry.snapshot_command(ctx.alias),
        "completionReportCommand": run_registry.run_output_command(
            ctx.alias, completion_report=True
        ),
        "completionReportPath": completion_report_path(ctx.run_id),
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
    }
    # Always emit isolatedWorkspace as explicit boolean.
    payload["isolatedWorkspace"] = ctx.isolated_workspace

    # Always emit isolation metadata.
    payload["isolationMode"] = ctx.isolation_mode
    payload["effectiveIsolation"] = ctx.effective_isolation
    payload["isolationLifecycle"] = ctx.isolation_lifecycle
    payload["preservedWorkspace"] = ctx.preserved_workspace

    # Surface persistent-worktree fields.
    if ctx.source_git_root is not None:
        payload["sourceGitRoot"] = ctx.source_git_root
    if ctx.branch is not None:
        payload["branch"] = ctx.branch
    if ctx.creation_context is not None:
        payload["creationContext"] = ctx.creation_context
    if ctx.worktree_status is not None:
        payload["worktreeStatus"] = ctx.worktree_status

    # Worktree cleanup commands for persistent worktrees.
    cleanup = _worktree_cleanup_commands(ctx)
    if cleanup is not None:
        payload["worktreeCleanupCommands"] = cleanup

    if not ok:
        payload["error"] = "child_failed"
        payload["message"] = "Child command failed."
    return payload


def _drain_stream(
    pipe: BinaryIO,
    log_path: Path,
    byte_counter: list[int],
    *,
    on_line: Callable[[str], None] | None,
) -> None:
    with log_path.open("ab") as log_handle:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            byte_counter[0] += len(chunk)
            log_handle.write(chunk)
            if on_line is not None:
                on_line(chunk.decode("utf-8", errors="replace"))


def _join_drain_thread(thread: threading.Thread, pipe: BinaryIO | None) -> None:
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
    completion_report_mode: str = delegate_config.COMPLETION_REPORT_MODE_MARKDOWN,
) -> tuple[int, JsonObject | None]:
    run_path = run_registry.run_directory(ctx.registry_root, ctx.run_id)
    run_path.mkdir(parents=True, exist_ok=True)
    write_manifest(run_path, build_manifest(ctx, argv))

    stdout_log = run_path / STDOUT_LOG
    stderr_log = run_path / STDERR_LOG
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
    progress_dirty = False

    def maybe_persist_running() -> None:
        nonlocal lines_since_persist, last_persist_at, progress_dirty
        if not progress_dirty:
            return
        progress_dirty = False
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

    with open_events_log(run_path) as events_handle:

        def handle_stdout_line(chunk_text: str) -> None:
            nonlocal line_buffer, lines_since_persist, last_persist_at, progress_dirty
            line_buffer += chunk_text
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                accumulator.ingest_line(line)
                progress_dirty = True
                append_event(
                    events_handle,
                    {"kind": "stream.line", "stream": "stdout", "text": line[:500]},
                )
                lines_since_persist += 1
                elapsed = time.monotonic() - last_persist_at
                if (
                    lines_since_persist >= PROGRESS_PERSIST_LINE_INTERVAL
                    or elapsed >= PROGRESS_PERSIST_TIME_INTERVAL_SEC
                ):
                    events_handle.flush()
                    maybe_persist_running()

        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_log, stdout_bytes_counter),
            kwargs={"on_line": handle_stdout_line},
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_log, stderr_bytes_counter),
            kwargs={"on_line": None},
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

    stdout_bytes = stdout_bytes_counter[0]
    stderr_bytes = stderr_bytes_counter[0]
    status = status_from_exit(exit_code)
    if accumulator.completion_text:
        report_source = accumulator.completion_text
    elif completion_report_mode == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN:
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
