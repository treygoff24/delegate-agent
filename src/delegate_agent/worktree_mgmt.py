from __future__ import annotations

import shlex
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject

SCHEMA_LIST = "delegate.worktree-list.v1"
SCHEMA_SHOW = "delegate.worktree-show.v1"
SCHEMA_REMOVE = "delegate.worktree-remove.v1"
SCHEMA_PRUNE = "delegate.worktree-prune.v1"
SCHEMA_GC = "delegate.worktree-gc.v1"

STATUS_PRESENT = "present"
STATUS_REMOVED = "removed"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"
VALID_STATUSES = {STATUS_PRESENT, STATUS_REMOVED, STATUS_MISSING, STATUS_UNKNOWN}


class WorktreeManagementError(Exception):
    def __init__(self, payload: JsonObject) -> None:
        super().__init__(str(payload.get("message", payload.get("code", "worktree_error"))))
        self.payload = payload
        self.code = str(payload.get("code", "worktree_error"))
        self.message = str(payload.get("message", self.code))


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime(run_registry.UTC_TIMESTAMP_FORMAT)


def _shell(args: list[str]) -> str:
    return shlex.join(args)


def _run_git(cwd: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _creation_context(manifest: JsonObject | None, snapshot: JsonObject | None) -> JsonObject:
    for source in (manifest, snapshot):
        if not isinstance(source, dict):
            continue
        value = source.get("creationContext")
        if isinstance(value, dict):
            return value
    return {}


def _branch_from(manifest: JsonObject | None, snapshot: JsonObject | None) -> str | None:
    creation = _creation_context(manifest, snapshot)
    return _first_string(
        manifest.get("branch") if isinstance(manifest, dict) else None,
        snapshot.get("branch") if isinstance(snapshot, dict) else None,
        creation.get("branch"),
        creation.get("plannedBranch"),
    )


def _execution_cwd_from(
    manifest: JsonObject | None,
    snapshot: JsonObject | None,
    state: JsonObject | None,
) -> str | None:
    return _first_string(
        manifest.get("executionCwd") if isinstance(manifest, dict) else None,
        snapshot.get("executionCwd") if isinstance(snapshot, dict) else None,
        state.get("plannedExecutionCwd") if isinstance(state, dict) else None,
    )


def _source_git_root_from(manifest: JsonObject | None, snapshot: JsonObject | None) -> str | None:
    return _first_string(
        manifest.get("sourceGitRoot") if isinstance(manifest, dict) else None,
        snapshot.get("sourceGitRoot") if isinstance(snapshot, dict) else None,
        manifest.get("cwd") if isinstance(manifest, dict) else None,
        snapshot.get("cwd") if isinstance(snapshot, dict) else None,
    )


def _registry_worktree_status(
    state: JsonObject | None,
    manifest: JsonObject | None,
    snapshot: JsonObject | None,
) -> str | None:
    for source in (state, manifest, snapshot):
        if not isinstance(source, dict):
            continue
        value = source.get("worktreeStatus")
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
        state.get("alias") if isinstance(state, dict) else None,
        manifest.get("alias") if isinstance(manifest, dict) else None,
        snapshot.get("alias") if isinstance(snapshot, dict) else None,
        entry.get("alias"),
    )
    harness = _first_string(
        entry.get("harness"),
        manifest.get("harness") if isinstance(manifest, dict) else None,
        snapshot.get("harness") if isinstance(snapshot, dict) else None,
    )
    creation = _creation_context(manifest, snapshot)
    created_at = _first_string(
        manifest.get("startedAt") if isinstance(manifest, dict) else None,
        snapshot.get("startedAt") if isinstance(snapshot, dict) else None,
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
        "_state": state or {},
        "_manifest": manifest or {},
        "_snapshot": snapshot or {},
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


def _reload_record(registry_root: Path, run_id: str) -> JsonObject | None:
    index = run_registry.load_index(registry_root)
    index_entry = index.get("runs", {}).get(run_id)
    return _record_for_run(registry_root, run_id, index_entry if isinstance(index_entry, dict) else {})


def _branch_exists(source_git_root: str, branch: str) -> bool | None:
    result = _run_git(source_git_root, ["rev-parse", "--verify", "--quiet", branch])
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
    )
    if result.returncode != 0:
        return None, None, [f"git status failed: {result.stderr.strip()}"]
    lines = result.stdout.splitlines()
    if limit is None:
        return lines, len(lines), []
    return lines[:limit], len(lines), []


