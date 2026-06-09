from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from delegate_agent import archived_logs
from delegate_agent.json_types import JsonObject, JsonValue

DELEGATE_DIR_NAME = ".delegate"
GIT_EXCLUDE_ENTRY = ".delegate/"
RUN_ID_RE = re.compile(r"^del_\d{8}T\d{6}Z_[0-9a-f]{6}$")

STDOUT_LOG = "stdout.log"
STDERR_LOG = "stderr.log"
EVENTS_JSONL = "events.jsonl"
MANIFEST_FILE = "manifest.json"
STATE_FILE = "state.json"
SNAPSHOT_FILE = "snapshot.json"
COMPLETION_REPORT_FILE = "completion-report.md"
INDEX_VERSION = 1
MANIFEST_SCHEMA = "delegate.manifest.v1"
STATE_SCHEMA = "delegate.state.v1"
SNAPSHOT_SCHEMA = "delegate.snapshot.v1"
RUNS_SCHEMA = "delegate.runs.v1"
RUN_OUTPUT_SCHEMA = "delegate.run-output.v1"
REGISTRY_LOCK_NAME = ".registry.lock"
REGISTRY_LOCK_TIMEOUT_SECONDS = 30.0
REGISTRY_LOCK_POLL_SECONDS = 0.05
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def supports_private_modes() -> bool:
    """Return whether chmod-style private mode hardening is available."""
    return os.name == "posix"


def ensure_private_dir(path: Path) -> None:
    """Create a registry directory and make it owner-only on POSIX."""
    path.mkdir(parents=True, exist_ok=True)
    if supports_private_modes():
        os.chmod(path, PRIVATE_DIR_MODE)


def ensure_private_file(path: Path) -> None:
    """Make an existing registry file owner-read/write on POSIX."""
    if supports_private_modes():
        os.chmod(path, PRIVATE_FILE_MODE)


def write_private_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Create/truncate a registry text file with owner-only permissions."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding=encoding) as handle:
        handle.write(text)
    ensure_private_file(path)


def write_private_bytes(path: Path, payload: bytes) -> None:
    """Create/truncate a registry byte file with owner-only permissions."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    ensure_private_file(path)


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


def registry_root_if_exists(workspace: Path) -> Path | None:
    root = delegate_root(workspace)
    if not index_path(root).exists():
        return None
    return root


def git_info_exclude_path(git_root: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    relative = completed.stdout.strip()
    if not relative:
        return None
    exclude_file = Path(relative)
    if not exclude_file.is_absolute():
        exclude_file = (git_root / exclude_file).resolve()
    return exclude_file


def aliases_dir(registry_root: Path) -> Path:
    return registry_root / "aliases"


def runs_dir(registry_root: Path) -> Path:
    return registry_root / "runs"


def index_path(registry_root: Path) -> Path:
    return registry_root / "index.json"


def empty_index() -> JsonObject:
    return {"version": INDEX_VERSION, "aliases": {}, "runs": {}}


def load_index(registry_root: Path) -> JsonObject:
    path = index_path(registry_root)
    if not path.exists():
        return empty_index()
    data: JsonValue = json.loads(path.read_text(encoding="utf-8"))
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


def write_json_atomic(path: Path, payload: JsonObject) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    write_private_text(
        temp,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    os.replace(temp, path)
    ensure_private_file(path)


def save_index(registry_root: Path, index: JsonObject) -> None:
    write_json_atomic(index_path(registry_root), index)


def ensure_git_delegate_exclude(git_root: Path) -> None:
    exclude_file = git_info_exclude_path(git_root)
    if exclude_file is None or not exclude_file.parent.exists():
        return
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    lines = existing.splitlines()
    if any(
        line.strip() == GIT_EXCLUDE_ENTRY.rstrip("/") or line.strip() == GIT_EXCLUDE_ENTRY
        for line in lines
    ):
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    exclude_file.write_text(existing + GIT_EXCLUDE_ENTRY + "\n", encoding="utf-8")


def ensure_registry(workspace: Path, *, workspace_kind: str) -> Path:
    root = delegate_root(workspace)
    ensure_private_dir(root)
    ensure_private_dir(aliases_dir(root))
    ensure_private_dir(runs_dir(root))
    if workspace_kind == "git":
        ensure_git_delegate_exclude(workspace)
    if not index_path(root).exists():
        save_index(root, empty_index())
    else:
        ensure_private_file(index_path(root))
    return root


def allocate_alias(registry_root: Path, harness: str) -> str:
    claims = aliases_dir(registry_root)
    ensure_private_dir(claims)
    counter = 1
    while True:
        alias = harness if counter == 1 else f"{harness}-{counter}"
        claim_path = claims / alias
        try:
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        except FileExistsError:
            counter += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("")
        return alias


def bind_alias_claim(registry_root: Path, alias: str, run_id: str) -> None:
    claim_path = aliases_dir(registry_root) / alias
    write_private_text(claim_path, run_id + "\n")


def registry_lock_path(registry_root: Path) -> Path:
    return registry_root / REGISTRY_LOCK_NAME


@contextmanager
def registry_lock(
    registry_root: Path,
    *,
    timeout_seconds: float = REGISTRY_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize registry mutations. flock releases on process exit (no stale locks)."""
    ensure_private_dir(registry_root)
    lock_path = registry_lock_path(registry_root)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
    ensure_private_file(lock_path)
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
    metadata: JsonObject | None = None,
) -> tuple[str, str]:
    run_id = run_id or generate_run_id()
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"run id does not match expected format: {run_id}")
    with registry_lock(registry_root):
        alias = allocate_alias(registry_root, harness)
        bind_alias_claim(registry_root, alias, run_id)
        run_dir = runs_dir(registry_root) / run_id
        ensure_private_dir(run_dir)
        index = load_index(registry_root)
        entry = {"alias": alias, "harness": harness, **(metadata or {})}
        index["aliases"][alias] = run_id
        index["runs"][run_id] = entry
        save_index(registry_root, index)
    return run_id, alias


