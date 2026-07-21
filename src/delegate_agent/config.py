from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
from typing import Final

from delegate_agent import reasoning, redaction, wsl
from delegate_agent.constants import VALID_MODES
from delegate_agent.json_types import JsonObject, JsonValue, is_non_negative_int

DEFAULT_CONFIG_PATH: Path | None = None
DEFAULT_CONFIG_RELATIVE: Final = Path(".delegate") / "config.json"
WORKSPACE_CONFIG_RELATIVE = Path(".delegate") / "config.json"
CONFIG_ENV = "DELEGATE_CONFIG"
COMPLETION_REPORT_MODE_MARKDOWN = "markdown"
COMPLETION_REPORT_MODE_NONE = "none"
COMPLETION_REPORT_MODES = (
    COMPLETION_REPORT_MODE_MARKDOWN,
    COMPLETION_REPORT_MODE_NONE,
)

ISOLATION_AUTO = "auto"
ISOLATION_NONE = "none"
ISOLATION_WORKTREE = "worktree"
VALID_ISOLATION_VALUES = (ISOLATION_AUTO, ISOLATION_NONE, ISOLATION_WORKTREE)
SAFE_ISOLATION_REQUIRED_ENGINES = frozenset(
    {"cursor", "droid", "kimi", "claude", "grok", "devin", "opencode", "pi", "omp"}
)

POLICY_PROFILES = ("safe", "trusted-hooks", "external-sandbox", "custom")
POLICY_MODE_KEYS = frozenset(
    {
        "networkAccess",
        "webSearch",
        "bypassApprovalsAndSandbox",
        "bypassHookTrust",
    }
)
# Bypass flags are escalations that only make sense for edit-capable work runs.
# Safe mode is read-only by promise, so reject them in any safe-mode policy block.
SAFE_FORBIDDEN_BYPASS_KEYS = ("bypassApprovalsAndSandbox", "bypassHookTrust")
CODEX_WORK_SANDBOX_VALUES = ("read-only", "workspace-write", "danger-full-access")
CLAUDE_WORK_PERMISSION_MODES = ("acceptEdits", "auto", "default", "dontAsk", "plan")
GROK_PERMISSION_MODES = CLAUDE_WORK_PERMISSION_MODES
GROK_BYPASS_PERMISSION_MODE = "bypassPermissions"
GROK_SAFE_SANDBOX_VALUES = ("read-only", "strict")
GROK_WORK_SANDBOX_VALUES = ("workspace", "devbox", "read-only", "strict")

DEFAULT_MODE_POLICY: JsonObject = {
    "networkAccess": False,
    "webSearch": False,
    "bypassApprovalsAndSandbox": False,
    "bypassHookTrust": False,
}

_EMBEDDED_DEFAULT_CONFIG: JsonObject = {
    "tracking": {
        "completionReport": {
            "defaultMode": COMPLETION_REPORT_MODE_MARKDOWN,
        },
        "retention": {
            "enabled": True,
            "rawLogDays": 7,
        },
    },
    "cursor": {
        "argvPrefix": ["agent"],
        "defaultModel": "composer-2.5",
        "defaultReasoningEffort": None,
        "reasoningEffortModels": {},
        "models": {},
    },
    "droid": {
        "binary": "droid",
        "models": {},
        "defaultReasoningEffort": None,
    },
    "reasoning": {
        "capabilities": {},
    },
    "kimi": {
        "binary": "kimi",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "models": {},
    },
    "claude": {
        "binary": "claude",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "workPermissionMode": "auto",
        "noSessionPersistence": True,
        "bare": False,
        "models": {},
    },
    "grok": {
        "binary": "grok",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "workPermissionMode": "auto",
        "safePermissionMode": "dontAsk",
        "safeSandbox": "read-only",
        "workSandbox": None,
        "disableWebSearch": True,
        "noSubagents": False,
        "models": {},
    },
    "devin": {
        "binary": "devin",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "models": {},
    },
    "opencode": {
        "binary": "opencode",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "defaultAgent": None,
        "models": {},
    },
    "pi": {
        "binary": "pi",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "models": {},
    },
    "omp": {
        "binary": "omp",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "models": {},
    },
    "policy": {
        "profile": "safe",
        "work": {
            "networkAccess": True,
        },
    },
    "codex": {
        "binary": "codex",
        "defaultModel": None,
        "defaultReasoningEffort": None,
        "profile": None,
        "fallbackProfile": None,
        "workSandbox": "workspace-write",
        "ephemeral": True,
        "ignoreUserConfig": False,
        "models": {},
    },
    "profiles": {
        "detectFrom": ["DELEGATE_PROFILE", "AI_PROFILE"],
        "default": None,
        "definitions": {},
    },
    "isolation": {
        "safe": ISOLATION_AUTO,
        "work": ISOLATION_NONE,
    },
    "worktrees": {
        "dataHome": None,
        "poolWarnCount": 20,
        "autoPrune": {
            "enabled": False,
            "mergedOlderThanDays": 7,
        },
    },
    "progress": {
        "enabled": False,
        "initialDelaySec": 30,
        "intervalSec": 60,
    },
    "workflows": {
        "engineCaps": {},
        "itemThreads": 64,
        "structuredOutputRetries": 2,
    },
}


def embedded_default_config() -> JsonObject:
    """Return a fresh copy of Delegate's embedded default configuration."""
    return copy.deepcopy(_EMBEDDED_DEFAULT_CONFIG)


def example_config() -> JsonObject:
    """Return an editable starter config equivalent to config.example.json."""

    config = embedded_default_config()
    droid = config["droid"]
    if isinstance(droid, dict):
        droid["models"] = {
            "reviewer": "replace-with-read-only-model-id",
            "implementer": "replace-with-edit-capable-model-id",
        }
    profiles = config["profiles"]
    if isinstance(profiles, dict):
        profiles["definitions"] = {
            "work": {"env": {"CODEX_HOME": "~/replace-with-work-codex-home"}},
            "personal": {"env": {"CODEX_HOME": "~/replace-with-personal-codex-home"}},
        }
    return config


