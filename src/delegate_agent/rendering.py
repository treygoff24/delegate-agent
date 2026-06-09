from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from delegate_agent import retention as delegate_retention
from delegate_agent import run_registry
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.run_registry import parse_utc_timestamp as parse_timestamp

REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization header value, quoted or bare. The optional quote after the key
    # tolerates JSON ({"Authorization": "..."}); the value is bounded on the right
    # so we don't swallow trailing structure (closing quote/brace/&/,).
    (
        re.compile(
            r"(?i)\b(authorization[\"']?\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,&}\"'\r\n][^\r\n,&}]*)"
        ),
        r"\1***",
    ),
    # Bare scheme token not behind an Authorization key (e.g. "Bearer eyJ...").
    (
        re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1***",
    ),
    # Bracketed environment assignments, e.g. os.environ["OPENAI_API_KEY"] = "..."
    # and env['DB_PASSWORD']='...'. Keep this before the generic key matcher: the
    # separator between the secret key and value is outside the bracketed lookup.
    (
        re.compile(
            r"(?i)\b((?:os\.environ|env)\[\s*[\"'][^\"'\]\r\n]*"
            r"(?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key|private[_-]?key|"
            r"access[_-]?token|refresh[_-]?token|auth[_-]?token|authtoken|"
            r"client[_-]?secret|password|passwd|secret|token)"
            r"[^\"'\]\r\n]*[\"']\s*\]\s*=\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,&};\"'\r\n][^\r\n,&};]*)"
        ),
        r"\1***",
    ),
    # Named credential keys with the value quoted, bare, or JSON-quoted. The left
    # edge anchors on a non-alphanumeric character (or string start) rather than
    # \b, so env-style prefixes joined by "_" still redact (OPENAI_API_KEY=,
    # DB_PASSWORD=, aws_secret_access_key=). Separator is preserved so the shape
    # stays readable.
    (
        re.compile(
            r"(?i)(?:(?<=[^A-Za-z0-9])|^)("
            r"api[_-]?key|apikey|access[_-]?key|secret[_-]?key|private[_-]?key|"
            r"access[_-]?token|refresh[_-]?token|auth[_-]?token|authtoken|"
            r"client[_-]?secret|password|passwd|secret|token"
            r")([\"']?\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,&};\"'\r\n][^\r\n,&};]*)"
        ),
        r"\1\2***",
    ),
    # Password embedded in a connection string: scheme://[user]:PASS@host. The
    # scheme length is bounded so a long dotted string cannot backtrack quadratically.
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,40}://[^\s:/@]*:)[^\s:/@]+(@)"),
        r"\1***\2",
    ),
    # JWTs are anchored on the eyJ header (base64url of '{"') so this does not shred
    # ordinary dotted identifiers and tracebacks the parent agent needs to read.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "***",
    ),
    # PEM private key blocks are handled by _redact_pem_blocks() before this list
    # runs. A DOTALL regex here is both easier to make backtracking-prone and can
    # let keyed values like SECRET_KEY=<PEM> leak the body after the first newline.
    # Provider token shapes are prefix-anchored; we deliberately avoid a blanket
    # high-entropy matcher, which would redact legitimate hashes/IDs/output.
    (re.compile(r"\bsk-(?:proj|ant|svcacct)-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{8,}"), "sk-***"),
    (re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"), "gh***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_***"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1***"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{32,}\b"), "AIza***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "xox***"),
    # Stripe secret/restricted keys (live or test); publishable pk_ keys are
    # public by design and intentionally excluded.
    (re.compile(r"\b([sr]k_(?:live|test)_)[0-9A-Za-z]{10,}\b"), r"\1***"),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{10,}\b"), "whsec_***"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b"), "npm_***"),
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"), "SG.***"),
    (
        re.compile(
            r"https://hooks\.slack(?:-gov)?\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"
        ),
        "***",
    ),
]


PEM_BLOCK_PLACEHOLDER = "***PRIVATE KEY REDACTED***"
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _redact_pem_blocks(value: str) -> str:
    match = _PEM_BEGIN.search(value)
    if match is None:
        return value
    parts: list[str] = []
    pos = 0
    while match is not None:
        parts.append(value[pos : match.start()])
        end = _PEM_END.search(value, match.end())
        parts.append(PEM_BLOCK_PLACEHOLDER)
        if end is None:
            return "".join(parts)
        pos = end.end()
        match = _PEM_BEGIN.search(value, pos)
    parts.append(value[pos:])
    return "".join(parts)


