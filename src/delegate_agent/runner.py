from __future__ import annotations

import codecs
import contextlib
import errno
import io
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TextIO

from delegate_agent import (
    account_binding,
    child_failures,
    failover_state,
    harness_events,
    mail,
    mail_push,
    notify,
    profiles,
    prompt_instructions,
    reasoning,
    redaction,
    rendering,
    resume_command,
    run_metadata,
    run_registry,
    sandbox_bwrap,
    seatbelt,
    stall_watchdog,
    worktree_summary,
)
from delegate_agent import config as delegate_config
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_SLASH, PROMPT_INSTRUCTION_MODE_WRAPPED
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject, is_non_negative_int

STDOUT_LOG = run_registry.STDOUT_LOG
STDERR_LOG = run_registry.STDERR_LOG
EVENTS_JSONL = run_registry.EVENTS_JSONL
MANIFEST_FILE = run_registry.MANIFEST_FILE
STATE_FILE = run_registry.STATE_FILE
SNAPSHOT_FILE = run_registry.SNAPSHOT_FILE
COMPLETION_REPORT_FILE = run_registry.COMPLETION_REPORT_FILE
PROMPT_TXT_FILE = run_registry.PROMPT_TXT_FILE
PERSONA_TXT_FILE = run_registry.PERSONA_TXT_FILE

PROGRESS_PERSIST_LINE_INTERVAL = 10
PROGRESS_PERSIST_TIME_INTERVAL_SEC = 0.5
DRAIN_JOIN_TIMEOUT_SEC = 5.0
MILLISECONDS_PER_SECOND = 1000
CALL_STDOUT_MAX_BYTES = 16 * 1024 * 1024
CALL_STDERR_MAX_BYTES = 16 * 1024 * 1024
TRACKED_STREAM_MAX_BYTES = 16 * 1024 * 1024
STREAM_READ_CHUNK_BYTES = 64 * 1024
TRACKED_PROCESS_POLL_SEC = 0.05
TERMINAL_EXIT_GRACE_SEC = 1.0
PROCESS_GROUP_TERMINATION_GRACE_SEC = delegate_config.default_process_group_termination_grace_sec()
PROCESS_GROUP_KILL_WAIT_SEC = 5.0
PROCESS_GROUP_TERMINATION_GRACE_ENV = "DELEGATE_PROCESS_GROUP_TERMINATION_GRACE_SEC"
PROGRESS_INITIAL_DELAY_SEC = delegate_config.default_progress_initial_delay_sec()
PROGRESS_HEARTBEAT_INTERVAL_SEC = delegate_config.default_progress_interval_sec()
PROGRESS_INITIAL_DELAY_ENV = "DELEGATE_PROGRESS_INITIAL_DELAY_SEC"
PROGRESS_INTERVAL_ENV = "DELEGATE_PROGRESS_INTERVAL_SEC"
STALL_SECONDS_DEFAULT = stall_watchdog.stall_seconds_from_minutes(
    stall_watchdog.STALL_MINUTES_DEFAULT
)
STALL_MINUTES_ENV = "DELEGATE_STALL_MINUTES"
ZERO_COMMIT_BUDGET_FRACTION_DEFAULT = 0.5
ZERO_COMMIT_BUDGET_FRACTION_ENV = "DELEGATE_ZERO_COMMIT_BUDGET_FRACTION"
ZERO_COMMIT_HEALTH_EVENT_KIND = "run.zero_commits_at_half_budget"
RESULT_QUALITY_OK = harness_events.RESULT_QUALITY_OK
RESULT_QUALITY_HOUSEKEEPING = harness_events.RESULT_QUALITY_HOUSEKEEPING
RESULT_QUALITY_EMPTY = harness_events.RESULT_QUALITY_EMPTY
RESULT_QUALITY_SUSPECT_SHORT = harness_events.RESULT_QUALITY_SUSPECT_SHORT
RESULT_QUALITY_NO_ASSISTANT_TEXT = harness_events.RESULT_QUALITY_NO_ASSISTANT_TEXT
EMPTY_RETRY_INSTRUCTION = (
    "Delegate retry instruction: The previous attempt exited successfully but emitted no final "
    "answer. Emit the final answer or report now as plain text."
)
EMPTY_RETRY_WARNING = "empty_success_retry: the retry also emitted no final answer."
EMPTY_RETRY_SKIPPED_WRITE_CAPABLE_WARNING = (
    "empty_success_retry: skipped because this call is write-capable."
)
EMPTY_RETRY_SKIPPED_VERBATIM_WARNING = (
    "empty_success_retry: skipped to preserve the verbatim prompt boundary."
)
COMPLETION_REPORT_SOURCE_CHILD = "child"
COMPLETION_REPORT_SOURCE_SYNTHESIZED = "delegate_synthesized"
COMPLETION_REPORT_SOURCE_STDOUT_RECOVERY = "stdout_recovery"
SKILL_REVIEW_PREFIX = prompt_instructions.SKILL_REVIEW_PREFIX
COMPLETION_REPORT_SUFFIX = prompt_instructions.COMPLETION_REPORT_SUFFIX
prepend_skill_review_instructions = prompt_instructions.prepend_skill_review_instructions
append_completion_report_instructions = prompt_instructions.append_completion_report_instructions
detect_slash_command = prompt_instructions.detect_slash_command


def _bounded_call_fallback_text(text: str) -> str:
    if len(text) <= harness_events.ASSISTANT_TEXT_LIMIT:
        return text
    head = text[: harness_events.ASSISTANT_TEXT_HEAD]
    tail = text[-harness_events.ASSISTANT_TEXT_TAIL :]
    omitted = len(text) - harness_events.ASSISTANT_TEXT_HEAD - harness_events.ASSISTANT_TEXT_TAIL
    return f"{head}\n\n… [{omitted} chars omitted] …\n\n{tail}"


class RunnerLaunchError(RuntimeError):
    def __init__(self, error: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code


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
    model_alias: str | None = None
    model_resolved: str | None = None
    model_requested: str | None = None
    capability_model: str | None = None
    capability_model_source: str | None = None
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
    requested_reasoning_effort: str | None = None
    reasoning_effort_source: str | None = None
    reasoning_capability_source: str | None = None
    reasoning_capability_evidence: str | None = None
    reasoning_transport: str | None = None
    fast: bool | None = None
    prompt_transport: str = "argv"
    forbid_commit: bool = False
    progress_initial_delay_sec: float = PROGRESS_INITIAL_DELAY_SEC
    progress_interval_sec: float = PROGRESS_HEARTBEAT_INTERVAL_SEC
    stall_seconds: float = STALL_SECONDS_DEFAULT
    process_group_termination_grace_sec: float = PROCESS_GROUP_TERMINATION_GRACE_SEC
    env_overrides: dict[str, str] = field(default_factory=dict)
    fallback_env_overrides: dict[str, str] = field(default_factory=dict)
    auth_profile: str | None = None
    fallback_auth_profile: str | None = None
    codex_failover_identity: str | None = None
    codex_fallback_failover_identity: str | None = None
    include_dirty: bool = False
    synced_files: int = 0
    retire_worktree_on_completion: bool = True
    worktree_auto_prune_on_completion: bool = False
    worktree_auto_prune_merged_older_than_days: int = 7
    group: str | None = None
    notify: str | None = None
    workflow_agent_key: str | None = None
    call_read_only: bool = False
    pure: bool = False
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED
    source_prompt: str | None = None
    progress_requested: str | None = None
    timeout_seconds: int | None = None
    output_schema_text: str | None = None
    agent: str | None = None
    resumed_from: JsonObject | None = None
    worktree_attachment: JsonObject | None = None
    temporary_workspace_cleanup: JsonObject | None = None
    persona_name: str | None = None
    persona_source: str | None = None
    persona_transport: str | None = None
    persona_digest: str | None = None
    persona_file: str | None = None
    persona_text: str | None = None
    mail_push: bool = False
    resumable: bool = False
    followup_of: str | None = None
    resume_session_id: str | None = None
    structured_retry: bool = False
    harness_session_id: str | None = None
    account_binding_command: tuple[str, ...] | None = None
    sandbox: JsonObject | None = None
    # Bounded wait for registry mutations. Finalization writes a WAL when this
    # budget expires; launch admission fails before spawning a child.
    registry_lock_timeout_seconds: float = run_registry.REGISTRY_LOCK_TIMEOUT_SECONDS


def _process_group_grace_seconds(ctx: RunContext) -> float:
    """Resolve the parent-configured group grace, including worktree contexts."""
    raw = (ctx.env_overrides or {}).get(PROCESS_GROUP_TERMINATION_GRACE_ENV)
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = -1.0
        if math.isfinite(value) and value >= 0:
            return value
    return max(float(ctx.process_group_termination_grace_sec), 0.0)


@contextlib.contextmanager
def _launch_registry_lock(ctx: RunContext):
    """Acquire the launch-generation lock or fail before ``Popen``."""
    timeout = _registry_lock_timeout(ctx)
    try:
        with run_registry.registry_lock(
            ctx.registry_root,
            timeout_seconds=timeout,
        ):
            yield
    except TimeoutError as exc:
        raise RunnerLaunchError(
            "registry_lock_timeout",
            f"Could not launch child: registry lock was not acquired within {timeout:g}s.",
        ) from exc


def _registry_lock_timeout(ctx: RunContext) -> float:
    value = getattr(ctx, "registry_lock_timeout_seconds", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return run_registry.resolve_registry_lock_timeout_seconds()
    return max(float(value), 0.0)


def write_manifest(run_path: Path, manifest: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / MANIFEST_FILE, manifest)


def write_state(run_path: Path, state: JsonObject) -> None:
    run_registry.write_json_atomic(run_path / STATE_FILE, state)


def write_snapshot(run_path: Path, snapshot: JsonObject) -> None:
    run_registry.write_snapshot(run_path, snapshot)


def open_events_log(run_path: Path) -> TextIO:
    run_registry.ensure_private_dir(run_path)
    fd = run_registry.open_private_file(
        run_path / EVENTS_JSONL,
        os.O_CREAT | os.O_APPEND | os.O_WRONLY,
    )
    return os.fdopen(fd, "a", encoding="utf-8")


def append_event(handle: TextIO, event: JsonObject) -> None:
    handle.write(json.dumps(event, sort_keys=True) + "\n")


def _stream_line_event_state(events_path: Path) -> tuple[int, bool]:
    written = 0
    try:
        handle = events_path.open(encoding="utf-8")
    except OSError:
        return written, False
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("stream") != "stdout":
                continue
            if event.get("kind") == "stream.lines_truncated":
                return min(written, harness_events.EVENT_LIMIT), True
            if event.get("kind") == "stream.line":
                written += 1
                if written > harness_events.EVENT_LIMIT:
                    return harness_events.EVENT_LIMIT, True
    return min(written, harness_events.EVENT_LIMIT), False


def _stream_line_event(stream: str, text: str) -> JsonObject:
    bounded, truncated, original_chars = harness_events.bounded_event_text(text)
    event: JsonObject = {"kind": "stream.line", "stream": stream, "text": bounded}
    if truncated:
        event["truncated"] = True
        event["textChars"] = original_chars
    return event


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
    return run_registry.STATUS_SUCCEEDED if exit_code == 0 else run_registry.STATUS_FAILED


def _terminal_override_extra(accumulator: harness_events.StreamAccumulator) -> JsonObject:
    if accumulator.terminal_status not in {
        run_registry.STATUS_FAILED,
        run_registry.STATUS_CANCELLED,
    }:
        return {}
    extra: JsonObject = {"terminalStatus": accumulator.terminal_status}
    if accumulator.terminal_status == run_registry.STATUS_CANCELLED:
        extra["failureReason"] = "harness_cancelled"
    if accumulator.terminal_event is not None:
        extra["terminalEvent"] = accumulator.terminal_event
    return extra


def _merge_extra(payload: JsonObject, extra: JsonObject) -> None:
    for key, value in extra.items():
        if (
            key == "warnings"
            and isinstance(payload.get("warnings"), list)
            and isinstance(value, list)
        ):
            merged: list[object] = []
            seen: set[str] = set()
            for warning in [*payload["warnings"], *value]:
                if isinstance(warning, str):
                    if warning in seen:
                        continue
                    seen.add(warning)
                merged.append(warning)
            payload[key] = merged
        else:
            payload[key] = value


def _add_persona_payload_fields(payload: JsonObject, ctx: RunContext) -> None:
    if ctx.persona_name is None:
        return
    payload["personaName"] = ctx.persona_name
    payload["personaSource"] = ctx.persona_source
    payload["personaTransport"] = ctx.persona_transport
    payload["personaDigest"] = ctx.persona_digest
    payload["personaFile"] = ctx.persona_file or PERSONA_TXT_FILE


def build_manifest(ctx: RunContext, argv: list[str]) -> JsonObject:
    payload: JsonObject = {
        "schema": run_registry.MANIFEST_SCHEMA,
        "runId": ctx.run_id,
        "alias": ctx.alias,
        "harness": ctx.harness,
        "engine": ctx.engine,
        "mode": ctx.mode,
        "model": ctx.model,
        "modelAlias": ctx.model_alias,
        "modelResolved": ctx.model_resolved or ctx.model,
        "cwd": ctx.source_cwd,
        "executionCwd": ctx.execution_cwd,
        "workspaceRoot": str(Path(ctx.execution_cwd).resolve(strict=False)),
        "workspaceKind": ctx.workspace_kind,
        "startedAt": ctx.started_at,
        "argv": argv,
        "promptTransport": ctx.prompt_transport,
        "promptInstructionMode": ctx.prompt_instruction_mode,
    }
    if ctx.temporary_workspace_cleanup is not None:
        payload["temporaryWorkspaceCleanup"] = ctx.temporary_workspace_cleanup
    run_metadata.add_run_metadata_payload_fields(payload, ctx)
    run_metadata.add_model_payload_fields(payload, ctx)
    reasoning.add_reasoning_payload_fields(payload, ctx)
    run_metadata.add_speed_payload_fields(payload, ctx)
    if ctx.forbid_commit:
        payload["commitPolicy"] = {"forbidCommit": True}
    if ctx.auth_profile is not None:
        payload["authProfile"] = ctx.auth_profile
    if ctx.fallback_auth_profile is not None:
        payload["fallbackProfile"] = ctx.fallback_auth_profile
    if ctx.group is not None:
        payload["group"] = ctx.group
    if ctx.notify is not None:
        payload["notify"] = {"target": ctx.notify}
    if ctx.workflow_agent_key is not None:
        payload["workflowAgentKey"] = ctx.workflow_agent_key
    if ctx.mail_push:
        payload["mailPush"] = True
    if ctx.resumable:
        payload["resumable"] = True
    if ctx.harness_session_id is not None:
        payload["harnessSessionId"] = ctx.harness_session_id
    if ctx.include_dirty:
        payload["includeDirty"] = True
        payload["syncedFiles"] = ctx.synced_files
    if ctx.source_prompt is not None:
        payload["promptFile"] = PROMPT_TXT_FILE
    if ctx.progress_requested is not None:
        payload["progressRequested"] = ctx.progress_requested
    if ctx.timeout_seconds is not None:
        payload["timeoutSeconds"] = ctx.timeout_seconds
    if ctx.output_schema_text is not None:
        payload["outputSchema"] = ctx.output_schema_text
    if ctx.agent is not None:
        payload["agent"] = ctx.agent
    _add_persona_payload_fields(payload, ctx)
    if ctx.resumed_from is not None:
        payload["resumedFrom"] = ctx.resumed_from
    if ctx.followup_of is not None:
        payload["followupOf"] = ctx.followup_of
    if ctx.structured_retry:
        payload["structuredRetryWorkspace"] = True
    if ctx.worktree_attachment is not None:
        payload["worktreeAttachment"] = ctx.worktree_attachment
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
    pgid: int | None = None,
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
    state["completionReportWritten"] = bool(
        extra.get("completionReportWritten") if extra is not None else False
    )
    state["completionReportSource"] = (
        extra.get("completionReportSource") if extra is not None else None
    )
    # Default to "ok" only when no extra payload is supplied (e.g. an early
    # running persist). When extra is provided, respect its resultQuality
    # explicitly: a None value means "no result to classify" (e.g. a launch
    # failure that never ran the child), so the key is omitted entirely rather
    # than defaulted to "ok".
    if extra is None:
        state["resultQuality"] = RESULT_QUALITY_OK
    elif "resultQuality" in extra and extra["resultQuality"] is not None:
        state["resultQuality"] = extra["resultQuality"]
    if exit_code is not None:
        state["exitCode"] = exit_code
        state["finishedAt"] = now
    if current:
        state["current"] = redaction.redact_string(current)
    if pid is not None:
        state["pid"] = pid
        if pgid is not None:
            state["pgid"] = pgid
        else:
            with contextlib.suppress(OSError):
                state["pgid"] = os.getpgid(pid)
    if extra is not None:
        state.update(extra)
    if ctx.group is not None:
        state["group"] = ctx.group
    if ctx.include_dirty:
        state["includeDirty"] = True
        state["syncedFiles"] = ctx.synced_files
    # A None resultQuality means "no result to classify" (e.g. a launch failure
    # that never ran the child). Omit the key entirely rather than persist null,
    # so launch-failure state stays consistent with its snapshot.
    if state.get("resultQuality") is None:
        state.pop("resultQuality", None)
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
        "workspaceRoot": str(Path(ctx.execution_cwd).resolve(strict=False)),
        "mode": ctx.mode,
        "model": ctx.model,
        "startedAt": ctx.started_at,
        "current": (
            redaction.redact_string(accumulator.current)
            if accumulator.current
            else accumulator.current
        ),
        "recentEvents": recent_events,
        "completionReportWritten": completion_report_written,
        "completionReportSource": None,
        "resultQuality": RESULT_QUALITY_OK,
        **assistant_meta,
        **events_meta,
    }
    run_metadata.add_run_metadata_payload_fields(snapshot, ctx)
    run_metadata.add_model_payload_fields(snapshot, ctx)
    reasoning.add_reasoning_payload_fields(snapshot, ctx)
    run_metadata.add_speed_payload_fields(snapshot, ctx)
    if ctx.resumable:
        snapshot["resumable"] = True
    if ctx.resumable and accumulator.harness_session_id is not None:
        snapshot["harnessSessionId"] = accumulator.harness_session_id
    snapshot["promptInstructionMode"] = ctx.prompt_instruction_mode
    if ctx.auth_profile is not None:
        snapshot["authProfile"] = ctx.auth_profile
    if ctx.workflow_agent_key is not None:
        snapshot["workflowAgentKey"] = ctx.workflow_agent_key
    if ctx.temporary_workspace_cleanup is not None:
        snapshot["temporaryWorkspaceCleanup"] = ctx.temporary_workspace_cleanup
    cleanup = _worktree_cleanup_commands(ctx)
    if cleanup is not None:
        snapshot["worktreeCleanupCommands"] = cleanup

    if exit_code is not None:
        snapshot["exitCode"] = exit_code
    if accumulator.terminal_event is not None:
        snapshot["terminalEvent"] = accumulator.terminal_event
    if accumulator.terminal_status is not None:
        snapshot["terminalStatus"] = accumulator.terminal_status
    if accumulator.session_id is not None:
        snapshot["sessionId"] = accumulator.session_id
    if ctx.group is not None:
        snapshot["group"] = ctx.group
    if ctx.include_dirty:
        snapshot["includeDirty"] = True
        snapshot["syncedFiles"] = ctx.synced_files
    if completion_report_written:
        report_path = completion_report_path(ctx.run_id)
        snapshot["completionReport"] = {
            "path": report_path,
            "command": run_registry.run_output_command(
                ctx.alias,
                completion_report=True,
                cwd=ctx.source_cwd,
            ),
        }
    if extra is not None:
        _merge_extra(snapshot, extra)
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
    pgid: int | None = None,
    completion_report_written: bool = False,
    extra: JsonObject | None = None,
    lock_timeout_seconds: float | None = None,
) -> None:
    if lock_timeout_seconds is None:
        lock_timeout_seconds = _registry_lock_timeout(ctx)
    with run_registry.registry_lock(
        ctx.registry_root,
        timeout_seconds=lock_timeout_seconds,
    ):
        current = run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
        current_status = current.get("status") if isinstance(current, dict) else None
        if current_status in run_registry.TERMINAL_STATUSES:
            return
        persisted_extra = dict(extra or {})
        if isinstance(current, dict) and current.get("cancelRequested") is True:
            persisted_extra["cancelRequested"] = True
            cancel_requested_at = current.get("cancelRequestedAt")
            if isinstance(cancel_requested_at, str):
                persisted_extra["cancelRequestedAt"] = cancel_requested_at
        persisted_pgid = pgid
        if persisted_pgid is None and isinstance(current, dict):
            current_pgid = current.get("pgid")
            if isinstance(current_pgid, int) and not isinstance(current_pgid, bool):
                persisted_pgid = current_pgid
        if ctx.resumable and accumulator.harness_session_id is not None:
            persisted_extra["harnessSessionId"] = accumulator.harness_session_id
            persisted_extra["resumable"] = True
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
                pgid=persisted_pgid,
                extra=persisted_extra or None,
            ),
        )
        snapshot = build_snapshot(
            ctx,
            accumulator=accumulator,
            exit_code=exit_code,
            completion_report_written=completion_report_written,
            extra=persisted_extra or None,
        )
        snapshot["status"] = status
        write_snapshot(run_path, snapshot)


