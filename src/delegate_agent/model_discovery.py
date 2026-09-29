"""Per-engine advisory model discovery: bundled tables, config aliases, optional live probes."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from difflib import SequenceMatcher
from pathlib import Path
from typing import TextIO

from delegate_agent import harness_discovery, profiles, redaction
from delegate_agent.bundled_models import BUNDLED_MODELS
from delegate_agent.constants import ENGINES_PROSE, KNOWN_ENGINES
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject, JsonValue

ENGINE_MODELS_SCHEMA = "delegate.engine-models.v1"
LIVE_WARNING_LIMIT = 8_000
LIVE_UNSUPPORTED_ENGINES = frozenset({"claude"})
_SOURCE_RANK = {"bundled": 0, "cache": 1, "discovery": 2, "live": 2, "config": 3}


def validate_engine_name(engine: str) -> None:
    if engine not in KNOWN_ENGINES:
        raise DelegateError(
            "invalid_engine",
            f"engine must be {ENGINES_PROSE}.",
        )


def engine_models_payload(
    config: JsonObject,
    engine: str,
    *,
    live: bool = False,
    factory_settings_path: Path | None = None,
    workspace: Path | None = None,
    discovery: JsonObject | None = None,
    legacy_cache: JsonObject | None = None,
    profile: profiles.ProfileResolution | None = None,
) -> JsonObject:
    validate_engine_name(engine)
    section = config.get(engine)
    if not isinstance(section, dict):
        section = {}

    default_model = section.get("defaultModel")
    default = default_model if isinstance(default_model, str) and default_model else None

    alias_map = section.get("models")
    aliases: list[JsonObject] = []
    if isinstance(alias_map, dict):
        for alias, mapping in sorted(alias_map.items()):
            model_id = _model_id_from_mapping(mapping)
            if isinstance(alias, str) and alias and model_id:
                aliases.append({"alias": alias, "model": model_id})

    entries: dict[str, JsonObject] = {}
    for item in BUNDLED_MODELS.get(engine, ()):
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        entry: JsonObject = {"id": model_id, "source": "bundled"}
        note = item.get("note")
        if isinstance(note, str) and note:
            entry["note"] = note
        entries[model_id] = entry

    warning: str | None = None
    live_field: JsonValue = False

    for item in _legacy_reasoning_models(legacy_cache, engine):
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id:
            _merge_entry(entries, model_id, source="cache")

    if live:
        if engine in LIVE_UNSUPPORTED_ENGINES:
            live_field = {
                "supported": False,
                "reason": f"{engine} has no non-interactive model enumeration",
            }
        else:
            live_models, probe_warning, live_default = _probe_live_models(
                config,
                engine,
                factory_settings_path=factory_settings_path,
                workspace=workspace,
                profile=profile,
            )
            if probe_warning:
                warning = probe_warning
                live_field = bool(live_models)
            else:
                live_field = True
            if default is None and live_default is not None:
                default = live_default
            for item in live_models:
                model_id = item.get("id")
                if not isinstance(model_id, str) or not model_id:
                    continue
                _merge_entry(
                    entries,
                    model_id,
                    source="live",
                    note=item.get("note") if isinstance(item.get("note"), str) else None,
                )
    else:
        if default is None:
            default = _discovered_default(discovery, engine)
        for item in _discovered_models(discovery, engine):
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            _merge_entry(
                entries,
                model_id,
                source="discovery",
                note=item.get("note") if isinstance(item.get("note"), str) else None,
            )

    _merge_config_models(entries, section, aliases)

    models = sorted(entries.values(), key=lambda item: str(item.get("id", "")))
    payload: JsonObject = {
        "schema": ENGINE_MODELS_SCHEMA,
        "ok": True,
        "engine": engine,
        "default": default,
        "aliases": aliases,
        "models": models,
        "live": live_field,
    }
    if warning:
        payload["warning"] = warning
    return payload


def _merge_config_models(
    entries: dict[str, JsonObject],
    section: JsonObject,
    aliases: list[JsonObject],
) -> None:
    default_model = section.get("defaultModel")
    if isinstance(default_model, str) and default_model:
        _merge_entry(entries, default_model, source="config")

    for item in aliases:
        model_id = item["model"]
        alias = item["alias"]
        if isinstance(model_id, str) and isinstance(alias, str):
            _merge_entry(entries, model_id, source="config", alias=alias)

    if section is not None:
        rem = section.get("reasoningEffortModels")
        if isinstance(rem, dict):
            for model_id in rem.values():
                if isinstance(model_id, str) and model_id:
                    _merge_entry(entries, model_id, source="config")


def _model_id_from_mapping(mapping: object) -> str | None:
    if isinstance(mapping, str) and mapping:
        return mapping
    if isinstance(mapping, dict):
        model = mapping.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _merge_entry(
    entries: dict[str, JsonObject],
    model_id: str,
    *,
    source: str,
    alias: str | None = None,
    note: str | None = None,
) -> None:
    existing = entries.get(model_id)
    if existing is None:
        entry: JsonObject = {"id": model_id, "source": source}
        if alias:
            entry["aliases"] = [alias]
        if note:
            entry["note"] = note
        entries[model_id] = entry
        return

    current_source = existing.get("source")
    current_rank = _SOURCE_RANK.get(current_source if isinstance(current_source, str) else "", -1)
    new_rank = _SOURCE_RANK.get(source, -1)
    if new_rank >= current_rank:
        existing["source"] = source
    if alias:
        alias_list = existing.get("aliases")
        if not isinstance(alias_list, list):
            alias_list = []
            existing["aliases"] = alias_list
        if alias not in alias_list:
            alias_list.append(alias)
    if note and "note" not in existing:
        existing["note"] = note


def _probe_live_models(
    config: JsonObject,
    engine: str,
    *,
    factory_settings_path: Path | None = None,
    workspace: Path | None = None,
    profile: profiles.ProfileResolution | None = None,
) -> tuple[list[JsonObject], str | None, str | None]:
    env = profiles.child_environment(overrides=profile.env if profile is not None else None)
    try:
        del workspace  # Global discovery is intentionally repository-neutral.
        record = harness_discovery.probe_harness(
            config,
            engine,
            env=env,
            factory_settings_path=factory_settings_path,
        )
        models = _legacy_models(record)
        status = record.get("probeStatus")
        raw_warnings = record.get("warnings")
        warnings = (
            [item for item in raw_warnings if isinstance(item, str)]
            if isinstance(raw_warnings, list)
            else []
        )
        warning = _public_live_warning("; ".join(warnings)) if warnings else None
        if status in {"missing", "error"}:
            return [], warning or f"{engine} metadata probe {status}", None
        default_model = record.get("defaultModel")
        discovered_default = (
            default_model if isinstance(default_model, str) and default_model else None
        )
        return models, warning, discovered_default
    except Exception as exc:
        return [], _public_live_warning(f"live probe failed: {exc}"), None


def _public_live_warning(value: str) -> str:
    return redaction.redact_progress_label(value)[:LIVE_WARNING_LIMIT]


def _legacy_models(fragment: JsonObject) -> list[JsonObject]:
    raw_models = fragment.get("models")
    if not isinstance(raw_models, dict):
        return []
    models: list[JsonObject] = []
    for selector, model in raw_models.items():
        if not isinstance(selector, str):
            continue
        entry: JsonObject = {"id": selector}
        if isinstance(model, dict) and isinstance(model.get("displayName"), str):
            entry["note"] = model["displayName"]
        models.append(entry)
    return models


def _discovered_models(discovery: JsonObject | None, engine: str) -> list[JsonObject]:
    if not isinstance(discovery, dict):
        return []
    harnesses = discovery.get("harnesses")
    record = harnesses.get(engine) if isinstance(harnesses, dict) else None
    return _legacy_models(record) if isinstance(record, dict) else []


def _discovered_default(discovery: JsonObject | None, engine: str) -> str | None:
    if not isinstance(discovery, dict):
        return None
    harnesses = discovery.get("harnesses")
    record = harnesses.get(engine) if isinstance(harnesses, dict) else None
    default = record.get("defaultModel") if isinstance(record, dict) else None
    return default if isinstance(default, str) and default else None


def discovered_model_ids(discovery: JsonObject | None, engine: str) -> tuple[str, ...]:
    """Catalog selectors a discovery snapshot observed for one engine.

    Empty means the snapshot carries no catalog for that engine -- absence of
    evidence, which callers must not read as evidence that a configured model
    is gone.
    """
    return tuple(
        entry["id"]
        for entry in _discovered_models(discovery, engine)
        if isinstance(entry.get("id"), str)
    )


def catalog_display_name(
    discovery: JsonObject | None, engine: str, selector: str | None
) -> str | None:
    """The catalog ``displayName`` recorded for one selector, or None.

    Cursor's stream reports a model's display name rather than the selector it
    was launched with, so this is the authoritative selector-to-label mapping
    when a discovery snapshot carries one.
    """
    if not isinstance(discovery, dict) or not selector:
        return None
    harnesses = discovery.get("harnesses")
    record = harnesses.get(engine) if isinstance(harnesses, dict) else None
    models = record.get("models") if isinstance(record, dict) else None
    entry = models.get(selector) if isinstance(models, dict) else None
    name = entry.get("displayName") if isinstance(entry, dict) else None
    return name if isinstance(name, str) and name else None


def is_provider_qualified(selector: str | None) -> bool:
    """Does an omp selector name its provider (``provider/model``)?

    This is the one place Delegate tells an explicit provider id from a bare
    name. omp splits the provider at the FIRST slash and keeps the rest as the
    model id, so ``gateway/acme/model-pro`` is provider ``gateway``; a leading
    or trailing slash names no provider or no model and is not qualified.
    """
    if not isinstance(selector, str):
        return False
    provider, separator, model_id = selector.partition("/")
    return bool(separator and provider and model_id)


def is_explicit_provider_id(selection: str | None, aliases: object) -> bool:
    """Is an omp selection a ``provider/model`` id the caller typed, not an alias?

    A key of ``omp.models`` is a Delegate alias even when its target names a
    provider, and it wins over the raw-id reading exactly as it does when the
    model is resolved. Aliases and ``omp.defaultModel`` carry the fleet's
    multi-subscription failover, so only a typed id counts as an operator's
    choice of one provider.
    """
    if not is_provider_qualified(selection):
        return False
    return not (isinstance(aliases, dict) and selection in aliases)


def _is_bare_omp_selection(selection: str, aliases: object) -> bool:
    """A selection that is neither a Delegate alias nor a provider-qualified id."""
    if not selection or is_provider_qualified(selection):
        return False
    return not (isinstance(aliases, dict) and selection in aliases)


def omp_unverifiable_selection_warning(
    selection: str,
    aliases: object,
    discovery: JsonObject | None,
) -> tuple[str, ...]:
    """Warn that a bare omp selection could not be checked for lack of a catalog.

    ``omp_unknown_alias_error`` refuses a bare token the discovered catalog does
    not list; with no catalog it cannot, and saying nothing would let a retired
    alias resolve by fuzzy match in silence.
    """
    if not _is_bare_omp_selection(selection, aliases) or discovered_model_ids(discovery, "omp"):
        return ()
    return (
        f"omp model {redaction.redact_string(selection)!r} is not a configured omp alias and no "
        "discovered omp catalog is available to confirm it as an exact model id; omp resolves "
        "such a name by fuzzy match and may serve a different provider. Use an explicit "
        "provider/model id, or run `delegate capabilities refresh` so bare names can be checked.",
    )


def omp_unknown_alias_error(
    selection: str,
    aliases: object,
    discovery: JsonObject | None,
) -> DelegateError | None:
    """An error for an omp selection that is neither an alias nor a real id.

    A selection resolves in this order: a key of ``omp.models`` is a Delegate
    alias; a ``provider/model`` selector is an explicit raw id and is never
    second-guessed here; anything else is a bare token. omp resolves a bare
    token by exact id and then by fuzzy match against its own bundled catalog,
    so a retired alias such as ``kimi`` quietly lands on whatever model the
    fuzzy pass finds first, on whatever provider owns it. A bare token is
    accepted only when it is exactly the model id of a discovered catalog entry.

    With no discovered catalog there is no evidence either way, so this returns
    None and the launch keeps its catalog-absence warning: an unprobed machine
    is not a reason to refuse a model id that may well be real.
    """
    if not _is_bare_omp_selection(selection, aliases):
        return None
    catalog = discovered_model_ids(discovery, "omp")
    if not catalog:
        return None
    if any(entry == selection or entry.partition("/")[2] == selection for entry in catalog):
        return None
    shown = redaction.redact_string(selection)
    configured = (
        sorted(alias for alias in aliases if isinstance(alias, str))
        if isinstance(aliases, dict)
        else []
    )
    known = (
        "Configured omp aliases: " + ", ".join(redaction.redact_string(a) for a in configured) + "."
        if configured
        else "No omp aliases are configured."
    )
    nearest = nearest_model_ids(selection, catalog)
    suggestion = (
        " Nearest catalog selectors: "
        + ", ".join(redaction.redact_string(selector) for selector in nearest)
        + "."
        if nearest
        else ""
    )
    return DelegateError(
        "invalid_alias",
        f"Unknown omp model alias {shown!r}: it is not a key of omp.models and not the exact "
        f"model id of any catalog entry, so omp would resolve it by fuzzy match and could serve "
        f"a different provider. {known}{suggestion} Use a configured alias or an explicit "
        "provider/model id (see `delegate models omp`); if the catalog is stale, run "
        "`delegate capabilities refresh`.",
    )


def nearest_model_ids(model: str, catalog: tuple[str, ...], *, limit: int = 5) -> list[str]:
    """Catalog selectors closest to ``model``, by shared prefix then length."""
    lowered = model.lower()

    def score(selector: str) -> tuple[int, int, str]:
        candidate = selector.lower()
        prefix = 0
        while prefix < min(len(candidate), len(lowered)) and candidate[prefix] == lowered[prefix]:
            prefix += 1
        return (-prefix, abs(len(candidate) - len(lowered)), candidate)

    return sorted(catalog, key=score)[:limit]


def configured_model_absence_warning(
    engine: str,
    model: str | None,
    discovery: JsonObject | None,
    *,
    alias: str | None = None,
) -> tuple[str, ...]:
    """Warn when a configured selector is absent from the discovered catalog.

    A configured default is validated for type only, so a retired selector
    survives in config until something else fails on it: cursor-agent rejects an
    unknown model outright ("Cannot use this model: grok"), while a
    fuzzy-resolving engine quietly serves a different concrete model. This is a
    warning, never a refusal -- a provider-side catalog lag is not evidence that
    a selector is dead -- and the configured value is never rewritten.

    ``alias`` names the `<engine>.models` key a configured target came from, so
    the finding points at the key an operator wrote rather than at a target
    that no other line of config mentions. Configured and catalog selectors are
    both operator/provider-supplied, so every one of them is redacted before it
    enters the message: the warning is persisted in `delegate.doctor.v1` and
    printed at launch, both of which promise to scrub credential-shaped text.
    """
    if not model:
        return ()
    catalog = discovered_model_ids(discovery, engine)
    if not catalog or model in catalog:
        return ()
    subject = (
        f"{engine} alias {redaction.redact_string(alias)!r} targets "
        f"{redaction.redact_string(model)!r}, which is"
        if alias
        else f"{engine} model {redaction.redact_string(model)!r} is"
    )
    nearest = nearest_model_ids(model, catalog)
    suggestion = (
        " Nearest discovered selectors: "
        + ", ".join(redaction.redact_string(selector) for selector in nearest)
        + "."
        if nearest
        else ""
    )
    return (
        f"{subject} absent from the discovered catalog.{suggestion} "
        "Run `delegate capabilities refresh` to update the cached catalog; "
        f"`delegate models {engine} --live` shows a fresh catalog without saving it.",
    )


def configured_alias_absence_warnings(
    engine: str,
    models: object,
    discovery: JsonObject | None,
) -> tuple[str, ...]:
    """Warn about configured `<engine>.models` targets the catalog does not list.

    Only the selected model is resolved through the alias table on a launch, so
    a target no run currently selects is still a selector an operator wrote
    down expecting it to work: a typo or a retired id sits there until someone
    happens to name its alias. Cache-only and advisory, exactly like the
    default-model check: no probe, no launch refusal, no rewrite of config.
    """
    if not isinstance(models, dict):
        return ()
    warnings: list[str] = []
    checked: set[str] = set()
    for alias, target in models.items():
        if not isinstance(target, str) or not target or target in checked:
            continue
        checked.add(target)
        warnings.extend(
            configured_model_absence_warning(
                engine,
                target,
                discovery,
                alias=alias if isinstance(alias, str) and alias else None,
            )
        )
    return tuple(warnings)


def launch_catalog(discovery: JsonObject | None, engine: str) -> tuple[tuple[str, ...], str]:
    """The catalog a launch checks a selector against, and its provenance.

    The discovered catalog is authoritative when the snapshot has one; the
    bundled table stands in only when discovery has nothing for the engine.
    """
    discovered = discovered_model_ids(discovery, engine)
    if discovered:
        return discovered, "discovered"
    bundled = tuple(entry["id"] for entry in BUNDLED_MODELS.get(engine, ()) if "id" in entry)
    return bundled, "bundled"


def launch_model_absence_warning(
    engine: str,
    model: str | None,
    discovery: JsonObject | None,
    *,
    catalog_model: str | None = None,
) -> tuple[str, ...]:
    """Warn on discovered catalog misses or likely typos in the bundled list.

    Launch preflight refuses only selectors it can prove unverifiable; a
    concrete id the harness will reject still launches and fails inside the
    child. This names the likely cause first. Advisory only: a catalog can lag
    the provider, so the launch is never refused and the selector never
    rewritten.

    ``catalog_model`` is the id compared against (and matched against nearest
    suggestions) when the launch selector carries Delegate-side decoration the
    catalog never stores, such as a Claude ``[1m]`` context-window suffix.
    """
    if not model:
        return ()
    lookup = catalog_model if catalog_model is not None else model
    catalog, source = launch_catalog(discovery, engine)
    if not catalog or lookup in catalog:
        return ()
    if source == "bundled":
        # Ranking alone always returns neighbors, even for unrelated selectors.
        # Cursor's optional provider prefix is not part of the model spelling.
        prefix = "cursor-" if engine == "cursor" else ""
        catalog = tuple(
            selector
            for selector in catalog
            if SequenceMatcher(
                None,
                lookup.lower().removeprefix(prefix),
                selector.lower().removeprefix(prefix),
            ).ratio()
            >= 0.8
        )
        if not catalog:
            return ()
    nearest = nearest_model_ids(lookup, catalog)
    if source == "bundled":
        return (
            f"{engine} model {redaction.redact_string(model)!r} is not in Delegate's small "
            f"built-in list; no discovery snapshot catalog exists for {engine}. "
            "It resembles "
            + ", ".join(redaction.redact_string(selector) for selector in nearest)
            + ". The launch proceeds. Run `delegate capabilities refresh` to update the "
            f"cached catalog, or `delegate models {engine} --live` to see a fresh one.",
        )
    suggestion = (
        " Nearest known selectors: "
        + ", ".join(redaction.redact_string(selector) for selector in nearest)
        + "."
        if nearest
        else ""
    )
    return (
        f"{engine} model {redaction.redact_string(model)!r} is absent from the {source} "
        f"catalog, so {engine} may reject it.{suggestion} Run `delegate capabilities refresh` "
        f"to update the cached catalog, or `delegate models {engine} --live` to see a fresh one.",
    )


_CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_CLAUDE_ALIASES = (*_CLAUDE_FAMILIES, "best", "opusplan", "default")
# A family word followed by a version: the shape of a mistyped alias or id.
_CLAUDE_ALIAS_TYPO_RE = re.compile(r"(?:claude[ _.])?(?:opus|sonnet|haiku|fable)[-_ .]?v?\d")
# Set for Bedrock, Vertex, Foundry, or a gateway; model names then follow the
# provider's rules, which Delegate cannot check.
CLAUDE_PROVIDER_ENV = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BASE_URL",
)


def claude_unknown_model_error(
    model: str | None,
    discovery: JsonObject | None,
    env: Mapping[str, str] | None = None,
) -> DelegateError | None:
    """Refuse a typed Claude selector that is an alias typo, before launch.

    Claude rejects a selector like ``opus-5.5`` only after the workspace and
    prompt were prepared, and a dry run cannot see that. The refusal is narrow on
    purpose: provider launches (Bedrock ARNs, Vertex ids, Foundry deployment
    names, gateway strings) take model names Delegate cannot enumerate, so only
    two shapes are refused. One is a selector that carries no model at all (a
    bare ``claude-`` or an empty ``[]``). The other is a family word followed by
    a version (``opus-5.5``, ``Sonnet 5``), and only when no provider or gateway
    variable in ``CLAUDE_PROVIDER_ENV`` is set. Everything else launches and
    gets the advisory ``launch_model_absence_warning`` if the catalog lacks it.
    Returns the ``invalid_alias`` error (the code omp and droid use for an
    unknown selector) or None when the selector is acceptable.
    """
    if not model:
        return None
    base = model.partition("[")[0]
    lowered = base.lower()
    catalog, _ = launch_catalog(discovery, "claude")
    # Stay permissive: a false refusal of a working model costs more than a missed
    # typo, so bracket suffix shapes are not whitelisted. Only a bare `claude-`
    # and an empty `[]` suffix carry no model at all.
    well_formed = (
        lowered in _CLAUDE_ALIASES
        or (lowered.startswith("claude-") and len(lowered) > len("claude-"))
        or base in catalog
    )
    empty = lowered == "claude-" or model.endswith("[]")
    if well_formed and not empty:
        return None
    if not empty:
        if not _CLAUDE_ALIAS_TYPO_RE.match(lowered):
            return None
        environment = os.environ if env is None else env
        if any(environment.get(name) for name in CLAUDE_PROVIDER_ENV):
            return None
    families = [family for family in _CLAUDE_FAMILIES if family in lowered]
    suggestions: list[str] = []
    for family in families:
        ids = [
            selector
            for selector in catalog
            if f"-{family}-" in selector or selector.endswith(f"-{family}")
        ]
        newest = max(ids, key=lambda s: tuple(int(n) for n in re.findall(r"\d+", s)), default=None)
        suggestions.append(f"{family} ({newest})" if newest else family)
    if not suggestions:
        suggestions = [
            redaction.redact_string(s) for s in nearest_model_ids(base, catalog, limit=3)
        ]
    did_you_mean = f" did you mean {' or '.join(suggestions)}?" if suggestions else ""
    valid = (
        f"Valid: aliases {', '.join(_CLAUDE_ALIASES)}"
        + (
            "; catalog ids " + ", ".join(redaction.redact_string(s) for s in catalog)
            if catalog
            else ""
        )
        + "; any well-formed claude-... id is also accepted."
        + " Provider model names (Bedrock, Vertex, Foundry, a gateway) pass through"
        " when CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_USE_VERTEX, CLAUDE_CODE_USE_FOUNDRY,"
        " or ANTHROPIC_BASE_URL is set in the launching environment or passed with --env or"
        " --env-file (e.g. `--env CLAUDE_CODE_USE_FOUNDRY=1`)."
    )
    return DelegateError(
        "invalid_alias",
        f"Unknown Claude model {redaction.redact_string(model)!r}:{did_you_mean} {valid} "
        "Claude would reject this selector after launch; see `delegate models claude`.",
    )


_FAMILY_WORD_RE = re.compile(r"[A-Za-z]+")
# Preferred effort/tier suffix inside one family version when a bare family name
# is resolved: the unsuffixed selector, then the balanced tiers.
_FAMILY_SUFFIX_PREFERENCE = ("", "high", "medium", "xhigh", "low")


def newest_family_selector(word: str, catalog: tuple[str, ...]) -> str | None:
    """Newest catalog selector for a bare family name such as ``grok``.

    Matches ``<word>-<version>[-suffix]`` with an optional ``cursor-`` prefix,
    takes the highest version, and within it prefers a non-fast selector and
    then ``_FAMILY_SUFFIX_PREFERENCE``. Returns None for anything that is not a
    bare word or when nothing in the catalog belongs to the family.
    """
    if not _FAMILY_WORD_RE.fullmatch(word):
        return None
    pattern = re.compile(rf"(?:cursor-)?{re.escape(word.lower())}-(\d+(?:\.\d+)*)(?:-(.+))?")
    ranked: list[tuple[tuple[int, ...], bool, int, str]] = []
    for selector in catalog:
        match = pattern.fullmatch(selector.lower())
        if match is None:
            continue
        version = tuple(int(part) for part in match.group(1).split("."))
        suffix_parts = (match.group(2) or "").split("-")
        fast = "fast" in suffix_parts
        tier = "-".join(part for part in suffix_parts if part and part != "fast")
        rank = (
            _FAMILY_SUFFIX_PREFERENCE.index(tier)
            if tier in _FAMILY_SUFFIX_PREFERENCE
            else len(_FAMILY_SUFFIX_PREFERENCE)
        )
        ranked.append((version, not fast, -rank, selector))
    if not ranked:
        return None
    best = max(ranked, key=lambda item: (item[0], item[1], item[2]))
    ties = sorted(item[3] for item in ranked if item[:3] == best[:3])
    return ties[0]


def _legacy_reasoning_models(cache: JsonObject | None, engine: str) -> list[JsonObject]:
    if not isinstance(cache, dict):
        return []
    harnesses = cache.get("harnesses")
    record = harnesses.get(engine) if isinstance(harnesses, dict) else None
    return _legacy_models(record) if isinstance(record, dict) else []


def emit_engine_models_text(payload: JsonObject, stdout: TextIO) -> None:
    engine = payload.get("engine")
    print(f"engine: {engine}", file=stdout)
    print(f"default: {payload.get('default')}", file=stdout)
    live = payload.get("live")
    if isinstance(live, dict) and live.get("supported") is False:
        print(f"warning: live unsupported — {live.get('reason')}", file=stdout)
    warning = payload.get("warning")
    if isinstance(warning, str) and warning:
        print(f"warning: {warning}", file=stdout)
    print("aliases:", file=stdout)
    aliases = payload.get("aliases")
    if isinstance(aliases, list) and aliases:
        for item in aliases:
            if isinstance(item, dict):
                print(f"  {item.get('alias')} -> {item.get('model')}", file=stdout)
    else:
        print("  (none)", file=stdout)
    print("models:", file=stdout)
    print(f"  {'id':<40} {'source':<8} {'aliases':<24} note", file=stdout)
    models = payload.get("models")
    if isinstance(models, list):
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", ""))
            source = str(item.get("source", ""))
            alias_list = item.get("aliases")
            alias_text = ",".join(alias_list) if isinstance(alias_list, list) else ""
            note = item.get("note")
            note_text = note if isinstance(note, str) else ""
            print(f"  {model_id:<40} {source:<8} {alias_text:<24} {note_text}", file=stdout)
    print(
        "note: bundled tables are advisory; harness is source of truth "
        "(use --live where supported).",
        file=stdout,
    )