def lookup_run_id(index: JsonObject, handle: str) -> str | None:
    if handle in index.get("runs", {}):
        return handle
    aliases = index.get("aliases", {})
    if handle in aliases:
        return aliases[handle]
    return None


LARGE_LOG_WARN_BYTES = 50 * 1024 * 1024
DEFAULT_RUNS_LIMIT = 20
STATUS_RUNNING = "running"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"
STATUS_FILTER_ACTIVE = "active"
STATUS_FILTER_RECENT = "recent"
STATUS_FILTER_RUNNING = "running"
STATUS_FILTER_STALE = "stale"

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime(UTC_TIMESTAMP_FORMAT)


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def snapshot_command(alias: str) -> str:
    return f"delegate snapshot {alias}"


def run_output_command(handle: str, *, completion_report: bool = False) -> str:
    base = f"delegate run-output {handle}"
    if completion_report:
        return f"{base} --completion-report"
    return base


@dataclass(frozen=True)
class ResolveResult:
    run_id: str | None
    alias: str | None
    suggestions: tuple[str, ...]


def read_json_object(path: Path) -> JsonObject | None:
    if not path.exists():
        return None
    try:
        data: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def run_directory(registry_root: Path, run_id: str) -> Path:
    return runs_dir(registry_root) / run_id


def large_log_warnings(stdout_bytes: int, stderr_bytes: int) -> list[str]:
    warnings: list[str] = []
    if stdout_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(f"{STDOUT_LOG} > 50 MB ({stdout_bytes} bytes)")
    if stderr_bytes > LARGE_LOG_WARN_BYTES:
        warnings.append(f"{STDERR_LOG} > 50 MB ({stderr_bytes} bytes)")
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


def effective_status(state: JsonObject | None) -> str:
    if not state:
        return "unknown"
    status = state.get("status")
    if not isinstance(status, str) or not status:
        return "unknown"
    if status != STATUS_RUNNING:
        return status
    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return STATUS_STALE
    alive = process_alive(pid)
    if alive is False:
        return STATUS_STALE
    return STATUS_RUNNING


def raw_status(state: JsonObject | None) -> str:
    if not state:
        return STATUS_UNKNOWN
    status = state.get("status")
    if not isinstance(status, str) or not status:
        return STATUS_UNKNOWN
    return status


