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
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import subprocess  # nosec B404 - fixed systemd argv, shell=False.
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LaneScope:
    unit: str
    slice: str
    systemd_run: str
    systemctl: str


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


def stop(scope: LaneScope | None, *, run: Runner = subprocess.run) -> None:
    """Stop the lane's scope, killing anything left in it. Never raises.

    A scope whose processes all exited is already gone (``--collect``), and
    the stop then fails harmlessly.
    """
    if scope is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        run(  # nosec B603 - fixed systemctl argv, shell=False.
            [scope.systemctl, "--user", "stop", scope.unit],
            capture_output=True,
            text=True,
            timeout=_STOP_WAIT_SEC,
            check=False,
        )
