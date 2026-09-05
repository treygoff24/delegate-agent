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

import os
import shutil
import subprocess  # nosec B404 - Delegate launches a fixed bwrap probe argv with shell=False.
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from delegate_agent.config import (
    VALID_SAFE_BACKEND_VALUES,
)
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS, run_git_bytes
from delegate_agent.json_types import JsonObject

SAFE_BACKEND_ENV = "DELEGATE_SAFE_BACKEND"
SAFE_BACKEND_COPY = "copy"
BWRAP_BINARY = "bwrap"
BWRAP_METHOD = "bwrap-ro-bind"

MASK_OVERFLOW_LIMIT = 2000
MASK_KIND_TMPFS = "tmpfs"
MASK_KIND_DEVNULL = "devnull"
REGISTRY_DIR_NAME = ".delegate"

# Engine home override per engine: only the SELECTED engine's home is rw-bound
# (a Codex child must never receive Claude's credential home or vice versa).
_ENGINE_HOME_ENV_VAR = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}

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


class Bind(NamedTuple):
    path: str
    mode: str  # "ro" or "rw"


BIND_MODES = ("ro", "rw")


@dataclass(frozen=True)
class SandboxPlan:
    """Validated in-memory bwrap inputs; serialize only for public metadata."""

    bwrap_path: str | None
    masks: tuple[Mask, ...] = ()
    binds: tuple[Bind, ...] = ()

    @property
    def backend(self) -> str:
        return "bwrap"

    def __post_init__(self) -> None:
        if not isinstance(self.masks, tuple) or not isinstance(self.binds, tuple):
            raise DelegateError(
                "invalid_bwrap_plan", "Sandbox masks and binds must be immutable tuples."
            )
        if self.bwrap_path is not None and (
            not isinstance(self.bwrap_path, str)
            or not os.path.isabs(self.bwrap_path)
            or "\0" in self.bwrap_path
        ):
            raise DelegateError("invalid_bwrap_plan", "bwrap executable must be an absolute path.")
        if len(self.masks) > MASK_OVERFLOW_LIMIT:
            raise DelegateError("bwrap_mask_overflow", "Sandbox plan has too many parity masks.")
        for mask in self.masks:
            if (
                not isinstance(mask, Mask)
                or not isinstance(mask.kind, str)
                or mask.kind not in {MASK_KIND_TMPFS, MASK_KIND_DEVNULL}
                or not isinstance(mask.path, str)
                or not mask.path
                or "\0" in mask.path
                or os.path.isabs(mask.path)
                or any(part in {"", ".", ".."} for part in mask.path.split("/"))
            ):
                raise DelegateError(
                    "invalid_bwrap_plan", "Sandbox mask must stay inside its workspace."
                )
        for bind in self.binds:
            if (
                not isinstance(bind, Bind)
                or not isinstance(bind.mode, str)
                or bind.mode not in BIND_MODES
                or not isinstance(bind.path, str)
                or not os.path.isabs(bind.path)
                or "\0" in bind.path
            ):
                raise DelegateError(
                    "invalid_bwrap_plan", "Sandbox bind must have an absolute path and ro/rw mode."
                )

    def payload(self) -> JsonObject:
        return {
            "backend": self.backend,
            "bwrapPath": self.bwrap_path,
            "masks": [{"path": mask.path, "kind": mask.kind} for mask in self.masks],
            "binds": [{"path": bind.path, "mode": bind.mode} for bind in self.binds],
        }


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


def configured_bwrap_binds(config: Mapping[str, object], *, workspace: str) -> tuple[Bind, ...]:
    """Resolve ``isolation.bwrapBinds`` into absolute host paths, fail closed.

    Site-specific launch surfaces (an account broker socket, brokered engine
    home trees, a managed-env contract file) cannot be guessed by the generic
    boundary, so operators declare them. Every entry must be absolute (after
    ``~`` expansion) and exist on the host; paths are canonicalised. A writable
    entry may never intersect the workspace in either direction: a rw mount of
    the workspace or an ancestor would shadow the read-only bind, and a rw
    mount of a descendant would re-open that subtree (or the masked registry)
    for writes. Only the tracked launcher's own run directory is ever rw-bound
    inside the workspace.
    """
    isolation = config.get("isolation")
    entries = isolation.get("bwrapBinds") if isinstance(isolation, Mapping) else None
    if not isinstance(entries, list):
        return ()
    resolved_workspace = Path(workspace).resolve()
    binds: list[Bind] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw_path = entry.get("path")
        mode = entry.get("mode")
        if not isinstance(raw_path, str) or mode not in BIND_MODES:
            continue
        expanded = os.path.expanduser(raw_path)
        if not os.path.isabs(expanded):
            raise DelegateError(
                "invalid_isolation_config",
                f"isolation.bwrapBinds entry {raw_path!r} must be an absolute path "
                "(a leading ~ is expanded).",
            )
        if not os.path.exists(expanded):
            raise DelegateError(
                "bwrap_bind_missing",
                f"isolation.bwrapBinds entry {raw_path!r} does not exist on this host; "
                "remove it or create the path before running with the bwrap backend.",
            )
        expanded = os.path.realpath(expanded)
        target = Path(expanded)
        if mode == "rw" and _paths_intersect(target, resolved_workspace):
            raise DelegateError(
                "bwrap_bind_conflict",
                f"isolation.bwrapBinds entry {raw_path!r} is writable and intersects the "
                f"workspace {workspace}; a rw bind may neither contain nor live inside the "
                "read-only workspace.",
            )
        bind = Bind(path=expanded, mode=mode)
        if bind not in binds:
            binds.append(bind)
    return tuple(binds)


