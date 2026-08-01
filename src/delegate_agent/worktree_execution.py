from __future__ import annotations

import shlex
import subprocess  # nosec B404 - persistent worktree cleanup intentionally runs fixed git argv with shell=False.
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from delegate_agent import harness_events, mail, profiles, retention, run_registry, safe_workspace
from delegate_agent import runner as delegate_runner
from delegate_agent.argv_utils import public_argv, replace_workspace_arg_in_argv
from delegate_agent.git_utils import (
    GIT_MUTATION_TIMEOUT_SECONDS,
    GIT_QUICK_TIMEOUT_SECONDS,
    capture_git_metadata,
)
from delegate_agent.git_utils import run_git as _run_git
from delegate_agent.isolation import (
    PERSISTENT_WORKTREE_COMMIT_NOTE,
    PERSISTENT_WORKTREE_CONTEXT_NOTE,
    IsolationContext,
    IsolationExecutionError,
    PoolRootUnreadable,
    branch_label,
    compute_repo_fingerprint_from_common_dir,
    create_persistent_worktree,
    iter_pool_fingerprints,
    plan_branch_name,
    plan_worktree_path,
    require_valid_head,
    short_run_id,
    target_contains_source_root,
    worktrees_data_home,
)
from delegate_agent.json_types import JsonObject, is_non_negative_int
from delegate_agent.prompt_transport import (
    DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
    PERSONA_FILE_ARG_PLACEHOLDER,
    PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_models import Request, ResolvedWorkspace


class PersistentWorktreeError(Exception):
    """User-facing error raised by the persistent worktree execution boundary."""

    def __init__(self, error: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code


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
    dirty_example_paths: tuple[str, ...]
    dirty_snapshot: safe_workspace.DirtySyncSnapshot


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
    persona_file_text: str | None


def execute_persistent_worktree(
    execution: PersistentWorktreeExecution,
) -> tuple[int, JsonObject | None]:
    """Launch a work-mode child in a preserved Delegate-managed git worktree."""
    preflight = _validate_persistent_worktree_request(execution)
    _warn_if_worktree_pool_large(execution.config, execution.stderr)
    registration = _register_persistent_worktree_run(execution, preflight)
    _create_persistent_worktree_or_record_failure(execution, preflight, registration)
    return _launch_child_in_persistent_worktree(execution, preflight, registration)


def _worktree_pool_count(data_home: Path) -> int:
    # ponytail: count-based guardrail only — a full-tree byte walk was slowest
    # exactly when the pool was large, the case the warning exists to catch.
    try:
        return sum(len(fingerprint.worktrees) for fingerprint in iter_pool_fingerprints(data_home))
    except PoolRootUnreadable:
        # An advisory warning must never block a launch: a pool root that is
        # absent (nothing created yet) or unreadable simply has no count to give.
        return 0


def _warn_if_worktree_pool_large(config: JsonObject, stderr: TextIO) -> None:
    worktrees = config.get("worktrees")
    if not isinstance(worktrees, dict):
        return
    threshold = worktrees.get("poolWarnCount")
    if not is_non_negative_int(threshold):
        return
    data_home = worktrees_data_home(config)
    worktree_count = _worktree_pool_count(data_home)
    if worktree_count <= threshold:
        return
    noun = "worktree" if worktree_count == 1 else "worktrees"
    print(
        "WARNING: persistent worktree pool "
        f"{data_home} holds {worktree_count} {noun}; "
        f"worktrees.poolWarnCount={threshold}. Reclaim retained work with "
        "`delegate worktree remove` or `delegate worktree prune`.",
        file=stderr,
    )


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
        dirty_snapshot = safe_workspace.dirty_sync_snapshot(source_git_root)
        tracked_dirty_files = len(dirty_snapshot.diff_names)
        untracked_files = len(dirty_snapshot.untracked_names)
        dirty_submodules = safe_workspace.dirty_submodule_paths(source_git_root)
        dirty_example_paths = dirty_snapshot.example_paths
    except Exception as exc:
        raise PersistentWorktreeError(
            getattr(exc, "error", "dirty_source_check_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc
    if dirty_submodules:
        paths = ", ".join(repr(path) for path in dirty_submodules[:5])
        omitted = max(len(dirty_submodules) - 5, 0)
        suffix = f"; {omitted} more omitted" if omitted else ""
        raise PersistentWorktreeError(
            "dirty_source_workspace",
            "Submodule dirt cannot be synced into persistent worktree isolation "
            f"({paths}{suffix}); Git's diff/apply snapshot does not reproduce gitlink pointer updates. "
            "Commit or stash the submodule changes, or use --isolation none.",
        )

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
        dirty_example_paths=dirty_example_paths,
        dirty_snapshot=dirty_snapshot,
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
        model_requested=request.model_requested,
        capability_model=request.capability_model,
        capability_model_source=request.capability_model_source,
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
        requested_reasoning_effort=request.requested_reasoning_effort,
        reasoning_effort_source=request.reasoning_effort_source,
        reasoning_capability_source=request.reasoning_capability_source,
        reasoning_capability_evidence=request.reasoning_capability_evidence,
        reasoning_transport=request.reasoning_transport,
        fast=request.fast,
        prompt_transport=request.prompt_transport,
        forbid_commit=request.forbid_commit,
        progress_initial_delay_sec=request.progress_initial_delay_sec,
        progress_interval_sec=request.progress_interval_sec,
        env_overrides={
            **(request.env_overrides or {}),
            "DELEGATE_SOURCE_ROOT": str(Path(execution.source_workspace.path).resolve()),
            "DELEGATE_EXECUTION_ROOT": str(Path(worktree_path).resolve()),
            "WORKSPACE_ROOT": str(Path(worktree_path).resolve()),
        },
        fallback_env_overrides=profiles.codex_fallback_child_env_overrides(
            request.profile_resolution,
            {
                **(request.env_overrides or {}),
                "DELEGATE_SOURCE_ROOT": str(Path(execution.source_workspace.path).resolve()),
                "DELEGATE_EXECUTION_ROOT": str(Path(worktree_path).resolve()),
                "WORKSPACE_ROOT": str(Path(worktree_path).resolve()),
            },
        ),
        auth_profile=request.auth_profile,
        fallback_auth_profile=request.fallback_auth_profile,
        include_dirty=bool(creation_context.get("includeDirty")),
        synced_files=int(creation_context.get("syncedFiles") or 0),
        mail_push=request.mail_push,
        group=request.group,
        call_read_only=request.call_read_only or request.pure,
        pure=request.pure,
        prompt_instruction_mode=request.prompt_instruction_mode,
        source_prompt=request.source_prompt,
        progress_requested=request.progress_requested,
        timeout_seconds=request.timeout,
        output_schema_text=request.output_schema_record_text,
        agent=request.agent,
        resumed_from=request.resumed_from,
        persona_name=request.persona_name,
        persona_source=request.persona_source,
        persona_transport=request.persona_transport,
        persona_digest=request.persona_digest,
        persona_file=request.persona_file,
        persona_text=request.persona_text,
    )


def _register_persistent_worktree_run(
    execution: PersistentWorktreeExecution,
    preflight: PersistentWorktreePreflight,
) -> PersistentWorktreeRegistration:
    request = execution.request
    label = branch_label(request.engine, request.model_alias)
    if request.mode == "work":
        mail.prepare_mail_storage(preflight.registry_root)
    mail.sanitize_inherited_mail_identity(request.env_overrides)

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
    child_env = request.env_overrides or {}
    if request.mode == "work":
        mail.bind_mail_identity(child_env, run_id, alias)
    request.env_overrides = child_env
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
    run_registry.ensure_private_dir(run_path)
    if pre_ctx.source_prompt is not None:
        # Written here as well as in _prepare_tracked_run so a worktree-creation
        # failure still leaves a resumable prompt record.
        run_registry.write_private_text(
            run_path / run_registry.PROMPT_TXT_FILE, pre_ctx.source_prompt
        )
    if pre_ctx.persona_text is not None:
        run_registry.write_private_text(
            run_path / run_registry.PERSONA_TXT_FILE, pre_ctx.persona_text
        )
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
    worktree_realized: bool = False,
) -> None:
    extra: JsonObject = {
        "error": error,
        "message": message,
    }
    if worktree_realized:
        # The worktree and branch already exist on disk, so the failure record
        # keeps the realized worktreeStatus for `delegate worktree show|remove`.
        extra["worktreeStatus"] = "present"
    else:
        extra["plannedBranch"] = registration.branch
        extra["plannedExecutionCwd"] = registration.worktree_path
    failed_state = delegate_runner.build_state(
        registration.pre_ctx,
        status="failed",
        extra=extra,
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
    if not worktree_realized:
        # Pre-creation failure: nothing was realized on disk, so strip the
        # realized worktree fields build_snapshot derived from the registration
        # context and record only the planned branch/path. When the worktree
        # was realized, executionCwd, branch, worktreeStatus, and
        # worktreeCleanupCommands stay so the operator can inspect and clean up
        # the preserved worktree.
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
        if auto_include_dirty:
            examples = ", ".join(repr(path) for path in preflight.dirty_example_paths[:5])
            print(
                "Auto-including dirty source into persistent worktree: "
                f"{preflight.tracked_dirty_files} tracked-modified and "
                f"{preflight.untracked_files} untracked file(s). "
                f"Examples: {examples}.",
                file=execution.stderr,
            )
        if execution.request.include_dirty or auto_include_dirty:
            synced_files, tracked_files, untracked_files, warnings = (
                safe_workspace.sync_git_dirty_snapshot(
                    preflight.source_git_root,
                    registration.worktree_path,
                    snapshot=preflight.dirty_snapshot,
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
    execution_prompt = (
        request.argv[-1]
        if request.prompt_transport not in (PROMPT_TRANSPORT_STDIN, PROMPT_TRANSPORT_FILE)
        else request.prompt
    )
    if (
        request.isolation_context is not None
        and request.isolation_context.isolation_lifecycle
        in (
            "persistent",
            "attached",
        )
        and not request.persistent_worktree_notes_framed
    ):
        execution_prompt = _persistent_prompt(request, execution_prompt)
        if request.prompt_transport == PROMPT_TRANSPORT_STDIN:
            execution_stdin_text = _persistent_prompt(
                request, execution_stdin_text or execution_prompt
            )
        elif request.prompt_transport == PROMPT_TRANSPORT_FILE:
            execution_prompt_file_text = _persistent_prompt(
                request, execution_prompt_file_text or execution_prompt
            )
    if request.prompt_transport == PROMPT_TRANSPORT_STDIN:
        execution_stdin_text = execution_stdin_text or request.prompt
    elif request.prompt_transport == PROMPT_TRANSPORT_FILE:
        execution_prompt_file_text = execution_prompt_file_text or request.prompt
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
        persona_file_text=(
            request.persona_text if request.persona_transport == "native-file" else None
        ),
    )


def _persistent_prompt(request: Request, prompt: str) -> str:
    """Structurally frame a legacy request missing persistent-worktree notes."""
    from delegate_agent.request_build import effective_prompt

    worktree_note = PERSISTENT_WORKTREE_CONTEXT_NOTE
    if request.forbid_commit:
        worktree_note = f"{PERSISTENT_WORKTREE_COMMIT_NOTE}\n\n{worktree_note}"
    return effective_prompt(
        request.source_prompt if request.source_prompt is not None else prompt,
        engine=request.engine,
        mode=request.mode,
        completion_report_mode=request.completion_report_mode,
        instruction_mode=request.prompt_instruction_mode,
        persona_text=(request.persona_text if request.persona_transport == "prepend" else None),
        worktree_note=worktree_note,
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
        provision: mail.MailPushProvision | None = None
        if request.mode == "work":
            wired_argv, wired_display_argv = mail.wire_work_mail_launch(
                request.engine,
                execution_request.argv,
                execution_request.display_argv,
                preflight.registry_root,
                prompt=request.prompt,
                prompt_transport=request.prompt_transport,
                stderr=execution.stderr,
                isolated_workspace=True,
            )
            execution_request = replace(
                execution_request, argv=wired_argv, display_argv=wired_display_argv
            )
            if request.mail_push:
                provision = mail.provision_mail_push(
                    request.engine,
                    execution_request.argv,
                    execution_request.display_argv,
                    preflight.registry_root,
                    registration.run_id,
                    request.env_overrides or {},
                    profiles.codex_fallback_child_env_overrides(
                        request.profile_resolution, request.env_overrides or {}
                    ),
                )
                if provision.warning is not None:
                    request.warnings = (*request.warnings, provision.warning)
                    print(f"delegate mail: WARNING: {provision.warning}", file=execution.stderr)
                execution_request = replace(
                    execution_request, argv=provision.argv, display_argv=provision.display_argv
                )
        if request.resumed_from is not None:
            from delegate_agent.resume_command import enforce_resume_prompt_size

            enforce_resume_prompt_size(request.engine, execution_request.argv[-1])
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
        if provision is not None:
            exec_ctx = replace(
                exec_ctx,
                env_overrides=(
                    dict(provision.env)
                    if provision.warning is not None and provision.env is not None
                    else exec_ctx.env_overrides
                ),
                fallback_env_overrides=mail.mail_push_fallback_env_overrides(
                    provision,
                    exec_ctx.fallback_env_overrides,
                    preflight.registry_root,
                    registration.run_id,
                ),
                warnings=tuple(
                    dict.fromkeys(
                        (*exec_ctx.warnings, *((provision.warning,) if provision.warning else ()))
                    )
                ),
            )
            if provision.warning is not None and provision.env is not None:
                exec_ctx = replace(exec_ctx, env_overrides=dict(provision.env))
        if exec_ctx.persona_text is not None:
            run_registry.write_private_text(
                registration.run_path / run_registry.PERSONA_TXT_FILE,
                exec_ctx.persona_text,
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
            persona_file_text=execution_request.persona_file_text,
            persona_file_placeholder=PERSONA_FILE_ARG_PLACEHOLDER,
            manifest_argv=execution_request.display_argv,
            progress=request.progress,
            progress_initial_delay_sec=request.progress_initial_delay_sec,
            progress_interval_sec=request.progress_interval_sec,
            timeout=request.timeout,
        )
    except Exception as exc:
        if provision is not None:
            try:
                mail.cleanup_mail_push_private_homes(preflight.registry_root, registration.run_id)
            except OSError:
                warning = delegate_runner.MAIL_PUSH_CLEANUP_WARNING
                request.warnings = (*request.warnings, warning)
                registration.pre_ctx = replace(
                    registration.pre_ctx,
                    warnings=(*registration.pre_ctx.warnings, warning),
                )
                print(f"delegate mail: WARNING: {warning}", file=execution.stderr)
        error_msg = str(exc)
        error_code = getattr(exc, "error", "execution_failed")
        state = run_registry.load_run_state_or_none(preflight.registry_root, registration.run_id)
        if isinstance(state, dict) and state.get("status") in {
            run_registry.STATUS_FAILED,
            run_registry.STATUS_CANCELLED,
            run_registry.STATUS_SUCCEEDED,
        }:
            # execute_tracked already finalized the run. Preserve its captured
            # output, report, quality, and terminal metadata; only add the
            # realized worktree state needed for later inspection/removal.
            run_registry.set_worktree_status(
                preflight.registry_root,
                registration.run_id,
                "present",
            )
        else:
            _record_persistent_worktree_failure(
                registration,
                error=str(error_code),
                message=error_msg,
                worktree_realized=True,
            )
        raise PersistentWorktreeError(
            error_code,
            error_msg,
            getattr(exc, "exit_code", 2),
        ) from exc

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
    guarded = target_contains_source_root(worktree_path, source_git_root)
    if guarded:
        cleanup_failed = True
    elif path.exists() or path.is_symlink():
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
    if remove_branch and not guarded:
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
        if guarded:
            manual = "source_root_guard refused unsafe cleanup; inspect run metadata"
        else:
            commands = [
                shlex.join(
                    [
                        "git",
                        "-C",
                        source_git_root,
                        "worktree",
                        "remove",
                        "--force",
                        worktree_path,
                    ]
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
                    if guarded:
                        existing["cleanupRefused"] = "source_root_guard"
                    run_registry.write_snapshot(run_path, existing)
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
