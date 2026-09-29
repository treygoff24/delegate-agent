from __future__ import annotations

import contextlib
import os
import signal
import subprocess  # nosec B404 - Delegate inspects process identity with shell=False.
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TextIO

from delegate_agent import (
    command_errors,
    degraded,
    outcome,
    profiles,
    redaction,
    run_registry,
    snapshot_view,
    terminal_states,
)
from delegate_agent import rendering as delegate_rendering
from delegate_agent.json_types import JsonObject

# staleReason values from run_status.status_fields that mean the runner process
# is gone, as opposed to the stall watchdog's idle-child verdict.
_RUNNER_LOST_STALE_REASONS = frozenset({"dead_pid", "missing_pid"})

WAIT_SCHEMA = "delegate.wait.v1"
CANCEL_SCHEMA = "delegate.cancel.v1"
WAIT_DEFAULT_TIMEOUT_SECONDS = 3600
# A process may legitimately start up to this many seconds before the run's
# manifest startedAt is stamped (subprocess launch + manifest write latency).
# Used by the PID-reuse identity check as the allowed skew window.
PID_IDENTITY_SKEW_SECONDS = 60.0
WAIT_DEFAULT_INTERVAL_SECONDS = 3
WAIT_MIN_INTERVAL_SECONDS = 1
CANCEL_GRACE_SECONDS = 5.0
# The raw status a worktree launcher writes before its child exists, including
# while its workspace setup command runs.
SETUP_WINDOW_STATUS = "creating_isolation"
# A tracked run can launch a primary attempt, an auth fallback, and an empty-
# result retry. Four selections let cancel follow all three generation changes
# while still failing closed if the run keeps churning unexpectedly.
CANCEL_GENERATION_MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class WaitCommand:
    handles: tuple[str, ...]
    latest_harness: str | None = None
    group: str | None = None
    timeout_seconds: int = WAIT_DEFAULT_TIMEOUT_SECONDS
    interval_seconds: int = WAIT_DEFAULT_INTERVAL_SECONDS
    completion_report: bool = False
    structural: bool = False
    json_mode: bool = False


# `wait --json --structural`: what a caller needs to decide its next step
# (identity, terminal status, and why a run failed), without the full view.
WAIT_STRUCTURAL_KEYS = (
    "runId",
    "alias",
    "harness",
    "group",
    "mode",
    "rawStatus",
    "effectiveStatus",
    "status",
    "terminalState",
    "resultQuality",
    "degraded",
    "degradedReason",
    "failureKind",
    "failureReason",
    "staleReason",
    "exitCode",
    "startedAt",
    "finishedAt",
    "completionReportContent",
)


@dataclass(frozen=True)
class CancelCommand:
    handles: tuple[str, ...]
    json_mode: bool = False


class WaitCancelError(command_errors.CommandError):
    pass


def _registry_for_workspace(workspace_path: str) -> Path:
    workspace = Path(workspace_path)
    return run_registry.registry_root_if_exists(workspace) or run_registry.registry_root(workspace)


def _group_targets(registry_root: Path, group: str) -> list[run_registry.RunTarget]:
    index = run_registry.load_index(registry_root)
    runs = index.get("runs", {})
    targets: list[run_registry.RunTarget] = []
    for run_id, entry in runs.items():
        if not isinstance(run_id, str) or not isinstance(entry, dict):
            continue
        if entry.get("group") != group:
            continue
        alias = entry.get("alias")
        targets.append(run_registry.RunTarget(run_id, alias if isinstance(alias, str) else None))

    def registration_ordinal(target: run_registry.RunTarget) -> int:
        entry = runs.get(target.run_id)
        if not isinstance(entry, dict):
            return 0
        ordinal = entry.get("registrationOrdinal", 0)
        return ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else 0

    targets.sort(key=registration_ordinal)
    return targets


def _lookup_error(target: run_registry.RunTargetLookupError) -> WaitCancelError:
    error = WaitCancelError(target.error, target.message)
    error.next_actions = list(target.next_actions) or None
    return error


def _resolve_targets(
    registry_root: Path,
    handles: tuple[str, ...],
    latest_harness: str | None,
    group: str | None = None,
    *,
    command: str = "wait",
):
    targets: dict[str, run_registry.RunTarget] = {}
    for handle in handles:
        # Both wait and cancel act on one Registry, so a run found in another
        # workspace is reported with the exact --cwd command, never followed.
        target = run_registry.resolve_run_target(
            registry_root,
            handle=handle,
            latest_harness=None,
            command=command,
        )
        if isinstance(target, run_registry.RunTargetLookupError):
            raise _lookup_error(target)
        targets.setdefault(target.run_id, target)
    if latest_harness is not None:
        target = run_registry.resolve_run_target(
            registry_root,
            handle=None,
            latest_harness=latest_harness,
            command=command,
        )
        if isinstance(target, run_registry.RunTargetLookupError):
            raise _lookup_error(target)
        targets.setdefault(target.run_id, target)
    if group is not None:
        for target in _group_targets(registry_root, group):
            targets.setdefault(target.run_id, target)
    if not targets:
        if group is not None:
            raise WaitCancelError(
                "no_matching_runs",
                f"No runs found for group: {group}. The run Registry is workspace-scoped "
                f"({registry_root}); use --cwd PATH to target another workspace's Registry.",
            )
        raise WaitCancelError("missing_handle", "wait/cancel requires at least one run handle.")
    return list(targets.values())


def _merged_view(registry_root: Path, run_id: str, target: run_registry.RunTarget) -> JsonObject:
    snapshot = run_registry.load_run_snapshot_or_none(registry_root, run_id)
    view = (
        redaction.redact_value(snapshot)
        if snapshot is not None
        else snapshot_view.merge_snapshot_view(registry_root, run_id, None, redact=True)
    )
    run_registry.add_run_target_resolution(view, target)
    return dict(view)


