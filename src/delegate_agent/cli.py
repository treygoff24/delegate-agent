#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, TextIO


VERSION = "0.1.2"
DEFAULT_CONFIG_PATH = Path.home() / ".delegate" / "config.json"
CONFIG_ENV = "DELEGATE_CONFIG"

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
CURSOR_SAFE_CLI_CONFIG: dict[str, Any] = {
    "version": 1,
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

DEFAULT_CONFIG: dict[str, Any] = {
    "cursor": {
        "argvPrefix": ["agent"],
        "defaultModel": "composer-2.5",
    },
    "droid": {
        "binary": "droid",
        "models": {
            "glm": "custom:OpenCode-Go-:-GLM-5.1-4",
            "kimi": "custom:OpenCode-Go-:-Kimi-K2.6-5",
            "mimo": "custom:OpenCode-Go-:-MiMo-V2.5-6",
            "mimo pro": "custom:OpenCode-Go-:-MiMo-V2.5-Pro-7",
            "minimax": "custom:OpenCode-Go-:-MiniMax-M2.7-8",
            "qwen": "custom:OpenCode-Go-:-Qwen3.6-Plus-9",
            "deepseek pro": "custom:OpenCode-Go-:-DeepSeek-V4-Pro-10",
            "deepseek flash": "custom:OpenCode-Go-:-DeepSeek-V4-Flash-11",
            "grok": "custom:xAI-:-Grok-4.3-44",
            "gemini": "custom:Gemini-:-3.5-Flash-15",
        },
    },
}


class DelegateError(Exception):
    def __init__(self, error: str, message: str, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code


@dataclass
class ParsedCommand:
    subcommand: str
    json_mode: bool = False
    cwd: str | None = None
    engine: str | None = None
    mode: str | None = None
    model_alias: str | None = None
    prompt_parts: list[str] | None = None
    prompt_file: str | None = None
    input_json: str | None = None
    dry_run: bool = False


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
    model: str
    dry_run: bool = False
    workspace_kind: str = "git"
    safe_isolation: SafeIsolationContext | None = None


HELP = f"""delegate {VERSION}

Usage:
  delegate [--cwd PATH] [--json] cursor {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] droid MODEL_ALIAS {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] dry-run cursor {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] dry-run droid MODEL_ALIAS {{safe,work}} [--prompt-file PATH] [prompt...]
  delegate [--cwd PATH] [--json] run --input-json FILE
  delegate [--json] models
  delegate [--json] describe
  delegate agent-help

Global options must appear before the subcommand.
"""


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, str(DEFAULT_CONFIG_PATH))).expanduser()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> tuple[dict[str, Any], str]:
    path = path or config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG), "embedded-default"
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise DelegateError("invalid_config_json", f"Invalid JSON in {path}: {exc}")
    if not isinstance(loaded, dict):
        raise DelegateError("invalid_config", "Config root must be a JSON object.")
    return deep_merge(DEFAULT_CONFIG, loaded), str(path)


def validate_config(config: dict[str, Any]) -> None:
    cursor = config.get("cursor")
    droid = config.get("droid")
    if not isinstance(cursor, dict):
        raise DelegateError("invalid_cursor_config", "cursor config must be an object.")
    if "binary" in cursor:
        raise DelegateError(
            "invalid_cursor_config",
            "cursor.binary is not supported; use cursor.argvPrefix as an array of strings.",
        )
    prefix = cursor.get("argvPrefix")
    if not isinstance(prefix, list) or not prefix or not all(isinstance(x, str) and x for x in prefix):
        raise DelegateError("invalid_cursor_config", "cursor.argvPrefix must be a non-empty array of strings.")
    if not isinstance(cursor.get("defaultModel"), str) or not cursor["defaultModel"].strip():
        raise DelegateError("invalid_cursor_config", "cursor.defaultModel must be a non-empty string.")
    if not isinstance(droid, dict):
        raise DelegateError("invalid_droid_config", "droid config must be an object.")
    if not isinstance(droid.get("binary"), str) or not droid["binary"].strip():
        raise DelegateError("invalid_droid_config", "droid.binary must be a non-empty string.")
    models = droid.get("models")
    if not isinstance(models, dict) or not models:
        raise DelegateError("invalid_droid_config", "droid.models must be a non-empty object.")
    for alias, model_id in models.items():
        if not isinstance(alias, str) or not isinstance(model_id, str) or not alias or not model_id:
            raise DelegateError("invalid_droid_config", "droid model aliases and ids must be non-empty strings.")


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
        break

    if i >= len(argv):
        raise DelegateError("missing_subcommand", "Missing subcommand.")

    subcommand = argv[i]
    rest = argv[i + 1 :]
    if subcommand.startswith("-"):
        raise DelegateError("unknown_option", f"Unknown global option before subcommand: {subcommand}")

    if subcommand == "models":
        require_no_extra(rest, "models")
        return ParsedCommand("models", json_mode=json_mode, cwd=cwd)
    if subcommand == "describe":
        require_no_extra(rest, "describe")
        return ParsedCommand("describe", json_mode=json_mode, cwd=cwd)
    if subcommand == "agent-help":
        require_no_extra(rest, "agent-help")
        return ParsedCommand("agent-help", json_mode=json_mode, cwd=cwd)
    if subcommand == "run":
        return parse_run(rest, json_mode, cwd)
    if subcommand == "cursor":
        return parse_cursor(rest, json_mode, cwd, dry_run=False)
    if subcommand == "droid":
        return parse_droid(rest, json_mode, cwd, dry_run=False)
    if subcommand == "dry-run":
        return parse_dry_run(rest, json_mode, cwd)

    raise DelegateError("unknown_subcommand", f"Unknown subcommand: {subcommand}")


