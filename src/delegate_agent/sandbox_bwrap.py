"""Linux bubblewrap boundary for zero-copy safe-mode isolation.

The Linux analogue of ``seatbelt.py``: instead of materialising a temporary
copy of the workspace, the engine argv is prefixed with a bubblewrap (bwrap)
invocation that read-only binds the REAL workspace and hides gitignored state
with parity masks. The probed-working shape on lxcfs/containers is deliberate:

- no ``--proc``: a fresh procfs is EPERM under lxcfs overmounts;
- ``/proc`` is bind-mounted instead, which keeps nested sandboxes working
  (a fresh procfs makes ``uid_map`` unwritable);
- no ``--unshare-pid``: it hangs Codex's own sandbox; the engine sandbox
  stays ON inside bwrap as a second layer;
- no ``--unshare-net``.

``$HOME`` is a tmpfs so ambient credentials (e.g. ``~/.ai-profiles/*``
secret files) are invisible while the one engine home directory named by the
child env (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``) is rw-bound on top of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - Delegate launches a fixed bwrap probe argv with shell=False.
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from delegate_agent.config import (
    VALID_SAFE_BACKEND_VALUES,
)
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS, run_git_bytes

SAFE_BACKEND_ENV = "DELEGATE_SAFE_BACKEND"
SAFE_BACKEND_COPY = "copy"
BWRAP_BINARY = "bwrap"
BWRAP_METHOD = "bwrap-ro-bind"

MASK_OVERFLOW_LIMIT = 2000
MASK_KIND_TMPFS = "tmpfs"
MASK_KIND_DEVNULL = "devnull"

BWRAP_MASK_OVERFLOW_WARNING = (
    "bwrap mask overflow: more than "
    f"{MASK_OVERFLOW_LIMIT} gitignored paths; falling back to the copy backend."
)

_ENGINE_HOME_ENV_VARS = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")

# Engine config directories that live directly under $HOME. They are ro-bound
# whenever they exist; an explicit env override (CODEX_HOME,
# CLAUDE_CONFIG_DIR) is rw-bound separately on top.
_OPTIONAL_ENGINE_DOT_DIRS = {
    "codex": ".codex",
    "claude": ".claude",
    "droid": ".factory",
    "kimi": ".kimi",
    "grok": ".grok",
    "pi": ".pi",
    "omp": ".omp",
}
_OPTIONAL_HOME_RO_BINDS = (".local", ".cargo/bin", ".bun")

# System roots bound read-only when they exist. /opt hosts engines installed
# outside /usr (e.g. Codex, symlinked from /usr/local/bin);
# /run/systemd/resolve backs /etc/resolv.conf on systemd-resolved hosts, and
# without it DNS fails inside the boundary.
_OPTIONAL_SYSTEM_RO_BINDS = ("/opt", "/run/systemd/resolve")


class Mask(NamedTuple):
    """One parity mask: a workspace-relative path hidden inside bwrap."""

    path: str  # relative POSIX path from the workspace root, no trailing slash
    kind: str  # MASK_KIND_TMPFS for directories, MASK_KIND_DEVNULL for files


class BwrapMaskOverflow(Exception):
    """Raised when parity masks exceed MASK_OVERFLOW_LIMIT."""


def requested_safe_backend(
    config: Mapping[str, object], env: Mapping[str, str] | None = None
) -> str:
    """Return the configured safe backend: ``copy`` or ``bwrap``.

    ``DELEGATE_SAFE_BACKEND`` overrides ``isolation.safeBackend``. An invalid
    value on either channel fails closed rather than silently falling back.
    """
    environment = os.environ if env is None else env
    sources: tuple[tuple[str, object], ...] = (
        ("DELEGATE_SAFE_BACKEND", environment.get(SAFE_BACKEND_ENV)),
        (
            "config isolation.safeBackend",
            config.get("isolation", {}).get("safeBackend")
            if isinstance(config.get("isolation"), dict)
            else None,
        ),
    )
    for label, value in sources:
        if value is None:
            continue
        if value not in VALID_SAFE_BACKEND_VALUES:
            raise DelegateError(
                "invalid_safe_backend",
                f"{label} must be one of: {', '.join(VALID_SAFE_BACKEND_VALUES)}; got {value!r}.",
            )
        return str(value)
    return SAFE_BACKEND_COPY


def ensure_bwrap_backend() -> None:
    """Raise ``bwrap_unavailable`` unless this host can actually run bwrap."""
    if not sys.platform.startswith("linux"):
        raise DelegateError(
            "bwrap_unavailable",
            "isolation.safeBackend=bwrap requires Linux with bubblewrap installed; "
            "this host is not Linux.",
        )
    bwrap_path = shutil.which(BWRAP_BINARY)
    if bwrap_path is None:
        raise DelegateError(
            "bwrap_unavailable",
            "isolation.safeBackend=bwrap requires the 'bwrap' binary on PATH; "
            "install bubblewrap (e.g. 'apt install bubblewrap') or set "
            'isolation.safeBackend back to "copy".',
        )
    if not bwrap_available(bwrap_path):
        raise DelegateError(
            "bwrap_unavailable",
            "The bubblewrap probe failed on this host (user namespaces may be "
            "disabled); Delegate never falls back silently. Install a working "
            'bubblewrap or set isolation.safeBackend back to "copy".',
        )


def _probe_cache_path() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache_root) / "delegate" / "bwrap-probe.json"


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cache_key(bwrap_path: str) -> str:
    try:
        mtime = os.stat(bwrap_path).st_mtime_ns
    except OSError:
        mtime = 0
    return f"{bwrap_path}:{mtime}:{_boot_id()}"


def _read_probe_cache(bwrap_path: str) -> bool | None:
    try:
        payload = json.loads(_probe_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("key") != _cache_key(bwrap_path):
        return None
    available = payload.get("available")
    return available if isinstance(available, bool) else None


def _write_probe_cache(bwrap_path: str, *, available: bool) -> None:
    # Best-effort cache write; probe correctness never depends on it.
    try:
        path = _probe_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"key": _cache_key(bwrap_path), "available": available}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _run_probe(bwrap_path: str) -> bool:
    result = subprocess.run(  # nosec B603 - fixed probe argv, shell=False.
        [
            bwrap_path,
            "--unshare-user",
            "--die-with-parent",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--symlink",
            "usr/bin",
            "/bin",
            "--bind",
            "/proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "/bin/true",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def bwrap_available(bwrap_path: str | None = None) -> bool:
    """Return whether bwrap can build its boundary on this host (cached)."""
    if not sys.platform.startswith("linux"):
        return False
    resolved = bwrap_path or shutil.which(BWRAP_BINARY)
    if resolved is None:
        return False
    cached = _read_probe_cache(resolved)
    if cached is not None:
        return cached
    try:
        available = _run_probe(resolved)
    except (OSError, subprocess.TimeoutExpired):
        available = False
    _write_probe_cache(resolved, available=available)
    return available


def parity_masks(git_root: str) -> tuple[Mask, ...]:
    """Return parity masks hiding gitignored paths for a zero-copy safe run.

    Uses ``git ls-files -o -i --exclude-standard --directory -z`` from the
    workspace root: untracked-and-ignored entries collapse to their top-level
    directory when a directory entry covers them. Directories become tmpfs
    masks; files become ``/dev/null`` ro-binds. ``.delegate/`` is never masked
    (the run scratch lives there and is rw-bound explicitly). Non-git
    workspaces have no masks by construction (callers pass an empty tuple).
    """
    result = run_git_bytes(
        git_root,
        ["ls-files", "-o", "-i", "--exclude-standard", "--directory", "-z"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    entries = {entry for entry in os.fsdecode(result.stdout).split("\x00") if entry}
    dir_paths = {entry[:-1] for entry in entries if entry.endswith("/")}
    covered_prefixes = tuple(f"{dir_path}/" for dir_path in dir_paths)
    masks: list[Mask] = []
    for entry in sorted(entries):
        relative = entry[:-1] if entry.endswith("/") else entry
        top = relative.split("/", 1)[0]
        if top == ".delegate":
            continue
        if any(relative.startswith(prefix) for prefix in covered_prefixes):
            continue
        masks.append(
            Mask(path=relative, kind=MASK_KIND_TMPFS if entry.endswith("/") else MASK_KIND_DEVNULL)
        )
    if len(masks) > MASK_OVERFLOW_LIMIT:
        raise BwrapMaskOverflow(
            f"{len(masks)} parity masks exceed the limit of {MASK_OVERFLOW_LIMIT}."
        )
    return tuple(masks)


_CORE_RO_BINDS = ("/usr", "/etc")
_SYMLINK_PAIRS = (
    ("usr/lib", "lib"),
    ("usr/lib64", "lib64"),
    ("usr/bin", "bin"),
    ("usr/sbin", "sbin"),
)


def build_bwrap_argv(
    *,
    workspace: str,
    engine_argv: list[str],
    env: Mapping[str, str],
    home: str,
    rw_roots: list[str],
    ro_roots: list[str],
    masks: tuple[Mask, ...] = (),
) -> list[str]:
    """Build the full bwrap argv prefix for one launch. Pure: no filesystem access.

    Mount targets are deduplicated keep-first across every bind/tmpfs/mask so
    no mount point is declared twice. Engine homes named by the child env
    (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``) are appended to ``rw_roots``.
    Emission order matters: system roots first, then ``$HOME`` tmpfs, then the
    read-only workspace, then masks stacked on top of the workspace, then
    writable roots (run scratch, mail-push homes, engine homes) stacked last.
    """
    mounted: set[str] = set()
    argv: list[str] = [
        BWRAP_BINARY,
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
    ]

    def bind(flag: str, *pair: str) -> None:
        target = pair[-1]
        if target in mounted:
            return
        mounted.add(target)
        argv.extend((flag, *pair))

    for root in _CORE_RO_BINDS:
        bind("--ro-bind", root, root)
    for link_target, link_path in _SYMLINK_PAIRS:
        argv.extend(("--symlink", link_target, link_path))
    for root in ro_roots:
        bind("--ro-bind", root, root)
    bind("--dev", "/dev", "/dev")
    bind("--bind", "/proc", "/proc")
    bind("--tmpfs", "/tmp", "/tmp")
    bind("--tmpfs", home, home)
    bind("--ro-bind", workspace, workspace)
    for mask in masks:
        target = os.path.normpath(os.path.join(workspace, mask.path))
        if mask.kind == MASK_KIND_TMPFS:
            bind("--tmpfs", target, target)
        else:
            bind("--ro-bind", "/dev/null", target)
    engine_homes = [
        os.path.expanduser(env[name]) for name in _ENGINE_HOME_ENV_VARS if env.get(name, "").strip()
    ]
    for root in [*rw_roots, *engine_homes]:
        bind("--bind", root, root)
    argv.extend(("--chdir", workspace))
    argv.append("--")
    argv.extend(engine_argv)
    return argv


def bwrap_display_argv(engine_argv: list[str]) -> list[str]:
    """Human-facing truncated bwrap prefix; the full argv lives in the manifest."""
    return [BWRAP_BINARY, "…", "--", *engine_argv]


def wrap_engine_argv(
    *,
    engine_argv: list[str],
    cwd: str,
    env: Mapping[str, str],
    engine: str,
    scratch_dir: str | None = None,
    masks: tuple[Mask, ...] = (),
    home: str | None = None,
    extra_rw_roots: list[str] | None = None,
) -> list[str]:
    """Prefix ``engine_argv`` with the bwrap boundary using live filesystem facts.

    Pure assembly lives in ``build_bwrap_argv``; this wrapper resolves the
    host-dependent inputs: real HOME, existing optional ro-bind roots (home
    dirs, the per-engine dot-directory, system roots), the run scratch dir
    (rw), and any extra rw roots (e.g. mail-push private homes).
    """
    environment = env or {}
    resolved_home = home or environment.get("HOME") or str(Path.home())
    ro_roots: list[str] = []
    candidates = [
        *(os.path.join(resolved_home, rel) for rel in _OPTIONAL_HOME_RO_BINDS),
        *(
            [os.path.join(resolved_home, _OPTIONAL_ENGINE_DOT_DIRS[engine])]
            if engine in _OPTIONAL_ENGINE_DOT_DIRS
            else []
        ),
        *_OPTIONAL_SYSTEM_RO_BINDS,
    ]
    for candidate in candidates:
        if candidate not in ro_roots and os.path.isdir(candidate):
            ro_roots.append(candidate)
    rw_roots = [scratch_dir] if scratch_dir else []
    for root in extra_rw_roots or []:
        if root not in rw_roots:
            rw_roots.append(root)
    return build_bwrap_argv(
        workspace=cwd,
        engine_argv=engine_argv,
        env=environment,
        home=resolved_home,
        rw_roots=rw_roots,
        ro_roots=ro_roots,
        masks=masks,
    )
