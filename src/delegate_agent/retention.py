from __future__ import annotations

import os
import tarfile
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from delegate_agent import archived_logs, log_output, run_registry, run_scratch
from delegate_agent.constants import DEFAULT_SCRATCH_RECLAIM_DAYS
from delegate_agent.json_types import JsonObject, is_non_negative_int
from delegate_agent.log_output import LogOutput

ARCHIVE_MEMBER_NAMES = (
    run_registry.STDOUT_LOG,
    run_registry.STDERR_LOG,
    run_registry.EVENTS_JSONL,
)
DEFAULT_RAW_LOG_RETENTION_DAYS = 7
DEFAULT_SCRATCH_RETENTION_DAYS = DEFAULT_SCRATCH_RECLAIM_DAYS
RETENTION_CADENCE_SECONDS = 60
SCRATCH_RECLAIMED_AT_KEY = "scratchReclaimedAt"
SCRATCH_RECLAIMED_BYTES_KEY = "scratchReclaimedBytes"
RUNS_RECLAIM_SCHEMA = "delegate.runs-reclaim.v1"
# The implicit pass runs inside launches and read commands. Deleting a run's
# scratch can take minutes when a test filled it, so the implicit pass stops
# starting new reclaims once it has spent this long and the next pass carries on.
# `delegate runs reclaim` has no such budget.
SCRATCH_RECLAIM_BUDGET_SECONDS = 20.0


def _retention_completed_at(index: JsonObject) -> datetime | None:
    retention = index.get("retention")
    value = retention.get("completedAt") if isinstance(retention, dict) else None
    return run_registry.parse_utc_timestamp(value if isinstance(value, str) else None)


def _mark_retention_completed(
    registry_root: Path,
    completed_at: datetime,
    run_count: int,
) -> None:
    with run_registry.registry_lock(registry_root):
        index = run_registry.load_index(registry_root)
        retention = index.get("retention")
        if not isinstance(retention, dict):
            retention = {}
            index["retention"] = retention
        retention["completedAt"] = completed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        retention["completedRunCount"] = run_count
        run_registry.save_index(registry_root, index)


def archive_dir(registry_root: Path) -> Path:
    return archived_logs.archive_dir(registry_root)


def archive_path(registry_root: Path, run_id: str) -> Path:
    return archived_logs.archive_path(registry_root, run_id)


def retention_settings(config: JsonObject) -> JsonObject:
    tracking = config.get("tracking")
    if not isinstance(tracking, dict):
        return {}
    retention = tracking.get("retention")
    return retention if isinstance(retention, dict) else {}


def retention_enabled(config: JsonObject) -> bool:
    enabled = retention_settings(config).get("enabled", True)
    return enabled if isinstance(enabled, bool) else True


def raw_log_retention_days(config: JsonObject) -> int:
    settings = retention_settings(config)
    days = settings.get("rawLogDays", DEFAULT_RAW_LOG_RETENTION_DAYS)
    if not is_non_negative_int(days):
        return DEFAULT_RAW_LOG_RETENTION_DAYS
    return days


def scratch_retention_days(config: JsonObject) -> int:
    settings = retention_settings(config)
    days = settings.get("scratchDays", DEFAULT_SCRATCH_RETENTION_DAYS)
    if not is_non_negative_int(days):
        return DEFAULT_SCRATCH_RETENTION_DAYS
    return days


def should_skip_archival(registry_root: Path, run_id: str) -> bool:
    state = run_registry.load_run_state_or_none(registry_root, run_id)
    status = run_registry.effective_status(state)
    return status in (
        run_registry.STATUS_RUNNING,
        run_registry.STATUS_STALE,
        run_registry.STATUS_UNKNOWN,
    )


def raw_logs_present(run_path: Path) -> bool:
    return any((run_path / name).exists() for name in ARCHIVE_MEMBER_NAMES)


def raw_logs_archived(registry_root: Path, run_id: str) -> bool:
    return archive_path(registry_root, run_id).exists()


def _validate_archive_member_name(member_name: str) -> None:
    if member_name not in ARCHIVE_MEMBER_NAMES:
        raise ValueError(f"unsupported archive member: {member_name}")


