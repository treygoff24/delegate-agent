"""Temporary safe-mode workspace isolation.

Safe-mode runs execute against a throwaway copy of the workspace so the
delegated harness cannot mutate the real checkout. This module owns that
machinery: detached-worktree / directory-copy creation, tracked-diff sync,
external-symlink blocking (so a symlink can't escape the sandbox), and the
``safe_isolated_request`` context manager that swaps a request onto the
isolated workspace for the duration of a run and tears it down afterwards.

This is the temporary-isolation twin of ``isolation.py`` (which owns
persistent-worktree planning/creation); both are deliberately kept distinct.
The security constraints here — symlink containment, ``.git``/``.delegate``
exclusion, atomic writes — are load-bearing; preserve them exactly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404 - Delegate launches configured git/harness commands with shell=False.
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from delegate_agent.argv_utils import public_argv
from delegate_agent.argv_utils import replace_workspace_arg_in_argv as _replace_ws_by_engine
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_SLASH
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import (
    GIT_MUTATION_TIMEOUT_SECONDS,
    GIT_QUICK_TIMEOUT_SECONDS,
)
from delegate_agent.git_utils import run_git as _run_git
from delegate_agent.git_utils import run_git_bytes as _run_git_bytes
from delegate_agent.isolation import IsolationContext
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_transport import PROMPT_TRANSPORT_ARGV
from delegate_agent.request_models import Request

# Project .cursor/cli.json is permissions-only; global cli-config examples may
# include other top-level keys such as "version", but Cursor rejects them here.
CURSOR_SAFE_CLI_CONFIG: JsonObject = {
    "permissions": {
        "allow": [
            "Read(**)",
            "Shell(rg)",
            "Shell(grep)",
            "Shell(cat)",
            "Shell(head)",
            "Shell(tail)",
            "Shell(wc)",
        ],
        "deny": [
            "Write(**)",
            "Shell(rm)",
            "Shell(mv)",
            "Shell(tee)",
            "Shell(curl)",
            "Shell(wget)",
            "Read(.env*)",
            "Read(**/.env*)",
            "Read(**/id_rsa*)",
            "Read(**/*.pem)",
        ],
    },
}

SAFE_UNBORN_GIT_WARNING = (
    "Git repository has no commits; safe isolation used a directory copy instead "
    "of a detached git worktree."
)

SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX = (
    "Safe isolation blocked external symlink(s); placeholder files were used "
    "inside the isolated workspace"
)

SAFE_BLOCKED_SYMLINK_PLACEHOLDER = "External symlink blocked by Delegate safe isolation.\n"

SAFE_CHECK_IGNORE_FAIL_CLOSED_WARNING = (
    "Safe isolation could not verify gitignore status for one or more untracked "
    "symlink target(s); fail-closed replaced all queried symlinks with placeholders."
)

SAFE_ISOLATION_REPORT_INSTRUCTION = (
    "\n\nSafe-isolation note: cite files relative to the workspace in your report; "
    "do not include the temporary workspace path."
)


def _ensure_codex_skip_git_repo_check(argv: list[str]) -> list[str]:
    if "--skip-git-repo-check" in argv:
        return argv
    updated = list(argv)
    # Codex exec options belong before the final prompt argument.
    insert_at = max(len(updated) - 1, 0)
    updated.insert(insert_at, "--skip-git-repo-check")
    return updated


def replace_safe_workspace_arg_in_argv(
    request: Request,
    argv: list[str],
    isolated_workspace: str,
    *,
    workspace_kind: str | None = None,
) -> list[str]:
    updated = _replace_ws_by_engine(request.engine, argv, isolated_workspace)
    if request.engine == "codex" and workspace_kind == "directory":
        updated = _ensure_codex_skip_git_repo_check(updated)
    return updated


def replace_workspace_path_prefix(
    text: str,
    source_workspace: str,
    isolated_workspace: str,
) -> str:
    """Re-root exact source-workspace paths without touching prefix lookalikes."""
    source = os.path.normpath(source_workspace)
    isolated = os.path.normpath(isolated_workspace)
    if source == os.sep:
        return isolated if text == source else text
    return re.sub(
        rf"(?<![\w./~+-]){re.escape(source)}(?=$|{re.escape(os.sep)})",
        lambda _match: isolated,
        text,
    )


def _isolated_prompt_text(
    text: str | None,
    source_workspace: str,
    isolated_workspace: str,
) -> str | None:
    if text is None:
        return None
    updated = replace_workspace_path_prefix(text, source_workspace, isolated_workspace)
    if SAFE_ISOLATION_REPORT_INSTRUCTION not in updated:
        updated += SAFE_ISOLATION_REPORT_INSTRUCTION
    return updated


def _isolated_prompt_argv(
    request: Request,
    argv: list[str],
    isolated_workspace: str,
) -> list[str]:
    if request.prompt_transport != PROMPT_TRANSPORT_ARGV or not argv:
        return argv
    updated = list(argv)
    prompt = _isolated_prompt_text(updated[-1], request.workspace, isolated_workspace)
    if prompt is not None:
        updated[-1] = prompt
    return updated


def write_text_atomic(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        temporary_path.replace(path)
    except OSError:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink()
        raise


def write_cursor_safe_project_config(workspace: Path) -> None:
    config_dir = workspace / ".cursor"
    config_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        config_dir / "cli.json",
        json.dumps(CURSOR_SAFE_CLI_CONFIG, indent=2) + "\n",
    )


def read_git_tracked_diff(git_root: str) -> bytes:
    diff = _run_git_bytes(
        git_root,
        ["diff", "HEAD", "--binary"],
        timeout_seconds=GIT_MUTATION_TIMEOUT_SECONDS,
    )
    if diff.returncode != 0:
        stderr = diff.stderr.decode(errors="replace").strip()
        raise DelegateError("safe_workspace_sync_failed", f"Failed to read tracked diff: {stderr}")
    return diff.stdout


def apply_git_tracked_diff(worktree_path: str, diff: bytes) -> None:
    if not diff.strip():
        return
    applied = _run_git_bytes(
        worktree_path,
        ["apply", "--whitespace=nowarn"],
        input_bytes=diff,
        timeout_seconds=GIT_MUTATION_TIMEOUT_SECONDS,
    )
    if applied.returncode != 0:
        stderr = applied.stderr.decode(errors="replace").strip()
        raise DelegateError(
            "safe_workspace_sync_failed",
            f"Failed to apply tracked diff to isolated workspace: {stderr}",
        )


def _git_lines(git_root: str, args: list[str], *, error: str) -> list[str]:
    result = _run_git(
        git_root,
        args,
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise DelegateError("safe_workspace_sync_failed", f"{error}: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def changed_files_vs_head(git_root: str) -> tuple[str, ...]:
    """Return tracked HEAD diff paths plus untracked non-ignored paths."""
    paths: list[str] = []
    seen: set[str] = set()
    for line in _git_lines(
        git_root,
        ["diff", "HEAD", "--name-only"],
        error="Failed to list tracked changes",
    ) + _git_lines(
        git_root,
        ["ls-files", "--others", "--exclude-standard"],
        error="Failed to list untracked files",
    ):
        if line in seen:
            continue
        seen.add(line)
        paths.append(line)
    return tuple(paths)


def symlink_target_resolves_outside(path: Path, source_root: Path) -> bool:
    """Return true when ``path`` is a symlink whose target leaves ``source_root``."""
    if not path.is_symlink():
        return False
    try:
        root_resolved = source_root.resolve(strict=True)
        target = (path.parent / os.readlink(path)).resolve(strict=False)
    except OSError:
        return False
    return not target.is_relative_to(root_resolved)


def _git_check_ignore(git_root: str, paths: list[str]) -> tuple[set[str], bool]:
    """Return the subset of ``paths`` that Git reports as ignored.

    Uses a single batched ``git check-ignore -z --stdin`` invocation with
    NUL-separated input and output so a large untracked set never spawns one
    subprocess per path and so paths containing newlines are handled correctly.
    ``git check-ignore`` exits 0 when at least one path is ignored, 1 when none
    are, and any other code (e.g. 128) on error.

    Returns ``(ignored, fail_closed)``:
    - exit 1 -> ``({}, False)`` (clean: nothing ignored).
    - exit 0 -> parse NUL-separated stdout -> ``(ignored, False)``.
    - any other exit -> ``({}, True)``: FAIL CLOSED for the batch. The caller
      must treat every queried target as ignored (placeholder the symlinks) and
      emit a warning, but must not raise and abort the whole sync.
    """
    if not paths:
        return set(), False
    result = _run_git_bytes(
        git_root,
        ["check-ignore", "-z", "--stdin"],
        input_bytes=b"\x00".join(p.encode("utf-8") for p in paths) + b"\x00",
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode == 1:
        return set(), False
    if result.returncode != 0:
        return set(), True
    ignored: set[str] = set()
    for token in result.stdout.decode(errors="replace").split("\x00"):
        if token:
            ignored.add(token)
    return ignored, False


def _classify_untracked_symlink_leaks(
    git_root: str,
    untracked: list[str],
    root: Path,
    root_resolved: Path,
) -> tuple[set[str], tuple[str, ...]]:
    """Return relative paths of untracked symlinks blocked by the leak rule.

    A symlink is recreated only when its readlink target is RELATIVE, resolves
    INSIDE ``git_root``, and the resolved target is NOT gitignored. Anything
    else is a leak risk and is blocked here. External symlinks (target resolves
    outside ``git_root``) are deliberately excluded: they are already caught by
    ``symlink_target_resolves_outside`` and reported via
    ``external_symlink_warnings``, so reporting them here would duplicate the
    existing warning channel. The remaining cases -- an absolute readlink whose
    target resolves inside the repo, or a relative-inside symlink whose target
    is gitignored -- are the leak cases this function returns so the caller can
    placeholder them and emit one consolidated warning.

    ``git check-ignore`` is invoked once (batched over all inside-root targets)
    rather than per symlink. If that probe fails with an unexpected exit code,
    the batch FAILS CLOSED: every queried inside-root symlink is placeholdered
    and a distinct warning is emitted, but the sync is not aborted.

    Returns ``(leak_blocked, warnings)`` where ``warnings`` carries any
    fail-closed notice (the per-path blocked-symlink warning is emitted
    separately by the caller via ``_leak_blocked_symlink_warning``).
    """
    leak_blocked: set[str] = set()
    inside_targets: dict[str, str] = {}
    warnings: tuple[str, ...] = ()
    for relative in untracked:
        if not relative:
            continue
        source = root / relative
        if not source.is_symlink():
            continue
        # External symlinks are handled by the resolves-outside path; skip them
        # so we do not double-report on the existing warning channel.
        if symlink_target_resolves_outside(source, root):
            continue
        try:
            readlink_target = os.readlink(source)
        except OSError:
            leak_blocked.add(relative)
            continue
        if os.path.isabs(readlink_target):
            # Absolute readlink is never recreated, even when it resolves inside
            # the repo (it encodes a host path and can point at gitignored
            # content). External absolute symlinks were skipped above.
            leak_blocked.add(relative)
            continue
        try:
            resolved = (source.parent / readlink_target).resolve(strict=False)
        except OSError:
            leak_blocked.add(relative)
            continue
        if not resolved.is_relative_to(root_resolved):
            # Escaping relative link; resolves_outside handles it.
            continue
        try:
            inside_targets[relative] = resolved.relative_to(root_resolved).as_posix()
        except ValueError:
            leak_blocked.add(relative)
    if inside_targets:
        ignored, fail_closed = _git_check_ignore(git_root, list(inside_targets.values()))
        if fail_closed:
            # FAIL CLOSED: treat every queried inside-root symlink as a leak
            # risk and placeholder it. Do not raise; the sync still completes.
            leak_blocked.update(inside_targets.keys())
            warnings = (SAFE_CHECK_IGNORE_FAIL_CLOSED_WARNING,)
        else:
            for symlink_rel, target_rel in inside_targets.items():
                if target_rel in ignored:
                    leak_blocked.add(symlink_rel)
    return leak_blocked, warnings


def _leak_blocked_symlink_warning(leak_blocked: set[str], *, limit: int = 5) -> tuple[str, ...]:
    """Emit the existing blocked-symlink warning for leak-rule placeholders."""
    if not leak_blocked:
        return ()
    paths = sorted(leak_blocked)
    preview = ", ".join(paths[:limit])
    remaining = len(paths) - limit
    if remaining > 0:
        preview = f"{preview}, ... (+{remaining} more)"
    return (f"{SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX}: {preview}.",)


def write_blocked_symlink_placeholder(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
    write_text_atomic(path, SAFE_BLOCKED_SYMLINK_PLACEHOLDER)


def _symlink_dirnames(current_path: Path, dirnames: list[str]) -> set[str]:
    symlink_names: set[str] = set()
    for name in dirnames:
        try:
            is_symlink = (current_path / name).is_symlink()
        except OSError:
            # Treat unreadable/racing directory entries as non-descendable.
            is_symlink = True
        if is_symlink:
            symlink_names.add(name)
    return symlink_names


def _block_external_symlink_if_needed(
    path: Path,
    *,
    isolated_root: Path,
    source_root: Path,
    containment_root: Path,
) -> str | None:
    try:
        if not path.is_symlink():
            return None
        relative = path.relative_to(isolated_root)
        source_path = source_root / relative
        if not symlink_target_resolves_outside(source_path, containment_root):
            return None
        write_blocked_symlink_placeholder(path)
        return relative.as_posix()
    except (OSError, ValueError):
        # Filesystem walks can race with edits; skip only the unstable entry.
        return None


def block_external_symlinks(
    isolated_workspace: str | Path,
    source_workspace: str | Path,
    *,
    containment_root: str | Path | None = None,
    limit: int = 5,
) -> tuple[str, ...]:
    """Replace external symlinks in a safe workspace with inert placeholders.

    The isolated tree mirrors the source layout, so each isolated symlink can be
    evaluated against its matching source path. Internal symlinks stay intact;
    links whose source target resolves outside the source workspace are replaced
    with a small placeholder file that does not disclose the external target.
    """
    isolated_root = Path(isolated_workspace)
    source_root = Path(source_workspace)
    root_for_containment = Path(containment_root) if containment_root is not None else source_root
    blocked: list[str] = []
    # Each filesystem touch is guarded individually so a single unreadable or
    # racing entry skips only itself rather than aborting the whole sweep and
    # silently leaving the remaining external symlinks unblocked.
    for current, dirnames, filenames in os.walk(isolated_root):
        current_path = Path(current)
        original_symlink_dirnames = _symlink_dirnames(current_path, dirnames)
        for name in list(dirnames) + list(filenames):
            path = current_path / name
            blocked_link = _block_external_symlink_if_needed(
                path,
                isolated_root=isolated_root,
                source_root=source_root,
                containment_root=root_for_containment,
            )
            if blocked_link is None:
                continue
            blocked.append(blocked_link)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", ".delegate"} and name not in original_symlink_dirnames
        ]
    if not blocked:
        return ()
    blocked.sort()
    preview = ", ".join(blocked[:limit])
    remaining = len(blocked) - limit
    if remaining > 0:
        preview = f"{preview}, ... (+{remaining} more)"
    return (f"{SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX}: {preview}.",)


def merge_warnings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for warning in group:
            if warning in seen:
                continue
            seen.add(warning)
            merged.append(warning)
    return tuple(merged)


def mirror_path_preserving_symlinks(
    source: Path,
    destination: Path,
    *,
    source_root: Path | None = None,
    leak_blocked: set[str] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        relative: str | None = None
        if source_root is not None:
            with suppress(ValueError):
                relative = source.relative_to(source_root).as_posix()
        if relative is not None and relative in (leak_blocked or set()):
            write_blocked_symlink_placeholder(destination)
            return
        if source_root is not None and symlink_target_resolves_outside(source, source_root):
            write_blocked_symlink_placeholder(destination)
        else:
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            os.symlink(os.readlink(source), destination)
        return
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=_copytree_ignore_safe_workspace,
        )
        if source_root is not None:
            block_external_symlinks(
                destination,
                source,
                containment_root=source_root,
            )
        return
    if not source.is_file():
        return
    shutil.copy2(source, destination)


def _copytree_ignore_safe_workspace(directory: str, names: list[str]) -> set[str]:
    ignored = {".git", ".delegate"} & set(names)
    for name in names:
        path = Path(directory) / name
        try:
            mode = path.lstat().st_mode
        except OSError:
            ignored.add(name)
            continue
        if stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            continue
        ignored.add(name)
    return ignored


def _external_symlink_warning_path(path: Path, *, root: Path, root_resolved: Path) -> str | None:
    try:
        if not path.is_symlink():
            return None
        target = (path.parent / os.readlink(path)).resolve(strict=False)
    except OSError:
        # Filesystem walks can race with edits; skip only the unstable entry.
        return None
    if target.is_relative_to(root_resolved):
        return None
    with suppress(ValueError):
        return path.relative_to(root).as_posix()
    return path.name


def external_symlink_warnings(source_workspace: str, *, limit: int = 5) -> tuple[str, ...]:
    """Return a bounded warning when a source tree contains external symlinks."""
    root = Path(source_workspace)
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        return ()
    external_links: list[str] = []
    try:
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            names = list(dirnames) + list(filenames)
            for name in names:
                external_link = _external_symlink_warning_path(
                    current_path / name,
                    root=root,
                    root_resolved=root_resolved,
                )
                if external_link is None:
                    continue
                external_links.append(external_link)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in {".git", ".delegate"} and not (current_path / name).is_symlink()
            ]
    except OSError:
        return ()
    if not external_links:
        return ()
    external_links.sort()
    preview = ", ".join(external_links[:limit])
    remaining = len(external_links) - limit
    if remaining > 0:
        preview = f"{preview}, ... (+{remaining} more)"
    return (f"{SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX}: {preview}.",)


def dirty_sync_counts(git_root: str) -> tuple[int, int]:
    changed = changed_files_vs_head(git_root)
    untracked = _git_lines(
        git_root,
        ["ls-files", "--others", "--exclude-standard"],
        error="Failed to list untracked files",
    )
    return len(changed) - len(untracked), len(untracked)


def sync_git_dirty_snapshot(
    git_root: str, worktree_path: str
) -> tuple[int, int, int, tuple[str, ...]]:
    apply_git_tracked_diff(worktree_path, read_git_tracked_diff(git_root))
    changed = changed_files_vs_head(git_root)
    untracked = _git_lines(
        git_root,
        ["ls-files", "--others", "--exclude-standard"],
        error="Failed to list untracked files",
    )
    root = Path(git_root)
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        root_resolved = root
    leak_blocked, check_ignore_warnings = _classify_untracked_symlink_leaks(
        git_root,
        untracked,
        root,
        root_resolved,
    )
    for relative in untracked:
        if not relative:
            continue
        mirror_path_preserving_symlinks(
            Path(git_root) / relative,
            Path(worktree_path) / relative,
            source_root=Path(git_root),
            leak_blocked=leak_blocked,
        )
    tracked_count = len(changed) - len(untracked)
    return (
        len(changed),
        tracked_count,
        len(untracked),
        merge_warnings(
            external_symlink_warnings(git_root),
            block_external_symlinks(worktree_path, git_root),
            _leak_blocked_symlink_warning(leak_blocked),
            check_ignore_warnings,
        ),
    )


def sync_git_workspace_snapshot(git_root: str, worktree_path: str) -> tuple[str, ...]:
    _count, _tracked, _untracked, warnings = sync_git_dirty_snapshot(git_root, worktree_path)
    return warnings


def git_head_exists(git_root: str) -> bool:
    result = _run_git(
        git_root,
        ["rev-parse", "--verify", "HEAD"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def discard_git_safe_workspace(
    git_root: str, worktree_path: str, temp_base: str, *, worktree_added: bool
) -> None:
    if worktree_added:
        remove_git_safe_workspace(git_root, worktree_path)
    shutil.rmtree(temp_base, ignore_errors=True)


def create_git_safe_workspace(
    git_root: str,
    *,
    include_warnings: bool = False,
) -> tuple[str, str] | tuple[str, str, tuple[str, ...]]:
    temp_base = tempfile.mkdtemp(prefix="delegate-safe-")
    worktree_path = str(Path(temp_base) / "wt")
    worktree_added = False
    warnings: tuple[str, ...] = ()
    try:
        added = _run_git(
            git_root,
            ["worktree", "add", "--detach", worktree_path, "HEAD"],
            timeout_seconds=GIT_MUTATION_TIMEOUT_SECONDS,
        )
        if added.returncode != 0:
            raise DelegateError(
                "safe_workspace_create_failed",
                f"Failed to create detached git worktree: {added.stderr.strip()}",
            )
        worktree_added = True
        warnings = sync_git_workspace_snapshot(git_root, worktree_path)
    except Exception:
        discard_git_safe_workspace(
            git_root, worktree_path, temp_base, worktree_added=worktree_added
        )
        raise
    if include_warnings:
        return worktree_path, temp_base, warnings
    return worktree_path, temp_base


def create_directory_safe_workspace(
    source_workspace: str,
    *,
    include_warnings: bool = False,
) -> tuple[str, str] | tuple[str, str, tuple[str, ...]]:
    temp_base = tempfile.mkdtemp(prefix="delegate-safe-")
    copy_path = str(Path(temp_base) / "copy")
    warnings: tuple[str, ...] = ()
    try:
        shutil.copytree(
            source_workspace,
            copy_path,
            ignore=_copytree_ignore_safe_workspace,
            dirs_exist_ok=True,
            symlinks=True,
        )
        warnings = merge_warnings(
            external_symlink_warnings(source_workspace),
            block_external_symlinks(copy_path, source_workspace),
        )
    except Exception:
        shutil.rmtree(temp_base, ignore_errors=True)
        raise
    if include_warnings:
        return copy_path, temp_base, warnings
    return copy_path, temp_base


def remove_git_safe_workspace(git_root: str, worktree_path: str) -> None:
    with suppress(OSError, subprocess.SubprocessError):
        _run_git(
            git_root,
            ["worktree", "remove", "--force", worktree_path],
            timeout_seconds=GIT_MUTATION_TIMEOUT_SECONDS,
        )


def cleanup_safe_isolated_workspace(
    *,
    git_root: str | None,
    isolated_workspace: str,
    temp_base: str,
) -> None:
    if git_root is not None:
        remove_git_safe_workspace(git_root, isolated_workspace)
    shutil.rmtree(temp_base, ignore_errors=True)


@contextmanager
def safe_isolated_request(request: Request) -> Iterator[Request]:
    """Context manager that creates a temporary isolated workspace for safe-mode runs.

    Respects the isolation context:
    - effective_isolation == "none": skip isolation, yield original request.
    - effective_isolation == "worktree": create temp git worktree (or dir copy
      for auto legacy fallback). For cursor, writes .cursor/cli.json in the
      isolated workspace only.
    """
    ctx = request.isolation_context
    effective = ctx.effective_isolation if ctx is not None else None

    # No isolation needed.
    if effective != "worktree":
        yield request
        return

    # Isolation is worktree — create temp workspace.
    isolation_mode = ctx.isolation_mode if ctx is not None else "auto"
    source_git_root = request.workspace if request.workspace_kind == "git" else None
    cleanup_git_root: str | None = None
    workspace_kind = request.workspace_kind
    safe_workspace_method: str | None = None
    warnings_list: list[str] = []
    safe_workspace_warnings: tuple[str, ...] = ()

    if source_git_root is not None and git_head_exists(source_git_root):
        isolated_workspace, temp_base, safe_workspace_warnings = create_git_safe_workspace(
            source_git_root,
            include_warnings=True,
        )
        cleanup_git_root = source_git_root
        safe_workspace_method = "git-worktree"
    elif source_git_root is not None:
        isolated_workspace, temp_base, safe_workspace_warnings = create_directory_safe_workspace(
            source_git_root,
            include_warnings=True,
        )
        workspace_kind = "directory"
        safe_workspace_method = "directory-copy"
        warnings_list.append(SAFE_UNBORN_GIT_WARNING)
    elif isolation_mode == "auto":
        # Legacy auto fallback for non-git cursor/codex safe: directory copy.
        isolated_workspace, temp_base, safe_workspace_warnings = create_directory_safe_workspace(
            request.workspace,
            include_warnings=True,
        )
        workspace_kind = "directory"
        safe_workspace_method = "directory-copy"
    else:
        raise DelegateError(
            "worktree_requires_git",
            "--isolation worktree requires a Git workspace for safe mode.",
        )
    warnings = merge_warnings(
        tuple(warnings_list),
        safe_workspace_warnings,
    )

    isolation = IsolationContext(
        source_workspace=request.workspace,
        effective_isolation=effective,
        isolation_mode=isolation_mode,
        isolation_lifecycle="temporary",
        preserved_workspace=False,
        source_git_root=source_git_root,
        safe_workspace_method=safe_workspace_method,
        warnings=warnings,
    )
    try:
        if request.engine == "cursor":
            write_cursor_safe_project_config(Path(isolated_workspace))
        isolated_argv = replace_safe_workspace_arg_in_argv(
            request,
            request.argv,
            isolated_workspace,
            workspace_kind=workspace_kind,
        )
        slash_passthrough = request.prompt_instruction_mode == PROMPT_INSTRUCTION_MODE_SLASH
        if not slash_passthrough:
            isolated_argv = _isolated_prompt_argv(request, isolated_argv, isolated_workspace)
        isolated_display_argv = replace_safe_workspace_arg_in_argv(
            request,
            public_argv(request),
            isolated_workspace,
            workspace_kind=workspace_kind,
        )
        if slash_passthrough:
            isolated_prompt = request.prompt
            isolated_stdin_text = request.stdin_text
            isolated_prompt_file_text = request.prompt_file_text
        else:
            isolated_prompt = _isolated_prompt_text(
                request.prompt,
                request.workspace,
                isolated_workspace,
            )
            isolated_stdin_text = _isolated_prompt_text(
                request.stdin_text,
                request.workspace,
                isolated_workspace,
            )
            isolated_prompt_file_text = _isolated_prompt_text(
                request.prompt_file_text,
                request.workspace,
                isolated_workspace,
            )
        yield Request(
            engine=request.engine,
            mode=request.mode,
            workspace=isolated_workspace,
            prompt=isolated_prompt or "",
            argv=isolated_argv,
            model=request.model,
            model_alias=request.model_alias,
            output_schema=request.output_schema,
            dry_run=request.dry_run,
            workspace_kind=workspace_kind,
            isolation_context=isolation,
            reasoning_effort=request.reasoning_effort,
            reasoning_effort_source=request.reasoning_effort_source,
            reasoning_capability_source=request.reasoning_capability_source,
            reasoning_transport=request.reasoning_transport,
            fast=request.fast,
            progress=request.progress,
            progress_initial_delay_sec=request.progress_initial_delay_sec,
            progress_interval_sec=request.progress_interval_sec,
            forbid_commit=request.forbid_commit,
            include_dirty=request.include_dirty,
            warnings=request.warnings,
            stdin_text=isolated_stdin_text,
            prompt_file_text=isolated_prompt_file_text,
            agent_config_text=request.agent_config_text,
            prompt_transport=request.prompt_transport,
            display_argv=isolated_display_argv,
            env_overrides=request.env_overrides,
            auth_profile=request.auth_profile,
            fallback_auth_profile=request.fallback_auth_profile,
            cleanup_workspace=request.cleanup_workspace,
            group=request.group,
            workflow_agent_key=request.workflow_agent_key,
            prompt_instruction_mode=request.prompt_instruction_mode,
            profile_resolution=request.profile_resolution,
        )
    finally:
        cleanup_safe_isolated_workspace(
            git_root=cleanup_git_root,
            isolated_workspace=isolated_workspace,
            temp_base=temp_base,
        )
