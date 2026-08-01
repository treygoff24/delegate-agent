"""Resolution, validation, and inspection of named persona prompt files."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from delegate_agent.errors import DelegateError
from delegate_agent.prompt_instructions import contains_c0_control

PERSONA_DIR_NAME = "personas"
PERSONA_SUFFIX = ".md"
PERSONA_MAX_BYTES = 64 * 1024
PERSONA_PREVIEW_MAX_CHARS = 120
PERSONA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class PersonaResolution:
    name: str
    source: str
    path: Path
    text: str
    size_bytes: int
    digest: str
    preview: str


def validate_persona_name(name: str) -> str:
    if not isinstance(name, str) or not PERSONA_NAME_RE.fullmatch(name):
        raise DelegateError(
            "invalid_persona_name",
            "persona name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}.",
        )
    return name


def _persona_dirs(workspace: str | Path) -> tuple[Path, Path]:
    source = Path(workspace).expanduser().resolve()
    return source / ".delegate" / PERSONA_DIR_NAME, Path.home() / ".delegate" / PERSONA_DIR_NAME


def _read_persona(root: Path, *, name: str) -> tuple[str, int, str, str]:
    """Read the fixed persona leaf through an anchored, no-follow descriptor walk."""
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    leaf_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise DelegateError("invalid_persona", f"persona {name!r} is not readable.") from exc
    try:
        try:
            fd = os.open(f"{name}{PERSONA_SUFFIX}", leaf_flags, dir_fd=root_fd)
        except OSError as exc:
            raise DelegateError("invalid_persona", f"persona {name!r} is not readable.") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise DelegateError("invalid_persona", f"persona {name!r} must be a regular file.")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(fd, 65536):
                size += len(chunk)
                if size > PERSONA_MAX_BYTES:
                    raise DelegateError(
                        "persona_too_large",
                        f"persona {name!r} exceeds the {PERSONA_MAX_BYTES}-byte limit.",
                    )
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DelegateError("invalid_persona_encoding", f"persona {name!r} must be UTF-8.") from exc
    if contains_c0_control(text):
        raise DelegateError(
            "invalid_persona_control",
            f"persona {name!r} contains a disallowed C0 control character.",
        )
    return text, size, hashlib.sha256(raw).hexdigest(), escaped_preview(text)


def escaped_preview(text: str) -> str:
    escaped = "".join(
        char
        if 0x20 <= ord(char) <= 0x7E
        else {"\n": r"\n", "\r": r"\r", "\t": r"\t"}.get(char, f"\\u{ord(char):04x}")
        for char in text
    )
    return escaped[:PERSONA_PREVIEW_MAX_CHARS]


def resolve_persona(
    workspace: str | Path,
    name: str,
    *,
    mode: str | None = None,
    allow_repo_persona: bool = False,
) -> PersonaResolution:
    validate_persona_name(name)
    workspace_dir, global_dir = _persona_dirs(workspace)
    workspace_path = workspace_dir / f"{name}{PERSONA_SUFFIX}"
    global_path = global_dir / f"{name}{PERSONA_SUFFIX}"
    if os.path.lexists(workspace_path):
        if mode == "safe" and not allow_repo_persona:
            raise DelegateError(
                "workspace_persona_refused",
                "safe mode refuses workspace-local personas by default; use --allow-repo-persona to opt in.",
            )
        path, source = workspace_path, "workspace"
        root = workspace_dir
    elif os.path.lexists(global_path):
        path, source = global_path, "global"
        root = global_dir
    else:
        raise DelegateError("persona_not_found", f"persona {name!r} was not found.")
    text, size, digest, preview = _read_persona(root, name=name)
    return PersonaResolution(name, source, path, text, size, digest, preview)


def _file_names(directory: Path) -> list[str]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if entry.name.endswith(PERSONA_SUFFIX) and entry.name != PERSONA_SUFFIX
    )


def _invalid_reason(exc: DelegateError) -> str:
    return f"{exc.error}: {exc.message}"


def list_personas(workspace: str | Path) -> list[dict[str, object]]:
    """List local/global persona rows without failing the whole inventory."""

    workspace_dir, global_dir = _persona_dirs(workspace)
    local_names = {name[: -len(PERSONA_SUFFIX)] for name in _file_names(workspace_dir)}
    global_names = {name[: -len(PERSONA_SUFFIX)] for name in _file_names(global_dir)}
    rows: list[dict[str, object]] = []
    for name in sorted(local_names | global_names):
        sources = [("workspace", workspace_dir)] if name in local_names else []
        if name in global_names and name not in local_names:
            sources.append(("global", global_dir))
        for source, directory in sources:
            row: dict[str, object] = {"name": name, "source": source}
            try:
                validate_persona_name(name)
                _text, size, _digest, preview = _read_persona(directory, name=name)
                row.update({"sizeBytes": size, "preview": preview})
            except DelegateError as exc:
                row["invalid"] = _invalid_reason(exc)
            if source == "workspace" and name in global_names:
                row["shadowsGlobal"] = True
            rows.append(row)
    return rows
