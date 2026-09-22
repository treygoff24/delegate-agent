from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
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


def group_members_matching(pgid: int, markers: Sequence[str]) -> bool:
    """Whether a live member of ``pgid`` carries one of these ownership markers.

    The markers are paths the owning record itself names (a run's execution
    cwd, its registry cwd, the scratch and temp directories it allocated), so a
    reused group id cannot authorize a signal to an unrelated group. When
    ``ps`` is unavailable or nothing matches, this reports no ownership rather
    than guessing.
    """
    wanted = tuple(marker for marker in markers if marker)
    if not wanted or pgid <= 1:
        return False
    # -ww keeps ownership markers visible when CI exports a narrow COLUMNS.
    result = subprocess.run(
        ["ps", "-ww", "-axo", "pgid=,args="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) < 2:
            continue
        try:
            member_pgid = int(fields[0])
        except ValueError:
            continue
        if member_pgid == pgid and any(marker in fields[1] for marker in wanted):
            return True
    return False


def await_process_group(pgid: int, *, timeout: float) -> bool:
    """Wait for a group to exit on its own; ``False`` when it outstays ``timeout``.

    Nothing is signalled: a child that is still writing (inside the TMPDIR it
    owns, say) gets to reach its own exit before anything is reaped.
    """
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        if not _group_has_live_members(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PROCESS_GROUP_POLL_SECONDS)


def reap_recorded_group_matching(pgid: int, marker: str) -> None:
    """Reap a recorded group only when a member's command line carries ``marker``.

    Used for launch-time captured groups whose identity-checked supervisor is
    already gone: the marker (a unique per-test workspace path) keeps a reused
    group id from authorizing a signal to an unrelated group. When ``ps`` is
    unavailable or no member matches, nothing is signalled — the suite-end
    live-group assertion surfaces a leak loudly instead of a silent wrong kill.
    """
    _record_pgid(pgid)
    if group_members_matching(pgid, (marker,)):
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


def kill_process_tree_uncleanly(supervisor_pid: int, supervisor_pgid: int) -> set[int]:
    """SIGKILL an identity-checked supervisor and every descendant group at once.

    Nothing in the tree gets to run a handler or finalize a record. This is
    the shape of an OOM kill or a hard host reboot, and it leaves any tracked
    child row at rawStatus=running with a dead pid, which is the orphan a
    resume must seal. The groups are recorded so cleanup still reaps them.
    """
    pgids = register_process_tree(supervisor_pid, supervisor_pgid)
    for pgid in sorted(pgids):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
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


class ProducerExitProof:
    """A producer process's own liveness check, in `Popen.poll` shape.

    Test teardown deletes a run's compact child temp only once the process that
    can still launch another attempt into it is proven gone. A test that owns
    the producer as a `Popen` -- or that has the `CompletedProcess`
    `subprocess.run` returned -- passes that handle instead; this is the proof
    for a producer without one: a reaped workflow supervisor, or the suite's
    owned-process scan. ``poll`` mirrors `Popen.poll`: ``None`` while the
    producer is still there, an exit status once it is gone. The check runs
    again at the deletion boundary, so containment never decides on an
    observation that has since gone stale.
    """

    def __init__(self, alive: Callable[[], bool]) -> None:
        self._alive = alive

    def poll(self) -> int | None:
        return None if self._alive() else 0


def producers_exited(producers: Sequence[object]) -> bool:
    """Whether every producer proof shows its process gone.

    A reaped `Popen`, the `CompletedProcess` `subprocess.run` returns, and a
    `ProducerExitProof` all answer `poll` for the producer they own. An empty
    set, a live handle, and a handle this cannot read prove nothing: a run's
    compact temp is shared by every attempt of that run, so containment retains
    and reports the temp of a run whose producer may still retry rather than
    deleting a directory a live child could be writing into.

    A proof may also *raise* instead of answering -- the owned-process scan does
    exactly that when it cannot be completed -- and that is not turned into an
    answer here: it travels out of containment, which deletes nothing and names
    the temp it retained. A failed observation is never the same as a quiet one.
    """
    proofs = list(producers)
    if not proofs:
        return False
    for proof in proofs:
        poll = getattr(proof, "poll", None)
        if callable(poll):
            if poll() is None:
                return False
            continue
        returncode = getattr(proof, "returncode", None)
        if isinstance(returncode, int) and not isinstance(returncode, bool):
            continue
        return False
    return True


def owned_producer_proof(*roots: Path) -> ProducerExitProof:
    """Prove, without signalling, that no owned Delegate process is left.

    The scan-only form of `reaped_owned_producers`, for a caller that must ask
    the question while the producers are still running -- a deletion boundary
    asks it again and again, and a fixture that reaps owns its own reapers.

    A root is an ownership marker, and the proof only ever speaks for the
    producers whose command lines carry one: a process launched without any of
    these roots in its command line is outside this proof's population, so it is
    neither certified gone nor signalled by it. Roots are therefore chosen from
    the invocation each caller actually owns -- a fixture that launches
    ``bin/delegate.py --cwd <workspace>`` passes its workspace, and a detached
    workflow supervisor passes the HOME its pinned child entrypoint lives under.
    """
    from tests.process_guard import owned_delegate_processes

    return ProducerExitProof(lambda: bool(owned_delegate_processes(*roots)))


def reaped_owned_producers(*roots: Path) -> ProducerExitProof:
    """Reap the Delegate processes this suite owns under ``roots``, and prove none is left.

    The producer of a tracked run is the Delegate process that launched it, and
    `tests.process_guard` is the suite's own lifecycle for exactly those
    processes: `reap_delegate_processes` signals every owned process under these
    roots and raises unless all of them are gone. The returned proof asks that
    same scan again, so containment re-checks at its deletion boundary instead
    of trusting this call's snapshot; whatever the scan still finds it reaps.

    A scan that cannot be completed raises `tests.process_guard.
    OwnedProcessScanError` from here and from every later `poll`, so no caller
    can hold this proof as evidence of a producer's exit that was never
    observed.
    """
    from tests.process_guard import reap_delegate_processes

    reap_delegate_processes(*roots)
    return owned_producer_proof(*roots)


def workflow_producer_proofs(workspace: Path) -> list[ProducerExitProof]:
    """Proofs that the workflow supervisors this workspace records cannot retry.

    A workflow supervisor is the detached process that launches each agent child
    as a real Delegate subprocess. Its own lock proves one thing: while it is
    alive it can launch another child, and a supervisor whose probe is
    inconclusive counts as alive. It does *not* prove that the children it
    already launched are gone -- a child is itself a Delegate process (a
    ``run`` invocation) that can launch another attempt into its own run's temp
    until that process exits -- so a caller pairs these proofs with an
    owned-process proof whose roots cover the child's command line (see
    `owned_producer_proof`).

    This proves and never signals: a fixture reaps the workflows it launched
    with the reapers that already own them, and killing some other process a
    record happens to name, merely to make a deletion legal, is exactly the
    guess this boundary exists to remove.
    """
    root = registry.workflow_root(workspace)
    if not root.is_dir():
        return []
    proofs: list[ProducerExitProof] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        try:
            registry.validate_workflow_id(path.name)
        except ValueError:
            continue
        proofs.append(ProducerExitProof(lambda root=path: registry.supervisor_alive(root)))
    return proofs


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
        if process.poll() is not None and _group_has_live_members(pgid):
            # The leader is gone, so identity-gated tree discovery reaped
            # nothing — but the launch-time group still has members, which can
            # only be our descendants unless the entire group died and the id
            # was reused within this context's lifetime (the same residual
            # window the pre-identity design carried). Reap the recorded group
            # rather than leaking them.
            _reap_recorded_group(pgid)
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
    "ProducerExitProof",
    "assert_no_live_process_groups",
    "await_process_group",
    "group_members_matching",
    "owned_producer_proof",
    "producers_exited",
    "reap_process_group",
    "reap_process_tree",
    "reap_recorded_group_matching",
    "reap_workflow_now",
    "reaped_owned_producers",
    "register_process_tree",
    "spawn_process",
    "workflow_producer_proofs",
    "workflow_reap",
]
