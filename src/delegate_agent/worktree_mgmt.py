from __future__ import annotations

import shlex
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delegate_agent import run_registry
from delegate_agent.git_utils import (
    GIT_MUTATION_TIMEOUT_SECONDS,
    GIT_QUICK_TIMEOUT_SECONDS,
    GIT_TIMEOUT_RETURN_CODE,
    timeout_completed_process,
)
from delegate_agent.json_types import JsonObject, is_non_negative_int

SCHEMA_LIST = "delegate.worktree-list.v1"
SCHEMA_SHOW = "delegate.worktree-show.v1"
SCHEMA_REMOVE = "delegate.worktree-remove.v1"
SCHEMA_PRUNE = "delegate.worktree-prune.v1"
SCHEMA_GC = "delegate.worktree-gc.v1"
WORKTREE_ERROR_EXIT_CODE = 2
MAX_DIRTY_PATHS_REPORTED = 20

STATUS_PRESENT = "present"
STATUS_REMOVED = "removed"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"
VALID_STATUSES = {STATUS_PRESENT, STATUS_REMOVED, STATUS_MISSING, STATUS_UNKNOWN}


def _registered_worktree_path_matches(listed_paths: set[str], execution_cwd: str) -> bool:
    if execution_cwd in listed_paths:
        return True
    try:
        return str(Path(execution_cwd).resolve()) in listed_paths
    except OSError:
        return False


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


@dataclass(frozen=True)
class BranchRemovalResult:
    removed: bool
    kept_reason: str | None = None
    error: str | None = None
    error_code: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime(run_registry.UTC_TIMESTAMP_FORMAT)


def _shell(args: list[str]) -> str:
    return shlex.join(args)


def _run_git(
    cwd: str,
    args: list[str],
    *,
    timeout_seconds: int = GIT_MUTATION_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", cwd, *args]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return timeout_completed_process(
            command,
            exc,
            timeout_seconds=timeout_seconds,
        )


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _get_str(source: object, key: str) -> str | None:
    if not isinstance(source, dict):
        return None
    value = source.get(key)
    return value if isinstance(value, str) and value else None


def _get_dict(source: object, key: str) -> JsonObject:
    if not isinstance(source, dict):
        return {}
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _creation_context(manifest: JsonObject | None, snapshot: JsonObject | None) -> JsonObject:
    for source in (manifest, snapshot):
        value = _get_dict(source, "creationContext")
        if value:
            return value
    return {}


def _branch_from(manifest: JsonObject | None, snapshot: JsonObject | None) -> str | None:
    creation = _creation_context(manifest, snapshot)
    return _first_string(
        _get_str(manifest, "branch"),
        _get_str(snapshot, "branch"),
        _get_str(creation, "branch"),
        _get_str(creation, "plannedBranch"),
    )


def _execution_cwd_from(
    manifest: JsonObject | None,
    snapshot: JsonObject | None,
    state: JsonObject | None,
) -> str | None:
    return _first_string(
        _get_str(manifest, "executionCwd"),
        _get_str(snapshot, "executionCwd"),
        _get_str(state, "plannedExecutionCwd"),
    )


def _source_git_root_from(manifest: JsonObject | None, snapshot: JsonObject | None) -> str | None:
    return _first_string(
        _get_str(manifest, "sourceGitRoot"),
        _get_str(snapshot, "sourceGitRoot"),
        _get_str(manifest, "cwd"),
        _get_str(snapshot, "cwd"),
    )


def _registry_worktree_status(
    state: JsonObject | None,
    manifest: JsonObject | None,
    snapshot: JsonObject | None,
) -> str | None:
    for source in (state, manifest, snapshot):
        value = _get_str(source, "worktreeStatus")
        if isinstance(value, str) and value in VALID_STATUSES:
            return value
    return None


def _is_persistent_worktree_run(
    state: JsonObject | None,
    manifest: JsonObject | None,
    snapshot: JsonObject | None,
) -> bool:
    for source in (state, manifest, snapshot):
        if not isinstance(source, dict):
            continue
        if source.get("isolationLifecycle") == "persistent":
            return True
        if source.get("preservedWorkspace") is True:
            return True
    return _registry_worktree_status(state, manifest, snapshot) is not None


