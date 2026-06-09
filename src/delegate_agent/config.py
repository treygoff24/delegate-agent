from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from delegate_agent import reasoning
from delegate_agent.json_types import JsonObject, JsonValue, is_non_negative_int

DEFAULT_CONFIG_PATH = Path.home() / ".delegate" / "config.json"
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
        "defaultReasoningEffort": None,
        "reasoningEffortModels": {},
    },
    "droid": {
        "binary": "droid",
        "models": {},
        "defaultReasoningEffort": None,
    },
    "reasoning": {
        "capabilities": {},
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
        "workSandbox": "workspace-write",
        "ephemeral": True,
        "ignoreUserConfig": False,
    },
    "isolation": {
        "safe": ISOLATION_AUTO,
        "work": ISOLATION_NONE,
    },
    "worktrees": {
        "dataHome": None,
        "autoPrune": {
            "enabled": False,
            "mergedOlderThanDays": 7,
        },
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
        expanded = Path(data_home).expanduser()
        if not expanded.is_absolute():
            raise ConfigError(
                "invalid_worktrees_config",
                "worktrees.dataHome must be an absolute path or start with ~/.",
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
    _validate_provider_default_reasoning_effort(
        codex.get("defaultReasoningEffort"),
        path="codex.defaultReasoningEffort",
        error="invalid_codex_config",
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


def _validate_provider_default_reasoning_effort(
    value: JsonValue,
    *,
    path: str,
    error: str,
) -> None:
    if value is None:
        return
    if not reasoning.is_valid_effort_string(value):
        raise ConfigError(error, f"{path} must be a non-empty string without whitespace or null.")


def _validate_reasoning_effort_value(value: JsonValue, *, path: str) -> str:
    if not reasoning.is_valid_effort_string(value):
        raise ConfigError(
            "invalid_reasoning_config",
            f"{path} must be a non-empty string without whitespace.",
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
                "cursor.reasoningEffortModels effort keys must be non-empty strings without whitespace.",
            )
        if not isinstance(model, str) or not model:
            raise ConfigError(
                "invalid_cursor_config",
                "cursor.reasoningEffortModels values must be non-empty strings.",
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
        # Only codex and droid consult this table at launch. Accepting other
        # keys (e.g. cursor, or a typo) would validate cleanly and then be
        # silently ignored; cursor reasoning lives in cursor.reasoningEffortModels.
        if harness not in ("codex", "droid"):
            raise ConfigError(
                "invalid_reasoning_config",
                f"reasoning.capabilities.{harness} is not supported; capability "
                "declarations apply to codex and droid only (cursor uses "
                "cursor.reasoningEffortModels).",
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
        return cli_value
    if input_json_value is not None:
        if input_json_value not in VALID_ISOLATION_VALUES:
            raise InvalidIsolationError(
                f"isolation must be one of: {', '.join(VALID_ISOLATION_VALUES)}."
            )
        return input_json_value
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
                return value
    # Embedded defaults
    if mode == "work":
        return ISOLATION_NONE
    return ISOLATION_AUTO


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
    _validate_provider_default_reasoning_effort(
        cursor.get("defaultReasoningEffort"),
        path="cursor.defaultReasoningEffort",
        error="invalid_cursor_config",
    )
    _validate_cursor_reasoning_models(cursor.get("reasoningEffortModels"))
    if not isinstance(droid, dict):
        raise ConfigError("invalid_droid_config", "droid config must be an object.")
    if not isinstance(droid.get("binary"), str) or not droid["binary"].strip():
        raise ConfigError("invalid_droid_config", "droid.binary must be a non-empty string.")
    _validate_provider_default_reasoning_effort(
        droid.get("defaultReasoningEffort"),
        path="droid.defaultReasoningEffort",
        error="invalid_droid_config",
    )
    models = droid.get("models")
    if not isinstance(models, dict):
        raise ConfigError("invalid_droid_config", "droid.models must be an object.")
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
            if raw_log_days is not None:
                _validate_required_non_negative_int(
                    raw_log_days,
                    path="tracking.retention.rawLogDays",
                    error="invalid_tracking_config",
                )
    _validate_policy_section(config.get("policy"))
    _validate_codex_section(config.get("codex"))
    _validate_reasoning_section(config.get("reasoning"))
    _validate_isolation_section(config.get("isolation"))
    _validate_worktrees_section(config.get("worktrees"))


def completion_report_default_mode(config: JsonObject) -> str:
    tracking = config.get("tracking")
    if not isinstance(tracking, dict):
        return COMPLETION_REPORT_MODE_MARKDOWN
    completion = tracking.get("completionReport")
    if not isinstance(completion, dict):
        return COMPLETION_REPORT_MODE_MARKDOWN
    mode = completion.get("defaultMode", COMPLETION_REPORT_MODE_MARKDOWN)
    return mode if mode in COMPLETION_REPORT_MODES else COMPLETION_REPORT_MODE_MARKDOWN
