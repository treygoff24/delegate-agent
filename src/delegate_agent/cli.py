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
    command_help,
    inspection_commands,
    reasoning,
    run_output_commands,
    run_registry,
    worktree_commands,
    worktree_execution,
    worktree_mgmt,
)
from delegate_agent import config as delegate_config
from delegate_agent import rendering as delegate_rendering
from delegate_agent import retention as delegate_retention
from delegate_agent import runner as delegate_runner
from delegate_agent.argv_builders import (  # noqa: F401  # re-exported for tests / back-compat
    SAFE_REVIEW_PREFIX_BY_ENGINE,
    _claude_harness_bypass_enabled,
    build_claude_argv,
    build_codex_argv,
    build_cursor_argv,
    build_droid_argv,
    build_kimi_argv,
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
from delegate_agent.constants import MODE_SAFE, MODE_WORK
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
)
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import (  # noqa: F401  # CURSOR_PROMPT_REDACTION re-exported for tests
    CURSOR_PROMPT_REDACTION,
    DROID_PROMPT_FILE_ARG_PLACEHOLDER,
    DROID_PROMPT_FILE_DISPLAY,
    KIMI_PROMPT_REDACTION,
    PROMPT_TRANSPORT_ARGV,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_build import (  # noqa: F401  # re-exported for tests / back-compat
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

_replace_ws_by_engine = argv_utils.replace_workspace_arg_in_argv

DEFAULT_CONFIG = delegate_config.embedded_default_config()
CONFIG_ENV = delegate_config.CONFIG_ENV

MISSING_BINARY_PROBE_DIRS = (
    "~/.claude/local",
    "~/.kimi-code/bin",
    "~/.local/bin",
    "~/bin",
    "/opt/homebrew/bin",
)


HELP = command_help.render_overview_text()


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


def dry_run_payload(request: Request) -> JsonObject:
    payload: JsonObject = {
        "ok": True,
        "dryRun": True,
        "cwd": request.workspace,
        "workspaceKind": request.workspace_kind,
        "engine": request.engine,
        "mode": request.mode,
        "model": request.model,
        "argv": public_argv(request),
        "promptTransport": request.prompt_transport,
    }
    reasoning.add_reasoning_payload_fields(payload, request)
    if request.warnings:
        payload["warnings"] = list(request.warnings)

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
        if (
            request.engine in ("cursor", "codex", "droid", "kimi", "claude")
            and request.mode == MODE_SAFE
        ):
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
    if engine in {"codex", "droid", "kimi", "claude"}:
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
) -> DelegateError:
    config_key = _binary_config_key(engine)
    config_path_hint = _binary_config_path(config_source)
    suggested = _suggest_binary_path(binary)

    parts = [
        f"Missing binary: {binary}",
        "(searched PATH of the delegate process, not your interactive shell).",
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
) -> None:
    if not argv:
        raise DelegateError("missing_binary", "Empty argv.", EXIT_MISSING_BINARY)
    if shutil.which(argv[0]) is None:
        raise _missing_binary_error(argv[0], engine=engine, config_source=config_source)


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
        reasoning_effort_source=request.reasoning_effort_source,
        reasoning_capability_source=request.reasoning_capability_source,
        reasoning_transport=request.reasoning_transport,
        prompt_transport=request.prompt_transport,
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
                    ),
                )
            )
        except worktree_execution.PersistentWorktreeError as exc:
            raise DelegateError(exc.error, exc.message) from exc

    with safe_isolated_request(request) as isolated_request:
        ensure_binary(
            isolated_request.argv,
            engine=isolated_request.engine,
            config_source=config_source,
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
                    prompt_file_placeholder=DROID_PROMPT_FILE_ARG_PLACEHOLDER,
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
                prompt_file_placeholder=DROID_PROMPT_FILE_ARG_PLACEHOLDER,
                manifest_argv=public_argv(isolated_request),
            )
        except delegate_runner.RunnerLaunchError as exc:
            raise DelegateError(exc.error, exc.message) from exc


