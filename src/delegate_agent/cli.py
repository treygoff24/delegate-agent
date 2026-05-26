#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, TextIO

try:
    from delegate_agent import VERSION, harness_events, run_registry, worktree_mgmt
    from delegate_agent import config as delegate_config
    from delegate_agent import rendering as delegate_rendering
    from delegate_agent import retention as delegate_retention
    from delegate_agent import runner as delegate_runner
    from delegate_agent.git_utils import GIT_MUTATION_TIMEOUT_SECONDS
    from delegate_agent.isolation import (
        IsolationContext,
        IsolationExecutionError,
        branch_label,
        build_isolation_context,
        compute_repo_fingerprint_from_common_dir,
        create_persistent_worktree,
        plan_branch_name,
        plan_worktree_path,
        prepend_persistent_worktree_context,
        require_clean_source,
        require_valid_head,
        short_run_id,
        worktrees_data_home,
    )
    from delegate_agent.json_types import JsonObject, JsonValue
except ModuleNotFoundError:  # pragma: no cover - direct cli.py invocation in tests
    _src_root = Path(__file__).resolve().parent.parent
    if str(_src_root) not in sys.path:
        sys.path.insert(0, str(_src_root))
    from delegate_agent import VERSION, harness_events, run_registry, worktree_mgmt
    from delegate_agent import config as delegate_config
    from delegate_agent import rendering as delegate_rendering
    from delegate_agent import retention as delegate_retention
    from delegate_agent import runner as delegate_runner
    from delegate_agent.git_utils import GIT_MUTATION_TIMEOUT_SECONDS
    from delegate_agent.isolation import (
        IsolationContext,
        IsolationExecutionError,
        branch_label,
        build_isolation_context,
        compute_repo_fingerprint_from_common_dir,
        create_persistent_worktree,
        plan_branch_name,
        plan_worktree_path,
        prepend_persistent_worktree_context,
        require_clean_source,
        require_valid_head,
        short_run_id,
        worktrees_data_home,
    )
    from delegate_agent.json_types import JsonObject, JsonValue


DEFAULT_CONFIG = delegate_config.DEFAULT_CONFIG
DEFAULT_CONFIG_PATH = delegate_config.DEFAULT_CONFIG_PATH
CONFIG_ENV = delegate_config.CONFIG_ENV

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_BINARY = 3

MODE_SAFE = "safe"
MODE_WORK = "work"
VALID_MODES = {MODE_SAFE, MODE_WORK}
RUN_INPUT_KEYS = {"engine", "mode", "model", "cwd", "prompt", "isolation"}
CURSOR_SAFE_REVIEW_PREFIX = (
    "Delegate review mode (code review/investigation only): "
    "Do not edit, create, or delete files. "
    "Report findings with file path, line reference, severity, and rationale. "
    "If a write is blocked, do not retry it.\n\n"
)
CODEX_SAFE_REVIEW_PREFIX = (
    "Delegate Codex safe mode (code review/investigation only): "
    "Do not edit, create, or delete files. "
    "Report findings with file path, line reference, severity, and rationale. "
    "If a write is blocked, do not retry it.\n\n"
)
# Project .cursor/cli.json is permissions-only; global cli-config examples may
# include other top-level keys such as "version", but Cursor rejects them here.
CURSOR_SAFE_CLI_CONFIG: JsonObject = {
    "permissions": {
        "allow": [
            "Read(**)",
            "Shell(rg)",
            "Shell(grep)",
            "Shell(cat)",
            "Shell(head)",
            "Shell(tail)",
            "Shell(wc)",
        ],
        "deny": [
            "Write(**)",
            "Shell(rm)",
            "Shell(mv)",
            "Shell(tee)",
            "Shell(curl)",
            "Shell(wget)",
            "Read(.env*)",
            "Read(**/.env*)",
            "Read(**/id_rsa*)",
            "Read(**/*.pem)",
        ],
    },
}


