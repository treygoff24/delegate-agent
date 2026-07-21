from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Protocol, TypeAlias, TypeGuard

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject, JsonValue

ReasoningDeclaration: TypeAlias = Mapping[str, object]

# Bundled capabilities are a conservative fallback only. User config,
# profile-scoped discovery, and the legacy workspace cache take precedence so
# private/custom models do not require Delegate source changes and stale
# bundled data can be bypassed.
BUNDLED_REASONING_CAPABILITIES: dict[str, dict[str, ReasoningDeclaration]] = {
    "codex": {
        "gpt-5.6-sol": {
            "supported": ("low", "medium", "high", "xhigh", "max"),
            "default": "medium",
        },
        "gpt-5.5": {
            "supported": ("low", "medium", "high", "xhigh"),
            "default": "medium",
        },
        "gpt-5.4": {
            "supported": ("low", "medium", "high", "xhigh"),
            "default": "medium",
        },
        "gpt-5.4-mini": {
            "supported": ("low", "medium", "high", "xhigh"),
            "default": "high",
        },
        "gpt-5.3-codex-spark": {
            "supported": ("low", "medium", "high", "xhigh"),
            "default": "medium",
        },
    },
    "droid": {
        "claude-opus-4-8": {
            "supported": ("off", "low", "medium", "high", "xhigh", "max"),
            "default": "high",
        },
        "claude-sonnet-4-6": {
            "supported": ("off", "low", "medium", "high", "max"),
            "default": "high",
        },
        "gpt-5.5": {
            "supported": ("low", "medium", "high", "xhigh"),
            "default": "medium",
        },
        "gemini-3.5-flash": {
            "supported": ("minimal", "low", "medium", "high"),
            "default": "high",
        },
        "glm-5.1": {"supported": ("off", "high"), "default": "high"},
        "minimax-m2.7": {"supported": ("high",), "default": "high"},
    },
    "grok": {
        "grok-4.5": {
            "supported": ("low", "medium", "high"),
            "default": "high",
        },
    },
}

# Argv builders compare a capability's transport against these constants, so
# they must stay in lockstep with the table below — hence named constants, not
# re-typed string literals at the emission sites.
TRANSPORT_CODEX_CONFIG = "codex-config"
TRANSPORT_DROID_FLAG = "droid-flag"
TRANSPORT_CURSOR_MODEL_SELECTION = "cursor-model-selection"
TRANSPORT_CLAUDE_EFFORT_FLAG = "claude-effort-flag"
CLAUDE_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
TRANSPORT_GROK_EFFORT_FLAG = "grok-effort-flag"
GROK_NATIVE_EFFORTS = CLAUDE_NATIVE_EFFORTS
TRANSPORT_OPENCODE_VARIANT_FLAG = "variant-flag"
TRANSPORT_PI_THINKING_FLAG = "pi-thinking-flag"
PI_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
PI_THINKING_LEVELS = ("off", "minimal", *PI_NATIVE_EFFORTS)
INSPECT_REASONING_DISCOVERY_HINT = (
    "Inspect `delegate --json models --summary` or "
    "`delegate --json capabilities` for reasoning-effort support."
)
KIMI_UNSUPPORTED_REASONING_WARNING = "reasoning effort is not supported for kimi."
DEVIN_UNSUPPORTED_REASONING_WARNING = "reasoning effort is not supported for devin."


@dataclass(frozen=True)
class ReasoningProfile:
    transport: str | None
    # Strategies: "model-table" | "static-enum" | "effort-routing" | "pass-through" | "unsupported".
    strategy: str
    static_efforts: tuple[str, ...] | None = None  # static-enum (claude)
    unsupported_warning: str | None = None  # unsupported (kimi)


# Single source of truth for per-harness reasoning facts; the renderers below
# read from this table instead of re-encoding each fact. Row order matters:
# codex precedes droid so the derived model-table loop emits byte-identical
# output.
REASONING_PROFILES: dict[str, ReasoningProfile] = {
    "codex": ReasoningProfile(TRANSPORT_CODEX_CONFIG, "model-table"),
    "droid": ReasoningProfile(TRANSPORT_DROID_FLAG, "model-table"),
    "cursor": ReasoningProfile(TRANSPORT_CURSOR_MODEL_SELECTION, "effort-routing"),
    "claude": ReasoningProfile(
        TRANSPORT_CLAUDE_EFFORT_FLAG,
        "static-enum",
        static_efforts=CLAUDE_NATIVE_EFFORTS,
    ),
    "grok": ReasoningProfile(
        TRANSPORT_GROK_EFFORT_FLAG,
        "static-enum",
        static_efforts=GROK_NATIVE_EFFORTS,
    ),
    "kimi": ReasoningProfile(
        None,
        "unsupported",
        unsupported_warning=KIMI_UNSUPPORTED_REASONING_WARNING,
    ),
    "devin": ReasoningProfile(
        None,
        "unsupported",
        unsupported_warning=DEVIN_UNSUPPORTED_REASONING_WARNING,
    ),
    "opencode": ReasoningProfile(None, "pass-through"),
    "pi": ReasoningProfile(
        TRANSPORT_PI_THINKING_FLAG,
        "static-enum",
        static_efforts=PI_NATIVE_EFFORTS,
    ),
    "omp": ReasoningProfile(
        TRANSPORT_PI_THINKING_FLAG,
        "static-enum",
        static_efforts=PI_NATIVE_EFFORTS,
    ),
}

TRANSPORT_BY_HARNESS = {
    h: p.transport
    for h, p in REASONING_PROFILES.items()
    if p.transport is not None and p.strategy != "static-enum"
}


def _alias_key_for_default_model(default_model: object) -> str:
    return default_model if isinstance(default_model, str) and default_model else "(default)"


def _unsupported_reasoning_fields(harness: str) -> JsonObject:
    return {
        "supported": None,
        "source": "none",
        "warning": REASONING_PROFILES[harness].unsupported_warning,
    }


def _kimi_unsupported_reasoning_fields() -> JsonObject:
    return _unsupported_reasoning_fields("kimi")


