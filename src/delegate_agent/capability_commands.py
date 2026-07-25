from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import harness_discovery, profiles, reasoning, redaction
from delegate_agent import rendering as delegate_rendering
from delegate_agent.constants import KNOWN_ENGINES
from delegate_agent.errors import EXIT_MISSING_BINARY, DelegateError
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class CapabilitiesCommand:
    refresh: bool = False
    engines: tuple[str, ...] | None = None
    json_mode: bool = False


class CapabilitiesError(DelegateError):
    """Capability failure that carries its own process exit code.

    Deriving from ``DelegateError`` keeps the machine state a caller sees
    consistent across commands: "no harness installed" exits with
    ``EXIT_MISSING_BINARY`` from both setup and capability refresh.
    """


def _public_attempts(attempt_records: JsonObject) -> JsonObject:
    """Return bounded probe outcomes without argv, catalogs, or diagnostics."""
    projected: JsonObject = {}
    for harness, raw_record in attempt_records.items():
        if not isinstance(harness, str) or not isinstance(raw_record, dict):
            continue
        record: JsonObject = {
            "installed": raw_record.get("installed") is True,
        }
        probe_status = raw_record.get("probeStatus")
        if isinstance(probe_status, str):
            record["probeStatus"] = probe_status
        version = raw_record.get("version")
        if isinstance(version, str) or version is None:
            record["version"] = version
        model_scope = raw_record.get("modelScope")
        if isinstance(model_scope, str):
            record["modelScope"] = model_scope
        models = raw_record.get("models")
        if isinstance(models, dict):
            record["catalogCount"] = len(models)
        projected[harness] = record
    return redaction.scrub_public_projection(projected)


def _drop_drifted_records(
    config: JsonObject,
    discovery: JsonObject | None,
    profile: profiles.ProfileResolution,
) -> tuple[JsonObject | None, list[str]]:
    """Hide cached records whose selector no longer matches the config.

    A launch refuses a drifted record (``request_build._runtime_discovery_for_engine``),
    so inspection must not advertise its catalog as live capability.
    """
    if not isinstance(discovery, dict):
        return discovery, []
    harnesses = discovery.get("harnesses")
    if not isinstance(harnesses, dict):
        return discovery, []
    drifted = {
        harness
        for harness, record in harnesses.items()
        if harness in KNOWN_ENGINES
        and isinstance(record, dict)
        and record.get("installed") is True
        and harness_discovery.selector_has_drifted(config, harness, record, profile=profile)
    }
    if not drifted:
        return discovery, []
    kept = {harness: record for harness, record in harnesses.items() if harness not in drifted}
    return {**discovery, "harnesses": kept}, sorted(drifted)


def capabilities_payload(
    config: JsonObject,
    config_source: str,
    workspace: str,
    *,
    profile: profiles.ProfileResolution | None = None,
    discovery: JsonObject | None = None,
) -> JsonObject:
    active_profile = profile or profiles.empty_profile_resolution()
    live_discovery, drifted_harnesses = _drop_drifted_records(config, discovery, active_profile)
    legacy_cache = reasoning.load_reasoning_capability_cache(workspace)
    legacy_path = reasoning.reasoning_capability_cache_path(workspace)
    raw_reasoning_payload = reasoning.build_reasoning_capabilities_payload(
        config,
        legacy_cache,
        discovery=live_discovery,
    )
    reasoning_payload = raw_reasoning_payload
    payload: JsonObject = {
        "ok": True,
        "configSource": config_source,
        "cachePath": str(harness_discovery.discovery_cache_path(active_profile.name)),
        "reasoning": reasoning_payload,
    }
    if drifted_harnesses:
        payload["driftedHarnesses"] = drifted_harnesses
    _add_legacy_cache_fields(payload, legacy_path, reasoning_payload)
    return redaction.scrub_public_projection(payload)


def _add_legacy_cache_fields(
    payload: JsonObject,
    legacy_path: Path,
    reasoning_payload: JsonObject,
) -> None:
    if legacy_path.exists():
        payload["legacyWorkspaceCachePath"] = str(legacy_path)
    if _contains_source(reasoning_payload, "cache"):
        payload["legacyWorkspaceCache"] = True


def _contains_source(value: object, source: str) -> bool:
    if isinstance(value, dict):
        if value.get("source") == source:
            return True
        return any(_contains_source(child, source) for child in value.values())
    if isinstance(value, list):
        return any(_contains_source(child, source) for child in value)
    return False


