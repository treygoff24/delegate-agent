"""Cross-cutting CLI mode vocabulary and validation.

A dependency leaf shared by the parser, request builder, argv builders, and
execution layers so they agree on the safe/work mode vocabulary without
importing ``cli``.
"""

from __future__ import annotations

from delegate_agent.errors import DelegateError

MODE_SAFE = "safe"
MODE_WORK = "work"
VALID_MODES = {MODE_SAFE, MODE_WORK}


def validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise DelegateError("invalid_mode", "Mode must be safe or work.")
