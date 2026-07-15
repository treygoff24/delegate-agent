"""Command-line argument parsing.

Turns ``argv`` into a typed ``ParsedCommand`` (which subcommand, global options,
launch options, and per-subcommand argument objects). This layer only parses and
validates argument shape; workspace/prompt resolution and request construction
live in ``request_build``. The SAFE-mode security strictness lives in the argv
builders, not here.
"""

from __future__ import annotations

import difflib
import re
import shlex
from typing import NoReturn

from delegate_agent import (
    capability_commands,
    command_help,
    config_commands,
    inspection_commands,
    profile_commands,
    reasoning,
    run_output_commands,
    wait_cancel_commands,
    worktree_commands,
    worktree_mgmt,
)
from delegate_agent import config as delegate_config
from delegate_agent.constants import (
    ENGINES_PROSE,
    KNOWN_ENGINES,
    MODELESS_ENGINES,
    VALID_MODES,
    validate_mode,
)
from delegate_agent.errors import DelegateError
from delegate_agent.request_models import (
    GlobalOptions,
    InspectionOptions,
    LaunchOptions,
    ParsedCommand,
    RunJsonOptions,
)
from delegate_agent.run_output_commands import RUN_OUTPUT_DEFAULT_TAIL_LINES
from delegate_agent.workflows import commands as workflow_commands

MISPLACED_GLOBAL_OPTIONS = frozenset(
    {
        "--json",
        "--cwd",
        "--isolation",
        "--pass-through",
        "--completion-report",
        "--no-completion-report",
        "--auth-profile",
        "--group",
    }
)

VALUE_GLOBAL_OPTIONS = frozenset(
    {"--cwd", "--isolation", "--completion-report", "--auth-profile", "--group"}
)

AUTH_PROFILE_SUBCOMMANDS = frozenset(KNOWN_ENGINES) | frozenset(
    {"dry-run", "run", "profiles", "capabilities"}
)
GROUP_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
GROUP_SUBCOMMANDS = frozenset(KNOWN_ENGINES) | frozenset({"dry-run", "run"})


def validate_group(value: str, *, option: str = "--group") -> str:
    if not GROUP_RE.fullmatch(value):
        raise DelegateError(
            "invalid_group",
            f"{option} must match [A-Za-z0-9._-]{{1,64}}.",
        )
    return value


def infer_global_json(argv: list[str]) -> bool:
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--json":
            return True
        if token in VALUE_GLOBAL_OPTIONS:
            i += 2
            continue
        if token in MISPLACED_GLOBAL_OPTIONS:
            i += 1
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
INSPECTION_OPTION_SUBCOMMANDS = frozenset({"models", "describe"})


def parse_simple_inspection_subcommand(
    name: str,
    rest: list[str],
    *,
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
    auth_profile: str | None,
) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, name)
    summary = False
    engine: str | None = None
    live = False
    if name in INSPECTION_OPTION_SUBCOMMANDS:
        for token in rest:
            if token == "--summary":
                summary = True
                continue
            if name == "models" and token == "--live":
                live = True
                continue
            if name == "models" and not token.startswith("-"):
                if engine is not None:
                    require_no_extra([token], name)
                engine = token
                continue
            require_no_extra([token], name)
        if live and engine is None:
            raise DelegateError(
                "invalid_option_combination",
                "--live requires an engine argument (delegate models <engine> --live).",
            )
        if summary and engine is not None:
            raise DelegateError(
                "invalid_option_combination",
                "--summary is not supported with models <engine>.",
            )
        if engine is not None and engine not in KNOWN_ENGINES:
            raise DelegateError(
                "invalid_engine",
                f"engine must be {ENGINES_PROSE}.",
            )
    else:
        require_no_extra(rest, name)
    return ParsedCommand(
        name,
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
        ),
        inspection=InspectionOptions(summary=summary, engine=engine, live=live),
    )


def parse_capabilities_subcommand(
    rest: list[str],
    *,
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
    auth_profile: str | None,
) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "capabilities")
    refresh = False
    if rest == ["refresh"]:
        refresh = True
    elif rest:
        require_no_extra(rest, "capabilities")
    if auth_profile is not None and not refresh:
        raise DelegateError(
            "invalid_option_combination",
            "--auth-profile is only supported with capabilities refresh; "
            "the cached capabilities report does not spawn a harness.",
        )
    return ParsedCommand(
        "capabilities",
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
        ),
        capabilities=capability_commands.CapabilitiesCommand(
            refresh=refresh,
            json_mode=json_mode,
        ),
    )