def _record_for_run(
    registry_root: Path,
    run_id: str,
    index_entry: JsonObject | None,
) -> JsonObject | None:
    state = run_registry.load_run_state(registry_root, run_id)
    manifest = run_registry.load_run_manifest(registry_root, run_id)
    snapshot = run_registry.load_run_snapshot(registry_root, run_id)
    if not _is_persistent_worktree_run(state, manifest, snapshot):
        return None
    entry = index_entry if isinstance(index_entry, dict) else {}
    alias = _first_string(
        _get_str(state, "alias"),
        _get_str(manifest, "alias"),
        _get_str(snapshot, "alias"),
        _get_str(entry, "alias"),
    )
    harness = _first_string(
        _get_str(entry, "harness"),
        _get_str(manifest, "harness"),
        _get_str(snapshot, "harness"),
    )
    creation = _creation_context(manifest, snapshot)
    created_at = _first_string(
        _get_str(manifest, "startedAt"),
        _get_str(snapshot, "startedAt"),
        run_registry.timestamp_from_run_id(run_id),
    )
    last_activity = run_registry.activity_timestamp(state, manifest, run_id)
    return {
        "alias": alias,
        "runId": run_id,
        "harness": harness,
        "branch": _branch_from(manifest, snapshot),
        "executionCwd": _execution_cwd_from(manifest, snapshot, state),
        "sourceGitRoot": _source_git_root_from(manifest, snapshot),
        "createdAt": created_at,
        "lastActivityAt": last_activity,
        "creationContext": creation,
        "registryWorktreeStatus": _registry_worktree_status(state, manifest, snapshot),
    }


def load_persistent_records(registry_root: Path) -> list[JsonObject]:
    index = run_registry.load_index(registry_root)
    records: list[JsonObject] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(run_id, str) or not isinstance(entry, dict):
            continue
        record = _record_for_run(registry_root, run_id, entry)
        if record is not None:
            records.append(record)
    return records


def latest_persistent_record_for_harness(
    registry_root: Path,
    harness: str,
) -> JsonObject | None:
    matches = [
        record
        for record in load_persistent_records(registry_root)
        if record.get("harness") == harness
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda record: (
            str(record.get("lastActivityAt") or ""),
            str(record.get("runId") or ""),
        ),
        reverse=True,
    )
    return matches[0]


def _reload_record(registry_root: Path, run_id: str) -> JsonObject | None:
    index = run_registry.load_index(registry_root)
    index_entry = index.get("runs", {}).get(run_id)
    return _record_for_run(
        registry_root, run_id, index_entry if isinstance(index_entry, dict) else {}
    )


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


