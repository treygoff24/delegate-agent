from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path

from delegate_agent.json_types import JsonObject, JsonValue

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class RegistryJsonError(ValueError):
    """Raised when an existing registry JSON file cannot be trusted."""


def supports_private_modes() -> bool:
    """Return whether chmod-style private mode hardening is available."""
    return os.name == "posix"


def ensure_private_dir(path: Path) -> None:
    """Create a registry directory and make it owner-only on POSIX."""
    path.mkdir(parents=True, exist_ok=True)
    if supports_private_modes():
        os.chmod(path, PRIVATE_DIR_MODE)


def ensure_private_file(path: Path) -> None:
    """Make an existing registry file owner-read/write on POSIX."""
    if supports_private_modes():
        os.chmod(path, PRIVATE_FILE_MODE)


def write_private_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Create/truncate a registry text file with owner-only permissions."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding=encoding) as handle:
        handle.write(text)
    ensure_private_file(path)


def write_private_bytes(path: Path, payload: bytes) -> None:
    """Create/truncate a registry byte file with owner-only permissions."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    ensure_private_file(path)


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace a non-private text file while preserving existing mode."""
    temp = path.with_name(f".{path.name}.tmp")
    existing_mode: int | None = None
    try:
        existing_mode = path.stat().st_mode & 0o777
    except OSError:
        existing_mode = None
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, existing_mode or 0o666)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        if existing_mode is not None:
            os.chmod(temp, existing_mode)
        os.replace(temp, path)
    except OSError:
        with suppress(OSError):
            os.unlink(temp)
        raise


def write_json_atomic(path: Path, payload: JsonObject) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    write_private_text(
        temp,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    os.replace(temp, path)
    ensure_private_file(path)


def read_json_object(path: Path) -> JsonObject | None:
    if not path.exists():
        return None
    try:
        data: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryJsonError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise RegistryJsonError(f"could not read JSON file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryJsonError(f"{path} root must be a JSON object")
    return data


def read_json_object_or_none(path: Path) -> JsonObject | None:
    try:
        return read_json_object(path)
    except RegistryJsonError:
        return None
