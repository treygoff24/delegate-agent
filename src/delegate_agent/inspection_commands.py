from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import command_errors, redaction, run_registry, snapshot_view
from delegate_agent import rendering as delegate_rendering
from delegate_agent.json_types import JsonObject

STRUCTURAL_RUN_KEYS = (
    "runId",
    "alias",
    "harness",
    "group",
    "mode",
    "modelAlias",
    "modelResolved",
    "rawStatus",
    "effectiveStatus",
    "status",
    "terminalStatus",
    "activityAt",
    "initiatorRoot",
)


@dataclass(frozen=True)
class SnapshotCommand:
    handle: str | None
    latest_harness: str | None = None
    no_redact: bool = False
    json_mode: bool = False


@dataclass(frozen=True)
class RunsCommand:
    action: str | None = None
    active: bool = False
    running: bool = False
    stale: bool = False
    harness: str | None = None
    group: str | None = None
    limit: int | None = None
    older_than_days: int | None = None
    dry_run: bool = False
    structural: bool = False
    json_mode: bool = False


class InspectionError(command_errors.CommandError):
    pass


def emit_snapshot(command: SnapshotCommand, *, workspace_path: str, stdout: TextIO) -> int:
    workspace = Path(workspace_path)
    registry_root = run_registry.registry_root_if_exists(workspace)
    if registry_root is None:
        if command.latest_harness is None and command.handle is None:
            raise InspectionError("missing_handle", "snapshot requires a run handle or --latest.")
        registry_root = run_registry.registry_root(workspace)
    target = run_registry.resolve_run_target(
        registry_root,
        handle=command.handle,
        latest_harness=command.latest_harness,
    )
    if isinstance(target, run_registry.RunTargetLookupError):
        raise InspectionError(target.error, target.message)
    run_id = target.run_id
    snapshot = run_registry.load_run_snapshot(registry_root, run_id)
    view = snapshot_view.merge_snapshot_view(
        registry_root,
        run_id,
        snapshot,
        redact=not command.no_redact,
    )
    run_registry.add_run_target_resolution(view, target)
    if command.json_mode:
        delegate_rendering.print_json(snapshot_view.snapshot_json_payload(view), stdout)
    else:
        delegate_rendering.render_snapshot_text(view, stdout)
    return 0


def emit_runs(command: RunsCommand, *, workspace_path: str, stdout: TextIO) -> int:
    registry_root = run_registry.registry_root_if_exists(Path(workspace_path))
    if command.action == "prune":
        older_than_days = (
            command.older_than_days
            if command.older_than_days is not None
            else run_registry.DEFAULT_RUN_PRUNE_DAYS
        )
        payload = (
            run_registry.prune_runs(
                registry_root,
                older_than_days=older_than_days,
                dry_run=command.dry_run,
            )
            if registry_root is not None
            else run_registry.empty_run_prune_payload(
                older_than_days=older_than_days,
                dry_run=command.dry_run,
            )
        )
        if command.json_mode:
            delegate_rendering.print_json(payload, stdout)
        else:
            delegate_rendering.render_runs_prune_text(payload, stdout)
        if payload.get("ok") is False:
            exit_code = payload.get("exitCode")
            return exit_code if isinstance(exit_code, int) else 1
        return 0
    if command.action is not None:
        raise InspectionError("unknown_runs_action", f"Unknown runs action: {command.action}")
    limit = command.limit or run_registry.DEFAULT_RUNS_LIMIT
    if command.running:
        mode = "running"
        status_filter = run_registry.STATUS_FILTER_RUNNING
    elif command.stale:
        mode = "stale"
        status_filter = run_registry.STATUS_FILTER_STALE
    elif command.active:
        mode = "active"
        status_filter = None
    else:
        mode = "recent"
        status_filter = None
    if registry_root is None:
        summaries: list[JsonObject] = []
        total = 0
    else:
        index = run_registry.load_index(registry_root)
        summaries, total = run_registry.list_run_summaries(
            registry_root,
            index,
            active=command.active,
            status_filter=status_filter,
            harness=command.harness,
            group=command.group,
            limit=limit,
        )
    if command.structural:
        summaries = [
            redaction.redact_value(
                {key: summary[key] for key in STRUCTURAL_RUN_KEYS if key in summary}
            )
            for summary in summaries
        ]
    else:
        summaries = [redaction.redact_value(summary) for summary in summaries]
    warnings: list[str] = []
    if not summaries and (command.group is not None or command.harness is not None):
        if command.running:
            warnings.append("No running runs matched. Drop --running to include terminal runs.")
        elif command.stale:
            warnings.append("No stale runs matched. Drop --stale to include terminal runs.")
        elif command.active:
            warnings.append("No active runs matched. Drop --active to include terminal runs.")
        else:
            warnings.append(
                "No matching runs in this workspace Registry. "
                "The run Registry is workspace-scoped; use --cwd PATH to target another "
                "workspace's Registry."
            )
    if command.json_mode:
        delegate_rendering.print_json(
            delegate_rendering.runs_json_payload(
                summaries,
                limit=limit,
                mode=mode,
                total=total,
                warnings=warnings or None,
            ),
            stdout,
        )
    else:
        delegate_rendering.render_runs_text(
            summaries,
            stdout,
            mode=mode,
            total=total,
            warnings=warnings or None,
        )
    return 0