def _embedded_progress_default(key: str) -> float:
    progress = _EMBEDDED_DEFAULT_CONFIG["progress"]
    if not isinstance(progress, dict):
        raise AssertionError("embedded progress defaults must be an object")
    value = progress[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssertionError(f"embedded progress.{key} must be numeric")
    return float(value)


def default_progress_initial_delay_sec() -> float:
    return _embedded_progress_default("initialDelaySec")


def default_progress_interval_sec() -> float:
    return _embedded_progress_default("intervalSec")


DEFAULT_CONFIG: JsonObject = embedded_default_config()


def _profile_policy(profile: str) -> JsonObject:
    if profile == "trusted-hooks":
        return {"work": {"bypassHookTrust": True}}
    if profile == "external-sandbox":
        return {
            "work": {
                "bypassApprovalsAndSandbox": True,
                "bypassHookTrust": True,
            }
        }
    return {}


def effective_policy(config: JsonObject, *, engine: str, mode: str) -> JsonObject:
    policy = config.get("policy", {})
    if not isinstance(policy, dict):
        policy = {}
    profile = policy.get("profile", "safe")
    profile_defaults = _profile_policy(profile if isinstance(profile, str) else "safe")
    profile_mode = profile_defaults.get(mode)
    mode_policy = deep_merge(
        DEFAULT_MODE_POLICY,
        profile_mode if isinstance(profile_mode, dict) else {},
    )
    explicit_mode = policy.get(mode)
    if isinstance(explicit_mode, dict):
        mode_policy = deep_merge(mode_policy, explicit_mode)
    harness = policy.get("harness")
    if isinstance(harness, dict):
        engine_policy = harness.get(engine)
        if isinstance(engine_policy, dict):
            mode_override = engine_policy.get(mode)
            if isinstance(mode_override, dict):
                mode_policy = deep_merge(mode_policy, mode_override)
    return mode_policy


def _validate_policy_mode_policy(mode_policy: JsonObject, path: str, *, mode: str) -> None:
    if not isinstance(mode_policy, dict):
        raise ConfigError("invalid_policy_config", f"{path} must be an object.")
    unknown = set(mode_policy) - POLICY_MODE_KEYS
    if unknown:
        raise ConfigError(
            "invalid_policy_config",
            f"{path} has unknown keys: {', '.join(sorted(unknown))}.",
        )
    for key in POLICY_MODE_KEYS:
        value = mode_policy.get(key)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                "invalid_policy_config",
                f"{path}.{key} must be a boolean.",
            )
    if mode == "safe":
        for key in SAFE_FORBIDDEN_BYPASS_KEYS:
            if mode_policy.get(key) is True:
                raise ConfigError(
                    "invalid_policy_config",
                    f"{path}.{key} cannot be enabled in safe mode; safe mode is read-only.",
                )


def _validate_policy_section(policy: JsonValue, *, path: str = "policy") -> None:
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise ConfigError("invalid_policy_config", f"{path} must be an object.")
    profile = policy.get("profile", "safe")
    if profile not in POLICY_PROFILES:
        raise ConfigError(
            "invalid_policy_config",
            f"{path}.profile must be one of: {', '.join(POLICY_PROFILES)}.",
        )
    for mode in ("safe", "work"):
        mode_policy = policy.get(mode)
        if mode_policy is not None:
            _validate_policy_mode_policy(mode_policy, f"{path}.{mode}", mode=mode)
    harness = policy.get("harness")
    if harness is not None:
        if not isinstance(harness, dict):
            raise ConfigError("invalid_policy_config", f"{path}.harness must be an object.")
        for engine, engine_policy in harness.items():
            if not isinstance(engine, str) or not engine:
                raise ConfigError(
                    "invalid_policy_config",
                    f"{path}.harness engine names must be non-empty strings.",
                )
            if not isinstance(engine_policy, dict):
                raise ConfigError(
                    "invalid_policy_config",
                    f"{path}.harness.{engine} must be an object.",
                )
            for mode in ("safe", "work"):
                mode_policy = engine_policy.get(mode)
                if mode_policy is not None:
                    _validate_policy_mode_policy(
                        mode_policy,
                        f"{path}.harness.{engine}.{mode}",
                        mode=mode,
                    )


def _validate_isolation_section(isolation: JsonValue) -> None:
    if isolation is None:
        return
    if not isinstance(isolation, dict):
        raise ConfigError(
            "invalid_isolation_config",
            "isolation config must be an object.",
        )
    for mode in ("safe", "work"):
        if mode not in isolation:
            continue
        value = isolation[mode]
        if value is None:
            raise ConfigError(
                "invalid_isolation_config",
                f"isolation.{mode} must not be null; use one of: {', '.join(VALID_ISOLATION_VALUES)}.",
            )
        if value not in VALID_ISOLATION_VALUES:
            raise ConfigError(
                "invalid_isolation_config",
                f"isolation.{mode} must be one of: {', '.join(VALID_ISOLATION_VALUES)}.",
            )


def _validate_required_non_negative_int(
    value: JsonValue,
    *,
    path: str,
    error: str,
) -> None:
    if value is None:
        raise ConfigError(error, f"{path} must not be null.")
    if not is_non_negative_int(value):
        raise ConfigError(error, f"{path} must be a non-negative integer.")