def ensure_bwrap_backend() -> str:
    """Return the absolute bwrap path, or raise ``bwrap_unavailable``.

    The probe runs the production boundary (``build_bwrap_argv`` with
    ``/bin/true`` as the engine) through the exact binary that the launch will
    use. Results are never cached: a cached positive from a different process
    context (another sandbox, a changed PATH) once green-lit a launch that then
    failed, and the probe costs milliseconds.
    """
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
    return bwrap_path


def probe_argv(bwrap_path: str, *, home: str) -> list[str]:
    """The exact production boundary with ``/bin/true`` as the engine.

    ``/usr`` doubles as the read-only workspace so the probe needs no
    filesystem setup; the core ``/usr`` bind already covers it.
    """
    return build_bwrap_argv(
        workspace="/usr",
        engine_argv=["/bin/true"],
        env={},
        home=home,
        rw_roots=[],
        ro_roots=[],
        bwrap_path=bwrap_path,
    )


def _run_probe(bwrap_path: str) -> bool:
    result = subprocess.run(  # nosec B603 - fixed probe argv, shell=False.
        probe_argv(bwrap_path, home=str(Path.home())),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def bwrap_available(bwrap_path: str | None = None) -> bool:
    """Return whether bwrap can build the production boundary on this host now."""
    if not sys.platform.startswith("linux"):
        return False
    resolved = bwrap_path or shutil.which(BWRAP_BINARY)
    if resolved is None:
        return False
    try:
        return _run_probe(resolved)
    except (OSError, subprocess.TimeoutExpired):
        return False


def parity_masks(git_root: str) -> tuple[Mask, ...]:
    """Return parity masks hiding gitignored paths for a zero-copy safe run.

    Uses ``git ls-files -o -i --exclude-standard --directory -z`` from the
    workspace root: untracked-and-ignored entries collapse to their top-level
    directory when a directory entry covers them. Directories become tmpfs
    masks; files become ``/dev/null`` ro-binds. ``.delegate/`` is skipped here
    because ``wrap_engine_argv`` always masks the whole registry with a tmpfs
    (prior runs' prompts, logs and manifests must stay invisible) and then
    rw-binds only the current run's scratch on top. Non-git workspaces have no
    masks by construction (callers pass an empty tuple).
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
    engine: str = "",
    bwrap_path: str = BWRAP_BINARY,
) -> list[str]:
    """Build the full bwrap argv prefix for one launch. Pure: no filesystem access.

    Mount targets are deduplicated keep-first across every bind/tmpfs/mask so
    no mount point is declared twice. Only the selected ``engine``'s home
    override from the child env (``CODEX_HOME`` for codex, ``CLAUDE_CONFIG_DIR``
    for claude) is appended to ``rw_roots``; sibling engine homes stay hidden.
    Emission order matters: core system roots first, then ``$HOME`` tmpfs,
    then the optional read-only roots (several live under ``$HOME``), then the
    read-only workspace, then masks stacked on top of the workspace, then
    writable roots (run scratch, mail-push homes, engine homes) stacked last.
    """
    mounted: set[str] = set()
    argv: list[str] = [
        bwrap_path,
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
    bind("--dev", "/dev")
    bind("--bind", "/proc", "/proc")
    bind("--tmpfs", "/tmp")
    # The HOME tmpfs must precede every HOME-relative ro-bind: bwrap applies
    # mounts in argv order, so a later tmpfs would shadow ~/.local, ~/.bun and
    # the engine dot-dir (observed live: execvp of a wrapper under ~/.local/bin failed with ENOENT).
    bind("--tmpfs", home)
    for root in ro_roots:
        bind("--ro-bind", root, root)
    bind("--ro-bind", workspace, workspace)
    for mask in masks:
        target = os.path.normpath(os.path.join(workspace, mask.path))
        if mask.kind == MASK_KIND_TMPFS:
            bind("--tmpfs", target)
        else:
            bind("--ro-bind", "/dev/null", target)
    home_var = _ENGINE_HOME_ENV_VAR.get(engine)
    engine_homes = (
        [os.path.expanduser(env[home_var])] if home_var and env.get(home_var, "").strip() else []
    )
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
    extra_ro_roots: list[str] | None = None,
    bwrap_path: str | None = None,
) -> list[str]:
    """Prefix ``engine_argv`` with the bwrap boundary using live filesystem facts.

    Pure assembly lives in ``build_bwrap_argv``; this wrapper resolves the
    host-dependent inputs: real HOME, existing optional ro-bind roots (home
    dirs, the per-engine dot-directory, system roots), the run scratch dir
    (rw), and any extra ro/rw roots (configured ``isolation.bwrapBinds``,
    mail-push private homes). The workspace's ``.delegate/`` registry is always
    masked with a tmpfs so prior runs' prompts and logs are invisible; the
    current run's scratch (under it) is then rw-bound on top. Any rw root that
    equals or contains the workspace is refused.
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
    for root in extra_ro_roots or []:
        if root not in ro_roots:
            ro_roots.append(root)
    for engine_file in _engine_binary_files(engine_argv, environment):
        if not _visible_inside(engine_file, ro_roots) and engine_file not in ro_roots:
            ro_roots.append(engine_file)
    rw_roots = [scratch_dir] if scratch_dir else []
    for root in extra_rw_roots or []:
        if root not in rw_roots:
            rw_roots.append(root)
    home_var = _ENGINE_HOME_ENV_VAR.get(engine)
    engine_home = (
        os.path.expanduser(environment[home_var])
        if home_var and environment.get(home_var, "").strip()
        else None
    )
    registry = os.path.join(cwd, REGISTRY_DIR_NAME)
    run_dir = (
        os.path.dirname(os.path.realpath(scratch_dir))
        if scratch_dir
        and Path(os.path.realpath(scratch_dir)).is_relative_to(Path(registry).resolve())
        else None
    )
    _refuse_rw_roots_intersecting_workspace(
        cwd,
        [*rw_roots, *([engine_home] if engine_home else [])],
        internal_allowed=run_dir,
    )
    if os.path.isdir(registry) and not any(mask.path == REGISTRY_DIR_NAME for mask in masks):
        masks = (*masks, Mask(path=REGISTRY_DIR_NAME, kind=MASK_KIND_TMPFS))
    return build_bwrap_argv(
        workspace=cwd,
        engine_argv=engine_argv,
        env=environment,
        home=resolved_home,
        rw_roots=rw_roots,
        ro_roots=ro_roots,
        masks=masks,
        engine=engine,
        bwrap_path=bwrap_path or BWRAP_BINARY,
    )


def _engine_binary_files(engine_argv: list[str], env: Mapping[str, str]) -> list[str]:
    """The engine executable (as found on PATH) and its realpath.

    An engine installed outside the core roots (a wrapper in a temp dir, a
    per-user install under an unusual prefix) would otherwise be invisible:
    the boundary replaces /tmp and $HOME with tmpfs. Only the files themselves
    are bound, never their directories.
    """
    if not engine_argv:
        return []
    resolved = shutil.which(engine_argv[0], path=env.get("PATH") or None)
    if resolved is None:
        return []
    files: list[str] = []
    for candidate in (resolved, os.path.realpath(resolved)):
        if candidate not in files:
            files.append(candidate)
    return files


_ALWAYS_VISIBLE_PREFIXES = tuple(f"{root}/" for root in _CORE_RO_BINDS) + tuple(
    f"/{link_path}/" for _target, link_path in _SYMLINK_PAIRS
)


def _visible_inside(directory: str, ro_roots: list[str]) -> bool:
    path = directory.rstrip("/") + "/"
    if path.startswith(_ALWAYS_VISIBLE_PREFIXES):
        return True
    return any(path.startswith(root.rstrip("/") + "/") for root in ro_roots)


def preflight_plan(argv: list[str], *, timeout: float = 10.0) -> None:
    """Run the exact final boundary with ``/bin/true`` as the engine.

    A missing bind source or a mount the kernel refuses surfaces here as a
    clean ``bwrap_launch_failed`` instead of a half-started child.
    """
    try:
        separator = argv.index("--")
    except ValueError as exc:
        raise DelegateError("bwrap_launch_failed", "malformed bwrap argv (no --)") from exc
    probe = [*argv[: separator + 1], "/bin/true"]
    try:
        result = subprocess.run(  # nosec B603 - the plan under test, shell=False.
            probe,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DelegateError("bwrap_launch_failed", f"bwrap preflight could not run: {exc}") from exc
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr or b"").strip().splitlines()
        first = detail[0][:200] if detail else f"exit {result.returncode}"
        raise DelegateError("bwrap_launch_failed", f"bwrap preflight failed: {first}")


def _paths_intersect(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _refuse_rw_roots_intersecting_workspace(
    workspace: str, rw_roots: list[str], *, internal_allowed: str | None
) -> None:
    """Refuse any rw mount that intersects the workspace in either direction.

    The one permitted exception is the current run's own directory under the
    masked ``.delegate/`` registry (``internal_allowed``): scratch and the
    mail-push private homes live there and must stay writable on top of the
    registry tmpfs.
    """
    resolved_workspace = Path(workspace).resolve()
    allowed = Path(internal_allowed).resolve() if internal_allowed else None
    for root in rw_roots:
        target = Path(root).resolve()
        if allowed is not None and (target == allowed or target.is_relative_to(allowed)):
            continue
        if _paths_intersect(target, resolved_workspace):
            raise DelegateError(
                "bwrap_bind_conflict",
                f"writable root {root} intersects the read-only workspace {workspace}; "
                "refusing to build a boundary that would expose the checkout to writes.",
            )