def _resolved_model_required_detail(harness: str) -> str:
    if harness == "codex":
        return (
            "reasoning effort requires a resolved model — set codex.defaultModel "
            "in config (or pass a model via run-input JSON)"
        )
    if harness == "droid":
        return (
            "reasoning effort requires a resolved model — configure the selected "
            "Droid alias in droid.models"
        )
    return "reasoning effort requires a resolved model"


@dataclass(frozen=True)
class ReasoningCapability:
    harness: str
    model: str
    effort: str
    supported_efforts: tuple[str, ...]
    default_effort: str | None
    transport: str
    source: str
    evidence: str | None = None


class ReasoningCapabilityError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


class ReasoningPayloadCarrier(Protocol):
    reasoning_effort: str | None
    requested_reasoning_effort: str | None
    reasoning_effort_source: str | None
    reasoning_capability_source: str | None
    reasoning_capability_evidence: str | None
    reasoning_transport: str | None


def is_valid_effort_string(value: object) -> TypeGuard[str]:
    # Effort values land in child argv and inside a quoted Codex TOML override
    # (model_reasoning_effort="…"), so quoting hazards are banned alongside
    # whitespace.
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("-")
        and not any(ch.isspace() for ch in value)
        and '"' not in value
        and "\\" not in value
    )


def normalize_effort(value: object) -> str:
    if not is_valid_effort_string(value):
        raise ReasoningCapabilityError(
            "invalid_reasoning_effort",
            "reasoning effort must be a non-empty string that does not start with '-' and "
            "contains no whitespace, double quotes, or backslashes.",
        )
    return value


def format_explicit_reasoning_effort_error(
    *,
    harness: str,
    detail: str,
    alias: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    supported: tuple[str, ...] | list[str] | None = None,
) -> str:
    context_parts: list[str] = []
    if alias:
        context_parts.append(f"alias {alias!r}")
    context_parts.append(f"harness {harness}")
    if model:
        context_parts.append(f"model {model!r}")
    message = detail
    if context_parts:
        message = f"{detail} ({', '.join(context_parts)})"
    parts = [message]
    if effort is not None:
        parts.append(f"Requested effort: {effort!r}.")
    if supported:
        parts.append(f"Supported values: {', '.join(supported)}.")
    parts.append(INSPECT_REASONING_DISCOVERY_HINT)
    return " ".join(parts)


def resolve_native_effort(
    harness: str,
    requested_effort: str | None,
    *,
    alias: str | None = None,
    model: str | None = None,
) -> str | None:
    """Validate static native --effort values for harnesses that expose one."""
    if requested_effort is None:
        return None
    profile = REASONING_PROFILES[harness]
    supported = profile.static_efforts or ()
    effort = normalize_effort(requested_effort)
    if effort not in supported:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                model=model,
                effort=effort,
                supported=supported,
                detail="does not support reasoning effort",
            ),
        )
    return effort


def resolve_claude_native_effort(
    requested_effort: str | None,
    *,
    alias: str | None = None,
    model: str | None = None,
) -> str | None:
    """Validate Claude Code's native --effort values.

    Claude Code exposes reasoning effort as a process-level flag, not a
    per-model capability declaration. Keep this separate from
    ``resolve_reasoning_capability`` so user/cache model capability tables do
    not imply Claude model catalog support.
    """
    return resolve_native_effort("claude", requested_effort, alias=alias, model=model)


def resolve_grok_native_effort(
    requested_effort: str | None,
    *,
    alias: str | None = None,
    model: str | None = None,
) -> str | None:
    """Validate Grok Build CLI native --effort values."""
    return resolve_native_effort("grok", requested_effort, alias=alias, model=model)


def resolve_pi_native_effort(
    requested_effort: str | None,
    *,
    alias: str | None = None,
    model: str | None = None,
    engine: str = "pi",
) -> str | None:
    return resolve_native_effort(engine, requested_effort, alias=alias, model=model)


def _as_models_map(source: JsonValue) -> dict[str, JsonObject]:
    if not isinstance(source, dict):
        return {}
    models: dict[str, JsonObject] = {}
    for model, declaration in source.items():
        if isinstance(model, str) and model and isinstance(declaration, dict):
            models[model] = declaration
    return models


def _config_model_declarations(config: JsonObject, harness: str) -> dict[str, JsonObject]:
    reasoning = config.get("reasoning")
    if not isinstance(reasoning, dict):
        return {}
    capabilities = reasoning.get("capabilities")
    if not isinstance(capabilities, dict):
        return {}
    return _as_models_map(capabilities.get(harness))


def _cache_model_declarations(cache: JsonObject | None, harness: str) -> dict[str, JsonObject]:
    if not isinstance(cache, dict):
        return {}
    harnesses = cache.get("harnesses")
    if not isinstance(harnesses, dict):
        return {}
    harness_decl = harnesses.get(harness)
    if not isinstance(harness_decl, dict):
        return {}
    return _as_models_map(harness_decl.get("models"))


def _discovery_harness_record(discovery: JsonObject | None, harness: str) -> JsonObject | None:
    if not isinstance(discovery, dict):
        return None
    harnesses = discovery.get("harnesses")
    if not isinstance(harnesses, dict):
        return None
    record = harnesses.get(harness)
    return record if isinstance(record, dict) else None


def _discovery_model_declarations(
    discovery: JsonObject | None, harness: str
) -> dict[str, JsonObject]:
    record = _discovery_harness_record(discovery, harness)
    models = record.get("models") if record is not None else None
    if not isinstance(models, dict):
        return {}
    declarations: dict[str, JsonObject] = {}
    for model, entry in models.items():
        reasoning = entry.get("reasoning") if isinstance(entry, dict) else None
        if isinstance(model, str) and model and isinstance(reasoning, dict):
            declarations[model] = reasoning
    return declarations


def _discovery_harness_reasoning(discovery: JsonObject | None, harness: str) -> JsonObject | None:
    record = _discovery_harness_record(discovery, harness)
    declaration = record.get("harnessReasoning") if record is not None else None
    return declaration if isinstance(declaration, dict) else None


