"""Non-secret Delegate profile selection and credential namespace binding."""

from __future__ import annotations

import os
from pathlib import Path

from delegate_agent import profiles
from delegate_agent.json_types import JsonObject
from delegate_agent.workflow_pinning import WorkflowPinError

SCHEMA = "delegate.workflow-profile-identity.v1"
HOME_DEFAULTS = {"CODEX_HOME": ".codex", "CLAUDE_CONFIG_DIR": ".claude"}


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise WorkflowPinError(
            "invalid_workflow_profile", "credential home bindings must be absolute paths"
        )
    try:
        return str(Path(value).resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowPinError(
            "invalid_workflow_profile", "credential home binding cannot be resolved"
        ) from exc


def capture(config: JsonObject) -> JsonObject:
    # Resolving profiles reads selectors/paths, never auth-file contents.
    ambient = os.environ.copy()
    selected = profiles.resolve_active_profile(config, ambient, expand_env=ambient)
    effective = {**ambient, **selected.env}
    home = _path(effective.get("HOME") or str(Path.home()))
    namespaces: JsonObject = {"HOME": home}
    for variable, default in HOME_DEFAULTS.items():
        namespaces[variable] = _path(effective.get(variable, str(Path(home) / default)))
    return {
        "schema": SCHEMA,
        "profile": selected.name,
        "source": selected.source,
        "detectFrom": list(profiles.profiles_section(config).get("detectFrom") or []),
        "namespaces": namespaces,
        "fallbackProfile": profiles.codex_fallback_profile(config),
        "fallbackCodexHome": _path(selected.codex_fallback_home)
        if selected.codex_fallback_home is not None
        else None,
    }


def freeze(config: JsonObject) -> JsonObject:
    """Freeze expanded home paths in a newly owned base config, then stamp it."""
    identity = capture(config)
    definitions = profiles.profile_definitions(config)
    selected = identity["profile"]
    if isinstance(selected, str):
        environment = definitions[selected].get("env")
        if isinstance(environment, dict):
            for variable, path in identity["namespaces"].items():
                if variable in environment:
                    environment[variable] = path
    fallback = identity["fallbackProfile"]
    if isinstance(fallback, str) and identity["fallbackCodexHome"] is not None:
        definitions[fallback]["env"]["CODEX_HOME"] = identity["fallbackCodexHome"]
    return identity


def validate_stamp(identity: JsonObject, config: JsonObject) -> None:
    if (
        set(identity)
        != {
            "schema",
            "profile",
            "source",
            "detectFrom",
            "namespaces",
            "fallbackProfile",
            "fallbackCodexHome",
        }
        or identity.get("schema") != SCHEMA
        or not isinstance(identity.get("namespaces"), dict)
    ):
        raise WorkflowPinError("invalid_pin", "workflow profile identity is invalid")
    namespaces = identity["namespaces"]
    if set(namespaces) != {"HOME", *HOME_DEFAULTS}:
        raise WorkflowPinError("invalid_pin", "workflow credential namespaces are incomplete")
    for value in namespaces.values():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise WorkflowPinError("invalid_pin", "workflow credential namespace is invalid")
    selected = identity.get("profile")
    if selected is not None and (
        not isinstance(selected, str) or selected not in profiles.profile_definitions(config)
    ):
        raise WorkflowPinError("invalid_pin", "workflow selected profile is not defined")
    if identity.get("source") is not None and not isinstance(identity["source"], str):
        raise WorkflowPinError("invalid_pin", "workflow selector provenance is invalid")
    if identity.get("detectFrom") != list(
        profiles.profiles_section(config).get("detectFrom") or []
    ):
        raise WorkflowPinError("invalid_pin", "workflow selector definitions differ")
    if identity.get("fallbackProfile") != profiles.codex_fallback_profile(config):
        raise WorkflowPinError("invalid_pin", "workflow fallback profile differs")
    fallback_home = identity.get("fallbackCodexHome")
    if fallback_home is not None and (
        not isinstance(fallback_home, str) or not Path(fallback_home).is_absolute()
    ):
        raise WorkflowPinError("invalid_pin", "workflow fallback namespace is invalid")


def validate(identity: JsonObject, config: JsonObject) -> None:
    validate_stamp(identity, config)
    current = capture(config)
    # Different detector provenance is harmless when it selects the same
    # profile and namespaces. Raw ambient overrides hidden by definitions do
    # not change effective identity and must not create a false refusal.
    if any(
        current[key] != identity.get(key)
        for key in ("profile", "namespaces", "fallbackProfile", "fallbackCodexHome")
    ):
        raise WorkflowPinError(
            "workflow_profile_drift",
            "selected Delegate profile or credential namespace differs from the workflow creation pin; restore its selector/home bindings or start a new workflow",
        )