def dirty_info(record: JsonObject, status: str) -> tuple[bool | None, list[str], int | None, list[str]]:
    if status != STATUS_PRESENT:
        return None, [], None, []
    execution_cwd = record.get("executionCwd")
    if not isinstance(execution_cwd, str) or not execution_cwd:
        return None, [], None, ["missing executionCwd metadata"]
    lines, total, warnings = porcelain_status(execution_cwd)
    if lines is None:
        return None, [], total, warnings
    return bool(lines), lines, total, warnings


def _merge_base_is_ancestor(source_git_root: str, branch: str) -> bool | None:
    result = _run_git(source_git_root, ["merge-base", "--is-ancestor", branch, "HEAD"])
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
    if status != STATUS_PRESENT:
        return None, []
    creation = record.get("creationContext")
    if isinstance(creation, dict) and creation.get("sourceHeadRef") is None and not include_detached:
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
    result = _run_git(source_git_root, ["rev-parse", "--verify", rev])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _symbolic_ref(source_git_root: str, rev: str) -> str | None:
    """Return the symbolic ref (e.g. `refs/heads/main`) or None if detached/missing."""
    result = _run_git(source_git_root, ["symbolic-ref", "--quiet", rev])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _ahead_behind(source_git_root: str, branch: str, base: str) -> JsonObject | None:
    result = _run_git(source_git_root, ["rev-list", "--left-right", "--count", f"{base}...{branch}"])
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
    if status != STATUS_PRESENT:
        return None
    source_git_root = record.get("sourceGitRoot")
    branch = record.get("branch")
    creation = record.get("creationContext")
    if not isinstance(source_git_root, str) or not isinstance(branch, str) or not isinstance(creation, dict):
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
    if isinstance(execution_cwd, str) and status == STATUS_PRESENT:
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
        "worktreeStatus": status,
        "dirty": dirty,
        "mergedIntoSource": merged,
    }
    all_warnings = [*warnings, *dirty_warnings, *merged_warnings]
    if all_warnings:
        output["warnings"] = all_warnings
    if dirty_paths:
        output["dirtyPaths"] = dirty_paths[:20]
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
    for record in load_persistent_records(registry_root):
        if harness is not None and record.get("harness") != harness:
            continue
        entry = decorate_record(record, include_detached=include_detached)
        if status is not None and entry.get("worktreeStatus") != status:
            continue
        entries.append(entry)
    entries.sort(key=lambda item: str(item.get("lastActivityAt", "")), reverse=True)
    return {
        "schema": SCHEMA_LIST,
        "ok": True,
        "entries": entries[:limit],
        "limit": limit,
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
        "message": message,
        "retrySafe": retry_safe,
    }
    if record is not None:
        for key in ("alias", "runId", "branch", "executionCwd", "sourceGitRoot"):
            value = record.get(key)
            if isinstance(value, str) and value:
                payload[key] = value
    if dirty_paths is not None:
        capped = dirty_paths[:20]
        if len(dirty_paths) > 20:
            capped.append("...")
            payload["dirtyPathsTotal"] = len(dirty_paths)
        payload["dirtyPaths"] = capped
    if next_actions:
        payload["nextActions"] = next_actions
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def resolve_record(registry_root: Path, *, handle: str | None, latest_harness: str | None = None) -> JsonObject:
    index = run_registry.load_index(registry_root)
    if latest_harness is not None:
        run_id = run_registry.latest_run_id_for_harness(registry_root, index, latest_harness)
        if run_id is None:
            raise WorktreeManagementError(
                _error_payload(
                    "unknown_handle",
                    f"No runs found for harness: {latest_harness}",
                    next_actions=["delegate worktree list"],
                )
            )
    else:
        assert handle is not None
        resolved = run_registry.resolve_handle(index, handle)
        if resolved.run_id is None:
            suggestions = list(resolved.suggestions)
            next_actions = [f"delegate worktree show {suggestions[0]}"] if suggestions else ["delegate worktree list"]
            raise WorktreeManagementError(
                _error_payload(
                    "unknown_handle",
                    f"Unknown run handle: {handle}. Suggestions: {', '.join(suggestions) if suggestions else '(none)'}",
                    next_actions=next_actions,
                )
            )
        run_id = resolved.run_id
    index_entry = index.get("runs", {}).get(run_id)
    record = _record_for_run(registry_root, run_id, index_entry if isinstance(index_entry, dict) else {})
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
    if entry.get("worktreeStatus") == STATUS_PRESENT and isinstance(execution_cwd, str):
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


