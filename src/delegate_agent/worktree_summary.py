from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from pathlib import Path

from delegate_agent.git_utils import (
    GIT_QUICK_TIMEOUT_SECONDS,
    git_stdout_or_warn,
    rev_parse_verify,
    run_git,
)
from delegate_agent.json_types import JsonObject
from delegate_agent.worktree_records import (
    SYNCED_FILE_DIGESTS_KEY,
    file_content_digest,
)

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


def _changed_files(execution_cwd: str, warnings: list[str]) -> tuple[list[JsonObject], int] | None:
    """Every porcelain entry, or None when ``git status`` itself could not run.

    None is not "no changes": a failed status reads the same as a clean tree
    unless the caller keeps the two apart.
    """
    # ``--untracked-files=all``: the launch-seeded digests are per file (the
    # sync lists untracked files with ``git ls-files --others``), so a collapsed
    # ``?? seeded-dir/`` entry could never match the seeded filter and counted as
    # a change the child never made -- a quiet work run then reported success on
    # phantom changes, and a structured child with unparseable output was
    # refused as changed rather than relaunched.
    stdout = _git_stdout(
        execution_cwd,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
        warnings,
    )
    if stdout is None:
        return None
    lines = stdout.splitlines()
    return all_changed_files_from_porcelain_lines(lines)


def changed_files_from_porcelain_lines(
    lines: list[str],
    total: int | None = None,
) -> tuple[list[JsonObject], int]:
    return (
        [_parse_porcelain_line(line) for line in lines[:MAX_CHANGED_FILES_REPORTED]],
        len(lines) if total is None else total,
    )


def all_changed_files_from_porcelain_lines(
    lines: list[str],
    total: int | None = None,
) -> tuple[list[JsonObject], int]:
    """Parse every porcelain entry before any display/reporting cap is applied."""

    return [_parse_porcelain_line(line) for line in lines], len(lines) if total is None else total


def effective_changed_files_from_porcelain_lines(
    lines: list[str],
    *,
    execution_cwd: str,
    creation_context: JsonObject | None,
    total: int | None = None,
    ignore_globs: tuple[str, ...] = (),
) -> tuple[list[JsonObject], int, int]:
    """Filter launch-seeded paths whose content is unchanged at completion.

    ``git status`` alone cannot distinguish source dirt copied into a worktree
    from edits made by the child. A digest captured immediately after sync is
    the independent baseline; a seeded path is still dirty when its current
    content differs from that baseline (or when the baseline is unavailable).
    Returns ``(effective_entries, effective_total, raw_total)``.
    """

    raw_entries, raw_total = all_changed_files_from_porcelain_lines(lines, total)
    return effective_changed_files(
        raw_entries,
        execution_cwd=execution_cwd,
        creation_context=creation_context,
        raw_total=raw_total,
        ignore_globs=ignore_globs,
    )


def effective_changed_files(
    raw_entries: list[JsonObject],
    *,
    execution_cwd: str,
    creation_context: JsonObject | None,
    raw_total: int,
    ignore_globs: tuple[str, ...] = (),
) -> tuple[list[JsonObject], int, int]:
    """Apply seeded-content filtering to already parsed status entries."""

    creation = creation_context if isinstance(creation_context, dict) else {}
    digests = creation.get(SYNCED_FILE_DIGESTS_KEY)
    seeded = digests if isinstance(digests, dict) else {}

    effective: list[JsonObject] = []
    for entry in raw_entries:
        # ``oldPath`` is considered as well; a rename/deletion of a seeded path
        # is real dirt because its content no longer exists there.
        candidate_paths = _entry_paths(entry)
        if matches_ignore_globs(candidate_paths, ignore_globs):
            continue
        if not is_seeded_unchanged(candidate_paths, execution_cwd=execution_cwd, seeded=seeded):
            effective.append(entry)
    return effective[:MAX_CHANGED_FILES_REPORTED], len(effective), raw_total


def _entry_paths(entry: JsonObject) -> list[object]:
    paths: list[object] = [entry.get("path")]
    old_path = entry.get("oldPath")
    if isinstance(old_path, str):
        paths.append(old_path)
    return paths


def matches_ignore_globs(paths: Sequence[object], ignore_globs: Sequence[str]) -> bool:
    """True when every path is covered by an ignore glob (the discounted ledgers)."""

    return bool(ignore_globs) and all(
        isinstance(path, str) and any(fnmatch.fnmatch(path, pattern) for pattern in ignore_globs)
        for path in paths
    )


def is_seeded_unchanged(
    paths: Sequence[object],
    *,
    execution_cwd: str,
    seeded: JsonObject,
) -> bool:
    """True when every path is a launch-seeded file still holding its launch content."""

    for candidate in paths:
        if not isinstance(candidate, str) or candidate not in seeded:
            return False
        baseline = seeded.get(candidate)
        current = file_content_digest(Path(execution_cwd), candidate)
        if not isinstance(baseline, str) or current != baseline:
            return False
    return True


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


