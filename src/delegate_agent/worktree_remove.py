"""Worktree removal pipeline.

Implements ``delegate worktree remove`` end to end: option normalization, dirty
and merged safety gates, the ``git worktree remove`` + branch-delete sequence,
and registry status updates. ``worktree_mgmt`` re-exports this surface so callers
and tests keep importing from ``worktree_mgmt``.

Cross-cutting seams that tests monkeypatch on the ``worktree_mgmt`` module
(``_run_git``, ``_remove_branch``, ``merged_into_source``, ``detect_worktree_status``,
``dirty_info``, ``resolve_record``, ``_error_payload``) are read back through the
``worktree_mgmt`` facade (the ``wm`` alias) at call time so those patches still
take effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from delegate_agent import run_registry, worktree_records
from delegate_agent.git_utils import GIT_TIMEOUT_RETURN_CODE
from delegate_agent.isolation import target_contains_source_root
from delegate_agent.json_types import JsonObject
from delegate_agent.worktree_records import (
    SCHEMA_REMOVE,
    STATUS_MISSING,
    STATUS_PRESENT,
    STATUS_REMOVED,
    STATUS_UNKNOWN,
    WORKTREE_ERROR_EXIT_CODE,
    PersistentWorktreeRecord,
    _utc_now_iso,
)


@dataclass(frozen=True)
class BranchRemovalResult:
    removed: bool
    kept_reason: str | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class RemoveWorktreeOptions:
    discard_uncommitted: bool
    force_branch: bool
    keep_branch: bool


@dataclass(frozen=True)
class RemoveWorktreePlan:
    record: PersistentWorktreeRecord
    alias: str
    status: str
    source_git_root: str
    execution_cwd: str
    branch: str | None
    discarded_paths: list[str] | None
    warnings: list[str]


def _remove_branch(source_git_root: str, branch: str, *, force: bool) -> BranchRemovalResult:
    flag = "-D" if force else "-d"
    result = wm._run_git(source_git_root, ["branch", flag, branch])
    if result.returncode == 0:
        return BranchRemovalResult(removed=True)
    stderr = result.stderr.strip() or "delete_failed"
    if result.returncode == GIT_TIMEOUT_RETURN_CODE:
        return BranchRemovalResult(removed=False, error=stderr, error_code="git_timeout")
    lowered = stderr.lower()
    if not force and ("not fully merged" in lowered or "not merged" in lowered):
        return BranchRemovalResult(removed=False, kept_reason="unmerged")
    return BranchRemovalResult(removed=False, error=stderr)


def _apply_branch_removal_result(
    payload: JsonObject,
    result: BranchRemovalResult,
    *,
    alias: str,
) -> None:
    payload["branchRemoved"] = result.removed
    if result.kept_reason:
        payload["branchKept"] = result.kept_reason
        if result.kept_reason == "unmerged":
            payload["nextActions"] = [f"delegate worktree remove {alias} --force-branch"]
    if result.error:
        payload["ok"] = False
        code = result.error_code or "branch_remove_failed"
        payload["code"] = code
        payload["error"] = code
        payload["exitCode"] = WORKTREE_ERROR_EXIT_CODE
        payload["branchRemovalError"] = result.error
        if payload.get("pathRemoved") is True:
            payload["partialSuccess"] = True
            payload["nextActions"] = [f"delegate worktree remove {alias} --force-branch"]


def _normalize_remove_options(
    *,
    discard_uncommitted: bool,
    force_branch: bool,
    keep_branch: bool,
    force: bool,
    handle: str,
) -> tuple[bool, bool, bool]:
    """Normalize raw CLI flags into canonical removal booleans.

    Returns (discard_uncommitted, force_branch, keep_branch) after applying
    the ``--force`` shorthand and validating mutual exclusions.
    """
    if force:
        discard_uncommitted = True
        force_branch = True
    if keep_branch and force_branch:
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "invalid_option_combination",
                "--keep-branch is mutually exclusive with --force-branch.",
                next_actions=[f"delegate worktree remove {handle} --keep-branch"],
            )
        )
    return discard_uncommitted, force_branch, keep_branch


def _raise_if_dirty_without_discard(
    *,
    dirty: bool | None,
    dirty_paths: list[str],
    dirty_warnings: list[str] | None = None,
    discard_uncommitted: bool,
    record: PersistentWorktreeRecord,
    alias: str,
) -> None:
    """Fail closed when dirty state is unsafe to discard implicitly."""
    if dirty is None and not discard_uncommitted:
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "dirty_check_failed",
                "Could not determine whether the worktree has uncommitted changes; inspect it or pass --discard-uncommitted to remove anyway.",
                record=record,
                warnings=dirty_warnings or None,
                next_actions=[
                    f"delegate worktree show {alias}",
                    f"delegate worktree remove {alias} --discard-uncommitted",
                ],
            )
        )
    if dirty is True and not discard_uncommitted:
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "dirty_worktree",
                f"Worktree has {len(dirty_paths)} uncommitted changes; pass --discard-uncommitted to remove anyway.",
                record=record,
                dirty_paths=dirty_paths,
                next_actions=[
                    f"delegate worktree show {alias}",
                    f"delegate worktree remove {alias} --discard-uncommitted",
                ],
            )
        )


def _raise_if_unmerged_without_override(
    *,
    record: PersistentWorktreeRecord,
    status: str,
    branch: str | None,
    keep_branch: bool,
    force_branch: bool,
    alias: str,
) -> None:
    """Raise ``unmerged_branch`` when the branch is not merged into source
    and the caller did not request ``--keep-branch`` or ``--force-branch``."""
    if status not in (STATUS_PRESENT, STATUS_UNKNOWN):
        return
    if not isinstance(branch, str) or not branch:
        return
    if keep_branch or force_branch:
        return
    merged, merge_warnings = wm.merged_into_source(record, status)
    if merged is None:
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "merge_check_failed",
                "Could not determine whether the worktree branch is merged into current source HEAD; inspect it, merge it, or pass --keep-branch/--force-branch explicitly.",
                record=record,
                next_actions=[
                    f"delegate worktree show {alias}",
                    f"delegate worktree remove {alias} --keep-branch",
                    f"delegate worktree remove {alias} --force-branch",
                ],
                warnings=merge_warnings or None,
            )
        )
    if merged is False:
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "unmerged_branch",
                "Worktree branch is not merged into current source HEAD; inspect it, merge it, or pass --keep-branch/--force-branch explicitly.",
                record=record,
                next_actions=[
                    f"delegate worktree show {alias}",
                    f"delegate worktree remove {alias} --keep-branch",
                    f"delegate worktree remove {alias} --force-branch",
                ],
                warnings=merge_warnings or None,
            )
        )


def _require_removal_metadata(record: PersistentWorktreeRecord) -> tuple[str, str]:
    """Validate that ``sourceGitRoot`` and ``executionCwd`` are present.

    Returns ``(source_git_root, execution_cwd)`` for direct use by the caller.
    """
    source_git_root = record.get("sourceGitRoot")
    execution_cwd = record.get("executionCwd")
    if not isinstance(source_git_root, str) or not isinstance(execution_cwd, str):
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "worktree_remove_failed",
                "Run is missing sourceGitRoot or executionCwd metadata.",
                record=record,
            )
        )
    return source_git_root, execution_cwd


def _remove_worktree_path(
    *,
    source_git_root: str,
    execution_cwd: str,
    discard_uncommitted: bool,
    record: PersistentWorktreeRecord,
    alias: str,
) -> None:
    """Execute ``git worktree remove`` and raise on failure."""
    remove_args = ["worktree", "remove"]
    if discard_uncommitted:
        remove_args.append("--force")
    remove_args.append(execution_cwd)
    result = wm._run_git(source_git_root, remove_args)
    if result.returncode != 0:
        if result.returncode == GIT_TIMEOUT_RETURN_CODE:
            raise wm.WorktreeManagementError(
                wm._error_payload(
                    "git_timeout",
                    f"git worktree remove timed out: {result.stderr.strip()}",
                    record=record,
                    next_actions=[f"delegate worktree show {alias}"],
                    retry_safe=True,
                )
            )
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "worktree_remove_failed",
                f"git worktree remove failed: {result.stderr.strip()}",
                record=record,
                next_actions=[f"delegate worktree show {alias}"],
                retry_safe=True,
            )
        )


def _remove_branch_if_requested(
    *,
    source_git_root: str | None,
    branch: str | None,
    keep_branch: bool,
    force_branch: bool,
    status: str,
) -> BranchRemovalResult:
    """Decide and execute branch removal after the worktree path is gone.

    Returns the ``BranchRemovalResult`` describing what happened to the branch.
    For the normal-success path (not missing, not already-removed), the caller
    should post-process the result to handle prune-originated ``keep_branch``
    with an unmerged branch (spec L673).
    """
    if keep_branch:
        return BranchRemovalResult(removed=False, kept_reason="requested")
    if status == STATUS_REMOVED and not force_branch:
        return BranchRemovalResult(removed=False)
    if force_branch and isinstance(source_git_root, str) and isinstance(branch, str) and branch:
        return wm._remove_branch(source_git_root, branch, force=True)
    if status == STATUS_MISSING:
        return BranchRemovalResult(removed=False, kept_reason="path_missing")
    if isinstance(source_git_root, str) and isinstance(branch, str) and branch:
        return wm._remove_branch(source_git_root, branch, force=False)
    return BranchRemovalResult(removed=False)


def _mark_worktree_removed(
    *,
    registry_root: Path,
    run_id: str,
    discarded_paths: list[str] | None,
) -> None:
    """Update registry state to mark the run as removed."""
    run_registry.set_worktree_status_locked(
        registry_root,
        run_id,
        "removed",
        removed_at=_utc_now_iso(),
        discarded_dirty_paths=discarded_paths,
    )


def _remove_payload(
    *,
    record: PersistentWorktreeRecord,
    branch: object,
    execution_cwd: object,
    source_git_root: object,
    path_removed: bool,
    noop: bool,
    branch_result: BranchRemovalResult,
    alias: str,
    discarded_paths: list[str] | None = None,
    warnings: list[str] | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "schema": SCHEMA_REMOVE,
        "ok": not bool(branch_result.error),
        "alias": record.get("alias"),
        "runId": record.get("runId"),
        "branch": branch,
        "executionCwd": execution_cwd,
        "sourceGitRoot": source_git_root,
        "removed": True,
        "pathRemoved": path_removed,
        "worktreeStatus": STATUS_REMOVED,
        "noop": noop,
    }
    _apply_branch_removal_result(payload, branch_result, alias=alias)
    if discarded_paths is not None:
        payload["discardedDirtyPaths"] = discarded_paths
    if warnings:
        payload["warnings"] = warnings
    return payload


def _build_remove_worktree_plan(
    record: PersistentWorktreeRecord,
    *,
    alias: str,
    status: str,
    status_warnings: list[str],
    options: RemoveWorktreeOptions,
    merged_check_already_passed: bool,
) -> RemoveWorktreePlan:
    source_git_root, execution_cwd = _require_removal_metadata(record)
    if target_contains_source_root(execution_cwd, source_git_root):
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "source_root_guard",
                "Refusing to remove a worktree path that is or contains its source root.",
                record=record,
            )
        )
    dirty, dirty_paths, _dirty_total, dirty_warnings = wm.dirty_info(record, status)
    all_warnings = [*status_warnings, *dirty_warnings]
    if status in (STATUS_PRESENT, STATUS_UNKNOWN):
        _raise_if_dirty_without_discard(
            dirty=dirty,
            dirty_paths=dirty_paths,
            dirty_warnings=dirty_warnings,
            discard_uncommitted=options.discard_uncommitted,
            record=record,
            alias=alias,
        )

    branch = record.get("branch")
    if not merged_check_already_passed:
        _raise_if_unmerged_without_override(
            record=record,
            status=status,
            branch=branch,
            keep_branch=options.keep_branch,
            force_branch=options.force_branch,
            alias=alias,
        )

    return RemoveWorktreePlan(
        record=record,
        alias=alias,
        status=status,
        source_git_root=source_git_root,
        execution_cwd=execution_cwd,
        branch=branch,
        discarded_paths=dirty_paths if options.discard_uncommitted and dirty_paths else None,
        warnings=all_warnings,
    )


def _remove_already_removed(
    record: PersistentWorktreeRecord,
    *,
    alias: str,
    options: RemoveWorktreeOptions,
) -> JsonObject:
    source_git_root = record.get("sourceGitRoot")
    branch = record.get("branch")
    branch_result = _remove_branch_if_requested(
        source_git_root=source_git_root,
        branch=branch,
        keep_branch=options.keep_branch,
        force_branch=options.force_branch,
        status=STATUS_REMOVED,
    )
    return _remove_payload(
        record=record,
        branch=branch,
        execution_cwd=record.get("executionCwd"),
        source_git_root=source_git_root,
        path_removed=False,
        noop=not branch_result.removed,
        branch_result=branch_result,
        alias=alias,
    )


def _remove_missing_worktree_path(
    registry_root: Path,
    plan: RemoveWorktreePlan,
    *,
    options: RemoveWorktreeOptions,
) -> JsonObject:
    branch_result = _remove_branch_if_requested(
        source_git_root=plan.source_git_root,
        branch=plan.branch,
        keep_branch=options.keep_branch,
        force_branch=options.force_branch,
        status=plan.status,
    )
    _mark_worktree_removed(
        registry_root=registry_root,
        run_id=str(plan.record["runId"]),
        discarded_paths=plan.discarded_paths,
    )
    return _remove_payload(
        record=plan.record,
        branch=plan.branch,
        execution_cwd=plan.execution_cwd,
        source_git_root=plan.source_git_root,
        path_removed=False,
        noop=False,
        branch_result=branch_result,
        alias=plan.alias,
        discarded_paths=plan.discarded_paths,
    )


def _remove_present_worktree_path(
    registry_root: Path,
    plan: RemoveWorktreePlan,
    *,
    options: RemoveWorktreeOptions,
) -> JsonObject:
    _remove_worktree_path(
        source_git_root=plan.source_git_root,
        execution_cwd=plan.execution_cwd,
        discard_uncommitted=options.discard_uncommitted,
        record=plan.record,
        alias=plan.alias,
    )
    branch_result = _remove_branch_if_requested(
        source_git_root=plan.source_git_root,
        branch=plan.branch,
        keep_branch=options.keep_branch,
        force_branch=options.force_branch,
        status=plan.status,
    )
    # Override branchKept when keep_branch came from prune on a clean worktree
    # whose branch is not merged into source (spec L673).
    if (
        options.keep_branch
        and branch_result.kept_reason == "requested"
        and isinstance(plan.branch, str)
    ):
        merged_val, _ = wm.merged_into_source(plan.record, plan.status)
        if merged_val is False:
            branch_result = BranchRemovalResult(removed=False, kept_reason="unmerged")

    _mark_worktree_removed(
        registry_root=registry_root,
        run_id=str(plan.record["runId"]),
        discarded_paths=plan.discarded_paths,
    )
    return _remove_payload(
        record=plan.record,
        branch=plan.branch,
        execution_cwd=plan.execution_cwd,
        source_git_root=plan.source_git_root,
        path_removed=True,
        noop=False,
        branch_result=branch_result,
        alias=plan.alias,
        discarded_paths=plan.discarded_paths,
        warnings=plan.warnings,
    )


def remove_worktree(
    registry_root: Path,
    *,
    handle: str,
    discard_uncommitted: bool = False,
    force_branch: bool = False,
    keep_branch: bool = False,
    force: bool = False,
    _merged_check_already_passed: bool = False,
) -> JsonObject:
    discard_uncommitted, force_branch, keep_branch = _normalize_remove_options(
        discard_uncommitted=discard_uncommitted,
        force_branch=force_branch,
        keep_branch=keep_branch,
        force=force,
        handle=handle,
    )
    options = RemoveWorktreeOptions(
        discard_uncommitted=discard_uncommitted,
        force_branch=force_branch,
        keep_branch=keep_branch,
    )
    with run_registry.registry_lock(registry_root):
        record = wm.resolve_record(registry_root, handle=handle)
        status, warnings = wm.detect_worktree_status(record)
        alias = str(record.get("alias") or handle)

        if status == STATUS_REMOVED:
            return _remove_already_removed(record, alias=alias, options=options)

        execution_cwd = record.get("executionCwd")
        if status in (STATUS_PRESENT, STATUS_UNKNOWN) and isinstance(execution_cwd, str):
            attachments = worktree_records.live_attachments_for_path(
                registry_root, execution_cwd
            )
            if attachments:
                attached = ", ".join(
                    str(item.get("alias") or item.get("runId")) for item in attachments
                )
                raise wm.WorktreeManagementError(
                    wm._error_payload(
                        "worktree_attached",
                        f"Worktree is in use by attached resume run(s): {attached}. "
                        "Wait for or cancel the attached run before removing.",
                        record=record,
                        next_actions=[
                            f"delegate wait {attached.split(', ')[0]}",
                            f"delegate cancel {attached.split(', ')[0]}",
                        ],
                    )
                )

        plan = _build_remove_worktree_plan(
            record,
            alias=alias,
            status=status,
            status_warnings=warnings,
            options=options,
            merged_check_already_passed=_merged_check_already_passed,
        )

        if status == STATUS_MISSING:
            return _remove_missing_worktree_path(registry_root, plan, options=options)

        return _remove_present_worktree_path(registry_root, plan, options=options)


# Deferred to the bottom to break the worktree_mgmt<->worktree_remove facade
# cycle: worktree_mgmt re-exports this module's surface (a top-level import here
# would fail when worktree_remove is imported first). All `wm.<seam>` access
# above is call-time, so binding the alias after our own definitions is
# sufficient and keeps mock.patch.object(worktree_mgmt, ...) seams working.
from delegate_agent import worktree_mgmt as wm  # noqa: E402
