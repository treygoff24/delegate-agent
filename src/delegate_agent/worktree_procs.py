"""Find processes whose working directory sits inside a worktree.

Removing a directory out from under a shell or an agent leaves it in a deleted
cwd, so destructive cleanup of a path with no run record (nothing else says
whether anyone is working there) asks the operating system first. The scan is
one pass over every process, never a recursive ``lsof +D`` walk of the tree:
``/proc/<pid>/cwd`` on Linux, ``lsof -d cwd`` elsewhere.

Other users' processes are invisible to any such scan; they are noted and do
not block. On Linux the kernel also refuses ``/proc/<pid>/cwd`` for some of this
user's own processes, for one of two reasons (the ptrace access check):

- the process is non-dumpable: ``systemd --user``, ``sd-pam``, ``ssh-agent``,
  ``sshd-session``, and services started by root that dropped to this user.
  Every login session has some, so blocking on them refused every removal on
  Linux. When ``/proc/<pid>/status`` shows real and effective user and group
  ids equal to our filesystem ids, the scan takes the denial to be
  dumpability; such a process is noted and does not block. The saved ids may
  differ: a set-group-id program that dropped back to our group keeps its old
  group as the saved id (``ssh-agent`` on Debian keeps ``_ssh``), and the
  kernel then denies for that id too.
- its real or effective user or group id differs from ours, for example a
  shell started with ``newgrp``. That can be real work inside the worktree, so
  it leaves the answer open, as does a process whose status cannot be read.

Shells, editors, agents, and bubblewrap-sandboxed lanes are dumpable and stay
visible (the sandbox case probed on a devbox cell, 2026-09-29). This makes the
check best-effort: a same-credential process denied for another reason works
inside a worktree unseen. Examples are a binary with file or ambient
capabilities, a service started as root that dropped to this user inside the
worktree, or a security-module denial. Removal still refuses uncommitted work
separately.

``ProcessCwdScan.checked`` is False when the scan could not run or left a
process of this user open, so a caller can refuse instead of treating "nothing
found" as "nobody there".
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
    non_dumpable = 0
    unreadable = 0
    other_users = 0
    for name in entries:
        try:
            cwd = os.readlink(PROC_ROOT / name / "cwd")
        except PermissionError:
            if _owned_by_another_user(PROC_ROOT / name):
                other_users += 1
            elif _has_our_credentials(PROC_ROOT / name):
                non_dumpable += 1
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
        notes.append(
            f"{unreadable} process(es) of this user could not be inspected "
            "(different group or user ids, or unreadable status)"
        )
    if non_dumpable:
        notes.append(f"{non_dumpable} non-dumpable process(es) of this user were not inspected")
    if other_users:
        notes.append(f"{other_users} process(es) belong to other users and were not inspected")
    # Only a same-user process whose credentials differ from ours (or cannot be
    # read) leaves the answer open; see the module docstring.
    return ProcessCwdScan(tuple(holders), unreadable == 0, "; ".join(notes) or None)


def _owned_by_another_user(path: Path) -> bool:
    try:
        return os.stat(path).st_uid != os.getuid()
    except OSError:
        return False


def _status_ids(path: Path) -> dict[str, list[str]] | None:
    """The ``Uid`` and ``Gid`` fields of a ``status`` file (real, effective, saved, fs)."""

    try:
        # Bytes, not text: the Name field may hold invalid UTF-8.
        status = (path / "status").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    ids: dict[str, list[str]] = {}
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key in ("Uid", "Gid"):
            ids[key] = value.split()
    return ids


def _caller_fs_ids() -> dict[str, str]:
    """This process's filesystem uid and gid, the ids the kernel's cwd check uses."""

    ids = _status_ids(PROC_ROOT / "self") or {}
    uid = ids.get("Uid", [])
    gid = ids.get("Gid", [])
    return {
        "Uid": uid[3] if len(uid) == 4 else str(os.geteuid()),
        "Gid": gid[3] if len(gid) == 4 else str(os.getegid()),
    }


def _has_our_credentials(path: Path) -> bool:
    """True when the target's real and effective uid and gid equal ours.

    The denial is then taken to be dumpability, or a saved id left by a
    set-id program that dropped back to us. That is the usual cause, not a
    proof: a capability mismatch or a security module can also deny (module
    docstring).
    """

    ids = _status_ids(path)
    if ids is None:
        return False
    ours = _caller_fs_ids()
    return all(
        len(ids.get(key, ())) >= 2 and set(ids[key][:2]) == {want} for key, want in ours.items()
    )


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
    # An empty answer, or an exit code lsof uses for real failures, means it
    # produced nothing to trust.
    if result.returncode not in (0, 1) or not result.stdout.strip():
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return ProcessCwdScan(checked=False, note=f"lsof produced no output ({detail})")
    holders = [
        ProcessHolder(pid, command)
        for pid, command, cwd in _parse_lsof_cwds(result.stdout)
        if _is_inside(cwd, target)
    ]
    # With no search items, a complete scan exits 0 (checked on macOS 27 with
    # other users' and root's processes present). Exit 1 means lsof hit an
    # error partway, so the printed rows may omit a process working in the
    # path: holders it did find still count, but finding none proves nothing.
    if result.returncode == 1 and not holders:
        detail = result.stderr.strip() or "exit 1"
        return ProcessCwdScan(checked=False, note=f"lsof scan was incomplete ({detail})")
    return ProcessCwdScan(tuple(holders), True, None)


def processes_with_cwd_inside(path: str | Path) -> ProcessCwdScan:
    """Return the processes whose current directory is ``path`` or inside it."""

    try:
        target = os.path.realpath(path)
    except (OSError, ValueError) as exc:
        return ProcessCwdScan(checked=False, note=f"could not resolve {path}: {exc}")
    return _scan_proc(target) or _scan_lsof(target)
