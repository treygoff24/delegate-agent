"""Per-engine advisory model discovery: bundled tables, config aliases, optional live probes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent.bundled_models import BUNDLED_MODELS
from delegate_agent.constants import ENGINES_PROSE, KNOWN_ENGINES
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject, JsonValue

ENGINE_MODELS_SCHEMA = "delegate.engine-models.v1"
LIVE_PROBE_TIMEOUT_SEC = 10
DEVIN_LIVE_SENTINEL = "delegate-live-probe-sentinel"
LIVE_UNSUPPORTED_ENGINES = frozenset({"codex", "claude", "grok", "kimi"})
_SOURCE_RANK = {"bundled": 0, "live": 1, "config": 2}
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

    if live:
        if engine in LIVE_UNSUPPORTED_ENGINES:
            live_field = {
                "supported": False,
                "reason": f"{engine} has no non-interactive model enumeration",
            }
        else:
            live_models, probe_warning = _probe_live_models(
                config,
                engine,
                factory_settings_path=factory_settings_path,
            )
            if probe_warning:
                warning = probe_warning
                live_field = False
            else:
                live_field = True
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
) -> tuple[list[JsonObject], str | None]:
    try:
        if engine == "cursor":
            return _probe_cursor_models(config), None
        if engine == "droid":
            return _probe_droid_models(factory_settings_path=factory_settings_path), None
        if engine == "devin":
            return _probe_devin_models(config), None
        if engine == "opencode":
            return _probe_opencode_models(config), None
    except Exception as exc:
        return [], f"live probe failed: {exc}"
    return [], f"live probe unsupported for {engine}"


def _probe_cursor_models(config: JsonObject) -> list[JsonObject]:
    cursor = config.get("cursor")
    if not isinstance(cursor, dict):
        raise RuntimeError("cursor config missing")
    prefix = cursor.get("argvPrefix")
    if not isinstance(prefix, list) or not prefix or not all(isinstance(p, str) for p in prefix):
        raise RuntimeError("cursor.argvPrefix must be a non-empty string array")
    argv = [*prefix, "models"]
    # Throwaway cwd: a wrapper or the harness itself may write relative files
    # (caches, logs) during a listing; never let that land in the caller's tree.
    with tempfile.TemporaryDirectory(prefix="delegate-model-probe-") as probe_cwd:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=LIVE_PROBE_TIMEOUT_SEC,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=probe_cwd,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cursor models exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[:200]}"
        )
    return parse_cursor_models_output(completed.stdout or "")


def parse_cursor_models_output(raw: str) -> list[JsonObject]:
    text = strip_ansi(raw)
    lines = text.splitlines()
    collecting = False
    models: list[JsonObject] = []
    for line in lines:
        stripped = line.strip()
        if not collecting:
            if stripped.lower().startswith("available models"):
                collecting = True
            continue
        if not stripped:
            continue
        if " - " not in stripped:
            continue
        model_id, _, label = stripped.partition(" - ")
        model_id = model_id.strip()
        label = label.strip()
        if not model_id:
            continue
        entry: JsonObject = {"id": model_id}
        if label:
            entry["note"] = label
        models.append(entry)
    if not models:
        raise RuntimeError("cursor models output had no parseable model lines")
    return models


def _probe_droid_models(*, factory_settings_path: Path | None = None) -> list[JsonObject]:
    path = factory_settings_path
    if path is None:
        home = Path(os.path.expanduser("~"))
        path = home / ".factory" / "settings.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} root must be an object")
    custom = data.get("customModels")
    if custom is None:
        return []
    if not isinstance(custom, list):
        raise RuntimeError("customModels must be a list")
    return parse_droid_custom_models(custom)


def parse_droid_custom_models(custom_models: list[object]) -> list[JsonObject]:
    models: list[JsonObject] = []
    for item in custom_models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id:
            entry: JsonObject = {"id": model_id}
        else:
            display = item.get("displayName")
            if not isinstance(display, str) or not display.strip():
                continue
            derived = "custom:" + "-".join(display.split())
            entry = {"id": derived}
            entry["note"] = display
        note = item.get("displayName")
        if isinstance(note, str) and note and "note" not in entry:
            entry["note"] = note
        models.append(entry)
    return models


def _probe_devin_models(config: JsonObject) -> list[JsonObject]:
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


def _probe_opencode_models(config: JsonObject) -> list[JsonObject]:
    binary = delegate_config.harness_binary(config, "opencode")
    with tempfile.TemporaryDirectory(prefix="delegate-model-probe-") as probe_cwd:
        completed = subprocess.run(
            [binary, "models"],
            capture_output=True,
            text=True,
            timeout=LIVE_PROBE_TIMEOUT_SEC,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=probe_cwd,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"opencode models exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[:200]}"
        )
    return parse_opencode_models_output(completed.stdout or "")


def parse_opencode_models_output(raw: str) -> list[JsonObject]:
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