def parse_config_subcommand(
    rest: list[str],
    *,
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
    auth_profile: str | None,
) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if not rest or command_help.is_help_token(rest[0]):
        return help_command(json_mode, "config")
    if cwd is not None or pass_through or completion_report is not None or isolation is not None:
        raise DelegateError(
            "invalid_option_combination",
            "delegate config does not use --cwd, --isolation, --pass-through, or completion-report options.",
        )
    if auth_profile is not None:
        raise DelegateError(
            "invalid_option_combination",
            "--auth-profile is not supported with delegate config.",
        )
    action = rest[0]
    if action not in {"init", "sync-profiles"}:
        raise DelegateError("unknown_config_action", f"Unknown config action: {action}")
    force = False
    if action == "sync-profiles":
        for token in rest[1:]:
            if command_help.is_help_token(token):
                return help_command(json_mode, "config sync-profiles")
            require_no_extra([token], "config sync-profiles")
        return ParsedCommand(
            "config",
            global_options=GlobalOptions(json_mode=json_mode),
            config_command=config_commands.ConfigCommand(
                action="sync-profiles",
                json_mode=json_mode,
            ),
        )
    for token in rest[1:]:
        if command_help.is_help_token(token):
            return help_command(json_mode, "config init")
        if token == "--force":
            force = True
            continue
        require_no_extra([token], "config init")
    return ParsedCommand(
        "config",
        global_options=GlobalOptions(json_mode=json_mode),
        config_command=config_commands.ConfigCommand(
            action="init",
            force=force,
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
    auth_profile: str | None = None
    group: str | None = None
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
        if token == "--auth-profile":
            if i + 1 >= len(argv):
                raise DelegateError(
                    "missing_auth_profile", "--auth-profile requires a profile name."
                )
            auth_profile = argv[i + 1]
            if not auth_profile or auth_profile.startswith("-"):
                raise DelegateError(
                    "missing_auth_profile", "--auth-profile requires a profile name."
                )
            i += 2
            continue
        if token == "--group":
            if i + 1 >= len(argv):
                raise DelegateError("missing_group", "--group requires a group name.")
            group = validate_group(argv[i + 1])
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
    if subcommand == "list":
        subcommand = "runs"
    rest = argv[i + 1 :]
    if subcommand.startswith("-"):
        raise DelegateError(
            "unknown_option", f"Unknown global option before subcommand: {subcommand}"
        )
    if auth_profile is not None and subcommand not in AUTH_PROFILE_SUBCOMMANDS:
        raise DelegateError(
            "invalid_option_combination",
            f"--auth-profile is not supported with delegate {subcommand}; "
            "use it with launches, dry-run, run --input-json, profiles, or capabilities refresh.",
        )
    if group is not None and subcommand not in GROUP_SUBCOMMANDS:
        raise DelegateError(
            "invalid_option_combination",
            f"--group is not supported with delegate {subcommand}; use command-specific "
            "--group selectors where available.",
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
            auth_profile=auth_profile,
        )
    if subcommand == "capabilities":
        return parse_capabilities_subcommand(
            rest,
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
        )
    if subcommand == "config":
        return parse_config_subcommand(
            rest,
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
        )
    if subcommand == "run":
        return parse_run(
            rest, json_mode, cwd, pass_through, completion_report, isolation, auth_profile, group
        )
    if subcommand in MODELESS_ENGINES:
        return parse_modeless_engine(
            subcommand,
            rest,
            json_mode,
            cwd,
            dry_run=False,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
            group=group,
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
            auth_profile=auth_profile,
        )
    if subcommand == "dry-run":
        return parse_dry_run(
            rest, json_mode, cwd, pass_through, completion_report, isolation, auth_profile, group
        )
    if subcommand == "snapshot":
        return parse_snapshot(rest, json_mode, cwd)
    if subcommand == "runs":
        return parse_runs(rest, json_mode, cwd)
    if subcommand == "run-output":
        return parse_run_output(rest, json_mode, cwd)
    if subcommand == "wait":
        if isolation is not None:
            raise DelegateError(
                "invalid_option_combination",
                "--isolation is not supported with delegate wait.",
            )
        return parse_wait(rest, json_mode, cwd)
    if subcommand == "cancel":
        if isolation is not None:
            raise DelegateError(
                "invalid_option_combination",
                "--isolation is not supported with delegate cancel.",
            )
        return parse_cancel(rest, json_mode, cwd)
    if subcommand == "worktree":
        if isolation is not None:
            raise DelegateError(
                "invalid_option_combination",
                "--isolation is not supported with delegate worktree commands.",
            )
        return parse_worktree(rest, json_mode, cwd)
    if subcommand == "workflow":
        if isolation is not None:
            raise DelegateError(
                "invalid_option_combination",
                "--isolation is not supported with delegate workflow commands.",
            )
        return parse_workflow(rest, json_mode, cwd)
    if subcommand == "profiles":
        if isolation is not None:
            raise DelegateError(
                "invalid_option_combination",
                "--isolation is not supported with delegate profiles.",
            )
        return parse_profiles(rest, json_mode, cwd, auth_profile)

    raise DelegateError("unknown_subcommand", unknown_subcommand_message(subcommand))


def unknown_subcommand_message(subcommand: str) -> str:
    suggestion_only = {
        "codex-auth": "use: delegate profiles",
        "kill": "use: delegate cancel ...",
    }
    if subcommand.startswith("droid-"):
        model = subcommand.removeprefix("droid-")
        suggestion_only[subcommand] = f"use: delegate droid {model} ..."
    candidates = sorted(set(command_help.COMMAND_SPECS) | set(KNOWN_ENGINES))
    matches = difflib.get_close_matches(subcommand, candidates, n=3)
    parts = [f"Unknown subcommand: {subcommand}"]
    if subcommand in suggestion_only:
        parts.append(suggestion_only[subcommand])
    elif matches:
        parts.append(f"Did you mean: {', '.join(matches)}?")
    top_level = sorted({name.split()[0] for name in command_help.COMMAND_SPECS})
    if len(top_level) <= 20:
        parts.append(f"Valid subcommands: {', '.join(top_level)}.")
    return " ".join(parts)


def has_misplaced_global_option(tokens: list[str]) -> bool:
    return any(token in MISPLACED_GLOBAL_OPTIONS for token in tokens)


def _shell_command(argv: list[str]) -> str:
    return shlex.join(["delegate", *argv])


def corrected_global_command(argv: list[str]) -> str:
    globals_out: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in VALUE_GLOBAL_OPTIONS and i + 1 < len(argv):
            globals_out.extend([token, argv[i + 1]])
            i += 2
            continue
        if token in MISPLACED_GLOBAL_OPTIONS:
            globals_out.append(token)
            i += 1
            continue
        rest.append(token)
        i += 1
    return _shell_command([*globals_out, *rest])


def raise_misplaced_global_option(message: str, argv: list[str] | None = None) -> NoReturn:
    guidance = (
        "Move global options before the subcommand "
        "(for example: delegate --json --cwd PATH <subcommand> ...)."
    )
    corrected = f" Corrected command: {corrected_global_command(argv)}." if argv else ""
    raise DelegateError("misplaced_global_option", f"{message} {guidance}{corrected}")


def unknown_option_message(command: str, option: str) -> str:
    spec = command_help.COMMAND_SPECS.get(command)
    candidates = [opt.flag for opt in spec.options] if spec is not None else []
    matches = difflib.get_close_matches(option, candidates, n=3)
    if command == "run-output" and option == "--full":
        matches = ["--raw", *[match for match in matches if match != "--raw"]]
    suffix = f" Did you mean: {', '.join(matches)}?" if matches else ""
    return f"{command} does not support option: {option}.{suffix}"


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
    auth_profile: str | None,
    group: str | None = None,
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
            auth_profile=auth_profile,
            group=group,
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
    auth_profile: str | None,
    group: str | None = None,
    *,
    help_topic: str | None = None,
) -> ParsedCommand:
    """Parse the shared cursor/codex grammar: <mode> [--prompt-file PATH] [prompt...]."""
    topic = help_topic if help_topic is not None else engine
    # Help wins before a mode is consumed: `cursor --help` needs no mode.
    if rest and command_help.is_help_token(rest[0]):
        return help_command(json_mode, topic)
    if not rest:
        raise DelegateError("missing_mode", f"{engine} requires mode: safe, work, or call.")
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
        return help_command(json_mode, f"{topic} call" if mode == "call" else topic)
    (
        prompt_file,
        output_schema,
        reasoning_effort,
        fast,
        progress_intent,
        forbid_commit,
        prompt_parts,
        json_mode,
        isolation,
        read_only,
        include_dirty,
        model,
        agent,
    ) = parse_prompt_tail(rest[1:], json_mode, isolation, command_prefix=[engine, mode])
    if agent is not None and engine != "opencode":
        raise DelegateError("unsupported_agent", "--agent is only supported by opencode.")
    if fast is not None and engine != "codex":
        raise DelegateError("unsupported_fast", "--fast and --no-fast are only supported by codex.")
    forbid_commit_implied_isolation = False
    if forbid_commit and mode == "work" and isolation is None:
        isolation = "worktree"
        forbid_commit_implied_isolation = True
    if forbid_commit and mode == "work" and isolation == "none":
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit cannot be combined with --isolation none. "
            f"Corrected command: {_shell_command(['--isolation', 'worktree', engine, mode, '--forbid-commit', *(prompt_parts or [])])}.",
        )
    return ParsedCommand(
        engine,
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
            group=group,
        ),
        launch=LaunchOptions(
            engine=engine,
            mode=mode,
            prompt_parts=prompt_parts,
            prompt_file=prompt_file,
            output_schema=output_schema,
            reasoning_effort=reasoning_effort,
            fast=fast,
            progress_intent=progress_intent,
            forbid_commit=forbid_commit,
            forbid_commit_implied_isolation=forbid_commit_implied_isolation,
            include_dirty=include_dirty,
            read_only=read_only,
            dry_run=dry_run,
            model=model,
            agent=agent,
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
    auth_profile: str | None,
    group: str | None = None,
    *,
    help_topic: str | None = None,
) -> ParsedCommand:
    topic = help_topic if help_topic is not None else "droid"
    # Help wins before the alias is consumed: `droid --help` needs no alias.
    if rest and command_help.is_help_token(rest[0]):
        return help_command(json_mode, topic)
    if not rest:
        raise DelegateError(
            "missing_droid_args",
            "droid requires mode (and optionally a MODEL_ALIAS before the mode).",
        )
    if rest[0].startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    # First token is the mode when it is a known mode name; otherwise it is the
    # positional alias and the mode follows.
    if rest[0] in VALID_MODES:
        model_alias = None
        mode = rest[0]
        tail = rest[1:]
        command_prefix = ["droid", mode]
    else:
        if len(rest) < 2:
            raise DelegateError(
                "missing_droid_args",
                "droid requires MODEL_ALIAS and mode.",
            )
        model_alias = rest[0]
        # Help wins after the alias, before the mode: `droid x --help`.
        if command_help.is_help_token(rest[1]):
            return help_command(json_mode, topic)
        mode = rest[1]
        if mode.startswith("-"):
            raise DelegateError(
                "misplaced_global_option", "Global options must appear before the subcommand."
            )
        validate_mode(mode)
        tail = rest[2:]
        command_prefix = ["droid", model_alias, mode]
    # Help wins after the mode, before prompt capture: `droid [alias] safe --help`.
    if tail and command_help.is_help_token(tail[0]):
        return help_command(json_mode, f"{topic} call" if mode == "call" else topic)
    (
        prompt_file,
        output_schema,
        reasoning_effort,
        fast,
        progress_intent,
        forbid_commit,
        prompt_parts,
        json_mode,
        isolation,
        read_only,
        include_dirty,
        model,
        agent,
    ) = parse_prompt_tail(tail, json_mode, isolation, command_prefix=command_prefix)
    if agent is not None:
        raise DelegateError("unsupported_agent", "--agent is only supported by opencode.")
    if fast is not None:
        raise DelegateError("unsupported_fast", "--fast and --no-fast are only supported by codex.")
    forbid_commit_implied_isolation = False
    if forbid_commit and mode == "work" and isolation is None:
        isolation = "worktree"
        forbid_commit_implied_isolation = True
    if forbid_commit and mode == "work" and isolation == "none":
        droid_tokens = ["droid", *([model_alias] if model_alias else []), mode]
        raise DelegateError(
            "invalid_option_combination",
            "--forbid-commit cannot be combined with --isolation none. "
            f"Corrected command: {_shell_command(['--isolation', 'worktree', *droid_tokens, '--forbid-commit', *(prompt_parts or [])])}.",
        )
    return ParsedCommand(
        "droid",
        global_options=GlobalOptions(
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
            group=group,
        ),
        launch=LaunchOptions(
            engine="droid",
            mode=mode,
            model_alias=model_alias,
            prompt_parts=prompt_parts,
            prompt_file=prompt_file,
            output_schema=output_schema,
            reasoning_effort=reasoning_effort,
            fast=fast,
            progress_intent=progress_intent,
            forbid_commit=forbid_commit,
            forbid_commit_implied_isolation=forbid_commit_implied_isolation,
            include_dirty=include_dirty,
            read_only=read_only,
            dry_run=dry_run,
            model=model,
            agent=agent,
        ),
    )


