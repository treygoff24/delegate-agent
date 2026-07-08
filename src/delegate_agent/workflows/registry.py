from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import warnings
from pathlib import Path
from typing import Any

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject
from delegate_agent.workflows import WORKFLOW_SCHEMA

WORKFLOW_ID_PREFIX = "wf_"
SCRIPT_FILE = "script.py"
JOURNAL_FILE = "journal.jsonl"
STATUS_FILE = "status.json"
RESULT_FILE = "result.json"
ARGS_FILE = "args.json"
APPROVAL_FILE = "approval.json"
LOCK_FILE = "workflow.lock"
WORKFLOW_ID_HEX = 12
WORKFLOW_ID_RE = __import__("re").compile(r"^wf_[0-9a-f]{12}$")
# Durable events are fsynced: result events, agent_started (resume adoption
# anchor), adopt/timeout audit lines, and budget claims (idempotent-claim set
# must survive crashes — status.spent alone is not enough to skip re-claim).
# Phase/log ticks stay unfsynced.
DURABLE_EVENT_TYPES = {
    "agent_started",
    "agent_finished",
    "agent_result",
    "agent_adopt_rejected",
    "agent_timeout",
    "budget",
    "gate",
    "workflow_finished",
}


def workflow_root(workspace: Path) -> Path:
    return run_registry.delegate_root(workspace) / "workflows"


def user_workflow_root() -> Path:
    return Path.home() / ".delegate" / "workflows"


def generate_workflow_id() -> str:
    return f"{WORKFLOW_ID_PREFIX}{secrets.token_hex(WORKFLOW_ID_HEX // 2)}"


def validate_workflow_id(wf_id: str) -> str:
    if not WORKFLOW_ID_RE.fullmatch(wf_id):
        raise ValueError(f"invalid workflow id: {wf_id}")
    return wf_id


def workflow_dir(workspace: Path, wf_id: str) -> Path:
    return workflow_root(workspace) / validate_workflow_id(wf_id)


def ensure_workflow_dir(workspace: Path, wf_id: str) -> Path:
    root = workflow_dir(workspace, wf_id)
    run_registry.ensure_private_dir(root)
    return root


def script_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload: JsonObject) -> None:
    run_registry.write_json_atomic(path, payload)


def read_json(path: Path) -> JsonObject | None:
    return run_registry.read_json_object_or_none(path)


def write_status(root: Path, payload: JsonObject) -> None:
    merged: JsonObject = {"schema": WORKFLOW_SCHEMA, **payload}
    write_json(root / STATUS_FILE, merged)


def write_result(root: Path, payload: JsonObject) -> None:
    merged: JsonObject = {"schema": WORKFLOW_SCHEMA, **payload}
    write_json(root / RESULT_FILE, merged)


def acquire_workflow_lock(root: Path) -> int:
    run_registry.ensure_private_dir(root)
    path = root / LOCK_FILE
    fd = os.open(path, os.O_CREAT | os.O_RDWR, run_registry.PRIVATE_FILE_MODE)
    run_registry.ensure_private_file(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    return fd


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, run_registry.PRIVATE_FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        if event.get("type") in DURABLE_EVENT_TYPES:
            handle.flush()
            os.fsync(handle.fileno())


def iter_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    final_line_complete = text.endswith("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not final_line_complete:
                warnings.warn(
                    f"Ignoring truncated final workflow journal line in {path}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                break
            raise
        if isinstance(value, dict):
            events.append(value)
    return events


def saved_workflow_path(name: str) -> Path:
    clean = name.strip()
    if not clean or "/" in clean or clean.startswith(".") or "\\" in clean:
        raise ValueError("workflow name must be a simple file stem")
    if not clean.endswith(".py"):
        clean += ".py"
    return user_workflow_root() / clean
