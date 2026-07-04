"""Introspection surfaces.

Builds the JSON payloads behind ``delegate runtime``, ``config``, ``models``,
and ``describe``, plus the stdout emitters for models/describe/command-help/
agent-help. Reference output only — no run execution happens here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from delegate_agent import VERSION, command_help, reasoning, redaction
from delegate_agent import config as delegate_config
from delegate_agent import rendering as delegate_rendering
from delegate_agent.argv_builders import (
    _claude_harness_bypass_enabled,
    _grok_harness_bypass_enabled,
    build_claude_argv,
    build_codex_argv,
    build_grok_argv,
)
from delegate_agent.command_help import SAFE_WORKSPACE_SYNC_NOTE
from delegate_agent.config import config_path
from delegate_agent.constants import (
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    MODE_WORK,
    MODEL_SUMMARY_ENGINES,
)
from delegate_agent.errors import EXIT_OK, DelegateError
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import (
    DROID_PROMPT_FILE_DISPLAY,
    PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_FILE_DISPLAY,
    PROMPT_TRANSPORT_ARGV,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)
from delegate_agent.request_build import _resolve_default_model

CONFIG_ENV = delegate_config.CONFIG_ENV


def _scrub_discovery_payload(payload: JsonObject) -> JsonObject:
    scrubbed = redaction.redact_value(payload)
    return scrubbed if isinstance(scrubbed, dict) else {"ok": False}


def _text_or_none(value: object) -> str:
    return value if isinstance(value, str) and value else "(none)"


def _text_argv_prefix_label(value: object) -> str:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return "(none)"
    return " ".join(value) if value else "(none)"


def _global_options() -> list[str]:
    return [option.flag for option in command_help.GLOBAL_OPTIONS]


def _commands_catalog() -> list[JsonObject]:
    return [
        {
            "name": spec.name,
            "command": spec.name,
            "summary": spec.summary,
            "usage": list(spec.usage),
            "arguments": [
                {
                    "name": arg.name,
                    "required": arg.required,
                    "description": arg.description,
                }
                for arg in spec.arguments
            ],
            "options": [
                {
                    "flag": option.flag,
                    "arg": option.arg,
                    "description": option.description,
                }
                for option in spec.options
            ],
            "launchOptions": [option.flag for option in spec.options],
        }
        for spec in command_help.COMMAND_SPECS.values()
    ]


def _launch_options() -> list[str]:
    flags: list[str] = []
    for command in ("cursor", "droid", "codex", "claude", "grok", "kimi"):
        spec = command_help.COMMAND_SPECS[command]
        for option in spec.options:
            if option.flag not in flags:
                flags.append(option.flag)
    return flags


def _profiles_config_payload(config: JsonObject) -> JsonObject:
    section = config.get("profiles")
    if not isinstance(section, dict):
        return {"detectFrom": [], "default": None, "definedProfiles": [], "envKeys": {}}
    detect_from = section.get("detectFrom")
    definitions = section.get("definitions")
    profile_names = sorted(definitions) if isinstance(definitions, dict) else []
    env_keys: JsonObject = {}
    if isinstance(definitions, dict):
        for name in profile_names:
            entry = definitions.get(name)
            env = entry.get("env") if isinstance(entry, dict) else None
            env_keys[name] = sorted(env) if isinstance(env, dict) else []
    return {
        "detectFrom": list(detect_from) if isinstance(detect_from, list) else [],
        "default": section.get("default") if isinstance(section.get("default"), str) else None,
        "definedProfiles": profile_names,
        "envKeys": env_keys,
    }


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
        "grok": {
            "binary": config["grok"]["binary"],
            "defaultModel": config["grok"]["defaultModel"],
            "defaultReasoningEffort": config["grok"].get("defaultReasoningEffort"),
            "workPermissionMode": config["grok"]["workPermissionMode"],
            "safePermissionMode": config["grok"]["safePermissionMode"],
            "safeSandbox": config["grok"]["safeSandbox"],
            "workSandbox": config["grok"]["workSandbox"],
            "disableWebSearch": config["grok"]["disableWebSearch"],
            "noSubagents": config["grok"]["noSubagents"],
        },
    }


def _reasoning_for_alias(reasoning_aliases: JsonObject, engine: str, alias: str) -> JsonObject:
    engine_payload = reasoning_aliases.get(engine)
    if not isinstance(engine_payload, dict):
        return {}
    payload = engine_payload.get(alias)
    return payload if isinstance(payload, dict) else {}


def _summary_reasoning_fields(reasoning_payload: JsonObject) -> JsonObject:
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
    routing = reasoning_payload.get("effortModelRouting")
    if isinstance(routing, list):
        routed: list[JsonObject] = []
        for item in routing:
            if not isinstance(item, dict):
                continue
            effort = item.get("effort")
            model = item.get("model")
            if not isinstance(effort, str) or not effort:
                continue
            route: JsonObject = {"effort": effort}
            route["model"] = model if isinstance(model, str) and model else None
            routed.append(route)
        if routed:
            output["reasoningEffortRouting"] = routed
    warning = reasoning_payload.get("warning")
    if isinstance(warning, str):
        output["warnings"] = [warning]
    return output


def models_summary_payload(
    config: JsonObject,
    config_source: str,
    workspace: Path | None = None,
) -> JsonObject:
    cache = reasoning.load_reasoning_capability_cache(workspace) if workspace is not None else None
    reasoning_aliases = reasoning.build_alias_reasoning_summaries(config, cache)
    aliases: list[JsonObject] = []

    for engine in MODEL_SUMMARY_ENGINES:
        section = config.get(engine)
        if not isinstance(section, dict):
            continue
        default_model = section.get("defaultModel")
        entry: JsonObject = {
            "alias": engine,
            "provider": engine,
            "command": f"delegate {engine} {{safe,work,call}}",
            "available": True,
            "safeSupported": True,
            "workSupported": True,
            "defaultModel": default_model
            if isinstance(default_model, str) and default_model
            else None,
        }
        reason_key = (
            default_model if isinstance(default_model, str) and default_model else "(default)"
        )
        entry.update(
            _summary_reasoning_fields(
                _reasoning_for_alias(reasoning_aliases, engine, reason_key),
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
                    "command": f"delegate droid {alias} {{safe,work,call}}",
                    "available": isinstance(model_id, str) and bool(model_id),
                    "safeSupported": True,
                    "workSupported": True,
                    "model": model_id if isinstance(model_id, str) else None,
                }
                entry.update(
                    _summary_reasoning_fields(
                        _reasoning_for_alias(reasoning_aliases, "droid", alias),
                    )
                )
                aliases.append(entry)

    return {
        "ok": True,
        "summary": True,
        "configSource": config_source,
        "version": VERSION,
        "aliases": aliases,
        "counts": {
            "aliases": len(aliases),
            "providers": len({str(item.get("provider")) for item in aliases}),
        },
        "discovery": {
            "fullModels": "delegate --json models",
            "safeSummary": "delegate --json models --summary",
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
    grok_supported = dict(unsupported)
    grok_supported["webSearch"] = True
    grok_supported["bypassApprovalsAndSandbox"] = True
    return {
        "codex": codex_supported,
        "claude": claude_supported,
        "grok": grok_supported,
        "cursor": unsupported,
        "droid": unsupported,
        "kimi": unsupported,
    }


def _engine_capabilities() -> JsonObject:
    return {engine: {"outputSchema": engine == "codex"} for engine in KNOWN_ENGINES}


def _runtime_policy(config: JsonObject, mode: str, engine: str, bypass_check) -> JsonObject:
    policy = {key: False for key in delegate_config.POLICY_MODE_KEYS}
    if engine == "grok":
        policy = delegate_config.effective_policy(config, engine=engine, mode=mode)
    if mode == MODE_WORK:
        policy = dict(policy)
        policy["bypassApprovalsAndSandbox"] = bypass_check(config, mode)
    return policy


def _claude_runtime_policy(config: JsonObject, mode: str) -> JsonObject:
    return _runtime_policy(config, mode, "claude", _claude_harness_bypass_enabled)


def _grok_runtime_policy(config: JsonObject, mode: str) -> JsonObject:
    return _runtime_policy(config, mode, "grok", _grok_harness_bypass_enabled)


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


def _grok_describe_argv(
    config: JsonObject,
    grok: JsonObject,
    *,
    mode: str,
    workspace: str,
    policy: JsonObject,
) -> list[str]:
    argv = build_grok_argv(
        grok,
        mode,
        workspace,
        _resolve_default_model(grok),
        policy,
        allow_bypass_permissions=_grok_harness_bypass_enabled(config, mode),
    )
    return [PROMPT_FILE_DISPLAY if item == PROMPT_FILE_ARG_PLACEHOLDER else item for item in argv]


def describe_payload(
    config: JsonObject,
    config_source: str,
    workspace: Path | None = None,
) -> JsonObject:
    codex = config["codex"]
    claude = config["claude"]
    grok = config["grok"]
    codex_safe_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_SAFE)
    codex_work_policy = delegate_config.effective_policy(config, engine="codex", mode=MODE_WORK)
    claude_safe_policy = _claude_runtime_policy(config, MODE_SAFE)
    claude_work_policy = _claude_runtime_policy(config, MODE_WORK)
    grok_safe_policy = _grok_runtime_policy(config, MODE_SAFE)
    grok_work_policy = _grok_runtime_policy(config, MODE_WORK)
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
    grok_safe_argv = _grok_describe_argv(
        config,
        grok,
        mode=MODE_SAFE,
        workspace="<isolated-workspace>",
        policy=grok_safe_policy,
    )
    grok_work_argv = _grok_describe_argv(
        config,
        grok,
        mode=MODE_WORK,
        workspace="<workspace>",
        policy=grok_work_policy,
    )
    return {
        "ok": True,
        "summary": False,
        "version": VERSION,
        "runtime": runtime_payload(),
        "configPath": str(config_path()),
        "configSource": config_source,
        "configResolution": config_resolution_payload(config_source, workspace),
        "engines": list(KNOWN_ENGINES),
        "isolationValues": list(delegate_config.VALID_ISOLATION_VALUES),
        "policyProfiles": list(delegate_config.POLICY_PROFILES),
        "policyFieldSupport": _policy_field_support_matrix(),
        "engineCapabilities": _engine_capabilities(),
        "effectivePolicy": {
            "codex": {
                "safe": codex_safe_policy,
                "work": codex_work_policy,
            },
            "claude": {
                "safe": claude_safe_policy,
                "work": claude_work_policy,
            },
            "grok": {
                "safe": grok_safe_policy,
                "work": grok_work_policy,
            },
        },
        "modes": [MODE_SAFE, MODE_WORK, MODE_CALL],
        "promptSources": ["direct", "prompt-file", "stdin"],
        "promptTransports": {
            "cursor": PROMPT_TRANSPORT_ARGV,
            "droid": PROMPT_TRANSPORT_FILE,
            "codex": PROMPT_TRANSPORT_STDIN,
            "kimi": PROMPT_TRANSPORT_ARGV,
            "claude": PROMPT_TRANSPORT_STDIN,
            "grok": PROMPT_TRANSPORT_FILE,
        },
        "globalOptions": _global_options(),
        "launchOptions": _launch_options(),
        "completionReportModes": list(delegate_config.COMPLETION_REPORT_MODES),
        "promptTransforms": [
            "Always prepends mandatory skill review instructions before the operator prompt.",
            "Optionally appends completion-report instructions unless disabled.",
        ],
        "profiles": _profiles_config_payload(config),
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
                "grok": False,
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
                    SAFE_WORKSPACE_SYNC_NOTE,
                    "No --mode=plan, --mode=ask, --force, or --approve-mcps.",
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
                    SAFE_WORKSPACE_SYNC_NOTE,
                    "No --auto, --use-spec, or --skip-permissions-unsafe in safe mode.",
                    "Uses a read-only safety prompt; --isolation none is normalized to auto for Droid safe mode.",
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
                    SAFE_WORKSPACE_SYNC_NOTE,
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
                    SAFE_WORKSPACE_SYNC_NOTE,
                    "Uses Claude Code -p with --permission-mode plan, --strict-mcp-config, Read/Grep/Glob, and selected read-only Bash tools.",
                    "Prompt is delivered on stdin; dry-run argv and manifests do not contain the prompt.",
                    "Delegate does not prove Claude hooks, plugins, or user settings are disabled; keep safe-mode work review-only.",
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
                    SAFE_WORKSPACE_SYNC_NOTE,
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
            "grok": {
                "safe": grok_safe_argv,
                "safeNotes": [
                    SAFE_WORKSPACE_SYNC_NOTE,
                    "Uses Grok --prompt-file with Delegate temp prompt materialization; dry-run argv shows <prompt file>.",
                    "Safe mode uses Delegate isolated copy plus Grok read-only sandbox/permission controls; it does not use Grok plan mode.",
                    "Grok safe mode disables web search by default; explicit policy.harness.grok.safe.webSearch=true re-enables network egress, and Delegate safe-mode isolation is filesystem-only.",
                    "The isolated workspace is the effective write boundary; Grok sandbox/permission flags are advisory defense-in-depth.",
                ],
                "work": grok_work_argv,
                "workNotes": [
                    "Uses grok.workPermissionMode unless policy.harness.grok.work.bypassApprovalsAndSandbox explicitly requests bypassPermissions.",
                    "Reasoning effort maps to Grok --effort (low, medium, high, xhigh, max).",
                    "Tracked runs use --output-format streaming-json; pass-through uses plain.",
                ],
            },
        },
        "commands": _commands_catalog(),
        "recommendedDiscovery": [
            "delegate --json describe --summary",
            "delegate --json models --summary",
            "delegate --json help <command>",
        ],
    }


def describe_summary_payload(
    config: JsonObject,
    config_source: str,
    workspace: Path | None = None,
) -> JsonObject:
    return {
        "ok": True,
        "summary": True,
        "version": VERSION,
        "configSource": config_source,
        "configResolution": config_resolution_payload(config_source, workspace),
        "engines": list(KNOWN_ENGINES),
        "modes": [MODE_SAFE, MODE_WORK, MODE_CALL],
        "isolationValues": list(delegate_config.VALID_ISOLATION_VALUES),
        "profiles": _profiles_config_payload(config),
        "globalOptions": _global_options(),
        "launchOptions": _launch_options(),
        "commands": _commands_catalog(),
        "recommendedDiscovery": [
            "delegate --json describe --summary",
            "delegate --json models --summary",
            "delegate --json help <command>",
        ],
    }


def _emit_models_text(payload: JsonObject, config_source: str, stdout: TextIO) -> None:
    if config_source == "embedded-default":
        print("warning: using embedded default config", file=stdout)
    cursor = payload.get("cursor")
    if isinstance(cursor, dict):
        print(
            "cursor: "
            f"{_text_or_none(cursor.get('defaultModel'))} "
            f"({_text_argv_prefix_label(cursor.get('argvPrefix'))})",
            file=stdout,
        )
    print("droid:", file=stdout)
    droid = payload.get("droid")
    if isinstance(droid, dict):
        models = droid.get("models")
        if isinstance(models, dict):
            for alias, model_id in sorted(models.items()):
                print(f"  {alias} -> {_text_or_none(model_id)}", file=stdout)
    codex = payload.get("codex")
    if isinstance(codex, dict):
        profile = codex.get("profile")
        profile_label = _text_or_none(profile)
        print(
            "codex: "
            f"binary={_text_or_none(codex.get('binary'))} "
            f"defaultModel={_text_or_none(codex.get('defaultModel'))} "
            f"profile={profile_label}",
            file=stdout,
        )
    claude = payload.get("claude")
    if isinstance(claude, dict):
        print(
            "claude: "
            f"binary={_text_or_none(claude.get('binary'))} "
            f"defaultModel={_text_or_none(claude.get('defaultModel'))} "
            f"workPermissionMode={claude.get('workPermissionMode')}",
            file=stdout,
        )
    grok = payload.get("grok")
    if isinstance(grok, dict):
        print(
            "grok: "
            f"binary={_text_or_none(grok.get('binary'))} "
            f"defaultModel={_text_or_none(grok.get('defaultModel'))} "
            f"workPermissionMode={grok.get('workPermissionMode')}",
            file=stdout,
        )
    kimi = payload.get("kimi")
    if isinstance(kimi, dict):
        print(
            "kimi: "
            f"binary={_text_or_none(kimi.get('binary'))} "
            f"defaultModel={_text_or_none(kimi.get('defaultModel'))}",
            file=stdout,
        )
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        print(
            f"runtime: {_text_or_none(runtime.get('modulePath'))}",
            file=stdout,
        )


def emit_models(
    config: JsonObject,
    config_source: str,
    json_mode: bool,
    stdout: TextIO,
    *,
    workspace: Path | None = None,
    summary: bool = False,
) -> int:
    if summary:
        payload = _scrub_discovery_payload(models_summary_payload(config, config_source, workspace))
        if json_mode:
            delegate_rendering.print_json(payload, stdout)
        else:
            aliases = payload.get("aliases")
            if isinstance(aliases, list):
                for item in aliases:
                    if not isinstance(item, dict):
                        continue
                    warnings = item.get("warnings")
                    suffix = f" warnings={len(warnings)}" if isinstance(warnings, list) else ""
                    print(
                        f"{item['provider']}:{item['alias']} safe={item['safeSupported']} work={item['workSupported']}{suffix}",
                        file=stdout,
                    )
        return EXIT_OK
    payload = _scrub_discovery_payload(models_payload(config, config_source, workspace))
    if json_mode:
        delegate_rendering.print_json(payload, stdout)
        return EXIT_OK
    _emit_models_text(payload, config_source, stdout)
    return EXIT_OK


def emit_describe(
    config: JsonObject,
    config_source: str,
    json_mode: bool,
    stdout: TextIO,
    *,
    workspace: Path | None = None,
    summary: bool = False,
) -> int:
    raw_payload = (
        describe_summary_payload(config, config_source, workspace)
        if summary
        else describe_payload(config, config_source, workspace)
    )
    payload = _scrub_discovery_payload(raw_payload)
    if json_mode:
        delegate_rendering.print_json(payload, stdout)
        return EXIT_OK
    if summary:
        print(f"delegate {payload['version']} summary", file=stdout)
        print(f"config: {payload['configSource']}", file=stdout)
        print(f"engines: {', '.join(payload['engines'])}", file=stdout)
        print(f"modes: {', '.join(payload['modes'])}", file=stdout)
        print(f"launch options: {', '.join(payload['launchOptions'])}", file=stdout)
        print("recommended discovery:", file=stdout)
        for command in payload["recommendedDiscovery"]:
            print(f"  {command}", file=stdout)
        return EXIT_OK
    print(f"delegate {VERSION}", file=stdout)
    print(f"config: {payload['configPath']} ({payload['configSource']})", file=stdout)
    print(f"runtime: {payload['runtime']['modulePath']}", file=stdout)
    print("engines: cursor, droid, codex, kimi, claude, grok", file=stdout)
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
        f"""Use delegate for bounded execution tasks only.

