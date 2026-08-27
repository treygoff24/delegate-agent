"""Independent ``/proc`` watcher for linked-worktree registry-lock escapes.

The watcher looks at the kernel's flock table rather than monkeypatching
``fcntl`` in the pytest process.  Its separate process therefore observes
Python and non-Python descendants alike, including an installed launcher that
does not import this checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class LockViolation:
    owner_pid: int
    owner_command: str
    owner_cwd: str
    holder_pids: tuple[int, ...]
    target: str


def _target_identity(target: Path) -> tuple[int, int] | None:
    try:
        info = target.stat()
    except OSError:
        return None
    return os.major(info.st_dev), os.minor(info.st_dev), info.st_ino


def _process_details(pid: int) -> tuple[str, str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        command = " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
    except OSError:
        command = "<exited>"
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = "<unavailable>"
    return command or "<unknown>", cwd


def _fd_holders(target: Path) -> tuple[int, ...]:
    holders: set[int] = set()
    expected = str(target)
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return ()
    for process in processes:
        if not process.name.isdigit():
            continue
        pid = int(process.name)
        try:
            fds = list((process / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link == expected:
                holders.add(pid)
                break
    return tuple(sorted(holders))


def scan_lock(target: Path) -> tuple[LockViolation, ...]:
    """Return every matching exclusive flock currently held on ``target``."""
    identity = _target_identity(target)
    if identity is None:
        return ()
    major, minor, inode = identity
    expected_device = f"{major:02x}:{minor:02x}"
    violations: list[LockViolation] = []
    try:
        lines = Path("/proc/locks").read_text(encoding="ascii").splitlines()
    except OSError:
        return ()
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK" or fields[3] != "WRITE":
            continue
        if fields[5].split(":")[-1] != str(inode) or fields[5].rsplit(":", 1)[0] != expected_device:
            continue
        try:
            owner_pid = int(fields[4])
        except ValueError:
            continue
        command, cwd = _process_details(owner_pid)
        holders = _fd_holders(target)
        violations.append(LockViolation(owner_pid, command, cwd, holders, str(target)))
    return tuple(violations)


def _watch(target: Path, report: Path, stop: Path, *, interval: float = 0.02) -> int:
    seen: set[tuple[int, tuple[int, ...]]] = set()
    while not stop.exists():
        for violation in scan_lock(target):
            key = (violation.owner_pid, violation.holder_pids)
            if key in seen:
                continue
            seen.add(key)
            with report.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(violation), sort_keys=True) + "\n")
                handle.flush()
        time.sleep(interval)
    return 0


def source_lock_for_linked_worktree(worktree: Path) -> Path | None:
    """Resolve the source checkout's lock only when ``worktree`` is linked."""
    try:
        top = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        common = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not top or not common:
        return None
    top_path = Path(top).resolve()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (top_path / common_path).resolve()
    else:
        common_path = common_path.resolve()
    source_root = common_path.parent if common_path.name == ".git" else common_path
    if source_root == top_path:
        return None
    return source_root / ".delegate" / ".registry.lock"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    args = parser.parse_args()
    return _watch(args.target, args.report, args.stop)


if __name__ == "__main__":
    raise SystemExit(main())
