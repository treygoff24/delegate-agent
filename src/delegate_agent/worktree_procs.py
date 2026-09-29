"""Find processes whose working directory sits inside a worktree.

Removing a directory out from under a shell or an agent leaves it in a deleted
cwd, so destructive cleanup of a path with no run record (nothing else says
whether anyone is working there) asks the operating system first. The scan is
one pass over every process, never a recursive ``lsof +D`` walk of the tree:
``/proc/<pid>/cwd`` on Linux, ``lsof -d cwd`` elsewhere.

Other users' processes may be invisible, and a machine with neither ``/proc``
nor ``lsof`` cannot be checked at all. ``ProcessCwdScan.checked`` is False when
the scan could not run or could not read one of this user's own processes, so a
caller can refuse instead of treating "nothing found" as "nobody there".
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

LSOF_TIMEOUT_SECONDS = 20
MAX_HOLDERS_REPORTED = 5
PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class ProcessHolder:
    pid: int
    command: str


@dataclass(frozen=True)
class ProcessCwdScan:
    holders: tuple[ProcessHolder, ...] = ()
    checked: bool = True
    note: str | None = None


def _is_inside(cwd: str, target: str) -> bool:
    return cwd == target or cwd.startswith(target.rstrip(os.sep) + os.sep)


def _scan_proc(target: str) -> ProcessCwdScan | None:
    """Scan ``/proc``; None when this machine has no usable procfs."""

    try:
        entries = [name for name in os.listdir(PROC_ROOT) if name.isdigit()]
    except OSError:
        return None
    if not entries or not (PROC_ROOT / "self" / "cwd").exists():
        return None
    holders: list[ProcessHolder] = []
    unreadable = 0
    other_users = 0
    for name in entries:
        try:
            cwd = os.readlink(PROC_ROOT / name / "cwd")
        except PermissionError:
            if _owned_by_another_user(PROC_ROOT / name):
                other_users += 1
            else:
                unreadable += 1
            continue
        except OSError:
            continue  # the process exited mid-scan, or is a kernel thread
        cwd = cwd.removesuffix(" (deleted)")
        if not _is_inside(cwd, target):
            continue
        try:
            command = (PROC_ROOT / name / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            command = ""
        holders.append(ProcessHolder(int(name), command))
    notes = []
    if unreadable:
        notes.append(f"{unreadable} process(es) of this user could not be inspected")
    if other_users:
        notes.append(f"{other_users} process(es) belong to other users and were not inspected")
    # A process of this user that cannot be read leaves the answer open; other
    # users' processes are outside what this check can ever see.
    return ProcessCwdScan(tuple(holders), unreadable == 0, "; ".join(notes) or None)


def _owned_by_another_user(path: Path) -> bool:
    try:
        return os.stat(path).st_uid != os.getuid()
    except OSError:
        return False


def _parse_lsof_cwds(output: str) -> list[tuple[int, str, str]]:
    """Parse ``lsof -Fpcfn`` output into ``(pid, command, cwd)`` rows."""

    rows: list[tuple[int, str, str]] = []
    pid: int | None = None
    command = ""
    is_cwd = False
    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
            command = ""
            is_cwd = False
        elif tag == "c":
            command = value
        elif tag == "f":
            is_cwd = value == "cwd"
        elif tag == "n" and is_cwd and pid is not None:
            rows.append((pid, command, value))
    return rows


def _scan_lsof(target: str) -> ProcessCwdScan:
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed argv, shell=False, bounded by a timeout.
            ["lsof", "-d", "cwd", "-Fpcfn"],
            capture_output=True,
            text=True,
            check=False,
            timeout=LSOF_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return ProcessCwdScan(checked=False, note="lsof is not installed")
    except (OSError, subprocess.SubprocessError) as exc:
        return ProcessCwdScan(checked=False, note=f"lsof failed: {exc}")
    # lsof exits 1 when some processes are unreadable but still prints the rest;
    # an empty answer, or an exit code it uses for real failures, means it
    # produced nothing to trust.
    if result.returncode not in (0, 1) or not result.stdout.strip():
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return ProcessCwdScan(checked=False, note=f"lsof produced no output ({detail})")
    holders = [
        ProcessHolder(pid, command)
        for pid, command, cwd in _parse_lsof_cwds(result.stdout)
        if _is_inside(cwd, target)
    ]
    return ProcessCwdScan(tuple(holders), True, None)


def processes_with_cwd_inside(path: str | Path) -> ProcessCwdScan:
    """Return the processes whose current directory is ``path`` or inside it."""

    try:
        target = os.path.realpath(path)
    except (OSError, ValueError) as exc:
        return ProcessCwdScan(checked=False, note=f"could not resolve {path}: {exc}")
    return _scan_proc(target) or _scan_lsof(target)
