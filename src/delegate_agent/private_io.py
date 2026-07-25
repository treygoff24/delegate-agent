from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from delegate_agent.json_types import JsonObject, JsonValue

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class RegistryJsonError(ValueError):
    """Raised when an existing registry JSON file cannot be trusted."""


def supports_private_modes() -> bool:
    """Return whether chmod-style private mode hardening is available."""
    return os.name == "posix"


def _anchored_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    """Anchor registry paths at their workspace and keep traversal descriptor-relative."""
    absolute = Path(os.path.abspath(path))
    marker_indexes = [index for index, part in enumerate(absolute.parts) if part == ".delegate"]
    if marker_indexes:
        marker = marker_indexes[-1]
        anchor = Path(*absolute.parts[:marker])
        relative = absolute.parts[marker:]
    else:
        anchor = absolute.parent
        relative = (absolute.name,)
    return anchor.resolve(strict=True), relative


def _open_directory_parts(anchor: Path, parts: tuple[str, ...], *, create: bool) -> int:
    fd = os.open(anchor, _DIRECTORY_FLAGS)
    try:
        for part in parts:
            try:
                entry = os.lstat(part, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, PRIVATE_DIR_MODE, dir_fd=fd)
                entry = os.lstat(part, dir_fd=fd)
            if stat.S_ISLNK(entry.st_mode):
                raise OSError(errno.ELOOP, "registry path component is a symlink", part)
            if not stat.S_ISDIR(entry.st_mode):
                raise NotADirectoryError(
                    errno.ENOTDIR, "registry path component is not a directory", part
                )
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_parent(path: Path, *, create: bool) -> tuple[int, str]:
    anchor, parts = _anchored_parts(path)
    if not parts or not parts[-1]:
        raise ValueError(f"path must name a file: {path}")
    return _open_directory_parts(anchor, parts[:-1], create=create), parts[-1]


def _open_strict_parent(path: Path) -> tuple[int, str]:
    """Open/create a parent by traversing every component from the filesystem anchor.

    Unlike the registry-root anchoring used by existing helpers, this rejects
    symlinks anywhere in the caller's spelling. Callers using system path aliases
    (for example ``/var`` pointing at ``/private/var``) must pass a resolved path.
    """
    absolute = Path(os.path.abspath(path))
    anchor = Path(absolute.anchor)
    parts = absolute.relative_to(anchor).parts
    if not parts or not parts[-1]:
        raise ValueError(f"path must name a file: {path}")
    return _open_directory_parts(anchor, parts[:-1], create=True), parts[-1]


def open_private_file(path: Path, flags: int, mode: int = PRIVATE_FILE_MODE) -> int:
    """Open a registry file without following symlinks in it or its registry path."""
    parent_fd, name = _open_parent(path, create=bool(flags & os.O_CREAT))
    try:
        safe_flags = flags | _NOFOLLOW_FLAGS
        if flags & os.O_CREAT and not flags & os.O_EXCL:
            for _attempt in range(3):
                try:
                    return os.open(
                        name,
                        safe_flags | os.O_EXCL,
                        mode,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    try:
                        return os.open(
                            name,
                            safe_flags & ~os.O_CREAT,
                            mode,
                            dir_fd=parent_fd,
                        )
                    except FileNotFoundError:
                        continue
        return os.open(name, safe_flags, mode, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def ensure_private_dir(path: Path) -> None:
    """Create a registry directory without following symlinks and make it owner-only."""
    anchor, parts = _anchored_parts(path)
    fd = _open_directory_parts(anchor, parts, create=True)
    try:
        if supports_private_modes():
            os.fchmod(fd, PRIVATE_DIR_MODE)
    finally:
        os.close(fd)


def ensure_private_file(path: Path) -> None:
    """Make an existing non-symlink registry file owner-read/write on POSIX."""
    fd = open_private_file(path, os.O_RDONLY)
    try:
        if supports_private_modes():
            os.fchmod(fd, PRIVATE_FILE_MODE)
    finally:
        os.close(fd)


def write_private_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Create/truncate a registry text file with owner-only permissions."""
    fd = open_private_file(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding=encoding) as handle:
        handle.write(text)


def write_private_bytes(path: Path, payload: bytes) -> None:
    """Create/truncate a registry byte file with owner-only permissions."""
    fd = open_private_file(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace a non-private text file while preserving existing mode."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    existing_mode: int | None = None
    try:
        existing_mode = path.stat().st_mode & 0o777
    except OSError:
        existing_mode = None
    fd = os.open(
        temp,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW_FLAGS,
        existing_mode or 0o666,
    )
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


def write_json_atomic(
    path: Path,
    payload: JsonObject,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    """Atomically replace private JSON, optionally re-checking the destination first.

    ``before_replace`` runs after the temporary file is complete and
    immediately before ``os.replace``, so a caller whose decision to write
    depends on what is already on disk can re-read it with the narrowest
    possible window rather than deciding before serialization. Raising from it
    aborts the write and removes the temporary file; the destination is left
    exactly as it was.
    """
    parent_fd, name = _open_parent(path, create=True)
    temp_name = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    replaced = False
    try:
        try:
            existing = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise OSError(errno.ELOOP, "registry file is a symlink", str(path))
        fd = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW_FLAGS,
            PRIVATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if before_replace is not None:
            before_replace()
        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replaced = True
    finally:
        if not replaced:
            with suppress(OSError):
                os.unlink(temp_name, dir_fd=parent_fd)
        os.close(parent_fd)


def write_json_atomic_if_absent(path: Path, payload: JsonObject) -> bool:
    """Atomically create private JSON without replacing an existing target."""
    parent_fd, name = _open_strict_parent(path)
    temp_name = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    temp_exists = False
    try:
        try:
            existing = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise OSError(errno.ELOOP, "registry file is a symlink", str(path))
            return False

        fd = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW_FLAGS,
            PRIVATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        temp_exists = True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.link(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            try:
                existing = os.lstat(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(existing.st_mode):
                raise OSError(errno.ELOOP, "registry file is a symlink", str(path)) from None
            return False
        os.unlink(temp_name, dir_fd=parent_fd)
        temp_exists = False
        os.fsync(parent_fd)
        return True
    finally:
        if temp_exists:
            with suppress(OSError):
                os.unlink(temp_name, dir_fd=parent_fd)
        os.close(parent_fd)


def read_json_object(path: Path) -> JsonObject | None:
    try:
        fd = open_private_file(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryJsonError(f"could not read JSON file {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            data: JsonValue = json.load(handle)
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
