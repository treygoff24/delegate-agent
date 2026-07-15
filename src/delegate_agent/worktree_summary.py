from __future__ import annotations

import re

from delegate_agent.git_utils import (
    GIT_QUICK_TIMEOUT_SECONDS,
    git_stdout_or_warn,
    rev_parse_verify,
    run_git,
)
from delegate_agent.json_types import JsonObject

MAX_CHANGED_FILES_REPORTED = 50
MAX_COMMITS_REPORTED = 20


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _git_stdout(cwd: str, args: list[str], warnings: list[str]) -> str | None:
    return git_stdout_or_warn(
        cwd,
        args,
        warnings=warnings,
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        git_runner=run_git,
    )


def _parse_porcelain_line(line: str) -> JsonObject:
    path = line[3:] if len(line) > 3 else ""
    entry: JsonObject = {
        "status": line[:2] if len(line) >= 2 else line,
        "path": path,
    }
    if " -> " in path:
        old_path, new_path = path.split(" -> ", 1)
        entry["path"] = new_path
        entry["oldPath"] = old_path
    return entry


def _changed_files(execution_cwd: str, warnings: list[str]) -> tuple[list[JsonObject], int]:
    stdout = _git_stdout(
        execution_cwd,
        ["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"],
        warnings,
    )
    if stdout is None:
        return [], 0
    lines = stdout.splitlines()
    return changed_files_from_porcelain_lines(lines)


def changed_files_from_porcelain_lines(
    lines: list[str],
    total: int | None = None,
) -> tuple[list[JsonObject], int]:
    return (
        [_parse_porcelain_line(line) for line in lines[:MAX_CHANGED_FILES_REPORTED]],
        len(lines) if total is None else total,
    )


_SHORTSTAT_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed")
_SHORTSTAT_INSERTIONS_RE = re.compile(r"(\d+)\s+insertions?\(\+\)")
_SHORTSTAT_DELETIONS_RE = re.compile(r"(\d+)\s+deletions?\(-\)")


def _parse_shortstat(raw: str) -> JsonObject:
    payload: JsonObject = {"raw": raw}
    files = _SHORTSTAT_FILES_RE.search(raw)
    insertions = _SHORTSTAT_INSERTIONS_RE.search(raw)
    deletions = _SHORTSTAT_DELETIONS_RE.search(raw)
    if files is not None:
        payload["filesChanged"] = int(files.group(1))
    if insertions is not None:
        payload["insertions"] = int(insertions.group(1))
    if deletions is not None:
        payload["deletions"] = int(deletions.group(1))
    return payload


def _diff_shortstat(execution_cwd: str, rev: str, warnings: list[str]) -> JsonObject:
    stdout = _git_stdout(execution_cwd, ["diff", "--shortstat", rev], warnings)
    return _parse_shortstat(stdout or "")


def _rev_parse(cwd: str, rev: str, warnings: list[str]) -> str | None:
    return rev_parse_verify(
        cwd,
        rev,
        warnings=warnings,
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        git_runner=run_git,
    )


def _rev_list_count(cwd: str, rev_range: str, warnings: list[str]) -> int | None:
    stdout = _git_stdout(cwd, ["rev-list", "--count", rev_range], warnings)
    if stdout is None:
        return None
    try:
        return int(stdout.strip())
    except ValueError:
        warnings.append(f"git rev-list returned non-integer count for {rev_range!r}: {stdout}")
        return None


def _ahead_behind(cwd: str, left: str, right: str, warnings: list[str]) -> JsonObject | None:
    stdout = _git_stdout(
        cwd, ["rev-list", "--left-right", "--count", f"{left}...{right}"], warnings
    )
    if stdout is None:
        return None
    parts = stdout.split()
    if len(parts) != 2:
        warnings.append(f"git rev-list returned unexpected ahead/behind output: {stdout}")
        return None
    try:
        return {"behind": int(parts[0]), "ahead": int(parts[1])}
    except ValueError:
        warnings.append(f"git rev-list returned non-integer ahead/behind output: {stdout}")
        return None


