"""Dataclasses describing parsed CLI commands and built engine requests.

A dependency leaf for the CLI layer: it depends only on the command-argument
modules, isolation context, and prompt-transport constants, never on ``cli``,
so the parser, request builder, and argv builders can all share these models
without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

from delegate_agent import profiles, stall_watchdog
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_WRAPPED
from delegate_agent.isolation import IsolationContext
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import PROMPT_TRANSPORT_ARGV

CONTINUITY_MODES = frozenset({"pinned", "fungible", "panel"})
DEFAULT_CONTINUITY_MODE = "fungible"


@dataclass
class GlobalOptions:
    json_mode: bool = False
    cwd: str | None = None
    pass_through: bool = False
    completion_report: str | None = None
    no_mail: bool = False
    isolation: str | None = None
    auth_profile: str | None = None
    group: str | None = None
    notify: str | None = None


@dataclass
class LaunchOptions:
    engine: str | None
    mode: str | None
    model_alias: str | None = None
    prompt_parts: list[str] | None = None
    prompt_file: str | None = None
    output_schema: str | None = None
    # Internal-only resume seam: verified inherited schema text stays in memory
    # until normal launch-time materialization.
    output_schema_text: str | None = None
    reasoning_effort: str | None = None
    fast: bool | None = None
    progress_intent: str | None = None
    forbid_commit: bool = False
    forbid_commit_implied_isolation: bool = False
    include_dirty: bool = False
    read_only: bool = False
    pure: bool = False
    timeout: int | None = None
    dry_run: bool = False
    model: str | None = None
    agent: str | None = None
    persona: str | None = None
    no_persona: bool = False
    allow_repo_persona: bool = False
    persona_record_text: str | None = None
    persona_record_source: str | None = None
    persona_record_digest: str | None = None
    persona_record_path: str | None = None
    mail_push: bool = False
    resumable: bool = False
    resume_session_id: str | None = None
    continuity_mode: str | None = None
    # Parse-time advisories that only the parser can see (token positions are
    # gone by the time the prompt is one joined string).
    warnings: tuple[str, ...] = ()


@dataclass
class RunJsonOptions:
    input_json: str


@dataclass
class ResumeOptions:
    handle: str
    extra_parts: list[str] = field(default_factory=list)
    engine: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    fast: bool | None = None
    progress_intent: str | None = None
    timeout: int | None = None
    output_schema: str | None = None
    drop_output_schema: bool = False
    include_dirty: bool = False
    dry_run: bool = False
    persona: str | None = None
    no_persona: bool = False
    allow_repo_persona: bool = False
    mail_push: bool = False
    continuity_mode: str | None = None
    # Parse-time advisories, carried to the synthetic launch like LaunchOptions.
    warnings: tuple[str, ...] = ()


@dataclass
class FollowupOptions:
    handle: str
    prompt_parts: list[str] = field(default_factory=list)
    prompt_file: str | None = None
    timeout: int | None = None
    dry_run: bool = False
    warnings: tuple[str, ...] = ()


@dataclass
class InspectionOptions:
    summary: bool = False
    engine: str | None = None
    live: bool = False
    overview: bool = False
    full: bool = False


@dataclass(frozen=True)
class PromoteOptions:
    actor: str
    source: str
    runtime_digest: str | None = None


if TYPE_CHECKING:
    from delegate_agent import (
        capability_commands,
        config_commands,
        inspection_commands,
        mail,
        profile_commands,
        run_output_commands,
        wait_cancel_commands,
        worktree_commands,
    )
    from delegate_agent.workflows import commands as workflow_commands

    CommandPayload = (
        LaunchOptions
        | RunJsonOptions
        | inspection_commands.SnapshotCommand
        | inspection_commands.RunsCommand
        | run_output_commands.RunOutputCommand
        | wait_cancel_commands.WaitCommand
        | wait_cancel_commands.CancelCommand
        | workflow_commands.WorkflowCommand
        | worktree_commands.WorktreeCommand
        | config_commands.ConfigCommand
        | capability_commands.CapabilitiesCommand
        | profile_commands.ProfilesCommand
        | InspectionOptions
        | ResumeOptions
        | FollowupOptions
        | mail.MailCommand
        | PromoteOptions
    )


@dataclass
class ParsedCommand:
    subcommand: str
    global_options: GlobalOptions = field(default_factory=GlobalOptions, kw_only=True)
    help_topic: str | None = field(default=None, kw_only=True)
    payload: CommandPayload | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class ResolvedWorkspace:
    path: str
    kind: str
    # Literal launch directory when it sits below a Git root. `path` stays the
    # repository root (registry, worktrees, fleet identity); a non-isolated
    # child executes in `launch_cwd` so relative outputs land where the
    # caller stood, not at the repository root.
    launch_cwd: str | None = None


class PromptTail(NamedTuple):
    prompt_file: str | None
    output_schema: str | None
    reasoning_effort: str | None
    fast: bool | None
    progress_intent: str | None
    forbid_commit: bool
    prompt_parts: list[str]
    json_mode: bool
    isolation: str | None
    read_only: bool
    pure: bool
    timeout: int | None
    include_dirty: bool
    mail_push: bool
    model: str | None
    agent: str | None
    persona: str | None
    no_persona: bool
    allow_repo_persona: bool
    resumable: bool = False
    continuity_mode: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass
class Request:
    engine: str
    mode: str
    workspace: str
    prompt: str
    argv: list[str]
    model: str | None
    model_alias: str | None = None
    model_requested: str | None = None
    capability_model: str | None = None
    capability_model_source: str | None = None
    output_schema: str | None = None
    output_schema_text: str | None = None
    pure: bool = False
    timeout: int | None = None
    dry_run: bool = False
    workspace_kind: str = "git"
    isolation_context: IsolationContext | None = None
    reasoning_effort: str | None = None
    requested_reasoning_effort: str | None = None
    reasoning_effort_source: str | None = None
    reasoning_capability_source: str | None = None
    reasoning_capability_evidence: str | None = None
    reasoning_transport: str | None = None
    fast: bool | None = None
    progress: bool = False
    progress_initial_delay_sec: float = 30.0
    progress_interval_sec: float = 60.0
    # Seconds of no child progress before the stall watchdog cancels the run.
    # 0 disables it. Resolved from config at request-build time.
    stall_seconds: float = float(stall_watchdog.STALL_MINUTES_DEFAULT * 60)
    # Seconds to wait after SIGTERM before escalating a child process group to
    # SIGKILL. Resolved from tracking config at request-build time.
    process_group_termination_grace_sec: float = 3.0
    # Bounded wait for the workspace registry lock. Finalization publishes a
    # WAL when this budget expires; launch admission fails before spawning.
    registry_lock_timeout_seconds: float = 120.0
    forbid_commit: bool = False
    warnings: tuple[str, ...] = ()
    stdin_text: str | None = None
    prompt_file_text: str | None = None
    agent_config_text: str | None = None
    prompt_transport: str = PROMPT_TRANSPORT_ARGV
    display_argv: list[str] | None = None
    env_overrides: dict[str, str] | None = None
    auth_profile: str | None = None
    fallback_auth_profile: str | None = None
    codex_failover_identity: str | None = None
    codex_fallback_failover_identity: str | None = None
    cleanup_workspace: bool = False
    notify: str | None = None
    include_dirty: bool = False
    call_read_only: bool = False
    group: str | None = None
    workflow_agent_key: str | None = None
    prompt_instruction_mode: str = PROMPT_INSTRUCTION_MODE_WRAPPED
    profile_resolution: profiles.ProfileResolution = field(
        default_factory=profiles.empty_profile_resolution
    )
    # The user prompt exactly as resolved (before instruction framing); persisted
    # to <runDir>/prompt.txt for tracked runs so `delegate resume` can rebuild it.
    source_prompt: str | None = None
    # Requested progress intent as passed ("on"/"off"/None), distinct from the
    # effective `progress` bool, so resume can inherit intent rather than outcome.
    progress_requested: str | None = None
    # Output schema text recorded for the run manifest (codex: normalized
    # preflight text; claude: raw file text). Transport still uses
    # output_schema/output_schema_text unchanged.
    output_schema_record_text: str | None = None
    agent: str | None = None
    # Set on runs launched by `delegate resume`: {"runId": ..., "alias": ...}.
    resumed_from: JsonObject | None = None
    persona_name: str | None = None
    persona_source: str | None = None
    persona_transport: str | None = None
    persona_digest: str | None = None
    persona_file: str | None = None
    persona_text: str | None = None
    allow_repo_persona: bool = False
    persona_env_overrides: dict[str, str] | None = None
    completion_report_mode: str = "markdown"
    account_binding_command: tuple[str, ...] | None = None
    # True only when the central framer placed the persistent-worktree notes.
    # Execution uses this structural state rather than inspecting prompt bytes.
    persistent_worktree_notes_framed: bool = False
    mail_push: bool = False
    preserve_safe_workspace: bool = False
    temporary_workspace_cleanup: JsonObject | None = None
    resumable: bool = False
    followup_of: str | None = None
    resume_session_id: str | None = None
    # True while a workflow supervisor may re-enter this run's workspace for
    # structured-output retries. Completion must retain the tree until the
    # supervisor releases it.
    structured_retry: bool = False
    continuity_mode: str = DEFAULT_CONTINUITY_MODE
    # Literal launch directory for a non-isolated run below a Git root; the
    # child spawns there, its engine cwd argv and WORKSPACE_ROOT point there,
    # while `workspace` stays the repository root.
    launch_cwd: str | None = None


@dataclass(frozen=True)
class EngineRequestParts:
    model: str | None
    argv: list[str]
    model_alias: str | None = None
    capability_model: str | None = None
    capability_model_source: str | None = None
    prompt_transport: str = PROMPT_TRANSPORT_ARGV
    display_argv: list[str] | None = None
    warnings: tuple[str, ...] = ()
    stdin_text: str | None = None
    prompt_file_text: str | None = None
    agent_config_text: str | None = None
    reasoning_effort: str | None = None
    requested_reasoning_effort: str | None = None
    reasoning_effort_source: str | None = None
    reasoning_capability_source: str | None = None
    reasoning_capability_evidence: str | None = None
    reasoning_transport: str | None = None
    env_overrides: dict[str, str] | None = None
    persona_transport: str | None = None
    persona_file_text: str | None = None
    persona_file_placeholder: str | None = None
    persona_env_overrides: dict[str, str] | None = None
    agent: str | None = None


@dataclass(frozen=True)
class EngineBuildInput:
    mode: str
    model_alias: str | None
    resolved: ResolvedWorkspace
    prompt: str
    config: JsonObject
    stream_capture: bool
    requested_effort: str | None
    effort_source: str | None
    cache: JsonObject | None
    discovery: JsonObject | None = None
    fast: bool | None = None
    output_schema: str | None = None
    output_schema_text: str | None = None
    call_read_only: bool = False
    pure: bool = False
    model_override: str | None = None
    agent: str | None = None
    persona_name: str | None = None
    persona_text: str | None = None
    persona_digest: str | None = None
    persona_transport: str | None = None
    persona_env_overrides: dict[str, str] | None = None
    persist_session: bool = False
    resumable: bool = False
    resume_session_id: str | None = None
