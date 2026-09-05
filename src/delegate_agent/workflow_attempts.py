"""Immutable operational settings for one launch of an immutable workflow pin."""

from __future__ import annotations

import copy
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from delegate_agent import config as delegate_config
from delegate_agent import redaction, run_registry, workflow_pinning
from delegate_agent.json_types import JsonObject

ATTEMPT_ENV = "DELEGATE_WORKFLOW_ATTEMPT"
SCHEMA = "delegate.workflow-attempt-config.v1"
OPS_KEYS = (
    ("workflows", "engineCaps"),
    ("workflows", "itemThreads"),
    ("workflows", "structuredOutputRetries"),
    ("workflows", "stallMinutes"),
    ("tracking", "processGroupTerminationGraceSec"),
    ("tracking", "registryLockTimeoutSec"),
    ("progress", "enabled"),
    ("progress", "initialDelaySec"),
    ("progress", "intervalSec"),
    ("worktrees", "poolWarnCount"),
)
ENV_KEYS = {
    "DELEGATE_STALL_MINUTES": ("workflows", "stallMinutes"),
    "DELEGATE_PROCESS_GROUP_TERMINATION_GRACE_SEC": ("tracking", "processGroupTerminationGraceSec"),
    "DELEGATE_REGISTRY_LOCK_TIMEOUT_SECONDS": ("tracking", "registryLockTimeoutSec"),
    "DELEGATE_PROGRESS_INITIAL_DELAY_SEC": ("progress", "initialDelaySec"),
    "DELEGATE_PROGRESS_INTERVAL_SEC": ("progress", "intervalSec"),
}


def _error(message: str) -> workflow_pinning.WorkflowPinError:
    return workflow_pinning.WorkflowPinError("invalid_workflow_attempt", message)


def operational_values(
    config: JsonObject, *, environment: dict[str, str] | None = None
) -> JsonObject:
    """Resolve defaults and aliases once, without rereading operator config files."""
    defaults = delegate_config.embedded_default_config()
    merged = delegate_config.merge_config_layer(defaults, config)
    delegate_config.validate_config(merged)
    for section, _key in OPS_KEYS:
        if merged.get(section) is None:
            merged[section] = copy.deepcopy(defaults[section])
    merged["workflows"]["stallMinutes"] = delegate_config.resolve_stall_minutes(merged)
    tracking = merged["tracking"]
    tracking["registryLockTimeoutSec"] = tracking.get(
        "registryLockTimeoutSec",
        tracking.get("registryLockTimeoutSeconds", run_registry.REGISTRY_LOCK_TIMEOUT_SECONDS),
    )
    if environment:
        for name, (section, key) in ENV_KEYS.items():
            if name in environment:
                try:
                    merged[section][key] = float(environment[name])
                except ValueError as exc:
                    raise _error(f"{name} must be a finite operational number") from exc
        delegate_config.validate_config(merged)
        # Registry-lock validation is permissive for backward compatibility;
        # attempt snapshots cannot silently fall back from an invalid override.
    lock_timeout = tracking["registryLockTimeoutSec"]
    if isinstance(lock_timeout, str):
        try:
            lock_timeout = float(lock_timeout)
        except ValueError as exc:
            raise _error("tracking.registryLockTimeoutSec must be numeric") from exc
        tracking["registryLockTimeoutSec"] = lock_timeout
    if (
        not isinstance(lock_timeout, (int, float))
        or isinstance(lock_timeout, bool)
        or not math.isfinite(lock_timeout)
        or lock_timeout < 0
    ):
        raise _error("tracking.registryLockTimeoutSec must be finite and non-negative")
    return {f"{section}.{key}": copy.deepcopy(merged[section][key]) for section, key in OPS_KEYS}


def _effective_config(pin: workflow_pinning.WorkflowPin, values: JsonObject) -> JsonObject:
    if set(values) != {f"{section}.{key}" for section, key in OPS_KEYS}:
        raise _error("operational key set does not match the supported allowlist")
    # Do not merge current defaults into the identity surface of an older pin.
    # Defaults may change between CLI versions; only the allowlisted values
    # below are permitted to cross that boundary.
    base = copy.deepcopy(pin.config)
    for section, key in OPS_KEYS:
        # Optional sections may explicitly be null (meaning defaults). Only
        # operational keys are populated here; never refill frozen identity.
        if base.get(section) is None:
            base[section] = {}
        base[section][key] = copy.deepcopy(values[f"{section}.{key}"])
    delegate_config.validate_config(base)
    if operational_values(base) != values:
        raise _error("operational values are not canonical")
    return base


@dataclass(frozen=True)
class WorkflowAttempt:
    path: Path
    config_path: Path
    config: JsonObject
    metadata: JsonObject

    @property
    def environment(self) -> dict[str, str]:
        values = self.metadata["opsValues"]
        return {
            ATTEMPT_ENV: str(self.path),
            "DELEGATE_CONFIG": str(self.config_path),
            **{name: str(values[f"{section}.{key}"]) for name, (section, key) in ENV_KEYS.items()},
        }


