from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol, TypeAlias

from delegate_agent.json_types import JsonObject

MetadataKeyGroup: TypeAlias = tuple[str, ...]

ISOLATION_METADATA_KEYS: MetadataKeyGroup = (
    "isolatedWorkspace",
    "isolationMode",
    "effectiveIsolation",
    "isolationLifecycle",
    "preservedWorkspace",
)

PERSISTENT_WORKTREE_METADATA_KEYS: MetadataKeyGroup = (
    "sourceGitRoot",
    "branch",
    "creationContext",
    "worktreeStatus",
    "safeWorkspaceMethod",
)

REASONING_METADATA_KEYS: MetadataKeyGroup = (
    "requestedReasoningEffort",
    "resolvedReasoningEffort",
    "reasoningEffortSource",
    "reasoningCapabilitySource",
    "reasoningCapabilityEvidence",
    "reasoningTransport",
)

MODEL_METADATA_KEYS: MetadataKeyGroup = (
    "modelRequested",
    "modelResolved",
    "capabilityModel",
    "capabilityModelSource",
)

SPEED_METADATA_KEYS: MetadataKeyGroup = ("requestedFast",)

RESUME_METADATA_KEYS: MetadataKeyGroup = ("resumedFrom", "worktreeAttachment")
INITIATOR_METADATA_KEYS: MetadataKeyGroup = ("initiatorRoot",)
PERSONA_METADATA_KEYS: MetadataKeyGroup = (
    "personaName",
    "personaSource",
    "personaTransport",
    "personaDigest",
    "personaFile",
)

SNAPSHOT_STATE_FALLBACK_KEYS: MetadataKeyGroup = (
    "worktreeStatus",
    "safeWorkspaceMethod",
)

SNAPSHOT_MANIFEST_FALLBACK_KEYS: MetadataKeyGroup = (
    *ISOLATION_METADATA_KEYS[1:],
    *PERSISTENT_WORKTREE_METADATA_KEYS,
    "worktreeCleanupCommands",
    *MODEL_METADATA_KEYS,
    *REASONING_METADATA_KEYS,
    *SPEED_METADATA_KEYS,
    *RESUME_METADATA_KEYS,
    *PERSONA_METADATA_KEYS,
)

_NATIVE_INITIATOR_ENV_KEYS = (
    ("CODEX_THREAD_ID", "codex"),
    ("CLAUDE_CODE_SESSION_ID", "claude"),
)
_ROOT_INITIATOR_NAMESPACES = {"codex", "claude"}


def _clean_initiator_value(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        return None
    if ":" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _clean_root_initiator(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    namespace, sep, native_id = value.partition(":")
    if sep != ":" or namespace not in _ROOT_INITIATOR_NAMESPACES:
        return None
    clean_id = _clean_initiator_value(native_id)
    if clean_id is None:
        return None
    return f"{namespace}:{clean_id}"


def resolve_initiator_root(env: Mapping[str, str | None]) -> str | None:
    existing = _clean_root_initiator(env.get("DELEGATE_INITIATOR_ROOT"))
    if existing is not None:
        return existing

    roots = [
        f"{namespace}:{value}"
        for key, namespace in _NATIVE_INITIATOR_ENV_KEYS
        if (value := _clean_initiator_value(env.get(key))) is not None
    ]
    return roots[0] if len(roots) == 1 else None


def apply_initiator_root_env(
    env_overrides: dict[str, str] | None,
    parent_env: Mapping[str, str | None] | None = None,
) -> tuple[str | None, dict[str, str] | None]:
    env = {**(os.environ if parent_env is None else parent_env), **(env_overrides or {})}
    root = resolve_initiator_root(env)
    if root is None:
        return None, env_overrides
    return root, {**(env_overrides or {}), "DELEGATE_INITIATOR_ROOT": root}


def add_initiator_metadata(metadata: JsonObject, initiator_root: str | None) -> None:
    if initiator_root is not None:
        metadata["initiatorRoot"] = initiator_root


class SpeedMetadataCarrier(Protocol):
    fast: bool | None


def add_speed_payload_fields(payload: JsonObject, carrier: SpeedMetadataCarrier) -> None:
    if carrier.fast is not None:
        payload["requestedFast"] = carrier.fast


class ModelMetadataCarrier(Protocol):
    model: str | None
    model_requested: str | None
    capability_model: str | None
    capability_model_source: str | None


def add_model_payload_fields(payload: JsonObject, carrier: ModelMetadataCarrier) -> None:
    if payload.get("modelRequested") is None:
        payload["modelRequested"] = carrier.model_requested
    if payload.get("modelResolved") is None:
        payload["modelResolved"] = getattr(carrier, "model_resolved", None) or carrier.model
    if carrier.capability_model is not None:
        payload["capabilityModel"] = carrier.capability_model
    if carrier.capability_model_source is not None:
        payload["capabilityModelSource"] = carrier.capability_model_source


class RunMetadataCarrier(Protocol):
    isolated_workspace: bool
    isolation_mode: str
    effective_isolation: str
    isolation_lifecycle: str
    preserved_workspace: bool
    source_git_root: str | None
    branch: str | None
    creation_context: JsonObject | None
    worktree_status: str | None
    safe_workspace_method: str | None
    warnings: tuple[str, ...]


def add_run_metadata_payload_fields(payload: JsonObject, carrier: RunMetadataCarrier) -> None:
    payload["isolatedWorkspace"] = carrier.isolated_workspace
    payload["isolationMode"] = carrier.isolation_mode
    payload["effectiveIsolation"] = carrier.effective_isolation
    payload["isolationLifecycle"] = carrier.isolation_lifecycle
    payload["preservedWorkspace"] = carrier.preserved_workspace

    if carrier.source_git_root is not None:
        payload["sourceGitRoot"] = carrier.source_git_root
    if carrier.branch is not None:
        payload["branch"] = carrier.branch
    if carrier.creation_context is not None:
        payload["creationContext"] = carrier.creation_context
    if carrier.worktree_status is not None:
        payload["worktreeStatus"] = carrier.worktree_status
    if carrier.safe_workspace_method is not None:
        payload["safeWorkspaceMethod"] = carrier.safe_workspace_method
    if carrier.warnings:
        payload["warnings"] = list(carrier.warnings)