Good defaults:
  delegate cursor work "Implement the scoped task; report changed files and tests."
  delegate cursor safe "Review this diff for regressions; report findings with file/line/severity."
  delegate droid <alias> safe "Investigate this issue; do not edit."
  delegate droid <alias> work "Implement this bounded change; run the named check."
  delegate codex safe "Review this workspace. Do not edit files."
  delegate codex work "Implement the scoped fix, run the named check, and report changed files."
  delegate claude safe "Review this workspace. Do not edit files."
  delegate claude work "Implement the scoped fix, run the named check, and report changed files."
  delegate grok safe "Review this workspace. Do not edit files."
  delegate grok work "Implement the scoped fix, run the named check, and report changed files."
  delegate kimi safe "Review this repo for regressions; report file/line/severity."
  delegate kimi work "Implement the scoped task; report changed files and tests."

Kimi:
  - {SAFE_WORKSPACE_SYNC_NOTE}
  - Model selection uses kimi.defaultModel in config or optional JSON input model; no CLI model alias in v1.
  - Reasoning effort is unsupported for Kimi in v1.
  - No CLI workspace flag; Delegate sets subprocess cwd.

Codex:
  - {SAFE_WORKSPACE_SYNC_NOTE}
  - Model selection uses codex.defaultModel in config or optional JSON input model; no CLI model alias in v1.
  - Codex profile (codex.profile) is config-only; run input JSON must not include profile.

