"""Read-only observations after an interrupted `git worktree add`."""

from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path

from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS, run_git_bytes
from delegate_agent.json_types import JsonObject


def _probe(cwd: str, args: list[str]) -> tuple[int, bytes]:
    try:
        result = run_git_bytes(cwd, args, timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS)
    except OSError:
        return -1, b""
    return result.returncode, result.stdout


def _path_kind(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _registration(root: str, path: Path) -> tuple[bool | None, bool | None]:
    code, output = _probe(root, ["worktree", "list", "--porcelain", "-z"])
    if code != 0:
        return None, None
    canonical = os.path.realpath(path)
    for record in output.split(b"\0\0"):
        fields = record.split(b"\0")
        if fields[0].startswith(b"worktree "):
            registered_path = os.fsdecode(fields[0].removeprefix(b"worktree "))
            if os.path.realpath(registered_path) == canonical:
                return True, any(f.partition(b" ")[0] == b"locked" for f in fields[1:])
    return False, None


def describe_timeout(
    root: str, path: str, *, branch: str | None, seconds: int
) -> tuple[str, JsonObject]:
    """A point-in-time report, never a claim that a checkout completed or a lock is stale."""
    wt = Path(path)
    kind = _path_kind(wt)
    marker = _path_kind(wt / ".git") if kind == "directory" else None
    entries = None
    if kind == "directory":
        try:
            with os.scandir(wt) as listing:
                entries = any(entry.name != ".git" for entry in listing)
        except OSError:
            pass
    registered, locked = _registration(root, wt)
    index_lock_path = None
    index_locked = None
    if registered and marker == "file":
        # Explicit gitdir: a broken worktree marker must not fall back to a
        # parent repository and report an unrelated index lock.
        code, raw = _probe(path, ["--git-dir=.git", "rev-parse", "--absolute-git-dir"])
        if code == 0 and raw:
            index_lock_path = Path(os.fsdecode(raw.removesuffix(b"\n"))) / "index.lock"
            lock_kind = _path_kind(index_lock_path)
            if lock_kind != "unknown":
                index_locked = lock_kind != "missing"
    detail: JsonObject = {
        "path": path,
        "timeoutSeconds": seconds,
        "pathKind": kind,
        "gitMarker": marker,
        "hasCheckoutEntries": entries,
        "registered": registered,
        "worktreeLocked": locked,
        "indexLocked": index_locked,
    }
    if index_lock_path is not None:
        detail["indexLockPath"] = str(index_lock_path)
    if branch:
        code, _ = _probe(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        detail.update(branch=branch, branchExists=(code == 0 if code in (0, 1) else None))

    def label(value: object) -> str:
        if value is None:
            return "unknown/not applicable"
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    facts = (
        f"path={kind}, .git={label(marker)}, checkout entries={label(entries)}, "
        f"registered={label(registered)}, worktree locked={label(locked)}, "
        f"index.lock present={label(index_locked)}"
    )
    if index_locked:
        facts += f" at {str(index_lock_path)!r}"
    if branch:
        facts += f", branch {branch} exists={label(detail['branchExists'])}"
    inspect = shlex.join(["git", "-C", root, "worktree", "list", "--porcelain"])
    message = (
        f"git worktree add timed out after {seconds}s at {path!r} ({facts}). "
        f"Delegate left the destination and Git metadata untouched after the timeout. "
        f"Inspect {inspect} and the destination. Check for an active checkout before touching "
        "any lock or retrying. For slow checkouts increase isolation.worktreeCreateTimeoutSeconds."
    )
    return message, {"worktreeCreation": detail}
