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
import shutil
import subprocess  # nosec B404 - Delegate inspects git workspaces with shell=False.
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent import harness_discovery, profiles, reasoning, safe_workspace, wsl
from delegate_agent import runner as delegate_runner
from delegate_agent.argv_builders import (
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
    prefix_droid_safe_prompt,
    redacted_prompt_argv,
)
from delegate_agent.constants import (
    ENGINES_PROSE,
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    MODE_WORK,
    MODELESS_ENGINES,
    PROMPT_ENFORCED_SAFE_ENGINES,
    PROMPT_INSTRUCTION_MODE_SLASH,
    PROMPT_INSTRUCTION_MODE_WRAPPED,
    PURE_CALL_ENGINES,
    SAFE_REVIEW_PREFIX_INJECTED_HERE_ENGINES,
    pure_call_supported,
    validate_mode,
)
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS, capture_git_metadata
from delegate_agent.git_utils import run_git as _run_git
from delegate_agent.isolation import IsolationContext, build_isolation_context
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.prompt_transport import (
    DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
    DEVIN_AGENT_CONFIG_DISPLAY,
    DROID_PROMPT_FILE_ARG_PLACEHOLDER,
    DROID_PROMPT_FILE_DISPLAY,
    KIMI_PROMPT_REDACTION,
    OMP_PROMPT_REDACTION,
    PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_FILE_DISPLAY,
    PROMPT_TRANSPORT_ARGV,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_models import (
    EngineBuildInput,
    EngineRequestParts,
    LaunchOptions,
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
    "fast",
    "outputSchema",
    "progress",
    "forbidCommit",
    "readOnly",
    "includeDirty",
    "agent",
    "promptInstructionMode",
    "workflowAgentKey",
    "pure",
    "timeout",
}

OUTPUT_SCHEMA_COMPLETION_REPORT_WARNING = (
    "--output-schema enforces a JSON-only final message; completion-report instruction "
    "suppressed for this run."
)
CALL_TEMP_CWD_PLACEHOLDER = "<delegate-call-temp-cwd>"
CODEX_HARNESS_DEFAULT_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
DEVIN_READ_ONLY_AGENT_CONFIG = {
    "permissions": {
        "allow": ["read", "grep", "glob", "Read(/**)"],
        "deny": ["edit", "write", "exec", "Write(/**)", "mcp__*"],
    }
}
DEVIN_READ_ONLY_AGENT_CONFIG_TEXT = json.dumps(DEVIN_READ_ONLY_AGENT_CONFIG, separators=(",", ":"))
OPENCODE_SAFE_AGENT = "delegate-read-only"
OPENCODE_SAFE_PERMISSION = {
    "*": "deny",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "edit": "deny",
    "bash": "deny",
    "task": "deny",
    "skill": "deny",
    "external_directory": "deny",
    "webfetch": "deny",
}
OPENCODE_SAFE_PERMISSION_JSON = json.dumps(OPENCODE_SAFE_PERMISSION, separators=(",", ":"))
OPENCODE_PURE_PERMISSION = {key: "deny" for key in OPENCODE_SAFE_PERMISSION}
OPENCODE_PURE_PERMISSION_JSON = json.dumps(OPENCODE_PURE_PERMISSION, separators=(",", ":"))

# Read-only call is the stateless "judge/completion" contract: text in, text out,
# no tree. These harnesses default to a coding-agent framing ("inspect the
# workspace") that derails a judge prompt on an empty cwd, so neutralize that
# framing. The no-mutation clause is load-bearing for cursor/droid/kimi, whose
# read-only call has no CLI sandbox — the prompt is the only write boundary there
# (codex/claude/grok also get a real read-only sandbox flag). Work-level call is
# left raw — it may legitimately act in the cwd.
CALL_READONLY_PREAMBLE = (
    "You are being called to respond to the following prompt directly. There is "
    "no repository, working tree, or codebase to inspect, open, or review, and "
    "you must not create, edit, delete, or execute anything — produce your answer "
    "from the prompt text alone.\n\n"
)


def _call_effective_prompt(prompt: str, *, read_only: bool) -> str:
    if read_only and not prompt.startswith(CALL_READONLY_PREAMBLE):
        return f"{CALL_READONLY_PREAMBLE}{prompt}"
    return prompt


def _policy_mode(build: EngineBuildInput) -> str:
    """Resolve which policy tier a call inherits: default call is work-level, a
    read-only call is safe-level. Bypass flags stay bound to real work mode via
    the argv builders' own ``mode == MODE_WORK`` gates, so borrowing the work
    policy tier here only carries webSearch/networkAccess, never a bypass."""
    if build.mode == MODE_CALL:
        return MODE_SAFE if build.call_read_only else MODE_WORK
    return build.mode


def _reject_windows_path(value: str, field: str) -> None:
    if wsl.should_reject_windows_path(value):
        raise DelegateError("windows_path", wsl.windows_path_message(field, value))


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
    _reject_windows_path(path_text, "cwd")
    path = Path(path_text).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise DelegateError("invalid_cwd", f"cwd does not exist or is not a directory: {path}")
    git_root = git_root_for(path)
    if git_root is not None:
        return ResolvedWorkspace(git_root, "git")
    return ResolvedWorkspace(str(path), "directory")


def git_root_for(path: Path) -> str | None:
    message = wsl.windows_git_message(shutil.which("git"))
    if message is not None:
        raise DelegateError("windows_git_in_wsl", message)
    try:
        result = _run_git(
            str(path),
            ["rev-parse", "--show-toplevel"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
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
    if prompt_file is not None:
        _reject_windows_path(prompt_file, "--prompt-file")
        path = Path(prompt_file).expanduser()
        try:
            return validate_prompt(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DelegateError("prompt_file_not_found", f"Prompt file not found: {path}") from None
    if stdin_text is not None:
        return validate_prompt(stdin_text)
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
    cleaned = "".join(ch for ch in prompt if ord(ch) >= 0x20 or ch in ("\n", "\r", "\t"))
    if not cleaned.strip():
        raise DelegateError("empty_prompt", "Prompt is empty.")
    return cleaned


def resolve_output_schema(engine: str, output_schema: object) -> str | None:
    if output_schema is None:
        return None
    if engine == "grok":
        raise DelegateError(
            "unsupported_output_schema",
            "Grok --json-schema forces final json output, which breaks Delegate tracked "
            "streaming snapshots; --output-schema is not supported for grok in v1.",
        )
    if engine not in {"codex", "claude"}:
        raise DelegateError(
            "unsupported_output_schema",
            "--output-schema/outputSchema is only supported by codex and claude; "
            f"{engine} has no native schema enforcement.",
        )
    if not isinstance(output_schema, str) or not output_schema:
        raise DelegateError("invalid_output_schema", "outputSchema must be a non-empty string.")
    _reject_windows_path(output_schema, "outputSchema")
    path = Path(output_schema).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists():
        raise DelegateError("output_schema_not_found", f"Output schema not found: {path}")
    if not path.is_file():
        raise DelegateError("invalid_output_schema", f"Output schema is not a file: {path}")
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise DelegateError(
            "invalid_output_schema", f"Output schema is not readable: {path}"
        ) from exc
    return str(path)


def _validate_output_schema_mode(engine: str, mode: str, output_schema: object) -> None:
    if engine == "claude" and output_schema is not None and mode != MODE_CALL:
        raise DelegateError(
            "unsupported_output_schema", "Claude --output-schema is only supported in call mode."
        )


def _completion_report_prompt_mode(
    completion_report_mode: str,
    output_schema: str | None,
) -> tuple[str, tuple[str, ...]]:
    if (
        output_schema is not None
        and completion_report_mode == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN
    ):
        return (
            delegate_config.COMPLETION_REPORT_MODE_NONE,
            (OUTPUT_SCHEMA_COMPLETION_REPORT_WARNING,),
        )
    return completion_report_mode, ()


def resolve_prompt_instruction_mode(
    prompt: str,
    *,
    engine: str,
    mode: str,
) -> str:
    """Resolve wrapped vs slash-passthrough for a top-level launch prompt.

    Auto-detection applies only here, where position zero is invoker-controlled;
    interpolated content (e.g. workflow stage output) must never be sniffed.
    """
    if not delegate_runner.detect_slash_command(prompt):
        return PROMPT_INSTRUCTION_MODE_WRAPPED
    if mode == MODE_SAFE and engine in PROMPT_ENFORCED_SAFE_ENGINES:
        raise DelegateError(
            "slash_passthrough_unsupported",
            f"{engine} safe mode is prompt-enforced; a verbatim prompt would strip "
            "the read-only review contract. Use codex/claude/grok safe, or "
            f"{engine} work mode.",
        )
    return PROMPT_INSTRUCTION_MODE_SLASH


def resolve_input_json_prompt_instruction_mode(
    raw_mode: object,
    prompt: str,
    *,
    engine: str,
    mode: str,
) -> str:
    if raw_mode is None:
        return resolve_prompt_instruction_mode(prompt, engine=engine, mode=mode)
    if raw_mode not in {PROMPT_INSTRUCTION_MODE_WRAPPED, PROMPT_INSTRUCTION_MODE_SLASH}:
        raise DelegateError(
            "invalid_prompt_instruction_mode",
            "promptInstructionMode must be wrapped or slash-passthrough.",
        )
    if raw_mode == PROMPT_INSTRUCTION_MODE_SLASH:
        if mode == MODE_SAFE and engine in PROMPT_ENFORCED_SAFE_ENGINES:
            raise DelegateError(
                "slash_passthrough_unsupported",
                f"{engine} safe mode is prompt-enforced; a verbatim prompt would strip "
                "the read-only review contract. Use codex/claude/grok safe, or "
                f"{engine} work mode.",
            )
        return PROMPT_INSTRUCTION_MODE_SLASH
    return PROMPT_INSTRUCTION_MODE_WRAPPED


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
    instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED,
    skip_skill_preamble: bool = False,
) -> str:
    if instruction_mode == PROMPT_INSTRUCTION_MODE_SLASH:
        # Verbatim means verbatim: no skill preamble, no safe prefix, no
        # completion-report suffix. Harness slash commands need position zero.
        return prompt
    if mode == MODE_CALL:
        return prompt
    if not skip_skill_preamble:
        # --pass-through suppresses delegate's instruction wrapping but keeps the
        # safe-review prefix: that prefix is the write boundary for prompt-enforced
        # safe engines, not report plumbing.
        prompt = delegate_runner.prepend_skill_review_instructions(prompt)
    safe_prefix = (
        SAFE_REVIEW_PREFIX_BY_ENGINE.get(engine)
        if engine in SAFE_REVIEW_PREFIX_INJECTED_HERE_ENGINES
        else None
    )
    if mode == MODE_SAFE and safe_prefix is not None and safe_prefix not in prompt:
        # When the skill preamble is present it is guaranteed at index 0, so the
        # provider-specific safe prefix slots in between it and the user prompt.
        insert_at = (
            len(delegate_runner.SKILL_REVIEW_PREFIX)
            if prompt.startswith(delegate_runner.SKILL_REVIEW_PREFIX)
            else 0
        )
        prompt = prompt[:insert_at] + safe_prefix + prompt[insert_at:]
    if completion_report_mode == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN:
        return delegate_runner.append_completion_report_instructions(prompt)
    return prompt


def _safe_dirty_tree_note(
    resolved: ResolvedWorkspace,
    mode: str,
    isolation_context: IsolationContext | None,
) -> str | None:
    if mode != MODE_SAFE or resolved.kind != "git":
        return None
    if isolation_context is None or isolation_context.effective_isolation != "worktree":
        return None
    git_root = isolation_context.source_git_root
    if git_root is None or not safe_workspace.git_head_exists(git_root):
        return None
    # ponytail: one bounded git status probe per safe review; cache if this ever shows up hot.
    changed = safe_workspace.changed_files_vs_head(git_root)
    if not changed:
        return None
    shown = [f"`{path}`" for path in changed[:20]]
    remaining = len(changed) - len(shown)
    if remaining > 0:
        shown.append(f"+{remaining} more")
    return (
        f"Note: {len(changed)} file(s) have uncommitted/untracked changes synced into "
        f"this review copy: {', '.join(shown)}. Run `git diff HEAD` and check "
        "untracked files in the workspace to see them."
    )


def _append_safe_dirty_tree_note(
    prompt: str,
    resolved: ResolvedWorkspace,
    mode: str,
    isolation_context: IsolationContext | None,
) -> str:
    note = _safe_dirty_tree_note(resolved, mode, isolation_context)
    if note is None:
        return prompt
    return f"{prompt}\n\n{note}"


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
    source_workspace = (
        isolation_context.source_workspace
        if isolation_context is not None
        else "the selected workspace"
    )
    if isolation_context is not None and isolation_context.source_git_root is None:
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit needs worktree isolation, which requires a Git workspace; "
            f"{source_workspace} is not a Git repo, so no-commit enforcement isn't "
            "available here. Omit --forbid-commit (the child may commit), or run "
            "from a Git workspace.",
        )
    if isolation_context is None or isolation_context.isolation_lifecycle != "persistent":
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit requires --isolation worktree so Delegate can enforce "
            "the policy — add --isolation worktree, or omit --forbid-commit.",
        )


def _forbid_commit_implied_isolation_note() -> str:
    """The single source of the --forbid-commit implies --isolation worktree note."""
    return "note: --forbid-commit implies --isolation worktree"


def _validate_agent_option(engine: str, agent: str | None) -> None:
    if agent is not None and engine != "opencode":
        raise DelegateError("unsupported_agent", "agent is only supported for opencode.")


def _apply_forbid_commit_isolation_implication(
    *,
    forbid_commit: bool,
    mode: str,
    cli_isolation: str | None,
    json_isolation: str | None,
) -> tuple[str | None, str | None, bool]:
    """Apply the forbid-commit isolation implication shared by CLI and input-json paths.

    Returns ``(resolved_json_isolation, note, implied)``:
    - When forbid-commit is active in work mode with no explicit isolation, the
      implied worktree isolation is returned for the JSON path (the CLI path
      sets this in the parser), plus the note, and ``implied=True``.
    - When forbid-commit is active in work mode with explicit ``none``,
      an ``invalid_option_combination`` error is raised (both paths share this).
    - Otherwise the inputs are returned unchanged with ``implied=False``.

    This is the single place that owns the implication so ``run --input-json``
    with ``forbidCommit: true`` and no isolation gets the same implied worktree
    isolation + note as the CLI path.
    """
    if not forbid_commit or mode != MODE_WORK:
        return json_isolation, None, False
    effective = cli_isolation if cli_isolation is not None else json_isolation
    if effective is None:
        return "worktree", _forbid_commit_implied_isolation_note(), True
    if effective == "none":
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit cannot be combined with --isolation none. "
            "Use --isolation worktree, or omit --forbid-commit.",
        )
    return json_isolation, None, False