class DelegateError(Exception):
    def __init__(self, error: str, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code


@dataclass
class ParsedCommand:
    subcommand: str
    json_mode: bool = False
    cwd: str | None = None
    pass_through: bool = False
    completion_report: str | None = None
    engine: str | None = None
    mode: str | None = None
    model_alias: str | None = None
    prompt_parts: list[str] | None = None
    prompt_file: str | None = None
    input_json: str | None = None
    dry_run: bool = False
    snapshot_handle: str | None = None
    snapshot_latest_harness: str | None = None
    snapshot_no_redact: bool = False
    runs_active: bool = False
    runs_harness: str | None = None
    runs_limit: int | None = None
    run_output_handle: str | None = None
    run_output_completion_report: bool = False
    run_output_stdout: bool = False
    run_output_stderr: bool = False
    run_output_tail: int | None = None
    run_output_raw: bool = False
    run_output_no_redact: bool = False
    isolation: str | None = None
    worktree_action: str | None = None
    worktree_handle: str | None = None
    worktree_latest_harness: str | None = None
    worktree_harness: str | None = None
    worktree_status: str | None = None
    worktree_limit: int | None = None
    worktree_no_auto_prune: bool = False
    worktree_discard_uncommitted: bool = False
    worktree_force_branch: bool = False
    worktree_force: bool = False
    worktree_keep_branch: bool = False
    worktree_merged: bool = False
    worktree_older_than: int | None = None
    worktree_include_detached: bool = False
    worktree_dry_run: bool = False


@dataclass(frozen=True)
class ResolvedWorkspace:
    path: str
    kind: str


@dataclass
class Request:
    engine: str
    mode: str
    workspace: str
    prompt: str
    argv: list[str]
    model: str | None
    model_alias: str | None = None
    dry_run: bool = False
    workspace_kind: str = "git"
    isolation_context: IsolationContext | None = None


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


@dataclass(frozen=True)
class PersistentWorktreeRegistration:
    run_id: str
    alias: str
    run_path: Path
    branch: str
    worktree_path: str
    creation_context: JsonObject
    pre_ctx: delegate_runner.RunContext


HELP = f"""delegate {VERSION}

Usage:
  delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] cursor {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] droid MODEL_ALIAS {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] codex {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] dry-run cursor {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] dry-run droid MODEL_ALIAS {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] dry-run codex {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] run --input-json FILE
  delegate [--cwd PATH] [--json] snapshot [--latest HARNESS] [--no-redact] <alias-or-runId>
  delegate [--cwd PATH] [--json] runs [--active] [--recent] [--harness HARNESS] [--limit N]
  delegate [--cwd PATH] [--json] run-output <alias-or-runId> [--completion-report] [--stdout] [--stderr] [--tail N] [--raw] [--no-redact]
  delegate [--cwd PATH] [--json] worktree list [--harness HARNESS] [--status STATUS] [--limit N] [--no-auto-prune]
  delegate [--cwd PATH] [--json] worktree show <alias-or-runId>
  delegate [--cwd PATH] [--json] worktree show --latest HARNESS
  delegate [--cwd PATH] [--json] worktree remove <alias-or-runId> [--discard-uncommitted] [--force-branch] [--force] [--keep-branch]
  delegate [--cwd PATH] [--json] worktree prune [--merged] [--older-than DAYS] [--harness HARNESS] [--include-detached] [--dry-run] [--discard-uncommitted] [--force-branch] [--force]
  delegate [--cwd PATH] [--json] worktree gc [--dry-run]
  delegate [--json] models
  delegate [--json] describe
  delegate agent-help

Global options must appear before the subcommand.

Run output options (before subcommand):
  --pass-through              Stream raw child stdout/stderr (incompatible with --json)
  --completion-report MODE    markdown (default) or none
  --no-completion-report      Disable completion-report prompt injection

Tracked runs return bounded summaries by default. Avoid piping launches through tail;
inspect runs with delegate snapshot, delegate runs, and delegate run-output.
"""


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
                raise DelegateError(
                    "missing_isolation_value", "--isolation requires a value."
                )
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

    if subcommand == "models":
        require_no_extra(rest, "models")
        return ParsedCommand(
            "models",
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        )
    if subcommand == "describe":
        require_no_extra(rest, "describe")
        return ParsedCommand(
            "describe",
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        )
    if subcommand == "agent-help":
        require_no_extra(rest, "agent-help")
        return ParsedCommand(
            "agent-help",
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
        )
    if subcommand == "run":
        return parse_run(rest, json_mode, cwd, pass_through, completion_report, isolation)
    if subcommand in ("cursor", "codex"):
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


def require_no_extra(rest: list[str], name: str) -> None:
    if rest:
        if any(tok in ("--json", "--cwd", "--isolation") for tok in rest):
            raise DelegateError(
                "misplaced_global_option", "Global options must appear before the subcommand."
            )
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
    if len(rest) != 2 or rest[0] != "--input-json":
        if any(tok in ("--json", "--cwd", "--isolation") for tok in rest):
            raise DelegateError(
                "misplaced_global_option", "Global options must appear before the subcommand."
            )
        raise DelegateError("invalid_run_args", "run requires: --input-json FILE")
    return ParsedCommand(
        "run",
        json_mode=json_mode,
        cwd=cwd,
        input_json=rest[1],
        pass_through=pass_through,
        completion_report=completion_report,
        isolation=isolation,
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
) -> ParsedCommand:
    """Parse the shared cursor/codex grammar: <mode> [--prompt-file PATH] [prompt...]."""
    if not rest:
        raise DelegateError("missing_mode", f"{engine} requires mode: safe or work.")
    mode = rest[0]
    if mode.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    validate_mode(mode)
    prompt_file, prompt_parts = parse_prompt_tail(rest[1:])
    return ParsedCommand(
        engine,
        json_mode=json_mode,
        cwd=cwd,
        engine=engine,
        mode=mode,
        prompt_file=prompt_file,
        prompt_parts=prompt_parts,
        dry_run=dry_run,
        pass_through=pass_through,
        completion_report=completion_report,
        isolation=isolation,
    )


def parse_droid(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    dry_run: bool,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
) -> ParsedCommand:
    if len(rest) < 2:
        raise DelegateError("missing_droid_args", "droid requires MODEL_ALIAS and mode.")
    model_alias = rest[0]
    if model_alias.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    mode = rest[1]
    if mode.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    validate_mode(mode)
    prompt_file, prompt_parts = parse_prompt_tail(rest[2:])
    return ParsedCommand(
        "droid",
        json_mode=json_mode,
        cwd=cwd,
        engine="droid",
        mode=mode,
        model_alias=model_alias,
        prompt_file=prompt_file,
        prompt_parts=prompt_parts,
        dry_run=dry_run,
        pass_through=pass_through,
        completion_report=completion_report,
        isolation=isolation,
    )


def parse_dry_run(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
    isolation: str | None,
) -> ParsedCommand:
    if not rest:
        raise DelegateError("missing_engine", "dry-run requires cursor, droid, or codex.")
    engine = rest[0]
    if engine.startswith("-"):
        raise DelegateError(
            "misplaced_global_option", "Global options must appear before the subcommand."
        )
    if engine in ("cursor", "codex"):
        return parse_modeless_engine(
            engine,
            rest[1:],
            json_mode,
            cwd,
            dry_run=True,
            pass_through=pass_through,
            completion_report=completion_report,
            isolation=isolation,
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
        )
    raise DelegateError("invalid_engine", "dry-run engine must be cursor, droid, or codex.")


def parse_prompt_tail(rest: list[str]) -> tuple[str | None, list[str]]:
    prompt_file: str | None = None
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
        prompt_parts = rest[i:]
        break
    if "--prompt-file" in prompt_parts:
        raise DelegateError(
            "ambiguous_prompt_source", "--prompt-file must appear before direct prompt text."
        )
    if any(tok in ("--json", "--cwd", "--isolation") for tok in prompt_parts):
        raise DelegateError(
            "misplaced_global_option",
            "Global options must appear before the subcommand; use --prompt-file for literal flag text.",
        )
    return prompt_file, prompt_parts


def validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise DelegateError("invalid_mode", "Mode must be safe or work.")


def parse_snapshot(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
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
        json_mode=json_mode,
        cwd=cwd,
        snapshot_handle=handle,
        snapshot_latest_harness=latest_harness,
        snapshot_no_redact=no_redact,
    )


KNOWN_HARNESSES = ("cursor", "droid", "codex")


def parse_runs(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    active = False
    recent = False
    harness: str | None = None
    limit: int | None = None
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--active":
            active = True
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
            if i + 1 >= len(rest):
                raise DelegateError("missing_limit", "runs --limit requires a positive integer.")
            try:
                limit = int(rest[i + 1])
            except ValueError as exc:
                raise DelegateError(
                    "invalid_limit", "runs --limit must be a positive integer."
                ) from exc
            if limit < 1:
                raise DelegateError("invalid_limit", "runs --limit must be at least 1.")
            i += 2
            continue
        raise DelegateError("unknown_option", f"runs does not support option: {token}")
    if active and recent:
        raise DelegateError(
            "invalid_option_combination",
            "runs --active and --recent cannot be combined.",
        )
    return ParsedCommand(
        "runs",
        json_mode=json_mode,
        cwd=cwd,
        runs_active=active,
        runs_harness=harness,
        runs_limit=limit,
    )


def parse_run_output(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
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
            if i + 1 >= len(rest):
                raise DelegateError("missing_tail", "run-output --tail requires a line count.")
            try:
                tail = int(rest[i + 1])
            except ValueError as exc:
                raise DelegateError(
                    "invalid_tail", "run-output --tail must be a positive integer."
                ) from exc
            if tail < 1:
                raise DelegateError("invalid_tail", "run-output --tail must be at least 1.")
            i += 2
            continue
        raise DelegateError("unknown_option", f"run-output does not support option: {token}")
    if not (completion_report or stdout_flag or stderr_flag or raw):
        raise DelegateError(
            "missing_output_selector",
            "run-output requires --completion-report, --stdout, --stderr, or --raw.",
        )
    if raw and tail is not None:
        raise DelegateError(
            "invalid_option_combination",
            "run-output --raw cannot be combined with --tail.",
        )
    if (stdout_flag or stderr_flag) and not raw and tail is None:
        raise DelegateError(
            "missing_tail",
            "run-output --stdout/--stderr require --tail N or --raw.",
        )
    return ParsedCommand(
        "run-output",
        json_mode=json_mode,
        cwd=cwd,
        run_output_handle=handle,
        run_output_completion_report=completion_report,
        run_output_stdout=stdout_flag,
        run_output_stderr=stderr_flag,
        run_output_tail=tail,
        run_output_raw=raw,
        run_output_no_redact=no_redact,
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


def _require_option_value(rest: list[str], index: int, option: str) -> str:
    if index + 1 >= len(rest):
        raise DelegateError("missing_option_value", f"{option} requires a value.")
    return rest[index + 1]


WorktreeOptionSpec = tuple[str, str]


WORKTREE_OPTION_SPECS: dict[str, dict[str, WorktreeOptionSpec]] = {
    "list": {
        "--harness": ("str", "worktree_harness"),
        "--status": ("status", "worktree_status"),
        "--limit": ("positive_int", "worktree_limit"),
        "--no-auto-prune": ("flag", "worktree_no_auto_prune"),
    },
    "show": {
        "--latest": ("str", "worktree_latest_harness"),
    },
    "remove": {
        "--discard-uncommitted": ("flag", "worktree_discard_uncommitted"),
        "--force-branch": ("flag", "worktree_force_branch"),
        "--force": ("flag", "worktree_force"),
        "--keep-branch": ("flag", "worktree_keep_branch"),
    },
    "prune": {
        "--merged": ("flag", "worktree_merged"),
        "--older-than": ("non_negative_int", "worktree_older_than"),
        "--harness": ("str", "worktree_harness"),
        "--include-detached": ("flag", "worktree_include_detached"),
        "--dry-run": ("flag", "worktree_dry_run"),
        "--discard-uncommitted": ("flag", "worktree_discard_uncommitted"),
        "--force-branch": ("flag", "worktree_force_branch"),
        "--force": ("flag", "worktree_force"),
    },
    "gc": {
        "--dry-run": ("flag", "worktree_dry_run"),
    },
}


def _apply_worktree_option(
    parsed: ParsedCommand,
    args: list[str],
    index: int,
    option: str,
    spec: WorktreeOptionSpec,
) -> int:
    kind, attr = spec
    if kind == "flag":
        setattr(parsed, attr, True)
        return index + 1
    value = _require_option_value(args, index, option)
    if kind == "str":
        setattr(parsed, attr, value)
    elif kind == "status":
        if value not in worktree_mgmt.VALID_STATUSES:
            raise DelegateError(
                "invalid_option_value",
                "--status must be present, removed, missing, or unknown.",
            )
        setattr(parsed, attr, value)
    elif kind == "positive_int":
        setattr(parsed, attr, parse_positive_int(value, option=option))
    elif kind == "non_negative_int":
        setattr(parsed, attr, parse_non_negative_int(value, option=option))
    else:  # pragma: no cover - table construction bug
        raise AssertionError(f"unknown worktree option kind: {kind}")
    return index + 2


def parse_worktree(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    if not rest:
        raise DelegateError("missing_worktree_action", "worktree requires list, show, remove, prune, or gc.")
    action = rest[0]
    args = rest[1:]
    if action not in WORKTREE_OPTION_SPECS:
        raise DelegateError("unknown_worktree_action", f"Unknown worktree action: {action}")
    parsed = ParsedCommand(
        "worktree",
        json_mode=json_mode,
        cwd=cwd,
        worktree_action=action,
    )
    positional: list[str] = []
    i = 0
    action_specs = WORKTREE_OPTION_SPECS[action]
    while i < len(args):
        token = args[i]
        if token in {"--json", "--cwd", "--isolation"}:
            raise DelegateError(
                "misplaced_global_option",
                f"{token} must appear before the subcommand.",
            )
        spec = action_specs.get(token)
        if spec is not None:
            i = _apply_worktree_option(parsed, args, i, token, spec)
            continue
        if token.startswith("--"):
            raise DelegateError("unknown_option", f"worktree {action} does not support option: {token}")
        positional.append(token)
        i += 1

    if action in {"list", "prune", "gc"} and positional:
        raise DelegateError("unexpected_argument", f"worktree {action} does not accept positional arguments.")
    if action == "show":
        if parsed.worktree_latest_harness is not None:
            if positional:
                raise DelegateError(
                    "invalid_option_combination",
                    "worktree show accepts either --latest HARNESS or a handle, not both.",
                )
        elif len(positional) != 1:
            raise DelegateError("missing_handle", "worktree show requires an alias or run id.")
        else:
            parsed.worktree_handle = positional[0]
    if action == "remove":
        if len(positional) != 1:
            raise DelegateError("missing_handle", "worktree remove requires an alias or run id.")
        parsed.worktree_handle = positional[0]
    if parsed.worktree_keep_branch and (parsed.worktree_force_branch or parsed.worktree_force):
        raise DelegateError(
            "invalid_option_combination",
            "worktree remove --keep-branch is mutually exclusive with --force-branch/--force.",
        )
    return parsed


def maybe_run_retention_pass(registry_root: Path, config: JsonObject) -> None:
    delegate_retention.run_retention_pass(registry_root, config)


def resolve_run_target(
    registry_root: Path,
    *,
    handle: str | None,
    latest_harness: str | None,
) -> tuple[str, str | None]:
    index = run_registry.load_index(registry_root)
    if latest_harness is not None:
        run_id = run_registry.latest_run_id_for_harness(registry_root, index, latest_harness)
        if run_id is None:
            raise DelegateError(
                "no_matching_runs",
                f"No runs found for harness: {latest_harness}",
            )
        return run_id, run_registry.alias_for_run(index, run_id)
    assert handle is not None
    resolved = run_registry.resolve_handle(index, handle)
    if resolved.run_id is None:
        suggestions = ", ".join(resolved.suggestions) if resolved.suggestions else "(none)"
        raise DelegateError(
            "unknown_handle",
            f"Unknown run handle: {handle}. Suggestions: {suggestions}",
        )
    return resolved.run_id, resolved.alias


def _raise_no_registry_snapshot_error(parsed: ParsedCommand) -> NoReturn:
    if parsed.snapshot_latest_harness is not None:
        raise DelegateError(
            "no_matching_runs",
            f"No runs found for harness: {parsed.snapshot_latest_harness}",
        )
    handle = parsed.snapshot_handle
    assert handle is not None
    raise DelegateError(
        "unknown_handle",
        f"Unknown run handle: {handle}. Suggestions: (none)",
    )


def emit_snapshot(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    registry_root = run_registry.registry_root_if_exists(Path(workspace.path))
    if registry_root is None:
        _raise_no_registry_snapshot_error(parsed)
    run_id, _alias = resolve_run_target(
        registry_root,
        handle=parsed.snapshot_handle,
        latest_harness=parsed.snapshot_latest_harness,
    )
    snapshot = run_registry.load_run_snapshot(registry_root, run_id)
    view = delegate_rendering.merge_snapshot_view(
        registry_root,
        run_id,
        snapshot,
        redact=not parsed.snapshot_no_redact,
    )
    if parsed.json_mode:
        delegate_rendering.print_json(delegate_rendering.snapshot_json_payload(view), stdout)
    else:
        delegate_rendering.render_snapshot_text(view, stdout)
    return EXIT_OK


def emit_runs(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    registry_root = run_registry.registry_root_if_exists(Path(workspace.path))
    limit = parsed.runs_limit or run_registry.DEFAULT_RUNS_LIMIT
    mode = "active" if parsed.runs_active else "recent"
    if registry_root is None:
        summaries: list[JsonObject] = []
    else:
        index = run_registry.load_index(registry_root)
        summaries = run_registry.list_run_summaries(
            registry_root,
            index,
            active=parsed.runs_active,
            harness=parsed.runs_harness,
            limit=limit,
        )
    summaries = [delegate_rendering.redact_value(summary) for summary in summaries]
    if parsed.json_mode:
        delegate_rendering.print_json(
            delegate_rendering.runs_json_payload(summaries, limit=limit, mode=mode),
            stdout,
        )
    else:
        delegate_rendering.render_runs_text(summaries, stdout, mode=mode)
    return EXIT_OK


def _add_log_output_section(
    *,
    registry_root: Path,
    run_id: str,
    log_name: str,
    section_name: str,
    tail: int | None,
    raw: bool,
    sections: JsonObject,
    text_sections: dict[str, str],
) -> None:
    content, truncated = delegate_retention.read_log_output(
        registry_root,
        run_id,
        log_name,
        tail=tail,
        raw=raw,
    )
    sections[section_name] = {
        "bytes": delegate_retention.log_file_byte_size(registry_root, run_id, log_name),
        "truncated": truncated,
        "archived": delegate_retention.raw_logs_archived(registry_root, run_id),
    }
    text_sections[section_name] = content


def emit_run_output(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    registry_root = run_registry.registry_root_if_exists(Path(workspace.path))
    if registry_root is None:
        handle = parsed.run_output_handle
        assert handle is not None
        raise DelegateError(
            "unknown_handle",
            f"Unknown run handle: {handle}. Suggestions: (none)",
        )
    run_id, alias = resolve_run_target(
        registry_root,
        handle=parsed.run_output_handle,
        latest_harness=None,
    )
    run_path = run_registry.run_directory(registry_root, run_id)
    sections: JsonObject = {}
    text_sections: dict[str, str] = {}
    if parsed.run_output_completion_report:
        report_path = run_path / run_registry.COMPLETION_REPORT_FILE
        if not report_path.exists():
            raise DelegateError(
                "missing_completion_report",
                f"Completion report not found for run: {alias or run_id}",
            )
        text = report_path.read_text(encoding="utf-8", errors="replace")
        sections["completionReport"] = {"bytes": len(text.encode("utf-8"))}
        text_sections["completionReport"] = text
    log_flags = parsed.run_output_stdout or parsed.run_output_stderr or parsed.run_output_raw
    if log_flags:
        try:
            if parsed.run_output_stdout or parsed.run_output_raw:
                _add_log_output_section(
                    registry_root=registry_root,
                    run_id=run_id,
                    log_name=run_registry.STDOUT_LOG,
                    section_name="stdout",
                    tail=parsed.run_output_tail,
                    raw=parsed.run_output_raw,
                    sections=sections,
                    text_sections=text_sections,
                )
            if parsed.run_output_stderr or parsed.run_output_raw:
                _add_log_output_section(
                    registry_root=registry_root,
                    run_id=run_id,
                    log_name=run_registry.STDERR_LOG,
                    section_name="stderr",
                    tail=parsed.run_output_tail,
                    raw=parsed.run_output_raw,
                    sections=sections,
                    text_sections=text_sections,
                )
        except ValueError as exc:
            raise DelegateError("missing_tail", str(exc)) from exc
    if not parsed.run_output_no_redact:
        text_sections = {
            key: delegate_rendering.redact_string(text) for key, text in text_sections.items()
        }
    if parsed.json_mode:
        merged_sections: JsonObject = {}
        for key, meta in sections.items():
            entry = dict(meta)
            if key in text_sections:
                entry["content"] = text_sections[key]
            merged_sections[key] = entry
        payload = delegate_rendering.run_output_json_payload(
            alias=alias,
            run_id=run_id,
            sections=merged_sections,
        )
        delegate_rendering.print_json(payload, stdout)
    else:
        delegate_rendering.render_run_output_text(text_sections, stdout)
    return EXIT_OK


def _worktree_list_payload(
    parsed: ParsedCommand,
    registry_root: Path,
    config: JsonObject,
) -> JsonObject:
    auto_prune = worktree_mgmt.maybe_auto_prune(
        registry_root,
        config,
        no_auto_prune=parsed.worktree_no_auto_prune,
    )
    payload = worktree_mgmt.list_worktrees(
        registry_root,
        harness=parsed.worktree_harness,
        status=parsed.worktree_status,
        limit=parsed.worktree_limit or run_registry.DEFAULT_RUNS_LIMIT,
    )
    if auto_prune is not None:
        payload["autoPrune"] = auto_prune
        if auto_prune.get("ok") is False and auto_prune.get("skipped") is not True:
            payload["ok"] = False
            exit_code = auto_prune.get("exitCode")
            payload["exitCode"] = (
                exit_code
                if isinstance(exit_code, int)
                else worktree_mgmt.WORKTREE_ERROR_EXIT_CODE
            )
    return payload


def _worktree_show_payload(
    parsed: ParsedCommand,
    registry_root: Path,
    _config: JsonObject,
) -> JsonObject:
    return worktree_mgmt.show_worktree(
        registry_root,
        handle=parsed.worktree_handle,
        latest_harness=parsed.worktree_latest_harness,
    )


def _worktree_remove_payload(
    parsed: ParsedCommand,
    registry_root: Path,
    _config: JsonObject,
) -> JsonObject:
    assert parsed.worktree_handle is not None
    return worktree_mgmt.remove_worktree(
        registry_root,
        handle=parsed.worktree_handle,
        discard_uncommitted=parsed.worktree_discard_uncommitted,
        force_branch=parsed.worktree_force_branch,
        keep_branch=parsed.worktree_keep_branch,
        force=parsed.worktree_force,
    )


def _worktree_prune_payload(
    parsed: ParsedCommand,
    registry_root: Path,
    _config: JsonObject,
) -> JsonObject:
    return worktree_mgmt.prune_worktrees(
        registry_root,
        merged=parsed.worktree_merged,
        older_than_days=parsed.worktree_older_than,
        harness=parsed.worktree_harness,
        include_detached=parsed.worktree_include_detached,
        dry_run=parsed.worktree_dry_run,
        discard_uncommitted=parsed.worktree_discard_uncommitted,
        force_branch=parsed.worktree_force_branch,
        force=parsed.worktree_force,
    )


def _worktree_gc_payload(
    parsed: ParsedCommand,
    registry_root: Path,
    _config: JsonObject,
) -> JsonObject:
    return worktree_mgmt.gc_worktrees(
        registry_root,
        dry_run=parsed.worktree_dry_run,
    )


WorktreePayloadBuilder = Callable[[ParsedCommand, Path, JsonObject], JsonObject]
WorktreeTextRenderer = Callable[[JsonObject, TextIO], None]
WORKTREE_ACTION_DISPATCH: dict[str, tuple[WorktreePayloadBuilder, WorktreeTextRenderer]] = {
    "list": (_worktree_list_payload, delegate_rendering.render_worktree_list_text),
    "show": (_worktree_show_payload, delegate_rendering.render_worktree_show_text),
    "remove": (_worktree_remove_payload, delegate_rendering.render_worktree_remove_text),
    "prune": (_worktree_prune_payload, delegate_rendering.render_worktree_prune_text),
    "gc": (_worktree_gc_payload, delegate_rendering.render_worktree_gc_text),
}


def emit_worktree(
    parsed: ParsedCommand,
    workspace: ResolvedWorkspace,
    config: JsonObject,
    stdout: TextIO,
) -> int:
    registry_root = run_registry.registry_root_if_exists(Path(workspace.path))
    if registry_root is None:
        raise worktree_mgmt.WorktreeManagementError(
            {
                "ok": False,
                "code": "no_registry",
                "message": "No Delegate run registry exists in this workspace.",
                "sourceGitRoot": workspace.path,
                "nextActions": ["run a tracked delegate command first"],
                "retrySafe": False,
            }
        )
    action = parsed.worktree_action
    if action not in WORKTREE_ACTION_DISPATCH:
        raise DelegateError("unknown_worktree_action", f"Unknown worktree action: {action}")
    build_payload, render_text = WORKTREE_ACTION_DISPATCH[action]
    payload = build_payload(parsed, registry_root, config)
    if parsed.json_mode:
        delegate_rendering.print_json(payload, stdout)
    else:
        render_text(payload, stdout)
    if payload.get("ok") is False:
        exit_code = payload.get("exitCode")
        return exit_code if isinstance(exit_code, int) else EXIT_USAGE
    return EXIT_OK


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
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
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
    stdin_text = read_stdin_source(stdin)
    has_stdin = stdin_text is not None
    if sum(1 for present in (has_direct, has_prompt_file, has_stdin) if present) > 1:
        raise DelegateError(
            "ambiguous_prompt_source",
            "Use exactly one prompt source: direct args, --prompt-file, or stdin.",
        )
    if has_direct:
        return validate_prompt(direct)
    if has_prompt_file:
        assert prompt_file is not None
        path = Path(prompt_file).expanduser()
        try:
            return validate_prompt(path.read_text())
        except FileNotFoundError:
            raise DelegateError("prompt_file_not_found", f"Prompt file not found: {path}") from None
    if has_stdin:
        assert stdin_text is not None
        return validate_prompt(stdin_text)
    raise DelegateError(
        "missing_prompt", "Missing prompt; pass prompt text, --prompt-file, or stdin."
    )


def read_stdin_source(stdin: TextIO) -> str | None:
    if stdin.isatty():
        return None
    try:
        ready, _, _ = select.select([stdin], [], [], 0)
        if not ready:
            return None
    except (AttributeError, OSError, ValueError):
        pass
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
    if parsed.pass_through:
        return delegate_config.COMPLETION_REPORT_MODE_NONE
    if parsed.completion_report is not None:
        return parsed.completion_report
    return delegate_config.completion_report_default_mode(config)


def effective_prompt(
    prompt: str,
    *,
    engine: str = "",
    mode: str = "",
    completion_report_mode: str,
) -> str:
    prompt = delegate_runner.prepend_skill_review_instructions(prompt)
    if engine == "codex" and mode == MODE_SAFE and CODEX_SAFE_REVIEW_PREFIX not in prompt:
        # prepend_skill_review_instructions guarantees SKILL_REVIEW_PREFIX at index 0,
        # so the codex safe prefix slots in cleanly between skill-review and the user prompt.
        insert_at = len(delegate_runner.SKILL_REVIEW_PREFIX)
        prompt = prompt[:insert_at] + CODEX_SAFE_REVIEW_PREFIX + prompt[insert_at:]
    if completion_report_mode == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN:
        return delegate_runner.append_completion_report_instructions(prompt)
    return prompt


def request_from_parsed(parsed: ParsedCommand, config: JsonObject, stdin: TextIO) -> Request:
    validate_config(config)
    if parsed.subcommand == "run":
        return request_from_input_json(parsed, config)
    if parsed.engine not in ("cursor", "droid", "codex") or parsed.mode is None:
        raise DelegateError("invalid_command", "Command does not map to an execution request.")
    workspace = resolve_workspace(parsed.cwd)
    prompt = resolve_prompt(parsed.prompt_parts, parsed.prompt_file, stdin)

    # Capture git metadata for isolation planning (read-only, safe in dry-run too).
    git_root, git_common_dir, git_head_oid, git_head_ref, git_branch = capture_git_metadata(workspace.path)

    # Resolve effective isolation and build isolation context.
    try:
        effective_isolation = delegate_config.resolve_isolation(
            cli_value=parsed.isolation,
            loaded_config=config,
            engine=parsed.engine,
            mode=parsed.mode,
        )
    except delegate_config.InvalidIsolationError as exc:
        raise DelegateError("invalid_isolation", str(exc)) from exc

    isolation_context = build_isolation_context(
        source_workspace=workspace.path,
        resolved_isolation=effective_isolation,
        engine=parsed.engine,
        mode=parsed.mode,
        model_alias=parsed.model_alias,
        config=config,
        run_short_id="<short-run-id-placeholder>" if parsed.dry_run else None,
        source_git_root=git_root,
        source_git_common_dir=git_common_dir,
        source_head_oid=git_head_oid,
        source_head_ref=git_head_ref,
        source_branch=git_branch,
    )

    completion_report_mode = resolve_completion_report_mode(parsed, config)
    prompt = effective_prompt(
        prompt,
        engine=parsed.engine,
        mode=parsed.mode,
        completion_report_mode=completion_report_mode,
    )
    return build_request(
        parsed.engine,
        parsed.mode,
        parsed.model_alias,
        workspace,
        prompt,
        config,
        parsed.dry_run,
        stream_capture=not parsed.pass_through,
        isolation_context=isolation_context,
    )


def request_from_input_json(parsed: ParsedCommand, config: JsonObject) -> Request:
    assert parsed.input_json is not None
    path = Path(parsed.input_json).expanduser()
    try:
        raw: JsonValue = json.loads(path.read_text())
    except FileNotFoundError:
        raise DelegateError("input_json_not_found", f"Input JSON file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise DelegateError("invalid_input_json", f"Invalid input JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DelegateError("invalid_input_json", "Input JSON root must be an object.")

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
    if engine not in ("cursor", "droid", "codex"):
        raise DelegateError("invalid_engine", "engine must be cursor, droid, or codex.")
    if not isinstance(mode, str):
        raise DelegateError("invalid_mode", "mode must be safe or work.")
    validate_mode(mode)
    if not isinstance(prompt, str):
        raise DelegateError("invalid_prompt", "prompt must be a string.")
    model_alias = raw.get("model")
    if engine == "droid":
        if not isinstance(model_alias, str) or not model_alias:
            raise DelegateError("missing_model", "droid run input requires model alias.")
    elif engine == "codex":
        if model_alias is not None and not isinstance(model_alias, str):
            raise DelegateError("invalid_model", "model must be a string or null for codex.")
        if model_alias == "":
            raise DelegateError(
                "invalid_model", "model must be a non-empty string or omitted for codex."
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
    workspace = resolve_workspace(parsed.cwd, json_cwd)

    # Capture git metadata for isolation planning (read-only).
    git_root, git_common_dir, git_head_oid, git_head_ref, git_branch = capture_git_metadata(workspace.path)

    # Resolve effective isolation and build isolation context.
    try:
        effective_isolation = delegate_config.resolve_isolation(
            cli_value=parsed.isolation,
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
        run_short_id="<short-run-id-placeholder>" if parsed.dry_run else None,
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
        stream_capture=not parsed.pass_through,
        isolation_context=isolation_context,
    )


def build_request(
    engine: str,
    mode: str,
    model_alias: str | None,
    workspace: ResolvedWorkspace | str,
    prompt: str,
    config: JsonObject,
    dry_run: bool,
    *,
    stream_capture: bool = True,
    isolation_context: IsolationContext | None = None,
) -> Request:
    resolved = (
        workspace
        if isinstance(workspace, ResolvedWorkspace)
        else ResolvedWorkspace(workspace, "git")
    )
    if engine == "cursor":
        cursor = config["cursor"]
        model = cursor["defaultModel"]
        argv = build_cursor_argv(
            cursor["argvPrefix"], mode, resolved.path, model, prompt, stream_capture=stream_capture
        )
        return Request(engine, mode, resolved.path, prompt, argv, model, model_alias=None, dry_run=dry_run, workspace_kind=resolved.kind, isolation_context=isolation_context)
    if engine == "droid":
        droid = config["droid"]
        models = droid["models"]
        if model_alias not in models:
            raise DelegateError("invalid_alias", f"Unknown Droid model alias: {model_alias}")
        assert model_alias is not None
        model = models[model_alias]
        if model.startswith("replace-with-") or model in {
            "your-droid-model-id",
            "real-droid-model-id",
        }:
            raise DelegateError(
                "unconfigured_model",
                (
                    f"Droid model alias '{model_alias}' is still a placeholder. "
                    "Copy config.example.json to ~/.delegate/config.json and set a real Droid model ID."
                ),
            )
        argv = build_droid_argv(
            droid["binary"], mode, resolved.path, model, prompt, stream_capture=stream_capture
        )
        return Request(engine, mode, resolved.path, prompt, argv, model, model_alias=model_alias, dry_run=dry_run, workspace_kind=resolved.kind, isolation_context=isolation_context)
    if engine == "codex":
        codex = config["codex"]
        model: str | None
        if isinstance(model_alias, str) and model_alias:
            model = model_alias
        else:
            default_model = codex.get("defaultModel")
            model = default_model if isinstance(default_model, str) and default_model else None
        policy = delegate_config.effective_policy(config, engine="codex", mode=mode)
        argv = build_codex_argv(
            codex,
            mode,
            resolved.path,
            model,
            prompt,
            policy,
            workspace_kind=resolved.kind,
            stream_capture=stream_capture,
        )
        return Request(engine, mode, resolved.path, prompt, argv, model, model_alias=model_alias, dry_run=dry_run, workspace_kind=resolved.kind, isolation_context=isolation_context)
    raise DelegateError("invalid_engine", "engine must be cursor, droid, or codex.")


def prefix_cursor_safe_prompt(prompt: str) -> str:
    if prompt.startswith(CURSOR_SAFE_REVIEW_PREFIX):
        return prompt
    return f"{CURSOR_SAFE_REVIEW_PREFIX}{prompt}"


def replace_argv_after_flag(argv: list[str], flag: str, value: str) -> list[str]:
    updated = list(argv)
    for index, token in enumerate(updated):
        if token == flag and index + 1 < len(updated):
            updated[index + 1] = value
            return updated
    return updated


def replace_safe_workspace_arg(request: Request, isolated_workspace: str) -> list[str]:
    if request.engine == "cursor":
        return replace_argv_after_flag(request.argv, "--workspace", isolated_workspace)
    if request.engine == "codex":
        return replace_argv_after_flag(request.argv, "--cd", isolated_workspace)
    if request.engine == "droid":
        return replace_argv_after_flag(request.argv, "--cwd", isolated_workspace)
    return list(request.argv)


def replace_workspace_arg(request: Request, execution_workspace: str) -> list[str]:
    """Rewrite the workspace/--cwd argument for all engines."""
    if request.engine == "cursor":
        return replace_argv_after_flag(request.argv, "--workspace", execution_workspace)
    if request.engine == "codex":
        return replace_argv_after_flag(request.argv, "--cd", execution_workspace)
    if request.engine == "droid":
        return replace_argv_after_flag(request.argv, "--cwd", execution_workspace)
    return list(request.argv)


def capture_git_metadata(workspace_path: str) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Capture git metadata from a workspace path for isolation planning.

    Returns (git_root, git_common_dir, head_oid, head_ref, branch_name).
    All None if the workspace is not a git repo or git commands fail.
    This is read-only -- safe to call in dry-run.
    """
    try:
        root_result = subprocess.run(
            ["git", "-C", workspace_path, "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, check=False,
        )
        if root_result.returncode != 0:
            return None, None, None, None, None
        git_root = root_result.stdout.strip()

        common_result = subprocess.run(
            ["git", "-C", workspace_path, "rev-parse", "--git-common-dir"],
            text=True, capture_output=True, check=False,
        )
        git_common_dir = common_result.stdout.strip() if common_result.returncode == 0 else None
        # If common dir is relative, make it absolute relative to git root
        if git_common_dir and not git_common_dir.startswith("/"):
            git_common_dir = str(Path(git_root) / git_common_dir)

        oid_result = subprocess.run(
            ["git", "-C", workspace_path, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False,
        )
        head_oid = oid_result.stdout.strip() if oid_result.returncode == 0 else None

        ref_result = subprocess.run(
            ["git", "-C", workspace_path, "symbolic-ref", "--quiet", "HEAD"],
            text=True, capture_output=True, check=False,
        )
        head_ref = ref_result.stdout.strip() if ref_result.returncode == 0 else None

        branch_name = None
        if head_ref and head_ref.startswith("refs/heads/"):
            branch_name = head_ref[11:]

        return git_root, git_common_dir, head_oid, head_ref, branch_name
    except (FileNotFoundError, OSError):
        return None, None, None, None, None


def write_cursor_safe_project_config(workspace: Path) -> None:
    config_dir = workspace / ".cursor"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli.json").write_text(json.dumps(CURSOR_SAFE_CLI_CONFIG, indent=2) + "\n")


def read_git_tracked_diff(git_root: str) -> bytes:
    diff = subprocess.run(
        ["git", "-C", git_root, "diff", "HEAD", "--binary"],
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        stderr = diff.stderr.decode(errors="replace").strip()
        raise DelegateError("safe_workspace_sync_failed", f"Failed to read tracked diff: {stderr}")
    return diff.stdout


def apply_git_tracked_diff(worktree_path: str, diff: bytes) -> None:
    if not diff.strip():
        return
    applied = subprocess.run(
        ["git", "-C", worktree_path, "apply", "--whitespace=nowarn"],
        input=diff,
        capture_output=True,
        check=False,
    )
    if applied.returncode != 0:
        stderr = applied.stderr.decode(errors="replace").strip()
        raise DelegateError(
            "safe_workspace_sync_failed",
            f"Failed to apply tracked diff to isolated workspace: {stderr}",
        )


def mirror_path_preserving_symlinks(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        os.symlink(os.readlink(source), destination)
        return
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        return
    shutil.copy2(source, destination)


def sync_git_workspace_snapshot(git_root: str, worktree_path: str) -> None:
    apply_git_tracked_diff(worktree_path, read_git_tracked_diff(git_root))
    untracked = subprocess.run(
        ["git", "-C", git_root, "ls-files", "--others", "--exclude-standard"],
        text=True,
        capture_output=True,
        check=False,
    )
    if untracked.returncode != 0:
        raise DelegateError(
            "safe_workspace_sync_failed",
            f"Failed to list untracked files: {untracked.stderr.strip()}",
        )
    for relative in untracked.stdout.splitlines():
        if not relative:
            continue
        mirror_path_preserving_symlinks(Path(git_root) / relative, Path(worktree_path) / relative)


def discard_git_safe_workspace(
    git_root: str, worktree_path: str, temp_base: str, *, worktree_added: bool
) -> None:
    if worktree_added:
        remove_git_safe_workspace(git_root, worktree_path)
    shutil.rmtree(temp_base, ignore_errors=True)


def create_git_safe_workspace(git_root: str) -> tuple[str, str]:
    temp_base = tempfile.mkdtemp(prefix="delegate-safe-")
    worktree_path = str(Path(temp_base) / "wt")
    worktree_added = False
    try:
        added = subprocess.run(
            ["git", "-C", git_root, "worktree", "add", "--detach", worktree_path, "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        if added.returncode != 0:
            raise DelegateError(
                "safe_workspace_create_failed",
                f"Failed to create detached git worktree: {added.stderr.strip()}",
            )
        worktree_added = True
        sync_git_workspace_snapshot(git_root, worktree_path)
    except Exception:
        discard_git_safe_workspace(
            git_root, worktree_path, temp_base, worktree_added=worktree_added
        )
        raise
    return worktree_path, temp_base


def create_directory_safe_workspace(source_workspace: str) -> tuple[str, str]:
    temp_base = tempfile.mkdtemp(prefix="delegate-safe-")
    copy_path = str(Path(temp_base) / "copy")
    try:
        shutil.copytree(
            source_workspace,
            copy_path,
            ignore=shutil.ignore_patterns(".git"),
            dirs_exist_ok=True,
            symlinks=True,
        )
    except Exception:
        shutil.rmtree(temp_base, ignore_errors=True)
        raise
    return copy_path, temp_base


def remove_git_safe_workspace(git_root: str, worktree_path: str) -> None:
    subprocess.run(
        ["git", "-C", git_root, "worktree", "remove", "--force", worktree_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def cleanup_safe_isolated_workspace(
    *,
    git_root: str | None,
    isolated_workspace: str,
    temp_base: str,
) -> None:
    if git_root is not None:
        remove_git_safe_workspace(git_root, isolated_workspace)
    shutil.rmtree(temp_base, ignore_errors=True)


@contextmanager
def safe_isolated_request(request: Request) -> Iterator[Request]:
    """Context manager that creates a temporary isolated workspace for safe-mode runs.

    Respects the isolation context:
    - effective_isolation == "none": skip isolation, yield original request.
    - effective_isolation == "worktree": create temp git worktree (or dir copy
      for auto legacy fallback). For cursor, writes .cursor/cli.json in the
      isolated workspace only.
    """
    ctx = request.isolation_context
    effective = ctx.effective_isolation if ctx is not None else None

    # No isolation needed.
    if effective != "worktree":
        yield request
        return

    # Isolation is worktree — create temp workspace.
    isolation_mode = ctx.isolation_mode if ctx is not None else "auto"
    git_root = request.workspace if request.workspace_kind == "git" else None

    if git_root is not None:
        isolated_workspace, temp_base = create_git_safe_workspace(git_root)
    elif isolation_mode == "auto":
        # Legacy auto fallback for non-git cursor/codex safe: directory copy.
        isolated_workspace, temp_base = create_directory_safe_workspace(request.workspace)
    else:
        raise DelegateError(
            "worktree_requires_git",
            "--isolation worktree requires a Git workspace for safe mode.",
        )

    isolation = IsolationContext(
        source_workspace=request.workspace,
        effective_isolation=effective,
        isolation_mode=isolation_mode,
        isolation_lifecycle="temporary",
        preserved_workspace=False,
        source_git_root=git_root,
    )
    try:
        if request.engine == "cursor":
            write_cursor_safe_project_config(Path(isolated_workspace))
        yield Request(
            engine=request.engine,
            mode=request.mode,
            workspace=isolated_workspace,
            prompt=request.prompt,
            argv=replace_safe_workspace_arg(request, isolated_workspace),
            model=request.model,
            model_alias=request.model_alias,
            dry_run=request.dry_run,
            workspace_kind=request.workspace_kind,
            isolation_context=isolation,
        )
    finally:
        cleanup_safe_isolated_workspace(
            git_root=git_root,
            isolated_workspace=isolated_workspace,
            temp_base=temp_base,
        )


def build_cursor_argv(
    prefix: list[str],
    mode: str,
    workspace: str,
    model: str,
    prompt: str,
    *,
    stream_capture: bool = True,
) -> list[str]:
    argv = [*prefix, "--workspace", workspace, "-p", "--trust"]
    if mode == MODE_WORK:
        argv.extend(["--approve-mcps", "--force"])
    elif mode == MODE_SAFE:
        prompt = prefix_cursor_safe_prompt(prompt)
    else:
        validate_mode(mode)
    if stream_capture:
        argv.extend(["--model", model, "--print", "--output-format", "stream-json", prompt])
    else:
        argv.extend(["--model", model, "--output-format", "text", prompt])
    return argv


def build_droid_argv(
    binary: str,
    mode: str,
    workspace: str,
    model: str,
    prompt: str,
    *,
    stream_capture: bool = True,
) -> list[str]:
    argv = [binary, "exec", "--cwd", workspace]
    if mode == MODE_WORK:
        argv.append("--skip-permissions-unsafe")
    elif mode != MODE_SAFE:
        validate_mode(mode)
    if stream_capture:
        argv.extend(["--model", model, "--output-format", "stream-json", prompt])
    else:
        argv.extend(["--model", model, prompt])
    return argv


def build_codex_argv(
    codex: JsonObject,
    mode: str,
    workspace: str,
    model: str | None,
    prompt: str,
    policy: JsonObject,
    *,
    workspace_kind: str,
    stream_capture: bool = True,
) -> list[str]:
    binary = str(codex["binary"])
    argv = [binary]
    if policy.get("webSearch") is True:
        argv.append("--search")
    if policy.get("bypassApprovalsAndSandbox") is not True:
        argv.extend(["--ask-for-approval", "never"])
    if codex.get("profile"):
        argv.extend(["--profile", str(codex["profile"])])
    if model:
        argv.extend(["--model", model])
    argv.append("exec")
    argv.extend(["--cd", workspace])
    if codex.get("ignoreUserConfig") is True:
        argv.append("--ignore-user-config")
    if workspace_kind != "git":
        argv.append("--skip-git-repo-check")
    if policy.get("bypassApprovalsAndSandbox") is True:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        sandbox = codex["workSandbox"] if mode == MODE_WORK else "read-only"
        argv.extend(["--sandbox", str(sandbox)])
        if (
            mode == MODE_WORK
            and sandbox == "workspace-write"
            and policy.get("networkAccess") is True
        ):
            argv.extend(["-c", "sandbox_workspace_write.network_access=true"])
    if policy.get("bypassHookTrust") is True:
        argv.append("--dangerously-bypass-hook-trust")
    if stream_capture:
        argv.extend(["--color", "never", "--json"])
        if codex.get("ephemeral", True) is True:
            argv.append("--ephemeral")
    argv.append(prompt)
    return argv


def dry_run_payload(request: Request) -> JsonObject:
    payload: JsonObject = {
        "ok": True,
        "dryRun": True,
        "cwd": request.workspace,
        "workspaceKind": request.workspace_kind,
        "engine": request.engine,
        "mode": request.mode,
        "model": request.model,
        "argv": list(request.argv),
    }

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

        # For worktree isolation, add planned placeholders with real
        # computed paths when available.
        if ctx.effective_isolation == "worktree":
            planned_cwd = ctx.planned_execution_cwd or "<planned-worktree-path>"
            planned_branch = ctx.planned_branch or "<planned-branch>"
            payload["plannedExecutionCwd"] = planned_cwd
            payload["plannedBranch"] = planned_branch
            # Rewrite argv to show the planned workspace path, not the source.
            if request.engine == "cursor":
                payload["argv"] = replace_argv_after_flag(
                    payload["argv"], "--workspace", planned_cwd
                )
            elif request.engine == "codex":
                payload["argv"] = replace_argv_after_flag(
                    payload["argv"], "--cd", planned_cwd
                )
            elif request.engine == "droid":
                payload["argv"] = replace_argv_after_flag(
                    payload["argv"], "--cwd", planned_cwd
                )
        else:
            payload["plannedExecutionCwd"] = None
            payload["plannedBranch"] = None

        # Always emit isolatedWorkspace as explicit boolean (mirrors preservedWorkspace).
        payload["isolatedWorkspace"] = ctx.isolation_lifecycle in ("temporary", "persistent")
    else:
        # Fallback when no isolation context is provided (e.g. direct build_request calls in tests).
        # Use embedded-default logic: cursor/codex safe -> worktree temporary, others -> none.
        if request.engine in ("cursor", "codex") and request.mode == MODE_SAFE:
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


def _cleanup_partial_worktree(
    source_git_root: str,
    worktree_path: str,
    branch: str,
    run_path: Path,
    *,
    remove_branch: bool = True,
) -> None:
    """Attempt to clean up a partially-created worktree after creation failure.

    If cleanup is unsafe or fails, preserve the path, record it in the
    snapshot, and print manual cleanup instructions to stderr.
    """
    path = Path(worktree_path)
    cleanup_failed = False
    if path.exists() or path.is_symlink():
        try:
            result = subprocess.run(
                ["git", "-C", source_git_root, "worktree", "remove", "--force", worktree_path],
                text=True, capture_output=True, check=False,
                timeout=GIT_MUTATION_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                cleanup_failed = True
        except (OSError, subprocess.SubprocessError):
            cleanup_failed = True
    if remove_branch:
        # Try to delete the branch even if worktree removal failed. Callers
        # must only set remove_branch when this run actually created the
        # branch; branch-collision cleanup must not delete pre-existing refs.
        try:
            subprocess.run(
                ["git", "-C", source_git_root, "branch", "-D", branch],
                text=True, capture_output=True, check=False,
                timeout=GIT_MUTATION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            cleanup_failed = True
    if cleanup_failed:
        commands = [
            shlex.join(["git", "-C", source_git_root, "worktree", "remove", "--force", worktree_path])
        ]
        if remove_branch:
            commands.append(shlex.join(["git", "-C", source_git_root, "branch", "-D", branch]))
        manual = " && ".join(commands)
        snapshot_path = run_path / run_registry.SNAPSHOT_FILE
        if snapshot_path.exists():
            try:
                existing = run_registry.read_json_object(snapshot_path)
                if existing is not None:
                    existing["cleanupFailed"] = True
                    existing["manualCleanup"] = manual
                    run_registry.write_json_atomic(snapshot_path, existing)
            except (OSError, ValueError):
                pass
        print(
            f"warning: partial worktree cleanup failed; manual cleanup required: {manual}",
            file=sys.stderr,
        )


def _validate_persistent_worktree_request(
    request: Request,
    *,
    config: JsonObject,
    pass_through: bool,
    source_workspace: ResolvedWorkspace,
) -> PersistentWorktreePreflight:
    """Validate a persistent worktree request before registering a run."""
    iso_ctx = request.isolation_context
    assert iso_ctx is not None

    if request.workspace_kind != "git":
        raise DelegateError(
            "worktree_requires_git",
            "--isolation worktree requires a Git workspace.",
        )
    source_git_root = request.workspace

    try:
        base_oid = require_valid_head(source_git_root)
    except IsolationExecutionError as exc:
        raise DelegateError(exc.error, exc.message) from exc
    (
        _current_git_root,
        current_git_common_dir,
        current_head_oid,
        current_head_ref,
        current_branch,
    ) = capture_git_metadata(source_git_root)
    current_head_oid = base_oid

    if pass_through:
        raise DelegateError(
            "pass_through_with_persistent_isolation",
            "--pass-through is not supported with persistent worktree runs (work mode + effective worktree isolation).",
        )

    registry_root = run_registry.ensure_registry(
        Path(source_workspace.path),
        workspace_kind=source_workspace.kind,
    )
    maybe_run_retention_pass(registry_root, config)

    try:
        require_clean_source(source_git_root)
    except IsolationExecutionError as exc:
        raise DelegateError(exc.error, exc.message) from exc

    return PersistentWorktreePreflight(
        iso_ctx=iso_ctx,
        source_git_root=source_git_root,
        base_oid=base_oid,
        source_git_common_dir=current_git_common_dir or iso_ctx.source_git_common_dir,
        source_head_oid=current_head_oid,
        source_head_ref=current_head_ref,
        source_branch=current_branch,
        registry_root=registry_root,
    )


def _build_persistent_worktree_run_context(
    request: Request,
    source_workspace: ResolvedWorkspace,
    preflight: PersistentWorktreePreflight,
    *,
    run_id: str,
    alias: str,
    branch: str,
    worktree_path: str,
    creation_context: JsonObject,
) -> delegate_runner.RunContext:
    iso_ctx = preflight.iso_ctx
    return delegate_runner.RunContext(
        registry_root=preflight.registry_root,
        run_id=run_id,
        alias=alias,
        harness=request.engine,
        engine=request.engine,
        mode=request.mode,
        model=request.model,
        source_cwd=source_workspace.path,
        execution_cwd=worktree_path,
        workspace_kind=source_workspace.kind,
        isolated_workspace=True,
        started_at=run_registry.utc_now_iso(),
        creation_context=creation_context,
        source_git_root=iso_ctx.source_git_root or preflight.source_git_root,
        isolation_mode=iso_ctx.isolation_mode,
        effective_isolation=iso_ctx.effective_isolation,
        isolation_lifecycle=iso_ctx.isolation_lifecycle,
        preserved_workspace=iso_ctx.preserved_workspace,
        branch=branch,
        worktree_status="present",
    )


def _register_persistent_worktree_run(
    request: Request,
    *,
    config: JsonObject,
    source_workspace: ResolvedWorkspace,
    preflight: PersistentWorktreePreflight,
) -> PersistentWorktreeRegistration:
    """Register a persistent worktree run and write pre-launch state."""
    label = branch_label(request.engine, request.model_alias)

    run_id, alias = run_registry.register_run(
        preflight.registry_root,
        harness=request.engine,
        metadata={
            "mode": request.mode,
            "model": request.model,
            "cwd": source_workspace.path,
        },
    )

    short_id = short_run_id(run_id)
    branch = plan_branch_name(label, short_id)
    dh = worktrees_data_home(config)

    source_git_common_dir = preflight.source_git_common_dir
    if source_git_common_dir is None:
        raise DelegateError(
            "worktree_requires_git",
            "--isolation worktree could not determine the Git common directory.",
        )

    fingerprint = compute_repo_fingerprint_from_common_dir(source_git_common_dir)
    worktree_path = str(plan_worktree_path(dh, fingerprint, label, short_id))

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
    }

    pre_ctx = _build_persistent_worktree_run_context(
        request,
        source_workspace,
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
        run_path, delegate_runner.build_manifest(pre_ctx, request.argv)
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


def _record_persistent_worktree_creation_failure(
    registration: PersistentWorktreeRegistration,
    exc: IsolationExecutionError,
) -> None:
    """Write inspectable state/snapshot for a pre-launch worktree failure."""
    failed_state = delegate_runner.build_state(
        registration.pre_ctx,
        status="failed",
        extra={
            "error": exc.error,
            "message": exc.message,
            "plannedBranch": registration.branch,
            "plannedExecutionCwd": registration.worktree_path,
        },
    )
    delegate_runner.write_state(registration.run_path, failed_state)

    failed_snapshot = delegate_runner.build_snapshot(
        registration.pre_ctx,
        accumulator=harness_events.StreamAccumulator(),
    )
    failed_snapshot["ok"] = False
    failed_snapshot["error"] = exc.error
    failed_snapshot["message"] = exc.message
    failed_snapshot["status"] = "failed"
    failed_snapshot["plannedBranch"] = registration.branch
    failed_snapshot["plannedExecutionCwd"] = registration.worktree_path
    for key in ("executionCwd", "worktreeStatus", "worktreeCleanupCommands", "branch"):
        failed_snapshot.pop(key, None)
    delegate_runner.write_snapshot(registration.run_path, failed_snapshot)


def _create_persistent_worktree_or_record_failure(
    preflight: PersistentWorktreePreflight,
    registration: PersistentWorktreeRegistration,
) -> None:
    """Create the git worktree or persist enough failure metadata to inspect it."""
    try:
        create_persistent_worktree(
            preflight.source_git_root,
            registration.branch,
            registration.worktree_path,
            preflight.base_oid,
        )
    except IsolationExecutionError as exc:
        _record_persistent_worktree_creation_failure(registration, exc)
        if exc.error != "branch_collision":
            _cleanup_partial_worktree(
                preflight.source_git_root,
                registration.worktree_path,
                registration.branch,
                registration.run_path,
                remove_branch=True,
            )
        raise DelegateError(exc.error, exc.message) from exc


def _launch_child_in_persistent_worktree(
    request: Request,
    json_mode: bool,
    *,
    completion_report_mode: str,
    source_workspace: ResolvedWorkspace,
    stdout: TextIO,
    stderr: TextIO,
    preflight: PersistentWorktreePreflight,
    registration: PersistentWorktreeRegistration,
) -> tuple[int, JsonObject | None]:
    """Rewrite the child request into the worktree and execute it as tracked."""
    try:
        execution_argv = replace_workspace_arg(request, registration.worktree_path)
        ensure_binary(execution_argv)
        execution_prompt = prepend_persistent_worktree_context(request.prompt)
        execution_argv[-1] = execution_prompt

        exec_ctx = _build_persistent_worktree_run_context(
            request,
            source_workspace,
            preflight,
            run_id=registration.run_id,
            alias=registration.alias,
            branch=registration.branch,
            worktree_path=registration.worktree_path,
            creation_context=registration.creation_context,
        )
        delegate_runner.write_manifest(
            registration.run_path, delegate_runner.build_manifest(exec_ctx, execution_argv)
        )
        exit_code, payload = delegate_runner.execute_tracked(
            execution_argv,
            registration.worktree_path,
            exec_ctx,
            json_mode=json_mode,
            stdout=stdout,
            stderr=stderr,
            completion_report_mode=completion_report_mode,
        )
    except Exception as exc:
        error_msg = str(exc)
        error_code = getattr(exc, "error", "execution_failed")
        delegate_runner.write_state(
            registration.run_path,
            delegate_runner.build_state(
                registration.pre_ctx,
                status="failed",
                extra={
                    "error": error_code,
                    "message": error_msg,
                    "plannedBranch": registration.branch,
                    "plannedExecutionCwd": registration.worktree_path,
                },
            ),
        )
        raise DelegateError(error_code, error_msg) from exc

    run_registry.set_worktree_status(
        preflight.registry_root,
        registration.run_id,
        "present",
    )

    return exit_code, payload


def _execute_persistent_worktree(
    request: Request,
    json_mode: bool,
    *,
    config: JsonObject,
    pass_through: bool,
    completion_report_mode: str,
    source_workspace: ResolvedWorkspace,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[int, JsonObject | None]:
    """Launch a work-mode child in a preserved Delegate-managed git worktree."""
    preflight = _validate_persistent_worktree_request(
        request,
        config=config,
        pass_through=pass_through,
        source_workspace=source_workspace,
    )
    registration = _register_persistent_worktree_run(
        request,
        config=config,
        source_workspace=source_workspace,
        preflight=preflight,
    )
    _create_persistent_worktree_or_record_failure(preflight, registration)
    return _launch_child_in_persistent_worktree(
        request,
        json_mode,
        completion_report_mode=completion_report_mode,
        source_workspace=source_workspace,
        stdout=stdout,
        stderr=stderr,
        preflight=preflight,
        registration=registration,
    )


def ensure_binary(argv: list[str]) -> None:
    if not argv:
        raise DelegateError("missing_binary", "Empty argv.", EXIT_MISSING_BINARY)
    if shutil.which(argv[0]) is None:
        raise DelegateError("missing_binary", f"Missing binary: {argv[0]}", EXIT_MISSING_BINARY)


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
    else:
        isolation_mode = "none"
        effective_isolation = "none"
        isolation_lifecycle = "none"
        preserved_workspace = False
        branch = None
        source_git_root = None

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
    )


def execute_request(
    request: Request,
    json_mode: bool,
    *,
    config: JsonObject,
    pass_through: bool,
    completion_report_mode: str,
    source_workspace: ResolvedWorkspace,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[int, JsonObject | None]:
    ctx = request.isolation_context

    # --- Persistent worktree path (work + worktree) ---
    if ctx is not None and ctx.isolation_lifecycle == "persistent":
        return _execute_persistent_worktree(
            request,
            json_mode,
            config=config,
            pass_through=pass_through,
            completion_report_mode=completion_report_mode,
            source_workspace=source_workspace,
            stdout=stdout,
            stderr=stderr,
        )

    with safe_isolated_request(request) as isolated_request:
        ensure_binary(isolated_request.argv)
        if pass_through:
            if json_mode:
                raise DelegateError(
                    "invalid_option_combination",
                    "--pass-through is incompatible with --json.",
                )
            exit_code = delegate_runner.execute_passthrough(
                isolated_request.argv,
                isolated_request.workspace,
            )
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
        return delegate_runner.execute_tracked(
            isolated_request.argv,
            isolated_request.workspace,
            ctx_runner,
            json_mode=json_mode,
            stdout=stdout,
            stderr=stderr,
            completion_report_mode=completion_report_mode,
        )


def models_payload(config: JsonObject, config_source: str) -> JsonObject:
    return {
        "ok": True,
        "configSource": config_source,
        "cursor": {
            "defaultModel": config["cursor"]["defaultModel"],
            "argvPrefix": config["cursor"]["argvPrefix"],
        },
        "droid": {"models": config["droid"]["models"]},
        "codex": {
            "binary": config["codex"]["binary"],
            "defaultModel": config["codex"]["defaultModel"],
            "profile": config["codex"]["profile"],
        },
    }


def _policy_field_support_matrix() -> JsonObject:
    supported = {
        "networkAccess": True,
        "webSearch": True,
        "bypassApprovalsAndSandbox": True,
        "bypassHookTrust": True,
    }
    unsupported = {key: False for key in delegate_config.POLICY_MODE_KEYS}
    return {
        "codex": supported,
        "cursor": unsupported,
        "droid": unsupported,
    }


def _codex_describe_model(codex: JsonObject) -> str | None:
    default_model = codex.get("defaultModel")
    return default_model if isinstance(default_model, str) and default_model else None


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
    )


def describe_payload(config: JsonObject, config_source: str) -> JsonObject:
    codex = config["codex"]
    codex_safe_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_SAFE)
    codex_work_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_WORK)
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
    return {
        "ok": True,
        "version": VERSION,
        "configPath": str(config_path()),
        "configSource": config_source,
        "engines": ["cursor", "droid", "codex"],
        "policyProfiles": list(delegate_config.POLICY_PROFILES),
        "policyFieldSupport": _policy_field_support_matrix(),
        "effectivePolicy": {
            "codex": {
                "safe": codex_safe_policy,
                "work": codex_work_policy,
            },
        },
        "modes": [MODE_SAFE, MODE_WORK],
        "promptSources": ["direct", "prompt-file", "stdin"],
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
                    "<workspace>",
                    "--model",
                    "<model-id>",
                    "--output-format",
                    "stream-json",
                    "<skill-review-prompt>",
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
                    "<skill-review-prompt>",
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
        },
    }


def emit_models(config: JsonObject, config_source: str, json_mode: bool, stdout: TextIO) -> int:
    if json_mode:
        delegate_rendering.print_json(models_payload(config, config_source), stdout)
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
    return EXIT_OK


def emit_describe(config: JsonObject, config_source: str, json_mode: bool, stdout: TextIO) -> int:
    payload = describe_payload(config, config_source)
    if json_mode:
        delegate_rendering.print_json(payload, stdout)
        return EXIT_OK
    print(f"delegate {VERSION}", file=stdout)
    print(f"config: {payload['configPath']} ({payload['configSource']})", file=stdout)
    print("engines: cursor, droid, codex", file=stdout)
    print("modes: safe, work", file=stdout)
    print("prompt sources: direct, --prompt-file, stdin", file=stdout)
    print("global options must appear before the subcommand", file=stdout)
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

Codex:
  - Model selection uses codex.defaultModel in config or optional JSON input model; no CLI model alias in v1.
  - Codex profile (codex.profile) is config-only; run input JSON must not include profile.

Droid work mode:
  - Droid safe mode remains read-only: no --auto, --use-spec, or unsafe skip.
  - Uses Factory Droid --skip-permissions-unsafe, not --auto high.
  - This is intentionally no-prompt; use only for bounded tasks in workspaces you trust.

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
    try:
        raw: JsonValue = json.loads(path.read_text())
    except FileNotFoundError:
        raise DelegateError("input_json_not_found", f"Input JSON file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise DelegateError("invalid_input_json", f"Invalid input JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DelegateError("invalid_input_json", "Input JSON root must be an object.")

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
        delegate_rendering.print_json(
            {
                "ok": False,
                "error": error.error,
                "message": error.message,
                "exitCode": error.exit_code,
            },
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
        json_mode = parsed.json_mode
        if parsed.subcommand == "help":
            print(HELP, file=stdout)
            return EXIT_OK
        if parsed.subcommand == "version":
            print(VERSION, file=stdout)
            return EXIT_OK

        # For run --input-json, pre-read the JSON to discover config from the
        # JSON-resolved workspace before loading/finalizing config.
        if parsed.subcommand == "run":
            assert parsed.input_json is not None
            workspace, config, source = pre_read_run_json_for_config(
                parsed.input_json, parsed.cwd
            )
        else:
            config, source = load_config(workspace=workspace_path_for_config(parsed.cwd))
            validate_config(config)

        if parsed.subcommand == "models":
            return emit_models(config, source, parsed.json_mode, stdout)
        if parsed.subcommand == "describe":
            return emit_describe(config, source, parsed.json_mode, stdout)
        if parsed.subcommand == "agent-help":
            return emit_agent_help(stdout)

        if parsed.subcommand != "run":
            workspace = resolve_workspace(parsed.cwd)
            # (non-run path uses workspace resolved above)

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
            if parsed.json_mode:
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
            parsed.json_mode,
            config=config,
            pass_through=parsed.pass_through,
            completion_report_mode=completion_report_mode,
            source_workspace=workspace,
            stdout=stdout,
            stderr=stderr,
        )
        if parsed.json_mode and payload is not None:
            delegate_rendering.print_json(payload, stdout)
        return exit_code
    except worktree_mgmt.WorktreeManagementError as exc:
        if json_mode:
            delegate_rendering.print_json(exc.payload, stdout)
        else:
            print(f"{exc.code}: {exc.message}", file=stderr)
        exit_code = exc.payload.get("exitCode")
        return exit_code if isinstance(exit_code, int) else EXIT_USAGE
    except DelegateError as exc:
        return emit_error(exc, json_mode, stdout, stderr)


if __name__ == "__main__":
    raise SystemExit(main())
