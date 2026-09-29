from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent import (
    private_io,
    registry_roster,
    rendering,
    run_registry,
    workflow_attempts,
    workflow_pinning,
    workspace_spec,
)
from delegate_agent.errors import EXIT_OK, DelegateError
from delegate_agent.isolation import worktrees_data_home
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.workflows import WORKFLOW_KEY_VERSION, registry, runtime, status_reasons
from delegate_agent.workflows import script as workflow_script

workflow_pinning.require_pinned_persona_resolver()

WORKFLOW_COMMAND_SCHEMA = "delegate.workflow-command.v1"
TERMINAL_WORKFLOW_STATUSES = {"succeeded", "failed", "killed"}
WAIT_DONE_WORKFLOW_STATUSES = TERMINAL_WORKFLOW_STATUSES | {"dry_run", "paused", "stalled"}
SIGNAL_DRAIN_INCOMPLETE_WARNING = (
    "signal audit incomplete: the supervisor's signal relay did not settle before the "
    "terminal status was published; signal fields may be missing"
)
LIVE_WORKFLOW_STATUSES = {"created", "running", "starting"}
DRY_RUN_WRITE_WARNING = (
    "dry-run only stubs agent calls; script filesystem writes are live. "
    "State-writing scripts must branch on dry_run/is_dry_run or run from a disposable checkout."
)

SYSTEMD_CGROUP_PATH = Path("/proc/self/cgroup")
# A unit name is a single path component: `[A-Za-z0-9_.@-]+` with a `.service`
# suffix. Unit names may not contain `/`, so anchoring the name at the leaf is
# exact rather than a heuristic.
_SYSTEMD_UNIT_NAME_RE = re.compile(r"[A-Za-z0-9_.@-]+\.service")
# A transient or user unit injected under the per-user manager slice
# (`/user.slice/user-1000.slice/user@1000.service/...`) is known only to that
# user's manager; `systemctl show` without `--user` cannot see it.
_SYSTEMD_USER_MANAGER_PREFIX = "/user.slice/"
# KillMode values that leave a detached supervisor alive when the unit's main
# process exits. `control-group` (the systemd default) and `mixed` do not.
SYSTEMD_SAFE_KILL_MODES = frozenset({"process", "none"})


@dataclass(frozen=True)
class SystemdUnit:
    """The unit that owns the current process, and the manager that knows it."""

    name: str
    user_manager: bool


def _systemd_unit_from_cgroup(text: str) -> SystemdUnit | None:
    """The systemd service unit this process runs under, if any.

    Only the leaf path component of a cgroup line names the unit that owns the
    current process. Searching the whole path returns an ancestor: an ordinary
    interactive launch sits at
    `/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.example.scope`,
    where `user@1000.service` is the per-user manager, not this process's unit.
    That ancestor stays alive while the session does, so warning about its
    KillMode would be false and would fire on every interactive run.
    """
    for line in text.splitlines():
        path = line.rsplit(":", 1)[-1].strip()
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        if _SYSTEMD_UNIT_NAME_RE.fullmatch(leaf):
            return SystemdUnit(leaf, path.startswith(_SYSTEMD_USER_MANAGER_PREFIX))
    return None


def _systemd_kill_mode(unit: str, *, user_manager: bool = False) -> str | None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return None
    argv = [systemctl, "--user"] if user_manager else [systemctl]
    argv.extend(["show", "-p", "KillMode", "--value", unit])
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell.
            argv,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    mode = result.stdout.strip().lower()
    return mode or None


def _systemd_detach_warning() -> str | None:
    """Warn when the invoking systemd unit will reap the detached supervisor.

    `workflow run` forks, setsids, and execs the supervisor, then the invoking
    process exits. Under `systemd-run`'s default `KillMode=control-group`, that
    exit reaps the whole cgroup and kills the supervisor about half a second
    later -- while the unit still reports `Result=success`,
    `ExecMainStatus=0` and ActiveState=inactive, so nothing on the systemd side
    admits the loss. Only `KillMode=process` (or `none`) keeps it alive.
    """
    try:
        cgroup = SYSTEMD_CGROUP_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    unit = _systemd_unit_from_cgroup(cgroup)
    if unit is None:
        return None
    kill_mode = _systemd_kill_mode(unit.name, user_manager=unit.user_manager)
    if kill_mode is not None and kill_mode in SYSTEMD_SAFE_KILL_MODES:
        return None
    detail = (
        f"its KillMode is {kill_mode!r}"
        if kill_mode is not None
        else "its KillMode could not be read, and the default is control-group"
    )
    return (
        f"this launch runs inside systemd unit {unit.name}, and {detail}: the unit's main "
        "process is this command, which exits as soon as the supervisor is detached. "
        "systemd will then stop the supervisor with the rest of the cgroup while the "
        "unit reports success -- re-run under `-p KillMode=process` (or set it in the "
        "unit) so the workflow outlives the launcher."
    )


def _delegate_cli_argv() -> list[str]:
    return [sys.executable, str(Path(sys.argv[0]).resolve())]


def _require_current_workflow(status: JsonObject) -> None:
    if status.get("workflowKeyVersion") != WORKFLOW_KEY_VERSION:
        raise DelegateError(
            "unsupported_workflow_version",
            "This workflow uses an unsupported saved format; start a new workflow.",
        )


@dataclass(frozen=True)
class WorkflowCommand:
    action: str
    script: str | None = None
    wf_id: str | None = None
    key_or_label: str | None = None
    reason: str | None = None
    args_json: str | None = None
    budget: int | None = None
    dry_run: bool = False
    resume: str | None = None
    name: str | None = None
    since: int = 0
    timeout: int | None = None
    result_field: str | None = None
    json_mode: bool = False
    jsonl: bool = False
    notify: str | None = None
    gate: str | None = None
    gate_action: str | None = None
    gate_note: str | None = None
    gate_data_json: str | None = None
    # workflow run --env/--env-file: recorded once at launch in the workflow
    # directory and applied to every child on every attempt, resume included.
    env: tuple[tuple[str, str], ...] = ()
    env_files: tuple[str, ...] = ()
    # workflow resume/approve --repin: move the workflow's pin onto the runtime
    # executing this command. Never implied; a resume keeps the pin by default.
    repin: bool = False


@dataclass(frozen=True)
class GateChoice:
    """An operator's ``approve --gate/--action/--note/--data`` selection."""

    gate: str | None = None
    action: str | None = None
    note: str | None = None
    data: JsonValue = None
    has_data: bool = False


def emit(
    command: WorkflowCommand,
    *,
    workspace_path: str,
    config: JsonObject,
    stdout: TextIO,
    stderr: TextIO,
    config_source: str = "command-config",
) -> int:
    workspace = Path(workspace_path)
    action = command.action
    if action == "check":
        return emit_check(command, stdout=stdout)
    if action == "run":
        return emit_run(
            command,
            workspace=workspace,
            config=config,
            stdout=stdout,
            stderr=stderr,
            config_source=config_source,
        )
    if action == "_supervise":
        if command.wf_id is None:
            raise DelegateError("missing_workflow", "workflow _supervise requires <wfId>.")
        try:
            root = registry.workflow_dir(workspace, command.wf_id)
            _require_current_workflow(registry.read_json(root / registry.STATUS_FILE) or {})
            pin = workflow_pinning.load_pin(command.wf_id)
            if pin is None:
                raise workflow_pinning.WorkflowPinError(
                    "invalid_pin", "workflow pin is missing; start a new workflow"
                )
            attempt = workflow_attempts.from_environment(pin=pin)
            if attempt is None:
                raise workflow_pinning.WorkflowPinError(
                    "invalid_workflow_attempt",
                    "this pinned supervisor requires an attempt snapshot",
                )
            previous_environment = workflow_pinning.temporarily_apply_environment(
                pin, attempt=attempt
            )
            return runtime.run_supervisor(
                workspace=workspace,
                wf_id=command.wf_id,
                cli_argv=pin.cli_argv,
                config=attempt.config,
                attempt_config=attempt.metadata,
                attempt_environment={**pin.environment, **attempt.environment},
                # Only the process detach_supervisor launched may end itself
                # to preserve ownership; an embedded caller of this entry
                # gets the in-process (unbounded fence wait) semantics.
                dedicated_process=os.environ.get(runtime.WORKFLOW_SUPERVISOR_PROCESS_ENV) == "1",
            )
        except (workflow_pinning.WorkflowPinError, delegate_config.ConfigError) as exc:
            raise DelegateError(exc.error, exc.message) from exc
        except BlockingIOError as exc:
            raise DelegateError(
                "workflow_locked",
                f"Workflow is already running: {command.wf_id}",
            ) from exc
        finally:
            if "previous_environment" in locals() and previous_environment:
                workflow_pinning.restore_environment(previous_environment)
    if action == "status":
        return emit_status(command, workspace=workspace, stdout=stdout)
    if action == "events":
        return emit_events(command, workspace=workspace, stdout=stdout)
    if action == "watch":
        return emit_watch(command, workspace=workspace, stdout=stdout)
    if action == "result":
        return emit_result(command, workspace=workspace, stdout=stdout)
    if action == "wait":
        return emit_wait(command, workspace=workspace, stdout=stdout)
    if action == "approve":
        return emit_approve(
            command, workspace=workspace, config=config, stdout=stdout, config_source=config_source
        )
    if action == "reject":
        return emit_reject(command, workspace=workspace, config=config, stdout=stdout)
    if action == "kill":
        return emit_kill(command, workspace=workspace, stdout=stdout)
    if action == "list":
        return emit_list(command, workspace=workspace, stdout=stdout)
    if action == "save":
        return emit_save(command, stdout=stdout)
    raise DelegateError("unknown_workflow_action", f"Unknown workflow action: {action}")


