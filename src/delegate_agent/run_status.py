from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from delegate_agent import archived_logs
from delegate_agent.json_types import JsonObject

LARGE_LOG_WARN_MIB = 50
# 1 << 20 == 1 MiB == run_registry.BYTES_PER_MIB. Inlined so this module needs no
# import-time run_registry reference, which would otherwise create an import-order
# cycle (run_registry re-exports this module after defining BYTES_PER_MIB).
LARGE_LOG_WARN_BYTES = LARGE_LOG_WARN_MIB * (1 << 20)
DEFAULT_RUNS_LIMIT = 20
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED})
STATUS_FILTER_ACTIVE = "active"
STATUS_FILTER_RECENT = "recent"
STATUS_FILTER_RUNNING = "running"
STATUS_FILTER_STALE = "stale"


_UNSET = object()


def large_log_warnings(stdout_bytes: int, stderr_bytes: int) -> list[str]:
    warnings: list[str] = []
    if stdout_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(
            f"{run_registry.STDOUT_LOG} > {LARGE_LOG_WARN_MIB} MiB ({stdout_bytes} bytes)"
        )
    if stderr_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(
            f"{run_registry.STDERR_LOG} > {LARGE_LOG_WARN_MIB} MiB ({stderr_bytes} bytes)"
        )
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
    return fields


def stale_next_actions(alias_or_run_id: str, *, cwd: str | None = None) -> list[str]:
    return [
        run_registry.snapshot_command(alias_or_run_id, cwd=cwd),
        run_registry.run_output_command(alias_or_run_id, completion_report=True, cwd=cwd),
        f"{run_registry.run_output_command(alias_or_run_id, cwd=cwd)} --stderr --tail 100",
    ]


def log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    run_path = run_registry.run_directory(registry_root, run_id)
    stdout_bytes = 0
    stderr_bytes = 0
    stdout_path = run_path / run_registry.STDOUT_LOG
    stderr_path = run_path / run_registry.STDERR_LOG
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
    run_path = run_registry.run_directory(registry_root, run_id)
    stdout_path = run_path / run_registry.STDOUT_LOG
    stderr_path = run_path / run_registry.STDERR_LOG
    if stdout_path.exists() or stderr_path.exists():
        return log_byte_sizes(registry_root, run_id)
    if raw_logs_archived(registry_root, run_id):
        if state is _UNSET:
            state = run_registry.load_run_state(registry_root, run_id)
        state_sizes = archived_logs.state_log_byte_sizes(state)
        if state_sizes is not None:
            return state_sizes
        return archived_logs.archive_log_byte_sizes(
            archived_logs.archive_path(registry_root, run_id),
            stdout_log=run_registry.STDOUT_LOG,
            stderr_log=run_registry.STDERR_LOG,
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
        return run_registry.timestamp_from_run_id(run_id)
    return ""


def activity_datetime(
    state: JsonObject | None,
    manifest: JsonObject | None,
    run_id: str | None = None,
) -> datetime | None:
    return run_registry.parse_utc_timestamp(activity_timestamp(state, manifest, run_id))


def build_run_summary(
    registry_root: Path,
    run_id: str,
    index_entry: JsonObject,
) -> JsonObject:
    state = run_registry.load_run_state_or_none(registry_root, run_id)
    manifest = run_registry.load_run_manifest_or_none(registry_root, run_id)
    source_cwd = _source_workspace(registry_root, index_entry, state, manifest)

    stdout_bytes, stderr_bytes = effective_log_byte_sizes(registry_root, run_id, state)
    alias = index_entry.get("alias")
    harness = index_entry.get("harness")
    handle = alias if isinstance(alias, str) else run_id
    # Merge state-persisted warnings into the runs-table warnings array the same
    # way snapshot_view does, deduping to avoid repeating the same warning across
    # channels.
    warnings: list[str] = list(large_log_warnings(stdout_bytes, stderr_bytes))
    if isinstance(state, dict):
        state_warnings = state.get("warnings")
        if isinstance(state_warnings, list):
            for warning in state_warnings:
                if isinstance(warning, str) and warning not in warnings:
                    warnings.append(warning)
    summary: JsonObject = {
        "runId": run_id,
        "alias": alias if isinstance(alias, str) else None,
        "harness": harness if isinstance(harness, str) else None,
        "group": index_entry.get("group") if isinstance(index_entry.get("group"), str) else None,
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "warnings": warnings,
        "activityAt": activity_timestamp(state, manifest),
    }
    if manifest:
        for key in ("modelAlias", "modelResolved", "terminalEvent", "terminalStatus"):
            value = manifest.get(key)
            if value is not None:
                summary[key] = value
    if state:
        for key in (
            "terminalEvent",
            "terminalStatus",
            "failureReason",
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
        summary["snapshotCommand"] = run_registry.snapshot_command(alias, cwd=source_cwd)

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
    for source in (manifest, state, index_entry):
        if not source:
            continue
        cwd = source.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return str(registry_root.parent)


def list_run_summaries(
    registry_root: Path,
    index: JsonObject,
    *,
    active: bool = False,
    status_filter: str | None = None,
    harness: str | None = None,
    group: str | None = None,
    limit: int = DEFAULT_RUNS_LIMIT,
) -> list[JsonObject]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    summaries: list[JsonObject] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(entry, dict):
            continue
        entry_harness = entry.get("harness")
        if harness is not None and entry_harness != harness:
            continue
        entry_group = entry.get("group")
        if group is not None and entry_group != group:
            continue
        summary = build_run_summary(registry_root, run_id, entry)
        status = summary.get("status")
        if active and status not in (STATUS_RUNNING, STATUS_STALE):
            continue
        if status_filter == STATUS_FILTER_RUNNING and status != STATUS_RUNNING:
            continue
        if status_filter == STATUS_FILTER_STALE and status != STATUS_STALE:
            continue
        summaries.append(summary)
    summaries.sort(key=lambda item: item.get("activityAt", ""), reverse=True)
    return summaries[:limit]


# Deferred to the bottom to break the run_registry<->run_status facade cycle:
# run_registry re-exports this module's surface (a top-level import here would
# fail when run_status is imported first). Every `run_registry.<name>` access
# above is call-time, so binding the module after our own definitions suffices.
from delegate_agent import run_registry  # noqa: E402
