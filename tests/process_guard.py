from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path


def _owned_delegate_processes(temp_root: Path) -> list[tuple[int, int]]:
    # -ww keeps ownership roots visible when CI exports a narrow COLUMNS.
    result = subprocess.run(
        ["ps", "-ww", "-axo", "pid=,pgid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    roots = {str(temp_root), str(temp_root.resolve(strict=False)), os.path.realpath(temp_root)}
    processes: list[tuple[int, int]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid_text, pgid_text, command = parts
        if "delegate.py" not in command or not any(root in command for root in roots):
            continue
        with contextlib.suppress(ValueError):
            processes.append((int(pid_text), int(pgid_text)))
    return processes


def _signal_processes(processes: list[tuple[int, int]], sig: signal.Signals) -> None:
    own_pgid = os.getpgrp()
    signalled_groups: set[int] = set()
    for pid, pgid in processes:
        try:
            if pgid > 1 and pgid != own_pgid:
                if pgid not in signalled_groups:
                    os.killpg(pgid, sig)
                    signalled_groups.add(pgid)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _running_pids(pids: set[int]) -> set[int]:
    running: set[int] = set()
    for pid in pids:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                continue
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        running.add(pid)
    return running


def reap_delegate_processes(temp_root: Path) -> set[int]:
    processes = _owned_delegate_processes(temp_root)
    pids = {pid for pid, _pgid in processes}
    if not pids:
        return set()
    _signal_processes(processes, signal.SIGTERM)
    deadline = time.monotonic() + 1
    running = _running_pids(pids)
    while running and time.monotonic() < deadline:
        time.sleep(0.02)
        running = _running_pids(running)
    if running:
        _signal_processes(
            [(pid, pgid) for pid, pgid in processes if pid in running], signal.SIGKILL
        )
        deadline = time.monotonic() + 1
        while running and time.monotonic() < deadline:
            time.sleep(0.02)
            running = _running_pids(running)
    if running:
        raise RuntimeError(f"failed to reap test Delegate processes: {sorted(running)}")
    return pids
