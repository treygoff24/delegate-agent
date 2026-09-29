"""Per-harness disable switch: ``<engine>.enabled: false`` in an engine's config block.

A retired harness whose binary is still on PATH otherwise stays in `models`,
`describe`, and `capabilities` forever, and stays launchable. Setting
``omp.enabled`` (or any engine's) to ``false`` removes the harness from those
listings and makes a launch fail fast with the config key to flip back. The key
is optional and defaults to enabled.
"""

from __future__ import annotations

from collections.abc import Mapping

from delegate_agent.constants import KNOWN_ENGINES
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject

# Payload locations keyed by harness name, as tuples of dict keys. Only these
# are stripped: a same-named key elsewhere (an `omp.models.droid` alias) is
# operator data, not a harness row.
_ENGINE_KEYED_DICTS: frozenset[tuple[str, ...]] = frozenset(
    {
        (),
        ("effectivePolicy",),
        ("engineCapabilities",),
        ("engineDefaults",),
        ("isolation", "safeNoneAllowed"),
        ("modeMapping",),
        ("personaTransports", "safe"),
        ("personaTransports", "work"),
        ("policyFieldSupport",),
        ("promptInstructionModes", "safeModeAllowed"),
        ("promptTransports",),
        ("reasoningAliases",),
        ("harnesses",),
        ("reasoning", "aliases"),
        ("reasoning", "harnesses"),
    }
)
# Top-level row lists whose rows name a harness, with the row fields that do.
_ROW_LISTS: Mapping[str, tuple[str, ...]] = {
    "commands": ("command", "name"),
    "aliases": ("provider",),
}


def validate_enabled_flags(config: Mapping[str, object]) -> tuple[str, str] | None:
    """Return (error code, message) for a non-boolean ``<engine>.enabled``, or None."""
    for engine in KNOWN_ENGINES:
        section = config.get(engine)
        if (
            isinstance(section, dict)
            and "enabled" in section
            and not isinstance(section["enabled"], bool)
        ):
            return (
                f"invalid_{engine}_config",
                f"{engine}.enabled must be a boolean (true or false).",
            )
    return None


def disabled_harnesses(config: Mapping[str, object] | None) -> frozenset[str]:
    if not isinstance(config, Mapping):
        return frozenset()
    return frozenset(
        engine
        for engine in KNOWN_ENGINES
        if isinstance(config.get(engine), dict) and config[engine].get("enabled") is False
    )


def require_enabled(config: Mapping[str, object] | None, engine: str) -> None:
    if engine in disabled_harnesses(config):
        raise DelegateError(
            "harness_disabled",
            f"Harness {engine} is disabled by config ({engine}.enabled is false), so it is "
            f"hidden from listings and cannot be launched. Set {engine}.enabled to true (or "
            "remove the key) in the active config to use it, or pick another harness.",
        )


def strip_disabled(payload: JsonObject, disabled: frozenset[str]) -> JsonObject:
    """Drop disabled harnesses from a listing payload's harness-keyed locations.

    Removes keys named for a disabled harness only at the known engine-keyed
    paths, drops it from the top-level ``engines`` list, and drops rows of the
    top-level ``commands`` and ``aliases`` lists that name it. Returns
    ``payload`` untouched when nothing is disabled.
    """
    if not disabled:
        return payload
    stripped = _strip(payload, disabled, ())
    return stripped if isinstance(stripped, dict) else payload


def _is_disabled_row(row: object, fields: tuple[str, ...], disabled: frozenset[str]) -> bool:
    return isinstance(row, dict) and any(
        isinstance(row.get(field), str) and row[field].split(" ", 1)[0] in disabled
        for field in fields
    )


def _strip(value: object, disabled: frozenset[str], path: tuple[str, ...]) -> object:
    if not isinstance(value, dict):
        return value
    engine_keyed = path in _ENGINE_KEYED_DICTS
    out: JsonObject = {}
    for key, child in value.items():
        if engine_keyed and key in disabled:
            continue
        if not path and key == "engines" and isinstance(child, list):
            out[key] = [item for item in child if item not in disabled]
        elif not path and key in _ROW_LISTS and isinstance(child, list):
            out[key] = [
                row for row in child if not _is_disabled_row(row, _ROW_LISTS[key], disabled)
            ]
        else:
            out[key] = _strip(child, disabled, (*path, key))
    return out
