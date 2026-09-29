"""Save ledger edits before a worktree removal would discard them.

Changes under the ledger globs (``worktrees.retirementIgnoreGlobs``: by default
``.beads/**`` and ``.papercuts.jsonl``) are discounted from "uncommitted work",
because the harness rewrites those files on its own and counting them pinned
every worktree. The discount cannot tell that churn from a real edit, so no
removal is allowed to be silent about them: the changed files are copied to
``<registry>/salvage/<worktree>-<timestamp>/<relative path>`` first, the copy is
compared byte for byte, and a copy that fails refuses the removal. A deleted or
renamed ledger file has nothing (or not everything) left to copy, so every
discounted change, deletions and rename sources included, is also written to a
``MANIFEST.tsv`` in the same directory and read back before anything is
removed. A worktree whose status Git cannot report refuses the removal too:
an unreadable status is not "no ledger changes".

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
MANIFEST_NAME = "MANIFEST.tsv"


@dataclass(frozen=True)
class LedgerSalvage:
    """Where the changed ledger files went, and which ones."""

    path: str
    files: tuple[str, ...]
    # Ledger paths the worktree deleted or renamed away: recorded in the
    # manifest, with no bytes to copy.
    removed: tuple[str, ...] = ()


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


def ledger_entries(
    execution_cwd: str,
    creation_context: JsonObject | None,
    ignore_globs: tuple[str, ...],
) -> list[tuple[str, list[str]]]:
    """``(status, [path, old path?])`` for each change only the ledger globs discount.

    Launch-seeded files that still match their launch digest are left out: the
    source checkout holds identical bytes. Raises ``OSError`` when Git cannot
    report the worktree's status, because the caller is about to remove it.
    """

    if not ignore_globs:
        return []
    entries = _status_entries(execution_cwd)
    if entries is None:
        raise OSError("git status could not read the worktree")
    creation = creation_context if isinstance(creation_context, dict) else {}
    digests = creation.get(SYNCED_FILE_DIGESTS_KEY)
    seeded = digests if isinstance(digests, dict) else {}
    kept: list[tuple[str, list[str]]] = []
    for status, paths in entries:
        if not worktree_summary.matches_ignore_globs(paths, ignore_globs):
            continue
        if worktree_summary.is_seeded_unchanged(paths, execution_cwd=execution_cwd, seeded=seeded):
            continue
        kept.append((status, paths))
    return kept


def ledger_changes(
    execution_cwd: str,
    creation_context: JsonObject | None,
    ignore_globs: tuple[str, ...],
) -> list[str]:
    """Relative paths of discounted ledger files that still exist to be copied."""

    root = Path(execution_cwd)
    return [
        paths[0]
        for _status, paths in ledger_entries(execution_cwd, creation_context, ignore_globs)
        if os.path.lexists(root / paths[0])
    ]


def _manifest_text(entries: list[tuple[str, list[str]]]) -> str:
    """One line per discounted change: status, path, and the old path of a rename."""

    lines = ["status\tpath\tfrom"]
    for status, paths in entries:
        old = paths[1] if len(paths) > 1 else ""
        lines.append(f"{status.strip() or status}\t{paths[0]}\t{old}")
    return "\n".join(lines) + "\n"


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

    root = Path(execution_cwd)
    name = root.name
    try:
        entries = ledger_entries(execution_cwd, creation_context, ignore_globs)
    except OSError as exc:
        raise _fail(name, str(exc)) from exc
    if not entries:
        return None
    changed: list[str] = []
    removed: list[str] = []
    for _status, paths in entries:
        if os.path.lexists(root / paths[0]):
            changed.append(paths[0])
        else:
            removed.append(paths[0])
        # A rename's source is gone from the worktree either way.
        removed.extend(paths[1:])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory: Path | None = None
    try:
        base = registry_root() if callable(registry_root) else registry_root
        directory = _unique_directory(base / SALVAGE_DIR_NAME, f"{name}-{stamp}")
        for relative in [*changed, *removed]:
            parts = PurePosixPath(relative).parts
            if PurePosixPath(relative).is_absolute() or ".." in parts:
                raise OSError(f"unsafe path {relative!r}")
        for relative in changed:
            _copy_one(root / relative, directory / relative)
        manifest = directory / MANIFEST_NAME
        expected = _manifest_text(entries)
        manifest.write_text(expected, encoding="utf-8")
        if manifest.read_text(encoding="utf-8") != expected:
            raise OSError("salvage manifest does not read back")
    except (OSError, shutil.Error, DelegateError) as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise _fail(name, str(exc)) from exc
    return LedgerSalvage(str(directory), tuple(changed), tuple(removed))