def _droid_models_map(config: JsonObject) -> dict:
    # config validation treats a null/absent models map as empty for every
    # engine; mirror that here so droid never crashes on `"models": null`.
    models = config["droid"].get("models")
    return models if isinstance(models, dict) else {}


def _validate_droid_model_alias(config: JsonObject, model_alias: str | None) -> None:
    if model_alias is None or model_alias not in _droid_models_map(config):
        raise DelegateError("invalid_alias", f"Unknown Droid model alias: {model_alias}")


def resolve_model_selection(engine_section: JsonObject, value: str) -> str:
    """Resolve ``value`` against ``<engine>.models`` aliases, else pass through."""
    models = engine_section.get("models")
    if isinstance(models, dict) and value in models:
        mapped = models[value]
        if isinstance(mapped, str):
            return mapped
    return value


def _resolve_engine_model(section: JsonObject, build: EngineBuildInput) -> str | None:
    """Per-run model: ``model_override`` > ``model_alias`` > ``defaultModel``.

    Both override and alias values go through ``resolve_model_selection``
    (alias-or-id). Computed before reasoning-capability resolution so effort
    validation follows the selected model.
    """
    if build.model_override is not None:
        return resolve_model_selection(section, build.model_override)
    if isinstance(build.model_alias, str) and build.model_alias:
        return resolve_model_selection(section, build.model_alias)
    return _resolve_default_model(section)


def _resolve_model_context(
    section: JsonObject,
    build: EngineBuildInput,
    *,
    harness: str,
) -> tuple[str | None, str | None, str | None]:
    """Return argv model, capability model, and capability-model source.

    A discovered native default is deliberately metadata/validation context
    only. Passing it to an argv builder would turn a refresh into a persistent
    pin and defeat the harness's own default selection.
    """
    selected = _resolve_engine_model(section, build)
    if selected is not None:
        source = (
            "explicit"
            if build.model_override is not None or build.model_alias is not None
            else "config"
        )
        return selected, selected, source
    discovered = reasoning.discovery_default_model(build.discovery, harness)
    if discovered is not None:
        return None, discovered, "discovery"
    return None, None, None


def _model_context_kwargs(
    capability_model: str | None, capability_model_source: str | None
) -> JsonObject:
    return {
        "capability_model": capability_model,
        "capability_model_source": capability_model_source,
    }


def _runtime_discovery_for_engine(
    config: JsonObject,
    engine: str,
    discovery: JsonObject | None,
    profile_resolution: profiles.ProfileResolution,
) -> JsonObject | None:
    """Drop only a selector-drifted engine record without probing the harness."""
    if not isinstance(discovery, dict):
        return None
    harnesses = discovery.get("harnesses")
    record = harnesses.get(engine) if isinstance(harnesses, dict) else None
    # Real snapshots are strictly validated at load. The shape guard keeps this
    # helper tolerant of direct unit fixtures while ensuring every persisted
    # installed record is tied to the configured opaque selector.
    if not isinstance(record, dict) or record.get("installed") is not True:
        return discovery
    selector = record.get("selector")
    selector_missing = not isinstance(selector, list) or not selector
    if not selector_missing and not harness_discovery.selector_has_drifted(
        config, engine, record, profile=profile_resolution
    ):
        return discovery
    return {
        **discovery,
        "harnesses": {harness: value for harness, value in harnesses.items() if harness != engine},
    }


def _resolve_opencode_alias(section: JsonObject, value: str) -> tuple[str, str | None]:
    models = section.get("models")
    if isinstance(models, dict) and value in models:
        mapped = models[value]
        if isinstance(mapped, str):
            return mapped, None
        if isinstance(mapped, dict):
            model = mapped.get("model")
            variant = mapped.get("variant")
            if isinstance(model, str):
                return model, variant if isinstance(variant, str) else None
    return value, None