def _supported_tuple(
    declaration: ReasoningDeclaration, *, allow_empty: bool = False
) -> tuple[str, ...] | None:
    raw = declaration.get("supported")
    if (
        isinstance(raw, tuple)
        and (allow_empty or bool(raw))
        and all(isinstance(item, str) and item for item in raw)
    ):
        return raw
    if (
        isinstance(raw, list)
        and (allow_empty or bool(raw))
        and all(isinstance(item, str) and item for item in raw)
    ):
        return tuple(raw)
    return None


def _default_effort(declaration: ReasoningDeclaration, supported: tuple[str, ...]) -> str | None:
    raw = declaration.get("default")
    if raw is None:
        return None
    if isinstance(raw, str) and raw in supported:
        return raw
    return None


def _lookup_declaration(
    *,
    harness: str,
    model: str,
    config: JsonObject,
    cache: JsonObject | None,
    discovery: JsonObject | None = None,
) -> tuple[ReasoningDeclaration | None, str]:
    for source, declarations in (
        ("config", _config_model_declarations(config, harness)),
        ("discovery", _discovery_model_declarations(discovery, harness)),
        ("cache", _cache_model_declarations(cache, harness)),
        ("bundled", BUNDLED_REASONING_CAPABILITIES.get(harness, {})),
    ):
        declaration = declarations.get(model)
        if (
            source == "discovery"
            and declaration is not None
            and declaration.get("evidence") != "exact"
        ):
            continue
        if declaration is not None:
            return declaration, source
    return None, "none"


def resolve_reasoning_capability(
    *,
    harness: str,
    model: str | None,
    requested_effort: str | None,
    config: JsonObject,
    cache: JsonObject | None = None,
    discovery: JsonObject | None = None,
    alias: str | None = None,
) -> ReasoningCapability | None:
    if requested_effort is None:
        return None
    effort = normalize_effort(requested_effort)
    if harness not in TRANSPORT_BY_HARNESS:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                model=model,
                detail="reasoning effort is not supported",
            ),
        )
    if not model:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                detail=_resolved_model_required_detail(harness),
            ),
        )

    declaration, source = _lookup_declaration(
        harness=harness,
        model=model,
        config=config,
        cache=cache,
        discovery=discovery,
    )
    if declaration is None:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                model=model,
                effort=effort,
                detail="has no declared reasoning-effort capability",
            ),
        )
    supported = _supported_tuple(declaration, allow_empty=source == "discovery")
    if supported is None:
        raise ReasoningCapabilityError(
            "invalid_reasoning_config",
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                model=model,
                effort=effort,
                detail="has malformed reasoning capability data",
            ),
        )
    if effort not in supported:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                model=model,
                effort=effort,
                supported=supported,
                detail="does not support reasoning effort",
            ),
        )
    return ReasoningCapability(
        harness=harness,
        model=model,
        effort=effort,
        supported_efforts=supported,
        default_effort=_default_effort(declaration, supported),
        transport=TRANSPORT_BY_HARNESS[harness],
        source=source,
        evidence=(
            declaration.get("evidence")
            if source == "discovery" and isinstance(declaration.get("evidence"), str)
            else "exact"
        ),
    )


def discovery_default_model(discovery: JsonObject | None, harness: str) -> str | None:
    """Return a captured native default without treating it as an argv choice."""
    record = _discovery_harness_record(discovery, harness)
    value = record.get("defaultModel") if record is not None else None
    return value if isinstance(value, str) and value else None


def _capability_from_declaration(
    *,
    harness: str,
    model: str,
    effort: str,
    declaration: ReasoningDeclaration,
    source: str,
    transport: str,
    alias: str | None,
    fallback_evidence: str,
) -> ReasoningCapability:
    supported = _supported_tuple(declaration, allow_empty=source == "discovery")
    if supported is None:
        detail = (
            "advertises no reasoning-effort enum"
            if source == "discovery" and declaration.get("supported") is None
            else "has malformed reasoning capability data"
        )
        error = (
            "unsupported_reasoning_effort" if source == "discovery" else "invalid_reasoning_config"
        )
        raise ReasoningCapabilityError(
            error,
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                model=model,
                effort=effort,
                detail=detail,
            ),
        )
    if effort not in supported:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            format_explicit_reasoning_effort_error(
                harness=harness,
                alias=alias,
                model=model,
                effort=effort,
                supported=supported,
                detail="does not support reasoning effort",
            ),
        )
    raw_evidence = declaration.get("evidence") if source == "discovery" else None
    return ReasoningCapability(
        harness=harness,
        model=model,
        effort=effort,
        supported_efforts=supported,
        default_effort=_default_effort(declaration, supported),
        transport=transport,
        source=source,
        evidence=raw_evidence if isinstance(raw_evidence, str) else fallback_evidence,
    )


def resolve_harness_enum_capability(
    *,
    harness: str,
    model: str | None,
    requested_effort: str | None,
    discovery: JsonObject | None,
    alias: str | None = None,
) -> ReasoningCapability | None:
    """Resolve a process-wide effort enum, preferring captured help evidence."""
    if requested_effort is None:
        return None
    effort = normalize_effort(requested_effort)
    profile = REASONING_PROFILES[harness]
    declaration = _discovery_harness_reasoning(discovery, harness)
    if declaration is not None:
        return _capability_from_declaration(
            harness=harness,
            model=model or "",
            effort=effort,
            declaration=declaration,
            source="discovery",
            transport=profile.transport or "",
            alias=alias,
            fallback_evidence="harness",
        )
    declaration = {"supported": profile.static_efforts or ()}
    return _capability_from_declaration(
        harness=harness,
        model=model or "",
        effort=effort,
        declaration=declaration,
        source="harness-compatibility",
        transport=profile.transport or "",
        alias=alias,
        fallback_evidence="harness",
    )


