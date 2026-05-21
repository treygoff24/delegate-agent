from __future__ import annotations

import os
import tarfile
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from delegate_agent import run_registry

ARCHIVE_MEMBER_NAMES = (
    run_registry.STDOUT_LOG,
    run_registry.STDERR_LOG,
    run_registry.EVENTS_JSONL,
)
DEFAULT_RAW_LOG_RETENTION_DAYS = 7


def archive_dir(registry_root: Path) -> Path:
    return registry_root / "archive"


def archive_path(registry_root: Path, run_id: str) -> Path:
    return archive_dir(registry_root) / f"{run_id}.tar.gz"


def retention_settings(config: dict[str, Any]) -> dict[str, Any]:
    tracking = config.get("tracking")
    if not isinstance(tracking, dict):
        return {}
    retention = tracking.get("retention")
    return retention if isinstance(retention, dict) else {}


def retention_enabled(config: dict[str, Any]) -> bool:
    return bool(retention_settings(config).get("enabled", True))


def raw_log_retention_days(config: dict[str, Any]) -> int:
    settings = retention_settings(config)
    days = settings.get("rawLogDays", DEFAULT_RAW_LOG_RETENTION_DAYS)
    if not isinstance(days, int) or days < 0:
        return DEFAULT_RAW_LOG_RETENTION_DAYS
    return days


def should_skip_archival(registry_root: Path, run_id: str) -> bool:
    state = run_registry.load_run_state(registry_root, run_id)
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


def _verify_archive_members(archive_file: Path, expected_sizes: dict[str, int]) -> bool:
    if not expected_sizes:
        return False
    with tarfile.open(archive_file, "r:gz") as archive:
        found: dict[str, int] = {}
        for member in archive.getmembers():
            if member.name in expected_sizes:
                found[member.name] = member.size
        return all(name in found and found[name] == size for name, size in expected_sizes.items())


def _tail_text_stream(stream: BinaryIO, lines: int) -> str:
    if lines < 1:
        raise ValueError("tail lines must be at least 1")
    buffer: deque[str] = deque(maxlen=lines)
    for raw_line in stream:
        buffer.append(raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))
    if not buffer:
        return ""
    return "\n".join(buffer) + "\n"


def _state_archived_log_byte_sizes(state: dict[str, Any] | None) -> tuple[int, int] | None:
    if not state:
        return None
    stdout_bytes = state.get("stdoutBytes")
    stderr_bytes = state.get("stderrBytes")
    if isinstance(stdout_bytes, int) and isinstance(stderr_bytes, int):
        return stdout_bytes, stderr_bytes
    return None


def archived_log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    state_sizes = _state_archived_log_byte_sizes(
        run_registry.load_run_state(registry_root, run_id)
    )
    if state_sizes is not None:
        return state_sizes
    path = archive_path(registry_root, run_id)
    try:
        archive = tarfile.open(path, "r:gz")
    except FileNotFoundError:
        return 0, 0
    with archive:
        stdout_bytes = 0
        stderr_bytes = 0
        for member in archive.getmembers():
            if member.name == run_registry.STDOUT_LOG:
                stdout_bytes = member.size
            elif member.name == run_registry.STDERR_LOG:
                stderr_bytes = member.size
        return stdout_bytes, stderr_bytes


def effective_log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    run_path = run_registry.run_directory(registry_root, run_id)
    stdout_path = run_path / run_registry.STDOUT_LOG
    stderr_path = run_path / run_registry.STDERR_LOG
    if stdout_path.exists() or stderr_path.exists():
        return run_registry.log_byte_sizes(registry_root, run_id)
    if raw_logs_archived(registry_root, run_id):
        return archived_log_byte_sizes(registry_root, run_id)
    return 0, 0


def archived_log_warning(alias: str | None, run_id: str) -> str:
    handle = alias or run_id
    return (
        f"raw logs archived for {handle}; use "
        f"{run_registry.run_output_command(handle)} --stdout|--stderr --tail N or --raw"
    )


