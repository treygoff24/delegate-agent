from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from delegate_agent import archived_logs, record_io
from delegate_agent.harness_events import NO_OUTPUT_RESULT_QUALITIES
from delegate_agent.json_types import JsonObject, first_string
from delegate_agent.terminal_states import COMPLETED_UNVERIFIED, COMPLETED_VERIFIED, STALLED

LARGE_LOG_WARN_MIB = 50
# One MiB, shared as a value rather than an import from the mutation layer.
LARGE_LOG_WARN_BYTES = LARGE_LOG_WARN_MIB * (1 << 20)
DEFAULT_RUNS_LIMIT = 20
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED})
STATUS_FILTER_RUNNING = "running"
STATUS_FILTER_STALE = "stale"


def run_succeeded(
    status: str,
    result_quality: str | None,
    terminal_state: object = None,
) -> bool:
    """Did this run finish AND come back with usable work?

    ``status`` alone answers a narrower question -- whether the child harness ran
    to completion -- and callers that branch on it (or on the exit code derived
    from it) were being told "succeeded" for runs that produced nothing at all.
    The resultQuality taxonomy already detected those, but nothing connected the
    two, so the verdict could not see what the classifier knew.

    Only the qualities in NO_OUTPUT_RESULT_QUALITIES veto success: those record
    that no output existed, which cannot be a false positive. Heuristic judgments
    about the content of real output stay warnings.
    """
    if status != STATUS_SUCCEEDED:
        return False
    if terminal_state is not None and terminal_state not in {
        COMPLETED_VERIFIED,
        COMPLETED_UNVERIFIED,
    }:
        return False
    return result_quality not in NO_OUTPUT_RESULT_QUALITIES


_UNSET = object()


def large_log_warnings(stdout_bytes: int, stderr_bytes: int) -> list[str]:
    warnings: list[str] = []
    if stdout_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(f"{record_io.STDOUT_LOG} > {LARGE_LOG_WARN_MIB} MiB ({stdout_bytes} bytes)")
    if stderr_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(f"{record_io.STDERR_LOG} > {LARGE_LOG_WARN_MIB} MiB ({stderr_bytes} bytes)")
    return warnings


def process_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def effective_status(state: JsonObject | None) -> str:
    return status_fields(state)["effectiveStatus"]


def raw_status(state: JsonObject | None) -> str:
    if not state:
        return STATUS_UNKNOWN
    status = state.get("status")
    if not isinstance(status, str) or not status:
        return STATUS_UNKNOWN
    return status


def status_fields(state: JsonObject | None) -> JsonObject:
    # Probe pid liveness once so effectiveStatus and staleReason cannot disagree
    # when the process exits between two separate probes.
    raw = raw_status(state)
    effective = raw
    reason: str | None = None
    if raw == STATUS_RUNNING:
        pid = state.get("pid") if state else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            effective, reason = STATUS_STALE, "missing_pid"
        elif process_alive(pid) is False:
            effective, reason = STATUS_STALE, "dead_pid"
    fields: JsonObject = {
        "rawStatus": raw,
        "effectiveStatus": effective,
        "status": effective,
    }
    if reason is not None:
        fields["staleReason"] = reason
    if effective == STATUS_STALE:
        fields["terminalState"] = STALLED
    return fields


def stale_next_actions(alias_or_run_id: str, *, cwd: str | None = None) -> list[str]:
    return [
        record_io.snapshot_command(alias_or_run_id, cwd=cwd),
        record_io.run_output_command(alias_or_run_id, completion_report=True, cwd=cwd),
        f"{record_io.run_output_command(alias_or_run_id, cwd=cwd)} --stderr --tail 100",
    ]


def log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    run_path = record_io.run_directory(registry_root, run_id)
    stdout_bytes = 0
    stderr_bytes = 0
    stdout_path = run_path / record_io.STDOUT_LOG
    stderr_path = run_path / record_io.STDERR_LOG
    if stdout_path.exists():
        stdout_bytes = stdout_path.stat().st_size
    if stderr_path.exists():
        stderr_bytes = stderr_path.stat().st_size
    return stdout_bytes, stderr_bytes


def raw_logs_archived(registry_root: Path, run_id: str) -> bool:
    return archived_logs.archive_path(registry_root, run_id).exists()