def _write_worktree_state(
    registry_root: Path,
    run_id: str,
    *,
    status: str,
    removed_at: str | None = None,
    discarded_dirty_paths: list[str] | None = None,
) -> JsonObject:
    state_path = run_registry.run_directory(registry_root, run_id) / run_registry.STATE_FILE
    if not state_path.exists():
        raise FileNotFoundError(f"State file not found for run {run_id}")
    state = run_registry.read_json_object(state_path) or {}
    state["worktreeStatus"] = status
    if removed_at is not None:
        state["worktreeRemovedAt"] = removed_at
    if discarded_dirty_paths is not None:
        state["discardedDirtyPaths"] = discarded_dirty_paths
    run_registry.write_json_atomic(state_path, state)
    return state


def _remove_branch(source_git_root: str, branch: str, *, force: bool) -> tuple[bool, str | None]:
    flag = "-D" if force else "-d"
    result = _run_git(source_git_root, ["branch", flag, branch])
    if result.returncode == 0:
        return True, None
    if not force:
        return False, "unmerged"
    return False, result.stderr.strip() or "delete_failed"


def remove_worktree(
    registry_root: Path,
    *,
    handle: str,
    discard_uncommitted: bool = False,
    force_branch: bool = False,
    keep_branch: bool = False,
    force: bool = False,
) -> JsonObject:
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
    with run_registry.registry_lock(registry_root):
        record = resolve_record(registry_root, handle=handle)
        status, warnings = detect_worktree_status(record)
        if status == STATUS_REMOVED:
            branch_removed = False
            branch_kept: str | None = None
            source_git_root = record.get("sourceGitRoot")
            branch = record.get("branch")
            if (
                force_branch
                and not keep_branch
                and isinstance(source_git_root, str)
                and isinstance(branch, str)
                and branch
            ):
                branch_removed, branch_kept = _remove_branch(
                    source_git_root,
                    branch,
                    force=True,
                )
            return {
                "schema": SCHEMA_REMOVE,
                "ok": True,
                "alias": record.get("alias"),
                "runId": record.get("runId"),
                "branch": branch,
                "executionCwd": record.get("executionCwd"),
                "sourceGitRoot": source_git_root,
                "removed": True,
                "pathRemoved": False,
                "branchRemoved": branch_removed,
                "branchKept": branch_kept,
                "worktreeStatus": STATUS_REMOVED,
                "noop": not branch_removed,
            }

        dirty, dirty_paths, _dirty_total, dirty_warnings = dirty_info(record, status)
        all_warnings = [*warnings, *dirty_warnings]
        alias = str(record.get("alias") or handle)
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

        source_git_root = record.get("sourceGitRoot")
        execution_cwd = record.get("executionCwd")
        branch = record.get("branch")
        if not isinstance(source_git_root, str) or not isinstance(execution_cwd, str):
            raise WorktreeManagementError(
                _error_payload(
                    "worktree_remove_failed",
                    "Run is missing sourceGitRoot or executionCwd metadata.",
                    record=record,
                )
            )

        if (
            status == STATUS_PRESENT
            and isinstance(branch, str)
            and branch
            and not keep_branch
            and not force_branch
        ):
            merged, merge_warnings = merged_into_source(record, status)
            if merged is not True:
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

        path_removed = False
        branch_removed = False
        branch_kept: str | None = None
        discarded_paths = dirty_paths if discard_uncommitted and dirty_paths else None

        if status == STATUS_MISSING:
            _write_worktree_state(
                registry_root,
                str(record["runId"]),
                status=STATUS_REMOVED,
                removed_at=_utc_now_iso(),
                discarded_dirty_paths=discarded_paths,
            )
            return {
                "schema": SCHEMA_REMOVE,
                "ok": True,
                "alias": record.get("alias"),
                "runId": record.get("runId"),
                "branch": branch,
                "executionCwd": execution_cwd,
                "sourceGitRoot": source_git_root,
                "removed": True,
                "pathRemoved": False,
                "branchRemoved": False,
                "branchKept": "path_missing",
                "worktreeStatus": STATUS_REMOVED,
                "noop": False,
            }

        remove_args = ["worktree", "remove"]
        if discard_uncommitted:
            remove_args.append("--force")
        remove_args.append(execution_cwd)
        result = _run_git(source_git_root, remove_args)
        if result.returncode != 0:
            raise WorktreeManagementError(
                _error_payload(
                    "worktree_remove_failed",
                    f"git worktree remove failed: {result.stderr.strip()}",
                    record=record,
                    next_actions=[f"delegate worktree show {alias}"],
                    retry_safe=True,
                )
            )
        path_removed = True

        if keep_branch:
            branch_kept = "requested"
        elif isinstance(branch, str) and branch:
            branch_removed, branch_kept = _remove_branch(
                source_git_root,
                branch,
                force=force_branch,
            )

        _write_worktree_state(
            registry_root,
            str(record["runId"]),
            status=STATUS_REMOVED,
            removed_at=_utc_now_iso(),
            discarded_dirty_paths=discarded_paths,
        )
        payload: JsonObject = {
            "schema": SCHEMA_REMOVE,
            "ok": True,
            "alias": record.get("alias"),
            "runId": record.get("runId"),
            "branch": branch,
            "executionCwd": execution_cwd,
            "sourceGitRoot": source_git_root,
            "removed": True,
            "pathRemoved": path_removed,
            "branchRemoved": branch_removed,
            "worktreeStatus": STATUS_REMOVED,
            "noop": False,
        }
        if branch_kept:
            payload["branchKept"] = branch_kept
            if branch_kept == "unmerged":
                payload["nextActions"] = [f"delegate worktree remove {alias} --force-branch"]
        if discarded_paths is not None:
            payload["discardedDirtyPaths"] = discarded_paths
        if all_warnings:
            payload["warnings"] = all_warnings
        return payload


