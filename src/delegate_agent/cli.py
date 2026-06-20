#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import select
import shlex
import shutil
import subprocess  # nosec B404 - Delegate intentionally launches configured harness commands with shell=False.
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, TextIO

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
from delegate_agent.constants import MODE_SAFE, MODE_WORK, validate_mode
from delegate_agent.errors import (
    EXIT_MISSING_BINARY,
    EXIT_OK,
    EXIT_USAGE,
    DelegateError,
)
from delegate_agent.git_utils import (
    GIT_QUICK_TIMEOUT_SECONDS,
    capture_git_metadata,
)
from delegate_agent.git_utils import (
    run_git as _run_git,
)
from delegate_agent.isolation import (
    IsolationContext,
    build_isolation_context,
)
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.prompt_transport import (  # noqa: F401  # CURSOR_PROMPT_REDACTION re-exported for tests
    CURSOR_PROMPT_REDACTION,
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

RUN_INPUT_KEYS = {"engine", "mode", "model", "cwd", "prompt", "isolation", "reasoningEffort"}
MISPLACED_GLOBAL_OPTIONS = frozenset(
    {
        "--json",
        "--cwd",
        "--isolation",
        "--pass-through",
        "--completion-report",
        "--no-completion-report",
    }
)
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


def workspace_path_for_config(global_cwd: str | None) -> Path | None:
    try:
        return Path(resolve_workspace(global_cwd).path)
    except DelegateError:
        return None


def infer_global_json(argv: list[str]) -> bool:
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--json":
            return True
        if token == "--cwd":
            i += 2
            continue
        if token in ("--help", "-h", "--version"):
            return False
        break
    return False


def canonical_help_topic(path: str | None) -> str | None:
    """Resolve a detected command path to its nearest valid COMMAND_SPECS key.

    Returns the path unchanged when it is empty/None (overview) or an exact spec
    key. Otherwise it falls back to the nearest valid prefix (e.g. an unknown
    worktree action maps to ``worktree``); a leftover with no spec is returned
    as-is so emit_command_help can surface unknown_help_topic.
    """

    if not path:
        return path
    if path in command_help.COMMAND_SPECS:
        return path
    parts = path.split()
    while len(parts) > 1:
        parts.pop()
        candidate = " ".join(parts)
        if candidate in command_help.COMMAND_SPECS:
            return candidate
    return path


def help_command(json_mode: bool, topic: str | None) -> ParsedCommand:
    """Build a help ParsedCommand, mapping topic to a canonical spec key."""

    return ParsedCommand(
        "help",
        global_options=GlobalOptions(json_mode=json_mode),
        help_topic=canonical_help_topic(topic),
    )


def parse_help_subcommand(rest: list[str], json_mode: bool) -> ParsedCommand:
    """Parse ``help [<command> [<subcommand>]]`` (D3).

    ``--json`` and help tokens are honored from anywhere in ``rest``; remaining
    tokens name the topic. An empty topic yields the overview, unless a help
    token was present, in which case ``delegate help --help`` describes the help
    command itself.
    """

    help_requested = False
    topic_parts: list[str] = []
    for token in rest:
        if token == "--json":
            json_mode = True
            continue
        if command_help.is_help_token(token):
            help_requested = True
            continue
        topic_parts.append(token)

    if topic_parts:
        topic = " ".join(topic_parts)
    elif help_requested:
        topic = "help"
    else:
        topic = None
    return help_command(json_mode, topic)


SIMPLE_INSPECTION_SUBCOMMANDS = frozenset({"models", "describe", "agent-help"})


def parse_simple_inspection_subcommand(
    name: str,
    rest: list[str],
    *,
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, name)
    require_no_extra(rest, name)
    return ParsedCommand(
        name,
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        ),
    )


def parse_capabilities_subcommand(
    rest: list[str],
    *,
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "capabilities")
    refresh = False
    if rest == ["refresh"]:
        refresh = True
    elif rest:
        require_no_extra(rest, "capabilities")
    return ParsedCommand(
        "capabilities",
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        ),
        capabilities=capability_commands.CapabilitiesCommand(
            refresh=refresh,
            json_mode=json_mode,
        ),
    )


