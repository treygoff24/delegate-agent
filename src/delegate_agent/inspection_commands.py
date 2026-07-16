from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import command_errors, redaction, run_registry, snapshot_view
from delegate_agent import rendering as delegate_rendering
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class SnapshotCommand:
    handle: str | None
    latest_harness: str | None = None
    no_redact: bool = False
    json_mode: bool = False


@dataclass(frozen=True)
class RunsCommand:
    active: bool = False
    running: bool = False
    stale: bool = False
    harness: str | None = None
    group: str | None = None
    limit: int | None = None
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
    else:
        index = run_registry.load_index(registry_root)
        summaries = run_registry.list_run_summaries(
            registry_root,
            index,
            active=command.active,
            status_filter=status_filter,
            harness=command.harness,
            group=command.group,
            limit=limit,
        )
    summaries = [redaction.redact_value(summary) for summary in summaries]
    if command.json_mode:
        delegate_rendering.print_json(
            delegate_rendering.runs_json_payload(summaries, limit=limit, mode=mode),
            stdout,
        )
    else:
        delegate_rendering.render_runs_text(summaries, stdout, mode=mode)
    return 0