def _rev_list_count(cwd: str, revs: Sequence[str], warnings: list[str]) -> int | None:
    argv = ["rev-list", "--count", *revs]
    stdout = _git_stdout(cwd, argv, warnings)
    if stdout is None:
        return None
    try:
        return int(stdout.strip())
    except ValueError:
        warnings.append(f"git rev-list returned non-integer count for {' '.join(revs)!r}: {stdout}")
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

    # A caller-supplied prefetch is porcelain output that already ran; only this
    # function's own ``git status`` can fail here.
    file_inspection_verified = True
    if prefetched_changed_files is None:
        inspected = _changed_files(execution_cwd, warnings)
        file_inspection_verified = inspected is not None
        changed_files, changed_total = inspected if inspected is not None else ([], 0)
    else:
        changed_files, changed_total = prefetched_changed_files
    effective_files, effective_total, raw_total = effective_changed_files(
        changed_files,
        execution_cwd=execution_cwd,
        creation_context=creation,
        raw_total=changed_total,
    )
    # When the caller supplied a truncated prefetch, retain the raw total for
    # reporting but do not claim that all unseen paths were seeded-clean. Normal
    # callers pass every porcelain entry; the cap is applied only to the
    # effective list returned above.
    if raw_total > len(changed_files):
        effective_total = max(effective_total, raw_total - len(changed_files))
    dirty = effective_total > 0
    head_commit = _rev_parse(execution_cwd, "HEAD", warnings)
    source_head = _rev_parse(source_git_root, "HEAD", warnings)

    commits_count: int | None = None
    commits: list[JsonObject] = []
    commits_fetch_ok = False
    branch_ahead_of_base: JsonObject | None = None
    diff_stat_vs_base: JsonObject | None = None
    if base is not None:
        commits_count = _rev_list_count(execution_cwd, [f"{base}..HEAD"], warnings)
        behind_base = _rev_list_count(execution_cwd, [f"HEAD..{base}"], warnings)
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

    # Work that landed on the source checkout after this lane was dispatched,
    # measured from the dispatch point rather than from the creation base. With
    # `--base <older commit>` the creation base is an ancestor of the checkout's
    # HEAD by construction, so a base-relative comparison reports drift on every
    # completion; the checkout HEAD at launch is the only honest reference. Old
    # records predate the field, and there the two are the same commit.
    #
    # What the child never saw is the count that matters, so the lane's own HEAD
    # is excluded too: a child that merged the source branch mid-run saw those
    # commits, and counting them made the completion warning claim work it had
    # already read.
    dispatch_base = _str(creation.get("sourceCheckoutHeadOid")) or base
    source_drift: JsonObject | None = None
    if dispatch_base is not None:
        drift_revs = ["HEAD", f"^{dispatch_base}"]
        if head_commit is not None:
            drift_revs.append(f"^{head_commit}")
        drift_commits = _rev_list_count(source_git_root, drift_revs, warnings)
        if drift_commits is not None:
            source_drift = {"baseOid": dispatch_base, "commits": drift_commits}

    summary: JsonObject = {
        "dirty": dirty,
        "changedFilesCount": effective_total,
        "changedFiles": effective_files,
        "changedFilesTruncated": effective_total > len(effective_files),
        "rawChangedFilesCount": raw_total,
        "seededOnlyChanges": raw_total > 0 and effective_total == 0,
        "commitsCreatedCount": commits_count,
        "commitsCreated": commits,
        "commitsCreatedTruncated": (
            commits_count > len(commits)
            if commit_inspection_verified and commits_fetch_ok
            else False
        ),
        "commitInspectionStatus": "verified" if commit_inspection_verified else "unverified",
        "fileInspectionStatus": "verified" if file_inspection_verified else "unverified",
        "baseCommit": base,
        "headCommit": head_commit,
        "sourceHead": source_head,
        "branch": branch,
        "diffStat": _diff_shortstat(execution_cwd, "HEAD", warnings),
        # "No changes" needs both inspections to have run: an empty file list from
        # a failed ``git status`` is missing evidence, not a clean tree.
        "noChanges": (
            not dirty and commits_count == 0
            if commit_inspection_verified and file_inspection_verified
            else False
        ),
    }
    if branch_ahead_of_base is not None:
        summary["branchAheadOfBase"] = branch_ahead_of_base
    if branch_ahead_of_source is not None:
        summary["branchAheadOfSource"] = branch_ahead_of_source
    if source_drift is not None:
        summary["sourceDrift"] = source_drift
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
