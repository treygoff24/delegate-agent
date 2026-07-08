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
from delegate_agent import profiles, reasoning, safe_workspace, wsl
from delegate_agent import runner as delegate_runner
from delegate_agent.argv_builders import (
    SAFE_REVIEW_PREFIX_BY_ENGINE,
    _claude_harness_bypass_enabled,
    _grok_harness_bypass_enabled,
    build_claude_argv,
    build_codex_argv,
    build_cursor_argv,
    build_droid_argv,
    build_grok_argv,
    build_kimi_argv,
    prefix_droid_safe_prompt,
    redacted_prompt_argv,
)
from delegate_agent.constants import (
    ENGINES_PROSE,
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    MODE_WORK,
    MODELESS_NONCURSOR_ENGINES,
    PROMPT_ENFORCED_SAFE_ENGINES,
    PROMPT_INSTRUCTION_MODE_SLASH,
    PROMPT_INSTRUCTION_MODE_WRAPPED,
    SAFE_REVIEW_PREFIX_INJECTED_HERE_ENGINES,
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
    PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_FILE_DISPLAY,
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
    "outputSchema",
    "progress",
    "forbidCommit",
    "readOnly",
    "includeDirty",
    "promptInstructionMode",
    "workflowAgentKey",
}

OUTPUT_SCHEMA_COMPLETION_REPORT_WARNING = (
    "--output-schema enforces a JSON-only final message; completion-report instruction "
    "suppressed for this run."
)
CALL_TEMP_CWD_PLACEHOLDER = "<delegate-call-temp-cwd>"
CODEX_HARNESS_DEFAULT_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")

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
        _reject_windows_path(prompt_file_path, "--prompt-file")
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
    if engine != "codex":
        raise DelegateError(
            "unsupported_output_schema",
            "--output-schema/outputSchema is only supported by the codex engine; "
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


def _validate_droid_model_alias(config: JsonObject, model_alias: str | None) -> None:
    models = config["droid"]["models"]
    if model_alias is None or model_alias not in models:
        raise DelegateError("invalid_alias", f"Unknown Droid model alias: {model_alias}")


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
    if cwd is not None:
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


def _validate_call_input_json_options(
    global_options: object,
    raw: JsonObject,
    *,
    raw_progress_intent: ProgressIntent,
    raw_forbid_commit: bool,
    raw_include_dirty: bool,
) -> None:
    if getattr(global_options, "cwd", None) is not None:
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
    if launch.mode == MODE_CALL:
        _validate_call_cli_options(global_options, launch)
        read_only = getattr(launch, "read_only", False)
        if launch.engine == "droid":
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
        try:
            return build_request(
                launch.engine,
                launch.mode,
                launch.model_alias,
                workspace,
                prompt,
                config,
                launch.dry_run,
                stream_capture=True,
                isolation_context=None,
                reasoning_effort=launch.reasoning_effort,
                reasoning_effort_source="cli" if launch.reasoning_effort is not None else None,
                progress=False,
                forbid_commit=False,
                auth_profile_override=global_options.auth_profile,
                output_schema=output_schema,
                cleanup_workspace=cleanup_workspace,
                call_read_only=read_only,
                group=global_options.group,
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
    effective_progress = resolve_effective_progress(launch.progress_intent, config)
    if effective_progress and global_options.pass_through:
        raise DelegateError(
            "invalid_option_combination",
            "--progress is incompatible with --pass-through.",
        )
    progress_initial_delay_sec, progress_interval_sec = resolve_progress_timing(config)
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
        model_alias=launch.model_alias,
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
        include_dirty=launch.include_dirty,
        auth_profile_override=global_options.auth_profile,
        output_schema=output_schema,
        warnings=(*output_schema_warnings, *isolation_warnings),
        group=global_options.group,
        prompt_instruction_mode=instruction_mode,
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
    if engine == "droid":
        if not isinstance(model_alias, str) or not model_alias:
            raise DelegateError("missing_model", "droid run input requires model alias.")
    elif engine in MODELESS_NONCURSOR_ENGINES:
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
        if engine == "droid":
            _validate_droid_model_alias(config, model_alias)
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
                model_alias,
                workspace,
                _call_effective_prompt(call_prompt, read_only=raw_read_only),
                config,
                dry_run=False,
                stream_capture=True,
                isolation_context=None,
                reasoning_effort=reasoning_effort,
                reasoning_effort_source="input-json" if reasoning_effort is not None else None,
                progress=False,
                forbid_commit=False,
                auth_profile_override=global_options.auth_profile,
                output_schema=output_schema,
                cleanup_workspace=cleanup_workspace,
                call_read_only=raw_read_only,
                group=global_options.group,
                workflow_agent_key=raw_workflow_agent_key,
                prompt_instruction_mode=resolve_input_json_prompt_instruction_mode(
                    raw_instruction_mode,
                    call_prompt,
                    engine=str(engine),
                    mode=str(mode),
                ),
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
    if engine == "droid":
        _validate_droid_model_alias(config, model_alias)

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
        include_dirty=raw_include_dirty,
        auth_profile_override=global_options.auth_profile,
        output_schema=output_schema,
        warnings=(*output_schema_warnings, *isolation_warnings),
        group=global_options.group,
        workflow_agent_key=raw_workflow_agent_key,
        prompt_instruction_mode=instruction_mode,
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
    include_dirty: bool = False,
    auth_profile_override: str | None = None,
    output_schema: str | None = None,
    warnings: tuple[str, ...] = (),
    cleanup_workspace: bool = False,
    call_read_only: bool = False,
    group: str | None = None,
    workflow_agent_key: str | None = None,
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED,
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
    output_schema = resolve_output_schema(engine, output_schema)

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
        include_dirty=include_dirty,
        auth_profile_override=auth_profile_override,
        output_schema=output_schema,
        warnings=warnings,
        cleanup_workspace=cleanup_workspace,
        call_read_only=call_read_only,
        group=group,
        workflow_agent_key=workflow_agent_key,
        prompt_instruction_mode=prompt_instruction_mode,
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
        call_read_only=build.call_read_only,
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
    _validate_droid_model_alias(build.config, build.model_alias)
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
        call_read_only=build.call_read_only,
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
    policy = delegate_config.effective_policy(
        build.config, engine="codex", mode=_policy_mode(build)
    )
    if model is None and build.requested_effort is not None and build.effort_source != "config":
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
        )
        reasoning_warnings = (
            "codex.defaultModel is unset; applying --reasoning-effort to the Codex "
            "harness default model.",
        )
    else:
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
        output_schema=build.output_schema,
        call_read_only=build.call_read_only,
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
    policy = delegate_config.effective_policy(
        build.config, engine="claude", mode=_policy_mode(build)
    )
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
        call_read_only=build.call_read_only,
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


def _grok_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    grok = build.config["grok"]
    if isinstance(build.model_alias, str) and build.model_alias:
        model = build.model_alias
    else:
        model = _resolve_default_model(grok)
    policy = delegate_config.effective_policy(build.config, engine="grok", mode=_policy_mode(build))
    effort: str | None = None
    warnings: tuple[str, ...] = ()
    if build.requested_effort is not None:
        try:
            effort = reasoning.resolve_grok_native_effort(
                build.requested_effort,
                alias=build.model_alias,
                model=model,
            )
        except reasoning.ReasoningCapabilityError as exc:
            if build.effort_source != "config":
                raise DelegateError(exc.error, exc.message) from exc
            warnings = (f"ignoring grok.defaultReasoningEffort: {exc.message}",)
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
        **grok_reasoning_request_kwargs(effort, build.effort_source),
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
    "grok": _grok_request_parts,
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
    include_dirty: bool,
    auth_profile_override: str | None,
    output_schema: str | None,
    warnings: tuple[str, ...],
    cleanup_workspace: bool,
    call_read_only: bool = False,
    group: str | None = None,
    workflow_agent_key: str | None = None,
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED,
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
            output_schema=output_schema,
            call_read_only=call_read_only,
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
            output_schema=output_schema,
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
            include_dirty=include_dirty,
            warnings=(*warnings, *parts.warnings),
            stdin_text=parts.stdin_text,
            prompt_file_text=parts.prompt_file_text,
            prompt_transport=parts.prompt_transport,
            display_argv=parts.display_argv,
            cleanup_workspace=cleanup_workspace,
            group=group,
            workflow_agent_key=workflow_agent_key,
            prompt_instruction_mode=prompt_instruction_mode,
        ),
        config,
        auth_profile_override=auth_profile_override,
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
    request: Request, config: JsonObject, *, auth_profile_override: str | None = None
) -> Request:
    resolution = profiles.resolve_active_profile(
        config, os.environ, cli_override=auth_profile_override
    )
    env_overrides = dict(request.env_overrides or {})
    env_overrides.update(resolution.env)
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