def parse_cli(argv: list[str]) -> ParsedCommand:
    if not argv or argv[0] in ("--help", "-h"):
        return ParsedCommand("help")
    if argv[0] == "--version":
        return ParsedCommand("version")

    json_mode = False
    cwd: str | None = None
    pass_through = False
    completion_report: str | None = None
    isolation: str | None = None
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--json":
            json_mode = True
            i += 1
            continue
        if token == "--cwd":
            if i + 1 >= len(argv):
                raise DelegateError("missing_cwd", "--cwd requires a path.")
            cwd = argv[i + 1]
            i += 2
            continue
        if token == "--pass-through":
            pass_through = True
            i += 1
            continue
        if token == "--no-completion-report":
            completion_report = delegate_config.COMPLETION_REPORT_MODE_NONE
            i += 1
            continue
        if token == "--completion-report":
            if i + 1 >= len(argv):
                raise DelegateError(
                    "missing_completion_report", "--completion-report requires markdown or none."
                )
            completion_report = argv[i + 1]
            if completion_report not in delegate_config.COMPLETION_REPORT_MODES:
                raise DelegateError(
                    "invalid_completion_report",
                    "--completion-report must be markdown or none.",
                )
            i += 2
            continue
        if token == "--isolation":
            if i + 1 >= len(argv):
                raise DelegateError("missing_isolation_value", "--isolation requires a value.")
            isolation = argv[i + 1]
            if isolation not in delegate_config.VALID_ISOLATION_VALUES:
                raise DelegateError(
                    "invalid_isolation",
                    "--isolation must be auto, none, or worktree.",
                )
            i += 2
            continue
        break

    if json_mode and pass_through:
        raise DelegateError(
            "invalid_option_combination",
            "--pass-through is incompatible with --json.",
        )

    if i >= len(argv):
        raise DelegateError("missing_subcommand", "Missing subcommand.")

    subcommand = argv[i]
    rest = argv[i + 1 :]
    if subcommand.startswith("-"):
        raise DelegateError(
            "unknown_option", f"Unknown global option before subcommand: {subcommand}"
        )

    if subcommand == "help":
        return parse_help_subcommand(rest, json_mode)

    if subcommand in SIMPLE_INSPECTION_SUBCOMMANDS:
        return parse_simple_inspection_subcommand(
            subcommand,
            rest,
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        )
    if subcommand == "capabilities":
        return parse_capabilities_subcommand(
            rest,
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        )
    if subcommand == "run":
        return parse_run(rest, json_mode, cwd, pass_through, completion_report, isolation)
    if subcommand in ("cursor", "codex", "kimi", "claude"):
        return parse_modeless_engine(
            subcommand,
            rest,
            json_mode,
            cwd,
            dry_run=False,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        )
    if subcommand == "droid":
        return parse_droid(
            rest,
            json_mode,
            cwd,
            dry_run=False,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        )
    if subcommand == "dry-run":
        return parse_dry_run(rest, json_mode, cwd, pass_through, completion_report, isolation)
    if subcommand == "snapshot":
        return parse_snapshot(rest, json_mode, cwd)
    if subcommand == "runs":
        return parse_runs(rest, json_mode, cwd)
    if subcommand == "run-output":
        return parse_run_output(rest, json_mode, cwd)
    if subcommand == "worktree":
        if isolation is not None:
            raise DelegateError(
                "invalid_option_combination",
                "--isolation is not supported with delegate worktree commands.",
            )
        return parse_worktree(rest, json_mode, cwd)

    raise DelegateError("unknown_subcommand", f"Unknown subcommand: {subcommand}")


def has_misplaced_global_option(tokens: list[str]) -> bool:
    return any(token in MISPLACED_GLOBAL_OPTIONS for token in tokens)


def raise_misplaced_global_option(message: str) -> NoReturn:
    guidance = (
        "Move global options before the subcommand "
        "(for example: delegate --json --cwd PATH <subcommand> ...)."
    )
    raise DelegateError("misplaced_global_option", f"{message} {guidance}")


