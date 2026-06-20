"""Persistent worktree record model and extraction layer.

Builds ``PersistentWorktreeRecord`` values from the run registry's
state/manifest/snapshot/index triplets. This module is a leaf: it depends only
on ``run_registry`` and ``json_types`` so the status, remove, and gc pipelines
(and the ``worktree_mgmt`` facade) can import the record model and shared
constants without an import cycle.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject, first_string

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
STATUS_UNKNOWN = run_registry.STATUS_UNKNOWN
VALID_STATUSES = {STATUS_PRESENT, STATUS_REMOVED, STATUS_MISSING, STATUS_UNKNOWN}


def _registered_worktree_path_matches(listed_paths: set[str], execution_cwd: str) -> bool:
    if execution_cwd in listed_paths:
        return True
    try:
        return str(Path(execution_cwd).resolve()) in listed_paths
    except OSError:
        return False


class PersistentWorktreeRecord(TypedDict, total=False):
    alias: str | None
    runId: str
    harness: str | None
    branch: str | None
    executionCwd: str | None
    sourceGitRoot: str | None
    createdAt: str
    lastActivityAt: str
    creationContext: JsonObject
    registryWorktreeStatus: str | None


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime(run_registry.UTC_TIMESTAMP_FORMAT)


def _shell(args: list[str]) -> str:
    return shlex.join(args)


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
    return first_string(
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
    return first_string(
        _get_str(manifest, "executionCwd"),
        _get_str(snapshot, "executionCwd"),
        _get_str(state, "plannedExecutionCwd"),
    )


def _source_git_root_from(manifest: JsonObject | None, snapshot: JsonObject | None) -> str | None:
    return first_string(
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
) -> PersistentWorktreeRecord | None:
    state = run_registry.load_run_state_or_none(registry_root, run_id)
    manifest = run_registry.load_run_manifest_or_none(registry_root, run_id)
    snapshot = run_registry.load_run_snapshot_or_none(registry_root, run_id)
    if not _is_persistent_worktree_run(state, manifest, snapshot):
        return None
    entry = index_entry if isinstance(index_entry, dict) else {}
    alias = first_string(
        _get_str(state, "alias"),
        _get_str(manifest, "alias"),
        _get_str(snapshot, "alias"),
        _get_str(entry, "alias"),
    )
    harness = first_string(
        _get_str(entry, "harness"),
        _get_str(manifest, "harness"),
        _get_str(snapshot, "harness"),
    )
    creation = _creation_context(manifest, snapshot)
    created_at = first_string(
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


def load_persistent_records(registry_root: Path) -> list[PersistentWorktreeRecord]:
    index = run_registry.load_index(registry_root)
    records: list[PersistentWorktreeRecord] = []
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
) -> PersistentWorktreeRecord | None:
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


def _reload_record(registry_root: Path, run_id: str) -> PersistentWorktreeRecord | None:
    index = run_registry.load_index(registry_root)
    index_entry = index.get("runs", {}).get(run_id)
    return _record_for_run(
        registry_root, run_id, index_entry if isinstance(index_entry, dict) else {}
    )
