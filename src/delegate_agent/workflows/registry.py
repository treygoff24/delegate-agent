from __future__ import annotations

import contextlib
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
# anchor), agent_adopted / agent_adopt_rejected / agent_timeout audit lines
# (adoption outcomes are resume anchors), and budget claims (idempotent-claim
# set must survive crashes — status.spent alone is not enough to skip re-claim).
# Phase/log ticks stay unfsynced. There is no agent_result emitter.
DURABLE_EVENT_TYPES = {
    "agent_started",
    "agent_finished",
    "agent_adopted",
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
    existing = read_json(root / STATUS_FILE)
    created_at = existing.get("createdAt") if isinstance(existing, dict) else None
    if not isinstance(created_at, str):
        candidate = payload.get("createdAt")
        created_at = candidate if isinstance(candidate, str) else run_registry.utc_now_iso()
    merged: JsonObject = {"schema": WORKFLOW_SCHEMA, **payload, "createdAt": created_at}
    created_ordinal = existing.get("createdOrdinal") if isinstance(existing, dict) else None
    if isinstance(created_ordinal, int) and not isinstance(created_ordinal, bool):
        merged["createdOrdinal"] = created_ordinal
    else:
        merged.pop("createdOrdinal", None)
    write_json(root / STATUS_FILE, merged)


def register_workflow(workspace: Path, root: Path, payload: JsonObject) -> None:
    """Write the first status with a registry-serialized creation ordinal."""
    registry_root = run_registry.registry_root(workspace)
    with run_registry.registry_lock(registry_root):
        # ponytail: O(n) registration scan; add a workflow index only if real
        # registries grow enough for this lock-held scan to become measurable.
        latest_ordinal = 0
        workflows = workflow_root(workspace)
        if workflows.exists():
            for child in workflows.iterdir():
                status = read_json(child / STATUS_FILE) if child.is_dir() else None
                ordinal = status.get("createdOrdinal") if isinstance(status, dict) else None
                if isinstance(ordinal, int) and not isinstance(ordinal, bool):
                    latest_ordinal = max(latest_ordinal, ordinal)
        created_at = payload.get("createdAt")
        if not isinstance(created_at, str):
            created_at = run_registry.utc_now_iso()
        merged: JsonObject = {
            "schema": WORKFLOW_SCHEMA,
            **payload,
            "createdAt": created_at,
            "createdOrdinal": latest_ordinal + 1,
        }
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


def supervisor_alive(root: Path) -> bool:
    """Return True if the workflow lock is held (supervisor still alive).

    Non-blocking probe: if the lock is acquirable, the supervisor is dead —
    release immediately so the read path never retains the lock.
    """
    path = root / LOCK_FILE
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        # Unexpected probe failure (EMFILE, permissions drift): fail toward
        # "alive" so a transient error never fabricates a stalled overlay.
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return True
    else:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def latest_workflow_dir(
    workspace: Path,
    *,
    require_result: bool = False,
    exclude_dry_run: bool = False,
) -> Path | None:
    root = workflow_root(workspace)
    if not root.exists():
        return None
    ordered: list[tuple[int, Path]] = []
    legacy: list[tuple[str, str, str, Path]] = []
    for child in root.iterdir():
        if not child.is_dir() or not WORKFLOW_ID_RE.fullmatch(child.name):
            continue
        status = read_json(child / STATUS_FILE)
        if status is None:
            continue
        if require_result and not (child / RESULT_FILE).is_file():
            continue
        if exclude_dry_run and status.get("status") == "dry_run":
            continue
        ordinal = status.get("createdOrdinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            ordered.append((ordinal, child))
            continue
        created_at = status.get("createdAt")
        updated_at = status.get("updatedAt")
        legacy.append(
            (
                created_at if isinstance(created_at, str) else "",
                updated_at if isinstance(updated_at, str) else "",
                child.name,
                child,
            )
        )
    if ordered:
        return max(ordered, key=lambda item: item[0])[1]
    return max(legacy)[3] if legacy else None


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