def runtime_payload() -> JsonObject:
    module_path = Path(__file__).resolve()
    return {
        "version": VERSION,
        "modulePath": str(module_path),
        "packageRoot": str(module_path.parents[1]),
        "executable": sys.argv[0],
        "pythonExecutable": sys.executable,
    }


def config_resolution_payload(config_source: str, workspace: Path | None = None) -> JsonObject:
    layers: list[JsonObject] = [{"name": "embedded-default", "applied": True}]
    global_path = delegate_config.default_config_path()
    global_exists = global_path.exists()
    layers.append(
        {
            "name": "user",
            "path": str(global_path),
            "exists": global_exists,
            "applied": global_exists,
        }
    )
    if workspace is not None:
        workspace_path = delegate_config.workspace_config_path(workspace)
        workspace_exists = workspace_path.exists()
        layers.append(
            {
                "name": "workspace",
                "path": str(workspace_path),
                "exists": workspace_exists,
                "applied": workspace_exists,
            }
        )
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        explicit_path = Path(explicit).expanduser()
        explicit_exists = explicit_path.exists()
        layers.append(
            {
                "name": CONFIG_ENV,
                "path": str(explicit_path),
                "exists": explicit_exists,
                "applied": explicit_exists,
            }
        )
    return {
        "source": config_source,
        "effectiveConfigPath": str(config_path()),
        "workspace": str(workspace) if workspace is not None else None,
        "layers": layers,
    }


def models_payload(
    config: JsonObject,
    config_source: str,
    workspace: Path | None = None,
) -> JsonObject:
    return {
        "ok": True,
        "configSource": config_source,
        "configResolution": config_resolution_payload(config_source, workspace),
        "runtime": runtime_payload(),
        "cursor": {
            "defaultModel": config["cursor"]["defaultModel"],
            "argvPrefix": config["cursor"]["argvPrefix"],
            "defaultReasoningEffort": config["cursor"].get("defaultReasoningEffort"),
            "reasoningEffortModels": config["cursor"].get("reasoningEffortModels", {}),
        },
        "droid": {
            "models": config["droid"]["models"],
            "defaultReasoningEffort": config["droid"].get("defaultReasoningEffort"),
        },
        "codex": {
            "binary": config["codex"]["binary"],
            "defaultModel": config["codex"]["defaultModel"],
            "defaultReasoningEffort": config["codex"].get("defaultReasoningEffort"),
            "profile": config["codex"]["profile"],
        },
        "claude": {
            "binary": config["claude"]["binary"],
            "defaultModel": config["claude"]["defaultModel"],
            "defaultReasoningEffort": config["claude"].get("defaultReasoningEffort"),
            "workPermissionMode": config["claude"]["workPermissionMode"],
            "noSessionPersistence": config["claude"]["noSessionPersistence"],
            "bare": config["claude"]["bare"],
        },
        "kimi": {
            "binary": config["kimi"]["binary"],
            "defaultModel": config["kimi"]["defaultModel"],
            "defaultReasoningEffort": config["kimi"].get("defaultReasoningEffort"),
        },
    }


def _policy_field_support_matrix() -> JsonObject:
    codex_supported = {
        "networkAccess": True,
        "webSearch": True,
        "bypassApprovalsAndSandbox": True,
        "bypassHookTrust": True,
    }
    unsupported = {key: False for key in delegate_config.POLICY_MODE_KEYS}
    claude_supported = dict(unsupported)
    claude_supported["bypassApprovalsAndSandbox"] = True
    return {
        "codex": codex_supported,
        "claude": claude_supported,
        "cursor": unsupported,
        "droid": unsupported,
        "kimi": unsupported,
    }


