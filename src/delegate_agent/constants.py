"""Cross-cutting CLI mode vocabulary and validation.

A dependency leaf shared by the parser, request builder, argv builders, and
execution layers so they agree on the launch mode vocabulary without
importing ``cli``.
"""

from __future__ import annotations

from delegate_agent.errors import DelegateError

MODE_SAFE = "safe"
MODE_WORK = "work"
MODE_CALL = "call"
VALID_MODES = {MODE_SAFE, MODE_WORK, MODE_CALL}

# Canonical engine/harness vocabulary. This tuple's order is the registry order
# reused wherever engines are enumerated (the describe payload, help prose, the
# runs/worktree --harness filters). Membership checks and the prose enumeration
# both derive from it so the list can never drift out of sync.
KNOWN_ENGINES = ("cursor", "droid", "codex", "kimi", "claude", "grok", "devin", "opencode")
# "<engine>, ..., or <engine>" — for error messages and help text.
ENGINES_PROSE = f"{', '.join(KNOWN_ENGINES[:-1])}, or {KNOWN_ENGINES[-1]}"

# Engines launched without a model-alias positional argument.
MODELESS_ENGINES = tuple(engine for engine in KNOWN_ENGINES if engine != "droid")
# Engines whose binary is a simple <engine>.binary config key.
BINARY_CONFIG_ENGINES = tuple(engine for engine in KNOWN_ENGINES if engine != "cursor")
# Engines whose safe-review prompt prefix is injected by request_build.effective_prompt.
SAFE_REVIEW_PREFIX_INJECTED_HERE_ENGINES = tuple(
    engine
    for engine in KNOWN_ENGINES
    if engine in {"codex", "droid", "claude", "grok", "devin", "opencode"}
)
# Stable public summary order; membership is still derived from the modeless engine set.
MODEL_SUMMARY_ENGINES = tuple(
    engine
    for engine in ("cursor", "codex", "claude", "grok", "devin", "kimi", "opencode")
    if engine in MODELESS_ENGINES
)

# Prompt instruction wrapping: "wrapped" gets the skill-review preamble,
# safe-review prefix, and completion-report suffix; "slash-passthrough" sends
# the prompt verbatim so harness slash commands (e.g. Codex `/goal`) keep
# their required position-zero characters.
PROMPT_INSTRUCTION_MODE_WRAPPED = "wrapped"
PROMPT_INSTRUCTION_MODE_SLASH = "slash-passthrough"
# Engines whose safe mode is substantially prompt-enforced: workspace isolation
# protects the source tree, but the advisory safe-review prefix is what keeps
# the run review-shaped. A verbatim prompt would strip that contract, so slash
# pass-through is rejected in safe mode for these engines. codex/claude/grok
# safe is argv/sandbox-enforced and stays allowed.
PROMPT_ENFORCED_SAFE_ENGINES = tuple(
    engine for engine in KNOWN_ENGINES if engine in {"cursor", "droid", "kimi"}
)


def validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise DelegateError(
            "invalid_mode",
            "Mode must be safe, work, or call. Valid forms: "
            "delegate <harness> safe|work <prompt>; "
            "delegate droid <model-alias> safe|work <prompt>.",
        )