def resolve_grok_reasoning_capability(
    *,
    model: str | None,
    requested_effort: str | None,
    config: JsonObject,
    discovery: JsonObject | None,
    cache: JsonObject | None = None,
    alias: str | None = None,
) -> tuple[ReasoningCapability | None, tuple[str, ...]]:
    """Resolve Grok exact declarations, then its documented CLI enum."""
    if requested_effort is None:
        return None, ()
    effort = normalize_effort(requested_effort)
    if model:
        declaration: ReasoningDeclaration | None = None
        source = "none"
        for candidate_source, declarations in (
            ("config", _config_model_declarations(config, "grok")),
            ("discovery", _discovery_model_declarations(discovery, "grok")),
            ("cache", _cache_model_declarations(cache, "grok")),
            ("bundled", BUNDLED_REASONING_CAPABILITIES.get("grok", {})),
        ):
            candidate = declarations.get(model)
            if candidate is None:
                continue
            if candidate_source == "discovery" and candidate.get("evidence") != "exact":
                continue
            declaration = candidate
            source = candidate_source
            break
        if declaration is not None:
            return (
                _capability_from_declaration(
                    harness="grok",
                    model=model,
                    effort=effort,
                    declaration=declaration,
                    source=source,
                    transport=TRANSPORT_GROK_EFFORT_FLAG,
                    alias=alias,
                    fallback_evidence="exact",
                ),
                (),
            )
    capability = resolve_harness_enum_capability(
        harness="grok",
        model=model,
        requested_effort=effort,
        discovery=discovery,
        alias=alias,
    )
    return capability, (
        "grok reasoning effort was validated against the harness compatibility enum; "
        "model-specific support is not known.",
    )


def resolve_discovered_model_capability(
    *,
    harness: str,
    model: str | None,
    requested_effort: str | None,
    discovery: JsonObject | None,
    alias: str | None = None,
) -> tuple[ReasoningCapability | None, tuple[str, ...]]:
    """Resolve OpenCode/Pi/OMP discovery strategies without inventing model facts."""
    if requested_effort is None:
        return None, ()
    effort = normalize_effort(requested_effort)
    declarations = _discovery_model_declarations(discovery, harness)
    declaration = declarations.get(model) if model else None
    transport = REASONING_PROFILES[harness].transport
    if harness == "opencode":
        if (
            declaration is None
            or declaration.get("evidence") != "exact"
            or declaration.get("supported") is None
        ):
            return (
                ReasoningCapability(
                    harness=harness,
                    model=model or "",
                    effort=effort,
                    supported_efforts=(effort,),
                    default_effort=None,
                    transport=TRANSPORT_OPENCODE_VARIANT_FLAG,
                    source="pass-through",
                    evidence="unknown",
                ),
                ("opencode_variant_unvalidated",),
            )
        return (
            _capability_from_declaration(
                harness=harness,
                model=model or "",
                effort=effort,
                declaration=declaration,
                source="discovery",
                transport=TRANSPORT_OPENCODE_VARIANT_FLAG,
                alias=alias,
                fallback_evidence="exact",
            ),
            (),
        )

    evidence = declaration.get("evidence") if declaration is not None else None
    has_usable_declaration = (
        declaration is not None
        and declaration.get("supported") is not None
        and (evidence == "exact" or (harness == "pi" and evidence == "harness"))
    )
    if has_usable_declaration:
        capability = _capability_from_declaration(
            harness=harness,
            model=model or "",
            effort=effort,
            declaration=declaration,
            source="discovery",
            transport=transport or "",
            alias=alias,
            fallback_evidence="exact",
        )
        if harness == "pi" and capability.evidence == "harness":
            capability = replace(capability, evidence="model-partial")
            return capability, (
                "pi reasoning effort was validated against a harness-wide enum; "
                "model-specific levels are partial evidence.",
            )
        return capability, ()

    harness_declaration = _discovery_harness_reasoning(discovery, harness)
    source = "discovery" if harness_declaration is not None else "harness-compatibility"
    fallback_evidence = "model-partial" if harness == "pi" else "harness-partial"
    capability = _capability_from_declaration(
        harness=harness,
        model=model or "",
        effort=effort,
        declaration=harness_declaration
        or {"supported": REASONING_PROFILES[harness].static_efforts or ()},
        source=source,
        transport=transport or "",
        alias=alias,
        fallback_evidence=fallback_evidence,
    )
    capability = replace(capability, evidence=fallback_evidence)
    return capability, (
        f"{harness} reasoning effort was validated against a harness-wide enum; "
        "model-specific levels are partial evidence.",
    )


def add_reasoning_payload_fields(payload: JsonObject, carrier: ReasoningPayloadCarrier) -> None:
    """Emit the reasoning JSON fields from any carrier with the shared attribute names.

    Used for both the CLI Request and the runner RunContext so dry-run payloads,
    manifests, and snapshots cannot drift. Requested and resolved effort are
    distinct because Cursor may route to another selector or drop an
    unsatisfied config-sourced default with a warning.
    """
    effort = carrier.reasoning_effort
    requested = getattr(carrier, "requested_reasoning_effort", None)
    if requested is not None:
        payload["requestedReasoningEffort"] = requested
    elif effort is not None:
        payload["requestedReasoningEffort"] = effort
    if effort is not None:
        payload["resolvedReasoningEffort"] = effort
    if carrier.reasoning_effort_source is not None:
        payload["reasoningEffortSource"] = carrier.reasoning_effort_source
    if carrier.reasoning_capability_source is not None:
        payload["reasoningCapabilitySource"] = carrier.reasoning_capability_source
    evidence = getattr(carrier, "reasoning_capability_evidence", None)
    if evidence is not None:
        payload["reasoningCapabilityEvidence"] = evidence
    if carrier.reasoning_transport is not None:
        payload["reasoningTransport"] = carrier.reasoning_transport


def reasoning_capability_cache_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"


def load_reasoning_capability_cache(workspace: str | Path) -> JsonObject | None:
    """Load the workspace capability cache, treating malformed payloads as absent.

    The disk read is the trust boundary: a corrupt cache must not block runs
    (config/bundled declarations may still resolve the model) and must not stop
    `capabilities refresh` from overwriting it with fresh data.
    """
    try:
        loaded = run_registry.read_json_object(reasoning_capability_cache_path(workspace))
    except run_registry.RegistryJsonError:
        return None
    if loaded is None:
        return None
    try:
        validate_cache_payload(loaded)
    except ReasoningCapabilityError:
        return None
    return loaded