def _claude_runtime_policy(config: JsonObject, mode: str) -> JsonObject:
    policy = {key: False for key in delegate_config.POLICY_MODE_KEYS}
    if mode == MODE_WORK:
        policy["bypassApprovalsAndSandbox"] = _claude_harness_bypass_enabled(
            config,
            mode,
        )
    return policy


def _codex_describe_model(codex: JsonObject) -> str | None:
    return _resolve_default_model(codex)


def _codex_describe_argv(
    codex: JsonObject,
    *,
    mode: str,
    workspace: str,
    prompt: str,
    policy: JsonObject,
) -> list[str]:
    return build_codex_argv(
        codex,
        mode,
        workspace,
        _codex_describe_model(codex),
        prompt,
        policy,
        workspace_kind="git",
        prompt_transport=PROMPT_TRANSPORT_STDIN,
    )


def _claude_describe_model(claude: JsonObject) -> str | None:
    return _resolve_default_model(claude)


def _claude_describe_argv(
    config: JsonObject,
    claude: JsonObject,
    *,
    mode: str,
    policy: JsonObject,
) -> list[str]:
    return build_claude_argv(
        claude,
        mode,
        _claude_describe_model(claude),
        policy,
        allow_bypass_permissions=_claude_harness_bypass_enabled(config, mode),
    )