def _expected_member_sizes(run_path: Path, members: list[str]) -> dict[str, int]:
    return {name: (run_path / name).stat().st_size for name in members}


def _member_identities(
    run_path: Path,
    members: list[str],
) -> dict[str, tuple[int, int, int, int, int]]:
    identities = {}
    for name in members:
        info = (run_path / name).stat()
        identities[name] = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
    return identities


def _verify_archive_members(archive_file: Path, expected_sizes: dict[str, int]) -> bool:
    if not expected_sizes:
        return False
    with tarfile.open(archive_file, "r:gz") as archive:
        found: dict[str, int] = {}
        for member in archive.getmembers():
            if member.name in expected_sizes:
                found[member.name] = member.size
        return all(name in found and found[name] == size for name, size in expected_sizes.items())


def _tail_text_stream_output(stream: BinaryIO, lines: int) -> LogOutput:
    if lines < 1:
        raise ValueError("tail lines must be at least 1")
    buffer: deque[str] = deque(maxlen=lines)
    total_lines = 0
    for raw_line in stream:
        total_lines += 1
        buffer.append(raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))
    if not buffer:
        return LogOutput(content="", truncated=False)
    return LogOutput(content="\n".join(buffer) + "\n", truncated=total_lines > lines)


def effective_log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    return run_registry.effective_log_byte_sizes(registry_root, run_id)


def archived_log_warning(alias: str | None, run_id: str, *, cwd: str | None = None) -> str:
    handle = alias or run_id
    return (
        f"raw logs archived for {handle}; use "
        f"{run_registry.run_output_command(handle, cwd=cwd)} --stdout|--stderr --tail N or --raw"
    )


def is_eligible_for_archival(
    registry_root: Path,
    run_id: str,
    *,
    config: JsonObject,
    now: datetime | None = None,
) -> bool:
    if not retention_enabled(config):
        return False
    if should_skip_archival(registry_root, run_id):
        return False
    run_path = run_registry.run_directory(registry_root, run_id)
    archive_exists = archive_path(registry_root, run_id).exists()
    logs_present = raw_logs_present(run_path)
    if archive_exists and not logs_present:
        return False
    if not archive_exists and not logs_present:
        return False
    moment = now or datetime.now(UTC)
    activity = run_registry.activity_datetime(
        run_registry.load_run_state_or_none(registry_root, run_id),
        run_registry.load_run_manifest_or_none(registry_root, run_id),
        run_id,
    )
    if activity is None:
        return False
    cutoff = moment - timedelta(days=raw_log_retention_days(config))
    return activity < cutoff


def _mark_raw_logs_archived(
    run_path: Path,
    *,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
) -> None:
    state = run_registry.read_json_object_or_none(run_path / run_registry.STATE_FILE)
    if state is None:
        state = {}
    state["rawLogsArchivedAt"] = run_registry.utc_now_iso()
    state["stdoutBytes"] = stdout_bytes
    state["stderrBytes"] = stderr_bytes
    run_registry.write_run_state(run_path, state)


def _remove_archived_raw_logs(run_path: Path, members: list[str]) -> None:
    for name in members:
        (run_path / name).unlink()


def _complete_archival_after_archive(
    registry_root: Path,
    run_id: str,
    *,
    destination: Path,
    members: list[str],
    archive_verified: bool = False,
) -> bool:
    run_path = run_registry.run_directory(registry_root, run_id)
    if not members:
        return False
    expected_sizes = _expected_member_sizes(run_path, members)
    if not archive_verified and not _verify_archive_members(destination, expected_sizes):
        return False
    _remove_archived_raw_logs(run_path, members)
    _mark_raw_logs_archived(
        run_path,
        stdout_bytes=expected_sizes.get(run_registry.STDOUT_LOG, 0),
        stderr_bytes=expected_sizes.get(run_registry.STDERR_LOG, 0),
    )
    return True