Claude:
  - Uses Claude Code headless mode: claude -p with prompt delivered on stdin.
  - {SAFE_WORKSPACE_SYNC_NOTE}
  - Claude safe mode runs with --permission-mode plan, --strict-mcp-config,
    Read/Grep/Glob, and selected read-only Bash tools.
    Delegate does not currently prove that Claude Code hooks, plugins, user
    settings, or other non-MCP customization surfaces are disabled.
  - Work mode uses claude.workPermissionMode, or bypassPermissions only when
    Delegate policy explicitly enables policy.harness.claude.work.bypassApprovalsAndSandbox.
  - Reasoning effort maps to Claude Code --effort (low, medium, high, xhigh, max).

Grok:
  - Uses Grok Build CLI with --prompt-file; Delegate materializes the effective prompt in a temp file.
  - {SAFE_WORKSPACE_SYNC_NOTE}
  - Safe mode uses Delegate isolated copy plus Grok read-only sandbox/permission controls; not Grok plan mode.
  - Work mode uses grok.workPermissionMode, or bypassPermissions only when
    Delegate policy explicitly enables policy.harness.grok.work.bypassApprovalsAndSandbox.
  - Reasoning effort maps to Grok --effort (low, medium, high, xhigh, max).
  - Tracked runs use streaming-json; pass-through uses plain output.
  - --output-schema is unsupported in v1 because Grok --json-schema forces final json output.

