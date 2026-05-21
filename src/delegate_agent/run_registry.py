from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DELEGATE_DIR_NAME = ".delegate"
GIT_EXCLUDE_ENTRY = ".delegate/"
RUN_ID_RE = re.compile(r"^del_\d{8}T\d{6}Z_[0-9a-f]{6}$")
INDEX_VERSION = 1
REGISTRY_LOCK_NAME = ".registry.lock"
REGISTRY_LOCK_TIMEOUT_SECONDS = 30.0
REGISTRY_LOCK_POLL_SECONDS = 0.05


def generate_run_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(3)
    run_id = f"del_{stamp}_{suffix}"
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"generated run id does not match expected format: {run_id}")
    return run_id


def delegate_root(workspace: Path) -> Path:
    return workspace / DELEGATE_DIR_NAME


def aliases_dir(registry_root: Path) -> Path:
    return registry_root / "aliases"


def runs_dir(registry_root: Path) -> Path:
    return registry_root / "runs"


def index_path(registry_root: Path) -> Path:
    return registry_root / "index.json"


def empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "aliases": {}, "runs": {}}


def load_index(registry_root: Path) -> dict[str, Any]:
    path = index_path(registry_root)
    if not path.exists():
        return empty_index()
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("index.json root must be an object")
    aliases = data.get("aliases")
    runs = data.get("runs")
    if not isinstance(aliases, dict) or not isinstance(runs, dict):
        raise ValueError("index.json must contain aliases and runs objects")
    return {
        "version": data.get("version", INDEX_VERSION),
        "aliases": aliases,
        "runs": runs,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temp, path)


def save_index(registry_root: Path, index: dict[str, Any]) -> None:
    write_json_atomic(index_path(registry_root), index)


def ensure_git_delegate_exclude(git_root: Path) -> None:
    exclude_file = git_root / ".git" / "info" / "exclude"
    if not exclude_file.parent.exists():
        return
    existing = exclude_file.read_text() if exclude_file.exists() else ""
    lines = existing.splitlines()
    if any(
        line.strip() == GIT_EXCLUDE_ENTRY.rstrip("/") or line.strip() == GIT_EXCLUDE_ENTRY
        for line in lines
    ):
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    exclude_file.write_text(existing + GIT_EXCLUDE_ENTRY + "\n")


def ensure_registry(workspace: Path, *, workspace_kind: str) -> Path:
    root = delegate_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    aliases_dir(root).mkdir(parents=True, exist_ok=True)
    runs_dir(root).mkdir(parents=True, exist_ok=True)
    if workspace_kind == "git":
        ensure_git_delegate_exclude(workspace)
    if not index_path(root).exists():
        save_index(root, empty_index())
    return root


def allocate_alias(registry_root: Path, harness: str) -> str:
    claims = aliases_dir(registry_root)
    claims.mkdir(parents=True, exist_ok=True)
    counter = 1
    while True:
        alias = harness if counter == 1 else f"{harness}-{counter}"
        claim_path = claims / alias
        try:
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            counter += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("")
        return alias


def bind_alias_claim(registry_root: Path, alias: str, run_id: str) -> None:
    claim_path = aliases_dir(registry_root) / alias
    claim_path.write_text(run_id + "\n", encoding="utf-8")


def registry_lock_path(registry_root: Path) -> Path:
    return registry_root / REGISTRY_LOCK_NAME


