#!/usr/bin/env python3
from __future__ import annotations

import json  # noqa: F401  # re-exported for tests (delegate.json)
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import TextIO

from delegate_agent import (
    VERSION,
    argv_utils,
    capability_commands,
    command_errors,
    command_help,  # noqa: F401  # re-exported for tests / back-compat
    config_commands,
    harness_discovery,
    inspection_commands,
    profile_commands,
    profile_guard,
    profiles,
    reasoning,
    redaction,
    run_metadata,
    run_output_commands,
    run_registry,
    setup_commands,
    wait_cancel_commands,
    worktree_commands,
    worktree_execution,
    worktree_mgmt,
    wsl,
)
from delegate_agent import config as delegate_config
from delegate_agent import rendering as delegate_rendering
from delegate_agent import retention as delegate_retention
from delegate_agent import runner as delegate_runner
from delegate_agent.argv_builders import (  # noqa: F401  # re-exported for tests / back-compat
    SAFE_REVIEW_PREFIX_BY_ENGINE,
    _claude_harness_bypass_enabled,
    _grok_harness_bypass_enabled,
    build_claude_argv,
    build_codex_argv,
    build_cursor_argv,
    build_devin_argv,
    build_droid_argv,
    build_grok_argv,
    build_kimi_argv,
    build_omp_argv,
    build_opencode_argv,
    build_pi_argv,
    prefix_cursor_safe_prompt,
    prefix_droid_safe_prompt,
    redacted_prompt_argv,
)
from delegate_agent.argv_utils import public_argv
from delegate_agent.cli_parser import (  # noqa: F401  # re-exported for tests / back-compat
    has_misplaced_global_option,
    infer_global_json,
    parse_cli,
    parse_required_positive_int_option,
)
from delegate_agent.constants import (
    BINARY_CONFIG_ENGINES,
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    PROMPT_INSTRUCTION_MODE_SLASH,
)
from delegate_agent.describe_payload import (  # noqa: F401  # re-exported for tests / back-compat
    _call_overview_text,
    _claude_runtime_policy,
    describe_payload,
    emit_agent_help,
    emit_command_help,
    emit_describe,
    emit_models,
    models_payload,
)
from delegate_agent.errors import (
    EXIT_MISSING_BINARY,
    EXIT_OK,
    EXIT_USAGE,
    DelegateError,
)
from delegate_agent.git_utils import capture_git_metadata  # noqa: F401  # re-exported for tests
from delegate_agent.isolation import (  # noqa: F401  # re-exported for tests
    IsolationContext,
    build_isolation_context,
    target_contains_source_root,
)
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import (  # noqa: F401  # CURSOR_PROMPT_REDACTION re-exported for tests
    CURSOR_PROMPT_REDACTION,
    DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
    DEVIN_AGENT_CONFIG_DISPLAY,
    DROID_PROMPT_FILE_ARG_PLACEHOLDER,
    DROID_PROMPT_FILE_DISPLAY,
    KIMI_PROMPT_REDACTION,
    PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_FILE_DISPLAY,
    PROMPT_TRANSPORT_ARGV,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_build import (  # noqa: F401  # re-exported for tests / back-compat
    CALL_TEMP_CWD_PLACEHOLDER,
    RUN_INPUT_KEYS,
    _load_input_json_object,
    _resolve_default_model,
    build_request,
    effective_prompt,
    load_config,
    read_stdin_source,
    request_from_input_json,
    request_from_parsed,
    resolve_completion_report_mode,
    resolve_prompt,
    resolve_workspace,
    validate_config,
    validate_prompt,
)
from delegate_agent.request_models import (  # noqa: F401  # re-exported for tests / back-compat
    GlobalOptions,
    InspectionOptions,
    LaunchOptions,
    ParsedCommand,
    Request,
    ResolvedWorkspace,
    RunJsonOptions,
)
from delegate_agent.safe_workspace import (  # noqa: F401  # re-exported for tests / back-compat
    CURSOR_SAFE_CLI_CONFIG,
    SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
    SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX,
    SAFE_UNBORN_GIT_WARNING,
    block_external_symlinks,
    cleanup_safe_isolated_workspace,
    create_directory_safe_workspace,
    create_git_safe_workspace,
    external_symlink_warnings,
    mirror_path_preserving_symlinks,
    read_git_tracked_diff,
    safe_isolated_request,
    write_cursor_safe_project_config,
)
from delegate_agent.workflows import commands as workflow_commands

_replace_ws_by_engine = argv_utils.replace_workspace_arg_in_argv

DEFAULT_CONFIG = delegate_config.embedded_default_config()
CONFIG_ENV = delegate_config.CONFIG_ENV

MISSING_BINARY_PROBE_DIRS = (
    "~/.claude/local",
    "~/.grok/bin",
    "~/.kimi-code/bin",
    "~/.local/bin",
    "~/bin",
    "/opt/homebrew/bin",
)


HELP = _call_overview_text()


def config_path() -> Path:
    return delegate_config.config_path()


def workspace_path_for_config(global_cwd: str | None) -> Path | None:
    try:
        return Path(resolve_workspace(global_cwd).path)
    except DelegateError:
        return None


def maybe_run_retention_pass(registry_root: Path, config: JsonObject) -> None:
    delegate_retention.run_retention_pass(registry_root, config)


def emit_snapshot(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    command = parsed.snapshot
    if command is None:
        raise DelegateError("invalid_command", "snapshot options are required.")
    return inspection_commands.emit_snapshot(
        command,
        workspace_path=workspace.path,
        stdout=stdout,
    )


def emit_runs(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    command = parsed.runs
    if command is None:
        raise DelegateError("invalid_command", "runs options are required.")
    return inspection_commands.emit_runs(
        command,
        workspace_path=workspace.path,
        stdout=stdout,
    )


RECOVERY_STDOUT_TAIL_LINES = run_output_commands.RECOVERY_STDOUT_TAIL_LINES
RECOVERY_STDOUT_TAIL_BYTES = run_output_commands.RECOVERY_STDOUT_TAIL_BYTES
RUN_OUTPUT_DEFAULT_TAIL_LINES = run_output_commands.RUN_OUTPUT_DEFAULT_TAIL_LINES


def emit_run_output(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    command = parsed.run_output
    if command is None:
        raise DelegateError("invalid_command", "run-output options are required.")
    return run_output_commands.emit(
        command,
        workspace_path=workspace.path,
        stdout=stdout,
    )


def emit_wait(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    command = parsed.wait_command
    if command is None:
        raise DelegateError("invalid_command", "wait options are required.")
    return wait_cancel_commands.emit_wait(command, workspace_path=workspace.path, stdout=stdout)


def emit_cancel(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    command = parsed.cancel_command
    if command is None:
        raise DelegateError("invalid_command", "cancel options are required.")
    return wait_cancel_commands.emit_cancel(command, workspace_path=workspace.path, stdout=stdout)


def emit_worktree(
    parsed: ParsedCommand,
    workspace: ResolvedWorkspace,
    config: JsonObject,
    stdout: TextIO,
) -> int:
    command = parsed.worktree
    if command is None:
        raise DelegateError("invalid_command", "worktree options are required.")
    return worktree_commands.emit(
        command,
        workspace_path=workspace.path,
        config=config,
        stdout=stdout,
    )


def emit_workflow(
    parsed: ParsedCommand,
    workspace: ResolvedWorkspace,
    config: JsonObject,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    command = parsed.workflow_command
    if command is None:
        raise DelegateError("invalid_command", "workflow options are required.")
    return workflow_commands.emit(
        command,
        workspace_path=workspace.path,
        config=config,
        stdout=stdout,
        stderr=stderr,
    )


def emit_profiles_command(
    parsed: ParsedCommand,
    config: JsonObject,
    config_source: str,
    stdout: TextIO,
) -> int:
    command = parsed.profiles_command
    if command is None:
        raise DelegateError("invalid_command", "profiles options are required.")
    resolution = profiles.resolve_active_profile(
        config,
        os.environ,
        cli_override=parsed.global_options.auth_profile,
    )
    return profile_commands.emit(
        command,
        resolution=resolution,
        config_source=config_source,
        stdout=stdout,
    )


def emit_config_command(parsed: ParsedCommand, stdout: TextIO) -> int:
    command = parsed.config_command
    if command is None:
        raise DelegateError("invalid_command", "config options are required.")
    return config_commands.emit(command, stdout)


def dry_run_payload(request: Request) -> JsonObject:
    payload: JsonObject = {
        "ok": True,
        "dryRun": True,
        "cwd": request.workspace,
        "workspaceRoot": request.workspace,
        "workspaceKind": request.workspace_kind,
        "engine": request.engine,
        "mode": request.mode,
        "model": request.model,
        "argv": public_argv(request),
        "promptTransport": request.prompt_transport,
        "promptInstructionMode": request.prompt_instruction_mode,
    }
    run_metadata.add_model_payload_fields(payload, request)
    reasoning.add_reasoning_payload_fields(payload, request)
    run_metadata.add_speed_payload_fields(payload, request)
    if request.warnings:
        payload["warnings"] = list(request.warnings)
    if request.progress:
        payload["progressRequested"] = True
    if request.forbid_commit:
        payload["commitPolicy"] = {"forbidCommit": True}
    if request.include_dirty:
        payload["includeDirty"] = True
    if request.group is not None:
        payload["group"] = request.group
    if request.auth_profile is not None:
        payload["authProfile"] = request.auth_profile
    if request.fallback_auth_profile is not None:
        payload["fallbackProfile"] = request.fallback_auth_profile
    if request.profile_resolution.name is not None:
        payload["profileEnv"] = redaction.redact_env_map(request.profile_resolution.env)

    # Structured isolation fields from the isolation context.
    if request.isolation_context is not None:
        ctx = request.isolation_context

        # Keep the existing human-readable `isolation` note for legacy use.
        if ctx.effective_isolation == "worktree" and ctx.isolation_lifecycle == "persistent":
            payload["isolation"] = "worktree persistent"
        elif ctx.effective_isolation == "worktree":
            payload["isolation"] = "worktree temporary"
        elif ctx.effective_isolation == "none":
            payload["isolation"] = "none"
        else:
            payload["isolation"] = "source workspace"

        payload["isolationMode"] = ctx.isolation_mode
        payload["effectiveIsolation"] = ctx.effective_isolation
        payload["isolationLifecycle"] = ctx.isolation_lifecycle
        payload["preservedWorkspace"] = ctx.preserved_workspace

        # Only persistent worktree isolation has a planned Delegate-managed
        # branch/path. Temporary safe-mode isolation is created ephemerally at
        # execution time and must not claim a persistent worktree plan.
        if ctx.isolation_lifecycle == "persistent":
            planned_cwd = ctx.planned_execution_cwd or "<planned-worktree-path>"
            planned_branch = ctx.planned_branch or "<planned-branch>"
            payload["plannedExecutionCwd"] = planned_cwd
            payload["plannedBranch"] = planned_branch
            # Rewrite argv to show the planned workspace path, not the source.
            payload["argv"] = _replace_ws_by_engine(request.engine, payload["argv"], planned_cwd)
        else:
            payload["plannedExecutionCwd"] = None
            payload["plannedBranch"] = None

        # Always emit isolatedWorkspace as explicit boolean (mirrors preservedWorkspace).
        payload["isolatedWorkspace"] = ctx.isolation_lifecycle in ("temporary", "persistent")
    else:
        # Fallback when no isolation context is provided (e.g. direct build_request calls in tests).
        # Use embedded-default logic: safe local harnesses -> worktree temporary, others -> none.
        if request.mode == MODE_CALL:
            payload["isolatedWorkspace"] = False
            payload["isolation"] = "call temporary cwd"
            payload["isolationMode"] = "none"
            payload["effectiveIsolation"] = "none"
            payload["isolationLifecycle"] = "none"
            payload["preservedWorkspace"] = False
        elif request.engine in KNOWN_ENGINES and request.mode == MODE_SAFE:
            payload["isolatedWorkspace"] = True
            payload["isolation"] = (
                "Execution uses a temporary detached git worktree or directory copy; "
                "the child runs outside the original workspace; tracked runs may write .delegate metadata."
            )
            payload["isolationMode"] = "worktree"
            payload["effectiveIsolation"] = "worktree"
            payload["isolationLifecycle"] = "temporary"
            payload["preservedWorkspace"] = False
        else:
            payload["isolatedWorkspace"] = False
            payload["isolation"] = "source workspace"
            payload["isolationMode"] = "none"
            payload["effectiveIsolation"] = "none"
            payload["isolationLifecycle"] = "none"
            payload["preservedWorkspace"] = False
        payload["plannedExecutionCwd"] = None
        payload["plannedBranch"] = None

    return payload


def _binary_config_key(engine: str | None) -> str | None:
    if engine == "cursor":
        return "cursor.argvPrefix"
    if engine in BINARY_CONFIG_ENGINES:
        return f"{engine}.binary"
    return None


def _binary_config_path(config_source: str | None) -> str:
    if config_source and config_source not in {"embedded-default", "cli-overrides"}:
        return config_source
    return str(config_path())


def _suggest_binary_path(binary: str) -> str | None:
    if not binary or os.path.isabs(binary) or os.sep in binary:
        return None
    for directory in MISSING_BINARY_PROBE_DIRS:
        candidate = Path(directory).expanduser() / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _missing_binary_error(
    binary: str,
    *,
    engine: str | None = None,
    config_source: str | None = None,
    search_context: str = "delegate process",
) -> DelegateError:
    config_key = _binary_config_key(engine)
    config_path_hint = _binary_config_path(config_source)
    suggested = _suggest_binary_path(binary)

    parts = [
        f"Missing binary: {binary}",
        f"(searched PATH of the {search_context}, not your interactive shell).",
    ]
    if config_key is not None:
        parts.append(f"Fix: set an absolute path in {config_path_hint} under {config_key!r}.")
    else:
        parts.append("Fix: set the configured binary to an absolute path or add it to PATH.")
    if suggested is not None:
        parts.append(f"Found candidate at {suggested}; set the config value to that absolute path.")

    diagnostics: JsonObject = {
        "binary": binary,
        "configPath": config_path_hint,
    }
    if config_key is not None:
        diagnostics["configKey"] = config_key
    if suggested is not None:
        diagnostics["suggestedBinaryPath"] = suggested

    next_actions = [
        f"command -v {shlex.quote(binary)}",
    ]
    if config_key is not None:
        next_actions.append(f"set {config_key} to an absolute path in {config_path_hint}")

    return DelegateError(
        "missing_binary",
        " ".join(parts),
        EXIT_MISSING_BINARY,
        diagnostics=diagnostics,
        next_actions=next_actions,
    )


def ensure_binary(
    argv: list[str],
    *,
    engine: str | None = None,
    config_source: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> None:
    if not argv:
        raise DelegateError("missing_binary", "Empty argv.", EXIT_MISSING_BINARY)
    env = profiles.child_environment(overrides=env_overrides) if env_overrides else os.environ
    if shutil.which(argv[0], path=env.get("PATH")) is None:
        search_context = (
            "child environment" if env_overrides and "PATH" in env_overrides else "delegate process"
        )
        raise _missing_binary_error(
            argv[0],
            engine=engine,
            config_source=config_source,
            search_context=search_context,
        )


def make_run_context(
    registry_root: Path,
    request: Request,
    *,
    run_id: str,
    alias: str,
    source_workspace: ResolvedWorkspace,
    creation_context: JsonObject | None = None,
) -> delegate_runner.RunContext:
    source_cwd = (
        request.isolation_context.source_workspace
        if request.isolation_context is not None
        else source_workspace.path
    )
    execution_cwd = request.workspace
    # isolated_workspace must reflect the EFFECTIVE behavior, not the
    # mere presence of an isolation_context object.  Only "temporary" or
    # "persistent" lifecycle means a physically separate execution workspace.
    isolated_workspace = (
        request.isolation_context.isolation_lifecycle in ("temporary", "persistent")
        if request.isolation_context is not None
        else False
    )
    # Extract isolation metadata from the isolation context.
    iso_ctx = request.isolation_context
    if iso_ctx is not None:
        isolation_mode = iso_ctx.isolation_mode
        effective_isolation = iso_ctx.effective_isolation
        isolation_lifecycle = iso_ctx.isolation_lifecycle
        preserved_workspace = iso_ctx.preserved_workspace
        branch = iso_ctx.planned_branch
        source_git_root = iso_ctx.source_git_root
        safe_workspace_method = iso_ctx.safe_workspace_method
        warnings = iso_ctx.warnings
    else:
        isolation_mode = "none"
        effective_isolation = "none"
        isolation_lifecycle = "none"
        preserved_workspace = False
        branch = None
        source_git_root = None
        safe_workspace_method = None
        warnings = ()

    return delegate_runner.RunContext(
        registry_root=registry_root,
        run_id=run_id,
        alias=alias,
        harness=request.engine,
        engine=request.engine,
        mode=request.mode,
        model=request.model,
        source_cwd=source_cwd,
        execution_cwd=execution_cwd,
        workspace_kind=source_workspace.kind,
        isolated_workspace=isolated_workspace,
        started_at=run_registry.utc_now_iso(),
        model_alias=request.model_alias,
        model_resolved=request.model,
        model_requested=request.model_requested,
        capability_model=request.capability_model,
        capability_model_source=request.capability_model_source,
        creation_context=creation_context,
        source_git_root=source_git_root,
        isolation_mode=isolation_mode,
        effective_isolation=effective_isolation,
        isolation_lifecycle=isolation_lifecycle,
        preserved_workspace=preserved_workspace,
        branch=branch,
        safe_workspace_method=safe_workspace_method,
        warnings=(*warnings, *request.warnings),
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
        env_overrides=dict(request.env_overrides or {}),
        fallback_env_overrides=profiles.codex_fallback_child_env_overrides(
            request.profile_resolution,
            request.env_overrides,
        ),
        auth_profile=request.auth_profile,
        fallback_auth_profile=request.fallback_auth_profile,
        include_dirty=request.include_dirty,
        group=request.group,
        call_read_only=request.call_read_only or request.pure,
        pure=request.pure,
        prompt_instruction_mode=request.prompt_instruction_mode,
    )


def _set_child_root_env(request: Request, source_workspace: ResolvedWorkspace) -> None:
    execution_root = str(Path(request.workspace).resolve(strict=False))
    source_root = (
        execution_root
        if request.mode == MODE_CALL
        else str(Path(source_workspace.path).resolve(strict=False))
    )
    env = {
        **(request.env_overrides or {}),
        "DELEGATE_SOURCE_ROOT": source_root,
        "WORKSPACE_ROOT": execution_root,
    }
    if execution_root != source_root:
        env["DELEGATE_EXECUTION_ROOT"] = execution_root
    else:
        env.pop("DELEGATE_EXECUTION_ROOT", None)
    request.env_overrides = env


def _artifact_files(root: Path) -> list[str]:
    return sorted(str(item) for item in root.rglob("*") if item.is_file() or item.is_symlink())


def _preserve_call_artifacts(
    workspace: Path,
    source_workspace: ResolvedWorkspace,
    artifact_id: str | None,
) -> tuple[str, list[str], str | None] | None:
    if not any(workspace.iterdir()):
        return None
    try:
        registry_root = run_registry.ensure_registry(
            Path(source_workspace.path), workspace_kind=source_workspace.kind
        )
        artifacts_root = registry_root / "artifacts"
        run_registry.ensure_private_dir(artifacts_root)
        destination = artifacts_root / (artifact_id or run_registry.generate_run_id())
        shutil.move(str(workspace), destination)
        return str(destination), _artifact_files(destination), None
    except (OSError, run_registry.RegistryJsonError) as exc:
        return (
            str(workspace),
            _artifact_files(workspace),
            f"Could not move call artifacts into Delegate state; retained temp cwd at "
            f"{workspace}: {exc}",
        )


def execute_request(
    request: Request,
    json_mode: bool,
    *,
    config: JsonObject,
    config_source: str | None = None,
    pass_through: bool,
    completion_report_mode: str,
    source_workspace: ResolvedWorkspace,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[int, JsonObject | None]:
    _set_child_root_env(request, source_workspace)
    if request.mode == MODE_CALL:
        call_response: tuple[int, JsonObject | None] | None = None
        artifact_id: str | None = None
        try:
            if request.engine == "codex":
                profiles.preflight_codex_request(request, config.get("codex", {}))
            if pass_through:
                raise DelegateError(
                    "invalid_option_combination",
                    "--pass-through is not supported with call mode.",
                )
            ensure_binary(
                request.argv,
                engine=request.engine,
                config_source=config_source,
                env_overrides=request.env_overrides,
            )
            # Group-tagged call runs register in the invocation workspace so
            # workflow kill/adopt/group scans can see them; execution stays in
            # the throwaway call cwd. Plain (ungrouped) call stays untracked.
            if request.group is not None:
                registry_root = run_registry.ensure_registry(
                    Path(source_workspace.path),
                    workspace_kind=source_workspace.kind,
                )
                maybe_run_retention_pass(registry_root, config)
                run_id, alias = run_registry.register_run(
                    registry_root,
                    harness=request.engine,
                    metadata={
                        "mode": request.mode,
                        "model": request.model,
                        "modelAlias": request.model_alias,
                        "modelResolved": request.model,
                        "group": request.group,
                        "workflowAgentKey": request.workflow_agent_key,
                        "cwd": source_workspace.path,
                    },
                )
                artifact_id = run_id
                ctx_runner = make_run_context(
                    registry_root,
                    request,
                    run_id=run_id,
                    alias=alias,
                    source_workspace=source_workspace,
                )
                try:
                    call_response = delegate_runner.execute_tracked(
                        request.argv,
                        request.workspace,
                        ctx_runner,
                        json_mode=json_mode,
                        stdout=stdout,
                        stderr=stderr,
                        completion_report_mode=completion_report_mode,
                        stdin_text=request.stdin_text,
                        prompt_file_text=request.prompt_file_text,
                        prompt_file_placeholder=PROMPT_FILE_ARG_PLACEHOLDER,
                        agent_config_text=request.agent_config_text,
                        agent_config_placeholder=DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
                        manifest_argv=public_argv(request),
                        progress=False,
                        timeout=request.timeout,
                    )
                    return call_response
                except delegate_runner.RunnerLaunchError as exc:
                    raise DelegateError(exc.error, exc.message, exc.exit_code) from exc
            try:
                sensitive_texts = [request.prompt]
                if "--json-schema" in request.argv:
                    schema_index = request.argv.index("--json-schema") + 1
                    if schema_index < len(request.argv):
                        sensitive_texts.append(request.argv[schema_index])
                if request.engine == "codex" and "--output-schema" in request.argv:
                    schema_index = request.argv.index("--output-schema") + 1
                    if schema_index < len(request.argv):
                        sensitive_texts.append(request.argv[schema_index])
                result = delegate_runner.execute_call(
                    request.argv,
                    request.workspace,
                    harness=request.engine,
                    stdin_text=request.stdin_text,
                    prompt_file_text=request.prompt_file_text,
                    prompt_file_placeholder=PROMPT_FILE_ARG_PLACEHOLDER,
                    agent_config_text=request.agent_config_text,
                    agent_config_placeholder=DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
                    env_overrides=request.env_overrides,
                    read_only=request.call_read_only,
                    pure=request.pure,
                    prompt_instruction_mode=request.prompt_instruction_mode,
                    timeout=request.timeout,
                    structured_output=request.output_schema is not None,
                    sensitive_texts=tuple(sensitive_texts),
                )
            except delegate_runner.RunnerLaunchError as exc:
                raise DelegateError(exc.error, exc.message, exc.exit_code) from exc
            status = delegate_runner.status_from_exit(result.exit_code)
            if json_mode:
                payload: JsonObject = {
                    "ok": result.exit_code == 0,
                    "status": status,
                    "exitCode": result.exit_code,
                    "engine": request.engine,
                    "mode": request.mode,
                    "workspaceRoot": (request.env_overrides or {}).get(
                        "WORKSPACE_ROOT", request.workspace
                    ),
                    "model": request.model,
                    "pure": request.pure,
                    "structuredOutput": request.output_schema is not None,
                    "modelRequested": request.model_requested,
                    "modelResolved": (
                        result.model_resolved
                        if request.engine == "claude"
                        else request.model
                        if request.engine == "codex"
                        else None
                    ),
                    "usage": result.usage,
                    "text": result.text,
                    "textChars": result.text_chars,
                    "textTruncated": result.text_truncated,
                    "stdoutBytes": result.stdout_bytes,
                    "stderrBytes": result.stderr_bytes,
                    "durationMs": result.duration_ms,
                }
                run_metadata.add_model_payload_fields(payload, request)
                if request.engine in {"pi", "omp"}:
                    # Keep the established call-mode `text` field while giving
                    # Pi-family engines the same assistantText contract as tracked modes.
                    payload["assistantText"] = result.text
                if request.warnings:
                    payload["warnings"] = list(request.warnings)
                if result.warnings:
                    # Dedupe while preserving order so a warning present in both
                    # request.warnings and result.warnings is not emitted twice.
                    merged: list[str] = []
                    seen: set[str] = set()
                    for warning in [*payload.get("warnings", []), *result.warnings]:
                        if isinstance(warning, str) and warning not in seen:
                            seen.add(warning)
                            merged.append(warning)
                    payload["warnings"] = merged
                if result.empty_retry_attempted:
                    payload["resultQuality"] = result.result_quality
                    payload["emptyRetry"] = {
                        "attempted": True,
                        "resolved": result.empty_retry_resolved,
                    }
                    if result.stderr_tail:
                        payload["stderrTail"] = result.stderr_tail
                reasoning.add_reasoning_payload_fields(payload, request)
                run_metadata.add_speed_payload_fields(payload, request)
                if result.exit_code != 0:
                    payload["error"] = result.error or "child_failed"
                    if result.message:
                        payload["message"] = result.message
                    elif result.error == "call_output_invalid":
                        payload["message"] = "Child output was invalid."
                    else:
                        payload["message"] = "Child command failed."
                    payload["stderrTail"] = result.stderr_tail
                call_response = (result.exit_code, payload)
                return call_response
            for warning in result.warnings:
                print(f"warning: {warning}", file=stderr)
            if result.exit_code != 0:
                if result.message:
                    print(result.message, file=stderr)
                elif result.stderr_tail:
                    print(result.stderr_tail, file=stderr)
            if result.text:
                print(result.text, file=stdout)
            call_response = (result.exit_code, None)
            return call_response
        finally:
            if request.cleanup_workspace:
                if target_contains_source_root(request.workspace, source_workspace.path):
                    raise DelegateError(
                        "source_root_guard",
                        "Refusing to remove a call workspace that is or contains the source root.",
                    )
                preserved = _preserve_call_artifacts(
                    Path(request.workspace), source_workspace, artifact_id
                )
                if preserved is None:
                    shutil.rmtree(request.workspace, ignore_errors=True)
                else:
                    artifacts_path, artifacts, warning = preserved
                    payload = call_response[1] if call_response is not None else None
                    if payload is not None:
                        payload["artifactsPath"] = artifacts_path
                        payload["preservedArtifacts"] = artifacts
                        if warning is not None:
                            payload.setdefault("warnings", []).append(warning)
                    else:
                        print(f"preserved call artifacts: {artifacts_path}", file=stderr)
                        if warning is not None:
                            print(f"warning: {warning}", file=stderr)
    if request.engine == "codex":
        profiles.preflight_codex_request(request, config.get("codex", {}))
    ctx = request.isolation_context

    # --- Persistent worktree path (work + worktree) ---
    if ctx is not None and ctx.isolation_lifecycle == "persistent":
        try:
            return worktree_execution.execute_persistent_worktree(
                worktree_execution.PersistentWorktreeExecution(
                    request=request,
                    json_mode=json_mode,
                    config=config,
                    pass_through=pass_through,
                    completion_report_mode=completion_report_mode,
                    source_workspace=source_workspace,
                    stdout=stdout,
                    stderr=stderr,
                    binary_validator=lambda argv, engine: ensure_binary(
                        argv,
                        engine=engine,
                        config_source=config_source,
                        env_overrides=request.env_overrides,
                    ),
                )
            )
        except worktree_execution.PersistentWorktreeError as exc:
            raise DelegateError(exc.error, exc.message, exc.exit_code) from exc

    with safe_isolated_request(request) as isolated_request:
        _set_child_root_env(isolated_request, source_workspace)
        ensure_binary(
            isolated_request.argv,
            engine=isolated_request.engine,
            config_source=config_source,
            env_overrides=isolated_request.env_overrides,
        )
        if pass_through:
            if json_mode:
                raise DelegateError(
                    "invalid_option_combination",
                    "--pass-through is incompatible with --json.",
                )
            try:
                exit_code = delegate_runner.execute_passthrough(
                    isolated_request.argv,
                    isolated_request.workspace,
                    stdin_text=isolated_request.stdin_text,
                    prompt_file_text=isolated_request.prompt_file_text,
                    prompt_file_placeholder=PROMPT_FILE_ARG_PLACEHOLDER,
                    agent_config_text=isolated_request.agent_config_text,
                    agent_config_placeholder=DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
                    env_overrides=isolated_request.env_overrides,
                )
            except delegate_runner.RunnerLaunchError as exc:
                raise DelegateError(exc.error, exc.message) from exc
            return exit_code, None
        registry_root = run_registry.ensure_registry(
            Path(source_workspace.path),
            workspace_kind=source_workspace.kind,
        )
        maybe_run_retention_pass(registry_root, config)
        run_id, alias = run_registry.register_run(
            registry_root,
            harness=isolated_request.engine,
            metadata={
                "mode": isolated_request.mode,
                "model": isolated_request.model,
                "modelAlias": isolated_request.model_alias,
                "modelResolved": isolated_request.model,
                "group": isolated_request.group,
                "workflowAgentKey": isolated_request.workflow_agent_key,
                "cwd": (
                    isolated_request.isolation_context.source_workspace
                    if isolated_request.isolation_context is not None
                    else source_workspace.path
                ),
            },
        )
        ctx_runner = make_run_context(
            registry_root,
            isolated_request,
            run_id=run_id,
            alias=alias,
            source_workspace=source_workspace,
        )
        try:
            return delegate_runner.execute_tracked(
                isolated_request.argv,
                isolated_request.workspace,
                ctx_runner,
                json_mode=json_mode,
                stdout=stdout,
                stderr=stderr,
                completion_report_mode=completion_report_mode,
                stdin_text=isolated_request.stdin_text,
                prompt_file_text=isolated_request.prompt_file_text,
                prompt_file_placeholder=PROMPT_FILE_ARG_PLACEHOLDER,
                agent_config_text=isolated_request.agent_config_text,
                agent_config_placeholder=DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
                manifest_argv=public_argv(isolated_request),
                progress=isolated_request.progress,
                progress_initial_delay_sec=isolated_request.progress_initial_delay_sec,
                progress_interval_sec=isolated_request.progress_interval_sec,
                timeout=isolated_request.timeout,
            )
        except delegate_runner.RunnerLaunchError as exc:
            raise DelegateError(exc.error, exc.message, exc.exit_code) from exc


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in argv)


def pre_read_run_json_for_config(
    input_json_path: str,
    cli_cwd: str | None,
    cli_isolation: str | None = None,
    *,
    group: str | None = None,
) -> tuple[ResolvedWorkspace, JsonObject, str]:
    """Pre-read run input JSON for config discovery: extract cwd/isolation, resolve workspace,
    load config from that workspace, validate config. Returns (workspace, config, source)."""
    if wsl.should_reject_windows_path(input_json_path):
        raise DelegateError(
            "windows_path", wsl.windows_path_message("--input-json", input_json_path)
        )
    path = Path(input_json_path).expanduser()
    raw = _load_input_json_object(path)
    if raw.get("mode") == MODE_CALL:
        if cli_isolation is not None:
            raise DelegateError(
                "invalid_option_combination",
                "call mode does not use --isolation.",
            )
        if raw.get("cwd") is not None:
            raise DelegateError(
                "invalid_option_combination", "call mode input JSON must not include cwd."
            )
        if "isolation" in raw:
            raise DelegateError(
                "invalid_option_combination",
                "call mode input JSON must not include isolation.",
            )
        # Grouped call runs register in the invocation workspace registry while
        # still executing in a throwaway call cwd. Ungrouped call stays untracked.
        if group is not None:
            if cli_cwd is not None:
                workspace = resolve_workspace(cli_cwd)
            else:
                workspace = resolve_workspace(None)
            config, source = load_config(workspace=Path(workspace.path))
            validate_config(config)
            return workspace, config, source
        if cli_cwd is not None:
            raise DelegateError("invalid_option_combination", "call mode does not use --cwd.")
        config, source = load_config(workspace=None)
        validate_config(config)
        return ResolvedWorkspace(CALL_TEMP_CWD_PLACEHOLDER, "directory"), config, source

    # Read ONLY cwd and isolation for config discovery.
    json_cwd = raw.get("cwd")
    if json_cwd is not None and not isinstance(json_cwd, str):
        raise DelegateError("invalid_cwd", "cwd must be a string.")

    # Reject explicit null isolation in the JSON pre-read.
    if "isolation" in raw and raw["isolation"] is None:
        raise DelegateError(
            "invalid_isolation",
            "isolation in input JSON must be auto, none, or worktree (null is not allowed).",
        )

    workspace = resolve_workspace(cli_cwd, json_cwd)
    config, source = load_config(workspace=Path(workspace.path))
    validate_config(config)
    return workspace, config, source


def emit_error(error: DelegateError, json_mode: bool, stdout: TextIO, stderr: TextIO) -> int:
    if json_mode:
        payload: JsonObject = {
            "ok": False,
            "error": error.error,
            "message": error.message,
            "exitCode": error.exit_code,
        }
        if error.diagnostics is not None:
            payload["diagnostics"] = error.diagnostics
            for key, value in error.diagnostics.items():
                if key not in payload:
                    payload[key] = value
        if error.next_actions:
            payload["nextActions"] = error.next_actions
        delegate_rendering.print_json(
            payload,
            stdout,
        )
    else:
        print(f"{error.error}: {error.message}", file=stderr)
    return error.exit_code


def main(
    argv: list[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    json_mode = infer_global_json(argv)
    try:
        parsed = parse_cli(argv)
        global_options = parsed.global_options
        json_mode = global_options.json_mode
        profile_guard.enforce_profile_guard(parsed, stderr=stderr)
        if parsed.subcommand == "help":
            return emit_command_help(parsed.help_topic, global_options.json_mode, stdout)
        if parsed.subcommand == "version":
            print(VERSION, file=stdout)
            return EXIT_OK
        if parsed.subcommand == "config":
            return emit_config_command(parsed, stdout)
        if parsed.subcommand == "setup":
            return setup_commands.emit(
                json_mode=global_options.json_mode,
                auth_profile_override=global_options.auth_profile,
                stdout=stdout,
            )

        # For run --input-json, pre-read the JSON to discover config from the
        # JSON-resolved workspace before loading/finalizing config.
        if parsed.subcommand == "run":
            run_json = parsed.run_json
            if run_json is None:
                raise DelegateError("invalid_command", "run --input-json options are required.")
            workspace, config, source = pre_read_run_json_for_config(
                run_json.input_json,
                global_options.cwd,
                global_options.isolation,
                group=global_options.group,
            )
            config_workspace = Path(workspace.path)
        else:
            if parsed.launch is not None and parsed.launch.mode == MODE_CALL:
                if global_options.cwd is not None and global_options.group is None:
                    raise DelegateError(
                        "invalid_option_combination",
                        "call mode does not use --cwd.",
                    )
                if global_options.group is not None:
                    config_workspace = workspace_path_for_config(global_options.cwd)
                else:
                    config_workspace = None
            else:
                config_workspace = workspace_path_for_config(global_options.cwd)
            config, source = load_config(workspace=config_workspace)
            validate_config(config)

        discovery_profile: profiles.ProfileResolution | None = None
        discovery_snapshot: JsonObject | None = None
        if parsed.subcommand in {"models", "capabilities"}:
            discovery_profile = profiles.resolve_active_profile(
                config,
                profiles.child_environment(),
                cli_override=global_options.auth_profile,
            )
            capabilities_refresh = (
                parsed.subcommand == "capabilities"
                and parsed.capabilities is not None
                and parsed.capabilities.refresh
            )
            if not capabilities_refresh:
                discovery_snapshot = harness_discovery.load_discovery_cache(discovery_profile.name)

        if parsed.subcommand == "models":
            inspection = parsed.inspection or InspectionOptions()
            return emit_models(
                config,
                source,
                global_options.json_mode,
                stdout,
                workspace=config_workspace,
                summary=inspection.summary,
                engine=inspection.engine,
                live=inspection.live,
                discovery=discovery_snapshot,
                profile=discovery_profile,
            )
        if parsed.subcommand == "describe":
            inspection = parsed.inspection or InspectionOptions()
            return emit_describe(
                config,
                source,
                global_options.json_mode,
                stdout,
                workspace=config_workspace,
                summary=inspection.summary,
            )
        if parsed.subcommand == "agent-help":
            return emit_agent_help(stdout)

        if parsed.subcommand != "run":
            workspace = resolve_workspace(global_options.cwd)

        if parsed.subcommand == "capabilities":
            command = parsed.capabilities
            if command is None:
                raise DelegateError("invalid_command", "capabilities options are required.")
            return capability_commands.emit(
                command,
                config=config,
                config_source=source,
                workspace=workspace.path,
                auth_profile_override=global_options.auth_profile,
                profile=discovery_profile,
                discovery=discovery_snapshot,
                stdout=stdout,
            )

        if parsed.subcommand in {
            "snapshot",
            "runs",
            "run-output",
            "wait",
            "cancel",
            "worktree",
            "workflow",
        }:
            existing_registry = run_registry.registry_root_if_exists(Path(workspace.path))
            if existing_registry is not None:
                maybe_run_retention_pass(existing_registry, config)
        if parsed.subcommand == "snapshot":
            return emit_snapshot(parsed, workspace, stdout)
        if parsed.subcommand == "runs":
            return emit_runs(parsed, workspace, stdout)
        if parsed.subcommand == "run-output":
            return emit_run_output(parsed, workspace, stdout)
        if parsed.subcommand == "wait":
            return emit_wait(parsed, workspace, stdout)
        if parsed.subcommand == "cancel":
            return emit_cancel(parsed, workspace, stdout)
        if parsed.subcommand == "worktree":
            return emit_worktree(parsed, workspace, config, stdout)
        if parsed.subcommand == "workflow":
            return emit_workflow(parsed, workspace, config, stdout, stderr)

        if parsed.subcommand == "profiles":
            return emit_profiles_command(parsed, config, source, stdout)

        request = request_from_parsed(parsed, config, stdin)
        if request.dry_run:
            payload = dry_run_payload(request)
            if global_options.json_mode:
                delegate_rendering.print_json(payload, stdout)
            else:
                print(f"cwd: {request.workspace} ({request.workspace_kind})", file=stdout)
                if payload.get("isolatedWorkspace"):
                    print(f"isolation: {payload['isolation']}", file=stdout)
                lifecycle = payload.get("isolationLifecycle", "")
                if lifecycle:
                    print(f"isolationLifecycle: {lifecycle}", file=stdout)
                if payload.get("plannedBranch"):
                    print(f"plannedBranch: {payload['plannedBranch']}", file=stdout)
                if payload.get("plannedExecutionCwd"):
                    print(f"plannedExecutionCwd: {payload['plannedExecutionCwd']}", file=stdout)
                # Use the payload's rewritten argv (which shows planned paths) when
                # worktree isolation is active; otherwise use the source request.argv.
                display_argv = payload.get("argv", request.argv)
                print(f"argv: {shell_join(display_argv)}", file=stdout)
                for warning in payload.get("warnings", ()):
                    print(f"warning: {warning}", file=stdout)
            return EXIT_OK

        completion_report_mode = resolve_completion_report_mode(parsed, config)
        if request.prompt_instruction_mode == PROMPT_INSTRUCTION_MODE_SLASH:
            # Verbatim prompt carries no completion-report instruction; don't
            # expect (or synthesize around) a report the child was never asked for.
            completion_report_mode = delegate_config.COMPLETION_REPORT_MODE_NONE
            if not global_options.pass_through and not global_options.json_mode:
                print(
                    "notice: leading slash command detected; sending prompt verbatim "
                    "(delegate instruction wrapping suppressed).",
                    file=stderr,
                )
        exit_code, payload = execute_request(
            request,
            global_options.json_mode,
            config=config,
            config_source=source,
            pass_through=global_options.pass_through,
            completion_report_mode=completion_report_mode,
            source_workspace=workspace,
            stdout=stdout,
            stderr=stderr,
        )
        if global_options.json_mode and payload is not None:
            delegate_rendering.print_json(payload, stdout)
        return exit_code
    except worktree_mgmt.WorktreeManagementError as exc:
        if json_mode:
            delegate_rendering.print_json(exc.payload, stdout)
        else:
            print(f"{exc.code}: {exc.message}", file=stderr)
        exit_code = exc.payload.get("exitCode")
        return exit_code if isinstance(exit_code, int) else EXIT_USAGE
    except run_registry.RegistryJsonError as exc:
        return emit_error(
            DelegateError("invalid_run_registry", str(exc)),
            json_mode,
            stdout,
            stderr,
        )
    except command_errors.CommandError as exc:
        return emit_error(
            DelegateError(
                exc.error,
                exc.message,
                diagnostics=getattr(exc, "diagnostics", None),
                next_actions=getattr(exc, "next_actions", None),
            ),
            json_mode,
            stdout,
            stderr,
        )
    except DelegateError as exc:
        return emit_error(exc, json_mode, stdout, stderr)


if __name__ == "__main__":
    raise SystemExit(main())