def effective_log_byte_sizes(
    registry_root: Path,
    run_id: str,
    state: JsonObject | None = _UNSET,  # type: ignore[assignment]
) -> tuple[int, int]:
    run_path = record_io.run_directory(registry_root, run_id)
    stdout_path = run_path / record_io.STDOUT_LOG
    stderr_path = run_path / record_io.STDERR_LOG
    if stdout_path.exists() or stderr_path.exists():
        return log_byte_sizes(registry_root, run_id)
    if raw_logs_archived(registry_root, run_id):
        if state is _UNSET:
            state = record_io.load_run_state(registry_root, run_id)
        state_sizes = archived_logs.state_log_byte_sizes(state)
        if state_sizes is not None:
            return state_sizes
        return archived_logs.archive_log_byte_sizes(
            archived_logs.archive_path(registry_root, run_id),
            stdout_log=record_io.STDOUT_LOG,
            stderr_log=record_io.STDERR_LOG,
        )
    return 0, 0


def activity_timestamp(
    state: JsonObject | None,
    manifest: JsonObject | None,
    run_id: str | None = None,
) -> str:
    if state:
        for key in ("finishedAt", "lastActivityAt", "startedAt"):
            value = state.get(key)
            if isinstance(value, str) and value:
                return value
    if manifest:
        started = manifest.get("startedAt")
        if isinstance(started, str) and started:
            return started
    if run_id:
        return record_io.timestamp_from_run_id(run_id)
    return ""


def activity_datetime(
    state: JsonObject | None,
    manifest: JsonObject | None,
    run_id: str | None = None,
) -> datetime | None:
    return record_io.parse_utc_timestamp(activity_timestamp(state, manifest, run_id))


def build_run_summary(
    registry_root: Path,
    run_id: str,
    index_entry: JsonObject,
    *,
    include_logs: bool = True,
    state: JsonObject | object | None = _UNSET,
    manifest: JsonObject | object | None = _UNSET,
) -> JsonObject:
    if state is _UNSET:
        state = record_io.load_run_state_or_none(registry_root, run_id)
    if manifest is _UNSET:
        manifest = record_io.load_run_manifest_or_none(registry_root, run_id)
    assert state is None or isinstance(state, dict)
    assert manifest is None or isinstance(manifest, dict)
    source_cwd = _source_workspace(registry_root, index_entry, state, manifest)

    stdout_bytes, stderr_bytes = (
        effective_log_byte_sizes(registry_root, run_id, state) if include_logs else (0, 0)
    )
    alias = index_entry.get("alias")
    harness = index_entry.get("harness")
    handle = alias if isinstance(alias, str) else run_id
    # Merge state-persisted warnings into the runs-table warnings array the same
    # way snapshot_view does, deduping to avoid repeating the same warning across
    # channels.
    warnings: list[str] = list(large_log_warnings(stdout_bytes, stderr_bytes))
    for source in (manifest, state):
        if isinstance(source, dict):
            source_warnings = source.get("warnings")
            if not isinstance(source_warnings, list):
                continue
            for warning in source_warnings:
                if isinstance(warning, str) and warning not in warnings:
                    warnings.append(warning)
    summary: JsonObject = {
        "runId": run_id,
        "alias": alias if isinstance(alias, str) else None,
        "harness": harness if isinstance(harness, str) else None,
        "group": index_entry.get("group") if isinstance(index_entry.get("group"), str) else None,
        "workflowAgentKey": (
            index_entry.get("workflowAgentKey")
            if isinstance(index_entry.get("workflowAgentKey"), str)
            else None
        ),
        "mode": index_entry.get("mode") if isinstance(index_entry.get("mode"), str) else None,
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "warnings": warnings,
        "activityAt": activity_timestamp(state, manifest),
    }
    initiator_root = index_entry.get("initiatorRoot")
    if isinstance(initiator_root, str):
        summary["initiatorRoot"] = initiator_root
    if manifest:
        for key in (
            "modelAlias",
            "modelResolved",
            "continuityMode",
            "modelProvenance",
            "terminalEvent",
            "terminalStatus",
            "resumedFrom",
            "worktreeAttachment",
            "personaName",
            "personaSource",
            "personaTransport",
            "personaDigest",
            "personaFile",
        ):
            value = manifest.get(key)
            if value is not None:
                summary[key] = value
    if state:
        for key in (
            "terminalEvent",
            "terminalStatus",
            "terminalState",
            "terminalRecord",
            "continuityMode",
            "modelProvenance",
            "failureReason",
            "error",
            "message",
            "pgid",
            "completionReportWritten",
            "completionReportSource",
            "resultQuality",
        ):
            value = state.get(key)
            if value is not None:
                summary[key] = value
    summary.update(status_fields(state))
    if summary.get("effectiveStatus") == STATUS_STALE:
        summary["nextActions"] = stale_next_actions(handle, cwd=source_cwd)
    if state and isinstance(state.get("current"), str):
        summary["current"] = state["current"]
    if isinstance(alias, str):
        summary["snapshotCommand"] = record_io.snapshot_command(alias, cwd=source_cwd)

    # Isolation metadata: detect persistent worktree runs.
    worktree_status = None
    if state and isinstance(state.get("worktreeStatus"), str):
        worktree_status = state["worktreeStatus"]
        summary["worktreeStatus"] = worktree_status
        summary["isolationLifecycle"] = "persistent"
    if manifest:
        execution_cwd = manifest.get("executionCwd")
        if isinstance(execution_cwd, str) and execution_cwd:
            summary["executionCwd"] = execution_cwd
        source_git_root = manifest.get("sourceGitRoot")
        if isinstance(source_git_root, str) and source_git_root:
            summary["sourceGitRoot"] = source_git_root

    return summary


