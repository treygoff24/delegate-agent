from __future__ import annotations

from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent import harness_discovery, private_io, profiles, redaction, wsl
from delegate_agent import rendering as delegate_rendering
from delegate_agent.constants import BINARY_CONFIG_ENGINES, KNOWN_ENGINES, engine_modes
from delegate_agent.describe_payload import models_summary_payload
from delegate_agent.errors import EXIT_MISSING_BINARY, EXIT_OK, DelegateError
from delegate_agent.json_types import JsonObject

PUBLIC_VERSION_LIMIT = 256


def _config_error(exc: delegate_config.ConfigError) -> DelegateError:
    return DelegateError(exc.error, exc.message)


def _target_path() -> Path:
    path = delegate_config.config_path()
    if wsl.should_reject_windows_path(str(path)):
        raise DelegateError(
            "windows_path",
            wsl.windows_path_message(delegate_config.CONFIG_ENV, str(path)),
        )
    return path


def _load_existing_config(path: Path) -> JsonObject:
    try:
        config, source = delegate_config.load_config(workspace=None)
        delegate_config.validate_config(config)
    except delegate_config.ConfigError as exc:
        raise _config_error(exc) from exc
    except OSError as exc:
        raise DelegateError(
            "config_read_failed",
            f"Could not read config at {delegate_config.config_path()}.",
        ) from exc
    if source == "embedded-default" or Path(source).absolute() != path.absolute():
        raise DelegateError(
            "config_changed_during_setup",
            f"Config at {path} disappeared or changed source during setup.",
        )
    return config


def _config_state_after_write_error(path: Path) -> str:
    try:
        path.lstat()
    except OSError:
        return "absent"
    return "present-unverified"


def _read_config_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DelegateError(
            "config_read_failed",
            f"Could not read config at {path}.",
        ) from exc


def _config_bytes_match(path: Path, expected: bytes) -> bool:
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def _embedded_config() -> JsonObject:
    config = delegate_config.embedded_default_config()
    try:
        delegate_config.validate_config(config)
    except delegate_config.ConfigError as exc:  # pragma: no cover - package invariant
        raise _config_error(exc) from exc
    return config


def _safe_selector(record: object) -> list[str] | None:
    if not isinstance(record, dict) or record.get("installed") is not True:
        return None
    selector = record.get("selector")
    if (
        not isinstance(selector, list)
        or len(selector) != 1
        or not isinstance(selector[0], str)
        or not selector[0]
        or "\0" in selector[0]
        or not Path(selector[0]).is_absolute()
        or redaction.redact_string(selector[0]) != selector[0]
    ):
        return None
    return list(selector)


def _minimal_selector_overrides(attempts: JsonObject) -> JsonObject:
    overrides: JsonObject = {}
    for engine in KNOWN_ENGINES:
        selector = _safe_selector(attempts.get(engine))
        if selector is None:
            continue
        if engine == "cursor":
            overrides[engine] = {"argvPrefix": selector}
        elif engine in BINARY_CONFIG_ENGINES and len(selector) == 1:
            overrides[engine] = {"binary": selector[0]}
    return overrides


def _reasoning_status(record: object) -> str:
    if not isinstance(record, dict) or record.get("installed") is not True:
        return "unavailable"
    declarations: list[object] = [record.get("harnessReasoning")]
    models = record.get("models")
    if isinstance(models, dict):
        declarations.extend(
            model.get("reasoning") for model in models.values() if isinstance(model, dict)
        )
    evidence = {
        declaration.get("evidence")
        for declaration in declarations
        if isinstance(declaration, dict) and isinstance(declaration.get("evidence"), str)
    }
    if "exact" in evidence or "inferred-route" in evidence:
        return "model-specific"
    if "harness" in evidence:
        return "harness-wide"
    return "unknown"


def _next_action(
    engine: str,
    record: object,
    *,
    launchable: bool,
    selector_drift: bool,
) -> str:
    if not isinstance(record, dict) or record.get("installed") is not True:
        return f"Install and authenticate the {engine} CLI, then rerun delegate setup."
    if selector_drift:
        return f"Config selector drifted for {engine}; rerun delegate setup."
    if engine == "droid" and not launchable:
        return "Pass --model or configure droid.defaultModel."
    if record.get("probeStatus") == "error":
        return f"Fix {engine} metadata discovery, then rerun delegate setup."
    if not launchable:
        return f"Configure a launchable {engine} default, then rerun delegate setup."
    mode = "safe" if "safe" in engine_modes(engine) else engine_modes(engine)[0]
    return f"Run delegate {engine} {mode}."


