from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from delegate_agent import redaction, rendering
from delegate_agent.json_types import JsonObject
from delegate_agent.profiles import ProfileResolution


@dataclass(frozen=True)
class ProfilesCommand:
    json_mode: bool = False


def emit(
    command: ProfilesCommand,
    *,
    resolution: ProfileResolution,
    config_source: str,
    stdout: TextIO,
) -> int:
    env = redaction.redact_env_map(resolution.env)
    payload: JsonObject = {
        "ok": True,
        "profile": resolution.name,
        "source": resolution.source,
        "envKeys": sorted(env),
        "env": env,
        "warnings": list(resolution.warnings),
        "configSource": config_source,
    }
    if command.json_mode:
        rendering.print_json(payload, stdout)
    else:
        print(f"profile: {resolution.name or '(none)'}", file=stdout)
        print(f"source: {resolution.source or '(none)'}", file=stdout)
        keys = ", ".join(payload["envKeys"]) if payload["envKeys"] else "(none)"
        print(f"envKeys: {keys}", file=stdout)
        for warning in resolution.warnings:
            print(f"warning: {warning}", file=stdout)
    return 0