def _reconcile_cancel_extra(extra: JsonObject) -> None:
    extra["failureReason"] = "cancelled_by_user"
    extra["exitCode"] = 1
    extra.pop("error", None)
    extra.pop("message", None)
    extra.pop("nextActions", None)


def _persist_final_progress(
    run_path: Path,
    ctx: RunContext,
    accumulator: harness_events.StreamAccumulator,
    *,
    status: str,
    exit_code: int,
    stdout_bytes: int,
    stderr_bytes: int,
    completion_report_written: bool,
    extra: JsonObject,
    lock_timeout_seconds: float | None = None,
) -> tuple[str, JsonObject]:
    """Persist terminal state with cancel-precedence reconciliation.

    Acquires the registry lock, re-reads the current persisted state, and if a
    concurrent ``cancel`` already wrote ``cancelled`` (or stamped the
    ``cancelRequested`` marker before signaling), preserves/adopts cancelled
    status and the ``cancelled_by_user`` failure reason instead of downgrading
    to the runner's exit-code-derived status. The runner's work summary/output
    metadata (exit_code, byte counts, completion report, result quality, etc.)
    is still recorded. Returns the status and extra metadata that were actually
    persisted.

    The ``cancelRequested`` marker handles the finalize-first race: cancel
    stamps the marker under the lock BEFORE sending SIGTERM, so if the child
    exits 0 on SIGTERM and the runner finalizes before cancel's post-grace
    terminal write, the finalizer still observes the marker and persists
    cancelled, keeping the live envelope (ok/status/exitCode) consistent with
    the eventual reconciled state.
    """
    lock_timeout_seconds = (
        _registry_lock_timeout(ctx) if lock_timeout_seconds is None else lock_timeout_seconds
    )

    def terminal_payloads(
        current: JsonObject | None,
    ) -> tuple[str, JsonObject, JsonObject, JsonObject]:
        persisted_status = status
        persisted_extra = dict(extra)
        if ctx.resumable and accumulator.harness_session_id is not None:
            persisted_extra["harnessSessionId"] = accumulator.harness_session_id
            persisted_extra["resumable"] = True
        current_status = current.get("status") if isinstance(current, dict) else None
        cancel_requested = isinstance(current, dict) and current.get("cancelRequested") is True
        if current_status == run_registry.STATUS_CANCELLED or cancel_requested:
            # Cancel wins both before and after a finalizer's lock attempt. The
            # replay owner applies the same rule to a WAL published after this
            # read, so a late cancel cannot be downgraded by success.
            persisted_status = run_registry.STATUS_CANCELLED
            _reconcile_cancel_extra(persisted_extra)
        persisted_exit_code = 1 if persisted_status == run_registry.STATUS_CANCELLED else exit_code
        state = build_state(
            ctx,
            status=persisted_status,
            exit_code=persisted_exit_code,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            current=accumulator.current,
            pid=persisted_extra.get("pid"),
            extra=persisted_extra,
        )
        snapshot = build_snapshot(
            ctx,
            accumulator=accumulator,
            exit_code=persisted_exit_code,
            completion_report_written=completion_report_written,
            extra=persisted_extra,
        )
        snapshot["ok"] = run_registry.run_succeeded(persisted_status, snapshot.get("resultQuality"))
        snapshot["status"] = persisted_status
        return persisted_status, persisted_extra, state, snapshot

    try:
        with run_registry.registry_lock(
            ctx.registry_root,
            timeout_seconds=lock_timeout_seconds,
        ):
            persisted_status, persisted_extra, state, snapshot = terminal_payloads(
                run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
            )
            write_state(run_path, state)
            write_snapshot(run_path, snapshot)
        return persisted_status, persisted_extra
    except TimeoutError:
        # The child has completed and its output/logs are durable. Keep the
        # caller's real result while publishing an atomic WAL for the next
        # successful lock holder to fold. Replay re-reads state under the lock,
        # preserving cancel precedence even if cancellation wins this race.
        persisted_status, persisted_extra, state, snapshot = terminal_payloads(
            run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
        )
        run_registry.write_finalize_wal(
            ctx.registry_root,
            ctx.run_id,
            status=persisted_status,
            state=state,
            snapshot=snapshot,
        )
        return persisted_status, persisted_extra