def require_no_extra(rest: list[str], name: str) -> None:
    if rest:
        if any(tok in ("--json", "--cwd") for tok in rest):
            raise DelegateError("misplaced_global_option", "Global options must appear before the subcommand.")
        raise DelegateError("unexpected_argument", f"{name} does not accept arguments: {' '.join(rest)}")


def parse_run(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    if len(rest) != 2 or rest[0] != "--input-json":
        if any(tok in ("--json", "--cwd") for tok in rest):
            raise DelegateError("misplaced_global_option", "Global options must appear before the subcommand.")
        raise DelegateError("invalid_run_args", "run requires: --input-json FILE")
    return ParsedCommand("run", json_mode=json_mode, cwd=cwd, input_json=rest[1])


def parse_cursor(rest: list[str], json_mode: bool, cwd: str | None, dry_run: bool) -> ParsedCommand:
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
    )


def parse_droid(rest: list[str], json_mode: bool, cwd: str | None, dry_run: bool) -> ParsedCommand:
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
    )


def parse_dry_run(rest: list[str], json_mode: bool, cwd: str | None) -> ParsedCommand:
    if not rest:
        raise DelegateError("missing_engine", "dry-run requires cursor or droid.")
    engine = rest[0]
    if engine == "cursor":
        return parse_cursor(rest[1:], json_mode, cwd, dry_run=True)
    if engine == "droid":
        return parse_droid(rest[1:], json_mode, cwd, dry_run=True)
    raise DelegateError("invalid_engine", "dry-run engine must be cursor or droid.")


def parse_prompt_tail(rest: list[str]) -> tuple[str | None, list[str]]:
    prompt_file: str | None = None
    prompt_parts: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--prompt-file":
            if prompt_parts:
                raise DelegateError("ambiguous_prompt_source", "--prompt-file must appear before direct prompt text.")
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
        raise DelegateError("ambiguous_prompt_source", "--prompt-file must appear before direct prompt text.")
    if any(tok in ("--json", "--cwd") for tok in prompt_parts):
        raise DelegateError(
            "misplaced_global_option",
            "Global options must appear before the subcommand; use --prompt-file for literal flag text.",
        )
    return prompt_file, prompt_parts


def validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise DelegateError("invalid_mode", "Mode must be safe or work.")


def resolve_workspace(global_cwd: str | None, json_cwd: str | None = None) -> ResolvedWorkspace:
    if global_cwd and json_cwd:
        global_workspace = workspace_for(global_cwd)
        json_workspace = workspace_for(json_cwd)
        if Path(global_workspace.path).resolve() != Path(json_workspace.path).resolve():
            raise DelegateError("ambiguous_cwd", "CLI --cwd and JSON cwd resolve to different workspaces.")
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        raise DelegateError("ambiguous_prompt_source", "Use exactly one prompt source: direct args, --prompt-file, or stdin.")
    if has_direct:
        return validate_prompt(direct)
    if has_prompt_file:
        path = Path(prompt_file or "").expanduser()
        try:
            return validate_prompt(path.read_text())
        except FileNotFoundError:
            raise DelegateError("prompt_file_not_found", f"Prompt file not found: {path}")
    if has_stdin:
        return validate_prompt(stdin_text or "")
    raise DelegateError("missing_prompt", "Missing prompt; pass prompt text, --prompt-file, or stdin.")


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


