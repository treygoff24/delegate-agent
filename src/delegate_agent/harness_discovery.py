"""Normalized harness metadata discovery primitives.

This module owns the private discovery snapshot contract and the bounded
subprocess boundary used by harness adapters.  Catalog parsing and cache I/O
are deliberately layered on later.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from delegate_agent import config as delegate_config
from delegate_agent import private_io, profiles, redaction
from delegate_agent.constants import KNOWN_ENGINES
from delegate_agent.json_types import JsonObject

DISCOVERY_SCHEMA = 1
PROBE_STATUSES = frozenset({"ok", "partial", "missing", "error"})
MODEL_SCOPES = frozenset({"account", "configured", "catalog", "unknown"})
EVIDENCE_LEVELS = frozenset({"exact", "harness", "inferred-route", "unknown"})
# Recorded as a harness warning when an explicitly configured selector no longer
# resolves while the harness binary is still on PATH, so callers can tell a
# stale config apart from an uninstalled harness.
CONFIGURED_SELECTOR_MISSING = "configured_selector_missing"

_PATH_CANDIDATES: dict[str, tuple[str, ...]] = {
    "cursor": ("agent", "cursor-agent"),
    **{engine: (engine,) for engine in KNOWN_ENGINES if engine != "cursor"},
}
_CANONICAL_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("grok", re.compile(r"^grok\s+[0-9][0-9A-Za-z.+-]*", re.IGNORECASE | re.MULTILINE)),
    ("codex", re.compile(r"^codex-cli\s+[0-9][0-9A-Za-z.+-]*", re.IGNORECASE | re.MULTILINE)),
    (
        "claude",
        re.compile(
            r"^[0-9][0-9A-Za-z.+-]*\s+\(Claude Code\)$",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    ("devin", re.compile(r"^devin\s+[0-9][0-9A-Za-z.+-]*", re.IGNORECASE | re.MULTILINE)),
    ("omp", re.compile(r"^omp/[0-9][0-9A-Za-z.+-]*", re.IGNORECASE | re.MULTILINE)),
    (
        "cursor",
        re.compile(r"^\d{4}\.\d{2}\.\d{2}-[0-9a-f]+$", re.IGNORECASE | re.MULTILINE),
    ),
)
_GENERIC_VERSION = re.compile(r"^\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?$")
_AMBIGUOUS_VERSION_BASENAMES = frozenset({"agent"})
_DIAGNOSTIC_LIMIT = 8_000
METADATA_PROBE_TIMEOUT_SEC = 15
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CURSOR_EFFORT_LABELS = {
    "none": "None",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
}
_SNAPSHOT_FIELDS = frozenset({"schema", "profile", "capturedAt", "harnesses"})
_HARNESS_FIELDS = frozenset(
    {
        "installed",
        "selector",
        "version",
        "probeStatus",
        "modelScope",
        "defaultModel",
        "models",
        "harnessReasoning",
        "warnings",
    }
)
_MODEL_FIELDS = frozenset({"displayName", "reasoning", "routeFamily", "routeEffort"})
_REASONING_FIELDS = frozenset({"supported", "default", "evidence"})


def empty_snapshot(*, profile: str = "default", captured_at: str | None = None) -> JsonObject:
    normalized_profile = profile.strip() if isinstance(profile, str) else profile
    return {
        "schema": DISCOVERY_SCHEMA,
        "profile": normalized_profile,
        "capturedAt": captured_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "harnesses": {},
    }


def validate_snapshot(
    snapshot: JsonObject,
    *,
    expected_profile: str | None = None,
    allow_unknown_harnesses: bool = False,
) -> tuple[str, ...]:
    """Validate a normalized snapshot and return forward-compat warnings."""
    _reject_extra_fields(snapshot, _SNAPSHOT_FIELDS, "discovery snapshot")
    schema = snapshot.get("schema")
    if isinstance(schema, bool) or schema != DISCOVERY_SCHEMA:
        raise ValueError(f"discovery snapshot schema must be {DISCOVERY_SCHEMA}")
    profile = snapshot.get("profile")
    if not _nonempty_string(profile) or profile != profile.strip():
        raise ValueError("discovery snapshot profile must be a normalized non-empty string")
    if expected_profile is not None and profile != expected_profile.strip():
        raise ValueError("discovery snapshot profile does not match the requested profile")
    captured_at = snapshot.get("capturedAt")
    if not _nonempty_string(captured_at):
        raise ValueError("discovery snapshot capturedAt must be a non-empty string")
    try:
        datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("discovery snapshot capturedAt must be ISO-8601") from exc
    harnesses = snapshot.get("harnesses")
    if not isinstance(harnesses, dict):
        raise ValueError("discovery snapshot harnesses must be an object")

    warnings: list[str] = []
    for harness, record in harnesses.items():
        if not _nonempty_string(harness) or not isinstance(record, dict):
            raise ValueError("discovery snapshot harness entries must be named objects")
        if harness not in KNOWN_ENGINES:
            if not allow_unknown_harnesses:
                raise ValueError(f"unsupported discovery harness: {harness}")
            warnings.append(f"ignored unknown discovery harness {harness!r}")
        _validate_harness_record(harness, record)
    return tuple(warnings)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _string_list(value: object, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(_nonempty_string(item) for item in value)
    )


def _reject_extra_fields(value: JsonObject, allowed: frozenset[str], path: str) -> None:
    extras = [key for key in value if key not in allowed]
    if extras:
        raise ValueError(f"{path} contains unsupported field {extras[0]!r}")


def _validate_harness_record(harness: str, record: JsonObject) -> None:
    _reject_extra_fields(record, _HARNESS_FIELDS, f"discovery harness {harness}")
    if not isinstance(record.get("installed"), bool):
        raise ValueError(f"discovery harness {harness}.installed must be boolean")
    selector = record.get("selector")
    if not _string_list(selector):
        raise ValueError(f"discovery harness {harness}.selector must be a string array")
    version = record.get("version")
    if version is not None and not _nonempty_string(version):
        raise ValueError(f"discovery harness {harness}.version must be a string or null")
    if record.get("probeStatus") not in PROBE_STATUSES:
        raise ValueError(f"discovery harness {harness}.probeStatus is invalid")
    if record.get("modelScope") not in MODEL_SCOPES:
        raise ValueError(f"discovery harness {harness}.modelScope is invalid")
    default_model = record.get("defaultModel")
    if default_model is not None and not _nonempty_string(default_model):
        raise ValueError(f"discovery harness {harness}.defaultModel must be a string or null")
    models = record.get("models")
    if not isinstance(models, dict):
        raise ValueError(f"discovery harness {harness}.models must be an object")
    route_pairs: set[tuple[str, str]] = set()
    for selector_name, model in models.items():
        if not _nonempty_string(selector_name) or not isinstance(model, dict):
            raise ValueError(f"discovery harness {harness}.models entries are malformed")
        route_pair = _validate_model_record(harness, selector_name, model)
        if route_pair is not None:
            if route_pair in route_pairs:
                raise ValueError(f"discovery harness {harness} has duplicate Cursor routes")
            route_pairs.add(route_pair)
    harness_reasoning = record.get("harnessReasoning")
    if harness_reasoning is not None:
        if not isinstance(harness_reasoning, dict):
            raise ValueError(
                f"discovery harness {harness}.harnessReasoning must be an object or null"
            )
        _validate_reasoning(harness, "harnessReasoning", harness_reasoning)
    if not _string_list(record.get("warnings")):
        raise ValueError(f"discovery harness {harness}.warnings must be a string array")


def _validate_model_record(
    harness: str, selector: str, model: JsonObject
) -> tuple[str, str] | None:
    if selector.startswith("-"):
        raise ValueError(f"discovery model {harness}/{selector} selector is unsafe")
    _reject_extra_fields(model, _MODEL_FIELDS, f"discovery model {harness}/{selector}")
    display_name = model.get("displayName")
    if display_name is not None and not _nonempty_string(display_name):
        raise ValueError(f"discovery model {harness}/{selector}.displayName is invalid")
    route_family = model.get("routeFamily")
    route_effort = model.get("routeEffort")
    route_values = (route_family, route_effort)
    has_route = any(value is not None for value in route_values)
    if has_route and (
        harness != "cursor" or not all(_nonempty_string(value) for value in route_values)
    ):
        raise ValueError(
            f"discovery model {harness}/{selector} requires a complete Cursor route pair"
        )
    if route_effort is not None and not _transport_safe_effort(route_effort):
        raise ValueError(f"discovery model {harness}/{selector}.routeEffort is unsafe")
    reasoning = model.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, dict):
            raise ValueError(f"discovery model {harness}/{selector}.reasoning is invalid")
        _validate_reasoning(harness, selector, reasoning)
    if has_route:
        if not isinstance(reasoning, dict) or reasoning.get("evidence") != "inferred-route":
            raise ValueError(
                f"discovery model {harness}/{selector} route requires inferred-route evidence"
            )
        supported = reasoning.get("supported")
        if not isinstance(supported, list) or route_effort not in supported:
            raise ValueError(f"discovery model {harness}/{selector} route effort must be supported")
        assert isinstance(route_family, str) and isinstance(route_effort, str)
        return route_family, route_effort
    if isinstance(reasoning, dict) and reasoning.get("evidence") == "inferred-route":
        raise ValueError(
            f"discovery model {harness}/{selector} inferred route requires a route pair"
        )
    return None


def _validate_reasoning(harness: str, selector: str, reasoning: JsonObject) -> None:
    _reject_extra_fields(reasoning, _REASONING_FIELDS, f"discovery reasoning {harness}/{selector}")
    supported = reasoning.get("supported")
    if supported is not None and (
        not isinstance(supported, list)
        or not all(_transport_safe_effort(item) for item in supported)
    ):
        raise ValueError(f"discovery reasoning {harness}/{selector}.supported is invalid")
    if isinstance(supported, list) and len(set(supported)) != len(supported):
        raise ValueError(f"discovery reasoning {harness}/{selector}.supported has duplicates")
    default = reasoning.get("default")
    if default is not None and (
        not _nonempty_string(default) or not isinstance(supported, list) or default not in supported
    ):
        raise ValueError(f"discovery reasoning {harness}/{selector}.default is unsupported")
    if reasoning.get("evidence") not in EVIDENCE_LEVELS:
        raise ValueError(f"discovery reasoning {harness}/{selector}.evidence is invalid")
    if supported == [] and reasoning.get("evidence") != "exact":
        raise ValueError(
            f"discovery reasoning {harness}/{selector} empty support requires exact evidence"
        )


def _transport_safe_effort(value: object) -> bool:
    # Keep this identical to reasoning.is_valid_effort_string without importing
    # that higher-level module and creating a future discovery/reasoning cycle.
    return (
        _nonempty_string(value)
        and not value.startswith("-")
        and not any(character.isspace() for character in value)
        and '"' not in value
        and "\\" not in value
    )


@dataclass(frozen=True)
class ProbeResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None


@dataclass(frozen=True)
class SelectorResolution:
    selector: tuple[str, ...] | None
    version: str | None
    error: str | None
    warnings: tuple[str, ...] = ()


def run_metadata_probe(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout_sec: int = 15,
    output_limit_bytes: int = 8 * 1024 * 1024,
) -> ProbeResult:
    if not argv or not all(_nonempty_string(item) for item in argv):
        raise ValueError("metadata probe argv must be a non-empty string array")
    if timeout_sec <= 0 or output_limit_bytes <= 0:
        raise ValueError("metadata probe limits must be positive")
    result_argv = tuple(argv)
    with (
        tempfile.TemporaryDirectory(prefix="delegate-discovery-") as cwd,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            completed = subprocess.run(  # nosec B603 - validated argv, always shell=False.
                argv,
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                check=False,
                timeout=timeout_sec,
            )
            returncode = completed.returncode
            error = None if returncode == 0 else "probe_failed"
        except FileNotFoundError:
            return ProbeResult(result_argv, None, "", "", "probe_missing")
        except subprocess.TimeoutExpired:
            returncode = None
            error = "probe_timeout"
        except OSError as exc:
            diagnostic = _scrub_diagnostic(str(exc))
            return ProbeResult(result_argv, None, "", diagnostic, "probe_launch_failed")

        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        stdout = _read_probe_output(stdout_file, output_limit_bytes)
        stderr = _read_probe_output(stderr_file, output_limit_bytes)
        if stdout_size > output_limit_bytes or stderr_size > output_limit_bytes:
            error = "probe_output_too_large"
        if error is not None:
            stdout = _scrub_diagnostic(stdout)
            stderr = _scrub_diagnostic(stderr)
        return ProbeResult(result_argv, returncode, stdout, stderr, error)


def _read_probe_output(handle: BinaryIO, limit: int) -> str:
    handle.seek(0)
    return handle.read(limit).decode("utf-8", errors="replace")


def _scrub_diagnostic(value: str) -> str:
    return redaction.redact_progress_label(value)[:_DIAGNOSTIC_LIMIT]


def selector_candidates(
    config: JsonObject, harness: str
) -> tuple[tuple[tuple[str, ...], bool], ...]:
    if harness not in KNOWN_ENGINES:
        raise ValueError(f"unknown harness: {harness}")
    embedded = delegate_config.embedded_default_config()
    section = config.get(harness)
    embedded_section = embedded.get(harness)
    configured: tuple[str, ...] | None = None
    embedded_selector: tuple[str, ...] | None = None
    if harness == "cursor":
        if isinstance(section, dict) and _string_list(section.get("argvPrefix"), allow_empty=False):
            configured = tuple(section["argvPrefix"])
        if isinstance(embedded_section, dict) and _string_list(
            embedded_section.get("argvPrefix"), allow_empty=False
        ):
            embedded_selector = tuple(embedded_section["argvPrefix"])
    else:
        configured = (delegate_config.harness_binary(config, harness),)
        if isinstance(embedded_section, dict) and _nonempty_string(embedded_section.get("binary")):
            embedded_selector = (embedded_section["binary"],)

    candidates: list[tuple[tuple[str, ...], bool]] = []
    if configured is not None:
        candidates.append((configured, configured != embedded_selector))
    for candidate in _PATH_CANDIDATES[harness]:
        item = ((candidate,), False)
        if item[0] not in [selector for selector, _explicit in candidates]:
            candidates.append(item)
    return tuple(candidates)


def resolve_harness_selector(
    config: JsonObject,
    harness: str,
    *,
    env: Mapping[str, str],
) -> SelectorResolution:
    warnings: list[str] = []
    for selector, explicit in selector_candidates(config, harness):
        normalized = _normalize_selector(selector, env=env)
        if normalized is None:
            if explicit:
                # A configured selector that no longer resolves is a stale
                # config, not an uninstalled harness, whenever the harness
                # binary is still reachable on PATH. Never silently fall back
                # to it: report the distinction so setup can name the fix.
                error = (
                    CONFIGURED_SELECTOR_MISSING
                    if _path_candidate_resolves(config, harness, env=env)
                    else "probe_missing"
                )
                return SelectorResolution(None, None, error, tuple(warnings))
            continue
        selector = normalized
        probe = run_metadata_probe([*selector, "--version"], env=env)
        if probe.error is not None:
            if explicit:
                return SelectorResolution(None, None, probe.error, tuple(warnings))
            continue
        output = "\n".join(part for part in (probe.stdout, probe.stderr) if part)
        version = _canonical_version(harness, selector, output)
        if version is None:
            warning = f"selector {' '.join(selector)!r} did not identify as {harness}"
            if explicit:
                return SelectorResolution(None, None, "fingerprint_mismatch", (warning,))
            warnings.append(warning)
            continue
        return SelectorResolution(selector, version, None, tuple(warnings))
    return SelectorResolution(None, None, "probe_missing", tuple(warnings))


def _path_candidate_resolves(config: JsonObject, harness: str, *, env: Mapping[str, str]) -> bool:
    return any(
        not explicit and _normalize_selector(selector, env=env) is not None
        for selector, explicit in selector_candidates(config, harness)
    )


def _normalize_selector(
    selector: tuple[str, ...], *, env: Mapping[str, str]
) -> tuple[str, ...] | None:
    binary = shutil.which(selector[0], path=env.get("PATH", ""))
    if binary is None:
        return None
    return (str(Path(binary).absolute()), *selector[1:])


def _canonical_version(harness: str, selector: tuple[str, ...], output: str) -> str | None:
    for identified_harness, pattern in _CANONICAL_VERSION_PATTERNS:
        match = pattern.search(output)
        if match is not None:
            return match.group(0) if identified_harness == harness else None
    binary_name = Path(selector[0]).name
    if binary_name not in _PATH_CANDIDATES[harness] or binary_name in _AMBIGUOUS_VERSION_BASENAMES:
        return None
    for line in output.splitlines():
        candidate = line.strip()
        if _GENERIC_VERSION.fullmatch(candidate):
            return candidate
    return None


def _fragment(
    *,
    model_scope: str,
    models: JsonObject,
    default_model: str | None = None,
    harness_reasoning: JsonObject | None = None,
    probe_status: str = "ok",
    warnings: list[str] | None = None,
) -> JsonObject:
    return {
        "probeStatus": probe_status,
        "modelScope": model_scope,
        "defaultModel": default_model,
        "models": models,
        "harnessReasoning": harness_reasoning,
        "warnings": warnings or [],
    }


def parse_codex_catalog(raw: str) -> JsonObject:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("codex catalog was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("codex catalog root must be an object")

    # Import locally so reasoning can later consume discovery projections
    # without creating a module-import cycle.
    from delegate_agent.reasoning import (
        ReasoningCapabilityError,
        parse_codex_models_payload,
    )

    try:
        parsed = parse_codex_models_payload(payload)
    except ReasoningCapabilityError as exc:
        raise ValueError(exc.message) from exc
    parsed_harnesses = parsed.get("harnesses")
    parsed_codex = parsed_harnesses.get("codex") if isinstance(parsed_harnesses, dict) else None
    declarations = parsed_codex.get("models") if isinstance(parsed_codex, dict) else None
    if not isinstance(declarations, dict):
        raise ValueError("codex reasoning projection was malformed")

    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ValueError("codex catalog must contain a models array")
    models: JsonObject = {}
    for item in raw_models:
        if not isinstance(item, dict):
            raise ValueError("codex model entries must be objects")
        selector = item.get("slug")
        if not isinstance(selector, str):
            raise ValueError("codex model entries require a selector")
        declaration = declarations.get(selector)
        if not isinstance(declaration, dict):
            raise ValueError(f"codex reasoning projection omitted {selector!r}")
        supported = declaration.get("supported")
        if not isinstance(supported, list):
            raise ValueError(f"codex reasoning projection malformed {selector!r}")
        reasoning: JsonObject = {
            "supported": list(supported),
            "evidence": "exact",
        }
        model: JsonObject = {
            "reasoning": reasoning,
        }
        display_name = item.get("display_name")
        if _nonempty_string(display_name):
            model["displayName"] = display_name
        default = declaration.get("default")
        if isinstance(default, str):
            reasoning["default"] = default
        models[selector] = model
    return _fragment(model_scope="account", models=models)


def parse_omp_catalog(raw: str) -> JsonObject:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("omp catalog was not valid JSON") from exc
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("omp catalog must contain a models array")
    models: JsonObject = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        selector = entry.get("selector")
        if not _nonempty_string(selector):
            continue
        model: JsonObject = {}
        name = entry.get("name")
        if _nonempty_string(name):
            model["displayName"] = name
        thinking = entry.get("thinking")
        if (
            isinstance(thinking, list)
            and thinking
            and all(_transport_safe_effort(item) for item in thinking)
        ):
            model["reasoning"] = {
                "supported": list(thinking),
                "evidence": "exact",
            }
        elif entry.get("reasoning") is False:
            model["reasoning"] = {"supported": [], "evidence": "exact"}
        elif entry.get("reasoning") is True:
            model["reasoning"] = {
                "supported": None,
                "evidence": "unknown",
            }
        models[selector] = model
    if not models:
        raise ValueError("omp catalog had no parseable models")
    return _fragment(model_scope="catalog", models=models)


def _opencode_object_end(raw: str, start: int) -> int | None:
    """Return the index just past the brace-balanced object opened at ``start``.

    String-aware so a malformed entry can be skipped whole; resuming the scan
    inside a rejected body lets a nested ``provider/model``-shaped line be
    re-read as a top-level selector. Returns None when the braces never
    balance, which leaves the caller no delimiter to trust.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        character = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _next_opencode_selector(raw: str, start: int) -> re.Match[str] | None:
    return re.compile(r"(?m)^([^\s/]+/[^\s/]+)\r?\n(?=\s*\{)").search(raw, start)