def _validate_progress_section(progress: JsonValue) -> None:
    if progress is None:
        return
    if not isinstance(progress, dict):
        raise ConfigError("invalid_progress_config", "progress config must be an object.")
    if "enabled" in progress:
        enabled = progress["enabled"]
        if not isinstance(enabled, bool):
            raise ConfigError(
                "invalid_progress_config",
                "progress.enabled must be a boolean.",
            )
    for key in ("initialDelaySec", "intervalSec"):
        if key not in progress:
            continue
        value = progress[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ConfigError(
                "invalid_progress_config",
                f"progress.{key} must be a positive number.",
            )


def _validate_worktrees_section(worktrees: JsonValue) -> None:
    if worktrees is None:
        return
    if not isinstance(worktrees, dict):
        raise ConfigError(
            "invalid_worktrees_config",
            "worktrees config must be an object.",
        )
    data_home = worktrees.get("dataHome")
    if data_home is not None and not (isinstance(data_home, str) and data_home):
        raise ConfigError(
            "invalid_worktrees_config",
            "worktrees.dataHome must be null or a non-empty string.",
        )
    if isinstance(data_home, str) and data_home:
        if wsl.should_reject_windows_path(data_home):
            raise ConfigError(
                "windows_path",
                wsl.windows_path_message("worktrees.dataHome", data_home),
            )
        expanded = Path(data_home).expanduser()
        if not expanded.is_absolute():
            raise ConfigError(
                "invalid_worktrees_config",
                "worktrees.dataHome must be an absolute path or start with ~/.",
            )
    if "poolWarnCount" in worktrees:
        _validate_required_non_negative_int(
            worktrees["poolWarnCount"],
            path="worktrees.poolWarnCount",
            error="invalid_worktrees_config",
        )
    auto_prune = worktrees.get("autoPrune")
    if auto_prune is not None:
        if not isinstance(auto_prune, dict):
            raise ConfigError(
                "invalid_worktrees_config",
                "worktrees.autoPrune must be an object.",
            )
        if "enabled" in auto_prune:
            enabled = auto_prune["enabled"]
            if enabled is None:
                raise ConfigError(
                    "invalid_worktrees_config",
                    "worktrees.autoPrune.enabled must not be null.",
                )
            if not isinstance(enabled, bool):
                raise ConfigError(
                    "invalid_worktrees_config",
                    "worktrees.autoPrune.enabled must be a boolean.",
                )
        if "mergedOlderThanDays" in auto_prune:
            _validate_required_non_negative_int(
                auto_prune["mergedOlderThanDays"],
                path="worktrees.autoPrune.mergedOlderThanDays",
                error="invalid_worktrees_config",
            )


def _validate_workflows_section(workflows: JsonValue) -> None:
    if workflows is None:
        return
    if not isinstance(workflows, dict):
        raise ConfigError("invalid_workflows_config", "workflows config must be an object.")
    engine_caps = workflows.get("engineCaps")
    if engine_caps is not None:
        if not isinstance(engine_caps, dict):
            raise ConfigError("invalid_workflows_config", "workflows.engineCaps must be an object.")
        for engine, cap in engine_caps.items():
            if not isinstance(engine, str) or not engine:
                raise ConfigError(
                    "invalid_workflows_config",
                    "workflows.engineCaps keys must be non-empty engine names.",
                )
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise ConfigError(
                    "invalid_workflows_config",
                    f"workflows.engineCaps.{engine} must be a positive integer.",
                )
    for key in ("itemThreads", "structuredOutputRetries"):
        if key not in workflows:
            continue
        value = workflows[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                "invalid_workflows_config",
                f"workflows.{key} must be a non-negative integer.",
            )


def _profiles_definitions(profiles: JsonValue) -> dict[str, JsonObject]:
    if not isinstance(profiles, dict):
        return {}
    definitions = profiles.get("definitions")
    if not isinstance(definitions, dict):
        return {}
    return {
        name.strip(): entry
        for name, entry in definitions.items()
        if isinstance(name, str) and name.strip() and isinstance(entry, dict)
    }


def _validate_profiles_section(profiles: JsonValue) -> None:
    if profiles is None:
        return
    if not isinstance(profiles, dict):
        raise ConfigError("invalid_profiles_config", "profiles config must be an object.")
    detect_from = profiles.get("detectFrom", [])
    if not isinstance(detect_from, list) or not all(
        isinstance(item, str) and item.strip() for item in detect_from
    ):
        raise ConfigError(
            "invalid_profiles_config",
            "profiles.detectFrom must be an array of non-empty strings.",
        )
    definitions = profiles.get("definitions", {})
    if not isinstance(definitions, dict):
        raise ConfigError("invalid_profiles_config", "profiles.definitions must be an object.")
    normalized_names: set[str] = set()
    for name, entry in definitions.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                "invalid_profiles_config",
                "profiles.definitions keys must be non-empty strings.",
            )
        normalized_name = name.strip()
        normalized_names.add(normalized_name)
        if not isinstance(entry, dict):
            raise ConfigError(
                "invalid_profiles_config",
                f"profiles.definitions.{normalized_name} must be an object.",
            )
        unknown = set(entry) - {"env"}
        if unknown:
            raise ConfigError(
                "invalid_profiles_config",
                f"profiles.definitions.{normalized_name} has unknown keys: "
                f"{', '.join(sorted(unknown))}.",
            )
        env = entry.get("env", {})
        if not isinstance(env, dict):
            raise ConfigError(
                "invalid_profiles_config",
                f"profiles.definitions.{normalized_name}.env must be an object.",
            )
        for key, value in env.items():
            path = f"profiles.definitions.{normalized_name}.env.{key}"
            if not isinstance(key, str) or not key.strip():
                raise ConfigError(
                    "invalid_profiles_config",
                    f"profiles.definitions.{normalized_name}.env keys must be non-empty strings.",
                )
            if redaction.key_looks_secret(key):
                raise ConfigError(
                    "secret_in_profile_env",
                    f"{path} looks like a secret. Export secrets in the shell; Phase-2 envFile "
                    "will support secret files.",
                )
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    "invalid_profiles_config",
                    f"{path} must be a non-empty string.",
                )
            if key.strip() == "CODEX_HOME" and wsl.should_reject_windows_path(value):
                raise ConfigError("windows_path", wsl.windows_path_message(path, value))
    default = profiles.get("default")
    if default is not None and (not isinstance(default, str) or default not in normalized_names):
        raise ConfigError(
            "invalid_profiles_config",
            "profiles.default must be null or a defined profile name.",
        )


