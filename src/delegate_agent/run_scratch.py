"""Private per-run temporary storage outside workspace Git ancestry."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess  # nosec B404 - fixed offline Git ancestry probe.
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from delegate_agent import private_io
from delegate_agent.record_io import RUN_ID_RE

SCRATCH_ROOT_NAME = "run-scratch"
PERSISTENT_TEMP_ROOT = Path("/var/tmp")
# A sidecar name cannot collide with a run id: run ids never contain a dot.
SIDECAR_SEPARATOR = "."
_SIDECAR_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# The directory children get as TMPDIR/TMP/TEMP. It is deliberately NOT the run
# scratch: that path carries the registry identity hash and the run id, and a
# child that binds a Unix socket under it overruns `sun_path` (108 bytes, 91 of
# them already spent before the socket's own name). `/var/tmp/dlg-<euid>/<token>`
# leaves the child ~60 characters for a socket name.
COMPACT_TEMP_DIR_PREFIX = "dlg-"
COMPACT_TEMP_TOKEN_CHARS = 24


class ScratchSafetyError(OSError):
    """A neutral scratch path could not be established or safely removed."""


@dataclass(frozen=True)
class ScratchPlan:
    root: Path
    bucket: Path
    path: Path
    fallback: bool


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _require_real_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ScratchSafetyError(f"could not inspect scratch directory {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ScratchSafetyError(f"scratch directory is not a real directory: {path}")
    return info


def _require_owned_directory(path: Path, *, private: bool) -> None:
    info = _require_real_directory(path)
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ScratchSafetyError(f"scratch directory has a foreign owner: {path}")
    if private and private_io.supports_private_modes():
        mode = stat.S_IMODE(info.st_mode)
        if mode != private_io.PRIVATE_DIR_MODE:
            raise ScratchSafetyError(
                f"scratch directory is not owner-only (mode {mode:04o}): {path}"
            )


def _git_worktree_root(path: Path) -> str | None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["LC_ALL"] = "C"
    try:
        result = subprocess.run(  # nosec B603 - fixed Git argv, no shell.
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScratchSafetyError(f"could not verify neutral scratch placement: {exc}") from exc
    if result.returncode == 0:
        return result.stdout.strip() or "an unknown Git worktree"
    if result.returncode == 128 and "not a git repository" in result.stderr.lower():
        return None
    detail = result.stderr.strip() or f"git exited {result.returncode}"
    raise ScratchSafetyError(f"could not verify neutral scratch placement: {detail}")


def _require_outside_git_worktree(path: Path) -> None:
    ancestor = _git_worktree_root(path)
    if ancestor is not None:
        raise ScratchSafetyError(f"scratch root is inside Git worktree {ancestor}: {path}")


def _fallback_root() -> Path:
    if not hasattr(os, "geteuid"):
        raise ScratchSafetyError("no persistent neutral scratch fallback is available")
    _require_real_directory(PERSISTENT_TEMP_ROOT)
    _require_outside_git_worktree(PERSISTENT_TEMP_ROOT)
    owner_root = PERSISTENT_TEMP_ROOT / f"delegate-{os.geteuid()}"
    if _path_exists(owner_root):
        _require_owned_directory(owner_root, private=False)
    return owner_root / SCRATCH_ROOT_NAME


def _select_root() -> tuple[Path, bool]:
    home = Path.home()
    _require_owned_directory(home, private=False)
    delegate_home = home / ".delegate"
    if _path_exists(delegate_home):
        # A hostile default is an integrity failure, not permission to route
        # around it through the fallback.
        _require_owned_directory(delegate_home, private=False)
    if _git_worktree_root(home) is not None:
        return _fallback_root(), True
    if _path_exists(delegate_home):
        _require_outside_git_worktree(delegate_home)
    root = delegate_home / SCRATCH_ROOT_NAME
    if _path_exists(root):
        _require_owned_directory(root, private=False)
        _require_outside_git_worktree(root)
    return root, False


def plan(registry_root: Path, run_id: str) -> ScratchPlan:
    """Calculate the selected root and run path without filesystem mutation."""
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ScratchSafetyError(f"invalid run id for scratch allocation: {run_id!r}")
    try:
        identity = str(registry_root.resolve(strict=True)).encode("utf-8")
    except OSError as exc:
        raise ScratchSafetyError(f"could not identify run registry: {exc}") from exc
    root, fallback = _select_root()
    bucket = root / hashlib.sha256(identity).hexdigest()[:24]
    return ScratchPlan(root=root, bucket=bucket, path=bucket / run_id, fallback=fallback)


def expected_path(registry_root: Path, run_id: str) -> Path:
    return plan(registry_root, run_id).path


def _compact_temp_root() -> Path:
    """The owner-only directory every compact child temp of this user sits in.

    Resolved before use because the parent helpers refuse symlinked path
    components (macOS spells this root `/private/var/tmp`), and the root itself
    is never chmod'ed: it is a shared system directory, not one of ours.
    """
    if not hasattr(os, "geteuid"):
        raise ScratchSafetyError("no compact child temp directory is available")
    root = Path(str(PERSISTENT_TEMP_ROOT)).resolve(strict=False)
    _require_real_directory(root)
    _require_outside_git_worktree(root)
    return root / f"{COMPACT_TEMP_DIR_PREFIX}{os.geteuid()}"


def compact_temp_plan(registry_root: Path, run_id: str) -> ScratchPlan:
    """Plan the short-path run temp directory children get as TMPDIR/TMP/TEMP.

    The run's own scratch keeps its documented location and cleanup contract;
    this is a second deterministic run-owned directory whose whole path stays
    short enough for a child's Unix socket. It derives from registry identity
    and run id alone, so no record has to carry it, cleanup finds it for every
    run, and a legacy run that never allocated one has nothing to remove.
    """
    base = plan(registry_root, run_id)
    owner_root = _compact_temp_root()
    token = hashlib.sha256(str(base.path).encode("utf-8")).hexdigest()[:COMPACT_TEMP_TOKEN_CHARS]
    return replace(base, root=owner_root, bucket=owner_root, path=owner_root / token)


def expected_compact_temp_path(registry_root: Path, run_id: str) -> Path:
    return compact_temp_plan(registry_root, run_id).path


def _ensure_compact_temp_roots(compact_plan: ScratchPlan) -> None:
    private_io.ensure_private_owned_dir(compact_plan.root)
    _require_owned_directory(compact_plan.root, private=True)
    _require_outside_git_worktree(compact_plan.root)


def allocate_compact_temp(registry_root: Path, run_id: str) -> Path:
    """Create or reuse one run's compact child temp directory.

    Unlike the run scratch this is not an atomic claim: a retry inside the same
    launch re-enters allocation, and nothing depends on exclusive creation.
    Symlinked, foreign-owned, or non-directory paths are refused rather than
    replaced, so a pre-planted path cannot redirect the child's temp files.
    """
    compact_plan = compact_temp_plan(registry_root, run_id)
    try:
        _ensure_compact_temp_roots(compact_plan)
        private_io.ensure_private_owned_dir(compact_plan.path)
    except OSError as exc:
        raise ScratchSafetyError(f"could not allocate compact child temp directory: {exc}") from exc
    _require_owned_directory(compact_plan.path, private=True)
    _require_outside_git_worktree(compact_plan.path)
    return compact_plan.path


def _ensure_shared_roots(scratch_plan: ScratchPlan) -> None:
    """Create and re-verify the roots every run in this registry shares."""
    shared_parent = scratch_plan.root.parent
    if scratch_plan.fallback:
        private_io.ensure_private_owned_dir(shared_parent)
    else:
        private_io.ensure_owned_dir(shared_parent)
    for directory in (scratch_plan.root, scratch_plan.bucket):
        private_io.ensure_private_owned_dir(directory)
        _require_owned_directory(directory, private=True)
        _require_outside_git_worktree(directory)


def allocate_plan(scratch_plan: ScratchPlan) -> Path:
    """Create one previously calculated scratch path."""
    try:
        _ensure_shared_roots(scratch_plan)
        private_io.create_private_owned_dir(scratch_plan.path)
    except OSError as exc:
        raise ScratchSafetyError(f"could not allocate private run scratch: {exc}") from exc
    _require_owned_directory(scratch_plan.path, private=True)
    _require_outside_git_worktree(scratch_plan.path)
    return scratch_plan.path


def allocate(registry_root: Path, run_id: str) -> Path:
    return allocate_plan(plan(registry_root, run_id))


def sidecar_plan(registry_root: Path, run_id: str, name: str) -> ScratchPlan:
    """Plan a run-scoped neutral directory beside the run's own scratch.

    The run scratch is claimed atomically at launch and must not already
    exist, so an artifact provisioned earlier in the launch (the mail-push
    private engine homes) cannot live inside it. A sidecar shares the run's
    bucket, carries the run id, and is removed with the run's scratch.
    """
    if _SIDECAR_NAME_RE.fullmatch(name) is None:
        raise ScratchSafetyError(f"invalid run scratch sidecar name: {name!r}")
    base = plan(registry_root, run_id)
    return replace(base, path=base.bucket / f"{run_id}{SIDECAR_SEPARATOR}{name}")


def allocate_sidecar(registry_root: Path, run_id: str, name: str) -> Path:
    """Create or reuse one run-scoped sidecar directory.

    Unlike the run scratch this is not an atomic claim: provisioning writes
    several artifacts into the same sidecar across separate calls. Foreign
    ownership, a symlink, and a non-owner-only mode are still refused.
    """
    scratch_plan = sidecar_plan(registry_root, run_id, name)
    try:
        _ensure_shared_roots(scratch_plan)
        private_io.ensure_private_owned_dir(scratch_plan.path)
    except OSError as exc:
        raise ScratchSafetyError(f"could not allocate private run scratch: {exc}") from exc
    _require_owned_directory(scratch_plan.path, private=True)
    _require_outside_git_worktree(scratch_plan.path)
    return scratch_plan.path


def _sidecar_paths(scratch_plan: ScratchPlan, run_id: str) -> list[Path]:
    """Existing sidecars of this run, found by name rather than by record."""
    if not scratch_plan.bucket.is_dir() or scratch_plan.bucket.is_symlink():
        return []
    prefix = f"{run_id}{SIDECAR_SEPARATOR}"
    return sorted(entry for entry in scratch_plan.bucket.iterdir() if entry.name.startswith(prefix))


def verify_recorded_paths(
    registry_root: Path,
    run_id: str,
    manifest: dict | None,
    *,
    action: str = "prune",
) -> None:
    """Refuse when a manifest's recorded scratch or temp path is not the owned one.

    Removal only ever touches the deterministic paths derived from registry
    identity and run id, never record bytes. A recorded path that no longer
    matches is conflicting metadata: refuse rather than delete a directory the
    record no longer claims. A manifest without either key carries no pointer
    and passes, because a run that never allocated scratch can still have left
    a sidecar.
    """
    if manifest is None:
        return
    if "scratchPath" in manifest:
        recorded = manifest.get("scratchPath")
        if not isinstance(recorded, str) or not recorded:
            raise ScratchSafetyError(
                f"refusing to {action} run {run_id}: invalid recorded scratch path"
            )
        expected = expected_path(registry_root, run_id)
        recorded_path = Path(os.path.abspath(recorded))
        if recorded_path != expected:
            raise ScratchSafetyError(
                f"refusing to {action} run {run_id}: recorded scratch path {recorded_path} "
                f"does not match current owned path {expected}"
            )
    if "tempPath" in manifest:
        recorded_temp = manifest.get("tempPath")
        if not isinstance(recorded_temp, str) or not recorded_temp:
            raise ScratchSafetyError(
                f"refusing to {action} run {run_id}: invalid recorded temp path"
            )
        expected_temp = expected_compact_temp_path(registry_root, run_id)
        if Path(os.path.abspath(recorded_temp)) != expected_temp:
            raise ScratchSafetyError(
                f"refusing to {action} run {run_id}: recorded temp path {recorded_temp} "
                f"does not match current owned path {expected_temp}"
            )


def owned_targets(registry_root: Path, run_id: str) -> list[Path]:
    """Existing scratch, sidecars, and compact temp of this run, safety-checked.

    Only the deterministic paths are considered. The shared roots must be real
    owner-only directories we own; a hostile or foreign root raises rather than
    being routed around.
    """
    scratch_plan = plan(registry_root, run_id)
    targets: list[Path] = []
    if _path_exists(scratch_plan.bucket):
        _require_owned_directory(scratch_plan.root.parent, private=scratch_plan.fallback)
        for directory in (scratch_plan.root, scratch_plan.bucket):
            _require_owned_directory(directory, private=True)
        if _path_exists(scratch_plan.path):
            targets.append(scratch_plan.path)
        targets.extend(_sidecar_paths(scratch_plan, run_id))
    compact_plan = compact_temp_plan(registry_root, run_id)
    if _path_exists(compact_plan.path):
        _ensure_compact_temp_roots(compact_plan)
        targets.append(compact_plan.path)
    return targets


def tree_bytes(path: Path) -> int:
    """Apparent size of the regular files under ``path``; symlinks are never followed."""
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
        except OSError:
            continue
    return total


class ScratchBudgetExceeded(Exception):
    """The caller's time budget ran out; whatever was freed so far stays freed."""


