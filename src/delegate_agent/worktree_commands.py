from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import isolation, run_registry, worktree_mgmt
from delegate_agent import rendering as delegate_rendering
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class WorktreeCommand:
    action: str | None
    json_mode: bool = False
    handle: str | None = None
    latest_harness: str | None = None
    harness: str | None = None
    group: str | None = None
    status: str | None = None
    limit: int | None = None
    no_auto_prune: bool = False
    discard_uncommitted: bool = False
    force_branch: bool = False
    force: bool = False
    keep_branch: bool = False
    merged: bool = False
    older_than_days: int | None = None
    include_detached: bool = False
    dry_run: bool = False
    all_pools: bool = False
    pool: str | None = None


def _list_payload(command: WorktreeCommand, registry_root: Path, config: JsonObject) -> JsonObject:
    auto_prune = worktree_mgmt.maybe_auto_prune(
        registry_root,
        config,
        no_auto_prune=command.no_auto_prune,
    )
    payload = worktree_mgmt.list_worktrees(
        registry_root,
        harness=command.harness,
        group=command.group,
        status=command.status,
        limit=command.limit or run_registry.DEFAULT_RUNS_LIMIT,
    )
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary["autoPruneMode"] = (
            "suppressed"
            if command.no_auto_prune
            else "attempted"
            if auto_prune is not None
            else "disabled"
        )
        if auto_prune is not None and auto_prune.get("skipped") is not True:
            summary["readOnly"] = False
    if auto_prune is not None:
        payload["autoPrune"] = auto_prune
        if auto_prune.get("ok") is False and auto_prune.get("skipped") is not True:
            payload["ok"] = False
            exit_code = auto_prune.get("exitCode")
            payload["exitCode"] = (
                exit_code if isinstance(exit_code, int) else worktree_mgmt.WORKTREE_ERROR_EXIT_CODE
            )
    return payload


def _show_payload(command: WorktreeCommand, registry_root: Path, _config: JsonObject) -> JsonObject:
    return worktree_mgmt.show_worktree(
        registry_root,
        handle=command.handle,
        latest_harness=command.latest_harness,
    )


def _remove_payload(
    command: WorktreeCommand, registry_root: Path, _config: JsonObject
) -> JsonObject:
    if command.group is not None:
        removed: list[JsonObject] = []
        errors: list[JsonObject] = []
        matches = [
            record
            for record in worktree_mgmt.load_persistent_records(registry_root)
            if record.get("group") == command.group
        ]
        if not matches:
            raise worktree_mgmt.WorktreeManagementError(
                {
                    "ok": False,
                    "code": "no_matching_worktrees",
                    "message": f"No persistent worktrees found for group: {command.group}",
                    "group": command.group,
                    "matched": 0,
                    "retrySafe": False,
                }
            )
        for record in matches:
            handle = str(record.get("alias") or record.get("runId"))
            try:
                removed.append(
                    worktree_mgmt.remove_worktree(
                        registry_root,
                        handle=handle,
                        discard_uncommitted=command.discard_uncommitted,
                        force_branch=command.force_branch,
                        keep_branch=command.keep_branch,
                        force=command.force,
                    )
                )
                if removed[-1].get("ok") is False:
                    errors.append(removed[-1])
            except worktree_mgmt.WorktreeManagementError as exc:
                errors.append(exc.payload)
        return {
            "schema": worktree_mgmt.SCHEMA_REMOVE,
            "ok": not errors,
            "group": command.group,
            "matched": len(matches),
            "removed": removed,
            "errors": errors,
            **({"exitCode": worktree_mgmt.WORKTREE_ERROR_EXIT_CODE} if errors else {}),
        }
    if command.handle is None:
        raise worktree_mgmt.WorktreeManagementError(
            {
                "ok": False,
                "code": "missing_handle",
                "message": "worktree remove requires a handle.",
                "retrySafe": False,
            }
        )
    return worktree_mgmt.remove_worktree(
        registry_root,
        handle=command.handle,
        discard_uncommitted=command.discard_uncommitted,
        force_branch=command.force_branch,
        keep_branch=command.keep_branch,
        force=command.force,
    )


def _prune_payload(
    command: WorktreeCommand, registry_root: Path, _config: JsonObject
) -> JsonObject:
    return worktree_mgmt.prune_worktrees(
        registry_root,
        merged=command.merged,
        older_than_days=command.older_than_days,
        harness=command.harness,
        group=command.group,
        include_detached=command.include_detached,
        dry_run=command.dry_run,
        discard_uncommitted=command.discard_uncommitted,
        force_branch=command.force_branch,
        force=command.force,
    )


def _gc_pool_data_home(command: WorktreeCommand, config: JsonObject) -> Path | None:
    if command.pool is not None:
        return Path(command.pool).expanduser()
    return isolation.worktrees_data_home(config) if command.all_pools else None


def _gc_payload(
    command: WorktreeCommand, registry_root: Path | None, config: JsonObject
) -> JsonObject:
    return worktree_mgmt.gc_worktrees(
        registry_root,
        dry_run=command.dry_run,
        pool_data_home=_gc_pool_data_home(command, config),
    )


PayloadBuilder = Callable[[WorktreeCommand, Path | None, JsonObject], JsonObject]
TextRenderer = Callable[[JsonObject, TextIO], None]
ACTION_DISPATCH: dict[str, tuple[PayloadBuilder, TextRenderer]] = {
    "list": (_list_payload, delegate_rendering.render_worktree_list_text),
    "show": (_show_payload, delegate_rendering.render_worktree_show_text),
    "remove": (_remove_payload, delegate_rendering.render_worktree_remove_text),
    "prune": (_prune_payload, delegate_rendering.render_worktree_prune_text),
    "gc": (_gc_payload, delegate_rendering.render_worktree_gc_text),
}


def emit(
    command: WorktreeCommand,
    *,
    workspace_path: str,
    config: JsonObject,
    stdout: TextIO,
) -> int:
    registry_root = run_registry.registry_root_if_exists(Path(workspace_path))
    # `gc --all` / `gc --pool` scan the machine-global worktree pool, which is
    # exactly the surface that outlives its per-repo registry — requiring one
    # here would make the leak it reports unreportable.
    scans_pool = command.action == "gc" and (command.all_pools or command.pool is not None)
    if registry_root is None and not scans_pool:
        raise worktree_mgmt.WorktreeManagementError(
            {
                "ok": False,
                "code": "no_registry",
                "message": "No Delegate run registry exists in this workspace.",
                "sourceGitRoot": workspace_path,
                "nextActions": ["run a tracked delegate command first"],
                "retrySafe": False,
            }
        )
    action = command.action
    if action not in ACTION_DISPATCH:
        raise worktree_mgmt.WorktreeManagementError(
            {
                "ok": False,
                "code": "unknown_worktree_action",
                "message": f"Unknown worktree action: {action}",
                "sourceGitRoot": workspace_path,
                "retrySafe": False,
            }
        )
    build_payload, render_text = ACTION_DISPATCH[action]
    payload = build_payload(command, registry_root, config)
    if command.json_mode:
        delegate_rendering.print_json(payload, stdout)
    else:
        render_text(payload, stdout)
    if payload.get("ok") is False:
        exit_code = payload.get("exitCode")
        return exit_code if isinstance(exit_code, int) else worktree_mgmt.WORKTREE_ERROR_EXIT_CODE
    return 0