def _reject_opencode_flag_like_value(value: str | None, *, field: str, error: str) -> None:
    """Reject empty or leading-dash values that would inject argv flags."""
    if value is None:
        return
    if not value.strip() or value.startswith("-"):
        raise DelegateError(
            error,
            f"{field} must be a non-empty string that does not start with '-'.",
        )


def _resolve_opencode_selection_detail(
    section: JsonObject, build: EngineBuildInput
) -> tuple[str | None, str | None, str | None]:
    alias_variant: str | None = None
    if build.model_override is not None:
        _reject_opencode_flag_like_value(build.model_override, field="model", error="invalid_model")
        model, alias_variant = _resolve_opencode_alias(section, build.model_override)
    elif isinstance(build.model_alias, str) and build.model_alias:
        _reject_opencode_flag_like_value(build.model_alias, field="model", error="invalid_model")
        model, alias_variant = _resolve_opencode_alias(section, build.model_alias)
    else:
        model = _resolve_default_model(section)
    _reject_opencode_flag_like_value(model, field="model", error="invalid_model")
    if build.requested_effort is not None and build.effort_source != "config":
        _reject_opencode_flag_like_value(
            build.requested_effort, field="reasoningEffort", error="invalid_reasoning_effort"
        )
        return model, build.requested_effort, build.effort_source or "cli"
    if alias_variant is not None:
        _reject_opencode_flag_like_value(
            alias_variant, field="variant", error="invalid_reasoning_effort"
        )
        try:
            return model, reasoning.normalize_effort(alias_variant), "alias"
        except reasoning.ReasoningCapabilityError as exc:
            raise DelegateError(exc.error, f"opencode alias variant: {exc.message}") from exc
    if build.requested_effort is not None:
        _reject_opencode_flag_like_value(
            build.requested_effort, field="reasoningEffort", error="invalid_reasoning_effort"
        )
        return model, build.requested_effort, build.effort_source or "config"
    return model, None, None


def _resolve_opencode_agent(section: JsonObject, build: EngineBuildInput) -> str | None:
    if isinstance(build.agent, str) and build.agent.strip():
        _reject_opencode_flag_like_value(build.agent, field="agent", error="invalid_agent")
        return build.agent
    default = section.get("defaultAgent")
    if isinstance(default, str) and default.strip():
        _reject_opencode_flag_like_value(default, field="agent", error="invalid_agent")
        return default
    return None


def _resolve_pi_family_alias(section: JsonObject, value: str) -> tuple[str, str | None]:
    models = section.get("models")
    if isinstance(models, dict) and value in models:
        mapped = models[value]
        if isinstance(mapped, str):
            return mapped, None
        if isinstance(mapped, dict):
            model = mapped.get("model")
            thinking = mapped.get("thinking")
            if isinstance(model, str):
                return model, thinking if isinstance(thinking, str) else None
    return value, None


def _reject_pi_model_thinking_suffix(model: str | None) -> None:
    if isinstance(model, str) and ":" in model:
        raise DelegateError(
            "invalid_model",
            "model must not contain ':'; use the alias thinking field or --reasoning-effort.",
        )


def _resolve_pi_family_selection_detail(
    section: JsonObject,
    build: EngineBuildInput,
    *,
    engine: str,
) -> tuple[str | None, str | None, str | None]:
    alias_thinking: str | None = None
    selection = build.model_override or build.model_alias
    if selection:
        _reject_opencode_flag_like_value(selection, field="model", error="invalid_model")
        model, alias_thinking = _resolve_pi_family_alias(section, selection)
    else:
        model = _resolve_default_model(section)
    _reject_opencode_flag_like_value(model, field="model", error="invalid_model")
    _reject_pi_model_thinking_suffix(model)

    if build.requested_effort is not None and build.effort_source != "config":
        try:
            thinking = reasoning.normalize_effort(build.requested_effort)
        except reasoning.ReasoningCapabilityError as exc:
            raise DelegateError(exc.error, exc.message) from exc
        return model, thinking, build.effort_source or "cli"
    if alias_thinking is not None:
        if alias_thinking not in reasoning.PI_THINKING_LEVELS:
            raise DelegateError(
                "invalid_reasoning_effort",
                f"{engine} alias thinking must be one of: {', '.join(reasoning.PI_THINKING_LEVELS)}.",
            )
        return model, alias_thinking, "alias"
    if build.requested_effort is not None:
        if build.effort_source == "config":
            return model, build.requested_effort, "config"
        try:
            thinking = reasoning.normalize_effort(build.requested_effort)
        except reasoning.ReasoningCapabilityError as exc:
            raise DelegateError(exc.error, exc.message) from exc
        return model, thinking, build.effort_source or "cli"
    return model, None, None


def _effective_cli_model_alias(launch: LaunchOptions, config: JsonObject) -> str | None:
    """The alias the built Request will carry — for isolation-context parity.

    Worktree branch/path planning happens before request parts resolve, so it
    must predict the same model_alias the execution path will record: droid's
    positional, a droid --model value that hits the alias map, or the modeless
    --model selection token.
    """
    alias, _ = _classify_cli_model(launch)
    if launch.engine == "droid" and launch.model is not None:
        models = config.get("droid", {}).get("models")
        if isinstance(models, dict) and launch.model in models:
            return launch.model
    return alias


def _classify_cli_model(launch: LaunchOptions) -> tuple[str | None, str | None]:
    """Split a CLI launch's model selection into (model_alias, model_override).

    Modeless engines route --model through the model_alias channel — the same
    one input-JSON uses — so manifests record modelAlias identically for both
    (``codex:fast`` selectors keep working) while resolution stays alias-or-id.
    Droid keeps its positional in model_alias (strict) and --model in
    model_override (alias-or-id, classified inside _droid_request_parts).
    """
    if launch.engine == "droid":
        return launch.model_alias, launch.model
    if launch.model is not None:
        return launch.model, None
    return launch.model_alias, None


def _reject_droid_model_conflict(model_alias: str | None, model_override: str | None) -> None:
    if model_alias is not None and model_override is not None:
        raise DelegateError(
            "model_conflict",
            "Give either the positional droid model alias or --model, not both.",
        )


def _guard_droid_placeholder_model(model: str, *, alias: str | None) -> None:
    if model.startswith("replace-with-") or model in {
        "your-droid-model-id",
        "real-droid-model-id",
    }:
        label = f"'{alias}'" if alias else "selection"
        raise DelegateError(
            "unconfigured_model",
            (
                f"Droid model {label} is still a placeholder. "
                "Copy config.example.json to ~/.delegate/config.json and set a real Droid model ID."
            ),
        )


def _validate_include_dirty(
    *,
    include_dirty: bool,
    mode: str,
    isolation_context: IsolationContext | None,
) -> None:
    if not include_dirty:
        return
    if mode != MODE_WORK or isolation_context is None:
        raise DelegateError(
            "invalid_option_combination",
            "--include-dirty requires work mode with --isolation worktree.",
        )
    if isolation_context.isolation_lifecycle != "persistent":
        raise DelegateError(
            "invalid_option_combination",
            "--include-dirty requires work mode with --isolation worktree.",
        )


def _call_workspace(dry_run: bool) -> tuple[ResolvedWorkspace, bool]:
    if dry_run:
        return ResolvedWorkspace(CALL_TEMP_CWD_PLACEHOLDER, "directory"), False
    temp_dir = tempfile.mkdtemp(prefix="delegate-call-")
    return ResolvedWorkspace(temp_dir, "directory"), True


def _validate_call_cli_options(global_options: object, launch: object) -> None:
    cwd = getattr(global_options, "cwd", None)
    isolation = getattr(global_options, "isolation", None)
    pass_through = getattr(global_options, "pass_through", False)
    completion_report = getattr(global_options, "completion_report", None)
    progress_intent = getattr(launch, "progress_intent", None)
    forbid_commit = getattr(launch, "forbid_commit", False)
    include_dirty = getattr(launch, "include_dirty", False)
    pure = getattr(launch, "pure", False)
    read_only = getattr(launch, "read_only", False)
    group = getattr(global_options, "group", None)
    # Grouped call runs may take --cwd so the run registers in the invocation
    # workspace registry; ungrouped call still rejects --cwd.
    if cwd is not None and group is None:
        raise DelegateError("invalid_option_combination", "call mode does not use --cwd.")
    if isolation is not None:
        raise DelegateError("invalid_option_combination", "call mode does not use --isolation.")
    if pass_through:
        raise DelegateError(
            "invalid_option_combination",
            "--pass-through is not supported with call mode; call already returns synchronously.",
        )
    if completion_report == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN:
        raise DelegateError(
            "invalid_option_combination",
            "--completion-report is not supported with call mode.",
        )
    if progress_intent == "on":
        raise DelegateError(
            "invalid_option_combination", "--progress is not supported with call mode."
        )
    if forbid_commit:
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit requires work mode with persistent worktree isolation.",
        )
    if include_dirty:
        raise DelegateError(
            "invalid_option_combination",
            "--include-dirty requires work mode with persistent worktree isolation.",
        )
    _validate_pure_call(
        getattr(launch, "engine", ""),
        pure=pure,
        read_only=read_only,
        group=group,
    )


def _validate_pure_call(
    engine: str, *, pure: bool, read_only: bool, group: str | None = None
) -> None:
    if not pure:
        return
    if group is not None:
        raise DelegateError(
            "pure_conflicts_group",
            "--pure cannot be combined with --group; pure call is a stateless one-hop completion.",
        )
    if read_only:
        raise DelegateError(
            "pure_conflicts_read_only", "--pure cannot be combined with --read-only."
        )
    if not pure_call_supported(engine):
        supported = ", ".join(PURE_CALL_ENGINES)
        raise DelegateError(
            "unsupported_pure_call",
            f"{engine} does not support pure call mode.",
            next_actions=[f"Use --pure with one of: {supported}."],
        )