def emit_check(command: WorkflowCommand, *, stdout: TextIO) -> int:
    path = _script_path_for_command(command)
    result = check_script(path)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "scriptPath": str(path),
        "meta": result.meta,
        "warnings": list(result.warnings),
    }
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(f"ok: {path}", file=stdout)
        for warning in result.warnings:
            print(f"warning: {warning}", file=stdout)
    return EXIT_OK


def emit_run(
    command: WorkflowCommand,
    *,
    workspace: Path,
    config: JsonObject,
    stdout: TextIO,
    stderr: TextIO,
    approve_gate: bool = False,
    config_source: str = "command-config",
    gate_choice: GateChoice | None = None,
) -> int:
    if command.repin and not command.resume:
        raise DelegateError(
            "invalid_option_combination",
            "workflow run --repin applies only to a resume: a new workflow pins the live "
            "runtime already.",
        )
    if command.repin and command.dry_run:
        raise DelegateError(
            "invalid_option_combination",
            "workflow resume --repin does not apply to --dry-run: a dry run launches no "
            "supervisor and never moves the pin.",
        )
    warnings: list[str] = []
    gate_decision: JsonObject | None = None
    operational_environment = {
        key: value for key, value in os.environ.copy().items() if key in workflow_attempts.ENV_KEYS
    }
    lock_fd: int | None = None
    previous_status: JsonObject | None = None
    previous_result: bytes | None = None
    previous_result_exists = False
    previous_approval: str | None = None
    approval_changed = False
    approval_loaded = False
    approval_payload: JsonObject = {}
    journal_events: list[JsonObject] | None = None
    journal_sequence = 0

    def load_journal() -> list[JsonObject]:
        nonlocal journal_events, journal_sequence
        if journal_events is None:
            journal_events = registry.iter_journal(root / registry.JOURNAL_FILE)
            journal_sequence = max(
                (event["seq"] for event in journal_events if isinstance(event.get("seq"), int)),
                default=0,
            )
        return journal_events

    def append_run_event(event_type: str, **payload: JsonValue) -> None:
        nonlocal journal_sequence
        load_journal()
        journal_sequence += 1
        registry.append_jsonl(
            root / registry.JOURNAL_FILE,
            {
                "seq": journal_sequence,
                "type": event_type,
                "at": run_registry.utc_now_iso(),
                **payload,
            },
        )

    def load_approval() -> JsonObject:
        nonlocal approval_loaded, approval_payload, previous_approval
        if approval_loaded:
            return approval_payload
        approval_loaded = True
        try:
            previous_approval = private_io.read_private_text_bounded(
                root / registry.APPROVAL_FILE,
                max_bytes=private_io.PRIVATE_RECORD_READ_MAX_BYTES,
            )
        except private_io.BoundedReadError as exc:
            if exc.reason == "not_found":
                return approval_payload
            raise DelegateError("invalid_workflow_approval", str(exc)) from exc
        try:
            value = json.loads(previous_approval)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DelegateError("invalid_workflow_approval", "approval file is invalid") from exc
        if not isinstance(value, dict):
            raise DelegateError("invalid_workflow_approval", "approval file is invalid")
        approval_payload = value
        return approval_payload

    def restore_approval() -> None:
        if not approval_changed:
            return
        # Both call sites still hold the workflow lock. Restore exact prior
        # bytes, not a reserialized approval with different evidence or format.
        path = root / registry.APPROVAL_FILE
        if previous_approval is None:
            path.unlink(missing_ok=True)
        else:
            run_registry.write_private_text_atomic(path, previous_approval)

    pin: workflow_pinning.WorkflowPin | None = None
    attempt: workflow_attempts.WorkflowAttempt | None = None
    source_script: str | None = None
    script_hash: str | None = None
    repin_result: workflow_pinning.RepinResult | None = None
    runtime_pin: JsonObject | None = None
    repin_attempted = False
    repin_event_journaled = False
    launched = False

    def rollback_repin() -> None:
        # A resume that fails after the pin moved must leave the workflow on the
        # runtime it had, the way it restores status.json and the approval file.
        # The way back is the pre-repin backup on disk, so it works even when
        # repin_to_live raised before returning a result. Once the supervisor has
        # launched there is nothing to undo: it runs on the new pin.
        if not repin_attempted or launched:
            return
        try:
            undone = workflow_pinning.rollback_repin(wf_id)
        except (OSError, workflow_pinning.WorkflowPinError):
            # The backup stays where it is; the next resume puts the pin back.
            return
        if undone is not None and repin_event_journaled:
            # The journal said the workflow moved; say that it did not.
            with contextlib.suppress(OSError):
                append_run_event("runtime_repin_rolled_back", reason="launch_failed", **undone)

    if command.resume:
        wf_id = _validate_wf_id(command.resume)
        root = registry.workflow_dir(workspace, wf_id)
        if not root.exists():
            raise _workflow_not_found(
                wf_id,
                workspace,
                "run --resume",
                registry_roster.find_workflow(wf_id, exclude=workspace),
            )
        _require_current_workflow(registry.read_json(root / registry.STATUS_FILE) or {})
        if not command.dry_run:
            _recover_interrupted_repin(root, wf_id)
        try:
            if command.repin:
                # The pin moves under the workflow lock, after the checks below,
                # and the attempt snapshot is bound to whichever pin results, so
                # only the operational config can be validated this early.
                workflow_attempts.operational_values(config, environment=operational_environment)
            else:
                pin = workflow_pinning.load_pin(wf_id)
                if pin is None:
                    raise workflow_pinning.WorkflowPinError(
                        "invalid_pin", "workflow pin is missing; start a new workflow"
                    )
                # load_pin already vouched for the pin, so a description that
                # still fails is reported as unchecked; the notice is advisory
                # and must never be the reason a resume is refused.
                runtime_pin = _describe_runtime_pin(wf_id)
                notice = workflow_pinning.runtime_drift_notice(
                    runtime_pin,
                    workflow_id=wf_id,
                    resume_hint=(
                        "Resume keeps the pinned runtime, so fixes shipped since then do not "
                        "reach this workflow; pass --repin to run it on the live runtime instead."
                    ),
                )
                if notice is not None:
                    warnings.append(notice)
                if not command.dry_run:
                    attempt = workflow_attempts.create(
                        pin,
                        workflow_attempts.prepare(
                            pin, config, config_source, environment=operational_environment
                        ),
                    )
        except (workflow_pinning.WorkflowPinError, delegate_config.ConfigError) as exc:
            raise DelegateError(exc.error, exc.message) from exc
        # Acquire the lock before any approval/budget mutation so a failed
        # resume cannot clobber a live supervisor's status.json.
        try:
            lock_fd = _acquire_workflow_lock(root, wf_id)
        except DelegateError as exc:
            if approve_gate and exc.error == "workflow_locked":
                # Same code, but say what the operator can do about it.
                raise DelegateError(
                    "workflow_locked",
                    f"Workflow is already running: {wf_id}. "
                    + _approve_refusal_hint(root, wf_id, "running"),
                ) from exc
            raise
        try:
            status = registry.read_json(root / registry.STATUS_FILE) or {}
            _require_current_workflow(status)
            previous_status = dict(status)
            if command.repin:
                # Checked before anything is touched. A resume cancels the prior
                # attempt's live children; a repin would add a runtime move on
                # top of losing that work, and the operator should choose both.
                live_children = runtime.live_workflow_children(
                    workspace, wf_id, include_starting=True
                )
                if live_children:
                    named = ", ".join(
                        str(child.get("alias") or child["runId"]) for child in live_children[:5]
                    )
                    more = len(live_children) - 5
                    raise DelegateError(
                        "repin_children_running",
                        f"cannot repin {wf_id}: {len(live_children)} of its child run(s) are "
                        f"still running ({named}{f', and {more} more' if more > 0 else ''}). "
                        "Resume cancels them, and a repin would move the runtime under work "
                        "still in flight. Wait for them to finish, stop them with "
                        f"`delegate workflow kill {wf_id}`, or resume without --repin.",
                    )
            if approve_gate:
                # Recover gate evidence only after acquiring the supervisor
                # lock; an approval racing a draining supervisor must not
                # publish a paused projection before its resume is admitted.
                gate_name = gate_choice.gate if gate_choice is not None else None
                recovered = _latest_unapproved_gate_event(
                    root,
                    load_approval(),
                    events=load_journal(),
                    gate=gate_name,
                )
                if gate_name is not None and recovered is None:
                    pending = _pending_gate_names(root, load_approval(), load_journal())
                    raise DelegateError(
                        "workflow_gate_not_found",
                        f"Workflow {wf_id} has no unapproved gate named {gate_name!r}; "
                        f"pending gates: {', '.join(pending) if pending else 'none'}."
                        + (
                            ""
                            if pending
                            else " " + _approve_refusal_hint(root, wf_id, status.get("status"))
                        ),
                    )
                gate_key = recovered.get("key") if recovered else status.get("gateKey")
                if not isinstance(gate_key, str):
                    raise DelegateError(
                        "workflow_not_gated",
                        f"Workflow is not waiting on a gate: {wf_id}. "
                        + _approve_refusal_hint(root, wf_id, status.get("status")),
                    )
                gate_event = recovered or _gate_event(
                    load_journal(), gate_key, status.get("gateResultHash")
                )
                gate_decision = _gate_decision(gate_choice, gate_event, gate_key)
                status = dict(status)
                status.update({"status": "paused", "gateKey": gate_key})
                if recovered:
                    status.update(
                        {
                            "ok": True,
                            "gateResult": recovered.get("result"),
                            "gateResultHash": recovered["gateResultHash"],
                            "updatedAt": run_registry.utc_now_iso(),
                        }
                    )
                    # status.json is a projection the journal outranks, so the
                    # repair is published to disk rather than kept in memory
                    # for this call. Re-reading it makes the recovered
                    # projection the rollback target too: a resume that fails
                    # below must not restore the clobbered projection.
                    registry.write_status(root, status)
                    previous_status = registry.read_json(root / registry.STATUS_FILE) or {}
            if command.repin:
                repin_attempted = True
                try:
                    repin_result = workflow_pinning.repin_to_live(wf_id)
                    pin = repin_result.pin
                    attempt = workflow_attempts.create(
                        pin,
                        workflow_attempts.prepare(
                            pin, config, config_source, environment=operational_environment
                        ),
                    )
                    runtime_pin = workflow_pinning.runtime_drift(
                        workflow_pinning.pinned_runtime_summary(wf_id)
                    )
                    runtime_pin["checked"] = True
                except (workflow_pinning.WorkflowPinError, delegate_config.ConfigError) as exc:
                    raise DelegateError(exc.error, exc.message) from exc
                if repin_result.changed:
                    runtime_pin["repinned"] = True
                    runtime_pin["previous"] = repin_result.previous
            # This lock is only available because the prior attempt's
            # supervisor is gone. Any child it still had in flight is an
            # orphan: its row sits at rawStatus=running with a dead pid, lists
            # as stale forever, and the replay fails the thunk that reaches it
            # with "already terminal (stale)" instead of relaunching. Seal
            # those rows before the new attempt starts, the same way kill
            # does, and leave the lost supervisor in the journal. Runs after
            # gate validation so an invalid approve never reaches it, and
            # never on a dry run, which launches no replacement supervisor.
            if not command.dry_run:
                try:
                    superseded = runtime.cancel_workflow_children(workspace, wf_id)
                except runtime.WorkflowChildCancellationError as exc:
                    raise DelegateError(
                        "workflow_children_unsealed",
                        f"resume could not seal the prior attempt's children: {exc}",
                    ) from exc
                prior_status = status.get("status")
                supervisor_lost = prior_status in LIVE_WORKFLOW_STATUSES
                if superseded or supervisor_lost:
                    append_run_event(
                        "attempt_superseded",
                        priorStatus=prior_status,
                        priorSupervisorPid=status.get("supervisorPid"),
                        supervisorLost=supervisor_lost,
                        cancelled=superseded,
                    )
            result_path = root / registry.RESULT_FILE
            try:
                previous_result = result_path.read_bytes()
                previous_result_exists = True
            except FileNotFoundError:
                pass
            resume_from_dry_run = (
                status.get("status") == "dry_run" or status.get("replayJournal") is False
            )
            gate_key = status.get("gateKey")
            # A dry run neither approves the gate nor records that it did: a
            # dry-run park_gate() answers itself, and a recorded approval would
            # silently open the gate for the next live resume.
            if (
                not command.dry_run
                and status.get("status") == "paused"
                and isinstance(gate_key, str)
            ):
                gate_result_hash = status.get("gateResultHash")
                if not isinstance(gate_result_hash, str):
                    raise DelegateError(
                        "invalid_workflow_gate",
                        "Workflow gate is missing its result identity; start a new workflow.",
                    )
                approval_changed = True
                approval_payload = registry.record_approval(
                    root,
                    gate_key,
                    gate_result_hash,
                    previous=load_approval(),
                    decision=gate_decision,
                )
            script_path = root / registry.SCRIPT_FILE
            recorded_source_script = status.get("sourceScript")
            source_script = (
                recorded_source_script if isinstance(recorded_source_script, str) else None
            )
            recorded_script_hash = status.get("scriptSha256")
            script_hash = recorded_script_hash if isinstance(recorded_script_hash, str) else None
            warnings.extend(_resume_source_warnings(status))
            warnings.extend(_resume_frozen_script_warnings(status, script_path))
            args_value = status.get("args")
            budget_total = command.budget
            if budget_total is None:
                budget_payload = status.get("budget")
                if isinstance(budget_payload, dict) and isinstance(
                    budget_payload.get("total"), int
                ):
                    budget_total = budget_payload["total"]
            else:
                budget_payload = status.get("budget")
                spent = (
                    0
                    if resume_from_dry_run
                    else budget_payload.get("spent")
                    if isinstance(budget_payload, dict)
                    else 0
                )
                spent = spent if isinstance(spent, int) and spent >= 0 else 0
                status["budget"] = {
                    "total": budget_total,
                    "spent": spent,
                    "remaining": max(budget_total - spent, 0),
                }
                if not command.dry_run:
                    registry.write_status(root, status)
        except BaseException:
            try:
                rollback_repin()
                restore_approval()
            finally:
                with contextlib.suppress(OSError):
                    os.close(lock_fd)
            raise
    else:
        if not command.dry_run:
            try:
                workflow_attempts.operational_values(config, environment=operational_environment)
            except (workflow_pinning.WorkflowPinError, delegate_config.ConfigError) as exc:
                raise DelegateError(exc.error, exc.message) from exc
        source = _script_path_for_command(command)
        check_result = check_script(source)
        warnings = list(check_result.warnings)
        launch_env = workspace_spec.resolve_env(dict(command.env), command.env_files)
        wf_id = registry.generate_workflow_id()
        root = registry.ensure_workflow_dir(workspace, wf_id)
        if launch_env:
            workspace_spec.write_run_env(root, launch_env)
        data = source.read_bytes()
        script_path = root / registry.SCRIPT_FILE
        source_script = str(source)
        script_hash = registry.script_sha256(data)
        run_registry.write_private_bytes(script_path, data)
        args_value = _parse_args(command.args_json)
        budget_total = command.budget
        registry.write_json(root / registry.ARGS_FILE, {"args": args_value})
        registry.register_workflow(
            workspace,
            root,
            {
                "ok": True,
                "wfId": wf_id,
                "status": "created",
                "workspace": str(workspace),
                "scriptPath": str(script_path),
                "sourceScript": str(source),
                "journalPath": str(root / registry.JOURNAL_FILE),
                "resultPath": str(root / registry.RESULT_FILE),
                "scriptSha256": script_hash,
                "args": args_value,
                "budget": {"total": budget_total, "spent": 0, "remaining": budget_total},
                # Persisted rather than passed on argv: the supervisor is
                # detached and re-execs itself, so status.json is the only thing
                # that survives to tell it where to report.
                "notify": command.notify,
            },
        )
        try:
            pin = workflow_pinning.create_pin(
                wf_id,
                workspace=workspace,
                config=config,
                data_home=worktrees_data_home(config),
            )
            if not command.dry_run:
                attempt = workflow_attempts.create(
                    pin,
                    workflow_attempts.prepare(
                        pin, config, config_source, environment=operational_environment
                    ),
                )
        except (workflow_pinning.WorkflowPinError, delegate_config.ConfigError) as exc:
            raise DelegateError(exc.error, exc.message) from exc
    if command.dry_run:
        if not command.resume:
            status = registry.read_json(root / registry.STATUS_FILE) or {}
            status.update({"status": "dry_run", "updatedAt": run_registry.utc_now_iso()})
            registry.write_status(root, status)
        if lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(lock_fd)
        return emit_dry_run(
            notify=command.notify,
            wf_id=wf_id,
            root=root,
            script_path=script_path,
            source_script=source_script,
            script_hash=script_hash,
            workspace=workspace,
            config=config,
            args_value=args_value,
            budget_total=budget_total,
            json_mode=command.json_mode,
            warnings=warnings,
            stdout=stdout,
            stderr=stderr,
            preserve_status=bool(command.resume),
        )
    if lock_fd is None:
        lock_fd = _acquire_workflow_lock(root, wf_id)
    supervisor_argv = [
        *pin.cli_argv,
        "--cwd",
        str(workspace),
        "workflow",
        "_supervise",
        wf_id,
    ]
    try:
        if attempt is not None:
            append_run_event("attempt_config", **attempt.metadata)
            current_status = registry.read_json(root / registry.STATUS_FILE) or {}
            current_status["attemptConfig"] = attempt.metadata
            registry.write_status(root, current_status)
            if command.resume:
                status["attemptConfig"] = attempt.metadata
        if command.resume:
            prior_attempt = status.get("replayAttempt")
            replay_attempt = (
                prior_attempt if isinstance(prior_attempt, int) and prior_attempt >= 0 else 0
            )
            status.update(
                {
                    "ok": True,
                    "status": "starting",
                    "replayJournal": not resume_from_dry_run,
                    "replayAttempt": replay_attempt + 1,
                    "updatedAt": run_registry.utc_now_iso(),
                    # A resume starts a new attempt: watchdog markers from a prior
                    # fire must not survive into it, or a resumed-then-successful
                    # run reports succeeded with watchdogCancelRequested still
                    # true (WDB-R4). Explicit None is required — write_status
                    # preserves these keys only when absent from the payload, so
                    # a pop-based clear would be silently undone.
                    "watchdogFiredAt": None,
                    "watchdogReason": None,
                    "watchdogCancelRequested": None,
                }
            )
            # A resume may name a different target, or none; an explicit --notify
            # on the resume wins, and its absence keeps whatever the run set.
            if command.notify is not None:
                status["notify"] = command.notify
            if resume_from_dry_run and isinstance(status.get("budget"), dict):
                total = status["budget"].get("total")
                status["budget"].update(
                    {"spent": 0, "remaining": total if isinstance(total, int) else None}
                )
            registry.write_status(root, status)
            with contextlib.suppress(FileNotFoundError):
                result_path.unlink()
        workflow_pinning.register_active_supervisor(
            wf_id,
            workflow_root=root,
            workspace=workspace,
            pin=pin,
        )
        systemd_warning = _systemd_detach_warning()
        if systemd_warning is not None and systemd_warning not in warnings:
            warnings.append(systemd_warning)
        if repin_result is not None and repin_result.changed:
            # As late as the parent can write: once the supervisor starts it owns
            # the journal, so this cannot follow the launch. A launch that fails
            # after this line journals runtime_repin_rolled_back (see above).
            previous_runtime = repin_result.previous or {}
            append_run_event(
                "runtime_repinned",
                fromDigest=previous_runtime.get("digest"),
                fromVersion=previous_runtime.get("version"),
                fromPinnedAt=previous_runtime.get("pinnedAt"),
                toDigest=pin.runtime_digest,
                toVersion=(runtime_pin or {}).get("pinned", {}).get("version"),
            )
            repin_event_journaled = True
        previous_environment = workflow_pinning.temporarily_apply_environment(pin, attempt=attempt)
        try:
            runtime.detach_supervisor(supervisor_argv, cwd=workspace, lock_fd=lock_fd)
            launched = True
        finally:
            if previous_environment:
                workflow_pinning.restore_environment(previous_environment)
        # The pre-repin backup is retired by the supervisor itself, before its
        # first step (runtime.run_supervisor): only a supervisor that actually
        # runs on the new pin may make the move permanent.
    except BaseException:
        rollback_repin()
        restore_approval()
        if previous_status is not None:
            registry.write_json(root / registry.STATUS_FILE, previous_status)
            if previous_result_exists and previous_result is not None:
                run_registry.write_private_bytes(result_path, previous_result)
            else:
                with contextlib.suppress(FileNotFoundError):
                    result_path.unlink()
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(lock_fd)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "wfId": wf_id,
        "journalPath": str(root / registry.JOURNAL_FILE),
        "scriptPath": str(script_path),
    }
    if attempt is not None:
        payload["attemptConfig"] = attempt.metadata
        payload["effectiveConfigPath"] = str(attempt.config_path)
    payload["profileIdentityPinned"] = True
    payload["profileIdentity"] = pin.profile_identity
    if runtime_pin is not None:
        payload["runtimePin"] = runtime_pin
    if source_script is not None:
        payload["sourceScript"] = source_script
    if script_hash is not None:
        payload["scriptSha256"] = script_hash
    if warnings:
        payload["warnings"] = warnings
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        for warning in warnings:
            print(f"warning: {warning}", file=stderr)
        print(f"wfId: {wf_id}", file=stdout)
        if repin_result is not None and runtime_pin is not None:
            pinned_now = workflow_pinning.describe_runtime(runtime_pin["pinned"])
            if repin_result.changed:
                previous_runtime = workflow_pinning.describe_runtime(runtime_pin["previous"])
                print(f"runtimeRepinned: {previous_runtime} -> {pinned_now}", file=stdout)
            else:
                print(
                    f"runtimeRepinned: no change, already on the live runtime {pinned_now}",
                    file=stdout,
                )
        print(f"journalPath: {payload['journalPath']}", file=stdout)
        print(f"scriptPath: {payload['scriptPath']}", file=stdout)
        if source_script is not None:
            print(f"sourceScript: {source_script}", file=stdout)
        if script_hash is not None:
            print(f"scriptSha256: {script_hash}", file=stdout)
    return EXIT_OK


