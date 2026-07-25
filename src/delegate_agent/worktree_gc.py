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

import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from delegate_agent import isolation, run_registry
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


ORPHAN_SAFE_ACTIONS = {
    "source_root_missing": "restore_source_root_or_inspect_path_before_manual_cleanup",
    "worktree_metadata_missing": "inspect_path_before_manual_cleanup",
    "branch_missing": "inspect_branch_metadata_before_manual_cleanup",
    "detached_backlink": "inspect_path_before_manual_cleanup",
    "worktree_list_failed": "retry_after_git_worktree_list_succeeds",
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
    if reason in ORPHAN_SAFE_ACTIONS:
        entry["safeAction"] = ORPHAN_SAFE_ACTIONS[reason]
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
    source = record.get("sourceGitRoot")
    if isinstance(source, str) and not Path(source).exists():
        return False
    if dry_run:
        if isinstance(source, str):
            prune_roots.add(source)
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
    source = record.get("sourceGitRoot")
    reason = (
        "source_root_missing"
        if isinstance(source, str) and not Path(source).exists()
        else "worktree_list_failed"
    )
    if dry_run:
        orphans.append(_gc_orphan_entry(record, execution, reason, list_warning))
        return True

    def mark_unknown(
        fresh: PersistentWorktreeRecord, fresh_source: str, fresh_execution: str
    ) -> None:
        source_missing = not Path(fresh_source).exists()
        if not source_missing and not Path(fresh_execution).exists():
            return
        fresh_reason = "source_root_missing" if source_missing else "worktree_list_failed"
        run_registry.set_worktree_status_locked(
            registry_root,
            str(fresh["runId"]),
            STATUS_UNKNOWN,
        )
        orphans.append(_gc_orphan_entry(fresh, fresh_execution, fresh_reason, list_warning))

    _with_locked_fresh_gc_candidate(registry_root, record, mark_unknown)
    return True


def _resolved_git_common_dir(worktree: str) -> Path | None:
    result = wm._run_git(worktree, ["rev-parse", "--git-common-dir"])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = Path(worktree) / common_dir
    try:
        return common_dir.resolve()
    except (OSError, RuntimeError):
        return None


def _has_detached_backlink(source: str, execution: str) -> bool:
    source_common_dir = _resolved_git_common_dir(source)
    execution_common_dir = _resolved_git_common_dir(execution)
    return (
        source_common_dir is not None
        and execution_common_dir is not None
        and source_common_dir != execution_common_dir
    )


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
        source = record.get("sourceGitRoot")
        reason = (
            "detached_backlink"
            if isinstance(source, str) and _has_detached_backlink(source, execution)
            else "worktree_metadata_missing"
        )
        orphans.append(_gc_orphan_entry(record, execution, reason))
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
            fresh_reason = (
                "detached_backlink"
                if _has_detached_backlink(fresh_source, fresh_execution)
                else "worktree_metadata_missing"
            )
            run_registry.set_worktree_status_locked(
                registry_root,
                str(fresh["runId"]),
                STATUS_UNKNOWN,
            )
            orphans.append(_gc_orphan_entry(fresh, fresh_execution, fresh_reason))

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


WORKTREE_BACKLINK_MARKER = "/.git/worktrees/"


def _gc_mode(dry_run: bool, registry_root: Path | None) -> str:
    if dry_run:
        return "dry-run"
    return "report-pool" if registry_root is None else "reconcile-registry"


# A real backlink file is one short line. Anything larger is not one, and the
# walk crosses paths it does not control, so the read is bounded rather than
# trusting the entry to be small.
BACKLINK_MAX_BYTES = 4096


# Opening a file inside a directory Delegate does not own is only safe with both
# flags: without O_NOFOLLOW the read follows a symlink out of the pool, and
# without O_NONBLOCK opening a FIFO blocks before the regular-file check can
# reject it. Degrading to a plain open would trade a report for those risks, so
# a platform missing either flag gets no read at all.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
_NONBLOCK = getattr(os, "O_NONBLOCK", None)
BACKLINK_OPEN_FLAGS: int | None = (
    None if _NOFOLLOW is None or _NONBLOCK is None else os.O_RDONLY | _NOFOLLOW | _NONBLOCK
)


class BacklinkRead(NamedTuple):
    """One attempt to read a Git pointer file, kept deliberately tri-state.

    ``raw`` holds the bytes when the read succeeded. ``unverifiable`` describes,
    for a human, a read that established nothing — refused, failed, or capped —
    and is what separates those from a file that is definitely not there. Only a
    settled absence may feed an orphan report: a false orphan invites someone to
    delete real work by hand, while a false live merely under-reports.
    """

    raw: bytes | None = None
    unverifiable: str | None = None


def _read_backlink_file(path: Path) -> BacklinkRead:
    """Read a Git pointer file without following it anywhere.

    Oversized content is rejected rather than truncated, since half a path is
    worse than no path — but rejected as unverifiable, not as absence.
    """
    flags = BACKLINK_OPEN_FLAGS
    if flags is None:
        return BacklinkRead(unverifiable="cannot be opened safely on this platform")
    try:
        handle = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        return BacklinkRead()
    except OSError as error:
        return BacklinkRead(unverifiable=f"could not be opened ({error.strerror})")
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            return BacklinkRead(unverifiable="is not a regular file")
        raw = os.read(handle, BACKLINK_MAX_BYTES + 1)
    except OSError as error:
        return BacklinkRead(unverifiable=f"could not be read ({error.strerror})")
    finally:
        os.close(handle)
    if len(raw) > BACKLINK_MAX_BYTES:
        return BacklinkRead(unverifiable=f"is larger than {BACKLINK_MAX_BYTES} bytes")
    return BacklinkRead(raw=raw)


def _decode_path_bytes(raw: bytes) -> str:
    """Decode filesystem bytes losslessly, matching how paths round-trip in Python."""
    return raw.decode("utf-8", errors="surrogateescape")


class Backlink(NamedTuple):
    """Where a worktree's ``.git`` file points, or why that could not be settled.

    ``gitdir`` set means a usable target was read. Both fields unset means the
    backlink is definitely gone or definitely unusable — the only state that may
    become an orphan. ``unverifiable`` set means the file exists in some form
    that could not be read, which is reported as a warning and treated as live.
    """

    gitdir: str | None = None
    unverifiable: str | None = None


def _parse_worktree_backlink(worktree: Path) -> Backlink:
    """Return the ``gitdir:`` target recorded in ``<worktree>/.git``.

    Read as plain text on purpose. Inside an orphaned worktree every ``git``
    invocation hard-fails (``fatal: not a git repository``, exit 128), so the
    git-shelling classifiers cannot see this population at all.
    """
    read = _read_backlink_file(worktree / ".git")
    if read.unverifiable is not None:
        return Backlink(unverifiable=read.unverifiable)
    if read.raw is None:
        return Backlink()
    for line in _decode_path_bytes(read.raw).splitlines():
        stripped = line.strip()
        if stripped.startswith("gitdir:"):
            target = stripped.removeprefix("gitdir:").strip()
            return Backlink(gitdir=target) if target else Backlink()
    return Backlink()


def _resolve_backlink_target(worktree: Path, gitdir: str) -> Path:
    """Resolve a ``gitdir:`` value the way Git does: relative to the worktree.

    Git writes absolute backlinks by default but supports relative ones
    (``worktree.useRelativePaths``). Resolving those against the process CWD
    would report a live worktree as an orphan.
    """
    target = Path(gitdir)
    return target if target.is_absolute() else worktree / target


def _source_root_from_backlink(gitdir: str) -> str | None:
    """Recover the source checkout from a worktree backlink, for reporting only.

    Exact for the standard ``<source>/.git/worktrees/<name>`` layout; returns
    None under ``--separate-git-dir`` or a bare repo rather than guessing.
    """
    marker = gitdir.rfind(WORKTREE_BACKLINK_MARKER)
    return gitdir[:marker] if marker > 0 else None


def _paths_match(left: Path, right: Path) -> bool:
    """Compare two paths that may be spelled differently but name the same place.

    ``samefile`` first, because string comparison of two spellings is wrong in
    the dangerous direction here: a case-insensitive, bind-mounted, or network
    filesystem can spell one file two ways, and a mismatch makes a healthy
    worktree look orphaned. It needs both paths to exist, so string comparison
    remains the fallback for the case where one of them does not.
    """
    if os.path.normpath(str(left)) == os.path.normpath(str(right)):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        pass
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return False


def _admin_dir_serves_worktree(admin_dir: Path, worktree: Path) -> bool | None:
    """Is ``admin_dir`` the Git admin directory for ``worktree``?

    A live worktree's admin directory holds a ``gitdir`` backfile naming that
    worktree's ``.git`` file. Requiring the round trip is what stops a stale or
    half-built admin directory — or a backlink pointing at something that merely
    exists, like ``/`` — from masking a real orphan.

    Returns None when the answer cannot be established (unreadable metadata):
    callers must treat that as live, because a wrong "orphan" invites someone to
    delete work by hand while a wrong "live" only under-reports.

    ``os.stat`` rather than ``Path.is_dir``: the latter reports an inaccessible
    path as a missing one, which is exactly the ambiguity this must not collapse.
    """
    backfile = admin_dir / "gitdir"
    for path, kind in ((admin_dir, stat.S_ISDIR), (backfile, stat.S_ISREG)):
        try:
            info = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return None
        if not kind(info.st_mode):
            return False
    read = _read_backlink_file(backfile)
    if read.unverifiable is not None:
        return None
    if read.raw is None:
        return False
    recorded = _decode_path_bytes(read.raw).strip()
    if not recorded:
        return False
    target = Path(recorded)
    if not target.is_absolute():
        target = admin_dir / target
    return _paths_match(target, worktree / ".git") or _paths_match(target, worktree)


class PoolVerdict(NamedTuple):
    """The outcome of classifying one pooled worktree: at most one field is set.

    Both unset means the worktree is live and needs no mention.
    """

    orphan: JsonObject | None = None
    warning: JsonObject | None = None


def _pool_orphan_entry(
    worktree: Path, gitdir: str | None, source_root: str | None, reason: str
) -> JsonObject:
    return {
        "worktreePath": str(worktree),
        "fingerprint": worktree.parent.name,
        "sourceGitRoot": source_root,
        "gitdir": gitdir,
        "reason": reason,
        "safeAction": ORPHAN_SAFE_ACTIONS[reason],
    }


def _pool_warning(path: Path, reason: str, message: str) -> JsonObject:
    return {"path": str(path), "reason": reason, "message": message}


POOL_UNRECOGNIZED_SAMPLE = 5


def _unrecognized_dirs_warning(data_home: Path, names: list[str]) -> JsonObject:
    """Summarize every non-fingerprint directory in one warning, not one apiece.

    A root that is not a pool at all — ``--pool ~`` — has hundreds of children,
    and a line for each would bury whatever the report actually found.
    """
    sample = ", ".join(names[:POOL_UNRECOGNIZED_SAMPLE])
    remaining = len(names) - POOL_UNRECOGNIZED_SAMPLE
    if remaining > 0:
        sample = f"{sample}, and {remaining} more"
    noun = "directory" if len(names) == 1 else "directories"
    return _pool_warning(
        data_home,
        "not_a_pool_fingerprint_dir",
        (
            f"Skipped {len(names)} {noun} under {data_home} whose names are not Delegate "
            f"repository fingerprints, so nothing under them was examined ({sample})."
        ),
    )


def _unverifiable_worktree_warning(worktree: Path, detail: str) -> JsonObject:
    return _pool_warning(
        worktree,
        "worktree_metadata_unverifiable",
        f"{worktree}: {detail}. Treated as live and not reported as an orphan.",
    )


def _classify_pool_worktree(worktree: Path) -> PoolVerdict:
    """Classify one pooled worktree as an orphan, a warning, or neither.

    The orphan predicate is "this worktree's Git admin directory no longer
    serves it". It is safe to apply across repositories because git refuses to
    operate in a worktree whose backlink is broken — a live worktree necessarily
    has a live backlink, so this walk can never misjudge a worktree another
    repo's run is currently using.

    Every step that cannot reach an answer becomes a warning instead. Silence
    would be defensible for the walk itself, but the report is what an operator
    reads before deleting directories by hand, and a worktree Delegate could not
    inspect must not simply vanish from it.
    """
    backlink = _parse_worktree_backlink(worktree)
    if backlink.unverifiable is not None:
        return PoolVerdict(
            warning=_unverifiable_worktree_warning(
                worktree, f"its .git entry {backlink.unverifiable}"
            )
        )
    if backlink.gitdir is None:
        return PoolVerdict(
            orphan=_pool_orphan_entry(worktree, None, None, "worktree_metadata_missing")
        )
    admin_dir = _resolve_backlink_target(worktree, backlink.gitdir)
    serves = _admin_dir_serves_worktree(admin_dir, worktree)
    if serves is None:
        return PoolVerdict(
            warning=_unverifiable_worktree_warning(
                worktree, f"its Git admin directory ({admin_dir}) could not be inspected"
            )
        )
    if serves:
        return PoolVerdict()
    source_root = _source_root_from_backlink(os.path.normpath(str(admin_dir)))
    reason = (
        "worktree_metadata_missing"
        if source_root is not None and Path(source_root).exists()
        else "source_root_missing"
    )
    return PoolVerdict(orphan=_pool_orphan_entry(worktree, backlink.gitdir, source_root, reason))


POOL_ROOT_PROBLEM_MESSAGES = {
    "missing": "Worktree pool root does not exist: {path}",
    "not_a_directory": "Worktree pool root is not a directory: {path}",
    "unreadable": "Worktree pool root could not be read: {path}",
}


def _pool_root_problem_payload(data_home: Path, problem: str) -> JsonObject:
    return {
        "path": str(data_home),
        "reason": f"pool_root_{problem}",
        "message": POOL_ROOT_PROBLEM_MESSAGES[problem].format(path=data_home),
    }


def _invalid_pool_root_error(data_home: Path, detail: JsonObject) -> wm.WorktreeManagementError:
    return wm.WorktreeManagementError(
        {
            "ok": False,
            "code": "invalid_pool_root",
            "message": str(detail["message"]),
            "poolRoot": str(data_home),
            "reason": detail["reason"],
            "nextActions": ["pass --pool with an existing, readable pool directory"],
            "retrySafe": False,
        }
    )


def _empty_pool_result(data_home: Path, warnings: list[JsonObject]) -> JsonObject:
    return {
        "dataHome": str(data_home),
        "scannedWorktrees": 0,
        "orphans": [],
        "emptyFingerprintDirs": [],
        "warnings": warnings,
    }


def scan_worktree_pool(data_home: Path, *, required: bool = False) -> JsonObject:
    """Report pooled worktrees whose backing repository is gone, machine-wide.

    Report-only, in full: this walk removes nothing at all, not even the empty
    fingerprint directories it reports. An orphan's dirtiness is structurally
    unknowable (``git status`` cannot run there), so nothing can distinguish
    committed from uncommitted work, and a walk that crosses every repository on
    the machine has no business holding a delete. Emptiness is reported so the
    directories can be cleared by hand, and means the directory holds nothing at
    all — a fingerprint directory with loose files in it is not empty.

    Only first-level directories named like a repository fingerprint are treated
    as Delegate's; the root may be any path the caller passed, and a report that
    an operator acts on by hand must never describe a stranger's directory.

    ``required`` marks a root the caller named explicitly, where an unusable
    path is a mistake worth failing on — including one that becomes unusable
    between the check and the walk. The configured root is not required: it
    simply does not exist until the first persistent worktree, so a problem
    there is reported as a warning instead of a silent empty scan.
    """
    problem = isolation.pool_root_problem(data_home)
    if problem is not None:
        detail = _pool_root_problem_payload(data_home, problem)
        if required:
            raise _invalid_pool_root_error(data_home, detail)
        return _empty_pool_result(data_home, [detail])
    orphans: list[JsonObject] = []
    empty_dirs: list[JsonObject] = []
    warnings: list[JsonObject] = []
    unrecognized: list[str] = []
    scanned = 0
    try:
        for fingerprint in isolation.iter_pool_fingerprints(data_home):
            if fingerprint.unrecognized:
                unrecognized.append(fingerprint.path.name)
                continue
            if fingerprint.unreadable:
                warnings.append(
                    _pool_warning(
                        fingerprint.path,
                        "fingerprint_dir_unreadable",
                        (
                            f"Could not list {fingerprint.path}; "
                            "any worktrees under it were not scanned."
                        ),
                    )
                )
                continue
            if not fingerprint.worktrees:
                if fingerprint.other_entries:
                    warnings.append(
                        _pool_warning(
                            fingerprint.path,
                            "fingerprint_dir_not_empty",
                            (
                                f"{fingerprint.path} holds no worktree directories but is "
                                "not empty, so it is not reported as an empty directory."
                            ),
                        )
                    )
                else:
                    empty_dirs.append(
                        {"fingerprint": fingerprint.path.name, "path": str(fingerprint.path)}
                    )
                continue
            for worktree in fingerprint.worktrees:
                scanned += 1
                verdict = _classify_pool_worktree(worktree)
                if verdict.orphan is not None:
                    orphans.append(verdict.orphan)
                elif verdict.warning is not None:
                    warnings.append(verdict.warning)
    except isolation.PoolRootUnreadable as error:
        # Only the root listing raises, and it happens before the first yield,
        # so nothing collected above is discarded here.
        detail = _pool_root_problem_payload(data_home, error.reason)
        if required:
            raise _invalid_pool_root_error(data_home, detail) from error
        return _empty_pool_result(data_home, [detail])
    if unrecognized:
        warnings.append(_unrecognized_dirs_warning(data_home, unrecognized))
    return {
        "dataHome": str(data_home),
        "scannedWorktrees": scanned,
        "orphans": orphans,
        "emptyFingerprintDirs": empty_dirs,
        "warnings": warnings,
    }


def gc_worktrees(
    registry_root: Path | None,
    *,
    dry_run: bool = False,
    pool_data_home: Path | None = None,
    pool_required: bool = False,
) -> JsonObject:
    # Scanned before the registry pass so an unusable --pool fails without
    # having written anything. The pool walk is read-only and the registry pass
    # never deletes worktree paths, so neither ordering changes the result.
    pool = (
        wm.scan_worktree_pool(pool_data_home, required=pool_required)
        if pool_data_home is not None
        else None
    )
    records = load_persistent_records(registry_root) if registry_root is not None else []
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
    payload: JsonObject = {
        "schema": SCHEMA_GC,
        "ok": True,
        "dryRun": dry_run,
        "mode": _gc_mode(dry_run, registry_root),
        "effects": {
            "registryWrites": not dry_run and registry_root is not None,
            "deletesWorktreePaths": False,
            "runsGitWorktreePrune": (not dry_run and bool(prune_roots)),
        },
        "prunedSourceRoots": 0 if dry_run else len(prune_roots),
        "wouldPruneSourceRoots": len(prune_roots) if dry_run else 0,
        "reconciled": len(reconciled),
        "reconciledEntries": reconciled,
        "orphans": orphans,
        "warnings": warnings,
    }
    if pool is not None:
        # Kept out of the registry-shaped top-level ``orphans`` list: pool
        # entries are keyed by path, not by alias/runId, and existing consumers
        # read ``orphans`` expecting registry records.
        payload["pool"] = pool
    return payload


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