Droid modes:
  - {SAFE_WORKSPACE_SYNC_NOTE}
  - Droid safe mode remains read-only: no --auto, --use-spec, or unsafe skip.
  - Uses Factory Droid --skip-permissions-unsafe, not --auto high.
  - Work mode is intentionally no-prompt; use only for bounded tasks in workspaces you trust.
  - --reasoning-effort requires a resolved Droid model alias from droid.models.

Cursor safe mode:
  - {SAFE_WORKSPACE_SYNC_NOTE}
  - Uses default Cursor Agent behavior, not plan/ask mode.
  - The child runs in the isolated copy; tracked runs may still write .delegate metadata in the source workspace.

Profiles (auth/env switching):
  - A profile selects which credentials/env every spawned harness inherits; the active profile is detected from env (profiles.detectFrom) or set explicitly with --auth-profile NAME before the subcommand.
  - --auth-profile applies to launches, dry-run, run, profiles, and capabilities refresh; it is rejected for run-inspection, worktree, discovery, and the cached capabilities report.
  - delegate profiles (optionally with --json) reports the resolved profile, source, and non-secret env keys; it never mutates config.
  - profiles.definitions.<name>.env holds non-secret pointers only (e.g. CODEX_HOME); secret-shaped keys are rejected at config load, and values must not interpolate secrets via $VAR. Export real credentials in the shell instead.

Rules for agents:
  - Keep prompts bounded: task, scope, verification, report format.
  - Delegate always prepends a mandatory skill-review instruction before your prompt.
  - Use --prompt-file or delegate --json run --input-json for long prompts.
  - Run from the target workspace, or pass --cwd before the subcommand.
  - Inside Git, --cwd resolves to the repo root; outside Git, the directory is used directly.
  - Always review diffs after work mode when Git is available; outside Git, manually review changed files.
  - Do not use delegate for production deploys or repository publishing unless the operator explicitly asks.
  - Launch normally; do not pipe delegate launches through tail just to suppress noise.
  - For long tracked foreground runs, add --progress before prompt text to get bounded, redacted stderr heartbeats.
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
  delegate snapshot cursor-1
  delegate run-output cursor-1 --completion-report

Discovery:
  delegate --json models --summary
  delegate --json describe --summary
  delegate --json models        # full/raw details when needed
  delegate --json describe      # full/raw details when needed
  delegate agent-help
""".rstrip(),
        file=stdout,
    )
    return EXIT_OK