def describe_payload(
    config: JsonObject,
    config_source: str,
    workspace: Path | None = None,
) -> JsonObject:
    codex = config["codex"]
    claude = config["claude"]
    codex_safe_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_SAFE)
    codex_work_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_WORK)
    claude_safe_policy = _claude_runtime_policy(config, MODE_SAFE)
    claude_work_policy = _claude_runtime_policy(config, MODE_WORK)
    codex_safe_argv = _codex_describe_argv(
        codex,
        mode=MODE_SAFE,
        workspace="<isolated-workspace>",
        prompt="<codex-safe-prefixed-skill-review-prompt>",
        policy=codex_safe_policy,
    )
    codex_work_argv = _codex_describe_argv(
        codex,
        mode=MODE_WORK,
        workspace="<workspace>",
        prompt="<skill-review-prompt>",
        policy=codex_work_policy,
    )
    claude_safe_argv = _claude_describe_argv(
        config,
        claude,
        mode=MODE_SAFE,
        policy=claude_safe_policy,
    )
    claude_work_argv = _claude_describe_argv(
        config,
        claude,
        mode=MODE_WORK,
        policy=claude_work_policy,
    )
    return {
        "ok": True,
        "version": VERSION,
        "runtime": runtime_payload(),
        "configPath": str(config_path()),
        "configSource": config_source,
        "configResolution": config_resolution_payload(config_source, workspace),
        "engines": ["cursor", "droid", "codex", "kimi", "claude"],
        "policyProfiles": list(delegate_config.POLICY_PROFILES),
        "policyFieldSupport": _policy_field_support_matrix(),
        "effectivePolicy": {
            "codex": {
                "safe": codex_safe_policy,
                "work": codex_work_policy,
            },
            "claude": {
                "safe": claude_safe_policy,
                "work": claude_work_policy,
            },
        },
        "modes": [MODE_SAFE, MODE_WORK],
        "promptSources": ["direct", "prompt-file", "stdin"],
        "promptTransports": {
            "cursor": PROMPT_TRANSPORT_ARGV,
            "droid": PROMPT_TRANSPORT_FILE,
            "codex": PROMPT_TRANSPORT_STDIN,
            "kimi": PROMPT_TRANSPORT_ARGV,
            "claude": PROMPT_TRANSPORT_STDIN,
        },
        "globalOptions": [
            "--cwd",
            "--json",
            "--isolation",
            "--pass-through",
            "--completion-report",
            "--no-completion-report",
        ],
        "completionReportModes": list(delegate_config.COMPLETION_REPORT_MODES),
        "promptTransforms": [
            "Always prepends mandatory skill review instructions before the operator prompt.",
            "Optionally appends completion-report instructions unless disabled.",
        ],
        "passThrough": "Opt-in raw child stdout/stderr streaming; incompatible with --json.",
        "cwdResolution": "Git directories resolve to the repository root; non-Git directories are used directly.",
        "isolation": {
            "defaults": config["isolation"],
            "supportedValues": list(delegate_config.VALID_ISOLATION_VALUES),
            "safeNoneAllowed": {
                "cursor": False,
                "droid": False,
                "codex": True,
                "kimi": False,
                "claude": False,
            },
        },
        "worktrees": {
            "dataHome": config["worktrees"]["dataHome"],
            "autoPrune": config["worktrees"]["autoPrune"],
        },
        "modeMapping": {
            "cursor": {
                "safe": [
                    *config["cursor"]["argvPrefix"],
                    "--workspace",
                    "<isolated-workspace>",
                    "-p",
                    "--trust",
                    "--model",
                    config["cursor"]["defaultModel"],
                    "--print",
                    "--output-format",
                    "stream-json",
                    "<read-only-review-prefixed-skill-review-prompt>",
                ],
                "safeNotes": [
                    "No --mode=plan, --mode=ask, --force, or --approve-mcps.",
                    "Runs in an isolated temporary workspace (detached git worktree or directory copy).",
                    "Writes .cursor/cli.json in the isolated workspace (Read(**), read-only shell helpers; no git/find shell).",
                ],
                "work": [
                    *config["cursor"]["argvPrefix"],
                    "--workspace",
                    "<workspace>",
                    "-p",
                    "--trust",
                    "--approve-mcps",
                    "--force",
                    "--model",
                    config["cursor"]["defaultModel"],
                    "--print",
                    "--output-format",
                    "stream-json",
                    "<skill-review-prompt>",
                ],
            },
            "droid": {
                "safe": [
                    config["droid"]["binary"],
                    "exec",
                    "--cwd",
                    "<isolated-workspace>",
                    "--model",
                    "<model-id>",
                    "--output-format",
                    "stream-json",
                    "--file",
                    DROID_PROMPT_FILE_DISPLAY,
                ],
                "safeNotes": [
                    "Runs in an isolated temporary workspace (detached git worktree or directory copy).",
                    "No --auto, --use-spec, or --skip-permissions-unsafe in safe mode.",
                    "Uses a read-only safety prompt; --isolation none is rejected for Droid safe mode.",
                ],
                "work": [
                    config["droid"]["binary"],
                    "exec",
                    "--cwd",
                    "<workspace>",
                    "--skip-permissions-unsafe",
                    "--model",
                    "<model-id>",
                    "--output-format",
                    "stream-json",
                    "--file",
                    DROID_PROMPT_FILE_DISPLAY,
                ],
            },
            "codex": {
                "safe": codex_safe_argv,
                "safeNotes": [
                    "Runs in an isolated temporary workspace (detached git worktree or directory copy).",
                    "Always uses --sandbox read-only; safe sandbox is not configurable in v1.",
                    "Non-interactive: --ask-for-approval never.",
                ],
                "work": codex_work_argv,
                "workNotes": [
                    "networkAccess enables -c sandbox_workspace_write.network_access=true when workSandbox is workspace-write.",
                    "webSearch enables global --search before exec.",
                    "profile is config-only (codex.profile); not accepted in run input JSON.",
                ],
            },
            "claude": {
                "safe": claude_safe_argv,
                "safeNotes": [
                    "Runs in an isolated temporary workspace (detached git worktree or directory copy).",
                    "Uses Claude Code -p with --permission-mode plan, --strict-mcp-config, Read/Grep/Glob, and selected read-only Bash tools.",
                    "Prompt is delivered on stdin; dry-run argv and manifests do not contain the prompt.",
                ],
                "work": claude_work_argv,
                "workNotes": [
                    "Uses claude.workPermissionMode unless policy.harness.claude.work.bypassApprovalsAndSandbox explicitly requests bypassPermissions.",
                    "Reasoning effort is emitted as Claude Code --effort, independent of model capability cache.",
                    "Delegate sets subprocess cwd; Claude Code receives no workspace argv flag.",
                ],
            },
            "kimi": {
                "safe": [
                    config["kimi"]["binary"],
                    "--model",
                    config["kimi"]["defaultModel"],
                    "--output-format",
                    "stream-json",
                    "--prompt",
                    "<kimi-safe-prefixed-skill-review-prompt>",
                ],
                "safeNotes": [
                    "Runs in an isolated temporary workspace (detached git worktree or directory copy).",
                    "Prompt mode cannot be combined with Kimi --plan; Delegate uses a read-only safety prompt instead.",
                    "Kimi prompt mode auto-approves tool actions; the isolated workspace is the effective write boundary and the safety prompt is advisory.",
                    "No CLI workspace flag; Delegate sets subprocess cwd.",
                ],
                "work": [
                    config["kimi"]["binary"],
                    "--model",
                    config["kimi"]["defaultModel"],
                    "--output-format",
                    "stream-json",
                    "--prompt",
                    "<skill-review-prompt>",
                ],
                "workNotes": [
                    "Kimi prompt mode auto-approves tool actions; Delegate does not pass --yolo because Kimi rejects combining it with --prompt.",
                    "No CLI workspace flag; Delegate sets subprocess cwd.",
                ],
            },
        },
        "commands": [
            {"command": spec.name, "summary": spec.summary}
            for spec in command_help.COMMAND_SPECS.values()
        ],
    }