def archive_run_raw_logs(registry_root: Path, run_id: str) -> bool:
    run_path = run_registry.run_directory(registry_root, run_id)
    members = [name for name in ARCHIVE_MEMBER_NAMES if (run_path / name).exists()]
    if not members:
        return False
    destination = archive_path(registry_root, run_id)
    if destination.exists():
        with run_registry.registry_lock(registry_root):
            run_registry.reconcile_finalize_wal_locked(registry_root, run_id)
            return _complete_archival_after_archive(
                registry_root,
                run_id,
                destination=destination,
                members=members,
            )
    source_identities = _member_identities(run_path, members)
    expected_sizes = {name: identity[2] for name, identity in source_identities.items()}
    run_registry.ensure_private_dir(archive_dir(registry_root))
    temp_destination = destination.with_name(f"{destination.name}.tmp")
    if temp_destination.exists():
        temp_destination.unlink()
    try:
        with tarfile.open(temp_destination, "w:gz") as archive:
            for name in members:
                archive.add(run_path / name, arcname=name)
        run_registry.ensure_private_file(temp_destination)
        if not _verify_archive_members(temp_destination, expected_sizes):
            return False
        with run_registry.registry_lock(registry_root):
            run_registry.reconcile_finalize_wal_locked(registry_root, run_id)
            if _member_identities(run_path, members) != source_identities:
                return False
            if not destination.exists():
                os.replace(temp_destination, destination)
                run_registry.ensure_private_file(destination)
                return _complete_archival_after_archive(
                    registry_root,
                    run_id,
                    destination=destination,
                    members=members,
                    archive_verified=True,
                )
            return _complete_archival_after_archive(
                registry_root,
                run_id,
                destination=destination,
                members=members,
            )
    finally:
        if temp_destination.exists():
            temp_destination.unlink()


def empty_reclaim_payload(*, older_than_days: int, dry_run: bool) -> JsonObject:
    return {
        "schema": RUNS_RECLAIM_SCHEMA,
        "ok": True,
        "olderThanDays": older_than_days,
        "dryRun": dry_run,
        "planned": [],
        "reclaimed": [],
        "skipped": [],
        "errors": [],
        "totalBytes": 0,
        "budgetExhausted": False,
    }


def _mark_scratch_reclaimed(
    registry_root: Path, run_id: str, *, reclaimed_bytes: int, moment: datetime
) -> None:
    """Record in the run state that the scratch is gone, so readers are not confused."""
    run_path = run_registry.run_directory(registry_root, run_id)
    with run_registry.registry_lock(registry_root):
        run_registry.reconcile_finalize_wal_locked(registry_root, run_id)
        state = run_registry.read_json_object_or_none(run_path / run_registry.STATE_FILE)
        if state is None:
            return
        state[SCRATCH_RECLAIMED_AT_KEY] = moment.strftime(run_registry.UTC_TIMESTAMP_FORMAT)
        state[SCRATCH_RECLAIMED_BYTES_KEY] = reclaimed_bytes
        run_registry.write_run_state(run_path, state)


