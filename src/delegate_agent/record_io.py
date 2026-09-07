"""Read-only run record primitives shared by registry mutations and status views.

Keep path validation and bounded private reads here, below both callers in the
dependency graph. The registry re-exports the existing helper surface.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from delegate_agent import terminal_states
from delegate_agent.json_types import JsonObject
from delegate_agent.private_io import (
    RegistryJsonError,
    read_json_object,
    read_json_object_or_none,
)

RUN_ID_RE = re.compile(r"^del_\d{8}T\d{6}Z_[0-9a-f]{6}$")
STDOUT_LOG = "stdout.log"
STDERR_LOG = "stderr.log"
MANIFEST_FILE = "manifest.json"
STATE_FILE = "state.json"
STATE_SCHEMA = "delegate.state.v1"
SNAPSHOT_FILE = "snapshot.json"
FINALIZE_WAL_FILE = "finalize-wal.json"
FINALIZE_WAL_SCHEMA = "delegate.finalize-wal.v2"
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def runs_dir(registry_root: Path) -> Path:
    return registry_root / "runs"


def run_directory(registry_root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise RegistryJsonError(f"invalid run id for registry path: {run_id!r}")
    root = runs_dir(registry_root).resolve(strict=False)
    path = (root / run_id).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError:
        raise RegistryJsonError(f"run path escapes registry: {run_id!r}") from None
    return path


def index_run_entries(index: JsonObject) -> Iterator[tuple[str, JsonObject]]:
    """Yield usable index entries without deriving paths from corrupt keys."""
    runs = index.get("runs", {})
    if not isinstance(runs, dict):
        return
    for run_id, entry in runs.items():
        if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) and isinstance(entry, dict):
            yield run_id, entry


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def timestamp_from_run_id(run_id: str) -> str:
    match = re.match(r"^del_(\d{8}T\d{6}Z)_", run_id)
    if not match:
        return ""
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}:{raw[11:13]}:{raw[13:15]}Z"


def snapshot_command(alias: str, *, cwd: str | None = None) -> str:
    if cwd is None:
        return f"delegate snapshot {alias}"
    return shlex.join(["delegate", "--cwd", cwd, "snapshot", alias])


def run_output_command(
    handle: str, *, completion_report: bool = False, cwd: str | None = None
) -> str:
    if cwd is not None:
        argv = ["delegate", "--cwd", cwd, "run-output", handle]
        if completion_report:
            argv.append("--completion-report")
        return shlex.join(argv)
    base = f"delegate run-output {handle}"
    if completion_report:
        return f"{base} --completion-report"
    return base


def validate_finalize_wal(wal: JsonObject, run_id: str) -> JsonObject:
    record = wal.get("record")
    status = wal.get("status")
    if (
        wal.get("schema") != FINALIZE_WAL_SCHEMA
        or wal.get("runId") != run_id
        or status not in TERMINAL_STATUSES
        or not isinstance(record, dict)
        or record.get("schema") != STATE_SCHEMA
        or record.get("runId") != run_id
        or record.get("status") != status
    ):
        raise RegistryJsonError("WAL schema, run id, or terminal record is invalid")
    return record


def read_finalize_wal(
    run_path: Path,
    run_id: str,
    *,
    suppress_malformed: bool,
) -> JsonObject | None:
    try:
        wal = read_json_object(run_path / FINALIZE_WAL_FILE)
    except RegistryJsonError:
        if suppress_malformed:
            return None
        raise
    if not isinstance(wal, dict):
        return None
    try:
        return validate_finalize_wal(wal, run_id)
    except RegistryJsonError:
        if suppress_malformed:
            return None
        raise


def pending_finalize_wal_exists(registry_root: Path, run_id: str) -> bool:
    return (run_directory(registry_root, run_id) / FINALIZE_WAL_FILE).exists()


def merge_terminal_record(
    current: JsonObject | None,
    pending: JsonObject,
) -> JsonObject:
    current_status = current.get("status") if isinstance(current, dict) else None
    if isinstance(current, dict) and (
        current_status == "cancelled" or current.get("cancelRequested") is True
    ):
        # Cancellation owns the outcome, not the finalizer's late output.
        cancelled = {**current, **pending}
        cancelled["status"] = "cancelled"
        cancelled["ok"] = False
        terminal_states.apply_operator_cancel_override(cancelled)
        for key in ("stdoutBytes", "stderrBytes"):
            old = current.get(key)
            new = pending.get(key)
            counts = [value for value in (old, new) if type(value) is int and value >= 0]
            if counts:
                cancelled[key] = max(counts)
        for key in ("cancelRequested", "cancelRequestedAt"):
            if key in current:
                cancelled[key] = current[key]
        warnings: list[str] = []
        for source in (current, pending):
            values = source.get("warnings")
            if isinstance(values, list):
                warnings.extend(
                    value for value in values if isinstance(value, str) and value not in warnings
                )
        if warnings:
            cancelled["warnings"] = warnings
        return cancelled
    if isinstance(current, dict) and current_status in TERMINAL_STATUSES:
        return current
    return pending


def _load_run_state_with_pending_wal(
    registry_root: Path,
    run_id: str,
    *,
    permissive: bool,
) -> JsonObject | None:
    run_path = run_directory(registry_root, run_id)
    reader = read_json_object_or_none if permissive else read_json_object
    current = reader(run_path / STATE_FILE)
    pending = read_finalize_wal(run_path, run_id, suppress_malformed=True)
    if pending is None:
        return current
    fresh = reader(run_path / STATE_FILE)
    return merge_terminal_record(fresh, pending)


def load_run_state(registry_root: Path, run_id: str) -> JsonObject | None:
    return _load_run_state_with_pending_wal(registry_root, run_id, permissive=False)


def load_run_state_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return _load_run_state_with_pending_wal(registry_root, run_id, permissive=True)


def _computed_snapshot(
    registry_root: Path,
    run_id: str,
    *,
    permissive: bool,
) -> JsonObject | None:
    run_path = run_directory(registry_root, run_id)
    state = _load_run_state_with_pending_wal(registry_root, run_id, permissive=permissive)
    legacy_reader = (
        read_json_object_or_none if permissive or state is not None else read_json_object
    )
    legacy_snapshot = legacy_reader(run_path / SNAPSHOT_FILE)
    if state is None and legacy_snapshot is None:
        return None
    from delegate_agent import snapshot_view

    return snapshot_view.snapshot_json_payload(
        snapshot_view.merge_snapshot_view(
            registry_root,
            run_id,
            legacy_snapshot,
            redact=False,
            state=state,
        )
    )


def load_run_snapshot(registry_root: Path, run_id: str) -> JsonObject | None:
    return _computed_snapshot(registry_root, run_id, permissive=False)


def load_run_snapshot_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return _computed_snapshot(registry_root, run_id, permissive=True)


def load_run_manifest(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / MANIFEST_FILE)


def load_run_manifest_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object_or_none(run_directory(registry_root, run_id) / MANIFEST_FILE)
