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

# Canonical engine/harness vocabulary. This tuple's order is the registry order
# reused wherever engines are enumerated (the describe payload, help prose, the
# runs/worktree --harness filters). Membership checks and the prose enumeration
# both derive from it so the list can never drift out of sync.
KNOWN_ENGINES = ("cursor", "droid", "codex", "kimi", "claude", "grok")
# "cursor, droid, codex, kimi, claude, or grok" — for error messages and help text.
ENGINES_PROSE = f"{', '.join(KNOWN_ENGINES[:-1])}, or {KNOWN_ENGINES[-1]}"


def validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise DelegateError("invalid_mode", "Mode must be safe or work.")