def _commits_created(execution_cwd: str, base: str, warnings: list[str]) -> list[JsonObject] | None:
    stdout = _git_stdout(
        execution_cwd,
        [
            "log",
            f"--max-count={MAX_COMMITS_REPORTED}",
            "--reverse",
            "--format=%H%x01%h%x01%s",
            f"{base}..HEAD",
        ],
        warnings,
    )
    if stdout is None:
        return None
    commits: list[JsonObject] = []
    for line in stdout.splitlines():
        parts = line.split("\x01", 2)
        if len(parts) != 3:
            continue
        oid, short_oid, subject = parts
        commits.append({"oid": oid, "shortOid": short_oid, "subject": subject})
    return commits


def build_work_summary(
    *,
    source_git_root: str | None,
    execution_cwd: str,
    branch: str | None,
    creation_context: JsonObject | None,
    prefetched_changed_files: tuple[list[JsonObject], int] | None = None,
) -> JsonObject | None:
    """Return a compact objective summary for a persistent worktree run.

    The summary is best-effort: unavailable Git metadata is reported in
    ``warnings`` instead of making launch finalization fail.
    """

    if not source_git_root or not execution_cwd or not branch:
        return None

    warnings: list[str] = []
    creation = creation_context if isinstance(creation_context, dict) else {}
    base = _str(creation.get("sourceHeadOid"))

    if prefetched_changed_files is None:
        changed_files, changed_total = _changed_files(execution_cwd, warnings)
    else:
        changed_files, changed_total = prefetched_changed_files
    dirty = changed_total > 0
    head_commit = _rev_parse(execution_cwd, "HEAD", warnings)
    source_head = _rev_parse(source_git_root, "HEAD", warnings)

    commits_count: int | None = None
    commits: list[JsonObject] = []
    commits_fetch_ok = False
    branch_ahead_of_base: JsonObject | None = None
    diff_stat_vs_base: JsonObject | None = None
    if base is not None:
        commits_count = _rev_list_count(execution_cwd, f"{base}..HEAD", warnings)
        behind_base = _rev_list_count(execution_cwd, f"HEAD..{base}", warnings)
        if commits_count is not None and behind_base is not None:
            branch_ahead_of_base = {
                "ahead": commits_count,
                "behind": behind_base,
                "baseOid": base,
            }
        if commits_count is not None:
            commits_result = _commits_created(execution_cwd, base, warnings)
            commits_fetch_ok = commits_result is not None
            commits = commits_result or []
        diff_stat_vs_base = _diff_shortstat(execution_cwd, base, warnings)
    commit_inspection_verified = commits_count is not None

    branch_ahead_of_source = (
        _ahead_behind(execution_cwd, source_head, "HEAD", warnings) if source_head else None
    )

    summary: JsonObject = {
        "dirty": dirty,
        "changedFilesCount": changed_total,
        "changedFiles": changed_files,
        "changedFilesTruncated": changed_total > len(changed_files),
        "commitsCreatedCount": commits_count,
        "commitsCreated": commits,
        "commitsCreatedTruncated": (
            commits_count > len(commits)
            if commit_inspection_verified and commits_fetch_ok
            else False
        ),
        "commitInspectionStatus": "verified" if commit_inspection_verified else "unverified",
        "baseCommit": base,
        "headCommit": head_commit,
        "sourceHead": source_head,
        "branch": branch,
        "diffStat": _diff_shortstat(execution_cwd, "HEAD", warnings),
        "noChanges": (not dirty and commits_count == 0 if commit_inspection_verified else False),
    }
    if branch_ahead_of_base is not None:
        summary["branchAheadOfBase"] = branch_ahead_of_base
    if branch_ahead_of_source is not None:
        summary["branchAheadOfSource"] = branch_ahead_of_source
    if diff_stat_vs_base is not None:
        summary["diffStatVsBase"] = diff_stat_vs_base
    if warnings:
        summary["warnings"] = warnings
    return summary


def commits_created_count(summary: JsonObject | None) -> int | None:
    if not isinstance(summary, dict):
        return None
    count = summary.get("commitsCreatedCount")
    return count if isinstance(count, int) and count >= 0 else None