def request_from_parsed(parsed: ParsedCommand, config: dict[str, Any], stdin: TextIO) -> Request:
    validate_config(config)
    if parsed.subcommand == "run":
        return request_from_input_json(parsed, config)
    if parsed.engine not in ("cursor", "droid") or parsed.mode is None:
        raise DelegateError("invalid_command", "Command does not map to an execution request.")
    workspace = resolve_workspace(parsed.cwd)
    prompt = resolve_prompt(parsed.prompt_parts, parsed.prompt_file, stdin)
    return build_request(parsed.engine, parsed.mode, parsed.model_alias, workspace, prompt, config, parsed.dry_run)


def request_from_input_json(parsed: ParsedCommand, config: dict[str, Any]) -> Request:
    path = Path(parsed.input_json or "").expanduser()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise DelegateError("input_json_not_found", f"Input JSON file not found: {path}")
    except json.JSONDecodeError as exc:
        raise DelegateError("invalid_input_json", f"Invalid input JSON: {exc}")
    if not isinstance(raw, dict):
        raise DelegateError("invalid_input_json", "Input JSON root must be an object.")
    unknown = sorted(set(raw) - RUN_INPUT_KEYS)
    if unknown:
        raise DelegateError("unknown_input_key", f"Unknown input JSON keys: {', '.join(unknown)}")
    engine = raw.get("engine")
    mode = raw.get("mode")
    prompt = raw.get("prompt")
    if engine not in ("cursor", "droid"):
        raise DelegateError("invalid_engine", "engine must be cursor or droid.")
    if not isinstance(mode, str):
        raise DelegateError("invalid_mode", "mode must be safe or work.")
    validate_mode(mode)
    if not isinstance(prompt, str):
        raise DelegateError("invalid_prompt", "prompt must be a string.")
    model_alias = raw.get("model")
    if engine == "droid":
        if not isinstance(model_alias, str) or not model_alias:
            raise DelegateError("missing_model", "droid run input requires model alias.")
    elif model_alias is not None and model_alias != config["cursor"]["defaultModel"]:
        raise DelegateError("invalid_model", "cursor model override must match configured Composer model.")
    json_cwd = raw.get("cwd")
    if json_cwd is not None and not isinstance(json_cwd, str):
        raise DelegateError("invalid_cwd", "cwd must be a string.")
    workspace = resolve_workspace(parsed.cwd, json_cwd)
    return build_request(engine, mode, model_alias, workspace, validate_prompt(prompt), config, dry_run=False)


def build_request(
    engine: str,
    mode: str,
    model_alias: str | None,
    workspace: ResolvedWorkspace | str,
    prompt: str,
    config: dict[str, Any],
    dry_run: bool,
) -> Request:
    resolved = workspace if isinstance(workspace, ResolvedWorkspace) else ResolvedWorkspace(workspace, "git")
    if engine == "cursor":
        cursor = config["cursor"]
        model = cursor["defaultModel"]
        argv = build_cursor_argv(cursor["argvPrefix"], mode, resolved.path, model, prompt)
        return Request(engine, mode, resolved.path, prompt, argv, model, dry_run, resolved.kind)
    if engine == "droid":
        droid = config["droid"]
        models = droid["models"]
        if model_alias not in models:
            raise DelegateError("invalid_alias", f"Unknown Droid model alias: {model_alias}")
        model = models[model_alias or ""]
        argv = build_droid_argv(droid["binary"], mode, resolved.path, model, prompt)
        return Request(engine, mode, resolved.path, prompt, argv, model, dry_run, resolved.kind)
    raise DelegateError("invalid_engine", "engine must be cursor or droid.")


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


def write_cursor_safe_project_config(workspace: Path) -> None:
    config_dir = workspace / ".cursor"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli.json").write_text(json.dumps(CURSOR_SAFE_CLI_CONFIG, indent=2) + "\n")