def prepare(
    pin: workflow_pinning.WorkflowPin,
    config: JsonObject,
    source: str,
    *,
    environment: dict[str, str] | None = None,
) -> JsonObject:
    if environment is None:
        environment = {key: value for key, value in os.environ.copy().items() if key in ENV_KEYS}
    values = operational_values(config, environment=environment)
    effective = _effective_config(pin, values)
    base_values = operational_values(pin.config)
    return {
        "schema": SCHEMA,
        "wfId": pin.workflow_id,
        "baseRuntimeDigest": pin.runtime_digest,
        "baseConfigDigest": workflow_pinning._json_digest(pin.config),
        "effectiveConfigDigest": workflow_pinning._json_digest(effective),
        "opsSource": redaction.redact_string(source),
        "opsEnvironment": sorted(name for name in ENV_KEYS if name in environment),
        "opsChangedKeys": sorted(key for key in values if values[key] != base_values[key]),
        "opsValues": values,
    }


def create(pin: workflow_pinning.WorkflowPin, metadata: JsonObject) -> WorkflowAttempt:
    effective = _effective_config(pin, metadata["opsValues"])
    digest = workflow_pinning._json_digest(metadata)
    root = pin.path.parent.parent.resolve() / "attempts" / pin.workflow_id / digest
    if root.parent.resolve(strict=False) != root.parent.absolute():
        raise _error("attempt storage path must not traverse symlinks")
    run_registry.ensure_private_dir(root.parent.parent)
    run_registry.ensure_private_dir(root.parent)
    with run_registry.file_lock(root.parent / ".attempt.lock"):
        if not root.exists():
            # A failed write may leave this private staging directory for
            # inspection, but never publishes a poisoned content-addressed
            # destination. Do not remove pre-existing partial artifacts.
            staging = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=root.parent))
            run_registry.write_json_atomic(staging / "config.json", effective)
            run_registry.write_json_atomic(staging / "attempt.json", metadata)
            if (
                run_registry.read_json_object(staging / "config.json") != effective
                or run_registry.read_json_object(staging / "attempt.json") != metadata
            ):
                raise _error("staged workflow attempt differs from its validated input")
            (staging / "config.json").chmod(0o400)
            (staging / "attempt.json").chmod(0o400)
            staging.chmod(0o500)
            if root.exists() or root.is_symlink():
                # A non-cooperating writer may have published while we staged.
                # Never replace even an empty foreign partial directory.
                return load(root / "attempt.json", pin=pin)
            staging.rename(root)
        return load(root / "attempt.json", pin=pin)


def load(path: Path, *, pin: workflow_pinning.WorkflowPin | None = None) -> WorkflowAttempt:
    """Validate location, base binding, config bytes, and allowlist before use."""
    try:
        metadata = run_registry.read_json_object(path)
        if not isinstance(metadata, dict) or metadata.get("schema") != SCHEMA:
            raise _error("unsupported attempt metadata")
        if set(metadata) != {
            "schema",
            "wfId",
            "baseRuntimeDigest",
            "baseConfigDigest",
            "effectiveConfigDigest",
            "opsSource",
            "opsEnvironment",
            "opsChangedKeys",
            "opsValues",
        }:
            raise _error("unsupported attempt metadata fields")
        wf_id = metadata.get("wfId")
        if not isinstance(wf_id, str):
            raise _error("attempt workflow id is missing")
        pin = pin or workflow_pinning.load_pin(wf_id)
        if pin is None or pin.attempt_config_version != 1 or pin.workflow_id != wf_id:
            raise _error("attempt does not bind a supported workflow pin")
        environment_pin = os.environ.get("DELEGATE_WORKFLOW_PIN")
        if environment_pin is not None and environment_pin != str(pin.path):
            raise _error("workflow pin environment disagrees with the attempt")
        digest = workflow_pinning._json_digest(metadata)
        expected = pin.path.parent.parent.resolve() / "attempts" / wf_id / digest / "attempt.json"
        if path.absolute() != expected or path.resolve() != expected:
            raise _error("attempt path does not match its content-addressed pin location")
        if metadata.get("baseRuntimeDigest") != pin.runtime_digest or metadata.get(
            "baseConfigDigest"
        ) != workflow_pinning._json_digest(pin.config):
            raise _error("attempt base pin digest differs")
        values = metadata.get("opsValues")
        if not isinstance(values, dict):
            raise _error("attempt operational values are missing")
        base_values = operational_values(pin.config)
        if metadata.get("opsChangedKeys") != sorted(
            key for key in values if values[key] != base_values.get(key)
        ):
            raise _error("attempt changed-key provenance differs")
        environments = metadata.get("opsEnvironment")
        if (
            not isinstance(metadata.get("opsSource"), str)
            or not isinstance(environments, list)
            or any(name not in ENV_KEYS for name in environments)
        ):
            raise _error("attempt operational provenance is invalid")
        effective = _effective_config(pin, values)
        config_path = path.parent / "config.json"
        if (
            config_path.is_symlink()
            or run_registry.read_json_object(config_path) != effective
            or metadata.get("effectiveConfigDigest") != workflow_pinning._json_digest(effective)
        ):
            raise _error("attempt effective config differs from its allowed base projection")
        return WorkflowAttempt(path, config_path, effective, metadata)
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        delegate_config.ConfigError,
    ) as exc:
        raise _error("could not validate workflow attempt artifact") from exc


def from_environment(*, pin: workflow_pinning.WorkflowPin | None = None) -> WorkflowAttempt | None:
    value = os.environ.get(ATTEMPT_ENV)
    if not value:
        return None
    attempt = load(Path(value), pin=pin)
    if os.environ.get("DELEGATE_CONFIG") != str(attempt.config_path):
        raise _error("DELEGATE_CONFIG does not match the workflow attempt")
    if any(os.environ.get(key) != value for key, value in attempt.environment.items()):
        raise _error("operational environment differs from the workflow attempt")
    return attempt