class Deadline:
    """A monotonic time budget checked between filesystem operations.

    ``Deadline(None)`` never expires. The clock is injectable so a caller with
    its own notion of time (and its tests) stays in charge of it.
    """

    def __init__(self, seconds: float | None, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._limit = None if seconds is None else clock() + seconds

    def expired(self) -> bool:
        return self._limit is not None and self._clock() >= self._limit

    def check(self) -> None:
        if self.expired():
            raise ScratchBudgetExceeded


@dataclass
class RemovalProgress:
    """What a removal freed, and whether it reached the end of every target."""

    freed_bytes: int = 0
    complete: bool = False


_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _safe_removal_available() -> bool:
    return (
        hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.scandir in os.supports_fd
    )


def _scan_for_foreign_owner(target: Path, deadline: Deadline | None) -> None:
    """Refuse a tree holding an entry owned by someone else, before removing anything.

    Symlinks are inspected with ``lstat`` semantics and never entered.
    """
    if not hasattr(os, "geteuid"):
        return
    euid = os.geteuid()
    pending = [target]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if deadline is not None:
                    deadline.check()
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != euid:
                    raise ScratchSafetyError(f"scratch entry has a foreign owner: {entry.path}")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))


def _list_directory(fd: int, deadline: Deadline | None) -> list[tuple[str, bool, int]]:
    """(name, is directory, regular-file size) of each entry, without following symlinks."""
    items: list[tuple[str, bool, int]] = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if deadline is not None:
                deadline.check()
            info = entry.stat(follow_symlinks=False)
            size = info.st_size if stat.S_ISREG(info.st_mode) else 0
            items.append((entry.name, stat.S_ISDIR(info.st_mode), size))
    return items


