from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import codex_auth, rendering
from delegate_agent import config as delegate_config
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class CodexAuthCommand:
    action: str
    json_mode: bool = False
    profile: str | None = None
    fallback: str | None = None


def _ensure_codex_object(raw: JsonObject) -> JsonObject:
    codex = raw.get("codex")
    if codex is None:
        raw["codex"] = {}
        return raw["codex"]
    if not isinstance(codex, dict):
        raise DelegateError("invalid_config", "Existing config codex section must be an object.")
    return codex


def _merge_missing_auth_profiles(
    codex: JsonObject,
    *,
    required_names: set[str],
    source_profiles: dict[str, JsonObject],
) -> None:
    existing = codex.get("authProfiles")
    if not isinstance(existing, dict):
        existing = {}
        codex["authProfiles"] = existing
    for name in required_names:
        if name in existing or name not in source_profiles:
            continue
        existing[name] = dict(source_profiles[name])


def _validate_auth_profiles_resolvable(codex: JsonObject, profile_names: set[str]) -> None:
    for name in profile_names:
        codex_auth.resolve_profile_codex_home(codex, name)


def _prepare_auth_write_payload(
    *,
    auth_profile: str | None,
    fallback_auth_profile: str | None,
    effective_profiles: dict[str, JsonObject] | None = None,
    bootstrap_profiles: dict[str, JsonObject] | None = None,
) -> tuple[Path, JsonObject]:
    target = codex_auth.codex_auth_write_target()
    raw = codex_auth.read_raw_config_object(target)
    codex = _ensure_codex_object(raw)
    if auth_profile is None:
        codex.pop("authProfile", None)
    else:
        codex["authProfile"] = auth_profile
    if fallback_auth_profile is None:
        codex.pop("fallbackAuthProfile", None)
    else:
        codex["fallbackAuthProfile"] = fallback_auth_profile

    required_names: set[str] = set()
    if auth_profile:
        required_names.add(auth_profile)
    if fallback_auth_profile:
        required_names.add(fallback_auth_profile)

    existing_profiles = codex.get("authProfiles")
    if bootstrap_profiles and (not isinstance(existing_profiles, dict) or not existing_profiles):
        codex["authProfiles"] = bootstrap_profiles
    elif effective_profiles and required_names:
        _merge_missing_auth_profiles(
            codex,
            required_names=required_names,
            source_profiles=effective_profiles,
        )

    _validate_auth_profiles_resolvable(codex, required_names)
    return target, raw


def _write_auth_fields(
    *,
    auth_profile: str | None,
    fallback_auth_profile: str | None,
    effective_profiles: dict[str, JsonObject] | None = None,
    bootstrap_profiles: dict[str, JsonObject] | None = None,
) -> Path:
    target, raw = _prepare_auth_write_payload(
        auth_profile=auth_profile,
        fallback_auth_profile=fallback_auth_profile,
        effective_profiles=effective_profiles,
        bootstrap_profiles=bootstrap_profiles,
    )
    codex_auth.write_raw_config_object(target, raw)
    return target


def _show_payload_from_effective(config: JsonObject, *, config_source: str) -> JsonObject:
    payload = codex_auth.show_payload(config, config_source=config_source)
    payload["config"] = str(codex_auth.codex_auth_write_target())
    return payload


def _render_show(payload: JsonObject, stdout: TextIO) -> None:
    print(f"codex auth profile: {payload.get('authProfile') or '(unset)'}", file=stdout)
    print(
        f"fallback auth profile: {payload.get('fallbackAuthProfile') or '(unset)'}",
        file=stdout,
    )
    codex_home = payload.get("codexHome")
    print(f"codex home: {codex_home or '(unset)'}", file=stdout)
    print(f"config: {payload.get('config')}", file=stdout)


def emit_show(*, config: JsonObject, config_source: str, json_mode: bool, stdout: TextIO) -> int:
    payload = _show_payload_from_effective(config, config_source=config_source)
    if json_mode:
        rendering.print_json(payload, stdout)
    else:
        _render_show(payload, stdout)
    return 0


def _validate_use_fallback(
    profile: str, fallback: str | None, profiles: dict[str, JsonObject]
) -> None:
    if fallback is None:
        return
    if not isinstance(fallback, str) or not fallback.strip():
        raise DelegateError(
            "invalid_fallback_auth_profile",
            "codex-auth use --fallback requires a non-empty profile name.",
        )
    fallback_name = fallback.strip()
    if fallback_name not in profiles:
        raise DelegateError(
            "unknown_fallback_auth_profile",
            f"Unknown Codex fallback auth profile: {fallback_name}.",
        )
    if fallback_name == profile:
        raise DelegateError(
            "invalid_fallback_auth_profile",
            "codex-auth use --fallback must differ from the active profile.",
        )