def _copy_payload_model(declaration: ReasoningDeclaration, *, source: str) -> JsonObject | None:
    supported = _supported_tuple(declaration, allow_empty=source == "discovery")
    if source == "discovery" and declaration.get("supported") is None:
        payload: JsonObject = {"supported": None, "source": source}
        evidence = declaration.get("evidence")
        if isinstance(evidence, str):
            payload["evidence"] = evidence
        return payload
    if supported is None:
        return None
    payload: JsonObject = {"supported": list(supported), "source": source}
    default = _default_effort(declaration, supported)
    if default is not None:
        payload["default"] = default
    evidence = declaration.get("evidence")
    if source == "discovery" and isinstance(evidence, str):
        payload["evidence"] = evidence
    return payload


def _overlay_payload_models(
    payload: JsonObject, declarations: Mapping[str, ReasoningDeclaration], source: str
) -> None:
    for model, declaration in sorted(declarations.items()):
        model_payload = _copy_payload_model(declaration, source=source)
        if model_payload is not None:
            payload[model] = model_payload


def _cursor_reasoning_models_payload(mappings: JsonValue) -> JsonObject:
    models: JsonObject = {}
    if not isinstance(mappings, dict):
        return models
    for effort, model in sorted(mappings.items()):
        if not (isinstance(effort, str) and effort and isinstance(model, str) and model):
            continue
        model_payload = models.setdefault(model, {"supported": [], "source": "config"})
        supported = model_payload["supported"]
        if isinstance(supported, list):
            supported.append(effort)
    return models


def _model_reasoning_summary(
    *,
    harness: str,
    alias: str,
    model: str | None,
    config: JsonObject,
    cache: JsonObject | None,
    discovery: JsonObject | None = None,
    config_default: str | None = None,
) -> JsonObject:
    payload: JsonObject = {"alias": alias}
    if model:
        payload["model"] = model
    else:
        payload["supported"] = None
        payload["source"] = "none"
        payload["warning"] = f"{harness} requires a resolved model for reasoning-effort support."
        return payload

    declaration, source = _lookup_declaration(
        harness=harness,
        model=model,
        config=config,
        cache=cache,
        discovery=discovery,
    )
    if declaration is None:
        payload["supported"] = None
        payload["source"] = "none"
        payload["warning"] = (
            f"no declared reasoning-effort capability for {harness} model {model!r}."
        )
        return payload

    supported = _supported_tuple(declaration, allow_empty=source == "discovery")
    if source == "discovery" and declaration.get("supported") is None:
        payload["supported"] = None
        payload["source"] = source
        evidence = declaration.get("evidence")
        if isinstance(evidence, str):
            payload["evidence"] = evidence
        payload["warning"] = f"{harness} model {model!r} advertises no effort enum."
        if config_default is not None:
            payload["configDefault"] = config_default
        return payload
    if supported is None:
        payload["supported"] = None
        payload["source"] = source
        payload["warning"] = f"malformed reasoning capability data for {harness} model {model!r}."
        return payload

    payload["supported"] = list(supported)
    payload["source"] = source
    default = _default_effort(declaration, supported)
    if default is not None:
        payload["default"] = default
    evidence = declaration.get("evidence")
    if source == "discovery" and isinstance(evidence, str):
        payload["evidence"] = evidence
    if config_default is not None:
        payload["configDefault"] = config_default
    return payload


def _cursor_alias_reasoning_summary(cursor: JsonObject) -> JsonObject:
    default_model = cursor.get("defaultModel")
    alias = _alias_key_for_default_model(default_model)
    payload: JsonObject = {"alias": alias}
    if isinstance(default_model, str) and default_model:
        payload["defaultModel"] = default_model
    mappings = cursor.get("reasoningEffortModels")
    routing: list[JsonObject] = []
    if isinstance(mappings, dict):
        routing = [
            {"effort": effort, "model": model}
            for effort, model in sorted(mappings.items())
            if isinstance(effort, str) and effort and isinstance(model, str) and model
        ]
    if routing:
        payload["effortModelRouting"] = routing
        payload["source"] = "config"
    else:
        payload["source"] = "none"
        payload["warning"] = (
            "cursor.reasoningEffortModels is empty; configure effort-to-model mappings."
        )
    default_effort = cursor.get("defaultReasoningEffort")
    if isinstance(default_effort, str):
        payload["configDefault"] = default_effort
    return payload


def _static_alias_reasoning_summary(
    harness: str,
    section: JsonObject,
    discovery: JsonObject | None,
) -> JsonObject:
    default_model = section.get("defaultModel")
    alias = _alias_key_for_default_model(default_model)
    payload: JsonObject = {"alias": alias}
    if isinstance(default_model, str) and default_model:
        payload["model"] = default_model
    declaration = (
        _discovery_model_declarations(discovery, harness).get(default_model)
        if isinstance(default_model, str) and default_model
        else None
    )
    if declaration is None:
        declaration = _discovery_harness_reasoning(discovery, harness)
    observed = (
        _copy_payload_model(declaration, source="discovery") if declaration is not None else None
    )
    if observed is not None:
        payload.update(observed)
    else:
        payload.update(
            {
                "supported": list(REASONING_PROFILES[harness].static_efforts),
                "source": "static",
            }
        )
    payload["transport"] = REASONING_PROFILES[harness].transport
    default_effort = section.get("defaultReasoningEffort")
    if isinstance(default_effort, str):
        payload["configDefault"] = default_effort
    return payload


def _claude_alias_reasoning_summary(
    claude: JsonObject, discovery: JsonObject | None = None
) -> JsonObject:
    return _static_alias_reasoning_summary("claude", claude, discovery)


def _grok_alias_reasoning_summary(
    grok: JsonObject, discovery: JsonObject | None = None
) -> JsonObject:
    return _static_alias_reasoning_summary("grok", grok, discovery)


def _pi_alias_reasoning_summary(
    pi: JsonObject, discovery: JsonObject | None = None, *, harness: str = "pi"
) -> JsonObject:
    return _static_alias_reasoning_summary(harness, pi, discovery)


