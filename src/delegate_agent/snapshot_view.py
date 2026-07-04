from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

from delegate_agent import retention as delegate_retention
from delegate_agent import run_metadata, run_registry
from delegate_agent.json_types import JsonObject, JsonValue, first_string
from delegate_agent.redaction import redact_value


class SnapshotView(TypedDict, total=False):
    schema: str
    ok: bool
    runId: str
    alias: str
    harness: str
    cwd: str
    executionCwd: str
    mode: str
    model: str
    status: str
    rawStatus: str
    effectiveStatus: str
    staleReason: str
    pid: int
    exitCode: int
    startedAt: str
    lastActivityAt: str
    finishedAt: str
    current: str
    error: str
    message: str
    plannedBranch: str | None
    plannedExecutionCwd: str | None
    stdoutBytes: int
    stderrBytes: int
    warnings: list[str]
    nextActions: list[str]
    assistantText: str
    assistantTextChars: int
    assistantTextTruncated: bool
    recentEvents: list[JsonObject]
    isolatedWorkspace: bool
    isolationMode: str
    effectiveIsolation: str
    isolationLifecycle: str
    preservedWorkspace: bool
    sourceGitRoot: str
    branch: str
    creationContext: JsonObject
    worktreeStatus: str
    safeWorkspaceMethod: str
    worktreeCleanupCommands: JsonObject
    requestedReasoningEffort: str
    resolvedReasoningEffort: str
    reasoningEffortSource: str
    reasoningCapabilitySource: str
    reasoningTransport: str
    snapshotCommand: str
    completionReport: JsonObject
    completionReportWritten: bool
    completionReportSource: str
    resultQuality: str


def _snapshot_value(snapshot: JsonObject | None, key: str) -> object:
    if snapshot is None:
        return None
    return snapshot.get(key)


def _write_status_contract(view: SnapshotView, state: JsonObject | None) -> str | None:
    status = run_registry.status_fields(state)
    raw_status = status.get("rawStatus")
    effective_status = status.get("effectiveStatus")
    display_status = status.get("status")
    stale_reason = status.get("staleReason")
    if isinstance(raw_status, str):
        view["rawStatus"] = raw_status
    if isinstance(effective_status, str):
        view["effectiveStatus"] = effective_status
    if isinstance(display_status, str):
        view["status"] = display_status
    if isinstance(stale_reason, str):
        view["staleReason"] = stale_reason
    return effective_status if isinstance(effective_status, str) else None


def _write_identity_contract(
    view: SnapshotView,
    *,
    run_id: str,
    snapshot: JsonObject | None,
    state: JsonObject | None,
    manifest: JsonObject | None,
) -> str | None:
    view["runId"] = run_id
    alias = first_string(
        _snapshot_value(snapshot, "alias"),
        manifest.get("alias") if manifest else None,
        state.get("alias") if state else None,
    )
    if alias is not None:
        view["alias"] = alias
    return alias


def _write_completion_report_contract(
    view: SnapshotView,
    snapshot: JsonObject | None,
) -> JsonObject | None:
    completion_report = _snapshot_value(snapshot, "completionReport")
    if isinstance(completion_report, dict):
        view["completionReport"] = completion_report
        return completion_report
    return None


def _source_workspace(
    registry_root: Path,
    snapshot: JsonObject | None,
    state: JsonObject | None,
    manifest: JsonObject | None,
) -> str:
    return first_string(
        manifest.get("cwd") if manifest else None,
        state.get("cwd") if state else None,
        _snapshot_value(snapshot, "cwd"),
        str(registry_root.parent),
    ) or str(registry_root.parent)


def merge_snapshot_view(
    registry_root: Path,
    run_id: str,
    snapshot: JsonObject | None,
    *,
    redact: bool,
) -> SnapshotView:
    state = run_registry.load_run_state(registry_root, run_id)
    manifest = run_registry.load_run_manifest(registry_root, run_id)
    stdout_bytes, stderr_bytes = delegate_retention.effective_log_byte_sizes(registry_root, run_id)
    view: SnapshotView = dict(snapshot or {})
    if not view:
        view = {
            "schema": run_registry.SNAPSHOT_SCHEMA,
            "ok": True,
        }
    view.setdefault("ok", True)
    alias = _write_identity_contract(
        view,
        run_id=run_id,
        snapshot=snapshot,
        state=state,
        manifest=manifest,
    )
    effective_status = _write_status_contract(view, state)
    completion_report = _write_completion_report_contract(view, snapshot)
    source_cwd = _source_workspace(registry_root, snapshot, state, manifest)
    view["stdoutBytes"] = stdout_bytes
    view["stderrBytes"] = stderr_bytes
    if state:
        for key in (
            "lastActivityAt",
            "current",
            "exitCode",
            "finishedAt",
            "completionReportWritten",
            "completionReportSource",
            "resultQuality",
        ):
            if key in state and key not in view:
                view[key] = state[key]
        # Surface pre-launch failure fields when status is "failed".
        if state.get("status") == "failed":
            for key in ("error", "message", "plannedBranch", "plannedExecutionCwd"):
                if key in state and key not in view:
                    view[key] = state[key]
        # Surface isolation metadata from state when present.
        for key in run_metadata.SNAPSHOT_STATE_FALLBACK_KEYS:
            if key in state and key not in view:
                view[key] = state[key]
    if manifest:
        for key in ("alias", "harness", "cwd", "executionCwd", "mode", "model", "startedAt"):
            if key in manifest and key not in view:
                view[key] = manifest[key]
        for key in run_metadata.SNAPSHOT_MANIFEST_FALLBACK_KEYS:
            if key in manifest and key not in view:
                view[key] = manifest[key]
    warnings = list(view.get("warnings") or [])
    for source in (state, manifest):
        if not source:
            continue
        source_warnings = source.get("warnings")
        if isinstance(source_warnings, list):
            for warning in source_warnings:
                if isinstance(warning, str) and warning not in warnings:
                    warnings.append(warning)
    for warning in run_registry.large_log_warnings(stdout_bytes, stderr_bytes):
        if warning not in warnings:
            warnings.append(warning)
    if delegate_retention.raw_logs_archived(registry_root, run_id):
        archive_warning = delegate_retention.archived_log_warning(
            alias if isinstance(alias, str) else None,
            run_id,
            cwd=source_cwd,
        )
        if archive_warning not in warnings:
            warnings.append(archive_warning)
    if warnings:
        view["warnings"] = warnings
    if isinstance(alias, str):
        view["snapshotCommand"] = run_registry.snapshot_command(alias, cwd=source_cwd)
        if effective_status == run_registry.STATUS_STALE:
            view["nextActions"] = run_registry.stale_next_actions(alias, cwd=source_cwd)
        if completion_report is not None:
            completion_report["command"] = run_registry.run_output_command(
                alias,
                completion_report=True,
                cwd=source_cwd,
            )
    if redact:
        view = cast(SnapshotView, redact_value(view))
    return view


def snapshot_json_payload(view: SnapshotView) -> JsonObject:
    return cast(dict[str, JsonValue], view)