def status_fields(state: JsonObject | None) -> JsonObject:
    # Probe pid liveness once so effectiveStatus and staleReason cannot disagree
    # when the process exits between two separate probes.
    raw = raw_status(state)
    effective = raw
    reason: str | None = None
    if raw == STATUS_RUNNING:
        pid = state.get("pid") if state else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            effective, reason = STATUS_STALE, "missing_pid"
        elif process_alive(pid) is False:
            effective, reason = STATUS_STALE, "dead_pid"
    fields: JsonObject = {
        "rawStatus": raw,
        "effectiveStatus": effective,
        "status": effective,
    }
    if reason is not None:
        fields["staleReason"] = reason
    return fields


def stale_next_actions(alias_or_run_id: str) -> list[str]:
    return [
        f"delegate snapshot {alias_or_run_id}",
        f"delegate run-output {alias_or_run_id} --completion-report",
        f"delegate run-output {alias_or_run_id} --stderr --tail 100",
    ]


def suggest_handles(index: JsonObject, handle: str, *, limit: int = 8) -> list[str]:
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


def resolve_handle(index: JsonObject, handle: str) -> ResolveResult:
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


def load_run_state(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / STATE_FILE)


def load_run_snapshot(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / SNAPSHOT_FILE)


def load_run_manifest(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / MANIFEST_FILE)


def log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    run_path = run_directory(registry_root, run_id)
    stdout_bytes = 0
    stderr_bytes = 0
    stdout_path = run_path / STDOUT_LOG
    stderr_path = run_path / STDERR_LOG
    if stdout_path.exists():
        stdout_bytes = stdout_path.stat().st_size
    if stderr_path.exists():
        stderr_bytes = stderr_path.stat().st_size
    return stdout_bytes, stderr_bytes


def _archived_log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    state_sizes = archived_logs.state_log_byte_sizes(load_run_state(registry_root, run_id))
    if state_sizes is not None:
        return state_sizes
    return archived_logs.archive_log_byte_sizes(
        archived_logs.archive_path(registry_root, run_id),
        stdout_log=STDOUT_LOG,
        stderr_log=STDERR_LOG,
    )


def raw_logs_archived(registry_root: Path, run_id: str) -> bool:
    return archived_logs.archive_path(registry_root, run_id).exists()


def effective_log_byte_sizes(registry_root: Path, run_id: str) -> tuple[int, int]:
    run_path = run_directory(registry_root, run_id)
    stdout_path = run_path / STDOUT_LOG
    stderr_path = run_path / STDERR_LOG
    if stdout_path.exists() or stderr_path.exists():
        return log_byte_sizes(registry_root, run_id)
    if raw_logs_archived(registry_root, run_id):
        return _archived_log_byte_sizes(registry_root, run_id)
    return 0, 0


def activity_timestamp(
    state: JsonObject | None,
    manifest: JsonObject | None,
    run_id: str | None = None,
) -> str:
    if state:
        for key in ("finishedAt", "lastActivityAt", "startedAt"):
            value = state.get(key)
            if isinstance(value, str) and value:
                return value
    if manifest:
        started = manifest.get("startedAt")
        if isinstance(started, str) and started:
            return started
    if run_id:
        return timestamp_from_run_id(run_id)
    return ""


def activity_datetime(
    state: JsonObject | None,
    manifest: JsonObject | None,
    run_id: str | None = None,
) -> datetime | None:
    return parse_utc_timestamp(activity_timestamp(state, manifest, run_id))


def alias_for_run(index: JsonObject, run_id: str) -> str | None:
    entry = index.get("runs", {}).get(run_id)
    if isinstance(entry, dict):
        alias = entry.get("alias")
        if isinstance(alias, str):
            return alias
    for candidate, candidate_id in index.get("aliases", {}).items():
        if candidate_id == run_id and isinstance(candidate, str):
            return candidate
    return None


