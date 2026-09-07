from __future__ import annotations

from pathlib import Path

from delegate_agent import retention as delegate_retention
from delegate_agent import run_metadata, run_registry
from delegate_agent.json_types import JsonObject, first_string
from delegate_agent.redaction import redact_value

SnapshotView = JsonObject


def merge_snapshot_view(
    registry_root: Path,
    run_id: str,
    snapshot: JsonObject | None,
    *,
    redact: bool,
    state: JsonObject | None = None,
) -> JsonObject:
    if state is None:
        state = run_registry.load_run_state(registry_root, run_id)
    manifest = run_registry.load_run_manifest(registry_root, run_id)
    stdout_bytes, stderr_bytes = delegate_retention.effective_log_byte_sizes(registry_root, run_id)
    view: JsonObject = dict(snapshot or {})
    if state:
        view.update(state)
    if not view:
        view = {
            "schema": run_registry.SNAPSHOT_SCHEMA,
            "ok": True,
        }
    view.setdefault("ok", True)
    view["runId"] = run_id
    alias = first_string(
        state.get("alias") if state else None,
        view.get("alias"),
        manifest.get("alias") if manifest else None,
    )
    if alias is not None:
        view["alias"] = alias
    status = run_registry.status_fields(state)
    view.update(status)
    effective_status = status.get("effectiveStatus")
    completion_report = view.get("completionReport")
    completion_report = completion_report if isinstance(completion_report, dict) else None
    source_cwd = first_string(
        manifest.get("cwd") if manifest else None,
        state.get("cwd") if state else None,
        view.get("cwd"),
        str(registry_root.parent),
    ) or str(registry_root.parent)
    view["stdoutBytes"] = stdout_bytes
    view["stderrBytes"] = stderr_bytes
    if manifest:
        for key in (
            "alias",
            "harness",
            "cwd",
            "executionCwd",
            "mode",
            "model",
            "startedAt",
            "workflowAgentKey",
            "isolatedWorkspace",
        ):
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
    view["schema"] = run_registry.SNAPSHOT_SCHEMA
    if redact:
        view = redact_value(view)
    return view


def snapshot_json_payload(view: JsonObject) -> JsonObject:
    return view
