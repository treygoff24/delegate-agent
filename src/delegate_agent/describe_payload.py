"""Introspection surfaces.

Builds the JSON payloads behind ``delegate runtime``, ``config``, ``models``,
and ``describe``, plus the stdout emitters for models/describe/command-help/
agent-help. Reference output only — no run execution happens here.
"""

from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path
from typing import TextIO

from delegate_agent import VERSION, command_help, reasoning
from delegate_agent import config as delegate_config
from delegate_agent import rendering as delegate_rendering
from delegate_agent.argv_builders import (
    _claude_harness_bypass_enabled,
    build_claude_argv,
    build_codex_argv,
)
from delegate_agent.config import config_path
from delegate_agent.constants import KNOWN_ENGINES, MODE_SAFE, MODE_WORK
from delegate_agent.errors import EXIT_OK, DelegateError
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.prompt_transport import (
    DROID_PROMPT_FILE_DISPLAY,
    PROMPT_TRANSPORT_ARGV,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_build import _resolve_default_model

CONFIG_ENV = delegate_config.CONFIG_ENV

REDACTED_PATH = "<redacted-path>"
REDACTED_MODEL_ID = "<redacted-model-id>"


def _redacted_path(value: object) -> str:
    _ = value
    return REDACTED_PATH


def _redacted_model_id(value: object) -> str:
    _ = value
    return REDACTED_MODEL_ID


def _redact_discovery_value(key: str, value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        if key in {"models", "reasoningEffortModels"} and all(
            isinstance(item, str) for item in value.values()
        ):
            return {str(alias): _redacted_model_id(model) for alias, model in value.items()}
        return {
            str(child_key): _redact_discovery_value(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_discovery_value(key, item) for item in value]
    if isinstance(value, str):
        if key in {
            "path",
            "effectiveConfigPath",
            "workspace",
            "modulePath",
            "packageRoot",
            "executable",
            "pythonExecutable",
            "configPath",
            "dataHome",
        }:
            return _redacted_path(value)
        if key in {"model", "defaultModel"}:
            return _redacted_model_id(value)
    return value


def redact_discovery_payload(payload: JsonObject) -> JsonObject:
    """Mask local paths and private model ids for agent discovery surfaces.

    This is intentionally separate from secret redaction: the goal is not to
    hide credentials inside arbitrary text, but to make routine `models` and
    `describe` discovery safe to paste into logs or subagent prompts while
    preserving aliases and shape.
    """

    copied = copy.deepcopy(payload)
    redacted = _redact_discovery_value("", copied)
    return redacted if isinstance(redacted, dict) else {"ok": False}


def runtime_payload() -> JsonObject:
    # modulePath reports the CLI entrypoint (cli.py), a sibling of this module;
    # callers and tests rely on it pointing at cli.py, not at describe_payload.py.
    module_path = Path(__file__).resolve().with_name("cli.py")
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
    cache = reasoning.load_reasoning_capability_cache(workspace) if workspace is not None else None
    return {
        "ok": True,
        "configSource": config_source,
        "configResolution": config_resolution_payload(config_source, workspace),
        "runtime": runtime_payload(),
        "reasoningAliases": reasoning.build_alias_reasoning_summaries(config, cache),
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


def _reasoning_for_alias(reasoning_aliases: JsonObject, engine: str, alias: str) -> JsonObject:
    engine_payload = reasoning_aliases.get(engine)
    if not isinstance(engine_payload, dict):
        return {}
    payload = engine_payload.get(alias)
    return payload if isinstance(payload, dict) else {}


def _summary_reasoning_fields(
    reasoning_payload: JsonObject, *, redacted: bool = False
) -> JsonObject:
    output: JsonObject = {}
    supported = reasoning_payload.get("supported")
    if isinstance(supported, list) and all(isinstance(item, str) for item in supported):
        output["reasoningEfforts"] = supported
    elif supported is None and reasoning_payload:
        output["reasoningEfforts"] = None
    default = reasoning_payload.get("default")
    if isinstance(default, str):
        output["defaultReasoningEffort"] = default
    config_default = reasoning_payload.get("configDefault")
    if isinstance(config_default, str):
        output["configuredReasoningEffort"] = config_default
    source = reasoning_payload.get("source")
    if isinstance(source, str):
        output["reasoningCapabilitySource"] = source
    warning = reasoning_payload.get("warning")
    if isinstance(warning, str):
        output["warnings"] = _warning_field(warning, redacted=redacted)
    return output


def _model_field(model: object, *, redacted: bool) -> JsonObject:
    configured = isinstance(model, str) and bool(model)
    if redacted:
        return {"modelConfigured": configured}
    return {"defaultModel": model if configured else None}


def _redact_reasoning_warning(warning: str) -> str:
    return re.sub(r"model '[^']+'", f"model '{REDACTED_MODEL_ID}'", warning)


def _warning_field(warning: str, *, redacted: bool) -> list[str]:
    return [_redact_reasoning_warning(warning) if redacted else warning]


def models_summary_payload(
    config: JsonObject,
    config_source: str,
    workspace: Path | None = None,
    *,
    redacted: bool = False,
) -> JsonObject:
    cache = reasoning.load_reasoning_capability_cache(workspace) if workspace is not None else None
    reasoning_aliases = reasoning.build_alias_reasoning_summaries(config, cache)
    aliases: list[JsonObject] = []

    for engine in ("cursor", "codex", "claude", "kimi"):
        section = config.get(engine)
        if not isinstance(section, dict):
            continue
        default_model = section.get("defaultModel")
        entry: JsonObject = {
            "alias": engine,
            "provider": engine,
            "command": f"delegate {engine} {{safe,work}}",
            "available": True,
            "safeSupported": True,
            "workSupported": True,
            **_model_field(default_model, redacted=redacted),
        }
        reason_key = (
            default_model if isinstance(default_model, str) and default_model else "(default)"
        )
        entry.update(
            _summary_reasoning_fields(
                _reasoning_for_alias(reasoning_aliases, engine, reason_key),
                redacted=redacted,
            )
        )
        aliases.append(entry)

    droid = config.get("droid")
    if isinstance(droid, dict):
        models = droid.get("models")
        if isinstance(models, dict):
            for alias, model_id in sorted(models.items()):
                if not isinstance(alias, str) or not alias:
                    continue
                entry = {
                    "alias": alias,
                    "provider": "droid",
                    "command": f"delegate droid {alias} {{safe,work}}",
                    "available": isinstance(model_id, str) and bool(model_id),
                    "safeSupported": True,
                    "workSupported": True,
                }
                if redacted:
                    entry["modelConfigured"] = isinstance(model_id, str) and bool(model_id)
                else:
                    entry["model"] = model_id if isinstance(model_id, str) else None
                entry.update(
                    _summary_reasoning_fields(
                        _reasoning_for_alias(reasoning_aliases, "droid", alias),
                        redacted=redacted,
                    )
                )
                aliases.append(entry)

    return {
        "ok": True,
        "summary": True,
        "redacted": redacted,
        "configSource": config_source,
        "version": VERSION,
        "aliases": aliases,
        "counts": {
            "aliases": len(aliases),
            "providers": len({str(item.get("provider")) for item in aliases}),
        },
        "discovery": {
            "fullModels": "delegate --json models",
            "safeSummary": "delegate --json models --summary --redacted",
            "reasoningCapabilities": "delegate --json capabilities",
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
        _resolve_default_model(codex),
        prompt,
        policy,
        workspace_kind="git",
        prompt_transport=PROMPT_TRANSPORT_STDIN,
    )


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
        _resolve_default_model(claude),
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
        "engines": list(KNOWN_ENGINES),
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


def describe_summary_payload(
    config: JsonObject,
    config_source: str,
    workspace: Path | None = None,
    *,
    redacted: bool = False,
) -> JsonObject:
    full = describe_payload(config, config_source, workspace)
    config_resolution = full.get("configResolution")
    if redacted and isinstance(config_resolution, dict):
        config_resolution = redact_discovery_payload(config_resolution)
    commands = full.get("commands")
    return {
        "ok": True,
        "summary": True,
        "redacted": redacted,
        "version": VERSION,
        "configSource": config_source,
        "configResolution": config_resolution if isinstance(config_resolution, dict) else {},
        "engines": list(KNOWN_ENGINES),
        "modes": [MODE_SAFE, MODE_WORK],
        "isolationValues": list(delegate_config.VALID_ISOLATION_VALUES),
        "globalOptions": full["globalOptions"],
        "launchOptions": ["--prompt-file", "--reasoning-effort"],
        "commands": commands if isinstance(commands, list) else [],
        "recommendedDiscovery": [
            "delegate --json describe --summary --redacted",
            "delegate --json models --summary --redacted",
            "delegate --json help <command>",
        ],
    }


def emit_models(
    config: JsonObject,
    config_source: str,
    json_mode: bool,
    stdout: TextIO,
    *,
    workspace: Path | None = None,
    summary: bool = False,
    redacted: bool = False,
) -> int:
    if summary:
        payload = models_summary_payload(config, config_source, workspace, redacted=redacted)
        if json_mode:
            delegate_rendering.print_json(payload, stdout)
        else:
            for item in payload["aliases"]:
                warnings = item.get("warnings")
                suffix = f" warnings={len(warnings)}" if isinstance(warnings, list) else ""
                print(
                    f"{item['provider']}:{item['alias']} safe={item['safeSupported']} work={item['workSupported']}{suffix}",
                    file=stdout,
                )
        return EXIT_OK
    if json_mode:
        payload = models_payload(config, config_source, workspace)
        if redacted:
            payload = redact_discovery_payload(payload)
            payload["redacted"] = True
        delegate_rendering.print_json(payload, stdout)
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
    summary: bool = False,
    redacted: bool = False,
) -> int:
    payload = (
        describe_summary_payload(config, config_source, workspace, redacted=redacted)
        if summary
        else describe_payload(config, config_source, workspace)
    )
    if redacted and not summary:
        payload = redact_discovery_payload(payload)
        payload["redacted"] = True
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
  delegate --json models --summary --redacted
  delegate --json describe --summary --redacted
  delegate --json models        # full/raw details when needed
  delegate --json describe      # full/raw details when needed
  delegate agent-help
""".rstrip(),
        file=stdout,
    )
    return EXIT_OK
