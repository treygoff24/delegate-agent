from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, TextIO

from delegate_agent import config as delegate_config
from delegate_agent import (
    harness_events,
    profiles,
    prompt_instructions,
    reasoning,
    redaction,
    rendering,
    run_metadata,
    run_registry,
    seatbelt,
    worktree_summary,
)
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_WRAPPED
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
CALL_STDOUT_MAX_BYTES = 16 * 1024 * 1024
CALL_STDERR_MAX_BYTES = 16 * 1024 * 1024
PROGRESS_INITIAL_DELAY_SEC = delegate_config.default_progress_initial_delay_sec()
PROGRESS_HEARTBEAT_INTERVAL_SEC = delegate_config.default_progress_interval_sec()
PROGRESS_INITIAL_DELAY_ENV = "DELEGATE_PROGRESS_INITIAL_DELAY_SEC"
PROGRESS_INTERVAL_ENV = "DELEGATE_PROGRESS_INTERVAL_SEC"
RESULT_QUALITY_OK = harness_events.RESULT_QUALITY_OK
RESULT_QUALITY_HOUSEKEEPING = harness_events.RESULT_QUALITY_HOUSEKEEPING
RESULT_QUALITY_EMPTY = harness_events.RESULT_QUALITY_EMPTY
RESULT_QUALITY_SUSPECT_SHORT = harness_events.RESULT_QUALITY_SUSPECT_SHORT
RESULT_QUALITY_NO_ASSISTANT_TEXT = harness_events.RESULT_QUALITY_NO_ASSISTANT_TEXT
COMPLETION_REPORT_SOURCE_CHILD = "child"
COMPLETION_REPORT_SOURCE_SYNTHESIZED = "delegate_synthesized"
COMPLETION_REPORT_SOURCE_STDOUT_RECOVERY = "stdout_recovery"
# Auth-failure detection matches against the REDACTED STDERR TAIL ONLY (not the
# normalized-events haystack), so a child report merely DISCUSSING a 401 in its
# events/report text cannot be misclassified as an auth failure. ``token_expired``
# and ``refresh token was revoked`` are unambiguous auth signals on their own; a
# bare ``401`` only counts when auth context appears nearby (unauthorized/token/
# auth within the same line), otherwise a 401 in unrelated output is ignored.
AUTH_FAILURE_PATTERNS = (
    re.compile(r"401[^\n]{0,80}(?:unauthorized|token|auth)", re.IGNORECASE),
    re.compile(r"token_expired", re.IGNORECASE),
    re.compile(r"refresh token was revoked", re.IGNORECASE),
)

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
    fast: bool | None = None
    prompt_transport: str = "argv"
    forbid_commit: bool = False
    progress_initial_delay_sec: float = PROGRESS_INITIAL_DELAY_SEC
    progress_interval_sec: float = PROGRESS_HEARTBEAT_INTERVAL_SEC
    env_overrides: dict[str, str] = field(default_factory=dict)
    fallback_env_overrides: dict[str, str] = field(default_factory=dict)
    auth_profile: str | None = None
    fallback_auth_profile: str | None = None
    include_dirty: bool = False
    synced_files: int = 0
    group: str | None = None
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED


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
    return run_registry.STATUS_SUCCEEDED if exit_code == 0 else run_registry.STATUS_FAILED