def _refresh_payload(
    config: JsonObject,
    workspace: str,
    *,
    profile: profiles.ProfileResolution,
    engines: tuple[str, ...] | None = None,
) -> JsonObject:
    try:
        result = harness_discovery.refresh_discovery(config, profile=profile, engines=engines)
    except OSError as exc:
        raise CapabilitiesError(
            "capability_refresh_failed",
            "capability refresh could not safely update the discovery cache.",
        ) from exc
    except ValueError as exc:
        raise CapabilitiesError(
            "capability_refresh_failed",
            "capability refresh returned invalid discovery data.",
        ) from exc

    attempts = result.get("attempts")
    attempt_records = attempts if isinstance(attempts, dict) else {}
    updated = result.get("updatedHarnesses")
    updated_harnesses = updated if isinstance(updated, list) else []
    public_attempts = _public_attempts(attempt_records)
    diagnostics: JsonObject = {
        "cachePath": str(result.get("cachePath", "")),
        "updatedHarnesses": updated_harnesses,
        "staleHarnesses": result.get("staleHarnesses", []),
        "attempts": public_attempts,
    }
    installed = [
        harness
        for harness, record in attempt_records.items()
        if isinstance(record, dict) and record.get("installed") is True
    ]
    if not installed:
        if engines:
            raise CapabilitiesError(
                "requested_harnesses_not_installed",
                "capability refresh found none of the requested harnesses installed: "
                f"{', '.join(engines)}. Other installed harnesses were not probed.",
                EXIT_MISSING_BINARY,
                diagnostics=diagnostics,
            )
        raise CapabilitiesError(
            "no_harnesses_installed",
            "capability refresh found no installed supported harnesses.",
            EXIT_MISSING_BINARY,
            diagnostics=diagnostics,
        )
    if not updated_harnesses:
        raise CapabilitiesError(
            "capability_refresh_failed",
            "installed harnesses were found, but every metadata probe failed.",
            diagnostics=diagnostics,
        )

    snapshot = result.get("snapshot")
    discovery = snapshot if isinstance(snapshot, dict) else None
    # A refreshed record carries the selector it was just probed with, but the
    # snapshot also returns records this run never touched: engines outside an
    # engine subset, and probe failures that kept their last-known-good record.
    live_discovery, drifted_harnesses = _drop_drifted_records(config, discovery, profile)
    legacy_cache = reasoning.load_reasoning_capability_cache(workspace)
    legacy_path = reasoning.reasoning_capability_cache_path(workspace)
    raw_reasoning_payload = reasoning.build_reasoning_capabilities_payload(
        config,
        legacy_cache,
        discovery=live_discovery,
    )
    reasoning_payload = raw_reasoning_payload
    payload: JsonObject = {
        "ok": True,
        "refreshed": True,
        "cachePath": str(result["cachePath"]),
        "reasoning": reasoning_payload,
        "updatedHarnesses": updated_harnesses,
        "staleHarnesses": result.get("staleHarnesses", []),
        "attempts": public_attempts,
    }
    if drifted_harnesses:
        payload["driftedHarnesses"] = drifted_harnesses
    _add_legacy_cache_fields(payload, legacy_path, reasoning_payload)
    return redaction.scrub_public_projection(payload)


def emit(
    command: CapabilitiesCommand,
    *,
    config: JsonObject,
    config_source: str,
    workspace: str,
    auth_profile_override: str | None = None,
    profile: profiles.ProfileResolution | None = None,
    discovery: JsonObject | None = None,
    stdout: TextIO,
) -> int:
    active_profile = profile or profiles.resolve_active_profile(
        config,
        profiles.child_environment(),
        cli_override=auth_profile_override,
    )
    if command.refresh:
        payload = _refresh_payload(
            config, workspace, profile=active_profile, engines=command.engines
        )
    else:
        payload = capabilities_payload(
            config,
            config_source,
            workspace,
            profile=active_profile,
            discovery=discovery,
        )

    if command.json_mode:
        delegate_rendering.print_json(payload, stdout)
    else:
        print(f"reasoning capabilities: {payload['cachePath']}", file=stdout)
        drifted = payload.get("driftedHarnesses")
        if isinstance(drifted, list) and drifted:
            print(
                "excluded (config selector drifted; rerun delegate setup): "
                f"{', '.join(str(harness) for harness in drifted)}",
                file=stdout,
            )
        harnesses = payload["reasoning"]["harnesses"]
        if not isinstance(harnesses, dict):
            return 0
        for harness, harness_payload in harnesses.items():
            if not isinstance(harness_payload, dict):
                continue
            supported = harness_payload.get("supported")
            warning = harness_payload.get("warning")
            if isinstance(warning, str):
                print(f"{harness}: {warning}", file=stdout)
                continue
            if isinstance(supported, list):
                print(f"{harness}: {len(supported)} static effort level(s)", file=stdout)
                continue
            models = harness_payload.get("models")
            if not isinstance(models, dict):
                continue
            print(f"{harness}: {len(models)} model(s)", file=stdout)
    return 0