def emit_models(
    config: JsonObject,
    config_source: str,
    json_mode: bool,
    stdout: TextIO,
    *,
    workspace: Path | None = None,
) -> int:
    if json_mode:
        delegate_rendering.print_json(models_payload(config, config_source, workspace), stdout)
        return EXIT_OK
    if config_source == "embedded-default":
        print("warning: using embedded default config", file=stdout)
    print(
        f"cursor: {config['cursor']['defaultModel']} ({' '.join(config['cursor']['argvPrefix'])})",
        file=stdout,
    )
    print("droid:", file=stdout)
    for alias, model_id in sorted(config["droid"]["models"].items()):
        print(f"  {alias} -> {model_id}", file=stdout)
    codex = config["codex"]
    default_model = codex.get("defaultModel")
    model_label = default_model if isinstance(default_model, str) and default_model else "(none)"
    profile = codex.get("profile")
    profile_label = profile if isinstance(profile, str) and profile else "(none)"
    print(
        f"codex: binary={codex['binary']} defaultModel={model_label} profile={profile_label}",
        file=stdout,
    )
    claude = config["claude"]
    claude_default_model = claude.get("defaultModel")
    claude_model_label = (
        claude_default_model
        if isinstance(claude_default_model, str) and claude_default_model
        else "(none)"
    )
    print(
        "claude: "
        f"binary={claude['binary']} defaultModel={claude_model_label} "
        f"workPermissionMode={claude['workPermissionMode']}",
        file=stdout,
    )
    kimi = config["kimi"]
    kimi_default_model = kimi.get("defaultModel")
    kimi_model_label = (
        kimi_default_model
        if isinstance(kimi_default_model, str) and kimi_default_model
        else "(none)"
    )
    print(
        f"kimi: binary={kimi['binary']} defaultModel={kimi_model_label}",
        file=stdout,
    )
    print(f"runtime: {runtime_payload()['modulePath']}", file=stdout)
    return EXIT_OK