def _source_workspace(
    registry_root: Path,
    index_entry: JsonObject,
    state: JsonObject | None,
    manifest: JsonObject | None,
) -> str:
    return first_string(
        manifest.get("cwd") if manifest else None,
        state.get("cwd") if state else None,
        index_entry.get("cwd") if index_entry else None,
    ) or str(registry_root.parent)


def list_run_summaries(
    registry_root: Path,
    index: JsonObject,
    *,
    active: bool = False,
    status_filter: str | None = None,
    harness: str | None = None,
    group: str | None = None,
    limit: int = DEFAULT_RUNS_LIMIT,
) -> tuple[list[JsonObject], int, int]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    candidates: list[tuple[JsonObject, JsonObject | None, bool, JsonObject]] = []
    scope_total = 0
    for run_id, entry in record_io.index_run_entries(index):
        entry_harness = entry.get("harness")
        if harness is not None and entry_harness != harness:
            continue
        entry_group = entry.get("group")
        if group is not None and entry_group != group:
            continue
        scope_total += 1
        from delegate_agent import run_registry

        projected_state = run_registry.terminal_selection_state(registry_root, run_id, entry)
        state = (
            projected_state
            if projected_state is not None
            else record_io.load_run_state_or_none(registry_root, run_id)
        )
        needs_manifest = not any(
            isinstance(state, dict) and isinstance(state.get(key), str) and state.get(key)
            for key in ("finishedAt", "lastActivityAt", "startedAt")
        )
        manifest = (
            record_io.load_run_manifest_or_none(registry_root, run_id) if needs_manifest else None
        )
        summary = build_run_summary(
            registry_root,
            run_id,
            entry,
            include_logs=False,
            state=state,
            manifest=manifest,
        )
        status = summary.get("status")
        if active and status not in (STATUS_RUNNING, STATUS_STALE):
            continue
        if status_filter == STATUS_FILTER_RUNNING and status != STATUS_RUNNING:
            continue
        if status_filter == STATUS_FILTER_STALE and status != STATUS_STALE:
            continue
        candidates.append((summary, state, projected_state is not None, entry))
    candidates.sort(key=lambda item: item[0].get("activityAt", ""), reverse=True)
    total = len(candidates)
    selected: list[JsonObject] = []
    for summary, candidate_state, projected, entry in candidates[:limit]:
        full_state = (
            record_io.load_run_state_or_none(registry_root, summary["runId"])
            if projected
            else candidate_state
        )
        manifest = record_io.load_run_manifest_or_none(registry_root, summary["runId"])
        summary = build_run_summary(
            registry_root,
            summary["runId"],
            entry,
            include_logs=False,
            state=full_state,
            manifest=manifest,
        )
        stdout_bytes, stderr_bytes = effective_log_byte_sizes(
            registry_root,
            summary["runId"],
            full_state,
        )
        summary["stdoutBytes"] = stdout_bytes
        summary["stderrBytes"] = stderr_bytes
        warnings = large_log_warnings(stdout_bytes, stderr_bytes)
        for warning in summary["warnings"]:
            if warning not in warnings:
                warnings.append(warning)
        summary["warnings"] = warnings
        selected.append(summary)
    return selected, total, scope_total
