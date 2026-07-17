from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - Delegate inspects process identity with shell=False.
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TextIO

from delegate_agent import command_errors, run_registry, snapshot_view
from delegate_agent import rendering as delegate_rendering
from delegate_agent.json_types import JsonObject

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
    json_mode: bool = False


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


def _resolve_targets(
    registry_root: Path,
    handles: tuple[str, ...],
    latest_harness: str | None,
    group: str | None = None,
):
    targets: dict[str, run_registry.RunTarget] = {}
    for handle in handles:
        target = run_registry.resolve_run_target(
            registry_root,
            handle=handle,
            latest_harness=None,
        )
        if isinstance(target, run_registry.RunTargetLookupError):
            raise WaitCancelError(target.error, target.message)
        targets.setdefault(target.run_id, target)
    if latest_harness is not None:
        target = run_registry.resolve_run_target(
            registry_root,
            handle=None,
            latest_harness=latest_harness,
        )
        if isinstance(target, run_registry.RunTargetLookupError):
            raise WaitCancelError(target.error, target.message)
        targets.setdefault(target.run_id, target)
    if group is not None:
        for target in _group_targets(registry_root, group):
            targets.setdefault(target.run_id, target)
    if not targets:
        if group is not None:
            raise WaitCancelError("no_matching_runs", f"No runs found for group: {group}")
        raise WaitCancelError("missing_handle", "wait/cancel requires at least one run handle.")
    return list(targets.values())


def _merged_view(registry_root: Path, run_id: str, target: run_registry.RunTarget) -> JsonObject:
    snapshot = run_registry.load_run_snapshot_or_none(registry_root, run_id)
    view = snapshot_view.merge_snapshot_view(registry_root, run_id, snapshot, redact=True)
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
    return payload


def _status_label(payload: JsonObject) -> str:
    return str(payload.get("status") or payload.get("effectiveStatus") or "unknown")


def _print_wait_table(runs: list[JsonObject], stdout: TextIO) -> None:
    for run in runs:
        delegate_rendering.render_resolution_text(run, stdout)
        warnings = run.get("warnings")
        if isinstance(warnings, list):
            for warning in warnings:
                if isinstance(warning, str) and warning.startswith("bare_handle_stale:"):
                    print(f"warning: {warning}", file=stdout)
    print("alias        status     quality          failure", file=stdout)
    for run in runs:
        alias = str(run.get("alias") or run.get("runId") or "?")[:12]
        status = _status_label(run)[:10]
        quality = str(run.get("resultQuality") or "")[:16]
        failure = str(run.get("failureReason") or run.get("staleReason") or "")[:40]
        print(f"{alias:<12} {status:<10} {quality:<16} {failure}", file=stdout)


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
            "ok": not timed_out
            and all(_status_label(run) == run_registry.STATUS_SUCCEEDED for run in runs),
            "schema": WAIT_SCHEMA,
            "timedOut": timed_out,
            "runs": runs,
        }
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
    return 0 if all(_status_label(run) == run_registry.STATUS_SUCCEEDED for run in runs) else 1


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
            env={**os.environ, "LC_ALL": "C"},
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


def _cancel_signal_generation(
    state: JsonObject | None,
    target: run_registry.RunTarget,
) -> tuple[int | None, int | None, int, bool]:
    pid = _state_int(state, "pid")
    pgid = _state_int(state, "pgid")
    process_group = pgid is not None
    signal_value = pgid if process_group else pid
    if signal_value is None:
        raise WaitCancelError(
            "missing_pid", f"Run {target.alias or target.run_id} has no pid/pgid."
        )
    if signal_value <= 1:
        raise WaitCancelError(
            "unsafe_signal_target", f"Refusing to signal pid/pgid <= 1: {signal_value}"
        )
    return pid, pgid, signal_value, process_group


def _persist_cancelled_terminal_locked(
    registry_root: Path,
    target: run_registry.RunTarget,
    state: JsonObject | None,
    warnings: list[str],
) -> None:
    """Persist the canonical cancelled outcome while registry_lock is held."""
    run_path = run_registry.run_directory(registry_root, target.run_id)
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
        updated["failureReason"] = "cancelled_by_user"
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
                "failureReason": "cancelled_by_user",
                "exitCode": 1,
                "finishedAt": now,
                "lastActivityAt": now,
                "stdoutBytes": stdout_bytes,
                "stderrBytes": stderr_bytes,
            }
        )
    updated["exitCode"] = 1
    updated.pop("error", None)
    updated.pop("message", None)
    updated.pop("nextActions", None)
    if warnings:
        existing = updated.get("warnings") if isinstance(updated.get("warnings"), list) else []
        updated["warnings"] = [
            *existing,
            *(warning for warning in warnings if warning not in existing),
        ]
    run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, updated)

    snapshot = dict(run_registry.load_run_snapshot_or_none(registry_root, target.run_id) or {})
    snapshot.update(
        {
            "schema": run_registry.SNAPSHOT_SCHEMA,
            "ok": False,
            "runId": target.run_id,
            "alias": target.alias,
            "status": run_registry.STATUS_CANCELLED,
            "failureReason": "cancelled_by_user",
            "finishedAt": now,
            "stdoutBytes": stdout_bytes,
            "stderrBytes": stderr_bytes,
            "exitCode": 1,
        }
    )
    snapshot.pop("error", None)
    snapshot.pop("message", None)
    snapshot.pop("nextActions", None)
    if warnings:
        existing = snapshot.get("warnings") if isinstance(snapshot.get("warnings"), list) else []
        snapshot["warnings"] = [
            *existing,
            *(warning for warning in warnings if warning not in existing),
        ]
    run_registry.write_json_atomic(run_path / run_registry.SNAPSHOT_FILE, snapshot)


