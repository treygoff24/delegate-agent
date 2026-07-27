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
MODE_ORDER = (MODE_SAFE, MODE_WORK, MODE_CALL)

# Canonical engine/harness vocabulary. This tuple's order is the registry order
# reused wherever engines are enumerated (the describe payload, help prose, the
# runs/worktree --harness filters). Membership checks and the prose enumeration
# both derive from it so the list can never drift out of sync.
KNOWN_ENGINES = (
    "cursor",
    "droid",
    "codex",
    "kimi",
    "claude",
    "grok",
    "devin",
    "opencode",
    "pi",
    "omp",
)
# "<engine>, ..., or <engine>" — for error messages and help text.
ENGINES_PROSE = f"{', '.join(KNOWN_ENGINES[:-1])}, or {KNOWN_ENGINES[-1]}"
ENGINE_SUPPORTED_MODES = {
    engine: (MODE_WORK, MODE_CALL) if engine == "devin" else (MODE_SAFE, MODE_WORK, MODE_CALL)
    for engine in KNOWN_ENGINES
}


def engine_modes(engine: str) -> tuple[str, ...]:
    return ENGINE_SUPPORTED_MODES[engine]


def engine_mode_display(engine: str) -> str:
    return "{" + ",".join(engine_modes(engine)) + "}"


def pure_call_supported(engine: str) -> bool:
    # Claude-only for dogfood. Codex/OpenCode remain ineligible until their
    # boundaries meet the hostile-input contract (credential transport / tripwire).
    return engine == "claude"


def validate_pure_call(
    engine: str, *, pure: bool, read_only: bool, group: str | None = None
) -> None:
    """Reject --pure combinations that conflict with its stateless one-hop contract."""
    if not pure:
        return
    if group is not None:
        raise DelegateError(
            "pure_conflicts_group",
            "--pure cannot be combined with --group; pure call is a stateless one-hop completion.",
        )
    if read_only:
        raise DelegateError(
            "pure_conflicts_read_only", "--pure cannot be combined with --read-only."
        )
    if not pure_call_supported(engine):
        supported = ", ".join(PURE_CALL_ENGINES)
        raise DelegateError(
            "unsupported_pure_call",
            f"{engine} does not support pure call mode.",
            next_actions=[f"Use --pure with one of: {supported}."],
        )


# Public harness capability contract. Request validation and describe output both
# derive from this map so enabling a capability cannot drift between the two.
ENGINE_CAPABILITIES = {
    engine: {
        "pureCall": pure_call_supported(engine),
        "pureTripwire": engine == "claude",
        "structuredOutput": engine in {"codex", "claude"},
        "noSessionPersistence": engine in {"codex", "claude", "pi", "omp"},
        "usageEvents": engine == "claude",
        # These engines use stdin for all modes (not pure-only); keep the capability.
        "promptStdin": engine in {"codex", "claude", "opencode", "pi"},
    }
    for engine in KNOWN_ENGINES
}
PURE_CALL_ENGINES = tuple(
    engine for engine in KNOWN_ENGINES if ENGINE_CAPABILITIES[engine]["pureCall"]
)

# Engines launched without a model-alias positional argument.
MODELESS_ENGINES = tuple(engine for engine in KNOWN_ENGINES if engine != "droid")
# Engines whose binary is a simple <engine>.binary config key.
BINARY_CONFIG_ENGINES = tuple(engine for engine in KNOWN_ENGINES if engine != "cursor")
# Engines whose safe-review prompt prefix is injected by request_build.effective_prompt.
SAFE_REVIEW_PREFIX_INJECTED_HERE_ENGINES = tuple(
    engine
    for engine in KNOWN_ENGINES
    if engine in {"codex", "droid", "claude", "grok", "devin", "opencode", "pi", "omp"}
)
# Stable public summary order; membership is still derived from the modeless engine set.
MODEL_SUMMARY_ENGINES = tuple(
    engine
    for engine in (
        "cursor",
        "codex",
        "claude",
        "grok",
        "devin",
        "kimi",
        "opencode",
        "pi",
        "omp",
    )
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
