"""Run each tracked lane inside the host's capped test slice (Linux only).

The devbox installs ``agent-tests.slice``, one CPU and memory cap shared by all
test work on the host. When that slice is installed as a unit file in the user
manager, each tracked lane starts in its own transient scope under
``agent-tests-<repo>.slice``. Whatever the lane runs, through ``testrun`` or
not, then shares that cap, and stopping the scope when the lane ends kills
anything that escaped the lane's process group.

``systemd-run --scope`` registers the scope and then execs the command in
place, so the lane keeps its pid, process group, environment, cwd, and pipes;
the runner's own signal handling is unchanged. Anywhere the slice is absent
(macOS, no user systemd, no user bus, the slice not installed) or
``DELEGATE_LANE_SLICE=off`` is set, lanes launch exactly as before.

Slice release contract (shared with linux-devbox ``testrun``, keep both in step)
-------------------------------------------------------------------------------
A slice stays loaded in the user manager until something stops it, and
thousands of empty ``agent-tests-<repo>.slice`` units made daemon-reloads take
minutes. So every user of a slice coordinates through one lock file,
``${XDG_RUNTIME_DIR:-/tmp}/testrun/<slice>.lock``: a user holds ``flock -s``
on it for its whole life, and at the end tries ``flock -x -n``; only a user that
gets it, and finds the slice's cgroup with no child directories, runs
``systemctl --user stop --job-mode=fail <slice>``. :func:`acquire` takes the
shared lock before the lane launches and :func:`stop` does the release.

The delegate runner process owns the lock fd, never the lane: it outlives the
lane (it waits on it and stops its scope), the fd is close-on-exec and
``Popen(close_fds=True)`` so no lane command or daemon it spawns inherits it,
and a runner that dies by SIGKILL drops the lock while the orphaned lane's
scope still keeps the slice's cgroup non-empty, which the release check honours.
Every step is fail-open: a lock or stop problem never fails the lane. If the
shared lock cannot be taken within :data:`_LOCK_WAIT_SEC`, the lane launches
unwrapped (no scope, as when :func:`plan` is None) rather than unlocked in the
slice, since a releaser could then stop the slice under a running scope.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import subprocess  # nosec B404 - fixed systemd argv, shell=False.
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

try:  # not available on Windows; the slice is Linux-only anyway
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

PARENT_SLICE = "agent-tests.slice"
OPT_OUT_ENV = "DELEGATE_LANE_SLICE"
_OFF_VALUES = frozenset({"0", "off", "false", "no"})
# The user bus is found through these; a child env missing them would make
# systemd-run fail before the lane starts.
BUS_ENV_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
# A scope stop sends SIGTERM, then SIGKILL after this many seconds (systemd's
# default is 90), so a lane-end stop never hangs on a process ignoring SIGTERM.
STOP_TIMEOUT_SEC = 5
_PROBE_TIMEOUT_SEC = 5.0
_STOP_WAIT_SEC = 15.0
# Longest the lane waits to take the shared slice lock: an exclusive holder
# only holds it for one systemctl show plus stop.
_LOCK_WAIT_SEC = 30.0
LOCK_SUBDIR = "testrun"
CGROUP_ROOT = "/sys/fs/cgroup"

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class SliceLock:
    """The lane's shared lock on its slice; ``fd`` is None when not held."""

    fd: int | None = None
    path: Path | None = None

    def __del__(self) -> None:
        # Safety net for an exit path that never reached release(): closing
        # the fd drops the lock; no slice stop is attempted from here.
        fd, self.fd = self.fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


@dataclass(frozen=True)
class LaneScope:
    unit: str
    slice: str
    systemd_run: str
    systemctl: str
    lock: SliceLock = field(default_factory=SliceLock, compare=False, repr=False)


def repo_slug(path: str | None) -> str:
    """The slice component for a repo: ``[a-z0-9_]`` only.

    A dash in a slice name means nesting (``a-b.slice`` sits under
    ``a.slice``), so ``delegate-agent`` must become ``delegate_agent`` or it
    would share a ``delegate`` repo's weight.
    """
    name = Path(path).name if path else ""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:48]
    return slug or "workspace"