def _validate_codex_section(codex: JsonValue) -> None:
    if not isinstance(codex, dict):
        raise ConfigError("invalid_codex_config", "codex config must be an object.")
    require_non_empty_str(codex.get("binary"), path="codex.binary", error="invalid_codex_config")
    optional_str(codex.get("defaultModel"), path="codex.defaultModel", error="invalid_codex_config")
    _validate_provider_default_reasoning_effort(
        codex.get("defaultReasoningEffort"),
        path="codex.defaultReasoningEffort",
        error="invalid_codex_config",
    )
    optional_str(codex.get("profile"), path="codex.profile", error="invalid_codex_config")
    optional_str(
        codex.get("fallbackProfile"), path="codex.fallbackProfile", error="invalid_codex_config"
    )
    work_sandbox = codex.get("workSandbox", "workspace-write")
    if work_sandbox not in CODEX_WORK_SANDBOX_VALUES:
        raise ConfigError(
            "invalid_codex_config",
            "codex.workSandbox must be read-only, workspace-write, or danger-full-access.",
        )
    if "safeSandbox" in codex:
        raise ConfigError(
            "invalid_codex_config",
            "codex.safeSandbox is not supported; Codex safe always uses read-only.",
        )
    require_bool(codex.get("ephemeral", True), path="codex.ephemeral", error="invalid_codex_config")
    require_bool(
        codex.get("ignoreUserConfig", False),
        path="codex.ignoreUserConfig",
        error="invalid_codex_config",
    )
    _validate_engine_models(codex.get("models"), engine="codex", error="invalid_codex_config")


def _validate_codex_profile_references(config: JsonObject) -> None:
    codex = config.get("codex")
    if not isinstance(codex, dict):
        return
    fallback = codex.get("fallbackProfile")
    if fallback is None:
        return
    if not isinstance(fallback, str) or not fallback.strip():
        return
    fallback = fallback.strip()
    definitions = _profiles_definitions(config.get("profiles"))
    profile = definitions.get(fallback)
    if profile is None:
        raise ConfigError(
            "invalid_codex_config",
            f"codex.fallbackProfile {fallback!r} is not defined in profiles.definitions.",
        )
    env = profile.get("env")
    if (
        not isinstance(env, dict)
        or not isinstance(env.get("CODEX_HOME"), str)
        or not env["CODEX_HOME"].strip()
    ):
        raise ConfigError(
            "profile_missing_codex_home",
            f"codex.fallbackProfile {fallback!r} must define profiles.definitions.{fallback}.env.CODEX_HOME.",
        )


def _validate_kimi_section(kimi: JsonValue) -> None:
    if not isinstance(kimi, dict):
        raise ConfigError("invalid_kimi_config", "kimi config must be an object.")
    require_non_empty_str(kimi.get("binary"), path="kimi.binary", error="invalid_kimi_config")
    optional_str(kimi.get("defaultModel"), path="kimi.defaultModel", error="invalid_kimi_config")
    if kimi.get("defaultReasoningEffort") is not None:
        raise ConfigError(
            "invalid_kimi_config",
            "kimi.defaultReasoningEffort is not supported; set it to null.",
        )
    _validate_engine_models(kimi.get("models"), engine="kimi", error="invalid_kimi_config")


def _validate_claude_section(claude: JsonValue) -> None:
    if not isinstance(claude, dict):
        raise ConfigError("invalid_claude_config", "claude config must be an object.")
    require_non_empty_str(claude.get("binary"), path="claude.binary", error="invalid_claude_config")
    optional_str(
        claude.get("defaultModel"), path="claude.defaultModel", error="invalid_claude_config"
    )
    default_effort = claude.get("defaultReasoningEffort")
    if default_effort is not None:
        if not isinstance(default_effort, str):
            raise ConfigError(
                "invalid_claude_config",
                "claude.defaultReasoningEffort must be a string or null.",
            )
        try:
            reasoning.resolve_claude_native_effort(default_effort)
        except reasoning.ReasoningCapabilityError as exc:
            raise ConfigError(
                "invalid_claude_config",
                f"claude.defaultReasoningEffort: {exc.message}",
            ) from exc
    permission_mode = claude.get("workPermissionMode", "auto")
    if permission_mode == "bypassPermissions":
        raise ConfigError(
            "invalid_claude_config",
            "claude.workPermissionMode cannot be bypassPermissions; "
            "use policy.harness.claude.work.bypassApprovalsAndSandbox "
            "for explicit Delegate-controlled bypass.",
        )
    if permission_mode not in CLAUDE_WORK_PERMISSION_MODES:
        raise ConfigError(
            "invalid_claude_config",
            f"claude.workPermissionMode must be one of: {', '.join(CLAUDE_WORK_PERMISSION_MODES)}.",
        )
    require_bool(
        claude.get("noSessionPersistence", True),
        path="claude.noSessionPersistence",
        error="invalid_claude_config",
    )
    require_bool(claude.get("bare", False), path="claude.bare", error="invalid_claude_config")
    _validate_engine_models(claude.get("models"), engine="claude", error="invalid_claude_config")


def _validate_grok_section(grok: JsonValue) -> None:
    if not isinstance(grok, dict):
        raise ConfigError("invalid_grok_config", "grok config must be an object.")
    require_non_empty_str(grok.get("binary"), path="grok.binary", error="invalid_grok_config")
    optional_str(grok.get("defaultModel"), path="grok.defaultModel", error="invalid_grok_config")
    default_effort = grok.get("defaultReasoningEffort")
    if default_effort is not None:
        if not isinstance(default_effort, str):
            raise ConfigError(
                "invalid_grok_config",
                "grok.defaultReasoningEffort must be a string or null.",
            )
        try:
            reasoning.resolve_grok_native_effort(default_effort)
        except reasoning.ReasoningCapabilityError as exc:
            raise ConfigError(
                "invalid_grok_config",
                f"grok.defaultReasoningEffort: {exc.message}",
            ) from exc
    safe_permission = grok.get("safePermissionMode", "dontAsk")
    if safe_permission == "plan":
        raise ConfigError(
            "invalid_grok_config",
            "grok.safePermissionMode cannot be plan; Delegate safe mode uses isolated "
            "workspace plus read-only sandbox controls instead of Grok plan mode.",
        )
    if safe_permission not in ("dontAsk", "default", "auto"):
        raise ConfigError(
            "invalid_grok_config",
            "grok.safePermissionMode must be dontAsk, default, or auto.",
        )
    work_permission = grok.get("workPermissionMode", "auto")
    if work_permission == GROK_BYPASS_PERMISSION_MODE:
        raise ConfigError(
            "invalid_grok_config",
            "grok.workPermissionMode cannot be bypassPermissions; "
            "use policy.harness.grok.work.bypassApprovalsAndSandbox "
            "for explicit Delegate-controlled bypass.",
        )
    if work_permission not in GROK_PERMISSION_MODES:
        raise ConfigError(
            "invalid_grok_config",
            f"grok.workPermissionMode must be one of: {', '.join(GROK_PERMISSION_MODES)}.",
        )
    safe_sandbox = grok.get("safeSandbox", "read-only")
    if safe_sandbox not in GROK_SAFE_SANDBOX_VALUES:
        raise ConfigError(
            "invalid_grok_config",
            f"grok.safeSandbox must be one of: {', '.join(GROK_SAFE_SANDBOX_VALUES)}.",
        )
    work_sandbox = grok.get("workSandbox")
    if work_sandbox is not None and work_sandbox not in GROK_WORK_SANDBOX_VALUES:
        raise ConfigError(
            "invalid_grok_config",
            f"grok.workSandbox must be null or one of: {', '.join(GROK_WORK_SANDBOX_VALUES)}.",
        )
    require_bool(
        grok.get("disableWebSearch", True),
        path="grok.disableWebSearch",
        error="invalid_grok_config",
    )
    require_bool(
        grok.get("noSubagents", False), path="grok.noSubagents", error="invalid_grok_config"
    )
    _validate_engine_models(grok.get("models"), engine="grok", error="invalid_grok_config")


