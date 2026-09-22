"""Private per-run temporary storage outside workspace Git ancestry."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess  # nosec B404 - fixed offline Git ancestry probe.
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


def remove_owned(registry_root: Path, run_id: str) -> None:
    """Remove the deterministic scratch, sidecars, and compact temp of this run.

    The compact child temp is removed from exactly here, alongside the run
    scratch: whatever retains the scratch (a non-pruned run, a failed prune)
    retains it too, and a run whose scratch was never allocated has no compact
    temp to find.
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
    if not targets:
        return
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ScratchSafetyError("safe no-follow directory removal is unavailable")
    for target in targets:
        _require_owned_directory(target, private=True)
        _require_outside_git_worktree(target)
        for root, directories, files in os.walk(target, topdown=True, followlinks=False):
            for name in [*directories, *files]:
                entry = Path(root) / name
                info = entry.lstat()
                if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                    raise ScratchSafetyError(f"scratch entry has a foreign owner: {entry}")
        shutil.rmtree(target)