def parse_dry_run(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
    auth_profile: str | None,
    group: str | None = None,
) -> ParsedCommand:
    # Help wins before the engine is consumed: `dry-run --help`.
    if rest and command_help.is_help_token(rest[0]):
        return help_command(json_mode, "dry-run")
    if not rest:
        raise DelegateError(
            "missing_engine",
            f"dry-run requires {ENGINES_PROSE}.",
        )
    engine = rest[0]
    if engine.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    if engine in MODELESS_ENGINES:
        return parse_modeless_engine(
            engine,
            rest[1:],
            json_mode,
            cwd,
            dry_run=True,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
            group=group,
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
            auth_profile=auth_profile,
            group=group,
            help_topic="dry-run",
        )
    raise DelegateError(
        "invalid_engine",
        f"dry-run engine must be {ENGINES_PROSE}.",
    )


def parse_prompt_tail(
    rest: list[str],
    json_mode: bool,
    isolation: str | None,
    *,
    command_prefix: list[str] | None = None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    bool | None,
    str | None,
    bool,
    list[str],
    bool,
    str | None,
    bool,
    bool,
    str | None,
    str | None,
]:
    prompt_file: str | None = None
    output_schema: str | None = None
    reasoning_effort: str | None = None
    fast: bool | None = None
    progress_intent: str | None = None
    forbid_commit = False
    include_dirty = False
    read_only = False
    model: str | None = None
    agent: str | None = None
    prompt_parts: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        # `--json` is unambiguous anywhere before inline prompt text starts (e.g.
        # after --prompt-file), so accept it here instead of forcing it ahead of the
        # subcommand. Once prompt text begins it lands in prompt_parts and the
        # post-loop misplaced-global guard rejects it, since then it could be prompt
        # text. Mirrors consume_json_option for inspection commands.
        if token == "--json":
            json_mode = True
            i += 1
            continue
        if token == "--prompt-file":
            if prompt_parts:
                raise DelegateError(
                    "ambiguous_prompt_source",
                    "--prompt-file must appear before direct prompt text."
                    + corrected_prompt_file_suffix(command_prefix, rest),
                )
            if prompt_file is not None:
                raise DelegateError("ambiguous_prompt_source", "Only one --prompt-file is allowed.")
            if i + 1 >= len(rest):
                raise DelegateError("missing_prompt_file", "--prompt-file requires a path.")
            prompt_file = rest[i + 1]
            i += 2
            continue
        if token == "--isolation":
            if i + 1 >= len(rest):
                raise DelegateError("missing_isolation_value", "--isolation requires a value.")
            isolation = rest[i + 1]
            if isolation not in delegate_config.VALID_ISOLATION_VALUES:
                raise DelegateError(
                    "invalid_isolation",
                    "--isolation must be auto, none, or worktree.",
                )
            i += 2
            continue
        if token == "--output-schema":
            if output_schema is not None:
                raise DelegateError("invalid_output_schema", "Only one --output-schema is allowed.")
            if i + 1 >= len(rest):
                raise DelegateError("missing_output_schema", "--output-schema requires a path.")
            output_schema = rest[i + 1]
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
                corrected = corrected_drop_option_suffix(command_prefix, rest, i, takes_value=True)
                raise DelegateError(exc.error, f"{exc.message}{corrected}") from exc
            i += 2
            continue
        if token == "--fast":
            if fast is not None:
                detail = (
                    "Only one --fast flag is allowed."
                    if fast
                    else "--fast and --no-fast cannot be combined."
                )
                raise DelegateError("invalid_option_combination", detail)
            fast = True
            i += 1
            continue
        if token == "--no-fast":
            if fast is not None:
                detail = (
                    "Only one --no-fast flag is allowed."
                    if not fast
                    else "--fast and --no-fast cannot be combined."
                )
                raise DelegateError("invalid_option_combination", detail)
            fast = False
            i += 1
            continue
        if token == "--model":
            if model is not None:
                raise DelegateError(
                    "invalid_model",
                    "Only one --model is allowed.",
                )
            if i + 1 >= len(rest):
                raise DelegateError(
                    "missing_model",
                    "--model requires a value.",
                )
            value = rest[i + 1]
            if not value.strip() or value.startswith("-") or command_help.is_help_token(value):
                raise DelegateError(
                    "missing_model",
                    "--model requires a non-empty value.",
                )
            model = value
            i += 2
            continue
        if token == "--agent":
            if agent is not None:
                raise DelegateError(
                    "invalid_agent",
                    "Only one --agent is allowed.",
                )
            if i + 1 >= len(rest):
                raise DelegateError(
                    "missing_agent",
                    "--agent requires a value.",
                )
            value = rest[i + 1]
            if not value.strip() or value.startswith("-") or command_help.is_help_token(value):
                raise DelegateError(
                    "missing_agent",
                    "--agent requires a non-empty value.",
                )
            agent = value
            i += 2
            continue
        if token == "--progress":
            if progress_intent == "on":
                raise DelegateError(
                    "invalid_option_combination",
                    "Only one --progress flag is allowed.",
                )
            if progress_intent == "off":
                raise DelegateError(
                    "invalid_option_combination",
                    "--progress and --no-progress cannot be combined.",
                )
            progress_intent = "on"
            i += 1
            continue
        if token == "--no-progress":
            if progress_intent == "off":
                raise DelegateError(
                    "invalid_option_combination",
                    "Only one --no-progress flag is allowed.",
                )
            if progress_intent == "on":
                raise DelegateError(
                    "invalid_option_combination",
                    "--progress and --no-progress cannot be combined.",
                )
            progress_intent = "off"
            i += 1
            continue
        if token == "--forbid-commit":
            if forbid_commit:
                raise DelegateError(
                    "invalid_option_combination",
                    "Only one --forbid-commit flag is allowed.",
                )
            forbid_commit = True
            i += 1
            continue
        if token == "--include-dirty":
            if include_dirty:
                raise DelegateError(
                    "invalid_option_combination",
                    "Only one --include-dirty flag is allowed.",
                )
            include_dirty = True
            i += 1
            continue
        if token == "--read-only":
            read_only = True
            i += 1
            continue
        prompt_parts = rest[i:]
        break
    if "--prompt-file" in prompt_parts:
        raise DelegateError(
            "ambiguous_prompt_source",
            "--prompt-file must appear before direct prompt text."
            + corrected_prompt_file_suffix(command_prefix, rest),
        )
    if "--output-schema" in prompt_parts:
        raise DelegateError(
            "invalid_output_schema", "--output-schema must appear before direct prompt text."
        )
    if has_misplaced_global_option(prompt_parts):
        raise_misplaced_global_option(
            "Global options must appear before the subcommand; use --prompt-file for literal flag text.",
        )
    return (
        prompt_file,
        output_schema,
        reasoning_effort,
        fast,
        progress_intent,
        forbid_commit,
        prompt_parts,
        json_mode,
        isolation,
        read_only,
        include_dirty,
        model,
        agent,
    )


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
            raise DelegateError("unknown_option", unknown_option_message("snapshot", token))
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
    group: str | None = None
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
            if harness not in KNOWN_ENGINES:
                raise DelegateError(
                    "invalid_harness",
                    f"runs --harness must be one of {', '.join(KNOWN_ENGINES)}.",
                )
            i += 2
            continue
        if token == "--group":
            if i + 1 >= len(rest):
                raise DelegateError("missing_group", "runs --group requires a group name.")
            group = validate_group(rest[i + 1])
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
            group=group,
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
        raise DelegateError("missing_handle", "run-output requires <alias-or-runId> or --latest.")
    handle: str | None = None
    latest_harness: str | None = None
    completion_report = False
    stdout_flag = False
    stderr_flag = False
    tail: int | None = None
    max_chars: int | None = None
    raw = False
    no_redact = False
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--latest":
            if i + 1 >= len(rest):
                raise DelegateError(
                    "missing_harness", "run-output --latest requires a harness name."
                )
            latest_harness = rest[i + 1]
            i += 2
            continue
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
        if token == "--max-chars":
            max_chars, i = parse_required_positive_int_option(
                rest,
                i,
                option_label="run-output --max-chars",
                missing_error="missing_max_chars",
                invalid_error="invalid_max_chars",
                missing_value_description="a positive integer",
            )
            continue
        if token.startswith("-"):
            raise DelegateError("unknown_option", unknown_option_message("run-output", token))
        if handle is not None:
            raise DelegateError(
                "unexpected_argument", f"run-output accepts one handle: {' '.join(rest)}"
            )
        handle = token
        i += 1
    if latest_harness is None and handle is None:
        raise DelegateError("missing_handle", "run-output requires <alias-or-runId> or --latest.")
    if latest_harness is not None and handle is not None:
        raise DelegateError(
            "ambiguous_run_output_target",
            "Use either --latest <harness> or an exact handle, not both.",
        )
    default_output = not (completion_report or stdout_flag or stderr_flag or raw)
    if default_output:
        completion_report = True
    if raw and tail is not None:
        raise DelegateError(
            "invalid_option_combination",
            "run-output --raw cannot be combined with --tail.",
        )
    if raw and max_chars is not None:
        raise DelegateError(
            "invalid_option_combination",
            "run-output --raw cannot be combined with --max-chars.",
        )
    if not (stdout_flag or stderr_flag or raw) and (tail is not None or max_chars is not None):
        raise DelegateError(
            "invalid_option_combination",
            "run-output --tail/--max-chars only apply to --stdout or --stderr; "
            "add --stdout/--stderr or remove the ignored flag.",
        )
    if (stdout_flag or stderr_flag) and not raw and tail is None:
        tail = RUN_OUTPUT_DEFAULT_TAIL_LINES
    return ParsedCommand(
        "run-output",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        run_output=run_output_commands.RunOutputCommand(
            handle=handle,
            latest_harness=latest_harness,
            json_mode=json_mode,
            completion_report=completion_report,
            stdout=stdout_flag,
            stderr=stderr_flag,
            tail=tail,
            max_chars=max_chars,
            raw=raw,
            no_redact=no_redact,
            default=default_output,
        ),
    )