def _remove_tree(target: Path, deadline: Deadline | None, progress: RemovalProgress) -> None:
    """Remove ``target`` bottom-up, one entry at a time, through directory descriptors.

    Every directory is opened with ``O_NOFOLLOW`` relative to its parent's
    descriptor, so a path swapped for a symlink after it was listed is refused
    rather than followed. The deadline is checked before each entry; when it
    fires the walk stops where it is (``ScratchBudgetExceeded``) and
    ``progress.freed_bytes`` holds what was actually freed. Removal is resumable
    because a half-removed tree is just a smaller tree.
    """
    parent_fd = os.open(target.parent, _DIR_OPEN_FLAGS)
    stack: list[tuple[int, list[tuple[str, bool, int]], str]] = []
    try:
        root_fd = os.open(target.name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
        try:
            stack.append((root_fd, _list_directory(root_fd, deadline), target.name))
        except BaseException:
            os.close(root_fd)
            raise
        while stack:
            fd, remaining, name = stack[-1]
            if not remaining:
                stack.pop()
                os.close(fd)
                container = stack[-1][0] if stack else parent_fd
                os.rmdir(name, dir_fd=container)
                continue
            if deadline is not None:
                deadline.check()
            entry_name, is_directory, size = remaining.pop()
            if is_directory:
                child_fd = os.open(entry_name, _DIR_OPEN_FLAGS, dir_fd=fd)
                try:
                    stack.append((child_fd, _list_directory(child_fd, deadline), entry_name))
                except BaseException:
                    os.close(child_fd)
                    raise
            else:
                os.unlink(entry_name, dir_fd=fd)
                progress.freed_bytes += size
    finally:
        for fd, _remaining, _name in stack:
            os.close(fd)
        os.close(parent_fd)


def remove_targets(targets: list[Path], *, deadline: Deadline | None = None) -> RemovalProgress:
    """Remove ``owned_targets`` results, refusing foreign-owned entries.

    Each target is checked as before (owned, owner-only, outside any Git
    worktree, no foreign-owned entry inside) and then removed incrementally.
    With a ``deadline`` the walk and the removal stop cleanly when it passes:
    the result is incomplete, what was freed stays freed, and a later call
    finishes the rest. Without one it runs to the end.
    """
    progress = RemovalProgress()
    if not targets:
        progress.complete = True
        return progress
    if not _safe_removal_available():
        raise ScratchSafetyError("safe no-follow directory removal is unavailable")
    try:
        for target in targets:
            if deadline is not None:
                deadline.check()
            _require_owned_directory(target, private=True)
            _require_outside_git_worktree(target)
            _scan_for_foreign_owner(target, deadline)
            _remove_tree(target, deadline, progress)
    except ScratchBudgetExceeded:
        return progress
    progress.complete = True
    return progress


def remove_owned(registry_root: Path, run_id: str) -> None:
    """Remove the deterministic scratch, sidecars, and compact temp of this run.

    The compact child temp is removed from exactly here, alongside the run
    scratch: whatever retains the scratch (a non-pruned run, a failed prune)
    retains it too, and a run whose scratch was never allocated has no compact
    temp to find.
    """
    remove_targets(owned_targets(registry_root, run_id))
