from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, TextIO

from delegate_agent import config as delegate_config
from delegate_agent import (
    harness_events,
    prompt_instructions,
    reasoning,
    redaction,
    rendering,
    run_metadata,
    run_registry,
    worktree_summary,
)
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
MILLISECONDS_PER_SECOND = 1000
PROGRESS_INITIAL_DELAY_SEC = 30.0
PROGRESS_HEARTBEAT_INTERVAL_SEC = 60.0
PROGRESS_INITIAL_DELAY_ENV = "DELEGATE_PROGRESS_INITIAL_DELAY_SEC"
PROGRESS_INTERVAL_ENV = "DELEGATE_PROGRESS_INTERVAL_SEC"

SKILL_REVIEW_PREFIX = prompt_instructions.SKILL_REVIEW_PREFIX
COMPLETION_REPORT_SUFFIX = prompt_instructions.COMPLETION_REPORT_SUFFIX
prepend_skill_review_instructions = prompt_instructions.prepend_skill_review_instructions
append_completion_report_instructions = prompt_instructions.append_completion_report_instructions


class RunnerLaunchError(RuntimeError):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


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
    safe_workspace_method: str | None = None
    warnings: tuple[str, ...] = ()
    reasoning_effort: str | None = None
    reasoning_effort_source: str | None = None
    reasoning_capability_source: str | None = None
    reasoning_transport: str | None = None
    prompt_transport: str = "argv"
    forbid_commit: bool = False


def write_manifest(run_path: Path, manifest: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / MANIFEST_FILE, manifest)


def write_state(run_path: Path, state: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / STATE_FILE, state)


def write_snapshot(run_path: Path, snapshot: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / SNAPSHOT_FILE, snapshot)


def open_events_log(run_path: Path) -> TextIO:
    run_registry.ensure_private_dir(run_path)
    fd = os.open(
        run_path / EVENTS_JSONL,
        os.O_CREAT | os.O_APPEND | os.O_WRONLY,
        run_registry.PRIVATE_FILE_MODE,
    )
    return os.fdopen(fd, "a", encoding="utf-8")


def append_event(handle: TextIO, event: JsonObject) -> None:
    handle.write(json.dumps(event, sort_keys=True) + "\n")


def completion_report_path(run_id: str) -> str:
    return f".delegate/runs/{run_id}/{COMPLETION_REPORT_FILE}"


def format_duration(duration_ms: int) -> str:
    total_seconds = max(duration_ms // MILLISECONDS_PER_SECOND, 0)
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
        "promptTransport": ctx.prompt_transport,
    }
    run_metadata.add_run_metadata_payload_fields(payload, ctx)
    reasoning.add_reasoning_payload_fields(payload, ctx)
    if ctx.forbid_commit:
        payload["commitPolicy"] = {"forbidCommit": True}
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
    remove_argv = ["git", "-C", source_git, "worktree", "remove", exec_cwd]
    branch_argv = ["git", "-C", source_git, "branch", "-d", branch]
    return {
        "safe": f"delegate worktree remove {alias_str}",
        "forceBranch": f"delegate worktree remove {alias_str} --force-branch",
        "discardUncommitted": f"delegate worktree remove {alias_str} --discard-uncommitted",
        "force": f"delegate worktree remove {alias_str} --force",
        "rawGit": f"{shlex.join(remove_argv)} && {shlex.join(branch_argv)}",
    }


def build_snapshot(
    ctx: RunContext,
    *,
    accumulator: harness_events.StreamAccumulator,
    exit_code: int | None = None,
    completion_report_written: bool = False,
    extra: JsonObject | None = None,
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
    run_metadata.add_run_metadata_payload_fields(snapshot, ctx)
    reasoning.add_reasoning_payload_fields(snapshot, ctx)

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
    if extra is not None:
        snapshot.update(extra)
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
    extra: JsonObject | None = None,
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
            extra=extra,
        ),
    )
    write_snapshot(
        run_path,
        build_snapshot(
            ctx,
            accumulator=accumulator,
            exit_code=exit_code,
            completion_report_written=completion_report_written,
            extra=extra,
        ),
    )