def parse_opencode_catalog(raw: str) -> JsonObject:
    decoder = json.JSONDecoder()
    models: JsonObject = {}
    warnings: list[str] = []
    position = 0
    while match := _next_opencode_selector(raw, position):
        selector = match.group(1)
        object_start = match.end()
        while object_start < len(raw) and raw[object_start].isspace():
            object_start += 1
        try:
            entry, end = decoder.raw_decode(raw, object_start)
        except json.JSONDecodeError:
            warnings.append(f"ignored malformed OpenCode entry for {selector!r}")
            # Skip the whole rejected object when its braces balance; only an
            # unbalanced (truncated) body falls back to the next selector line.
            position = _opencode_object_end(raw, object_start) or match.end()
            continue
        position = end
        if not isinstance(entry, dict):
            warnings.append(f"ignored non-object OpenCode entry for {selector!r}")
            continue
        provider = entry.get("providerID")
        model_id = entry.get("id")
        if not (_nonempty_string(provider) and _nonempty_string(model_id)) or (
            f"{provider}/{model_id}" != selector
        ):
            warnings.append(f"ignored mismatched OpenCode entry for {selector!r}")
            continue
        model: JsonObject = {}
        name = entry.get("name")
        if _nonempty_string(name):
            model["displayName"] = name
        variants = entry.get("variants")
        supported = (
            [key for key in variants if _transport_safe_effort(key)]
            if isinstance(variants, dict)
            else []
        )
        if supported:
            model["reasoning"] = {"supported": supported, "evidence": "exact"}
        else:
            capabilities = entry.get("capabilities")
            reasoning_flag = (
                capabilities.get("reasoning") if isinstance(capabilities, dict) else None
            )
            if reasoning_flag is False:
                model["reasoning"] = {"supported": [], "evidence": "exact"}
            elif reasoning_flag is True:
                model["reasoning"] = {"supported": None, "evidence": "unknown"}
        models[selector] = model
    if not models:
        raise ValueError("OpenCode catalog had no parseable selector/object pairs")
    return _fragment(
        model_scope="catalog",
        models=models,
        probe_status="partial" if warnings else "ok",
        warnings=warnings,
    )