def write_completion_report(run_path: Path, text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    path = run_path / COMPLETION_REPORT_FILE
    run_registry.write_private_text_atomic(path, cleaned + "\n")
    return True


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _aggregate_usage(*usages: JsonObject) -> JsonObject:
    if not usages:
        return {"basis": "unavailable"}
    basis = usages[0].get("basis")
    if basis not in {"exact", "reported"} or any(usage.get("basis") != basis for usage in usages):
        return {"basis": "unavailable"}
    required_keys = ("inputTokens", "outputTokens")
    if not all(is_non_negative_int(usage.get(key)) for usage in usages for key in required_keys):
        return {"basis": "unavailable"}
    result: JsonObject = {
        "basis": basis,
        **{key: sum(int(usage[key]) for usage in usages) for key in required_keys},
    }
    if basis == "reported":
        for key in ("cacheReadTokens", "cacheWriteTokens"):
            result[key] = (
                sum(int(usage[key]) for usage in usages)
                if all(is_non_negative_int(usage.get(key)) for usage in usages)
                else None
            )
    return result


# Delegate to the shared helper in harness_events so the runner (write-time) and
# run-output (read-time) channels emit identical warning text.
_quality_warning = harness_events.quality_warning


def _classify_result_quality(
    *,
    ctx: RunContext,
    exit_code: int,
    report_text: str,
    report_written: bool,
    report_source: str | None,
    accumulator: harness_events.StreamAccumulator,
) -> str:
    if report_text.strip():
        quality = harness_events.assistant_recovery_quality_for_text(report_text)
        if quality == "housekeeping_fallback" and harness_events.is_housekeeping_assistant_text(
            report_text
        ):
            return RESULT_QUALITY_HOUSEKEEPING
        # suspect_short applies ONLY to genuine child reports that are short AND
        # not substantive: a terse but substantive "Verdict:/Status:" report must
        # NOT be flagged, while a preamble-only fragment like "Performing an
        # adversarial review..." must still flag. Delegate-synthesized reports
        # are never suspect (they are structured diagnostics, not child output).
        if (
            ctx.mode == "safe"
            and report_source == COMPLETION_REPORT_SOURCE_CHILD
            and len(report_text.strip()) < 200
            and not harness_events.is_substantive_assistant_text(report_text)
        ):
            return RESULT_QUALITY_SUSPECT_SHORT
        return RESULT_QUALITY_OK
    if (
        exit_code == 0
        and accumulator.structured_events_seen > 0
        and not accumulator.assistant_text.strip()
        and not accumulator.completion_text
    ):
        return RESULT_QUALITY_NO_ASSISTANT_TEXT
    if exit_code == 0 and not report_written:
        return RESULT_QUALITY_EMPTY
    return RESULT_QUALITY_OK


def _failure_details(
    *,
    status: str,
    signal_text: str,
    extra: JsonObject,
) -> child_failures.ChildFailure | None:
    existing = extra.get("failureReason")
    if status not in {run_registry.STATUS_FAILED, run_registry.STATUS_CANCELLED}:
        return None
    if status == run_registry.STATUS_CANCELLED:
        code = existing if isinstance(existing, str) and existing else "harness_cancelled"
        return child_failures.ChildFailure(code, "Run was cancelled before completion.")
    existing_error = extra.get("error")
    if isinstance(existing_error, str) and existing_error:
        message = extra.get("message")
        return child_failures.ChildFailure(
            existing_error,
            message if isinstance(message, str) and message else "Child harness failed.",
        )
    classified = child_failures.classify(signal_text)
    if classified is not None:
        return classified
    return child_failures.ChildFailure(
        "child_failed",
        "Child command failed.",
    )


def _auth_remediation_actions(ctx: RunContext) -> list[str]:
    """Harness-specific auth-remediation next-actions for an auth_failed run."""
    if ctx.harness == "codex" or ctx.engine == "codex":
        return ["delegate profiles", "codex login"]
    return [f"re-authenticate the {ctx.harness} CLI"]


def _auth_remediation_line(ctx: RunContext) -> str:
    """Harness-specific auth-remediation prose for a synthesized report."""
    if ctx.harness == "codex" or ctx.engine == "codex":
        return (
            "Remediation: inspect `delegate profiles`, then refresh Codex auth with `codex login`."
        )
    return f"Remediation: re-authenticate the {ctx.harness} CLI."


def _completion_report_text_and_source(
    ctx: RunContext,
    accumulator: harness_events.StreamAccumulator,
    *,
    completion_report_mode: str,
    status: str,
    failure_reason: str | None,
    failure_message: str | None,
    stderr_tail: str,
) -> tuple[str, str | None]:
    child_text = _completion_report_source(
        ctx,
        accumulator,
        completion_report_mode=completion_report_mode,
    ).strip()
    if status == run_registry.STATUS_CANCELLED:
        # A cancelled run was killed mid-flight: child output is not a valid
        # completion report, so synthesize one regardless of recoverable text.
        reason = (
            failure_reason
            if failure_reason
            in {
                "cancelled_by_user",
                "harness_cancelled",
            }
            else "cancelled_by_user"
        )
        next_actions = [
            run_registry.run_output_command(
                ctx.alias,
                cwd=ctx.source_cwd,
            )
            + " for partial output",
        ]
        lines = [
            "Synthesized by delegate.",
            "",
            f"Status: {status}",
            f"Failure reason: {reason}",
        ]
        terminal = accumulator.terminal_event or {}
        terminal_reason = terminal.get("reason")
        if isinstance(terminal_reason, str) and terminal_reason.strip():
            lines.append(f"Harness error: {redaction.redact_string(terminal_reason.strip())}")
        if stderr_tail.strip():
            lines.extend(["", "Redacted stderr tail:", "```text", stderr_tail.rstrip(), "```"])
        # Whatever the child said before it stopped is still the most useful
        # thing here, and it was being dropped: a harness that cancels itself
        # mid-turn has usually produced real work first, and pointing at
        # run-output instead left "no partial report worth reading". Quoted,
        # never returned as the child's own report, so nothing downstream can
        # mistake a truncated turn for a completed one.
        if child_text:
            lines.extend(
                [
                    "",
                    "Partial output recovered before the run stopped."
                    " This is not a completion report:",
                    "```text",
                    redaction.redact_string(child_text).rstrip(),
                    "```",
                ]
            )
        lines.extend(["", "Next actions:", *(f"- {action}" for action in next_actions)])
        return "\n".join(lines), COMPLETION_REPORT_SOURCE_SYNTHESIZED
    if accumulator.completion_text and child_text:
        return child_text, COMPLETION_REPORT_SOURCE_CHILD
    if status == run_registry.STATUS_FAILED and not accumulator.completion_text:
        next_actions = [
            run_registry.run_output_command(
                ctx.alias,
                cwd=ctx.source_cwd,
            )
            + " --stderr --tail 80",
        ]
        if failure_reason == "auth_failed":
            next_actions = [*_auth_remediation_actions(ctx), *next_actions]
        lines = [
            "Synthesized by delegate.",
            "",
            f"Status: {status}",
            f"Failure reason: {failure_reason or 'child_failed'}",
        ]
        if failure_message:
            lines.append(f"Message: {redaction.redact_string(failure_message)}")
        terminal = accumulator.terminal_event or {}
        terminal_reason = terminal.get("reason")
        if isinstance(terminal_reason, str) and terminal_reason.strip():
            lines.append(f"Harness error: {redaction.redact_string(terminal_reason.strip())}")
        if failure_reason == "auth_failed":
            lines.append(_auth_remediation_line(ctx))
        if stderr_tail.strip():
            lines.extend(["", "Redacted stderr tail:", "```text", stderr_tail.rstrip(), "```"])
        lines.extend(["", "Next actions:", *(f"- {action}" for action in next_actions)])
        return "\n".join(lines), COMPLETION_REPORT_SOURCE_SYNTHESIZED
    if child_text:
        return child_text, COMPLETION_REPORT_SOURCE_STDOUT_RECOVERY
    return "", None


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
    if ctx.include_dirty:
        print(f"syncedFiles: {ctx.synced_files}", file=stdout)
    for warning in ctx.warnings:
        print(f"warning: {warning}", file=stdout)
    if extra is not None:
        error = extra.get("error")
        message = extra.get("message")
        if isinstance(error, str) and error:
            print(f"error: {error}", file=stdout)
        if isinstance(message, str) and message:
            print(f"message: {message}", file=stdout)
        extra_warnings = extra.get("warnings")
        if isinstance(extra_warnings, list):
            for warning in extra_warnings:
                if isinstance(warning, str) and warning not in ctx.warnings:
                    print(f"warning: {warning}", file=stdout)
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
    print(f"snapshot: {run_registry.snapshot_command(ctx.alias, cwd=ctx.source_cwd)}", file=stdout)
    report_written = extra.get("completionReportWritten") if isinstance(extra, dict) else False
    report_source = extra.get("completionReportSource") if isinstance(extra, dict) else None
    if report_written:
        print(
            "completion report: "
            f"{run_registry.run_output_command(ctx.alias, completion_report=True, cwd=ctx.source_cwd)}",
            file=stdout,
        )
        if isinstance(report_source, str):
            print(f"completion report source: {report_source}", file=stdout)
    else:
        print(
            f"diagnostics: {run_registry.run_output_command(ctx.alias, cwd=ctx.source_cwd)}",
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
    assistant_meta: JsonObject | None = None,
    usage: JsonObject | None = None,
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
        "modelAlias": ctx.model_alias,
        "modelResolved": ctx.model_resolved or ctx.model,
        "cwd": ctx.source_cwd,
        "executionCwd": ctx.execution_cwd,
        "workspaceRoot": str(Path(ctx.execution_cwd).resolve(strict=False)),
        "workspaceKind": ctx.workspace_kind,
        "durationMs": duration_ms,
        "snapshotCommand": run_registry.snapshot_command(ctx.alias, cwd=ctx.source_cwd),
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
    }
    if completion_report_written:
        payload["completionReportCommand"] = run_registry.run_output_command(
            ctx.alias,
            completion_report=True,
            cwd=ctx.source_cwd,
        )
        payload["completionReportPath"] = completion_report_path(ctx.run_id)
    payload["completionReportWritten"] = completion_report_written
    payload["completionReportSource"] = (
        extra.get("completionReportSource") if extra is not None else None
    )
    payload["resultQuality"] = (
        extra.get("resultQuality") if extra is not None else RESULT_QUALITY_OK
    )
    if ctx.auth_profile is not None:
        payload["authProfile"] = ctx.auth_profile
    if ctx.fallback_auth_profile is not None:
        payload["fallbackProfile"] = ctx.fallback_auth_profile
    if ctx.group is not None:
        payload["group"] = ctx.group
    if ctx.resumed_from is not None:
        payload["resumedFrom"] = ctx.resumed_from
    if ctx.followup_of is not None:
        payload["followupOf"] = ctx.followup_of
    if ctx.resumable:
        payload["resumable"] = True
    if ctx.include_dirty:
        payload["includeDirty"] = True
        payload["syncedFiles"] = ctx.synced_files
    if ctx.temporary_workspace_cleanup is not None:
        payload["temporaryWorkspaceCleanup"] = ctx.temporary_workspace_cleanup
    payload["promptInstructionMode"] = ctx.prompt_instruction_mode
    run_metadata.add_run_metadata_payload_fields(payload, ctx)
    run_metadata.add_model_payload_fields(payload, ctx)
    reasoning.add_reasoning_payload_fields(payload, ctx)
    run_metadata.add_speed_payload_fields(payload, ctx)
    _add_persona_payload_fields(payload, ctx)
    if assistant_meta is not None:
        payload.update(assistant_meta)
    if usage is not None:
        payload["usage"] = usage

    cleanup = _worktree_cleanup_commands(ctx)
    if cleanup is not None:
        payload["worktreeCleanupCommands"] = cleanup
    if extra is not None:
        _merge_extra(payload, extra)
    account_binding.add_account_fingerprint(
        payload,
        engine=ctx.engine,
        command=ctx.account_binding_command,
        env_overrides=ctx.env_overrides,
    )

    if not ok:
        if status == run_registry.STATUS_CANCELLED and extra is not None:
            payload["error"] = str(extra.get("failureReason") or "cancelled_by_user")
            payload["message"] = "Run was cancelled."
        elif extra is not None and extra.get("commitPolicyCausedFailure") is True:
            payload["error"] = str(extra.get("error") or "commit_policy_violated")
            payload["message"] = str(extra.get("message") or "Commit policy failed.")
        else:
            payload["error"] = str(
                (extra.get("error") if extra is not None else None) or "child_failed"
            )
            payload["message"] = str(
                (extra.get("message") if extra is not None else None)
                or "Child harness failed for an unrecognized reason."
            )
    return payload


def _drain_stream(
    pipe: BinaryIO,
    log_path: Path,
    byte_counter: ByteCounter,
    *,
    on_line: Callable[[str], None] | None,
    max_bytes: int,
    limit_signal: StreamLimitSignal,
    stream: str,
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace") if on_line else None
    with log_path.open("ab") as log_handle:
        captured_bytes = log_handle.tell()
        while True:
            chunk = pipe.readline(STREAM_READ_CHUNK_BYTES)
            if not chunk:
                break
            remaining = max(max_bytes - captured_bytes, 0)
            captured = chunk[:remaining]
            if captured:
                log_handle.write(captured)
                captured_bytes += len(captured)
                byte_counter.total += len(captured)
                if on_line is not None and decoder is not None:
                    decoded = decoder.decode(captured, final=False)
                    if decoded:
                        on_line(decoded)
            if len(captured) < len(chunk):
                limit_signal.trip(stream)
        if on_line is not None and decoder is not None:
            decoded = decoder.decode(b"", final=True)
            if decoded:
                on_line(decoded)


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
    except OSError as exc:
        # The child may have exited or closed stdin before reading the prompt.
        # Record it so the run can report possibly-undelivered prompt text
        # instead of silently proceeding as if delivery succeeded.
        detail = f"stdin prompt delivery may have failed: {exc}"
        if exc.errno == errno.EPIPE:
            # EPIPE means the child was already gone. Read alone this looks
            # like broken prompt plumbing, and operators have chased it as
            # such; the child's own exit code and stderr hold the real cause.
            detail += (
                " (the child closed stdin or exited before reading the prompt;"
                " its exit code and stderr tail carry the actual failure)"
            )
        failures.append(detail)
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _join_stdin_thread(thread: threading.Thread | None, pipe: BinaryIO | None) -> None:
    if thread is not None:
        _join_drain_thread(thread, pipe)


def _mkdtemp_under(prefix: str, temp_base: Path | None) -> Path:
    # Under the bwrap backend /tmp is a private tmpfs, so prompt/schema temp
    # files must live under the run scratch dir (rw-bound) to be child-visible.
    if temp_base is None:
        return Path(tempfile.mkdtemp(prefix=prefix))
    return Path(tempfile.mkdtemp(prefix=prefix, dir=temp_base))


def _materialize_prompt_file_argv(
    argv: list[str],
    *,
    prompt_file_text: str | None,
    prompt_file_placeholder: str | None,
    agent_config_text: str | None = None,
    agent_config_placeholder: str | None = None,
    persona_file_text: str | None = None,
    persona_file_placeholder: str | None = None,
    agent_config_dir: Path | None = None,
    temp_base: Path | None = None,
) -> tuple[list[str], Path | None]:
    if prompt_file_text is None and agent_config_text is None and persona_file_text is None:
        return list(argv), None
    temp_dir: Path | None = None
    replacements: dict[str, str] = {}
    if prompt_file_text is not None:
        if prompt_file_placeholder is None or prompt_file_placeholder not in argv:
            raise ValueError("prompt_file_placeholder must be present in argv")
        temp_dir = _mkdtemp_under("delegate-prompt-", temp_base)
        os.chmod(temp_dir, run_registry.PRIVATE_DIR_MODE)
        prompt_path = temp_dir / "prompt.txt"
        fd = os.open(
            prompt_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            run_registry.PRIVATE_FILE_MODE,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt_file_text)
        replacements[prompt_file_placeholder] = str(prompt_path)
    if agent_config_text is not None:
        if agent_config_placeholder is None or agent_config_placeholder not in argv:
            raise ValueError("agent_config_placeholder must be present in argv")
        if agent_config_dir is None:
            if temp_dir is None:
                temp_dir = _mkdtemp_under("delegate-prompt-", temp_base)
                os.chmod(temp_dir, run_registry.PRIVATE_DIR_MODE)
            agent_config_dir = temp_dir
        path = agent_config_dir / "agent-config.json"
        fd = os.open(
            path,
            os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
            run_registry.PRIVATE_FILE_MODE,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(agent_config_text)
        os.chmod(path, run_registry.PRIVATE_FILE_MODE)
        replacements[agent_config_placeholder] = str(path)
    if persona_file_text is not None:
        if persona_file_placeholder is None or persona_file_placeholder not in argv:
            raise ValueError("persona_file_placeholder must be present in argv")
        if agent_config_dir is None:
            if temp_dir is None:
                temp_dir = _mkdtemp_under("delegate-prompt-", temp_base)
                os.chmod(temp_dir, run_registry.PRIVATE_DIR_MODE)
            persona_path = temp_dir / PERSONA_TXT_FILE
        else:
            run_registry.ensure_private_dir(agent_config_dir)
            persona_path = agent_config_dir / PERSONA_TXT_FILE
        run_registry.write_private_text(persona_path, persona_file_text)
        replacements[persona_file_placeholder] = str(persona_path)
    return [replacements.get(item, item) for item in argv], temp_dir


def _materialize_output_schema_argv(
    argv: list[str],
    *,
    output_schema_text: str | None,
    output_schema_path: str | None,
    destination_dir: Path | None = None,
    temp_base: Path | None = None,
) -> tuple[list[str], Path | None]:
    if output_schema_text is None:
        return list(argv), None
    if output_schema_path is None or output_schema_path not in argv:
        raise ValueError("output_schema_path must be present in argv")
    temp_dir: Path | None = None
    if destination_dir is None:
        temp_dir = _mkdtemp_under("delegate-schema-", temp_base)
        os.chmod(temp_dir, run_registry.PRIVATE_DIR_MODE)
        schema_path = temp_dir / "schema.json"
    else:
        run_registry.ensure_private_dir(destination_dir)
        schema_path = destination_dir / "output-schema.json"
    try:
        run_registry.write_private_text(schema_path, output_schema_text)
    except BaseException:
        _cleanup_prompt_file_dir(temp_dir)
        raise
    return [str(schema_path) if item == output_schema_path else item for item in argv], temp_dir


def _cleanup_prompt_file_dir(temp_dir: Path | None) -> None:
    if temp_dir is not None:
        shutil.rmtree(temp_dir, ignore_errors=True)


@dataclass(frozen=True)
class TrackedRunFiles:
    run_path: Path
    stdout_log: Path
    stderr_log: Path
    scratch_dir: Path | None = None


@dataclass(frozen=True)
class TrackedCaptureResult:
    accumulator: harness_events.StreamAccumulator
    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    stdin_failures: tuple[str, ...]
    pid: int
    pgid: int | None
    mail_push_failure_reason: str | None = None
    error: str | None = None
    message: str | None = None
    output_limit_stream: str | None = None
    output_limit_bytes: int | None = None
    stopped_after_completion: bool = False
    stall: JsonObject | None = None
    process_group_survived: bool = False
    zero_commit_health: JsonObject | None = None


@dataclass(frozen=True)
class CallResult:
    text: str
    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    text_chars: int
    text_truncated: bool
    stderr_tail: str = ""
    warnings: tuple[str, ...] = ()
    error: str | None = None
    message: str | None = None
    model_resolved: str | None = None
    usage: JsonObject = field(default_factory=lambda: {"basis": "unavailable"})
    result_quality: str = RESULT_QUALITY_OK
    empty_retry_attempted: bool = False
    empty_retry_resolved: bool = False
    codex_thread_fallback: JsonObject | None = None


@dataclass(frozen=True)
class TrackedFinalization:
    status: str
    exit_code: int
    report_written: bool
    extra: JsonObject


def _persistent_work_summary(ctx: RunContext) -> JsonObject | None:
    # Attached resume runs share the owner's worktree; their commit accounting
    # keys on the attachment-start HEAD (recorded in creation_context by the
    # attach executor), not the owner's creation base.
    if ctx.isolation_lifecycle not in ("persistent", "attached"):
        return None
    return worktree_summary.build_work_summary(
        source_git_root=ctx.source_git_root,
        execution_cwd=ctx.execution_cwd,
        branch=ctx.branch,
        creation_context=ctx.creation_context,
    )


def _source_commits_missed(summary: JsonObject | None) -> int | None:
    """Commits on the source branch that this worktree's base predates."""
    if not isinstance(summary, dict):
        return None
    pair = summary.get("branchAheadOfSource")
    if not isinstance(pair, dict):
        return None
    behind = pair.get("behind")
    return behind if isinstance(behind, int) and behind > 0 else None


def _final_extra(ctx: RunContext, capture_exit_code: int) -> tuple[int, JsonObject]:
    extra: JsonObject = {}
    summary = _persistent_work_summary(ctx)
    if summary is not None:
        extra["workSummary"] = summary
        if summary.get("noChanges") is True:
            warnings = list(extra.get("warnings") or [])
            _append_unique(
                warnings,
                "Work-mode run completed with no file changes or commits detected.",
            )
            extra["warnings"] = warnings
    # A worktree snapshots its base at launch, so anything landing on the source
    # afterwards is invisible to the child. That is usually fine and occasionally
    # ruinous: a reviewer lane dispatched before a contract fix reviews the tree
    # without it and returns a confident verdict about a document that no longer
    # exists in that form -- which is what happened to a W1 gate review, caught
    # only because a human noticed and relaunched it.
    #
    # There is nothing to warn about at dispatch, because at dispatch there is no
    # drift; it accrues while the child runs. Completion is therefore the
    # earliest honest moment, and it is also the moment the verdict is about to
    # be trusted. The number already existed in the summary and nothing surfaced
    # it.
    behind = _source_commits_missed(summary)
    if behind:
        warnings = list(extra.get("warnings") or [])
        _append_unique(
            warnings,
            f"Worktree base is {behind} commit(s) behind the source branch: "
            "the child never saw work that landed after it was dispatched. "
            "Re-run against a current base if this run's conclusions depend on it.",
        )
        extra["warnings"] = warnings

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
        # Attached resume runs have no worktree record of their own; point
        # inspection commands at the owning run's alias.
        attachment = ctx.worktree_attachment or {}
        show_handle = (
            attachment.get("sourceAlias") if ctx.isolation_lifecycle == "attached" else None
        )
        extra["nextActions"] = [
            f"delegate worktree show {show_handle or ctx.alias}",
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
            if capture_exit_code == 0:
                extra["error"] = "commit_policy_unverified"
                extra["message"] = (
                    "Delegate could not verify --forbid-commit because final Git inspection failed."
                )
                extra["commitPolicyCausedFailure"] = True
                return 1, extra
        if violated:
            extra["commitPolicyViolated"] = True
            extra["childExitCode"] = capture_exit_code
            if capture_exit_code == 0:
                extra["error"] = "commit_policy_violated"
                extra["message"] = (
                    "Child command created commits even though --forbid-commit was set."
                )
                extra["commitPolicyCausedFailure"] = True
                return 1, extra
    return capture_exit_code, extra


@dataclass
class ByteCounter:
    total: int = 0


@dataclass
class StreamLimitSignal:
    event: threading.Event = field(default_factory=threading.Event)
    stream: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def trip(self, stream: str) -> None:
        with self._lock:
            if self.stream is None:
                self.stream = stream
            self.event.set()


def _stall_message(detail: JsonObject) -> str:
    idle = detail.get("idleSeconds")
    threshold = detail.get("thresholdSeconds")
    idle_text = f"{float(idle):.0f}s" if isinstance(idle, (int, float)) else "the stall window"
    threshold_text = (
        f"{float(threshold) / 60:.0f} min" if isinstance(threshold, (int, float)) else "the limit"
    )
    return (
        f"Child produced no new output and no tool activity for {idle_text} "
        f"(stall threshold {threshold_text}); the run was cancelled by the stall watchdog."
    )


def _stall_seconds_from_env(default: float) -> float:
    """Operator override for the configured stall threshold, in minutes.

    Mirrors the progress-heartbeat env overrides: an unparseable or negative
    value falls back to the configured threshold rather than silently disabling
    the watchdog. ``0`` is honoured, because disabling is a real intent.
    """
    raw = os.environ.get(STALL_MINUTES_ENV)
    if raw is None:
        return default
    try:
        minutes = float(raw)
    except ValueError:
        return default
    if not math.isfinite(minutes) or minutes < 0:
        return default
    return stall_watchdog.stall_seconds_from_minutes(minutes)


def _zero_commit_budget_fraction(default: float = ZERO_COMMIT_BUDGET_FRACTION_DEFAULT) -> float:
    """Resolve the advisory zero-commit checkpoint as a budget fraction.

    A persistent worktree lane doing real work normally checkpoints commits
    incrementally.  Reaching the checkpoint with no commits is therefore a
    useful health signal, but not a failure: reviewers may intentionally leave
    a read-only lane uncommitted.  ``0`` disables the signal; malformed,
    negative, or greater-than-one overrides fall back to the default.
    """
    raw = os.environ.get(ZERO_COMMIT_BUDGET_FRACTION_ENV)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0 or value > 1:
        return default
    return value


def _zero_commit_health_detail(
    ctx: RunContext,
    *,
    elapsed_seconds: float,
    budget_seconds: float,
    threshold_fraction: float | None = None,
) -> JsonObject | None:
    """Return the zero-commit health flag once a lane crosses its checkpoint.

    Commit accounting is deliberately restricted to persistent/attached
    worktree contexts through ``_persistent_work_summary``.  If Git metadata
    cannot be verified, the advisory stays silent rather than guessing from
    the source checkout or treating an unobservable lane as unhealthy.
    """
    if not math.isfinite(elapsed_seconds) or not math.isfinite(budget_seconds):
        return None
    if budget_seconds <= 0:
        return None
    fraction = _zero_commit_budget_fraction() if threshold_fraction is None else threshold_fraction
    if fraction <= 0 or elapsed_seconds < budget_seconds * fraction:
        return None
    summary = _persistent_work_summary(ctx)
    commits_created = worktree_summary.commits_created_count(summary)
    if commits_created != 0:
        return None
    return {
        "branch": ctx.branch,
        "budgetSeconds": round(budget_seconds, 3),
        "elapsedSeconds": round(max(elapsed_seconds, 0.0), 3),
        "thresholdFraction": round(fraction, 3),
        "commitsCreatedCount": 0,
    }


def _zero_commit_health_warning(detail: JsonObject) -> str:
    branch = detail.get("branch")
    branch_text = branch if isinstance(branch, str) and branch else "the lane branch"
    elapsed = detail.get("elapsedSeconds")
    budget = detail.get("budgetSeconds")
    fraction = detail.get("thresholdFraction")
    if isinstance(elapsed, (int, float)) and isinstance(budget, (int, float)):
        timing = f"after {float(elapsed):.0f}s of its {float(budget):.0f}s budget"
    else:
        timing = "at its budget checkpoint"
    fraction_text = (
        f" ({float(fraction):.0%} checkpoint)" if isinstance(fraction, (int, float)) else ""
    )
    return (
        "zero_commits_at_half_budget: "
        f"{branch_text} has produced zero commits {timing}{fraction_text}; "
        "lane health warning only (the run continues)."
    )


def _progress_interval_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0:
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
        f"snapshot={run_registry.snapshot_command(ctx.alias, cwd=ctx.source_cwd)!r}",
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
    scratch_dir: Path | None = None
    if ctx.mode == "safe" or ctx.effective_isolation != "none":
        scratch_dir = run_path / "scratch"
        run_registry.ensure_private_dir(scratch_dir)
    if ctx.source_prompt is not None:
        # The user prompt exactly as resolved, before instruction framing, so
        # `delegate resume` can rebuild the original task text. Verbatim by
        # design; deleted only by `runs prune` (never archived).
        run_registry.write_private_text(run_path / PROMPT_TXT_FILE, ctx.source_prompt)
    if ctx.persona_text is not None:
        run_registry.write_private_text(run_path / PERSONA_TXT_FILE, ctx.persona_text)
    write_manifest(run_path, build_manifest(ctx, manifest_argv or argv))

    stdout_log = run_path / STDOUT_LOG
    stderr_log = run_path / STDERR_LOG
    run_registry.write_private_bytes(stdout_log, b"")
    run_registry.write_private_bytes(stderr_log, b"")
    return TrackedRunFiles(
        run_path=run_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        scratch_dir=scratch_dir,
    )


def _env_overrides_with_scratch(
    env_overrides: dict[str, str] | None,
    scratch_dir: Path | None,
) -> dict[str, str] | None:
    if scratch_dir is None:
        return env_overrides
    return {
        **(env_overrides or {}),
        "TMPDIR": str(scratch_dir),
        "TMP": str(scratch_dir),
        "TEMP": str(scratch_dir),
    }


def _codex_argv_with_scratch(argv: list[str], scratch_dir: Path | None) -> list[str]:
    if scratch_dir is None or "--sandbox" not in argv:
        return list(argv)
    sandbox_index = argv.index("--sandbox")
    if sandbox_index + 1 >= len(argv) or argv[sandbox_index + 1] != "read-only":
        return list(argv)
    updated = list(argv)
    insert_at = max(len(updated) - 1, 0)
    updated[insert_at:insert_at] = ["--add-dir", str(scratch_dir)]
    return updated


def _masks_from_sandbox(payload: JsonObject | None) -> tuple[sandbox_bwrap.Mask, ...]:
    if not payload:
        return ()
    entries = payload.get("masks")
    if not isinstance(entries, list):
        return ()
    masks: list[sandbox_bwrap.Mask] = []
    for entry in entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("kind"), str)
        ):
            masks.append(sandbox_bwrap.Mask(path=entry["path"], kind=entry["kind"]))
    return tuple(masks)


def _binds_from_sandbox(payload: JsonObject | None, mode: str) -> list[str]:
    if not payload:
        return []
    entries = payload.get("binds")
    if not isinstance(entries, list):
        return []
    return [
        entry["path"]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("mode") == mode
        and isinstance(entry.get("path"), str)
    ]


def _bwrap_mail_push_rw_roots(ctx: RunContext) -> list[str]:
    """Mail-push private homes must stay writable inside the bwrap boundary."""
    if not ctx.mail_push:
        return []
    run_path = run_registry.run_directory(ctx.registry_root, ctx.run_id)
    return [
        str(run_path / name)
        for name in (
            mail_push.MAIL_PUSH_CODEX_HOME_NAME,
            mail_push.MAIL_PUSH_FALLBACK_CODEX_HOME_NAME,
        )
        if (run_path / name).is_dir()
    ]


def _launch_tracked_process(
    argv: list[str],
    cwd: str,
    *,
    stdin_text: str | None,
    env_overrides: dict[str, str] | None = None,
    drop_env: tuple[str, ...] = (),
    scratch_dir: Path | None = None,
    sandbox: JsonObject | None = None,
    engine: str = "",
    extra_rw_roots: list[str] | None = None,
) -> subprocess.Popen[bytes]:
    env = profiles.child_environment(
        overrides=_env_overrides_with_scratch(env_overrides, scratch_dir)
    )
    for key in drop_env:
        env.pop(key, None)
    if sandbox:
        # Child env is final here (CODEX_HOME / mail-push homes / TMPDIR all
        # resolved), mirroring where the codex-pure seatbelt prefix is applied.
        # Boundary construction and the preflight of the final plan raise
        # DelegateError; the caller records them as launch failures.
        argv = sandbox_bwrap.wrap_engine_argv(
            engine_argv=argv,
            cwd=cwd,
            env=env,
            engine=engine,
            scratch_dir=str(scratch_dir) if scratch_dir is not None else None,
            masks=_masks_from_sandbox(sandbox),
            extra_rw_roots=[*_binds_from_sandbox(sandbox, "rw"), *(extra_rw_roots or [])],
            extra_ro_roots=_binds_from_sandbox(sandbox, "ro"),
            bwrap_path=sandbox.get("bwrapPath")
            if isinstance(sandbox.get("bwrapPath"), str)
            else None,
        )
        sandbox_bwrap.preflight_plan(argv)
    return subprocess.Popen(  # nosec B603 - Delegate intentionally launches validated harness argv with shell=False.
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )


def _runner_launch_error(argv: list[str], cwd: str, exc: OSError) -> RunnerLaunchError:
    binary = argv[0] if argv else "<empty argv>"
    message = f"Failed to launch child command {binary!r} in {cwd}: {exc}"
    if exc.errno == errno.EPERM:
        message += (
            " Sandboxed parent shells can forbid launching this harness binary; "
            "retry the launch from an unsandboxed shell."
        )
    return RunnerLaunchError("child_launch_failed", message)


def _record_tracked_launch_failure(
    files: TrackedRunFiles,
    ctx: RunContext,
    error: RunnerLaunchError,
    *,
    prior_capture: TrackedCaptureResult | None = None,
) -> None:
    # A launch failure never ran the child, so there is no result to classify.
    # resultQuality is set to None explicitly so build_state omits the key,
    # keeping state consistent with the snapshot below (which also omits it).
    extra: JsonObject = {
        "error": error.error,
        "message": error.message,
        "resultQuality": None,
    }
    recorded = False
    accumulator = (
        prior_capture.accumulator
        if prior_capture is not None
        else harness_events.StreamAccumulator(harness=ctx.harness)
    )
    with _launch_registry_lock(ctx):
        current = run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
        current_status = current.get("status") if isinstance(current, dict) else None
        cancel_requested = isinstance(current, dict) and current.get("cancelRequested") is True
        if current_status in run_registry.TERMINAL_STATUSES or cancel_requested:
            # A concurrent cancel or terminal finalizer owns the outcome. In
            # particular, a fallback launch failure after cancel must not
            # replace cancelled with child_launch_failed.
            return
        recorded = True
        write_state(
            files.run_path,
            build_state(
                ctx,
                status="failed",
                exit_code=1,
                stdout_bytes=prior_capture.stdout_bytes if prior_capture is not None else 0,
                stderr_bytes=prior_capture.stderr_bytes if prior_capture is not None else 0,
                current=accumulator.current,
                extra=extra,
            ),
        )
        snapshot = build_snapshot(ctx, accumulator=accumulator, exit_code=1)
        snapshot["ok"] = False
        snapshot["status"] = "failed"
        # The child never ran, so there is no result quality to report. Remove the
        # snapshot's default "ok" verdict so launch-failure state and snapshot agree.
        snapshot.pop("resultQuality", None)
        snapshot.update({"error": error.error, "message": error.message})
        write_snapshot(files.run_path, snapshot)
    if recorded:
        # A launch failure is a terminal state too; the --notify ping fires
        # outside the registry lock so a slow post cannot hold it.
        _send_completion_notification(files.run_path, ctx, "failed")


def _mail_push_failure_nonce(ctx: RunContext) -> str | None:
    if not ctx.mail_push:
        return None
    try:
        from delegate_agent import mail

        return mail.read_hook_failure_nonce(ctx.registry_root, ctx.run_id)
    except (OSError, ValueError):
        return None


def _capture_mail_push_failure_sentinel(
    ctx: RunContext,
    chunk_text: str,
    *,
    nonce: str | None,
    current_reason: str | None,
) -> str | None:
    if current_reason is not None or not ctx.mail_push:
        return current_reason
    from delegate_agent import mail

    return mail.hook_failure_reason_from_stderr(chunk_text, nonce=nonce)


def _capture_tracked_process(
    process: subprocess.Popen[bytes],
    files: TrackedRunFiles,
    ctx: RunContext,
    *,
    started: float,
    stdin_text: str | None,
    deadline: float | None = None,
    progress_stderr: TextIO | None = None,
    progress_initial_delay_sec: float = PROGRESS_INITIAL_DELAY_SEC,
    progress_interval_sec: float = PROGRESS_HEARTBEAT_INTERVAL_SEC,
    process_group_pgid: int | None = None,
    process_group_grace_seconds: float = PROCESS_GROUP_TERMINATION_GRACE_SEC,
) -> TrackedCaptureResult:
    accumulator = harness_events.StreamAccumulator(harness=ctx.harness)
    watchdog = stall_watchdog.StallWatchdog(
        stall_seconds=_stall_seconds_from_env(ctx.stall_seconds),
        harness=ctx.harness,
    )
    pgid = process_group_pgid or _process_group_for_process(process)
    persist_progress(
        files.run_path,
        ctx,
        accumulator,
        status="running",
        pid=process.pid,
        pgid=pgid,
    )

    line_buffer = ""
    stdout_bytes_counter = ByteCounter()
    stderr_bytes_counter = ByteCounter()
    lines_since_persist = 0
    last_persist_at = time.monotonic()
    progress_dirty = False
    zero_commit_health: JsonObject | None = None
    zero_commit_health_warning: str | None = None
    zero_commit_running_extra: JsonObject = {}
    zero_commit_fraction = _zero_commit_budget_fraction()
    zero_commit_budget_seconds: float | None = None
    zero_commit_checkpoint_at: float | None = None
    if deadline is not None:
        budget_seconds = deadline - started
        if math.isfinite(budget_seconds) and budget_seconds > 0 and zero_commit_fraction > 0:
            zero_commit_budget_seconds = budget_seconds
            zero_commit_checkpoint_at = started + budget_seconds * zero_commit_fraction
    mail_push_failure_reason: str | None = None
    mail_push_nonce = _mail_push_failure_nonce(ctx)
    terminal_signal = threading.Event()
    limit_signal = StreamLimitSignal()
    stdout_line_events_written, stdout_line_events_truncated = _stream_line_event_state(
        files.run_path / EVENTS_JSONL
    )

    def maybe_persist_running() -> None:
        nonlocal lines_since_persist, last_persist_at, progress_dirty
        if not progress_dirty:
            return
        try:
            persist_progress(
                files.run_path,
                ctx,
                accumulator,
                status="running",
                pid=process.pid,
                stdout_bytes=stdout_bytes_counter.total,
                stderr_bytes=stderr_bytes_counter.total,
                extra=zero_commit_running_extra or None,
                lock_timeout_seconds=0,
            )
        except TimeoutError:
            return
        progress_dirty = False
        lines_since_persist = 0
        last_persist_at = time.monotonic()

    if process.stdout is None or process.stderr is None:
        raise RunnerLaunchError(
            "missing_child_stream",
            "Child process did not expose stdout/stderr pipes for tracking.",
        )
    with open_events_log(files.run_path) as events_handle:

        def append_stdout_line_event(line: str) -> bool:
            nonlocal stdout_line_events_written, stdout_line_events_truncated
            if stdout_line_events_written < harness_events.EVENT_LIMIT:
                append_event(events_handle, _stream_line_event("stdout", line))
                stdout_line_events_written += 1
                return True
            if not stdout_line_events_truncated:
                append_event(
                    events_handle,
                    {
                        "kind": "stream.lines_truncated",
                        "stream": "stdout",
                        "limit": harness_events.EVENT_LIMIT,
                    },
                )
                stdout_line_events_truncated = True
            return False

        def handle_stdout_line(chunk_text: str) -> None:
            nonlocal line_buffer, lines_since_persist, last_persist_at, progress_dirty
            line_buffer += chunk_text
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                prior_session_id = accumulator.session_id
                accumulator.ingest_line(line)
                watchdog.observe_line(line, now=time.monotonic())
                if accumulator.terminal_status is not None:
                    terminal_signal.set()
                progress_dirty = True
                if append_stdout_line_event(line):
                    lines_since_persist += 1
                elapsed = time.monotonic() - last_persist_at
                if (
                    lines_since_persist >= PROGRESS_PERSIST_LINE_INTERVAL
                    or elapsed >= PROGRESS_PERSIST_TIME_INTERVAL_SEC
                    or accumulator.session_id != prior_session_id
                ):
                    events_handle.flush()
                    maybe_persist_running()

        def handle_stderr_line(chunk_text: str) -> None:
            nonlocal mail_push_failure_reason
            mail_push_failure_reason = _capture_mail_push_failure_sentinel(
                ctx,
                chunk_text,
                nonce=mail_push_nonce,
                current_reason=mail_push_failure_reason,
            )

        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, files.stdout_log, stdout_bytes_counter),
            kwargs={
                "on_line": handle_stdout_line,
                "max_bytes": TRACKED_STREAM_MAX_BYTES,
                "limit_signal": limit_signal,
                "stream": "stdout",
            },
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, files.stderr_log, stderr_bytes_counter),
            kwargs={
                "on_line": handle_stderr_line,
                "max_bytes": TRACKED_STREAM_MAX_BYTES,
                "limit_signal": limit_signal,
                "stream": "stderr",
            },
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

        timed_out = False
        output_limited = False
        stopped_after_completion = False
        emit_progress = progress_stderr is not None
        if emit_progress:
            try:
                assert progress_stderr is not None
                _emit_progress_started(ctx, progress_stderr)
            except OSError:
                emit_progress = False
        initial_delay = _progress_interval_from_env(
            PROGRESS_INITIAL_DELAY_ENV,
            progress_initial_delay_sec,
        )
        interval = _progress_interval_from_env(
            PROGRESS_INTERVAL_ENV,
            progress_interval_sec,
        )
        next_progress_at = time.monotonic() + initial_delay
        terminal_seen_at: float | None = None
        stall_detail: JsonObject | None = None
        while True:
            now = time.monotonic()
            if (
                zero_commit_health is None
                and zero_commit_checkpoint_at is not None
                and zero_commit_budget_seconds is not None
                and now >= zero_commit_checkpoint_at
            ):
                detail = _zero_commit_health_detail(
                    ctx,
                    elapsed_seconds=now - started,
                    budget_seconds=zero_commit_budget_seconds,
                    threshold_fraction=zero_commit_fraction,
                )
                if detail is not None:
                    zero_commit_health = detail
                    zero_commit_health_warning = _zero_commit_health_warning(detail)
                    zero_commit_running_extra = {
                        "zeroCommitHealth": detail,
                        "warnings": [zero_commit_health_warning],
                    }
                    append_event(
                        events_handle,
                        {"kind": ZERO_COMMIT_HEALTH_EVENT_KIND, **detail},
                    )
                    events_handle.flush()
                    # The event and final state still carry the signal; a
                    # contended registry must not change child execution.
                    with contextlib.suppress(TimeoutError):
                        persist_progress(
                            files.run_path,
                            ctx,
                            accumulator,
                            status="running",
                            pid=process.pid,
                            stdout_bytes=stdout_bytes_counter.total,
                            stderr_bytes=stderr_bytes_counter.total,
                            extra=zero_commit_running_extra,
                            lock_timeout_seconds=0,
                        )
            if limit_signal.event.is_set():
                _terminate_call_process(
                    process,
                    pgid=pgid,
                    grace_seconds=process_group_grace_seconds,
                    identity_ctx=ctx,
                )
                output_limited = True
                exit_code = 1
                break
            if terminal_signal.is_set():
                terminal_seen_at = terminal_seen_at or now
                if now - terminal_seen_at >= TERMINAL_EXIT_GRACE_SEC:
                    if process.poll() is None:
                        _terminate_call_process(
                            process,
                            pgid=pgid,
                            grace_seconds=process_group_grace_seconds,
                            identity_ctx=ctx,
                        )
                        stopped_after_completion = True
                    exit_code = 0 if accumulator.terminal_status == "succeeded" else 1
                    break
            if deadline is not None and now >= deadline and not terminal_signal.is_set():
                _terminate_call_process(
                    process,
                    pgid=pgid,
                    grace_seconds=process_group_grace_seconds,
                    identity_ctx=ctx,
                )
                timed_out = True
                exit_code = 1
                break
            if not terminal_signal.is_set():
                # A child that is alive and emitting, but has produced no new
                # content and no tool activity for the configured window, is
                # cancelled the way `delegate cancel` cancels: SIGTERM to the
                # process group, then SIGKILL. The run finalizes as failed with
                # failureReason "stalled" so a workflow's retry/park logic runs.
                idle_seconds = watchdog.stalled_for(now)
                if idle_seconds is not None:
                    stall_detail = watchdog.stall_detail(idle_seconds)
                    _terminate_call_process(
                        process,
                        pgid=pgid,
                        grace_seconds=process_group_grace_seconds,
                        identity_ctx=ctx,
                    )
                    exit_code = 1
                    break
            return_code = process.poll()
            if return_code is not None:
                exit_code = return_code
                break
            if emit_progress and now >= next_progress_at:
                assert progress_stderr is not None
                try:
                    _emit_progress_heartbeat(
                        ctx,
                        accumulator,
                        started=started,
                        stderr=progress_stderr,
                    )
                except OSError:
                    emit_progress = False
                finally:
                    next_progress_at = time.monotonic() + interval
            wait_for = TRACKED_PROCESS_POLL_SEC
            if deadline is not None:
                wait_for = min(wait_for, max(deadline - now, 0.01))
            if terminal_seen_at is not None:
                wait_for = min(
                    wait_for,
                    max(TERMINAL_EXIT_GRACE_SEC - (now - terminal_seen_at), 0.01),
                )
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=wait_for)
        # Reap the whole group before joining drain threads. A grandchild that
        # inherited stdout/stderr can keep those pipes open after the leader
        # exits; waiting for the drains first would otherwise delay cleanup
        # until the daemon's natural exit.
        _terminate_call_process(
            process,
            pgid=pgid,
            grace_seconds=process_group_grace_seconds,
            identity_ctx=ctx,
        )
        _cleanup_tracked_process_streams(
            process,
            stdin_thread=stdin_thread,
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
        )
        if limit_signal.event.is_set():
            output_limited = True
            exit_code = 1
        if not output_limited and line_buffer.strip():
            # Final unterminated stdout line: ingest and mirror into events.jsonl
            # so the raw event log matches what the accumulator saw.
            accumulator.ingest_line(line_buffer)
            append_stdout_line_event(line_buffer)
        error: str | None = None
        message: str | None = None
        if stall_detail is not None:
            # Written here rather than from the poll loop: the stdout drain
            # thread shares this handle and has only just been joined.
            append_event(events_handle, {"kind": "run.stalled", **stall_detail})
            error = "stalled"
            message = _stall_message(stall_detail)
        elif timed_out:
            error = "call_timeout"
            message = "Child command exceeded the configured timeout."
        elif output_limited:
            error = "output_limit_exceeded"
            message = (
                f"Child {limit_signal.stream or 'output'} exceeded the tracked output limit "
                f"of {TRACKED_STREAM_MAX_BYTES} bytes."
            )
    return TrackedCaptureResult(
        accumulator=accumulator,
        exit_code=exit_code,
        duration_ms=int((time.monotonic() - started) * MILLISECONDS_PER_SECOND),
        stdout_bytes=stdout_bytes_counter.total,
        stderr_bytes=stderr_bytes_counter.total,
        stdin_failures=tuple(stdin_failures),
        pid=process.pid,
        pgid=pgid,
        mail_push_failure_reason=mail_push_failure_reason,
        error=error,
        message=message,
        output_limit_stream=limit_signal.stream if output_limited else None,
        output_limit_bytes=TRACKED_STREAM_MAX_BYTES if output_limited else None,
        stopped_after_completion=stopped_after_completion,
        stall=stall_detail,
        zero_commit_health=zero_commit_health,
    )


