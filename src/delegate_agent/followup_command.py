"""`delegate followup`: re-enter a completed child's native harness session.

`delegate followup <handle> [prompt...]` re-enters a completed child's
HARNESS-NATIVE session (Codex `exec resume <SESSION_ID> <PROMPT>`, Claude
`--resume <session-id>`) so the child retains its full conversation context.

This is the deliberate counterpart to `delegate resume`, which synthesizes a
plain-text continuation (original prompt + output digest) and stays
ephemeral / cross-engine. `followup` continues the native harness conversation
directly on supported engines (Codex and Claude) and supports work-mode runs.

Trust model: the session ID is read from the canonical state view /
manifest.json. Because work-mode children can modify workspace files, the
session ID is treated as attacker-controlled input entering subprocess argv.
All record reads use bounded no-follow readers, and session IDs are strictly
validated against character, length, and engine-specific shape constraints
before entering subprocess arguments.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from delegate_agent import run_registry, stall_watchdog, worktree_records
from delegate_agent.constants import (
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    MODE_WORK,
    VALID_MODES,
)
from delegate_agent.errors import DelegateError
from delegate_agent.isolation import IsolationContext
from delegate_agent.json_types import JsonObject
from delegate_agent.private_io import (
    PRIVATE_RECORD_READ_MAX_BYTES,
    BoundedReadError,
    read_private_text_bounded,
)
from delegate_agent.request_models import (
    CONTINUITY_MODES,
    DEFAULT_CONTINUITY_MODE,
    FollowupOptions,
    GlobalOptions,
    LaunchOptions,
    ParsedCommand,
    Request,
    ResolvedWorkspace,
)
from delegate_agent.resume_command import (
    RESUMABLE_STATUSES,
    _attachment_owner_target,
    _validate_attach_target,
    recorded_workspace_env,
)

FOLLOWUP_RECORD_READ_MAX_BYTES = PRIVATE_RECORD_READ_MAX_BYTES

CLAUDE_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
CODEX_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass
class FollowupPlan:
    parsed: ParsedCommand
    original_run_id: str
    original_alias: str
    session_id: str
    attach: JsonObject | None = None
    forbid_commit: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


def _record_invalid(message: str) -> DelegateError:
    return DelegateError("followup_record_invalid", message)


def _read_record_text(path: Path) -> str:
    try:
        return read_private_text_bounded(path, max_bytes=FOLLOWUP_RECORD_READ_MAX_BYTES)
    except BoundedReadError as exc:
        if exc.reason == "not_found":
            raise
        raise _record_invalid(str(exc)) from exc


def _read_record_json(path: Path, allow_missing: bool = False) -> JsonObject | None:
    try:
        text = read_private_text_bounded(path, max_bytes=FOLLOWUP_RECORD_READ_MAX_BYTES)
    except BoundedReadError as exc:
        if allow_missing and exc.reason == "not_found":
            return None
        if exc.reason == "not_found":
            raise _record_invalid(f"required record file is missing: {path.name}") from exc
        raise _record_invalid(str(exc)) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _record_invalid(f"record file {path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _record_invalid(f"record file {path.name} must contain a JSON object")
    return data


def _load_snapshot_record(registry_root: Path, run_id: str) -> JsonObject | None:
    try:
        return run_registry.load_run_snapshot(registry_root, run_id)
    except run_registry.RegistryJsonError as exc:
        raise _record_invalid(str(exc)) from exc


def _manifest_str(manifest: JsonObject, key: str) -> str | None:
    val = manifest.get(key)
    return val if isinstance(val, str) and val else None


def validate_session_id(session_id: object, engine: str, alias: str = "run") -> str:
    """Validate a harness session ID read from untrusted record files."""
    if not isinstance(session_id, str):
        raise DelegateError(
            "session-invalid",
            f"The recorded session id for {alias} is not a string.",
            diagnostics={"code": "session-invalid"},
            next_actions=["The run record may be corrupt or tampered."],
        )
    trimmed = session_id.strip()
    if not trimmed or trimmed != session_id or len(session_id) > 256:
        raise DelegateError(
            "session-invalid",
            f"The recorded session id for {alias} is empty, untrimmed, or exceeds 256 characters.",
            diagnostics={"code": "session-invalid"},
            next_actions=["The run record may be corrupt or tampered."],
        )
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in session_id):
        raise DelegateError(
            "session-invalid",
            f"The recorded session id for {alias} contains whitespace or control characters.",
            diagnostics={"code": "session-invalid"},
            next_actions=["The run record may be corrupt or tampered."],
        )
    if session_id.startswith("-"):
        raise DelegateError(
            "session-invalid",
            f"The recorded session id for {alias} starts with a hyphen (flag injection guard).",
            diagnostics={"code": "session-invalid"},
            next_actions=["The run record may be corrupt or tampered."],
        )
    if engine == "claude":
        if not CLAUDE_SESSION_ID_RE.fullmatch(session_id):
            raise DelegateError(
                "session-invalid",
                f"The recorded Claude session id {session_id!r} for {alias} is malformed.",
                diagnostics={"code": "session-invalid"},
                next_actions=["The run record may be corrupt or tampered."],
            )
    elif engine == "codex":
        if not CODEX_SESSION_ID_RE.fullmatch(session_id):
            raise DelegateError(
                "session-invalid",
                f"The recorded Codex session id {session_id!r} for {alias} is malformed.",
                diagnostics={"code": "session-invalid"},
                next_actions=["The run record may be corrupt or tampered."],
            )
    else:
        raise DelegateError(
            "followup-unsupported",
            f"Engine {engine} does not support native session resumption; supported engines are codex and claude.",
            diagnostics={"code": "followup-unsupported"},
            next_actions=["Use delegate resume instead of followup for this engine."],
        )
    return session_id


def _resolve_followup_target(registry_root: Path, handle: str) -> tuple[str, str]:
    """Resolve a followup source without inspecting arbitrary run records."""
    index = run_registry.load_index(registry_root)
    resolved = run_registry.resolve_handle(index, handle, registry_root=registry_root)
    run_id = resolved.run_id
    alias = resolved.alias
    if not isinstance(run_id, str) or not isinstance(alias, str):
        suggestions = ", ".join(run_registry.suggest_handles(index, handle)) or "(none)"
        raise DelegateError(
            "unknown_handle",
            f"Unknown run handle: {handle}. Suggestions: {suggestions}. "
            "Runs are recorded per-workspace under <workspace>/.delegate; "
            "if this run was launched elsewhere, pass --cwd <that workspace>.",
        )
    if handle == run_id:
        return run_id, alias
    try:
        claim = _read_record_text(run_registry.aliases_dir(registry_root) / alias).strip()
    except BoundedReadError as exc:
        raise _record_invalid(f"The source run alias claim is unavailable: {alias}.") from exc
    if claim != run_id:
        raise _record_invalid(f"The source run alias claim does not match {alias}.")
    return run_id, alias


def _later_continuations(registry_root: Path, run_id: str) -> list[tuple[str, str, bool]]:
    """Runs that resumed or followed up ``run_id``, oldest first.

    Each entry is (handle, relation, has_native_session).

    Run ids start with their UTC launch second, so only runs from that second
    on can descend from this one; older manifests are never opened.
    """
    index = run_registry.load_index(registry_root)
    floor = run_id[: len("del_YYYYMMDDTHHMMSSZ")]
    found: list[tuple[str, str, str, bool]] = []
    for candidate, entry in run_registry.index_run_entries(index):
        if candidate == run_id or candidate[: len(floor)] < floor:
            continue
        try:
            manifest = run_registry.load_run_manifest_or_none(registry_root, candidate)
        except (OSError, ValueError, run_registry.RegistryJsonError):
            continue
        if not isinstance(manifest, dict):
            continue
        resumed = manifest.get("resumedFrom")
        if manifest.get("followupOf") == run_id:
            relation = "followup"
        elif isinstance(resumed, dict) and resumed.get("runId") == run_id:
            relation = "resume"
        else:
            continue
        handle = entry.get("alias") if isinstance(entry.get("alias"), str) else candidate
        try:
            state = run_registry.load_run_state_or_none(registry_root, candidate)
            snapshot = run_registry.load_run_snapshot_or_none(registry_root, candidate)
        except (OSError, ValueError, run_registry.RegistryJsonError):
            # Advisory lineage only: an unreadable sibling never blocks a followup.
            state = snapshot = None
        has_session = any(
            isinstance(record, dict) and bool(record.get("harnessSessionId"))
            for record in (state, snapshot, manifest)
        )
        found.append((candidate, handle, relation, has_session))
    return [
        (handle, relation, has_session)
        for _candidate, handle, relation, has_session in sorted(found)
    ]


def build_followup_plan(
    parsed: ParsedCommand,
    workspace: ResolvedWorkspace,
    config: JsonObject,
    *,
    stderr: TextIO,
) -> FollowupPlan:
    opts = parsed.payload
    if not isinstance(opts, FollowupOptions):
        raise DelegateError("invalid_command", "followup options are required.")
    global_options = parsed.global_options
    if global_options.pass_through:
        raise DelegateError(
            "invalid_option_combination", "--pass-through is not supported with followup."
        )
    registry_root = run_registry.registry_root_if_exists(Path(workspace.path))
    if registry_root is None:
        raise DelegateError(
            "unknown_handle",
            f"No delegate run registry exists in {workspace.path}; nothing to followup. "
            "Pass --cwd for the workspace where the run was launched.",
        )
    run_id, alias = _resolve_followup_target(registry_root, opts.handle)
    run_path = run_registry.run_directory(registry_root, run_id)

    manifest = _read_record_json(run_path / run_registry.MANIFEST_FILE)
    if manifest is None:
        raise _record_invalid("The source run has no manifest.json record.")
    manifest_cwd = _manifest_str(manifest, "cwd")
    if manifest_cwd is not None and worktree_records._canonical_path(
        manifest_cwd
    ) != worktree_records._canonical_path(workspace.path):
        raise _record_invalid(
            "The source run's cwd does not match the workspace containing its Registry."
        )

    source_state = run_registry.load_run_state_or_none(registry_root, run_id)
    source_snapshot = _load_snapshot_record(registry_root, run_id)
    effective_status = run_registry.effective_status(source_state)

    if effective_status not in RESUMABLE_STATUSES:
        raise DelegateError(
            "followup-not-terminal",
            f"The source run {alias} has status {effective_status!r}, which is not terminal; "
            "wait or cancel first.",
            diagnostics={"code": "followup-not-terminal"},
            next_actions=["Wait for the run to finish or cancel it."],
        )

    mode = _manifest_str(manifest, "mode")
    if mode not in VALID_MODES:
        raise _record_invalid("The source run's manifest does not name a valid mode.")
    if mode == MODE_CALL:
        raise DelegateError(
            "followup-unsupported-mode",
            "call runs have no resumable workspace.",
            diagnostics={"code": "followup-unsupported-mode"},
            next_actions=["call runs cannot be followed up; launch a work run."],
        )
    if mode == MODE_SAFE:
        raise DelegateError(
            "followup-unsupported-mode",
            "safe temp workspace is gone; use delegate resume.",
            diagnostics={"code": "followup-unsupported-mode"},
            next_actions=["Use delegate resume instead of followup for safe-mode runs."],
        )
    if mode != MODE_WORK:
        raise DelegateError(
            "followup-unsupported-mode",
            f"followup supports work runs only; mode {mode} is unsupported.",
            diagnostics={"code": "followup-unsupported-mode"},
            next_actions=["Use delegate resume instead of followup."],
        )

    source_engine = _manifest_str(manifest, "engine") or _manifest_str(manifest, "harness")
    if source_engine not in KNOWN_ENGINES:
        raise _record_invalid("The source run's manifest does not name a known engine.")
    if source_engine not in ("codex", "claude"):
        raise DelegateError(
            "followup-unsupported",
            f"Engine {source_engine} does not support native session resumption; supported engines are codex and claude.",
            diagnostics={"code": "followup-unsupported"},
            next_actions=["Use delegate resume instead of followup for this engine."],
        )

    raw_session_id = (
        (source_state.get("harnessSessionId") if source_state else None)
        or (source_snapshot.get("harnessSessionId") if source_snapshot else None)
        or manifest.get("harnessSessionId")
    )
    if not raw_session_id:
        raise DelegateError(
            "session-missing",
            f"Run {alias} has no recorded harnessSessionId; relaunch with --resumable "
            f"(work-mode codex or claude) to record one, or carry this run's report into a "
            f"new run with `delegate resume {alias}`.",
            diagnostics={"code": "session-missing"},
            next_actions=[
                "Relaunch the run with --resumable (work mode, codex or claude) when a native followup will be needed.",
                f'delegate resume {alias} "<extra instructions>"',
            ],
        )

    session_id = validate_session_id(raw_session_id, source_engine, alias=alias)

    notes: list[str] = []
    continuations = _later_continuations(registry_root, run_id)
    if continuations:
        latest_alias, relation, latest_has_session = continuations[-1]
        continue_with = (
            f"delegate followup {latest_alias}"
            if latest_has_session
            else f"delegate resume {latest_alias} (it recorded no native session)"
        )
        notes.append(
            f"{alias} was already continued by {latest_alias} ({relation}). This followup "
            f"resumes {alias}'s own session and will not see {latest_alias}'s work; to continue "
            f"from there use: {continue_with}."
        )

    model = (
        _manifest_str(manifest, "requestedModel")
        or _manifest_str(manifest, "model")
        or _manifest_str(manifest, "resolvedModel")
    )
    model_alias = _manifest_str(manifest, "modelAlias")
    reasoning_effort = _manifest_str(manifest, "requestedReasoningEffort") or _manifest_str(
        manifest, "resolvedReasoningEffort"
    )
    fast = (
        manifest.get("requestedFast") if isinstance(manifest.get("requestedFast"), bool) else None
    )
    recorded_continuity = manifest.get("continuityMode")
    if recorded_continuity is None:
        continuity_mode = DEFAULT_CONTINUITY_MODE
    elif isinstance(recorded_continuity, str) and recorded_continuity in CONTINUITY_MODES:
        continuity_mode = recorded_continuity
    else:
        raise _record_invalid(
            "continuityMode in the source manifest must be pinned, fungible, or panel."
        )

    timeout = opts.timeout
    if timeout is None:
        manifest_timeout = manifest.get("timeoutSeconds")
        if (
            isinstance(manifest_timeout, int)
            and not isinstance(manifest_timeout, bool)
            and manifest_timeout > 0
        ):
            timeout = manifest_timeout
    # Only an explicit --stall-minutes is recorded; absence re-resolves config.
    stall_minutes = stall_watchdog.stall_minutes_value(manifest.get("stallMinutes"))

    group = global_options.group or _manifest_str(manifest, "group")
    auth_profile = global_options.auth_profile or _manifest_str(manifest, "authProfile")

    forbid_commit = False
    commit_policy = manifest.get("commitPolicy")
    if isinstance(commit_policy, dict) and commit_policy.get("forbidCommit") is True:
        forbid_commit = True

    attach: JsonObject | None = None
    isolation: str | None = None
    workspace_env: dict[str, str] | None = None
    persistent_source = worktree_records._is_persistent_worktree_run(
        source_state,
        manifest,
        source_snapshot,
    )
    attached_source = isinstance(manifest.get("worktreeAttachment"), dict)
    worktree_source = persistent_source or attached_source
    if worktree_source:
        attach = (
            _attachment_owner_target(registry_root, manifest)
            if attached_source
            else _validate_attach_target(
                registry_root,
                run_id,
                manifest,
                source_state,
                source_snapshot,
            )
        )
        isolation = "none"
        workspace_env = recorded_workspace_env(run_path, manifest, verb="followup", notes=notes)
    else:
        isolation_mode = _manifest_str(manifest, "isolationMode")
        if isolation_mode in ("auto", "none", "worktree"):
            isolation = isolation_mode

    launch = LaunchOptions(
        engine=source_engine,
        mode=mode,
        model_alias=model_alias,
        prompt_parts=list(opts.prompt_parts),
        prompt_file=opts.prompt_file,
        reasoning_effort=reasoning_effort,
        fast=fast,
        timeout=timeout,
        stall_minutes=stall_minutes,
        dry_run=opts.dry_run,
        model=model,
        resumable=True,
        resume_session_id=session_id,
        continuity_mode=continuity_mode,
        warnings=opts.warnings,
        workspace_env=workspace_env,
        workspace_env_recorded=workspace_env is not None,
    )
    synthetic = ParsedCommand(
        source_engine,
        global_options=GlobalOptions(
            json_mode=global_options.json_mode,
            cwd=workspace.path,
            pass_through=False,
            completion_report=global_options.completion_report,
            isolation=isolation,
            auth_profile=auth_profile,
            auth_profile_inherited=auth_profile is not None and global_options.auth_profile is None,
            group=group,
            notify=global_options.notify,
        ),
        payload=launch,
    )

    for note in notes:
        print(f"followup note: {note}", file=stderr)

    return FollowupPlan(
        parsed=synthetic,
        original_run_id=run_id,
        original_alias=alias,
        session_id=session_id,
        attach=attach,
        forbid_commit=forbid_commit,
        notes=tuple(notes),
    )


def apply_followup_to_request(request: Request, plan: FollowupPlan) -> Request:
    """Stamp followup metadata and the attach execution context onto the Request."""
    updated = replace(
        request,
        followup_of=plan.original_run_id,
        resumable=True,
        resume_session_id=plan.session_id,
    )
    if plan.forbid_commit:
        updated = replace(updated, forbid_commit=True, persistent_worktree_notes_framed=False)
    if plan.attach is not None:
        attach = plan.attach
        source_git_root = attach.get("sourceGitRoot")
        updated = replace(
            updated,
            isolation_context=IsolationContext.attached(
                request.workspace,
                branch=str(attach.get("branch") or ""),
                execution_cwd=str(attach.get("path") or ""),
                source_git_root=str(source_git_root) if isinstance(source_git_root, str) else None,
                attachment={
                    "sourceRunId": attach.get("sourceRunId"),
                    "sourceAlias": attach.get("sourceAlias"),
                    "path": attach.get("path"),
                },
            ),
            persistent_worktree_notes_framed=False,
        )
    return updated