def _validate_devin_section(devin: JsonValue) -> None:
    if not isinstance(devin, dict):
        raise ConfigError("invalid_devin_config", "devin config must be an object.")
    require_non_empty_str(devin.get("binary"), path="devin.binary", error="invalid_devin_config")
    optional_str(devin.get("defaultModel"), path="devin.defaultModel", error="invalid_devin_config")
    if devin.get("defaultReasoningEffort") is not None:
        raise ConfigError(
            "invalid_devin_config",
            "devin.defaultReasoningEffort is not supported; set it to null.",
        )
    _validate_engine_models(devin.get("models"), engine="devin", error="invalid_devin_config")


def _validate_opencode_section(opencode: JsonValue) -> None:
    if not isinstance(opencode, dict):
        raise ConfigError("invalid_opencode_config", "opencode config must be an object.")
    require_non_empty_str(
        opencode.get("binary"), path="opencode.binary", error="invalid_opencode_config"
    )
    optional_str(
        opencode.get("defaultModel"), path="opencode.defaultModel", error="invalid_opencode_config"
    )
    _reject_opencode_flag_like_value(
        opencode.get("defaultModel"),
        path="opencode.defaultModel",
        error="invalid_opencode_config",
    )
    _validate_provider_default_reasoning_effort(
        opencode.get("defaultReasoningEffort"),
        path="opencode.defaultReasoningEffort",
        error="invalid_opencode_config",
    )
    _reject_opencode_flag_like_value(
        opencode.get("defaultReasoningEffort"),
        path="opencode.defaultReasoningEffort",
        error="invalid_opencode_config",
    )
    optional_str(
        opencode.get("defaultAgent"), path="opencode.defaultAgent", error="invalid_opencode_config"
    )
    _reject_opencode_flag_like_value(
        opencode.get("defaultAgent"),
        path="opencode.defaultAgent",
        error="invalid_opencode_config",
    )
    _validate_opencode_models(opencode.get("models"))


def _validate_pi_family_section(section: JsonValue, *, engine: str) -> None:
    error = f"invalid_{engine}_config"
    if not isinstance(section, dict):
        raise ConfigError(error, f"{engine} config must be an object.")
    require_non_empty_str(section.get("binary"), path=f"{engine}.binary", error=error)
    optional_str(section.get("defaultModel"), path=f"{engine}.defaultModel", error=error)
    _reject_pi_family_flag_like_value(
        section.get("defaultModel"), path=f"{engine}.defaultModel", error=error
    )
    default_effort = section.get("defaultReasoningEffort")
    if default_effort is not None:
        try:
            reasoning.resolve_pi_native_effort(default_effort, engine=engine)
        except reasoning.ReasoningCapabilityError as exc:
            raise ConfigError(
                error,
                f"{engine}.defaultReasoningEffort: {exc.message}",
            ) from exc
    _validate_pi_family_models(section.get("models"), engine=engine)


def _reject_opencode_flag_like_value(
    value: JsonValue,
    *,
    path: str,
    error: str,
) -> None:
    """Reject empty or leading-dash strings that would inject argv flags."""
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or value.startswith("-"):
        raise ConfigError(
            error,
            f"{path} must be a non-empty string that does not start with '-'.",
        )