def consume_json_option(rest: list[str], json_mode: bool) -> tuple[list[str], bool]:
    """Accept `--json` after contained inspection commands.

    Launching commands still require global options before the subcommand so
    prompt boundaries stay unambiguous. For no-launch inspection commands,
    accepting `delegate describe --json` and friends removes a common operator
    foot-gun without changing child-runtime invocation semantics.
    """

    normalized: list[str] = []
    for token in rest:
        if token == "--json":
            json_mode = True
            continue
        normalized.append(token)
    return normalized, json_mode


def require_no_extra(rest: list[str], name: str) -> None:
    if rest:
        if has_misplaced_global_option(rest):
            raise_misplaced_global_option("Global options must appear before the subcommand.")
        raise DelegateError(
            "unexpected_argument", f"{name} does not accept arguments: {' '.join(rest)}"
        )


def parse_run(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
) -> ParsedCommand:
    # Help wins before required-arg validation: `run --help` needs no --input-json.
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "run")
    if len(rest) != 2 or rest[0] != "--input-json":
        if has_misplaced_global_option(rest):
            raise_misplaced_global_option("Global options must appear before the subcommand.")
        raise DelegateError("invalid_run_args", "run requires: --input-json FILE")
    return ParsedCommand(
        "run",
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        ),
        run_json=RunJsonOptions(rest[1]),
    )


def parse_modeless_engine(
    engine: str,
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    dry_run: bool,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
    *,
    help_topic: str | None = None,
) -> ParsedCommand:
    """Parse the shared cursor/codex grammar: <mode> [--prompt-file PATH] [prompt...]."""
    topic = help_topic if help_topic is not None else engine
    # Help wins before a mode is consumed: `cursor --help` needs no mode.
    if rest and command_help.is_help_token(rest[0]):
        return help_command(json_mode, topic)
    if not rest:
        raise DelegateError("missing_mode", f"{engine} requires mode: safe or work.")
    mode = rest[0]
    if mode.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    validate_mode(mode)
    # Help wins immediately after the mode, before prompt capture begins:
    # `cursor safe --help`. Once a prompt positional begins, a later --help is
    # prompt text (`cursor work explain --help`).
    if len(rest) >= 2 and command_help.is_help_token(rest[1]):
        return help_command(json_mode, topic)
    prompt_file, reasoning_effort, prompt_parts = parse_prompt_tail(rest[1:])
    return ParsedCommand(
        engine,
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        ),
        launch=LaunchOptions(
            engine=engine,
            mode=mode,
            prompt_parts=prompt_parts,
            prompt_file=prompt_file,
            reasoning_effort=reasoning_effort,
            dry_run=dry_run,
        ),
    )


def parse_droid(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    dry_run: bool,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
    *,
    help_topic: str | None = None,
) -> ParsedCommand:
    topic = help_topic if help_topic is not None else "droid"
    # Help wins before the alias is consumed: `droid --help` needs no alias.
    if rest and command_help.is_help_token(rest[0]):
        return help_command(json_mode, topic)
    if len(rest) < 2:
        raise DelegateError("missing_droid_args", "droid requires MODEL_ALIAS and mode.")
    model_alias = rest[0]
    if model_alias.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    # Help wins after the alias, before the mode: `droid x --help`.
    if command_help.is_help_token(rest[1]):
        return help_command(json_mode, topic)
    mode = rest[1]
    if mode.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    validate_mode(mode)
    # Help wins after the mode, before prompt capture: `droid x safe --help`.
    if len(rest) >= 3 and command_help.is_help_token(rest[2]):
        return help_command(json_mode, topic)
    prompt_file, reasoning_effort, prompt_parts = parse_prompt_tail(rest[2:])
    return ParsedCommand(
        "droid",
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        ),
        launch=LaunchOptions(
            engine="droid",
            mode=mode,
            model_alias=model_alias,
            prompt_parts=prompt_parts,
            prompt_file=prompt_file,
            reasoning_effort=reasoning_effort,
            dry_run=dry_run,
        ),
    )