def _validate_call_input_json_options(
    global_options: object,
    raw: JsonObject,
    *,
    raw_progress_intent: ProgressIntent,
    raw_forbid_commit: bool,
    raw_include_dirty: bool,
) -> None:
    group = getattr(global_options, "group", None)
    if getattr(global_options, "cwd", None) is not None and group is None:
        raise DelegateError("invalid_option_combination", "call mode does not use --cwd.")
    if getattr(global_options, "isolation", None) is not None:
        raise DelegateError("invalid_option_combination", "call mode does not use --isolation.")
    if getattr(global_options, "pass_through", False):
        raise DelegateError(
            "invalid_option_combination",
            "--pass-through is not supported with call mode; call already returns synchronously.",
        )
    if (
        getattr(global_options, "completion_report", None)
        == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN
    ):
        raise DelegateError(
            "invalid_option_combination",
            "--completion-report is not supported with call mode.",
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
    if raw_progress_intent == "on":
        raise DelegateError(
            "invalid_option_combination", "progress is not supported with call mode."
        )
    if raw_forbid_commit:
        raise DelegateError(
            "invalid_option_combination",
            "forbidCommit requires work mode with persistent worktree isolation.",
        )
    if raw_include_dirty:
        raise DelegateError(
            "invalid_option_combination",
            "includeDirty requires work mode with persistent worktree isolation.",
        )


def _safe_none_normalization_warnings(
    *,
    engine: str,
    mode: str,
    requested: str | None,
    effective: str,
) -> tuple[str, ...]:
    if (
        mode == MODE_SAFE
        and requested == delegate_config.ISOLATION_NONE
        and effective == delegate_config.ISOLATION_AUTO
        and engine in delegate_config.SAFE_ISOLATION_REQUIRED_ENGINES
    ):
        return (
            f"isolation none is not used for {engine} safe mode; using auto "
            "temporary isolation instead.",
        )
    return ()


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
    _validate_agent_option(launch.engine, launch.agent)
    if launch.mode == MODE_CALL:
        _validate_call_cli_options(global_options, launch)
        read_only = getattr(launch, "read_only", False)
        pure = getattr(launch, "pure", False)
        if launch.engine == "droid":
            _reject_droid_model_conflict(launch.model_alias, launch.model)
            if launch.model_alias is not None:
                _validate_droid_model_alias(config, launch.model_alias)
        output_schema = resolve_output_schema(launch.engine, launch.output_schema)
        raw_prompt = resolve_prompt(launch.prompt_parts, launch.prompt_file, stdin)
        if read_only and delegate_runner.detect_slash_command(raw_prompt):
            raise DelegateError(
                "slash_passthrough_unsupported",
                "call --read-only wraps the prompt in the read-only contract; "
                "slash-command prompts cannot run verbatim there. Use plain call mode.",
            )
        prompt = _call_effective_prompt(raw_prompt, read_only=read_only)
        workspace, cleanup_workspace = _call_workspace(launch.dry_run)
        cli_model_alias, cli_model_override = _classify_cli_model(launch)
        try:
            return build_request(
                launch.engine,
                launch.mode,
                cli_model_alias,
                workspace,
                prompt,
                config,
                launch.dry_run,
                stream_capture=True,
                isolation_context=None,
                reasoning_effort=launch.reasoning_effort,
                reasoning_effort_source="cli" if launch.reasoning_effort is not None else None,
                fast=launch.fast,
                progress=False,
                forbid_commit=False,
                auth_profile_override=global_options.auth_profile,
                output_schema=output_schema,
                cleanup_workspace=cleanup_workspace,
                call_read_only=read_only,
                pure=pure,
                timeout=launch.timeout,
                group=global_options.group,
                agent=launch.agent,
                model_override=cli_model_override,
            )
        except BaseException:
            if cleanup_workspace:
                shutil.rmtree(workspace.path, ignore_errors=True)
            raise
    if getattr(launch, "read_only", False):
        raise DelegateError(
            "invalid_option_combination",
            "--read-only only applies to call mode.",
        )
    if getattr(launch, "pure", False):
        raise DelegateError("unsupported_pure_call", "--pure only applies to call mode.")
    effective_progress = resolve_effective_progress(launch.progress_intent, config)
    if effective_progress and global_options.pass_through:
        raise DelegateError(
            "invalid_option_combination",
            "--progress is incompatible with --pass-through.",
        )
    progress_initial_delay_sec, progress_interval_sec = resolve_progress_timing(config)
    _validate_output_schema_mode(launch.engine, launch.mode, launch.output_schema)
    output_schema = resolve_output_schema(launch.engine, launch.output_schema)
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
    isolation_warnings = list(
        _safe_none_normalization_warnings(
            engine=launch.engine,
            mode=launch.mode,
            requested=global_options.isolation,
            effective=effective_isolation,
        )
    )
    if getattr(launch, "forbid_commit_implied_isolation", False):
        isolation_warnings.append(_forbid_commit_implied_isolation_note())

    isolation_context = build_isolation_context(
        source_workspace=workspace.path,
        resolved_isolation=effective_isolation,
        engine=launch.engine,
        mode=launch.mode,
        model_alias=_effective_cli_model_alias(launch, config),
        config=config,
        run_short_id="<short-run-id-placeholder>" if launch.dry_run else None,
        source_git_root=git_root,
        source_git_common_dir=git_common_dir,
        source_head_oid=git_head_oid,
        source_head_ref=git_head_ref,
        source_branch=git_branch,
        include_dirty=launch.include_dirty,
    )
    _validate_forbid_commit(
        forbid_commit=launch.forbid_commit,
        mode=launch.mode,
        isolation_context=isolation_context,
    )
    _validate_include_dirty(
        include_dirty=launch.include_dirty,
        mode=launch.mode,
        isolation_context=isolation_context,
    )
    if launch.engine == "droid":
        _reject_droid_model_conflict(launch.model_alias, launch.model)
        if launch.model_alias is not None:
            _validate_droid_model_alias(config, launch.model_alias)

    completion_report_mode = resolve_completion_report_mode(parsed, config)
    completion_report_prompt_mode, output_schema_warnings = _completion_report_prompt_mode(
        completion_report_mode,
        output_schema,
    )
    instruction_mode = resolve_prompt_instruction_mode(
        prompt,
        engine=launch.engine,
        mode=launch.mode,
    )
    prompt = effective_prompt(
        prompt,
        engine=launch.engine,
        mode=launch.mode,
        completion_report_mode=completion_report_prompt_mode,
        instruction_mode=instruction_mode,
        skip_skill_preamble=global_options.pass_through,
    )
    cli_model_alias, cli_model_override = _classify_cli_model(launch)
    return build_request(
        launch.engine,
        launch.mode,
        cli_model_alias,
        workspace,
        prompt,
        config,
        launch.dry_run,
        stream_capture=not global_options.pass_through,
        isolation_context=isolation_context,
        reasoning_effort=launch.reasoning_effort,
        reasoning_effort_source="cli" if launch.reasoning_effort is not None else None,
        fast=launch.fast,
        progress=effective_progress,
        progress_initial_delay_sec=progress_initial_delay_sec,
        progress_interval_sec=progress_interval_sec,
        forbid_commit=launch.forbid_commit,
        include_dirty=launch.include_dirty,
        auth_profile_override=global_options.auth_profile,
        output_schema=output_schema,
        warnings=(*output_schema_warnings, *isolation_warnings),
        timeout=launch.timeout,
        group=global_options.group,
        prompt_instruction_mode=instruction_mode,
        agent=launch.agent,
        model_override=cli_model_override,
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
    _reject_windows_path(run_json.input_json, "--input-json")
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
        raise DelegateError("invalid_mode", "mode must be safe, work, or call.")
    validate_mode(mode)
    if not isinstance(prompt, str):
        raise DelegateError("invalid_prompt", "prompt must be a string.")
    model_alias = raw.get("model")
    raw_reasoning_effort = raw.get("reasoningEffort")
    if raw_reasoning_effort is not None:
        if isinstance(raw_reasoning_effort, str) and raw_reasoning_effort.startswith("-"):
            raise DelegateError(
                "invalid_reasoning_effort",
                "reasoningEffort must be a non-empty string that does not start with '-'.",
            )
        try:
            reasoning_effort = reasoning.normalize_effort(raw_reasoning_effort)
        except reasoning.ReasoningCapabilityError as exc:
            raise DelegateError(exc.error, "reasoningEffort must be a non-empty string.") from exc
    else:
        reasoning_effort = None
    if "fast" in raw and engine != "codex":
        raise DelegateError("unsupported_fast", "fast is only supported by codex.")
    raw_fast = raw.get("fast")
    if raw_fast is not None and not isinstance(raw_fast, bool):
        raise DelegateError("invalid_fast", "fast must be true, false, null, or omitted.")
    fast = raw_fast if isinstance(raw_fast, bool) else None
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
    raw_read_only = raw.get("readOnly", False)
    if not isinstance(raw_read_only, bool):
        raise DelegateError("invalid_read_only", "readOnly must be true or false.")
    if raw_read_only and mode != MODE_CALL:
        raise DelegateError(
            "invalid_option_combination",
            "readOnly only applies to call mode.",
        )
    raw_include_dirty = raw.get("includeDirty", False)
    if not isinstance(raw_include_dirty, bool):
        raise DelegateError("invalid_include_dirty", "includeDirty must be true or false.")
    raw_pure = raw.get("pure", False)
    if not isinstance(raw_pure, bool):
        raise DelegateError("invalid_pure", "pure must be true or false.")
    raw_timeout = raw.get("timeout")
    if raw_timeout is not None and (
        isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int) or raw_timeout <= 0
    ):
        raise DelegateError("invalid_timeout", "timeout must be a positive integer.")
    if raw_timeout is not None and global_options.pass_through:
        raise DelegateError(
            "invalid_option_combination",
            "timeout is not supported with --pass-through; "
            "pass-through runs have no tracked deadline.",
        )
    json_model_alias: str | None = model_alias if isinstance(model_alias, str) else None
    json_model_override: str | None = None
    if engine == "droid":
        if model_alias is None and _resolve_default_model(config.get("droid", {})) is not None:
            pass  # droid.defaultModel covers the omitted key.
        elif not isinstance(model_alias, str) or not model_alias.strip():
            raise DelegateError(
                "missing_model",
                "droid run input requires a model alias or id (or set droid.defaultModel).",
            )
        else:
            # Alias-or-id, matching --model: a droid.models key keeps alias
            # semantics (modelAlias metadata, placeholder guard); anything else
            # passes through verbatim and the harness validates it.
            droid_models = config.get("droid", {}).get("models")
            if not (isinstance(droid_models, dict) and model_alias in droid_models):
                json_model_alias = None
                json_model_override = model_alias
    elif engine in MODELESS_ENGINES:
        if model_alias is not None and not isinstance(model_alias, str):
            raise DelegateError("invalid_model", f"model must be a string or null for {engine}.")
        if isinstance(model_alias, str) and not model_alias.strip():
            raise DelegateError(
                "invalid_model", f"model must be a non-empty string or omitted for {engine}."
            )
        if engine == "opencode" and isinstance(model_alias, str):
            _reject_opencode_flag_like_value(model_alias, field="model", error="invalid_model")
    if "agent" in raw and engine != "opencode":
        raise DelegateError("unsupported_agent", "agent is only supported for opencode.")
    raw_agent = raw.get("agent")
    if raw_agent is not None and (not isinstance(raw_agent, str) or not raw_agent.strip()):
        raise DelegateError("invalid_agent", "agent must be a non-empty string or omitted.")
    json_agent = raw_agent if isinstance(raw_agent, str) else None
    _validate_agent_option(str(engine), json_agent)
    if engine == "opencode" and json_agent is not None:
        _reject_opencode_flag_like_value(json_agent, field="agent", error="invalid_agent")
    _validate_output_schema_mode(str(engine), str(mode), raw.get("outputSchema"))
    output_schema = resolve_output_schema(str(engine), raw.get("outputSchema"))
    raw_instruction_mode = raw.get("promptInstructionMode")
    raw_workflow_agent_key = raw.get("workflowAgentKey")
    if raw_workflow_agent_key is not None and not isinstance(raw_workflow_agent_key, str):
        raise DelegateError("invalid_workflow_agent_key", "workflowAgentKey must be a string.")

    if mode == MODE_CALL:
        _validate_call_input_json_options(
            global_options,
            raw,
            raw_progress_intent=raw_progress_intent,
            raw_forbid_commit=raw_forbid_commit,
            raw_include_dirty=raw_include_dirty,
        )
        workspace, cleanup_workspace = _call_workspace(False)
        call_prompt = validate_prompt(prompt)
        if raw_instruction_mode == PROMPT_INSTRUCTION_MODE_SLASH and raw_read_only:
            raise DelegateError(
                "slash_passthrough_unsupported",
                "call --read-only wraps the prompt in the read-only contract; "
                "slash-command prompts cannot run verbatim there. Use plain call mode.",
            )
        try:
            return build_request(
                str(engine),
                str(mode),
                json_model_alias,
                workspace,
                _call_effective_prompt(call_prompt, read_only=raw_read_only),
                config,
                dry_run=False,
                stream_capture=True,
                isolation_context=None,
                reasoning_effort=reasoning_effort,
                reasoning_effort_source="input-json" if reasoning_effort is not None else None,
                fast=fast,
                progress=False,
                forbid_commit=False,
                auth_profile_override=global_options.auth_profile,
                output_schema=output_schema,
                cleanup_workspace=cleanup_workspace,
                call_read_only=raw_read_only,
                pure=raw_pure,
                timeout=raw_timeout,
                group=global_options.group,
                workflow_agent_key=raw_workflow_agent_key,
                prompt_instruction_mode=resolve_input_json_prompt_instruction_mode(
                    raw_instruction_mode,
                    call_prompt,
                    engine=str(engine),
                    mode=str(mode),
                ),
                agent=json_agent,
                model_override=json_model_override,
            )
        except BaseException:
            if cleanup_workspace:
                shutil.rmtree(workspace.path, ignore_errors=True)
            raise

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

    # Apply the forbid-commit isolation implication shared with the CLI path so
    # run --input-json with forbidCommit: true and no isolation gets the same
    # implied worktree isolation + note. Explicit "none" + forbidCommit errors
    # here (both paths share this refusal).
    forbid_commit_note: str | None = None
    if raw_forbid_commit:
        json_isolation, forbid_commit_note, _ = _apply_forbid_commit_isolation_implication(
            forbid_commit=raw_forbid_commit,
            mode=str(mode),
            cli_isolation=global_options.isolation,
            json_isolation=json_isolation,
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
    isolation_warnings = list(
        _safe_none_normalization_warnings(
            engine=str(engine),
            mode=str(mode),
            requested=global_options.isolation or json_isolation,
            effective=effective_isolation,
        )
    )
    if forbid_commit_note is not None:
        isolation_warnings.append(forbid_commit_note)

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
        include_dirty=raw_include_dirty,
    )
    _validate_forbid_commit(
        forbid_commit=raw_forbid_commit,
        mode=str(mode),
        isolation_context=isolation_context,
    )
    _validate_include_dirty(
        include_dirty=raw_include_dirty,
        mode=str(mode),
        isolation_context=isolation_context,
    )
    completion_report_mode = resolve_completion_report_mode(parsed, config)
    completion_report_prompt_mode, output_schema_warnings = _completion_report_prompt_mode(
        completion_report_mode,
        output_schema,
    )
    prompt = validate_prompt(prompt)
    instruction_mode = resolve_input_json_prompt_instruction_mode(
        raw_instruction_mode,
        prompt,
        engine=str(engine),
        mode=str(mode),
    )
    prompt = effective_prompt(
        prompt,
        engine=str(engine),
        mode=str(mode),
        completion_report_mode=completion_report_prompt_mode,
        instruction_mode=instruction_mode,
        skip_skill_preamble=global_options.pass_through,
    )
    return build_request(
        str(engine),
        str(mode),
        json_model_alias,
        workspace,
        prompt,
        config,
        dry_run=False,
        stream_capture=not global_options.pass_through,
        isolation_context=isolation_context,
        reasoning_effort=reasoning_effort,
        reasoning_effort_source="input-json" if reasoning_effort is not None else None,
        fast=fast,
        progress=effective_progress,
        progress_initial_delay_sec=progress_initial_delay_sec,
        progress_interval_sec=progress_interval_sec,
        forbid_commit=raw_forbid_commit,
        include_dirty=raw_include_dirty,
        auth_profile_override=global_options.auth_profile,
        output_schema=output_schema,
        warnings=(*output_schema_warnings, *isolation_warnings),
        group=global_options.group,
        workflow_agent_key=raw_workflow_agent_key,
        prompt_instruction_mode=instruction_mode,
        agent=json_agent,
        model_override=json_model_override,
        pure=raw_pure,
        timeout=raw_timeout,
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
    fast: bool | None = None,
    progress: bool = False,
    progress_initial_delay_sec: float = delegate_runner.PROGRESS_INITIAL_DELAY_SEC,
    progress_interval_sec: float = delegate_runner.PROGRESS_HEARTBEAT_INTERVAL_SEC,
    forbid_commit: bool = False,
    include_dirty: bool = False,
    auth_profile_override: str | None = None,
    output_schema: str | None = None,
    warnings: tuple[str, ...] = (),
    cleanup_workspace: bool = False,
    call_read_only: bool = False,
    pure: bool = False,
    timeout: int | None = None,
    group: str | None = None,
    workflow_agent_key: str | None = None,
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED,
    agent: str | None = None,
    model_override: str | None = None,
) -> Request:
    _validate_agent_option(engine, agent)
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
    if fast is not None and engine != "codex":
        raise DelegateError("unsupported_fast", "fast is only supported by codex.")
    _validate_output_schema_mode(engine, mode, output_schema)
    output_schema = resolve_output_schema(engine, output_schema)
    if pure:
        if mode != MODE_CALL:
            raise DelegateError("unsupported_pure_call", "--pure only applies to call mode.")
        _validate_pure_call(engine, pure=True, read_only=call_read_only, group=group)
    if timeout is not None and timeout <= 0:
        raise DelegateError("invalid_timeout", "timeout must be a positive integer.")

    expand_env = profiles.child_environment(pure=True) if pure else os.environ
    profile_resolution = profiles.resolve_active_profile(
        config,
        os.environ,
        cli_override=auth_profile_override,
        expand_env=expand_env,
    )
    discovery = harness_discovery.load_discovery_cache(profile_resolution.name)
    discovery = _runtime_discovery_for_engine(
        config,
        engine,
        discovery,
        profile_resolution,
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
        fast=fast,
        progress=progress,
        progress_initial_delay_sec=progress_initial_delay_sec,
        progress_interval_sec=progress_interval_sec,
        forbid_commit=forbid_commit,
        include_dirty=include_dirty,
        profile_resolution=profile_resolution,
        discovery=discovery,
        output_schema=output_schema,
        warnings=warnings,
        cleanup_workspace=cleanup_workspace,
        call_read_only=call_read_only,
        pure=pure,
        timeout=timeout,
        group=group,
        workflow_agent_key=workflow_agent_key,
        prompt_instruction_mode=prompt_instruction_mode,
        agent=agent,
        model_override=model_override,
    )


def _cursor_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.cache
    cursor = build.config["cursor"]
    pinned = None
    if build.model_override is not None:
        pinned = resolve_model_selection(cursor, build.model_override)
    elif isinstance(build.model_alias, str) and build.model_alias:
        pinned = resolve_model_selection(cursor, build.model_alias)

    warnings: list[str] = []
    capability: reasoning.ReasoningCapability | None = None
    mappings = cursor.get("reasoningEffortModels")
    discovered_routes: dict[str, str] = {}
    if build.requested_effort is not None:
        record = reasoning._discovery_harness_record(build.discovery, "cursor")
        raw_models = record.get("models") if record is not None else None
        discovered_models = raw_models if isinstance(raw_models, dict) else {}
        base_model = pinned or cursor["defaultModel"]
        base_entry = discovered_models.get(base_model)
        route_family = base_entry.get("routeFamily") if isinstance(base_entry, dict) else None
        if isinstance(route_family, str) and route_family:
            for selector, entry in discovered_models.items():
                if not (isinstance(selector, str) and isinstance(entry, dict)):
                    continue
                declaration = entry.get("reasoning")
                if (
                    entry.get("routeFamily") == route_family
                    and isinstance(entry.get("routeEffort"), str)
                    and isinstance(declaration, dict)
                    and declaration.get("evidence") == "inferred-route"
                ):
                    discovered_routes[entry["routeEffort"]] = selector

    # An explicit model remains authoritative unless discovery has routes for
    # that exact selector family. Without a pin, the user's configured routing
    # map remains authoritative over observed routes.
    if build.requested_effort is not None and pinned is not None and not discovered_routes:
        error = DelegateError(
            "unsupported_reasoning_effort",
            reasoning.format_explicit_reasoning_effort_error(
                harness="cursor",
                alias=build.model_alias,
                model=pinned,
                effort=build.requested_effort,
                detail="has no authoritative discovered route family for the pinned model",
            ),
        )
        if build.effort_source != "config":
            raise error
        warnings.append(f"ignoring cursor.defaultReasoningEffort: {error.message}")
        model = pinned
        capability_model_source = "explicit"
    elif (
        build.requested_effort is not None
        and pinned is None
        and isinstance(mappings, dict)
        and mappings
    ):
        capability, reasoning_warnings = _capability_with_config_fallback(
            lambda: resolve_cursor_reasoning_capability(cursor, build.requested_effort),
            engine="cursor",
            effort_source=build.effort_source,
        )
        warnings.extend(reasoning_warnings)
        model = capability.model if capability is not None else cursor["defaultModel"]
        capability_model_source = "config"
    elif build.requested_effort is not None and discovered_routes:
        routed_model = discovered_routes.get(build.requested_effort)
        if routed_model is None:
            error = DelegateError(
                "unsupported_reasoning_effort",
                reasoning.format_explicit_reasoning_effort_error(
                    harness="cursor",
                    alias=build.model_alias,
                    model=base_model,
                    effort=build.requested_effort,
                    supported=sorted(discovered_routes),
                    detail="has authoritative routes that omit the requested effort",
                ),
            )
            if build.effort_source != "config":
                raise error
            warnings.append(f"ignoring cursor.defaultReasoningEffort: {error.message}")
            model = base_model
            capability_model_source = "explicit" if pinned is not None else "config"
        else:
            _reject_opencode_flag_like_value(
                routed_model,
                field="discovered Cursor model",
                error="invalid_model",
            )
            model = routed_model
            capability = reasoning.ReasoningCapability(
                harness="cursor",
                model=model,
                effort=build.requested_effort,
                supported_efforts=tuple(sorted(discovered_routes)),
                default_effort=None,
                transport=reasoning.TRANSPORT_CURSOR_MODEL_SELECTION,
                source="discovery",
                evidence="inferred-route",
            )
            capability_model_source = "discovery"
            if pinned is not None and model != pinned:
                warnings.append(
                    "cursor reasoning-effort routing replaced the explicitly pinned selector "
                    "with another selector from the same discovered model family."
                )
    elif build.requested_effort is not None:
        # Config defaults remain mandatory for Cursor, but an effort needs
        # either an explicit map or authoritative routes; never synthesize a
        # selector from a naming convention.
        error = DelegateError(
            "unsupported_reasoning_effort",
            reasoning.format_explicit_reasoning_effort_error(
                harness="cursor",
                model=base_model,
                effort=build.requested_effort,
                detail="requires configured mappings or authoritative discovered routes",
            ),
        )
        if build.effort_source != "config":
            raise error
        warnings.append(f"ignoring cursor.defaultReasoningEffort: {error.message}")
        model = base_model
        capability_model_source = "config"
    else:
        model = pinned or cursor["defaultModel"]
        capability_model_source = "explicit" if pinned is not None else "config"

    argv = build_cursor_argv(
        cursor["argvPrefix"],
        build.mode,
        build.resolved.path,
        model,
        build.prompt,
        stream_capture=build.stream_capture,
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    reasoning_kwargs = reasoning_request_kwargs(capability, build.effort_source)
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_ARGV,
        display_argv=redacted_prompt_argv(argv),
        warnings=tuple(warnings),
        **_model_context_kwargs(model, capability_model_source),
        **reasoning_kwargs,
    )


def _droid_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    droid = build.config["droid"]
    models = _droid_models_map(build.config)
    _reject_droid_model_conflict(build.model_alias, build.model_override)

    if build.model_override is not None:
        # A droid.models key keeps full alias semantics (modelAlias metadata,
        # placeholder guard context) — parity with the positional and
        # input-JSON alias paths.
        if isinstance(models, dict) and build.model_override in models:
            model = models[build.model_override]
            alias_for_errors = build.model_override
        else:
            model = build.model_override
            alias_for_errors = None
        capability_model = model
        capability_model_source = "explicit"
    elif build.model_alias is not None:
        _validate_droid_model_alias(build.config, build.model_alias)
        model = models[build.model_alias]
        alias_for_errors = build.model_alias
        capability_model = model
        capability_model_source = "explicit"
    else:
        model = _resolve_default_model(droid)
        alias_for_errors = None
        if model is None:
            raise DelegateError(
                "missing_model",
                "droid requires a positional model alias, --model, or droid.defaultModel; "
                "captured help does not prove that --model may be omitted.",
            )
        else:
            capability_model = model
            capability_model_source = "config"

    _guard_droid_placeholder_model(capability_model, alias=alias_for_errors)
    capability, reasoning_warnings = _capability_with_config_fallback(
        lambda: reasoning.resolve_reasoning_capability(
            harness="droid",
            model=capability_model,
            requested_effort=build.requested_effort,
            config=build.config,
            cache=build.cache,
            discovery=build.discovery,
            alias=alias_for_errors,
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
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    display_argv = [
        DROID_PROMPT_FILE_DISPLAY if item == DROID_PROMPT_FILE_ARG_PLACEHOLDER else item
        for item in argv
    ]
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=alias_for_errors if alias_for_errors is not None else build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_FILE,
        prompt_file_text=prompt,
        display_argv=display_argv,
        warnings=reasoning_warnings,
        **_model_context_kwargs(capability_model, capability_model_source),
        **reasoning_request_kwargs(capability, build.effort_source),
    )


def _codex_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    codex = build.config["codex"]
    model, capability_model, capability_model_source = _resolve_model_context(
        codex, build, harness="codex"
    )
    policy = delegate_config.effective_policy(
        build.config, engine="codex", mode=_policy_mode(build)
    )
    if (
        capability_model is None
        and build.requested_effort is not None
        and build.effort_source != "config"
    ):
        effort = reasoning.normalize_effort(build.requested_effort)
        if effort not in CODEX_HARNESS_DEFAULT_REASONING_EFFORTS:
            raise DelegateError(
                "unsupported_reasoning_effort",
                reasoning.format_explicit_reasoning_effort_error(
                    harness="codex",
                    effort=effort,
                    supported=CODEX_HARNESS_DEFAULT_REASONING_EFFORTS,
                    detail="does not support reasoning effort with the harness default model",
                ),
            )
        capability = reasoning.ReasoningCapability(
            harness="codex",
            model="",
            effort=effort,
            supported_efforts=CODEX_HARNESS_DEFAULT_REASONING_EFFORTS,
            default_effort=None,
            transport=reasoning.TRANSPORT_BY_HARNESS["codex"],
            source="harness-default",
            evidence="harness",
        )
        reasoning_warnings = (
            "codex.defaultModel is unset; applying --reasoning-effort to the Codex "
            "harness default model.",
        )
    else:
        capability, reasoning_warnings = _capability_with_config_fallback(
            lambda: reasoning.resolve_reasoning_capability(
                harness="codex",
                model=capability_model,
                requested_effort=build.requested_effort,
                config=build.config,
                cache=build.cache,
                discovery=build.discovery,
                alias=build.model_alias or capability_model,
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
        fast=build.fast,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
        output_schema=build.output_schema,
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
        stdin_text=build.prompt,
        display_argv=list(argv),
        warnings=reasoning_warnings,
        **_model_context_kwargs(capability_model, capability_model_source),
        **reasoning_request_kwargs(capability, build.effort_source),
    )


def _claude_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    claude = build.config["claude"]
    model, capability_model, capability_model_source = _resolve_model_context(
        claude, build, harness="claude"
    )
    policy = delegate_config.effective_policy(
        build.config, engine="claude", mode=_policy_mode(build)
    )
    capability, warnings = _capability_with_config_fallback(
        lambda: reasoning.resolve_harness_enum_capability(
            harness="claude",
            model=capability_model,
            requested_effort=build.requested_effort,
            discovery=build.discovery,
            alias=build.model_alias,
        ),
        engine="claude",
        effort_source=build.effort_source,
    )
    effort = capability.effort if capability is not None else None
    schema_contents = None
    if build.output_schema is not None:
        try:
            schema_contents = Path(build.output_schema).read_text(encoding="utf-8")
        except OSError as exc:
            raise DelegateError(
                "invalid_output_schema", f"Output schema is not readable: {build.output_schema}"
            ) from exc
    argv = build_claude_argv(
        claude,
        build.mode,
        model,
        policy,
        stream_capture=build.stream_capture,
        reasoning_effort=effort,
        allow_bypass_permissions=_claude_harness_bypass_enabled(build.config, build.mode),
        call_read_only=build.call_read_only,
        pure=build.pure,
        output_schema=schema_contents,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
        stdin_text=build.prompt,
        display_argv=list(argv),
        warnings=warnings,
        **_model_context_kwargs(capability_model, capability_model_source),
        **reasoning_request_kwargs(capability, build.effort_source),
    )


def _grok_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    grok = build.config["grok"]
    model, capability_model, capability_model_source = _resolve_model_context(
        grok, build, harness="grok"
    )
    policy = delegate_config.effective_policy(build.config, engine="grok", mode=_policy_mode(build))
    compatibility_warnings: list[str] = []

    def resolve_capability() -> reasoning.ReasoningCapability | None:
        capability, warnings = reasoning.resolve_grok_reasoning_capability(
            model=capability_model,
            requested_effort=build.requested_effort,
            config=build.config,
            discovery=build.discovery,
            cache=build.cache,
            alias=build.model_alias,
        )
        compatibility_warnings.extend(warnings)
        return capability

    capability, fallback_warnings = _capability_with_config_fallback(
        resolve_capability,
        engine="grok",
        effort_source=build.effort_source,
    )
    warnings = (*compatibility_warnings, *fallback_warnings)
    effort = capability.effort if capability is not None else None
    argv = build_grok_argv(
        grok,
        build.mode,
        build.resolved.path,
        model,
        policy,
        stream_capture=build.stream_capture,
        reasoning_effort=effort,
        allow_bypass_permissions=_grok_harness_bypass_enabled(build.config, build.mode),
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    display_argv = [
        PROMPT_FILE_DISPLAY if item == PROMPT_FILE_ARG_PLACEHOLDER else item for item in argv
    ]
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_FILE,
        prompt_file_text=build.prompt,
        display_argv=display_argv,
        warnings=warnings,
        **_model_context_kwargs(capability_model, capability_model_source),
        **reasoning_request_kwargs(capability, build.effort_source),
    )


def _devin_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.effort_source, build.cache
    devin = build.config["devin"]
    model, capability_model, capability_model_source = _resolve_model_context(
        devin, build, harness="devin"
    )
    if build.requested_effort is not None:
        raise DelegateError(
            "unsupported_reasoning_effort",
            reasoning.format_explicit_reasoning_effort_error(
                harness="devin",
                alias=build.model_alias,
                model=capability_model,
                effort=build.requested_effort,
                detail="reasoning effort is not supported",
            ),
        )
    argv = build_devin_argv(
        devin,
        build.mode,
        model,
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    display_argv = [
        DEVIN_AGENT_CONFIG_DISPLAY
        if item == DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER
        else PROMPT_FILE_DISPLAY
        if item == PROMPT_FILE_ARG_PLACEHOLDER
        else item
        for item in argv
    ]
    read_only = build.mode == MODE_SAFE or (build.mode == MODE_CALL and build.call_read_only)
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_FILE,
        prompt_file_text=build.prompt,
        agent_config_text=DEVIN_READ_ONLY_AGENT_CONFIG_TEXT if read_only else None,
        display_argv=display_argv,
        **_model_context_kwargs(capability_model, capability_model_source),
    )


def _opencode_env_overrides(
    *, read_only: bool, pure: bool = False, agent: str | None = None
) -> dict[str, str]:
    env = {"OPENCODE_DISABLE_AUTOUPDATE": "1"}
    if read_only:
        if agent is None:
            raise ValueError("read-only OpenCode requests require an agent")
        permission = OPENCODE_PURE_PERMISSION if pure else OPENCODE_SAFE_PERMISSION
        agent_config: JsonObject = {"permission": permission}
        if agent == OPENCODE_SAFE_AGENT:
            agent_config["mode"] = "primary"
        lockdown: JsonObject = {
            "permission": permission,
            "agent": {agent: agent_config},
            "autoupdate": False,
            "share": "disabled",
        }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(lockdown, separators=(",", ":"))
        env["OPENCODE_PERMISSION"] = (
            OPENCODE_PURE_PERMISSION_JSON if pure else OPENCODE_SAFE_PERMISSION_JSON
        )
    return env


def _opencode_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.cache
    opencode = build.config["opencode"]
    model, variant, variant_source = _resolve_opencode_selection_detail(opencode, build)
    if model is not None:
        capability_model = model
        capability_model_source = (
            "explicit"
            if build.model_override is not None or build.model_alias is not None
            else "config"
        )
    else:
        capability_model = reasoning.discovery_default_model(build.discovery, "opencode")
        capability_model_source = "discovery" if capability_model is not None else None
    capability_warnings: list[str] = []

    def resolve_capability() -> reasoning.ReasoningCapability | None:
        capability, warnings = reasoning.resolve_discovered_model_capability(
            harness="opencode",
            model=capability_model,
            requested_effort=variant,
            discovery=build.discovery,
            alias=build.model_alias,
        )
        capability_warnings.extend(warnings)
        return capability

    if variant is not None and variant_source == "alias":
        capability = reasoning.ReasoningCapability(
            harness="opencode",
            model=capability_model or "",
            effort=variant,
            supported_efforts=(variant,),
            default_effort=None,
            transport=reasoning.TRANSPORT_OPENCODE_VARIANT_FLAG,
            source="alias",
            evidence="exact",
        )
        fallback_warnings: tuple[str, ...] = ()
    else:
        capability, fallback_warnings = _capability_with_config_fallback(
            resolve_capability,
            engine="opencode",
            effort_source=variant_source,
        )
    resolved_variant = capability.effort if capability is not None else None
    agent = _resolve_opencode_agent(opencode, build)
    read_only = build.mode == MODE_SAFE or (
        build.mode == MODE_CALL and (build.call_read_only or build.pure)
    )
    if read_only and agent is None:
        agent = OPENCODE_SAFE_AGENT
    argv = build_opencode_argv(
        opencode,
        build.mode,
        build.resolved.path,
        model,
        agent,
        resolved_variant,
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
        stdin_text=build.prompt,
        display_argv=list(argv),
        warnings=(*capability_warnings, *fallback_warnings),
        env_overrides=_opencode_env_overrides(read_only=read_only, pure=build.pure, agent=agent),
        **_model_context_kwargs(capability_model, capability_model_source),
        **reasoning_request_kwargs(capability, variant_source),
    )


def _pi_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.cache
    pi = build.config["pi"]
    model, thinking, thinking_source = _resolve_pi_family_selection_detail(
        pi,
        build,
        engine="pi",
    )
    if model is not None:
        capability_model = model
        capability_model_source = (
            "explicit"
            if build.model_override is not None or build.model_alias is not None
            else "config"
        )
    else:
        capability_model = reasoning.discovery_default_model(build.discovery, "pi")
        capability_model_source = "discovery" if capability_model is not None else None
    capability_warnings: list[str] = []

    def resolve_capability() -> reasoning.ReasoningCapability | None:
        capability, warnings = reasoning.resolve_discovered_model_capability(
            harness="pi",
            model=capability_model,
            requested_effort=thinking,
            discovery=build.discovery,
            alias=build.model_alias,
        )
        capability_warnings.extend(warnings)
        return capability

    if thinking is not None and thinking_source == "alias":
        capability = reasoning.ReasoningCapability(
            harness="pi",
            model=capability_model or "",
            effort=thinking,
            supported_efforts=reasoning.PI_THINKING_LEVELS,
            default_effort=None,
            transport=reasoning.TRANSPORT_PI_THINKING_FLAG,
            source="alias",
            evidence="exact",
        )
        fallback_warnings: tuple[str, ...] = ()
    else:
        capability, fallback_warnings = _capability_with_config_fallback(
            resolve_capability,
            engine="pi",
            effort_source=thinking_source,
        )
    resolved_thinking = capability.effort if capability is not None else None
    argv = build_pi_argv(
        pi,
        build.mode,
        model,
        resolved_thinking,
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_STDIN,
        stdin_text=build.prompt,
        display_argv=list(argv),
        warnings=(*capability_warnings, *fallback_warnings),
        **_model_context_kwargs(capability_model, capability_model_source),
        **reasoning_request_kwargs(capability, thinking_source),
    )


def _omp_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.cache
    omp = build.config["omp"]
    model, thinking, thinking_source = _resolve_pi_family_selection_detail(
        omp,
        build,
        engine="omp",
    )
    if model is not None:
        capability_model = model
        capability_model_source = (
            "explicit"
            if build.model_override is not None or build.model_alias is not None
            else "config"
        )
    else:
        capability_model = reasoning.discovery_default_model(build.discovery, "omp")
        capability_model_source = "discovery" if capability_model is not None else None
    capability_warnings: list[str] = []

    def resolve_capability() -> reasoning.ReasoningCapability | None:
        capability, warnings = reasoning.resolve_discovered_model_capability(
            harness="omp",
            model=capability_model,
            requested_effort=thinking,
            discovery=build.discovery,
            alias=build.model_alias,
        )
        capability_warnings.extend(warnings)
        return capability

    if thinking is not None and thinking_source == "alias":
        capability = reasoning.ReasoningCapability(
            harness="omp",
            model=capability_model or "",
            effort=thinking,
            supported_efforts=reasoning.PI_THINKING_LEVELS,
            default_effort=None,
            transport=reasoning.TRANSPORT_PI_THINKING_FLAG,
            source="alias",
            evidence="exact",
        )
        fallback_warnings: tuple[str, ...] = ()
    else:
        capability, fallback_warnings = _capability_with_config_fallback(
            resolve_capability,
            engine="omp",
            effort_source=thinking_source,
        )
    resolved_thinking = capability.effort if capability is not None else None
    argv = build_omp_argv(
        omp,
        build.mode,
        model,
        resolved_thinking,
        build.prompt,
        call_read_only=build.call_read_only,
        pure=build.pure,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_ARGV,
        display_argv=redacted_prompt_argv(argv, replacement=OMP_PROMPT_REDACTION),
        warnings=(*capability_warnings, *fallback_warnings),
        **_model_context_kwargs(capability_model, capability_model_source),
        **reasoning_request_kwargs(capability, thinking_source),
    )


def _kimi_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.effort_source, build.cache
    kimi = build.config["kimi"]
    model, capability_model, capability_model_source = _resolve_model_context(
        kimi, build, harness="kimi"
    )
    if build.requested_effort is not None:
        raise DelegateError(
            "unsupported_reasoning_effort",
            reasoning.format_explicit_reasoning_effort_error(
                harness="kimi",
                alias=build.model_alias,
                model=capability_model,
                effort=build.requested_effort,
                detail="reasoning effort is not supported",
            ),
        )
    argv = build_kimi_argv(
        kimi,
        build.mode,
        build.resolved.path,
        model,
        build.prompt,
        stream_capture=build.stream_capture,
        pure=build.pure,
    )
    return EngineRequestParts(
        model=model,
        argv=argv,
        model_alias=build.model_alias,
        prompt_transport=PROMPT_TRANSPORT_ARGV,
        display_argv=redacted_prompt_argv(argv, replacement=KIMI_PROMPT_REDACTION),
        **_model_context_kwargs(capability_model, capability_model_source),
    )


EngineRequestPartsBuilder = Callable[[EngineBuildInput], EngineRequestParts]

ENGINE_REQUEST_PARTS_BUILDERS: dict[str, EngineRequestPartsBuilder] = {
    "cursor": _cursor_request_parts,
    "droid": _droid_request_parts,
    "codex": _codex_request_parts,
    "claude": _claude_request_parts,
    "grok": _grok_request_parts,
    "devin": _devin_request_parts,
    "opencode": _opencode_request_parts,
    "pi": _pi_request_parts,
    "omp": _omp_request_parts,
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
    fast: bool | None,
    progress: bool,
    progress_initial_delay_sec: float,
    progress_interval_sec: float,
    forbid_commit: bool,
    include_dirty: bool,
    profile_resolution: profiles.ProfileResolution,
    discovery: JsonObject | None,
    output_schema: str | None,
    warnings: tuple[str, ...],
    cleanup_workspace: bool,
    call_read_only: bool = False,
    pure: bool = False,
    timeout: int | None = None,
    group: str | None = None,
    workflow_agent_key: str | None = None,
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED,
    agent: str | None = None,
    model_override: str | None = None,
) -> Request:
    if prompt_instruction_mode != PROMPT_INSTRUCTION_MODE_SLASH:
        prompt = _append_safe_dirty_tree_note(prompt, resolved, mode, isolation_context)
    workspace_warning = wsl.drivefs_workspace_warning(resolved.path)
    if workspace_warning is not None:
        warnings = (*warnings, workspace_warning)
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
            discovery=discovery,
            fast=fast,
            output_schema=output_schema,
            call_read_only=call_read_only,
            pure=pure,
            model_override=model_override,
            agent=agent,
        ),
    )
    return _apply_profile_resolution(
        Request(
            engine,
            mode,
            resolved.path,
            prompt,
            parts.argv,
            parts.model,
            model_alias=parts.model_alias,
            model_requested=model_override or model_alias,
            capability_model=parts.capability_model,
            capability_model_source=parts.capability_model_source,
            output_schema=output_schema,
            pure=pure,
            timeout=timeout,
            dry_run=dry_run,
            workspace_kind=resolved.kind,
            isolation_context=isolation_context,
            reasoning_effort=parts.reasoning_effort,
            requested_reasoning_effort=(
                parts.requested_reasoning_effort
                if parts.requested_reasoning_effort is not None
                else requested_effort
                if effort_source != "config"
                else None
            ),
            reasoning_effort_source=parts.reasoning_effort_source,
            reasoning_capability_source=parts.reasoning_capability_source,
            reasoning_capability_evidence=parts.reasoning_capability_evidence,
            reasoning_transport=parts.reasoning_transport,
            fast=fast,
            progress=progress,
            progress_initial_delay_sec=progress_initial_delay_sec,
            progress_interval_sec=progress_interval_sec,
            forbid_commit=forbid_commit,
            include_dirty=include_dirty,
            call_read_only=call_read_only,
            warnings=(*warnings, *parts.warnings),
            stdin_text=parts.stdin_text,
            prompt_file_text=parts.prompt_file_text,
            agent_config_text=parts.agent_config_text,
            prompt_transport=parts.prompt_transport,
            display_argv=parts.display_argv,
            env_overrides=parts.env_overrides,
            cleanup_workspace=cleanup_workspace,
            group=group,
            workflow_agent_key=workflow_agent_key,
            prompt_instruction_mode=prompt_instruction_mode,
        ),
        config,
        resolution=profile_resolution,
    )


def _dedupe_warnings(warnings: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for warning in warnings:
        if warning in seen:
            continue
        seen.add(warning)
        deduped.append(warning)
    return tuple(deduped)


def _apply_profile_resolution(
    request: Request,
    config: JsonObject,
    *,
    resolution: profiles.ProfileResolution,
) -> Request:
    env_overrides = dict(request.env_overrides or {})
    env_overrides.update(resolution.env)
    if request.engine == "opencode":
        # Profiles and ambient environment must not relax a read-only request.
        request_env = request.env_overrides or {}
        env_overrides["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        if "OPENCODE_PERMISSION" in request_env:
            env_overrides.update(
                {
                    key: request_env[key]
                    for key in (
                        "OPENCODE_CONFIG_CONTENT",
                        "OPENCODE_PERMISSION",
                        "OPENCODE_DISABLE_AUTOUPDATE",
                    )
                }
            )
    auth_profile = resolution.name
    fallback_profile = None
    if request.engine == "codex":
        if resolution.name is not None and resolution.codex_home is None:
            raise DelegateError(
                "profile_missing_codex_home",
                f"Profile {resolution.name!r} is active for a Codex Run but does not define CODEX_HOME.",
            )
        if resolution.name is not None:
            fallback_profile = profiles.codex_fallback_profile(config)
    return replace(
        request,
        warnings=_dedupe_warnings((*request.warnings, *resolution.warnings)),
        env_overrides=env_overrides or None,
        auth_profile=auth_profile,
        fallback_auth_profile=fallback_profile,
        profile_resolution=resolution,
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
            except reasoning.ReasoningCapabilityError:
                # Capability resolution owns config-default degradation. Carry
                # malformed defaults far enough to produce the same warning +
                # omission behavior as a well-formed but unsupported default.
                return default if isinstance(default, str) else None, "config"
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
        evidence="exact",
    )


def reasoning_request_kwargs(
    capability: reasoning.ReasoningCapability | None,
    source: str | None,
) -> JsonObject:
    if capability is None:
        return {}
    return {
        "reasoning_effort": capability.effort,
        "requested_reasoning_effort": capability.effort,
        "reasoning_effort_source": source or "cli",
        "reasoning_capability_source": capability.source,
        "reasoning_capability_evidence": capability.evidence,
        "reasoning_transport": capability.transport,
    }


def _resolve_default_model(section: JsonObject) -> str | None:
    default_model = section.get("defaultModel")
    # Whitespace-only counts as unset — it would otherwise reach child argv.
    return default_model if isinstance(default_model, str) and default_model.strip() else None