def parse_kimi_catalog(raw: str) -> JsonObject:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Kimi catalog was not valid JSON") from exc
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        raise ValueError("Kimi catalog must contain a top-level models object")
    models: JsonObject = {}
    warnings: list[str] = []
    for selector, entry in entries.items():
        if not (_nonempty_string(selector) and isinstance(entry, dict)):
            continue
        model: JsonObject = {}
        display_name = entry.get("displayName")
        if _nonempty_string(display_name):
            model["displayName"] = display_name
        supported = entry.get("supportEfforts")
        if (
            isinstance(supported, list)
            and supported
            and all(_transport_safe_effort(item) for item in supported)
        ):
            reasoning: JsonObject = {
                "supported": list(supported),
                "evidence": "exact",
            }
            default = entry.get("defaultEffort")
            if isinstance(default, str) and default in supported:
                reasoning["default"] = default
            elif default is not None:
                warnings.append(f"Kimi model {selector!r} advertised an unsupported default effort")
            model["reasoning"] = reasoning
        models[selector] = model
    if not models:
        raise ValueError("Kimi catalog had no parseable models")
    return _fragment(
        model_scope="configured",
        models=models,
        probe_status="partial" if warnings else "ok",
        warnings=warnings,
    )


def parse_cursor_catalog(raw: str) -> JsonObject:
    raw = _ANSI_RE.sub("", raw)
    entries: list[tuple[str, str]] = []
    collecting = False
    default_model: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not collecting:
            collecting = stripped.lower().startswith("available models")
            continue
        if entries and (not stripped or " - " not in stripped):
            break
        if not stripped or " - " not in stripped:
            continue
        selector, _, label = stripped.partition(" - ")
        selector = selector.strip()
        label = label.strip()
        if selector.startswith("-"):
            break
        if not selector:
            continue
        if label.lower().endswith("(default)"):
            default_model = selector
            label = label[: -len("(default)")].strip()
        entries.append((selector, label))
    if not entries:
        raise ValueError("Cursor catalog had no parseable model lines")

    route_candidates: dict[tuple[str, bool], dict[str, tuple[str, bool, str]]] = {}
    for selector, label in entries:
        parsed = _cursor_route_candidate(selector, label)
        if parsed is None:
            continue
        family, fast, effort, direct = parsed
        route_candidates.setdefault((family, fast), {})[effort] = (selector, direct, label)

    accepted: dict[str, tuple[str, str]] = {}
    for (family, fast), efforts in route_candidates.items():
        sibling_high = _cursor_high_corroborated(efforts, fast=fast)
        for effort, (selector, direct, _label) in efforts.items():
            if direct or (effort == "high" and sibling_high):
                route_family = f"{family}-fast" if fast else family
                accepted[selector] = (route_family, effort)

    # "<family>-fast-<effort>" and "<family>-<effort>-fast" name the same route,
    # so a catalog carrying both would emit an ambiguous pair. Drop every
    # colliding selector's route rather than failing the whole harness record.
    warnings: list[str] = []
    owners: dict[tuple[str, str], list[str]] = {}
    for selector, route in accepted.items():
        owners.setdefault(route, []).append(selector)
    for route, selectors in sorted(owners.items()):
        if len(selectors) < 2:
            continue
        for selector in selectors:
            del accepted[selector]
        warnings.append(
            f"ignored ambiguous Cursor route {route[0]}/{route[1]} claimed by "
            f"{', '.join(sorted(selectors))}"
        )

    models: JsonObject = {}
    for selector, label in entries:
        model: JsonObject = {}
        if label:
            model["displayName"] = label
        route = accepted.get(selector)
        if route is not None:
            family, effort = route
            model.update(
                {
                    "routeFamily": family,
                    "routeEffort": effort,
                    "reasoning": {
                        "supported": [effort],
                        "default": effort,
                        "evidence": "inferred-route",
                    },
                }
            )
        models[selector] = model
    return _fragment(
        model_scope="account",
        models=models,
        default_model=default_model,
        probe_status="partial" if warnings else "ok",
        warnings=warnings,
    )


