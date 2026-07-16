from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TextIO

from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject
from delegate_agent.run_registry import parse_utc_timestamp as parse_timestamp
from delegate_agent.snapshot_view import SnapshotView


def format_age(started_at: str | None, *, now: datetime | None = None) -> str:
    start = parse_timestamp(started_at)
    if start is None:
        return "unknown"
    moment = now or datetime.now(UTC)
    delta = moment - start
    total_seconds = max(int(delta.total_seconds()), 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


SnapshotTextField = tuple[str, str]


SNAPSHOT_CONTEXT_FIELDS: tuple[SnapshotTextField, ...] = (
    ("cwd", "cwd"),
    ("executionCwd", "execution cwd"),
    ("model", "model"),
    ("mode", "mode"),
)


def _render_snapshot_string_fields(
    view: SnapshotView,
    fields: tuple[SnapshotTextField, ...],
    stdout: TextIO,
) -> None:
    for key, label in fields:
        value = view.get(key)
        if isinstance(value, str) and value:
            print(f"{label}: {value}", file=stdout)


def _render_snapshot_header(view: SnapshotView, stdout: TextIO) -> None:
    alias = view.get("alias") or view.get("runId") or "?"
    status = view.get("status") or "unknown"
    started_at = view.get("startedAt")
    age = format_age(started_at if isinstance(started_at, str) else None)
    print(f"{alias} · {status} · {age} elapsed", file=stdout)


def _render_snapshot_status_detail(view: SnapshotView, stdout: TextIO) -> None:
    raw_status = view.get("rawStatus")
    effective_status = view.get("effectiveStatus")
    stale_reason = view.get("staleReason")
    if (
        raw_status != effective_status
        and isinstance(raw_status, str)
        and isinstance(effective_status, str)
    ):
        print(f"status detail: raw={raw_status} effective={effective_status}", file=stdout)
    if isinstance(stale_reason, str) and stale_reason:
        print(f"stale reason: {stale_reason}", file=stdout)
    render_resolution_text(view, stdout)


def render_resolution_text(view: JsonObject, stdout: TextIO) -> None:
    requested = view.get("requestedHandle")
    resolved = view.get("resolvedHandle")
    kind = view.get("resolutionKind")
    if isinstance(requested, str) and isinstance(resolved, str) and isinstance(kind, str):
        print(f"resolved handle: {requested} -> {resolved} ({kind})", file=stdout)
    run_id = view.get("resolvedRunId")
    workspace = view.get("resolvedWorkspace")
    age = view.get("resolvedAge")
    if isinstance(run_id, str) and isinstance(workspace, str) and isinstance(age, str):
        print(f"resolved run: {run_id} · workspace: {workspace} · age: {age}", file=stdout)


def _render_snapshot_isolation(view: SnapshotView, stdout: TextIO) -> None:
    isolation_lifecycle = view.get("isolationLifecycle")
    if isolation_lifecycle == "persistent":
        print("isolation: worktree persistent", file=stdout)
    elif isolation_lifecycle == "temporary":
        print("isolation: worktree temporary", file=stdout)
    elif isolation_lifecycle:
        print(f"isolation: {isolation_lifecycle}", file=stdout)
    branch = view.get("branch")
    if isinstance(branch, str) and branch:
        print(f"branch: {branch}", file=stdout)
    source_git_root = view.get("sourceGitRoot")
    if isinstance(source_git_root, str) and source_git_root:
        print(f"source git root: {source_git_root}", file=stdout)
    worktree_status = view.get("worktreeStatus")
    if isinstance(worktree_status, str):
        print(f"worktree status: {worktree_status}", file=stdout)
    safe_method = view.get("safeWorkspaceMethod")
    if isinstance(safe_method, str) and safe_method:
        print(f"safe workspace method: {safe_method}", file=stdout)


def _render_snapshot_reasoning(view: SnapshotView, stdout: TextIO) -> None:
    resolved_reasoning = view.get("resolvedReasoningEffort")
    if isinstance(resolved_reasoning, str) and resolved_reasoning:
        transport = view.get("reasoningTransport")
        source = view.get("reasoningCapabilitySource")
        detail = []
        if isinstance(transport, str) and transport:
            detail.append(transport)
        if isinstance(source, str) and source:
            detail.append(f"capability={source}")
        suffix = f" ({', '.join(detail)})" if detail else ""
        print(f"reasoning effort: {resolved_reasoning}{suffix}", file=stdout)


def _render_snapshot_current(view: SnapshotView, stdout: TextIO) -> None:
    current = view.get("current")
    if isinstance(current, str) and current:
        print(f"current: {current}", file=stdout)


def _render_snapshot_cleanup(view: SnapshotView, stdout: TextIO) -> None:
    cleanup = view.get("worktreeCleanupCommands")
    if isinstance(cleanup, dict):
        render_worktree_cleanup_commands(cleanup, stdout)


def _render_snapshot_assistant_text(view: SnapshotView, stdout: TextIO) -> None:
    assistant_text = view.get("assistantText")
    if isinstance(assistant_text, str) and assistant_text:
        print("assistant text:", file=stdout)
        print(assistant_text, file=stdout)


def _render_snapshot_recent_events(view: SnapshotView, stdout: TextIO) -> None:
    recent_events = view.get("recentEvents")
    if isinstance(recent_events, list) and recent_events:
        print("recent:", file=stdout)
        for event in recent_events[-20:]:
            if not isinstance(event, dict):
                continue
            kind = event.get("kind", "event")
            tool = event.get("tool")
            path = event.get("path") or event.get("target")
            if tool and path:
                print(f"  - {kind}: {tool} {path}", file=stdout)
            elif tool:
                print(f"  - {kind}: {tool}", file=stdout)
            else:
                print(f"  - {kind}", file=stdout)


def _render_snapshot_string_list(
    view: SnapshotView,
    key: str,
    heading: str,
    stdout: TextIO,
) -> None:
    values = view.get(key)
    if isinstance(values, list) and values:
        print(f"{heading}:", file=stdout)
        for value in values:
            if isinstance(value, str):
                print(f"  - {value}", file=stdout)


def _render_snapshot_completion(view: SnapshotView, stdout: TextIO) -> None:
    completion = view.get("completionReport")
    if isinstance(completion, dict):
        command = completion.get("command")
        if isinstance(command, str):
            print(f"completion report: {command}", file=stdout)


def render_snapshot_text(view: SnapshotView, stdout: TextIO) -> None:
    _render_snapshot_header(view, stdout)
    _render_snapshot_status_detail(view, stdout)
    _render_snapshot_string_fields(view, SNAPSHOT_CONTEXT_FIELDS, stdout)
    _render_snapshot_isolation(view, stdout)
    _render_snapshot_reasoning(view, stdout)
    _render_snapshot_current(view, stdout)
    _render_snapshot_cleanup(view, stdout)
    _render_snapshot_assistant_text(view, stdout)
    _render_snapshot_recent_events(view, stdout)
    _render_snapshot_string_list(view, "warnings", "warnings", stdout)
    _render_snapshot_string_list(view, "nextActions", "next actions", stdout)
    _render_snapshot_completion(view, stdout)


def runs_json_payload(
    summaries: list[JsonObject],
    *,
    limit: int,
    mode: str,
) -> JsonObject:
    return {
        "schema": run_registry.RUNS_SCHEMA,
        "ok": True,
        "mode": mode,
        "limit": limit,
        "runs": summaries,
    }


def render_runs_text(summaries: list[JsonObject], stdout: TextIO, *, mode: str) -> None:
    print(f"mode: {mode}", file=stdout)
    show_group = any(isinstance(summary.get("group"), str) for summary in summaries)
    if show_group:
        print(
            "alias      status    harness  age      iso          group       current", file=stdout
        )
    else:
        print("alias      status    harness  age      iso          current", file=stdout)
    for summary in summaries:
        alias = summary.get("alias") or summary.get("runId") or "?"
        status = summary.get("status", "unknown")
        harness = summary.get("harness", "?")
        activity = summary.get("activityAt")
        age = format_age(activity if isinstance(activity, str) else None)
        isolation = summary.get("isolationLifecycle", "")
        if isolation == "persistent":
            iso_label = "persistent"
        elif isolation == "temporary":
            iso_label = "temporary"
        else:
            iso_label = ""
        current = summary.get("current", "")
        if isinstance(current, str) and len(current) > 40:
            current = current[:37] + "..."
        if show_group:
            group = summary.get("group") if isinstance(summary.get("group"), str) else ""
            print(
                f"{alias:<10} {status:<9} {harness:<8} {age:<8} {iso_label:<11} {group:<11} {current}",
                file=stdout,
            )
        else:
            print(
                f"{alias:<10} {status:<9} {harness:<8} {age:<8} {iso_label:<11} {current}",
                file=stdout,
            )


def run_output_json_payload(
    *,
    alias: str | None,
    run_id: str,
    sections: JsonObject,
    resolution: JsonObject | None = None,
    warnings: list[str] | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "schema": run_registry.RUN_OUTPUT_SCHEMA,
        "ok": True,
        "runId": run_id,
        "sections": sections,
    }
    if alias:
        payload["alias"] = alias
    if resolution:
        payload.update(resolution)
    if warnings:
        payload["warnings"] = warnings
    return payload


def render_worktree_cleanup_commands(cleanup: JsonObject, stdout: TextIO) -> None:
    safe_cmd = cleanup.get("safe")
    force_branch = cleanup.get("forceBranch")
    discard = cleanup.get("discardUncommitted")
    force = cleanup.get("force")
    raw_git = cleanup.get("rawGit")
    if safe_cmd:
        print(f"cleanup (refuses dirty / unmerged):       {safe_cmd}", file=stdout)
    if force_branch:
        print(f"cleanup (allow unmerged branch deletion): {force_branch}", file=stdout)
    if discard:
        print(f"cleanup (DISCARD uncommitted edits):      {discard}", file=stdout)
    if force:
        print(f"cleanup (DISCARD edits + delete branch):  {force}", file=stdout)
    if raw_git:
        print(f"raw git equivalent:                       {raw_git}", file=stdout)


def _run_output_section_header(name: str, meta: JsonObject) -> str:
    # Text mode needs in-band cues for anything JSON mode flags in section
    # metadata: a recovered (synthetic) completion report is not a child-written
    # report, and a tailed log is not the whole log.
    details: list[str] = []
    if meta.get("synthetic") is True:
        details.append("synthetic: recovered from stdout.log tail")
    tail_lines = meta.get("tailLines")
    if meta.get("truncated") is True and isinstance(tail_lines, int):
        details.append(f"last {tail_lines} lines; full log {meta.get('bytes', '?')} bytes")
    if meta.get("charTruncated") is True:
        returned = meta.get("returnedChars", "?")
        omitted = meta.get("omittedChars", "?")
        details.append(f"last {returned} chars; {omitted} omitted")
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"{name}{suffix}"


def render_run_output_text(
    sections: dict[str, str],
    stdout: TextIO,
    *,
    section_meta: JsonObject | None = None,
    resolution: JsonObject | None = None,
    warnings: list[str] | None = None,
) -> None:
    if resolution:
        render_resolution_text(resolution, stdout)
    if warnings:
        print("warnings:", file=stdout)
        for warning in warnings:
            print(f"  - {warning}", file=stdout)
    for name in ("completionReport", "stdout", "stderr", "diagnostics"):
        content = sections.get(name)
        if not content:
            continue
        meta = (section_meta or {}).get(name)
        header = _run_output_section_header(name, meta if isinstance(meta, dict) else {})
        print(f"=== {header} ===", file=stdout)
        print(content, end="" if content.endswith("\n") else "\n", file=stdout)


def _render_tri_state_flag(label: str, value: object, stdout: TextIO) -> None:
    if value is True:
        print(f"{label}: yes", file=stdout)
    elif value is False:
        print(f"{label}: no", file=stdout)
    else:
        print(f"{label}: unknown", file=stdout)


def render_worktree_list_text(payload: JsonObject, stdout: TextIO) -> None:
    entries = payload.get("entries")
    print(
        "alias        status   harness  age      branch                                      dirty branch-merged integrated",
        file=stdout,
    )
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            alias = entry.get("alias") or entry.get("runId") or "?"
            status = entry.get("worktreeStatus") or "unknown"
            harness = entry.get("harness") or "?"
            age = format_age(
                entry.get("lastActivityAt")
                if isinstance(entry.get("lastActivityAt"), str)
                else None
            )
            branch = entry.get("branch") or "-"
            branch_label = str(branch)
            if len(branch_label) > 42:
                branch_label = branch_label[:39] + "..."
            dirty_value = entry.get("dirty")
            dirty = "yes" if dirty_value is True else "no" if dirty_value is False else "-"
            branch_merged_value = entry.get("branchMergedIntoSource")
            branch_merged = (
                "yes"
                if branch_merged_value is True
                else "no"
                if branch_merged_value is False
                else "-"
            )
            integrated_value = entry.get("fullyIntegrated")
            integrated = (
                "yes" if integrated_value is True else "no" if integrated_value is False else "-"
            )
            print(
                f"{alias!s:<12} {status!s:<8} {harness!s:<8} {age:<8} {branch_label:<43} {dirty:<5} {branch_merged:<13} {integrated}",
                file=stdout,
            )
    auto_prune = payload.get("autoPrune")
    if isinstance(auto_prune, dict):
        if auto_prune.get("skipped") is True:
            reason = auto_prune.get("reason") or "unknown"
            print(f"auto-prune: skipped ({reason})", file=stdout)
        elif auto_prune.get("ok") is False:
            code = auto_prune.get("code") or "failed"
            errors = auto_prune.get("errors")
            error_count = len(errors) if isinstance(errors, list) else 0
            suffix = f", errors={error_count}" if error_count else ""
            print(f"auto-prune: failed ({code}{suffix})", file=stdout)
        else:
            removed = auto_prune.get("removed")
            skipped = auto_prune.get("skipped")
            errors = auto_prune.get("errors")
            removed_count = len(removed) if isinstance(removed, list) else 0
            skipped_count = len(skipped) if isinstance(skipped, list) else 0
            error_count = len(errors) if isinstance(errors, list) else 0
            print(
                f"auto-prune: removed {removed_count}, skipped {skipped_count}, errors {error_count}",
                file=stdout,
            )


def _short_ref(ref: str | None) -> str:
    if ref is None:
        return "(detached)"
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return ref


def _short_oid(oid: str | None) -> str | None:
    if not isinstance(oid, str) or not oid:
        return None
    return oid[:7]


def render_worktree_show_text(payload: JsonObject, stdout: TextIO) -> None:
    alias = payload.get("alias") or payload.get("runId") or "?"
    status = payload.get("worktreeStatus", "unknown")
    print(f"{alias} · {status}", file=stdout)
    requested = payload.get("requestedHandle")
    resolved = payload.get("resolvedHandle")
    kind = payload.get("resolutionKind")
    if isinstance(requested, str) and isinstance(resolved, str) and isinstance(kind, str):
        print(f"resolved handle: {requested} -> {resolved} ({kind})", file=stdout)

    # Creation-context line: created from <ref>@<oid>; source now at <ref>@<oid>
    creation = payload.get("creationContext")
    if isinstance(creation, dict):
        src_ref = _short_ref(creation.get("sourceHeadRef"))
        src_oid = _short_oid(creation.get("sourceHeadOid"))
        if src_oid is not None:
            # Current source HEAD: ref from currentSourceHeadRef (re-read by show_worktree),
            # oid from vsCurrentHead.baseOid (computed by ahead_behind).
            current_ref = _short_ref(payload.get("currentSourceHeadRef"))
            ahead = payload.get("aheadBehind")
            current_oid: str | None = None
            if isinstance(ahead, dict):
                vs_current = ahead.get("vsCurrentHead")
                if isinstance(vs_current, dict):
                    current_oid = _short_oid(vs_current.get("baseOid"))
            if current_oid is None:
                current_oid = "(unknown)"
            print(
                f"created from {src_ref}@{src_oid}; source now at {current_ref}@{current_oid}",
                file=stdout,
            )

    # Dirty flag (tri-state: yes / no / unknown)
    _render_tri_state_flag("dirty", payload.get("dirty"), stdout)

    # Backward-compatible branch merge flag, followed by more explicit integration fields.
    _render_tri_state_flag("merged", payload.get("mergedIntoSource"), stdout)
    _render_tri_state_flag("branch merged", payload.get("branchMergedIntoSource"), stdout)
    _render_tri_state_flag("fully integrated", payload.get("fullyIntegrated"), stdout)
    integration_status = payload.get("integrationStatus")
    if isinstance(integration_status, str) and integration_status:
        print(f"integration status: {integration_status}", file=stdout)

    ahead = payload.get("aheadBehind")
    if isinstance(ahead, dict):
        for key, label in (
            ("vsCreationBase", "vs creation base"),
            ("vsCurrentHead", "vs current HEAD"),
        ):
            pair = ahead.get(key)
            if isinstance(pair, dict):
                print(
                    f"{label}: ahead {pair.get('ahead')} / behind {pair.get('behind')} ({pair.get('baseOid')})",
                    file=stdout,
                )
    porcelain = payload.get("porcelainStatus")
    if isinstance(porcelain, list) and porcelain:
        print("status:", file=stdout)
        for line in porcelain:
            print(f"  {line}", file=stdout)
        if payload.get("porcelainStatusTruncated"):
            print("  ...", file=stdout)
    elif isinstance(porcelain, list):
        # Empty porcelainStatus means the worktree is clean.
        print("porcelain: clean", file=stdout)
    commands = payload.get("suggestedCommands")
    if isinstance(commands, dict):
        print("suggested commands:", file=stdout)
        for key, value in commands.items():
            if isinstance(value, str) and value:
                print(f"  {key}: {value}", file=stdout)

    # Trailing metadata block (spec L621: rendered after suggested-commands).
    for key, label in (
        ("executionCwd", "execution"),
        ("sourceGitRoot", "source"),
        ("branch", "branch"),
        ("harness", "harness"),
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            print(f"{label}: {value}", file=stdout)

    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("warnings:", file=stdout)
        for warning in warnings:
            print(f"  - {warning}", file=stdout)


def render_worktree_remove_text(payload: JsonObject, stdout: TextIO) -> None:
    if isinstance(payload.get("removed"), list):
        print(
            f"group: {payload.get('group')} matched={payload.get('matched')} removed={len(payload.get('removed') or [])} errors={len(payload.get('errors') or [])}",
            file=stdout,
        )
        return
    alias = payload.get("alias") or payload.get("runId") or "?"
    print(
        f"{alias}: removed={payload.get('removed')} pathRemoved={payload.get('pathRemoved')} branchRemoved={payload.get('branchRemoved')}",
        file=stdout,
    )
    if payload.get("ok") is False or payload.get("branchRemovalError"):
        error = payload.get("branchRemovalError") or payload.get("message") or payload.get("code")
        if error:
            print(f"error: {error}", file=stdout)
    if payload.get("branchKept"):
        print(f"branch kept: {payload['branchKept']}", file=stdout)
    if payload.get("noop"):
        print("noop: already removed", file=stdout)
    actions = payload.get("nextActions")
    if isinstance(actions, list) and actions:
        print("next actions:", file=stdout)
        for action in actions:
            print(f"  - {action}", file=stdout)


def render_worktree_prune_text(payload: JsonObject, stdout: TextIO) -> None:
    for section in ("planned", "removed", "skipped", "errors"):
        items = payload.get(section)
        count = len(items) if isinstance(items, list) else 0
        print(f"{section}: {count}", file=stdout)
        if isinstance(items, list):
            for item in items[:20]:
                if isinstance(item, dict):
                    label = item.get("alias") or item.get("runId") or "?"
                    detail = (
                        item.get("reason") or item.get("code") or item.get("worktreeStatus") or ""
                    )
                    print(f"  - {label} {detail}", file=stdout)


def render_worktree_gc_text(payload: JsonObject, stdout: TextIO) -> None:
    print(f"reconciled: {payload.get('reconciled', 0)}", file=stdout)
    print(f"pruned source roots: {payload.get('prunedSourceRoots', 0)}", file=stdout)
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("warnings:", file=stdout)
        for warning in warnings:
            if isinstance(warning, dict):
                print(f"  - {warning.get('sourceGitRoot')}: {warning.get('message')}", file=stdout)
            else:
                print(f"  - {warning}", file=stdout)
    orphans = payload.get("orphans")
    if isinstance(orphans, list) and orphans:
        print("orphans:", file=stdout)
        for orphan in orphans:
            if isinstance(orphan, dict):
                print(
                    f"  - {orphan.get('alias') or orphan.get('runId')} {orphan.get('reason')}",
                    file=stdout,
                )


def print_json(payload: JsonObject, stdout: TextIO) -> None:
    print(json.dumps(payload, sort_keys=True), file=stdout)
