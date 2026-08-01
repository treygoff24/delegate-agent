"""Dataclasses describing parsed CLI commands and built engine requests.

A dependency leaf for the CLI layer: it depends only on the command-argument
modules, isolation context, and prompt-transport constants, never on ``cli``,
so the parser, request builder, and argv builders can all share these models
without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from delegate_agent import (
    capability_commands,
    config_commands,
    inspection_commands,
    mail,
    profile_commands,
    profiles,
    run_output_commands,
    wait_cancel_commands,
    worktree_commands,
)
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_WRAPPED
from delegate_agent.isolation import IsolationContext
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import PROMPT_TRANSPORT_ARGV
from delegate_agent.workflows import commands as workflow_commands


@dataclass
class GlobalOptions:
    json_mode: bool = False
    cwd: str | None = None
    pass_through: bool = False
    completion_report: str | None = None
    isolation: str | None = None
    auth_profile: str | None = None
    group: str | None = None


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


@dataclass
class InspectionOptions:
    summary: bool = False
    engine: str | None = None
    live: bool = False


@dataclass(init=False)
class ParsedCommand:
    subcommand: str
    global_options: GlobalOptions
    help_topic: str | None = None
    launch: LaunchOptions | None = None
    run_json: RunJsonOptions | None = None
    snapshot: inspection_commands.SnapshotCommand | None = None
    runs: inspection_commands.RunsCommand | None = None
    run_output: run_output_commands.RunOutputCommand | None = None
    wait_command: wait_cancel_commands.WaitCommand | None = None
    cancel_command: wait_cancel_commands.CancelCommand | None = None
    workflow_command: workflow_commands.WorkflowCommand | None = None
    worktree: worktree_commands.WorktreeCommand | None = None
    config_command: config_commands.ConfigCommand | None = None
    capabilities: capability_commands.CapabilitiesCommand | None = None
    profiles_command: profile_commands.ProfilesCommand | None = None
    inspection: InspectionOptions | None = None
    resume: ResumeOptions | None = None
    mail_command: mail.MailCommand | None = None

    def __init__(
        self,
        subcommand: str,
        *,
        help_topic: str | None = None,
        global_options: GlobalOptions | None = None,
        launch: LaunchOptions | None = None,
        run_json: RunJsonOptions | None = None,
        snapshot: inspection_commands.SnapshotCommand | None = None,
        runs: inspection_commands.RunsCommand | None = None,
        run_output: run_output_commands.RunOutputCommand | None = None,
        wait_command: wait_cancel_commands.WaitCommand | None = None,
        cancel_command: wait_cancel_commands.CancelCommand | None = None,
        workflow_command: workflow_commands.WorkflowCommand | None = None,
        worktree: worktree_commands.WorktreeCommand | None = None,
        config_command: config_commands.ConfigCommand | None = None,
        capabilities: capability_commands.CapabilitiesCommand | None = None,
        profiles_command: profile_commands.ProfilesCommand | None = None,
        inspection: InspectionOptions | None = None,
        resume: ResumeOptions | None = None,
        mail_command: mail.MailCommand | None = None,
    ) -> None:
        self.subcommand = subcommand
        self.global_options = global_options or GlobalOptions()
        self.help_topic = help_topic
        self.launch = launch
        self.run_json = run_json
        self.snapshot = snapshot
        self.runs = runs
        self.run_output = run_output
        self.wait_command = wait_command
        self.cancel_command = cancel_command
        self.workflow_command = workflow_command
        self.worktree = worktree
        self.config_command = config_command
        self.capabilities = capabilities
        self.profiles_command = profiles_command
        self.inspection = inspection
        self.resume = resume
        self.mail_command = mail_command


@dataclass(frozen=True)
class ResolvedWorkspace:
    path: str
    kind: str


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
    cleanup_workspace: bool = False
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
    # True only when the central framer placed the persistent-worktree notes.
    # Execution uses this structural state rather than inspecting prompt bytes.
    persistent_worktree_notes_framed: bool = False
    mail_push: bool = False


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
    call_read_only: bool = False
    pure: bool = False
    model_override: str | None = None
    agent: str | None = None
    persona_name: str | None = None
    persona_text: str | None = None
    persona_digest: str | None = None
    persona_transport: str | None = None
    persona_env_overrides: dict[str, str] | None = None