def _cursor_route_candidate(selector: str, label: str) -> tuple[str, bool, str, bool] | None:
    match = re.fullmatch(
        r"(?P<family>.+)-(?P<effort>none|low|medium|high|xhigh|max)(?P<fast>-fast)?",
        selector,
    )
    if match is None:
        return None
    effort = match.group("effort")
    expected_label = _CURSOR_EFFORT_LABELS[effort]
    direct = re.search(rf"\b{re.escape(expected_label)}\b", label, re.IGNORECASE) is not None
    return match.group("family"), match.group("fast") is not None, effort, direct


def _cursor_high_corroborated(efforts: dict[str, tuple[str, bool, str]], *, fast: bool) -> bool:
    low = efforts.get("low")
    medium = efforts.get("medium")
    high = efforts.get("high")
    if low is None or medium is None or high is None or not (low[1] and medium[1]):
        return False
    low_base = _cursor_direct_label_base(low[2], "low", fast=fast)
    medium_base = _cursor_direct_label_base(medium[2], "medium", fast=fast)
    if low_base is None or low_base.casefold() != (medium_base or "").casefold():
        return False
    expected_high = f"{low_base}{' Fast' if fast else ''}"
    return high[2].casefold() == expected_high.casefold()


def _cursor_direct_label_base(label: str, effort: str, *, fast: bool) -> str | None:
    suffix = f" {_CURSOR_EFFORT_LABELS[effort]}{' Fast' if fast else ''}"
    return label[: -len(suffix)] if label.casefold().endswith(suffix.casefold()) else None