def _cleanup_tracked_process_streams(
    process: subprocess.Popen[bytes],
    *,
    stdin_thread: threading.Thread | None,
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
) -> None:
    _join_stdin_thread(stdin_thread, process.stdin)
    _join_drain_thread(stdout_thread, process.stdout)
    _join_drain_thread(stderr_thread, process.stderr)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()


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


MAIL_PUSH_EVENT_KIND = mail.MAIL_PUSH_EVENT_KIND
MAIL_PUSH_WARNING_PREFIX = mail.MAIL_PUSH_WARNING_PREFIX
MAIL_PUSH_CLEANUP_WARNING = "mail push private-home cleanup failed"


def _mail_push_warnings(ctx: RunContext) -> list[str]:
    return [
        warning
        for warning in ctx.warnings
        if isinstance(warning, str) and warning.startswith(MAIL_PUSH_WARNING_PREFIX)
    ]


def _cleanup_mail_push_private_homes(ctx: RunContext) -> str | None:
    if not ctx.mail_push:
        return None
    try:
        from delegate_agent import mail

        mail.cleanup_mail_push_private_homes(ctx.registry_root, ctx.run_id)
    except OSError:
        return MAIL_PUSH_CLEANUP_WARNING
    return None


def _cleanup_unfinished_mail_push_private_homes(ctx: RunContext) -> None:
    """Release runner-owned credentials when setup never reaches finalization."""
    cleanup_warning = _cleanup_mail_push_private_homes(ctx)
    if cleanup_warning is not None:
        with contextlib.suppress(DelegateError, OSError):
            record_mail_push_degradation(
                ctx.registry_root,
                ctx.run_id,
                engine=ctx.engine,
                reason=cleanup_warning,
            )


