"""Private per-run temporary storage outside workspace Git ancestry."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess  # nosec B404 - fixed offline Git ancestry probe.
from dataclasses import dataclass
from pathlib import Path

from delegate_agent import private_io
from delegate_agent.record_io import RUN_ID_RE

SCRATCH_ROOT_NAME = "run-scratch"
PERSISTENT_TEMP_ROOT = Path("/var/tmp")


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


def allocate_plan(scratch_plan: ScratchPlan) -> Path:
    """Create one previously calculated scratch path."""
    shared_parent = scratch_plan.root.parent
    try:
        if scratch_plan.fallback:
            private_io.ensure_private_owned_dir(shared_parent)
        else:
            private_io.ensure_owned_dir(shared_parent)
        for directory in (scratch_plan.root, scratch_plan.bucket):
            private_io.ensure_private_owned_dir(directory)
            _require_owned_directory(directory, private=True)
            _require_outside_git_worktree(directory)
        private_io.create_private_owned_dir(scratch_plan.path)
    except OSError as exc:
        raise ScratchSafetyError(f"could not allocate private run scratch: {exc}") from exc
    _require_owned_directory(scratch_plan.path, private=True)
    _require_outside_git_worktree(scratch_plan.path)
    return scratch_plan.path


def allocate(registry_root: Path, run_id: str) -> Path:
    return allocate_plan(plan(registry_root, run_id))


def remove_owned(registry_root: Path, run_id: str) -> None:
    """Remove only the deterministic scratch owned by this registry and run."""
    scratch_plan = plan(registry_root, run_id)
    if not _path_exists(scratch_plan.path):
        return
    _require_owned_directory(scratch_plan.root.parent, private=scratch_plan.fallback)
    for directory in (scratch_plan.root, scratch_plan.bucket, scratch_plan.path):
        _require_owned_directory(directory, private=True)
    _require_outside_git_worktree(scratch_plan.path)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ScratchSafetyError("safe no-follow directory removal is unavailable")
    for root, directories, files in os.walk(scratch_plan.path, topdown=True, followlinks=False):
        for name in [*directories, *files]:
            entry = Path(root) / name
            info = entry.lstat()
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise ScratchSafetyError(f"scratch entry has a foreign owner: {entry}")
    shutil.rmtree(scratch_plan.path)