_DROID_ENTRY = re.compile(r"^\s{2,}(\S+)\s{2,}(.+?)\s*$")
_DROID_DETAIL = re.compile(
    r"^\s*-\s*(.+?):\s*supports reasoning:\s*(Yes|No);\s*"
    r"supported:\s*\[([^]]*)\];\s*default:\s*(\S+)\s*$",
    re.IGNORECASE,
)


def parse_droid_help(raw: str) -> JsonObject:
    section: str | None = None
    entries: dict[str, tuple[str, str]] = {}
    details: dict[str, tuple[bool, list[str], str]] = {}
    default_model: str | None = None
    headings: set[str] = set()
    for line in raw.splitlines():
        heading = line.strip().rstrip(":").lower()
        if heading in {"available models", "custom models", "model details"}:
            section = heading
            headings.add(heading)
            continue
        if line and not line[0].isspace():
            section = None
            continue
        if section in {"available models", "custom models"}:
            match = _DROID_ENTRY.match(line)
            if match is None:
                continue
            selector, display = match.groups()
            source = "built-in" if section == "available models" else "custom"
            if display.lower().endswith("(default)"):
                default_model = selector
                display = display[: -len("(default)")].strip()
            existing = entries.get(selector)
            if existing is None or existing[1] != "built-in":
                entries[selector] = (display, source)
        elif section == "model details":
            match = _DROID_DETAIL.match(line)
            if match is None:
                continue
            display, supports, supported_raw, default = match.groups()
            supported = [item.strip() for item in supported_raw.split(",") if item.strip()]
            if supported and all(_transport_safe_effort(item) for item in supported):
                details[display] = (supports.lower() == "yes", supported, default)
    if "available models" not in headings or not entries:
        raise ValueError("Droid help had no Available Models section")

    warnings: list[str] = []
    models: JsonObject = {}
    display_counts: dict[str, int] = {}
    for display, _source in entries.values():
        display_counts[display] = display_counts.get(display, 0) + 1
    for selector, (display, source) in entries.items():
        model: JsonObject = {"displayName": display}
        detail = details.get(display) if display_counts[display] == 1 else None
        if detail is not None:
            supports_reasoning, supported, default = detail
            if not supports_reasoning:
                supported = []
            reasoning: JsonObject = {"supported": supported, "evidence": "exact"}
            if default in supported:
                reasoning["default"] = default
            model["reasoning"] = reasoning
        elif source == "built-in":
            warning = (
                f"Droid help had an ambiguous display label for {selector!r}"
                if display_counts[display] > 1
                else f"Droid help omitted reasoning details for {selector!r}"
            )
            warnings.append(warning)
        models[selector] = model
    if "model details" not in headings:
        warnings.append("Droid help omitted the Model details section")
    return _fragment(
        model_scope="account",
        models=models,
        default_model=default_model,
        probe_status="partial" if warnings else "ok",
        warnings=warnings,
    )