def emit_dry_run(
    *,
    wf_id: str,
    root: Path,
    script_path: Path,
    workspace: Path,
    config: JsonObject,
    args_value: JsonValue,
    budget_total: int | None,
    json_mode: bool,
    warnings: list[str],
    stdout: TextIO,
    stderr: TextIO,
    notify: str | None = None,
    source_script: str | None = None,
    script_hash: str | None = None,
    preserve_status: bool = False,
) -> int:
    state = runtime.WorkflowState(
        wf_id=wf_id,
        workspace=workspace,
        root=root,
        script_path=script_path,
        config=config,
        cli_argv=_delegate_cli_argv(),
        args=args_value,
        budget=runtime.Budget(budget_total),
        dry_run=True,
        preserve_status=preserve_status,
        # A dry run writes status too, and status.json is rebuilt rather than
        # merged, so omitting the target here erases it from a workflow that was
        # created with one and then dry-run before launching.
        notify_target=notify,
    )
    timeout_seconds = delegate_config.dry_run_timeout_seconds(config)
    outcome: dict[str, object] = {}

    def _execute() -> None:
        try:
            outcome["result"] = runtime.execute_workflow(state)
        except BaseException as exc:  # surfaced on the calling thread below
            outcome["error"] = exc

    # A dry run launches no real children, so its worker threads are disposable
    # and the interpreter can exit out from under a script that parks forever
    # waiting on a human gate it will never receive.
    worker = threading.Thread(target=_execute, name="dry-run", daemon=True)
    # A real run's supervisor is detached with cwd=workspace, so scripts read
    # plan state and git heads relative to the --cwd workspace. The dry run
    # executes in this process, which may sit anywhere; give the script the
    # same working directory a real run would.
    try:
        previous_cwd: str | None = os.getcwd()
    except OSError:
        previous_cwd = None
    os.chdir(workspace)
    try:
        worker.start()
        worker.join(timeout_seconds)
    finally:
        # A worker still running keeps the workspace cwd. A timed-out script
        # cannot be stopped, and restoring the process cwd under it would move
        # every relative path it still resolves out of the workspace.
        if previous_cwd is not None and not worker.is_alive():
            with contextlib.suppress(OSError):
                os.chdir(previous_cwd)
    if worker.is_alive():
        raise DelegateError(
            "dry_run_timeout",
            f"Dry run exceeded {timeout_seconds}s and was abandoned. A workflow that "
            "waits on a human gate cannot resolve one during a dry run; give the "
            "script a dry-run path for its gates, or raise "
            "workflows.dryRunTimeoutSeconds.",
        )
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise DelegateError("workflow_execution_failed", str(error)) from error
    result = outcome.get("result")
    tree = _run_tree(state.dry_runs)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "dryRun": True,
        "wfId": wf_id,
        "scriptPath": str(script_path),
        "runTree": tree,
        "result": result,
        "warnings": [*warnings, DRY_RUN_WRITE_WARNING],
    }
    if source_script is not None:
        payload["sourceScript"] = source_script
    if script_hash is not None:
        payload["scriptSha256"] = script_hash
    if json_mode:
        rendering.print_json(payload, stdout)
    else:
        for warning in [*warnings, DRY_RUN_WRITE_WARNING]:
            print(f"warning: {warning}", file=stderr)
        print(f"scriptPath: {script_path}", file=stderr)
        if source_script is not None:
            print(f"sourceScript: {source_script}", file=stderr)
        if script_hash is not None:
            print(f"scriptSha256: {script_hash}", file=stderr)
        print(json.dumps(tree, indent=2, sort_keys=True), file=stdout)
    return EXIT_OK