def _kimi_alias_reasoning_summary(
    kimi: JsonObject, discovery: JsonObject | None = None
) -> JsonObject:
    default_model = kimi.get("defaultModel")
    alias = _alias_key_for_default_model(default_model)
    declaration = (
        _discovery_model_declarations(discovery, "kimi").get(default_model)
        if isinstance(default_model, str) and default_model
        else None
    )
    observed = (
        _copy_payload_model(declaration, source="discovery") if declaration is not None else None
    )
    payload: JsonObject = {
        "alias": alias,
        **(observed or _kimi_unsupported_reasoning_fields()),
        "transport": None,
        "warning": KIMI_UNSUPPORTED_REASONING_WARNING,
    }
    if isinstance(default_model, str) and default_model:
        payload["model"] = default_model
    return payload


def _devin_alias_reasoning_summary(devin: JsonObject) -> JsonObject:
    default_model = devin.get("defaultModel")
    alias = _alias_key_for_default_model(default_model)
    payload: JsonObject = {"alias": alias, **_unsupported_reasoning_fields("devin")}
    if isinstance(default_model, str) and default_model:
        payload["model"] = default_model
    return payload


def _opencode_alias_reasoning_summary(
    opencode: JsonObject, discovery: JsonObject | None = None
) -> JsonObject:
    default_model = opencode.get("defaultModel")
    alias = _alias_key_for_default_model(default_model)
    declaration = (
        _discovery_model_declarations(discovery, "opencode").get(default_model)
        if isinstance(default_model, str) and default_model
        else None
    )
    observed = (
        _copy_payload_model(declaration, source="discovery") if declaration is not None else None
    )
    payload: JsonObject = {
        "alias": alias,
        **(observed or {"supported": None, "source": "pass-through"}),
        "transport": TRANSPORT_OPENCODE_VARIANT_FLAG,
    }
    if isinstance(default_model, str) and default_model:
        payload["model"] = default_model
    default_effort = opencode.get("defaultReasoningEffort")
    if isinstance(default_effort, str):
        payload["configDefault"] = default_effort
    return payload


def _add_static_alias_summaries(
    aliases: JsonObject,
    section: JsonObject,
    summary_fn: Callable[[JsonObject], JsonObject],
) -> None:
    """Add alias entries, selecting discovery evidence for each mapped model."""
    models = section.get("models")
    if not isinstance(models, dict):
        return
    for alias, model_id in sorted(models.items()):
        if not (isinstance(alias, str) and alias and isinstance(model_id, str) and model_id):
            continue
        alias_section = {**section, "defaultModel": model_id}
        entry = dict(summary_fn(alias_section))
        entry["alias"] = alias
        entry["model"] = model_id
        aliases[alias] = entry


def _add_opencode_alias_summaries(
    aliases: JsonObject, opencode: JsonObject, discovery: JsonObject | None
) -> None:
    models = opencode.get("models")
    if not isinstance(models, dict):
        return
    for alias, mapping in sorted(models.items()):
        if not isinstance(alias, str) or not alias:
            continue
        if isinstance(mapping, str) and mapping:
            entry = dict(
                _opencode_alias_reasoning_summary({**opencode, "defaultModel": mapping}, discovery)
            )
            entry["alias"] = alias
            entry["model"] = mapping
            aliases[alias] = entry
            continue
        if not isinstance(mapping, dict):
            continue
        model = mapping.get("model")
        variant = mapping.get("variant")
        if not (isinstance(model, str) and model and isinstance(variant, str) and variant):
            continue
        entry = dict(
            _opencode_alias_reasoning_summary({**opencode, "defaultModel": model}, discovery)
        )
        entry["alias"] = alias
        entry["model"] = model
        entry["pinnedVariant"] = variant
        entry["source"] = "alias"
        aliases[alias] = entry


def _add_pi_alias_summaries(
    aliases: JsonObject,
    pi: JsonObject,
    discovery: JsonObject | None,
    *,
    harness: str,
) -> None:
    models = pi.get("models")
    if not isinstance(models, dict):
        return
    for alias, mapping in sorted(models.items()):
        if not isinstance(alias, str) or not alias:
            continue
        if isinstance(mapping, str) and mapping:
            entry = dict(
                _pi_alias_reasoning_summary(
                    {**pi, "defaultModel": mapping}, discovery, harness=harness
                )
            )
            entry["alias"] = alias
            entry["model"] = mapping
            aliases[alias] = entry
            continue
        if not isinstance(mapping, dict):
            continue
        model = mapping.get("model")
        thinking = mapping.get("thinking")
        if not (isinstance(model, str) and model and isinstance(thinking, str) and thinking):
            continue
        entry = dict(
            _pi_alias_reasoning_summary({**pi, "defaultModel": model}, discovery, harness=harness)
        )
        entry["alias"] = alias
        entry["model"] = model
        entry["pinnedThinking"] = thinking
        entry["source"] = "alias"
        aliases[alias] = entry


