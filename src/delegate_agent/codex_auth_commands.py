from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class CodexAuthCommand:
    action: str
    json_mode: bool = False
    profile: str | None = None
    fallback: str | None = None


def emit(
    command: CodexAuthCommand,
    *,
    config: JsonObject,
    config_source: str,
    stdout: TextIO,
) -> int:
    _ = command, config, config_source, stdout
    raise DelegateError(
        "codex_auth_removed",
        "delegate codex-auth used the removed Codex-only auth config surface; "
        "profile-aware auth now resolves from top-level profiles. The delegate profiles "
        "introspection command is Phase 2.",
    )
