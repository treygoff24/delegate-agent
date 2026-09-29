"""Per-harness disable switch: ``harnesses.<name>.enabled: false`` in config.

A retired harness whose binary is still on PATH otherwise stays in `models`,
`describe`, and `capabilities` forever, and stays launchable. Flipping the key
removes the harness from those listings and makes a launch fail fast with the
config key to flip back. The key is optional and defaults to enabled.
"""

from __future__ import annotations

from collections.abc import Mapping

from delegate_agent.constants import ENGINES_PROSE, KNOWN_ENGINES
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject

SECTION = "harnesses"
_PROVIDER_KEYS = ("provider", "engine", "harness", "command", "name")


def validate_harnesses_section(section: object) -> str | None:
    """Return a problem sentence for a bad ``harnesses`` section, or None."""
    if section is None:
        return None
    if not isinstance(section, dict):
        return "harnesses must be an object keyed by harness name."
    for name, entry in section.items():
        if name not in KNOWN_ENGINES:
            return f"harnesses.{name} is not a known harness; use {ENGINES_PROSE}."
        if not isinstance(entry, dict):
            return f'harnesses.{name} must be an object such as {{"enabled": false}}.'
        unknown = sorted(set(entry) - {"enabled"})
        if unknown:
            return (
                f"harnesses.{name} has unknown keys: {', '.join(unknown)}. Allowed keys: enabled."
            )
        if "enabled" in entry and not isinstance(entry["enabled"], bool):
            return f"harnesses.{name}.enabled must be a boolean (true or false)."
    return None


def disabled_harnesses(config: Mapping[str, object] | None) -> frozenset[str]:
    section = config.get(SECTION) if isinstance(config, Mapping) else None
    if not isinstance(section, dict):
        return frozenset()
    return frozenset(
        name
        for name, entry in section.items()
        if isinstance(entry, dict) and entry.get("enabled") is False
    )


def require_enabled(config: Mapping[str, object] | None, engine: str) -> None:
    if engine in disabled_harnesses(config):
        raise DelegateError(
            "harness_disabled",
            f"Harness {engine} is disabled by config (harnesses.{engine}.enabled is false), so "
            f"it is hidden from listings and cannot be launched. Set harnesses.{engine}.enabled to true (or remove the key) "
            "in the active config to use it, or pick another harness.",
        )


def strip_disabled(payload: JsonObject, disabled: frozenset[str]) -> JsonObject:
    """Drop disabled harnesses from a listing payload, wherever it keys by harness.

    Removes dict keys named for a disabled harness, drops it from string lists
    named ``engines``, and drops list rows whose ``provider``/``engine``/``command``/
    ``harness`` field names it. Returns ``payload`` untouched when nothing is
    disabled.
    """
    if not disabled:
        return payload
    stripped = _strip(payload, disabled)
    return stripped if isinstance(stripped, dict) else payload


def _strip(value: object, disabled: frozenset[str]) -> object:
    if isinstance(value, dict):
        out: JsonObject = {}
        for key, child in value.items():
            if key in disabled:
                continue
            if key == "engines" and isinstance(child, list):
                out[key] = [item for item in child if item not in disabled]
                continue
            out[key] = _strip(child, disabled)
        return out
    if isinstance(value, list):
        return [
            _strip(item, disabled)
            for item in value
            if not (
                isinstance(item, dict)
                and any(
                    isinstance(item.get(key), str) and item[key].split(" ", 1)[0] in disabled
                    for key in _PROVIDER_KEYS
                )
            )
        ]
    return value