def build_alias_reasoning_summaries(
    config: JsonObject,
    cache: JsonObject | None,
    *,
    discovery: JsonObject | None = None,
) -> JsonObject:
    summaries: JsonObject = {}

    droid = config.get("droid")
    droid_aliases: JsonObject = {}
    if isinstance(droid, dict):
        models = droid.get("models")
        config_default = droid.get("defaultReasoningEffort")
        default_effort = config_default if isinstance(config_default, str) else None
        if isinstance(models, dict):
            for alias, model_id in sorted(models.items()):
                if not (
                    isinstance(alias, str) and alias and isinstance(model_id, str) and model_id
                ):
                    continue
                droid_aliases[alias] = _model_reasoning_summary(
                    harness="droid",
                    alias=alias,
                    model=model_id,
                    config=config,
                    cache=cache,
                    discovery=discovery,
                    config_default=default_effort,
                )
    summaries["droid"] = droid_aliases

    codex = config.get("codex")
    codex_aliases: JsonObject = {}
    if isinstance(codex, dict):
        default_model = codex.get("defaultModel")
        config_default = codex.get("defaultReasoningEffort")
        default_effort = config_default if isinstance(config_default, str) else None
        if isinstance(default_model, str) and default_model:
            codex_aliases[default_model] = _model_reasoning_summary(
                harness="codex",
                alias=default_model,
                model=default_model,
                config=config,
                cache=cache,
                discovery=discovery,
                config_default=default_effort,
            )
        else:
            codex_aliases["(unconfigured)"] = {
                "alias": "(unconfigured)",
                "supported": None,
                "source": "none",
                "warning": "codex.defaultModel is not set; reasoning effort requires a resolved model.",
            }
        # codex.models aliases are CLI-selectable (--model <alias>) and their
        # effort validity is per-model, so give each the same table-backed
        # summary droid aliases get.
        models = codex.get("models")
        if isinstance(models, dict):
            for alias, model_id in sorted(models.items()):
                if not (
                    isinstance(alias, str) and alias and isinstance(model_id, str) and model_id
                ):
                    continue
                codex_aliases[alias] = _model_reasoning_summary(
                    harness="codex",
                    alias=alias,
                    model=model_id,
                    config=config,
                    cache=cache,
                    discovery=discovery,
                    config_default=default_effort,
                )
    summaries["codex"] = codex_aliases

    cursor = config.get("cursor")
    if isinstance(cursor, dict):
        default_model = cursor.get("defaultModel")
        alias_key = _alias_key_for_default_model(default_model)
        summaries["cursor"] = {alias_key: _cursor_alias_reasoning_summary(cursor)}
    else:
        summaries["cursor"] = {}

    claude = config.get("claude")
    if isinstance(claude, dict):
        default_model = claude.get("defaultModel")
        alias_key = _alias_key_for_default_model(default_model)
        summary_fn = partial(_claude_alias_reasoning_summary, discovery=discovery)
        claude_aliases: JsonObject = {alias_key: summary_fn(claude)}
        _add_static_alias_summaries(claude_aliases, claude, summary_fn)
        summaries["claude"] = claude_aliases
    else:
        summaries["claude"] = {}

    grok = config.get("grok")
    if isinstance(grok, dict):
        default_model = grok.get("defaultModel")
        alias_key = _alias_key_for_default_model(default_model)
        summary_fn = partial(_grok_alias_reasoning_summary, discovery=discovery)
        grok_aliases: JsonObject = {alias_key: summary_fn(grok)}
        _add_static_alias_summaries(grok_aliases, grok, summary_fn)
        summaries["grok"] = grok_aliases
    else:
        summaries["grok"] = {}

    devin = config.get("devin")
    if isinstance(devin, dict):
        default_model = devin.get("defaultModel")
        alias_key = _alias_key_for_default_model(default_model)
        devin_aliases: JsonObject = {alias_key: _devin_alias_reasoning_summary(devin)}
        _add_static_alias_summaries(devin_aliases, devin, _devin_alias_reasoning_summary)
        summaries["devin"] = devin_aliases
    else:
        summaries["devin"] = {}

    opencode = config.get("opencode")
    if isinstance(opencode, dict):
        default_model = opencode.get("defaultModel")
        alias_key = _alias_key_for_default_model(default_model)
        opencode_aliases: JsonObject = {
            alias_key: _opencode_alias_reasoning_summary(opencode, discovery)
        }
        _add_opencode_alias_summaries(opencode_aliases, opencode, discovery)
        summaries["opencode"] = opencode_aliases
    else:
        summaries["opencode"] = {}

    for engine in ("pi", "omp"):
        section = config.get(engine)
        if isinstance(section, dict):
            default_model = section.get("defaultModel")
            alias_key = _alias_key_for_default_model(default_model)
            aliases: JsonObject = {
                alias_key: _pi_alias_reasoning_summary(section, discovery, harness=engine)
            }
            _add_pi_alias_summaries(aliases, section, discovery, harness=engine)
            summaries[engine] = aliases
        else:
            summaries[engine] = {}

    kimi = config.get("kimi")
    if isinstance(kimi, dict):
        default_model = kimi.get("defaultModel")
        alias_key = _alias_key_for_default_model(default_model)
        summary_fn = partial(_kimi_alias_reasoning_summary, discovery=discovery)
        kimi_aliases: JsonObject = {alias_key: summary_fn(kimi)}
        _add_static_alias_summaries(kimi_aliases, kimi, summary_fn)
        summaries["kimi"] = kimi_aliases
    else:
        summaries["kimi"] = {}

    return summaries


def build_reasoning_capabilities_payload(
    config: JsonObject,
    cache: JsonObject | None,
    *,
    discovery: JsonObject | None = None,
) -> JsonObject:
    harnesses: JsonObject = {}
    for harness in [h for h, p in REASONING_PROFILES.items() if p.strategy == "model-table"]:
        models_payload: JsonObject = {}
        _overlay_payload_models(
            models_payload,
            BUNDLED_REASONING_CAPABILITIES.get(harness, {}),
            "bundled",
        )
        _overlay_payload_models(models_payload, _cache_model_declarations(cache, harness), "cache")
        _overlay_payload_models(
            models_payload,
            _discovery_model_declarations(discovery, harness),
            "discovery",
        )
        _overlay_payload_models(
            models_payload,
            _config_model_declarations(config, harness),
            "config",
        )
        harnesses[harness] = {
            "transport": TRANSPORT_BY_HARNESS[harness],
            "models": models_payload,
        }

    cursor = config.get("cursor")
    mappings = cursor.get("reasoningEffortModels") if isinstance(cursor, dict) else None
    cursor_models: JsonObject = {}
    _overlay_payload_models(
        cursor_models,
        _discovery_model_declarations(discovery, "cursor"),
        "discovery",
    )
    cursor_models.update(_cursor_reasoning_models_payload(mappings))
    harnesses["cursor"] = {
        "transport": TRANSPORT_BY_HARNESS["cursor"],
        "models": cursor_models,
    }
    for harness in ("claude", "grok", "pi", "omp"):
        models_payload = {}
        if harness == "grok":
            _overlay_payload_models(
                models_payload,
                BUNDLED_REASONING_CAPABILITIES.get("grok", {}),
                "bundled",
            )
        _overlay_payload_models(
            models_payload,
            _discovery_model_declarations(discovery, harness),
            "discovery",
        )
        harness_payload: JsonObject = {
            "transport": REASONING_PROFILES[harness].transport,
            "source": "static",
            "supported": list(REASONING_PROFILES[harness].static_efforts),
            "models": models_payload,
        }
        if harness == "grok":
            _overlay_payload_models(
                models_payload,
                _config_model_declarations(config, "grok"),
                "config",
            )
            harness_payload["source"] = "harness-compatibility"
        harness_reasoning = _discovery_harness_reasoning(discovery, harness)
        observed = (
            _copy_payload_model(harness_reasoning, source="discovery")
            if harness_reasoning is not None
            else None
        )
        if observed is not None:
            harness_payload.update(observed)
        harnesses[harness] = harness_payload
    for harness in [h for h, p in REASONING_PROFILES.items() if p.strategy == "pass-through"]:
        models_payload = {}
        _overlay_payload_models(
            models_payload,
            _discovery_model_declarations(discovery, harness),
            "discovery",
        )
        harnesses[harness] = {
            "transport": TRANSPORT_OPENCODE_VARIANT_FLAG,
            "source": "pass-through",
            "supported": None,
            "models": models_payload,
        }
    kimi_models: JsonObject = {}
    _overlay_payload_models(
        kimi_models,
        _discovery_model_declarations(discovery, "kimi"),
        "discovery",
    )
    harnesses["kimi"] = {
        "transport": REASONING_PROFILES["kimi"].transport,
        **_kimi_unsupported_reasoning_fields(),
        "models": kimi_models,
    }
    devin_models: JsonObject = {}
    _overlay_payload_models(
        devin_models,
        _discovery_model_declarations(discovery, "devin"),
        "discovery",
    )
    harnesses["devin"] = {
        "transport": REASONING_PROFILES["devin"].transport,
        **_unsupported_reasoning_fields("devin"),
        "models": devin_models,
    }
    return {
        "harnesses": harnesses,
        "aliases": build_alias_reasoning_summaries(config, cache, discovery=discovery),
    }