def read_git_tracked_diff(git_root: str) -> bytes:
    diff = subprocess.run(
        ["git", "-C", git_root, "diff", "HEAD", "--binary"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if untracked.returncode != 0:
        raise DelegateError("safe_workspace_sync_failed", f"Failed to list untracked files: {untracked.stderr.strip()}")
    for relative in untracked.stdout.splitlines():
        if not relative:
            continue
        mirror_path_preserving_symlinks(Path(git_root) / relative, Path(worktree_path) / relative)


def discard_git_safe_workspace(git_root: str, worktree_path: str, temp_base: str, *, worktree_added: bool) -> None:
    if worktree_added:
        remove_git_safe_workspace(git_root, worktree_path)
    shutil.rmtree(temp_base, ignore_errors=True)


def create_git_safe_workspace(git_root: str) -> tuple[str, str]:
    temp_base = tempfile.mkdtemp(prefix="delegate-cursor-safe-")
    worktree_path = str(Path(temp_base) / "wt")
    worktree_added = False
    try:
        added = subprocess.run(
            ["git", "-C", git_root, "worktree", "add", "--detach", worktree_path, "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        discard_git_safe_workspace(git_root, worktree_path, temp_base, worktree_added=worktree_added)
        raise
    return worktree_path, temp_base


def create_directory_safe_workspace(source_workspace: str) -> tuple[str, str]:
    temp_base = tempfile.mkdtemp(prefix="delegate-cursor-safe-")
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


def build_cursor_argv(prefix: list[str], mode: str, workspace: str, model: str, prompt: str) -> list[str]:
    argv = [*prefix, "--workspace", workspace, "-p", "--trust"]
    if mode == MODE_WORK:
        argv.extend(["--approve-mcps", "--force"])
    elif mode == MODE_SAFE:
        prompt = prefix_cursor_safe_prompt(prompt)
    else:
        validate_mode(mode)
    argv.extend(["--model", model, "--output-format", "text", prompt])
    return argv


def build_droid_argv(binary: str, mode: str, workspace: str, model: str, prompt: str) -> list[str]:
    argv = [binary, "exec", "--cwd", workspace]
    if mode == MODE_WORK:
        argv.append("--skip-permissions-unsafe")
    elif mode != MODE_SAFE:
        validate_mode(mode)
    argv.extend(["--model", model, prompt])
    return argv


def json_workspace_fields(request: Request) -> dict[str, Any]:
    if request.safe_isolation is not None:
        return {
            "cwd": request.safe_isolation.source_workspace,
            "executionCwd": request.safe_isolation.execution_workspace,
            "isolatedWorkspace": True,
        }
    return {"cwd": request.workspace}


def dry_run_payload(request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "dryRun": True,
        "cwd": request.workspace,
        "workspaceKind": request.workspace_kind,
        "engine": request.engine,
        "mode": request.mode,
        "model": request.model,
        "argv": request.argv,
    }
    if request.engine == "cursor" and request.mode == MODE_SAFE:
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


def run_child(request: Request, json_mode: bool) -> tuple[int, dict[str, Any] | None]:
    started = time.monotonic()
    if json_mode:
        completed = subprocess.run(
            request.argv,
            cwd=request.workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        payload = {
            "ok": completed.returncode == 0,
            "engine": request.engine,
            "mode": request.mode,
            "model": request.model,
            **json_workspace_fields(request),
            "workspaceKind": request.workspace_kind,
            "exitCode": completed.returncode,
            "durationMs": duration_ms,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode != 0:
            payload["error"] = "child_failed"
            payload["message"] = f"Child command exited {completed.returncode}."
        return completed.returncode, payload
    completed = subprocess.run(request.argv, cwd=request.workspace, text=True, check=False)
    return completed.returncode, None


def execute_request(request: Request, json_mode: bool) -> tuple[int, dict[str, Any] | None]:
    ensure_binary(request.argv)
    with cursor_safe_isolated_request(request) as isolated_request:
        return run_child(isolated_request, json_mode)


def models_payload(config: dict[str, Any], config_source: str) -> dict[str, Any]:
    return {
        "ok": True,
        "configSource": config_source,
        "cursor": {"defaultModel": config["cursor"]["defaultModel"], "argvPrefix": config["cursor"]["argvPrefix"]},
        "droid": {"models": config["droid"]["models"]},
    }


def describe_payload(config: dict[str, Any], config_source: str) -> dict[str, Any]:
    return {
        "ok": True,
        "version": VERSION,
        "configPath": str(config_path()),
        "configSource": config_source,
        "engines": ["cursor", "droid"],
        "modes": [MODE_SAFE, MODE_WORK],
        "promptSources": ["direct", "prompt-file", "stdin"],
        "globalOptions": ["--cwd", "--json"],
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
                    "--output-format",
                    "text",
                    "<review-prefixed-prompt>",
                ],
                "safeNotes": [
                    "No --mode=plan, --mode=ask, --force, or --approve-mcps.",
                    "Runs in an isolated temporary workspace (detached git worktree or directory copy).",
                    "Writes .cursor/cli.json in the isolated workspace (Read(**), read-only shell helpers; no git/find shell).",
                ],
                "work": [*config["cursor"]["argvPrefix"], "--workspace", "<workspace>", "-p", "--trust", "--approve-mcps", "--force", "--model", config["cursor"]["defaultModel"], "--output-format", "text", "<prompt>"],
            },
            "droid": {
                "safe": [config["droid"]["binary"], "exec", "--cwd", "<workspace>", "--model", "<model-id>", "<prompt>"],
                "work": [config["droid"]["binary"], "exec", "--cwd", "<workspace>", "--skip-permissions-unsafe", "--model", "<model-id>", "<prompt>"],
            },
        },
    }


def emit_models(config: dict[str, Any], config_source: str, json_mode: bool, stdout: TextIO) -> int:
    if json_mode:
        print_json(models_payload(config, config_source), stdout)
        return EXIT_OK
    if config_source == "embedded-default":
        print("warning: using embedded default config", file=stdout)
    print(f"cursor: {config['cursor']['defaultModel']} ({' '.join(config['cursor']['argvPrefix'])})", file=stdout)
    print("droid:", file=stdout)
    for alias, model_id in sorted(config["droid"]["models"].items()):
        print(f"  {alias} -> {model_id}", file=stdout)
    return EXIT_OK


def emit_describe(config: dict[str, Any], config_source: str, json_mode: bool, stdout: TextIO) -> int:
    payload = describe_payload(config, config_source)
    if json_mode:
        print_json(payload, stdout)
        return EXIT_OK
    print(f"delegate {VERSION}", file=stdout)
    print(f"config: {payload['configPath']} ({payload['configSource']})", file=stdout)
    print("engines: cursor, droid", file=stdout)
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

Droid work mode:
  - Droid safe mode remains read-only: no --auto, --use-spec, or unsafe skip.
  - Uses Factory Droid --skip-permissions-unsafe, not --auto high.
  - This is intentionally no-prompt; use only for bounded tasks in workspaces you trust.

Cursor safe mode:
  - Uses default Cursor Agent behavior in an isolated temporary workspace, not plan/ask mode.
  - The original workspace is not modified; review prompts are prefixed with read-only instructions.

Rules for agents:
  - Keep prompts bounded: task, scope, verification, report format.
  - Use --prompt-file or delegate --json run --input-json for long prompts.
  - Run from the target workspace, or pass --cwd before the subcommand.
  - Inside Git, --cwd resolves to the repo root; outside Git, the directory is used directly.
  - Always review diffs after work mode when Git is available; outside Git, manually review changed files.
  - Do not use delegate for production deploys or repository publishing unless the operator explicitly asks.

Discovery:
  delegate --json models
  delegate --json describe
  delegate agent-help
""".rstrip(),
        file=stdout,
    )
    return EXIT_OK


def print_json(payload: dict[str, Any], stdout: TextIO) -> None:
    print(json.dumps(payload, sort_keys=True), file=stdout)


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in argv)


def emit_error(error: DelegateError, json_mode: bool, stdout: TextIO, stderr: TextIO) -> int:
    if json_mode:
        print_json(
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


def main(argv: list[str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
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
        config, source = load_config()
        validate_config(config)
        if parsed.subcommand == "models":
            return emit_models(config, source, parsed.json_mode, stdout)
        if parsed.subcommand == "describe":
            return emit_describe(config, source, parsed.json_mode, stdout)
        if parsed.subcommand == "agent-help":
            return emit_agent_help(stdout)

        request = request_from_parsed(parsed, config, stdin)
        if request.dry_run:
            payload = dry_run_payload(request)
            if parsed.json_mode:
                print_json(payload, stdout)
            else:
                print(f"cwd: {request.workspace} ({request.workspace_kind})", file=stdout)
                if payload.get("isolatedWorkspace"):
                    print(f"isolation: {payload['isolation']}", file=stdout)
                print(f"argv: {shell_join(request.argv)}", file=stdout)
            return EXIT_OK

        exit_code, payload = execute_request(request, parsed.json_mode)
        if parsed.json_mode and payload is not None:
            print_json(payload, stdout)
        return exit_code
    except DelegateError as exc:
        return emit_error(exc, json_mode, stdout, stderr)


if __name__ == "__main__":
    raise SystemExit(main())
