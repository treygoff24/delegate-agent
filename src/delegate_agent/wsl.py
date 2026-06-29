from __future__ import annotations

import os
import re
from pathlib import Path

WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|%[A-Za-z_][A-Za-z0-9_]*%)")


def is_wsl(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def is_windows_path_text(value: str) -> bool:
    return bool(WINDOWS_PATH_RE.match(value.strip()))


def should_reject_windows_path(value: str) -> bool:
    return is_wsl() and is_windows_path_text(value)


def windows_path_message(field: str, value: str) -> str:
    return (
        f"{field} must be a POSIX/WSL path, got Windows-style path {value!r}. "
        "Inside WSL, convert Windows paths with `wslpath -u` or use `/home/...` "
        "or `/mnt/c/...`."
    )


def drivefs_mount(path: str | Path) -> str | None:
    parts = Path(path).as_posix().split("/")
    if len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1 and parts[2].isalnum():
        return f"/mnt/{parts[2]}"
    return None


def drivefs_workspace_warning(path: str | Path) -> str | None:
    if not is_wsl():
        return None
    mount = drivefs_mount(path)
    if mount is None:
        return None
    return (
        f"workspace is on Windows-mounted filesystem {mount}; WSL runs are faster "
        "and Delegate's private-mode file permissions are more reliable under "
        "`/home/<user>/...`."
    )


def windows_git_message(git_path: str | None) -> str | None:
    if not is_wsl() or not git_path:
        return None
    path = Path(git_path)
    if path.name.lower() != "git.exe":
        return None
    return (
        f"git resolves to Windows Git at {git_path!r}. Install Git inside WSL "
        "(`sudo apt install git`) or put WSL-native Git before Windows PATH entries."
    )