def _native_reasoning_request_kwargs(
    effort: str | None,
    source: str | None,
    *,
    transport: str,
    capability_source: str,
) -> JsonObject:
    if effort is None:
        return {}
    return {
        "reasoning_effort": effort,
        "reasoning_effort_source": source,
        "reasoning_capability_source": capability_source,
        "reasoning_transport": transport,
    }


def claude_reasoning_request_kwargs(effort: str | None, source: str | None) -> JsonObject:
    """Reasoning payload fields for Claude's static native-effort flag.

    The sibling of ``reasoning_request_kwargs`` for the engine that resolves a
    flag (``--effort``) rather than a ``ReasoningCapability`` object. Returns
    ``{}`` when no effort applies so the request-parts defaults stand.
    """
    return _native_reasoning_request_kwargs(
        effort,
        source,
        transport=reasoning.TRANSPORT_CLAUDE_EFFORT_FLAG,
        capability_source="static",
    )


def grok_reasoning_request_kwargs(effort: str | None, source: str | None) -> JsonObject:
    return _native_reasoning_request_kwargs(
        effort,
        source,
        transport=reasoning.TRANSPORT_GROK_EFFORT_FLAG,
        capability_source="static",
    )


def _resolve_default_model(section: JsonObject) -> str | None:
    default_model = section.get("defaultModel")
    return default_model if isinstance(default_model, str) and default_model else None
