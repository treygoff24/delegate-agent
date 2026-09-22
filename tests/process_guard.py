from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

# How long one complete process listing may take. A scan that outlives this is
# not an observation, so it is reported as a failure rather than as an empty
# answer.
_PS_TIMEOUT_SECONDS = 20.0

_PS_ARGV = ["ps", "-ww", "-axo", "pid=,pgid=,command="]


class OwnedProcessScanError(RuntimeError):
    """The owned-process scan could not be completed, so it proves nothing.

    A scan that cannot be completed is not the same answer as a scan that found
    nothing: ``ps`` failing, timing out, printing a row this cannot parse, and
    printing a listing that does not even name the caller all mean the suite has
    no observation of its own processes. Callers therefore never read them as
    "no owned process is running": the reaper fails loudly, and a containment
    proof built on the scan raises at its deletion boundary, which leaves every
    recorded temp retained and named instead of deleted under a producer that
    may still be running.
    """


def _ps_process_rows() -> list[tuple[int, int, str]]:
    """One complete ``ps`` listing as ``(pid, pgid, command)`` rows.

    ``-ww`` keeps ownership roots visible when CI exports a narrow COLUMNS. A
    listing that finished but is missing the caller itself is incomplete -- a
    full ``ps -ax`` always names the process that ran it -- so a truncated
    capture raises rather than passing for a quiet machine. Every row's ``pid``
    and ``pgid`` are parsed here, so a row carrying a nonnumeric field makes the
    whole listing unusable; it is never carried forward as text for a later
    caller to discard, which would read a producer the listing did name as
    absent.
    """
    try:
        result = subprocess.run(
            _PS_ARGV,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OwnedProcessScanError(f"ps could not be run: {exc!r}") from exc
    if result.returncode != 0:
        raise OwnedProcessScanError(
            f"ps exited {result.returncode}: {result.stderr.strip()[:200]!r}"
        )
    rows: list[tuple[int, int, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            raise OwnedProcessScanError(f"ps printed a row this cannot parse: {line!r}")
        pid_text, pgid_text, command = fields
        try:
            rows.append((int(pid_text), int(pgid_text), command))
        except ValueError as exc:
            raise OwnedProcessScanError(
                f"ps printed a row with a nonnumeric pid or pgid: {line!r}"
            ) from exc
    if not rows or not any(pid == os.getpid() for pid, _pgid, _command in rows):
        raise OwnedProcessScanError(
            "ps printed an incomplete listing: it does not name this process"
        )
    return rows


def _owned_delegate_processes(temp_roots: Sequence[Path]) -> list[tuple[int, int]]:
    """The owned Delegate processes under any of ``temp_roots``, or raise.

    A process is owned when its command line carries ``delegate.py`` and a path
    under one of the roots. Those roots are the ownership markers each caller
    proves its own producers carry -- the workspace a fixture launched through
    ``--cwd``, and the HOME a pinned workflow child's entrypoint lives under.
    A process outside that population is not evidence in either direction: this
    scan neither certifies it gone nor reaps it.
    """
    roots = {
        form
        for temp_root in temp_roots
        for form in (
            str(temp_root),
            str(Path(temp_root).resolve(strict=False)),
            os.path.realpath(temp_root),
        )
        if form
    }
    processes: list[tuple[int, int]] = []
    for pid, pgid, command in _ps_process_rows():
        if "delegate.py" not in command or not any(root in command for root in roots):
            continue
        processes.append((pid, pgid))
    return processes


def owned_delegate_processes(*temp_roots: Path) -> set[int]:
    """PIDs of the Delegate processes this suite owns under any of ``temp_roots``.

    The scan `reap_delegate_processes` uses, exposed for callers that need to
    ask again whether any owned producer is still running without reaping it.
    Raises `OwnedProcessScanError` when the scan cannot be completed, so a
    caller can never read a failed scan as "no owned process is left".
    """
    return {pid for pid, _pgid in _owned_delegate_processes(temp_roots)}


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


def reap_delegate_processes(*temp_roots: Path) -> set[int]:
    """Signal and reap the owned Delegate processes under any of ``temp_roots``.

    A scan that cannot be completed raises `OwnedProcessScanError` rather than
    returning an empty reap: "nothing was reaped" and "nothing could be looked
    for" must not be the same answer, because callers use this as the suite's
    producer boundary.
    """
    processes = _owned_delegate_processes(temp_roots)
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
