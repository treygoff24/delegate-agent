from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from delegate_agent import command_errors, profiles, reasoning
from delegate_agent import rendering as delegate_rendering
from delegate_agent.config import harness_binary
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class CapabilitiesCommand:
    refresh: bool = False
    json_mode: bool = False


class CapabilitiesError(command_errors.CommandError):
    pass


def capabilities_payload(config: JsonObject, config_source: str, workspace: str) -> JsonObject:
    cache = reasoning.load_reasoning_capability_cache(workspace)
    return {
        "ok": True,
        "configSource": config_source,
        "cachePath": str(reasoning.reasoning_capability_cache_path(workspace)),
        "reasoning": reasoning.build_reasoning_capabilities_payload(config, cache),
    }


def _refresh_payload(
    config: JsonObject,
    workspace: str,
    *,
    auth_profile_override: str | None = None,
) -> JsonObject:
    profile = profiles.resolve_active_profile(
        config,
        profiles.child_environment(),
        cli_override=auth_profile_override,
    )
    try:
        existing_cache = reasoning.load_reasoning_capability_cache(workspace)
        refreshed_cache = reasoning.refresh_reasoning_capabilities(
            cwd=workspace,
            codex_binary=harness_binary(config, "codex"),
            env=profiles.child_environment(overrides=profile.env),
        )
        cache = reasoning.merge_reasoning_capability_cache(existing_cache, refreshed_cache)
        cache_path = reasoning.write_reasoning_capability_cache(workspace, cache)
    except reasoning.ReasoningCapabilityError as exc:
        raise CapabilitiesError(exc.error, exc.message) from exc
    return {
        "ok": True,
        "refreshed": True,
        "cachePath": str(cache_path),
        "reasoning": reasoning.build_reasoning_capabilities_payload(config, cache),
    }


def emit(
    command: CapabilitiesCommand,
    *,
    config: JsonObject,
    config_source: str,
    workspace: str,
    auth_profile_override: str | None = None,
    stdout: TextIO,
) -> int:
    payload = (
        _refresh_payload(config, workspace, auth_profile_override=auth_profile_override)
        if command.refresh
        else capabilities_payload(config, config_source, workspace)
    )

    if command.json_mode:
        delegate_rendering.print_json(payload, stdout)
    else:
        print(f"reasoning capabilities: {payload['cachePath']}", file=stdout)
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
