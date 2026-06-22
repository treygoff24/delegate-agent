from __future__ import annotations

import tarfile
from pathlib import Path

from delegate_agent.json_types import JsonObject, is_non_negative_int


def archive_dir(registry_root: Path) -> Path:
    return registry_root / "archive"


def archive_path(registry_root: Path, run_id: str) -> Path:
    return archive_dir(registry_root) / f"{run_id}.tar.gz"


def state_log_byte_sizes(state: JsonObject | None) -> tuple[int, int] | None:
    if not state:
        return None
    stdout_bytes = state.get("stdoutBytes")
    stderr_bytes = state.get("stderrBytes")
    if is_non_negative_int(stdout_bytes) and is_non_negative_int(stderr_bytes):
        return stdout_bytes, stderr_bytes
    return None


def archive_log_byte_sizes(
    archive_file: Path,
    *,
    stdout_log: str,
    stderr_log: str,
) -> tuple[int, int]:
    try:
        with tarfile.open(archive_file, "r:gz") as archive:
            stdout_bytes = 0
            stderr_bytes = 0
            for member in archive.getmembers():
                if member.name == stdout_log:
                    stdout_bytes = member.size
                elif member.name == stderr_log:
                    stderr_bytes = member.size
            return stdout_bytes, stderr_bytes
    except FileNotFoundError:
        return 0, 0