def parse_dry_run(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
) -> ParsedCommand:
    # Help wins before the engine is consumed: `dry-run --help`.
    if rest and command_help.is_help_token(rest[0]):
        return help_command(json_mode, "dry-run")
    if not rest:
        raise DelegateError(
            "missing_engine",
            "dry-run requires cursor, droid, codex, kimi, or claude.",
        )
    engine = rest[0]
    if engine.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    if engine in ("cursor", "codex", "kimi", "claude"):
        return parse_modeless_engine(
            engine,
            rest[1:],
            json_mode,
            cwd,
            dry_run=True,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            help_topic="dry-run",
        )
    if engine == "droid":
        return parse_droid(
            rest[1:],
            json_mode,
            cwd,
            dry_run=True,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            help_topic="dry-run",
        )
    raise DelegateError(
        "invalid_engine",
        "dry-run engine must be cursor, droid, codex, kimi, or claude.",
    )


def parse_prompt_tail(rest: list[str]) -> tuple[str | None, str | None, list[str]]:
    prompt_file: str | None = None
    reasoning_effort: str | None = None
    prompt_parts: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--prompt-file":
            if prompt_parts:
                raise DelegateError(
                    "ambiguous_prompt_source",
                    "--prompt-file must appear before direct prompt text.",
                )
            if prompt_file is not None:
                raise DelegateError("ambiguous_prompt_source", "Only one --prompt-file is allowed.")
            if i + 1 >= len(rest):
                raise DelegateError("missing_prompt_file", "--prompt-file requires a path.")
            prompt_file = rest[i + 1]
            i += 2
            continue
        if token == "--reasoning-effort":
            if reasoning_effort is not None:
                raise DelegateError(
                    "invalid_reasoning_effort",
                    "Only one --reasoning-effort is allowed.",
                )
            if i + 1 >= len(rest):
                raise DelegateError(
                    "missing_reasoning_effort",
                    "--reasoning-effort requires a value.",
                )
            value = rest[i + 1]
            if value.startswith("-") or command_help.is_help_token(value):
                raise DelegateError(
                    "missing_reasoning_effort",
                    "--reasoning-effort requires a value.",
                )
            try:
                reasoning_effort = reasoning.normalize_effort(value)
            except reasoning.ReasoningCapabilityError as exc:
                raise DelegateError(exc.error, exc.message) from exc
            i += 2
            continue
        prompt_parts = rest[i:]
        break
    if "--prompt-file" in prompt_parts:
        raise DelegateError(
            "ambiguous_prompt_source", "--prompt-file must appear before direct prompt text."
        )
    if has_misplaced_global_option(prompt_parts):
        raise_misplaced_global_option(
            "Global options must appear before the subcommand; use --prompt-file for literal flag text.",
        )
    return prompt_file, reasoning_effort, prompt_parts


def parse_snapshot(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    # Help wins over the optional handle/flags: a help token anywhere is help.
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "snapshot")
    latest_harness: str | None = None
    no_redact = False
    handle: str | None = None
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--latest":
            if i + 1 >= len(rest):
                raise DelegateError("missing_harness", "snapshot --latest requires a harness name.")
            latest_harness = rest[i + 1]
            i += 2
            continue
        if token == "--no-redact":
            no_redact = True
            i += 1
            continue
        if token.startswith("-"):
            raise DelegateError("unknown_option", f"snapshot does not support option: {token}")
        if handle is not None:
            raise DelegateError(
                "unexpected_argument", f"snapshot accepts one handle: {' '.join(rest)}"
            )
        handle = token
        i += 1
    if latest_harness is None and handle is None:
        raise DelegateError(
            "missing_handle", "snapshot requires <alias-or-runId> or --latest <harness>."
        )
    if latest_harness is not None and handle is not None:
        raise DelegateError(
            "ambiguous_snapshot_target",
            "Use either --latest <harness> or an exact handle, not both.",
        )
    return ParsedCommand(
        "snapshot",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        snapshot=inspection_commands.SnapshotCommand(
            handle=handle,
            latest_harness=latest_harness,
            no_redact=no_redact,
            json_mode=json_mode,
        ),
    )


KNOWN_HARNESSES = ("cursor", "droid", "codex", "kimi", "claude")


