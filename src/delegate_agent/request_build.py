"""Request construction.

Turns a parsed command plus resolved config into a launch-ready ``Request``:
workspace/prompt resolution, per-engine request-parts assembly (dispatched via
``ENGINE_REQUEST_PARTS_BUILDERS``), reasoning-effort resolution, and the
JSON/stdin request entry points. The per-engine parts stay explicit because the
engines differ materially; the argv strings themselves are built in
``argv_builders``.
"""

from __future__ import annotations

import io
import json
import os
import select
import subprocess  # nosec B404 - Delegate inspects git workspaces with shell=False.
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from delegate_agent import codex_auth, reasoning
from delegate_agent import config as delegate_config
from delegate_agent import runner as delegate_runner
from delegate_agent.argv_builders import (
    SAFE_REVIEW_PREFIX_BY_ENGINE,
    _claude_harness_bypass_enabled,
    build_claude_argv,
    build_codex_argv,
    build_cursor_argv,
    build_droid_argv,
    build_kimi_argv,
    prefix_droid_safe_prompt,
    redacted_prompt_argv,
)
from delegate_agent.constants import (
    ENGINES_PROSE,
    KNOWN_ENGINES,
    MODE_SAFE,
    MODE_WORK,
    validate_mode,
)
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS, capture_git_metadata
from delegate_agent.git_utils import run_git as _run_git
from delegate_agent.isolation import IsolationContext, build_isolation_context
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.prompt_transport import (
    DROID_PROMPT_FILE_ARG_PLACEHOLDER,
    DROID_PROMPT_FILE_DISPLAY,
    KIMI_PROMPT_REDACTION,
    PROMPT_TRANSPORT_ARGV,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_models import (
    EngineBuildInput,
    EngineRequestParts,
    ParsedCommand,
    Request,
    ResolvedWorkspace,
)

RUN_INPUT_KEYS = {
    "engine",
    "mode",
    "model",
    "cwd",
    "prompt",
    "isolation",
    "reasoningEffort",
    "progress",
    "forbidCommit",
}


def load_config(
    path: Path | None = None,
    *,
    workspace: Path | None = None,
    cli_overrides: JsonObject | None = None,
) -> tuple[JsonObject, str]:
    try:
        return delegate_config.load_config(path, workspace=workspace, cli_overrides=cli_overrides)
    except delegate_config.ConfigError as exc:
        raise DelegateError(exc.error, exc.message) from exc


def validate_config(config: JsonObject) -> None:
    try:
        delegate_config.validate_config(config)
    except delegate_config.ConfigError as exc:
        raise DelegateError(exc.error, exc.message) from exc


ProgressIntent = str | None


def resolve_effective_progress(intent: ProgressIntent, config: JsonObject) -> bool:
    if intent == "off":
        return False
    if intent == "on":
        return True
    progress_section = config.get("progress")
    if isinstance(progress_section, dict):
        enabled = progress_section.get("enabled", False)
        if isinstance(enabled, bool):
            return enabled
    return False


def resolve_progress_timing(config: JsonObject) -> tuple[float, float]:
    default_initial = delegate_config.default_progress_initial_delay_sec()
    default_interval = delegate_config.default_progress_interval_sec()
    progress_section = config.get("progress")
    if not isinstance(progress_section, dict):
        return (default_initial, default_interval)
    initial = progress_section.get("initialDelaySec", default_initial)
    interval = progress_section.get("intervalSec", default_interval)
    return float(initial), float(interval)


def resolve_workspace(global_cwd: str | None, json_cwd: str | None = None) -> ResolvedWorkspace:
    if global_cwd and json_cwd:
        global_workspace = workspace_for(global_cwd)
        json_workspace = workspace_for(json_cwd)
        if Path(global_workspace.path).resolve() != Path(json_workspace.path).resolve():
            raise DelegateError(
                "ambiguous_cwd", "CLI --cwd and JSON cwd resolve to different workspaces."
            )
        return global_workspace
    if json_cwd:
        return workspace_for(json_cwd)
    if global_cwd:
        return workspace_for(global_cwd)
    return workspace_for(os.getcwd())


def workspace_for(path_text: str) -> ResolvedWorkspace:
    path = Path(path_text).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise DelegateError("invalid_cwd", f"cwd does not exist or is not a directory: {path}")
    git_root = git_root_for(path)
    if git_root is not None:
        return ResolvedWorkspace(git_root, "git")
    return ResolvedWorkspace(str(path), "directory")


def git_root_for(path: Path) -> str | None:
    try:
        result = _run_git(
            str(path),
            ["rev-parse", "--show-toplevel"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return str(Path(result.stdout.strip()).resolve())


def resolve_prompt(
    prompt_parts: list[str] | None,
    prompt_file: str | None,
    stdin: TextIO,
) -> str:
    direct = " ".join(prompt_parts or [])
    has_direct = bool(direct)
    has_prompt_file = prompt_file is not None
    stdin_text = read_stdin_source(stdin, block=not (has_direct or has_prompt_file))
    has_stdin = stdin_text is not None
    if sum(1 for present in (has_direct, has_prompt_file, has_stdin) if present) > 1:
        raise DelegateError(
            "ambiguous_prompt_source",
            "Use exactly one prompt source: direct args, --prompt-file, or stdin.",
        )
    if has_direct:
        return validate_prompt(direct)
    if has_prompt_file:
        prompt_file_path = prompt_file
        if prompt_file_path is None:
            raise DelegateError("missing_prompt_file", "--prompt-file requires a path.")
        path = Path(prompt_file_path).expanduser()
        try:
            return validate_prompt(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DelegateError("prompt_file_not_found", f"Prompt file not found: {path}") from None
    if has_stdin:
        stdin_prompt = stdin_text
        if stdin_prompt is None:
            raise DelegateError("missing_prompt", "Missing stdin prompt.")
        return validate_prompt(stdin_prompt)
    raise DelegateError(
        "missing_prompt", "Missing prompt; pass prompt text, --prompt-file, or stdin."
    )


def read_stdin_source(stdin: TextIO, *, block: bool = False) -> str | None:
    if stdin.isatty():
        return None
    if not block:
        try:
            ready, _, _ = select.select([stdin], [], [], 0)
            if not ready:
                return None
        except (AttributeError, OSError, ValueError):
            if not isinstance(stdin, io.StringIO):
                return None
    data = stdin.read()
    return data if data else None


def validate_prompt(prompt: str) -> str:
    if not prompt.strip():
        raise DelegateError("empty_prompt", "Prompt is empty.")
    for ch in prompt:
        code = ord(ch)
        if ch == "\x00" or (code < 0x20 and ch not in ("\n", "\r", "\t")):
            raise DelegateError("invalid_prompt", "Prompt contains disallowed control characters.")
    return prompt


def resolve_completion_report_mode(parsed: ParsedCommand, config: JsonObject) -> str:
    global_options = parsed.global_options
    if global_options.pass_through:
        return delegate_config.COMPLETION_REPORT_MODE_NONE
    if global_options.completion_report is not None:
        return global_options.completion_report
    return delegate_config.completion_report_default_mode(config)


def effective_prompt(
    prompt: str,
    *,
    engine: str = "",
    mode: str = "",
    completion_report_mode: str,
) -> str:
    prompt = delegate_runner.prepend_skill_review_instructions(prompt)
    safe_prefix = (
        SAFE_REVIEW_PREFIX_BY_ENGINE.get(engine) if engine in {"codex", "droid", "claude"} else None
    )
    if mode == MODE_SAFE and safe_prefix is not None and safe_prefix not in prompt:
        # prepend_skill_review_instructions guarantees SKILL_REVIEW_PREFIX at index 0,
        # so provider-specific safe prefixes slot in cleanly between skill-review
        # and the user prompt.
        insert_at = len(delegate_runner.SKILL_REVIEW_PREFIX)
        prompt = prompt[:insert_at] + safe_prefix + prompt[insert_at:]
    if completion_report_mode == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN:
        return delegate_runner.append_completion_report_instructions(prompt)
    return prompt


def _validate_forbid_commit(
    *,
    forbid_commit: bool,
    mode: str,
    isolation_context: IsolationContext | None,
) -> None:
    if not forbid_commit:
        return
    if mode != MODE_WORK:
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit requires work mode with persistent worktree isolation.",
        )
    if isolation_context is None or isolation_context.isolation_lifecycle != "persistent":
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit requires --isolation worktree so Delegate can enforce the policy.",
        )


def request_from_parsed(parsed: ParsedCommand, config: JsonObject, stdin: TextIO) -> Request:
    validate_config(config)
    if parsed.subcommand == "run":
        return request_from_input_json(parsed, config)
    launch = parsed.launch
    global_options = parsed.global_options
    if launch is None or launch.engine not in KNOWN_ENGINES:
        raise DelegateError("invalid_command", "Command does not map to an execution request.")
    if launch.mode is None:
        raise DelegateError("invalid_command", "Command does not map to an execution request.")
    effective_progress = resolve_effective_progress(launch.progress_intent, config)
    if effective_progress and global_options.pass_through:
        raise DelegateError(
            "invalid_option_combination",
            "--progress is incompatible with --pass-through.",
        )
    progress_initial_delay_sec, progress_interval_sec = resolve_progress_timing(config)
    workspace = resolve_workspace(global_options.cwd)
    prompt = resolve_prompt(launch.prompt_parts, launch.prompt_file, stdin)

    # Capture git metadata for isolation planning (read-only, safe in dry-run too).
    git_root, git_common_dir, git_head_oid, git_head_ref, git_branch = capture_git_metadata(
        workspace.path
    )

    # Resolve effective isolation and build isolation context.
    try:
        effective_isolation = delegate_config.resolve_isolation(
            cli_value=global_options.isolation,
            loaded_config=config,
            engine=launch.engine,
            mode=launch.mode,
        )
    except delegate_config.InvalidIsolationError as exc:
        raise DelegateError("invalid_isolation", str(exc)) from exc

    isolation_context = build_isolation_context(
        source_workspace=workspace.path,
        resolved_isolation=effective_isolation,
        engine=launch.engine,
        mode=launch.mode,
        model_alias=launch.model_alias,
        config=config,
        run_short_id="<short-run-id-placeholder>" if launch.dry_run else None,
        source_git_root=git_root,
        source_git_common_dir=git_common_dir,
        source_head_oid=git_head_oid,
        source_head_ref=git_head_ref,
        source_branch=git_branch,
    )
    _validate_forbid_commit(
        forbid_commit=launch.forbid_commit,
        mode=launch.mode,
        isolation_context=isolation_context,
    )

    completion_report_mode = resolve_completion_report_mode(parsed, config)
    prompt = effective_prompt(
        prompt,
        engine=launch.engine,
        mode=launch.mode,
        completion_report_mode=completion_report_mode,
    )
    return build_request(
        launch.engine,
        launch.mode,
        launch.model_alias,
        workspace,
        prompt,
        config,
        launch.dry_run,
        stream_capture=not global_options.pass_through,
        isolation_context=isolation_context,
        reasoning_effort=launch.reasoning_effort,
        reasoning_effort_source="cli" if launch.reasoning_effort is not None else None,
        progress=effective_progress,
        progress_initial_delay_sec=progress_initial_delay_sec,
        progress_interval_sec=progress_interval_sec,
        forbid_commit=launch.forbid_commit,
    )


def _load_input_json_object(path: Path) -> JsonObject:
    try:
        raw: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DelegateError("input_json_not_found", f"Input JSON file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise DelegateError("invalid_input_json", f"Invalid input JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DelegateError("invalid_input_json", "Input JSON root must be an object.")
    return raw


def request_from_input_json(parsed: ParsedCommand, config: JsonObject) -> Request:
    run_json = parsed.run_json
    if run_json is None:
        raise DelegateError("invalid_command", "run --input-json options are required.")
    global_options = parsed.global_options
    path = Path(run_json.input_json).expanduser()
    raw = _load_input_json_object(path)

    if "profile" in raw:
        raise DelegateError(
            "invalid_input_key",
            "Input JSON must not include profile; set codex.profile in config for Codex.",
        )
    unknown = sorted(set(raw) - RUN_INPUT_KEYS)
    if unknown:
        raise DelegateError("unknown_input_key", f"Unknown input JSON keys: {', '.join(unknown)}")
    engine = raw.get("engine")
    mode = raw.get("mode")
    prompt = raw.get("prompt")
    if engine not in KNOWN_ENGINES:
        raise DelegateError(
            "invalid_engine",
            f"engine must be {ENGINES_PROSE}.",
        )
    if not isinstance(mode, str):
        raise DelegateError("invalid_mode", "mode must be safe or work.")
    validate_mode(mode)
    if not isinstance(prompt, str):
        raise DelegateError("invalid_prompt", "prompt must be a string.")
    model_alias = raw.get("model")
    raw_reasoning_effort = raw.get("reasoningEffort")
    if raw_reasoning_effort is not None:
        try:
            reasoning_effort = reasoning.normalize_effort(raw_reasoning_effort)
        except reasoning.ReasoningCapabilityError as exc:
            raise DelegateError(exc.error, "reasoningEffort must be a non-empty string.") from exc
    else:
        reasoning_effort = None
    raw_progress_intent: ProgressIntent
    if "progress" in raw:
        raw_progress = raw["progress"]
        if not isinstance(raw_progress, bool):
            raise DelegateError("invalid_progress", "progress must be true or false.")
        raw_progress_intent = "on" if raw_progress else "off"
    else:
        raw_progress_intent = None
    effective_progress = resolve_effective_progress(raw_progress_intent, config)
    if effective_progress and global_options.pass_through:
        raise DelegateError(
            "invalid_option_combination",
            "progress is incompatible with --pass-through.",
        )
    progress_initial_delay_sec, progress_interval_sec = resolve_progress_timing(config)
    raw_forbid_commit = raw.get("forbidCommit", False)
    if not isinstance(raw_forbid_commit, bool):
        raise DelegateError("invalid_forbid_commit", "forbidCommit must be true or false.")
    if engine == "droid":
        if not isinstance(model_alias, str) or not model_alias:
            raise DelegateError("missing_model", "droid run input requires model alias.")
    elif engine in ("codex", "kimi", "claude"):
        if model_alias is not None and not isinstance(model_alias, str):
            raise DelegateError("invalid_model", f"model must be a string or null for {engine}.")
        if model_alias == "":
            raise DelegateError(
                "invalid_model", f"model must be a non-empty string or omitted for {engine}."
            )
    elif model_alias is not None and model_alias != config["cursor"]["defaultModel"]:
        raise DelegateError(
            "invalid_model", "cursor model override must match configured Composer model."
        )

    # Pre-read cwd and isolation from JSON for config discovery (already done in main() for
    # config loading, but re-validate and resolve here for the request).
    json_cwd = raw.get("cwd")
    if json_cwd is not None and not isinstance(json_cwd, str):
        raise DelegateError("invalid_cwd", "cwd must be a string.")

    # Reject explicit null isolation in the JSON (distinguish missing-key from null).
    if "isolation" in raw and raw["isolation"] is None:
        raise DelegateError(
            "invalid_isolation",
            "isolation in input JSON must be auto, none, or worktree (null is not allowed).",
        )
    json_isolation = raw.get("isolation")
    if json_isolation is not None and json_isolation not in delegate_config.VALID_ISOLATION_VALUES:
        raise DelegateError(
            "invalid_isolation",
            "isolation in input JSON must be auto, none, or worktree.",
        )

    workspace = resolve_workspace(global_options.cwd, json_cwd)
    git_root, git_common_dir, git_head_oid, git_head_ref, git_branch = capture_git_metadata(
        workspace.path
    )

    try:
        effective_isolation = delegate_config.resolve_isolation(
            cli_value=global_options.isolation,
            input_json_value=json_isolation,
            loaded_config=config,
            engine=str(engine),
            mode=str(mode),
        )
    except delegate_config.InvalidIsolationError as exc:
        raise DelegateError("invalid_isolation", str(exc)) from exc

    isolation_context = build_isolation_context(
        source_workspace=workspace.path,
        resolved_isolation=effective_isolation,
        engine=str(engine),
        mode=str(mode),
        model_alias=model_alias,
        config=config,
        run_short_id=None,
        source_git_root=git_root,
        source_git_common_dir=git_common_dir,
        source_head_oid=git_head_oid,
        source_head_ref=git_head_ref,
        source_branch=git_branch,
    )
    _validate_forbid_commit(
        forbid_commit=raw_forbid_commit,
        mode=str(mode),
        isolation_context=isolation_context,
    )

    completion_report_mode = resolve_completion_report_mode(parsed, config)
    prompt = effective_prompt(
        validate_prompt(prompt),
        engine=str(engine),
        mode=str(mode),
        completion_report_mode=completion_report_mode,
    )
    return build_request(
        str(engine),
        str(mode),
        model_alias,
        workspace,
        prompt,
        config,
        dry_run=False,
        stream_capture=not global_options.pass_through,
        isolation_context=isolation_context,
        reasoning_effort=reasoning_effort,
        reasoning_effort_source="input-json" if reasoning_effort is not None else None,
        progress=effective_progress,
        progress_initial_delay_sec=progress_initial_delay_sec,
        progress_interval_sec=progress_interval_sec,
        forbid_commit=raw_forbid_commit,
    )


def build_request(
    engine: str,
    mode: str,
    model_alias: str | None,
    workspace: ResolvedWorkspace,
    prompt: str,
    config: JsonObject,
    dry_run: bool,
    *,
    stream_capture: bool = True,
    isolation_context: IsolationContext | None = None,
    reasoning_effort: str | None = None,
    reasoning_effort_source: str | None = None,
    progress: bool = False,
    progress_initial_delay_sec: float = delegate_runner.PROGRESS_INITIAL_DELAY_SEC,
    progress_interval_sec: float = delegate_runner.PROGRESS_HEARTBEAT_INTERVAL_SEC,
    forbid_commit: bool = False,
) -> Request:
    if not isinstance(workspace, ResolvedWorkspace):
        raise TypeError(
            "build_request requires a ResolvedWorkspace; "
            "wrap raw Git workspace paths with ResolvedWorkspace(path, 'git')."
        )
    requested_effort, effort_source = resolve_effective_reasoning_effort(
        config,
        engine,
        reasoning_effort,
        reasoning_effort_source=reasoning_effort_source,
    )

    return _build_request_for_workspace(
        engine,
        mode,
        model_alias,
        workspace,
        prompt,
        config,
        dry_run,
        stream_capture=stream_capture,
        isolation_context=isolation_context,
        requested_effort=requested_effort,
        effort_source=effort_source,
        progress=progress,
        progress_initial_delay_sec=progress_initial_delay_sec,
        progress_interval_sec=progress_interval_sec,
        forbid_commit=forbid_commit,
    )


def _cursor_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.model_alias, build.cache
    cursor = build.config["cursor"]
    capability, reasoning_warnings = _capability_with_config_fallback(
        lambda: resolve_cursor_reasoning_capability(cursor, build.requested_effort),
        engine="cursor",
        effort_source=build.effort_source,
    )
    model = capability.model if capability is not None else cursor["defaultModel"]
    argv = build_cursor_argv(
        cursor["argvPrefix"],
        build.mode,
        build.resolved.path,
        model,
        build.prompt,
        stream_capture=build.stream_capture,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=None,
        prompt_transport=PROMPT_TRANSPORT_ARGV,
        display_argv=redacted_prompt_argv(argv),
        warnings=reasoning_warnings,
        **reasoning_request_kwargs(capability, build.effort_source),
    )


def _droid_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    droid = build.config["droid"]
    models = droid["models"]
    if build.model_alias is None or build.model_alias not in models:
        raise DelegateError("invalid_alias", f"Unknown Droid model alias: {build.model_alias}")
    model = models[build.model_alias]
    if model.startswith("replace-with-") or model in {
        "your-droid-model-id",
        "real-droid-model-id",
    }:
        raise DelegateError(
            "unconfigured_model",
            (
                f"Droid model alias '{build.model_alias}' is still a placeholder. "
                "Copy config.example.json to ~/.delegate/config.json and set a real Droid model ID."
            ),
        )
    capability, reasoning_warnings = _capability_with_config_fallback(
        lambda: reasoning.resolve_reasoning_capability(
            harness="droid",
            model=model,
            requested_effort=build.requested_effort,
            config=build.config,
            cache=build.cache,
            alias=build.model_alias,
        ),
        engine="droid",
        effort_source=build.effort_source,
    )
    prompt = build.prompt
    if build.mode == MODE_SAFE:
        prompt = prefix_droid_safe_prompt(prompt)
    argv = build_droid_argv(
        droid["binary"],
        build.mode,
        build.resolved.path,
        model,
        prompt,
        stream_capture=build.stream_capture,
        reasoning_capability=capability,
        prompt_transport=PROMPT_TRANSPORT_FILE,
    )
    display_argv = [
        DROID_PROMPT_FILE_DISPLAY if item == DROID_PROMPT_FILE_ARG_PLACEHOLDER else item
        for item in argv
    ]
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_FILE,
        prompt_file_text=prompt,
        display_argv=display_argv,
        warnings=reasoning_warnings,
        **reasoning_request_kwargs(capability, build.effort_source),
    )


def _codex_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    codex = build.config["codex"]
    if isinstance(build.model_alias, str) and build.model_alias:
        model = build.model_alias
    else:
        model = _resolve_default_model(codex)
    policy = delegate_config.effective_policy(build.config, engine="codex", mode=build.mode)
    capability, reasoning_warnings = _capability_with_config_fallback(
        lambda: reasoning.resolve_reasoning_capability(
            harness="codex",
            model=model,
            requested_effort=build.requested_effort,
            config=build.config,
            cache=build.cache,
            alias=build.model_alias or model,
        ),
        engine="codex",
        effort_source=build.effort_source,
    )
    argv = build_codex_argv(
        codex,
        build.mode,
        build.resolved.path,
        model,
        build.prompt,
        policy,
        workspace_kind=build.resolved.kind,
        stream_capture=build.stream_capture,
        reasoning_capability=capability,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
        stdin_text=build.prompt,
        display_argv=list(argv),
        warnings=reasoning_warnings,
        **reasoning_request_kwargs(capability, build.effort_source),
    )


def _claude_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    claude = build.config["claude"]
    if isinstance(build.model_alias, str) and build.model_alias:
        model = build.model_alias
    else:
        model = _resolve_default_model(claude)
    policy = delegate_config.effective_policy(build.config, engine="claude", mode=build.mode)
    effort: str | None = None
    warnings: tuple[str, ...] = ()
    if build.requested_effort is not None:
        try:
            effort = reasoning.resolve_claude_native_effort(
                build.requested_effort,
                alias=build.model_alias,
                model=model,
            )
        except reasoning.ReasoningCapabilityError as exc:
            if build.effort_source != "config":
                raise DelegateError(exc.error, exc.message) from exc
            warnings = (f"ignoring claude.defaultReasoningEffort: {exc.message}",)
    argv = build_claude_argv(
        claude,
        build.mode,
        model,
        policy,
        stream_capture=build.stream_capture,
        reasoning_effort=effort,
        allow_bypass_permissions=_claude_harness_bypass_enabled(build.config, build.mode),
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
        stdin_text=build.prompt,
        display_argv=list(argv),
        warnings=warnings,
        **claude_reasoning_request_kwargs(effort, build.effort_source),
    )


def _kimi_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.effort_source, build.cache
    if build.requested_effort is not None:
        kimi = build.config.get("kimi")
        default_model = kimi.get("defaultModel") if isinstance(kimi, dict) else None
        model = default_model if isinstance(default_model, str) and default_model else None
        raise DelegateError(
            "unsupported_reasoning_effort",
            reasoning.format_explicit_reasoning_effort_error(
                harness="kimi",
                alias=build.model_alias,
                model=model,
                effort=build.requested_effort,
                detail="reasoning effort is not supported",
            ),
        )
    kimi = build.config["kimi"]
    if isinstance(build.model_alias, str) and build.model_alias:
        model = build.model_alias
    else:
        model = _resolve_default_model(kimi)
    argv = build_kimi_argv(
        kimi,
        build.mode,
        build.resolved.path,
        model,
        build.prompt,
        stream_capture=build.stream_capture,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_ARGV,
        display_argv=redacted_prompt_argv(argv, replacement=KIMI_PROMPT_REDACTION),
    )


EngineRequestPartsBuilder = Callable[[EngineBuildInput], EngineRequestParts]

ENGINE_REQUEST_PARTS_BUILDERS: dict[str, EngineRequestPartsBuilder] = {
    "cursor": _cursor_request_parts,
    "droid": _droid_request_parts,
    "codex": _codex_request_parts,
    "claude": _claude_request_parts,
    "kimi": _kimi_request_parts,
}


def _engine_request_parts(
    engine: str,
    *,
    build: EngineBuildInput,
) -> EngineRequestParts:
    builder = ENGINE_REQUEST_PARTS_BUILDERS.get(engine)
    if builder is None:
        raise DelegateError(
            "invalid_engine",
            f"engine must be {ENGINES_PROSE}.",
        )
    return builder(build)


def _build_request_for_workspace(
    engine: str,
    mode: str,
    model_alias: str | None,
    resolved: ResolvedWorkspace,
    prompt: str,
    config: JsonObject,
    dry_run: bool,
    *,
    stream_capture: bool,
    isolation_context: IsolationContext | None,
    requested_effort: str | None,
    effort_source: str | None,
    progress: bool,
    progress_initial_delay_sec: float,
    progress_interval_sec: float,
    forbid_commit: bool,
) -> Request:
    cache = (
        reasoning.load_reasoning_capability_cache(resolved.path)
        if requested_effort is not None
        else None
    )
    parts = _engine_request_parts(
        engine,
        build=EngineBuildInput(
            mode=mode,
            model_alias=model_alias,
            resolved=resolved,
            prompt=prompt,
            config=config,
            stream_capture=stream_capture,
            requested_effort=requested_effort,
            effort_source=effort_source,
            cache=cache,
        ),
    )
    return _apply_codex_auth(
        Request(
            engine,
            mode,
            resolved.path,
            prompt,
            parts.argv,
            parts.model,
            model_alias=parts.model_alias,
            dry_run=dry_run,
            workspace_kind=resolved.kind,
            isolation_context=isolation_context,
            reasoning_effort=parts.reasoning_effort,
            reasoning_effort_source=parts.reasoning_effort_source,
            reasoning_capability_source=parts.reasoning_capability_source,
            reasoning_transport=parts.reasoning_transport,
            progress=progress,
            progress_initial_delay_sec=progress_initial_delay_sec,
            progress_interval_sec=progress_interval_sec,
            forbid_commit=forbid_commit,
            warnings=parts.warnings,
            stdin_text=parts.stdin_text,
            prompt_file_text=parts.prompt_file_text,
            prompt_transport=parts.prompt_transport,
            display_argv=parts.display_argv,
        ),
        config,
    )


def _apply_codex_auth(request: Request, config: JsonObject) -> Request:
    if request.engine != "codex":
        return request
    overrides, auth_profile, fallback = codex_auth.resolve_codex_auth_for_request(config)
    if not overrides and auth_profile is None and fallback is None:
        return request
    return replace(
        request,
        env_overrides=overrides or None,
        auth_profile=auth_profile,
        fallback_auth_profile=fallback,
    )


def resolve_effective_reasoning_effort(
    config: JsonObject,
    engine: str,
    requested: str | None,
    *,
    reasoning_effort_source: str | None,
) -> tuple[str | None, str | None]:
    if requested is not None:
        try:
            return reasoning.normalize_effort(requested), reasoning_effort_source or "cli"
        except reasoning.ReasoningCapabilityError as exc:
            raise DelegateError(exc.error, exc.message) from exc
    section = config.get(engine)
    if isinstance(section, dict):
        default = section.get("defaultReasoningEffort")
        if default is not None:
            try:
                return reasoning.normalize_effort(default), "config"
            except reasoning.ReasoningCapabilityError as exc:
                raise DelegateError(exc.error, exc.message) from exc
    return None, None


def _capability_with_config_fallback(
    resolver: Callable[[], reasoning.ReasoningCapability | None],
    *,
    engine: str,
    effort_source: str | None,
) -> tuple[reasoning.ReasoningCapability | None, tuple[str, ...]]:
    """Resolve a capability, degrading config-default failures to a warning.

    An explicit per-run --reasoning-effort fails closed, but a config
    defaultReasoningEffort is a preference: when it cannot be satisfied (no
    resolved model, no capability declaration, no cursor mapping), the run
    proceeds without reasoning effort instead of bricking every launch of the
    engine until the config is edited.
    """
    try:
        return resolver(), ()
    except reasoning.ReasoningCapabilityError as exc:
        if effort_source != "config":
            raise DelegateError(exc.error, exc.message) from exc
        return None, (f"ignoring {engine}.defaultReasoningEffort: {exc.message}",)
    except DelegateError as exc:
        if effort_source != "config":
            raise
        return None, (f"ignoring {engine}.defaultReasoningEffort: {exc.message}",)


def resolve_cursor_reasoning_capability(
    cursor_config: JsonObject,
    requested_effort: str | None,
) -> reasoning.ReasoningCapability | None:
    if requested_effort is None:
        return None
    default_model = cursor_config.get("defaultModel")
    alias = default_model if isinstance(default_model, str) and default_model else None
    mappings = cursor_config.get("reasoningEffortModels")
    if not isinstance(mappings, dict):
        raise DelegateError(
            "unsupported_reasoning_effort",
            reasoning.format_explicit_reasoning_effort_error(
                harness="cursor",
                alias=alias,
                model=alias,
                effort=requested_effort,
                detail="requires cursor.reasoningEffortModels",
            ),
        )
    model = mappings.get(requested_effort)
    if not isinstance(model, str) or not model:
        supported = sorted(
            effort
            for effort, mapped_model in mappings.items()
            if isinstance(effort, str) and effort and isinstance(mapped_model, str) and mapped_model
        )
        raise DelegateError(
            "unsupported_reasoning_effort",
            reasoning.format_explicit_reasoning_effort_error(
                harness="cursor",
                alias=alias,
                model=alias,
                effort=requested_effort,
                supported=supported or None,
                detail="requires a configured model mapping",
            ),
        )
    return reasoning.ReasoningCapability(
        harness="cursor",
        model=model,
        effort=requested_effort,
        supported_efforts=(requested_effort,),
        default_effort=None,
        transport=reasoning.TRANSPORT_BY_HARNESS["cursor"],
        source="config",
    )


def reasoning_request_kwargs(
    capability: reasoning.ReasoningCapability | None,
    source: str | None,
) -> JsonObject:
    if capability is None:
        return {}
    return {
        "reasoning_effort": capability.effort,
        "reasoning_effort_source": source or "cli",
        "reasoning_capability_source": capability.source,
        "reasoning_transport": capability.transport,
    }


def claude_reasoning_request_kwargs(effort: str | None, source: str | None) -> JsonObject:
    """Reasoning payload fields for Claude's static native-effort flag.

    The sibling of ``reasoning_request_kwargs`` for the engine that resolves a
    flag (``--effort``) rather than a ``ReasoningCapability`` object. Returns
    ``{}`` when no effort applies so the request-parts defaults stand.
    """
    if effort is None:
        return {}
    return {
        "reasoning_effort": effort,
        "reasoning_effort_source": source,
        "reasoning_capability_source": "static",
        "reasoning_transport": reasoning.TRANSPORT_CLAUDE_EFFORT_FLAG,
    }


def _resolve_default_model(section: JsonObject) -> str | None:
    default_model = section.get("defaultModel")
    return default_model if isinstance(default_model, str) and default_model else None
