from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shlex
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from delegate_agent import private_io
from delegate_agent.json_types import JsonObject
from delegate_agent.private_io import (  # noqa: F401  # re-exported
    RegistryJsonError,
    ensure_private_dir,
    ensure_private_file,
    read_json_object,
    read_json_object_or_none,
    supports_private_modes,
    write_json_atomic,
    write_private_bytes,
    write_private_text,
    write_text_atomic,
)

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
PRIVATE_DIR_MODE = private_io.PRIVATE_DIR_MODE
PRIVATE_FILE_MODE = private_io.PRIVATE_FILE_MODE
GIT_INFO_EXCLUDE_TIMEOUT_SECONDS = 5.0
BYTES_PER_KIB = 1 << 10
BYTES_PER_MIB = BYTES_PER_KIB * BYTES_PER_KIB


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


def registry_root(workspace: Path) -> Path:
    return delegate_root(workspace)


def registry_root_if_exists(workspace: Path) -> Path | None:
    root = registry_root(workspace)
    if not index_path(root).exists():
        return None
    return root


def git_info_exclude_path(git_root: Path) -> Path | None:
    try:
        completed = subprocess.run(  # nosec B603 B607 - fixed git argv is executed with shell=False.
            ["git", "-C", str(git_root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_INFO_EXCLUDE_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
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
    data = read_json_object(path)
    if data is None:
        return empty_index()
    aliases = data.get("aliases")
    runs = data.get("runs")
    if not isinstance(aliases, dict) or not isinstance(runs, dict):
        raise RegistryJsonError(f"{path} must contain aliases and runs objects")
    return {
        "version": data.get("version", INDEX_VERSION),
        "aliases": aliases,
        "runs": runs,
    }


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
    write_text_atomic(exclude_file, existing + GIT_EXCLUDE_ENTRY + "\n")


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


def snapshot_command(alias: str, *, cwd: str | None = None) -> str:
    if cwd is None:
        return f"delegate snapshot {alias}"
    return shlex.join(["delegate", "--cwd", cwd, "snapshot", alias])


def run_output_command(
    handle: str,
    *,
    completion_report: bool = False,
    cwd: str | None = None,
) -> str:
    if cwd is not None:
        argv = ["delegate", "--cwd", cwd, "run-output", handle]
        if completion_report:
            argv.append("--completion-report")
        return shlex.join(argv)
    base = f"delegate run-output {handle}"
    if completion_report:
        return f"{base} --completion-report"
    return base


def alias_sequence_for_harness(alias: object, harness: str) -> int:
    """Return the monotonic alias generation for a harness alias.

    Alias claims are durable and allocated with exclusive creates, so the
    numeric suffix is a better same-timestamp tiebreaker than the random run-id
    suffix.  ``cursor`` is generation 1, ``cursor-2`` is generation 2, and
    malformed or foreign aliases rank after valid harness aliases.
    """
    if not isinstance(alias, str):
        return 0
    if alias == harness:
        return 1
    prefix = f"{harness}-"
    if not alias.startswith(prefix):
        return 0
    suffix = alias.removeprefix(prefix)
    if not suffix.isdecimal():
        return 0
    return int(suffix)


@dataclass(frozen=True)
class ResolveResult:
    run_id: str | None
    alias: str | None
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class RunTarget:
    run_id: str
    alias: str | None


@dataclass(frozen=True)
class RunTargetLookupError:
    error: str
    message: str


def run_directory(registry_root: Path, run_id: str) -> Path:
    return runs_dir(registry_root) / run_id


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


def resolve_run_target(
    registry_root: Path,
    *,
    handle: str | None,
    latest_harness: str | None,
) -> RunTarget | RunTargetLookupError:
    index = load_index(registry_root)
    if latest_harness is not None:
        run_id = latest_run_id_for_harness(registry_root, index, latest_harness)
        if run_id is None:
            return RunTargetLookupError(
                "no_matching_runs",
                f"No runs found for harness: {latest_harness}",
            )
        return RunTarget(run_id, alias_for_run(index, run_id))
    if handle is None:
        return RunTargetLookupError(
            "missing_handle",
            "A run handle is required.",
        )
    resolved = resolve_handle(index, handle)
    if resolved.run_id is None:
        suggestions = ", ".join(resolved.suggestions) if resolved.suggestions else "(none)"
        return RunTargetLookupError(
            "unknown_handle",
            f"Unknown run handle: {handle}. Suggestions: {suggestions}. "
            "Runs are recorded per-workspace under <workspace>/.delegate; "
            "if this run was launched elsewhere, pass --cwd <that workspace>.",
        )
    return RunTarget(resolved.run_id, resolved.alias)


def load_run_state(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / STATE_FILE)


def load_run_state_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object_or_none(run_directory(registry_root, run_id) / STATE_FILE)


def load_run_snapshot(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / SNAPSHOT_FILE)


def load_run_snapshot_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object_or_none(run_directory(registry_root, run_id) / SNAPSHOT_FILE)


def load_run_manifest(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object(run_directory(registry_root, run_id) / MANIFEST_FILE)


def load_run_manifest_or_none(registry_root: Path, run_id: str) -> JsonObject | None:
    return read_json_object_or_none(run_directory(registry_root, run_id) / MANIFEST_FILE)


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


def timestamp_from_run_id(run_id: str) -> str:
    match = re.match(r"^del_(\d{8}T\d{6}Z)_", run_id)
    if not match:
        return ""
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}:{raw[11:13]}:{raw[13:15]}Z"


def _write_worktree_status(
    registry_root: Path,
    run_id: str,
    status: str,
    *,
    removed_at: str | None = None,
    discarded_dirty_paths: list[str] | None = None,
) -> JsonObject:
    valid_statuses = {"present", "removed", "missing", "unknown"}
    if status not in valid_statuses:
        raise ValueError(f"worktree status must be one of: {', '.join(sorted(valid_statuses))}")
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


def set_worktree_status(
    registry_root: Path,
    run_id: str,
    status: str,
    *,
    removed_at: str | None = None,
    discarded_dirty_paths: list[str] | None = None,
) -> JsonObject:
    """Set the worktreeStatus field on a run's state, and optionally worktreeRemovedAt.

    Acquires the registry lock, loads the run's state, updates fields,
    persists via write_json_atomic, and returns the updated state.

    Status must be one of: 'present', 'removed', 'missing', 'unknown'.
    """
    with registry_lock(registry_root):
        return _write_worktree_status(
            registry_root,
            run_id,
            status,
            removed_at=removed_at,
            discarded_dirty_paths=discarded_dirty_paths,
        )


def set_worktree_status_locked(
    registry_root: Path,
    run_id: str,
    status: str,
    *,
    removed_at: str | None = None,
    discarded_dirty_paths: list[str] | None = None,
) -> JsonObject:
    """Set worktree status when the caller already holds the registry lock."""
    return _write_worktree_status(
        registry_root,
        run_id,
        status,
        removed_at=removed_at,
        discarded_dirty_paths=discarded_dirty_paths,
    )


def latest_run_id_for_harness(registry_root: Path, index: JsonObject, harness: str) -> str | None:
    matches: list[tuple[str, int, str]] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(entry, dict) or entry.get("harness") != harness:
            continue
        state = load_run_state_or_none(registry_root, run_id)
        manifest = load_run_manifest_or_none(registry_root, run_id)
        sort_ts = run_status.activity_timestamp(state, manifest, run_id)
        alias_sequence = alias_sequence_for_harness(entry.get("alias"), harness)
        matches.append((sort_ts, alias_sequence, run_id))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][2]


from delegate_agent import run_status  # noqa: E402  # facade re-export, placed after core defs
from delegate_agent.run_status import (  # noqa: E402, F401  # re-exported
    DEFAULT_RUNS_LIMIT,
    LARGE_LOG_WARN_BYTES,
    LARGE_LOG_WARN_MIB,
    STATUS_FILTER_ACTIVE,
    STATUS_FILTER_RECENT,
    STATUS_FILTER_RUNNING,
    STATUS_FILTER_STALE,
    STATUS_RUNNING,
    STATUS_STALE,
    STATUS_UNKNOWN,
    activity_datetime,
    activity_timestamp,
    build_run_summary,
    effective_log_byte_sizes,
    effective_status,
    large_log_warnings,
    list_run_summaries,
    log_byte_sizes,
    process_alive,
    raw_logs_archived,
    raw_status,
    stale_next_actions,
    status_fields,
)
