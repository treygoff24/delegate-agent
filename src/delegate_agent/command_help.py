"""Registry-backed single source of truth for delegate CLI help.

This module is import-pure: it performs zero I/O, loads no configuration, and
touches no filesystem. Every piece of help -- per-command text, the agent-facing
JSON contract, and the global overview block -- derives from the declarative
``COMMAND_SPECS`` registry and ``GLOBAL_OPTIONS`` so the three views cannot drift.

Worktree maintenance deliberately uses remove/prune/gc; the word "delete" never
appears in any spec or rendered output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from delegate_agent import VERSION
from delegate_agent.constants import ENGINES_PROSE
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class OptionSpec:
    """A single ``--flag`` (optionally taking an argument)."""

    flag: str
    arg: str | None
    description: str


@dataclass(frozen=True)
class ArgSpec:
    """A positional argument."""

    name: str
    required: bool
    description: str


@dataclass(frozen=True)
class CommandSpec:
    """Declarative metadata for one command path (e.g. ``"worktree remove"``)."""

    name: str
    summary: str
    usage: tuple[str, ...]
    arguments: tuple[ArgSpec, ...] = field(default_factory=tuple)
    options: tuple[OptionSpec, ...] = field(default_factory=tuple)
    examples: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    see_also: tuple[str, ...] = field(default_factory=tuple)
    unsupported_global_options: tuple[str, ...] = field(default_factory=tuple)


SAFE_WORKSPACE_SYNC_NOTE = (
    "Safe mode reviews your **current working tree** — uncommitted tracked edits "
    "and untracked, non-ignored files are mirrored into an isolated throwaway copy "
    "(only gitignored paths are excluded), so you can review local changes without "
    "committing first or pasting a diff."
)


# --------------------------------------------------------------------------- #
# Global options (must appear before the subcommand).
# --------------------------------------------------------------------------- #

GLOBAL_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec("--cwd", "PATH", "Resolve the workspace from PATH (repo root inside Git)."),
    OptionSpec("--json", None, "Emit machine-readable JSON instead of human text."),
    OptionSpec(
        "--isolation",
        "auto|none|worktree",
        "Override isolation strategy for this run (auto, none, or worktree).",
    ),
    OptionSpec(
        "--auth-profile",
        "NAME",
        "Select a configured profiles.definitions entry for this launch.",
    ),
    OptionSpec(
        "--pass-through",
        None,
        "Stream raw child stdout/stderr (incompatible with --json).",
    ),
    OptionSpec(
        "--completion-report",
        "MODE",
        "Completion-report mode: markdown (default) or none.",
    ),
    OptionSpec(
        "--no-completion-report",
        None,
        "Disable completion-report prompt injection.",
    ),
)


# Shared option/argument fragments reused across the engine-style commands.
_PROMPT_FILE_OPTION = OptionSpec(
    "--prompt-file",
    "PATH",
    "Read the prompt from PATH instead of trailing prompt text.",
)
_REASONING_EFFORT_OPTION = OptionSpec(
    "--reasoning-effort",
    "LEVEL",
    "Request model-specific reasoning depth; unsupported model/level pairs fail closed.",
)
_OUTPUT_SCHEMA_OPTION = OptionSpec(
    "--output-schema",
    "FILE",
    "Codex-only JSON Schema for the final message; also suppresses the completion-report instruction.",
)
_PROGRESS_OPTION = OptionSpec(
    "--progress",
    None,
    "Emit bounded, credential-scrubbed parent progress heartbeats to stderr while preserving final stdout.",
)
_NO_PROGRESS_OPTION = OptionSpec(
    "--no-progress",
    None,
    "Disable progress heartbeats for this launch even when progress.enabled is true in config.",
)
_FORBID_COMMIT_OPTION = OptionSpec(
    "--forbid-commit",
    None,
    "Only valid for persistent worktree work runs (--isolation worktree); "
    "fail if commits remain ahead of the creation base when the child exits.",
)
_PROMPT_ARG = ArgSpec(
    "prompt",
    False,
    "Trailing prompt text. Use --prompt-file or stdin for long or flag-like prompts.",
)
_MODE_ARG = ArgSpec(
    "mode",
    True,
    "Execution mode: safe (read-only review) or work (may edit the workspace).",
)


# --------------------------------------------------------------------------- #
# Command registry.
# --------------------------------------------------------------------------- #

COMMAND_SPECS: dict[str, CommandSpec] = {
    "cursor": CommandSpec(
        name="cursor",
        summary="Run Cursor Composer in safe (read-only) or work (editing) mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "cursor {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate cursor work "Implement the scoped task; report changed files and tests."',
            'delegate cursor safe "Review this diff for regressions; report file/line/severity."',
            "delegate cursor work --prompt-file task.md",
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            "Reasoning effort uses cursor.reasoningEffortModels; no standalone Cursor effort flag is emitted.",
            "Trailing prompt text begins after the mode; a later --help is prompt text, "
            "not a help request.",
        ),
        see_also=("codex", "droid", "kimi", "dry-run", "agent-help"),
    ),
    "kimi": CommandSpec(
        name="kimi",
        summary="Run Kimi Code CLI in safe (read-only) or work (editing) mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "kimi {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate kimi work "Implement the scoped task; report changed files and tests."',
            'delegate kimi safe "Review this repo for regressions; report file/line/severity."',
            "delegate kimi work --prompt-file task.md",
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            "Kimi safe mode also uses a read-only safety prompt.",
            "work mode uses Kimi prompt mode; Delegate does not emit --yolo because "
            "Kimi rejects combining --yolo with --prompt.",
            "Model selection uses kimi.defaultModel in config or the run-input JSON model; "
            "there is no CLI model alias.",
            "Reasoning effort is unsupported for Kimi in v1.",
            "Trailing prompt text begins after the mode; a later --help is prompt text, "
            "not a help request.",
        ),
        see_also=("cursor", "codex", "droid", "dry-run", "agent-help"),
    ),
    "codex": CommandSpec(
        name="codex",
        summary="Run OpenAI Codex CLI in safe (read-only) or work (editing) mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "codex {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--output-schema FILE] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _OUTPUT_SCHEMA_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate codex safe "Review this workspace. Do not edit files."',
            'delegate codex safe --reasoning-effort high "Review this workspace."'
            "  # requires codex.defaultModel",
            'delegate codex work "Implement the scoped fix, run the named check, report changes."',
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            "Model selection uses codex.defaultModel in config or the run-input JSON model; "
            "there is no CLI model alias.",
            "Reasoning effort is validated against the resolved model and emitted as a Codex "
            "config override; --reasoning-effort therefore requires codex.defaultModel "
            "(or a run-input model) to be set.",
            "--output-schema resolves relative paths from the launch cwd and is native Codex "
            "schema enforcement for the final message.",
            "codex.profile is a Codex CLI config overlay; top-level profiles selects "
            "Delegate-injected auth/env.",
        ),
        see_also=("cursor", "droid", "profiles", "models", "agent-help"),
    ),
    "claude": CommandSpec(
        name="claude",
        summary="Run Claude Code headless in safe (read-only) or work (editing) mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "claude {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate claude safe "Review this workspace. Do not edit files."',
            'delegate claude safe --reasoning-effort high "Review this workspace."',
            'delegate claude work "Implement the scoped fix, run the named check, report changes."',
        ),
        notes=(
            "Uses Claude Code -p with prompt delivery on stdin; dry-run argv and run manifests "
            "do not contain the prompt.",
            SAFE_WORKSPACE_SYNC_NOTE,
            "Claude safe mode runs with --permission-mode plan, "
            "--strict-mcp-config, Read/Grep/Glob, and selected read-only Bash tools.",
            "Claude safe mode is not hermetic: Delegate does not prove hooks, plugins, "
            "user settings, output styles, or other non-MCP customization surfaces are disabled.",
            "Work mode uses claude.workPermissionMode, unless Delegate policy explicitly "
            "enables harness-scoped bypassApprovalsAndSandbox.",
            "Reasoning effort maps to Claude Code --effort.",
        ),
        see_also=("cursor", "codex", "droid", "models", "agent-help"),
    ),
    "droid": CommandSpec(
        name="droid",
        summary="Run a Factory Droid BYOK model alias in safe or work mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "droid MODEL_ALIAS {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--prompt-file PATH] [prompt...]",
        ),
        arguments=(
            ArgSpec(
                "MODEL_ALIAS",
                True,
                "A droid model alias from config (see delegate models).",
            ),
            _MODE_ARG,
            _PROMPT_ARG,
        ),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate droid grok safe "Investigate this issue; do not edit."',
            'delegate droid grok work --reasoning-effort xhigh "Implement and verify."',
            'delegate droid grok work "Implement this bounded change; run the named check."',
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            "Droid safe mode stays read-only; work mode uses --skip-permissions-unsafe "
            "and is intentionally no-prompt -- use only in workspaces you trust.",
            "Reasoning effort is model-specific and never changes safe/work permissions.",
            "--reasoning-effort requires a resolved Droid model alias from droid.models.",
            "Run delegate models to list available aliases.",
        ),
        see_also=("models", "cursor", "codex", "agent-help"),
    ),
    "dry-run": CommandSpec(
        name="dry-run",
        summary="Resolve a cursor/codex/droid/kimi/claude invocation and print the planned argv without running it.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run {cursor,kimi,claude} {safe,work} [--reasoning-effort LEVEL] "
            "[--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run codex {safe,work} [--reasoning-effort LEVEL] [--output-schema FILE] "
            "[--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run droid MODEL_ALIAS {safe,work} [--reasoning-effort LEVEL] "
            "[--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        ),
        arguments=(
            ArgSpec("engine", True, "Engine to plan: cursor, codex, kimi, claude, or droid."),
            _PROMPT_ARG,
        ),
        options=(
            _REASONING_EFFORT_OPTION,
            _OUTPUT_SCHEMA_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate dry-run cursor work "Refactor the parser"',
            "delegate --json dry-run droid grok safe --prompt-file task.md",
            'delegate dry-run claude safe "Review this repo."',
            'delegate dry-run kimi safe "Review this repo."',
        ),
        notes=(
            "dry-run shares the full engine grammar but launches no child process.",
            "--output-schema is accepted only for codex dry-runs.",
            "Reasoning effort is resolved from config/cache/bundled capabilities without invoking child binaries.",
        ),
        see_also=("cursor", "codex", "droid", "kimi", "claude", "describe"),
    ),
    "run": CommandSpec(
        name="run",
        summary=(
            "Launch a run from a JSON input file (engine, mode, model, cwd, prompt, isolation)."
        ),
        usage=("delegate [--json] [--isolation auto|none|worktree] run --input-json FILE",),
        options=(
            OptionSpec(
                "--input-json",
                "FILE",
                "Path to a JSON file describing the run (required).",
            ),
        ),
        examples=("delegate run --input-json task.json",),
        notes=(
            "Accepted JSON keys: engine, mode, model, cwd, prompt, isolation, "
            "reasoningEffort, outputSchema, progress, forbidCommit.",
            "Use this for long prompts or programmatic invocation.",
        ),
        see_also=("cursor", "codex", "droid", "claude", "agent-help"),
    ),
    "snapshot": CommandSpec(
        name="snapshot",
        summary="Print a bounded snapshot of a tracked run.",
        usage=("delegate [--json] snapshot [--latest HARNESS] [--no-redact] <alias-or-runId>",),
        arguments=(
            ArgSpec(
                "<alias-or-runId>",
                False,
                "Run handle to snapshot. Omit when using --latest.",
            ),
        ),
        options=(
            OptionSpec(
                "--latest",
                "HARNESS",
                f"Snapshot the most recent run for HARNESS ({ENGINES_PROSE}).",
            ),
            OptionSpec("--no-redact", None, "Do not redact secrets in the output."),
        ),
        examples=(
            "delegate snapshot cursor",
            "delegate snapshot --latest codex",
        ),
        notes=("Provide either a handle or --latest HARNESS, not both.",),
        see_also=("runs", "run-output"),
        unsupported_global_options=("--auth-profile",),
    ),
    "runs": CommandSpec(
        name="runs",
        summary="List tracked runs, optionally filtered by activity, recency, or harness.",
        usage=(
            "delegate [--json] runs [--active|--running|--stale|--recent] "
            "[--harness HARNESS] [--limit N]",
        ),
        options=(
            OptionSpec(
                "--active",
                None,
                "Show effective running and stale runs (back-compatible active view).",
            ),
            OptionSpec("--running", None, "Show only runs whose tracked process is still alive."),
            OptionSpec("--stale", None, "Show only runs recorded as running but no longer live."),
            OptionSpec("--recent", None, "Show only recent runs."),
            OptionSpec(
                "--harness",
                "HARNESS",
                f"Filter by harness: {ENGINES_PROSE}.",
            ),
            OptionSpec("--limit", "N", "Cap the number of runs listed (positive integer)."),
        ),
        examples=(
            "delegate runs --active",
            "delegate runs --running",
            "delegate runs --stale",
            "delegate runs --harness cursor --limit 5",
        ),
        notes=("--active, --running, --stale, and --recent are mutually exclusive.",),
        see_also=("snapshot", "run-output"),
        unsupported_global_options=("--auth-profile",),
    ),
    "run-output": CommandSpec(
        name="run-output",
        summary="Inspect a tracked run's completion report or captured stdout/stderr.",
        usage=(
            "delegate [--json] run-output <alias-or-runId> "
            "[--completion-report] [--stdout] [--stderr] [--tail N] [--max-chars N] "
            "[--raw] [--no-redact]",
        ),
        arguments=(ArgSpec("<alias-or-runId>", True, "Run handle to inspect."),),
        options=(
            OptionSpec("--completion-report", None, "Print the run's completion report."),
            OptionSpec("--stdout", None, "Print captured stdout (defaults to --tail 80)."),
            OptionSpec("--stderr", None, "Print captured stderr (defaults to --tail 80)."),
            OptionSpec("--tail", "N", "Print only the last N lines of the selected stream."),
            OptionSpec(
                "--max-chars",
                "N",
                "Cap non-raw stdout/stderr output to the last N characters after tailing "
                "(default 60000; incompatible with --raw).",
            ),
            OptionSpec(
                "--raw",
                None,
                "Print the full stream unbounded (incompatible with --tail and --max-chars; "
                "may be very large; JSON includes rawOutputBytes).",
            ),
            OptionSpec("--no-redact", None, "Do not redact secrets in the output."),
        ),
        examples=(
            "delegate run-output cursor",
            "delegate run-output cursor --completion-report",
            "delegate run-output cursor --stderr --tail 100",
            "delegate run-output cursor --stdout --max-chars 20000",
        ),
        notes=(
            "With no selector, prints the best available parent-facing output.",
            "Prefer this over piping launch output through tail.",
            "Non-raw stdout/stderr are bounded by line tail and character cap; use --raw only "
            "when you intentionally need the full stream.",
        ),
        see_also=("snapshot", "runs"),
        unsupported_global_options=("--auth-profile",),
    ),
    "profiles": CommandSpec(
        name="profiles",
        summary="Show the detected active auth/env profile and injected env keys.",
        usage=("delegate [--cwd PATH] [--json] [--auth-profile NAME] profiles",),
        examples=(
            "delegate profiles",
            "delegate --json --auth-profile work profiles",
        ),
        notes=(
            "Selection is read-only: flag > profiles.detectFrom environment order > profiles.default.",
            "Env values are key-aware redacted; inline profile env must not contain secrets.",
        ),
        see_also=("describe", "codex", "models"),
        unsupported_global_options=(
            "--isolation",
            "--pass-through",
            "--completion-report",
            "--no-completion-report",
        ),
    ),
    "worktree": CommandSpec(
        name="worktree",
        summary="Manage persistent isolation worktrees (list, show, remove, prune, gc).",
        usage=(
            "delegate [--cwd PATH] [--json] worktree list ...",
            "delegate [--cwd PATH] [--json] worktree show ...",
            "delegate [--cwd PATH] [--json] worktree remove ...",
            "delegate [--cwd PATH] [--json] worktree prune ...",
            "delegate [--cwd PATH] [--json] worktree gc ...",
        ),
        arguments=(
            ArgSpec(
                "action",
                True,
                "One of: list, show, remove, prune, gc.",
            ),
        ),
        notes=(
            "--isolation is not supported with worktree commands.",
            "Run delegate worktree <action> --help for action-specific options.",
        ),
        see_also=(
            "worktree list",
            "worktree show",
            "worktree remove",
            "worktree prune",
            "worktree gc",
        ),
        unsupported_global_options=("--isolation", "--auth-profile"),
    ),
    "worktree list": CommandSpec(
        name="worktree list",
        summary="List persistent worktrees, optionally filtered by harness or status.",
        usage=(
            "delegate [--cwd PATH] [--json] worktree list "
            "[--harness HARNESS] [--status STATUS] [--limit N] [--no-auto-prune]",
        ),
        options=(
            OptionSpec("--harness", "HARNESS", f"Filter by harness ({ENGINES_PROSE})."),
            OptionSpec(
                "--status",
                "STATUS",
                "Filter by status: present, removed, missing, or unknown.",
            ),
            OptionSpec("--limit", "N", "Cap the number of worktrees listed (positive integer)."),
            OptionSpec(
                "--no-auto-prune",
                None,
                "Skip the implicit auto-prune pass before listing.",
            ),
        ),
        examples=(
            "delegate worktree list",
            "delegate worktree list --harness cursor --status present",
        ),
        see_also=("worktree show", "worktree prune", "worktree gc"),
        unsupported_global_options=("--isolation", "--auth-profile"),
    ),
    "worktree show": CommandSpec(
        name="worktree show",
        summary="Show details for one persistent worktree.",
        usage=(
            "delegate [--cwd PATH] [--json] worktree show <alias-or-runId>",
            "delegate [--cwd PATH] [--json] worktree show --latest HARNESS",
        ),
        arguments=(
            ArgSpec(
                "<alias-or-runId>",
                False,
                "Worktree handle to show. Omit when using --latest.",
            ),
        ),
        options=(
            OptionSpec(
                "--latest",
                "HARNESS",
                f"Show the most recent worktree for HARNESS ({ENGINES_PROSE}).",
            ),
        ),
        examples=(
            "delegate worktree show cursor",
            "delegate worktree show --latest droid",
        ),
        notes=("Provide either a handle or --latest HARNESS, not both.",),
        see_also=("worktree list", "worktree remove"),
        unsupported_global_options=("--isolation", "--auth-profile"),
    ),
    "worktree remove": CommandSpec(
        name="worktree remove",
        summary="Remove one persistent worktree and, by default, its branch.",
        usage=(
            "delegate [--cwd PATH] [--json] worktree remove <alias-or-runId> "
            "[--discard-uncommitted] [--force-branch] [--force] [--keep-branch]",
        ),
        arguments=(ArgSpec("<alias-or-runId>", True, "Worktree handle to remove."),),
        options=(
            OptionSpec(
                "--discard-uncommitted",
                None,
                "Remove even if the worktree has uncommitted changes.",
            ),
            OptionSpec(
                "--force-branch",
                None,
                "Remove the branch even if it is not fully merged.",
            ),
            OptionSpec("--force", None, "Force removal of the worktree and its branch."),
            OptionSpec("--keep-branch", None, "Remove the worktree but keep its branch."),
        ),
        examples=(
            "delegate worktree remove cursor",
            "delegate worktree remove cursor --keep-branch",
        ),
        notes=(
            "--keep-branch is mutually exclusive with --force-branch and --force.",
            "A --help token anywhere in the args prints help and removes nothing.",
        ),
        see_also=("worktree list", "worktree prune", "worktree gc"),
        unsupported_global_options=("--isolation", "--auth-profile"),
    ),
    "worktree prune": CommandSpec(
        name="worktree prune",
        summary="Prune persistent worktrees matching age, merge, or harness criteria.",
        usage=(
            "delegate [--cwd PATH] [--json] worktree prune "
            "[--merged] [--older-than DAYS] [--harness HARNESS] [--include-detached] "
            "[--dry-run] [--discard-uncommitted] [--force-branch] [--force]",
        ),
        options=(
            OptionSpec("--merged", None, "Prune only worktrees whose branch is merged."),
            OptionSpec(
                "--older-than",
                "DAYS",
                "Prune only worktrees older than DAYS (non-negative integer).",
            ),
            OptionSpec(
                "--harness",
                "HARNESS",
                f"Prune only the given harness ({ENGINES_PROSE}).",
            ),
            OptionSpec(
                "--include-detached",
                None,
                "Also prune detached worktrees (no tracking branch).",
            ),
            OptionSpec("--dry-run", None, "Report what would be pruned without removing anything."),
            OptionSpec(
                "--discard-uncommitted",
                None,
                "Prune even worktrees with uncommitted changes.",
            ),
            OptionSpec(
                "--force-branch",
                None,
                "Remove branches even if not fully merged.",
            ),
            OptionSpec("--force", None, "Force removal of matched worktrees and their branches."),
        ),
        examples=(
            "delegate worktree prune --merged",
            "delegate worktree prune --older-than 7 --dry-run",
        ),
        notes=("Run with --dry-run first to preview the affected worktrees.",),
        see_also=("worktree list", "worktree remove", "worktree gc"),
        unsupported_global_options=("--isolation", "--auth-profile"),
    ),
    "worktree gc": CommandSpec(
        name="worktree gc",
        summary="Garbage-collect orphaned worktree metadata and stale registry entries.",
        usage=("delegate [--cwd PATH] [--json] worktree gc [--dry-run]",),
        options=(
            OptionSpec(
                "--dry-run", None, "Report what would be collected without changing anything."
            ),
        ),
        examples=(
            "delegate worktree gc",
            "delegate worktree gc --dry-run",
        ),
        see_also=("worktree list", "worktree prune", "worktree remove"),
        unsupported_global_options=("--isolation", "--auth-profile"),
    ),
    "models": CommandSpec(
        name="models",
        summary="List configured engines and model settings.",
        usage=("delegate [--json] models [--summary]",),
        options=(OptionSpec("--summary", None, "Emit a compact alias-centered inventory."),),
        examples=(
            "delegate models",
            "delegate --json models",
            "delegate --json models --summary",
        ),
        notes=(
            "Discovery output applies best-effort credential scrubbing; model IDs and paths are shown verbatim.",
            "Agent discovery should prefer --summary, then use raw output only when needed.",
        ),
        see_also=("describe", "cursor", "codex", "droid", "kimi", "claude"),
        unsupported_global_options=("--auth-profile",),
    ),
    "capabilities": CommandSpec(
        name="capabilities",
        summary="Report reasoning-effort capabilities from config, workspace cache, and bundled fallback.",
        usage=(
            "delegate [--json] [--auth-profile NAME] capabilities",
            "delegate [--json] [--auth-profile NAME] capabilities refresh",
        ),
        examples=(
            "delegate --json capabilities",
            "delegate capabilities refresh",
        ),
        notes=(
            "Reporting does not invoke child binaries.",
            "refresh may invoke child CLIs and writes .delegate/capabilities/reasoning.json in the workspace.",
        ),
        see_also=("models", "describe", "codex", "droid", "cursor"),
    ),
    "describe": CommandSpec(
        name="describe",
        summary="Print a machine-readable inventory of engines, modes, argv shapes, and policy.",
        usage=("delegate [--json] describe [--summary]",),
        options=(OptionSpec("--summary", None, "Emit a compact command/config surface summary."),),
        examples=(
            "delegate describe",
            "delegate --json describe",
            "delegate --json describe --summary",
        ),
        notes=(
            "--json describe is the full detailed surface.",
            "Discovery output applies best-effort credential scrubbing.",
            "Agent discovery should start with --summary, then use raw describe only when needed.",
        ),
        see_also=("models", "agent-help", "help"),
        unsupported_global_options=("--auth-profile",),
    ),
    "agent-help": CommandSpec(
        name="agent-help",
        summary="Print best-practices guidance for agents driving delegate.",
        usage=("delegate agent-help",),
        examples=("delegate agent-help",),
        see_also=("describe", "help"),
        unsupported_global_options=("--auth-profile",),
    ),
    "help": CommandSpec(
        name="help",
        summary="Show the overview, or focused help for a command path.",
        usage=(
            "delegate help",
            "delegate help <command> [<subcommand>]",
            "delegate --json help [<command> [<subcommand>]]",
        ),
        arguments=(
            ArgSpec(
                "command",
                False,
                "Command path to describe (e.g. cursor, worktree remove). Omit for the overview.",
            ),
        ),
        examples=(
            "delegate help",
            "delegate help worktree prune",
            "delegate --json help cursor",
        ),
        notes=(
            "<command> --help and -h are equivalent to delegate help <command>.",
            "A --json token anywhere in the help arguments selects JSON output.",
        ),
        see_also=("describe", "agent-help"),
        unsupported_global_options=("--auth-profile",),
    ),
}


# --------------------------------------------------------------------------- #
# Help-token detection.
# --------------------------------------------------------------------------- #


def is_help_token(tok: str) -> bool:
    """True only for the literal help tokens ``--help`` and ``-h``."""

    return tok in ("--help", "-h")


# --------------------------------------------------------------------------- #
# Text renderers.
# --------------------------------------------------------------------------- #


def _format_option(opt: OptionSpec) -> str:
    return f"{opt.flag} {opt.arg}" if opt.arg else opt.flag


def render_command_help_text(spec: CommandSpec, *, prog: str = "delegate") -> str:
    """Render focused, human-readable help for one command spec."""

    lines: list[str] = [f"{prog} {spec.name} -- {spec.summary}", ""]

    lines.append("Usage:")
    for usage in spec.usage:
        lines.append(f"  {usage}")

    if spec.arguments:
        lines.append("")
        lines.append("Arguments:")
        width = max(len(arg.name) for arg in spec.arguments)
        for arg in spec.arguments:
            tag = "required" if arg.required else "optional"
            lines.append(f"  {arg.name.ljust(width)}  {arg.description} ({tag})")

    if spec.options:
        lines.append("")
        lines.append("Options:")
        rendered = [(_format_option(opt), opt.description) for opt in spec.options]
        width = max(len(label) for label, _ in rendered)
        for label, description in rendered:
            lines.append(f"  {label.ljust(width)}  {description}")

    lines.append("")
    lines.append("Global options (before the subcommand):")
    unsupported_globals = set(spec.unsupported_global_options)
    rendered_globals = [
        (_format_option(opt), opt.description)
        for opt in GLOBAL_OPTIONS
        if opt.flag not in unsupported_globals
    ]
    width = max(len(label) for label, _ in rendered_globals)
    for label, description in rendered_globals:
        lines.append(f"  {label.ljust(width)}  {description}")

    if spec.examples:
        lines.append("")
        lines.append("Examples:")
        for example in spec.examples:
            lines.append(f"  {example}")

    if spec.notes:
        lines.append("")
        lines.append("Notes:")
        for note in spec.notes:
            lines.append(f"  - {note}")

    if spec.see_also:
        lines.append("")
        lines.append(f"See also: {', '.join(spec.see_also)}")

    return "\n".join(lines) + "\n"


def render_overview_text() -> str:
    """Render the global usage/help body that replaces the hand-written ``HELP``.

    Worktree actions are enumerated on their own lines (``worktree prune ...``)
    so the overview contains the literal substring ``worktree prune``.
    """

    iso = "[--isolation auto|none|worktree]"
    lines: list[str] = [f"delegate {VERSION}", "", "Usage:"]

    usage_lines = [
        f"delegate [--cwd PATH] [--json] {iso} cursor {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} droid MODEL_ALIAS {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} codex {{safe,work}} [--reasoning-effort LEVEL] [--output-schema FILE] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} claude {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} kimi {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run cursor {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run droid MODEL_ALIAS {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run codex {{safe,work}} [--reasoning-effort LEVEL] [--output-schema FILE] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run claude {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run kimi {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} run --input-json FILE",
        "delegate [--cwd PATH] [--json] snapshot [--latest HARNESS] [--no-redact] <alias-or-runId>",
        "delegate [--cwd PATH] [--json] runs "
        "[--active|--running|--stale|--recent] [--harness HARNESS] [--limit N]",
        "delegate [--cwd PATH] [--json] run-output <alias-or-runId> "
        "[--completion-report] [--stdout] [--stderr] [--tail N] [--max-chars N] "
        "[--raw] [--no-redact]",
        "delegate [--cwd PATH] [--json] worktree list "
        "[--harness HARNESS] [--status STATUS] [--limit N] [--no-auto-prune]",
        "delegate [--cwd PATH] [--json] worktree show <alias-or-runId>",
        "delegate [--cwd PATH] [--json] worktree show --latest HARNESS",
        "delegate [--cwd PATH] [--json] worktree remove <alias-or-runId> "
        "[--discard-uncommitted] [--force-branch] [--force] [--keep-branch]",
        "delegate [--cwd PATH] [--json] worktree prune "
        "[--merged] [--older-than DAYS] [--harness HARNESS] [--include-detached] [--dry-run] "
        "[--discard-uncommitted] [--force-branch] [--force]",
        "delegate [--cwd PATH] [--json] worktree gc [--dry-run]",
        "delegate [--cwd PATH] [--json] [--auth-profile NAME] profiles",
        "delegate [--json] models [--summary]",
        "delegate [--json] capabilities [refresh]",
        "delegate [--json] describe [--summary]",
        "delegate agent-help",
        "delegate help [<command> [<subcommand>]]",
    ]
    for usage in usage_lines:
        lines.append(f"  {usage}")

    lines.append("")
    lines.append("Global options must appear before the subcommand.")

    lines.append("")
    lines.append("Run output options (before subcommand):")
    detail_globals = (
        "--pass-through",
        "--completion-report",
        "--no-completion-report",
    )
    for opt in GLOBAL_OPTIONS:
        if opt.flag not in detail_globals:
            continue
        label = _format_option(opt)
        lines.append(f"  {label.ljust(26)} {opt.description}")

    lines.append("")
    lines.append("Discovery:")
    lines.append("  delegate help <command>        Focused help for any command path.")
    lines.append("  delegate --json <command> --help   Machine-readable spec for an agent.")
    lines.append("  delegate --json describe --summary  Compact command/config surface inventory.")
    lines.append("  delegate --json models --summary    Compact model inventory.")
    lines.append("  delegate agent-help             Full agent guidance.")

    lines.append("")
    lines.append(
        "Tracked runs return bounded summaries by default. Avoid piping launches through tail;"
    )
    lines.append("inspect runs with delegate snapshot, delegate runs, and delegate run-output.")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# JSON payloads.
# --------------------------------------------------------------------------- #


def _option_payload(opt: OptionSpec) -> JsonObject:
    return {"flag": opt.flag, "argument": opt.arg, "description": opt.description}


def _argument_payload(arg: ArgSpec) -> JsonObject:
    return {"name": arg.name, "required": arg.required, "description": arg.description}


def command_help_payload(spec: CommandSpec) -> JsonObject:
    """Return the stable, agent-facing JSON help contract for one command (D4)."""

    return {
        "ok": True,
        "command": spec.name,
        "summary": spec.summary,
        "usage": list(spec.usage),
        "arguments": [_argument_payload(arg) for arg in spec.arguments],
        "options": [_option_payload(opt) for opt in spec.options],
        "examples": list(spec.examples),
        "notes": list(spec.notes),
        "seeAlso": list(spec.see_also),
    }


def help_index_payload() -> JsonObject:
    """Return the overview JSON: command catalog plus global options."""

    return {
        "ok": True,
        "commands": [
            {"command": spec.name, "summary": spec.summary} for spec in COMMAND_SPECS.values()
        ],
        "globalOptions": [_option_payload(opt) for opt in GLOBAL_OPTIONS],
    }