def build_run_summary(
    registry_root: Path,
    run_id: str,
    index_entry: JsonObject,
) -> JsonObject:
    state = load_run_state(registry_root, run_id)
    manifest = load_run_manifest(registry_root, run_id)

    stdout_bytes, stderr_bytes = effective_log_byte_sizes(registry_root, run_id)
    alias = index_entry.get("alias")
    harness = index_entry.get("harness")
    handle = alias if isinstance(alias, str) else run_id
    summary: JsonObject = {
        "runId": run_id,
        "alias": alias if isinstance(alias, str) else None,
        "harness": harness if isinstance(harness, str) else None,
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "warnings": large_log_warnings(stdout_bytes, stderr_bytes),
        "activityAt": activity_timestamp(state, manifest),
    }
    summary.update(status_fields(state))
    if summary.get("effectiveStatus") == STATUS_STALE:
        summary["nextActions"] = stale_next_actions(handle)
    if state and isinstance(state.get("current"), str):
        summary["current"] = state["current"]
    if isinstance(alias, str):
        summary["snapshotCommand"] = snapshot_command(alias)

    # Isolation metadata: detect persistent worktree runs.
    worktree_status = None
    if state and isinstance(state.get("worktreeStatus"), str):
        worktree_status = state["worktreeStatus"]
        summary["worktreeStatus"] = worktree_status
        summary["isolationLifecycle"] = "persistent"
    if manifest:
        execution_cwd = manifest.get("executionCwd")
        if isinstance(execution_cwd, str) and execution_cwd:
            summary["executionCwd"] = execution_cwd
        source_git_root = manifest.get("sourceGitRoot")
        if isinstance(source_git_root, str) and source_git_root:
            summary["sourceGitRoot"] = source_git_root

    return summary


def list_run_summaries(
    registry_root: Path,
    index: JsonObject,
    *,
    active: bool = False,
    status_filter: str | None = None,
    harness: str | None = None,
    limit: int = DEFAULT_RUNS_LIMIT,
) -> list[JsonObject]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    summaries: list[JsonObject] = []
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
        if status_filter == STATUS_FILTER_RUNNING and status != STATUS_RUNNING:
            continue
        if status_filter == STATUS_FILTER_STALE and status != STATUS_STALE:
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


def set_worktree_status(
    registry_root: Path,
    run_id: str,
    status: str,
    *,
    removed_at: str | None = None,
    discarded_dirty_paths: list[str] | None = None,
    _skip_lock: bool = False,
) -> JsonObject:
    """Set the worktreeStatus field on a run's state, and optionally worktreeRemovedAt.

    Acquires the registry lock, loads the run's state, updates fields,
    persists via write_json_atomic, and returns the updated state.

    Status must be one of: 'present', 'removed', 'missing', 'unknown'.
    _skip_lock is for internal use when the caller already holds the registry lock.
    """
    valid_statuses = {"present", "removed", "missing", "unknown"}
    if status not in valid_statuses:
        raise ValueError(f"worktree status must be one of: {', '.join(sorted(valid_statuses))}")

    def _write():
        run_path = run_directory(registry_root, run_id)
        state_path = run_path / STATE_FILE
        if not state_path.exists():
            raise FileNotFoundError(f"State file not found for run {run_id}")
        state: JsonObject = json.loads(state_path.read_text(encoding="utf-8"))
        state["worktreeStatus"] = status
        if removed_at is not None:
            state["worktreeRemovedAt"] = removed_at
        if discarded_dirty_paths is not None:
            state["discardedDirtyPaths"] = discarded_dirty_paths
        write_json_atomic(state_path, state)
        return state

    if _skip_lock:
        return _write()
    with registry_lock(registry_root):
        return _write()


def set_worktree_status_locked(
    registry_root: Path,
    run_id: str,
    status: str,
    *,
    removed_at: str | None = None,
    discarded_dirty_paths: list[str] | None = None,
) -> JsonObject:
    """Set worktree status when the caller already holds the registry lock."""
    return set_worktree_status(
        registry_root,
        run_id,
        status,
        removed_at=removed_at,
        discarded_dirty_paths=discarded_dirty_paths,
        _skip_lock=True,
    )


def latest_run_id_for_harness(registry_root: Path, index: JsonObject, harness: str) -> str | None:
    matches: list[tuple[str, str]] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(entry, dict) or entry.get("harness") != harness:
            continue
        state = load_run_state(registry_root, run_id)
        manifest = load_run_manifest(registry_root, run_id)
        sort_ts = activity_timestamp(state, manifest, run_id)
        matches.append((sort_ts, run_id))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][1]
