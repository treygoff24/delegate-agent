"""Worktree removal with locked safety checks and registry status updates.

Shared inspection and error helpers come from worktree_mgmt; removal helpers
and branch deletion belong to this module."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from delegate_agent import isolation, run_registry
from delegate_agent import worktree_mgmt as wm
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_TIMEOUT_RETURN_CODE
from delegate_agent.isolation import target_contains_source_root
from delegate_agent.json_types import JsonObject
from delegate_agent.worktree_records import (
    SCHEMA_REMOVE,
    STATUS_MISSING,
    STATUS_REMOVED,
    WORKTREE_ERROR_EXIT_CODE,
    PersistentWorktreeRecord,
    _utc_now_iso,
)
from delegate_agent.worktree_salvage import LedgerSalvage, salvage_ledger_changes


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
    kill_live: bool = False
    ledger_globs: tuple[str, ...] = ()


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


def _remove_worktree_path(
    *,
    source_git_root: str,
    execution_cwd: str,
    discard_uncommitted: bool,
    record: PersistentWorktreeRecord,
    alias: str,
    salvage_root: Path | Callable[[], Path] | None = None,
    ledger_globs: tuple[str, ...] = (),
) -> LedgerSalvage | None:
    """Save changed ledger files, then execute ``git worktree remove``; raise on failure.

    Returns where the ledger files were saved (None when there was nothing to
    save). A failed save raises before anything is removed.
    """
    salvage = (
        salvage_ledger_changes(
            execution_cwd=execution_cwd,
            registry_root=salvage_root,
            creation_context=record.get("creationContext"),
            ignore_globs=ledger_globs,
        )
        if salvage_root is not None and ledger_globs
        else None
    )
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
    return salvage


def remove_empty_pool_parent(execution_cwd: str) -> bool:
    """Remove an empty Delegate fingerprint directory after path retirement."""

    worktree = Path(execution_cwd)
    parent = worktree.parent
    if not isolation.is_pool_fingerprint_name(parent.name):
        return False
    try:
        parent.rmdir()
    except OSError:
        return False
    return True


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
        return _remove_branch(source_git_root, branch, force=True)
    if status == STATUS_MISSING:
        return BranchRemovalResult(removed=False, kept_reason="path_missing")
    if isinstance(source_git_root, str) and isinstance(branch, str) and branch:
        return _remove_branch(source_git_root, branch, force=False)
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
    salvage: LedgerSalvage | None = None,
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
    if salvage is not None:
        payload["salvagePath"] = salvage.path
        payload["salvagedPaths"] = list(salvage.files)
        if salvage.removed:
            payload["salvageRemovedPaths"] = list(salvage.removed)
    return payload


def _build_remove_worktree_plan(
    inspection: wm.WorktreeInspection,
    *,
    alias: str,
    options: RemoveWorktreeOptions,
) -> RemoveWorktreePlan:
    record = inspection.record
    status = inspection.status
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
    if target_contains_source_root(execution_cwd, source_git_root):
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "source_root_guard",
                "Refusing to remove a worktree path that is or contains its source root.",
                record=record,
            )
        )
    decision = wm.evaluate_worktree_safety(
        inspection,
        discard_uncommitted=options.discard_uncommitted,
        force_branch=options.force_branch,
        keep_branch=options.keep_branch,
        kill_live=options.kill_live,
        require_merged=True,
    )
    if decision.reason is not None:
        raise wm.WorktreeManagementError(
            wm.safety_error_payload(inspection, alias=alias, reason=decision.reason)
        )

    branch = record.get("branch")
    all_warnings = [*inspection.status_warnings, *inspection.dirty_warnings]
    all_warnings.extend(inspection.merge_warnings)

    return RemoveWorktreePlan(
        record=record,
        alias=alias,
        status=status,
        source_git_root=source_git_root,
        execution_cwd=execution_cwd,
        branch=branch,
        discarded_paths=(
            list(inspection.dirty_paths)
            if options.discard_uncommitted and inspection.dirty_paths
            else None
        ),
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
    salvage = _remove_worktree_path(
        source_git_root=plan.source_git_root,
        execution_cwd=plan.execution_cwd,
        discard_uncommitted=options.discard_uncommitted,
        record=plan.record,
        alias=plan.alias,
        salvage_root=registry_root,
        ledger_globs=options.ledger_globs,
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
        salvage=salvage,
    )


def _nested_registry_error(
    code: str,
    message: str,
    *,
    parent_record: PersistentWorktreeRecord,
    nested_root: Path,
    parent_cwd: str,
) -> wm.WorktreeManagementError:
    payload = wm._error_payload(
        code,
        message,
        record=parent_record,
        next_actions=[
            f"delegate --cwd {parent_cwd} worktree list",
            f"delegate worktree reap --path {parent_cwd}",
        ],
        retry_safe=True,
    )
    payload["nestedRegistry"] = str(nested_root)
    return wm.WorktreeManagementError(payload)


def _collect_nested_worktrees(
    parent_record: PersistentWorktreeRecord,
    registry_root: Path,
    stack: contextlib.ExitStack,
    top_record: PersistentWorktreeRecord,
    absent: list[Path],
) -> list[tuple[Path, PersistentWorktreeRecord]]:
    """Lock and read every nested Registry under a worktree, deepest records first.

    A run launched with ``--cwd <this worktree>`` registers in the worktree's own
    ``.delegate``, so the owning Registry never lists its worktree; removing the
    parent would orphan it. Locks are taken parent then nested, depth first, and
    stay held on ``stack`` so nothing can register between selection and removal.
    Anything unreadable or unlockable fails closed. A Registry directory that
    exists is locked before its index is read, so a launch creating the index
    concurrently is either seen or blocked; a directory that does not exist yet
    is appended to ``absent`` so the caller can recheck it right before deleting.
    """
    execution = parent_record.get("executionCwd")
    if not isinstance(execution, str) or not execution:
        return []
    parent_cwd = str(top_record.get("executionCwd") or execution)
    nested_root = run_registry.registry_root(Path(execution))
    try:
        os.stat(nested_root)
    except (FileNotFoundError, NotADirectoryError):
        absent.append(nested_root)
        return []
    except OSError as exc:
        raise _nested_registry_error(
            "nested_registry_unreadable",
            f"Cannot read the nested Registry at {nested_root} ({exc}); removing "
            f"{parent_cwd} could orphan worktrees registered there. --kill-live does not "
            "override this. Repair or move aside that Registry, then retry.",
            parent_record=top_record,
            nested_root=nested_root,
            parent_cwd=parent_cwd,
        ) from exc
    try:
        if nested_root.resolve() == registry_root.resolve():
            return []
        stack.enter_context(run_registry.registry_lock(nested_root))
        records = wm.load_persistent_records(nested_root)
    except (OSError, RuntimeError, ValueError, TimeoutError, DelegateError) as exc:
        raise _nested_registry_error(
            "nested_registry_unreadable",
            f"Cannot lock or read the nested Registry at {nested_root} ({exc}); removing "
            f"{parent_cwd} could orphan worktrees registered there. Nothing was removed; "
            "--kill-live does not override this. Repair or move aside that Registry, then "
            "retry.",
            parent_record=top_record,
            nested_root=nested_root,
            parent_cwd=parent_cwd,
        ) from exc
    collected: list[tuple[Path, PersistentWorktreeRecord]] = []
    for record in records:
        if record.get("registryWorktreeStatus") == STATUS_REMOVED:
            continue
        collected.extend(_collect_nested_worktrees(record, nested_root, stack, top_record, absent))
        collected.append((nested_root, record))
    return collected


def _refuse_if_registry_appeared(
    absent: list[Path],
    parent_record: PersistentWorktreeRecord,
    nested_removed: list[JsonObject],
) -> None:
    """Refuse when a Registry that did not exist during the walk exists now.

    It could only have been created by a launch into the worktree after the walk,
    and it is not locked, so deleting the parent could take a new run's record.
    """
    appeared = [root for root in absent if os.path.lexists(root)]
    if not appeared:
        return
    alias = str(parent_record.get("alias") or parent_record.get("runId"))
    parent_cwd = str(parent_record.get("executionCwd") or "")
    payload = _nested_registry_error(
        "nested_registry_appeared",
        f"A run registered inside {parent_cwd} while {alias} was being removed "
        f"(new Registry at {appeared[0]}); {alias} was not removed. Check that run "
        "with `worktree list`, then remove the parent again.",
        parent_record=parent_record,
        nested_root=appeared[0],
        parent_cwd=parent_cwd,
    ).payload
    if nested_removed:
        payload["nestedRemoved"] = _nested_summary(nested_removed)
    raise wm.WorktreeManagementError(payload)


def nested_worktrees_reap_block(target: Path, stack: contextlib.ExitStack) -> JsonObject | None:
    """Why a delete that bypasses ``remove_worktree`` (``reap``) must not take ``target``.

    Runs launched with ``--cwd <target>`` keep their Registry in
    ``<target>/.delegate``; deleting the path deletes those records while their
    worktrees live on elsewhere. Reap never removes nested worktrees itself: any
    unremoved one refuses, naming the command that removes the parent properly.
    The nested Registry lock is held on ``stack`` through the caller's delete.
    """
    nested_root = run_registry.registry_root(target)
    try:
        os.stat(nested_root)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        return {
            "code": "nested_registry_unreadable",
            "nestedRegistry": str(nested_root),
            "message": f"Cannot read the nested Registry at {nested_root} ({exc}).",
        }
    try:
        stack.enter_context(run_registry.registry_lock(nested_root))
        records = wm.load_persistent_records(nested_root)
    except (OSError, RuntimeError, ValueError, TimeoutError, DelegateError) as exc:
        return {
            "code": "nested_registry_unreadable",
            "nestedRegistry": str(nested_root),
            "message": f"Cannot lock or read the nested Registry at {nested_root} ({exc}).",
        }
    nested = [r for r in records if r.get("registryWorktreeStatus") != STATUS_REMOVED]
    if not nested:
        return None
    return {
        "code": "nested_worktrees_present",
        "nestedRegistry": str(nested_root),
        "nestedWorktrees": [
            {
                "alias": r.get("alias"),
                "runId": r.get("runId"),
                "executionCwd": r.get("executionCwd"),
            }
            for r in nested
        ],
        "message": (
            f"{len(nested)} worktree(s) from runs launched with --cwd {target} are "
            "registered inside it; reaping it would orphan them. Remove them by name "
            f"(delegate --cwd {target} worktree list), or remove the parent with "
            "`worktree remove`, which removes clean, merged nested worktrees first."
        ),
    }


def _nested_blockers(
    targets: list[tuple[Path, PersistentWorktreeRecord]],
    *,
    parent_cwd: str,
    kill_live: bool,
    ledger_globs: tuple[str, ...],
) -> list[JsonObject]:
    """One entry per nested worktree that a plain remove, by name, would refuse.

    The parent's discard flags never apply here: a dirty or unmerged nested
    worktree is only removed when the caller names it and passes the flag.
    """
    blockers: list[JsonObject] = []
    for nested_root, record in targets:
        inspection = wm.inspect_worktree(
            nested_root,
            record,
            kill_live=kill_live,
            check_merge=True,
            retirement_ignore_globs=ledger_globs,
        )
        if inspection.status == STATUS_REMOVED:
            continue
        decision = wm.evaluate_worktree_safety(
            inspection,
            kill_live=kill_live,
            require_merged=True,
        )
        if decision.reason is None:
            continue
        alias = str(record.get("alias") or record.get("runId"))
        flag = {
            "dirty": " --discard-uncommitted",
            "dirty_check_failed": " --discard-uncommitted",
            "unmerged_branch": " --keep-branch",
            "merge_check_failed": " --keep-branch",
        }.get(decision.reason, "")
        nested_cwd = str(nested_root.parent)
        blockers.append(
            {
                "alias": alias,
                "runId": record.get("runId"),
                "executionCwd": record.get("executionCwd"),
                "branch": record.get("branch"),
                "reason": "dirty_worktree" if decision.reason == "dirty" else decision.reason,
                "command": f"delegate --cwd {nested_cwd or parent_cwd} worktree remove {alias}{flag}",
            }
        )
    return blockers


def _remove_nested_worktrees(
    parent_record: PersistentWorktreeRecord,
    registry_root: Path,
    stack: contextlib.ExitStack,
    *,
    options: RemoveWorktreeOptions,
    absent: list[Path],
) -> list[JsonObject]:
    """Remove clean, merged nested worktrees first; refuse, naming each, if any is not.

    Nested Registry locks are added to ``stack`` and stay held so the caller can
    recheck and delete the parent while they are still held.
    """
    parent_alias = str(parent_record.get("alias") or parent_record.get("runId"))
    parent_cwd = str(parent_record.get("executionCwd") or "")
    targets = _collect_nested_worktrees(parent_record, registry_root, stack, parent_record, absent)
    blockers = _nested_blockers(
        targets,
        parent_cwd=parent_cwd,
        kill_live=options.kill_live,
        ledger_globs=options.ledger_globs,
    )
    if blockers:
        listing = "; ".join(
            f"{b['alias']} ({b['reason']}) at {b['executionCwd']}" for b in blockers
        )
        payload = wm._error_payload(
            "nested_worktrees_block_remove",
            f"{parent_alias} has {len(blockers)} nested worktree(s) from runs launched with "
            f"--cwd inside it that are not safe to remove automatically: {listing}. Removing "
            f"{parent_alias} would orphan them, and the parent's --force/--discard-uncommitted "
            "never applies to them. Nothing was removed. Remove each by name with its "
            "`command` (dirty or unmerged ones need the flag shown), then remove the parent again.",
            record=parent_record,
            next_actions=[str(b["command"]) for b in blockers],
        )
        payload["nestedWorktrees"] = blockers
        raise wm.WorktreeManagementError(payload)
    nested_options = RemoveWorktreeOptions(
        discard_uncommitted=False,
        force_branch=False,
        keep_branch=False,
        kill_live=options.kill_live,
        ledger_globs=options.ledger_globs,
    )
    removed: list[JsonObject] = []
    for nested_root, record in targets:
        alias = str(record.get("alias") or record.get("runId"))
        try:
            result = _remove_locked(
                nested_root,
                handle=str(record.get("runId")),
                options=nested_options,
                include_detached=False,
                retirement_ignore_globs=options.ledger_globs,
                workspace=None,
                nested=True,
            )
        except wm.WorktreeManagementError as exc:
            result = exc.payload
        if result.get("ok") is False:
            failure = wm._error_payload(
                "nested_worktree_remove_failed",
                f"Removing nested worktree {alias} failed ({result.get('code') or 'error'}: "
                f"{result.get('message') or result.get('branchRemovalError') or 'see nestedResult'}); "
                f"{parent_alias} was not removed. Fix or finish removing {alias}, then remove "
                f"{parent_alias} again.",
                record=parent_record,
                next_actions=[
                    *[str(a) for a in result.get("nextActions") or []],
                    f"delegate --cwd {parent_cwd} worktree remove {alias}",
                ],
                retry_safe=True,
            )
            failure["nestedResult"] = result
            failure["nestedRemoved"] = _nested_summary(removed)
            raise wm.WorktreeManagementError(failure)
        removed.append(result)
    return removed


def _nested_summary(items: list[JsonObject]) -> list[JsonObject]:
    return [
        {
            "alias": item.get("alias"),
            "runId": item.get("runId"),
            "executionCwd": item.get("executionCwd"),
            "pathRemoved": item.get("pathRemoved"),
            "ok": item.get("ok"),
        }
        for item in items
    ]


def remove_worktree(
    registry_root: Path,
    *,
    handle: str,
    discard_uncommitted: bool = False,
    force_branch: bool = False,
    keep_branch: bool = False,
    force: bool = False,
    kill_live: bool = False,
    include_detached: bool = False,
    retirement_ignore_globs: tuple[str, ...] | None = None,
    workspace: Path | None = None,
) -> JsonObject:
    """Remove one persistent worktree after the shared safety checks.

    ``force`` is the shorthand for ``discard_uncommitted`` plus ``force_branch``.
    It never overrides a live owner run; ``kill_live`` does, on its own. Neither
    flag reaches worktrees that runs launched from inside this one registered:
    those are removed only when clean and merged, else the remove is refused.
    """
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
        kill_live=kill_live,
        ledger_globs=wm.DEFAULT_RETIREMENT_IGNORE_GLOBS
        if retirement_ignore_globs is None
        else retirement_ignore_globs,
    )
    with run_registry.registry_lock(registry_root):
        return _remove_locked(
            registry_root,
            handle=handle,
            options=options,
            include_detached=include_detached,
            retirement_ignore_globs=retirement_ignore_globs,
            workspace=workspace,
            nested=False,
        )


def _remove_locked(
    registry_root: Path,
    *,
    handle: str,
    options: RemoveWorktreeOptions,
    include_detached: bool,
    retirement_ignore_globs: tuple[str, ...] | None,
    workspace: Path | None,
    nested: bool,
) -> JsonObject:
    """Body of ``remove_worktree``; the caller holds ``registry_root``'s lock.

    ``nested`` marks a worktree removed on behalf of its parent, whose nested
    Registries were already locked and walked as one tree.
    """
    kill_live = options.kill_live
    keep_branch = options.keep_branch
    force_branch = options.force_branch
    record = wm.resolve_record(registry_root, handle=handle, workspace=workspace)
    alias = str(record.get("alias") or handle)
    inspection = wm.inspect_worktree(
        registry_root,
        record,
        include_detached=include_detached,
        kill_live=kill_live,
        check_merge=not keep_branch and not force_branch,
        retirement_ignore_globs=retirement_ignore_globs,
    )
    if inspection.status == STATUS_REMOVED:
        return _remove_already_removed(record, alias=alias, options=options)
    plan = _build_remove_worktree_plan(
        inspection,
        alias=alias,
        options=options,
    )

    if plan.status == STATUS_MISSING:
        return _remove_missing_worktree_path(registry_root, plan, options=options)

    with contextlib.ExitStack() as stack:
        nested_removed: list[JsonObject] = []
        if not nested:
            absent: list[Path] = []
            nested_removed = _remove_nested_worktrees(
                record, registry_root, stack, options=options, absent=absent
            )
            _refuse_if_registry_appeared(absent, record, nested_removed)
            # Nested Registry locks are still held: recheck immediately before the
            # delete that would take those Registries with it.
            block = wm._owner_run_block_reason(registry_root, record) or (
                wm._nested_run_block_reason(registry_root, record)
            )
            if block is not None and not kill_live:
                raise wm.WorktreeManagementError(
                    wm.safety_error_payload(inspection, alias=alias, reason=block)
                )
        # The policy above judged effective dirt, so what remains is dirt the
        # policy discounts (seeded or ledger files). Git only removes a path
        # holding any of it with its own force, which is now authorized.
        payload = _remove_present_worktree_path(
            registry_root, plan, options=replace(options, discard_uncommitted=True)
        )
    if nested_removed:
        payload["nestedRemoved"] = _nested_summary(nested_removed)
    return payload