def _unit_token(run_id: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", run_id).strip("_")[:48]
    return token or "run"


def plan(
    run_id: str,
    repo_path: str | None,
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
) -> LaneScope | None:
    """The scope this lane should run in, or None to launch it unwrapped."""
    env = os.environ if environ is None else environ
    if not platform.startswith("linux"):
        return None
    if env.get(OPT_OUT_ENV, "").strip().lower() in _OFF_VALUES:
        return None
    systemd_run = which("systemd-run")
    systemctl = which("systemctl")
    if systemd_run is None or systemctl is None:
        return None
    try:
        probe = run(  # nosec B603 - fixed systemctl argv, shell=False.
            [systemctl, "--user", "show", PARENT_SLICE, "-p", "LoadState", "-p", "FragmentPath"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SEC,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    fields = dict(line.split("=", 1) for line in (probe.stdout or "").splitlines() if "=" in line)
    # Only an installed unit file counts: a slice systemd created implicitly
    # (as the parent of some child) is loaded but carries no cap.
    if fields.get("LoadState") != "loaded" or not fields.get("FragmentPath"):
        return None
    unit = f"delegate-{_unit_token(run_id)}-{secrets.token_hex(3)}.scope"
    return LaneScope(
        unit=unit,
        slice=f"agent-tests-{repo_slug(repo_path)}.slice",
        systemd_run=systemd_run,
        systemctl=systemctl,
    )


def wrap_argv(argv: list[str], scope: LaneScope) -> list[str]:
    """Prefix a launch argv so it runs in ``scope``; the outermost wrapper."""
    return [
        scope.systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={scope.unit}",
        f"--slice={scope.slice}",
        "-p",
        f"TimeoutStopSec={STOP_TIMEOUT_SEC}",
        "--",
        *argv,
    ]


def ensure_bus_env(env: dict[str, str], parent: Mapping[str, str] | None = None) -> None:
    """Keep the user-bus variables systemd-run needs in the child env."""
    source = os.environ if parent is None else parent
    for key in BUS_ENV_KEYS:
        if key not in env and key in source:
            env[key] = source[key]


def stop(scope: LaneScope | None, *, run: Runner = subprocess.run, launched: bool = True) -> None:
    """Stop the lane's scope, killing anything left in it. Never raises.

    A scope whose processes all exited is already gone (``--collect``), and
    the stop then fails harmlessly. ``launched=False`` (the lane never started)
    skips the scope stop. Either way the slice lock is then released.
    """
    if scope is None:
        return
    try:
        if launched:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                run(  # nosec B603 - fixed systemctl argv, shell=False.
                    [scope.systemctl, "--user", "stop", scope.unit],
                    capture_output=True,
                    text=True,
                    timeout=_STOP_WAIT_SEC,
                    check=False,
                )
    finally:
        # Only after the scope is gone: the slice is stopped only if childless.
        release(scope, run=run)


def lock_path(slice_name: str, environ: Mapping[str, str] | None = None) -> Path:
    """The shared lock file for ``slice_name``, the same path testrun uses."""
    env = os.environ if environ is None else environ
    # testrun's rule: XDG_RUNTIME_DIR, else /run/user/<uid> when it exists, else /tmp.
    base = env.get("XDG_RUNTIME_DIR")
    if not base:
        user_run = Path(f"/run/user/{os.getuid()}")
        base = str(user_run) if user_run.is_dir() else "/tmp"  # nosec B108 - testrun's fallback.
    return Path(base) / LOCK_SUBDIR / f"{slice_name}.lock"


def acquire(
    scope: LaneScope | None,
    *,
    environ: Mapping[str, str] | None = None,
    wait_sec: float = _LOCK_WAIT_SEC,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Take the shared slice lock before the lane launches; True when held.

    Never raises. False means the lane must launch unwrapped, with no scope:
    a lane started in the slice without the lock could have its slice stopped
    by a releaser that sees an empty cgroup just before the scope appears
    (``--job-mode=fail`` covers pending jobs, not a scope that already
    started). One line says why. A None scope returns False (nothing to hold).
    """
    if scope is None or fcntl is None:
        return False
    if scope.lock.fd is not None:
        return True
    say = log if log is not None else _log
    fd: int | None = None
    try:
        path = lock_path(scope.slice, environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        deadline = time.monotonic() + max(wait_sec, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    except (OSError, ValueError) as exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(Exception):
            say(
                f"delegate: slice lock for {scope.slice} unavailable ({exc!r}); "
                "launching the lane without a cgroup scope"
            )
        return False
    scope.lock.fd = fd
    scope.lock.path = path
    return True


def _log(message: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        print(message, file=sys.stderr)


def _slice_is_childless(
    scope: LaneScope, *, run: Runner, cgroup_root: str, env: Mapping[str, str] | None
) -> bool:
    shown = run(  # nosec B603 - fixed systemctl argv, shell=False.
        [scope.systemctl, "--user", "show", "-p", "ControlGroup", "--value", scope.slice],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SEC,
        check=False,
        **({"env": dict(env)} if env is not None else {}),
    )
    cgroup = (shown.stdout or "").strip()
    if shown.returncode != 0 or not cgroup.startswith("/"):
        return False
    with os.scandir(cgroup_root.rstrip("/") + cgroup) as entries:
        return not any(entry.is_dir(follow_symlinks=False) for entry in entries)


def release(
    scope: LaneScope | None,
    *,
    run: Runner = subprocess.run,
    cgroup_root: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Drop the shared lock; stop the slice if this was its last user. Never raises."""
    if scope is None:
        return
    fd, scope.lock.fd = scope.lock.fd, None
    if fd is None or fcntl is None:
        return
    try:
        # Convert the shared lock on the same fd, as testrun does: it fails
        # while any other user still holds the slice.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _slice_is_childless(scope, run=run, cgroup_root=cgroup_root or CGROUP_ROOT, env=environ):
            run(  # nosec B603 - fixed systemctl argv, shell=False.
                [scope.systemctl, "--user", "stop", "--job-mode=fail", scope.slice],
                capture_output=True,
                text=True,
                timeout=_STOP_WAIT_SEC,
                check=False,
                **({"env": dict(environ)} if environ is not None else {}),
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
