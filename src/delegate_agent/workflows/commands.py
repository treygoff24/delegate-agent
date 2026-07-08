from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from delegate_agent import rendering, run_registry
from delegate_agent.errors import EXIT_OK, DelegateError
from delegate_agent.json_types import JsonObject
from delegate_agent.workflows import registry, runtime
from delegate_agent.workflows import script as workflow_script

WORKFLOW_COMMAND_SCHEMA = "delegate.workflow-command.v1"
TERMINAL_WORKFLOW_STATUSES = {"succeeded", "failed", "killed"}
WAIT_DONE_WORKFLOW_STATUSES = TERMINAL_WORKFLOW_STATUSES | {"paused"}


def _delegate_cli_argv() -> list[str]:
    return [sys.executable, str(Path(sys.argv[0]).resolve())]


@dataclass(frozen=True)
class WorkflowCommand:
    action: str
    script: str | None = None
    wf_id: str | None = None
    args_json: str | None = None
    budget: int | None = None
    dry_run: bool = False
    resume: str | None = None
    name: str | None = None
    since: int = 0
    timeout: int | None = None
    json_mode: bool = False


def emit(
    command: WorkflowCommand,
    *,
    workspace_path: str,
    config: JsonObject,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    workspace = Path(workspace_path)
    action = command.action
    if action == "check":
        return emit_check(command, stdout=stdout)
    if action == "run":
        return emit_run(command, workspace=workspace, config=config, stdout=stdout, stderr=stderr)
    if action == "_supervise":
        if command.wf_id is None:
            raise DelegateError("missing_workflow", "workflow _supervise requires <wfId>.")
        return runtime.run_supervisor(
            workspace=workspace,
            wf_id=command.wf_id,
            cli_argv=_delegate_cli_argv(),
            config=config,
        )
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
        return emit_approve(command, workspace=workspace, config=config, stdout=stdout)
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
) -> int:
    if command.resume:
        wf_id = _validate_wf_id(command.resume)
        root = registry.workflow_dir(workspace, wf_id)
        if not root.exists():
            raise DelegateError("workflow_not_found", f"Workflow not found: {wf_id}")
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        gate_key = status.get("gateKey") if isinstance(status, dict) else None
        if status.get("status") == "paused" and isinstance(gate_key, str):
            registry.write_json(
                root / registry.APPROVAL_FILE, {"approved": True, "gateKey": gate_key}
            )
        script_path = root / registry.SCRIPT_FILE
        args_value = status.get("args") if isinstance(status, dict) else None
        budget_total = command.budget
        if budget_total is None:
            budget_payload = status.get("budget") if isinstance(status, dict) else None
            if isinstance(budget_payload, dict) and isinstance(budget_payload.get("total"), int):
                budget_total = budget_payload["total"]
        else:
            budget_payload = status.get("budget") if isinstance(status, dict) else None
            spent = budget_payload.get("spent") if isinstance(budget_payload, dict) else 0
            spent = spent if isinstance(spent, int) and spent >= 0 else 0
            status["budget"] = {
                "total": budget_total,
                "spent": spent,
                "remaining": max(budget_total - spent, 0),
            }
            registry.write_status(root, status)
    else:
        source = _script_path_for_command(command)
        check_script(source)
        wf_id = registry.generate_workflow_id()
        root = registry.ensure_workflow_dir(workspace, wf_id)
        data = source.read_bytes()
        script_path = root / registry.SCRIPT_FILE
        run_registry.write_private_bytes(script_path, data)
        args_value = _parse_args(command.args_json)
        budget_total = command.budget
        registry.write_json(root / registry.ARGS_FILE, {"args": args_value})
        registry.write_status(
            root,
            {
                "ok": True,
                "wfId": wf_id,
                "status": "created",
                "workspace": str(workspace),
                "scriptPath": str(script_path),
                "journalPath": str(root / registry.JOURNAL_FILE),
                "resultPath": str(root / registry.RESULT_FILE),
                "scriptSha256": registry.script_sha256(data),
                "args": args_value,
                "budget": {"total": budget_total, "spent": 0, "remaining": budget_total},
            },
        )
    if command.dry_run:
        return emit_dry_run(
            wf_id=wf_id,
            root=root,
            script_path=script_path,
            workspace=workspace,
            config=config,
            args_value=args_value,
            budget_total=budget_total,
            json_mode=command.json_mode,
            stdout=stdout,
        )
    supervisor_argv = [
        *_delegate_cli_argv(),
        "--cwd",
        str(workspace),
        "workflow",
        "_supervise",
        wf_id,
    ]
    runtime.detach_supervisor(supervisor_argv, cwd=workspace)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "wfId": wf_id,
        "journalPath": str(root / registry.JOURNAL_FILE),
        "scriptPath": str(script_path),
    }
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(f"wfId: {wf_id}", file=stdout)
        print(f"journalPath: {payload['journalPath']}", file=stdout)
        print(f"scriptPath: {payload['scriptPath']}", file=stdout)
    _ = stderr
    return EXIT_OK