def redact_string(value: str) -> str:
    redacted = _redact_pem_blocks(value)
    for pattern, replacement in REDACT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_value(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


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


def merge_snapshot_view(
    registry_root: Path,
    run_id: str,
    snapshot: JsonObject | None,
    *,
    redact: bool,
) -> JsonObject:
    state = run_registry.load_run_state(registry_root, run_id)
    manifest = run_registry.load_run_manifest(registry_root, run_id)
    stdout_bytes, stderr_bytes = delegate_retention.effective_log_byte_sizes(registry_root, run_id)
    view: JsonObject = dict(snapshot or {})
    if not view:
        view = {
            "schema": run_registry.SNAPSHOT_SCHEMA,
            "ok": True,
            "runId": run_id,
        }
    view.setdefault("ok", True)
    view["runId"] = run_id
    view["status"] = run_registry.effective_status(state)
    view["stdoutBytes"] = stdout_bytes
    view["stderrBytes"] = stderr_bytes
    if state:
        for key in ("lastActivityAt", "current", "exitCode", "finishedAt"):
            if key in state and key not in view:
                view[key] = state[key]
        # Surface pre-launch failure fields when status is "failed".
        if state.get("status") == "failed":
            for key in ("error", "message", "plannedBranch", "plannedExecutionCwd"):
                if key in state and key not in view:
                    view[key] = state[key]
        # Surface isolation metadata from state when present.
        for key in ("worktreeStatus", "safeWorkspaceMethod"):
            if key in state and key not in view:
                view[key] = state[key]
    if manifest:
        for key in ("alias", "harness", "cwd", "executionCwd", "mode", "model", "startedAt"):
            if key in manifest and key not in view:
                view[key] = manifest[key]
        for key in (
            "isolationMode",
            "effectiveIsolation",
            "isolationLifecycle",
            "preservedWorkspace",
            "sourceGitRoot",
            "branch",
            "worktreeStatus",
            "worktreeCleanupCommands",
            "safeWorkspaceMethod",
            "requestedReasoningEffort",
            "resolvedReasoningEffort",
            "reasoningEffortSource",
            "reasoningCapabilitySource",
            "reasoningTransport",
        ):
            if key in manifest and key not in view:
                view[key] = manifest[key]
    warnings = list(view.get("warnings") or [])
    for source in (state, manifest):
        if not source:
            continue
        source_warnings = source.get("warnings")
        if isinstance(source_warnings, list):
            for warning in source_warnings:
                if isinstance(warning, str) and warning not in warnings:
                    warnings.append(warning)
    for warning in run_registry.large_log_warnings(stdout_bytes, stderr_bytes):
        if warning not in warnings:
            warnings.append(warning)
    alias = view.get("alias")
    if delegate_retention.raw_logs_archived(registry_root, run_id):
        archive_warning = delegate_retention.archived_log_warning(
            alias if isinstance(alias, str) else None,
            run_id,
        )
        if archive_warning not in warnings:
            warnings.append(archive_warning)
    if warnings:
        view["warnings"] = warnings
    if isinstance(alias, str):
        view.setdefault("snapshotCommand", run_registry.snapshot_command(alias))
        if "completionReport" in view and isinstance(view["completionReport"], dict):
            view["completionReport"].setdefault(
                "command",
                run_registry.run_output_command(alias, completion_report=True),
            )
    if redact:
        view = redact_value(view)
    return view


def snapshot_json_payload(view: JsonObject) -> JsonObject:
    return view


def render_snapshot_text(view: JsonObject, stdout: TextIO) -> None:
    alias = view.get("alias", view.get("runId", "?"))
    status = view.get("status", "unknown")
    started_at = view.get("startedAt")
    age = format_age(started_at if isinstance(started_at, str) else None)
    print(f"{alias} · {status} · {age} elapsed", file=stdout)
    for key, label in (
        ("cwd", "cwd"),
        ("executionCwd", "execution cwd"),
        ("model", "model"),
        ("mode", "mode"),
    ):
        value = view.get(key)
        if isinstance(value, str) and value:
            print(f"{label}: {value}", file=stdout)
    # Isolation metadata
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

    current = view.get("current")
    if isinstance(current, str) and current:
        print(f"current: {current}", file=stdout)

    cleanup = view.get("worktreeCleanupCommands")
    if isinstance(cleanup, dict):
        render_worktree_cleanup_commands(cleanup, stdout)

    assistant_text = view.get("assistantText")
    if isinstance(assistant_text, str) and assistant_text:
        print("assistant text:", file=stdout)
        print(assistant_text, file=stdout)
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
    warnings = view.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("warnings:", file=stdout)
        for warning in warnings:
            if isinstance(warning, str):
                print(f"  - {warning}", file=stdout)
    completion = view.get("completionReport")
    if isinstance(completion, dict):
        command = completion.get("command")
        if isinstance(command, str):
            print(f"completion report: {command}", file=stdout)


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
        print(
            f"{alias:<10} {status:<9} {harness:<8} {age:<8} {iso_label:<11} {current}", file=stdout
        )


def run_output_json_payload(
    *,
    alias: str | None,
    run_id: str,
    sections: JsonObject,
) -> JsonObject:
    payload: JsonObject = {
        "schema": run_registry.RUN_OUTPUT_SCHEMA,
        "ok": True,
        "runId": run_id,
        "sections": sections,
    }
    if alias:
        payload["alias"] = alias
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


def render_run_output_text(sections: dict[str, str], stdout: TextIO) -> None:
    for name in ("completionReport", "stdout", "stderr"):
        content = sections.get(name)
        if not content:
            continue
        print(f"=== {name} ===", file=stdout)
        print(content, end="" if content.endswith("\n") else "\n", file=stdout)


def render_worktree_list_text(payload: JsonObject, stdout: TextIO) -> None:
    entries = payload.get("entries")
    print(
        "alias        status   harness  age      branch                                      dirty merged",
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
            merged_value = entry.get("mergedIntoSource")
            merged = "yes" if merged_value is True else "no" if merged_value is False else "-"
            print(
                f"{alias!s:<12} {status!s:<8} {harness!s:<8} {age:<8} {branch_label:<43} {dirty:<5} {merged}",
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
    dirty_value = payload.get("dirty")
    if dirty_value is True:
        print("dirty: yes", file=stdout)
    elif dirty_value is False:
        print("dirty: no", file=stdout)
    else:
        print("dirty: unknown", file=stdout)

    # Merged flag (tri-state: yes / no / unknown)
    merged_value = payload.get("mergedIntoSource")
    if merged_value is True:
        print("merged: yes", file=stdout)
    elif merged_value is False:
        print("merged: no", file=stdout)
    else:
        print("merged: unknown", file=stdout)

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
