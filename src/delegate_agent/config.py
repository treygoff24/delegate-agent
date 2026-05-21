from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".delegate" / "config.json"
WORKSPACE_CONFIG_RELATIVE = Path(".delegate") / "config.json"
CONFIG_ENV = "DELEGATE_CONFIG"

DEFAULT_CONFIG: dict[str, Any] = {
    "tracking": {
        "completionReport": {
            "defaultMode": "markdown",
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
}


class ConfigError(Exception):
    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
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


def read_config_file(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError("invalid_config_json", f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("invalid_config", "Config root must be a JSON object.")
    return loaded


def load_config(
    path: Path | None = None,
    *,
    workspace: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
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


def validate_config(config: dict[str, Any]) -> None:
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
            if default_mode is not None and default_mode not in ("markdown", "none"):
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


def completion_report_default_mode(config: dict[str, Any]) -> str:
    tracking = config.get("tracking")
    if not isinstance(tracking, dict):
        return "markdown"
    completion = tracking.get("completionReport")
    if not isinstance(completion, dict):
        return "markdown"
    mode = completion.get("defaultMode", "markdown")
    return mode if mode in ("markdown", "none") else "markdown"
