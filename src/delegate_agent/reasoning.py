from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias, cast

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject, JsonValue

ReasoningDeclaration: TypeAlias = Mapping[str, object]

# Bundled capabilities are a conservative fallback only. User config and a
# refreshed workspace cache take precedence so private/custom models do not
# require Delegate source changes, and stale bundled data can be bypassed.
BUNDLED_REASONING_CAPABILITIES: dict[str, dict[str, ReasoningDeclaration]] = {
    "codex": {
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
}

# Argv builders compare a capability's transport against these constants, so
# they must stay in lockstep with the table below — hence named constants, not
# re-typed string literals at the emission sites.
TRANSPORT_CODEX_CONFIG = "codex-config"
TRANSPORT_DROID_FLAG = "droid-flag"
TRANSPORT_CURSOR_MODEL_SELECTION = "cursor-model-selection"
TRANSPORT_CLAUDE_EFFORT_FLAG = "claude-effort-flag"
CLAUDE_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
INSPECT_REASONING_DISCOVERY_HINT = (
    "Inspect `delegate --json models --summary --redacted` or "
    "`delegate --json capabilities` for reasoning-effort support."
)
KIMI_UNSUPPORTED_REASONING_WARNING = "reasoning effort is not supported for kimi."

TRANSPORT_BY_HARNESS = {
    "codex": TRANSPORT_CODEX_CONFIG,
    "droid": TRANSPORT_DROID_FLAG,
    "cursor": TRANSPORT_CURSOR_MODEL_SELECTION,
}


@dataclass(frozen=True)
class ReasoningCapability:
    harness: str
    model: str
    effort: str
    supported_efforts: tuple[str, ...]
    default_effort: str | None
    transport: str
    source: str


class ReasoningCapabilityError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


class ReasoningPayloadCarrier(Protocol):
    reasoning_effort: str | None
    reasoning_effort_source: str | None
    reasoning_capability_source: str | None
    reasoning_transport: str | None


def is_valid_effort_string(value: object) -> bool:
    # Effort values land in child argv and inside a quoted Codex TOML override
    # (model_reasoning_effort="…"), so quoting hazards are banned alongside
    # whitespace.
    return (
        isinstance(value, str)
        and bool(value)
        and not any(ch.isspace() for ch in value)
        and '"' not in value
        and "\\" not in value
    )


def normalize_effort(value: object) -> str:
    if not is_valid_effort_string(value):
        raise ReasoningCapabilityError(
            "invalid_reasoning_effort",
            "reasoning effort must be a non-empty string without whitespace, "
            "double quotes, or backslashes.",
        )
    return cast(str, value)


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
    if requested_effort is None:
        return None
    effort = normalize_effort(requested_effort)
    if effort not in CLAUDE_NATIVE_EFFORTS:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            format_explicit_reasoning_effort_error(
                harness="claude",
                alias=alias,
                model=model,
                effort=effort,
                supported=CLAUDE_NATIVE_EFFORTS,
                detail="does not support reasoning effort",
            ),
        )
    return effort


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


def _supported_tuple(declaration: ReasoningDeclaration) -> tuple[str, ...] | None:
    raw = declaration.get("supported")
    if isinstance(raw, tuple) and raw and all(isinstance(item, str) and item for item in raw):
        return raw
    if isinstance(raw, list) and raw and all(isinstance(item, str) and item for item in raw):
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
) -> tuple[ReasoningDeclaration | None, str]:
    for source, declarations in (
        ("config", _config_model_declarations(config, harness)),
        ("cache", _cache_model_declarations(cache, harness)),
        ("bundled", BUNDLED_REASONING_CAPABILITIES.get(harness, {})),
    ):
        declaration = declarations.get(model)
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
                detail="reasoning effort requires a resolved model",
            ),
        )

    declaration, source = _lookup_declaration(
        harness=harness,
        model=model,
        config=config,
        cache=cache,
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
    supported = _supported_tuple(declaration)
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
    )


def add_reasoning_payload_fields(payload: JsonObject, carrier: ReasoningPayloadCarrier) -> None:
    """Emit the reasoning JSON fields from any carrier with the shared attribute names.

    Used for both the CLI Request and the runner RunContext so dry-run payloads,
    manifests, and snapshots cannot drift. The documented schema exposes both
    requestedReasoningEffort and resolvedReasoningEffort; resolution never
    rewrites the value today, so both keys carry the single internal effort.
    """
    effort = carrier.reasoning_effort
    if effort is not None:
        payload["requestedReasoningEffort"] = effort
        payload["resolvedReasoningEffort"] = effort
    if carrier.reasoning_effort_source is not None:
        payload["reasoningEffortSource"] = carrier.reasoning_effort_source
    if carrier.reasoning_capability_source is not None:
        payload["reasoningCapabilitySource"] = carrier.reasoning_capability_source
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
    supported = _supported_tuple(declaration)
    if supported is None:
        return None
    payload: JsonObject = {"supported": list(supported), "source": source}
    default = _default_effort(declaration, supported)
    if default is not None:
        payload["default"] = default
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
    )
    if declaration is None:
        payload["supported"] = None
        payload["source"] = "none"
        payload["warning"] = (
            f"no declared reasoning-effort capability for {harness} model {model!r}."
        )
        return payload

    supported = _supported_tuple(declaration)
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
    if config_default is not None:
        payload["configDefault"] = config_default
    return payload