def _workflow_state_hint(workspace: Path) -> str:
    """Say where workflow state was searched.

    Workflow dirs live under the workspace's own ``.delegate`` store, so a
    lookup from the main checkout of the repo a workflow runs in legitimately
    finds nothing.  Naming the searched root and the ``--cwd`` form keeps
    "stored elsewhere" distinguishable from "gone".
    """
    return (
        f"Workflow state is workspace-scoped; searched {registry.workflow_root(workspace)} "
        "(use --cwd PATH to target the workspace that launched it)."
    )


def _workflow_not_found(
    wf_id: str, workspace: Path, action: str, matches: list[registry_roster.RosterMatch]
) -> DelegateError:
    """workflow_not_found that names the workspace a known Registry holds it in."""
    where, next_actions = registry_roster.describe_matches(
        wf_id, matches, command=f"workflow {action}"
    )
    return DelegateError(
        "workflow_not_found",
        f"Workflow not found: {wf_id}. {_workflow_state_hint(workspace)}{where}",
        next_actions=next_actions or None,
    )


def emit_status(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace, read_across_registries=True)
    payload = registry.read_json(root / registry.STATUS_FILE)
    if payload is None:
        raise DelegateError(
            "workflow_not_found",
            f"Workflow status not found: {command.wf_id} (looked in {root}).",
        )
    view = _status_view(root, payload)
    _add_journal_reasons(root, view)
    runtime_pin, runtime_notice = _status_runtime_pin(root.name, view.get("status"))
    view["runtimePin"] = runtime_pin
    resolved_workspace = _resolved_elsewhere(root, workspace)
    if resolved_workspace is not None:
        view["resolvedWorkspace"] = resolved_workspace
    if command.json_mode:
        rendering.print_json(view, stdout)
    else:
        print(f"{view.get('wfId')}: {view.get('status')}", file=stdout)
        print(f"journalPath: {view.get('journalPath')}", file=stdout)
        pause = view.get("pause")
        if isinstance(pause, dict):
            print(f"paused: {pause.get('summary')}", file=stdout)
            gate_line = f"gate: {pause.get('gateKey')}"
            for field_name in ("gateName", "title", "assignee"):
                if pause.get(field_name):
                    gate_line += f" {field_name}={pause[field_name]}"
            print(gate_line, file=stdout)
            if pause.get("next"):
                print(f"next: {pause['next']}", file=stdout)
        for row in view.get("timeouts") or []:
            print(
                f"timeout: {row.get('label') or row.get('key')} item={row.get('item')} "
                f"engine={row.get('engine')} after {row.get('timeout')}s "
                f"next={row.get('nextEngine')}",
                file=stdout,
            )
        if runtime_notice is not None:
            print(runtime_notice, file=stdout)
        if resolved_workspace is not None:
            print(f"resolvedWorkspace: {resolved_workspace}", file=stdout)
        if view.get("status") == "stalled":
            print(
                f"supervisor dead; resume with: workflow run --resume {view.get('wfId')}",
                file=stdout,
            )
    return EXIT_OK