def parse_wait(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "wait")
    handles: list[str] = []
    latest_harness: str | None = None
    group: str | None = None
    timeout = wait_cancel_commands.WAIT_DEFAULT_TIMEOUT_SECONDS
    interval = wait_cancel_commands.WAIT_DEFAULT_INTERVAL_SECONDS
    completion_report = False
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--latest":
            if i + 1 >= len(rest):
                raise DelegateError("missing_harness", "wait --latest requires a harness name.")
            latest_harness = rest[i + 1]
            i += 2
            continue
        if token == "--timeout":
            timeout, i = parse_required_positive_int_option(
                rest,
                i,
                option_label="wait --timeout",
                missing_error="missing_timeout",
                invalid_error="invalid_timeout",
                missing_value_description="seconds",
            )
            continue
        if token == "--group":
            if i + 1 >= len(rest):
                raise DelegateError("missing_group", "wait --group requires a group name.")
            group = validate_group(rest[i + 1])
            i += 2
            continue
        if token == "--interval":
            interval, i = parse_required_positive_int_option(
                rest,
                i,
                option_label="wait --interval",
                missing_error="missing_interval",
                invalid_error="invalid_interval",
                missing_value_description="seconds",
            )
            interval = max(interval, wait_cancel_commands.WAIT_MIN_INTERVAL_SECONDS)
            continue
        if token == "--completion-report":
            completion_report = True
            i += 1
            continue
        if token.startswith("-"):
            raise DelegateError("unknown_option", unknown_option_message("wait", token))
        handles.append(token)
        i += 1
    if not handles and latest_harness is None and group is None:
        raise DelegateError(
            "missing_handle", "wait requires a handle, --latest <harness>, or --group <name>."
        )
    return ParsedCommand(
        "wait",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        wait_command=wait_cancel_commands.WaitCommand(
            handles=tuple(handles),
            latest_harness=latest_harness,
            group=group,
            timeout_seconds=timeout,
            interval_seconds=interval,
            completion_report=completion_report,
            json_mode=json_mode,
        ),
    )