def _terminal_override_extra(accumulator: harness_events.StreamAccumulator) -> JsonObject:
    if accumulator.terminal_status not in {
        run_registry.STATUS_FAILED,
        run_registry.STATUS_CANCELLED,
    }:
        return {}
    extra: JsonObject = {
        "terminalStatus": accumulator.terminal_status,
        "failureReason": "harness_cancelled"
        if accumulator.terminal_status == run_registry.STATUS_CANCELLED
        else "harness_error",
    }
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
        "workspaceKind": ctx.workspace_kind,
        "startedAt": ctx.started_at,
        "argv": argv,
        "promptTransport": ctx.prompt_transport,
        "promptInstructionMode": ctx.prompt_instruction_mode,
    }
    run_metadata.add_run_metadata_payload_fields(payload, ctx)
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
    if ctx.include_dirty:
        payload["includeDirty"] = True
        payload["syncedFiles"] = ctx.synced_files
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
        state["current"] = current
    if pid is not None:
        state["pid"] = pid
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
        "mode": ctx.mode,
        "model": ctx.model,
        "startedAt": ctx.started_at,
        "current": accumulator.current,
        "recentEvents": recent_events,
        "completionReportWritten": completion_report_written,
        "completionReportSource": None,
        "resultQuality": RESULT_QUALITY_OK,
        **assistant_meta,
        **events_meta,
    }
    run_metadata.add_run_metadata_payload_fields(snapshot, ctx)
    reasoning.add_reasoning_payload_fields(snapshot, ctx)
    run_metadata.add_speed_payload_fields(snapshot, ctx)

    # Worktree cleanup commands for persistent worktrees.
    cleanup = _worktree_cleanup_commands(ctx)
    if cleanup is not None:
        snapshot["worktreeCleanupCommands"] = cleanup

    if exit_code is not None:
        snapshot["exitCode"] = exit_code
    if accumulator.terminal_event is not None:
        snapshot["terminalEvent"] = accumulator.terminal_event
    if accumulator.terminal_status is not None:
        snapshot["terminalStatus"] = accumulator.terminal_status
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
) -> str:
    """Persist terminal state with cancel-precedence reconciliation.

    Acquires the registry lock, re-reads the current persisted state, and if a
    concurrent ``cancel`` already wrote ``cancelled`` (or stamped the
    ``cancelRequested`` marker before signaling), preserves/adopts cancelled
    status and the ``cancelled_by_user`` failure reason instead of downgrading
    to the runner's exit-code-derived status. The runner's work summary/output
    metadata (exit_code, byte counts, completion report, result quality, etc.)
    is still recorded. Returns the status that was actually persisted.

    The ``cancelRequested`` marker handles the finalize-first race: cancel
    stamps the marker under the lock BEFORE sending SIGTERM, so if the child
    exits 0 on SIGTERM and the runner finalizes before cancel's post-grace
    terminal write, the finalizer still observes the marker and persists
    cancelled, keeping the live envelope (ok/status/exitCode) consistent with
    the eventual reconciled state.
    """
    persisted_status = status
    persisted_extra = dict(extra)
    with run_registry.registry_lock(ctx.registry_root):
        current = run_registry.load_run_state_or_none(ctx.registry_root, ctx.run_id)
        current_status = current.get("status") if isinstance(current, dict) else None
        cancel_requested = isinstance(current, dict) and current.get("cancelRequested") is True
        if current_status == run_registry.STATUS_CANCELLED or cancel_requested:
            # Cancel won the race (either it already wrote cancelled, or it
            # stamped the cancelRequested marker before signaling and the child
            # exited before cancel's post-grace terminal write). Do not
            # downgrade. Persist cancelled status and the cancel failure reason,
            # but still record the runner's work summary/output metadata.
            persisted_status = run_registry.STATUS_CANCELLED
            persisted_extra["failureReason"] = "cancelled_by_user"
            if exit_code == 0:
                # A cancelled run never reports success.
                persisted_extra["exitCode"] = 1
            else:
                persisted_extra["exitCode"] = exit_code
        write_state(
            run_path,
            build_state(
                ctx,
                status=persisted_status,
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                current=accumulator.current,
                pid=persisted_extra.get("pid"),
                extra=persisted_extra,
            ),
        )
        write_snapshot(
            run_path,
            build_snapshot(
                ctx,
                accumulator=accumulator,
                exit_code=exit_code,
                completion_report_written=completion_report_written,
                extra=persisted_extra,
            ),
        )
    return persisted_status


