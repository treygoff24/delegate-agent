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
SNAPSHOT_FILE = "snapshot.json"


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


def load_run_state(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / STATE_FILE)


def load_run_state_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object_or_none(run_directory(registry_root, run_id) / STATE_FILE)


def load_run_snapshot(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / SNAPSHOT_FILE)


def load_run_snapshot_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object_or_none(run_directory(registry_root, run_id) / SNAPSHOT_FILE)


def load_run_manifest(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / MANIFEST_FILE)


def load_run_manifest_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object_or_none(run_directory(registry_root, run_id) / MANIFEST_FILE)
