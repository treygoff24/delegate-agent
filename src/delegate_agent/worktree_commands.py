from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import rendering as delegate_rendering
from delegate_agent import run_registry, worktree_mgmt
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class WorktreeCommand:
    action: str | None
    json_mode: bool = False
    handle: str | None = None
    latest_harness: str | None = None
    harness: str | None = None
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


def _list_payload(command: WorktreeCommand, registry_root: Path, config: JsonObject) -> JsonObject:
    auto_prune = worktree_mgmt.maybe_auto_prune(
        registry_root,
        config,
        no_auto_prune=command.no_auto_prune,
    )
    payload = worktree_mgmt.list_worktrees(
        registry_root,
        harness=command.harness,
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
        include_detached=command.include_detached,
        dry_run=command.dry_run,
        discard_uncommitted=command.discard_uncommitted,
        force_branch=command.force_branch,
        force=command.force,
    )


def _gc_payload(command: WorktreeCommand, registry_root: Path, _config: JsonObject) -> JsonObject:
    return worktree_mgmt.gc_worktrees(
        registry_root,
        dry_run=command.dry_run,
    )


PayloadBuilder = Callable[[WorktreeCommand, Path, JsonObject], JsonObject]
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
    if registry_root is None:
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