def record_mail_push_degradation(
    registry_root: Path,
    run_id: str,
    *,
    engine: str,
    reason: str,
) -> str:
    """Persist one hook degradation event and project it into the snapshot."""
    warning = f"{MAIL_PUSH_WARNING_PREFIX} for {engine}: {reason[:200]}."
    run_path = run_registry.run_directory(registry_root, run_id)
    with run_registry.registry_lock(registry_root):
        state = run_registry.load_run_state_or_none(registry_root, run_id) or {}
        if state.get("mailPushDegraded") is True:
            existing = state.get("mailPushWarning")
            return existing if isinstance(existing, str) else warning
        state = dict(state)
        state["mailPushDegraded"] = True
        state["mailPushWarning"] = warning
        warnings = [item for item in state.get("warnings", []) if isinstance(item, str)]
        if warning not in warnings:
            warnings.append(warning)
        state["warnings"] = warnings
        run_registry.write_json_atomic(run_path / STATE_FILE, state)
        event = {"kind": MAIL_PUSH_EVENT_KIND, "message": warning}
        with open_events_log(run_path) as handle:
            append_event(handle, event)
            handle.flush()
        snapshot = run_registry.load_run_snapshot_or_none(registry_root, run_id)
        if isinstance(snapshot, dict):
            snapshot = dict(snapshot)
            snapshot_warnings = [
                item for item in snapshot.get("warnings", []) if isinstance(item, str)
            ]
            if warning not in snapshot_warnings:
                snapshot_warnings.append(warning)
            snapshot["warnings"] = snapshot_warnings
            recent_events = snapshot.get("recentEvents")
            if not isinstance(recent_events, list):
                recent_events = []
            recent_events = [item for item in recent_events if isinstance(item, dict)]
            total = snapshot.get("eventsTotal")
            recent_events.append(event)
            snapshot["recentEvents"] = recent_events[-harness_events.EVENT_LIMIT :]
            snapshot["eventsTotal"] = total + 1 if isinstance(total, int) else len(recent_events)
            snapshot["eventsTruncated"] = snapshot["eventsTotal"] > harness_events.EVENT_LIMIT
            snapshot["eventsLimit"] = harness_events.EVENT_LIMIT
            run_registry.write_snapshot(run_path, snapshot)
    return warning


def _mail_push_failure_marker(ctx: RunContext, sentinel_reason: str | None = None) -> str | None:
    if not ctx.mail_push:
        return None
    try:
        from delegate_agent import mail

        return mail.read_hook_failure_marker(ctx.registry_root, ctx.run_id) or sentinel_reason
    except (DelegateError, OSError, ValueError):
        return "hook failure marker could not be read"


def _append_mail_push_event(accumulator: harness_events.StreamAccumulator, warning: str) -> None:
    if any(
        event.kind == MAIL_PUSH_EVENT_KIND and event.message == warning
        for event in accumulator.events
    ):
        return
    accumulator.events.append(
        harness_events.NormalizedEvent(kind=MAIL_PUSH_EVENT_KIND, message=warning)
    )


def _finalize_mail_push_state(
    files: TrackedRunFiles,
    ctx: RunContext,
    capture: TrackedCaptureResult,
) -> tuple[list[str], str]:
    mail_warnings = _mail_push_warnings(ctx)
    cleanup_warning = _cleanup_mail_push_private_homes(ctx)
    if cleanup_warning is not None:
        mail_warnings.append(cleanup_warning)
    stderr_tail = profiles.read_bounded_stderr_tail(files.stderr_log)
    marker_reason = _mail_push_failure_marker(ctx, capture.mail_push_failure_reason)
    if marker_reason is not None:
        try:
            mail_warnings.append(
                record_mail_push_degradation(
                    ctx.registry_root,
                    ctx.run_id,
                    engine=ctx.engine,
                    reason=marker_reason,
                )
            )
        except (OSError, DelegateError):
            mail_warnings.append(
                f"{MAIL_PUSH_WARNING_PREFIX} for {ctx.engine}: event recording failed."
            )
    return mail_warnings, stderr_tail