def _cursor_alias_reasoning_summary(cursor: JsonObject) -> JsonObject:
    default_model = cursor.get("defaultModel")
    alias = default_model if isinstance(default_model, str) and default_model else "(default)"
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


def _claude_alias_reasoning_summary(claude: JsonObject) -> JsonObject:
    default_model = claude.get("defaultModel")
    alias = default_model if isinstance(default_model, str) and default_model else "(default)"
    payload: JsonObject = {
        "alias": alias,
        "supported": list(CLAUDE_NATIVE_EFFORTS),
        "source": "static",
        "transport": TRANSPORT_CLAUDE_EFFORT_FLAG,
    }
    if isinstance(default_model, str) and default_model:
        payload["model"] = default_model
    default_effort = claude.get("defaultReasoningEffort")
    if isinstance(default_effort, str):
        payload["configDefault"] = default_effort
    return payload


def _kimi_alias_reasoning_summary(kimi: JsonObject) -> JsonObject:
    default_model = kimi.get("defaultModel")
    alias = default_model if isinstance(default_model, str) and default_model else "(default)"
    payload: JsonObject = {
        "alias": alias,
        "supported": None,
        "source": "none",
        "warning": KIMI_UNSUPPORTED_REASONING_WARNING,
    }
    if isinstance(default_model, str) and default_model:
        payload["model"] = default_model
    return payload


def build_alias_reasoning_summaries(
    config: JsonObject,
    cache: JsonObject | None,
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
                config_default=default_effort,
            )
        else:
            codex_aliases["(unconfigured)"] = {
                "alias": "(unconfigured)",
                "supported": None,
                "source": "none",
                "warning": "codex.defaultModel is not set; reasoning effort requires a resolved model.",
            }
    summaries["codex"] = codex_aliases

    cursor = config.get("cursor")
    if isinstance(cursor, dict):
        default_model = cursor.get("defaultModel")
        alias_key = (
            default_model if isinstance(default_model, str) and default_model else "(default)"
        )
        summaries["cursor"] = {alias_key: _cursor_alias_reasoning_summary(cursor)}
    else:
        summaries["cursor"] = {}

    claude = config.get("claude")
    if isinstance(claude, dict):
        default_model = claude.get("defaultModel")
        alias_key = (
            default_model if isinstance(default_model, str) and default_model else "(default)"
        )
        summaries["claude"] = {alias_key: _claude_alias_reasoning_summary(claude)}
    else:
        summaries["claude"] = {}

    kimi = config.get("kimi")
    if isinstance(kimi, dict):
        default_model = kimi.get("defaultModel")
        alias_key = (
            default_model if isinstance(default_model, str) and default_model else "(default)"
        )
        summaries["kimi"] = {alias_key: _kimi_alias_reasoning_summary(kimi)}
    else:
        summaries["kimi"] = {}

    return summaries


def build_reasoning_capabilities_payload(
    config: JsonObject,
    cache: JsonObject | None,
) -> JsonObject:
    harnesses: JsonObject = {}
    for harness in ("codex", "droid"):
        models_payload: JsonObject = {}
        _overlay_payload_models(
            models_payload,
            BUNDLED_REASONING_CAPABILITIES.get(harness, {}),
            "bundled",
        )
        _overlay_payload_models(models_payload, _cache_model_declarations(cache, harness), "cache")
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
    harnesses["cursor"] = {
        "transport": TRANSPORT_BY_HARNESS["cursor"],
        "models": _cursor_reasoning_models_payload(mappings),
    }
    harnesses["claude"] = {
        "transport": TRANSPORT_CLAUDE_EFFORT_FLAG,
        "source": "static",
        "supported": list(CLAUDE_NATIVE_EFFORTS),
        "models": {},
    }
    harnesses["kimi"] = {
        "transport": None,
        "supported": None,
        "source": "none",
        "warning": KIMI_UNSUPPORTED_REASONING_WARNING,
        "models": {},
    }
    return {"harnesses": harnesses, "aliases": build_alias_reasoning_summaries(config, cache)}


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


def refresh_reasoning_capabilities(*, cwd: str, codex_binary: str = "codex") -> JsonObject:
    try:
        completed = subprocess.run(  # nosec B603 - configured Codex binary is executed with shell=False and a timeout.
            [codex_binary, "debug", "models"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReasoningCapabilityError(
            "capability_refresh_failed",
            f"could not run {codex_binary} debug models: {exc}",
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise ReasoningCapabilityError(
            "capability_refresh_failed",
            f"{codex_binary} debug models failed: {stderr or completed.returncode}",
        )
    try:
        raw: JsonValue = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReasoningCapabilityError(
            "capability_refresh_failed",
            f"{codex_binary} debug models did not return JSON: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise ReasoningCapabilityError(
            "capability_refresh_failed",
            f"{codex_binary} debug models returned a non-object payload.",
        )
    return parse_codex_models_payload(raw)


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