def _add_journal_reasons(root: Path, view: JsonObject) -> None:
    """Attach ``pause`` (why it is paused) and ``timeouts`` from the journal."""
    journal = root / registry.JOURNAL_FILE
    if not journal.exists():
        return
    events = list(registry.iter_journal(journal))
    if view.get("status") == "paused" and isinstance(view.get("gateKey"), str):
        pause = status_reasons.pause_reason(view.get("gateKey"), view.get("gateResultHash"), events)
        # The commands come from the gate's declared actions: a bare approve is
        # refused by a gate that offers only retry/accept.
        decision = view.get("decision")
        next_actions = decision.get("nextActions") if isinstance(decision, dict) else None
        if isinstance(next_actions, list) and next_actions:
            pause["next"] = next_actions[0]
            pause["nextActions"] = next_actions
        view["pause"] = pause
    recent = status_reasons.timeouts(events)
    if recent:
        view["timeouts"] = recent


def _describe_runtime_pin(wf_id: str) -> JsonObject:
    """Pinned-versus-live runtime facts, or ``checked: false`` with why they are unknown."""
    try:
        drift = workflow_pinning.runtime_drift(workflow_pinning.pinned_runtime_summary(wf_id))
    except workflow_pinning.WorkflowPinError as exc:
        return {"checked": False, "error": exc.error, "message": exc.message}
    drift["checked"] = True
    return drift


def _status_runtime_pin(wf_id: str, status: object) -> tuple[JsonObject, str | None]:
    """The pinned-versus-live runtime view for ``status``, and its notice when they differ.

    ``status`` must keep working when the pin cannot be described (a workflow
    that predates pinning, a deleted pin), so that reports ``checked: false``
    with the reason instead of failing the command.
    """
    drift = _describe_runtime_pin(wf_id)
    if not drift["checked"]:
        return drift, None
    if status in {"succeeded", "dry_run"}:
        # Nothing left to move: a finished workflow's pin is history.
        return drift, None
    resumable = status in {"stalled", "failed", "killed", "paused"}
    hint = (
        f"A resume keeps the pinned runtime; `delegate workflow resume {wf_id} --repin` "
        "moves it onto the live runtime."
        if resumable
        else "This supervisor keeps running on the pinned runtime."
    )
    return drift, workflow_pinning.runtime_drift_notice(drift, workflow_id=wf_id, resume_hint=hint)


def emit_events(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace, read_across_registries=True)
    events = [
        event
        for event in registry.iter_journal(root / registry.JOURNAL_FILE)
        if event.get("seq", 0) > command.since
    ]
    payload: JsonObject = {"ok": True, "schema": WORKFLOW_COMMAND_SCHEMA, "events": events}
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        for event in events:
            print(json.dumps(event, sort_keys=True), file=stdout)
    return EXIT_OK


def emit_watch(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace, read_across_registries=True)
    since = command.since
    collected: list[JsonObject] = []
    stalled = False
    reader = registry.JournalReader(root / registry.JOURNAL_FILE)
    while True:
        # Observe terminal status before the final drain so a completion event
        # appended just before that projection is not lost at watch shutdown.
        status = _status_view(root, registry.read_json(root / registry.STATUS_FILE) or {})
        poll_since = since
        for event in reader.read_events(final=status.get("status") in WAIT_DONE_WORKFLOW_STATUSES):
            if event.get("seq", 0) <= poll_since:
                continue
            seq = event.get("seq")
            if isinstance(seq, int):
                since = max(since, seq)
            if command.jsonl:
                print(
                    json.dumps(
                        {"type": "event", "schema": WORKFLOW_COMMAND_SCHEMA, "event": event}
                    ),
                    file=stdout,
                    flush=True,
                )
            elif command.json_mode:
                collected.append(event)
            else:
                print(json.dumps(event, sort_keys=True), file=stdout, flush=True)
        if status.get("status") in WAIT_DONE_WORKFLOW_STATUSES:
            stalled = status.get("status") == "stalled"
            break
        time.sleep(1)
    drain_incomplete = status.get("signalDrainIncomplete") is True
    if command.jsonl:
        print(
            json.dumps(
                {
                    "type": "final",
                    "schema": WORKFLOW_COMMAND_SCHEMA,
                    "ok": not stalled,
                    "lastSeq": since,
                    "workflow": status,
                }
            ),
            file=stdout,
            flush=True,
        )
    elif command.json_mode:
        rendering.print_json(
            {
                "ok": not stalled,
                "schema": WORKFLOW_COMMAND_SCHEMA,
                "events": collected,
                "lastSeq": since,
                **({"status": "stalled"} if stalled else {}),
                **({"signalDrainIncomplete": True} if drain_incomplete else {}),
            },
            stdout,
        )
    else:
        if stalled:
            print(
                f"stalled: supervisor dead; resume with: workflow run --resume {command.wf_id}",
                file=stdout,
            )
        if drain_incomplete:
            print(SIGNAL_DRAIN_INCOMPLETE_WARNING, file=stdout)
    return 1 if stalled else EXIT_OK


