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


def _target_path() -> Path:
    raw = delegate_config.config_path()
    if wsl.should_reject_windows_path(str(raw)):
        raise DelegateError("windows_path", wsl.windows_path_message("DELEGATE_CONFIG", str(raw)))
    return raw


def emit(command: ConfigCommand, stdout: TextIO) -> int:
    if command.action != "init":
        raise DelegateError("invalid_config_command", f"Unknown config action: {command.action}")
    path = _target_path()
    if path.exists() and not command.force:
        raise DelegateError(
            "config_exists",
            f"Config already exists at {path}; pass --force to overwrite it.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = delegate_config.example_config()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    ensure_private_file(path)
    result: JsonObject = {
        "ok": True,
        "path": str(path),
        "action": "init",
        "force": command.force,
    }
    if command.json_mode:
        delegate_rendering.print_json(result, stdout)
    else:
        print(f"wrote config: {path}", file=stdout)
    return EXIT_OK