def _reject_pi_family_flag_like_value(value: JsonValue, *, path: str, error: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or value.startswith("-"):
        raise ConfigError(
            error,
            f"{path} must be a non-empty string that does not start with '-'.",
        )
    if ":" in value:
        raise ConfigError(
            error,
            f"{path} must not contain ':'; use the thinking field to set Pi reasoning effort.",
        )


def _validate_provider_default_reasoning_effort(
    value: JsonValue,
    *,
    path: str,
    error: str,
) -> None:
    if value is None:
        return
    if not reasoning.is_valid_effort_string(value):
        raise ConfigError(
            error,
            f"{path} must be a non-empty string that does not start with '-' and has no whitespace.",
        )


def _validate_engine_models(models: JsonValue, *, engine: str, error: str) -> None:
    path = f"{engine}.models"
    if models is None:
        return
    if not isinstance(models, dict):
        raise ConfigError(error, f"{path} must be an object.")
    for alias, model_id in models.items():
        _validate_engine_model_alias(alias, engine=engine, error=error)
        if not isinstance(model_id, str) or not model_id.strip():
            raise ConfigError(error, f"{engine} model aliases and ids must be non-empty strings.")


def _validate_engine_model_alias(alias: object, *, engine: str, error: str) -> None:
    path = f"{engine}.models"
    if not isinstance(alias, str) or not alias.strip():
        raise ConfigError(error, f"{engine} model aliases must be non-empty strings.")
    if alias in VALID_MODES:
        raise ConfigError(
            error,
            f"{path} alias {alias!r} collides with a launch mode name; rename the alias.",
        )
    if alias == engine:
        raise ConfigError(
            error,
            f"{path} alias {alias!r} collides with its own engine name "
            "(shadowing the engine's summary entry); rename the alias.",
        )
    if alias.startswith("-"):
        raise ConfigError(
            error,
            f"{path} alias {alias!r} must not start with '-'.",
        )


def _validate_opencode_models(models: JsonValue) -> None:
    engine = "opencode"
    error = "invalid_opencode_config"
    path = f"{engine}.models"
    if models is None:
        return
    if not isinstance(models, dict):
        raise ConfigError(error, f"{path} must be an object.")
    for alias, mapping in models.items():
        _validate_engine_model_alias(alias, engine=engine, error=error)
        if isinstance(mapping, str):
            _reject_opencode_flag_like_value(
                mapping,
                path=f"{path}.{alias}",
                error=error,
            )
            continue
        if not isinstance(mapping, dict):
            raise ConfigError(
                error,
                f"{path}.{alias} must be a non-empty string or an object with model and variant.",
            )
        unknown = sorted(set(mapping) - {"model", "variant"})
        if unknown:
            raise ConfigError(
                error,
                f"{path}.{alias} has unknown keys: {', '.join(unknown)}.",
            )
        model = mapping.get("model")
        variant = mapping.get("variant")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(error, f"{path}.{alias}.model must be a non-empty string.")
        _reject_opencode_flag_like_value(model, path=f"{path}.{alias}.model", error=error)
        if not isinstance(variant, str) or not variant.strip():
            raise ConfigError(error, f"{path}.{alias}.variant must be a non-empty string.")
        _reject_opencode_flag_like_value(variant, path=f"{path}.{alias}.variant", error=error)
        try:
            reasoning.normalize_effort(variant)
        except reasoning.ReasoningCapabilityError as exc:
            raise ConfigError(error, f"{path}.{alias}.variant: {exc.message}") from exc


def _validate_pi_family_models(models: JsonValue, *, engine: str) -> None:
    error = f"invalid_{engine}_config"
    path = f"{engine}.models"
    if models is None:
        return
    if not isinstance(models, dict):
        raise ConfigError(error, f"{path} must be an object.")
    for alias, mapping in models.items():
        _validate_engine_model_alias(alias, engine=engine, error=error)
        if isinstance(mapping, str):
            _reject_pi_family_flag_like_value(mapping, path=f"{path}.{alias}", error=error)
            continue
        if not isinstance(mapping, dict):
            raise ConfigError(
                error,
                f"{path}.{alias} must be a non-empty string or an object with model and thinking.",
            )
        unknown = sorted(set(mapping) - {"model", "thinking"})
        if unknown:
            raise ConfigError(
                error,
                f"{path}.{alias} has unknown keys: {', '.join(unknown)}.",
            )
        model = mapping.get("model")
        thinking = mapping.get("thinking")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(error, f"{path}.{alias}.model must be a non-empty string.")
        if not isinstance(thinking, str) or not thinking.strip():
            raise ConfigError(error, f"{path}.{alias}.thinking must be a non-empty string.")
        _reject_pi_family_flag_like_value(model, path=f"{path}.{alias}.model", error=error)
        _reject_pi_family_flag_like_value(
            thinking,
            path=f"{path}.{alias}.thinking",
            error=error,
        )
        if thinking not in reasoning.PI_THINKING_LEVELS:
            raise ConfigError(
                error,
                f"{path}.{alias}.thinking must be one of: {', '.join(reasoning.PI_THINKING_LEVELS)}.",
            )


def _validate_reasoning_effort_value(value: JsonValue, *, path: str) -> str:
    if not reasoning.is_valid_effort_string(value):
        raise ConfigError(
            "invalid_reasoning_config",
            f"{path} must be a non-empty string that does not start with '-' and has no whitespace.",
        )
    return value


def _validate_cursor_reasoning_models(value: JsonValue) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ConfigError(
            "invalid_cursor_config",
            "cursor.reasoningEffortModels must be an object.",
        )
    for effort, model in value.items():
        if not reasoning.is_valid_effort_string(effort):
            raise ConfigError(
                "invalid_cursor_config",
                "cursor.reasoningEffortModels effort keys must be non-empty strings that do not "
                "start with '-' and have no whitespace.",
            )
        if not isinstance(model, str) or not model.strip() or model.startswith("-"):
            raise ConfigError(
                "invalid_cursor_config",
                "cursor.reasoningEffortModels values must be non-empty strings that do not "
                "start with '-'.",
            )


def _validate_reasoning_section(section: JsonValue) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        raise ConfigError("invalid_reasoning_config", "reasoning config must be an object.")
    capabilities = section.get("capabilities")
    if capabilities is None:
        return
    if not isinstance(capabilities, dict):
        raise ConfigError(
            "invalid_reasoning_config",
            "reasoning.capabilities must be an object.",
        )
    for harness, models in capabilities.items():
        # Exact per-model declarations are consulted by these launch paths.
        # Cursor reasoning lives in cursor.reasoningEffortModels and Claude
        # exposes a harness-wide native enum instead.
        if harness not in ("codex", "droid", "grok"):
            raise ConfigError(
                "invalid_reasoning_config",
                f"reasoning.capabilities.{harness} is not supported; capability "
                "declarations apply to codex, droid, and grok only (cursor uses "
                "cursor.reasoningEffortModels; claude uses native --effort labels).",
            )
        if not isinstance(models, dict):
            raise ConfigError(
                "invalid_reasoning_config",
                f"reasoning.capabilities.{harness} must be an object.",
            )
        for model, declaration in models.items():
            if not isinstance(model, str) or not model:
                raise ConfigError(
                    "invalid_reasoning_config",
                    f"reasoning.capabilities.{harness} model names must be non-empty strings.",
                )
            if not isinstance(declaration, dict):
                raise ConfigError(
                    "invalid_reasoning_config",
                    f"reasoning.capabilities.{harness}.{model} must be an object.",
                )
            supported = declaration.get("supported")
            if not isinstance(supported, list) or not supported:
                raise ConfigError(
                    "invalid_reasoning_config",
                    f"reasoning.capabilities.{harness}.{model}.supported must be a non-empty array.",
                )
            supported_values = [
                _validate_reasoning_effort_value(
                    item,
                    path=f"reasoning.capabilities.{harness}.{model}.supported[]",
                )
                for item in supported
            ]
            default = declaration.get("default")
            if default is not None:
                default_value = _validate_reasoning_effort_value(
                    default,
                    path=f"reasoning.capabilities.{harness}.{model}.default",
                )
                if default_value not in supported_values:
                    raise ConfigError(
                        "invalid_reasoning_config",
                        f"reasoning.capabilities.{harness}.{model}.default must be in supported.",
                    )


class InvalidIsolationError(Exception):
    """Raised when an isolation value is invalid; caller translates to DelegateError or ConfigError."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _normalize_resolved_isolation_boundary(
    value: str,
    *,
    engine: str,
    mode: str,
) -> str:
    if mode == "safe" and engine in SAFE_ISOLATION_REQUIRED_ENGINES and value == ISOLATION_NONE:
        return ISOLATION_AUTO
    return value


def resolve_isolation(
    cli_value: str | None = None,
    input_json_value: str | None = None,
    loaded_config: JsonObject | None = None,
    engine: str = "",
    mode: str = "",
) -> str:
    if cli_value is not None:
        if cli_value not in VALID_ISOLATION_VALUES:
            raise InvalidIsolationError(
                f"--isolation must be one of: {', '.join(VALID_ISOLATION_VALUES)}."
            )
        return _normalize_resolved_isolation_boundary(
            cli_value,
            engine=engine,
            mode=mode,
        )
    if input_json_value is not None:
        if input_json_value not in VALID_ISOLATION_VALUES:
            raise InvalidIsolationError(
                f"isolation must be one of: {', '.join(VALID_ISOLATION_VALUES)}."
            )
        return _normalize_resolved_isolation_boundary(
            input_json_value,
            engine=engine,
            mode=mode,
        )
    if isinstance(loaded_config, dict):
        isolation_cfg = loaded_config.get("isolation")
        if "isolation" in loaded_config and not isinstance(isolation_cfg, dict):
            raise InvalidIsolationError("config isolation must be an object when present.")
        if isinstance(isolation_cfg, dict):
            if mode in isolation_cfg and isolation_cfg[mode] is None:
                raise InvalidIsolationError(
                    f"config isolation.{mode} must not be null; "
                    f"use one of: {', '.join(VALID_ISOLATION_VALUES)}."
                )
            value = isolation_cfg.get(mode)
            if value is not None:
                if not isinstance(value, str) or value not in VALID_ISOLATION_VALUES:
                    raise InvalidIsolationError(
                        f"config isolation.{mode} must be one of: {', '.join(VALID_ISOLATION_VALUES)}."
                    )
                return _normalize_resolved_isolation_boundary(
                    value,
                    engine=engine,
                    mode=mode,
                )
    # Embedded defaults
    if mode == "work":
        return ISOLATION_NONE
    return ISOLATION_AUTO


class ConfigError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def require_non_empty_str(value: JsonValue, *, path: str, error: str) -> None:
    """Require ``value`` to be a string whose stripped form is non-empty."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(error, f"{path} must be a non-empty string.")


def optional_str(value: JsonValue, *, path: str, error: str) -> None:
    """Allow ``value`` to be a string or null; reject any other type."""
    if value is not None and not isinstance(value, str):
        raise ConfigError(error, f"{path} must be a string or null.")


def require_bool(value: JsonValue, *, path: str, error: str) -> None:
    """Require ``value`` to be a boolean."""
    if not isinstance(value, bool):
        raise ConfigError(error, f"{path} must be a boolean.")


def deep_merge(base: JsonObject, override: JsonObject) -> JsonObject:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _replace_profile_definitions(merged: JsonObject, override: JsonObject) -> None:
    override_profiles = override.get("profiles")
    if not isinstance(override_profiles, dict):
        return
    override_definitions = override_profiles.get("definitions")
    if not isinstance(override_definitions, dict):
        return
    profiles = merged.get("profiles")
    if not isinstance(profiles, dict):
        return
    definitions = profiles.get("definitions")
    if not isinstance(definitions, dict):
        return
    for name, entry in override_definitions.items():
        definitions[name] = copy.deepcopy(entry)


def merge_config_layer(base: JsonObject, override: JsonObject) -> JsonObject:
    """Merge one config file or CLI override layer onto ``base``.

    Profile definitions are replaced atomically per profile name so a higher layer
    cannot inherit stale ``env`` keys from lower layers.
    """
    merged = deep_merge(base, override)
    _replace_profile_definitions(merged, override)
    return merged


def default_config_path() -> Path:
    if DEFAULT_CONFIG_PATH is not None:
        return DEFAULT_CONFIG_PATH.expanduser()
    return Path.home() / DEFAULT_CONFIG_RELATIVE


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV, str(default_config_path()))
    return Path(raw).expanduser()