def _current_harness_projection(
    config: JsonObject,
    attempts: JsonObject,
    *,
    profile: profiles.ProfileResolution,
    stale_harnesses: set[str],
) -> JsonObject:
    summary = models_summary_payload(
        config,
        "setup",
        discovery={"harnesses": attempts},
        profile=profile,
    )
    raw_statuses = summary.get("harnesses")
    statuses = raw_statuses if isinstance(raw_statuses, dict) else {}
    output: JsonObject = {}
    for engine in KNOWN_ENGINES:
        raw_record = attempts.get(engine)
        record = raw_record if isinstance(raw_record, dict) else {}
        raw_status = statuses.get(engine)
        status = raw_status if isinstance(raw_status, dict) else {}
        models = record.get("models")
        launchable = status.get("launchable") is True
        selector_drift = status.get("stale") is True
        version = record.get("version")
        probe_status = record.get("probeStatus")
        output[engine] = {
            "installed": record.get("installed") is True,
            "probeStatus": probe_status if isinstance(probe_status, str) else "error",
            "version": (
                redaction.redact_progress_label(version)[:PUBLIC_VERSION_LIMIT]
                if isinstance(version, str)
                else None
            ),
            "catalogCount": len(models) if isinstance(models, dict) else 0,
            "reasoningStatus": _reasoning_status(record),
            "launchable": launchable,
            "stale": selector_drift or engine in stale_harnesses,
            "nextAction": _next_action(
                engine,
                record,
                launchable=launchable,
                selector_drift=selector_drift,
            ),
        }
    return output


def _base_diagnostics(
    *,
    path: Path,
    config_state: str,
    profile: profiles.ProfileResolution,
    result: JsonObject,
) -> JsonObject:
    updated = result.get("updatedHarnesses")
    stale = result.get("staleHarnesses")
    return {
        "action": "setup",
        "configPath": str(path),
        "configState": config_state,
        "cachePath": str(result.get("cachePath", "")),
        "cacheState": "updated" if result.get("wrote") is True else "unchanged",
        "discoveryReady": isinstance(updated, list) and bool(updated),
        "profile": {
            "name": profile.name,
            "source": profile.source,
            "warnings": [redaction.redact_progress_label(item) for item in profile.warnings],
        },
        "updatedHarnesses": list(updated) if isinstance(updated, list) else [],
        "staleHarnesses": list(stale) if isinstance(stale, list) else [],
    }


def _emit_text(payload: JsonObject, stdout: TextIO) -> None:
    print(f"config: {payload['configState']} ({payload['configPath']})", file=stdout)
    print(f"cache: {payload['cacheState']} ({payload['cachePath']})", file=stdout)
    print(f"discovery ready: {str(payload['discoveryReady']).lower()}", file=stdout)
    print(f"ready: {str(payload['ready']).lower()}", file=stdout)
    harnesses = payload.get("harnesses")
    if isinstance(harnesses, dict):
        for engine in KNOWN_ENGINES:
            record = harnesses.get(engine)
            if not isinstance(record, dict) or record.get("installed") is not True:
                continue
            print(
                f"{engine}: {record['probeStatus']}, "
                f"models={record['catalogCount']}, launchable={str(record['launchable']).lower()}",
                file=stdout,
            )
    action = payload.get("nextAction")
    if isinstance(action, str):
        print(f"next: {action}", file=stdout)


def _config_changed_error(
    *,
    path: Path,
    config_state: str,
    profile: profiles.ProfileResolution,
    result: JsonObject,
    message: str,
) -> DelegateError:
    return DelegateError(
        "config_changed_during_setup",
        message,
        diagnostics=_base_diagnostics(
            path=path,
            config_state=config_state,
            profile=profile,
            result=result,
        ),
        next_actions=["Rerun delegate setup to discover against the current config."],
    )


