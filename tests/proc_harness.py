from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from delegate_agent.workflows import registry

_RECORDED_PGIDS: set[int] = set()
_PROCESS_GROUP_GRACE_SECONDS = 1.0
_PROCESS_GROUP_POLL_SECONDS = 0.05


def _record_pgid(pgid: int) -> None:
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1:
        _RECORDED_PGIDS.add(pgid)


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _wait_for_group_exit(pgid: int) -> bool:
    """Wait briefly for a process group to disappear after SIGTERM."""
    deadline = time.monotonic() + _PROCESS_GROUP_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        time.sleep(_PROCESS_GROUP_POLL_SECONDS)
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def _reap_recorded_group(pgid: int) -> None:
    """Terminate one process group, allowing cooperative shutdown first."""
    _signal_group(pgid, signal.SIGTERM)
    if not _wait_for_group_exit(pgid):
        _signal_group(pgid, signal.SIGKILL)


def reap_process_group(pgid: int) -> None:
    """Terminate every process in one group, tolerating an already-dead group."""
    _record_pgid(pgid)
    _reap_recorded_group(pgid)


def _process_snapshot() -> dict[int, tuple[int, int]]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,pgid="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    snapshot: dict[int, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            pid, ppid, pgid = (int(value) for value in fields[:3])
        except ValueError:
            continue
        snapshot[pid] = (ppid, pgid)
    return snapshot


def _descendant_pgids(supervisor_pid: int) -> set[int]:
    """Return all process groups in the supervisor's transitive process tree."""
    snapshot = _process_snapshot()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _pgid) in snapshot.items():
        children.setdefault(ppid, []).append(pid)
    pgids: set[int] = set()
    pending = [supervisor_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        record = snapshot.get(pid)
        if record is not None:
            pgids.add(record[1])
        pending.extend(children.get(pid, ()))
    return {pgid for pgid in pgids if pgid > 1}


def register_process_tree(supervisor_pid: int, supervisor_pgid: int) -> set[int]:
    """Record a supervisor group and all currently visible descendant groups.

    The PID/PGID identity check prevents a reused PID from authorizing a reap
    of an unrelated group.
    """
    if (
        not isinstance(supervisor_pid, int)
        or isinstance(supervisor_pid, bool)
        or supervisor_pid <= 1
        or not isinstance(supervisor_pgid, int)
        or isinstance(supervisor_pgid, bool)
        or supervisor_pgid <= 1
    ):
        raise ValueError("invalid supervisor process identity")
    try:
        live_pgid = os.getpgid(supervisor_pid)
    except (ProcessLookupError, PermissionError) as exc:
        raise RuntimeError("supervisor process is no longer available") from exc
    if live_pgid != supervisor_pgid:
        raise RuntimeError("supervisor process group identity changed")
    pgids = _descendant_pgids(supervisor_pid)
    pgids.add(supervisor_pgid)
    for pgid in pgids:
        _record_pgid(pgid)
    return pgids


def _reap_process_tree(supervisor_pid: int, supervisor_pgid: int) -> None:
    """Reap an identity-checked supervisor and every descendant group."""
    try:
        live_pgid = os.getpgid(supervisor_pid)
    except (ProcessLookupError, PermissionError):
        return
    if live_pgid != supervisor_pgid:
        return
    pgids = _descendant_pgids(supervisor_pid)
    pgids.add(supervisor_pgid)
    for pgid in pgids:
        _record_pgid(pgid)
    for pgid in sorted(pgids):
        _reap_recorded_group(pgid)


def reap_process_tree(supervisor_pid: int, supervisor_pgid: int) -> None:
    """Reap an identity-checked supervisor and its current descendants."""
    _reap_process_tree(supervisor_pid, supervisor_pgid)


def _workflow_identity(workspace: Path, wf_id: str) -> tuple[int, int] | None:
    try:
        root = registry.workflow_dir(workspace, wf_id)
    except (TypeError, ValueError):
        return None
    status = registry.read_json(root / registry.STATUS_FILE) or {}
    supervisor_pid = status.get("supervisorPid")
    pgid = status.get("supervisorPgid")
    if (
        not isinstance(supervisor_pid, int)
        or isinstance(supervisor_pid, bool)
        or supervisor_pid <= 1
        or not isinstance(pgid, int)
        or isinstance(pgid, bool)
        or pgid <= 1
    ):
        return None
    return supervisor_pid, pgid


def reap_workflow_now(workspace: Path, wf_id: str) -> None:
    """Reap a workflow supervisor group using its durable status record."""
    identity = _workflow_identity(workspace, wf_id)
    if identity is not None:
        _reap_process_tree(*identity)


@contextmanager
def workflow_reap(workspace: Path, wf_id: str) -> Iterator[None]:
    """Reap the workflow's detached supervisor group when the scope exits."""
    try:
        yield
    finally:
        reap_workflow_now(workspace, wf_id)


@contextmanager
def spawn_process(
    argv: Sequence[str],
    **kwargs: Any,
) -> Iterator[subprocess.Popen[Any]]:
    """Spawn a child in a fresh process group and reap that group on exit."""
    kwargs.pop("start_new_session", None)
    process = subprocess.Popen(argv, start_new_session=True, **kwargs)
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        # A process that exits before getpgid is still the leader of the
        # session we requested; its pid is therefore the group id to reap.
        pgid = process.pid
    _record_pgid(pgid)
    try:
        yield process
    finally:
        _reap_process_tree(process.pid, pgid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def _group_has_live_members(pgid: int) -> bool:
    """Return whether ``ps`` sees a non-zombie member in ``pgid``."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            member_pgid = int(fields[1])
        except ValueError:
            continue
        if member_pgid == pgid and not fields[2].startswith("Z"):
            return True
    return False


def assert_no_live_process_groups() -> None:
    """Fail if any process group recorded by this harness still has a member."""
    live = sorted(pgid for pgid in _RECORDED_PGIDS if _group_has_live_members(pgid))
    if live:
        raise AssertionError(f"test process groups still live: {live}")


def _assert_at_suite_end() -> None:
    try:
        assert_no_live_process_groups()
    except AssertionError as exc:
        os.write(2, f"{exc}\n".encode("utf-8", "replace"))
        os._exit(1)


atexit.register(_assert_at_suite_end)


__all__ = [
    "assert_no_live_process_groups",
    "reap_process_group",
    "reap_process_tree",
    "reap_workflow_now",
    "register_process_tree",
    "spawn_process",
    "workflow_reap",
]
