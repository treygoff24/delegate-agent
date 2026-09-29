"""Save ledger edits before a worktree removal would discard them.

Changes under the ledger globs (``worktrees.retirementIgnoreGlobs``: by default
``.beads/**`` and ``.papercuts.jsonl``) are discounted from "uncommitted work",
because the harness rewrites those files on its own and counting them pinned
every worktree. The discount cannot tell that churn from a real edit, so no
removal is allowed to be silent about them: the changed files are copied to
``<registry>/salvage/<worktree>-<timestamp>/<relative path>`` first, the copy is
compared byte for byte, and a copy that fails refuses the removal.

Nothing in Delegate reads or deletes a salvage directory; retention and
``worktree prune`` only ever remove run directories and worktrees.
"""

from __future__ import annotations

import filecmp
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from delegate_agent import worktree_mgmt as wm
from delegate_agent import worktree_summary
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS
from delegate_agent.json_types import JsonObject
from delegate_agent.private_io import ensure_private_dir
from delegate_agent.worktree_records import SYNCED_FILE_DIGESTS_KEY

SALVAGE_DIR_NAME = "salvage"


@dataclass(frozen=True)
class LedgerSalvage:
    """Where the changed ledger files went, and which ones."""

    path: str
    files: tuple[str, ...]


def _status_entries(execution_cwd: str) -> list[tuple[str, list[str]]] | None:
    """``(status, [path, old path?])`` per changed file, exact (NUL-separated)."""

    result = wm._run_git(
        execution_cwd,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    tokens = result.stdout.split("\0")
    entries: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        status, paths = token[:2], [token[3:]]
        if "R" in status or "C" in status:
            if index < len(tokens):
                paths.append(tokens[index])
            index += 1
        entries.append((status, paths))
    return entries


def ledger_changes(
    execution_cwd: str,
    creation_context: JsonObject | None,
    ignore_globs: tuple[str, ...],
) -> list[str]:
    """Relative paths of changed files that only the ledger globs keep from counting as dirt.

    Launch-seeded files that still match their launch digest are not listed:
    the source checkout holds identical bytes. Deleted files have nothing left
    to copy (their content is in ``HEAD``) and are not listed either.
    """

    if not ignore_globs:
        return []
    entries = _status_entries(execution_cwd)
    if entries is None:
        return []
    creation = creation_context if isinstance(creation_context, dict) else {}
    digests = creation.get(SYNCED_FILE_DIGESTS_KEY)
    seeded = digests if isinstance(digests, dict) else {}
    root = Path(execution_cwd)
    changed: list[str] = []
    for _status, paths in entries:
        if not worktree_summary.matches_ignore_globs(paths, ignore_globs):
            continue
        if worktree_summary.is_seeded_unchanged(paths, execution_cwd=execution_cwd, seeded=seeded):
            continue
        relative = paths[0]
        if os.path.lexists(root / relative):
            changed.append(relative)
    return changed


def _fail(name: str, detail: str) -> wm.WorktreeManagementError:
    return wm.WorktreeManagementError(
        wm._error_payload(
            "ledger_salvage_failed",
            f"Could not save the changed ledger files of {name} before removing it "
            f"({detail}); nothing was removed. Copy them out by hand, or fix the "
            "cause and retry.",
            retry_safe=True,
        )
    )


def _copy_one(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
    elif source.is_file():
        shutil.copy2(source, destination)
        if not filecmp.cmp(source, destination, shallow=False):
            raise OSError(f"copy of {source.name} does not match the original")
    else:
        raise OSError(f"{source.name} is not a regular file")


def _unique_directory(parent: Path, stem: str) -> Path:
    ensure_private_dir(parent)
    for attempt in range(1, 100):
        candidate = parent / (stem if attempt == 1 else f"{stem}-{attempt}")
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"no free salvage directory name under {parent}")


def salvage_ledger_changes(
    *,
    execution_cwd: str,
    registry_root: Path | Callable[[], Path],
    creation_context: JsonObject | None,
    ignore_globs: tuple[str, ...],
) -> LedgerSalvage | None:
    """Copy the worktree's changed ledger files aside; None when there are none.

    ``registry_root`` may be a callable so a Registry is only created or looked
    up when something needs saving. Raises ``WorktreeManagementError``
    (``ledger_salvage_failed``) when any file cannot be saved and verified.
    """

    changed = ledger_changes(execution_cwd, creation_context, ignore_globs)
    if not changed:
        return None
    root = Path(execution_cwd)
    name = root.name
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory: Path | None = None
    try:
        base = registry_root() if callable(registry_root) else registry_root
        directory = _unique_directory(base / SALVAGE_DIR_NAME, f"{name}-{stamp}")
        for relative in changed:
            parts = PurePosixPath(relative).parts
            if PurePosixPath(relative).is_absolute() or ".." in parts:
                raise OSError(f"unsafe path {relative!r}")
            _copy_one(root / relative, directory / relative)
    except (OSError, shutil.Error, DelegateError) as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise _fail(name, str(exc)) from exc
    return LedgerSalvage(str(directory), tuple(changed))