def _finalize_tracked_run(
    files: TrackedRunFiles,
    ctx: RunContext,
    capture: TrackedCaptureResult,
    *,
    completion_report_mode: str,
    extra: JsonObject | None = None,
) -> TrackedFinalization:
    mail_warnings, stderr_tail = _finalize_mail_push_state(files, ctx, capture)
    for warning in dict.fromkeys(mail_warnings):
        _append_mail_push_event(capture.accumulator, warning)
    exit_code, merged_extra = _final_extra(ctx, capture.exit_code)
    if extra:
        merged_extra = {**merged_extra, **extra}
    if mail_warnings:
        warnings = list(merged_extra.get("warnings") or [])
        for warning in mail_warnings:
            _append_unique(warnings, warning)
        merged_extra["warnings"] = warnings
        merged_extra["mailPushDegraded"] = True
        merged_extra["mailPushWarning"] = mail_warnings[0]
    merged_extra = {
        **merged_extra,
        "pid": capture.pid,
        "processGroupTerminationGraceSec": _process_group_grace_seconds(ctx),
    }
    if capture.pgid is not None:
        merged_extra["pgid"] = capture.pgid
    if capture.process_group_survived:
        merged_extra["processGroupSurvived"] = True
    terminal_extra = _terminal_override_extra(capture.accumulator)
    if terminal_extra:
        merged_extra = {**merged_extra, **terminal_extra}
    status = status_from_exit(exit_code)
    if capture.accumulator.terminal_status in {
        run_registry.STATUS_FAILED,
        run_registry.STATUS_CANCELLED,
    }:
        status = capture.accumulator.terminal_status
        if status == run_registry.STATUS_CANCELLED or exit_code == 0:
            exit_code = 1
    # Marker protocol (finalize-first race): cancel stamps cancelRequested under
    # the registry lock BEFORE signaling. If the child exits 0 on SIGTERM and
    # the runner finalizes before cancel's post-grace terminal write, the
    # finalizer observes the marker here and treats the run as cancelled for
    # both report synthesis and the persisted finalization. This is a non-lock
    # read for the report/failure-reason decision; _persist_final_progress
    # re-reads under the lock and makes the authoritative persisted-status
    # decision, so a marker that disappears between reads cannot corrupt state.
    pre_state = run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
    cancel_requested = isinstance(pre_state, dict) and pre_state.get("cancelRequested") is True
    empty_result = False
    if cancel_requested:
        status = run_registry.STATUS_CANCELLED
        exit_code = 1
        _reconcile_cancel_extra(merged_extra)
    elif status == run_registry.STATUS_SUCCEEDED:
        work_summary = merged_extra.get("workSummary")
        empty_result = (
            isinstance(work_summary, dict)
            and work_summary.get("noChanges") is True
            and _tracked_capture_quality(
                files,
                ctx,
                capture,
                completion_report_mode=completion_report_mode,
            )
            == RESULT_QUALITY_NO_ASSISTANT_TEXT
        )
        if empty_result:
            merged_extra.update(
                childExitCode=exit_code,
                error="empty_result",
                message="Child harness exited without assistant text or file changes.",
            )
            status = run_registry.STATUS_FAILED
            exit_code = 1
        else:
            for key in ("failureReason", "error", "message"):
                merged_extra.pop(key, None)
    signal_text = "\n".join(
        part
        for part in (stderr_tail, _accumulator_failure_signal_text(capture.accumulator))
        if part
    )
    failure = _failure_details(
        status=status,
        signal_text=signal_text,
        extra=merged_extra,
    )
    if ctx.followup_of is not None:
        session_failure = child_failures.classify_followup_session_failure(signal_text, ctx.engine)
        if session_failure is not None:
            failure = session_failure
    failure_reason = failure.code if failure is not None else None
    failure_message = failure.message if failure is not None else None
    if failure_reason is not None:
        merged_extra["failureReason"] = failure_reason
        if status != run_registry.STATUS_CANCELLED:
            merged_extra["error"] = failure_reason
            merged_extra["message"] = failure_message
        if failure_reason == "auth_failed":
            merged_extra["nextActions"] = _auth_remediation_actions(ctx)
        # An unclassified child failure carries a generic message, so without
        # this the child's own words reach only the completion report and the
        # caller is told "Child harness failed" and nothing else. Call mode
        # already returns stderrTail on failure; tracked runs now match it.
        if stderr_tail.strip():
            merged_extra["stderrTail"] = stderr_tail
    report_text, report_source = _completion_report_text_and_source(
        ctx,
        capture.accumulator,
        completion_report_mode=completion_report_mode,
        status=status,
        failure_reason=failure_reason,
        failure_message=failure_message,
        stderr_tail=stderr_tail,
    )
    failover_notice = merged_extra.get("failoverNotice")
    if isinstance(failover_notice, str) and failover_notice.strip():
        report_text = (
            f"{failover_notice}\n\n{report_text}" if report_text.strip() else failover_notice
        )
    report_written = write_completion_report(files.run_path, report_text)
    result_quality = (
        RESULT_QUALITY_NO_ASSISTANT_TEXT
        if empty_result
        else _classify_result_quality(
            ctx=ctx,
            exit_code=exit_code,
            report_text=report_text,
            report_written=report_written,
            report_source=report_source,
            accumulator=capture.accumulator,
        )
    )
    merged_extra["completionReportWritten"] = report_written
    merged_extra["completionReportSource"] = report_source if report_written else None
    merged_extra["resultQuality"] = result_quality
    if result_quality != RESULT_QUALITY_OK:
        warnings = list(merged_extra.get("warnings") or [])
        _append_unique(warnings, _quality_warning(result_quality, harness=ctx.harness))
        merged_extra["warnings"] = warnings
    persisted_status, persisted_extra = _persist_final_progress(
        files.run_path,
        ctx,
        capture.accumulator,
        status=status,
        exit_code=exit_code,
        stdout_bytes=capture.stdout_bytes,
        stderr_bytes=capture.stderr_bytes,
        completion_report_written=report_written,
        extra=merged_extra,
    )
    if (
        persisted_status == run_registry.STATUS_CANCELLED
        and status != run_registry.STATUS_CANCELLED
    ):
        # Cancel arrived after the preliminary state read and report decision.
        # Rebuild the report for the authoritative cancelled outcome, then
        # persist its reconciled metadata in the same rare path.
        _reconcile_cancel_extra(persisted_extra)
        report_text, report_source = _completion_report_text_and_source(
            ctx,
            capture.accumulator,
            completion_report_mode=completion_report_mode,
            status=run_registry.STATUS_CANCELLED,
            failure_reason="cancelled_by_user",
            failure_message="Run was cancelled before completion.",
            stderr_tail=stderr_tail,
        )
        report_written = write_completion_report(files.run_path, report_text)
        result_quality = _classify_result_quality(
            ctx=ctx,
            exit_code=1,
            report_text=report_text,
            report_written=report_written,
            report_source=report_source,
            accumulator=capture.accumulator,
        )
        persisted_extra["completionReportWritten"] = report_written
        persisted_extra["completionReportSource"] = report_source if report_written else None
        persisted_extra["resultQuality"] = result_quality
        persisted_status, persisted_extra = _persist_final_progress(
            files.run_path,
            ctx,
            capture.accumulator,
            status=run_registry.STATUS_CANCELLED,
            exit_code=1,
            stdout_bytes=capture.stdout_bytes,
            stderr_bytes=capture.stderr_bytes,
            completion_report_written=report_written,
            extra=persisted_extra,
        )
    # Cancel-precedence reconciliation: when the finalizer preserved a
    # concurrent cancel (persisted_status is cancelled but the runner's own
    # exit-code-derived status was not cancelled), the LIVE result returned to
    # the caller must agree with the persisted state. A cancelled run never
    # reports success, so normalize exit_code to 1, status to cancelled, and
    # the failure reason to cancelled_by_user. This keeps the CLI envelope
    # (ok/status/exitCode), the process exit code, and state.json in lockstep.
    if persisted_status == run_registry.STATUS_CANCELLED:
        return TrackedFinalization(
            status=run_registry.STATUS_CANCELLED,
            exit_code=1,
            report_written=report_written,
            extra=persisted_extra,
        )
    return TrackedFinalization(
        status=persisted_status,
        exit_code=exit_code,
        report_written=report_written,
        extra=persisted_extra,
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
        _assistant_text, assistant_meta = capture.accumulator.bounded_assistant_text()
        extra = dict(finalization.extra)
        if capture.accumulator.session_id is not None:
            extra["sessionId"] = capture.accumulator.session_id
        payload = completion_json_payload(
            ctx,
            ok=ok,
            status=finalization.status,
            exit_code=finalization.exit_code,
            duration_ms=capture.duration_ms,
            stdout_bytes=capture.stdout_bytes,
            stderr_bytes=capture.stderr_bytes,
            completion_report_written=finalization.report_written,
            assistant_meta=assistant_meta,
            usage=capture.accumulator.usage,
            extra=extra,
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


def _attempt_delimiter(label: str) -> bytes:
    prefix = (
        "delegate codex auth attempt"
        if label == "fallback"
        else "delegate codex thread attempt"
        if label in {"thread-retry", "thread-ephemeral-fallback"}
        else "delegate empty-retry attempt"
        if label == "empty-success-retry"
        else "delegate attempt"
    )
    return f"\n--- {prefix}: {label} ---\n".encode()


def _append_attempt_delimiter(stderr_log: Path, *, label: str) -> None:
    with stderr_log.open("ab") as handle:
        handle.write(_attempt_delimiter(label))


def _prepend_attempt_delimiter(stderr_log: Path, *, label: str) -> None:
    # Several retry paths can each precede the same run's first attempt (codex
    # thread-retry, auth fallback, empty-success retry), so prepending is
    # idempotent: the label is written once no matter how many retries stack.
    existing = stderr_log.read_bytes() if stderr_log.exists() else b""
    delimiter = _attempt_delimiter(label)
    if existing.startswith(delimiter):
        return
    stderr_log.write_bytes(delimiter + existing)


def _accumulator_failure_signal_text(accumulator: harness_events.StreamAccumulator) -> str:
    # Redacted here rather than at each call site: this is the only classifier
    # input that is not already scrubbed, and the classified message reaches
    # state.json, the snapshot, and the completion report.
    events = list(accumulator.events)
    for kind in ("error", "run.completed"):
        latest = accumulator.events.last_by_kind.get(kind)
        if latest is not None and latest not in events:
            events.append(latest)
    return redaction.redact_string(
        "\n".join(
            event.message
            for event in events
            if event.message
            and (
                event.kind == "error"
                or (event.kind == "run.completed" and event.status == run_registry.STATUS_FAILED)
            )
        )
    )


def _attempt_log_tail(path: Path, byte_count: int) -> str:
    return profiles.read_bounded_stderr_tail(
        path, limit=min(byte_count, profiles.STDERR_TAIL_LIMIT)
    )


def _capture_failure(
    files: TrackedRunFiles, capture: TrackedCaptureResult
) -> child_failures.ChildFailure | None:
    if capture.exit_code == 0 and capture.accumulator.terminal_status not in {
        run_registry.STATUS_FAILED,
        run_registry.STATUS_CANCELLED,
    }:
        return None
    signal = "\n".join(
        part
        for part in (
            _attempt_log_tail(files.stderr_log, capture.stderr_bytes),
            _accumulator_failure_signal_text(capture.accumulator),
        )
        if part
    )
    return child_failures.classify(signal)


def _codex_failure_signal_text(files: TrackedRunFiles, capture: TrackedCaptureResult) -> str:
    return "\n".join(
        part
        for part in (
            _attempt_log_tail(files.stderr_log, capture.stderr_bytes),
            _accumulator_failure_signal_text(capture.accumulator),
        )
        if part
    )


def _record_codex_usage_block(
    files: TrackedRunFiles,
    capture: TrackedCaptureResult,
    identity: str | None,
    *,
    profile_alias: str | None = None,
) -> int | None:
    signal = _codex_failure_signal_text(files, capture)
    if not profiles.classify_codex_usage_limit(signal):
        return None
    reset_epoch = failover_state.parse_reset_epoch(signal)
    failover_state.write_block("codex", identity, reset_epoch, profile_alias=profile_alias)
    return reset_epoch


def _codex_ephemeral_fallback_argv(argv: list[str]) -> list[str]:
    updated: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--profile" and i + 1 < len(argv):
            i += 2
            continue
        updated.append(argv[i])
        i += 1
    if "exec" in updated and "--ignore-user-config" not in updated:
        updated.insert(updated.index("exec") + 1, "--ignore-user-config")
    if "--ephemeral" not in updated:
        updated.insert(max(len(updated) - 1, 0), "--ephemeral")
    return updated


def _append_runtime_event(files: TrackedRunFiles, kind: str, message: str) -> None:
    with open_events_log(files.run_path) as handle:
        append_event(handle, {"kind": kind, "message": message})


def _should_retry_profiles(
    ctx: RunContext,
    capture: TrackedCaptureResult,
    *,
    cwd: str,
    stderr_log: Path,
    workspace_baseline: profiles.WorkspaceBaseline | None,
) -> bool:
    if (
        ctx.engine != "codex"
        or not ctx.fallback_env_overrides
        or ctx.codex_failover_identity is None
        or ctx.codex_fallback_failover_identity is None
    ):
        return False
    if capture.error is not None or capture.exit_code == 0:
        return False
    # Codex --json reports usage limits as stdout events, not stderr, so the
    # classifier must see both channels (stderr-only missed real quota walls).
    stderr_tail = profiles.read_bounded_stderr_tail(stderr_log)
    event_text = _accumulator_failure_signal_text(capture.accumulator)
    signal_text = "\n".join(part for part in (stderr_tail, event_text) if part)
    if not profiles.classify_codex_usage_limit(signal_text):
        return False
    return _retry_is_safe(ctx, capture, cwd=cwd, workspace_baseline=workspace_baseline)


def _build_failover_notice(
    ctx: RunContext,
    *,
    blocked_profile: str | None,
    serving_profile: str | None,
    blocked_until: int | None = None,
    preflight: bool = False,
) -> str:
    primary = (blocked_profile or "unknown").upper()
    fallback = (serving_profile or "unknown").upper()
    trigger = (
        f"{primary} {ctx.engine.upper()} was already blocked (preflight)"
        if preflight
        else f"{primary} {ctx.engine.upper()} hit its usage limit"
    )
    until = (
        f"until ~{failover_state.epoch_to_human(blocked_until)}"
        if blocked_until is not None
        else "until the limit resets"
    )
    return (
        f"[ai-failover] {trigger}; this run was served by {fallback}; "
        f"future runs use {fallback} {until}; continue normally."
    )


def _retry_is_safe(
    ctx: RunContext,
    capture: TrackedCaptureResult,
    *,
    cwd: str,
    workspace_baseline: profiles.WorkspaceBaseline | None,
) -> bool:
    if profiles.accumulator_had_tool_events(capture.accumulator):
        return False
    if ctx.mode == "call":
        return ctx.call_read_only
    if ctx.mode == "work":
        return profiles.work_mode_safe_for_codex_fallback(cwd, workspace_baseline)
    if ctx.mode == "safe":
        if not ctx.isolated_workspace:
            return True
        return (
            workspace_baseline is not None
            and workspace_baseline.directory_entries is None
            and profiles.workspace_baseline_unchanged(cwd, workspace_baseline)
        )
    return False


def _cancel_requested_or_cancelled(ctx: RunContext) -> bool:
    state = run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
    return isinstance(state, dict) and (
        state.get("cancelRequested") is True or state.get("status") == run_registry.STATUS_CANCELLED
    )


def _run_single_tracked_attempt(
    argv: list[str],
    cwd: str,
    files: TrackedRunFiles,
    ctx: RunContext,
    *,
    started: float,
    deadline: float | None,
    stdin_text: str | None,
    env_overrides: dict[str, str] | None,
    scratch_dir: Path | None,
    progress: bool,
    progress_stderr: TextIO | None,
    progress_initial_delay_sec: float,
    progress_interval_sec: float,
    attempt_label: str | None = None,
    prior_capture: TrackedCaptureResult | None = None,
) -> TrackedCaptureResult:
    process: subprocess.Popen[bytes] | None = None
    process_pgid: int | None = None
    grace_seconds = _process_group_grace_seconds(ctx)
    launch_exc: OSError | None = None
    boundary_exc: DelegateError | None = None
    # Admission, launch, and pid/pgid publication are one locked generation
    # transition for both the primary attempt and retries. Cancel therefore
    # observes either its marker blocking a retry or a complete live generation;
    # there is no launched-but-unpublished child it can accidentally miss.
    with _launch_registry_lock(ctx):
        current = run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
        if (
            prior_capture is not None
            and isinstance(current, dict)
            and (
                current.get("cancelRequested") is True
                or current.get("status") == run_registry.STATUS_CANCELLED
            )
        ):
            raise RunnerLaunchError("cancelled_by_user", "Run was cancelled.", 1)
        if attempt_label is not None:
            _append_attempt_delimiter(files.stderr_log, label=attempt_label)
        try:
            process = _launch_tracked_process(
                argv,
                cwd,
                stdin_text=stdin_text,
                env_overrides=env_overrides,
                drop_env=(
                    ("DELEGATE_CONFIG",) if env_overrides is ctx.fallback_env_overrides else ()
                ),
                scratch_dir=scratch_dir,
                sandbox=ctx.sandbox,
                engine=ctx.engine,
                extra_rw_roots=_bwrap_mail_push_rw_roots(ctx) if ctx.sandbox else None,
            )
        except OSError as exc:
            launch_exc = exc
        except DelegateError as exc:
            # bwrap boundary construction or preflight refused the plan: the
            # child never ran. Recorded below, outside the registry lock.
            boundary_exc = exc
        else:
            try:
                process_pgid = _process_group_for_process(process)
                # start_new_session makes the child the group leader. Keep the
                # launch pid as a last-resort record when an immediately
                # exiting child has already made getpgid() return ESRCH.
                if process_pgid is None:
                    raise RunnerLaunchError(
                        "missing_child_pid",
                        "Child process did not expose a numeric pid for process-group tracking.",
                    )
                write_state(
                    files.run_path,
                    build_state(
                        ctx,
                        status="running",
                        pid=process.pid,
                        pgid=process_pgid,
                    ),
                )
                manifest = run_registry.load_run_manifest_or_none(ctx.registry_root, ctx.run_id)
                if isinstance(manifest, dict):
                    manifest["pid"] = process.pid
                    manifest["pgid"] = process_pgid
                    manifest["processGroupTerminationGraceSec"] = grace_seconds
                    write_manifest(files.run_path, manifest)
                index = run_registry.load_index(ctx.registry_root)
                runs = index.get("runs")
                entry = runs.get(ctx.run_id) if isinstance(runs, dict) else None
                if isinstance(entry, dict):
                    entry["pid"] = process.pid
                    entry["pgid"] = process_pgid
                    run_registry.save_index(ctx.registry_root, index)
            except BaseException:
                _terminate_call_process(
                    process,
                    pgid=process_pgid,
                    grace_seconds=grace_seconds,
                    identity_ctx=ctx,
                )
                raise
    if boundary_exc is not None:
        boundary_error = RunnerLaunchError(boundary_exc.error, boundary_exc.message)
        _record_tracked_launch_failure(files, ctx, boundary_error, prior_capture=prior_capture)
        raise boundary_error from boundary_exc
    if launch_exc is not None:
        exc = launch_exc
        error = _runner_launch_error(argv, cwd, exc)
        _record_tracked_launch_failure(files, ctx, error, prior_capture=prior_capture)
        raise error from exc
    assert process is not None
    capture: TrackedCaptureResult | None = None
    try:
        capture = _capture_tracked_process(
            process,
            files,
            ctx,
            started=started,
            stdin_text=stdin_text,
            deadline=deadline,
            progress_stderr=progress_stderr,
            progress_initial_delay_sec=progress_initial_delay_sec,
            progress_interval_sec=progress_interval_sec,
            process_group_pgid=process_pgid,
            process_group_grace_seconds=grace_seconds,
        )
    except RunnerLaunchError as error:
        _record_tracked_launch_failure(files, ctx, error, prior_capture=prior_capture)
        raise
    finally:
        # Capture terminates timeout/stall/overflow paths while the leader is
        # alive. This second, unconditional pass handles normal exits, failed
        # exits, retries, and unexpected capture exceptions where grandchildren
        # can remain in the recorded group after the leader was reaped.
        group_exited = _terminate_call_process(
            process,
            pgid=process_pgid,
            grace_seconds=grace_seconds,
            identity_ctx=ctx,
        )
    if capture is None:  # pragma: no cover - the try block either returns or raises
        raise AssertionError("tracked capture did not produce a result")
    if not group_exited:
        capture = replace(capture, process_group_survived=True)
    return capture


def _merge_tracked_attempt_captures(
    prior_capture: TrackedCaptureResult,
    current_capture: TrackedCaptureResult,
) -> TrackedCaptureResult:
    accumulator = harness_events.StreamAccumulator(harness=current_capture.accumulator.harness)
    accumulator.assistant_chunks = [
        *prior_capture.accumulator.assistant_chunks,
        *current_capture.accumulator.assistant_chunks,
    ]
    accumulator.events.extend_buffer(prior_capture.accumulator.events)
    accumulator.events.extend_buffer(current_capture.accumulator.events)
    accumulator.completion_text = (
        current_capture.accumulator.completion_text or prior_capture.accumulator.completion_text
    )
    accumulator.current = current_capture.accumulator.current or prior_capture.accumulator.current
    accumulator.terminal_event = current_capture.accumulator.terminal_event
    accumulator.terminal_status = current_capture.accumulator.terminal_status
    prior_usage = prior_capture.accumulator.usage
    current_usage = current_capture.accumulator.usage
    if prior_usage is None and current_usage is None:
        accumulator.usage = None
    elif prior_usage is None or current_usage is None:
        accumulator.usage = {"basis": "unavailable"}
    else:
        accumulator.usage = _aggregate_usage(prior_usage, current_usage)
    accumulator.structured_events_seen = (
        prior_capture.accumulator.structured_events_seen
        + current_capture.accumulator.structured_events_seen
    )
    return replace(
        current_capture,
        accumulator=accumulator,
        stdout_bytes=prior_capture.stdout_bytes + current_capture.stdout_bytes,
        stderr_bytes=prior_capture.stderr_bytes + current_capture.stderr_bytes,
        stdin_failures=tuple(
            dict.fromkeys([*prior_capture.stdin_failures, *current_capture.stdin_failures])
        ),
        mail_push_failure_reason=(
            current_capture.mail_push_failure_reason or prior_capture.mail_push_failure_reason
        ),
        process_group_survived=(
            prior_capture.process_group_survived or current_capture.process_group_survived
        ),
        zero_commit_health=(current_capture.zero_commit_health or prior_capture.zero_commit_health),
    )


def _append_empty_retry_instruction(prompt: str) -> str:
    return f"{prompt.rstrip()}\n\n{EMPTY_RETRY_INSTRUCTION}"


def _empty_retry_allowed(mode: str, *, call_read_only: bool = False) -> bool:
    return mode == "safe" or (mode == "call" and call_read_only)


def _tracked_capture_quality(
    files: TrackedRunFiles,
    ctx: RunContext,
    capture: TrackedCaptureResult,
    *,
    completion_report_mode: str,
) -> str:
    if capture.exit_code != 0 or capture.accumulator.terminal_status in {
        run_registry.STATUS_FAILED,
        run_registry.STATUS_CANCELLED,
    }:
        return RESULT_QUALITY_OK
    report_text, report_source = _completion_report_text_and_source(
        ctx,
        capture.accumulator,
        completion_report_mode=completion_report_mode,
        status=run_registry.STATUS_SUCCEEDED,
        failure_reason=None,
        failure_message=None,
        stderr_tail=profiles.read_bounded_stderr_tail(files.stderr_log),
    )
    return _classify_result_quality(
        ctx=ctx,
        exit_code=capture.exit_code,
        report_text=report_text,
        report_written=bool(report_text.strip()),
        report_source=report_source,
        accumulator=capture.accumulator,
    )


def _materialize_empty_retry(
    argv: list[str],
    *,
    stdin_text: str | None,
    prompt_file_text: str | None,
    prompt_file_placeholder: str | None,
    agent_config_text: str | None,
    agent_config_placeholder: str | None,
    agent_config_dir: Path | None = None,
    persona_file_text: str | None = None,
    persona_file_placeholder: str | None = None,
    temp_base: Path | None = None,
) -> tuple[list[str], str | None, str | None]:
    if stdin_text is not None:
        return list(argv), _append_empty_retry_instruction(stdin_text), None
    if prompt_file_text is not None:
        retry_argv, retry_dir = _materialize_prompt_file_argv(
            argv,
            prompt_file_text=_append_empty_retry_instruction(prompt_file_text),
            prompt_file_placeholder=prompt_file_placeholder,
            agent_config_text=agent_config_text,
            agent_config_placeholder=agent_config_placeholder,
            agent_config_dir=agent_config_dir,
            persona_file_text=persona_file_text,
            persona_file_placeholder=persona_file_placeholder,
            temp_base=temp_base,
        )
        return retry_argv, None, retry_dir
    retry_argv = list(argv)
    if retry_argv:
        retry_argv[-1] = _append_empty_retry_instruction(retry_argv[-1])
    return retry_argv, None, None


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
    agent_config_text: str | None = None,
    agent_config_placeholder: str | None = None,
    persona_file_text: str | None = None,
    persona_file_placeholder: str | None = None,
    output_schema_text: str | None = None,
    output_schema_path: str | None = None,
    manifest_argv: list[str] | None = None,
    progress: bool = False,
    progress_initial_delay_sec: float = PROGRESS_INITIAL_DELAY_SEC,
    progress_interval_sec: float = PROGRESS_HEARTBEAT_INTERVAL_SEC,
    timeout: int | None = None,
) -> tuple[int, JsonObject | None]:
    completed = False
    try:
        result = _execute_tracked(
            argv,
            cwd,
            ctx,
            json_mode=json_mode,
            stdout=stdout,
            stderr=stderr,
            completion_report_mode=completion_report_mode,
            stdin_text=stdin_text,
            prompt_file_text=prompt_file_text,
            prompt_file_placeholder=prompt_file_placeholder,
            agent_config_text=agent_config_text,
            agent_config_placeholder=agent_config_placeholder,
            persona_file_text=persona_file_text,
            persona_file_placeholder=persona_file_placeholder,
            output_schema_text=output_schema_text,
            output_schema_path=output_schema_path,
            manifest_argv=manifest_argv,
            progress=progress,
            progress_initial_delay_sec=progress_initial_delay_sec,
            progress_interval_sec=progress_interval_sec,
            timeout=timeout,
        )
        completed = True
        return result
    finally:
        if not completed:
            _cleanup_unfinished_mail_push_private_homes(ctx)


def _execute_tracked(
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
    agent_config_text: str | None = None,
    agent_config_placeholder: str | None = None,
    persona_file_text: str | None = None,
    persona_file_placeholder: str | None = None,
    output_schema_text: str | None = None,
    output_schema_path: str | None = None,
    manifest_argv: list[str] | None = None,
    progress: bool = False,
    progress_initial_delay_sec: float = PROGRESS_INITIAL_DELAY_SEC,
    progress_interval_sec: float = PROGRESS_HEARTBEAT_INTERVAL_SEC,
    timeout: int | None = None,
) -> tuple[int, JsonObject | None]:
    if stdin_text is not None and prompt_file_text is not None:
        raise ValueError("stdin_text and prompt_file_text are mutually exclusive")
    files = _prepare_tracked_run(argv, ctx, manifest_argv=manifest_argv)
    for warning in _mail_push_warnings(ctx):
        _append_runtime_event(files, MAIL_PUSH_EVENT_KIND, warning)
    started = time.monotonic()
    deadline = None if timeout is None else started + timeout
    run_argv = _codex_argv_with_scratch(argv, files.scratch_dir) if ctx.engine == "codex" else argv
    run_manifest_argv = (
        _codex_argv_with_scratch(manifest_argv, files.scratch_dir)
        if ctx.engine == "codex" and manifest_argv is not None
        else manifest_argv
    )
    if ctx.engine == "codex" and files.scratch_dir is not None:
        write_manifest(files.run_path, build_manifest(ctx, run_manifest_argv or run_argv))
    sandbox_temp_base = files.scratch_dir if ctx.sandbox else None
    launch_argv, prompt_temp_dir = _materialize_prompt_file_argv(
        run_argv,
        prompt_file_text=prompt_file_text,
        prompt_file_placeholder=prompt_file_placeholder,
        agent_config_text=agent_config_text,
        agent_config_placeholder=agent_config_placeholder,
        persona_file_text=persona_file_text,
        persona_file_placeholder=persona_file_placeholder,
        # Inside the bwrap boundary the run directory is hidden behind the
        # registry tmpfs; every child-consumed artifact must live under scratch.
        agent_config_dir=sandbox_temp_base if ctx.sandbox else files.run_path,
        temp_base=sandbox_temp_base,
    )
    launch_argv, schema_temp_dir = _materialize_output_schema_argv(
        launch_argv,
        output_schema_text=output_schema_text,
        output_schema_path=output_schema_path,
        destination_dir=(
            (sandbox_temp_base if ctx.sandbox else files.run_path)
            if ctx.resumed_from is not None
            else None
        ),
        temp_base=sandbox_temp_base,
    )
    retry_workspace = ctx.execution_cwd if ctx.isolated_workspace else cwd
    workspace_baseline = (
        profiles.capture_workspace_baseline(
            retry_workspace,
            allow_directory=(
                ctx.mode == "work"
                and ctx.workspace_kind == "directory"
                and bool(ctx.fallback_env_overrides)
            ),
        )
        if ctx.engine == "codex"
        and (
            ctx.mode == "work"
            or (ctx.mode == "safe" and ctx.isolated_workspace and ctx.workspace_kind == "git")
        )
        else None
    )
    fallback_extra: JsonObject | None = None
    thread_extra: JsonObject | None = None
    empty_retry_extra: JsonObject | None = None
    retry_prompt_temp_dir: str | None = None
    attempt_env = ctx.env_overrides or None
    capture: TrackedCaptureResult | None = None
    preflight_swap = False
    blocked_until: int | None = None
    if (
        ctx.engine == "codex"
        and ctx.codex_failover_identity is not None
        and ctx.codex_fallback_failover_identity is not None
        and ctx.fallback_env_overrides
    ):
        primary_blocked, blocked_until = failover_state.check_blocked(
            "codex", ctx.codex_failover_identity, profile_alias=ctx.auth_profile
        )
        fallback_blocked, _ = failover_state.check_blocked(
            "codex",
            ctx.codex_fallback_failover_identity,
            profile_alias=ctx.fallback_auth_profile,
        )
        if primary_blocked and not fallback_blocked:
            attempt_env = ctx.fallback_env_overrides
            preflight_swap = True

    def run_attempt(
        attempt_argv: list[str],
        *,
        env_overrides: dict[str, str] | None,
        attempt_stdin_text: str | None = stdin_text,
        attempt_label: str | None = None,
        prior_capture: TrackedCaptureResult | None = None,
    ) -> TrackedCaptureResult:
        return _run_single_tracked_attempt(
            attempt_argv,
            cwd,
            files,
            ctx,
            started=started,
            deadline=deadline,
            stdin_text=attempt_stdin_text,
            env_overrides=env_overrides,
            scratch_dir=files.scratch_dir,
            progress=progress,
            progress_stderr=stderr if progress else None,
            progress_initial_delay_sec=progress_initial_delay_sec,
            progress_interval_sec=progress_interval_sec,
            attempt_label=attempt_label,
            prior_capture=prior_capture,
        )

    try:
        capture = run_attempt(
            launch_argv,
            env_overrides=attempt_env,
            attempt_label="preflight-fallback" if preflight_swap else None,
        )
        if preflight_swap:
            _record_codex_usage_block(
                files,
                capture,
                ctx.codex_fallback_failover_identity,
                profile_alias=ctx.fallback_auth_profile,
            )
            fallback_extra = {
                "codexAuthFallback": {
                    **profiles.codex_auth_fallback_metadata(
                        reason="usage_limit_preflight",
                        primary_auth_profile=ctx.auth_profile,
                        fallback_auth_profile=ctx.fallback_auth_profile,
                        primary_exit_code=-1,
                        fallback_exit_code=capture.exit_code,
                        primary_stderr_tail="",
                    ),
                    "failoverPreflight": True,
                },
                "failoverNotice": _build_failover_notice(
                    ctx,
                    blocked_profile=ctx.auth_profile,
                    serving_profile=ctx.fallback_auth_profile,
                    blocked_until=blocked_until,
                    preflight=True,
                ),
            }
        if (
            ctx.engine == "codex"
            and not _cancel_requested_or_cancelled(ctx)
            and (primary_failure := _capture_failure(files, capture)) is not None
            and primary_failure.code == "codex_thread_lost"
            and _retry_is_safe(
                ctx,
                capture,
                cwd=retry_workspace,
                workspace_baseline=workspace_baseline,
            )
        ):
            _prepend_attempt_delimiter(files.stderr_log, label="primary")
            retry_capture = run_attempt(
                launch_argv,
                env_overrides=attempt_env,
                attempt_label="thread-retry",
                prior_capture=capture,
            )
            retry_failure = _capture_failure(files, retry_capture)
            capture = _merge_tracked_attempt_captures(capture, retry_capture)
            resolved = retry_capture.exit_code == 0 and retry_failure is None
            thread_extra = {
                "codexThreadFallback": {
                    "retryAttempted": True,
                    "engaged": False,
                    "resolved": resolved,
                }
            }
            if (
                not resolved
                and retry_failure is not None
                and retry_failure.code == "codex_thread_lost"
                and not _cancel_requested_or_cancelled(ctx)
                and not ctx.followup_of
                and _retry_is_safe(
                    ctx,
                    retry_capture,
                    cwd=retry_workspace,
                    workspace_baseline=workspace_baseline,
                )
            ):
                _append_runtime_event(
                    files,
                    "codex_thread_fallback",
                    "Codex thread lookup failed twice; engaging ephemeral ignore-user-config fallback.",
                )
                fallback_capture = run_attempt(
                    _codex_ephemeral_fallback_argv(launch_argv),
                    env_overrides=attempt_env,
                    attempt_label="thread-ephemeral-fallback",
                    prior_capture=capture,
                )
                fallback_failure = _capture_failure(files, fallback_capture)
                capture = _merge_tracked_attempt_captures(capture, fallback_capture)
                resolved = fallback_capture.exit_code == 0 and fallback_failure is None
                thread_extra = {
                    "codexThreadFallback": {
                        "retryAttempted": True,
                        "engaged": True,
                        "resolved": resolved,
                    },
                    "warnings": [
                        "Codex thread lookup failed twice; ephemeral ignore-user-config "
                        "fallback engaged."
                    ],
                }
                if not resolved:
                    final_failure = fallback_failure or child_failures.ChildFailure(
                        "child_failed",
                        "Codex ephemeral fallback failed for an unrecognized reason.",
                    )
                    thread_extra.update(
                        failureReason=final_failure.code,
                        error=final_failure.code,
                        message=final_failure.message,
                    )
            elif not resolved:
                final_failure = retry_failure or child_failures.ChildFailure(
                    "child_failed",
                    "Codex retry failed for an unrecognized reason.",
                )
                thread_extra.update(
                    failureReason=final_failure.code,
                    error=final_failure.code,
                    message=final_failure.message,
                )
        if (
            not preflight_swap
            and not _cancel_requested_or_cancelled(ctx)
            and _should_retry_profiles(
                ctx,
                capture,
                cwd=retry_workspace,
                stderr_log=files.stderr_log,
                workspace_baseline=workspace_baseline,
            )
        ):
            primary_exit_code = capture.exit_code
            primary_stderr_tail = profiles.read_bounded_stderr_tail(files.stderr_log)
            _record_codex_usage_block(
                files, capture, ctx.codex_failover_identity, profile_alias=ctx.auth_profile
            )
            fallback_blocked, _ = failover_state.check_blocked(
                "codex",
                ctx.codex_fallback_failover_identity,
                profile_alias=ctx.fallback_auth_profile,
            )
            if fallback_blocked:
                fallback_extra = {
                    "warnings": [
                        "codex_auth_fallback: skipped because the fallback profile is also blocked."
                    ],
                }
            else:
                _prepend_attempt_delimiter(files.stderr_log, label="primary")
                fallback_capture = run_attempt(
                    launch_argv,
                    env_overrides=ctx.fallback_env_overrides,
                    attempt_label="fallback",
                    prior_capture=capture,
                )
                _record_codex_usage_block(
                    files,
                    fallback_capture,
                    ctx.codex_fallback_failover_identity,
                    profile_alias=ctx.fallback_auth_profile,
                )
                capture = _merge_tracked_attempt_captures(capture, fallback_capture)
                attempt_env = ctx.fallback_env_overrides
                fallback_extra = {
                    "codexAuthFallback": profiles.codex_auth_fallback_metadata(
                        reason="usage_limit",
                        primary_auth_profile=ctx.auth_profile,
                        fallback_auth_profile=ctx.fallback_auth_profile,
                        primary_exit_code=primary_exit_code,
                        fallback_exit_code=fallback_capture.exit_code,
                        primary_stderr_tail=primary_stderr_tail,
                    ),
                    "failoverNotice": _build_failover_notice(
                        ctx,
                        blocked_profile=ctx.auth_profile,
                        serving_profile=ctx.fallback_auth_profile,
                        blocked_until=failover_state.check_blocked(
                            "codex",
                            ctx.codex_failover_identity,
                            profile_alias=ctx.auth_profile,
                        )[1],
                    ),
                }
        elif (
            not preflight_swap
            and ctx.engine == "codex"
            and ctx.codex_failover_identity is not None
            and capture.exit_code != 0
        ):
            _record_codex_usage_block(
                files, capture, ctx.codex_failover_identity, profile_alias=ctx.auth_profile
            )
        retry_supported = (
            stdin_text is not None or prompt_file_text is not None or manifest_argv is not None
        )
        if (
            not _cancel_requested_or_cancelled(ctx)
            and retry_supported
            and _tracked_capture_quality(
                files,
                ctx,
                capture,
                completion_report_mode=completion_report_mode,
            )
            == RESULT_QUALITY_EMPTY
        ):
            if ctx.pure or ctx.prompt_instruction_mode == PROMPT_INSTRUCTION_MODE_SLASH:
                empty_retry_extra = {"warnings": [EMPTY_RETRY_SKIPPED_VERBATIM_WARNING]}
            elif _empty_retry_allowed(ctx.mode, call_read_only=ctx.call_read_only):
                retry_argv, retry_stdin, retry_prompt_temp_dir = _materialize_empty_retry(
                    run_argv,
                    stdin_text=stdin_text,
                    prompt_file_text=prompt_file_text,
                    prompt_file_placeholder=prompt_file_placeholder,
                    agent_config_text=agent_config_text,
                    agent_config_placeholder=agent_config_placeholder,
                    agent_config_dir=files.run_path,
                    persona_file_text=persona_file_text,
                    persona_file_placeholder=persona_file_placeholder,
                    temp_base=sandbox_temp_base,
                )
                if (
                    ctx.resumed_from is not None
                    and ctx.engine in resume_command.ARGV_PROMPT_TRANSPORT_ENGINES
                    and retry_stdin is None
                ):
                    try:
                        resume_command.enforce_resume_prompt_size(ctx.engine, retry_argv[-1])
                    except DelegateError as exc:
                        error = RunnerLaunchError(exc.error, exc.message)
                        _record_tracked_launch_failure(files, ctx, error, prior_capture=capture)
                        raise error from exc
                _prepend_attempt_delimiter(files.stderr_log, label="primary")
                retry_capture = run_attempt(
                    retry_argv,
                    attempt_stdin_text=retry_stdin,
                    env_overrides=attempt_env,
                    attempt_label="empty-success-retry",
                    prior_capture=capture,
                )
                capture = _merge_tracked_attempt_captures(capture, retry_capture)
                retry_quality = _tracked_capture_quality(
                    files,
                    ctx,
                    capture,
                    completion_report_mode=completion_report_mode,
                )
                resolved = (
                    capture.exit_code == 0
                    and capture.accumulator.terminal_status
                    not in {run_registry.STATUS_FAILED, run_registry.STATUS_CANCELLED}
                    and retry_quality == RESULT_QUALITY_OK
                )
                empty_retry_extra = {
                    "emptyRetry": {
                        "attempted": True,
                        "resolved": resolved,
                    }
                }
                if not resolved:
                    empty_retry_extra["warnings"] = [EMPTY_RETRY_WARNING]
            elif ctx.mode == "call":
                empty_retry_extra = {
                    "warnings": [EMPTY_RETRY_SKIPPED_WRITE_CAPABLE_WARNING],
                }
    except RunnerLaunchError:
        if capture is None:
            _cleanup_unfinished_mail_push_private_homes(ctx)
        if capture is None or not _cancel_requested_or_cancelled(ctx):
            raise
        # A retry launch lost the race to cancellation. Its locked launch-
        # failure persistence preserved the marker/terminal state; finalize the
        # prior capture through the ordinary cancelled path so live state,
        # snapshot, report, and exit code converge on cancelled/1.
    finally:
        _cleanup_prompt_file_dir(prompt_temp_dir)
        _cleanup_prompt_file_dir(retry_prompt_temp_dir)
        _cleanup_prompt_file_dir(schema_temp_dir)

    assert capture is not None
    ctx = _ctx_with_stdin_warnings(ctx, capture.stdin_failures, stderr)
    final_extra: JsonObject = {}
    final_warnings: list[str] = []
    for attempt_extra in (fallback_extra, thread_extra, empty_retry_extra):
        if attempt_extra is None:
            continue
        final_extra.update(
            {key: value for key, value in attempt_extra.items() if key != "warnings"}
        )
        for warning in attempt_extra.get("warnings") or []:
            if isinstance(warning, str):
                _append_unique(final_warnings, warning)
    if capture.zero_commit_health is not None:
        final_extra["zeroCommitHealth"] = capture.zero_commit_health
        _append_unique(final_warnings, _zero_commit_health_warning(capture.zero_commit_health))
    if final_warnings:
        final_extra["warnings"] = final_warnings
    if capture.output_limit_stream is not None:
        final_extra["outputLimit"] = {
            "stream": capture.output_limit_stream,
            "bytes": capture.output_limit_bytes,
        }
    if capture.stopped_after_completion:
        final_extra["stoppedAfterCompletion"] = True
    if capture.stall is not None:
        final_extra["stall"] = capture.stall
    if capture.error is not None:
        final_extra.update(
            error=capture.error,
            message=capture.message,
            failureReason=capture.error,
        )
    finalization = _finalize_tracked_run(
        files,
        ctx,
        capture,
        completion_report_mode=completion_report_mode,
        extra=final_extra or None,
    )
    from delegate_agent import worktree_mgmt

    worktree_mgmt.retire_worktree_on_completion(ctx, finalization.extra)
    _send_completion_notification(files.run_path, ctx, finalization.status)
    if capture.error is not None and finalization.status != run_registry.STATUS_CANCELLED:
        raise RunnerLaunchError(capture.error, capture.message or capture.error, 1)
    return _tracked_result(ctx, capture, finalization, json_mode=json_mode, stdout=stdout)


def _best_effort_stderr(line: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        print(line, file=sys.stderr)


def _send_completion_notification(run_path: Path, ctx: RunContext, status: str) -> None:
    """Fire the --notify ping exactly once, after the terminal state is persisted.

    Degradation is recorded in the manifest (``notify.ok=false`` + reason) and
    never alters the run's status or exit code.
    """
    if ctx.notify is None:
        return
    try:
        target = notify.parse_notify_target(ctx.notify)
        elapsed: float | None = None
        started = _parse_rfc3339(ctx.started_at) if ctx.started_at else None
        if started is not None:
            elapsed = max((datetime.now(UTC) - started).total_seconds(), 0.0)
        message = notify.notify_message(
            run_id=ctx.run_id,
            status=status,
            engine=ctx.engine,
            model=ctx.model_resolved or ctx.model,
            elapsed_sec=elapsed,
            workspace=ctx.source_cwd,
        )
        outcome = notify.send_notification(target, message, cwd=ctx.source_cwd, env=os.environ)
    except Exception as exc:
        outcome = notify.NotifyOutcome(
            ok=False,
            target=ctx.notify,
            reason=notify.REASON_HOOK_FAILED,
            detail=f"{type(exc).__name__}: {exc}"[: notify.DETAIL_LIMIT],
        )
    try:
        with run_registry.registry_lock(ctx.registry_root):
            manifest = run_registry.load_run_manifest_or_none(ctx.registry_root, ctx.run_id) or {}
            manifest["notify"] = outcome.payload()
            if not outcome.ok:
                warnings = [w for w in manifest.get("warnings", []) if isinstance(w, str)]
                warning = f"notify_degraded: {outcome.reason}"
                if warning not in warnings:
                    warnings.append(warning)
                manifest["warnings"] = warnings
            write_manifest(run_path, manifest)
    except Exception as exc:
        _best_effort_stderr(f"delegate: notify outcome not recorded ({type(exc).__name__})")
    if not outcome.ok:
        suffix = f": {outcome.detail}" if outcome.detail else ""
        _best_effort_stderr(f"delegate: notify degraded ({outcome.reason}{suffix})")


def _parse_rfc3339(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_process_group_id(pgid: int | None) -> int | None:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return None
    with contextlib.suppress(OSError):
        if pgid == os.getpgrp():
            return None
    return pgid


def _process_group_for_process(
    process: subprocess.Popen[bytes] | subprocess.Popen[str],
) -> int | None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    with contextlib.suppress(OSError, TypeError):
        return os.getpgid(pid)
    # start_new_session makes the leader pid the pgid. This fallback also
    # preserves a usable group id after the leader has already been reaped.
    return pid


def _process_identity_matches(ctx: RunContext, pid: int) -> bool:
    """Return whether a reaped tracked leader still belongs to this run."""

    # Keep the cancel and completion paths on the same PID-reuse guard. Import
    # lazily so the runner does not make the command module part of its import
    # cycle during CLI startup.
    from delegate_agent import wait_cancel_commands

    target = run_registry.RunTarget(run_id=ctx.run_id, alias=ctx.alias)
    try:
        wait_cancel_commands._check_pid_identity(ctx.registry_root, target, pid)
    except wait_cancel_commands.WaitCancelError as exc:
        if exc.error == "pid_identity_mismatch":
            return False
        raise
    return True


def _kill_process_group(pgid: int, sig: signal.Signals) -> None:
    safe_pgid = _safe_process_group_id(pgid)
    if safe_pgid is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(safe_pgid, sig)


def _wait_for_process_group_exit(pgid: int, timeout: float) -> bool:
    safe_pgid = _safe_process_group_id(pgid)
    if safe_pgid is None:
        return True
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(safe_pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _terminate_call_process(
    process: subprocess.Popen[bytes],
    *,
    pgid: int | None = None,
    grace_seconds: float = PROCESS_GROUP_TERMINATION_GRACE_SEC,
    identity_ctx: RunContext | None = None,
) -> bool:
    """Terminate a child process group, tolerating reaped leaders and ESRCH."""
    if pgid is None:
        pgid = _process_group_for_process(process)
    safe_pgid = _safe_process_group_id(pgid)
    if safe_pgid is None:
        return True
    if (
        identity_ctx is not None
        and process.poll() is not None
        and not _process_identity_matches(identity_ctx, process.pid)
    ):
        return True
    grace = max(float(grace_seconds), 0.0)
    _kill_process_group(safe_pgid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=grace)
    if _wait_for_process_group_exit(safe_pgid, grace):
        return True
    _kill_process_group(safe_pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=PROCESS_GROUP_KILL_WAIT_SEC)
    return _wait_for_process_group_exit(safe_pgid, PROCESS_GROUP_KILL_WAIT_SEC)


def _bounded_call_communicate(
    process: subprocess.Popen[bytes],
    stdin_bytes: bytes | None,
    timeout: float | None,
    max_stdout: int,
    max_stderr: int,
    process_group_grace_seconds: float = PROCESS_GROUP_TERMINATION_GRACE_SEC,
) -> tuple[bytes, bytes]:
    """Read child stdout/stderr under fixed byte caps; kill on overflow or timeout.

    The deadline starts immediately. Stdin is fed on a writer thread so a child
    that stops reading cannot block the parent past ``timeout``.
    """
    stdout_buf = io.BytesIO()
    stderr_buf = io.BytesIO()
    overflow = threading.Event()
    overflow_message: list[str] = [""]
    overflow_stream: list[str | None] = [None]
    start = time.monotonic()

    def _deadline_exceeded() -> bool:
        return timeout is not None and (time.monotonic() - start) >= timeout

    def _drain_to_buffer(
        pipe: BinaryIO,
        buf: io.BytesIO,
        limit: int,
        message: str,
        stream: str,
    ) -> None:
        try:
            while not overflow.is_set():
                chunk = pipe.read(65536)
                if not chunk:
                    break
                available = limit - buf.tell()
                if available <= 0:
                    overflow_message[0] = message
                    overflow_stream[0] = stream
                    overflow.set()
                    break
                if len(chunk) > available:
                    buf.write(chunk[:available])
                    overflow_message[0] = message
                    overflow_stream[0] = stream
                    overflow.set()
                    break
                buf.write(chunk)
        finally:
            with contextlib.suppress(OSError):
                pipe.close()

    def _write_stdin() -> None:
        assert process.stdin is not None and stdin_bytes is not None
        try:
            view = memoryview(stdin_bytes)
            offset = 0
            chunk_size = 65536
            while offset < len(view):
                if overflow.is_set() or _deadline_exceeded():
                    break
                end = min(offset + chunk_size, len(view))
                try:
                    written = process.stdin.write(view[offset:end])
                except OSError:
                    return
                if not written:
                    break
                offset += written
            with contextlib.suppress(OSError):
                process.stdin.flush()
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()

    stdout_thread = threading.Thread(
        target=_drain_to_buffer,
        args=(
            process.stdout,
            stdout_buf,
            max_stdout,
            f"Child call stdout exceeded the maximum allowed size of {max_stdout} bytes.",
            "stdout",
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_to_buffer,
        args=(
            process.stderr,
            stderr_buf,
            max_stderr,
            f"Child call stderr exceeded the maximum allowed size of {max_stderr} bytes.",
            "stderr",
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    stdin_thread: threading.Thread | None = None
    if stdin_bytes is not None and process.stdin is not None:
        stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
        stdin_thread.start()

    while True:
        poll_interval = 0.05
        if timeout is not None:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                break
            poll_interval = min(poll_interval, remaining)
        try:
            process.wait(timeout=poll_interval)
        except subprocess.TimeoutExpired:
            if overflow.is_set():
                break
            continue
        else:
            break

    def _join_io_threads() -> None:
        if stdin_thread is not None:
            stdin_thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
        stdout_thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
        stderr_thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)

    timed_out = _deadline_exceeded() and process.poll() is None
    if overflow.is_set() or timed_out:
        # Terminate BEFORE closing stdin. On a real full pipe the writer thread
        # is blocked in write() holding the buffered-writer lock, so a parent
        # close() would deadlock on that same lock. Killing the process group
        # closes the child's read end first, so the blocked write() raises and
        # releases the lock; only then can we close and join safely.
        if process.poll() is None:
            if process_group_grace_seconds == PROCESS_GROUP_TERMINATION_GRACE_SEC:
                _terminate_call_process(process)
            else:
                _terminate_call_process(process, grace_seconds=process_group_grace_seconds)
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        _join_io_threads()
        if overflow.is_set():
            stream = overflow_stream[0] or "stdout"
            raise RunnerLaunchError(
                f"call_{stream}_overflow",
                overflow_message[0],
                1,
            )
        raise RunnerLaunchError(
            "call_timeout",
            f"Child command exceeded timeout of {timeout} seconds.",
            1,
        )

    # A successful leader can leave grandchildren holding stdout/stderr open.
    # Kill the recorded group before joining drains so a daemon cannot make a
    # one-shot call wait for its natural lifetime.
    if process_group_grace_seconds == PROCESS_GROUP_TERMINATION_GRACE_SEC:
        _terminate_call_process(process)
    else:
        _terminate_call_process(process, grace_seconds=process_group_grace_seconds)
    _join_io_threads()
    return stdout_buf.getvalue(), stderr_buf.getvalue()


def _call_stderr_tail(data: bytes, sensitive_texts: tuple[str, ...]) -> str:
    tail = data.decode("utf-8", errors="replace")
    for value in sensitive_texts:
        if value:
            tail = tail.replace(value, "[REDACTED]")
    return redaction.redact_string(tail)[-profiles.STDERR_TAIL_LIMIT :]


def _claude_model_resolved(event: JsonObject) -> str | None:
    model_usage = event.get("modelUsage")
    if not isinstance(model_usage, dict):
        return None
    candidates: list[tuple[int, str]] = []
    for model, values in model_usage.items():
        if not isinstance(model, str) or not isinstance(values, dict):
            continue
        output_tokens = values.get("outputTokens")
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
            candidates.append((output_tokens, model))
    return max(candidates)[1] if candidates else None


def _claude_usage(event: JsonObject) -> JsonObject:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return {"basis": "unavailable"}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (input_tokens, output_tokens)
    ):
        return {"basis": "unavailable"}
    return {"inputTokens": input_tokens, "outputTokens": output_tokens, "basis": "exact"}


def _parse_claude_call_json(
    stdout_text: str, *, pure: bool
) -> tuple[str, int, tuple[str, ...], str | None, JsonObject, str | None, str | None]:
    try:
        events = json.loads(stdout_text)
    except json.JSONDecodeError:
        return "", 1, (), None, {"basis": "unavailable"}, "call_output_invalid", None
    if not isinstance(events, list):
        return "", 1, (), None, {"basis": "unavailable"}, "call_output_invalid", None
    result = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, dict) and event.get("type") == "result"
        ),
        None,
    )
    if not isinstance(result, dict) or not isinstance(result.get("result"), str):
        return "", 1, (), None, {"basis": "unavailable"}, "call_output_invalid", None
    denials = result.get("permission_denials")
    if pure:
        if not isinstance(denials, list):
            return (
                result["result"],
                1,
                (),
                _claude_model_resolved(result),
                _claude_usage(result),
                "pure_boundary_unverified",
                "Pure boundary unverified: permission_denials missing or malformed.",
            )
        if denials:
            return (
                result["result"],
                1,
                (),
                _claude_model_resolved(result),
                _claude_usage(result),
                "pure_boundary_violation",
                f"Pure boundary violation: {len(denials)} permission denial(s).",
            )
    exit_code = 1 if result.get("is_error") is True else 0
    return (
        result["result"],
        exit_code,
        (),
        _claude_model_resolved(result),
        _claude_usage(result),
        "child_failed" if exit_code else None,
        None,
    )


def _call_failure_details(
    exit_code: int,
    signal_text: str,
    *,
    error: str | None = None,
    message: str | None = None,
) -> tuple[str | None, str | None]:
    if exit_code == 0:
        return None, None
    if error not in {None, "child_failed"}:
        return error, message
    failure = child_failures.classify(signal_text)
    if failure is not None:
        return failure.code, failure.message
    return "child_failed", message


def _resolve_codex_auth_file(env: dict[str, str]) -> str:
    """Return the resolved real path to the codex auth.json credential.

    The auth file is resolved from the effective CODEX_HOME (or ~/.codex) so
    symlinked credentials outside the real home are copied into the
    ephemeral home correctly.
    """
    codex_home = env.get("CODEX_HOME")
    if not codex_home:
        home = env.get("HOME") or str(Path.home())
        codex_home = os.path.join(os.path.expanduser(home), ".codex")
    auth_file = os.path.join(os.path.expanduser(codex_home), "auth.json")
    real_auth_file = os.path.realpath(auth_file)
    if not os.path.isfile(real_auth_file):
        raise RunnerLaunchError(
            "codex_auth_unavailable",
            f"Codex pure call requires a readable auth.json; not found at {auth_file}.",
        )
    return real_auth_file


def _copy_auth(src: str, dst: str) -> None:
    """Copy *src* to *dst* as a private file. Never hardlink (avoids inode aliasing)."""
    shutil.copy2(src, dst)
    os.chmod(dst, 0o600)


def _execute_call_once(
    argv: list[str],
    cwd: str,
    *,
    harness: str,
    stdin_text: str | None = None,
    prompt_file_text: str | None = None,
    prompt_file_placeholder: str | None = None,
    agent_config_text: str | None = None,
    agent_config_placeholder: str | None = None,
    output_schema_text: str | None = None,
    output_schema_path: str | None = None,
    env_overrides: dict[str, str] | None = None,
    pure: bool = False,
    timeout: float | None = None,
    structured_output: bool = False,
    sensitive_texts: tuple[str, ...] = (),
    process_group_grace_seconds: float = PROCESS_GROUP_TERMINATION_GRACE_SEC,
) -> CallResult:
    """Run a one-shot stateless model call and return parsed assistant text."""
    if stdin_text is not None and prompt_file_text is not None:
        raise ValueError("stdin_text and prompt_file_text are mutually exclusive")
    launch_argv, prompt_temp_dir = _materialize_prompt_file_argv(
        argv,
        prompt_file_text=prompt_file_text,
        prompt_file_placeholder=prompt_file_placeholder,
        agent_config_text=agent_config_text,
        agent_config_placeholder=agent_config_placeholder,
    )
    launch_argv, schema_temp_dir = _materialize_output_schema_argv(
        launch_argv,
        output_schema_text=output_schema_text,
        output_schema_path=output_schema_path,
    )
    env = profiles.child_environment(overrides=env_overrides, pure=pure)
    started = time.monotonic()
    seatbelt_profile_path: str | None = None
    ephemeral_codex_home: str | None = None
    process: subprocess.Popen[bytes] | None = None
    process_pgid: int | None = None
    try:
        if harness == "codex" and pure:
            if not seatbelt.codex_pure_available():
                raise RunnerLaunchError(
                    "unsupported_pure_call",
                    "Codex pure call requires macOS with sandbox-exec available.",
                )
            real_auth_file = _resolve_codex_auth_file(env)
            ephemeral_codex_home = tempfile.mkdtemp(prefix="delegate-codex-pure-")
            os.chmod(ephemeral_codex_home, 0o700)
            ephemeral_auth_file = os.path.join(ephemeral_codex_home, "auth.json")
            _copy_auth(real_auth_file, ephemeral_auth_file)
            env["CODEX_HOME"] = ephemeral_codex_home

            extra_read_roots: list[str] = []
            resolved_auth_file = os.path.realpath(ephemeral_auth_file)
            resolved_ephemeral_home = os.path.realpath(ephemeral_codex_home)
            if not resolved_auth_file.startswith(resolved_ephemeral_home + os.sep):
                extra_read_roots.append(resolved_auth_file)
            if "--output-schema" in launch_argv:
                schema_index = launch_argv.index("--output-schema") + 1
                if schema_index < len(launch_argv):
                    extra_read_roots.append(launch_argv[schema_index])
            profile = seatbelt.build_codex_pure_profile(
                home=env.get("HOME", str(Path.home())),
                temp_cwd=cwd,
                codex_home=ephemeral_codex_home,
                extra_read_roots=extra_read_roots,
                env=env,
            )
            profile_fd, seatbelt_profile_path = tempfile.mkstemp(
                prefix="delegate-codex-pure-", suffix=".sb"
            )
            with os.fdopen(profile_fd, "w", encoding="utf-8") as profile_file:
                profile_file.write(profile)
            launch_argv = ["sandbox-exec", "-f", seatbelt_profile_path, *launch_argv]
        try:
            popen_kwargs: dict[str, object] = {
                "cwd": cwd,
                "env": env,
                "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "start_new_session": True,
            }
            process = subprocess.Popen(  # nosec B603 - Delegate intentionally launches validated harness argv with shell=False.
                launch_argv,
                **popen_kwargs,
            )
            process_pgid = _process_group_for_process(process)
            stdout_data, stderr_data = _bounded_call_communicate(
                process,
                stdin_text.encode("utf-8") if stdin_text is not None else None,
                timeout,
                CALL_STDOUT_MAX_BYTES,
                CALL_STDERR_MAX_BYTES,
                process_group_grace_seconds,
            )
        except OSError as exc:
            raise _runner_launch_error(launch_argv, cwd, exc) from exc
    finally:
        if process is not None:
            _terminate_call_process(
                process,
                pgid=process_pgid,
                grace_seconds=process_group_grace_seconds,
            )
        if seatbelt_profile_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(seatbelt_profile_path)
        if ephemeral_codex_home is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(ephemeral_codex_home, ignore_errors=True)
        _cleanup_prompt_file_dir(prompt_temp_dir)
        _cleanup_prompt_file_dir(schema_temp_dir)

    stdout_bytes = len(stdout_data or b"")
    stderr_bytes = len(stderr_data or b"")
    stdout_text = (stdout_data or b"").decode("utf-8", errors="replace")
    stderr_tail = _call_stderr_tail(stderr_data or b"", sensitive_texts)
    if harness == "claude" and (pure or structured_output):
        raw_text, parsed_exit, warnings, model_resolved, usage, error, message = (
            _parse_claude_call_json(stdout_text, pure=pure)
        )
        text = _bounded_call_fallback_text(raw_text)
        result_exit_code = parsed_exit if process.returncode == 0 else process.returncode
        failure_signal = stderr_tail
        if error == "child_failed":
            # Claude marks result text as a harness error channel only when
            # is_error=true. Successful result text remains model output.
            failure_signal = "\n".join(part for part in (stderr_tail, raw_text) if part)
        error, message = _call_failure_details(
            result_exit_code,
            failure_signal,
            error=error,
            message=message,
        )
        return CallResult(
            text=text,
            exit_code=result_exit_code,
            duration_ms=int((time.monotonic() - started) * MILLISECONDS_PER_SECOND),
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            text_chars=len(raw_text),
            text_truncated=len(raw_text) > harness_events.ASSISTANT_TEXT_LIMIT,
            stderr_tail=stderr_tail,
            warnings=warnings,
            error=error,
            message=message,
            model_resolved=model_resolved,
            usage=usage,
            result_quality=(
                RESULT_QUALITY_EMPTY
                if result_exit_code == 0 and not raw_text.strip()
                else RESULT_QUALITY_OK
            ),
        )
    accumulator = harness_events.StreamAccumulator(harness=harness)
    for line in stdout_text.splitlines():
        accumulator.ingest_line(line)
    if harness == "codex" and structured_output and accumulator.completion_text:
        raw_text = accumulator.completion_text
        text = _bounded_call_fallback_text(raw_text)
        meta: JsonObject = {
            "assistantTextChars": len(raw_text),
            "assistantTextTruncated": len(raw_text) > harness_events.ASSISTANT_TEXT_LIMIT,
        }
    else:
        text, meta = accumulator.bounded_assistant_text()
    warnings: tuple[str, ...] = ()
    if text:
        text_chars = int(meta.get("assistantTextChars", len(text)))
        text_truncated = bool(meta.get("assistantTextTruncated", False))
    elif accumulator.structured_events_seen > 0:
        warnings = (
            "Structured child stdout contained no assistant text; suppressed raw event output.",
        )
        text = ""
        text_chars = 0
        text_truncated = False
    else:
        # No structured assistant events parsed: fall back to raw stdout, bounded.
        raw = stdout_text.strip()
        text = _bounded_call_fallback_text(raw)
        text_chars = len(raw)
        text_truncated = len(raw) > harness_events.ASSISTANT_TEXT_LIMIT
    error, message = _call_failure_details(
        process.returncode,
        "\n".join(
            part for part in (stderr_tail, _accumulator_failure_signal_text(accumulator)) if part
        ),
    )
    return CallResult(
        text=text,
        exit_code=process.returncode,
        duration_ms=int((time.monotonic() - started) * MILLISECONDS_PER_SECOND),
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        text_chars=text_chars,
        text_truncated=text_truncated,
        stderr_tail=stderr_tail,
        warnings=warnings,
        error=error,
        message=message,
        usage=accumulator.usage or {"basis": "unavailable"},
        result_quality=(
            RESULT_QUALITY_NO_ASSISTANT_TEXT
            if process.returncode == 0
            and accumulator.structured_events_seen > 0
            and not text.strip()
            else RESULT_QUALITY_EMPTY
            if process.returncode == 0 and not text.strip()
            else RESULT_QUALITY_OK
        ),
    )


def _merge_call_attempts(first: CallResult, last: CallResult, label: str) -> CallResult:
    warnings = list(first.warnings)
    for warning in last.warnings:
        _append_unique(warnings, warning)
    stderr = f"{first.stderr_tail}\n--- {label} ---\n{last.stderr_tail}".strip()
    return replace(
        last,
        duration_ms=first.duration_ms + last.duration_ms,
        stdout_bytes=first.stdout_bytes + last.stdout_bytes,
        stderr_bytes=first.stderr_bytes + last.stderr_bytes,
        stderr_tail=stderr[-profiles.STDERR_TAIL_LIMIT :],
        warnings=tuple(warnings),
        text_truncated=first.text_truncated or last.text_truncated,
        usage=_aggregate_usage(first.usage, last.usage),
    )


def execute_call(
    argv: list[str],
    cwd: str,
    *,
    harness: str,
    stdin_text: str | None = None,
    prompt_file_text: str | None = None,
    prompt_file_placeholder: str | None = None,
    agent_config_text: str | None = None,
    agent_config_placeholder: str | None = None,
    output_schema_text: str | None = None,
    output_schema_path: str | None = None,
    env_overrides: dict[str, str] | None = None,
    read_only: bool = False,
    pure: bool = False,
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED,
    timeout: int | None = None,
    structured_output: bool = False,
    sensitive_texts: tuple[str, ...] = (),
    process_group_grace_seconds: float = PROCESS_GROUP_TERMINATION_GRACE_SEC,
) -> CallResult:
    deadline = None if timeout is None else time.monotonic() + timeout

    def call_once(
        call_argv: list[str],
        *,
        call_stdin_text: str | None = stdin_text,
        call_prompt_file_text: str | None = prompt_file_text,
    ) -> CallResult:
        return _execute_call_once(
            call_argv,
            cwd,
            harness=harness,
            stdin_text=call_stdin_text,
            prompt_file_text=call_prompt_file_text,
            prompt_file_placeholder=prompt_file_placeholder,
            agent_config_text=agent_config_text,
            agent_config_placeholder=agent_config_placeholder,
            output_schema_text=output_schema_text,
            output_schema_path=output_schema_path,
            env_overrides=env_overrides,
            pure=pure,
            timeout=None if deadline is None else max(deadline - time.monotonic(), 0),
            structured_output=structured_output,
            sensitive_texts=sensitive_texts,
            process_group_grace_seconds=process_group_grace_seconds,
        )

    result = call_once(argv)
    if read_only and harness == "codex" and result.error == "codex_thread_lost":
        retry = call_once(argv)
        result = _merge_call_attempts(result, retry, "codex thread retry")
        metadata: JsonObject = {
            "retryAttempted": True,
            "engaged": False,
            "resolved": retry.exit_code == 0,
        }
        if retry.error == "codex_thread_lost":
            fallback = call_once(_codex_ephemeral_fallback_argv(argv))
            result = _merge_call_attempts(result, fallback, "codex ephemeral fallback")
            warnings = list(result.warnings)
            _append_unique(
                warnings,
                "Codex thread lookup failed twice; ephemeral ignore-user-config fallback engaged.",
            )
            result = replace(result, warnings=tuple(warnings))
            metadata.update(engaged=True, resolved=fallback.exit_code == 0)
        result = replace(result, codex_thread_fallback=metadata)
    if result.result_quality != RESULT_QUALITY_EMPTY:
        return result
    if pure or prompt_instruction_mode == PROMPT_INSTRUCTION_MODE_SLASH:
        warnings = list(result.warnings)
        _append_unique(warnings, EMPTY_RETRY_SKIPPED_VERBATIM_WARNING)
        return replace(result, warnings=tuple(warnings))
    if not _empty_retry_allowed("call", call_read_only=read_only):
        warnings = list(result.warnings)
        _append_unique(warnings, EMPTY_RETRY_SKIPPED_WRITE_CAPABLE_WARNING)
        return replace(result, warnings=tuple(warnings))

    retry_argv = list(argv)
    retry_stdin = stdin_text
    retry_prompt_file = prompt_file_text
    if retry_stdin is not None:
        retry_stdin = _append_empty_retry_instruction(retry_stdin)
    elif retry_prompt_file is not None:
        retry_prompt_file = _append_empty_retry_instruction(retry_prompt_file)
    elif retry_argv:
        retry_argv[-1] = _append_empty_retry_instruction(retry_argv[-1])

    retry = call_once(
        retry_argv,
        call_stdin_text=retry_stdin,
        call_prompt_file_text=retry_prompt_file,
    )
    warnings = list(result.warnings)
    for warning in retry.warnings:
        _append_unique(warnings, warning)
    resolved = retry.exit_code == 0 and retry.result_quality == RESULT_QUALITY_OK
    if not resolved:
        _append_unique(warnings, EMPTY_RETRY_WARNING)
    stderr_parts = [part for part in (result.stderr_tail, retry.stderr_tail) if part]
    return replace(
        retry,
        duration_ms=result.duration_ms + retry.duration_ms,
        stdout_bytes=result.stdout_bytes + retry.stdout_bytes,
        stderr_bytes=result.stderr_bytes + retry.stderr_bytes,
        stderr_tail=("\n--- empty-success retry ---\n".join(stderr_parts))[
            -profiles.STDERR_TAIL_LIMIT :
        ],
        warnings=tuple(warnings),
        text_truncated=result.text_truncated or retry.text_truncated,
        usage=_aggregate_usage(result.usage, retry.usage),
        empty_retry_attempted=True,
        empty_retry_resolved=resolved,
    )


def execute_passthrough(
    argv: list[str],
    cwd: str,
    *,
    stdin_text: str | None = None,
    prompt_file_text: str | None = None,
    prompt_file_placeholder: str | None = None,
    agent_config_text: str | None = None,
    agent_config_placeholder: str | None = None,
    env_overrides: dict[str, str] | None = None,
    process_group_grace_seconds: float = PROCESS_GROUP_TERMINATION_GRACE_SEC,
) -> int:
    """Stream child stdout/stderr to the caller. JSON mode is not supported."""
    if stdin_text is not None and prompt_file_text is not None:
        raise ValueError("stdin_text and prompt_file_text are mutually exclusive")
    launch_argv, prompt_temp_dir = _materialize_prompt_file_argv(
        argv,
        prompt_file_text=prompt_file_text,
        prompt_file_placeholder=prompt_file_placeholder,
        agent_config_text=agent_config_text,
        agent_config_placeholder=agent_config_placeholder,
    )
    env = profiles.child_environment(overrides=env_overrides)
    process: subprocess.Popen[str] | None = None
    process_pgid: int | None = None
    try:
        # Passthrough mode mirrors the child runtime directly, so Delegate does
        # not impose a separate timeout here.
        try:
            process = subprocess.Popen(  # nosec B603 - passthrough intentionally mirrors validated harness argv with shell=False.
                launch_argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            process_pgid = _process_group_for_process(process)
            process.communicate(input=stdin_text)
        except OSError as exc:
            raise _runner_launch_error(launch_argv, cwd, exc) from exc
        return process.returncode
    finally:
        if process is not None:
            _terminate_call_process(
                process,
                pgid=process_pgid,
                grace_seconds=process_group_grace_seconds,
            )
        _cleanup_prompt_file_dir(prompt_temp_dir)
