"""Persistent worktree management facade.

This module owns the worktree status/inspection layer (status detection, dirty
and merged checks, ahead/behind, record decoration, ``list``/``show``) and the
shared error envelope, and re-exports the record model (``worktree_records``),
the removal pipeline (``worktree_remove``), and the prune/gc pipelines
(``worktree_gc``) so callers — ``worktree_commands``, ``cli``, and the test
suite — keep importing the full surface from ``worktree_mgmt`` unchanged.

The seam functions tests monkeypatch via ``worktree_mgmt.<name>`` (e.g.
``_run_git``, ``porcelain_status``, ``merged_into_source``,
``detect_worktree_status``, ``_remove_branch``, ``prune_worktrees``) are either
defined here or re-exported here; the moved pipeline functions read them back
through this module so those patches keep taking effect.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from delegate_agent import run_registry, worktree_summary
from delegate_agent.git_utils import (
    GIT_QUICK_TIMEOUT_SECONDS,
    rev_parse_verify,
)
from delegate_agent.git_utils import (
    run_git as _run_git,
)
from delegate_agent.json_types import JsonObject
from delegate_agent.worktree_records import (  # noqa: F401  # re-exported
    MAX_DIRTY_PATHS_REPORTED,
    SCHEMA_GC,
    SCHEMA_LIST,
    SCHEMA_PRUNE,
    SCHEMA_REMOVE,
    SCHEMA_SHOW,
    STATUS_MISSING,
    STATUS_PRESENT,
    STATUS_REMOVED,
    STATUS_UNKNOWN,
    VALID_STATUSES,
    WORKTREE_ERROR_EXIT_CODE,
    PersistentWorktreeRecord,
    _branch_from,
    _creation_context,
    _execution_cwd_from,
    _get_dict,
    _get_str,
    _is_persistent_worktree_run,
    _record_for_run,
    _registered_worktree_path_matches,
    _registry_worktree_status,
    _reload_record,
    _shell,
    _source_git_root_from,
    _utc_now_iso,
    latest_persistent_record_for_harness,
    load_persistent_records,
)


class WorktreeManagementError(Exception):
    def __init__(self, payload: JsonObject) -> None:
        code = payload.get("code")
        message = payload.get("message")
        if not isinstance(code, str) or not code:
            raise ValueError("worktree error payload requires non-empty string code")
        if not isinstance(message, str) or not message:
            raise ValueError("worktree error payload requires non-empty string message")
        normalized = dict(payload)
        normalized["ok"] = False
        normalized["code"] = code
        normalized["error"] = str(normalized.get("error") or code)
        normalized["message"] = message
        exit_code = normalized.get("exitCode")
        normalized["exitCode"] = (
            exit_code if isinstance(exit_code, int) else WORKTREE_ERROR_EXIT_CODE
        )
        super().__init__(message)
        self.payload = normalized
        self.code = code
        self.message = message


def _branch_ref(branch: str) -> str:
    return branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"


def _branch_exists(source_git_root: str, branch: str) -> bool | None:
    result = _run_git(
        source_git_root,
        ["rev-parse", "--verify", "--quiet", _branch_ref(branch)],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def detect_worktree_status(record: PersistentWorktreeRecord) -> tuple[str, list[str]]:
    warnings: list[str] = []
    registry_status = record.get("registryWorktreeStatus")
    if registry_status == STATUS_REMOVED:
        return STATUS_REMOVED, warnings
    execution_cwd = record.get("executionCwd")
    source_git_root = record.get("sourceGitRoot")
    branch = record.get("branch")
    if not isinstance(execution_cwd, str) or not execution_cwd:
        return STATUS_UNKNOWN, ["missing executionCwd metadata"]
    if not Path(execution_cwd).exists():
        return STATUS_MISSING, warnings
    if not isinstance(source_git_root, str) or not source_git_root:
        return STATUS_UNKNOWN, ["missing sourceGitRoot metadata"]
    if not isinstance(branch, str) or not branch:
        return STATUS_UNKNOWN, ["missing branch metadata"]
    listed_paths, list_warning = _worktree_list_paths_with_warning(source_git_root)
    if listed_paths is None:
        return STATUS_UNKNOWN, [list_warning or "could not list registered git worktrees"]
    if not _registered_worktree_path_matches(listed_paths, execution_cwd):
        return STATUS_UNKNOWN, ["worktree path is not registered with git"]
    branch_exists = _branch_exists(source_git_root, branch)
    if branch_exists is True:
        return STATUS_PRESENT, warnings
    if branch_exists is False:
        return STATUS_UNKNOWN, ["branch does not resolve"]
    return STATUS_UNKNOWN, ["could not determine branch status"]


def porcelain_status(
    execution_cwd: str,
    *,
    limit: int | None = None,
) -> tuple[list[str] | None, int | None, list[str]]:
    result = _run_git(
        execution_cwd,
        ["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None, None, [f"git status failed: {result.stderr.strip()}"]
    lines = result.stdout.splitlines()
    if limit is None:
        return lines, len(lines), []
    return lines[:limit], len(lines), []


def dirty_info(
    record: PersistentWorktreeRecord, status: str
) -> tuple[bool | None, list[str], int | None, list[str]]:
    if status not in (STATUS_PRESENT, STATUS_UNKNOWN):
        return None, [], None, []
    execution_cwd = record.get("executionCwd")
    if not isinstance(execution_cwd, str) or not execution_cwd:
        return None, [], None, ["missing executionCwd metadata"]
    lines, total, warnings = porcelain_status(execution_cwd)
    if lines is None:
        return None, [], total, warnings
    return bool(lines), lines, total, warnings


def _merge_base_is_ancestor(source_git_root: str, branch: str) -> bool | None:
    result = _run_git(
        source_git_root,
        ["merge-base", "--is-ancestor", _branch_ref(branch), "HEAD"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def merged_into_source(
    record: PersistentWorktreeRecord,
    status: str,
    *,
    include_detached: bool = False,
) -> tuple[bool | None, list[str]]:
    if status not in (STATUS_PRESENT, STATUS_UNKNOWN):
        return None, []
    creation = record.get("creationContext")
    if (
        isinstance(creation, dict)
        and creation.get("sourceHeadRef") is None
        and not include_detached
    ):
        return None, ["source was detached at creation; integration target unknown"]
    source_git_root = record.get("sourceGitRoot")
    branch = record.get("branch")
    if not isinstance(source_git_root, str) or not isinstance(branch, str):
        return None, ["missing sourceGitRoot or branch metadata"]
    value = _merge_base_is_ancestor(source_git_root, branch)
    if value is None:
        return None, ["could not determine whether branch is merged into current source HEAD"]
    return value, []


def _rev_parse(source_git_root: str, rev: str) -> str | None:
    return rev_parse_verify(
        source_git_root,
        rev,
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        git_runner=_run_git,
    )


def _symbolic_ref(source_git_root: str, rev: str) -> str | None:
    """Return the symbolic ref (e.g. `refs/heads/main`) or None if detached/missing."""
    result = _run_git(
        source_git_root,
        ["symbolic-ref", "--quiet", rev],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _ahead_behind(source_git_root: str, branch: str, base: str) -> JsonObject | None:
    result = _run_git(
        source_git_root,
        ["rev-list", "--left-right", "--count", f"{base}...{branch}"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except ValueError:
        return None
    base_oid = _rev_parse(source_git_root, base) or base
    return {"ahead": ahead, "behind": behind, "baseOid": base_oid}


def ahead_behind(record: PersistentWorktreeRecord, status: str) -> JsonObject | None:
    if status not in (STATUS_PRESENT, STATUS_UNKNOWN):
        return None
    source_git_root = record.get("sourceGitRoot")
    branch = record.get("branch")
    creation = record.get("creationContext")
    if (
        not isinstance(source_git_root, str)
        or not isinstance(branch, str)
        or not isinstance(creation, dict)
    ):
        return None
    creation_base = creation.get("sourceHeadOid")
    vs_creation = (
        _ahead_behind(source_git_root, branch, creation_base)
        if isinstance(creation_base, str) and creation_base
        else None
    )
    current_head = _rev_parse(source_git_root, "HEAD")
    vs_current = _ahead_behind(source_git_root, branch, current_head) if current_head else None
    return {
        "vsCreationBase": vs_creation,
        "vsCurrentHead": vs_current,
    }


def _integration_status(
    *,
    branch_merged: bool | None,
    has_uncommitted_changes: bool | None,
) -> str | None:
    if branch_merged is None or has_uncommitted_changes is None:
        return "unknown"
    if branch_merged:
        return "branch-merged-worktree-dirty" if has_uncommitted_changes else "fully-integrated"
    return "branch-unmerged-worktree-dirty" if has_uncommitted_changes else "branch-unmerged"


def integration_fields(
    *,
    branch_merged: bool | None,
    has_uncommitted_changes: bool | None,
) -> JsonObject:
    fully_integrated = (
        branch_merged is True and has_uncommitted_changes is False
        if branch_merged is not None and has_uncommitted_changes is not None
        else None
    )
    uncommitted_changes_integrated = (
        None if has_uncommitted_changes is None else not has_uncommitted_changes
    )
    return {
        "branchMergedIntoSource": branch_merged,
        "hasUncommittedChanges": has_uncommitted_changes,
        # Backward compatibility: v1 exposed mergedIntoSource as the branch
        # ancestry check. Keep that meaning and expose the aggregate state under
        # a new field instead of silently repurposing the old one.
        "mergedIntoSource": branch_merged,
        "fullyIntegrated": fully_integrated,
        "integrationStatus": _integration_status(
            branch_merged=branch_merged,
            has_uncommitted_changes=has_uncommitted_changes,
        ),
        "uncommittedChangesIntegrated": uncommitted_changes_integrated,
    }


def _suppress_merge_suggestions(
    *,
    status: str,
    ahead_behind_payload: JsonObject | None,
) -> bool:
    if status not in (STATUS_PRESENT, STATUS_UNKNOWN):
        return False
    if not isinstance(ahead_behind_payload, dict):
        return False
    vs_current = ahead_behind_payload.get("vsCurrentHead")
    if not isinstance(vs_current, dict):
        return False
    ahead = vs_current.get("ahead")
    return ahead == 0


def suggested_commands(
    record: PersistentWorktreeRecord,
    status: str,
    *,
    ahead_behind_payload: JsonObject | None = None,
) -> JsonObject:
    alias = record.get("alias") or record.get("runId") or "<handle>"
    execution_cwd = record.get("executionCwd")
    source_git_root = record.get("sourceGitRoot")
    branch = record.get("branch")
    creation = record.get("creationContext")
    base = creation.get("sourceHeadOid") if isinstance(creation, dict) else None
    review_diff = None
    review_diff_base = None
    merge = None
    cherry = None
    if isinstance(execution_cwd, str) and status in (STATUS_PRESENT, STATUS_UNKNOWN):
        review_diff = _shell(["git", "-C", execution_cwd, "diff", "--stat", "HEAD"])
        if isinstance(base, str) and base:
            review_diff_base = _shell(["git", "-C", execution_cwd, "diff", "--stat", base])
    suppress_merge = _suppress_merge_suggestions(
        status=status,
        ahead_behind_payload=ahead_behind_payload,
    )
    if not suppress_merge and isinstance(source_git_root, str) and isinstance(branch, str):
        merge = _shell(["git", "-C", source_git_root, "merge", "--no-ff", branch])
        if isinstance(base, str) and base:
            cherry = _shell(["git", "-C", source_git_root, "cherry-pick", f"{base}..{branch}"])
    return {
        "reviewDiff": review_diff,
        "reviewDiffVsCreationBase": review_diff_base,
        "mergeIntoSource": merge,
        "cherryPickRange": cherry,
        "safeRemove": f"delegate worktree remove {alias}",
        "discardAndRemove": f"delegate worktree remove {alias} --discard-uncommitted",
    }


def decorate_record(
    record: PersistentWorktreeRecord,
    *,
    include_detached: bool = False,
    include_work_summary: bool = False,
    include_porcelain_cache: bool = False,
) -> JsonObject:
    status, warnings = detect_worktree_status(record)
    dirty, dirty_paths, dirty_total, dirty_warnings = dirty_info(record, status)
    branch_merged, merged_warnings = merged_into_source(
        record,
        status,
        include_detached=include_detached,
    )
    output: JsonObject = {
        "alias": record.get("alias"),
        "runId": record.get("runId"),
        "harness": record.get("harness"),
        "group": record.get("group"),
        "branch": record.get("branch"),
        "executionCwd": record.get("executionCwd"),
        "sourceGitRoot": record.get("sourceGitRoot"),
        "createdAt": record.get("createdAt"),
        "lastActivityAt": record.get("lastActivityAt"),
        "computedAt": _utc_now_iso(),
        "registryWorktreeStatus": record.get("registryWorktreeStatus"),
        "worktreeStatus": status,
        "dirty": dirty,
        **integration_fields(
            branch_merged=branch_merged,
            has_uncommitted_changes=dirty,
        ),
    }
    if record.get("resolutionKind") in {"latest", "latest_model"}:
        output["requestedHandle"] = record.get("requestedHandle")
        output["resolvedHandle"] = record.get("resolvedHandle")
        output["resolutionKind"] = record.get("resolutionKind")
    registry_status = record.get("registryWorktreeStatus")
    if isinstance(registry_status, str) and registry_status != status:
        output["registryStatusDiffers"] = True
    all_warnings = [*warnings, *dirty_warnings, *merged_warnings]
    if all_warnings:
        output["warnings"] = all_warnings
    if dirty_paths:
        output["dirtyPaths"] = dirty_paths[:MAX_DIRTY_PATHS_REPORTED]
        output["dirtyPathsTotal"] = dirty_total
    if include_porcelain_cache:
        output["_porcelainStatusLines"] = dirty_paths if dirty is not None else None
        output["_porcelainStatusTotalLines"] = dirty_total
        output["_porcelainStatusWarnings"] = dirty_warnings
    if include_work_summary and status in (STATUS_PRESENT, STATUS_UNKNOWN):
        prefetched_changed_files = (
            worktree_summary.changed_files_from_porcelain_lines(
                dirty_paths,
                dirty_total if isinstance(dirty_total, int) else len(dirty_paths),
            )
            if dirty is not None
            else None
        )
        summary = worktree_summary.build_work_summary(
            source_git_root=record.get("sourceGitRoot")
            if isinstance(record.get("sourceGitRoot"), str)
            else None,
            execution_cwd=record.get("executionCwd")
            if isinstance(record.get("executionCwd"), str)
            else "",
            branch=record.get("branch") if isinstance(record.get("branch"), str) else None,
            creation_context=record.get("creationContext")
            if isinstance(record.get("creationContext"), dict)
            else None,
            prefetched_changed_files=prefetched_changed_files,
        )
        if summary is not None:
            output["workSummary"] = summary
    return output


def list_worktrees(
    registry_root: Path,
    *,
    harness: str | None = None,
    group: str | None = None,
    status: str | None = None,
    limit: int = run_registry.DEFAULT_RUNS_LIMIT,
    include_detached: bool = False,
) -> JsonObject:
    entries: list[JsonObject] = []
    all_status_counts: dict[str, int] = {status_key: 0 for status_key in sorted(VALID_STATUSES)}
    records = load_persistent_records(registry_root)
    total_persistent = len(records)
    for record in records:
        if harness is not None and record.get("harness") != harness:
            continue
        if group is not None and record.get("group") != group:
            continue
        unfiltered_entry = decorate_record(record, include_detached=include_detached)
        unfiltered_status = unfiltered_entry.get("worktreeStatus")
        if isinstance(unfiltered_status, str):
            all_status_counts[unfiltered_status] = all_status_counts.get(unfiltered_status, 0) + 1
        entry = unfiltered_entry
        if status is not None and entry.get("worktreeStatus") != status:
            continue
        entries.append(entry)
    entries.sort(key=lambda item: str(item.get("lastActivityAt", "")), reverse=True)
    visible_status_counts: dict[str, int] = {}
    warning_count = 0
    registry_drift_count = 0
    for entry in entries:
        entry_status = entry.get("worktreeStatus")
        if isinstance(entry_status, str):
            visible_status_counts[entry_status] = visible_status_counts.get(entry_status, 0) + 1
        entry_warnings = entry.get("warnings")
        if isinstance(entry_warnings, list):
            warning_count += len(entry_warnings)
        if entry.get("registryStatusDiffers") is True:
            registry_drift_count += 1
    return {
        "schema": SCHEMA_LIST,
        "ok": True,
        "entries": entries[:limit],
        "limit": limit,
        "summary": {
            # Registry-wide count, independent of --harness/--status filters.
            "totalPersistentWorktrees": total_persistent,
            "visible": min(len(entries), limit),
            "matched": len(entries),
            "statusCounts": visible_status_counts,
            # Pre-status-filter counts within the --harness scope (contrast
            # with statusCounts, which reflects the visible filtered entries).
            "allStatusCounts": all_status_counts,
            "warningCount": warning_count,
            "registryStatusDriftCount": registry_drift_count,
            "readOnly": True,
        },
    }


def _error_payload(
    code: str,
    message: str,
    *,
    record: PersistentWorktreeRecord | JsonObject | None = None,
    dirty_paths: list[str] | None = None,
    next_actions: list[str] | None = None,
    retry_safe: bool = False,
    warnings: list[str] | None = None,
    suggestions: list[str] | None = None,
    suggestion_scope: str | None = None,
    list_command: str | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "ok": False,
        "code": code,
        "error": code,
        "message": message,
        "exitCode": WORKTREE_ERROR_EXIT_CODE,
        "retrySafe": retry_safe,
    }
    if record is not None:
        for key in ("alias", "runId", "branch", "executionCwd", "sourceGitRoot"):
            value = record.get(key)
            if isinstance(value, str) and value:
                payload[key] = value
    if dirty_paths is not None:
        capped = dirty_paths[:MAX_DIRTY_PATHS_REPORTED]
        if len(dirty_paths) > MAX_DIRTY_PATHS_REPORTED:
            capped.append("...")
            payload["dirtyPathsTotal"] = len(dirty_paths)
        payload["dirtyPaths"] = capped
    if next_actions:
        payload["nextActions"] = next_actions
    if warnings is not None:
        payload["warnings"] = warnings
    if suggestions is not None:
        payload["suggestions"] = suggestions
    if suggestion_scope is not None:
        payload["suggestionScope"] = suggestion_scope
    if list_command is not None:
        payload["listCommand"] = list_command
    return payload


def suggest_worktree_handles(
    registry_root: Path,
    handle: str,
    *,
    limit: int = 8,
) -> list[str]:
    records = load_persistent_records(registry_root)
    scoped_index: JsonObject = {"aliases": {}, "runs": {}}
    aliases = scoped_index["aliases"]
    runs = scoped_index["runs"]
    if not isinstance(aliases, dict) or not isinstance(runs, dict):
        return []
    for record in records:
        alias = record.get("alias")
        run_id = record.get("runId")
        if not isinstance(alias, str) or not alias:
            continue
        if not isinstance(run_id, str) or not run_id:
            continue
        aliases[alias] = run_id
        runs[run_id] = {"alias": alias}
    return run_registry.suggest_handles(scoped_index, handle, limit=limit)


def resolve_record(
    registry_root: Path,
    *,
    handle: str | None,
    latest_harness: str | None = None,
) -> PersistentWorktreeRecord:
    index = run_registry.load_index(registry_root)
    if latest_harness is not None:
        latest_record = latest_persistent_record_for_harness(registry_root, latest_harness)
        if latest_record is None:
            raise WorktreeManagementError(
                _error_payload(
                    "unknown_handle",
                    f"No persistent worktree runs found for harness: {latest_harness}",
                    next_actions=["delegate worktree list"],
                )
            )
        latest_record.update(
            {
                "requestedHandle": latest_harness,
                "resolvedHandle": latest_record.get("alias") or latest_record.get("runId"),
                "resolutionKind": "latest",
            }
        )
        return latest_record
    if handle is None:
        raise WorktreeManagementError(
            _error_payload(
                "missing_handle",
                "A worktree handle is required.",
                next_actions=["delegate worktree list"],
            )
        )
    if handle in run_registry.HARNESS_NAMES:
        latest_record = latest_persistent_record_for_harness(registry_root, handle)
        if latest_record is None:
            raise WorktreeManagementError(
                _error_payload(
                    "unknown_handle",
                    f"No persistent worktree runs found for harness: {handle}",
                    next_actions=["delegate worktree list"],
                )
            )
        latest_record.update(
            {
                "requestedHandle": handle,
                "resolvedHandle": latest_record.get("alias") or latest_record.get("runId"),
                "resolutionKind": "latest",
            }
        )
        return latest_record
    resolved = run_registry.resolve_handle(index, handle)
    if resolved.run_id is None:
        suggestions = suggest_worktree_handles(registry_root, handle)
        next_actions = (
            [f"delegate worktree show {suggestions[0]}"]
            if suggestions
            else ["delegate worktree list"]
        )
        raise WorktreeManagementError(
            _error_payload(
                "unknown_handle",
                (
                    f"Unknown run handle: {handle}. Suggestions: "
                    f"{', '.join(suggestions) if suggestions else '(none)'}"
                ),
                next_actions=next_actions,
                suggestions=suggestions,
                suggestion_scope="worktrees",
                list_command="delegate worktree list",
            )
        )
    run_id = resolved.run_id
    index_entry = index.get("runs", {}).get(run_id)
    record = _record_for_run(
        registry_root,
        run_id,
        index_entry if isinstance(index_entry, dict) else {},
    )
    if record is None:
        alias = run_registry.alias_for_run(index, run_id)
        raise WorktreeManagementError(
            _error_payload(
                "not_worktree_run",
                f"Run is not a persistent worktree run: {alias or run_id}",
                record={"alias": alias, "runId": run_id},
                next_actions=[f"delegate snapshot {alias or run_id}"],
            )
        )
    return record


def show_worktree(
    registry_root: Path,
    *,
    handle: str | None,
    latest_harness: str | None = None,
    include_detached: bool = False,
) -> JsonObject:
    record = resolve_record(registry_root, handle=handle, latest_harness=latest_harness)
    entry = decorate_record(
        record,
        include_detached=include_detached,
        include_work_summary=True,
        include_porcelain_cache=True,
    )
    execution_cwd = entry.get("executionCwd")
    cached_porcelain = entry.pop("_porcelainStatusLines", None)
    cached_porcelain_total = entry.pop("_porcelainStatusTotalLines", None)
    cached_porcelain_warnings = entry.pop("_porcelainStatusWarnings", [])
    porcelain: list[str] | None = None
    porcelain_total = 0
    porcelain_truncated = False
    if entry.get("worktreeStatus") in (STATUS_PRESENT, STATUS_UNKNOWN) and isinstance(
        execution_cwd, str
    ):
        if isinstance(cached_porcelain, list):
            porcelain = cached_porcelain[:50]
            porcelain_total = (
                cached_porcelain_total
                if isinstance(cached_porcelain_total, int)
                else len(cached_porcelain)
            )
            porcelain_truncated = porcelain_total > len(porcelain)
        elif isinstance(cached_porcelain_warnings, list) and cached_porcelain_warnings:
            entry["warnings"] = [*(entry.get("warnings") or []), *cached_porcelain_warnings]
    entry["schema"] = SCHEMA_SHOW
    entry["ok"] = True
    entry["creationContext"] = record.get("creationContext") or {}
    entry["porcelainStatus"] = porcelain
    entry["porcelainStatusTotalLines"] = porcelain_total
    entry["porcelainStatusTruncated"] = porcelain_truncated
    entry["aheadBehind"] = ahead_behind(record, str(entry.get("worktreeStatus")))
    entry["suggestedCommands"] = suggested_commands(
        record,
        str(entry.get("worktreeStatus")),
        ahead_behind_payload=entry.get("aheadBehind")
        if isinstance(entry.get("aheadBehind"), dict)
        else None,
    )
    source_git_root = record.get("sourceGitRoot")
    entry["currentSourceHeadRef"] = (
        _symbolic_ref(source_git_root, "HEAD") if isinstance(source_git_root, str) else None
    )
    creation = record.get("creationContext")
    if isinstance(creation, dict) and creation.get("sourceHeadRef") is None:
        warnings = list(entry.get("warnings") or [])
        warning = "source was detached at creation; integration target unknown"
        if warning not in warnings:
            warnings.append(warning)
        entry["warnings"] = warnings
    return entry


def _worktree_list_paths_with_warning(source_git_root: str) -> tuple[set[str] | None, str | None]:
    result = _run_git(
        source_git_root,
        ["worktree", "list", "--porcelain"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git worktree list failed with exit {result.returncode}"
        return None, detail
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
            paths.add(path)
            with suppress(OSError):
                paths.add(str(Path(path).resolve()))
    return paths, None


# Re-export the removal pipeline so ``worktree_mgmt.<name>`` keeps resolving and
# the seam patches that target this module continue to reach the moved
# functions (which read their cross-module seams back through this module).
# Re-export the prune/gc pipelines for the same reason.
from delegate_agent.worktree_gc import (  # noqa: E402, F401  # re-exported
    BACKLINK_MAX_BYTES,
    POOL_SETTLE_SECONDS,
    GcFreshAction,
    _admin_dir_serves_worktree,
    _classify_pool_worktree,
    _entry_ref,
    _gc_missing_entry,
    _gc_orphan_entry,
    _gc_reconcile_list_failure,
    _gc_reconcile_missing_branch,
    _gc_reconcile_missing_metadata,
    _gc_reconcile_missing_path,
    _older_than,
    _parse_worktree_backlink,
    _paths_match,
    _reload_gc_candidate,
    _source_root_from_backlink,
    _with_locked_fresh_gc_candidate,
    gc_worktrees,
    maybe_auto_prune,
    prune_worktrees,
    scan_worktree_pool,
)
from delegate_agent.worktree_remove import (  # noqa: E402, F401  # re-exported
    BranchRemovalResult,
    RemoveWorktreeOptions,
    RemoveWorktreePlan,
    _apply_branch_removal_result,
    _build_remove_worktree_plan,
    _mark_worktree_removed,
    _normalize_remove_options,
    _raise_if_dirty_without_discard,
    _raise_if_unmerged_without_override,
    _remove_already_removed,
    _remove_branch,
    _remove_branch_if_requested,
    _remove_missing_worktree_path,
    _remove_payload,
    _remove_present_worktree_path,
    _remove_worktree_path,
    _require_removal_metadata,
    remove_worktree,
)