def emit_use(
    command: CodexAuthCommand,
    *,
    config: JsonObject,
    config_source: str,
    json_mode: bool,
    stdout: TextIO,
) -> int:
    profile = command.profile
    if profile is None:
        raise DelegateError("missing_auth_profile", "codex-auth use requires a profile name.")
    profiles = codex_auth.effective_auth_profiles_for_use(config)
    if profile not in profiles:
        raise DelegateError(
            "unknown_auth_profile",
            f"Unknown Codex auth profile: {profile}.",
        )
    _validate_use_fallback(profile, command.fallback, profiles)
    bootstrap = None
    codex = codex_auth.codex_section(config) or {}
    if not codex_auth.auth_profiles_map(codex):
        bootstrap = profiles
    target = _write_auth_fields(
        auth_profile=profile,
        fallback_auth_profile=command.fallback,
        effective_profiles=profiles,
        bootstrap_profiles=bootstrap,
    )
    raw_after = codex_auth.read_raw_config_object(target)
    codex_after = raw_after.get("codex")
    if not isinstance(codex_after, dict):
        codex_after = {}
    payload: JsonObject = {
        "ok": True,
        "configSource": str(target),
        "authProfile": profile,
        "fallbackAuthProfile": command.fallback,
        "codexHome": codex_auth.resolve_profile_codex_home(codex_after, profile),
        "config": str(target),
    }
    if json_mode:
        rendering.print_json(payload, stdout)
    else:
        _render_show(payload, stdout)
    return 0


def emit_swap(*, config: JsonObject, json_mode: bool, stdout: TextIO) -> int:
    codex = codex_auth.codex_section(config) or {}
    active = codex.get("authProfile")
    fallback = codex.get("fallbackAuthProfile")
    if not isinstance(active, str) or not active.strip():
        raise DelegateError(
            "missing_auth_profile",
            "codex-auth swap requires both authProfile and fallbackAuthProfile to be set.",
        )
    if not isinstance(fallback, str) or not fallback.strip():
        raise DelegateError(
            "missing_fallback_auth_profile",
            "codex-auth swap requires both authProfile and fallbackAuthProfile to be set.",
        )
    target = _write_auth_fields(auth_profile=fallback.strip(), fallback_auth_profile=active.strip())
    payload = codex_auth.show_payload(config, config_source=str(target))
    payload["authProfile"] = fallback.strip()
    payload["fallbackAuthProfile"] = active.strip()
    payload["config"] = str(target)
    try:
        payload["codexHome"] = codex_auth.resolve_profile_codex_home(
            codex_auth.read_raw_config_object(target).get("codex", codex),
            fallback.strip(),
        )
    except (DelegateError, delegate_config.ConfigError):
        payload["codexHome"] = None
    if json_mode:
        rendering.print_json(payload, stdout)
    else:
        _render_show(payload, stdout)
    return 0


def emit_clear(*, json_mode: bool, stdout: TextIO) -> int:
    target = _write_auth_fields(auth_profile=None, fallback_auth_profile=None)
    payload: JsonObject = {
        "ok": True,
        "configSource": str(target),
        "authProfile": None,
        "fallbackAuthProfile": None,
        "codexHome": None,
        "config": str(target),
    }
    if json_mode:
        rendering.print_json(payload, stdout)
    else:
        _render_show(payload, stdout)
    return 0


def emit(
    command: CodexAuthCommand,
    *,
    config: JsonObject,
    config_source: str,
    stdout: TextIO,
) -> int:
    if command.action == "show":
        return emit_show(
            config=config,
            config_source=config_source,
            json_mode=command.json_mode,
            stdout=stdout,
        )
    if command.action == "use":
        return emit_use(
            command,
            config=config,
            config_source=config_source,
            json_mode=command.json_mode,
            stdout=stdout,
        )
    if command.action == "swap":
        return emit_swap(config=config, json_mode=command.json_mode, stdout=stdout)
    if command.action == "clear":
        return emit_clear(json_mode=command.json_mode, stdout=stdout)
    raise DelegateError("unknown_codex_auth_action", f"Unknown codex-auth action: {command.action}")