def parse_runs(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    # Help wins over flags: a help token anywhere is help.
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "runs")
    active = False
    recent = False
    running = False
    stale = False
    harness: str | None = None
    limit: int | None = None
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--active":
            active = True
            i += 1
            continue
        if token == "--running":
            running = True
            i += 1
            continue
        if token == "--stale":
            stale = True
            i += 1
            continue
        if token == "--recent":
            recent = True
            i += 1
            continue
        if token == "--harness":
            if i + 1 >= len(rest):
                raise DelegateError("missing_harness", "runs --harness requires a harness name.")
            harness = rest[i + 1]
            if harness not in KNOWN_HARNESSES:
                raise DelegateError(
                    "invalid_harness",
                    f"runs --harness must be one of {', '.join(KNOWN_HARNESSES)}.",
                )
            i += 2
            continue
        if token == "--limit":
            limit, i = parse_required_positive_int_option(
                rest,
                i,
                option_label="runs --limit",
                missing_error="missing_limit",
                invalid_error="invalid_limit",
            )
            continue
        raise DelegateError("unknown_option", f"runs does not support option: {token}")
    selected_modes = [
        label
        for label, selected in (
            ("--active", active),
            ("--running", running),
            ("--stale", stale),
            ("--recent", recent),
        )
        if selected
    ]
    if len(selected_modes) > 1:
        raise DelegateError(
            "invalid_option_combination",
            f"runs filters are mutually exclusive: {', '.join(selected_modes)}.",
        )
    return ParsedCommand(
        "runs",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        runs=inspection_commands.RunsCommand(
            active=active,
            running=running,
            stale=stale,
            harness=harness,
            limit=limit,
            json_mode=json_mode,
        ),
    )


def parse_run_output(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    # Help wins over the required handle/selectors: a help token anywhere is help.
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "run-output")
    if not rest:
        raise DelegateError("missing_handle", "run-output requires <alias-or-runId>.")
    handle = rest[0]
    completion_report = False
    stdout_flag = False
    stderr_flag = False
    tail: int | None = None
    raw = False
    no_redact = False
    i = 1
    while i < len(rest):
        token = rest[i]
        if token == "--completion-report":
            completion_report = True
            i += 1
            continue
        if token == "--stdout":
            stdout_flag = True
            i += 1
            continue
        if token == "--stderr":
            stderr_flag = True
            i += 1
            continue
        if token == "--raw":
            raw = True
            i += 1
            continue
        if token == "--no-redact":
            no_redact = True
            i += 1
            continue
        if token == "--tail":
            tail, i = parse_required_positive_int_option(
                rest,
                i,
                option_label="run-output --tail",
                missing_error="missing_tail",
                invalid_error="invalid_tail",
                missing_value_description="a line count",
            )
            continue
        raise DelegateError("unknown_option", f"run-output does not support option: {token}")
    default_output = not (completion_report or stdout_flag or stderr_flag or raw)
    if default_output:
        completion_report = True
    if raw and tail is not None:
        raise DelegateError(
            "invalid_option_combination",
            "run-output --raw cannot be combined with --tail.",
        )
    if (stdout_flag or stderr_flag) and not raw and tail is None:
        tail = RUN_OUTPUT_DEFAULT_TAIL_LINES
    return ParsedCommand(
        "run-output",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        run_output=run_output_commands.RunOutputCommand(
            handle=handle,
            json_mode=json_mode,
            completion_report=completion_report,
            stdout=stdout_flag,
            stderr=stderr_flag,
            tail=tail,
            raw=raw,
            no_redact=no_redact,
            default=default_output,
        ),
    )