def emit_result(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root, wf_id, resolution_kind = _resolve_wait_or_result(command, workspace, require_result=True)
    resolved_workspace = _resolved_elsewhere(root, workspace)
    payload = registry.read_json(root / registry.RESULT_FILE)
    if payload is None:
        raise DelegateError("workflow_result_missing", f"Workflow result not found: {wf_id}")
    if command.result_field is not None:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DelegateError(
                "workflow_result_not_object",
                f"Workflow result is not an object: {wf_id}",
            )
        if command.result_field not in result:
            raise DelegateError(
                "workflow_result_field_missing",
                f"Workflow result field not found: {command.result_field}",
            )
        value = result[command.result_field]
        if command.json_mode:
            envelope: JsonObject = {
                "ok": True,
                "schema": WORKFLOW_COMMAND_SCHEMA,
                "wfId": wf_id,
                "field": command.result_field,
                "value": value,
            }
            if resolution_kind is not None:
                envelope["resolutionKind"] = resolution_kind
            if resolved_workspace is not None:
                envelope["resolvedWorkspace"] = resolved_workspace
            rendering.print_json(envelope, stdout)
        elif isinstance(value, str):
            print(value, file=stdout)
        else:
            print(json.dumps(value, sort_keys=True), file=stdout)
        return EXIT_OK
    if command.json_mode:
        output = dict(payload)
        if resolution_kind is not None:
            output["wfId"] = wf_id
            output["resolutionKind"] = resolution_kind
        if resolved_workspace is not None:
            output["resolvedWorkspace"] = resolved_workspace
        rendering.print_json(output, stdout)
    else:
        print(json.dumps(payload.get("result"), indent=2, sort_keys=True), file=stdout)
    return EXIT_OK


def emit_wait(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root, wf_id, resolution_kind = _resolve_wait_or_result(command, workspace, require_result=False)
    deadline = time.monotonic() + (command.timeout or 3600)
    payload: JsonObject | None = None
    while True:
        on_disk = registry.read_json(root / registry.STATUS_FILE)
        payload = _status_view(root, on_disk) if isinstance(on_disk, dict) else on_disk
        status = payload.get("status") if isinstance(payload, dict) else None
        if status in WAIT_DONE_WORKFLOW_STATUSES or time.monotonic() >= deadline:
            break
        time.sleep(1)
    timed_out = (
        not isinstance(payload, dict) or payload.get("status") not in WAIT_DONE_WORKFLOW_STATUSES
    )
    status = payload.get("status") if isinstance(payload, dict) else None
    result: JsonObject = {
        "ok": not timed_out and status in {"succeeded", "paused"},
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "timedOut": timed_out,
        "workflow": payload or {},
    }
    if resolution_kind is not None:
        result["wfId"] = wf_id
        result["resolutionKind"] = resolution_kind
    resolved_workspace = _resolved_elsewhere(root, workspace)
    if resolved_workspace is not None:
        result["resolvedWorkspace"] = resolved_workspace
    if command.json_mode:
        rendering.print_json(result, stdout)
    else:
        print(f"{wf_id}: {(payload or {}).get('status', 'unknown')}", file=stdout)
        if status == "stalled":
            print(
                f"supervisor dead; resume with: workflow run --resume {wf_id}",
                file=stdout,
            )
        if isinstance(payload, dict) and payload.get("signalDrainIncomplete") is True:
            print(SIGNAL_DRAIN_INCOMPLETE_WARNING, file=stdout)
    if timed_out:
        return 124
    return 0 if result["ok"] else 1


def emit_approve(
    command: WorkflowCommand,
    *,
    workspace: Path,
    config: JsonObject,
    stdout: TextIO,
    config_source: str = "command-config",
) -> int:
    root = _workflow_dir_for_command(command, workspace)
    gate_choice = GateChoice(
        gate=command.gate,
        action=command.gate_action,
        note=command.gate_note,
    )
    if command.gate_data_json is not None:
        try:
            data = json.loads(command.gate_data_json)
        except json.JSONDecodeError as exc:
            raise DelegateError(
                "invalid_workflow_gate_data", f"workflow approve --data is not valid JSON: {exc}"
            ) from exc
        gate_choice = replace(gate_choice, data=data, has_data=True)
    resumed = WorkflowCommand(
        "run", resume=command.wf_id, json_mode=command.json_mode, repin=command.repin
    )
    result = emit_run(
        resumed,
        workspace=workspace,
        config=config,
        stdout=stdout,
        stderr=stdout,
        approve_gate=True,
        config_source=config_source,
        gate_choice=gate_choice,
    )
    # Approval is an operator-facing transition: wait for the detached
    # trampoline to publish a terminal projection when the child is already
    # ready.  This removes a misleading transient ``starting`` read without
    # turning a genuinely slow workflow into a blocking wait.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        current = registry.read_json(root / registry.STATUS_FILE) or {}
        if current.get("status") not in {"starting", "running"}:
            break
        time.sleep(0.01)
    return result


def emit_reject(
    command: WorkflowCommand,
    *,
    workspace: Path,
    config: JsonObject,
    stdout: TextIO,
) -> int:
    """Tombstone a workflow agent key from a parked or dead supervisor."""
    root = _workflow_dir_for_command(command, workspace)
    wf_id = command.wf_id
    key_or_label = command.key_or_label
    reason = command.reason
    if wf_id is None or key_or_label is None:
        raise DelegateError(
            "missing_workflow_reject_args",
            'workflow reject requires <wfId> <key-or-label> --reason "<text>".',
        )
    if not isinstance(reason, str) or not reason.strip():
        raise DelegateError(
            "missing_workflow_reject_reason",
            "workflow reject requires a non-empty --reason.",
        )
    if registry.supervisor_alive(root):
        raise DelegateError(
            "workflow_running",
            "Cannot reject a live running supervisor; let the script judge — "
            "reject() from the workflow script.",
        )
    try:
        lock_fd = _acquire_workflow_lock(root, wf_id)
    except DelegateError as exc:
        raise DelegateError(
            "workflow_running",
            "Cannot reject a live running supervisor; let the script judge — "
            "reject() from the workflow script.",
        ) from exc
    try:
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        _require_current_workflow(status)
        budget_payload = status.get("budget")
        total = budget_payload.get("total") if isinstance(budget_payload, dict) else None
        spent = budget_payload.get("spent") if isinstance(budget_payload, dict) else None
        state = runtime.WorkflowState(
            wf_id=wf_id,
            workspace=workspace,
            root=root,
            script_path=root / registry.SCRIPT_FILE,
            config=config,
            cli_argv=_delegate_cli_argv(),
            args=runtime.load_args(root),
            budget=runtime.Budget(
                total if isinstance(total, int) else None,
                spent if isinstance(spent, int) and spent >= 0 else 0,
            ),
            replay_journal=status.get("replayJournal") is not False,
        )
        try:
            key, label = state.resolve_agent_key(key_or_label)
        except ValueError as exc:
            raise DelegateError("workflow_reject_unresolved", str(exc)) from exc
        # An operator tombstone is a coordinator action, never the seat's own
        # invalid output; journal consumers need the distinction.
        state.append_journal_only(
            "agent_rejected",
            **runtime.rejection_event(key=key, label=label, reason=reason, by="coordinator"),
        )
    finally:
        with contextlib.suppress(OSError):
            os.close(lock_fd)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "wfId": wf_id,
        "key": key,
        "reason": reason,
    }
    if label is not None:
        payload["label"] = label
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(f"rejected: {key}", file=stdout)
    return EXIT_OK


def _gate_matches(event: JsonObject, gate: str | None) -> bool:
    return gate is None or event.get("gateName") == gate or event.get("key") == gate


def _gate_event(events: list[JsonObject], gate_key: str, result_hash: object) -> JsonObject | None:
    latest: JsonObject | None = None
    for event in events:
        if (
            event.get("type") == "gate"
            and event.get("simulated") is not True
            and event.get("key") == gate_key
            and (not isinstance(result_hash, str) or event.get("gateResultHash") == result_hash)
        ):
            latest = event
    return latest


# A gate label that may be interpolated into a suggested approve command. The
# name is script-authored; anything else falls back to the journal key, which
# ``approve --gate`` also matches.
_GATE_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]*")


def _paused_gate_commands(
    root: Path, wf_id: str, gate_key: str, result_hash: object
) -> list[list[str]]:
    """Suggested commands for a paused gate, built from its declared actions.

    ``approve`` refuses a bare approval on a gate whose actions exclude
    ``approve``, so a suggestion that names no action can be a dead end. The
    pending gate's own list is suggested instead, one command per action; when
    the gate cannot be read from the journal, the events view is the honest
    fallback.
    """
    event = _gate_event(
        list(registry.iter_journal(root / registry.JOURNAL_FILE)),
        gate_key,
        result_hash,
    )
    if event is None:
        return [["workflow", "events", wf_id]]
    label = gate_key
    name = event.get("gateName")
    if isinstance(name, str) and _GATE_LABEL_RE.fullmatch(name):
        label = name
    commands: list[list[str]] = []
    for action in runtime._declared_gate_actions(event):
        if action == "approve":
            commands.append(["workflow", "approve", wf_id])
        else:
            commands.append(["workflow", "approve", wf_id, "--gate", label, "--action", action])
    commands.append(["workflow", "events", wf_id])
    return commands


def _approve_refusal_hint(root: Path, wf_id: str, status: object) -> str:
    """What to do instead of ``approve``, from the workflow's live status."""
    on_disk = status
    if on_disk in LIVE_WORKFLOW_STATUSES and not registry.supervisor_alive(root):
        on_disk = "stalled"
    failure: str | None = None
    if on_disk in {"failed", "stalled", "killed"}:
        events = list(registry.iter_journal(root / registry.JOURNAL_FILE))
        last = next(
            (
                e
                for e in reversed(events)
                if e.get("type") in status_reasons.FAILURE_EVENTS and e.get("simulated") is not True
            ),
            None,
        )
        failure = status_reasons.failure_summary(last) if last is not None else None
    return status_reasons.approve_refusal_hint(wf_id, on_disk, failure)