def parse_cancel(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "cancel")
    handles: list[str] = []
    for token in rest:
        if token.startswith("-"):
            raise DelegateError("unknown_option", unknown_option_message("cancel", token))
        handles.append(token)
    if not handles:
        raise DelegateError("missing_handle", "cancel requires at least one run handle.")
    return ParsedCommand(
        "cancel",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        cancel_command=wait_cancel_commands.CancelCommand(tuple(handles), json_mode=json_mode),
    )


def parse_workflow(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if not rest or command_help.is_help_token(rest[0]):
        return help_command(json_mode, "workflow")
    action = rest[0]
    args = rest[1:]
    if action == "_supervise":
        if len(args) != 1:
            raise DelegateError("missing_workflow", "workflow _supervise requires <wfId>.")
        return ParsedCommand(
            "workflow",
            global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
            workflow_command=workflow_commands.WorkflowCommand(
                action="_supervise", wf_id=args[0], json_mode=json_mode
            ),
        )
    if any(command_help.is_help_token(token) for token in args):
        return help_command(json_mode, f"workflow {action}")
    if action in {"run", "check", "save"}:
        return _parse_workflow_path_action(action, args, json_mode, cwd)
    if action in {"status", "watch", "events", "result", "wait", "approve", "kill"}:
        return _parse_workflow_id_action(action, args, json_mode, cwd)
    if action == "list":
        require_no_extra(args, "workflow list")
        return ParsedCommand(
            "workflow",
            global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
            workflow_command=workflow_commands.WorkflowCommand("list", json_mode=json_mode),
        )
    raise DelegateError("unknown_workflow_action", f"Unknown workflow action: {action}")


def _parse_workflow_path_action(
    action: str,
    args: list[str],
    json_mode: bool,
    cwd: str | None,
) -> ParsedCommand:
    script: str | None = None
    args_json: str | None = None
    budget: int | None = None
    dry_run = False
    resume: str | None = None
    name: str | None = None
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--args" and action == "run":
            if i + 1 >= len(args):
                raise DelegateError("missing_workflow_args", "workflow run --args requires JSON.")
            args_json = args[i + 1]
            i += 2
            continue
        if token == "--budget" and action == "run":
            budget, i = parse_required_positive_int_option(
                args,
                i,
                option_label="workflow run --budget",
                missing_error="missing_budget",
                invalid_error="invalid_budget",
            )
            continue
        if token == "--dry-run" and action == "run":
            dry_run = True
            i += 1
            continue
        if token == "--resume" and action == "run":
            if i + 1 >= len(args):
                raise DelegateError("missing_workflow", "workflow run --resume requires <wfId>.")
            resume = args[i + 1]
            i += 2
            continue
        if token == "--name" and action in {"run", "save"}:
            if i + 1 >= len(args):
                raise DelegateError("missing_workflow_name", "workflow --name requires a name.")
            name = args[i + 1]
            i += 2
            continue
        if token.startswith("-"):
            raise DelegateError(
                "unknown_option", unknown_option_message(f"workflow {action}", token)
            )
        if script is not None:
            raise DelegateError(
                "unexpected_argument", f"workflow {action} accepts one script path."
            )
        script = token
        i += 1
    if action == "check" and (script is None or name is not None or resume is not None):
        raise DelegateError("missing_workflow_script", "workflow check requires <script.py>.")
    if action == "save" and (script is None or name is None or resume is not None):
        raise DelegateError("missing_workflow_script", f"workflow {action} requires <script.py>.")
    if action == "run":
        target_count = sum(1 for item in (script, name, resume) if item is not None)
        if target_count != 1:
            raise DelegateError(
                "invalid_workflow_target",
                "workflow run requires exactly one of <script.py>, --name, or --resume.",
            )
        if resume is not None and args_json is not None:
            raise DelegateError(
                "invalid_workflow_args",
                "workflow run --resume does not accept --args; resume uses the pinned args.",
            )
    if action == "run" and resume is None and script is None and name is None:
        raise DelegateError(
            "missing_workflow_script", "workflow run requires <script.py>, --name, or --resume."
        )
    return ParsedCommand(
        "workflow",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        workflow_command=workflow_commands.WorkflowCommand(
            action,
            script=script,
            args_json=args_json,
            budget=budget,
            dry_run=dry_run,
            resume=resume,
            name=name,
            json_mode=json_mode,
        ),
    )


def _parse_workflow_id_action(
    action: str,
    args: list[str],
    json_mode: bool,
    cwd: str | None,
) -> ParsedCommand:
    wf_id: str | None = None
    since = 0
    timeout: int | None = None
    result_field: str | None = None
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--since" and action in {"events", "watch"}:
            if i + 1 >= len(args):
                raise DelegateError("missing_since", f"workflow {action} --since requires a value.")
            since = parse_non_negative_int(args[i + 1], option=f"workflow {action} --since")
            i += 2
            continue
        if token == "--timeout" and action == "wait":
            timeout, i = parse_required_positive_int_option(
                args,
                i,
                option_label="workflow wait --timeout",
                missing_error="missing_timeout",
                invalid_error="invalid_timeout",
            )
            continue
        if token == "--field" and action == "result":
            if i + 1 >= len(args) or not args[i + 1] or args[i + 1].startswith("-"):
                raise DelegateError(
                    "missing_workflow_result_field",
                    "workflow result --field requires a non-empty key that does not start with '-'.",
                )
            result_field = args[i + 1]
            i += 2
            continue
        if token.startswith("-"):
            raise DelegateError(
                "unknown_option", unknown_option_message(f"workflow {action}", token)
            )
        if wf_id is not None:
            raise DelegateError("unexpected_argument", f"workflow {action} accepts one wfId.")
        wf_id = token
        i += 1
    if wf_id is None and action not in {"wait", "result"}:
        raise DelegateError("missing_workflow", f"workflow {action} requires <wfId>.")
    return ParsedCommand(
        "workflow",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd),
        workflow_command=workflow_commands.WorkflowCommand(
            action,
            wf_id=wf_id,
            since=since,
            timeout=timeout,
            result_field=result_field,
            json_mode=json_mode,
        ),
    )