def emit(
    *,
    json_mode: bool,
    auth_profile_override: str | None,
    stdout: TextIO,
) -> int:
    path = _target_path()
    existed = path.exists()
    original_config_bytes = _read_config_bytes(path) if existed else None
    config = _load_existing_config(path) if existed else _embedded_config()
    profile = profiles.resolve_active_profile(
        config,
        profiles.child_environment(),
        cli_override=auth_profile_override,
    )

    try:
        result = harness_discovery.refresh_discovery(config, profile=profile, persist=False)
    except (OSError, ValueError) as exc:
        raise DelegateError(
            "setup_discovery_failed",
            "Setup could not safely refresh the discovery cache.",
            diagnostics={
                "action": "setup",
                "configPath": str(path),
                "configState": "existing" if existed else "absent",
                "cachePath": str(harness_discovery.discovery_cache_path(profile.name)),
                "cacheState": "error",
            },
        ) from exc

    if original_config_bytes is not None and not _config_bytes_match(path, original_config_bytes):
        raise _config_changed_error(
            path=path,
            config_state="changed" if path.exists() else "absent",
            profile=profile,
            result=result,
            message="Config changed while setup was refreshing discovery; no setup config was written.",
        )

    attempts_raw = result.get("attempts")
    attempts = attempts_raw if isinstance(attempts_raw, dict) else {}
    installed = [
        engine
        for engine in KNOWN_ENGINES
        if isinstance(attempts.get(engine), dict) and attempts[engine].get("installed") is True
    ]
    if not installed:
        diagnostics = _base_diagnostics(
            path=path,
            config_state="existing" if existed else "absent",
            profile=profile,
            result=result,
        )
        diagnostics.update(
            {
                "discoveryReady": False,
                "ready": False,
                "harnesses": _current_harness_projection(
                    config,
                    attempts,
                    profile=profile,
                    stale_harnesses=set(diagnostics["staleHarnesses"]),
                ),
            }
        )
        raise DelegateError(
            "no_harnesses_found",
            "No installed supported harnesses were found.",
            EXIT_MISSING_BINARY,
            diagnostics=diagnostics,
            next_actions=[
                "Install and authenticate a supported harness, then rerun delegate setup."
            ],
        )

    config_state = "existing"
    if not existed:
        minimal = _minimal_selector_overrides(attempts)
        if not minimal:
            raise DelegateError(
                "setup_selector_unsafe",
                "Installed harnesses were found, but none had a safe absolute selector.",
                diagnostics=_base_diagnostics(
                    path=path,
                    config_state="absent",
                    profile=profile,
                    result=result,
                ),
            )
        try:
            # Preserve the user's path in diagnostics/config resolution, but let
            # ordinary symlinked ancestor directories resolve before the strict
            # atomic writer checks the final component. The basename is appended
            # after resolution so a config-file symlink is still rejected.
            atomic_path = path.parent.resolve(strict=False) / path.name
            created = private_io.write_json_atomic_if_absent(atomic_path, minimal)
        except (OSError, ValueError) as exc:
            raise DelegateError(
                "setup_config_write_failed",
                "Discovery completed, but setup could not safely create the config file.",
                diagnostics=_base_diagnostics(
                    path=path,
                    config_state=_config_state_after_write_error(path),
                    profile=profile,
                    result=result,
                ),
                next_actions=["Inspect the config path, then rerun delegate setup."],
            ) from exc
        if not created:
            try:
                _load_existing_config(path)
            except DelegateError as exc:
                raise _config_changed_error(
                    path=path,
                    config_state="race-winner",
                    profile=profile,
                    result=result,
                    message="Another process created the config during setup, but it could not be validated.",
                ) from exc
            raise _config_changed_error(
                path=path,
                config_state="race-winner",
                profile=profile,
                result=result,
                message="Another process created the config during setup; rerun discovery against it.",
            )
        config_state = "created"
        try:
            config = _load_existing_config(path)
        except DelegateError as exc:
            exc.diagnostics = _base_diagnostics(
                path=path,
                config_state=config_state,
                profile=profile,
                result=result,
            )
            raise

    updated = result.get("updatedHarnesses")
    if isinstance(updated, list) and updated:
        snapshot = result.get("snapshot")
        try:
            if not isinstance(snapshot, dict):
                raise ValueError("discovery refresh returned no validated snapshot")
            harness_discovery.write_discovery_cache(profile.name, snapshot)
        except (OSError, ValueError) as exc:
            diagnostics = _base_diagnostics(
                path=path,
                config_state=config_state,
                profile=profile,
                result=result,
            )
            diagnostics["cacheState"] = "error"
            raise DelegateError(
                "setup_cache_write_failed",
                "Setup reconciled the config, but could not safely publish discovery.",
                diagnostics=diagnostics,
                next_actions=["Inspect the cache path, then rerun delegate setup."],
            ) from exc
        result["wrote"] = True

    stale_raw = result.get("staleHarnesses")
    stale_harnesses = set(stale_raw) if isinstance(stale_raw, list) else set()
    harnesses = _current_harness_projection(
        config,
        attempts,
        profile=profile,
        stale_harnesses=stale_harnesses,
    )
    ready_engines = [
        engine
        for engine in KNOWN_ENGINES
        if isinstance(harnesses.get(engine), dict) and harnesses[engine].get("launchable") is True
    ]
    discovery_ready = isinstance(updated, list) and bool(updated)
    diagnostics = _base_diagnostics(
        path=path,
        config_state=config_state,
        profile=profile,
        result=result,
    )
    payload: JsonObject = {
        "ok": True,
        **diagnostics,
        "discoveryReady": discovery_ready,
        "ready": bool(ready_engines),
        "harnesses": harnesses,
        "nextAction": (
            harnesses[ready_engines[0]]["nextAction"]
            if ready_engines
            else harnesses[installed[0]]["nextAction"]
        ),
    }
    if json_mode:
        delegate_rendering.print_json(payload, stdout)
    else:
        _emit_text(payload, stdout)
    return EXIT_OK