def _wait_state(registry_root: Path, run_id: str) -> JsonObject:
    state = run_registry.load_run_state_or_none(registry_root, run_id)
    fields = run_registry.status_fields(state)
    status = fields.get("effectiveStatus")
    result: JsonObject = {
        "rawStatus": fields.get("rawStatus"),
        "effectiveStatus": status,
        "terminal": status in run_registry.TERMINAL_STATUSES,
    }
    if fields.get("staleReason"):
        # A dead tracked child is terminal failure for wait, not an active stale state.
        result["effectiveStatus"] = run_registry.STATUS_FAILED
        result["terminal"] = True
        result["staleReason"] = fields["staleReason"]
        result["failureReason"] = fields["staleReason"]
    return result


def _terminal_payload(registry_root: Path, target: run_registry.RunTarget) -> JsonObject:
    payload = _merged_view(registry_root, target.run_id, target)
    wait_state = _wait_state(registry_root, target.run_id)
    payload["rawStatus"] = wait_state.get("rawStatus")
    payload["effectiveStatus"] = wait_state.get("effectiveStatus")
    payload["status"] = wait_state.get("effectiveStatus")
    if wait_state.get("staleReason"):
        payload["staleReason"] = wait_state["staleReason"]
        payload.setdefault("failureReason", wait_state.get("failureReason"))
        # A stale run's runner is gone (dead or missing pid); that is not the
        # stall watchdog's "stalled", which workflows retry as transient.
        payload["failureKind"] = (
            outcome.FAILURE_RUNNER_LOST
            if wait_state["staleReason"] in _RUNNER_LOST_STALE_REASONS
            else outcome.FAILURE_STALLED
        )
    return payload


def _status_label(payload: JsonObject) -> str:
    return str(payload.get("status") or payload.get("effectiveStatus") or "unknown")


def _run_succeeded(payload: JsonObject) -> bool:
    quality = payload.get("resultQuality")
    return run_registry.run_succeeded(
        _status_label(payload),
        quality if isinstance(quality, str) else None,
        payload.get("terminalState"),
        failure_kind=run_registry.record_failure_kind(payload),
    )


# Handle-resolution warnings: advisory, never a change to which run resolves,
# and the only per-run warnings the text table prints.
WAIT_RESOLUTION_WARNING_PREFIXES = (
    "bare_handle_stale:",
    "bare_handle_ambiguous:",
    "run_target_stale:",
)


def _print_wait_table(runs: list[JsonObject], stdout: TextIO) -> None:
    for run in runs:
        delegate_rendering.render_resolution_text(run, stdout)
        warnings = run.get("warnings")
        if isinstance(warnings, list):
            for warning in warnings:
                if isinstance(warning, str) and warning.startswith(
                    WAIT_RESOLUTION_WARNING_PREFIXES
                ):
                    print(f"warning: {warning}", file=stdout)
    print("alias        status     quality          failure", file=stdout)
    for run in runs:
        alias = str(run.get("alias") or run.get("runId") or "?")[:12]
        status = _status_label(run)[:10]
        quality = str(run.get("resultQuality") or "")[:16]
        failure = str(run.get("failureReason") or run.get("staleReason") or "")[:40]
        print(f"{alias:<12} {status:<10} {quality:<16} {failure}", file=stdout)
        flags = degraded.degraded_fields(run)
        if flags:
            print(
                f"  degraded: {flags.get('degradedReason', 'unknown')} "
                "(succeeded, but the child ended its turn with work unfinished)",
                file=stdout,
            )


def _group_workspace_warnings(command: WaitCommand, runs: list[JsonObject]) -> list[str]:
    if command.group is None:
        return []
    counts: dict[str, int] = {}
    for run in runs:
        mode = run.get("mode")
        isolated = run.get("isolatedWorkspace")
        execution_cwd = run.get("executionCwd")
        if (
            not isinstance(mode, str)
            or mode.strip().lower() != "work"
            or isolated is not False
            or not isinstance(execution_cwd, str)
            or not execution_cwd.strip()
        ):
            continue
        normalized_cwd = os.path.normcase(
            os.path.abspath(os.path.expanduser(execution_cwd.strip()))
        )
        counts[normalized_cwd] = counts.get(normalized_cwd, 0) + 1
    shared = sorted(path for path, count in counts.items() if count >= 2)
    if not shared:
        return []
    return [
        f"group {command.group} has work-mode runs that share the same non-isolated "
        "execution workspace; commit between feature waves or use persistent worktree "
        f"isolation and integrate separately: {', '.join(shared)}"
    ]


def _resolution_warnings(runs: list[JsonObject]) -> list[str]:
    """The handle-resolution warnings a structural view would otherwise drop.

    ``--structural`` keeps each run's identity and terminal fields only, so the
    run's own ``warnings`` list goes away; a compact caller still has to learn
    that its bare handle was stale or ambiguous, so these ride at the top level.
    """
    found: list[str] = []
    for run in runs:
        warnings = run.get("warnings")
        if not isinstance(warnings, list):
            continue
        for warning in warnings:
            if (
                isinstance(warning, str)
                and warning.startswith(WAIT_RESOLUTION_WARNING_PREFIXES)
                and warning not in found
            ):
                found.append(warning)
    return found


def _append_reports(
    runs: list[JsonObject],
    *,
    registry_root: Path,
    targets: list[run_registry.RunTarget],
    json_mode: bool,
    stdout: TextIO,
) -> None:
    for run, target in zip(runs, targets, strict=True):
        # Keep JSON/text behavior simple and local: read the report file the same
        # run-output command would prefer after synthesized failure reports.
        path = (
            run_registry.run_directory(registry_root, target.run_id)
            / run_registry.COMPLETION_REPORT_FILE
        )
        report = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if json_mode:
            run["completionReportContent"] = report
        else:
            print(f"\n=== {run.get('alias') or target.run_id} completionReport ===", file=stdout)
            print(report, end="" if report.endswith("\n") else "\n", file=stdout)