def is_eligible_for_archival(
    registry_root: Path,
    run_id: str,
    *,
    config: dict[str, Any],
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
        run_registry.load_run_state(registry_root, run_id),
        run_registry.load_run_manifest(registry_root, run_id),
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
    state_path = run_path / run_registry.STATE_FILE
    state = run_registry.read_json_object(state_path) or {}
    state["rawLogsArchivedAt"] = run_registry.utc_now_iso()
    state["stdoutBytes"] = stdout_bytes
    state["stderrBytes"] = stderr_bytes
    run_registry.write_json_atomic(state_path, state)


def _remove_archived_raw_logs(run_path: Path, members: list[str]) -> None:
    for name in members:
        (run_path / name).unlink()


def _complete_archival_after_archive(
    registry_root: Path,
    run_id: str,
    *,
    destination: Path,
    members: list[str],
) -> bool:
    run_path = run_registry.run_directory(registry_root, run_id)
    if not members:
        return False
    expected_sizes = _expected_member_sizes(run_path, members)
    if not _verify_archive_members(destination, expected_sizes):
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
        return _complete_archival_after_archive(
            registry_root,
            run_id,
            destination=destination,
            members=members,
        )
    expected_sizes = _expected_member_sizes(run_path, members)
    archive_dir(registry_root).mkdir(parents=True, exist_ok=True)
    temp_destination = destination.with_name(f"{destination.name}.tmp")
    if temp_destination.exists():
        temp_destination.unlink()
    try:
        with tarfile.open(temp_destination, "w:gz") as archive:
            for name in members:
                archive.add(run_path / name, arcname=name)
        if not _verify_archive_members(temp_destination, expected_sizes):
            return False
        os.replace(temp_destination, destination)
    finally:
        if temp_destination.exists():
            temp_destination.unlink()
    return _complete_archival_after_archive(
        registry_root,
        run_id,
        destination=destination,
        members=members,
    )


def run_retention_pass(
    registry_root: Path,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    if not retention_enabled(config):
        return {"scanned": 0, "archived": 0, "skipped": 0}
    index = run_registry.load_index(registry_root)
    scanned = 0
    archived = 0
    skipped = 0
    with run_registry.registry_lock(registry_root):
        for run_id in list(index.get("runs", {}).keys()):
            if not isinstance(run_id, str):
                continue
            scanned += 1
            if is_eligible_for_archival(registry_root, run_id, config=config, now=now):
                if archive_run_raw_logs(registry_root, run_id):
                    archived += 1
                else:
                    skipped += 1
            else:
                skipped += 1
    return {"scanned": scanned, "archived": archived, "skipped": skipped}


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


def tail_archived_member(archive_file: Path, member_name: str, lines: int) -> str:
    _validate_archive_member_name(member_name)
    with tarfile.open(archive_file, "r:gz") as archive:
        try:
            extracted = archive.extractfile(member_name)
        except KeyError:
            return ""
        if extracted is None:
            return ""
        return _tail_text_stream(extracted, lines)


def read_log_output(
    registry_root: Path,
    run_id: str,
    log_name: str,
    *,
    tail: int | None,
    raw: bool,
) -> tuple[str, bool]:
    from delegate_agent import rendering

    _validate_archive_member_name(log_name)
    run_path = run_registry.run_directory(registry_root, run_id)
    live_path = run_path / log_name
    if live_path.exists():
        return rendering.read_log_output(live_path, tail=tail, raw=raw)
    archive_file = archive_path(registry_root, run_id)
    if not archive_file.exists():
        return "", False
    if raw:
        content = read_archived_member(archive_file, log_name)
        return content, False
    if tail is None:
        raise ValueError("add --tail N or --raw to read stdout/stderr log output")
    trimmed = tail_archived_member(archive_file, log_name, tail)
    return trimmed, True


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
        if isinstance(value, int):
            return value
    archive_file = archive_path(registry_root, run_id)
    try:
        archive = tarfile.open(archive_file, "r:gz")
    except FileNotFoundError:
        return 0
    with archive:
        try:
            return archive.getmember(log_name).size
        except KeyError:
            return 0
