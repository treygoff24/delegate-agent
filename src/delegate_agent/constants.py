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

# Engines launched without a model-alias positional argument.
MODELESS_ENGINES = tuple(engine for engine in KNOWN_ENGINES if engine != "droid")
# Engines whose binary is a simple <engine>.binary config key.
BINARY_CONFIG_ENGINES = tuple(engine for engine in KNOWN_ENGINES if engine != "cursor")
# Modeless engines accepted by input JSON's optional model field.
MODELESS_NONCURSOR_ENGINES = tuple(
    engine for engine in KNOWN_ENGINES if engine not in {"cursor", "droid"}
)
# Engines whose safe-review prompt prefix is injected by request_build.effective_prompt.
SAFE_REVIEW_PREFIX_INJECTED_HERE_ENGINES = tuple(
    engine for engine in KNOWN_ENGINES if engine in {"codex", "droid", "claude", "grok"}
)
# Stable public summary order; membership is still derived from the modeless engine set.
MODEL_SUMMARY_ENGINES = tuple(
    engine for engine in ("cursor", "codex", "claude", "grok", "kimi") if engine in MODELESS_ENGINES
)


def validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise DelegateError("invalid_mode", "Mode must be safe or work.")