def detect_worktree_status(record: JsonObject) -> tuple[str, list[str]]:
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
    record: JsonObject, status: str
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
    record: JsonObject,
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
    result = _run_git(
        source_git_root,
        ["rev-parse", "--verify", rev],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


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


def ahead_behind(record: JsonObject, status: str) -> JsonObject | None:
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


def suggested_commands(record: JsonObject, status: str) -> JsonObject:
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
    if isinstance(source_git_root, str) and isinstance(branch, str):
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


def decorate_record(record: JsonObject, *, include_detached: bool = False) -> JsonObject:
    status, warnings = detect_worktree_status(record)
    dirty, dirty_paths, dirty_total, dirty_warnings = dirty_info(record, status)
    merged, merged_warnings = merged_into_source(
        record,
        status,
        include_detached=include_detached,
    )
    output: JsonObject = {
        "alias": record.get("alias"),
        "runId": record.get("runId"),
        "harness": record.get("harness"),
        "branch": record.get("branch"),
        "executionCwd": record.get("executionCwd"),
        "sourceGitRoot": record.get("sourceGitRoot"),
        "createdAt": record.get("createdAt"),
        "lastActivityAt": record.get("lastActivityAt"),
        "computedAt": _utc_now_iso(),
        "registryWorktreeStatus": record.get("registryWorktreeStatus"),
        "worktreeStatus": status,
        "dirty": dirty,
        "mergedIntoSource": merged,
    }
    registry_status = record.get("registryWorktreeStatus")
    if isinstance(registry_status, str) and registry_status != status:
        output["registryStatusDiffers"] = True
    all_warnings = [*warnings, *dirty_warnings, *merged_warnings]
    if all_warnings:
        output["warnings"] = all_warnings
    if dirty_paths:
        output["dirtyPaths"] = dirty_paths[:MAX_DIRTY_PATHS_REPORTED]
        output["dirtyPathsTotal"] = dirty_total
    return output


def list_worktrees(
    registry_root: Path,
    *,
    harness: str | None = None,
    status: str | None = None,
    limit: int = run_registry.DEFAULT_RUNS_LIMIT,
    include_detached: bool = False,
) -> JsonObject:
    entries: list[JsonObject] = []
    all_status_counts: dict[str, int] = {status_key: 0 for status_key in sorted(VALID_STATUSES)}
    for record in load_persistent_records(registry_root):
        if harness is not None and record.get("harness") != harness:
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
            "totalPersistentWorktrees": sum(all_status_counts.values()),
            "visible": min(len(entries), limit),
            "matched": len(entries),
            "statusCounts": visible_status_counts,
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
    record: JsonObject | None = None,
    dirty_paths: list[str] | None = None,
    next_actions: list[str] | None = None,
    retry_safe: bool = False,
    **extra: object,
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
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def resolve_record(
    registry_root: Path,
    *,
    handle: str | None,
    latest_harness: str | None = None,
) -> JsonObject:
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
        return latest_record
    else:
        assert handle is not None
        resolved = run_registry.resolve_handle(index, handle)
        if resolved.run_id is None:
            suggestions = list(resolved.suggestions)
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
    entry = decorate_record(record, include_detached=include_detached)
    execution_cwd = entry.get("executionCwd")
    porcelain: list[str] | None = None
    porcelain_total = 0
    porcelain_truncated = False
    if entry.get("worktreeStatus") in (STATUS_PRESENT, STATUS_UNKNOWN) and isinstance(
        execution_cwd, str
    ):
        lines, total, warnings = porcelain_status(execution_cwd, limit=50)
        if lines is not None:
            porcelain = lines
            porcelain_total = int(total or 0)
            porcelain_truncated = porcelain_total > len(lines)
        elif warnings:
            entry["warnings"] = [*(entry.get("warnings") or []), *warnings]
    entry["schema"] = SCHEMA_SHOW
    entry["ok"] = True
    entry["creationContext"] = record.get("creationContext") or {}
    entry["porcelainStatus"] = porcelain
    entry["porcelainStatusTotalLines"] = porcelain_total
    entry["porcelainStatusTruncated"] = porcelain_truncated
    entry["aheadBehind"] = ahead_behind(record, str(entry.get("worktreeStatus")))
    entry["suggestedCommands"] = suggested_commands(record, str(entry.get("worktreeStatus")))
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


def _remove_branch(source_git_root: str, branch: str, *, force: bool) -> BranchRemovalResult:
    flag = "-D" if force else "-d"
    result = _run_git(source_git_root, ["branch", flag, branch])
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
        raise WorktreeManagementError(
            _error_payload(
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
    record: JsonObject,
    alias: str,
) -> None:
    """Fail closed when dirty state is unsafe to discard implicitly."""
    if dirty is None and not discard_uncommitted:
        raise WorktreeManagementError(
            _error_payload(
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
        raise WorktreeManagementError(
            _error_payload(
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
    record: JsonObject,
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
    merged, merge_warnings = merged_into_source(record, status)
    if merged is None:
        raise WorktreeManagementError(
            _error_payload(
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
        raise WorktreeManagementError(
            _error_payload(
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


def _require_removal_metadata(record: JsonObject) -> tuple[str, str]:
    """Validate that ``sourceGitRoot`` and ``executionCwd`` are present.

    Returns ``(source_git_root, execution_cwd)`` for direct use by the caller.
    """
    source_git_root = record.get("sourceGitRoot")
    execution_cwd = record.get("executionCwd")
    if not isinstance(source_git_root, str) or not isinstance(execution_cwd, str):
        raise WorktreeManagementError(
            _error_payload(
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
    record: JsonObject,
    alias: str,
) -> None:
    """Execute ``git worktree remove`` and raise on failure."""
    remove_args = ["worktree", "remove"]
    if discard_uncommitted:
        remove_args.append("--force")
    remove_args.append(execution_cwd)
    result = _run_git(source_git_root, remove_args)
    if result.returncode != 0:
        if result.returncode == GIT_TIMEOUT_RETURN_CODE:
            raise WorktreeManagementError(
                _error_payload(
                    "git_timeout",
                    f"git worktree remove timed out: {result.stderr.strip()}",
                    record=record,
                    next_actions=[f"delegate worktree show {alias}"],
                    retry_safe=True,
                )
            )
        raise WorktreeManagementError(
            _error_payload(
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
    with run_registry.registry_lock(registry_root):
        record = resolve_record(registry_root, handle=handle)
        status, warnings = detect_worktree_status(record)
        alias = str(record.get("alias") or handle)

        # Already-removed: optionally clean up a leftover branch.
        if status == STATUS_REMOVED:
            source_git_root = record.get("sourceGitRoot")
            branch = record.get("branch")
            branch_result = _remove_branch_if_requested(
                source_git_root=source_git_root,
                branch=branch,
                keep_branch=keep_branch,
                force_branch=force_branch,
                status=status,
            )
            payload: JsonObject = {
                "schema": SCHEMA_REMOVE,
                "ok": not bool(branch_result.error),
                "alias": record.get("alias"),
                "runId": record.get("runId"),
                "branch": branch,
                "executionCwd": record.get("executionCwd"),
                "sourceGitRoot": source_git_root,
                "removed": True,
                "pathRemoved": False,
                "worktreeStatus": STATUS_REMOVED,
                "noop": not branch_result.removed,
            }
            _apply_branch_removal_result(payload, branch_result, alias=alias)
            return payload

        # Dirty guard: refuse to remove uncommitted work unless explicitly asked.
        dirty, dirty_paths, _dirty_total, dirty_warnings = dirty_info(record, status)
        all_warnings = [*warnings, *dirty_warnings]
        if status in (STATUS_PRESENT, STATUS_UNKNOWN):
            _raise_if_dirty_without_discard(
                dirty=dirty,
                dirty_paths=dirty_paths,
                dirty_warnings=dirty_warnings,
                discard_uncommitted=discard_uncommitted,
                record=record,
                alias=alias,
            )

        # Metadata validation.
        source_git_root, execution_cwd = _require_removal_metadata(record)
        branch = record.get("branch")

        # Unmerged-branch guard.
        if not _merged_check_already_passed:
            _raise_if_unmerged_without_override(
                record=record,
                status=status,
                branch=branch,
                keep_branch=keep_branch,
                force_branch=force_branch,
                alias=alias,
            )

        discarded_paths = dirty_paths if discard_uncommitted and dirty_paths else None

        # Missing path: update registry and optionally delete the branch.
        if status == STATUS_MISSING:
            branch_result = _remove_branch_if_requested(
                source_git_root=source_git_root,
                branch=branch,
                keep_branch=keep_branch,
                force_branch=force_branch,
                status=status,
            )
            _mark_worktree_removed(
                registry_root=registry_root,
                run_id=str(record["runId"]),
                discarded_paths=discarded_paths,
            )
            payload = {
                "schema": SCHEMA_REMOVE,
                "ok": not bool(branch_result.error),
                "alias": record.get("alias"),
                "runId": record.get("runId"),
                "branch": branch,
                "executionCwd": execution_cwd,
                "sourceGitRoot": source_git_root,
                "removed": True,
                "pathRemoved": False,
                "worktreeStatus": STATUS_REMOVED,
                "noop": False,
            }
            _apply_branch_removal_result(payload, branch_result, alias=alias)
            return payload

        # Remove the worktree directory.
        _remove_worktree_path(
            source_git_root=source_git_root,
            execution_cwd=execution_cwd,
            discard_uncommitted=discard_uncommitted,
            record=record,
            alias=alias,
        )

        # Branch removal.
        branch_result = _remove_branch_if_requested(
            source_git_root=source_git_root,
            branch=branch,
            keep_branch=keep_branch,
            force_branch=force_branch,
            status=status,
        )
        # Override branchKept when keep_branch came from prune on a clean
        # worktree whose branch is not merged into source (spec L673).
        # In that case _remove_branch was not called (force_branch=False),
        # kept_reason is "requested", and the branch is still present.
        if keep_branch and branch_result.kept_reason == "requested" and isinstance(branch, str):
            merged_val, _ = merged_into_source(record, status)
            if merged_val is False:
                branch_result = BranchRemovalResult(removed=False, kept_reason="unmerged")

        # Update registry.
        _mark_worktree_removed(
            registry_root=registry_root,
            run_id=str(record["runId"]),
            discarded_paths=discarded_paths,
        )

        payload = {
            "schema": SCHEMA_REMOVE,
            "ok": True,
            "alias": record.get("alias"),
            "runId": record.get("runId"),
            "branch": branch,
            "executionCwd": execution_cwd,
            "sourceGitRoot": source_git_root,
            "removed": True,
            "pathRemoved": True,
            "worktreeStatus": STATUS_REMOVED,
            "noop": False,
        }
        _apply_branch_removal_result(payload, branch_result, alias=alias)
        if discarded_paths is not None:
            payload["discardedDirtyPaths"] = discarded_paths
        if all_warnings:
            payload["warnings"] = all_warnings
        return payload


def _older_than(record: JsonObject, days: int) -> bool | None:
    timestamp = record.get("lastActivityAt")
    dt = run_registry.parse_utc_timestamp(timestamp if isinstance(timestamp, str) else None)
    if dt is None:
        return None
    return dt < datetime.now(UTC) - timedelta(days=days)


def _entry_ref(record: JsonObject, *, reason: str | None = None) -> JsonObject:
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
    include_detached: bool = False,
    dry_run: bool = False,
    discard_uncommitted: bool = False,
    force_branch: bool = False,
    force: bool = False,
) -> JsonObject:
    if not merged and older_than_days is None:
        raise WorktreeManagementError(
            _error_payload(
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
        status, _warnings = detect_worktree_status(record)
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
        dirty, _dirty_paths, _dirty_total, _dirty_warnings = dirty_info(record, status)
        if dirty is None and not discard_uncommitted:
            skipped.append(_entry_ref(record, reason="dirty_check_failed"))
            continue
        if dirty is True and not discard_uncommitted:
            skipped.append(_entry_ref(record, reason="dirty"))
            continue
        keep_branch_for_prune = False
        merged_check_already_passed = False
        if merged:
            merged_value, _merge_warnings = merged_into_source(
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
                    remove_worktree(
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
            except WorktreeManagementError as exc:
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


def _worktree_list_paths(source_git_root: str) -> set[str] | None:
    paths, _warning = _worktree_list_paths_with_warning(source_git_root)
    return paths


def _reload_gc_candidate(
    registry_root: Path,
    record: JsonObject,
) -> tuple[JsonObject, str, str] | None:
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


def gc_worktrees(registry_root: Path, *, dry_run: bool = False) -> JsonObject:
    records = load_persistent_records(registry_root)
    paths_by_root: dict[str, set[str] | None] = {}
    prune_roots: set[str] = set()
    reconciled: list[JsonObject] = []
    orphans: list[JsonObject] = []
    warnings: list[JsonObject] = []

    def append_missing(record: JsonObject, execution: str) -> None:
        reconciled.append(
            {
                "alias": record.get("alias"),
                "runId": record.get("runId"),
                "executionCwd": execution,
                "worktreeStatus": STATUS_MISSING,
                "reason": "path_missing",
                "action": "would_mark_missing" if dry_run else "marked_missing",
            }
        )

    def append_orphan(
        record: JsonObject,
        execution: str,
        reason: str,
        message: str | None = None,
    ) -> None:
        entry = {
            "alias": record.get("alias"),
            "runId": record.get("runId"),
            "executionCwd": execution,
            "branch": record.get("branch"),
            "reason": reason,
        }
        if message:
            entry["message"] = message
        if reason == "worktree_metadata_missing":
            entry["safeAction"] = "inspect_path_before_manual_cleanup"
        elif reason == "branch_missing":
            entry["safeAction"] = "inspect_branch_metadata_before_manual_cleanup"
        elif reason == "worktree_list_failed":
            entry["safeAction"] = "retry_after_git_worktree_list_succeeds"
        orphans.append(entry)

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
            listed, warning = _worktree_list_paths_with_warning(source)
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
        path_exists = Path(execution).exists()
        if not path_exists:
            if dry_run:
                append_missing(record, execution)
            else:
                with run_registry.registry_lock(registry_root):
                    fresh_candidate = _reload_gc_candidate(registry_root, record)
                    if fresh_candidate is None:
                        continue
                    fresh, fresh_source, fresh_execution = fresh_candidate
                    if Path(fresh_execution).exists():
                        continue
                    run_registry.set_worktree_status_locked(
                        registry_root,
                        str(fresh["runId"]),
                        "missing",
                    )
                    prune_roots.add(fresh_source)
                    append_missing(fresh, fresh_execution)
            continue
        if listed_paths is None:
            if dry_run:
                append_orphan(record, execution, "worktree_list_failed", list_warning)
            else:
                with run_registry.registry_lock(registry_root):
                    fresh_candidate = _reload_gc_candidate(registry_root, record)
                    if fresh_candidate is None:
                        continue
                    fresh, _fresh_source, fresh_execution = fresh_candidate
                    if Path(fresh_execution).exists():
                        run_registry.set_worktree_status_locked(
                            registry_root,
                            str(fresh["runId"]),
                            "unknown",
                        )
                        append_orphan(fresh, fresh_execution, "worktree_list_failed", list_warning)
            continue
        if listed_paths is not None and not _registered_worktree_path_matches(
            listed_paths, execution
        ):
            if dry_run:
                append_orphan(record, execution, "worktree_metadata_missing")
            else:
                with run_registry.registry_lock(registry_root):
                    fresh_candidate = _reload_gc_candidate(registry_root, record)
                    if fresh_candidate is None:
                        continue
                    fresh, fresh_source, fresh_execution = fresh_candidate
                    fresh_paths, warning = _worktree_list_paths_with_warning(fresh_source)
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
                            "unknown",
                        )
                        append_orphan(fresh, fresh_execution, "worktree_metadata_missing")
            continue
        if isinstance(branch, str) and _branch_exists(source, branch) is False:
            if dry_run:
                append_orphan(record, execution, "branch_missing")
            else:
                with run_registry.registry_lock(registry_root):
                    fresh_candidate = _reload_gc_candidate(registry_root, record)
                    if fresh_candidate is None:
                        continue
                    fresh, fresh_source, fresh_execution = fresh_candidate
                    fresh_branch = fresh.get("branch")
                    if (
                        isinstance(fresh_branch, str)
                        and Path(fresh_execution).exists()
                        and _branch_exists(fresh_source, fresh_branch) is False
                    ):
                        run_registry.set_worktree_status_locked(
                            registry_root,
                            str(fresh["runId"]),
                            "unknown",
                        )
                        append_orphan(fresh, fresh_execution, "branch_missing")
    if not dry_run:
        for source in prune_roots:
            _run_git(source, ["worktree", "prune"])
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
        return prune_worktrees(
            registry_root,
            merged=True,
            older_than_days=days,
            dry_run=False,
        )
    except WorktreeManagementError as exc:
        return dict(exc.payload)