def parse_droid_settings_models(custom_models: object) -> JsonObject:
    models: JsonObject = {}
    if not isinstance(custom_models, list):
        return models
    for item in custom_models:
        if not isinstance(item, dict):
            continue
        selector = item.get("id")
        display = item.get("displayName")
        if not _nonempty_string(selector):
            if not _nonempty_string(display):
                continue
            selector = "custom:" + "-".join(display.split())
        model: JsonObject = {}
        if _nonempty_string(display):
            model["displayName"] = display
        models[selector] = model
    return models


def degrade_droid_settings(fragment: JsonObject, warning: str) -> JsonObject:
    """Return ``fragment`` degraded to partial with one Factory settings warning."""
    merged = dict(fragment)
    merged["probeStatus"] = "partial"
    merged["warnings"] = [*fragment.get("warnings", []), warning]
    return merged


def merge_droid_settings(fragment: JsonObject, settings_raw: str | None) -> JsonObject:
    if settings_raw is None:
        return fragment
    merged = dict(fragment)
    warnings = list(fragment.get("warnings", []))
    try:
        settings = json.loads(settings_raw)
    except json.JSONDecodeError:
        return degrade_droid_settings(fragment, "Factory settings JSON was invalid")
    if not isinstance(settings, dict):
        return degrade_droid_settings(fragment, "Factory settings root was not an object")
    custom = settings.get("customModels")
    if custom is not None and not isinstance(custom, list):
        return degrade_droid_settings(fragment, "Factory settings customModels was not an array")
    settings_models = parse_droid_settings_models(custom)
    help_models = fragment.get("models")
    models: JsonObject = dict(settings_models)
    if isinstance(help_models, dict):
        models.update(help_models)
    merged["models"] = models
    merged["warnings"] = warnings
    return merged


def parse_claude_efforts(raw: str) -> JsonObject:
    # Claude Code has no model/effort enumeration command; the only
    # non-interactive source is the "Valid values: ..." warning emitted when
    # --effort rejects our sentinel. A CLI rewording of that warning (or the
    # sentinel ever becoming a valid effort) degrades this harness to an error
    # record rather than producing wrong data — watch probe warnings after
    # Claude Code upgrades.
    match = re.search(r"Valid values:\s*([^\r\n.]+)", raw, re.IGNORECASE)
    if match is None:
        raise ValueError("Claude help had no Valid values list")
    supported = [item.strip() for item in match.group(1).split(",") if item.strip()]
    if not supported or not all(_transport_safe_effort(item) for item in supported):
        raise ValueError("Claude help had an invalid effort list")
    return _fragment(
        model_scope="unknown",
        models={},
        harness_reasoning={"supported": supported, "evidence": "harness"},
        probe_status="partial",
        warnings=["Claude does not expose non-interactive model enumeration"],
    )


def parse_grok_catalog(raw: str) -> JsonObject:
    default_match = re.search(r"^Default model:\s*(\S+)\s*$", raw, re.MULTILINE)
    default_model = default_match.group(1) if default_match else None
    collecting = False
    models: JsonObject = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not collecting:
            collecting = stripped.lower() == "available models:"
            continue
        if models and (not stripped or (line and not line[0].isspace())):
            break
        match = re.match(r"^\*?\s*(\S+?)(?:\s+\(default\))?$", stripped)
        if match:
            selector = match.group(1)
            if selector.startswith("-"):
                break
            models[selector] = {}
    if not models:
        raise ValueError("Grok catalog had no Available models entries")
    warnings: list[str] = []
    status = "ok"
    if default_model is not None and default_model not in models:
        warnings.append("Grok default model was absent from Available models")
        status = "partial"
        default_model = None
    return _fragment(
        model_scope="account",
        models=models,
        default_model=default_model,
        probe_status=status,
        warnings=warnings,
    )


def parse_pi_catalog(raw: str) -> JsonObject:
    lines = raw.splitlines()
    header_index = next(
        (index for index, line in enumerate(lines) if line.split()[:2] == ["provider", "model"]),
        None,
    )
    if header_index is None:
        raise ValueError("Pi catalog had no provider/model header")
    headings = lines[header_index].split()
    try:
        provider_index = headings.index("provider")
        model_index = headings.index("model")
        thinking_index = headings.index("thinking")
    except ValueError as exc:
        raise ValueError("Pi catalog header omitted required columns") from exc
    help_match = re.search(r"Set thinking level:\s*([^\r\n]+)", raw, re.IGNORECASE)
    harness_efforts = (
        [item.strip() for item in help_match.group(1).split(",") if item.strip()]
        if help_match
        else []
    )
    if harness_efforts and not all(_transport_safe_effort(item) for item in harness_efforts):
        harness_efforts = []
    models: JsonObject = {}
    ragged_rows = 0
    for line in lines[header_index + 1 :]:
        columns = line.split()
        if len(columns) <= max(provider_index, model_index, thinking_index):
            continue
        thinking = columns[thinking_index].lower()
        if thinking not in {"yes", "no"}:
            continue
        if len(columns) != len(headings):
            # A blank cell shifts every later column, so a row that does not
            # match the header width cannot be indexed by header position: a
            # neighbouring flag would be read as the thinking flag. Drop the
            # row instead of caching a wrong capability.
            ragged_rows += 1
            continue
        selector = f"{columns[provider_index]}/{columns[model_index]}"
        if thinking == "no":
            reasoning: JsonObject = {"supported": [], "evidence": "exact"}
        elif harness_efforts:
            reasoning = {"supported": harness_efforts, "evidence": "harness"}
        else:
            reasoning = {"supported": None, "evidence": "unknown"}
        models[selector] = {"reasoning": reasoning}
    if not models:
        raise ValueError("Pi catalog had no parseable model rows")
    harness_reasoning = (
        {"supported": harness_efforts, "evidence": "harness"} if harness_efforts else None
    )
    warnings: list[str] = []
    if not harness_efforts:
        warnings.append("Pi help had no thinking-level enum")
    if ragged_rows:
        warnings.append(f"Pi catalog skipped {ragged_rows} row(s) that did not match the header")
    return _fragment(
        model_scope="catalog",
        models=models,
        harness_reasoning=harness_reasoning,
        probe_status="partial" if warnings else "ok",
        warnings=warnings,
    )


