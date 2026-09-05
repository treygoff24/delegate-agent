from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import warnings
from collections.abc import Iterator
from pathlib import Path

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
# anchor), agent_child (public child run identity), agent_adopted /
# agent_adopt_rejected / agent_timeout audit lines (adoption outcomes are
# resume anchors), reject and retry audit lines, and budget claims
# (idempotent-claim set must survive crashes — status.spent alone is not
# enough to skip re-claim).
# Phase/log ticks stay unfsynced. There is no agent_result emitter.
DURABLE_EVENT_TYPES = {
    "agent_started",
    "agent_child",
    "agent_finished",
    "agent_adopted",
    "agent_adopt_rejected",
    "agent_timeout",
    "agent_rejected",
    "agent_retry",
    "agent_structured_retry",
    "agent_structured_exhausted",
    "workflow_watchdog_fired",
    "budget",
    "gate",
    "item_parked",
    "item_unparked",
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


def record_approval(root: Path, gate_key: str, result_hash: str | None = None) -> JsonObject:
    """Approve ``gate_key`` without forgetting earlier approvals.

    A resume replays the whole script, so every gate the run already passed
    fires again with the same deterministic key; if the file only held the
    latest key or result, approving gate N would re-pause the run at gate N-1.
    """
    path = root / APPROVAL_FILE
    previous = read_json(path) or {}
    keys = [key for key in previous.get("approvedKeys", []) if isinstance(key, str)]
    earlier = previous.get("gateKey")
    if isinstance(earlier, str) and earlier not in keys:
        keys.append(earlier)
    if gate_key not in keys:
        keys.append(gate_key)
    payload: JsonObject = {"approved": True, "gateKey": gate_key, "approvedKeys": keys}
    approved_results: list[JsonObject] = []
    previous_results = previous.get("approvedResults")
    if isinstance(previous_results, list):
        for record in previous_results:
            if (
                isinstance(record, dict)
                and isinstance(record.get("key"), str)
                and isinstance(record.get("resultHash"), str)
            ):
                approved_results.append(dict(record))
    if result_hash is not None and not any(
        record.get("key") == gate_key and record.get("resultHash") == result_hash
        for record in approved_results
    ):
        approved_results.append({"key": gate_key, "resultHash": result_hash})
    if approved_results:
        payload["approvedResults"] = approved_results
    write_json(path, payload)
    return payload


def approval_allows(root: Path, gate_key: str, result_hash: str | None = None) -> bool:
    payload = read_json(root / APPROVAL_FILE)
    if not isinstance(payload, dict) or payload.get("approved") is not True:
        return False
    approved_results = payload.get("approvedResults")
    matching_records = (
        [
            record
            for record in approved_results
            if isinstance(record, dict)
            and record.get("key") == gate_key
            and isinstance(record.get("resultHash"), str)
        ]
        if isinstance(approved_results, list)
        else []
    )
    if matching_records:
        return result_hash is not None and any(
            record.get("resultHash") == result_hash for record in matching_records
        )
    if payload.get("gateKey") == gate_key:
        return True
    keys = payload.get("approvedKeys")
    return isinstance(keys, list) and gate_key in keys


def read_json(path: Path) -> JsonObject | None:
    return run_registry.read_json_object_or_none(path)


def write_status(root: Path, payload: JsonObject) -> None:
    existing = read_json(root / STATUS_FILE)
    created_at = existing.get("createdAt") if isinstance(existing, dict) else None
    if not isinstance(created_at, str):
        candidate = payload.get("createdAt")
        created_at = candidate if isinstance(candidate, str) else run_registry.utc_now_iso()
    merged: JsonObject = {"schema": WORKFLOW_SCHEMA, **payload, "createdAt": created_at}
    if isinstance(existing, dict):
        for key in (
            "scriptSha256",
            "sourceScript",
            "args",
            "watchdogFiredAt",
            "watchdogReason",
            "watchdogCancelRequested",
        ):
            if key not in merged and key in existing:
                merged[key] = existing[key]
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
    fd = run_registry.open_private_file(path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    return fd


def supervisor_alive(root: Path) -> bool:
    """Return True for a held lock or an inconclusive probe.

    A missing lock or an acquirable regular lock establishes no live owner.
    The read-only probe never retains a lock or repairs filesystem metadata.
    """
    path = root / LOCK_FILE
    try:
        fd = run_registry.open_private_file(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        # Unexpected probe failure (EMFILE, permissions drift): fail toward
        # "alive" so a transient error never fabricates a stalled overlay.
        return True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return True
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


def append_jsonl(path: Path, event: JsonObject) -> None:
    fd = run_registry.open_private_file(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        # Key order is part of the data: scripts embed prior results via repr()
        # in later prompts, and the agent cache key hashes that prompt. A
        # sorted replay would miss every downstream key after a resume.
        handle.write(json.dumps(event) + "\n")
        if event.get("type") in DURABLE_EVENT_TYPES:
            handle.flush()
            os.fsync(handle.fileno())


def iter_journal(path: Path) -> list[JsonObject]:
    if not path.exists():
        return []
    events: list[JsonObject] = []
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


class JournalReader:
    """Tail complete JSONL records without retaining already consumed events.

    An inode change, shrink, or changed boundary bytes restarts the cursor.
    Callers retaining a sequence watermark decide whether replayed rows should
    be emitted. An incomplete final line is retried on the next poll.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.identity: tuple[int, int] | None = None
        self.anchor = b""
        self.pending = bytearray()

    def read_events(self, *, final: bool = False) -> Iterator[JsonObject]:
        try:
            handle = self.path.open("rb")
        except FileNotFoundError:
            self.offset = 0
            self.identity = None
            self.anchor = b""
            self.pending.clear()
            return
        with handle:
            metadata = os.fstat(handle.fileno())
            identity = (metadata.st_dev, metadata.st_ino)
            reset = identity != self.identity or metadata.st_size < self.offset
            if not reset and self.anchor:
                handle.seek(self.offset - len(self.anchor))
                reset = handle.read(len(self.anchor)) != self.anchor
            if reset:
                self.offset = 0
                self.anchor = b""
                self.pending.clear()
            self.identity = identity
            handle.seek(self.offset)
            while handle.tell() < metadata.st_size:
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    self.pending.extend(line)
                    self.offset = handle.tell()
                    self.anchor = (self.anchor + line)[-64:]
                    break
                # Decode before advancing, so malformed complete records remain
                # errors rather than being silently discarded on another poll.
                complete = self.pending + line if self.pending else line
                value = json.loads(complete.decode("utf-8")) if complete.strip() else None
                self.pending.clear()
                self.offset = handle.tell()
                self.anchor = (self.anchor + line)[-64:]
                if isinstance(value, dict):
                    yield value
            if final and self.pending:
                # A settled/dead writer may have emitted complete JSON but not
                # its newline. Match the batch reader; active readers still wait.
                try:
                    value = json.loads(self.pending.decode("utf-8"))
                except json.JSONDecodeError:
                    warnings.warn(
                        f"Ignoring truncated final workflow journal line in {self.path}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                else:
                    self.pending.clear()
                    if isinstance(value, dict):
                        yield value


def saved_workflow_path(name: str) -> Path:
    clean = name.strip()
    if not clean or "/" in clean or clean.startswith(".") or "\\" in clean:
        raise ValueError("workflow name must be a simple file stem")
    if not clean.endswith(".py"):
        clean += ".py"
    return user_workflow_root() / clean
