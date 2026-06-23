"""Dataclasses describing parsed CLI commands and built engine requests.

A dependency leaf for the CLI layer: it depends only on the command-argument
modules, isolation context, and prompt-transport constants, never on ``cli``,
so the parser, request builder, and argv builders can all share these models
without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from delegate_agent import (
    capability_commands,
    inspection_commands,
    run_output_commands,
    worktree_commands,
)
from delegate_agent.isolation import IsolationContext
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import PROMPT_TRANSPORT_ARGV


@dataclass
class GlobalOptions:
    json_mode: bool = False
    cwd: str | None = None
    pass_through: bool = False
    completion_report: str | None = None
    isolation: str | None = None


@dataclass
class LaunchOptions:
    engine: str | None
    mode: str | None
    model_alias: str | None = None
    prompt_parts: list[str] | None = None
    prompt_file: str | None = None
    reasoning_effort: str | None = None
    dry_run: bool = False


@dataclass
class RunJsonOptions:
    input_json: str


@dataclass
class InspectionOptions:
    summary: bool = False
    redacted: bool = False


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
    worktree: worktree_commands.WorktreeCommand | None = None
    capabilities: capability_commands.CapabilitiesCommand | None = None
    inspection: InspectionOptions | None = None

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
        worktree: worktree_commands.WorktreeCommand | None = None,
        capabilities: capability_commands.CapabilitiesCommand | None = None,
        inspection: InspectionOptions | None = None,
    ) -> None:
        self.subcommand = subcommand
        self.global_options = global_options or GlobalOptions()
        self.help_topic = help_topic
        self.launch = launch
        self.run_json = run_json
        self.snapshot = snapshot
        self.runs = runs
        self.run_output = run_output
        self.worktree = worktree
        self.capabilities = capabilities
        self.inspection = inspection


@dataclass(frozen=True)
class ResolvedWorkspace:
    path: str
    kind: str


@dataclass
class Request:
    engine: str
    mode: str
    workspace: str
    prompt: str
    argv: list[str]
    model: str | None
    model_alias: str | None = None
    dry_run: bool = False
    workspace_kind: str = "git"
    isolation_context: IsolationContext | None = None
    reasoning_effort: str | None = None
    reasoning_effort_source: str | None = None
    reasoning_capability_source: str | None = None
    reasoning_transport: str | None = None
    warnings: tuple[str, ...] = ()
    stdin_text: str | None = None
    prompt_file_text: str | None = None
    prompt_transport: str = PROMPT_TRANSPORT_ARGV
    display_argv: list[str] | None = None


@dataclass(frozen=True)
class EngineRequestParts:
    model: str | None
    argv: list[str]
    model_alias: str | None = None
    prompt_transport: str = PROMPT_TRANSPORT_ARGV
    display_argv: list[str] | None = None
    warnings: tuple[str, ...] = ()
    stdin_text: str | None = None
    prompt_file_text: str | None = None
    reasoning_effort: str | None = None
    reasoning_effort_source: str | None = None
    reasoning_capability_source: str | None = None
    reasoning_transport: str | None = None


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
