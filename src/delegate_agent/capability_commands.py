from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from delegate_agent import command_errors, reasoning
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


def _refresh_payload(config: JsonObject, workspace: str) -> JsonObject:
    try:
        existing_cache = reasoning.load_reasoning_capability_cache(workspace)
        refreshed_cache = reasoning.refresh_reasoning_capabilities(
            cwd=workspace,
            codex_binary=harness_binary(config, "codex"),
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
    stdout: TextIO,
) -> int:
    payload = (
        _refresh_payload(config, workspace)
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
            if isinstance(supported, list):
                print(f"{harness}: {len(supported)} static effort level(s)", file=stdout)
                continue
            models = harness_payload.get("models")
            if not isinstance(models, dict):
                continue
            print(f"{harness}: {len(models)} model(s)", file=stdout)
    return 0
