from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent import private_io, rendering, run_registry, workflow_attempts, workflow_pinning
from delegate_agent.errors import EXIT_OK, DelegateError
from delegate_agent.isolation import worktrees_data_home
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.workflows import registry, runtime
from delegate_agent.workflows import script as workflow_script

workflow_pinning.require_pinned_persona_resolver()

WORKFLOW_COMMAND_SCHEMA = "delegate.workflow-command.v1"
TERMINAL_WORKFLOW_STATUSES = {"succeeded", "failed", "killed"}
WAIT_DONE_WORKFLOW_STATUSES = TERMINAL_WORKFLOW_STATUSES | {"dry_run", "paused", "stalled"}
LIVE_WORKFLOW_STATUSES = {"created", "running", "starting"}
DRY_RUN_WRITE_WARNING = (
    "dry-run only stubs agent calls; script filesystem writes are live. "
    "State-writing scripts must branch on dry_run/is_dry_run or run from a disposable checkout."
)


def _delegate_cli_argv() -> list[str]:
    return [sys.executable, str(Path(sys.argv[0]).resolve())]


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
            pin = workflow_pinning.load_pin(command.wf_id)
            attempt = workflow_attempts.from_environment(pin=pin)
            if pin is not None and pin.attempt_config_version == 1 and attempt is None:
                raise workflow_pinning.WorkflowPinError(
                    "invalid_workflow_attempt",
                    "this pinned supervisor requires an attempt snapshot",
                )
            if pin is not None:
                previous_environment = workflow_pinning.temporarily_apply_environment(
                    pin, attempt=attempt
                )
                cli_argv = pin.cli_argv
                launch_config = attempt.config if attempt is not None else pin.config
            else:
                previous_environment = {}
                cli_argv = _delegate_cli_argv()
                launch_config = config
            return runtime.run_supervisor(
                workspace=workspace,
                wf_id=command.wf_id,
                cli_argv=cli_argv,
                config=launch_config,
                attempt_config=attempt.metadata if attempt is not None else None,
                attempt_environment={**pin.environment, **attempt.environment}
                if pin is not None and attempt is not None
                else None,
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
) -> int:
    warnings: list[str] = []
    operational_environment = {
        key: value for key, value in os.environ.copy().items() if key in workflow_attempts.ENV_KEYS
    }
    lock_fd: int | None = None
    previous_status: JsonObject | None = None
    previous_result: bytes | None = None
    previous_result_exists = False
    previous_approval: str | None = None
    approval_changed = False

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
    if command.resume:
        wf_id = _validate_wf_id(command.resume)
        root = registry.workflow_dir(workspace, wf_id)
        if not root.exists():
            raise DelegateError("workflow_not_found", f"Workflow not found: {wf_id}")
        try:
            pin = workflow_pinning.load_pin(wf_id)
            if pin is None:
                prior = registry.read_json(root / registry.STATUS_FILE) or {}
                recorded = prior.get("attemptConfig")
                identity_bound = isinstance(recorded, dict) and isinstance(
                    recorded.get("baseProfileIdentityDigest"), str
                )
                if not identity_bound:
                    identity_bound = any(
                        event.get("type") == "attempt_config"
                        and isinstance(event.get("baseProfileIdentityDigest"), str)
                        for event in registry.iter_journal(root / registry.JOURNAL_FILE)
                    )
                if identity_bound:
                    raise workflow_pinning.WorkflowPinError(
                        "invalid_pin",
                        "identity-bound workflow pin is missing; restore its original HOME and pin before resuming",
                    )
            if pin is not None and pin.attempt_config_version == 1:
                if not command.dry_run:
                    attempt = workflow_attempts.create(
                        pin,
                        workflow_attempts.prepare(
                            pin, config, config_source, environment=operational_environment
                        ),
                    )
            elif pin is not None:
                warnings.append(
                    "operational updates unavailable: this legacy pinned runtime uses frozen creation config"
                )
            if pin is not None and pin.profile_identity is None:
                warnings.append(
                    "credential profile selection is not pinned by this legacy pin; selector identity enforcement is unavailable"
                )
        except (workflow_pinning.WorkflowPinError, delegate_config.ConfigError) as exc:
            raise DelegateError(exc.error, exc.message) from exc
        # Acquire the lock before any approval/budget mutation so a failed
        # resume cannot clobber a live supervisor's status.json.
        lock_fd = _acquire_workflow_lock(root, wf_id)
        try:
            status = registry.read_json(root / registry.STATUS_FILE) or {}
            previous_status = dict(status)
            if approve_gate:
                # Recover gate evidence only after acquiring the supervisor
                # lock; an approval racing a draining supervisor must not
                # publish a paused projection before its resume is admitted.
                recovered = _latest_unapproved_gate_event(root)
                gate_key = recovered.get("key") if recovered else status.get("gateKey")
                if not isinstance(gate_key, str):
                    raise DelegateError(
                        "workflow_not_gated", f"Workflow is not waiting on a gate: {wf_id}"
                    )
                status = dict(status)
                status.update({"status": "paused", "gateKey": gate_key})
                if recovered:
                    status["gateResult"] = recovered.get("result")
                    status["gateResultHash"] = recovered.get("gateResultHash")
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
            if status.get("status") == "paused" and isinstance(gate_key, str):
                gate_result_hash = status.get("gateResultHash")
                try:
                    previous_approval = private_io.read_private_text_bounded(
                        root / registry.APPROVAL_FILE,
                        max_bytes=private_io.PRIVATE_RECORD_READ_MAX_BYTES,
                    )
                except private_io.BoundedReadError as exc:
                    if exc.reason != "not_found":
                        raise DelegateError("invalid_workflow_approval", str(exc)) from exc
                approval_changed = True
                registry.record_approval(
                    root,
                    gate_key,
                    gate_result_hash if isinstance(gate_result_hash, str) else None,
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
                registry.write_status(root, status)
        except BaseException:
            try:
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
        wf_id = registry.generate_workflow_id()
        root = registry.ensure_workflow_dir(workspace, wf_id)
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
                # New workflow launches use v2 keys.  A resumed workflow keeps
                # its existing version (missing means legacy v1).
                "workflowKeyVersion": 2,
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
        )
    if lock_fd is None:
        lock_fd = _acquire_workflow_lock(root, wf_id)
    supervisor_argv = [
        *(pin.cli_argv if pin is not None else _delegate_cli_argv()),
        "--cwd",
        str(workspace),
        "workflow",
        "_supervise",
        wf_id,
    ]
    try:
        if pin is not None and pin.profile_identity is None:
            _append_command_event(root, "profile_identity_unavailable", reason="legacy_pin")
        if attempt is not None:
            _append_command_event(root, "attempt_config", **attempt.metadata)
            current_status = registry.read_json(root / registry.STATUS_FILE) or {}
            current_status["attemptConfig"] = attempt.metadata
            registry.write_status(root, current_status)
            if command.resume:
                status["attemptConfig"] = attempt.metadata
        elif pin is not None:
            _append_command_event(
                root, "attempt_config_unavailable", reason="legacy_pinned_runtime"
            )
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
        if pin is not None:
            workflow_pinning.register_active_supervisor(
                wf_id,
                workflow_root=root,
                workspace=workspace,
                pin=pin,
            )
        previous_environment = (
            workflow_pinning.temporarily_apply_environment(pin, attempt=attempt)
            if pin is not None
            else {}
        )
        try:
            runtime.detach_supervisor(supervisor_argv, cwd=workspace, lock_fd=lock_fd)
        finally:
            if previous_environment:
                workflow_pinning.restore_environment(previous_environment)
    except BaseException:
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
    if pin is not None:
        payload["profileIdentityPinned"] = pin.profile_identity is not None
        if pin.profile_identity is not None:
            payload["profileIdentity"] = pin.profile_identity
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
        workflow_key_version=2,
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
    worker.start()
    worker.join(timeout_seconds)
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