def _cancel_target(registry_root: Path, target: run_registry.RunTarget) -> JsonObject:
    # The runner publishes each launched generation under this same lock. Take
    # the initial selection under it too, so cancel waits for a primary Popen to
    # publish pid/pgid instead of racing the temporary no-state window.
    with run_registry.registry_lock(registry_root):
        state = run_registry.load_run_state_or_none(registry_root, target.run_id)
        fields = run_registry.status_fields(state)
        effective = fields.get("effectiveStatus")
        if effective in run_registry.TERMINAL_STATUSES or effective == run_registry.STATUS_STALE:
            raise WaitCancelError(
                "run_already_terminal",
                f"Run {target.alias or target.run_id} is already terminal ({effective}).",
            )
        generation = _cancel_signal_generation(state, target)
    warnings: list[str] = []
    cancel_marker_written = False
    for _attempt in range(CANCEL_GENERATION_MAX_ATTEMPTS):
        pid, pgid, signal_value, process_group = generation

        # PID-reuse start-identity guard: verify the tracked leader pid is not
        # older than the run. The locked reread below must still describe this
        # exact generation before the marker is written or any signal is sent.
        identity_pid = pid if pid is not None else signal_value
        generation_warnings = _check_pid_identity(registry_root, target, identity_pid)

        already_terminal = False
        generation_changed = False
        with run_registry.registry_lock(registry_root):
            pre_signal = run_registry.load_run_state_or_none(registry_root, target.run_id)
            pre_fields = run_registry.status_fields(pre_signal)
            pre_effective = pre_fields.get("effectiveStatus")
            if (
                pre_effective in run_registry.TERMINAL_STATUSES
                or pre_effective == run_registry.STATUS_STALE
            ):
                already_terminal = True
            elif _cancel_signal_generation(pre_signal, target) != generation:
                generation_changed = True
            else:
                stamped = dict(pre_signal or state or {})
                stamped["cancelRequested"] = True
                if not isinstance(stamped.get("cancelRequestedAt"), str):
                    stamped["cancelRequestedAt"] = run_registry.utc_now_iso()
                run_registry.write_json_atomic(
                    run_registry.run_directory(registry_root, target.run_id)
                    / run_registry.STATE_FILE,
                    stamped,
                )
                cancel_marker_written = True

        if already_terminal:
            return _terminal_payload(registry_root, target)
        if generation_changed:
            state = pre_signal
            generation = _cancel_signal_generation(state, target)
            continue
        state = pre_signal
        warnings.extend(generation_warnings)
        if pgid is None:
            warnings.append("pgid missing; fell back to pid signal for legacy run")
        try:
            _send_signal(signal_value, signal.SIGTERM, process_group=process_group)
        except ProcessLookupError:
            warnings.append("process exited before SIGTERM; checking for a replacement generation")
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
                warnings.append(
                    "SIGKILL was not permitted after SIGTERM; run state marked cancelled"
                )

        # A signalled attempt may immediately hand off to an auth fallback or
        # empty-result retry. Re-read under the lock before terminalizing; a new
        # live generation goes through the same identity/marker/signal protocol.
        follow_generation = False
        with run_registry.registry_lock(registry_root):
            latest = run_registry.load_run_state_or_none(registry_root, target.run_id)
            latest_fields = run_registry.status_fields(latest)
            latest_effective = latest_fields.get("effectiveStatus")
            if (
                latest_effective not in run_registry.TERMINAL_STATUSES
                and latest_effective != run_registry.STATUS_STALE
                and _cancel_signal_generation(latest, target) != generation
            ):
                state = latest
                follow_generation = True
            else:
                _persist_cancelled_terminal_locked(registry_root, target, latest or state, warnings)
        if follow_generation:
            generation = _cancel_signal_generation(state, target)
            continue

        payload = _terminal_payload(registry_root, target)
        if warnings:
            payload["warnings"] = warnings
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
    targets = _resolve_targets(registry_root, command.handles, None)
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
