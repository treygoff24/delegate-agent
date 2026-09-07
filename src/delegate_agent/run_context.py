"""Explicit request policy mapping shared by all tracked launch paths.

Callers own workspace identity, isolation and cleanup policy. This module never
copies arbitrary Request attributes into the runner's execution context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Required, TypedDict, Unpack

from delegate_agent import profiles, run_registry
from delegate_agent import runner as delegate_runner
from delegate_agent.json_types import JsonObject
from delegate_agent.request_models import Request
from delegate_agent.sandbox_bwrap import SandboxPlan


class RunLocation(TypedDict, total=False):
    registry_root: Required[Path]
    run_id: Required[str]
    alias: Required[str]
    source_cwd: Required[str]
    execution_cwd: Required[str]
    workspace_kind: Required[str]
    isolated_workspace: Required[bool]
    include_dirty: Required[bool]
    creation_context: JsonObject | None
    source_git_root: str | None
    isolation_mode: str
    effective_isolation: str
    isolation_lifecycle: str
    preserved_workspace: bool
    branch: str | None
    worktree_status: str | None
    safe_workspace_method: str | None
    sandbox: SandboxPlan | None
    warnings: tuple[str, ...]
    synced_files: int
    retire_worktree_on_completion: bool
    retirement_ignore_globs: tuple[str, ...]
    worktree_auto_prune_on_completion: bool
    worktree_auto_prune_merged_older_than_days: int
    temporary_workspace_cleanup: JsonObject | None


def from_request(
    request: Request,
    *,
    env_overrides: dict[str, str] | None = None,
    **location: Unpack[RunLocation],
) -> delegate_runner.RunContext:
    env = dict(request.env_overrides or {}) if env_overrides is None else dict(env_overrides)
    return delegate_runner.RunContext(
        **location,
        harness=request.engine,
        engine=request.engine,
        mode=request.mode,
        model=request.model,
        started_at=run_registry.utc_now_iso(),
        model_alias=request.model_alias,
        model_resolved=request.model,
        model_requested=request.model_requested,
        capability_model=request.capability_model,
        capability_model_source=request.capability_model_source,
        continuity_mode=request.continuity_mode,
        structured_output=request.output_schema is not None,
        reasoning_effort=request.reasoning_effort,
        requested_reasoning_effort=request.requested_reasoning_effort,
        reasoning_effort_source=request.reasoning_effort_source,
        reasoning_capability_source=request.reasoning_capability_source,
        reasoning_capability_evidence=request.reasoning_capability_evidence,
        reasoning_transport=request.reasoning_transport,
        fast=request.fast,
        prompt_transport=request.prompt_transport,
        forbid_commit=request.forbid_commit,
        progress_initial_delay_sec=request.progress_initial_delay_sec,
        progress_interval_sec=request.progress_interval_sec,
        stall_seconds=request.stall_seconds,
        process_group_termination_grace_sec=request.process_group_termination_grace_sec,
        registry_lock_timeout_seconds=request.registry_lock_timeout_seconds,
        tracked_stream_max_bytes=request.tracked_stream_max_bytes,
        env_overrides=env,
        fallback_env_overrides=profiles.codex_fallback_child_env_overrides(
            request.profile_resolution, env
        ),
        auth_profile=request.auth_profile,
        fallback_auth_profile=request.fallback_auth_profile,
        codex_failover_identity=request.codex_failover_identity,
        codex_fallback_failover_identity=request.codex_fallback_failover_identity,
        mail_push=request.mail_push,
        resumable=request.resumable,
        followup_of=request.followup_of,
        resume_session_id=request.resume_session_id,
        structured_retry=request.structured_retry,
        group=request.group,
        notify=request.notify,
        workflow_agent_key=request.workflow_agent_key,
        call_read_only=request.call_read_only or request.pure,
        pure=request.pure,
        prompt_instruction_mode=request.prompt_instruction_mode,
        source_prompt=request.source_prompt,
        progress_requested=request.progress_requested,
        timeout_seconds=request.timeout,
        output_schema_text=request.output_schema_record_text,
        agent=request.agent,
        resumed_from=request.resumed_from,
        persona_name=request.persona_name,
        persona_source=request.persona_source,
        persona_transport=request.persona_transport,
        persona_digest=request.persona_digest,
        persona_file=request.persona_file,
        persona_text=request.persona_text,
        account_binding_command=request.account_binding_command,
    )
