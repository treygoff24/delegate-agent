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
    attribution: str = "unverified"


def _target_identity(target: Path) -> tuple[int, int, int] | None:
    try:
        info = target.stat()
    except OSError:
        return None
    return os.major(info.st_dev), os.minor(info.st_dev), info.st_ino


def _parent_pid(pid: int) -> int | None:
    """Return ``pid``'s parent, or None when the process is gone."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The second field is the command name and may itself contain spaces and
    # parens, so start counting after the final closing paren.
    fields = raw.rpartition(")")[2].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _ancestry_contains(pid: int, ancestor_pid: int) -> bool:
    """Whether ``ancestor_pid`` is ``pid`` or one of its ancestors.

    A process that exited mid-walk (or whose chain cannot be read) fails the
    check: an unverifiable holder must not be attributed to the suite.
    """
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        parent = _parent_pid(current)
        if parent is None:
            return False
        current = parent
    return current == ancestor_pid


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


def scan_lock(
    target: Path,
    *,
    suite_pid: int | None = None,
) -> tuple[LockViolation, ...]:
    """Return every matching exclusive flock currently held on ``target``.

    ``suite_pid`` scopes the answer to holders this suite owns: a concurrent,
    unrelated Delegate launcher in another session holds the same file, but its
    parent chain never reaches the suite, so it is not a suite escape. Without
    ``suite_pid`` every holder is reported (the raw kernel view).
    """
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
        if suite_pid is None:
            attribution = "unverified"
        elif _ancestry_contains(owner_pid, suite_pid):
            attribution = "suite-descendant"
        else:
            continue
        command, cwd = _process_details(owner_pid)
        holders = _fd_holders(target)
        violations.append(LockViolation(owner_pid, command, cwd, holders, str(target), attribution))
    return tuple(violations)


def _watch(
    target: Path,
    report: Path,
    stop: Path,
    *,
    interval: float = 0.02,
    suite_pid: int | None = None,
) -> int:
    seen: set[tuple[int, tuple[int, ...]]] = set()
    while not stop.exists():
        for violation in scan_lock(target, suite_pid=suite_pid):
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
    parser.add_argument(
        "--suite-pid",
        type=int,
        default=None,
        help=(
            "Only report lock holders that descend from this pid. Omit to report "
            "every holder of the target (the raw kernel view)."
        ),
    )
    args = parser.parse_args()
    return _watch(args.target, args.report, args.stop, suite_pid=args.suite_pid)


if __name__ == "__main__":
    raise SystemExit(main())