def _pending_gate_names(
    root: Path, approval: JsonObject | None, events: list[JsonObject]
) -> list[str]:
    names: list[str] = []
    for event in events:
        if event.get("type") != "gate" or event.get("simulated") is True:
            continue
        key = event.get("key")
        result_hash = event.get("gateResultHash")
        if not isinstance(key, str) or not isinstance(result_hash, str):
            continue
        if registry.approval_allows(root, key, result_hash, approval=approval or {}):
            continue
        name = event.get("gateName")
        label = name if isinstance(name, str) else key
        if label not in names:
            names.append(label)
    return names


def _gate_decision(
    choice: GateChoice | None, event: JsonObject | None, gate_key: str
) -> JsonObject | None:
    """Validate an approve choice against the gate's declared actions.

    Returns the decision fields to store, or None for a bare approval (which
    stores exactly what approvals always stored).
    """
    allowed = runtime._declared_gate_actions(event if isinstance(event, dict) else None)
    name = event.get("gateName") if isinstance(event, dict) else None
    label = name if isinstance(name, str) else gate_key
    action = choice.action if choice is not None else None
    if action is None:
        if "approve" not in allowed:
            raise DelegateError(
                "invalid_gate_action",
                f"Gate {label!r} requires --action; allowed actions: {', '.join(allowed)}.",
            )
    elif action not in allowed:
        raise DelegateError(
            "invalid_gate_action",
            f"Gate {label!r} does not allow action {action!r}; "
            f"allowed actions: {', '.join(allowed)}.",
        )
    if choice is None or (choice.action is None and choice.note is None and not choice.has_data):
        return None
    decision: JsonObject = {
        "action": action or "approve",
        "approvedAt": run_registry.utc_now_iso(),
    }
    if choice.note is not None:
        decision["note"] = choice.note
    if choice.has_data:
        decision["data"] = choice.data
    return decision


def _latest_unapproved_gate_event(
    root: Path,
    approval: JsonObject | None = None,
    *,
    events: list[JsonObject] | None = None,
    gate: str | None = None,
) -> JsonObject | None:
    approval = (
        approval if approval is not None else registry.read_json(root / registry.APPROVAL_FILE)
    )
    latest: JsonObject | None = None
    for event in (
        events if events is not None else registry.iter_journal(root / registry.JOURNAL_FILE)
    ):
        if (
            event.get("type") != "gate"
            or event.get("simulated") is True
            or event.get("dryRun") is True
        ):
            continue
        key = event.get("key")
        result_hash = event.get("gateResultHash")
        if (
            isinstance(key, str)
            and isinstance(result_hash, str)
            and _gate_matches(event, gate)
            and not registry.approval_allows(
                root,
                key,
                result_hash,
                approval=approval or {},
            )
        ):
            latest = event
    return latest


def _merge_cancelled(primary: list[JsonObject], extra: object) -> list[JsonObject]:
    """Union two cancelled-child lists by runId, primary order first."""
    merged = list(primary)
    seen = {item.get("runId") for item in merged if isinstance(item, dict)}
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict) and item.get("runId") not in seen:
                merged.append(item)
                seen.add(item.get("runId"))
    return merged


def emit_kill(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
    status = registry.read_json(root / registry.STATUS_FILE) or {}
    pid = status.get("supervisorPid")
    pgid = status.get("supervisorPgid")
    # Everything live before the signal is this kill's responsibility; the
    # list is completed from the registry afterwards because the supervisor
    # seals children on its own once signalled, and a forced escalation can
    # end it before its aggregate list reaches status.json.
    already_terminal = runtime.workflow_terminal_child_run_ids(workspace, command.wf_id or "")
    supervisor_signalled = False
    if isinstance(pid, int):
        supervisor_signalled = runtime.kill_supervisor(pid, pgid if isinstance(pgid, int) else None)
    cancelled = runtime.cancel_workflow_children(workspace, command.wf_id or "")
    # Child safe-mode workspaces are parent-owned during structured retries.
    # Reap every descriptor, including terminal runs that finished after the
    # supervisor was signalled and therefore were not in ``cancelled``.
    runtime.cleanup_workflow_agent_workspaces(workspace, command.wf_id or "")
    # Wait for the supervisor to release the workflow lock before merging
    # status=killed, so the dying supervisor cannot overwrite the kill status.
    supervisor_exited = runtime.wait_for_workflow_lock(
        root, timeout_seconds=runtime.KILL_SUPERVISOR_WAIT_SECONDS
    )
    if not supervisor_exited and isinstance(pid, int) and isinstance(pgid, int):
        runtime.kill_supervisor(pid, pgid, force=True)
        supervisor_exited = runtime.wait_for_workflow_lock(
            root, timeout_seconds=runtime.KILL_SUPERVISOR_FORCE_WAIT_SECONDS
        )
    # Re-read after supervisor exit so we merge against the final snapshot.
    status = registry.read_json(root / registry.STATUS_FILE) or status
    # A supervisor that handled the SIGTERM above cancelled its own children
    # before this command reached them. Fold its list in so the kill reports
    # every child that was stopped, and do not let the signal-induced failure
    # fields it wrote turn a kill into a failure.
    cancelled = _merge_cancelled(cancelled, status.get("cancelled"))
    cancelled = runtime.reconcile_cancelled_children(
        workspace, command.wf_id or "", already_terminal, cancelled
    )
    merged = dict(status)
    if supervisor_signalled and status.get("signal") == "SIGTERM":
        for key in ("signal", "signalsRepeated", "signalAfterCompletion"):
            merged.pop(key, None)
        # Only diagnostics the signal itself produced are cleared: a genuine
        # watchdog fire that preceded the kill keeps its error text and
        # reason. write_status restores an absent watchdogReason from disk,
        # so the signal-induced one is cleared with an explicit None.
        if status.get("error") == "supervisor received SIGTERM":
            merged.pop("error", None)
            merged.pop("traceback", None)
        reason = status.get("watchdogReason")
        if isinstance(reason, str) and reason.startswith("signal:"):
            merged["watchdogReason"] = None
    _append_command_event(root, "workflow_killed", cancelled=cancelled)
    merged.update(
        {
            "ok": False,
            "wfId": command.wf_id,
            "status": "killed",
            "killedAt": run_registry.utc_now_iso(),
            "cancelled": cancelled,
            "supervisorSignalled": supervisor_signalled,
            "supervisorExited": supervisor_exited,
        }
    )
    registry.write_status(root, merged)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "wfId": command.wf_id,
        "cancelled": cancelled,
    }
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(f"killed: {command.wf_id}", file=stdout)
    return EXIT_OK


def emit_list(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = registry.workflow_root(workspace)
    workflows: list[JsonObject] = []
    if root.exists():
        for child in sorted(root.iterdir()):
            if child.is_dir() and registry.WORKFLOW_ID_RE.fullmatch(child.name):
                status = registry.read_json(child / registry.STATUS_FILE) or {}
                view = _status_view(child, status)
                entry: JsonObject = {
                    "wfId": child.name,
                    "status": view.get("status"),
                    "decision": view["decision"],
                }
                if "statusOnDisk" in view:
                    entry["statusOnDisk"] = view["statusOnDisk"]
                workflows.append(entry)
    saved: list[str] = []
    saved_root = registry.user_workflow_root()
    if saved_root.exists():
        saved = [path.stem for path in sorted(saved_root.glob("*.py"))]
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "workflows": workflows,
        "saved": saved,
    }
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        for item in workflows:
            print(f"{item['wfId']} {item.get('status')}", file=stdout)
        for name in saved:
            print(f"saved {name}", file=stdout)
    return EXIT_OK


def emit_save(command: WorkflowCommand, *, stdout: TextIO) -> int:
    if command.script is None or command.name is None:
        raise DelegateError(
            "missing_workflow_save_args", "workflow save requires <script.py> --name <name>."
        )
    source = Path(command.script).expanduser().resolve()
    check_script(source)
    target = _saved_workflow_path(command.name)
    run_registry.ensure_private_dir(target.parent)
    shutil.copyfile(source, target)
    target.chmod(run_registry.PRIVATE_FILE_MODE)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "name": command.name,
        "path": str(target),
    }
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(f"saved: {target}", file=stdout)
    return EXIT_OK