def write_completion_report(run_path: Path, text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    path = run_path / COMPLETION_REPORT_FILE
    run_registry.write_private_text(path, cleaned + "\n")
    return True


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


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


def _redacted_tail_from_bytes(data: bytes, *, limit: int = profiles.STDERR_TAIL_LIMIT) -> str:
    if limit <= 0:
        return ""
    return redaction.redact_string(data[-limit:].decode("utf-8", errors="replace"))


def _auth_failure_seen(stderr_tail: str, accumulator: harness_events.StreamAccumulator) -> bool:
    # Match against the redacted stderr tail ONLY. Inspecting the normalized
    # events haystack would misclassify a child report that merely DISCUSSES a
    # 401 (e.g. a review mentioning "the endpoint returned 401") as an auth
    # failure. The accumulator is accepted to keep the call site stable but is
    # intentionally not consulted here.
    del accumulator  # unused: auth signals come from stderr, not event text
    return any(pattern.search(stderr_tail) for pattern in AUTH_FAILURE_PATTERNS)


def _failure_reason(
    *,
    status: str,
    stderr_tail: str,
    accumulator: harness_events.StreamAccumulator,
    extra: JsonObject,
) -> str | None:
    existing = extra.get("failureReason")
    if isinstance(existing, str) and existing:
        return existing
    if status not in {run_registry.STATUS_FAILED, run_registry.STATUS_CANCELLED}:
        return None
    if _auth_failure_seen(stderr_tail, accumulator):
        return "auth_failed"
    if status == run_registry.STATUS_CANCELLED:
        return "harness_cancelled"
    return "child_failed"


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
    stderr_tail: str,
) -> tuple[str, str | None]:
    child_text = _completion_report_source(
        ctx,
        accumulator,
        completion_report_mode=completion_report_mode,
    ).strip()
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
        if failure_reason == "auth_failed":
            lines.append(_auth_remediation_line(ctx))
        if stderr_tail.strip():
            lines.extend(["", "Redacted stderr tail:", "```text", stderr_tail.rstrip(), "```"])
        lines.extend(["", "Next actions:", *(f"- {action}" for action in next_actions)])
        return "\n".join(lines), COMPLETION_REPORT_SOURCE_SYNTHESIZED
    if status == run_registry.STATUS_CANCELLED:
        # A cancelled run was killed mid-flight: a partial child message left in
        # stdout is NOT a valid completion report, so synthesize one regardless of
        # any recoverable assistant text. The failure reason is the cancel reason
        # already computed by _failure_reason (cancelled_by_user when cancel
        # requested/won the race, harness_cancelled when a harness terminal event
        # drove the cancellation). Same envelope fields as the failed path.
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
    if ctx.include_dirty:
        payload["includeDirty"] = True
        payload["syncedFiles"] = ctx.synced_files
    payload["promptInstructionMode"] = ctx.prompt_instruction_mode
    run_metadata.add_run_metadata_payload_fields(payload, ctx)
    reasoning.add_reasoning_payload_fields(payload, ctx)
    run_metadata.add_speed_payload_fields(payload, ctx)
    if assistant_meta is not None:
        payload.update(assistant_meta)

    # Worktree cleanup commands for persistent worktrees.
    cleanup = _worktree_cleanup_commands(ctx)
    if cleanup is not None:
        payload["worktreeCleanupCommands"] = cleanup
    if extra is not None:
        _merge_extra(payload, extra)

    if not ok:
        if extra is not None and extra.get("commitPolicyCausedFailure") is True:
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
    agent_config_text: str | None = None,
    agent_config_placeholder: str | None = None,
    agent_config_dir: Path | None = None,
) -> tuple[list[str], Path | None]:
    if prompt_file_text is None and agent_config_text is None:
        return list(argv), None
    temp_dir: Path | None = None
    replacements: dict[str, str] = {}
    if prompt_file_text is not None:
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
        replacements[prompt_file_placeholder] = str(prompt_path)
    if agent_config_text is not None:
        if agent_config_placeholder is None or agent_config_placeholder not in argv:
            raise ValueError("agent_config_placeholder must be present in argv")
        if agent_config_dir is None:
            if temp_dir is None:
                temp_dir = Path(tempfile.mkdtemp(prefix="delegate-prompt-"))
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
    return [replacements.get(item, item) for item in argv], temp_dir


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
        if summary.get("noChanges") is True:
            warnings = list(extra.get("warnings") or [])
            _append_unique(
                warnings,
                "Work-mode run completed with no file changes or commits detected.",
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


def _launch_tracked_process(
    argv: list[str],
    cwd: str,
    *,
    stdin_text: str | None,
    env_overrides: dict[str, str] | None = None,
    scratch_dir: Path | None = None,
) -> subprocess.Popen[bytes]:
    env = profiles.child_environment(
        overrides=_env_overrides_with_scratch(env_overrides, scratch_dir)
    )
    return subprocess.Popen(  # nosec B603 - Delegate intentionally launches validated harness argv with shell=False.
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
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
    # A launch failure never ran the child, so there is no result to classify.
    # resultQuality is set to None explicitly so build_state omits the key,
    # keeping state consistent with the snapshot below (which also omits it).
    extra: JsonObject = {
        "error": error.error,
        "message": error.message,
        "resultQuality": None,
    }
    write_state(files.run_path, build_state(ctx, status="failed", extra=extra))
    snapshot = build_snapshot(
        ctx, accumulator=harness_events.StreamAccumulator(harness=ctx.harness)
    )
    snapshot["ok"] = False
    snapshot["status"] = "failed"
    # The child never ran, so there is no result quality to report. Remove the
    # snapshot's default "ok" verdict so launch-failure state and snapshot agree.
    snapshot.pop("resultQuality", None)
    snapshot.update({"error": error.error, "message": error.message})
    write_snapshot(files.run_path, snapshot)


def _capture_tracked_process(
    process: subprocess.Popen[bytes],
    files: TrackedRunFiles,
    ctx: RunContext,
    *,
    started: float,
    stdin_text: str | None,
    progress_stderr: TextIO | None = None,
    progress_initial_delay_sec: float = PROGRESS_INITIAL_DELAY_SEC,
    progress_interval_sec: float = PROGRESS_HEARTBEAT_INTERVAL_SEC,
) -> TrackedCaptureResult:
    accumulator = harness_events.StreamAccumulator(harness=ctx.harness)
    pgid = None
    with contextlib.suppress(OSError):
        pgid = os.getpgid(process.pid)
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
            emit_progress = True
            try:
                _emit_progress_started(ctx, progress_stderr)
            except (BrokenPipeError, OSError):
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
            while True:
                if not emit_progress:
                    exit_code = process.wait()
                    break
                timeout = max(next_progress_at - time.monotonic(), 0.01)
                try:
                    exit_code = process.wait(timeout=timeout)
                    break
                except subprocess.TimeoutExpired:
                    try:
                        _emit_progress_heartbeat(
                            ctx,
                            accumulator,
                            started=started,
                            stderr=progress_stderr,
                        )
                    except (BrokenPipeError, OSError):
                        emit_progress = False
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
        pid=process.pid,
        pgid=pgid,
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
    extra: JsonObject | None = None,
) -> TrackedFinalization:
    exit_code, merged_extra = _final_extra(ctx, capture.exit_code)
    if extra:
        merged_extra = {**merged_extra, **extra}
    merged_extra = {**merged_extra, "pid": capture.pid}
    if capture.pgid is not None:
        merged_extra["pgid"] = capture.pgid
    terminal_extra = _terminal_override_extra(capture.accumulator)
    if terminal_extra:
        merged_extra = {**merged_extra, **terminal_extra}
    status = status_from_exit(exit_code)
    if capture.accumulator.terminal_status in {
        run_registry.STATUS_FAILED,
        run_registry.STATUS_CANCELLED,
    }:
        status = capture.accumulator.terminal_status
        if exit_code == 0:
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
    if cancel_requested and status != run_registry.STATUS_CANCELLED:
        status = run_registry.STATUS_CANCELLED
        if exit_code == 0:
            exit_code = 1
        merged_extra["failureReason"] = "cancelled_by_user"
    stderr_tail = profiles.read_bounded_stderr_tail(files.stderr_log)
    failure_reason = _failure_reason(
        status=status,
        stderr_tail=stderr_tail,
        accumulator=capture.accumulator,
        extra=merged_extra,
    )
    if failure_reason is not None:
        merged_extra["failureReason"] = failure_reason
        if failure_reason == "auth_failed":
            merged_extra["nextActions"] = _auth_remediation_actions(ctx)
    report_text, report_source = _completion_report_text_and_source(
        ctx,
        capture.accumulator,
        completion_report_mode=completion_report_mode,
        status=status,
        failure_reason=failure_reason,
        stderr_tail=stderr_tail,
    )
    report_written = write_completion_report(files.run_path, report_text)
    result_quality = _classify_result_quality(
        ctx=ctx,
        exit_code=exit_code,
        report_text=report_text,
        report_written=report_written,
        report_source=report_source,
        accumulator=capture.accumulator,
    )
    merged_extra["completionReportWritten"] = report_written
    merged_extra["completionReportSource"] = report_source if report_written else None
    merged_extra["resultQuality"] = result_quality
    if result_quality != RESULT_QUALITY_OK:
        warnings = list(merged_extra.get("warnings") or [])
        _append_unique(warnings, _quality_warning(result_quality, harness=ctx.harness))
        merged_extra["warnings"] = warnings
    persisted_status = _persist_final_progress(
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
    # Cancel-precedence reconciliation: when the finalizer preserved a
    # concurrent cancel (persisted_status is cancelled but the runner's own
    # exit-code-derived status was not cancelled), the LIVE result returned to
    # the caller must agree with the persisted state. A cancelled run never
    # reports success, so normalize exit_code to 1, status to cancelled, and
    # the failure reason to cancelled_by_user. This keeps the CLI envelope
    # (ok/status/exitCode), the process exit code, and state.json in lockstep.
    if (
        persisted_status == run_registry.STATUS_CANCELLED
        and status != run_registry.STATUS_CANCELLED
    ):
        final_extra = dict(merged_extra)
        final_extra["failureReason"] = "cancelled_by_user"
        final_extra["exitCode"] = 1
        return TrackedFinalization(
            status=run_registry.STATUS_CANCELLED,
            exit_code=1,
            report_written=report_written,
            extra=final_extra,
        )
    return TrackedFinalization(
        status=persisted_status,
        exit_code=exit_code,
        report_written=report_written,
        extra=merged_extra,
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


def _append_attempt_delimiter(stderr_log: Path, *, label: str) -> None:
    marker = f"\n--- delegate codex auth attempt: {label} ---\n"
    with stderr_log.open("ab") as handle:
        handle.write(marker.encode("utf-8"))


def _should_retry_profiles(
    ctx: RunContext,
    capture: TrackedCaptureResult,
    *,
    cwd: str,
    stderr_log: Path,
    workspace_baseline: profiles.WorkspaceBaseline | None,
) -> bool:
    if ctx.engine != "codex" or not ctx.fallback_env_overrides:
        return False
    if capture.exit_code == 0:
        return False
    stderr_tail = profiles.read_bounded_stderr_tail(stderr_log)
    if not profiles.classify_codex_usage_limit(stderr_tail):
        return False
    if profiles.accumulator_had_tool_events(capture.accumulator):
        return False
    return ctx.mode != "work" or profiles.work_mode_safe_for_codex_fallback(cwd, workspace_baseline)


def _run_single_tracked_attempt(
    argv: list[str],
    cwd: str,
    files: TrackedRunFiles,
    ctx: RunContext,
    *,
    started: float,
    stdin_text: str | None,
    env_overrides: dict[str, str] | None,
    scratch_dir: Path | None,
    progress: bool,
    progress_stderr: TextIO | None,
    progress_initial_delay_sec: float,
    progress_interval_sec: float,
    attempt_label: str | None = None,
) -> TrackedCaptureResult:
    if attempt_label is not None:
        _append_attempt_delimiter(files.stderr_log, label=attempt_label)
    process = _launch_tracked_process(
        argv,
        cwd,
        stdin_text=stdin_text,
        env_overrides=env_overrides,
        scratch_dir=scratch_dir,
    )
    return _capture_tracked_process(
        process,
        files,
        ctx,
        started=started,
        stdin_text=stdin_text,
        progress_stderr=progress_stderr,
        progress_initial_delay_sec=progress_initial_delay_sec,
        progress_interval_sec=progress_interval_sec,
    )


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
    manifest_argv: list[str] | None = None,
    progress: bool = False,
    progress_initial_delay_sec: float = PROGRESS_INITIAL_DELAY_SEC,
    progress_interval_sec: float = PROGRESS_HEARTBEAT_INTERVAL_SEC,
) -> tuple[int, JsonObject | None]:
    if stdin_text is not None and prompt_file_text is not None:
        raise ValueError("stdin_text and prompt_file_text are mutually exclusive")
    files = _prepare_tracked_run(argv, ctx, manifest_argv=manifest_argv)
    started = time.monotonic()
    run_argv = _codex_argv_with_scratch(argv, files.scratch_dir) if ctx.engine == "codex" else argv
    run_manifest_argv = (
        _codex_argv_with_scratch(manifest_argv, files.scratch_dir)
        if ctx.engine == "codex" and manifest_argv is not None
        else manifest_argv
    )
    if ctx.engine == "codex" and files.scratch_dir is not None:
        write_manifest(files.run_path, build_manifest(ctx, run_manifest_argv or run_argv))
    launch_argv, prompt_temp_dir = _materialize_prompt_file_argv(
        run_argv,
        prompt_file_text=prompt_file_text,
        prompt_file_placeholder=prompt_file_placeholder,
        agent_config_text=agent_config_text,
        agent_config_placeholder=agent_config_placeholder,
        agent_config_dir=files.run_path,
    )
    workspace_baseline = profiles.capture_workspace_baseline(cwd) if ctx.mode == "work" else None
    fallback_extra: JsonObject | None = None
    try:
        try:
            capture = _run_single_tracked_attempt(
                launch_argv,
                cwd,
                files,
                ctx,
                started=started,
                stdin_text=stdin_text,
                env_overrides=ctx.env_overrides or None,
                scratch_dir=files.scratch_dir,
                progress=progress,
                progress_stderr=stderr if progress else None,
                progress_initial_delay_sec=progress_initial_delay_sec,
                progress_interval_sec=progress_interval_sec,
                attempt_label="primary"
                if (ctx.engine == "codex" and ctx.fallback_env_overrides)
                else None,
            )
        except OSError as exc:
            error = _runner_launch_error(launch_argv, cwd, exc)
            _record_tracked_launch_failure(files, ctx, error)
            raise error from exc

        if _should_retry_profiles(
            ctx,
            capture,
            cwd=cwd,
            stderr_log=files.stderr_log,
            workspace_baseline=workspace_baseline,
        ):
            primary_exit_code = capture.exit_code
            primary_stderr_tail = profiles.read_bounded_stderr_tail(files.stderr_log)
            fallback_capture = _run_single_tracked_attempt(
                launch_argv,
                cwd,
                files,
                ctx,
                started=started,
                stdin_text=stdin_text,
                env_overrides=ctx.fallback_env_overrides,
                scratch_dir=files.scratch_dir,
                progress=progress,
                progress_stderr=stderr if progress else None,
                progress_initial_delay_sec=progress_initial_delay_sec,
                progress_interval_sec=progress_interval_sec,
                attempt_label="fallback",
            )
            capture = fallback_capture
            fallback_extra = {
                "codexAuthFallback": profiles.codex_auth_fallback_metadata(
                    reason="usage_limit",
                    primary_auth_profile=ctx.auth_profile,
                    fallback_auth_profile=ctx.fallback_auth_profile,
                    primary_exit_code=primary_exit_code,
                    fallback_exit_code=fallback_capture.exit_code,
                    primary_stderr_tail=primary_stderr_tail,
                )
            }
    finally:
        _cleanup_prompt_file_dir(prompt_temp_dir)

    ctx = _ctx_with_stdin_warnings(ctx, capture.stdin_failures, stderr)
    finalization = _finalize_tracked_run(
        files,
        ctx,
        capture,
        completion_report_mode=completion_report_mode,
        extra=fallback_extra,
    )
    return _tracked_result(ctx, capture, finalization, json_mode=json_mode, stdout=stdout)


def _kill_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


def _terminate_call_process(process: subprocess.Popen[bytes]) -> None:
    """Send SIGTERM, wait for graceful exit, then SIGKILL if needed."""
    _kill_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_process_group(process, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def _bounded_call_communicate(
    process: subprocess.Popen[bytes],
    stdin_bytes: bytes | None,
    timeout: int | None,
    max_stdout: int,
    max_stderr: int,
) -> tuple[bytes, bytes]:
    """Read child stdout/stderr under fixed byte caps; kill on overflow or timeout."""
    stdout_buf = io.BytesIO()
    stderr_buf = io.BytesIO()
    overflow = threading.Event()
    overflow_message: list[str] = [""]

    def _drain_to_buffer(
        pipe: BinaryIO,
        buf: io.BytesIO,
        limit: int,
        message: str,
    ) -> None:
        try:
            while not overflow.is_set():
                chunk = pipe.read(65536)
                if not chunk:
                    break
                available = limit - buf.tell()
                if available <= 0:
                    overflow_message[0] = message
                    overflow.set()
                    break
                if len(chunk) > available:
                    buf.write(chunk[:available])
                    overflow_message[0] = message
                    overflow.set()
                    break
                buf.write(chunk)
        finally:
            with contextlib.suppress(OSError):
                pipe.close()

    stdout_thread = threading.Thread(
        target=_drain_to_buffer,
        args=(
            process.stdout,
            stdout_buf,
            max_stdout,
            f"Child call stdout exceeded the maximum allowed size of {max_stdout} bytes.",
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
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    if stdin_bytes is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin_bytes)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()

    start = time.monotonic()
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

    if overflow.is_set() or (timeout is not None and process.poll() is None):
        if process.poll() is None:
            _terminate_call_process(process)
        stdout_thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
        stderr_thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
        if overflow.is_set():
            raise RunnerLaunchError(
                "call_stdout_overflow",
                overflow_message[0],
                1,
            )
        raise RunnerLaunchError(
            "call_timeout",
            f"Child call exceeded timeout of {timeout} seconds.",
            1,
        )

    stdout_thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
    stderr_thread.join(timeout=DRAIN_JOIN_TIMEOUT_SEC)
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
    if pure and isinstance(denials, list) and denials:
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


def _resolve_codex_auth_file(env: dict[str, str]) -> str:
    """Return the resolved real path to the codex auth.json credential.

    The auth file is resolved from the effective CODEX_HOME (or ~/.codex) so
    symlinked credentials outside the real home are copied/hardlinked into the
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


def _copy_or_link_auth(src: str, dst: str) -> None:
    """Copy *src* to *dst*, preferring a hardlink when the filesystem permits."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    os.chmod(dst, 0o600)


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
    env_overrides: dict[str, str] | None = None,
    pure: bool = False,
    timeout: int | None = None,
    structured_output: bool = False,
    sensitive_texts: tuple[str, ...] = (),
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
    env = profiles.child_environment(overrides=env_overrides, pure=pure)
    started = time.monotonic()
    seatbelt_profile_path: str | None = None
    ephemeral_codex_home: str | None = None
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
            _copy_or_link_auth(real_auth_file, ephemeral_auth_file)
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
            stdout_data, stderr_data = _bounded_call_communicate(
                process,
                stdin_text.encode("utf-8") if stdin_text is not None else None,
                timeout,
                CALL_STDOUT_MAX_BYTES,
                CALL_STDERR_MAX_BYTES,
            )
        except OSError as exc:
            raise _runner_launch_error(launch_argv, cwd, exc) from exc
    finally:
        if seatbelt_profile_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(seatbelt_profile_path)
        if ephemeral_codex_home is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(ephemeral_codex_home, ignore_errors=True)
        _cleanup_prompt_file_dir(prompt_temp_dir)

    stdout_bytes = len(stdout_data or b"")
    stderr_bytes = len(stderr_data or b"")
    stdout_text = (stdout_data or b"").decode("utf-8", errors="replace")
    stderr_tail = _call_stderr_tail(stderr_data or b"", sensitive_texts)
    if harness == "claude" and (pure or structured_output):
        raw_text, parsed_exit, warnings, model_resolved, usage, error, message = (
            _parse_claude_call_json(stdout_text, pure=pure)
        )
        text = _bounded_call_fallback_text(raw_text)
        return CallResult(
            text=text,
            exit_code=parsed_exit if process.returncode == 0 else process.returncode,
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
    try:
        # Passthrough mode mirrors the child runtime directly, so Delegate does
        # not impose a separate timeout here.
        try:
            if stdin_text is None:
                completed = subprocess.run(  # nosec B603 - passthrough intentionally mirrors validated harness argv with shell=False.
                    launch_argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
            else:
                completed = subprocess.run(  # nosec B603 - passthrough intentionally mirrors validated harness argv with shell=False.
                    launch_argv,
                    cwd=cwd,
                    env=env,
                    input=stdin_text,
                    text=True,
                    check=False,
                )
        except OSError as exc:
            raise _runner_launch_error(launch_argv, cwd, exc) from exc
        return completed.returncode
    finally:
        _cleanup_prompt_file_dir(prompt_temp_dir)