def _older_than(record: JsonObject, days: int) -> bool:
    timestamp = record.get("lastActivityAt")
    dt = run_registry.parse_utc_timestamp(timestamp if isinstance(timestamp, str) else None)
    if dt is None:
        return False
    return dt < datetime.now(UTC) - timedelta(days=days)


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
            skipped.append({"alias": alias, "runId": record.get("runId"), "reason": "harness_filter"})
            continue
        status, _warnings = detect_worktree_status(record)
        if status != STATUS_PRESENT:
            skipped.append({"alias": alias, "runId": record.get("runId"), "reason": "path_missing" if status == STATUS_MISSING else status})
            continue
        creation = record.get("creationContext")
        if merged and isinstance(creation, dict) and creation.get("sourceHeadRef") is None and not include_detached:
            skipped.append({"alias": alias, "runId": record.get("runId"), "reason": "detached_source"})
            continue
        if merged:
            merged_value, _merge_warnings = merged_into_source(
                record,
                status,
                include_detached=include_detached,
            )
            if merged_value is not True:
                skipped.append({"alias": alias, "runId": record.get("runId"), "reason": "not_merged"})
                continue
        if older_than_days is not None and not _older_than(record, older_than_days):
            skipped.append({"alias": alias, "runId": record.get("runId"), "reason": "not_yet_old_enough"})
            continue
        dirty, _dirty_paths, _dirty_total, _dirty_warnings = dirty_info(record, status)
        if dirty is True and not discard_uncommitted:
            skipped.append({"alias": alias, "runId": record.get("runId"), "reason": "dirty"})
            continue
        candidate = {
            "alias": alias,
            "runId": record.get("runId"),
            "branch": record.get("branch"),
            "executionCwd": record.get("executionCwd"),
            "sourceGitRoot": record.get("sourceGitRoot"),
        }
        planned.append(candidate)
        if not dry_run:
            try:
                removed.append(
                    remove_worktree(
                        registry_root,
                        handle=str(alias or record.get("runId")),
                        discard_uncommitted=discard_uncommitted,
                        force_branch=force_branch,
                    )
                )
            except WorktreeManagementError as exc:
                errors.append(exc.payload)
    return {
        "schema": SCHEMA_PRUNE,
        "ok": not errors,
        "planned": planned,
        "removed": removed,
        "skipped": skipped,
        "errors": errors,
        "dryRun": dry_run,
    }