def emit_dry_run(
    *,
    wf_id: str,
    root: Path,
    script_path: Path,
    workspace: Path,
    config: JsonObject,
    args_value: Any,
    budget_total: int | None,
    json_mode: bool,
    stdout: TextIO,
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
    )
    result = runtime.execute_workflow(state)
    tree = _run_tree(state.dry_runs)
    payload: JsonObject = {
        "ok": True,
        "schema": WORKFLOW_COMMAND_SCHEMA,
        "dryRun": True,
        "wfId": wf_id,
        "runTree": tree,
        "result": result,
    }
    if json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(json.dumps(tree, indent=2, sort_keys=True), file=stdout)
    return EXIT_OK


def emit_status(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
    payload = registry.read_json(root / registry.STATUS_FILE)
    if payload is None:
        raise DelegateError("workflow_not_found", f"Workflow status not found: {command.wf_id}")
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(f"{payload.get('wfId')}: {payload.get('status')}", file=stdout)
        print(f"journalPath: {payload.get('journalPath')}", file=stdout)
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
    collected: list[dict[str, Any]] = []
    while True:
        events = [
            event
            for event in registry.iter_journal(root / registry.JOURNAL_FILE)
            if event.get("seq", 0) > since
        ]
        for event in events:
            seq = event.get("seq")
            if isinstance(seq, int):
                since = max(since, seq)
            if command.json_mode:
                collected.append(event)
            else:
                print(json.dumps(event, sort_keys=True), file=stdout, flush=True)
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        if status.get("status") in WAIT_DONE_WORKFLOW_STATUSES:
            break
        time.sleep(1)
    if command.json_mode:
        rendering.print_json(
            {
                "ok": True,
                "schema": WORKFLOW_COMMAND_SCHEMA,
                "events": collected,
                "lastSeq": since,
            },
            stdout,
        )
    return EXIT_OK


def emit_result(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
    payload = registry.read_json(root / registry.RESULT_FILE)
    if payload is None:
        raise DelegateError(
            "workflow_result_missing", f"Workflow result not found: {command.wf_id}"
        )
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(json.dumps(payload.get("result"), indent=2, sort_keys=True), file=stdout)
    return EXIT_OK


def emit_wait(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
    deadline = time.monotonic() + (command.timeout or 3600)
    payload: JsonObject | None = None
    while True:
        payload = registry.read_json(root / registry.STATUS_FILE)
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
    if command.json_mode:
        rendering.print_json(result, stdout)
    else:
        print(f"{command.wf_id}: {(payload or {}).get('status', 'unknown')}", file=stdout)
    if timed_out:
        return 124
    return 0 if result["ok"] else 1


def emit_approve(
    command: WorkflowCommand,
    *,
    workspace: Path,
    config: JsonObject,
    stdout: TextIO,
) -> int:
    root = _workflow_dir_for_command(command, workspace)
    status = registry.read_json(root / registry.STATUS_FILE) or {}
    gate_key = status.get("gateKey")
    if not isinstance(gate_key, str):
        raise DelegateError(
            "workflow_not_gated", f"Workflow is not waiting on a gate: {command.wf_id}"
        )
    registry.write_json(root / registry.APPROVAL_FILE, {"approved": True, "gateKey": gate_key})
    resumed = WorkflowCommand("run", resume=command.wf_id, json_mode=command.json_mode)
    return emit_run(resumed, workspace=workspace, config=config, stdout=stdout, stderr=stdout)


def emit_kill(command: WorkflowCommand, *, workspace: Path, stdout: TextIO) -> int:
    root = _workflow_dir_for_command(command, workspace)
    status = registry.read_json(root / registry.STATUS_FILE) or {}
    pid = status.get("supervisorPid")
    if isinstance(pid, int):
        runtime.kill_supervisor(pid)
    cancelled = runtime.cancel_workflow_children(workspace, command.wf_id or "")
    _append_command_event(root, "workflow_killed", cancelled=cancelled)
    registry.write_status(
        root, {"ok": False, "wfId": command.wf_id, "status": "killed", "cancelled": cancelled}
    )
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
                workflows.append({"wfId": child.name, "status": status.get("status")})
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


def check_script(path: Path) -> workflow_script.CheckResult:
    source = workflow_script.read_script(path)
    return workflow_script.check_source(source, filename=str(path))


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


def _parse_args(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DelegateError("invalid_workflow_args", "workflow --args must be valid JSON.") from exc


def _append_command_event(root: Path, event_type: str, **payload: Any) -> None:
    sequence = 0
    for event in registry.iter_journal(root / registry.JOURNAL_FILE):
        seq = event.get("seq")
        if isinstance(seq, int):
            sequence = max(sequence, seq)
    registry.append_jsonl(
        root / registry.JOURNAL_FILE,
        {"seq": sequence + 1, "type": event_type, "at": run_registry.utc_now_iso(), **payload},
    )


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


def _run_tree(entries: list[dict[str, Any]]) -> JsonObject:
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