def emit_wait(command: WaitCommand, *, workspace_path: str, stdout: TextIO) -> int:
    registry_root = _registry_for_workspace(workspace_path)
    targets = _resolve_targets(
        registry_root,
        command.handles,
        command.latest_harness,
        command.group,
    )
    deadline = time.monotonic() + command.timeout_seconds
    last_statuses: dict[str, str] = {}
    timed_out = False

    while True:
        states = {target.run_id: _wait_state(registry_root, target.run_id) for target in targets}
        if not command.json_mode:
            for target in targets:
                status = str(states[target.run_id].get("effectiveStatus") or "unknown")
                if last_statuses.get(target.run_id) != status:
                    print(f"{target.alias or target.run_id}: {status}", file=stdout)
                    last_statuses[target.run_id] = status
        if all(state.get("terminal") for state in states.values()):
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(command.interval_seconds)

    runs = [_terminal_payload(registry_root, target) for target in targets]
    warnings = _group_workspace_warnings(command, runs)
    if command.completion_report and command.json_mode:
        _append_reports(
            runs,
            registry_root=registry_root,
            targets=targets,
            json_mode=command.json_mode,
            stdout=stdout,
        )
    if command.json_mode:
        payload: JsonObject = {
            "ok": not timed_out and all(_run_succeeded(run) for run in runs),
            "schema": WAIT_SCHEMA,
            "timedOut": timed_out,
            "runs": [{key: run[key] for key in WAIT_STRUCTURAL_KEYS if key in run} for run in runs]
            if command.structural
            else runs,
        }
        if command.structural:
            warnings = [*warnings, *(w for w in _resolution_warnings(runs) if w not in warnings)]
        if timed_out and not command.structural:
            pending = [
                target.alias or target.run_id
                for target in targets
                if not states[target.run_id].get("terminal")
            ]
            warnings = [
                *warnings,
                "wait timed out with full run snapshots in this payload. For a compact view "
                "rerun with: delegate --json wait --structural "
                + " ".join(pending or ["HANDLE"])
                + " ; for one run's full view: delegate snapshot HANDLE.",
            ]
        if warnings:
            payload["warnings"] = warnings
        delegate_rendering.print_json(payload, stdout)
    else:
        _print_wait_table(runs, stdout)
        for warning in warnings:
            print(f"warning: {warning}", file=stdout)
        if command.completion_report:
            _append_reports(
                runs,
                registry_root=registry_root,
                targets=targets,
                json_mode=command.json_mode,
                stdout=stdout,
            )
    # Exit-code precedence: any failed/cancelled run -> 1 (even if others timed
    # out); only timeouts (no terminal failure, but deadline hit) -> 124; all
    # succeeded -> 0. A non-terminal run that did not fail counts as a timeout
    # when the deadline was hit.
    failure_statuses = {run_registry.STATUS_FAILED, run_registry.STATUS_CANCELLED}
    any_failure = any(_status_label(run) in failure_statuses for run in runs)
    if any_failure:
        return 1
    if timed_out:
        return 124
    return 0 if all(_run_succeeded(run) for run in runs) else 1


def _signal_target_alive(value: int, *, process_group: bool) -> bool:
    try:
        if process_group:
            os.killpg(value, 0)
        else:
            os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _send_signal(value: int, sig: signal.Signals, *, process_group: bool) -> None:
    if value <= 1:
        raise WaitCancelError("unsafe_signal_target", f"Refusing to signal pid/pgid <= 1: {value}")
    if process_group and value == os.getpgrp():
        raise WaitCancelError(
            "unsafe_signal_target",
            f"Refusing to signal Delegate's own process group: {value}",
        )
    if process_group:
        os.killpg(value, sig)
    else:
        os.kill(value, sig)


