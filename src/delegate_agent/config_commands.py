from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent import rendering as delegate_rendering
from delegate_agent import wsl
from delegate_agent.errors import EXIT_OK, DelegateError
from delegate_agent.json_types import JsonObject
from delegate_agent.private_io import ensure_private_file


@dataclass(frozen=True)
class ConfigCommand:
    action: str
    force: bool = False
    json_mode: bool = False


PROFILE_CONFIG_NAMES = delegate_config.PROFILE_CONFIG_NAMES


def _target_path() -> Path:
    raw = delegate_config.config_path()
    if wsl.should_reject_windows_path(str(raw)):
        raise DelegateError("windows_path", wsl.windows_path_message("DELEGATE_CONFIG", str(raw)))
    return raw


def _profile_overlay(config: JsonObject, profile: str) -> JsonObject | None:
    profiles = config.get("profiles")
    if not isinstance(profiles, dict):
        return None
    definitions = profiles.get("definitions")
    if not isinstance(definitions, dict):
        return None
    definition = definitions.get(profile)
    if not isinstance(definition, dict):
        return None
    detect_from = profiles.get("detectFrom", [])
    overlay_profiles: JsonObject = {
        "default": profile,
        "definitions": {profile: definition},
    }
    if isinstance(detect_from, list):
        overlay_profiles["detectFrom"] = [
            item for item in detect_from if isinstance(item, str) and item.strip()
        ]
    return {"profiles": overlay_profiles}


def _write_missing_profile_configs(base_path: Path, config: JsonObject) -> JsonObject:
    created: list[str] = []
    existing: list[str] = []
    skipped: list[str] = []
    for profile in PROFILE_CONFIG_NAMES:
        path = delegate_config.profile_config_path(base_path, profile)
        if path.exists():
            existing.append(str(path))
            continue
        overlay = _profile_overlay(config, profile)
        if overlay is None:
            skipped.append(profile)
            continue
        path.write_text(json.dumps(overlay, indent=2) + "\n", encoding="utf-8")
        ensure_private_file(path)
        created.append(str(path))
    return {
        "created": created,
        "existing": existing,
        "skipped": skipped,
    }


def _read_config(path: Path) -> JsonObject:
    try:
        return delegate_config.read_config_file(path)
    except delegate_config.ConfigError as exc:
        raise DelegateError(exc.error, exc.message) from exc


def _validated_effective_config(config: JsonObject) -> JsonObject:
    merged = delegate_config.merge_config_layer(delegate_config.embedded_default_config(), config)
    try:
        delegate_config.validate_config(merged)
    except delegate_config.ConfigError as exc:
        raise DelegateError(exc.error, exc.message) from exc
    return merged


def emit(command: ConfigCommand, stdout: TextIO) -> int:
    if command.action not in {"init", "sync-profiles"}:
        raise DelegateError("invalid_config_command", f"Unknown config action: {command.action}")
    path = _target_path()
    if command.action == "sync-profiles":
        if not path.exists():
            raise DelegateError(
                "config_not_found",
                f"Config does not exist at {path}; run delegate config init first.",
            )
        payload = _validated_effective_config(_read_config(path))
        profile_configs = _write_missing_profile_configs(path, payload)
        result: JsonObject = {
            "ok": True,
            "path": str(path),
            "action": "sync-profiles",
            "profileConfigs": profile_configs,
        }
        if command.json_mode:
            delegate_rendering.print_json(result, stdout)
        else:
            for created in profile_configs["created"]:
                print(f"wrote profile config: {created}", file=stdout)
            if not profile_configs["created"]:
                print("profile configs already present", file=stdout)
        return EXIT_OK
    if path.exists() and not command.force:
        raise DelegateError(
            "config_exists",
            f"Config already exists at {path}; pass --force to overwrite it.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = delegate_config.example_config()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    ensure_private_file(path)
    profile_configs = _write_missing_profile_configs(path, payload)
    result: JsonObject = {
        "ok": True,
        "path": str(path),
        "action": "init",
        "force": command.force,
        "profileConfigs": profile_configs,
        "nextAction": "Run delegate setup for automatic harness discovery.",
    }
    if command.json_mode:
        delegate_rendering.print_json(result, stdout)
    else:
        print(f"wrote config: {path}", file=stdout)
        for created in profile_configs["created"]:
            print(f"wrote profile config: {created}", file=stdout)
        print("next: run delegate setup for automatic harness discovery", file=stdout)
    return EXIT_OK
