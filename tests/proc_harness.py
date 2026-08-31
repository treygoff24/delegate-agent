from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from delegate_agent.workflows import registry

_RECORDED_PGIDS: set[int] = set()


def _record_pgid(pgid: int) -> None:
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1:
        _RECORDED_PGIDS.add(pgid)


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def reap_process_group(pgid: int) -> None:
    """Terminate every process in a recorded group, tolerating ESRCH."""
    _record_pgid(pgid)
    _signal_group(pgid, signal.SIGTERM)
    _signal_group(pgid, signal.SIGKILL)


def _workflow_pgid(workspace: Path, wf_id: str) -> int | None:
    try:
        root = registry.workflow_dir(workspace, wf_id)
    except (TypeError, ValueError):
        return None
    status = registry.read_json(root / registry.STATUS_FILE) or {}
    pgid = status.get("supervisorPgid")
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return None
    return pgid


def reap_workflow_now(workspace: Path, wf_id: str) -> None:
    """Reap a workflow supervisor group using its durable status record."""
    pgid = _workflow_pgid(workspace, wf_id)
    if pgid is not None:
        reap_process_group(pgid)


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
        reap_process_group(pgid)
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
    "reap_workflow_now",
    "spawn_process",
    "workflow_reap",
]