@contextmanager
def registry_lock(
    registry_root: Path,
    *,
    timeout_seconds: float = REGISTRY_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize registry mutations. flock releases on process exit (no stale locks)."""
    registry_root.mkdir(parents=True, exist_ok=True)
    lock_path = registry_lock_path(registry_root)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for registry lock at {lock_path} after {timeout_seconds}s"
                    ) from None
                time.sleep(REGISTRY_LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def register_run(
    registry_root: Path,
    *,
    harness: str,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, str]:
    run_id = run_id or generate_run_id()
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"run id does not match expected format: {run_id}")
    with registry_lock(registry_root):
        alias = allocate_alias(registry_root, harness)
        bind_alias_claim(registry_root, alias, run_id)
        run_dir = runs_dir(registry_root) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        index = load_index(registry_root)
        entry = {"alias": alias, "harness": harness, **(metadata or {})}
        index["aliases"][alias] = run_id
        index["runs"][run_id] = entry
        save_index(registry_root, index)
    return run_id, alias


def lookup_run_id(index: dict[str, Any], handle: str) -> str | None:
    if handle in index.get("runs", {}):
        return handle
    aliases = index.get("aliases", {})
    if handle in aliases:
        return aliases[handle]
    return None


def lookup_alias(index: dict[str, Any], alias: str) -> str | None:
    run_id = index.get("aliases", {}).get(alias)
    return run_id if isinstance(run_id, str) else None


LARGE_LOG_WARN_BYTES = 50 * 1024 * 1024
DEFAULT_RUNS_LIMIT = 20
STATUS_RUNNING = "running"
STATUS_STALE = "stale"


@dataclass(frozen=True)
class ResolveResult:
    run_id: str | None
    alias: str | None
    suggestions: tuple[str, ...]


def read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def run_directory(registry_root: Path, run_id: str) -> Path:
    return runs_dir(registry_root) / run_id


def large_log_warnings(stdout_bytes: int, stderr_bytes: int) -> list[str]:
    warnings: list[str] = []
    if stdout_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(f"stdout.log > 50 MB ({stdout_bytes} bytes)")
    if stderr_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(f"stderr.log > 50 MB ({stderr_bytes} bytes)")
    return warnings


def process_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def effective_status(state: dict[str, Any] | None) -> str:
    if not state:
        return "unknown"
    status = state.get("status")
    if not isinstance(status, str) or not status:
        return "unknown"
    if status != STATUS_RUNNING:
        return status
    pid = state.get("pid")
    if not isinstance(pid, int):
        return STATUS_STALE
    alive = process_alive(pid)
    if alive is False:
        return STATUS_STALE
    return STATUS_RUNNING


def suggest_handles(index: dict[str, Any], handle: str, *, limit: int = 8) -> list[str]:
    aliases = sorted(index.get("aliases", {}).keys())
    if not aliases:
        return []
    exact_ci = [alias for alias in aliases if alias.lower() == handle.lower()]
    if exact_ci:
        return exact_ci[:limit]
    prefix = [alias for alias in aliases if alias.startswith(handle) or handle.startswith(alias)]
    substring = [alias for alias in aliases if handle in alias or alias in handle]
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in prefix + substring + aliases:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
        if len(ordered) >= limit:
            break
    return ordered


def resolve_handle(index: dict[str, Any], handle: str) -> ResolveResult:
    run_id = lookup_run_id(index, handle)
    if run_id is None:
        return ResolveResult(None, None, tuple(suggest_handles(index, handle)))
    alias = index.get("runs", {}).get(run_id, {}).get("alias")
    if not isinstance(alias, str):
        alias = None
        for candidate, candidate_id in index.get("aliases", {}).items():
            if candidate_id == run_id:
                alias = candidate
                break
    return ResolveResult(run_id, alias, ())


def load_run_state(registry_root: Path, run_id: str) -> dict[str, Any] | None:
    return read_json_object(run_directory(registry_root, run_id) / "state.json")


def load_run_snapshot(registry_root: Path, run_id: str) -> dict[str, Any] | None:
    return read_json_object(run_directory(registry_root, run_id) / "snapshot.json")


def load_run_manifest(registry_root: Path, run_id: str) -> dict[str, Any] | None:
    return read_json_object(run_directory(registry_root, run_id) / "manifest.json")


def log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    run_path = run_directory(registry_root, run_id)
    stdout_bytes = 0
    stderr_bytes = 0
    stdout_path = run_path / "stdout.log"
    stderr_path = run_path / "stderr.log"
    if stdout_path.exists():
        stdout_bytes = stdout_path.stat().st_size
    if stderr_path.exists():
        stderr_bytes = stderr_path.stat().st_size
    return stdout_bytes, stderr_bytes


def _activity_timestamp(state: dict[str, Any] | None, manifest: dict[str, Any] | None) -> str:
    if state:
        for key in ("lastActivityAt", "finishedAt", "startedAt"):
            value = state.get(key)
            if isinstance(value, str) and value:
                return value
    if manifest:
        started = manifest.get("startedAt")
        if isinstance(started, str) and started:
            return started
    return ""


def build_run_summary(
    registry_root: Path,
    run_id: str,
    index_entry: dict[str, Any],
) -> dict[str, Any]:
    state = load_run_state(registry_root, run_id)
    manifest = load_run_manifest(registry_root, run_id)
    from delegate_agent import retention as delegate_retention

    stdout_bytes, stderr_bytes = delegate_retention.effective_log_byte_sizes(registry_root, run_id)
    alias = index_entry.get("alias")
    harness = index_entry.get("harness")
    summary: dict[str, Any] = {
        "runId": run_id,
        "alias": alias if isinstance(alias, str) else None,
        "harness": harness if isinstance(harness, str) else None,
        "status": effective_status(state),
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "warnings": large_log_warnings(stdout_bytes, stderr_bytes),
        "activityAt": _activity_timestamp(state, manifest),
    }
    if state and isinstance(state.get("current"), str):
        summary["current"] = state["current"]
    if isinstance(alias, str):
        summary["snapshotCommand"] = f"delegate snapshot {alias}"
    return summary


def list_run_summaries(
    registry_root: Path,
    index: dict[str, Any],
    *,
    active: bool = False,
    harness: str | None = None,
    limit: int = DEFAULT_RUNS_LIMIT,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    summaries: list[dict[str, Any]] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(entry, dict):
            continue
        entry_harness = entry.get("harness")
        if harness is not None and entry_harness != harness:
            continue
        summary = build_run_summary(registry_root, run_id, entry)
        status = summary.get("status")
        if active and status not in (STATUS_RUNNING, STATUS_STALE):
            continue
        summaries.append(summary)
    summaries.sort(key=lambda item: item.get("activityAt", ""), reverse=True)
    return summaries[:limit]


def timestamp_from_run_id(run_id: str) -> str:
    match = re.match(r"^del_(\d{8}T\d{6}Z)_", run_id)
    if not match:
        return ""
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}:{raw[11:13]}:{raw[13:15]}Z"


def latest_run_id_for_harness(
    registry_root: Path, index: dict[str, Any], harness: str
) -> str | None:
    matches: list[tuple[str, str]] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(entry, dict) or entry.get("harness") != harness:
            continue
        state = load_run_state(registry_root, run_id)
        manifest = load_run_manifest(registry_root, run_id)
        activity = _activity_timestamp(state, manifest)
        sort_ts = activity or timestamp_from_run_id(run_id)
        matches.append((sort_ts, run_id))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][1]