def corrected_prompt_file_suffix(command_prefix: list[str] | None, rest: list[str]) -> str:
    if not command_prefix or "--prompt-file" not in rest:
        return ""
    prompt_index = rest.index("--prompt-file")
    if prompt_index + 1 >= len(rest):
        return ""
    prompt_file = rest[prompt_index + 1]
    before = [token for token in rest[:prompt_index] if token != "--json"]
    after = rest[prompt_index + 2 :]
    corrected = [*command_prefix, "--prompt-file", prompt_file, *before, *after]
    return f" Corrected command: {_shell_command(corrected)}."


def corrected_drop_option_suffix(
    command_prefix: list[str] | None,
    rest: list[str],
    index: int,
    *,
    takes_value: bool,
) -> str:
    if not command_prefix:
        return ""
    drop = 2 if takes_value else 1
    corrected = [*command_prefix, *rest[:index], *rest[index + drop :]]
    return f" Corrected command: {_shell_command(corrected)}."


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
        "--group": ("group", "group"),
        "--status": ("status", "status"),
        "--limit": ("positive_int", "limit"),
        "--no-auto-prune": ("flag", "no_auto_prune"),
    },
    "show": {
        "--latest": ("str", "latest_harness"),
    },
    "remove": {
        "--group": ("group", "group"),
        "--discard-uncommitted": ("flag", "discard_uncommitted"),
        "--force-branch": ("flag", "force_branch"),
        "--force": ("flag", "force"),
        "--keep-branch": ("flag", "keep_branch"),
    },
    "prune": {
        "--merged": ("flag", "merged"),
        "--older-than": ("non_negative_int", "older_than_days"),
        "--harness": ("str", "harness"),
        "--group": ("group", "group"),
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
    if kind == "group":
        options[attr] = validate_group(value, option=option)
    elif kind == "str":
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
        spec = action_specs.get(token)
        if spec is not None:
            i = _apply_worktree_option(options, args, i, token, spec)
            continue
        if token in MISPLACED_GLOBAL_OPTIONS:
            raise_misplaced_global_option(f"{token} must appear before the subcommand.")
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
        if options.get("group") is not None:
            if positional:
                raise DelegateError(
                    "invalid_option_combination",
                    "worktree remove accepts either --group NAME or a handle, not both.",
                )
        elif len(positional) != 1:
            raise DelegateError("missing_handle", "worktree remove requires an alias or run id.")
        else:
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


def parse_profiles(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    auth_profile: str | None,
) -> ParsedCommand:
    rest, json_mode = consume_json_option(rest, json_mode)
    if any(command_help.is_help_token(token) for token in rest):
        return help_command(json_mode, "profiles")
    require_no_extra(rest, "profiles")
    return ParsedCommand(
        "profiles",
        global_options=GlobalOptions(json_mode=json_mode, cwd=cwd, auth_profile=auth_profile),
        profiles_command=profile_commands.ProfilesCommand(json_mode=json_mode),
    )