def _probe_output(
    selector: tuple[str, ...], arguments: tuple[str, ...], env: Mapping[str, str]
) -> ProbeResult:
    result = run_metadata_probe(
        [*selector, *arguments], env=env, timeout_sec=METADATA_PROBE_TIMEOUT_SEC
    )
    if result.error is not None:
        raise ValueError(result.stderr or result.stdout or result.error)
    return result


def _probe_codex(selector: tuple[str, ...], env: Mapping[str, str], _: Path | None) -> JsonObject:
    return parse_codex_catalog(_probe_output(selector, ("debug", "models"), env).stdout)


def _probe_omp(selector: tuple[str, ...], env: Mapping[str, str], _: Path | None) -> JsonObject:
    return parse_omp_catalog(
        _probe_output(selector, ("models", "--json", "--no-extensions"), env).stdout
    )


def _probe_opencode(
    selector: tuple[str, ...], env: Mapping[str, str], _: Path | None
) -> JsonObject:
    return parse_opencode_catalog(
        _probe_output(selector, ("--pure", "models", "--verbose"), env).stdout
    )


def _probe_kimi(selector: tuple[str, ...], env: Mapping[str, str], _: Path | None) -> JsonObject:
    return parse_kimi_catalog(_probe_output(selector, ("provider", "list", "--json"), env).stdout)


def _probe_cursor(selector: tuple[str, ...], env: Mapping[str, str], _: Path | None) -> JsonObject:
    return parse_cursor_catalog(_probe_output(selector, ("models",), env).stdout)


def _probe_droid(
    selector: tuple[str, ...], env: Mapping[str, str], factory_settings_path: Path | None
) -> JsonObject:
    fragment = parse_droid_help(_probe_output(selector, ("exec", "--help"), env).stdout)
    settings_path = factory_settings_path
    if settings_path is None:
        settings_path = Path(env.get("HOME", str(Path.home()))) / ".factory" / "settings.json"
    try:
        settings_raw = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return merge_droid_settings(fragment, None)
    except OSError:
        # A settings file we cannot read (permissions, I/O) is a different
        # failure from one holding invalid JSON; do not conflate the two.
        return degrade_droid_settings(fragment, "Factory settings could not be read")
    return merge_droid_settings(fragment, settings_raw)


def _probe_claude(selector: tuple[str, ...], env: Mapping[str, str], _: Path | None) -> JsonObject:
    result = run_metadata_probe(
        [*selector, "--effort", "__delegate_probe__", "--help"],
        env=env,
        timeout_sec=METADATA_PROBE_TIMEOUT_SEC,
    )
    if result.error not in {None, "probe_failed"}:
        raise ValueError(f"Claude effort discovery failed: {result.error}")
    combined = f"{result.stdout}\n{result.stderr}"
    return parse_claude_efforts(combined)


def _probe_grok(selector: tuple[str, ...], env: Mapping[str, str], _: Path | None) -> JsonObject:
    return parse_grok_catalog(_probe_output(selector, ("models",), env).stdout)


_PI_LIST_ARGUMENTS = (
    "--offline",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-approve",
    "--list-models",
)


def _probe_pi(selector: tuple[str, ...], env: Mapping[str, str], _: Path | None) -> JsonObject:
    models = _probe_output(selector, _PI_LIST_ARGUMENTS, env).stdout
    help_output = _probe_output(selector, ("--help",), env).stdout
    return parse_pi_catalog(f"{models}\n{help_output}")


ADAPTERS = {
    "cursor": _probe_cursor,
    "droid": _probe_droid,
    "codex": _probe_codex,
    "kimi": _probe_kimi,
    "claude": _probe_claude,
    "grok": _probe_grok,
    "opencode": _probe_opencode,
    "pi": _probe_pi,
    "omp": _probe_omp,
}


def _empty_harness_record(
    *,
    installed: bool,
    selector: tuple[str, ...] | None,
    version: str | None,
    probe_status: str,
    warnings: list[str],
) -> JsonObject:
    return {
        "installed": installed,
        "selector": list(selector or ()),
        "version": version,
        "probeStatus": probe_status,
        "modelScope": "unknown",
        "defaultModel": None,
        "models": {},
        "harnessReasoning": None,
        "warnings": warnings,
    }


def probe_harness(
    config: JsonObject,
    harness: str,
    *,
    env: Mapping[str, str],
    factory_settings_path: Path | None = None,
) -> JsonObject:
    resolution = resolve_harness_selector(config, harness, env=env)
    warnings = list(resolution.warnings)
    if resolution.selector is None:
        return _empty_harness_record(
            installed=False,
            selector=None,
            version=None,
            probe_status="missing" if resolution.error == "probe_missing" else "error",
            warnings=warnings + ([resolution.error] if resolution.error else []),
        )
    if harness == "devin":
        return _empty_harness_record(
            installed=True,
            selector=resolution.selector,
            version=resolution.version,
            probe_status="partial",
            warnings=[*warnings, "Devin exposes no prompt-free model enumeration"],
        )
    adapter = ADAPTERS[harness]
    try:
        fragment = adapter(resolution.selector, env, factory_settings_path)
        record: JsonObject = {
            "installed": True,
            "selector": list(resolution.selector),
            "version": resolution.version,
            **fragment,
        }
        fragment_warnings = fragment.get("warnings")
        record["warnings"] = warnings + (
            list(fragment_warnings) if isinstance(fragment_warnings, list) else []
        )
        _validate_harness_record(harness, record)
        return record
    except (OSError, ValueError) as exc:
        return _empty_harness_record(
            installed=True,
            selector=resolution.selector,
            version=resolution.version,
            probe_status="error",
            warnings=[*warnings, _scrub_diagnostic(str(exc))],
        )


