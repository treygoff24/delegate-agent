"""macOS Seatbelt boundary for Codex pure calls."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from delegate_agent.errors import DelegateError

# Characters that break the Seatbelt s-expression grammar.  Absolute realpaths on
# macOS never legitimately contain them; a path that does is treated as hostile
# or malformed and the codex pure call fails closed.
_PROFILE_PATH_FORBIDDEN_CHARS = frozenset(("\n", "\r", '"', "(", ")"))


def codex_pure_available() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _absolute_real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _seatbelt_string(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _validate_profile_path(path: str) -> str:
    """Return the absolute realpath after rejecting Seatbelt metacharacters."""
    real = _absolute_real(path)
    for char in _PROFILE_PATH_FORBIDDEN_CHARS:
        if char in real:
            raise DelegateError(
                "seatbelt_profile_path_invalid",
                f"Seatbelt profile path contains a forbidden character ({char!r}): {path!r}",
            )
    return real


def _is_node_script(path: str) -> bool:
    """Return True if *path* is a text file whose shebang references node."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(256)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        return False
    try:
        line = head.split(b"\n", 1)[0].decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return False
    return "node" in line.lower()


def _package_root(binary_path: str) -> str:
    """Return the directory that contains the bin/ directory for *binary_path*.

    A Node binary needs its lib/ sibling (e.g. /Cellar/node/26.3.1), and the
    Codex JS wrapper needs its package root (e.g. .../node_modules/@openai/codex).
    """
    parent = Path(binary_path).parent
    if parent.name == "bin":
        return str(parent.parent)
    return str(parent)


class _RuntimeAllow:
    __slots__ = ("kind", "path")

    def __init__(self, path: str, kind: str) -> None:
        self.kind = kind
        self.path = path


def _runtime_directories(binary: str, env: Mapping[str, str] | None = None) -> list[_RuntimeAllow]:
    """Return runtime read-allows for *binary*.

    Where the binary is a single file that does not need sibling runtime files it
    is allowed as a Seatbelt ``literal``.  When the binary needs sibling runtime
    files (node needs its lib directory, the Codex JS wrapper needs its package
    root) the parent directory is allowed as a ``subpath``.
    """
    path = shutil.which(binary, path=env.get("PATH") if env else None)
    if path is None:
        return []
    absolute = os.path.abspath(os.path.expanduser(path))
    real = os.path.realpath(absolute)
    allows: list[_RuntimeAllow] = []

    if binary == "node":
        # node binary needs its lib/ sibling (libnode.dylib, etc.).
        allows.append(_RuntimeAllow(_validate_profile_path(_package_root(real)), "subpath"))
    elif binary == "codex":
        if _is_node_script(real):
            # The npm-installed codex wrapper loads modules from its package root.
            allows.append(_RuntimeAllow(_validate_profile_path(_package_root(real)), "subpath"))
        else:
            # Shell wrappers or compiled binaries only need the file itself.
            allows.append(_RuntimeAllow(_validate_profile_path(real), "literal"))
    else:
        allows.append(_RuntimeAllow(_validate_profile_path(real), "literal"))

    # If the absolute path is a symlink outside the real subpath, the symlink
    # itself must be readable so the kernel can resolve it.
    if absolute != real:
        covered = any(
            absolute == allowed.path or absolute.startswith(allowed.path + os.sep)
            for allowed in allows
        )
        if not covered:
            allows.append(_RuntimeAllow(_validate_profile_path(absolute), "literal"))

    return allows


def build_codex_pure_profile(
    *,
    home: str,
    temp_cwd: str,
    codex_home: str,
    extra_read_roots: list[str],
    env: Mapping[str, str] | None = None,
) -> str:
    """Return a deny-home Seatbelt profile with narrow runtime read exceptions."""
    home = _validate_profile_path(home)
    temp_cwd = _validate_profile_path(temp_cwd)
    codex_home = _validate_profile_path(codex_home)

    subpaths: list[str] = []
    literals: list[str] = []

    for binary in ("codex", "node"):
        for allow in _runtime_directories(binary, env=env):
            if allow.kind == "subpath":
                subpaths.append(allow.path)
            else:
                literals.append(allow.path)

    subpaths.extend([temp_cwd, codex_home])

    for path in extra_read_roots:
        validated = _validate_profile_path(path)
        if os.path.isdir(validated):
            subpaths.append(validated)
        else:
            literals.append(validated)

    denied_prefixes = [
        home,
        "/Users/Shared",
        "/tmp",
        "/private/tmp",
    ]

    lines = [
        "(version 1)",
        "(allow default)",
    ]
    for prefix in denied_prefixes:
        lines.append(f'(deny file-read-data (subpath "{_seatbelt_string(prefix)}"))')
    for path in dict.fromkeys(subpaths):
        lines.append(f'(allow file-read-data (subpath "{_seatbelt_string(path)}"))')
    for path in dict.fromkeys(literals):
        lines.append(f'(allow file-read-data (literal "{_seatbelt_string(path)}"))')

    return "\n".join(lines) + "\n"