def _worktree_list_paths(source_git_root: str) -> set[str] | None:
    result = _run_git(source_git_root, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return None
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(line[len("worktree "):])
    return paths


def gc_worktrees(registry_root: Path, *, dry_run: bool = False) -> JsonObject:
    records = load_persistent_records(registry_root)
    paths_by_root: dict[str, set[str] | None] = {}
    prune_roots: set[str] = set()
    reconciled: list[JsonObject] = []
    orphans: list[JsonObject] = []

    def append_missing(record: JsonObject, execution: str) -> None:
        reconciled.append({
            "alias": record.get("alias"),
            "runId": record.get("runId"),
            "executionCwd": execution,
            "worktreeStatus": STATUS_MISSING,
        })

    def append_orphan(record: JsonObject, execution: str, reason: str) -> None:
        orphans.append({
            "alias": record.get("alias"),
            "runId": record.get("runId"),
            "executionCwd": execution,
            "branch": record.get("branch"),
            "reason": reason,
        })

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
            paths_by_root[source] = _worktree_list_paths(source)
        listed_paths = paths_by_root[source]
        path_exists = Path(execution).exists()
        if not path_exists:
            if dry_run:
                append_missing(record, execution)
            else:
                with run_registry.registry_lock(registry_root):
                    fresh = _reload_record(registry_root, str(record["runId"]))
                    if fresh is None:
                        continue
                    fresh_current = fresh.get("registryWorktreeStatus")
                    if fresh_current not in (STATUS_PRESENT, STATUS_UNKNOWN, None):
                        continue
                    fresh_source = fresh.get("sourceGitRoot")
                    fresh_execution = fresh.get("executionCwd")
                    if not isinstance(fresh_source, str) or not isinstance(fresh_execution, str):
                        continue
                    if Path(fresh_execution).exists():
                        continue
                    _write_worktree_state(
                        registry_root,
                        str(fresh["runId"]),
                        status=STATUS_MISSING,
                        removed_at=_utc_now_iso(),
                    )
                    prune_roots.add(fresh_source)
                    append_missing(fresh, fresh_execution)
            continue
        if listed_paths is not None and execution not in listed_paths:
            if dry_run:
                append_orphan(record, execution, "worktree_metadata_missing")
            else:
                with run_registry.registry_lock(registry_root):
                    fresh = _reload_record(registry_root, str(record["runId"]))
                    if fresh is None:
                        continue
                    fresh_current = fresh.get("registryWorktreeStatus")
                    if fresh_current not in (STATUS_PRESENT, STATUS_UNKNOWN, None):
                        continue
                    fresh_source = fresh.get("sourceGitRoot")
                    fresh_execution = fresh.get("executionCwd")
                    if not isinstance(fresh_source, str) or not isinstance(fresh_execution, str):
                        continue
                    fresh_paths = _worktree_list_paths(fresh_source)
                    if (
                        Path(fresh_execution).exists()
                        and fresh_paths is not None
                        and fresh_execution not in fresh_paths
                    ):
                        _write_worktree_state(
                            registry_root,
                            str(fresh["runId"]),
                            status=STATUS_UNKNOWN,
                        )
                        append_orphan(fresh, fresh_execution, "worktree_metadata_missing")
            continue
        if isinstance(branch, str) and _branch_exists(source, branch) is False:
            if dry_run:
                append_orphan(record, execution, "branch_missing")
            else:
                with run_registry.registry_lock(registry_root):
                    fresh = _reload_record(registry_root, str(record["runId"]))
                    if fresh is None:
                        continue
                    fresh_current = fresh.get("registryWorktreeStatus")
                    if fresh_current not in (STATUS_PRESENT, STATUS_UNKNOWN, None):
                        continue
                    fresh_source = fresh.get("sourceGitRoot")
                    fresh_execution = fresh.get("executionCwd")
                    fresh_branch = fresh.get("branch")
                    if (
                        isinstance(fresh_source, str)
                        and isinstance(fresh_execution, str)
                        and isinstance(fresh_branch, str)
                        and Path(fresh_execution).exists()
                        and _branch_exists(fresh_source, fresh_branch) is False
                    ):
                        _write_worktree_state(
                            registry_root,
                            str(fresh["runId"]),
                            status=STATUS_UNKNOWN,
                        )
                        append_orphan(fresh, fresh_execution, "branch_missing")
    if not dry_run:
        for source in prune_roots:
            _run_git(source, ["worktree", "prune"])
    return {
        "schema": SCHEMA_GC,
        "ok": True,
        "dryRun": dry_run,
        "prunedSourceRoots": 0 if dry_run else len(prune_roots),
        "reconciled": len(reconciled),
        "reconciledEntries": reconciled,
        "orphans": orphans,
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
    if not isinstance(days, int):
        days = 7
    try:
        with run_registry.registry_lock(registry_root, timeout_seconds=0.0):
            pass
        return prune_worktrees(
            registry_root,
            merged=True,
            older_than_days=days,
            dry_run=False,
        )
    except (TimeoutError, WorktreeManagementError):
        return {"ok": False, "skipped": True, "reason": "auto_prune_unavailable"}
