from __future__ import annotations

import shlex
import subprocess  # nosec B404 - persistent worktree cleanup intentionally runs fixed git argv with shell=False.
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from delegate_agent import harness_events, profiles, retention, run_registry, safe_workspace
from delegate_agent import runner as delegate_runner
from delegate_agent.argv_utils import public_argv, replace_workspace_arg_in_argv
from delegate_agent.git_utils import (
    GIT_MUTATION_TIMEOUT_SECONDS,
    GIT_QUICK_TIMEOUT_SECONDS,
    capture_git_metadata,
)
from delegate_agent.git_utils import run_git as _run_git
from delegate_agent.isolation import (
    IsolationContext,
    IsolationExecutionError,
    branch_label,
    compute_repo_fingerprint_from_common_dir,
    create_persistent_worktree,
    plan_branch_name,
    plan_worktree_path,
    prepend_persistent_worktree_context,
    require_valid_head,
    short_run_id,
    worktrees_data_home,
)
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import (
    DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
    PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_models import Request, ResolvedWorkspace


class PersistentWorktreeError(Exception):
    """User-facing error raised by the persistent worktree execution boundary."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


BinaryValidator = Callable[[list[str], str], None]


@dataclass(frozen=True)
class PersistentWorktreeExecution:
    request: Request
    json_mode: bool
    config: JsonObject
    pass_through: bool
    completion_report_mode: str
    source_workspace: ResolvedWorkspace
    stdout: TextIO
    stderr: TextIO
    binary_validator: BinaryValidator


@dataclass(frozen=True)
class PersistentWorktreePreflight:
    iso_ctx: IsolationContext
    source_git_root: str
    base_oid: str
    source_git_common_dir: str | None
    source_head_oid: str
    source_head_ref: str | None
    source_branch: str | None
    registry_root: Path
    tracked_dirty_files: int
    untracked_files: int


@dataclass
class PersistentWorktreeRegistration:
    run_id: str
    alias: str
    run_path: Path
    branch: str
    worktree_path: str
    creation_context: JsonObject
    pre_ctx: delegate_runner.RunContext
    synced_files: int = 0


@dataclass(frozen=True)
class ExecutionWorkspaceRequest:
    argv: list[str]
    display_argv: list[str]
    stdin_text: str | None
    prompt_file_text: str | None
    agent_config_text: str | None


def execute_persistent_worktree(
    execution: PersistentWorktreeExecution,
) -> tuple[int, JsonObject | None]:
    """Launch a work-mode child in a preserved Delegate-managed git worktree."""
    preflight = _validate_persistent_worktree_request(execution)
    registration = _register_persistent_worktree_run(execution, preflight)
    _create_persistent_worktree_or_record_failure(execution, preflight, registration)
    return _launch_child_in_persistent_worktree(execution, preflight, registration)


def _validate_persistent_worktree_request(
    execution: PersistentWorktreeExecution,
) -> PersistentWorktreePreflight:
    request = execution.request
    iso_ctx = request.isolation_context
    if iso_ctx is None:
        raise PersistentWorktreeError(
            "missing_isolation_context",
            "Persistent worktree execution requires an isolation context.",
        )

    if request.workspace_kind != "git":
        raise PersistentWorktreeError(
            "worktree_requires_git",
            "--isolation worktree requires a Git workspace.",
        )
    source_git_root = request.workspace

    try:
        base_oid = require_valid_head(source_git_root)
    except IsolationExecutionError as exc:
        raise PersistentWorktreeError(exc.error, exc.message) from exc
    (
        _current_git_root,
        current_git_common_dir,
        _current_head_oid,
        current_head_ref,
        current_branch,
    ) = capture_git_metadata(source_git_root)

    if execution.pass_through:
        raise PersistentWorktreeError(
            "pass_through_with_persistent_isolation",
            "--pass-through is not supported with persistent worktree runs (work mode + effective worktree isolation).",
        )

    try:
        tracked_dirty_files, untracked_files = safe_workspace.dirty_sync_counts(source_git_root)
    except Exception as exc:
        raise PersistentWorktreeError(
            getattr(exc, "error", "dirty_source_check_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc

    execution.binary_validator(request.argv, request.engine)

    if request.engine == "codex":
        profiles.preflight_codex_request(request, execution.config.get("codex", {}))

    registry_root = run_registry.ensure_registry(
        Path(execution.source_workspace.path),
        workspace_kind=execution.source_workspace.kind,
    )
    retention.run_retention_pass(registry_root, execution.config)

    return PersistentWorktreePreflight(
        iso_ctx=iso_ctx,
        source_git_root=source_git_root,
        base_oid=base_oid,
        source_git_common_dir=current_git_common_dir or iso_ctx.source_git_common_dir,
        source_head_oid=base_oid,
        source_head_ref=current_head_ref,
        source_branch=current_branch,
        registry_root=registry_root,
        tracked_dirty_files=tracked_dirty_files,
        untracked_files=untracked_files,
    )


def _build_persistent_worktree_run_context(
    execution: PersistentWorktreeExecution,
    preflight: PersistentWorktreePreflight,
    *,
    run_id: str,
    alias: str,
    branch: str,
    worktree_path: str,
    creation_context: JsonObject,
) -> delegate_runner.RunContext:
    request = execution.request
    iso_ctx = preflight.iso_ctx
    dirty_warnings = creation_context.get("includeDirtyWarnings")
    merged_warnings = (
        (*request.warnings, *[w for w in dirty_warnings if isinstance(w, str)])
        if isinstance(dirty_warnings, list)
        else request.warnings
    )
    return delegate_runner.RunContext(
        registry_root=preflight.registry_root,
        run_id=run_id,
        alias=alias,
        harness=request.engine,
        engine=request.engine,
        mode=request.mode,
        model=request.model,
        source_cwd=execution.source_workspace.path,
        execution_cwd=worktree_path,
        workspace_kind=execution.source_workspace.kind,
        isolated_workspace=True,
        started_at=run_registry.utc_now_iso(),
        model_alias=request.model_alias,
        model_resolved=request.model,
        creation_context=creation_context,
        source_git_root=iso_ctx.source_git_root or preflight.source_git_root,
        isolation_mode=iso_ctx.isolation_mode,
        effective_isolation=iso_ctx.effective_isolation,
        isolation_lifecycle=iso_ctx.isolation_lifecycle,
        preserved_workspace=iso_ctx.preserved_workspace,
        branch=branch,
        worktree_status="present",
        warnings=merged_warnings,
        reasoning_effort=request.reasoning_effort,
        reasoning_effort_source=request.reasoning_effort_source,
        reasoning_capability_source=request.reasoning_capability_source,
        reasoning_transport=request.reasoning_transport,
        fast=request.fast,
        prompt_transport=request.prompt_transport,
        forbid_commit=request.forbid_commit,
        progress_initial_delay_sec=request.progress_initial_delay_sec,
        progress_interval_sec=request.progress_interval_sec,
        env_overrides=dict(request.env_overrides or {}),
        fallback_env_overrides=dict(
            profiles.codex_fallback_env_overrides(request.profile_resolution) or {}
        ),
        auth_profile=request.auth_profile,
        fallback_auth_profile=request.fallback_auth_profile,
        include_dirty=bool(creation_context.get("includeDirty")),
        synced_files=int(creation_context.get("syncedFiles") or 0),
        group=request.group,
        prompt_instruction_mode=request.prompt_instruction_mode,
    )


def _register_persistent_worktree_run(
    execution: PersistentWorktreeExecution,
    preflight: PersistentWorktreePreflight,
) -> PersistentWorktreeRegistration:
    request = execution.request
    label = branch_label(request.engine, request.model_alias)

    run_id, alias = run_registry.register_run(
        preflight.registry_root,
        harness=request.engine,
        metadata={
            "mode": request.mode,
            "model": request.model,
            "modelAlias": request.model_alias,
            "modelResolved": request.model,
            "cwd": execution.source_workspace.path,
            "group": request.group,
        },
    )

    short_id = short_run_id(run_id)
    branch = plan_branch_name(label, short_id)
    data_home = worktrees_data_home(execution.config)

    source_git_common_dir = preflight.source_git_common_dir
    if source_git_common_dir is None:
        raise PersistentWorktreeError(
            "worktree_requires_git",
            "--isolation worktree could not determine the Git common directory.",
        )

    fingerprint = compute_repo_fingerprint_from_common_dir(source_git_common_dir)
    worktree_path = str(plan_worktree_path(data_home, fingerprint, label, short_id))

    creation_context: JsonObject = {
        "sourceHeadOid": preflight.source_head_oid,
        "sourceHeadRef": preflight.source_head_ref,
        "sourceBranch": preflight.source_branch,
        "sourceGitCommonDir": source_git_common_dir,
        "branch": branch,
        "plannedBranch": branch,
        "plannedExecutionCwd": worktree_path,
        "label": label,
        "shortRunId": short_id,
        "includeDirty": request.include_dirty,
    }

    pre_ctx = _build_persistent_worktree_run_context(
        execution,
        preflight,
        run_id=run_id,
        alias=alias,
        branch=branch,
        worktree_path=worktree_path,
        creation_context=creation_context,
    )
    run_path = run_registry.run_directory(preflight.registry_root, run_id)
    run_path.mkdir(parents=True, exist_ok=True)
    delegate_runner.write_manifest(
        run_path,
        delegate_runner.build_manifest(pre_ctx, public_argv(request)),
    )

    delegate_runner.write_state(
        run_path,
        delegate_runner.build_state(
            pre_ctx,
            status="creating_isolation",
            extra={"plannedBranch": branch, "plannedExecutionCwd": worktree_path},
        ),
    )

    return PersistentWorktreeRegistration(
        run_id=run_id,
        alias=alias,
        run_path=run_path,
        branch=branch,
        worktree_path=worktree_path,
        creation_context=creation_context,
        pre_ctx=pre_ctx,
    )


def _record_persistent_worktree_failure(
    registration: PersistentWorktreeRegistration,
    *,
    error: str,
    message: str,
) -> None:
    failed_state = delegate_runner.build_state(
        registration.pre_ctx,
        status="failed",
        extra={
            "error": error,
            "message": message,
            "plannedBranch": registration.branch,
            "plannedExecutionCwd": registration.worktree_path,
        },
    )
    delegate_runner.write_state(registration.run_path, failed_state)

    failed_snapshot = delegate_runner.build_snapshot(
        registration.pre_ctx,
        accumulator=harness_events.StreamAccumulator(harness=registration.pre_ctx.harness),
    )
    failed_snapshot["ok"] = False
    failed_snapshot["error"] = error
    failed_snapshot["message"] = message
    failed_snapshot["status"] = "failed"
    failed_snapshot["plannedBranch"] = registration.branch
    failed_snapshot["plannedExecutionCwd"] = registration.worktree_path
    for key in ("executionCwd", "worktreeStatus", "worktreeCleanupCommands", "branch"):
        failed_snapshot.pop(key, None)
    delegate_runner.write_snapshot(registration.run_path, failed_snapshot)


def _create_persistent_worktree_or_record_failure(
    execution: PersistentWorktreeExecution,
    preflight: PersistentWorktreePreflight,
    registration: PersistentWorktreeRegistration,
) -> None:
    try:
        create_persistent_worktree(
            preflight.source_git_root,
            registration.branch,
            registration.worktree_path,
            preflight.base_oid,
        )
        auto_include_dirty = not execution.request.include_dirty and (
            preflight.tracked_dirty_files > 0 or preflight.untracked_files > 0
        )
        if execution.request.include_dirty or auto_include_dirty:
            synced_files, tracked_files, untracked_files, warnings = (
                safe_workspace.sync_git_dirty_snapshot(
                    preflight.source_git_root,
                    registration.worktree_path,
                )
            )
            registration.creation_context["includeDirty"] = True
            registration.creation_context["syncedFiles"] = synced_files
            sync_warnings = list(warnings)
            if auto_include_dirty:
                sync_warnings.insert(
                    0,
                    "dirty_source_auto_included: "
                    f"synced {tracked_files} tracked-modified and {untracked_files} "
                    "untracked file(s).",
                )
            registration.creation_context["includeDirtyWarnings"] = sync_warnings
            if sync_warnings:
                registration.pre_ctx = replace(
                    registration.pre_ctx,
                    warnings=(*registration.pre_ctx.warnings, *sync_warnings),
                    include_dirty=True,
                    synced_files=synced_files,
                )
            else:
                registration.pre_ctx = replace(
                    registration.pre_ctx,
                    include_dirty=True,
                    synced_files=synced_files,
                )
            delegate_runner.write_manifest(
                registration.run_path,
                delegate_runner.build_manifest(
                    registration.pre_ctx,
                    public_argv(execution.request),
                ),
            )
    except IsolationExecutionError as exc:
        _record_persistent_worktree_failure(
            registration,
            error=exc.error,
            message=exc.message,
        )
        if exc.error != "branch_collision":
            _cleanup_partial_worktree(
                preflight.source_git_root,
                registration.worktree_path,
                registration.branch,
                registration.run_path,
                stderr=execution.stderr,
                remove_branch=True,
            )
        raise PersistentWorktreeError(exc.error, exc.message) from exc
    except Exception as exc:
        error = getattr(exc, "error", "worktree_setup_failed")
        message = getattr(exc, "message", str(exc))
        _record_persistent_worktree_failure(
            registration,
            error=str(error),
            message=str(message),
        )
        _cleanup_partial_worktree(
            preflight.source_git_root,
            registration.worktree_path,
            registration.branch,
            registration.run_path,
            stderr=execution.stderr,
            remove_branch=True,
        )
        raise PersistentWorktreeError(str(error), str(message)) from exc


def _request_for_execution_workspace(
    request: Request,
    execution_workspace: str,
) -> ExecutionWorkspaceRequest:
    execution_argv = replace_workspace_arg_in_argv(
        request.engine,
        request.argv,
        execution_workspace,
    )
    execution_stdin_text = request.stdin_text
    execution_prompt_file_text = request.prompt_file_text
    execution_prompt = _persistent_prompt(request.prompt, forbid_commit=request.forbid_commit)
    if request.prompt_transport == PROMPT_TRANSPORT_STDIN:
        execution_stdin_text = _persistent_prompt(
            execution_stdin_text or request.prompt,
            forbid_commit=request.forbid_commit,
        )
    elif request.prompt_transport == PROMPT_TRANSPORT_FILE:
        execution_prompt_file_text = _persistent_prompt(
            execution_prompt_file_text or request.prompt,
            forbid_commit=request.forbid_commit,
        )
    else:
        execution_argv[-1] = execution_prompt
    execution_display_argv = replace_workspace_arg_in_argv(
        request.engine,
        public_argv(request),
        execution_workspace,
    )
    return ExecutionWorkspaceRequest(
        argv=execution_argv,
        display_argv=execution_display_argv,
        stdin_text=execution_stdin_text,
        prompt_file_text=execution_prompt_file_text,
        agent_config_text=request.agent_config_text,
    )


def _persistent_prompt(prompt: str, *, forbid_commit: bool) -> str:
    prompt = prepend_persistent_worktree_context(prompt)
    if not forbid_commit:
        return prompt
    return (
        "Delegate commit policy: --forbid-commit is active for this run. "
        "Do not run `git commit` or create commits. Leave file changes uncommitted; "
        "Delegate will mark the run failed if commits remain ahead of the creation base "
        "when the child exits.\n\n"
        f"{prompt}"
    )


def _launch_child_in_persistent_worktree(
    execution: PersistentWorktreeExecution,
    preflight: PersistentWorktreePreflight,
    registration: PersistentWorktreeRegistration,
) -> tuple[int, JsonObject | None]:
    request = execution.request
    try:
        execution_request = _request_for_execution_workspace(
            request,
            registration.worktree_path,
        )
        execution.binary_validator(execution_request.argv, request.engine)

        exec_ctx = _build_persistent_worktree_run_context(
            execution,
            preflight,
            run_id=registration.run_id,
            alias=registration.alias,
            branch=registration.branch,
            worktree_path=registration.worktree_path,
            creation_context=registration.creation_context,
        )
        delegate_runner.write_manifest(
            registration.run_path,
            delegate_runner.build_manifest(exec_ctx, execution_request.display_argv),
        )
        exit_code, payload = delegate_runner.execute_tracked(
            execution_request.argv,
            registration.worktree_path,
            exec_ctx,
            json_mode=execution.json_mode,
            stdout=execution.stdout,
            stderr=execution.stderr,
            completion_report_mode=execution.completion_report_mode,
            stdin_text=execution_request.stdin_text,
            prompt_file_text=execution_request.prompt_file_text,
            prompt_file_placeholder=PROMPT_FILE_ARG_PLACEHOLDER,
            agent_config_text=execution_request.agent_config_text,
            agent_config_placeholder=DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
            manifest_argv=execution_request.display_argv,
            progress=request.progress,
            progress_initial_delay_sec=request.progress_initial_delay_sec,
            progress_interval_sec=request.progress_interval_sec,
        )
    except Exception as exc:
        error_msg = str(exc)
        error_code = getattr(exc, "error", "execution_failed")
        # _record_persistent_worktree_failure writes both the failed state and
        # the failed snapshot in one consolidated pass; do not write_state here
        # first (that would be a redundant double write of the same failed
        # status). Behavior is identical, with a single state write.
        _record_persistent_worktree_failure(
            registration,
            error=str(error_code),
            message=error_msg,
        )
        raise PersistentWorktreeError(error_code, error_msg) from exc

    run_registry.set_worktree_status(
        preflight.registry_root,
        registration.run_id,
        "present",
    )

    return exit_code, payload


def _cleanup_partial_worktree(
    source_git_root: str,
    worktree_path: str,
    branch: str,
    run_path: Path,
    *,
    stderr: TextIO,
    remove_branch: bool = True,
) -> None:
    path = Path(worktree_path)
    cleanup_failed = False
    if path.exists() or path.is_symlink():
        try:
            result = _run_git(
                source_git_root,
                ["worktree", "remove", "--force", worktree_path],
                timeout_seconds=GIT_MUTATION_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                cleanup_failed = True
        except (OSError, subprocess.SubprocessError):
            cleanup_failed = True
    if remove_branch:
        try:
            result = _run_git(
                source_git_root,
                ["branch", "-D", branch],
                timeout_seconds=GIT_MUTATION_TIMEOUT_SECONDS,
            )
            if result.returncode != 0 and _branch_delete_still_needs_cleanup(
                source_git_root,
                branch,
            ):
                cleanup_failed = True
        except (OSError, subprocess.SubprocessError):
            cleanup_failed = True
    if cleanup_failed:
        commands = [
            shlex.join(
                ["git", "-C", source_git_root, "worktree", "remove", "--force", worktree_path]
            )
        ]
        if remove_branch:
            commands.append(shlex.join(["git", "-C", source_git_root, "branch", "-D", branch]))
        manual = " && ".join(commands)
        snapshot_path = run_path / run_registry.SNAPSHOT_FILE
        metadata_warning: str | None = None
        if snapshot_path.exists():
            try:
                existing = run_registry.read_json_object(snapshot_path)
                if existing is not None:
                    existing["cleanupFailed"] = True
                    existing["manualCleanup"] = manual
                    run_registry.write_json_atomic(snapshot_path, existing)
            except (OSError, ValueError) as exc:
                metadata_warning = (
                    "warning: partial worktree cleanup failed, and Delegate could not "
                    f"record cleanup metadata in {snapshot_path}: {exc}"
                )
        if metadata_warning is not None:
            print(metadata_warning, file=stderr)
        print(
            f"warning: partial worktree cleanup failed; manual cleanup required: {manual}",
            file=stderr,
        )


def _branch_delete_still_needs_cleanup(source_git_root: str, branch: str) -> bool:
    try:
        result = _run_git(
            source_git_root,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode == 0:
        return True
    return result.returncode != 1
