from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from delegate_agent.json_types import JsonObject, JsonValue

# Bundled capabilities are a conservative fallback only. User config and a
# refreshed workspace cache take precedence so private/custom models do not
# require Delegate source changes, and stale bundled data can be bypassed.
BUNDLED_REASONING_CAPABILITIES: dict[str, dict[str, JsonObject]] = {
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

TRANSPORT_BY_HARNESS = {
    "codex": "codex-config",
    "droid": "droid-flag",
    "cursor": "cursor-model-selection",
}


@dataclass(frozen=True)
class ReasoningCapability:
    harness: str
    model: str
    requested_effort: str
    resolved_effort: str
    supported_efforts: tuple[str, ...]
    default_effort: str | None
    transport: str
    source: str


class ReasoningCapabilityError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def normalize_effort(value: object) -> str:
    if not isinstance(value, str):
        raise ReasoningCapabilityError(
            "invalid_reasoning_effort",
            "reasoning effort must be a non-empty string.",
        )
    if not value or value != value.strip() or any(ch.isspace() for ch in value):
        raise ReasoningCapabilityError(
            "invalid_reasoning_effort",
            "reasoning effort must be a non-empty string without whitespace.",
        )
    return value


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


def _supported_tuple(declaration: JsonObject) -> tuple[str, ...] | None:
    raw = declaration.get("supported")
    if isinstance(raw, tuple) and raw and all(isinstance(item, str) and item for item in raw):
        return raw
    if isinstance(raw, list) and raw and all(isinstance(item, str) and item for item in raw):
        return tuple(raw)
    return None


def _default_effort(declaration: JsonObject, supported: tuple[str, ...]) -> str | None:
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
) -> tuple[JsonObject | None, str]:
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
) -> ReasoningCapability | None:
    if requested_effort is None:
        return None
    effort = normalize_effort(requested_effort)
    if harness not in TRANSPORT_BY_HARNESS:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            f"Reasoning effort is not supported for harness: {harness}.",
        )
    if not model:
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            f"{harness} reasoning effort requires a resolved model.",
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
            f"{harness} model {model!r} has no declared reasoning-effort capability.",
        )
    supported = _supported_tuple(declaration)
    if supported is None:
        raise ReasoningCapabilityError(
            "invalid_reasoning_config",
            f"{harness} model {model!r} has malformed reasoning capability data.",
        )
    if effort not in supported:
        supported_label = ", ".join(supported)
        raise ReasoningCapabilityError(
            "unsupported_reasoning_effort",
            f"{harness} model {model!r} does not support reasoning effort {effort!r}; "
            f"supported values: {supported_label}.",
        )
    return ReasoningCapability(
        harness=harness,
        model=model,
        requested_effort=effort,
        resolved_effort=effort,
        supported_efforts=supported,
        default_effort=_default_effort(declaration, supported),
        transport=TRANSPORT_BY_HARNESS[harness],
        source=source,
    )


def reasoning_capability_cache_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"


def load_reasoning_capability_cache(workspace: str | Path) -> JsonObject | None:
    path = reasoning_capability_cache_path(workspace)
    try:
        loaded: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _copy_payload_model(declaration: JsonObject, *, source: str) -> JsonObject | None:
    supported = _supported_tuple(declaration)
    if supported is None:
        return None
    payload: JsonObject = {"supported": list(supported), "source": source}
    default = _default_effort(declaration, supported)
    if default is not None:
        payload["default"] = default
    return payload


def _overlay_payload_models(
    payload: JsonObject, declarations: dict[str, JsonObject], source: str
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
    return {"harnesses": harnesses}


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
    parsed: JsonObject = {"schema": 1, "harnesses": {"codex": {"models": {}}}}
    target = parsed["harnesses"]["codex"]["models"]
    assert isinstance(target, dict)
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
        completed = subprocess.run(
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
    validate_cache_payload(refreshed)
    if existing is None:
        merged: JsonObject = {"schema": 1, "harnesses": {}}
    else:
        validate_cache_payload(existing)
        merged = copy.deepcopy(existing)
        if not isinstance(merged.get("harnesses"), dict):
            merged["harnesses"] = {}

    harnesses = merged["harnesses"]
    refreshed_harnesses = refreshed["harnesses"]
    assert isinstance(harnesses, dict)
    assert isinstance(refreshed_harnesses, dict)
    for harness, declaration in refreshed_harnesses.items():
        harnesses[harness] = copy.deepcopy(declaration)
    validate_cache_payload(merged)
    return merged


def write_reasoning_capability_cache(workspace: str | Path, cache: JsonObject) -> Path:
    validate_cache_payload(cache)
    path = reasoning_capability_cache_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
