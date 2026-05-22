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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

try:
    from delegate_agent import config as delegate_config
    from delegate_agent import rendering as delegate_rendering
    from delegate_agent import retention as delegate_retention
    from delegate_agent import run_registry
    from delegate_agent import runner as delegate_runner
    from delegate_agent.json_types import JsonObject, JsonValue
except ModuleNotFoundError:  # pragma: no cover - direct cli.py invocation in tests
    _src_root = Path(__file__).resolve().parent.parent
    if str(_src_root) not in sys.path:
        sys.path.insert(0, str(_src_root))
    from delegate_agent import config as delegate_config
    from delegate_agent import rendering as delegate_rendering
    from delegate_agent import retention as delegate_retention
    from delegate_agent import run_registry
    from delegate_agent import runner as delegate_runner
    from delegate_agent.json_types import JsonObject, JsonValue


VERSION = "0.1.2"
DEFAULT_CONFIG = delegate_config.DEFAULT_CONFIG
DEFAULT_CONFIG_PATH = delegate_config.DEFAULT_CONFIG_PATH
CONFIG_ENV = delegate_config.CONFIG_ENV

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_BINARY = 3

MODE_SAFE = "safe"
MODE_WORK = "work"
VALID_MODES = {MODE_SAFE, MODE_WORK}
RUN_INPUT_KEYS = {"engine", "mode", "model", "cwd", "prompt"}
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


@dataclass(frozen=True)
class ResolvedWorkspace:
    path: str
    kind: str


@dataclass(frozen=True)
class SafeIsolationContext:
    source_workspace: str
    execution_workspace: str


@dataclass
class Request:
    engine: str
    mode: str
    workspace: str
    prompt: str
    argv: list[str]
    model: str | None
    dry_run: bool = False
    workspace_kind: str = "git"
    safe_isolation: SafeIsolationContext | None = None


