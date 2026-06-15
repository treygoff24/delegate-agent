from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]
GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def default_started_at() -> str:
    return datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_snapshot_run(
    registry: Any,
    registry_root: Path,
    workspace: Path,
    *,
    harness: str = "cursor",
    status: str = "running",
    assistant_text: str = "planning the change",
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    pid: int | None = None,
    started_at: str | None = None,
) -> tuple[str, str]:
    run_id, alias = registry.register_run(registry_root, harness=harness)
    run_path = registry.run_directory(registry_root, run_id)
    started = started_at or default_started_at()
    snapshot: JsonObject = {
        "schema": registry.SNAPSHOT_SCHEMA,
        "ok": True,
        "alias": alias,
        "runId": run_id,
        "harness": harness,
        "status": status,
        "startedAt": started,
        "assistantText": assistant_text,
        "assistantTextChars": len(assistant_text),
        "assistantTextTruncated": False,
        "recentEvents": [{"kind": "tool.started", "tool": "read", "path": "README.md"}],
        "warnings": [],
    }
    state: JsonObject = {
        "schema": registry.STATE_SCHEMA,
        "runId": run_id,
        "alias": alias,
        "status": status,
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "lastActivityAt": started,
        "current": "editing cli.py",
    }
    if pid is not None:
        state["pid"] = pid
    registry.write_json_atomic(run_path / "snapshot.json", snapshot)
    registry.write_json_atomic(run_path / "state.json", state)
    registry.write_json_atomic(
        run_path / "manifest.json",
        {
            "schema": registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": harness,
            "startedAt": started,
            "cwd": str(workspace),
            "mode": "work",
            "model": "composer-2.5",
        },
    )
    if stdout_bytes:
        (run_path / "stdout.log").write_bytes(b"x" * stdout_bytes)
    if stderr_bytes:
        (run_path / "stderr.log").write_bytes(b"y" * stderr_bytes)
    return run_id, alias


def seed_plain_run(
    delegate: Any,
    repo_path: str,
    *,
    harness: str = "cursor",
    last_activity_at: str | None = None,
) -> tuple[str, str]:
    registry_root = delegate.run_registry.ensure_registry(Path(repo_path), workspace_kind="git")
    run_id, alias = delegate.run_registry.register_run(
        registry_root,
        harness=harness,
        metadata={"mode": "work", "cwd": repo_path},
    )
    run_path = delegate.run_registry.run_directory(registry_root, run_id)
    started = delegate.run_registry.utc_now_iso()
    if last_activity_at is None:
        last_activity_at = delegate.run_registry.utc_now_iso()
    delegate.run_registry.write_json_atomic(
        run_path / "manifest.json",
        {
            "schema": delegate.run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": harness,
            "engine": harness,
            "mode": "work",
            "model": "composer-2.5",
            "cwd": repo_path,
            "startedAt": started,
        },
    )
    delegate.run_registry.write_json_atomic(
        run_path / "state.json",
        {
            "schema": delegate.run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": "succeeded",
            "lastActivityAt": last_activity_at,
        },
    )
    return run_id, alias


def seed_persistent_worktree_run(
    delegate: Any,
    repo_path: str,
    *,
    git_runner: GitRunner,
    alias: str = "cursor-4",
    harness: str = "cursor",
    branch: str | None = None,
    execution_cwd: str | None = None,
    worktree_status: str = "present",
    creation_oid: str | None = None,
    source_head_ref: str | None | object = object(),
    last_activity_at: str | None = None,
) -> tuple[str, str]:
    registry_root = delegate.run_registry.ensure_registry(Path(repo_path), workspace_kind="git")
    run_id, allocated_alias = delegate.run_registry.register_run(
        registry_root,
        harness=harness,
        metadata={"mode": "work", "cwd": repo_path},
    )
    if branch is None:
        branch = f"delegate/cursor-{delegate.worktree_execution.short_run_id(run_id)}"
    if execution_cwd is None:
        execution_cwd = str(Path(repo_path).parent / "worktree" / allocated_alias)
    if creation_oid is None:
        creation_oid = git_runner("rev-parse", "HEAD", cwd=repo_path).stdout.strip()
    if not isinstance(source_head_ref, (str, type(None))):
        ref_result = subprocess.run(
            ["git", "-C", repo_path, "symbolic-ref", "--quiet", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        source_head_ref = ref_result.stdout.strip() if ref_result.returncode == 0 else None
    source_branch = (
        source_head_ref[len("refs/heads/") :]
        if isinstance(source_head_ref, str) and source_head_ref.startswith("refs/heads/")
        else None
    )
    common_dir = git_runner("rev-parse", "--git-common-dir", cwd=repo_path).stdout.strip()
    if not Path(common_dir).is_absolute():
        common_dir = str(Path(repo_path) / common_dir)
    creation_context: JsonObject = {
        "sourceHeadOid": creation_oid,
        "sourceHeadRef": source_head_ref,
        "sourceBranch": source_branch,
        "sourceGitCommonDir": common_dir,
        "branch": branch,
        "plannedBranch": branch,
        "plannedExecutionCwd": execution_cwd,
    }
    run_path = delegate.run_registry.run_directory(registry_root, run_id)
    run_path.mkdir(parents=True, exist_ok=True)
    manifest_alias = alias if alias != allocated_alias else allocated_alias
    started = delegate.run_registry.utc_now_iso()
    if last_activity_at is None:
        last_activity_at = delegate.run_registry.utc_now_iso()
    delegate.run_registry.write_json_atomic(
        run_path / "manifest.json",
        {
            "schema": delegate.run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": manifest_alias,
            "harness": harness,
            "engine": harness,
            "mode": "work",
            "model": "composer-2.5",
            "cwd": repo_path,
            "executionCwd": execution_cwd,
            "sourceGitRoot": repo_path,
            "isolatedWorkspace": True,
            "isolationMode": "worktree",
            "effectiveIsolation": "worktree",
            "isolationLifecycle": "persistent",
            "preservedWorkspace": True,
            "branch": branch,
            "worktreeStatus": worktree_status,
            "creationContext": creation_context,
            "startedAt": started,
        },
    )
    delegate.run_registry.write_json_atomic(
        run_path / "state.json",
        {
            "schema": delegate.run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": manifest_alias,
            "status": "succeeded",
            "worktreeStatus": worktree_status,
            "lastActivityAt": last_activity_at,
        },
    )
    if alias != allocated_alias:
        index = delegate.run_registry.load_index(registry_root)
        index["aliases"].pop(allocated_alias, None)
        (registry_root / "aliases" / allocated_alias).unlink(missing_ok=True)
        index["aliases"][alias] = run_id
        entry = index["runs"][run_id]
        if isinstance(entry, dict):
            entry["alias"] = alias
        delegate.run_registry.bind_alias_claim(registry_root, alias, run_id)
        delegate.run_registry.save_index(registry_root, index)
    return run_id, alias