PROFILE_CONFIG_NAMES = ("work", "personal")


def profile_config_path(base_path: Path, profile: str) -> Path:
    """Path to the AI_PROFILE overlay config for ``profile`` next to ``base_path``.

    Shared by ``config_commands`` (which writes these overlays) and
    ``profile_guard`` (which checks for them), so both agree on
    ``~/.delegate/config.<profile>.json`` naming without duplicating the rule.
    """
    return base_path.with_name(f"{base_path.stem}.{profile}{base_path.suffix}")


def workspace_config_path(workspace: Path) -> Path:
    return workspace / WORKSPACE_CONFIG_RELATIVE


def read_config_file(path: Path) -> JsonObject:
    try:
        loaded: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("invalid_config_json", f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("invalid_config", "Config root must be a JSON object.")
    return loaded


def load_config(
    path: Path | None = None,
    *,
    workspace: Path | None = None,
    cli_overrides: JsonObject | None = None,
) -> tuple[JsonObject, str]:
    """Load config with precedence: cli > DELEGATE_CONFIG > global > embedded.

    When DELEGATE_CONFIG is set, the path must exist; a missing file raises ConfigError
    instead of discarding lower-precedence layers. Workspace config is never merged
    implicitly because repositories are not trusted to select executables or policy.
    """
    merged = embedded_default_config()
    primary_source = "embedded-default"

    global_path = default_config_path()
    if global_path.exists():
        merged = merge_config_layer(merged, read_config_file(global_path))
        primary_source = str(global_path)

    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        if wsl.should_reject_windows_path(explicit):
            raise ConfigError("windows_path", wsl.windows_path_message(CONFIG_ENV, explicit))
        explicit_path = Path(explicit).expanduser()
        if not explicit_path.exists():
            # Explicit path was requested; do not discard lower-precedence merges.
            raise ConfigError(
                "config_not_found",
                f"{CONFIG_ENV} points to a missing file: {explicit_path}",
            )
        merged = merge_config_layer(merged, read_config_file(explicit_path))
        primary_source = str(explicit_path)
    elif path is not None and path != global_path and path.exists():
        merged = merge_config_layer(merged, read_config_file(path))
        primary_source = str(path)

    if cli_overrides:
        merged = merge_config_layer(merged, cli_overrides)
        primary_source = "cli-overrides"

    return merged, primary_source


def validate_config(config: JsonObject) -> None:
    cursor = config.get("cursor")
    droid = config.get("droid")
    if not isinstance(cursor, dict):
        raise ConfigError("invalid_cursor_config", "cursor config must be an object.")
    if "binary" in cursor:
        raise ConfigError(
            "invalid_cursor_config",
            "cursor.binary is not supported; use cursor.argvPrefix as an array of strings.",
        )
    prefix = cursor.get("argvPrefix")
    if (
        not isinstance(prefix, list)
        or not prefix
        or not all(isinstance(x, str) and x for x in prefix)
    ):
        raise ConfigError(
            "invalid_cursor_config", "cursor.argvPrefix must be a non-empty array of strings."
        )
    require_non_empty_str(
        cursor.get("defaultModel"), path="cursor.defaultModel", error="invalid_cursor_config"
    )
    _validate_provider_default_reasoning_effort(
        cursor.get("defaultReasoningEffort"),
        path="cursor.defaultReasoningEffort",
        error="invalid_cursor_config",
    )
    _validate_cursor_reasoning_models(cursor.get("reasoningEffortModels"))
    _validate_engine_models(cursor.get("models"), engine="cursor", error="invalid_cursor_config")
    if not isinstance(droid, dict):
        raise ConfigError("invalid_droid_config", "droid config must be an object.")
    require_non_empty_str(droid.get("binary"), path="droid.binary", error="invalid_droid_config")
    optional_str(droid.get("defaultModel"), path="droid.defaultModel", error="invalid_droid_config")
    _validate_provider_default_reasoning_effort(
        droid.get("defaultReasoningEffort"),
        path="droid.defaultReasoningEffort",
        error="invalid_droid_config",
    )
    _validate_engine_models(droid.get("models"), engine="droid", error="invalid_droid_config")
    tracking = config.get("tracking")
    if tracking is not None:
        if not isinstance(tracking, dict):
            raise ConfigError("invalid_tracking_config", "tracking config must be an object.")
        completion = tracking.get("completionReport")
        if completion is not None:
            if not isinstance(completion, dict):
                raise ConfigError(
                    "invalid_tracking_config", "tracking.completionReport must be an object."
                )
            default_mode = completion.get("defaultMode")
            if default_mode is not None and default_mode not in COMPLETION_REPORT_MODES:
                raise ConfigError(
                    "invalid_tracking_config",
                    "tracking.completionReport.defaultMode must be markdown or none.",
                )
        retention = tracking.get("retention")
        if retention is not None:
            if not isinstance(retention, dict):
                raise ConfigError(
                    "invalid_tracking_config", "tracking.retention must be an object."
                )
            enabled = retention.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                raise ConfigError(
                    "invalid_tracking_config", "tracking.retention.enabled must be a boolean."
                )
            raw_log_days = retention.get("rawLogDays")
            if raw_log_days is not None:
                _validate_required_non_negative_int(
                    raw_log_days,
                    path="tracking.retention.rawLogDays",
                    error="invalid_tracking_config",
                )
    _validate_policy_section(config.get("policy"))
    _validate_profiles_section(config.get("profiles"))
    _validate_codex_section(config.get("codex"))
    _validate_codex_profile_references(config)
    _validate_kimi_section(config.get("kimi"))
    _validate_claude_section(config.get("claude"))
    _validate_grok_section(config.get("grok"))
    _validate_devin_section(config.get("devin"))
    _validate_opencode_section(config.get("opencode"))
    _validate_pi_family_section(config.get("pi"), engine="pi")
    _validate_pi_family_section(config.get("omp"), engine="omp")
    _validate_reasoning_section(config.get("reasoning"))
    _validate_isolation_section(config.get("isolation"))
    _validate_worktrees_section(config.get("worktrees"))
    _validate_progress_section(config.get("progress"))
    _validate_workflows_section(config.get("workflows"))


def completion_report_default_mode(config: JsonObject) -> str:
    tracking = config.get("tracking")
    if not isinstance(tracking, dict):
        return COMPLETION_REPORT_MODE_MARKDOWN
    completion = tracking.get("completionReport")
    if not isinstance(completion, dict):
        return COMPLETION_REPORT_MODE_MARKDOWN
    mode = completion.get("defaultMode", COMPLETION_REPORT_MODE_MARKDOWN)
    return mode if mode in COMPLETION_REPORT_MODES else COMPLETION_REPORT_MODE_MARKDOWN


def harness_binary(config: JsonObject, engine: str) -> str:
    embedded = _EMBEDDED_DEFAULT_CONFIG.get(engine)
    # 'codex' is an arbitrary safe fallback that only fires when an engine has no
    # embedded section (an invariant violation), since codex is the baseline harness.
    default_binary = "codex"
    if isinstance(embedded, dict) and isinstance(embedded.get("binary"), str):
        default_binary = embedded["binary"]
    section = config.get(engine)
    if isinstance(section, dict) and isinstance(section.get("binary"), str):
        return section["binary"]
    return default_binary