def probe_all_harnesses(
    config: JsonObject,
    *,
    env: Mapping[str, str],
    factory_settings_path: Path | None = None,
) -> JsonObject:
    return {
        harness: probe_harness(
            config,
            harness,
            env=env,
            factory_settings_path=factory_settings_path,
        )
        for harness in KNOWN_ENGINES
    }


def _normalized_profile_name(profile_name: str | None) -> str:
    if profile_name is None:
        return "default"
    normalized = profile_name.strip()
    if not normalized:
        raise ValueError("discovery profile name must be non-empty")
    return normalized


def discovery_cache_path(profile_name: str | None, *, home: Path | None = None) -> Path:
    """Return the private user cache path for one resolved auth profile."""
    root = (home if home is not None else Path.home()) / ".delegate" / "cache" / "discovery"
    if profile_name is None:
        return root / "default.json"
    normalized = _normalized_profile_name(profile_name)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return root / f"profile-{digest}.json"


def load_discovery_cache(
    profile_name: str | None,
    *,
    home: Path | None = None,
) -> JsonObject | None:
    """Load a valid profile snapshot; malformed data is treated as absent."""
    path = discovery_cache_path(profile_name, home=home)
    try:
        snapshot = private_io.read_json_object(path)
        if snapshot is None:
            return None
        validate_snapshot(
            snapshot,
            expected_profile=_normalized_profile_name(profile_name),
            allow_unknown_harnesses=True,
        )
    except (private_io.RegistryJsonError, ValueError):
        return None

    harnesses = snapshot["harnesses"]
    if not isinstance(harnesses, dict):  # validate_snapshot already enforces this.
        return None
    if any(harness not in KNOWN_ENGINES for harness in harnesses):
        snapshot = dict(snapshot)
        snapshot["harnesses"] = {
            harness: record for harness, record in harnesses.items() if harness in KNOWN_ENGINES
        }
    return snapshot


def write_discovery_cache(
    profile_name: str | None,
    snapshot: JsonObject,
    *,
    home: Path | None = None,
) -> Path:
    """Atomically replace one whole validated profile snapshot.

    Same-profile concurrent refreshes intentionally remain whole-snapshot
    last-writer-wins. Different profiles never share a writable file.
    """
    validate_snapshot(snapshot, expected_profile=_normalized_profile_name(profile_name))
    path = discovery_cache_path(profile_name, home=home)
    private_io.write_json_atomic(path, snapshot)
    return path


def _effective_configured_selectors(
    config: JsonObject,
    harness: str,
    *,
    profile: profiles.ProfileResolution,
) -> tuple[tuple[str, ...], ...]:
    candidates = selector_candidates(config, harness)
    if candidates[0][1]:
        candidates = candidates[:1]
    env = profiles.child_environment(overrides=profile.env)
    return tuple(
        normalized
        for selector, _explicit in candidates
        if (normalized := _normalize_selector(selector, env=env)) is not None
    )


def selector_has_drifted(
    config: JsonObject,
    harness: str,
    cached_record: JsonObject,
    *,
    profile: profiles.ProfileResolution,
) -> bool:
    """Compare cached and configured selectors without invoking a child CLI."""
    cached = cached_record.get("selector")
    if not _string_list(cached, allow_empty=False):
        return True
    return tuple(cached) not in _effective_configured_selectors(config, harness, profile=profile)


def refresh_discovery(
    config: JsonObject,
    *,
    profile: profiles.ProfileResolution,
    engines: Collection[str] | None = None,
    home: Path | None = None,
    factory_settings_path: Path | None = None,
    persist: bool = True,
) -> JsonObject:
    """Probe harnesses, optionally persisting successful last-known-good records."""
    if isinstance(engines, str):
        raise ValueError("discovery engines must be a collection of harness names")
    selected = tuple(dict.fromkeys(KNOWN_ENGINES if engines is None else engines))
    unknown = [engine for engine in selected if engine not in KNOWN_ENGINES]
    if unknown:
        raise ValueError(f"unknown discovery harness: {unknown[0]}")

    profile_name = _normalized_profile_name(profile.name)
    existing = load_discovery_cache(profile.name, home=home)
    snapshot = existing if existing is not None else empty_snapshot(profile=profile_name)
    attempts: JsonObject = {}
    updated: list[str] = []
    stale: list[str] = []
    env = profiles.child_environment(overrides=profile.env)

    for harness in selected:
        try:
            record = probe_harness(
                config,
                harness,
                env=env,
                factory_settings_path=factory_settings_path,
            )
            _validate_harness_record(harness, record)
        except Exception as exc:
            diagnostic = _scrub_diagnostic(str(exc)) or "discovery probe failed"
            record = _empty_harness_record(
                installed=False,
                selector=None,
                version=None,
                probe_status="error",
                warnings=[diagnostic],
            )
        attempts[harness] = record
        if record.get("installed") is True and record.get("probeStatus") in {"ok", "partial"}:
            if not updated:
                snapshot = {
                    **snapshot,
                    "capturedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "harnesses": dict(snapshot["harnesses"]),
                }
            snapshot["harnesses"][harness] = record
            updated.append(harness)
        elif harness in snapshot["harnesses"]:
            stale.append(harness)

    cache_path = discovery_cache_path(profile.name, home=home)
    if persist and updated:
        write_discovery_cache(profile.name, snapshot, home=home)
    return {
        "snapshot": snapshot,
        "attempts": attempts,
        "updatedHarnesses": updated,
        "staleHarnesses": stale,
        "cachePath": str(cache_path),
        "wrote": persist and bool(updated),
    }