def validate_cache_payload(cache: JsonObject) -> None:
    harnesses = cache.get("harnesses")
    if not isinstance(harnesses, dict):
        raise ReasoningCapabilityError(
            "invalid_reasoning_config",
            "reasoning cache must contain a harnesses object.",
        )
    for harness, harness_decl in harnesses.items():
        if harness not in TRANSPORT_BY_HARNESS or not isinstance(harness_decl, dict):
            raise ReasoningCapabilityError(
                "invalid_reasoning_config",
                "reasoning cache harness entries must be objects for known harnesses.",
            )
        models = harness_decl.get("models")
        if not isinstance(models, dict):
            raise ReasoningCapabilityError(
                "invalid_reasoning_config",
                f"reasoning cache {harness}.models must be an object.",
            )
        for model, declaration in models.items():
            if not isinstance(model, str) or not model or not isinstance(declaration, dict):
                raise ReasoningCapabilityError(
                    "invalid_reasoning_config",
                    f"reasoning cache {harness}.models has malformed model declarations.",
                )
            supported = _supported_tuple(declaration)
            if supported is None:
                raise ReasoningCapabilityError(
                    "invalid_reasoning_config",
                    f"reasoning cache {harness}.{model}.supported must be non-empty strings.",
                )
            if (
                declaration.get("default") is not None
                and _default_effort(declaration, supported) is None
            ):
                raise ReasoningCapabilityError(
                    "invalid_reasoning_config",
                    f"reasoning cache {harness}.{model}.default must be in supported.",
                )


def parse_codex_models_payload(raw: JsonObject) -> JsonObject:
    models = raw.get("models")
    if not isinstance(models, list):
        raise ReasoningCapabilityError(
            "capability_refresh_failed",
            "codex model payload must contain a models array.",
        )
    target: JsonObject = {}
    parsed: JsonObject = {"schema": 1, "harnesses": {"codex": {"models": target}}}
    for item in models:
        if not isinstance(item, dict):
            raise ReasoningCapabilityError(
                "capability_refresh_failed", "codex model entries must be objects."
            )
        slug = item.get("slug")
        raw_supported = item.get("supported_reasoning_levels")
        if not isinstance(slug, str) or not slug:
            raise ReasoningCapabilityError(
                "capability_refresh_failed", "codex model entries require slug."
            )
        supported: list[str] = []
        if isinstance(raw_supported, list):
            for level in raw_supported:
                if isinstance(level, str) and level:
                    supported.append(level)
                elif (
                    isinstance(level, dict)
                    and isinstance(level.get("effort"), str)
                    and level["effort"]
                ):
                    supported.append(level["effort"])
        if not supported:
            raise ReasoningCapabilityError(
                "capability_refresh_failed",
                f"codex model {slug!r} has no supported reasoning levels.",
            )
        default = item.get("default_reasoning_level")
        declaration: JsonObject = {"supported": supported}
        if isinstance(default, str) and default:
            declaration["default"] = default
        target[slug] = declaration
    validate_cache_payload(parsed)
    return parsed


def merge_reasoning_capability_cache(
    existing: JsonObject | None,
    refreshed: JsonObject,
) -> JsonObject:
    """Overlay refreshed harness declarations on the existing cache.

    Inputs are already validated at their boundaries (parse_codex_models_payload
    for refreshed data, load_reasoning_capability_cache for the existing file),
    and write_reasoning_capability_cache validates the merged result.
    """
    harnesses: JsonObject = {}
    if existing is not None:
        existing_harnesses = existing.get("harnesses")
        if isinstance(existing_harnesses, dict):
            harnesses.update(existing_harnesses)
    refreshed_harnesses = refreshed["harnesses"]
    if not isinstance(refreshed_harnesses, dict):
        raise ReasoningCapabilityError(
            "capability_refresh_failed",
            "refreshed reasoning cache must contain a harnesses object.",
        )
    harnesses.update(refreshed_harnesses)
    return {"schema": 1, "harnesses": harnesses}


def write_reasoning_capability_cache(workspace: str | Path, cache: JsonObject) -> Path:
    validate_cache_payload(cache)
    path = reasoning_capability_cache_path(workspace)
    # write_json_atomic applies the registry's owner-only file/dir modes, keeping
    # this .delegate artifact as private as run manifests and logs.
    run_registry.write_json_atomic(path, cache)
    return path
