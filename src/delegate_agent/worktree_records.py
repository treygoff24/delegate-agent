"""Persistent worktree record model and extraction layer.

Builds ``PersistentWorktreeRecord`` values from the run registry's
state/manifest/snapshot/index triplets. This module is a leaf: it depends only
on ``run_registry`` and ``json_types`` so the status, remove, and gc pipelines
(and the ``worktree_mgmt`` facade) can import the record model and shared
constants without an import cycle.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject, first_string

SCHEMA_LIST = "delegate.worktree-list.v1"
SCHEMA_SHOW = "delegate.worktree-show.v1"
SCHEMA_REMOVE = "delegate.worktree-remove.v1"
SCHEMA_PRUNE = "delegate.worktree-prune.v1"
SCHEMA_GC = "delegate.worktree-gc.v1"
SCHEMA_REAP = "delegate.worktree-reap.v1"
WORKTREE_ERROR_EXIT_CODE = 2
MAX_DIRTY_PATHS_REPORTED = 20
SYNCED_FILE_DIGESTS_KEY = "syncedFileDigests"

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
    group: str | None
    branch: str | None
    executionCwd: str | None
    sourceGitRoot: str | None
    createdAt: str
    lastActivityAt: str
    creationContext: JsonObject
    registryWorktreeStatus: str | None
    recordWarnings: list[str]


@dataclass(frozen=True)
class WorktreeRecordBundle:
    """One read of each record, with identity conflicts retained as evidence.

    Missing older projections are valid fallback inputs; malformed existing
    records are not evidence permitting removal. No bundle survives a command
    or replaces the fresh reads under the mutation lock.
    """

    index: JsonObject | None
    state: JsonObject | None
    manifest: JsonObject | None
    snapshot: JsonObject | None
    warnings: tuple[str, ...] = ()

    @classmethod
    def load(
        cls, registry_root: Path, run_id: str, index: JsonObject | None
    ) -> WorktreeRecordBundle:
        values: list[JsonObject | None] = []
        warnings: list[str] = []
        readers = (
            (run_registry.STATE_FILE, run_registry.load_run_state),
            (run_registry.MANIFEST_FILE, run_registry.load_run_manifest),
            # A display view normalizes identity and can hide contradictory
            # legacy evidence. Destructive checks must inspect the raw record.
            (
                run_registry.SNAPSHOT_FILE,
                lambda root, key: run_registry.read_json_object(
                    run_registry.run_directory(root, key) / run_registry.SNAPSHOT_FILE
                ),
            ),
        )
        for filename, reader in readers:
            try:
                value = reader(registry_root, run_id)
            except (OSError, ValueError):
                value = None
                warnings.append(f"unreadable {filename}")
            if value is not None and value.get("runId", run_id) != run_id:
                warnings.append(f"conflicting runId in {filename}")
            values.append(value)
        return cls(index, *values, tuple(warnings))

    def record(self, registry_root: Path, run_id: str) -> PersistentWorktreeRecord | None:
        record = _record_from_parts(
            registry_root, run_id, self.index, self.state, self.manifest, self.snapshot
        )
        if record is None:
            return None
        warnings = list(self.warnings)
        for field in ("executionCwd", "sourceGitRoot", "branch"):
            values = {
                _get_str(source, field)
                for source in (self.index, self.state, self.manifest, self.snapshot)
            } - {None}
            if field == "executionCwd":
                planned = _get_str(self.state, "plannedExecutionCwd")
                if planned:
                    values.add(planned)
            if field != "branch":
                values = {_canonical_path(value) for value in values}
            if len(values) > 1:
                warnings.append(f"conflicting {field}")
        if warnings:
            record["recordWarnings"] = warnings
            # Existing removal/GC gates require these fields. Do not choose a
            # destructive target from conflicting or unreadable ownership data,
            # even when a caller requested force.
            record["sourceGitRoot"] = None
            record["registryWorktreeStatus"] = STATUS_UNKNOWN
        return record


_utc_now_iso = run_registry.utc_now_iso


def _shell(args: list[str]) -> str:
    return shlex.join(args)


def file_content_digest(root: str | Path, relative_path: str) -> str | None:
    """Return a stable digest for one seeded path, or ``None`` if unreadable.

    The digest includes the file kind so a seeded symlink replaced by a regular
    file (or vice versa) is real child dirt even when the bytes happen to match.
    ``git status`` reports paths, but retirement needs this content-level check.
    """

    path = Path(root) / relative_path
    try:
        stat_result = path.lstat()
        if stat.S_ISLNK(stat_result.st_mode):
            data = b"symlink\0" + os.fsencode(os.readlink(path))
        elif stat.S_ISREG(stat_result.st_mode):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return f"file:{digest.hexdigest()}"
        else:
            return None
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return None
    return f"symlink:{hashlib.sha256(data).hexdigest()}"


def capture_file_content_digests(root: str | Path, paths: tuple[str, ...]) -> dict[str, str]:
    """Capture content digests for paths copied into a persistent worktree."""

    digests: dict[str, str] = {}
    for relative_path in dict.fromkeys(paths):
        digest = file_content_digest(root, relative_path)
        if digest is not None:
            digests[relative_path] = digest
    return digests


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
    # Attached resume runs execute inside another run's worktree without owning
    # it; they must never derive a second worktree record for the same path.
    for source in (state, manifest, snapshot):
        if not isinstance(source, dict):
            continue
        if isinstance(source.get("worktreeAttachment"), dict):
            return False
        if source.get("isolationLifecycle") == "attached":
            return False
    for source in (state, manifest, snapshot):
        if not isinstance(source, dict):
            continue
        if source.get("isolationLifecycle") == "persistent":
            return True
        if source.get("preservedWorkspace") is True:
            return True
    return _registry_worktree_status(state, manifest, snapshot) is not None


def _record_from_parts(
    registry_root: Path,
    run_id: str,
    index_entry: JsonObject | None,
    state: JsonObject | None,
    manifest: JsonObject | None,
    snapshot: JsonObject | None,
) -> PersistentWorktreeRecord | None:
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
        "group": first_string(
            _get_str(entry, "group"),
            _get_str(state, "group"),
            _get_str(manifest, "group"),
            _get_str(snapshot, "group"),
        ),
        "branch": _branch_from(manifest, snapshot),
        "executionCwd": _execution_cwd_from(manifest, snapshot, state),
        "sourceGitRoot": _source_git_root_from(manifest, snapshot),
        "createdAt": created_at,
        "lastActivityAt": last_activity,
        "creationContext": creation,
        "registryWorktreeStatus": _registry_worktree_status(state, manifest, snapshot),
    }


def _record_for_run(
    registry_root: Path,
    run_id: str,
    index_entry: JsonObject | None,
) -> PersistentWorktreeRecord | None:
    return WorktreeRecordBundle.load(registry_root, run_id, index_entry).record(
        registry_root, run_id
    )


def _canonical_path(path: str) -> str:
    try:
        return str(Path(path).resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return path


def live_attachments_for_path(registry_root: Path, execution_cwd: str) -> list[JsonObject]:
    """Return {runId, alias} for effectively-running resume runs attached to a path.

    The attachment lease is derived from run records (the manifest's
    ``worktreeAttachment`` plus live status) rather than a separate state store,
    matching how worktree records themselves are derived. Removal paths refuse
    while this list is non-empty.
    """
    canonical = _canonical_path(execution_cwd)
    index = run_registry.load_index(registry_root)
    live: list[JsonObject] = []
    for run_id, entry in run_registry.index_run_entries(index):
        manifest = run_registry.load_run_manifest_or_none(registry_root, run_id)
        manifest_attachment = (
            manifest.get("worktreeAttachment") if isinstance(manifest, dict) else None
        )
        index_attachment = entry.get("worktreeAttachment")
        claims = [
            attachment
            for attachment in (manifest_attachment, index_attachment)
            if isinstance(attachment, dict)
        ]
        if not claims:
            continue
        matching_claims = [
            attachment
            for attachment in claims
            if isinstance(attachment.get("path"), str)
            and _canonical_path(str(attachment["path"])) == canonical
        ]
        if not matching_claims:
            continue
        state = run_registry.load_run_state_or_none(registry_root, run_id)
        if run_registry.effective_status(state) in {
            run_registry.STATUS_RUNNING,
            run_registry.STATUS_UNKNOWN,
        }:
            item: JsonObject = {"runId": run_id, "alias": _get_str(entry, "alias")}
            if len(claims) == 2 and _canonical_path(
                str(claims[0].get("path", ""))
            ) != _canonical_path(str(claims[1].get("path", ""))):
                item["warning"] = "corrupt_attachment"
            live.append(item)
    return live


def load_persistent_records(registry_root: Path) -> list[PersistentWorktreeRecord]:
    index = run_registry.load_index(registry_root)
    records: list[PersistentWorktreeRecord] = []
    for run_id, entry in run_registry.index_run_entries(index):
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