def _process_start_datetime(pid: int) -> datetime | None:
    """Return the process start time for ``pid`` via ``ps -o lstart=``, or None
    if ps is unavailable or the output is unparseable (soft-degrade).

    Uses ``LC_ALL=C`` so the asctime format is locale-stable on macOS and Linux.
    """
    try:
        completed = subprocess.run(  # nosec B603 - fixed ps argv, shell=False.
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            env=profiles.child_environment(overrides={"LC_ALL": "C"}),
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = completed.stdout.strip()
    if not raw or completed.returncode != 0:
        return None
    # ps lstart prints an asctime-style string, e.g. "Thu Jul  4 12:00:00 2026".
    # email.utils.parsedate_to_datetime parses RFC-2822 dates but also handles
    # the asctime format (day-of-week abbreviated month day time year) in a
    # locale-stable way under LC_ALL=C. The output is in the system's local
    # timezone (ps has no timezone flag), so we interpret the naive result as
    # local time and convert to UTC for comparison against the run's manifest
    # startedAt (which is UTC).
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # ps lstart is local time; convert to UTC.
        parsed = parsed.astimezone(UTC)
    return parsed


def _check_pid_identity(
    registry_root: Path,
    target: run_registry.RunTarget,
    pid: int,
) -> list[str]:
    """Verify the tracked pid is not older than the run (PID-reuse guard).

    Returns a list of soft-degrade warnings (e.g. when ps is unavailable).
    Raises WaitCancelError with ``pid_identity_mismatch`` if the process
    predates the run's manifest startedAt beyond the allowed skew window,
    indicating the original child is gone and the pid was reused.
    """
    manifest = run_registry.load_run_manifest_or_none(registry_root, target.run_id)
    started_at_str = manifest.get("startedAt") if isinstance(manifest, dict) else None
    if not isinstance(started_at_str, str) or not started_at_str:
        # No manifest startedAt to compare against; soft-degrade.
        return ["pid identity check skipped: run manifest has no startedAt"]
    started_at = run_registry.parse_utc_timestamp(started_at_str)
    if started_at is None:
        return ["pid identity check skipped: run manifest startedAt unparseable"]
    proc_start = _process_start_datetime(pid)
    if proc_start is None:
        # ps failed or output unparseable: never hard-block cancel on ps quirks.
        return [
            "pid identity check skipped: ps lstart unavailable or unparseable; "
            "proceeding without start-identity verification"
        ]
    # The process may start up to PID_IDENTITY_SKEW_SECONDS before startedAt is
    # stamped (launch + manifest write latency). If it predates the run beyond
    # that skew, the original child is gone and the pid was reused.
    skew = timedelta(seconds=PID_IDENTITY_SKEW_SECONDS)
    if proc_start + skew < started_at:
        raise WaitCancelError(
            "pid_identity_mismatch",
            f"Run {target.alias or target.run_id}: the tracked pid {pid} started at "
            f"{proc_start.isoformat()}, which predates the run's startedAt "
            f"{started_at.isoformat()} beyond the {PID_IDENTITY_SKEW_SECONDS:.0f}s skew "
            "window. The original child process is gone and the pid was likely reused. "
            "Refusing to signal a process that is not the run's child.",
        )
    return []


def _state_int(state: JsonObject | None, key: str) -> int | None:
    value = state.get(key) if isinstance(state, dict) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _cancel_signal_target(
    state: JsonObject | None,
) -> tuple[int | None, int | None, int, bool] | None:
    """The process a record names for cancel to signal, or None when it names none.

    ``None`` covers two states that used to be one error: a launch that has not
    published its pid yet, and the window after a setup group ended and before
    the launcher finalized the record. Only the caller knows which of those it
    was looking at, so this helper never decides.
    """
    pid = _state_int(state, "pid")
    pgid = _state_int(state, "pgid")
    process_group = pgid is not None
    signal_value = pgid if process_group else pid
    if signal_value is None:
        # Setup window: a persistent worktree launcher runs the caller's setup
        # command in its own session before any child exists, and records that
        # group separately (`setupPgid`) because a published pid/pgid means
        # "the child launched" to the unlaunched-seal logic. Cancel still has
        # to be able to stop it, so it is the signal target while recorded.
        setup_pgid = _state_int(state, "setupPgid")
        if setup_pgid is None:
            return None
        pid, pgid, signal_value, process_group = None, setup_pgid, setup_pgid, True
    return pid, pgid, signal_value, process_group


def _signals_the_setup_group(
    state: JsonObject | None, generation: tuple[int | None, int | None, int, bool]
) -> bool:
    """Whether a selected generation's target is the record's setup group."""
    pid, pgid, _signal_value, _process_group = generation
    return pid is None and pgid is not None and _state_int(state, "setupPgid") == pgid


def _cancel_signal_generation(
    state: JsonObject | None,
    target: run_registry.RunTarget,
) -> tuple[int | None, int | None, int, bool]:
    generation = _cancel_signal_target(state)
    if generation is None:
        raise WaitCancelError(
            "missing_pid", f"Run {target.alias or target.run_id} has no pid/pgid."
        )
    if generation[2] <= 1:
        raise WaitCancelError(
            "unsafe_signal_target", f"Refusing to signal pid/pgid <= 1: {generation[2]}"
        )
    return generation


def _setup_window_status(state: JsonObject | None) -> bool:
    """Whether the record still carries the status a worktree setup runs under.

    A launcher writes it before the worktree exists and clears nothing until it
    finalizes, so this is true both while setup runs (with ``setupPgid``
    recorded) and in the window before anything has been started at all.
    """
    return isinstance(state, dict) and state.get("status") == SETUP_WINDOW_STATUS


def _setup_path_taken(state: JsonObject | None) -> bool:
    """Whether the run's signal target came from the workspace setup window.

    The record is the only evidence: a setup in flight carries `setupPgid` (or
    still reports ``creating_isolation`` once it has been cleared). A legacy
    record with a pid but no pgid falls back to a pid signal and never took
    this path, so it must not be described as one.
    """
    if _state_int(state, "setupPgid") is not None:
        return True
    return _setup_window_status(state)


# A run record with no pid is normally a launch that has not published its
# process yet, which is why cancel refuses it (missing_pid). A worktree
# launcher that is still running its setup command is the exception: it
# publishes `setupPgid` instead, which cancel signals without making the
# record look launched to the seal check. After this long
# with no activity it is a launch that never produced a process: its
# launcher died during isolation or before Popen. Workflow resume and
# adoption may seal it instead of refusing forever.
UNLAUNCHED_SEAL_GRACE_SECONDS = 300.0
UNLAUNCHED_RAW_STATUSES = frozenset({run_registry.STATUS_RUNNING, SETUP_WINDOW_STATUS})
UNLAUNCHED_SEAL_WARNING = (
    "unlaunched run sealed as cancelled: the record never published a pid/pgid "
    "within the grace window (missing_pid); nothing was signalled"
)


def unlaunched_run_age(state: JsonObject | None, run_path: Path | None = None) -> float | None:
    """Seconds since an unlaunched run last showed activity, or None if it launched."""
    if not isinstance(state, dict):
        return None
    if _state_int(state, "pid") is not None or _state_int(state, "pgid") is not None:
        return None
    if state.get("status") not in UNLAUNCHED_RAW_STATUSES:
        return None
    last = run_registry.parse_utc_timestamp(state.get("lastActivityAt"))
    if last is not None:
        return (datetime.now(UTC) - last).total_seconds()
    if run_path is not None:
        try:
            mtime = (run_path / run_registry.STATE_FILE).stat().st_mtime
        except OSError:
            return None
        return time.time() - mtime
    return None


def unlaunched_launcher_alive(
    registry_root: Path, run_id: str, state: JsonObject | None
) -> bool | None:
    """Whether the process that wrote an unlaunched record may still launch it.

    Returns None when the record does not name its launcher (``launcherPid``),
    False when that pid is dead or now belongs to a process that started after
    the run, and True otherwise. A live pid whose start time cannot be read is
    treated as the launcher: sealing a live launch is the failure to avoid.
    """
    if not isinstance(state, dict):
        return None
    launcher = _state_int(state, "launcherPid")
    if launcher is None:
        return None
    if run_registry.process_alive(launcher) is not True:
        return False
    manifest = run_registry.load_run_manifest_or_none(registry_root, run_id)
    started_raw = manifest.get("startedAt") if isinstance(manifest, dict) else None
    started_at = (
        run_registry.parse_utc_timestamp(started_raw) if isinstance(started_raw, str) else None
    )
    proc_start = _process_start_datetime(launcher)
    if started_at is None or proc_start is None:
        return True
    # The launcher existed before it stamped the run's startedAt; a process
    # that started after that (beyond ps's one-second resolution) reuses the pid.
    return proc_start <= started_at + timedelta(seconds=PID_IDENTITY_SKEW_SECONDS)


def unlaunched_run_sealable(
    registry_root: Path,
    run_id: str,
    state: JsonObject | None,
    *,
    grace_seconds: float = UNLAUNCHED_SEAL_GRACE_SECONDS,
) -> bool:
    """Whether an unlaunched record may be sealed: its launcher is not
    verifiably alive and it has been quiet for the whole grace window."""
    age = unlaunched_run_age(state, run_registry.run_directory(registry_root, run_id))
    if age is None or age < grace_seconds:
        return False
    return unlaunched_launcher_alive(registry_root, run_id, state) is not True


def seal_unlaunched_run(
    registry_root: Path,
    run_id: str,
    *,
    grace_seconds: float = UNLAUNCHED_SEAL_GRACE_SECONDS,
) -> bool:
    """Seal a run that never launched a process, once its launcher is gone.

    Re-checked under the registry lock, so a launch that publishes its pid in
    the meantime wins and nothing is sealed. The seal also stamps the cancel
    marker, which the runner re-reads under the same lock before every Popen
    and before publishing a pid, so a launcher that outlives this check can
    never start a child for the sealed record.
    """
    with run_registry.registry_lock(registry_root):
        run_registry.reconcile_finalize_wal_locked(registry_root, run_id)
        state = run_registry.load_run_state_or_none(registry_root, run_id)
        if not unlaunched_run_sealable(registry_root, run_id, state, grace_seconds=grace_seconds):
            return False
        alias = state.get("alias") if isinstance(state, dict) else None
        target = run_registry.RunTarget(run_id, alias if isinstance(alias, str) else None)
        stamped = dict(state) if isinstance(state, dict) else {}
        # Stamp the cancel marker too: a launcher that is somehow still alive
        # finalizes as cancelled instead of reviving the record.
        stamped["cancelRequested"] = True
        stamped.setdefault("cancelRequestedAt", run_registry.utc_now_iso())
        _persist_cancelled_terminal_locked(
            registry_root,
            target,
            stamped,
            [UNLAUNCHED_SEAL_WARNING],
            stale_reason="missing_pid",
        )
    return True


STALE_SEAL_WARNING = (
    "stale run sealed as cancelled: the tracked process was already dead (dead_pid); "
    "nothing was signalled"
)


def _persist_cancelled_terminal_locked(
    registry_root: Path,
    target: run_registry.RunTarget,
    state: JsonObject | None,
    warnings: list[str],
    *,
    stale_reason: str | None = None,
) -> None:
    """Persist the canonical cancelled outcome while registry_lock is held."""
    stdout_bytes, stderr_bytes = run_registry.effective_log_byte_sizes(
        registry_root, target.run_id, state
    )
    now = run_registry.utc_now_iso()
    updated: JsonObject = dict(state or {})
    runner_terminal = (
        isinstance(state, dict)
        and state.get("status") in run_registry.TERMINAL_STATUSES
        and state.get("status") != run_registry.STATUS_CANCELLED
    )
    if runner_terminal:
        # Preserve the runner's work summary/output metadata, but cancellation
        # wins status and exit-code precedence.
        updated["status"] = run_registry.STATUS_CANCELLED
        updated["finishedAt"] = now
        updated["lastActivityAt"] = now
        updated["stdoutBytes"] = stdout_bytes
        updated["stderrBytes"] = stderr_bytes
    else:
        updated.update(
            {
                "schema": run_registry.STATE_SCHEMA,
                "runId": target.run_id,
                "alias": target.alias,
                "status": run_registry.STATUS_CANCELLED,
                "finishedAt": now,
                "lastActivityAt": now,
                "stdoutBytes": stdout_bytes,
                "stderrBytes": stderr_bytes,
            }
        )
    terminal_states.apply_operator_cancel_override(updated)
    if warnings:
        existing = updated.get("warnings") if isinstance(updated.get("warnings"), list) else []
        updated["warnings"] = [
            *existing,
            *(warning for warning in warnings if warning not in existing),
        ]
    updated.update(
        {
            "schema": run_registry.STATE_SCHEMA,
            "ok": False,
            "runId": target.run_id,
            "alias": target.alias,
            "status": run_registry.STATUS_CANCELLED,
            "finishedAt": now,
            "stdoutBytes": stdout_bytes,
            "stderrBytes": stderr_bytes,
        }
    )
    if stale_reason is not None:
        updated["staleReason"] = stale_reason
    terminal_states.apply_operator_cancel_override(updated)
    run_registry.publish_terminal_record_locked(registry_root, target.run_id, updated)


SETUP_SIGNALLED_WARNING = (
    "run was still in workspace setup: the setup process group was signalled, "
    "and no child had launched"
)
SETUP_ENDED_BEFORE_SIGNAL_WARNING = (
    "the run's workspace setup ended before cancel could signal it: no child had "
    "launched, and nothing was signalled"
)
SETUP_NOT_STARTED_WARNING = (
    "the run was in workspace setup with no published process: "
    "nothing was signalled, and the run was marked cancelled"
)
SETUP_GROUP_CLEARED_WARNING = (
    "the run's workspace setup group was signalled and the record was cleared before "
    "cancel finalized it: no child had launched"
)
SETUP_LAUNCHER_GONE_WARNING = (
    "the launcher that owns this run's setup group is gone: the recorded setup group "
    "was not signalled, and the run was marked cancelled"
)


def _stamp_cancel_marker_locked(
    registry_root: Path,
    target: run_registry.RunTarget,
    state: JsonObject | None,
    fallback: JsonObject | None,
) -> None:
    """Write the cancel marker while registry_lock is held.

    Every launcher re-reads the marker under this same lock before each child
    ``Popen``, so a stamped marker is what guarantees no child launches for the
    record afterwards.
    """
    stamped = dict(state or fallback or {})
    stamped["cancelRequested"] = True
    if not isinstance(stamped.get("cancelRequestedAt"), str):
        stamped["cancelRequestedAt"] = run_registry.utc_now_iso()
    run_registry.write_run_state(run_registry.run_directory(registry_root, target.run_id), stamped)


URGENT_CANCEL_WARNING = (
    "the registry lock stayed held past cancel's wait, so the recorded child was "
    "signalled without it after its start identity was verified"
)
URGENT_CANCEL_UNRECORDED_WARNING = (
    "the cancelled outcome is not recorded yet because the registry lock is still "
    "held: no cancel marker was written, so a runner that is still alive could "
    "launch a retry attempt. Re-run cancel once the lock frees"
)
# The urgent path's single bounded attempt to record what it did.
URGENT_CANCEL_RECORD_WAIT_SECONDS = 2.0


def _urgent_recheck_before_signal(
    registry_root: Path,
    target: run_registry.RunTarget,
    generation: tuple[int | None, int | None, int, bool],
) -> None:
    """Narrow the unlocked window immediately before the urgent SIGTERM.

    The locked path reconciles the finalize WAL, re-reads the record, and
    requires the generation it selected. Without the lock this can only read:
    ``load_run_state_or_none`` overlays a pending finalize WAL onto the record
    read-only (it never replays, quarantines, or removes the WAL), so one fresh
    read sees both a finalizer that finished since selection and a record that
    moved. A terminal result means the run already finished; a different
    generation means a retry launched (or the record moved) since selection,
    and signalling either one would be a guess.
    """
    label = target.alias or target.run_id
    latest = run_registry.load_run_state_or_none(registry_root, target.run_id)
    effective = run_registry.status_fields(latest).get("effectiveStatus")
    if effective in run_registry.TERMINAL_STATUSES:
        raise WaitCancelError(
            "run_already_terminal",
            f"Run {label} is already terminal ({effective}). Nothing was signalled.",
        )
    if _cancel_signal_target(latest) != generation:
        raise WaitCancelError(
            "cancel_target_changed",
            f"Run {label} changed its child process while cancel was reading it without "
            "the registry lock (another process holds it). Nothing was signalled. Retry "
            "cancel.",
        )


def _urgent_cancel_without_lock(registry_root: Path, target: run_registry.RunTarget) -> JsonObject:
    """Stop a run's recorded child when the registry lock cannot be taken.

    Reads the record without the lock and signals only a launched child
    generation (pid/pgid) whose start identity matches the run. It never
    seals, marks, or signals anything the locked protocol would have to decide
    first: an unlaunched record, a setup group, a dead-pid seal, or a record
    already terminal. Afterwards it makes one bounded attempt to record the
    cancelled outcome under the lock, and says plainly when it could not.
    """
    label = target.alias or target.run_id
    state = run_registry.load_run_state_or_none(registry_root, target.run_id)
    fields = run_registry.status_fields(state)
    effective = fields.get("effectiveStatus")
    if effective in run_registry.TERMINAL_STATUSES:
        raise WaitCancelError(
            "run_already_terminal", f"Run {label} is already terminal ({effective})."
        )
    if _state_int(state, "pid") is None and _state_int(state, "pgid") is None:
        raise WaitCancelError(
            "registry_lock_busy",
            f"Run {label} names no launched child and the registry lock is held by another "
            "process, so cancel cannot mark it safely. Retry when the lock frees.",
        )
    if effective == run_registry.STATUS_STALE:
        raise WaitCancelError(
            "registry_lock_busy",
            f"Run {label} is stale and sealing it needs the registry lock, which another "
            "process holds. Retry when the lock frees.",
        )
    generation = _cancel_signal_generation(state, target)
    pid, _pgid, signal_value, process_group = generation
    identity_pid = pid if pid is not None else signal_value
    warnings = [URGENT_CANCEL_WARNING]
    warnings.extend(_check_pid_identity(registry_root, target, identity_pid))
    _urgent_recheck_before_signal(registry_root, target, generation)
    signal_refusal: JsonObject | None = None
    with contextlib.suppress(ProcessLookupError):
        _send_signal(signal_value, signal.SIGTERM, process_group=process_group)
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if _signal_target_alive(signal_value, process_group=process_group) is False:
            break
        time.sleep(0.05)
    if _signal_target_alive(signal_value, process_group=process_group) is not False:
        try:
            _send_signal(signal_value, signal.SIGKILL, process_group=process_group)
        except ProcessLookupError:
            pass
        except PermissionError:
            signal_refusal = {"signal": "SIGKILL", "reason": "permission_denied"}
            warnings.append("SIGKILL was not permitted after SIGTERM")
    recorded = False
    try:
        with run_registry.registry_lock(
            registry_root, timeout_seconds=URGENT_CANCEL_RECORD_WAIT_SECONDS
        ):
            run_registry.reconcile_finalize_wal_locked(registry_root, target.run_id)
            latest = run_registry.load_run_state_or_none(registry_root, target.run_id)
            latest_effective = run_registry.status_fields(latest).get("effectiveStatus")
            if latest_effective in run_registry.TERMINAL_STATUSES:
                recorded = True
            elif _cancel_signal_target(latest) == generation:
                _stamp_cancel_marker_locked(registry_root, target, latest, state)
                stamped = run_registry.load_run_state_or_none(registry_root, target.run_id)
                _persist_cancelled_terminal_locked(
                    registry_root, target, stamped or latest, warnings
                )
                recorded = True
    except TimeoutError:
        recorded = False
    if not recorded:
        warnings.append(URGENT_CANCEL_UNRECORDED_WARNING)
    payload = _terminal_payload(registry_root, target)
    payload["warnings"] = warnings
    payload["registryLockBypassed"] = True
    if signal_refusal is not None:
        payload["signalRefusal"] = signal_refusal
    return payload


class _RegistryLockBusy(Exception):
    """Cancel's initial registry-lock wait ran out before selection began."""


@contextlib.contextmanager
def _registry_lock_or_busy(registry_root: Path) -> Iterator[None]:
    """The registry lock, raising ``_RegistryLockBusy`` only if acquiring it times out.

    A ``TimeoutError`` raised by work done while the lock is held is not a busy
    lock and propagates unchanged.
    """
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(run_registry.registry_lock(registry_root))
        except TimeoutError as exc:
            raise _RegistryLockBusy(str(exc)) from exc
        yield


def _cancel_target(registry_root: Path, target: run_registry.RunTarget) -> JsonObject:
    # A lock that stays held past the bounded wait (a wedged holder is exactly
    # when an operator reaches for cancel) takes the urgent path instead of
    # failing with a raw lock timeout.
    try:
        return _cancel_target_under_lock(registry_root, target)
    except _RegistryLockBusy:
        return _urgent_cancel_without_lock(registry_root, target)


def _cancel_target_under_lock(registry_root: Path, target: run_registry.RunTarget) -> JsonObject:
    # The runner publishes each launched generation under this same lock. Take
    # the initial selection under it too, so cancel waits for a primary Popen to
    # publish pid/pgid instead of racing the temporary no-state window.
    with _registry_lock_or_busy(registry_root):
        run_registry.reconcile_finalize_wal_locked(registry_root, target.run_id)
        state = run_registry.load_run_state_or_none(registry_root, target.run_id)
        fields = run_registry.status_fields(state)
        effective = fields.get("effectiveStatus")
        if effective == run_registry.STATUS_STALE and fields.get("staleReason") == "dead_pid":
            # The tracked leader is dead, but a row left at rawStatus=running
            # never becomes terminal on its own: it lists as stale forever, and
            # a workflow that resumes over it fails the thunk that reaches it
            # with "already terminal (stale)" instead of relaunching. Seal it
            # as the operator outcome so cancel is idempotent over a dead run
            # the way it is over a live one. missing_pid is deliberately left
            # alone: under this lock it can still be a launch that has not
            # published its pid. A dead leader is not an empty process group:
            # when the recorded group still has members, nothing is sealed and
            # nothing is signalled, because the group id may already belong to
            # someone else.
            pgid = state.get("pgid") if isinstance(state, dict) else None
            if (
                isinstance(pgid, int)
                and not isinstance(pgid, bool)
                and pgid > 1
                and _signal_target_alive(pgid, process_group=True)
            ):
                raise WaitCancelError(
                    "run_group_alive",
                    f"Run {target.alias or target.run_id} has a dead tracked pid but process "
                    f"group {pgid} still has members; nothing was sealed or signalled. "
                    f"Inspect with: ps -o pid,ppid,etime,cmd -g {pgid}",
                )
            _persist_cancelled_terminal_locked(
                registry_root,
                target,
                state,
                [STALE_SEAL_WARNING],
                stale_reason="dead_pid",
            )
            return _terminal_payload(registry_root, target)
        if effective in run_registry.TERMINAL_STATUSES or effective == run_registry.STATUS_STALE:
            raise WaitCancelError(
                "run_already_terminal",
                f"Run {target.alias or target.run_id} is already terminal ({effective}).",
            )
        if _cancel_signal_target(state) is None and _setup_window_status(state):
            # Before setup publishes its group, only the cancel marker can
            # stop this run. Launchers check it before setup and child Popen.
            _stamp_cancel_marker_locked(registry_root, target, state, None)
            state = run_registry.load_run_state_or_none(registry_root, target.run_id)
            _persist_cancelled_terminal_locked(
                registry_root, target, state, [SETUP_NOT_STARTED_WARNING]
            )
            payload = _terminal_payload(registry_root, target)
            payload["warnings"] = [SETUP_NOT_STARTED_WARNING]
            return payload
        generation = _cancel_signal_generation(state, target)
    warnings: list[str] = []
    cancel_marker_written = False
    signal_refusal: JsonObject | None = None
    for _attempt in range(CANCEL_GENERATION_MAX_ATTEMPTS):
        pid, pgid, signal_value, process_group = generation
        # Whether this generation's target is the run's setup group. The
        # launcher clears that field the moment setup ends, so a re-read that
        # now names no target at all means the setup group is gone -- not that
        # cancel lost the run.
        setup_generation = _signals_the_setup_group(state, generation)

        # PID-reuse start-identity guard: verify the tracked leader pid is not
        # older than the run. The locked reread below must still describe this
        # exact generation before the marker is written or any signal is sent.
        identity_pid = pid if pid is not None else signal_value
        generation_warnings = _check_pid_identity(registry_root, target, identity_pid)

        already_terminal = False
        generation_changed = False
        setup_ended = False
        launcher_gone = False
        with run_registry.registry_lock(registry_root):
            run_registry.reconcile_finalize_wal_locked(registry_root, target.run_id)
            pre_signal = run_registry.load_run_state_or_none(registry_root, target.run_id)
            pre_fields = run_registry.status_fields(pre_signal)
            pre_effective = pre_fields.get("effectiveStatus")
            pre_target = _cancel_signal_target(pre_signal)
            if (
                pre_effective in run_registry.TERMINAL_STATUSES
                or pre_effective == run_registry.STATUS_STALE
            ):
                already_terminal = True
            elif pre_target is None and setup_generation:
                # Setup ended between cancel's selection and this re-read: the
                # launcher cleared `setupPgid` and has not finalized the record
                # yet. There is no process left to signal, so stamp the marker
                # -- the run must not launch a child now -- and settle it as
                # cancelled below rather than failing with missing_pid.
                _stamp_cancel_marker_locked(registry_root, target, pre_signal, state)
                cancel_marker_written = True
                setup_ended = True
            elif pre_target != generation:
                generation_changed = True
            elif setup_generation and (
                unlaunched_launcher_alive(registry_root, target.run_id, pre_signal) is not True
            ):
                # The process that recorded this setup group is gone (killed
                # mid-setup, so `setupPgid` was never cleared). The record is
                # not proof of anything about the group id now: pid reuse can
                # hand it to an unrelated process, so nothing is signalled.
                warnings.append(SETUP_LAUNCHER_GONE_WARNING)
                cleared = dict(pre_signal or {})
                cleared.pop("setupPgid", None)
                _persist_cancelled_terminal_locked(registry_root, target, cleared, warnings)
                launcher_gone = True
            else:
                _stamp_cancel_marker_locked(registry_root, target, pre_signal, state)
                cancel_marker_written = True

        if already_terminal:
            return _terminal_payload(registry_root, target)
        if launcher_gone:
            payload = _terminal_payload(registry_root, target)
            payload["warnings"] = warnings
            return payload
        if generation_changed:
            state = pre_signal
            generation = _cancel_signal_generation(state, target)
            continue
        state = pre_signal
        warnings.extend(generation_warnings)
        if _setup_path_taken(state):
            setup_warning = (
                SETUP_ENDED_BEFORE_SIGNAL_WARNING if setup_ended else SETUP_SIGNALLED_WARNING
            )
            if setup_warning not in warnings:
                warnings.append(setup_warning)
        if not setup_ended:
            if pgid is None:
                warnings.append("pgid missing; fell back to pid signal for legacy run")
            try:
                _send_signal(signal_value, signal.SIGTERM, process_group=process_group)
            except ProcessLookupError:
                warnings.append(
                    "process exited before SIGTERM; checking for a replacement generation"
                )
            deadline = time.monotonic() + CANCEL_GRACE_SECONDS
            while time.monotonic() < deadline:
                alive = _signal_target_alive(signal_value, process_group=process_group)
                if alive is False:
                    break
                time.sleep(0.05)
            alive = _signal_target_alive(signal_value, process_group=process_group)
            if alive is not False:
                try:
                    _send_signal(signal_value, signal.SIGKILL, process_group=process_group)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    signal_refusal = {
                        "signal": "SIGKILL",
                        "reason": "permission_denied",
                    }
                    warnings.append(
                        "SIGKILL was not permitted after SIGTERM; run state marked cancelled"
                    )

        # A signalled attempt may immediately hand off to an auth fallback or
        # empty-result retry. Re-read under the lock before terminalizing; a new
        # live generation goes through the same identity/marker/signal protocol.
        follow_generation = False
        with run_registry.registry_lock(registry_root):
            run_registry.reconcile_finalize_wal_locked(registry_root, target.run_id)
            latest = run_registry.load_run_state_or_none(registry_root, target.run_id)
            latest_fields = run_registry.status_fields(latest)
            latest_effective = latest_fields.get("effectiveStatus")
            latest_target = _cancel_signal_target(latest)
            still_running = (
                latest_effective not in run_registry.TERMINAL_STATUSES
                and latest_effective != run_registry.STATUS_STALE
            )
            if latest_target is None and still_running and not setup_generation:
                # Refuse to guess: the generation this loop signalled named a
                # process and this one names none, and that is not the setup
                # window's cleared field.
                raise WaitCancelError(
                    "missing_pid", f"Run {target.alias or target.run_id} has no pid/pgid."
                )
            if still_running and latest_target is not None and latest_target != generation:
                state = latest
                follow_generation = True
            else:
                if (
                    latest_target is None
                    and setup_generation
                    and not setup_ended
                    and SETUP_GROUP_CLEARED_WARNING not in warnings
                ):
                    # The setup group was signalled and the launcher cleared
                    # `setupPgid` before cancel's reply: the marker already
                    # guarantees no child launches.
                    warnings.append(SETUP_GROUP_CLEARED_WARNING)
                _persist_cancelled_terminal_locked(registry_root, target, latest or state, warnings)
        if follow_generation:
            generation = _cancel_signal_generation(state, target)
            continue

        payload = _terminal_payload(registry_root, target)
        if warnings:
            payload["warnings"] = warnings
        if signal_refusal is not None:
            payload["signalRefusal"] = signal_refusal
        return payload

    marker_note = (
        "The cancel marker remains set and no successful terminal outcome was recorded."
        if cancel_marker_written
        else "No process was signaled and no cancel marker was written."
    )
    raise WaitCancelError(
        "cancel_target_changed",
        f"Run {target.alias or target.run_id} kept changing pid/pgid across "
        f"{CANCEL_GENERATION_MAX_ATTEMPTS} cancellation selections. {marker_note}",
    )


def emit_cancel(command: CancelCommand, *, workspace_path: str, stdout: TextIO) -> int:
    registry_root = _registry_for_workspace(workspace_path)
    targets = _resolve_targets(registry_root, command.handles, None, command="cancel")
    runs = [_cancel_target(registry_root, target) for target in targets]
    if command.json_mode:
        delegate_rendering.print_json(
            {"ok": True, "schema": CANCEL_SCHEMA, "runs": runs},
            stdout,
        )
        return 0
    for run in runs:
        label = run.get("alias") or run.get("runId")
        status = _status_label(run)
        if status == run_registry.STATUS_CANCELLED:
            print(f"cancelled: {label}", file=stdout)
        else:
            print(f"not cancelled: {label} is already {status}", file=stdout)
        for warning in run.get("warnings") or []:
            print(f"warning: {warning}", file=stdout)
    return 0