def write_completion_report(run_path: Path, text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    path = run_path / COMPLETION_REPORT_FILE
    run_registry.write_private_text(path, cleaned + "\n")
    return True


def emit_bounded_text_summary(
    ctx: RunContext,
    *,
    status: str,
    duration_ms: int,
    stdout: TextIO,
    extra: JsonObject | None = None,
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
    if ctx.safe_workspace_method:
        print(f"safe workspace method: {ctx.safe_workspace_method}", file=stdout)
    for warning in ctx.warnings:
        print(f"warning: {warning}", file=stdout)
    if extra is not None:
        work_summary = extra.get("workSummary")
        if isinstance(work_summary, dict):
            commits_created = work_summary.get("commitsCreatedCount", 0)
            print(
                "work summary: "
                f"{work_summary.get('changedFilesCount', 0)} changed files, "
                f"{commits_created} commits",
                file=stdout,
            )
            if commits_created:
                print(
                    "warning: child created commits; review them before integration",
                    file=stdout,
                )
            if work_summary.get("noChanges") is True:
                print("work summary: no file changes or commits detected", file=stdout)
        if extra.get("commitPolicyViolated") is True:
            print("commit policy: violated (--forbid-commit)", file=stdout)
        if extra.get("commitPolicyUnverified") is True:
            print("commit policy: unverified (--forbid-commit)", file=stdout)
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
    completion_report_written: bool = False,
    extra: JsonObject | None = None,
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
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
    }
    if completion_report_written:
        payload["completionReportCommand"] = run_registry.run_output_command(
            ctx.alias, completion_report=True
        )
        payload["completionReportPath"] = completion_report_path(ctx.run_id)
    run_metadata.add_run_metadata_payload_fields(payload, ctx)
    reasoning.add_reasoning_payload_fields(payload, ctx)

    # Worktree cleanup commands for persistent worktrees.
    cleanup = _worktree_cleanup_commands(ctx)
    if cleanup is not None:
        payload["worktreeCleanupCommands"] = cleanup
    if extra is not None:
        payload.update(extra)

    if not ok:
        if extra is not None and (
            extra.get("commitPolicyViolated") is True or extra.get("commitPolicyUnverified") is True
        ):
            payload["error"] = str(extra.get("error") or "commit_policy_violated")
            payload["message"] = str(extra.get("message") or "Commit policy failed.")
        else:
            payload["error"] = "child_failed"
            payload["message"] = "Child command failed."
    return payload


def _drain_stream(
    pipe: BinaryIO,
    log_path: Path,
    byte_counter: ByteCounter,
    *,
    on_line: Callable[[str], None] | None,
) -> None:
    with log_path.open("ab") as log_handle:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            byte_counter.total += len(chunk)
            log_handle.write(chunk)
            if on_line is not None:
                on_line(chunk.decode("utf-8", errors="replace"))


def _join_drain_thread(thread: threading.Thread, pipe: BinaryIO | None) -> None:
    thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
    if thread.is_alive() and pipe is not None:
        with contextlib.suppress(OSError):
            pipe.close()
        thread.join(timeout=1.0)


def _write_stdin(pipe: BinaryIO | None, stdin_text: str, failures: list[str]) -> None:
    if pipe is None:
        return
    try:
        pipe.write(stdin_text.encode("utf-8"))
        pipe.flush()
    except (BrokenPipeError, OSError) as exc:
        # The child may have exited or closed stdin before reading the prompt.
        # Record it so the run can report possibly-undelivered prompt text
        # instead of silently proceeding as if delivery succeeded.
        failures.append(f"stdin prompt delivery may have failed: {exc}")
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _join_stdin_thread(thread: threading.Thread | None, pipe: BinaryIO | None) -> None:
    if thread is not None:
        _join_drain_thread(thread, pipe)


def _materialize_prompt_file_argv(
    argv: list[str],
    *,
    prompt_file_text: str | None,
    prompt_file_placeholder: str | None,
) -> tuple[list[str], Path | None]:
    if prompt_file_text is None:
        return list(argv), None
    if prompt_file_placeholder is None or prompt_file_placeholder not in argv:
        raise ValueError("prompt_file_placeholder must be present in argv")
    temp_dir = Path(tempfile.mkdtemp(prefix="delegate-prompt-"))
    os.chmod(temp_dir, run_registry.PRIVATE_DIR_MODE)
    prompt_path = temp_dir / "prompt.txt"
    fd = os.open(
        prompt_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        run_registry.PRIVATE_FILE_MODE,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(prompt_file_text)
    return [
        str(prompt_path) if item == prompt_file_placeholder else item for item in argv
    ], temp_dir


def _cleanup_prompt_file_dir(temp_dir: Path | None) -> None:
    if temp_dir is not None:
        shutil.rmtree(temp_dir, ignore_errors=True)


@dataclass(frozen=True)
class TrackedRunFiles:
    run_path: Path
    stdout_log: Path
    stderr_log: Path


@dataclass(frozen=True)
class TrackedCaptureResult:
    accumulator: harness_events.StreamAccumulator
    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    stdin_failures: tuple[str, ...]


@dataclass(frozen=True)
class TrackedFinalization:
    status: str
    exit_code: int
    report_written: bool
    extra: JsonObject


def _persistent_work_summary(ctx: RunContext) -> JsonObject | None:
    if ctx.isolation_lifecycle != "persistent":
        return None
    return worktree_summary.build_work_summary(
        source_git_root=ctx.source_git_root,
        execution_cwd=ctx.execution_cwd,
        branch=ctx.branch,
        creation_context=ctx.creation_context,
    )


def _final_extra(ctx: RunContext, capture_exit_code: int) -> tuple[int, JsonObject]:
    extra: JsonObject = {}
    summary = _persistent_work_summary(ctx)
    if summary is not None:
        extra["workSummary"] = summary
    commits_created = worktree_summary.commits_created_count(summary)
    if (
        summary is not None
        and commits_created is not None
        and commits_created > 0
        and not ctx.forbid_commit
    ):
        extra["warnings"] = [
            "Child command created commits; review the persistent worktree before integration."
        ]
        extra["nextActions"] = [
            f"delegate worktree show {ctx.alias}",
            f"git -C {shlex.quote(ctx.execution_cwd)} log --oneline --decorate --max-count=5 HEAD",
        ]
        extra["commitsCreatedByChild"] = True
    if ctx.forbid_commit:
        unverified = commits_created is None
        violated = commits_created is not None and commits_created > 0
        extra["commitPolicy"] = {
            "forbidCommit": True,
            "violated": violated,
            "verified": not unverified,
            "commitsCreatedCount": commits_created,
        }
        if unverified:
            extra["commitPolicyUnverified"] = True
            extra["childExitCode"] = capture_exit_code
            extra["error"] = "commit_policy_unverified"
            extra["message"] = (
                "Delegate could not verify --forbid-commit because final Git inspection failed."
            )
            if capture_exit_code == 0:
                return 1, extra
        if violated:
            extra["commitPolicyViolated"] = True
            extra["childExitCode"] = capture_exit_code
            extra["error"] = "commit_policy_violated"
            extra["message"] = "Child command created commits even though --forbid-commit was set."
            if capture_exit_code == 0:
                return 1, extra
    return capture_exit_code, extra


@dataclass
class ByteCounter:
    total: int = 0


def _progress_interval_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(value, 0.01)


def _progress_current_label(accumulator: harness_events.StreamAccumulator) -> str:
    current = (accumulator.current or "").strip()
    if not current:
        return "waiting for child output"
    return redaction.redact_progress_label(current)[:160]


def _emit_progress_started(ctx: RunContext, stderr: TextIO) -> None:
    print(
        "delegate: run started "
        f"alias={ctx.alias} handle={ctx.alias} "
        f"snapshot={run_registry.snapshot_command(ctx.alias)!r}",
        file=stderr,
        flush=True,
    )


def _emit_progress_heartbeat(
    ctx: RunContext,
    accumulator: harness_events.StreamAccumulator,
    *,
    started: float,
    stderr: TextIO,
) -> None:
    elapsed_ms = int((time.monotonic() - started) * MILLISECONDS_PER_SECOND)
    print(
        "delegate: still running "
        f"alias={ctx.alias} elapsed={format_duration(elapsed_ms)} "
        f"last_event={_progress_current_label(accumulator)!r}",
        file=stderr,
        flush=True,
    )


def _prepare_tracked_run(
    argv: list[str],
    ctx: RunContext,
    *,
    manifest_argv: list[str] | None,
) -> TrackedRunFiles:
    run_path = run_registry.run_directory(ctx.registry_root, ctx.run_id)
    run_registry.ensure_private_dir(run_path)
    write_manifest(run_path, build_manifest(ctx, manifest_argv or argv))

    stdout_log = run_path / STDOUT_LOG
    stderr_log = run_path / STDERR_LOG
    run_registry.write_private_bytes(stdout_log, b"")
    run_registry.write_private_bytes(stderr_log, b"")
    return TrackedRunFiles(run_path=run_path, stdout_log=stdout_log, stderr_log=stderr_log)


def _launch_tracked_process(
    argv: list[str],
    cwd: str,
    *,
    stdin_text: str | None,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # nosec B603 - Delegate intentionally launches validated harness argv with shell=False.
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _runner_launch_error(argv: list[str], cwd: str, exc: OSError) -> RunnerLaunchError:
    binary = argv[0] if argv else "<empty argv>"
    return RunnerLaunchError(
        "child_launch_failed",
        f"Failed to launch child command {binary!r} in {cwd}: {exc}",
    )


def _record_tracked_launch_failure(
    files: TrackedRunFiles,
    ctx: RunContext,
    error: RunnerLaunchError,
) -> None:
    extra: JsonObject = {"error": error.error, "message": error.message}
    write_state(files.run_path, build_state(ctx, status="failed", extra=extra))
    snapshot = build_snapshot(ctx, accumulator=harness_events.StreamAccumulator())
    snapshot["ok"] = False
    snapshot["status"] = "failed"
    snapshot.update(extra)
    write_snapshot(files.run_path, snapshot)


def _capture_tracked_process(
    process: subprocess.Popen[bytes],
    files: TrackedRunFiles,
    ctx: RunContext,
    *,
    started: float,
    stdin_text: str | None,
    progress_stderr: TextIO | None = None,
) -> TrackedCaptureResult:
    accumulator = harness_events.StreamAccumulator()
    persist_progress(files.run_path, ctx, accumulator, status="running", pid=process.pid)

    line_buffer = ""
    stdout_bytes_counter = ByteCounter()
    stderr_bytes_counter = ByteCounter()
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
            files.run_path,
            ctx,
            accumulator,
            status="running",
            pid=process.pid,
            stdout_bytes=stdout_bytes_counter.total,
            stderr_bytes=stderr_bytes_counter.total,
        )

    if process.stdout is None or process.stderr is None:
        raise RunnerLaunchError(
            "missing_child_stream",
            "Child process did not expose stdout/stderr pipes for tracking.",
        )
    with open_events_log(files.run_path) as events_handle:

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
            args=(process.stdout, files.stdout_log, stdout_bytes_counter),
            kwargs={"on_line": handle_stdout_line},
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, files.stderr_log, stderr_bytes_counter),
            kwargs={"on_line": None},
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        stdin_thread: threading.Thread | None = None
        stdin_failures: list[str] = []
        if stdin_text is not None:
            stdin_thread = threading.Thread(
                target=_write_stdin,
                args=(process.stdin, stdin_text, stdin_failures),
                daemon=True,
            )
            stdin_thread.start()
        # Child agent runtimes are intentionally unbounded: callers cancel them
        # via the OS/run-management surface, while quick metadata probes
        # elsewhere use explicit timeouts.
        if progress_stderr is not None:
            _emit_progress_started(ctx, progress_stderr)
            initial_delay = _progress_interval_from_env(
                PROGRESS_INITIAL_DELAY_ENV,
                PROGRESS_INITIAL_DELAY_SEC,
            )
            interval = _progress_interval_from_env(
                PROGRESS_INTERVAL_ENV,
                PROGRESS_HEARTBEAT_INTERVAL_SEC,
            )
            next_progress_at = time.monotonic() + initial_delay
            while True:
                timeout = max(next_progress_at - time.monotonic(), 0.01)
                try:
                    exit_code = process.wait(timeout=timeout)
                    break
                except subprocess.TimeoutExpired:
                    _emit_progress_heartbeat(
                        ctx,
                        accumulator,
                        started=started,
                        stderr=progress_stderr,
                    )
                    next_progress_at = time.monotonic() + interval
        else:
            exit_code = process.wait()
        _join_stdin_thread(stdin_thread, process.stdin)
        _join_drain_thread(stdout_thread, process.stdout)
        _join_drain_thread(stderr_thread, process.stderr)
        process.stdout.close()
        process.stderr.close()
        if line_buffer.strip():
            accumulator.ingest_line(line_buffer)
    return TrackedCaptureResult(
        accumulator=accumulator,
        exit_code=exit_code,
        duration_ms=int((time.monotonic() - started) * MILLISECONDS_PER_SECOND),
        stdout_bytes=stdout_bytes_counter.total,
        stderr_bytes=stderr_bytes_counter.total,
        stdin_failures=tuple(stdin_failures),
    )


def _ctx_with_stdin_warnings(
    ctx: RunContext,
    stdin_failures: tuple[str, ...],
    stderr: TextIO,
) -> RunContext:
    if not stdin_failures:
        return ctx
    for failure in stdin_failures:
        print(f"warning: {failure}", file=stderr)
    return replace(ctx, warnings=(*ctx.warnings, *stdin_failures))


def _completion_report_source(
    ctx: RunContext,
    accumulator: harness_events.StreamAccumulator,
    *,
    completion_report_mode: str,
) -> str:
    if accumulator.completion_text:
        return accumulator.completion_text
    if (
        completion_report_mode == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN
        and ctx.harness != "codex"
        and ctx.engine != "codex"
    ):
        return accumulator.assistant_text
    return ""


def _finalize_tracked_run(
    files: TrackedRunFiles,
    ctx: RunContext,
    capture: TrackedCaptureResult,
    *,
    completion_report_mode: str,
) -> TrackedFinalization:
    exit_code, extra = _final_extra(ctx, capture.exit_code)
    status = status_from_exit(exit_code)
    report_written = write_completion_report(
        files.run_path,
        _completion_report_source(
            ctx,
            capture.accumulator,
            completion_report_mode=completion_report_mode,
        ),
    )
    persist_progress(
        files.run_path,
        ctx,
        capture.accumulator,
        status=status,
        exit_code=exit_code,
        stdout_bytes=capture.stdout_bytes,
        stderr_bytes=capture.stderr_bytes,
        completion_report_written=report_written,
        extra=extra,
    )
    return TrackedFinalization(
        status=status, exit_code=exit_code, report_written=report_written, extra=extra
    )


def _tracked_result(
    ctx: RunContext,
    capture: TrackedCaptureResult,
    finalization: TrackedFinalization,
    *,
    json_mode: bool,
    stdout: TextIO,
) -> tuple[int, JsonObject | None]:
    ok = finalization.exit_code == 0
    if json_mode:
        payload = completion_json_payload(
            ctx,
            ok=ok,
            status=finalization.status,
            exit_code=finalization.exit_code,
            duration_ms=capture.duration_ms,
            stdout_bytes=capture.stdout_bytes,
            stderr_bytes=capture.stderr_bytes,
            completion_report_written=finalization.report_written,
            extra=finalization.extra,
        )
        return finalization.exit_code, payload

    emit_bounded_text_summary(
        ctx,
        status=finalization.status,
        duration_ms=capture.duration_ms,
        stdout=stdout,
        extra=finalization.extra,
    )
    return finalization.exit_code, None


def execute_tracked(
    argv: list[str],
    cwd: str,
    ctx: RunContext,
    *,
    json_mode: bool,
    stdout: TextIO,
    stderr: TextIO,
    completion_report_mode: str = delegate_config.COMPLETION_REPORT_MODE_MARKDOWN,
    stdin_text: str | None = None,
    prompt_file_text: str | None = None,
    prompt_file_placeholder: str | None = None,
    manifest_argv: list[str] | None = None,
    progress: bool = False,
) -> tuple[int, JsonObject | None]:
    if stdin_text is not None and prompt_file_text is not None:
        raise ValueError("stdin_text and prompt_file_text are mutually exclusive")
    files = _prepare_tracked_run(argv, ctx, manifest_argv=manifest_argv)
    started = time.monotonic()
    launch_argv, prompt_temp_dir = _materialize_prompt_file_argv(
        argv,
        prompt_file_text=prompt_file_text,
        prompt_file_placeholder=prompt_file_placeholder,
    )
    try:
        try:
            process = _launch_tracked_process(
                launch_argv,
                cwd,
                stdin_text=stdin_text,
            )
        except OSError as exc:
            error = _runner_launch_error(launch_argv, cwd, exc)
            _record_tracked_launch_failure(files, ctx, error)
            raise error from exc
        capture = _capture_tracked_process(
            process,
            files,
            ctx,
            started=started,
            stdin_text=stdin_text,
            progress_stderr=stderr if progress else None,
        )
    finally:
        _cleanup_prompt_file_dir(prompt_temp_dir)

    ctx = _ctx_with_stdin_warnings(ctx, capture.stdin_failures, stderr)
    finalization = _finalize_tracked_run(
        files,
        ctx,
        capture,
        completion_report_mode=completion_report_mode,
    )
    return _tracked_result(ctx, capture, finalization, json_mode=json_mode, stdout=stdout)


def execute_passthrough(
    argv: list[str],
    cwd: str,
    *,
    stdin_text: str | None = None,
    prompt_file_text: str | None = None,
    prompt_file_placeholder: str | None = None,
) -> int:
    """Stream child stdout/stderr to the caller. JSON mode is not supported."""
    if stdin_text is not None and prompt_file_text is not None:
        raise ValueError("stdin_text and prompt_file_text are mutually exclusive")
    launch_argv, prompt_temp_dir = _materialize_prompt_file_argv(
        argv,
        prompt_file_text=prompt_file_text,
        prompt_file_placeholder=prompt_file_placeholder,
    )
    try:
        # Passthrough mode mirrors the child runtime directly, so Delegate does
        # not impose a separate timeout here.
        try:
            if stdin_text is None:
                completed = subprocess.run(  # nosec B603 - passthrough intentionally mirrors validated harness argv with shell=False.
                    launch_argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
            else:
                completed = subprocess.run(  # nosec B603 - passthrough intentionally mirrors validated harness argv with shell=False.
                    launch_argv,
                    cwd=cwd,
                    input=stdin_text,
                    text=True,
                    check=False,
                )
        except OSError as exc:
            raise _runner_launch_error(launch_argv, cwd, exc) from exc
        return completed.returncode
    finally:
        _cleanup_prompt_file_dir(prompt_temp_dir)
