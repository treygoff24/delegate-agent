"""Worktree prune and gc pipelines plus opportunistic auto-prune.

Implements ``delegate worktree prune`` (filtered batch removal), ``delegate
worktree gc`` (registry reconciliation against the live ``git worktree list``),
and the ``maybe_auto_prune`` hook fired opportunistically from ``worktree list``.
``worktree_mgmt`` re-exports this surface so callers and tests keep importing
from ``worktree_mgmt``.

Cross-cutting seams monkeypatched on the ``worktree_mgmt`` module
(``detect_worktree_status``, ``dirty_info``, ``merged_into_source``,
``_worktree_list_paths_with_warning``, ``prune_worktrees``, ``remove_worktree``,
``_error_payload``) are read back through the ``worktree_mgmt`` facade (the
``wm`` alias) at call time so those patches still take effect.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject, is_non_negative_int
from delegate_agent.worktree_records import (
    SCHEMA_GC,
    SCHEMA_PRUNE,
    STATUS_MISSING,
    STATUS_PRESENT,
    STATUS_REMOVED,
    STATUS_UNKNOWN,
    WORKTREE_ERROR_EXIT_CODE,
    PersistentWorktreeRecord,
    _registered_worktree_path_matches,
    _reload_record,
    load_persistent_records,
)


def _older_than(record: PersistentWorktreeRecord, days: int) -> bool | None:
    timestamp = record.get("lastActivityAt")
    dt = run_registry.parse_utc_timestamp(timestamp if isinstance(timestamp, str) else None)
    if dt is None:
        return None
    return dt < datetime.now(UTC) - timedelta(days=days)


def _entry_ref(record: PersistentWorktreeRecord, *, reason: str | None = None) -> JsonObject:
    entry: JsonObject = {"alias": record.get("alias"), "runId": record.get("runId")}
    if reason is not None:
        entry["reason"] = reason
    return entry


def prune_worktrees(
    registry_root: Path,
    *,
    merged: bool = False,
    older_than_days: int | None = None,
    harness: str | None = None,
    group: str | None = None,
    include_detached: bool = False,
    dry_run: bool = False,
    discard_uncommitted: bool = False,
    force_branch: bool = False,
    force: bool = False,
) -> JsonObject:
    if not merged and older_than_days is None:
        raise wm.WorktreeManagementError(
            wm._error_payload(
                "prune_filter_required",
                "delegate worktree prune requires --merged and/or --older-than DAYS.",
            )
        )
    if force:
        discard_uncommitted = True
        force_branch = True
    planned: list[JsonObject] = []
    removed: list[JsonObject] = []
    skipped: list[JsonObject] = []
    errors: list[JsonObject] = []
    for record in load_persistent_records(registry_root):
        alias = record.get("alias")
        if harness is not None and record.get("harness") != harness:
            skipped.append(_entry_ref(record, reason="harness_filter"))
            continue
        if group is not None and record.get("group") != group:
            skipped.append(_entry_ref(record, reason="group_filter"))
            continue
        status, _warnings = wm.detect_worktree_status(record)
        if status in (STATUS_REMOVED, STATUS_UNKNOWN):
            # Not candidates — filter silently (spec L678).
            continue
        if status == STATUS_MISSING:
            skipped.append(_entry_ref(record, reason="path_missing"))
            continue
        creation = record.get("creationContext")
        if (
            merged
            and isinstance(creation, dict)
            and creation.get("sourceHeadRef") is None
            and not include_detached
        ):
            skipped.append(_entry_ref(record, reason="detached_source"))
            continue
        # Compute dirty before the merged check so the merged branch path
        # can test dirty state without rebinding a later assignment.
        dirty, _dirty_paths, _dirty_total, _dirty_warnings = wm.dirty_info(record, status)
        if dirty is None and not discard_uncommitted:
            skipped.append(_entry_ref(record, reason="dirty_check_failed"))
            continue
        if dirty is True and not discard_uncommitted:
            skipped.append(_entry_ref(record, reason="dirty"))
            continue
        keep_branch_for_prune = False
        merged_check_already_passed = False
        if merged:
            merged_value, _merge_warnings = wm.merged_into_source(
                record,
                status,
                include_detached=include_detached,
            )
            merged_check_already_passed = merged_value is True
            if merged_value is None:
                skipped.append(_entry_ref(record, reason="merge_check_failed"))
                continue
            if merged_value is False and not force_branch:
                if dirty is not True:
                    keep_branch_for_prune = True
                else:
                    skipped.append(_entry_ref(record, reason="unmerged_branch"))
                    continue
        if older_than_days is not None:
            old_enough = _older_than(record, older_than_days)
            if old_enough is None:
                skipped.append(_entry_ref(record, reason="invalid_last_activity"))
                continue
            if not old_enough:
                skipped.append(_entry_ref(record, reason="not_yet_old_enough"))
                continue
        candidate = {
            "alias": alias,
            "runId": record.get("runId"),
            "branch": record.get("branch"),
            "executionCwd": record.get("executionCwd"),
            "sourceGitRoot": record.get("sourceGitRoot"),
        }
        if keep_branch_for_prune:
            candidate["keep_branch"] = True
        planned.append(candidate)
        if not dry_run:
            try:
                removed.append(
                    wm.remove_worktree(
                        registry_root,
                        handle=str(alias or record.get("runId")),
                        discard_uncommitted=discard_uncommitted,
                        force_branch=force_branch,
                        keep_branch=candidate.get("keep_branch", False),
                        _merged_check_already_passed=merged_check_already_passed,
                    )
                )
                if removed[-1].get("ok") is False:
                    errors.append(removed[-1])
            except wm.WorktreeManagementError as exc:
                errors.append(exc.payload)
    payload = {
        "schema": SCHEMA_PRUNE,
        "ok": not errors,
        "planned": planned,
        "removed": removed,
        "skipped": skipped,
        "errors": errors,
        "dryRun": dry_run,
    }
    if errors:
        payload["exitCode"] = WORKTREE_ERROR_EXIT_CODE
    return payload


def _reload_gc_candidate(
    registry_root: Path,
    record: PersistentWorktreeRecord,
) -> tuple[PersistentWorktreeRecord, str, str] | None:
    fresh = _reload_record(registry_root, str(record["runId"]))
    if fresh is None:
        return None
    fresh_current = fresh.get("registryWorktreeStatus")
    if fresh_current not in (STATUS_PRESENT, STATUS_UNKNOWN, None):
        return None
    fresh_source = fresh.get("sourceGitRoot")
    fresh_execution = fresh.get("executionCwd")
    if not isinstance(fresh_source, str) or not isinstance(fresh_execution, str):
        return None
    return fresh, fresh_source, fresh_execution


GcFreshAction = Callable[[PersistentWorktreeRecord, str, str], None]


def _with_locked_fresh_gc_candidate(
    registry_root: Path,
    record: PersistentWorktreeRecord,
    action: GcFreshAction,
) -> None:
    with run_registry.registry_lock(registry_root):
        fresh_candidate = _reload_gc_candidate(registry_root, record)
        if fresh_candidate is None:
            return
        action(*fresh_candidate)


def _gc_missing_entry(
    record: PersistentWorktreeRecord,
    execution: str,
    *,
    dry_run: bool,
) -> JsonObject:
    return {
        "alias": record.get("alias"),
        "runId": record.get("runId"),
        "executionCwd": execution,
        "worktreeStatus": STATUS_MISSING,
        "reason": "path_missing",
        "action": "would_mark_missing" if dry_run else "marked_missing",
    }


def _gc_orphan_entry(
    record: PersistentWorktreeRecord,
    execution: str,
    reason: str,
    message: str | None = None,
) -> JsonObject:
    entry = {
        "alias": record.get("alias"),
        "runId": record.get("runId"),
        "executionCwd": execution,
        "branch": record.get("branch"),
        "reason": reason,
    }
    if message:
        entry["message"] = message
    safe_actions = {
        "worktree_metadata_missing": "inspect_path_before_manual_cleanup",
        "branch_missing": "inspect_branch_metadata_before_manual_cleanup",
        "worktree_list_failed": "retry_after_git_worktree_list_succeeds",
    }
    if reason in safe_actions:
        entry["safeAction"] = safe_actions[reason]
    return entry


def _gc_reconcile_missing_path(
    registry_root: Path,
    record: PersistentWorktreeRecord,
    execution: str,
    *,
    dry_run: bool,
    reconciled: list[JsonObject],
    prune_roots: set[str],
) -> bool:
    if Path(execution).exists():
        return False
    if dry_run:
        reconciled.append(_gc_missing_entry(record, execution, dry_run=True))
        return True

    def mark_missing(
        fresh: PersistentWorktreeRecord, fresh_source: str, fresh_execution: str
    ) -> None:
        if Path(fresh_execution).exists():
            return
        run_registry.set_worktree_status_locked(
            registry_root,
            str(fresh["runId"]),
            STATUS_MISSING,
        )
        prune_roots.add(fresh_source)
        reconciled.append(_gc_missing_entry(fresh, fresh_execution, dry_run=False))

    _with_locked_fresh_gc_candidate(registry_root, record, mark_missing)
    return True


def _gc_reconcile_list_failure(
    registry_root: Path,
    record: PersistentWorktreeRecord,
    execution: str,
    *,
    listed_paths: set[str] | None,
    list_warning: str | None,
    dry_run: bool,
    orphans: list[JsonObject],
) -> bool:
    if listed_paths is not None:
        return False
    if dry_run:
        orphans.append(_gc_orphan_entry(record, execution, "worktree_list_failed", list_warning))
        return True

    def mark_unknown(
        fresh: PersistentWorktreeRecord, _fresh_source: str, fresh_execution: str
    ) -> None:
        if not Path(fresh_execution).exists():
            return
        run_registry.set_worktree_status_locked(
            registry_root,
            str(fresh["runId"]),
            STATUS_UNKNOWN,
        )
        orphans.append(
            _gc_orphan_entry(fresh, fresh_execution, "worktree_list_failed", list_warning)
        )

    _with_locked_fresh_gc_candidate(registry_root, record, mark_unknown)
    return True


def _gc_reconcile_missing_metadata(
    registry_root: Path,
    record: PersistentWorktreeRecord,
    execution: str,
    *,
    listed_paths: set[str] | None,
    dry_run: bool,
    orphans: list[JsonObject],
    warnings: list[JsonObject],
) -> bool:
    if listed_paths is None or _registered_worktree_path_matches(listed_paths, execution):
        return False
    if dry_run:
        orphans.append(_gc_orphan_entry(record, execution, "worktree_metadata_missing"))
        return True

    def mark_unknown_if_still_orphaned(
        fresh: PersistentWorktreeRecord,
        fresh_source: str,
        fresh_execution: str,
    ) -> None:
        fresh_paths, warning = wm._worktree_list_paths_with_warning(fresh_source)
        if warning is not None:
            warnings.append({"sourceGitRoot": fresh_source, "message": warning})
        if (
            Path(fresh_execution).exists()
            and fresh_paths is not None
            and not _registered_worktree_path_matches(fresh_paths, fresh_execution)
        ):
            run_registry.set_worktree_status_locked(
                registry_root,
                str(fresh["runId"]),
                STATUS_UNKNOWN,
            )
            orphans.append(_gc_orphan_entry(fresh, fresh_execution, "worktree_metadata_missing"))

    _with_locked_fresh_gc_candidate(registry_root, record, mark_unknown_if_still_orphaned)
    return True


def _gc_reconcile_missing_branch(
    registry_root: Path,
    record: PersistentWorktreeRecord,
    source: str,
    execution: str,
    branch: str | None,
    *,
    dry_run: bool,
    orphans: list[JsonObject],
) -> bool:
    if not isinstance(branch, str) or wm._branch_exists(source, branch) is not False:
        return False
    if dry_run:
        orphans.append(_gc_orphan_entry(record, execution, "branch_missing"))
        return True

    def mark_unknown_if_branch_still_missing(
        fresh: PersistentWorktreeRecord,
        fresh_source: str,
        fresh_execution: str,
    ) -> None:
        fresh_branch = fresh.get("branch")
        if (
            isinstance(fresh_branch, str)
            and Path(fresh_execution).exists()
            and wm._branch_exists(fresh_source, fresh_branch) is False
        ):
            run_registry.set_worktree_status_locked(
                registry_root,
                str(fresh["runId"]),
                STATUS_UNKNOWN,
            )
            orphans.append(_gc_orphan_entry(fresh, fresh_execution, "branch_missing"))

    _with_locked_fresh_gc_candidate(registry_root, record, mark_unknown_if_branch_still_missing)
    return True


def gc_worktrees(registry_root: Path, *, dry_run: bool = False) -> JsonObject:
    records = load_persistent_records(registry_root)
    paths_by_root: dict[str, set[str] | None] = {}
    prune_roots: set[str] = set()
    reconciled: list[JsonObject] = []
    orphans: list[JsonObject] = []
    warnings: list[JsonObject] = []

    for record in records:
        current = record.get("registryWorktreeStatus")
        if current not in (STATUS_PRESENT, STATUS_UNKNOWN, None):
            continue
        source = record.get("sourceGitRoot")
        execution = record.get("executionCwd")
        branch = record.get("branch")
        if not isinstance(source, str) or not isinstance(execution, str):
            continue
        if source not in paths_by_root:
            listed, warning = wm._worktree_list_paths_with_warning(source)
            paths_by_root[source] = listed
            if warning is not None:
                warnings.append({"sourceGitRoot": source, "message": warning})
        listed_paths = paths_by_root[source]
        list_warning = next(
            (
                str(warning.get("message"))
                for warning in warnings
                if warning.get("sourceGitRoot") == source
                and isinstance(warning.get("message"), str)
            ),
            None,
        )
        if _gc_reconcile_missing_path(
            registry_root,
            record,
            execution,
            dry_run=dry_run,
            reconciled=reconciled,
            prune_roots=prune_roots,
        ):
            continue
        if _gc_reconcile_list_failure(
            registry_root,
            record,
            execution,
            listed_paths=listed_paths,
            list_warning=list_warning,
            dry_run=dry_run,
            orphans=orphans,
        ):
            continue
        if _gc_reconcile_missing_metadata(
            registry_root,
            record,
            execution,
            listed_paths=listed_paths,
            dry_run=dry_run,
            orphans=orphans,
            warnings=warnings,
        ):
            continue
        _gc_reconcile_missing_branch(
            registry_root,
            record,
            source,
            execution,
            branch,
            dry_run=dry_run,
            orphans=orphans,
        )
    if not dry_run:
        for source in prune_roots:
            wm._run_git(source, ["worktree", "prune"])
    return {
        "schema": SCHEMA_GC,
        "ok": True,
        "dryRun": dry_run,
        "mode": "dry-run" if dry_run else "reconcile-registry",
        "effects": {
            "registryWrites": not dry_run,
            "deletesWorktreePaths": False,
            "runsGitWorktreePrune": (not dry_run and bool(prune_roots)),
        },
        "prunedSourceRoots": 0 if dry_run else len(prune_roots),
        "reconciled": len(reconciled),
        "reconciledEntries": reconciled,
        "orphans": orphans,
        "warnings": warnings,
    }


def maybe_auto_prune(
    registry_root: Path,
    config: JsonObject,
    *,
    no_auto_prune: bool = False,
) -> JsonObject | None:
    if no_auto_prune:
        return None
    worktrees_cfg = config.get("worktrees")
    if not isinstance(worktrees_cfg, dict):
        return None
    auto = worktrees_cfg.get("autoPrune")
    if not isinstance(auto, dict) or auto.get("enabled") is not True:
        return None
    days = auto.get("mergedOlderThanDays", 7)
    if not is_non_negative_int(days):
        days = 7
    try:
        # Probe lock availability without blocking. The actual prune uses
        # normal per-entry mutation locks so list doesn't monopolize the
        # registry across a large cleanup pass.
        with run_registry.registry_lock(registry_root, timeout_seconds=0.0):
            pass
    except TimeoutError:
        return {"ok": False, "skipped": True, "reason": "lock_contended"}
    try:
        return wm.prune_worktrees(
            registry_root,
            merged=True,
            older_than_days=days,
            dry_run=False,
        )
    except wm.WorktreeManagementError as exc:
        return dict(exc.payload)


# Deferred to the bottom to break the worktree_mgmt<->worktree_gc facade cycle:
# worktree_mgmt re-exports this module's surface (a top-level import here would
# fail when worktree_gc is imported first). All `wm.<seam>` access above is
# call-time, so binding the alias after our own definitions is sufficient and
# keeps the monkeypatch seams (mock.patch.object(worktree_mgmt, ...)) working.
from delegate_agent import worktree_mgmt as wm  # noqa: E402