def _reclaim_scratch_locked(
    registry_root: Path,
    *,
    older_than_days: int,
    dry_run: bool,
    now: datetime | None = None,
    budget_seconds: float | None = None,
) -> JsonObject:
    """Remove the scratch of terminal runs older than ``older_than_days``.

    The caller holds the retention lock. Only runs whose effective status is
    terminal qualify: a running or stale run is never touched, whatever its
    age. Removal reuses ``run_scratch``'s owned-path and ownership checks, and
    the run record itself stays; only the neutral scratch, its sidecars, and the
    compact child temp go. Records are never rewritten in a dry run.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=older_than_days)
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    payload = empty_reclaim_payload(older_than_days=older_than_days, dry_run=dry_run)
    planned: list[JsonObject] = payload["planned"]  # type: ignore[assignment]
    reclaimed: list[JsonObject] = payload["reclaimed"]  # type: ignore[assignment]
    skipped: list[JsonObject] = payload["skipped"]  # type: ignore[assignment]
    errors: list[JsonObject] = payload["errors"]  # type: ignore[assignment]
    index = run_registry.load_index(registry_root)
    total_bytes = 0
    for run_id in list(index.get("runs", {}).keys()):
        if not isinstance(run_id, str) or not run_registry.RUN_ID_RE.fullmatch(run_id):
            continue
        try:
            state = run_registry.load_run_state_or_none(registry_root, run_id)
            status = run_registry.effective_status(state)
            ref: JsonObject = {
                "alias": run_registry.alias_for_run(index, run_id),
                "runId": run_id,
                "effectiveStatus": status,
            }
            if status == run_registry.STATUS_RUNNING:
                skipped.append({**ref, "reason": "running"})
                continue
            if status not in run_registry.TERMINAL_STATUSES:
                skipped.append({**ref, "reason": "non_terminal"})
                continue
            if isinstance(state, dict) and SCRATCH_RECLAIMED_AT_KEY in state:
                skipped.append({**ref, "reason": "already_reclaimed"})
                continue
            manifest = run_registry.load_run_manifest_or_none(registry_root, run_id)
            if manifest is None or not ("scratchPath" in manifest or "tempPath" in manifest):
                skipped.append({**ref, "reason": "no_scratch"})
                continue
            activity = run_registry.activity_datetime(state, manifest, run_id)
            if activity is None:
                skipped.append({**ref, "reason": "invalid_activity"})
                continue
            if activity >= cutoff:
                skipped.append({**ref, "reason": "not_yet_old_enough"})
                continue
            if deadline is not None and time.monotonic() >= deadline:
                payload["budgetExhausted"] = True
                break
            ref["activityAt"] = run_registry.activity_timestamp(state, manifest, run_id)
            run_scratch.verify_recorded_paths(registry_root, run_id, manifest, action="reclaim")
            targets = run_scratch.owned_targets(registry_root, run_id)
            size = sum(run_scratch.tree_bytes(target) for target in targets)
            ref["scratchBytes"] = size
            ref["paths"] = [str(target) for target in targets]
            if not targets:
                if not dry_run:
                    _mark_scratch_reclaimed(registry_root, run_id, reclaimed_bytes=0, moment=moment)
                skipped.append({**ref, "reason": "nothing_to_reclaim"})
                continue
            planned.append(ref)
            if dry_run:
                total_bytes += size
                continue
            run_scratch.remove_targets(targets)
            _mark_scratch_reclaimed(registry_root, run_id, reclaimed_bytes=size, moment=moment)
            reclaimed.append(ref)
            total_bytes += size
        except (OSError, ValueError) as exc:
            errors.append(
                {
                    "code": "scratch_reclaim_failed",
                    "runId": run_id,
                    "message": str(exc),
                }
            )
    payload["totalBytes"] = total_bytes
    if errors:
        payload["ok"] = False
        payload["exitCode"] = run_registry.RUN_PRUNE_ERROR_EXIT_CODE
    return payload


def reclaim_scratch(
    registry_root: Path,
    *,
    older_than_days: int = DEFAULT_SCRATCH_RETENTION_DAYS,
    dry_run: bool = False,
    now: datetime | None = None,
) -> JsonObject:
    """Reclaim scratch of finished runs now (``delegate runs reclaim``)."""
    if not is_non_negative_int(older_than_days):
        raise ValueError("older_than_days must be a non-negative integer")
    with run_registry.file_lock(run_registry.retention_lock_path(registry_root)):
        return _reclaim_scratch_locked(
            registry_root, older_than_days=older_than_days, dry_run=dry_run, now=now
        )


def run_retention_pass(
    registry_root: Path,
    config: JsonObject,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    empty = {"scanned": 0, "archived": 0, "skipped": 0, "scratchReclaimed": 0}
    if not retention_enabled(config):
        return dict(empty)
    try:
        with run_registry.file_lock(
            run_registry.retention_lock_path(registry_root),
            timeout_seconds=0,
        ):
            index = run_registry.load_index(registry_root)
            moment = now or datetime.now(UTC)
            completed_at = _retention_completed_at(index)
            runs = index.get("runs")
            run_count = len(runs) if isinstance(runs, dict) else 0
            retention = index.get("retention")
            completed_run_count = (
                retention.get("completedRunCount") if isinstance(retention, dict) else None
            )
            if (
                completed_at is not None
                and completed_run_count == run_count
                and moment - completed_at < timedelta(seconds=RETENTION_CADENCE_SECONDS)
            ):
                return dict(empty)
            scanned = 0
            archived = 0
            skipped = 0
            for run_id in list(index.get("runs", {}).keys()):
                if not isinstance(run_id, str):
                    continue
                scanned += 1
                try:
                    if is_eligible_for_archival(
                        registry_root,
                        run_id,
                        config=config,
                        now=now,
                    ):
                        if archive_run_raw_logs(registry_root, run_id):
                            archived += 1
                        else:
                            skipped += 1
                    else:
                        skipped += 1
                except OSError:
                    skipped += 1
            # Scratch is reclaimed on its own age, independent of record
            # pruning and of raw-log archival. A failure here never fails the
            # command that happened to trigger the pass.
            scratch_reclaimed = 0
            budget_exhausted = False
            try:
                reclaim = _reclaim_scratch_locked(
                    registry_root,
                    older_than_days=scratch_retention_days(config),
                    dry_run=False,
                    now=now,
                    budget_seconds=SCRATCH_RECLAIM_BUDGET_SECONDS,
                )
                scratch_reclaimed = len(reclaim["reclaimed"])
                budget_exhausted = reclaim["budgetExhausted"] is True
            except (OSError, ValueError):
                pass
            # A pass cut short by the budget must not start the cadence window,
            # or the remaining scratch would wait for the next new run.
            if not budget_exhausted:
                _mark_retention_completed(registry_root, moment, run_count)
            return {
                "scanned": scanned,
                "archived": archived,
                "skipped": skipped,
                "scratchReclaimed": scratch_reclaimed,
            }
    except TimeoutError:
        return dict(empty)


def read_archived_member(archive_file: Path, member_name: str) -> str:
    _validate_archive_member_name(member_name)
    with tarfile.open(archive_file, "r:gz") as archive:
        try:
            member = archive.getmember(member_name)
        except KeyError:
            return ""
        extracted = archive.extractfile(member)
        if extracted is None:
            return ""
        return extracted.read().decode("utf-8", errors="replace")


def tail_archived_output(archive_file: Path, member_name: str, lines: int) -> LogOutput:
    _validate_archive_member_name(member_name)
    with tarfile.open(archive_file, "r:gz") as archive:
        try:
            extracted = archive.extractfile(member_name)
        except KeyError:
            return LogOutput(content="", truncated=False)
        if extracted is None:
            return LogOutput(content="", truncated=False)
        return _tail_text_stream_output(extracted, lines)


def read_log_output(
    registry_root: Path,
    run_id: str,
    log_name: str,
    *,
    tail: int | None,
    raw: bool,
) -> LogOutput:
    _validate_archive_member_name(log_name)
    run_path = run_registry.run_directory(registry_root, run_id)
    live_path = run_path / log_name
    if live_path.exists():
        return log_output.read_log_output(live_path, tail=tail, raw=raw)
    archive_file = archive_path(registry_root, run_id)
    if not archive_file.exists():
        return LogOutput(content="", truncated=False)
    if raw:
        content = read_archived_member(archive_file, log_name)
        return LogOutput(content=content, truncated=False)
    if tail is None:
        raise ValueError("add --tail N or --raw to read stdout/stderr log output")
    return tail_archived_output(archive_file, log_name, tail)


def log_file_byte_size(registry_root: Path, run_id: str, log_name: str) -> int:
    _validate_archive_member_name(log_name)
    run_path = run_registry.run_directory(registry_root, run_id)
    live_path = run_path / log_name
    if live_path.exists():
        return live_path.stat().st_size
    state = run_registry.load_run_state(registry_root, run_id)
    if state is not None:
        key = "stdoutBytes" if log_name == run_registry.STDOUT_LOG else "stderrBytes"
        value = state.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    archive_file = archive_path(registry_root, run_id)
    try:
        with tarfile.open(archive_file, "r:gz") as archive:
            try:
                return archive.getmember(log_name).size
            except KeyError:
                return 0
    except FileNotFoundError:
        return 0