def parse_non_negative_int(value: str, *, option: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise DelegateError("invalid_option_value", f"{option} must be an integer.") from None
    if parsed < 0:
        raise DelegateError("invalid_option_value", f"{option} must be non-negative.")
    return parsed


def parse_positive_int(value: str, *, option: str) -> int:
    parsed = parse_non_negative_int(value, option=option)
    if parsed < 1:
        raise DelegateError("invalid_option_value", f"{option} must be at least 1.")
    return parsed


def parse_required_positive_int_option(
    rest: list[str],
    index: int,
    *,
    option_label: str,
    missing_error: str,
    invalid_error: str,
    missing_value_description: str = "a positive integer",
) -> tuple[int, int]:
    if index + 1 >= len(rest):
        raise DelegateError(
            missing_error,
            f"{option_label} requires {missing_value_description}.",
        )
    try:
        parsed = int(rest[index + 1])
    except ValueError as exc:
        raise DelegateError(invalid_error, f"{option_label} must be a positive integer.") from exc
    if parsed < 1:
        raise DelegateError(invalid_error, f"{option_label} must be at least 1.")
    return parsed, index + 2


def _require_option_value(rest: list[str], index: int, option: str) -> str:
    if index + 1 >= len(rest):
        raise DelegateError("missing_option_value", f"{option} requires a value.")
    value = rest[index + 1]
    if value.startswith("-"):
        raise DelegateError("missing_option_value", f"{option} requires a value.")
    return value


WorktreeOptionSpec = tuple[str, str]


WORKTREE_OPTION_SPECS: dict[str, dict[str, WorktreeOptionSpec]] = {
    "list": {
        "--harness": ("str", "harness"),
        "--status": ("status", "status"),
        "--limit": ("positive_int", "limit"),
        "--no-auto-prune": ("flag", "no_auto_prune"),
    },
    "show": {
        "--latest": ("str", "latest_harness"),
    },
    "remove": {
        "--discard-uncommitted": ("flag", "discard_uncommitted"),
        "--force-branch": ("flag", "force_branch"),
        "--force": ("flag", "force"),
        "--keep-branch": ("flag", "keep_branch"),
    },
    "prune": {
        "--merged": ("flag", "merged"),
        "--older-than": ("non_negative_int", "older_than_days"),
        "--harness": ("str", "harness"),
        "--include-detached": ("flag", "include_detached"),
        "--dry-run": ("flag", "dry_run"),
        "--discard-uncommitted": ("flag", "discard_uncommitted"),
        "--force-branch": ("flag", "force_branch"),
        "--force": ("flag", "force"),
    },
    "gc": {
        "--dry-run": ("flag", "dry_run"),
    },
}


def _apply_worktree_option(
    options: dict[str, object],
    args: list[str],
    index: int,
    option: str,
    spec: WorktreeOptionSpec,
) -> int:
    kind, attr = spec
    if kind == "flag":
        options[attr] = True
        return index + 1
    value = _require_option_value(args, index, option)
    if kind == "str":
        options[attr] = value
    elif kind == "status":
        if value not in worktree_mgmt.VALID_STATUSES:
            raise DelegateError(
                "invalid_option_value",
                "--status must be present, removed, missing, or unknown.",
            )
        options[attr] = value
    elif kind == "positive_int":
        options[attr] = parse_positive_int(value, option=option)
    elif kind == "non_negative_int":
        options[attr] = parse_non_negative_int(value, option=option)
    else:  # pragma: no cover - table construction bug
        raise AssertionError(f"unknown worktree option kind: {kind}")
    return index + 2


def parse_worktree(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    # Help wins before an action is consumed: `worktree --help`.
    if rest and command_help.is_help_token(rest[0]):
        return help_command(json_mode, "worktree")
    if not rest:
        raise DelegateError(
            "missing_worktree_action", "worktree requires list, show, remove, prune, or gc."
        )
    action = rest[0]
    args = rest[1:]
    # Destructive safety: a help token anywhere in an action's args makes help
    # win, so no removal/prune ever fires when the user asked for help. An
    # unknown action with a help token falls back to the worktree overview.
    if any(command_help.is_help_token(token) for token in args):
        topic = f"worktree {action}" if action in WORKTREE_OPTION_SPECS else "worktree"
        return help_command(json_mode, topic)
    if action not in WORKTREE_OPTION_SPECS:
        raise DelegateError("unknown_worktree_action", f"Unknown worktree action: {action}")
    options: dict[str, object] = {}
    positional: list[str] = []
    i = 0
    action_specs = WORKTREE_OPTION_SPECS[action]
    while i < len(args):
        token = args[i]
        if token in MISPLACED_GLOBAL_OPTIONS:
            raise_misplaced_global_option(f"{token} must appear before the subcommand.")
        spec = action_specs.get(token)
        if spec is not None:
            i = _apply_worktree_option(options, args, i, token, spec)
            continue
        if token.startswith("--"):
            raise DelegateError(
                "unknown_option", f"worktree {action} does not support option: {token}"
            )
        positional.append(token)
        i += 1

    if action in {"list", "prune", "gc"} and positional:
        raise DelegateError(
            "unexpected_argument", f"worktree {action} does not accept positional arguments."
        )
    if action == "show":
        if options.get("latest_harness") is not None:
            if positional:
                raise DelegateError(
                    "invalid_option_combination",
                    "worktree show accepts either --latest HARNESS or a handle, not both.",
                )
        elif len(positional) != 1:
            raise DelegateError("missing_handle", "worktree show requires an alias or run id.")
        else:
            options["handle"] = positional[0]
    if action == "remove":
        if len(positional) != 1:
            raise DelegateError("missing_handle", "worktree remove requires an alias or run id.")
        options["handle"] = positional[0]
    if options.get("keep_branch") and (options.get("force_branch") or options.get("force")):
        raise DelegateError(
            "invalid_option_combination",
            "worktree remove --keep-branch is mutually exclusive with --force-branch/--force.",
        )
    return ParsedCommand(
        "worktree",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        worktree=worktree_commands.WorktreeCommand(
            action=action,
            json_mode=json_mode,
            **options,
        ),
    )


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


def request_from_parsed(parsed: ParsedCommand, config: JsonObject, stdin: TextIO) -> Request:
    validate_config(config)
    if parsed.subcommand == "run":
        return request_from_input_json(parsed, config)
    launch = parsed.launch
    global_options = parsed.global_options
    if launch is None or launch.engine not in ("cursor", "droid", "codex", "kimi", "claude"):
        raise DelegateError("invalid_command", "Command does not map to an execution request.")
    if launch.mode is None:
        raise DelegateError("invalid_command", "Command does not map to an execution request.")
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
    if engine not in ("cursor", "droid", "codex", "kimi", "claude"):
        raise DelegateError(
            "invalid_engine",
            "engine must be cursor, droid, codex, kimi, or claude.",
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

    # Resolve workspace from CLI --cwd and JSON cwd.
    workspace = resolve_workspace(global_options.cwd, json_cwd)

    # Capture git metadata for isolation planning (read-only).
    git_root, git_common_dir, git_head_oid, git_head_ref, git_branch = capture_git_metadata(
        workspace.path
    )

    # Resolve effective isolation and build isolation context.
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
            effort = reasoning.resolve_claude_native_effort(build.requested_effort)
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
        reasoning_effort=effort,
        reasoning_effort_source=build.effort_source if effort is not None else None,
        reasoning_capability_source="static" if effort is not None else None,
        reasoning_transport=(
            reasoning.TRANSPORT_CLAUDE_EFFORT_FLAG if effort is not None else None
        ),
    )


def _kimi_request_parts(build: EngineBuildInput) -> EngineRequestParts:
    _ = build.effort_source, build.cache
    if build.requested_effort is not None:
        raise DelegateError(
            "unsupported_reasoning_effort",
            "Reasoning effort is not supported for harness: kimi.",
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
            "engine must be cursor, droid, codex, kimi, or claude.",
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
    return Request(
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
        warnings=parts.warnings,
        stdin_text=parts.stdin_text,
        prompt_file_text=parts.prompt_file_text,
        prompt_transport=parts.prompt_transport,
        display_argv=parts.display_argv,
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
    mappings = cursor_config.get("reasoningEffortModels")
    if not isinstance(mappings, dict):
        raise DelegateError(
            "unsupported_reasoning_effort",
            "Cursor reasoning effort requires cursor.reasoningEffortModels.",
        )
    model = mappings.get(requested_effort)
    if not isinstance(model, str) or not model:
        raise DelegateError(
            "unsupported_reasoning_effort",
            f"Cursor reasoning effort {requested_effort!r} requires a configured model mapping.",
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


def _resolve_default_model(section: JsonObject) -> str | None:
    default_model = section.get("defaultModel")
    return default_model if isinstance(default_model, str) and default_model else None


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
