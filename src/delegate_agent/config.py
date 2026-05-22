from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from delegate_agent.json_types import JsonObject, JsonValue

DEFAULT_CONFIG_PATH = Path.home() / ".delegate" / "config.json"
WORKSPACE_CONFIG_RELATIVE = Path(".delegate") / "config.json"
CONFIG_ENV = "DELEGATE_CONFIG"
COMPLETION_REPORT_MODE_MARKDOWN = "markdown"
COMPLETION_REPORT_MODE_NONE = "none"
COMPLETION_REPORT_MODES = (
    COMPLETION_REPORT_MODE_MARKDOWN,
    COMPLETION_REPORT_MODE_NONE,
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
CODEX_WORK_SANDBOX_VALUES = ("read-only", "workspace-write", "danger-full-access")

DEFAULT_MODE_POLICY: JsonObject = {
    "networkAccess": False,
    "webSearch": False,
    "bypassApprovalsAndSandbox": False,
    "bypassHookTrust": False,
}

DEFAULT_CONFIG: JsonObject = {
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
    "policy": {
        "profile": "safe",
        "work": {
            "networkAccess": True,
        },
    },
    "codex": {
        "binary": "codex",
        "defaultModel": None,
        "profile": None,
        "workSandbox": "workspace-write",
        "ephemeral": True,
        "ignoreUserConfig": False,
    },
}


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


def _validate_policy_mode_policy(mode_policy: JsonObject, path: str) -> None:
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
            _validate_policy_mode_policy(mode_policy, f"{path}.{mode}")
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
                    )


def _validate_codex_section(codex: JsonValue) -> None:
    if not isinstance(codex, dict):
        raise ConfigError("invalid_codex_config", "codex config must be an object.")
    if not isinstance(codex.get("binary"), str) or not codex["binary"].strip():
        raise ConfigError("invalid_codex_config", "codex.binary must be a non-empty string.")
    default_model = codex.get("defaultModel")
    if default_model is not None and not isinstance(default_model, str):
        raise ConfigError(
            "invalid_codex_config",
            "codex.defaultModel must be a string or null.",
        )
    profile = codex.get("profile")
    if profile is not None and not isinstance(profile, str):
        raise ConfigError("invalid_codex_config", "codex.profile must be a string or null.")
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
    ephemeral = codex.get("ephemeral", True)
    if not isinstance(ephemeral, bool):
        raise ConfigError("invalid_codex_config", "codex.ephemeral must be a boolean.")
    ignore_user = codex.get("ignoreUserConfig", False)
    if not isinstance(ignore_user, bool):
        raise ConfigError(
            "invalid_codex_config",
            "codex.ignoreUserConfig must be a boolean.",
        )


class ConfigError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def deep_merge(base: JsonObject, override: JsonObject) -> JsonObject:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, str(DEFAULT_CONFIG_PATH))).expanduser()


def workspace_config_path(workspace: Path) -> Path:
    return workspace / WORKSPACE_CONFIG_RELATIVE


def read_config_file(path: Path) -> JsonObject:
    try:
        loaded: JsonValue = json.loads(path.read_text())
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
    """Load config with precedence: cli > DELEGATE_CONFIG > workspace > global > embedded.

    When DELEGATE_CONFIG is set, the path must exist; a missing file raises ConfigError
    instead of discarding lower-precedence layers.
    """
    merged = copy.deepcopy(DEFAULT_CONFIG)
    primary_source = "embedded-default"

    global_path = DEFAULT_CONFIG_PATH.expanduser()
    if global_path.exists():
        merged = deep_merge(merged, read_config_file(global_path))
        primary_source = str(global_path)

    if workspace is not None:
        local_path = workspace_config_path(workspace)
        if local_path.exists():
            merged = deep_merge(merged, read_config_file(local_path))
            primary_source = str(local_path)

    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if not explicit_path.exists():
            # Explicit path was requested; do not discard lower-precedence merges.
            raise ConfigError(
                "config_not_found",
                f"{CONFIG_ENV} points to a missing file: {explicit_path}",
            )
        merged = deep_merge(merged, read_config_file(explicit_path))
        primary_source = str(explicit_path)
    elif path is not None and path != global_path and path.exists():
        merged = deep_merge(merged, read_config_file(path))
        primary_source = str(path)

    if cli_overrides:
        merged = deep_merge(merged, cli_overrides)
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
    if not isinstance(cursor.get("defaultModel"), str) or not cursor["defaultModel"].strip():
        raise ConfigError(
            "invalid_cursor_config", "cursor.defaultModel must be a non-empty string."
        )
    if not isinstance(droid, dict):
        raise ConfigError("invalid_droid_config", "droid config must be an object.")
    if not isinstance(droid.get("binary"), str) or not droid["binary"].strip():
        raise ConfigError("invalid_droid_config", "droid.binary must be a non-empty string.")
    models = droid.get("models")
    if not isinstance(models, dict) or not models:
        raise ConfigError("invalid_droid_config", "droid.models must be a non-empty object.")
    for alias, model_id in models.items():
        if not isinstance(alias, str) or not isinstance(model_id, str) or not alias or not model_id:
            raise ConfigError(
                "invalid_droid_config", "droid model aliases and ids must be non-empty strings."
            )
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
            if raw_log_days is not None and (not isinstance(raw_log_days, int) or raw_log_days < 0):
                raise ConfigError(
                    "invalid_tracking_config",
                    "tracking.retention.rawLogDays must be a non-negative integer.",
                )
    _validate_policy_section(config.get("policy"))
    _validate_codex_section(config.get("codex"))


def completion_report_default_mode(config: JsonObject) -> str:
    tracking = config.get("tracking")
    if not isinstance(tracking, dict):
        return COMPLETION_REPORT_MODE_MARKDOWN
    completion = tracking.get("completionReport")
    if not isinstance(completion, dict):
        return COMPLETION_REPORT_MODE_MARKDOWN
    mode = completion.get("defaultMode", COMPLETION_REPORT_MODE_MARKDOWN)
    return mode if mode in COMPLETION_REPORT_MODES else COMPLETION_REPORT_MODE_MARKDOWN