HELP = f"""delegate {VERSION}

Usage:
  delegate [--cwd PATH] [--json] cursor {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] droid MODEL_ALIAS {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] codex {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] dry-run cursor {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] dry-run droid MODEL_ALIAS {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] dry-run codex {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] run --input-json FILE
  delegate [--cwd PATH] [--json] snapshot [--latest HARNESS] [--no-redact] <alias-or-runId>
  delegate [--cwd PATH] [--json] runs [--active] [--recent] [--harness HARNESS] [--limit N]
  delegate [--cwd PATH] [--json] run-output <alias-or-runId> [--completion-report] [--stdout] [--stderr] [--tail N] [--raw] [--no-redact]
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
        )
    if subcommand == "describe":
        require_no_extra(rest, "describe")
        return ParsedCommand(
            "describe",
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
        )
    if subcommand == "agent-help":
        require_no_extra(rest, "agent-help")
        return ParsedCommand(
            "agent-help",
            json_mode=json_mode,
            cwd=cwd,
            pass_through=pass_through,
            completion_report=completion_report,
        )
    if subcommand == "run":
        return parse_run(rest, json_mode, cwd, pass_through, completion_report)
    if subcommand == "cursor":
        return parse_cursor(
            rest,
            json_mode,
            cwd,
            dry_run=False,
            pass_through=pass_through,
            completion_report=completion_report,
        )
    if subcommand == "droid":
        return parse_droid(
            rest,
            json_mode,
            cwd,
            dry_run=False,
            pass_through=pass_through,
            completion_report=completion_report,
        )
    if subcommand == "codex":
        return parse_codex(
            rest,
            json_mode,
            cwd,
            dry_run=False,
            pass_through=pass_through,
            completion_report=completion_report,
        )
    if subcommand == "dry-run":
        return parse_dry_run(rest, json_mode, cwd, pass_through, completion_report)
    if subcommand == "snapshot":
        return parse_snapshot(rest, json_mode, cwd)
    if subcommand == "runs":
        return parse_runs(rest, json_mode, cwd)
    if subcommand == "run-output":
        return parse_run_output(rest, json_mode, cwd)

    raise DelegateError("unknown_subcommand", f"Unknown subcommand: {subcommand}")


def require_no_extra(rest: list[str], name: str) -> None:
    if rest:
        if any(tok in ("--json", "--cwd") for tok in rest):
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
) -> ParsedCommand:
    if len(rest) != 2 or rest[0] != "--input-json":
        if any(tok in ("--json", "--cwd") for tok in rest):
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
    )


def parse_cursor(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    dry_run: bool,
    pass_through: bool,
    completion_report: str | None,
) -> ParsedCommand:
    if not rest:
        raise DelegateError("missing_mode", "cursor requires mode: safe or work.")
    mode = rest[0]
    validate_mode(mode)
    prompt_file, prompt_parts = parse_prompt_tail(rest[1:])
    return ParsedCommand(
        "cursor",
        json_mode=json_mode,
        cwd=cwd,
        engine="cursor",
        mode=mode,
        prompt_file=prompt_file,
        prompt_parts=prompt_parts,
        dry_run=dry_run,
        pass_through=pass_through,
        completion_report=completion_report,
    )


def parse_droid(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    dry_run: bool,
    pass_through: bool,
    completion_report: str | None,
) -> ParsedCommand:
    if len(rest) < 2:
        raise DelegateError("missing_droid_args", "droid requires MODEL_ALIAS and mode.")
    model_alias = rest[0]
    mode = rest[1]
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
    )


def parse_codex(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    dry_run: bool,
    pass_through: bool,
    completion_report: str | None,
) -> ParsedCommand:
    if not rest:
        raise DelegateError("missing_mode", "codex requires mode: safe or work.")
    mode = rest[0]
    validate_mode(mode)
    prompt_file, prompt_parts = parse_prompt_tail(rest[1:])
    return ParsedCommand(
        "codex",
        json_mode=json_mode,
        cwd=cwd,
        engine="codex",
        mode=mode,
        prompt_file=prompt_file,
        prompt_parts=prompt_parts,
        dry_run=dry_run,
        pass_through=pass_through,
        completion_report=completion_report,
    )


def parse_dry_run(
    rest: list[str],
    json_mode: bool,
    cwd: str | None,
    pass_through: bool,
    completion_report: str | None,
) -> ParsedCommand:
    if not rest:
        raise DelegateError("missing_engine", "dry-run requires cursor, droid, or codex.")
    engine = rest[0]
    if engine == "cursor":
        return parse_cursor(
            rest[1:],
            json_mode,
            cwd,
            dry_run=True,
            pass_through=pass_through,
            completion_report=completion_report,
        )
    if engine == "droid":
        return parse_droid(
            rest[1:],
            json_mode,
            cwd,
            dry_run=True,
            pass_through=pass_through,
            completion_report=completion_report,
        )
    if engine == "codex":
        return parse_codex(
            rest[1:],
            json_mode,
            cwd,
            dry_run=True,
            pass_through=pass_through,
            completion_report=completion_report,
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
    if any(tok in ("--json", "--cwd") for tok in prompt_parts):
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


def registry_for_workspace(workspace: ResolvedWorkspace) -> Path:
    return run_registry.ensure_registry(Path(workspace.path), workspace_kind=workspace.kind)


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


def emit_snapshot(parsed: ParsedCommand, workspace: ResolvedWorkspace, stdout: TextIO) -> int:
    registry_root = registry_for_workspace(workspace)
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
    registry_root = registry_for_workspace(workspace)
    index = run_registry.load_index(registry_root)
    limit = parsed.runs_limit or run_registry.DEFAULT_RUNS_LIMIT
    mode = "active" if parsed.runs_active else "recent"
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
    registry_root = registry_for_workspace(workspace)
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
            key: (
                text
                if parsed.run_output_raw and key in ("stdout", "stderr")
                else delegate_rendering.redact_string(text)
            )
            for key, text in text_sections.items()
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
    if engine == "codex" and mode == MODE_SAFE:
        # Inject Codex safe prefix immediately after the skill-review block.
        # Anchor to the literal SKILL_REVIEW_PREFIX if present (simple suffix match).
        marker = delegate_runner.SKILL_REVIEW_PREFIX
        if marker in prompt:
            idx = prompt.find(marker) + len(marker)
            prompt = prompt[:idx] + CODEX_SAFE_REVIEW_PREFIX + prompt[idx:]
        else:
            # Fallback safety: prepend Codex prefix after skill-review wrapping.
            prompt = CODEX_SAFE_REVIEW_PREFIX + prompt
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
    completion_report_mode = resolve_completion_report_mode(parsed, config)
    prompt = effective_prompt(
        prompt, engine=parsed.engine, mode=parsed.mode, completion_report_mode=completion_report_mode
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
            raise DelegateError("invalid_model", "model must be a non-empty string or omitted for codex.")
    elif model_alias is not None and model_alias != config["cursor"]["defaultModel"]:
        raise DelegateError(
            "invalid_model", "cursor model override must match configured Composer model."
        )
    json_cwd = raw.get("cwd")
    if json_cwd is not None and not isinstance(json_cwd, str):
        raise DelegateError("invalid_cwd", "cwd must be a string.")
    workspace = resolve_workspace(parsed.cwd, json_cwd)
    completion_report_mode = resolve_completion_report_mode(parsed, config)
    prompt = effective_prompt(
        validate_prompt(prompt),
        engine=engine,
        mode=mode,
        completion_report_mode=completion_report_mode,
    )
    return build_request(
        engine,
        mode,
        model_alias,
        workspace,
        prompt,
        config,
        dry_run=False,
        stream_capture=not parsed.pass_through,
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
        return Request(engine, mode, resolved.path, prompt, argv, model, dry_run, resolved.kind)
    if engine == "droid":
        droid = config["droid"]
        models = droid["models"]
        if model_alias not in models:
            raise DelegateError("invalid_alias", f"Unknown Droid model alias: {model_alias}")
        assert model_alias is not None
        model = models[model_alias]
        argv = build_droid_argv(
            droid["binary"], mode, resolved.path, model, prompt, stream_capture=stream_capture
        )
        return Request(engine, mode, resolved.path, prompt, argv, model, dry_run, resolved.kind)
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
        return Request(engine, mode, resolved.path, prompt, argv, model, dry_run, resolved.kind)
    raise DelegateError("invalid_engine", "engine must be cursor, droid, or codex.")


def prefix_cursor_safe_prompt(prompt: str) -> str:
    if prompt.startswith(CURSOR_SAFE_REVIEW_PREFIX):
        return prompt
    return f"{CURSOR_SAFE_REVIEW_PREFIX}{prompt}"


def replace_argv_workspace(argv: list[str], workspace: str) -> list[str]:
    updated = list(argv)
    for index, token in enumerate(updated):
        if token == "--workspace" and index + 1 < len(updated):
            updated[index + 1] = workspace
            break
    return updated


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
    return list(request.argv)


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


def cleanup_cursor_safe_workspace(
    *,
    git_root: str | None,
    isolated_workspace: str,
    temp_base: str,
) -> None:
    if git_root is not None:
        remove_git_safe_workspace(git_root, isolated_workspace)
    shutil.rmtree(temp_base, ignore_errors=True)


@contextmanager
def cursor_safe_isolated_request(request: Request) -> Iterator[Request]:
    if request.engine != "cursor" or request.mode != MODE_SAFE:
        yield request
        return

    git_root = request.workspace if request.workspace_kind == "git" else None
    if git_root is not None:
        isolated_workspace, temp_base = create_git_safe_workspace(git_root)
    else:
        isolated_workspace, temp_base = create_directory_safe_workspace(request.workspace)

    isolation = SafeIsolationContext(request.workspace, isolated_workspace)
    try:
        write_cursor_safe_project_config(Path(isolated_workspace))
        yield Request(
            request.engine,
            request.mode,
            isolated_workspace,
            request.prompt,
            replace_argv_workspace(request.argv, isolated_workspace),
            request.model,
            request.dry_run,
            request.workspace_kind,
            safe_isolation=isolation,
        )
    finally:
        cleanup_cursor_safe_workspace(
            git_root=git_root,
            isolated_workspace=isolated_workspace,
            temp_base=temp_base,
        )


@contextmanager
def safe_isolated_request(request: Request) -> Iterator[Request]:
    if request.mode != MODE_SAFE or request.engine not in ("cursor", "codex"):
        yield request
        return

    git_root = request.workspace if request.workspace_kind == "git" else None
    if git_root is not None:
        isolated_workspace, temp_base = create_git_safe_workspace(git_root)
    else:
        isolated_workspace, temp_base = create_directory_safe_workspace(request.workspace)

    isolation = SafeIsolationContext(request.workspace, isolated_workspace)
    try:
        if request.engine == "cursor":
            write_cursor_safe_project_config(Path(isolated_workspace))
        yield Request(
            request.engine,
            request.mode,
            isolated_workspace,
            request.prompt,
            replace_safe_workspace_arg(request, isolated_workspace),
            request.model,
            request.dry_run,
            request.workspace_kind,
            safe_isolation=isolation,
        )
    finally:
        cleanup_cursor_safe_workspace(
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
        "argv": request.argv,
    }
    if request.engine in ("cursor", "codex") and request.mode == MODE_SAFE:
        payload["isolatedWorkspace"] = True
        payload["isolation"] = (
            "Execution uses a temporary detached git worktree or directory copy; "
            "the original workspace is not modified."
        )
    return payload


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
) -> delegate_runner.RunContext:
    source_cwd = (
        request.safe_isolation.source_workspace
        if request.safe_isolation is not None
        else source_workspace.path
    )
    execution_cwd = request.workspace
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
        isolated_workspace=request.safe_isolation is not None,
        started_at=run_registry.utc_now_iso(),
    )


def execute_request(
    request: Request,
    json_mode: bool,
    *,
    pass_through: bool,
    completion_report_mode: str,
    source_workspace: ResolvedWorkspace,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[int, JsonObject | None]:
    ensure_binary(request.argv)
    with safe_isolated_request(request) as isolated_request:
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
        run_id, alias = run_registry.register_run(
            registry_root,
            harness=isolated_request.engine,
            metadata={
                "mode": isolated_request.mode,
                "model": isolated_request.model,
                "cwd": (
                    isolated_request.safe_isolation.source_workspace
                    if isolated_request.safe_isolation is not None
                    else source_workspace.path
                ),
            },
        )
        ctx = make_run_context(
            registry_root,
            isolated_request,
            run_id=run_id,
            alias=alias,
            source_workspace=source_workspace,
        )
        return delegate_runner.execute_tracked(
            isolated_request.argv,
            isolated_request.workspace,
            ctx,
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


def describe_payload(config: JsonObject, config_source: str) -> JsonObject:
    codex = config["codex"]
    codex_safe_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_SAFE)
    codex_work_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_WORK)
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
                "safe": [
                    codex["binary"],
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--cd",
                    "<isolated-workspace>",
                    "--sandbox",
                    "read-only",
                    "--color",
                    "never",
                    "--json",
                    "--ephemeral",
                    "<skill-review-prompt>",
                ],
                "safeNotes": [
                    "Runs in an isolated temporary workspace (detached git worktree or directory copy).",
                    "Always uses --sandbox read-only; safe sandbox is not configurable in v1.",
                    "Non-interactive: --ask-for-approval never.",
                ],
                "work": [
                    codex["binary"],
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--cd",
                    "<workspace>",
                    "--sandbox",
                    codex["workSandbox"],
                    "-c",
                    "sandbox_workspace_write.network_access=true",
                    "--color",
                    "never",
                    "--json",
                    "--ephemeral",
                    "<skill-review-prompt>",
                ],
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
  delegate droid minimax safe "Investigate this issue; do not edit."
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
  - The original workspace is not modified; review prompts are prefixed with read-only instructions.

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
        config, source = load_config(workspace=workspace_path_for_config(parsed.cwd))
        validate_config(config)
        if parsed.subcommand == "models":
            return emit_models(config, source, parsed.json_mode, stdout)
        if parsed.subcommand == "describe":
            return emit_describe(config, source, parsed.json_mode, stdout)
        if parsed.subcommand == "agent-help":
            return emit_agent_help(stdout)
        workspace = resolve_workspace(parsed.cwd)
        if parsed.subcommand in {
            "snapshot",
            "runs",
            "run-output",
            "cursor",
            "droid",
            "codex",
            "dry-run",
            "run",
        }:
            maybe_run_retention_pass(registry_for_workspace(workspace), config)
        if parsed.subcommand == "snapshot":
            return emit_snapshot(parsed, workspace, stdout)
        if parsed.subcommand == "runs":
            return emit_runs(parsed, workspace, stdout)
        if parsed.subcommand == "run-output":
            return emit_run_output(parsed, workspace, stdout)

        request = request_from_parsed(parsed, config, stdin)
        if request.dry_run:
            payload = dry_run_payload(request)
            if parsed.json_mode:
                delegate_rendering.print_json(payload, stdout)
            else:
                print(f"cwd: {request.workspace} ({request.workspace_kind})", file=stdout)
                if payload.get("isolatedWorkspace"):
                    print(f"isolation: {payload['isolation']}", file=stdout)
                print(f"argv: {shell_join(request.argv)}", file=stdout)
            return EXIT_OK

        completion_report_mode = resolve_completion_report_mode(parsed, config)
        exit_code, payload = execute_request(
            request,
            parsed.json_mode,
            pass_through=parsed.pass_through,
            completion_report_mode=completion_report_mode,
            source_workspace=workspace,
            stdout=stdout,
            stderr=stderr,
        )
        if parsed.json_mode and payload is not None:
            delegate_rendering.print_json(payload, stdout)
        return exit_code
    except DelegateError as exc:
        return emit_error(exc, json_mode, stdout, stderr)


if __name__ == "__main__":
    raise SystemExit(main())