def emit_describe(
    config: JsonObject,
    config_source: str,
    json_mode: bool,
    stdout: TextIO,
    *,
    workspace: Path | None = None,
) -> int:
    payload = describe_payload(config, config_source, workspace)
    if json_mode:
        delegate_rendering.print_json(payload, stdout)
        return EXIT_OK
    print(f"delegate {VERSION}", file=stdout)
    print(f"config: {payload['configPath']} ({payload['configSource']})", file=stdout)
    print(f"runtime: {payload['runtime']['modulePath']}", file=stdout)
    print("engines: cursor, droid, codex, kimi, claude", file=stdout)
    print("modes: safe, work", file=stdout)
    print("prompt sources: direct, --prompt-file, stdin", file=stdout)
    print("global options must appear before the subcommand", file=stdout)
    return EXIT_OK


def emit_command_help(topic: str | None, json_mode: bool, stdout: TextIO) -> int:
    """Render help: overview when topic is empty, else focused help for one command."""

    if not topic:
        if json_mode:
            delegate_rendering.print_json(command_help.help_index_payload(), stdout)
        else:
            print(command_help.render_overview_text(), file=stdout, end="")
        return EXIT_OK
    spec = command_help.COMMAND_SPECS.get(topic)
    if spec is None:
        raise DelegateError(
            "unknown_help_topic",
            f"Unknown help topic: {topic}. Run delegate help for the command list.",
        )
    if json_mode:
        delegate_rendering.print_json(command_help.command_help_payload(spec), stdout)
    else:
        print(command_help.render_command_help_text(spec), file=stdout, end="")
    return EXIT_OK


def emit_agent_help(stdout: TextIO) -> int:
    print(
        """Use delegate for bounded execution tasks only.

Good defaults:
  delegate cursor work "Implement the scoped task; report changed files and tests."
  delegate cursor safe "Review this diff for regressions; report findings with file/line/severity."
  delegate droid <alias> safe "Investigate this issue; do not edit."
  delegate droid <alias> work "Implement this bounded change; run the named check."
  delegate codex safe "Review this workspace. Do not edit files."
  delegate codex work "Implement the scoped fix, run the named check, and report changed files."
  delegate claude safe "Review this workspace. Do not edit files."
  delegate claude work "Implement the scoped fix, run the named check, and report changed files."
  delegate kimi safe "Review this repo for regressions; report file/line/severity."
  delegate kimi work "Implement the scoped task; report changed files and tests."

Kimi:
  - Model selection uses kimi.defaultModel in config or optional JSON input model; no CLI model alias in v1.
  - Reasoning effort is unsupported for Kimi in v1.
  - No CLI workspace flag; Delegate sets subprocess cwd.

Codex:
  - Model selection uses codex.defaultModel in config or optional JSON input model; no CLI model alias in v1.
  - Codex profile (codex.profile) is config-only; run input JSON must not include profile.

Claude:
  - Uses Claude Code headless mode: claude -p with prompt delivered on stdin.
  - Safe mode runs in an isolated temporary workspace with --permission-mode plan,
    --strict-mcp-config, Read/Grep/Glob, and selected read-only Bash tools.
  - Work mode uses claude.workPermissionMode, or bypassPermissions only when
    Delegate policy explicitly enables policy.harness.claude.work.bypassApprovalsAndSandbox.
  - Reasoning effort maps to Claude Code --effort (low, medium, high, xhigh, max).

Droid modes:
  - Droid safe mode remains read-only in an isolated temporary workspace: no --auto, --use-spec, or unsafe skip.
  - Uses Factory Droid --skip-permissions-unsafe, not --auto high.
  - Work mode is intentionally no-prompt; use only for bounded tasks in workspaces you trust.

Cursor safe mode:
  - Uses default Cursor Agent behavior in an isolated temporary workspace, not plan/ask mode.
  - The child runs in the isolated copy; tracked runs may still write .delegate metadata in the source workspace.

Rules for agents:
  - Keep prompts bounded: task, scope, verification, report format.
  - Delegate always prepends a mandatory skill-review instruction before your prompt.
  - Use --prompt-file or delegate --json run --input-json for long prompts.
  - Run from the target workspace, or pass --cwd before the subcommand.
  - Inside Git, --cwd resolves to the repo root; outside Git, the directory is used directly.
  - Always review diffs after work mode when Git is available; outside Git, manually review changed files.
  - Do not use delegate for production deploys or repository publishing unless the operator explicitly asks.
  - Launch normally; do not pipe delegate launches through tail just to suppress noise.
  - After a tracked run, use delegate snapshot/runs/run-output; do not tail launch output or .delegate log files.
  - Default output is bounded; use --pass-through only when raw harness streaming is required.
  - If you intentionally pipe delegate output in a shell script, use set -o pipefail.

Run inspection:
  delegate snapshot <alias-or-runId>
  delegate runs --active
  delegate run-output <alias> --completion-report
  delegate run-output <alias> --stderr --tail 100

Avoid:
  delegate cursor work --prompt-file task.md 2>&1 | tail -20

Prefer:
  delegate cursor work --prompt-file task.md
  delegate snapshot cursor
  delegate run-output cursor --completion-report

Discovery:
  delegate --json models
  delegate --json describe
  delegate agent-help
""".rstrip(),
        file=stdout,
    )
    return EXIT_OK


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in argv)