def emit_status(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
    payload = registry.read_json(root / registry.STATUS_FILE)
    if payload is None:
        raise DelegateError("workflow_not_found", f"Workflow status not found: {command.wf_id}")
    view = _status_view(root, payload)
    if command.json_mode:
        rendering.print_json(view, stdout)
    else:
        print(f"{view.get('wfId')}: {view.get('status')}", file=stdout)
        print(f"journalPath: {view.get('journalPath')}", file=stdout)
        if view.get("status") == "stalled":
            print(
                f"supervisor dead; resume with: workflow run --resume {view.get('wfId')}",
                file=stdout,
            )
    return EXIT_OK


def emit_events(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
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
    root = _workflow_dir_for_command(command, workspace)
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
            },
            stdout,
        )
    elif stalled:
        print(
            f"stalled: supervisor dead; resume with: workflow run --resume {command.wf_id}",
            file=stdout,
        )
    return 1 if stalled else EXIT_OK


def emit_result(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root, wf_id, resolution_kind = _resolve_wait_or_result(command, workspace, require_result=True)
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
    if command.json_mode:
        rendering.print_json(result, stdout)
    else:
        print(f"{wf_id}: {(payload or {}).get('status', 'unknown')}", file=stdout)
        if status == "stalled":
            print(
                f"supervisor dead; resume with: workflow run --resume {wf_id}",
                file=stdout,
            )
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
    resumed = WorkflowCommand("run", resume=command.wf_id, json_mode=command.json_mode)
    result = emit_run(
        resumed,
        workspace=workspace,
        config=config,
        stdout=stdout,
        stderr=stdout,
        approve_gate=True,
        config_source=config_source,
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
            workflow_key_version=(
                status.get("workflowKeyVersion")
                if status.get("workflowKeyVersion") in {1, 2}
                else 1
            ),
        )
        try:
            key, label = state.resolve_agent_key(key_or_label)
        except ValueError as exc:
            raise DelegateError("workflow_reject_unresolved", str(exc)) from exc
        event: JsonObject = {"key": key, "reason": reason}
        if label is not None:
            event["label"] = label
        _append_command_event(root, "agent_rejected", **event)
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


def _latest_unapproved_gate_event(root: Path) -> JsonObject | None:
    latest: JsonObject | None = None
    for event in registry.iter_journal(root / registry.JOURNAL_FILE):
        if event.get("type") != "gate":
            continue
        key = event.get("key")
        result_hash = event.get("gateResultHash")
        if isinstance(key, str) and not registry.approval_allows(
            root,
            key,
            result_hash if isinstance(result_hash, str) else None,
        ):
            latest = event
    return latest


def emit_kill(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
    status = registry.read_json(root / registry.STATUS_FILE) or {}
    pid = status.get("supervisorPid")
    pgid = status.get("supervisorPgid")
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
    _append_command_event(root, "workflow_killed", cancelled=cancelled)
    # Re-read after supervisor exit so we merge against the final snapshot.
    status = registry.read_json(root / registry.STATUS_FILE) or status
    merged = dict(status)
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


def _workflow_dir_for_command(command: WorkflowCommand, workspace: Path) -> Path:
    if command.wf_id is None:
        raise DelegateError("missing_workflow", f"workflow {command.action} requires <wfId>.")
    root = registry.workflow_dir(workspace, _validate_wf_id(command.wf_id))
    if not root.exists():
        raise DelegateError("workflow_not_found", f"Workflow not found: {command.wf_id}")
    return root


def _resolve_wait_or_result(
    command: WorkflowCommand,
    workspace: Path,
    *,
    require_result: bool,
) -> tuple[Path, str, str | None]:
    if command.wf_id is not None:
        root = _workflow_dir_for_command(command, workspace)
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


def _append_command_event(root: Path, event_type: str, **payload: JsonValue) -> None:
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
    actions: list[str] = []
    if status == "paused" and isinstance(view.get("gateKey"), str):
        actions = [f"workflow approve {wf_id}", f"workflow events {wf_id}"]
    elif status in {"stalled", "failed", "killed", "paused"}:
        actions = [f"workflow events {wf_id}", f"workflow run --resume {wf_id}"]
    elif status in LIVE_WORKFLOW_STATUSES:
        actions = [f"workflow wait {wf_id}"]
    elif status in {"succeeded", "dry_run"}:
        actions = [f"workflow result {wf_id}"]
    actions = [
        shlex.join(["delegate", "--cwd", str(root.parent.parent.parent), *action.split()])
        for action in actions
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
