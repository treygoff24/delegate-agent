"""Detect files an unisolated run changed outside its working directory.

``--isolation none`` runs the child in the caller's real tree, and nothing stops
it from editing a sibling directory of the repo it was pointed at. The only
place Delegate can see that cheaply is the enclosing git repository: when cwd
is a subdirectory of a work tree, a before/after ``git status`` over the whole
tree shows changes that landed outside cwd. Anything outside that repository
(or a run whose cwd is the repo root) is not detectable here, and the caller
says so rather than implying coverage.

Each dirty entry is keyed by its porcelain status plus the file's size and
mtime, so a further edit to a file that was already dirty before launch is
still seen. Best effort throughout: a git failure yields no snapshot, never a
blocked run.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - git with shell=False via git_utils.
from dataclasses import dataclass
from pathlib import Path

from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS
from delegate_agent.git_utils import run_git_bytes as _run_git_bytes

EXAMPLE_LIMIT = 5

Signature = tuple[str, int, int]


@dataclass(frozen=True)
class OutsideCwdSnapshot:
    toplevel: str
    cwd_relative: str  # POSIX, no trailing slash; never "" (root cwd is not snapshotted)
    entries: dict[str, Signature]
    ignored_prefixes: tuple[str, ...] = ()  # Delegate's own registry writes


def _toplevel(cwd: str) -> str | None:
    try:
        result = _run_git_bytes(
            cwd,
            ["--no-optional-locks", "rev-parse", "--show-toplevel"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return os.fsdecode(result.stdout).strip() or None


def _status_entries(toplevel: str) -> dict[str, Signature] | None:
    try:
        result = _run_git_bytes(
            toplevel,
            [
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
            ],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    entries: dict[str, Signature] = {}
    root = Path(toplevel)
    for record in result.stdout.split(b"\0"):
        if len(record) < 4:
            continue
        status = os.fsdecode(record[:2])
        path = os.fsdecode(record[3:])
        try:
            stat = (root / path).lstat()
            entries[path] = (status, stat.st_size, stat.st_mtime_ns)
        except OSError:
            entries[path] = (status, -1, -1)
    return entries


def capture(cwd: str, *, own_paths: tuple[str, ...] = ()) -> OutsideCwdSnapshot | None:
    """Snapshot the enclosing repo when cwd is strictly inside it; else None.

    ``own_paths`` are directories Delegate itself writes during the run (the
    registry); changes under them are never attributed to the child.
    """
    toplevel = _toplevel(cwd)
    if toplevel is None:
        return None
    try:
        relative = Path(cwd).resolve().relative_to(Path(toplevel).resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if relative in ("", "."):
        return None
    entries = _status_entries(toplevel)
    if entries is None:
        return None
    top = Path(toplevel).resolve()
    ignored: list[str] = []
    for own in own_paths:
        try:
            ignored.append(Path(own).resolve().relative_to(top).as_posix() + "/")
        except (OSError, ValueError):
            continue
    return OutsideCwdSnapshot(
        toplevel=toplevel,
        cwd_relative=relative,
        entries=entries,
        ignored_prefixes=tuple(ignored),
    )


def changed_outside(snapshot: OutsideCwdSnapshot) -> tuple[str, ...] | None:
    """Repo-relative paths outside cwd whose dirty state changed since ``snapshot``."""
    current = _status_entries(snapshot.toplevel)
    if current is None:
        return None
    prefix = snapshot.cwd_relative + "/"
    changed = {
        path
        for path in snapshot.entries.keys() | current.keys()
        if snapshot.entries.get(path) != current.get(path)
    }
    skipped = (prefix, *snapshot.ignored_prefixes)
    return tuple(sorted(path for path in changed if not path.startswith(skipped)))


def warning(paths: tuple[str, ...], toplevel: str) -> str:
    preview = ", ".join(paths[:EXAMPLE_LIMIT])
    if len(paths) > EXAMPLE_LIMIT:
        preview = f"{preview}, ... (+{len(paths) - EXAMPLE_LIMIT} more)"
    return (
        f"--isolation none: the child changed {len(paths)} path(s) outside its working "
        f"directory in {toplevel}: {preview}. Review them; only changes inside this git "
        "repository are detectable."
    )