def pre_read_run_json_for_config(
    input_json_path: str, cli_cwd: str | None
) -> tuple[ResolvedWorkspace, JsonObject, str]:
    """Pre-read run input JSON for config discovery: extract cwd/isolation, resolve workspace,
    load config from that workspace, validate config. Returns (workspace, config, source)."""
    path = Path(input_json_path).expanduser()
    raw = _load_input_json_object(path)

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
        if parsed.subcommand == "help":
            return emit_command_help(parsed.help_topic, global_options.json_mode, stdout)
        if parsed.subcommand == "version":
            print(VERSION, file=stdout)
            return EXIT_OK

        # For run --input-json, pre-read the JSON to discover config from the
        # JSON-resolved workspace before loading/finalizing config.
        if parsed.subcommand == "run":
            run_json = parsed.run_json
            if run_json is None:
                raise DelegateError("invalid_command", "run --input-json options are required.")
            workspace, config, source = pre_read_run_json_for_config(
                run_json.input_json, global_options.cwd
            )
            config_workspace = Path(workspace.path)
        else:
            config_workspace = workspace_path_for_config(global_options.cwd)
            config, source = load_config(workspace=config_workspace)
            validate_config(config)

        if parsed.subcommand == "models":
            return emit_models(
                config, source, global_options.json_mode, stdout, workspace=config_workspace
            )
        if parsed.subcommand == "describe":
            return emit_describe(
                config, source, global_options.json_mode, stdout, workspace=config_workspace
            )
        if parsed.subcommand == "agent-help":
            return emit_agent_help(stdout)

        if parsed.subcommand != "run":
            workspace = resolve_workspace(global_options.cwd)
            # (non-run path uses workspace resolved above)

        if parsed.subcommand == "capabilities":
            command = parsed.capabilities
            if command is None:
                raise DelegateError("invalid_command", "capabilities options are required.")
            return capability_commands.emit(
                command,
                config=config,
                config_source=source,
                workspace=workspace.path,
                stdout=stdout,
            )

        if parsed.subcommand in {"snapshot", "runs", "run-output", "worktree"}:
            existing_registry = run_registry.registry_root_if_exists(Path(workspace.path))
            if existing_registry is not None:
                maybe_run_retention_pass(existing_registry, config)
        if parsed.subcommand == "snapshot":
            return emit_snapshot(parsed, workspace, stdout)
        if parsed.subcommand == "runs":
            return emit_runs(parsed, workspace, stdout)
        if parsed.subcommand == "run-output":
            return emit_run_output(parsed, workspace, stdout)
        if parsed.subcommand == "worktree":
            return emit_worktree(parsed, workspace, config, stdout)

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
            return EXIT_OK

        completion_report_mode = resolve_completion_report_mode(parsed, config)
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
