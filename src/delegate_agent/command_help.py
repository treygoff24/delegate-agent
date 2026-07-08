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
CALL_MODE_NOTE = (
    "call mode is a stateless model call with no repo: no workspace cwd, isolation, "
    "progress heartbeat, commit policy, run registry, snapshot, or completion report. "
    "It defaults to work-level capability (the child may write/run in a throwaway temp "
    "cwd, so treat it like work and only pass trusted prompts); pass --read-only for the "
    "stateless judge/completion contract (read-only capability plus an evaluator preamble "
    "so non-Codex engines don't derail on repo-flavored prompts)."
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
        "--group",
        "NAME",
        "Tag a launched run for later runs/wait/worktree selectors ([A-Za-z0-9._-]{1,64}).",
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
_INCLUDE_DIRTY_OPTION = OptionSpec(
    "--include-dirty",
    None,
    "work + persistent worktree only: sync tracked edits and untracked non-ignored files into the new worktree.",
)
_READ_ONLY_OPTION = OptionSpec(
    "--read-only",
    None,
    "call mode only: drop to read-only capability and add an evaluator preamble "
    "(the stateless judge/completion contract). Rejected for safe/work.",
)
_PROMPT_ARG = ArgSpec(
    "prompt",
    False,
    "Trailing prompt text. Use --prompt-file or stdin for long or flag-like prompts.",
)
_MODE_ARG = ArgSpec(
    "mode",
    True,
    "Execution mode: safe (read-only review), work (may edit the workspace), or call (stateless one-shot model call).",
)


# --------------------------------------------------------------------------- #
# Command registry.
# --------------------------------------------------------------------------- #

COMMAND_SPECS: dict[str, CommandSpec] = {
    "cursor": CommandSpec(
        name="cursor",
        summary="Run Cursor Composer in safe, work, or stateless call mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "cursor {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] cursor call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate cursor work "Implement the scoped task; report changed files and tests."',
            'delegate cursor safe "Review this diff for regressions; report file/line/severity."',
            "delegate cursor work --prompt-file task.md",
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            CALL_MODE_NOTE,
            "Reasoning effort uses cursor.reasoningEffortModels; no standalone Cursor effort flag is emitted.",
            "Trailing prompt text begins after the mode; a later --help is prompt text, "
            "not a help request.",
        ),
        see_also=("codex", "droid", "kimi", "dry-run", "agent-help"),
    ),
    "kimi": CommandSpec(
        name="kimi",
        summary="Run Kimi Code CLI in safe, work, or stateless call mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "kimi {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] kimi call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate kimi work "Implement the scoped task; report changed files and tests."',
            'delegate kimi safe "Review this repo for regressions; report file/line/severity."',
            "delegate kimi work --prompt-file task.md",
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            CALL_MODE_NOTE,
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
        summary="Run OpenAI Codex CLI in safe, work, or stateless call mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "codex {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--output-schema FILE] [--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] codex call [--read-only] [--reasoning-effort LEVEL] "
            "[--output-schema FILE] [--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _OUTPUT_SCHEMA_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate codex safe "Review this workspace. Do not edit files."',
            'delegate codex call --reasoning-effort high "Summarize this context."',
            'delegate codex work "Implement the scoped fix, run the named check, report changes."',
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            CALL_MODE_NOTE,
            "Model selection uses codex.defaultModel in config or the run-input JSON model; "
            "there is no CLI model alias.",
            "Reasoning effort is validated against the resolved model and emitted as a Codex "
            "config override; with no codex.defaultModel, explicit --reasoning-effort applies "
            "to the Codex harness default model.",
            "--output-schema resolves relative paths from the launch cwd and is native Codex "
            "schema enforcement for the final message.",
            "codex.profile is a Codex CLI config overlay; top-level profiles selects "
            "Delegate-injected auth/env.",
        ),
        see_also=("cursor", "droid", "profiles", "models", "agent-help"),
    ),
    "claude": CommandSpec(
        name="claude",
        summary="Run Claude Code headless in safe, work, or stateless call mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "claude {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] claude call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
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
            CALL_MODE_NOTE,
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
    "grok": CommandSpec(
        name="grok",
        summary="Run xAI Grok Build CLI in safe, work, or stateless call mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "grok {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] grok call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate grok safe "Review this workspace. Do not edit files."',
            'delegate grok safe --reasoning-effort high "Review this workspace."',
            'delegate grok work "Implement the scoped fix, run the named check, report changes."',
            "delegate --isolation worktree grok work --forbid-commit "
            '"Make the change without committing."',
        ),
        notes=(
            "Prompt uses Delegate temp file via Grok --prompt-file; dry-run argv shows <prompt file>.",
            SAFE_WORKSPACE_SYNC_NOTE,
            CALL_MODE_NOTE,
            "Tracked runs use --output-format streaming-json for snapshots/run-output; "
            "pass-through uses plain.",
            "Safe mode uses Delegate isolated copy plus Grok read-only sandbox/permission controls; "
            "it does not use Grok plan mode.",
            "Grok safe mode disables web search by default; explicit "
            "policy.harness.grok.safe.webSearch=true re-enables network egress, and Delegate "
            "safe-mode isolation is filesystem-only.",
            "Work mode uses grok.workPermissionMode, unless Delegate policy explicitly "
            "enables policy.harness.grok.work.bypassApprovalsAndSandbox.",
            "Reasoning effort maps to Grok --effort (low, medium, high, xhigh, max).",
            "--output-schema is unsupported in v1 because Grok --json-schema forces final json output.",
            "The top-level grok engine is distinct from any Droid-served Grok model alias.",
        ),
        see_also=("cursor", "codex", "droid", "claude", "models", "agent-help"),
    ),
    "devin": CommandSpec(
        name="devin",
        summary="Run Cognition Devin CLI in safe, work, or stateless call mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "devin {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] devin call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
        ),
        arguments=(_MODE_ARG, _PROMPT_ARG),
        options=(
            _REASONING_EFFORT_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate devin safe "Review this workspace. Do not edit files."',
            'delegate devin work "Implement the scoped fix, run the named check, report changes."',
            "delegate devin call --read-only --prompt-file judge.md",
        ),
        notes=(
            "Prompt uses Delegate temp file via Devin --prompt-file plus -p; dry-run argv shows <prompt file>.",
            SAFE_WORKSPACE_SYNC_NOTE,
            CALL_MODE_NOTE,
            "Safe and call --read-only pass a Delegate-generated --agent-config deny-list for edit/write/exec and mcp__* plus --permission-mode auto.",
            "Work and default call mode use --permission-mode dangerous because Devin print mode rejects unapproved edit/exec tools.",
            "Model selection uses devin.defaultModel in config or the run-input JSON model; unknown models are left to Devin CLI validation.",
            "Reasoning effort is unsupported for Devin in v1.",
        ),
        see_also=("cursor", "codex", "droid", "grok", "models", "agent-help"),
    ),
    "droid": CommandSpec(
        name="droid",
        summary="Run a Factory Droid BYOK model alias in safe, work, or stateless call mode.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "droid MODEL_ALIAS {safe,work} [--reasoning-effort LEVEL] [--progress] "
            "[--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] droid MODEL_ALIAS call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
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
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate droid reviewer safe "Investigate this issue; do not edit."',
            'delegate droid reviewer work --reasoning-effort xhigh "Implement and verify."',
            'delegate droid reviewer work "Implement this bounded change; run the named check."',
        ),
        notes=(
            SAFE_WORKSPACE_SYNC_NOTE,
            CALL_MODE_NOTE,
            "Droid safe mode stays read-only; work mode uses --skip-permissions-unsafe "
            "and is intentionally no-prompt -- use only in workspaces you trust.",
            "Reasoning effort is model-specific and never changes safe/work/call permissions.",
            "--reasoning-effort requires a resolved Droid model alias from droid.models.",
            "Run delegate models to list available aliases.",
        ),
        see_also=("models", "cursor", "codex", "agent-help"),
    ),
    "dry-run": CommandSpec(
        name="dry-run",
        summary="Resolve a cursor/codex/droid/kimi/claude/grok/devin invocation and print the planned argv without running it.",
        usage=(
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run {cursor,kimi,claude,grok,devin} {safe,work} [--reasoning-effort LEVEL] "
            "[--progress] [--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run {cursor,kimi,claude,grok,devin} call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run codex {safe,work} [--reasoning-effort LEVEL] [--output-schema FILE] "
            "[--progress] [--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run codex call [--read-only] [--reasoning-effort LEVEL] [--output-schema FILE] "
            "[--prompt-file PATH] [prompt...]",
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run droid MODEL_ALIAS {safe,work} [--reasoning-effort LEVEL] "
            "[--progress] [--forbid-commit] [--include-dirty] [--prompt-file PATH] [prompt...]",
            "delegate [--json] [--isolation auto|none|worktree] "
            "dry-run droid MODEL_ALIAS call [--read-only] [--reasoning-effort LEVEL] "
            "[--prompt-file PATH] [prompt...]",
        ),
        arguments=(
            ArgSpec(
                "engine",
                True,
                "Engine to plan: cursor, codex, kimi, claude, grok, devin, or droid.",
            ),
            _PROMPT_ARG,
        ),
        options=(
            _REASONING_EFFORT_OPTION,
            _OUTPUT_SCHEMA_OPTION,
            _PROGRESS_OPTION,
            _NO_PROGRESS_OPTION,
            _FORBID_COMMIT_OPTION,
            _INCLUDE_DIRTY_OPTION,
            _READ_ONLY_OPTION,
            _PROMPT_FILE_OPTION,
        ),
        examples=(
            'delegate dry-run cursor work "Refactor the parser"',
            "delegate --json dry-run droid reviewer safe --prompt-file task.md",
            'delegate dry-run grok safe "Review this repo."',
            'delegate dry-run devin safe "Review this repo."',
            'delegate dry-run claude safe "Review this repo."',
            'delegate dry-run kimi safe "Review this repo."',
        ),
        notes=(
            "dry-run shares the full engine grammar but launches no child process.",
            "--output-schema is accepted only for codex dry-runs.",
            "Reasoning effort is resolved from config/cache/bundled capabilities without invoking child binaries.",
        ),
        see_also=("cursor", "codex", "droid", "kimi", "claude", "grok", "describe"),
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
        see_also=("cursor", "codex", "droid", "claude", "grok", "agent-help"),
    ),
    "snapshot": CommandSpec(
        name="snapshot",
        summary="Print a bounded snapshot of a tracked run.",
        usage=("delegate [--json] snapshot [--latest HARNESS] [--no-redact] <handle>",),
        arguments=(
            ArgSpec(
                "<handle>",
                False,
                "Run ID, numbered alias, bare harness latest selector, or harness:modelAlias.",
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
            "delegate snapshot cursor-1",
            "delegate snapshot cursor",
            "delegate snapshot --latest codex",
        ),
        notes=(
            "Provide either a handle or --latest HARNESS, not both.",
            "Bare harness handles resolve to the latest run; generated commands use numbered aliases.",
        ),
        see_also=("runs", "run-output"),
        unsupported_global_options=("--auth-profile",),
    ),
    "runs": CommandSpec(
        name="runs",
        summary="List tracked runs, optionally filtered by activity, recency, or harness.",
        usage=(
            "delegate [--json] runs [--active|--running|--stale|--recent] "
            "[--harness HARNESS] [--group NAME] [--limit N]",
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
            OptionSpec("--group", "NAME", "Filter by launch group."),
        ),
        examples=(
            "delegate runs --active",
            "delegate runs --running",
            "delegate runs --stale",
            "delegate runs --harness cursor --limit 5",
            "delegate runs --group wave4",
        ),
        notes=("--active, --running, --stale, and --recent are mutually exclusive.",),
        see_also=("snapshot", "run-output"),
        unsupported_global_options=("--auth-profile",),
    ),
    "run-output": CommandSpec(
        name="run-output",
        summary="Inspect a tracked run's completion report or captured stdout/stderr.",
        usage=(
            "delegate [--json] run-output [--latest HARNESS] <handle> "
            "[--completion-report] [--stdout] [--stderr] [--tail N] [--max-chars N] "
            "[--raw] [--no-redact]",
        ),
        arguments=(
            ArgSpec(
                "<handle>",
                True,
                "Run ID, numbered alias, bare harness latest selector, or harness:modelAlias.",
            ),
        ),
        options=(
            OptionSpec(
                "--latest",
                "HARNESS",
                f"Inspect the most recent run for HARNESS ({ENGINES_PROSE}); accepts harness:modelAlias.",
            ),
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
            "delegate run-output cursor-1",
            "delegate run-output --latest codex --completion-report",
            "delegate run-output cursor --completion-report",
            "delegate run-output cursor-1 --stderr --tail 100",
            "delegate run-output cursor-1 --stdout --max-chars 20000",
        ),
        notes=(
            "With no selector, prints the best available parent-facing output.",
            "Bare harness handles resolve to the latest run; generated commands use numbered aliases.",
            "Prefer this over piping launch output through tail.",
            "Non-raw stdout/stderr are bounded by line tail and character cap; use --raw only "
            "when you intentionally need the full stream.",
            "--tail and --max-chars require --stdout or --stderr; completion reports reject them.",
        ),
        see_also=("snapshot", "runs"),
        unsupported_global_options=("--auth-profile",),
    ),
    "wait": CommandSpec(
        name="wait",
        summary="Wait for tracked runs to finish and report terminal states.",
        usage=(
            "delegate [--json] wait <handle>... [--latest HARNESS] [--group NAME] "
            "[--timeout SEC] [--interval SEC] [--completion-report]",
        ),
        arguments=(
            ArgSpec(
                "<handle>",
                False,
                "Run ID, numbered alias, bare harness latest selector, or harness:modelAlias.",
            ),
        ),
        options=(
            OptionSpec(
                "--latest",
                "HARNESS",
                f"Also wait for the most recent run for HARNESS ({ENGINES_PROSE}); accepts harness:modelAlias.",
            ),
            OptionSpec("--timeout", "SEC", "Maximum wait in seconds (default 3600)."),
            OptionSpec("--group", "NAME", "Wait for all runs tagged with this group."),
            OptionSpec("--interval", "SEC", "Polling interval in seconds (default 3; min 1)."),
            OptionSpec("--completion-report", None, "Append each run's completion report."),
        ),
        examples=(
            "delegate wait codex-1 cursor-2",
            "delegate wait --latest droid:glm --timeout 600 --interval 1",
            "delegate wait --group wave4",
            "delegate --json wait cursor --completion-report",
        ),
        notes=(
            "Exit codes: 0 all succeeded; 1 any failed/cancelled; 124 timeout.",
            "Dead recorded child pids are treated as terminal failures, not as hangs.",
        ),
        see_also=("runs", "snapshot", "run-output", "cancel"),
        unsupported_global_options=("--auth-profile", "--isolation"),
    ),
    "cancel": CommandSpec(
        name="cancel",
        summary="Cancel tracked runs by signaling the recorded child process group.",
        usage=("delegate [--json] cancel <handle>...",),
        arguments=(
            ArgSpec(
                "<handle>",
                True,
                "Run ID, numbered alias, bare harness latest selector, or harness:modelAlias.",
            ),
        ),
        examples=(
            "delegate cancel cursor-1",
            "delegate cancel droid:glm",
            "delegate --json cancel codex-2",
        ),
        notes=(
            "Refuses already-terminal runs.",
            "Tracked launches record a process group; legacy runs without pgid fall back to pid with a warning.",
            "call mode is untracked and therefore not cancellable.",
        ),
        see_also=("wait", "runs", "snapshot", "run-output"),
        unsupported_global_options=("--auth-profile", "--isolation"),
    ),
    "workflow": CommandSpec(
        name="workflow",
        summary="Run, inspect, resume, and manage Delegate Workflows.",
        usage=(
            "delegate [--json] workflow run <script.py> [--args JSON] [--budget N] [--dry-run]",
            "delegate [--json] workflow run --resume <wfId> [--budget N]",
            "delegate [--json] workflow run --name <saved-name> [--args JSON] [--budget N]",
            "delegate [--json] workflow check <script.py>",
            "delegate [--json] workflow status|events|result|wait|approve|kill <wfId>",
            "delegate [--json] workflow list",
            "delegate [--json] workflow save <script.py> --name <name>",
        ),
        options=(
            OptionSpec("--args", "JSON", "JSON value exposed to the script as args."),
            OptionSpec("--budget", "N", "Maximum number of live agent() runs."),
            OptionSpec("--dry-run", None, "Stub agents and print the would-be run tree."),
            OptionSpec("--resume", "wfId", "Resume an existing workflow from its journal."),
            OptionSpec("--name", "NAME", "Use or save a user-level workflow name."),
            OptionSpec("--since", "SEQ", "For events/watch, emit events after sequence number."),
            OptionSpec("--timeout", "SEC", "For wait, maximum seconds to wait."),
        ),
        examples=(
            'delegate workflow run review.py --args \'{"files":["src/cli.py"]}\' --budget 10',
            "delegate workflow run --resume wf_0123abcdef45",
            "delegate workflow events wf_0123abcdef45 --since 12 --json",
            "delegate workflow save review.py --name review-changes",
        ),
        notes=(
            "Workflow IDs are wf_<12 hex> and live under .delegate/workflows/, separate from run IDs.",
            "Each agent() child is tagged with --group <wfId>, so runs/snapshot/run-output/cancel still work.",
            "Scripts execute as the invoking user; v1 accepts explicit paths and ~/.delegate/workflows names only.",
        ),
        see_also=("runs", "snapshot", "run-output", "describe"),
        unsupported_global_options=("--auth-profile", "--isolation", "--pass-through"),
    ),
    "workflow run": CommandSpec(
        name="workflow run",
        summary="Launch a detached workflow supervisor.",
        usage=(
            "delegate [--json] workflow run <script.py> [--args JSON] [--budget N] [--dry-run]",
            "delegate [--json] workflow run --resume <wfId>",
            "delegate [--json] workflow run --name <saved-name>",
        ),
        options=(
            OptionSpec("--args", "JSON", "JSON value exposed to the script as args."),
            OptionSpec("--budget", "N", "Maximum number of live agent() runs."),
            OptionSpec("--dry-run", None, "Stub agents and print the would-be run tree."),
            OptionSpec("--resume", "wfId", "Resume an existing workflow from its journal."),
            OptionSpec("--name", "NAME", "Resolve a saved user-level workflow name."),
        ),
        see_also=("workflow status", "workflow events", "workflow wait"),
    ),
    "workflow check": CommandSpec(
        name="workflow check",
        summary="Validate a workflow script without launching agents.",
        usage=("delegate [--json] workflow check <script.py>",),
        see_also=("workflow run",),
    ),
    "workflow status": CommandSpec(
        name="workflow status",
        summary="Read a workflow status snapshot.",
        usage=("delegate [--json] workflow status <wfId>",),
        see_also=("workflow events", "workflow result"),
    ),
    "workflow events": CommandSpec(
        name="workflow events",
        summary="Emit journal events after a sequence number.",
        usage=("delegate [--json] workflow events <wfId> [--since SEQ]",),
        options=(OptionSpec("--since", "SEQ", "Emit events with seq greater than SEQ."),),
        see_also=("workflow watch", "workflow status"),
    ),
    "workflow watch": CommandSpec(
        name="workflow watch",
        summary="Print workflow journal events incrementally.",
        usage=("delegate [--json] workflow watch <wfId> [--since SEQ]",),
        options=(OptionSpec("--since", "SEQ", "Start after sequence number."),),
        see_also=("workflow events",),
    ),
    "workflow result": CommandSpec(
        name="workflow result",
        summary="Print result.json for a completed workflow.",
        usage=("delegate [--json] workflow result <wfId>",),
        see_also=("workflow wait",),
    ),
    "workflow wait": CommandSpec(
        name="workflow wait",
        summary="Wait for a workflow to reach a terminal status.",
        usage=("delegate [--json] workflow wait <wfId> [--timeout SEC]",),
        options=(OptionSpec("--timeout", "SEC", "Maximum seconds to wait."),),
        see_also=("workflow status", "workflow result"),
    ),
    "workflow approve": CommandSpec(
        name="workflow approve",
        summary="Approve a paused gate and relaunch the supervisor.",
        usage=("delegate [--json] workflow approve <wfId>",),
        see_also=("workflow run",),
    ),
    "workflow kill": CommandSpec(
        name="workflow kill",
        summary="Terminate a supervisor and cancel in-flight child runs.",
        usage=("delegate [--json] workflow kill <wfId>",),
        see_also=("cancel", "workflow status"),
    ),
    "workflow list": CommandSpec(
        name="workflow list",
        summary="List local workflow runs and saved user workflows.",
        usage=("delegate [--json] workflow list",),
        see_also=("workflow save",),
    ),
    "workflow save": CommandSpec(
        name="workflow save",
        summary="Validate and copy a script to ~/.delegate/workflows/<name>.py.",
        usage=("delegate [--json] workflow save <script.py> --name <name>",),
        options=(OptionSpec("--name", "NAME", "Saved user workflow name."),),
        see_also=("workflow run", "workflow list"),
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
    "config": CommandSpec(
        name="config",
        summary="Manage the user Delegate config file.",
        usage=("delegate [--json] config <action>",),
        arguments=(ArgSpec("action", True, "Currently: init, sync-profiles."),),
        notes=(
            "config init writes an editable starter config to ~/.delegate/config.json, "
            "or to DELEGATE_CONFIG when that environment variable is set.",
            "config sync-profiles writes missing config.work.json/config.personal.json overlays.",
            "Run delegate config init --force to overwrite an existing base config.",
        ),
        see_also=("config init", "config sync-profiles", "profiles", "models", "describe"),
        unsupported_global_options=(
            "--cwd",
            "--isolation",
            "--auth-profile",
            "--pass-through",
            "--completion-report",
            "--no-completion-report",
        ),
    ),
    "config init": CommandSpec(
        name="config init",
        summary="Write an editable starter config file.",
        usage=("delegate [--json] config init [--force]",),
        options=(OptionSpec("--force", None, "Overwrite an existing config file."),),
        examples=(
            "delegate config init",
            "delegate --json config init --force",
        ),
        notes=(
            "The starter config includes placeholder Droid aliases and placeholder CODEX_HOME profile pointers.",
            "It also writes missing config.work.json/config.personal.json profile overlays next to the base config.",
            "Use POSIX paths inside WSL; convert Windows paths with wslpath before putting them in config.",
        ),
        see_also=("config", "config sync-profiles", "profiles", "models"),
        unsupported_global_options=(
            "--cwd",
            "--isolation",
            "--auth-profile",
            "--pass-through",
            "--completion-report",
            "--no-completion-report",
        ),
    ),
    "config sync-profiles": CommandSpec(
        name="config sync-profiles",
        summary="Write missing profile overlay config files.",
        usage=("delegate [--json] config sync-profiles",),
        examples=(
            "delegate config sync-profiles",
            "env -u AI_PROFILE delegate --json config sync-profiles",
        ),
        notes=(
            "Creates missing config.work.json and config.personal.json next to the base config.",
            "Existing profile overlay files are left untouched.",
            "Each overlay pins profiles.default and carries that profile's CODEX_HOME pointer; secrets stay out of repo/config examples.",
        ),
        see_also=("config", "config init", "profiles"),
        unsupported_global_options=(
            "--cwd",
            "--isolation",
            "--auth-profile",
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
            "[--harness HARNESS] [--group NAME] [--status STATUS] [--limit N] [--no-auto-prune]",
        ),
        options=(
            OptionSpec("--harness", "HARNESS", f"Filter by harness ({ENGINES_PROSE})."),
            OptionSpec("--group", "NAME", "Filter by launch group."),
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
            "delegate [--cwd PATH] [--json] worktree show <handle>",
            "delegate [--cwd PATH] [--json] worktree show --latest HARNESS",
        ),
        arguments=(
            ArgSpec(
                "<handle>",
                False,
                "Worktree run ID, numbered alias, or bare harness latest-worktree selector.",
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
            "delegate worktree show cursor-1",
            "delegate worktree show cursor",
            "delegate worktree show --latest droid",
        ),
        notes=(
            "Provide either a handle or --latest HARNESS, not both.",
            "Bare harness handles resolve to the latest persistent worktree, never a non-worktree run.",
        ),
        see_also=("worktree list", "worktree remove"),
        unsupported_global_options=("--isolation", "--auth-profile"),
    ),
    "worktree remove": CommandSpec(
        name="worktree remove",
        summary="Remove one persistent worktree and, by default, its branch.",
        usage=(
            "delegate [--cwd PATH] [--json] worktree remove <handle|--group NAME> "
            "[--discard-uncommitted] [--force-branch] [--force] [--keep-branch]",
        ),
        arguments=(
            ArgSpec(
                "<handle>",
                True,
                "Worktree run ID, numbered alias, or bare harness latest-worktree selector.",
            ),
        ),
        options=(
            OptionSpec(
                "--group", "NAME", "Remove all persistent worktrees tagged with this group."
            ),
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
            "delegate worktree remove cursor-1",
            "delegate worktree remove --group wave4 --discard-uncommitted",
            "delegate worktree remove cursor-1 --keep-branch",
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
            "[--merged] [--older-than DAYS] [--harness HARNESS] [--group NAME] [--include-detached] "
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
            OptionSpec("--group", "NAME", "Prune only worktrees tagged with this group."),
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
        f"delegate [--cwd PATH] [--json] {iso} cursor call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} droid MODEL_ALIAS {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} droid MODEL_ALIAS call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} codex {{safe,work}} [--reasoning-effort LEVEL] [--output-schema FILE] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} codex call [--read-only] [--reasoning-effort LEVEL] [--output-schema FILE] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} claude {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} claude call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} grok {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} grok call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} devin {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} devin call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} kimi {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} kimi call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run cursor {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run cursor call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run droid MODEL_ALIAS {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run droid MODEL_ALIAS call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run codex {{safe,work}} [--reasoning-effort LEVEL] [--output-schema FILE] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run codex call [--read-only] [--reasoning-effort LEVEL] [--output-schema FILE] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run claude {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run claude call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run grok {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run grok call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run devin {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run devin call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run kimi {{safe,work}} [--reasoning-effort LEVEL] [--progress] [--forbid-commit] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} dry-run kimi call [--read-only] [--reasoning-effort LEVEL] [--prompt-file PATH] [prompt...]",
        f"delegate [--cwd PATH] [--json] {iso} run --input-json FILE",
        "delegate [--cwd PATH] [--json] snapshot [--latest HARNESS] [--no-redact] <handle>",
        "delegate [--cwd PATH] [--json] runs "
        "[--active|--running|--stale|--recent] [--harness HARNESS] [--limit N]",
        "delegate [--cwd PATH] [--json] run-output <handle> "
        "[--completion-report] [--stdout] [--stderr] [--tail N] [--max-chars N] "
        "[--raw] [--no-redact]",
        "delegate [--cwd PATH] [--json] wait <handle>... [--latest HARNESS] "
        "[--timeout SEC] [--interval SEC] [--completion-report]",
        "delegate [--cwd PATH] [--json] cancel <handle>...",
        "delegate [--cwd PATH] [--json] worktree list "
        "[--harness HARNESS] [--status STATUS] [--limit N] [--no-auto-prune]",
        "delegate [--cwd PATH] [--json] worktree show <handle>",
        "delegate [--cwd PATH] [--json] worktree show --latest HARNESS",
        "delegate [--cwd PATH] [--json] worktree remove <handle> "
        "[--discard-uncommitted] [--force-branch] [--force] [--keep-branch]",
        "delegate [--cwd PATH] [--json] worktree prune "
        "[--merged] [--older-than DAYS] [--harness HARNESS] [--include-detached] [--dry-run] "
        "[--discard-uncommitted] [--force-branch] [--force]",
        "delegate [--cwd PATH] [--json] worktree gc [--dry-run]",
        "delegate [--cwd PATH] [--json] workflow run <script.py> "
        "[--args JSON] [--budget N] [--dry-run]",
        "delegate [--cwd PATH] [--json] workflow run --resume <wfId>",
        "delegate [--cwd PATH] [--json] workflow status|events|result|wait|approve|kill <wfId>",
        "delegate [--cwd PATH] [--json] workflow list",
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

    unsupported = set(spec.unsupported_global_options)
    return {
        "ok": True,
        "command": spec.name,
        "summary": spec.summary,
        "usage": list(spec.usage),
        "arguments": [_argument_payload(arg) for arg in spec.arguments],
        "options": [_option_payload(opt) for opt in spec.options],
        "globalOptions": [
            _option_payload(opt) for opt in GLOBAL_OPTIONS if opt.flag not in unsupported
        ],
        "unsupportedGlobalOptions": list(spec.unsupported_global_options),
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