def _resume_source_warnings(status: JsonObject) -> list[str]:
    """Compare the recorded source with the launch-time frozen hash.

    Resume always executes ``root/script.py``.  This read is only provenance:
    a changed or unavailable source must not alter the frozen execution path.
    """
    source_script = status.get("sourceScript")
    if not isinstance(source_script, str):
        # Workflows created before source provenance was recorded resume as-is.
        return []
    recorded_hash = status.get("scriptSha256")
    if not isinstance(recorded_hash, str):
        return [
            f"unable to compare source script {source_script!r}: recorded frozen hash is missing"
        ]
    try:
        current_hash = registry.script_sha256(Path(source_script).expanduser().read_bytes())
    except (OSError, ValueError):
        return [f"unable to compare source script {source_script!r} with frozen copy"]
    if current_hash == recorded_hash:
        return []
    return [
        f"source script differs from frozen copy: {source_script!r} "
        f"(current sha256 {current_hash}; recorded {recorded_hash})"
    ]


def _resume_frozen_script_warnings(status: JsonObject, script_path: Path) -> list[str]:
    """Warn when the frozen execution input no longer matches its recorded hash."""
    recorded_hash = status.get("scriptSha256")
    if not isinstance(recorded_hash, str):
        return []
    try:
        current_hash = registry.script_sha256(script_path.read_bytes())
    except OSError:
        return []
    if current_hash == recorded_hash:
        return []
    return [
        f"frozen script differs from recorded hash: {script_path} "
        f"(current sha256 {current_hash}; recorded {recorded_hash})"
    ]


def check_script(path: Path) -> workflow_script.CheckResult:
    try:
        source = workflow_script.read_script(path)
        return workflow_script.check_source(source, filename=str(path))
    except workflow_script.WorkflowScriptError as exc:
        raise DelegateError("invalid_workflow_script", str(exc)) from exc


def _script_path_for_command(command: WorkflowCommand) -> Path:
    if command.name:
        path = _saved_workflow_path(command.name)
    elif command.script:
        path = Path(command.script).expanduser()
    else:
        raise DelegateError(
            "missing_workflow_script", "workflow requires <script.py> or --name <name>."
        )
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise DelegateError("workflow_script_not_found", f"Workflow script not found: {path}")
    return path


def _workflow_dir_for_command(
    command: WorkflowCommand, workspace: Path, *, read_across_registries: bool = False
) -> Path:
    """The workflow's directory in this workspace, or in the one other Registry that has it.

    Only read-only actions pass ``read_across_registries``: a workflow id is
    globally unique, so the one workspace on the roster that holds it is read
    directly. Mutating actions fail with the exact ``--cwd`` command instead.
    """
    if command.wf_id is None:
        raise DelegateError("missing_workflow", f"workflow {command.action} requires <wfId>.")
    wf_id = _validate_wf_id(command.wf_id)
    root = registry.workflow_dir(workspace, wf_id)
    if not root.exists():
        matches = registry_roster.find_workflow(wf_id, exclude=workspace)
        if read_across_registries and len(matches) == 1:
            return registry.workflow_dir(matches[0].workspace, wf_id)
        raise _workflow_not_found(wf_id, workspace, command.action, matches)
    return root


def _resolved_elsewhere(root: Path, workspace: Path) -> str | None:
    """The workspace a workflow was read from when that is not the current one."""
    owner = root.parent.parent.parent
    return None if owner == workspace else str(owner)


def _resolve_wait_or_result(
    command: WorkflowCommand,
    workspace: Path,
    *,
    require_result: bool,
) -> tuple[Path, str, str | None]:
    if command.wf_id is not None:
        root = _workflow_dir_for_command(command, workspace, read_across_registries=True)
        return root, command.wf_id, None
    root = registry.latest_workflow_dir(
        workspace,
        require_result=require_result,
        exclude_dry_run=not require_result,
    )
    if root is None:
        if require_result:
            raise DelegateError("workflow_result_missing", "No workflow results found.")
        raise DelegateError("workflow_not_found", "No workflows available to wait for.")
    return root, root.name, "latest"


def _parse_args(raw: str | None) -> JsonValue:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DelegateError("invalid_workflow_args", "workflow --args must be valid JSON.") from exc


def _recover_interrupted_repin(root: Path, wf_id: str) -> None:
    """Undo a repin that no supervisor ever ran on.

    A repin keeps the pin it replaced in a backup, and only a supervisor
    running on the new pin removes it (durably, before its first step), so a
    backup found here means nothing ran on the repinned runtime: the resume
    failed, or its process or supervisor died first. It is put back under the
    workflow lock, and the journal, which may already say the workflow moved,
    gets the matching rollback record.
    """
    if not workflow_pinning.repin_backup_path(wf_id).exists():
        return
    lock_fd = _acquire_workflow_lock(root, wf_id)
    try:
        try:
            undone = workflow_pinning.rollback_repin(wf_id)
        except workflow_pinning.WorkflowPinError as exc:
            raise DelegateError(exc.error, exc.message) from exc
        if undone is not None and _repin_journaled_for(root, undone.get("abandonedDigest")):
            _append_command_event(root, "runtime_repin_rolled_back", reason="interrupted", **undone)
    finally:
        with contextlib.suppress(OSError):
            os.close(lock_fd)


def _repin_journaled_for(root: Path, digest: object) -> bool:
    """Whether the journal's latest repin record is an unretracted move to ``digest``."""
    latest: JsonObject | None = None
    for event in registry.iter_journal(root / registry.JOURNAL_FILE):
        if event.get("type") in {"runtime_repinned", "runtime_repin_rolled_back"}:
            latest = event
    return (
        latest is not None
        and latest.get("type") == "runtime_repinned"
        and latest.get("toDigest") == digest
    )


def _append_command_event(root: Path, event_type: str, **payload: JsonValue) -> None:
    # Command events are explicit operator actions, not script dry-run events;
    # emit_reject resolves simulated-only keys before reaching this writer.
    sequence = 0
    for event in registry.iter_journal(root / registry.JOURNAL_FILE):
        seq = event.get("seq")
        if isinstance(seq, int):
            sequence = max(sequence, seq)
    registry.append_jsonl(
        root / registry.JOURNAL_FILE,
        {"seq": sequence + 1, "type": event_type, "at": run_registry.utc_now_iso(), **payload},
    )


def _acquire_workflow_lock(root: Path, wf_id: str) -> int:
    # Retry briefly: the read-path stalled probe holds the flock for a moment,
    # and a resume racing that probe must not fail spuriously.
    for _ in range(3):
        try:
            return registry.acquire_workflow_lock(root)
        except BlockingIOError:
            time.sleep(0.05)
    try:
        return registry.acquire_workflow_lock(root)
    except BlockingIOError as exc:
        raise DelegateError("workflow_locked", f"Workflow is already running: {wf_id}") from exc


def _status_view(root: Path, payload: JsonObject) -> JsonObject:
    """Overlay stalled detection onto a status payload without mutating disk."""
    on_disk = payload.get("status")
    view = dict(payload)
    if on_disk in LIVE_WORKFLOW_STATUSES and not registry.supervisor_alive(root):
        view["status"] = "stalled"
        view["statusOnDisk"] = on_disk
        view["ok"] = False
    status = view.get("status")
    # The directory was selected through validated workflow targeting. Do not
    # interpolate a child-written status field into a suggested shell command.
    wf_id = root.name
    gate_key = view.get("gateKey")
    suggested: list[list[str]]
    if status == "paused" and isinstance(gate_key, str):
        suggested = _paused_gate_commands(root, wf_id, gate_key, view.get("gateResultHash"))
    elif status in {"stalled", "failed", "killed", "paused"}:
        suggested = [["workflow", "events", wf_id], ["workflow", "run", "--resume", wf_id]]
    elif status in LIVE_WORKFLOW_STATUSES:
        suggested = [["workflow", "wait", wf_id]]
    elif status in {"succeeded", "dry_run"}:
        suggested = [["workflow", "result", wf_id]]
    else:
        suggested = []
    actions = [
        shlex.join(["delegate", "--cwd", str(root.parent.parent.parent), *command])
        for command in suggested
    ]
    view["decision"] = {
        "status": status,
        "gate": {"key": view.get("gateKey"), "resultHash": view.get("gateResultHash")}
        if isinstance(view.get("gateKey"), str)
        else None,
        "error": view.get("error"),
        "budget": view.get("budget"),
        "nextActions": actions,
    }
    return view


def _validate_wf_id(wf_id: str) -> str:
    try:
        return registry.validate_workflow_id(wf_id)
    except ValueError as exc:
        raise DelegateError("invalid_workflow_id", str(exc)) from exc


def _saved_workflow_path(name: str) -> Path:
    try:
        return registry.saved_workflow_path(name)
    except ValueError as exc:
        raise DelegateError("invalid_workflow_name", str(exc)) from exc


def _run_tree(entries: list[JsonObject]) -> JsonObject:
    counts: dict[str, int] = {}
    phases: dict[str, int] = {}
    for entry in entries:
        engines = entry.get("engine")
        engine_label = ",".join(engines) if isinstance(engines, list) else str(engines)
        key = f"{engine_label}:{entry.get('mode')}"
        counts[key] = counts.get(key, 0) + 1
        phase = entry.get("phase")
        if isinstance(phase, str):
            phases[phase] = phases.get(phase, 0) + 1
    return {"calls": entries, "counts": counts, "phases": phases}
