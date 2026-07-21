"""Per-engine advisory model discovery: bundled tables, config aliases, optional live probes."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent import harness_discovery, profiles
from delegate_agent.bundled_models import BUNDLED_MODELS
from delegate_agent.constants import ENGINES_PROSE, KNOWN_ENGINES
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject, JsonValue

ENGINE_MODELS_SCHEMA = "delegate.engine-models.v1"
LIVE_PROBE_TIMEOUT_SEC = 10
DEVIN_LIVE_SENTINEL = "delegate-live-probe-sentinel"
LIVE_UNSUPPORTED_ENGINES = frozenset({"claude"})
_SOURCE_RANK = {"bundled": 0, "cache": 1, "discovery": 2, "live": 2, "config": 3}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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
        if engine == "devin":
            return _probe_devin_models(config, env=env), None, None
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
        warning = "; ".join(warnings) or None
        if status in {"missing", "error"}:
            return [], warning or f"{engine} metadata probe {status}", None
        default_model = record.get("defaultModel")
        discovered_default = (
            default_model if isinstance(default_model, str) and default_model else None
        )
        return models, warning, discovered_default
    except Exception as exc:
        return [], f"live probe failed: {exc}", None


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


def _legacy_reasoning_models(cache: JsonObject | None, engine: str) -> list[JsonObject]:
    if not isinstance(cache, dict):
        return []
    harnesses = cache.get("harnesses")
    record = harnesses.get(engine) if isinstance(harnesses, dict) else None
    return _legacy_models(record) if isinstance(record, dict) else []


def parse_cursor_models_output(raw: str) -> list[JsonObject]:
    return _legacy_models(harness_discovery.parse_cursor_catalog(strip_ansi(raw)))


def parse_droid_custom_models(custom_models: list[object]) -> list[JsonObject]:
    return _legacy_models({"models": harness_discovery.parse_droid_settings_models(custom_models)})


def _probe_devin_models(config: JsonObject, *, env: dict[str, str]) -> list[JsonObject]:
    binary = delegate_config.harness_binary(config, "devin")
    # The invalid sentinel makes devin fail fast pre-session; running from a
    # throwaway cwd additionally guarantees the probe can never touch the
    # caller's workspace even if that behavior changes.
    with tempfile.TemporaryDirectory(prefix="delegate-model-probe-") as probe_cwd:
        completed = subprocess.run(
            [binary, "--model", DEVIN_LIVE_SENTINEL, "-p", "--", "probe"],
            capture_output=True,
            text=True,
            timeout=LIVE_PROBE_TIMEOUT_SEC,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=probe_cwd,
            env=env,
        )
    combined = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    return parse_devin_available_models(combined)


def parse_devin_available_models(raw: str) -> list[JsonObject]:
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("available:"):
            continue
        _, _, rest = stripped.partition(":")
        ids = [part.strip() for part in rest.split(",") if part.strip()]
        if not ids:
            raise RuntimeError("devin Available line was empty")
        return [{"id": model_id} for model_id in ids]
    raise RuntimeError("devin output had no Available: line")


def parse_opencode_models_output(raw: str) -> list[JsonObject]:
    try:
        fragment = harness_discovery.parse_opencode_catalog(strip_ansi(raw))
    except ValueError:
        fragment = None
    if isinstance(fragment, dict):
        return [{"id": item["id"]} for item in _legacy_models(fragment)]
    models: list[JsonObject] = []
    for line in strip_ansi(raw).splitlines():
        model_id = line.strip()
        # Verified shape: provider/model with no whitespace (e.g. openai/gpt-5).
        # Reject single-token junk like "warning:" or "loading".
        if not model_id or any(char.isspace() for char in model_id) or "/" not in model_id:
            continue
        models.append({"id": model_id})
    if not models:
        raise RuntimeError("opencode models output had no parseable model lines")
    return models


def parse_pi_models_output(raw: str) -> list[JsonObject]:
    try:
        fragment = harness_discovery.parse_pi_catalog(strip_ansi(raw))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return [{"id": item["id"]} for item in _legacy_models(fragment)]


def parse_omp_models_output(raw: str) -> list[JsonObject]:
    try:
        fragment = harness_discovery.parse_omp_catalog(raw)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return [{"id": item["id"]} for item in _legacy_models(fragment)]


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
