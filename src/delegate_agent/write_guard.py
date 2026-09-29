"""Work write guard: keep work lanes off irreplaceable paths.

Work mode runs the child with the caller's own filesystem rights, and the
estate's policy profile turns the engines' own sandboxes off. A confused lane
(``rm -rf ~``, a stray redirect into a sibling checkout) can therefore destroy
things nobody can recreate. This module owns the *policy*: a named list of
protected paths, the writable re-opens a lane still needs, and the plan a
backend (bubblewrap on Linux, Seatbelt on macOS) turns into mounts or rules.

It is deliberately a protect-list, not "HOME read-only plus an allowlist":
everything a lane legitimately touches (caches, toolchains, dotfiles it edits)
stays writable, and only the named paths are locked. The list is configurable
(``isolation.writeGuard``), extendable per run (``--writable PATH``), and
recorded in the run manifest and dry-run output.

Pure planning lives here; the backends live in ``sandbox_bwrap`` and
``seatbelt``, and the launch seam in ``runner``.

Also here: the ``--forbid-commit`` git hook injection, which is independent of
the sandbox and works in every isolation mode.
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject

ENV_OVERRIDE = "DELEGATE_WRITE_GUARD"
_FALSE_VALUES = frozenset({"0", "false", "off", "no"})
_TRUE_VALUES = frozenset({"1", "true", "on", "yes"})

BACKEND_BWRAP = "bwrap"
BACKEND_SEATBELT = "seatbelt"
BACKEND_CODEX_NATIVE = "codex-native-sandbox"

ON_UNAVAILABLE_WARN = "warn"
ON_UNAVAILABLE_REFUSE = "refuse"
VALID_ON_UNAVAILABLE = (ON_UNAVAILABLE_WARN, ON_UNAVAILABLE_REFUSE)

DEFAULT_CODE_ROOT = "~/Code"

# Credential stores and estate state a lane must never rewrite. Read access is
# untouched; only writes, deletes and renames are refused.
DEFAULT_PROTECTED = (
    "~/.ssh",
    "~/.gnupg",
    "~/.ai-profiles",
    "~/.config/gh",
    "~/.config/gcloud",
    "~/.aws",
    "~/.azure",
    "~/.kube",
    "~/.netrc",
    "~/.git-credentials",
    "~/.password-store",
    "~/.local/bin/delegate",
)

# The installed Delegate runtime. Run scratch, worktrees and caches under
# ~/.delegate stay writable.
DELEGATE_HOME_RELATIVE = ".delegate"
DELEGATE_INSTALL_PROTECTED = ("src", "releases", "bin")
DELEGATE_CONFIG_GLOB = "config*.json"

# Package-manager caches under HOME. A protect-list backend leaves these
# writable by construction; the Codex native sandbox does not, so its writable
# roots add the ones that exist.
DEFAULT_HOME_CACHES = (
    "~/.cache",
    "~/.npm",
    "~/.cargo/registry",
    "~/.cargo/git",
    "~/.bun/install/cache",
    "~/.local/share/pnpm",
    "~/Library/Caches",
    "~/Library/pnpm",
)

CONFIG_KEYS = frozenset(
    {
        "enabled",
        "onUnavailable",
        "macosSeatbelt",
        "codeRoot",
        "add",
        "remove",
        "writable",
        "homeCaches",
    }
)

NOTE_PROTECT_LIST = (
    "Delegate write guard: this run cannot write to irreplaceable paths (SSH and GPG keys, "
    "credential stores, agent profile homes, the Delegate install{code_root}); a write there "
    "fails with a read-only or permission error. Work only inside this workspace and its git "
    "directory, and report a blocked write instead of working around it."
)
NOTE_CODEX_NATIVE = (
    "Delegate write guard: this run is sandboxed by Codex to this workspace, its git "
    "directory, scratch and temp directories; a write anywhere else fails with a permission "
    "error. Report a blocked write instead of working around it."
)

NOTE_FORBID_COMMIT_IN_PLACE = (
    "Delegate commit policy: --forbid-commit is active for this run. Commits are refused "
    "by hooks in this checkout: do not run `git commit` or create commits, and leave file "
    "changes uncommitted. Delegate marks the run failed if the branch gained commits "
    "since launch."
)

FORBID_COMMIT_HOOKS = (
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "pre-merge-commit",
)
FORBID_COMMIT_MESSAGE = (
    "delegate: --forbid-commit is active for this run, so commits are refused. "
    "Leave your changes uncommitted; the caller reviews and commits them."
)
FORBID_COMMIT_HOOKS_DIRNAME = "forbid-commit-hooks"


class Reopen(NamedTuple):
    """A path a lane may write although it sits inside a protected path."""

    path: str
    reason: str


@dataclass(frozen=True)
class WriteGuardSettings:
    """Resolved guard configuration for one run. Paths keep a leading ``~``."""

    enabled: bool = True
    on_unavailable: str = ON_UNAVAILABLE_WARN
    macos_seatbelt: bool = False
    code_root: str | None = DEFAULT_CODE_ROOT
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()
    home_caches: tuple[str, ...] = DEFAULT_HOME_CACHES
    # Absolute paths from ``--writable`` (resolved against the caller's cwd).
    run_writable: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardFacts:
    """Launch-time facts the plan is built from."""

    home: str
    exec_root: str
    git_common_dir: str | None = None
    registry_root: str | None = None
    # Every other path Delegate itself makes this run write: scratch, compact temp,
    # mail-push homes.
    run_roots: tuple[Reopen, ...] = ()
    # The selected engine's home. It comes from the environment, so it is checked
    # before it may reopen anything inside a protected path (see plan_guard).
    home_candidates: tuple[Reopen, ...] = ()


@dataclass(frozen=True)
class GuardPlan:
    backend: str
    protected: tuple[str, ...]
    writable: tuple[Reopen, ...]
    code_root: str | None = None
    # Engine-home candidates plan_guard declined to reopen, with the reason.
    refused: tuple[Reopen, ...] = ()

    def mounts(self, pin: Sequence[str] = ()) -> tuple[tuple[str, str], ...]:
        """Ordered ``(mode, path)`` pairs; a later entry overrides an earlier one.

        Parents come before children, so a child's mode always wins inside its
        parent. bwrap applies them as mounts and Seatbelt as rules; both give
        "last match wins" semantics for nested paths.

        ``pin`` names paths that must appear as their own mount even when their
        mode already matches the parent's (bwrap only). A mount point cannot be
        renamed or removed, so pinning the execution root is what stops a lane
        moving its own checkout away when that checkout sits outside every
        protected path (a worktree under ``~/.delegate/worktrees``). Paths must
        be real paths; an existing entry keeps its mode.
        """
        entries: dict[str, str] = {path: "ro" for path in self.protected}
        for reopen in self.writable:
            entries[reopen.path] = "rw"
        pinned = {path for path in pin if path and path != os.sep}
        for path in pinned:
            entries.setdefault(path, "rw")
        kept: list[tuple[str, str]] = []
        for path in sorted(entries, key=lambda p: (_depth(p), p)):
            mode = entries[path]
            parent_mode = "rw"
            for kept_mode, kept_path in reversed(kept):
                if _is_within(path, kept_path):
                    parent_mode = kept_mode
                    break
            if mode == parent_mode and path not in pinned:
                continue
            kept.append((mode, path))
        return tuple(kept)

    def payload(self) -> JsonObject:
        payload: JsonObject = {
            "backend": self.backend,
            "codeRoot": self.code_root,
            "protected": list(self.protected),
            "writable": [{"path": r.path, "reason": r.reason} for r in self.writable],
        }
        if self.refused:
            payload["refused"] = [{"path": r.path, "reason": r.reason} for r in self.refused]
        return payload


def _depth(path: str) -> int:
    return len([part for part in path.split(os.sep) if part])


def _is_within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def expand(raw: str, home: str) -> str:
    """Expand a leading ``~`` against ``home`` (not the ambient HOME)."""
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return os.path.join(home, raw[2:])
    return raw


def _real(path: str) -> str:
    return os.path.realpath(path)


def default_home() -> str:
    return os.environ.get("HOME") or str(Path.home())


def validate_config_section(section: object) -> str | None:
    """Return an error message for a bad ``isolation.writeGuard`` value, or None."""
    if section is None:
        return None
    if not isinstance(section, dict):
        return "isolation.writeGuard must be an object."
    unknown = sorted(set(section) - CONFIG_KEYS)
    if unknown:
        return (
            f"isolation.writeGuard has unknown keys: {', '.join(unknown)}; "
            f"allowed keys are {', '.join(sorted(CONFIG_KEYS))}."
        )
    for key in ("enabled", "macosSeatbelt"):
        if key in section and not isinstance(section[key], bool):
            return f"isolation.writeGuard.{key} must be a boolean."
    if "onUnavailable" in section and section["onUnavailable"] not in VALID_ON_UNAVAILABLE:
        return (
            f"isolation.writeGuard.onUnavailable must be one of: {', '.join(VALID_ON_UNAVAILABLE)}."
        )
    if "codeRoot" in section:
        code_root = section["codeRoot"]
        if code_root is not None and (
            not isinstance(code_root, str) or _bad_path_entry(code_root) is not None
        ):
            return "isolation.writeGuard.codeRoot must be null or an absolute path (a leading ~ is expanded)."
    for key in ("add", "remove", "writable", "homeCaches"):
        if key not in section:
            continue
        entries = section[key]
        if not isinstance(entries, list):
            return f"isolation.writeGuard.{key} must be a list of absolute paths."
        for index, entry in enumerate(entries):
            if not isinstance(entry, str) or _bad_path_entry(entry) is not None:
                return (
                    f"isolation.writeGuard.{key}[{index}] must be an absolute path "
                    "(a leading ~ is expanded)."
                )
    return None


def _bad_path_entry(value: str) -> str | None:
    if not value.strip() or "\0" in value:
        return "empty"
    if value == "~" or value.startswith("~/") or os.path.isabs(value):
        return None
    return "not absolute"


def settings_from_config(
    config: Mapping[str, object], env: Mapping[str, str] | None = None
) -> WriteGuardSettings:
    """Resolve ``isolation.writeGuard`` and the ``DELEGATE_WRITE_GUARD`` override."""
    environment = os.environ if env is None else env
    isolation = config.get("isolation")
    section = isolation.get("writeGuard") if isinstance(isolation, dict) else None
    defaults = WriteGuardSettings()
    if not isinstance(section, dict):
        section = {}

    def strings(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = section.get(key)
        if not isinstance(value, list):
            return default
        return tuple(entry for entry in value if isinstance(entry, str))

    enabled = section.get("enabled")
    override = environment.get(ENV_OVERRIDE, "").strip().lower()
    if override in _FALSE_VALUES:
        enabled = False
    elif override in _TRUE_VALUES:
        enabled = True
    code_root = section.get("codeRoot", defaults.code_root)
    return WriteGuardSettings(
        enabled=enabled if isinstance(enabled, bool) else defaults.enabled,
        on_unavailable=(
            section["onUnavailable"]
            if section.get("onUnavailable") in VALID_ON_UNAVAILABLE
            else defaults.on_unavailable
        ),
        macos_seatbelt=section.get("macosSeatbelt") is True,
        code_root=code_root if isinstance(code_root, str) else None,
        add=strings("add", ()),
        remove=strings("remove", ()),
        writable=strings("writable", ()),
        home_caches=strings("homeCaches", defaults.home_caches),
    )


def resolve_run_writable(
    paths: Iterable[str], *, cwd: str, home: str | None = None
) -> tuple[str, ...]:
    """Resolve ``--writable`` values to existing absolute real paths."""
    base = home or default_home()
    resolved: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise DelegateError("invalid_writable_path", "--writable requires a non-empty path.")
        expanded = expand(raw, base)
        absolute = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
        if not os.path.exists(absolute):
            raise DelegateError(
                "invalid_writable_path",
                f"--writable path {raw!r} does not exist; create it first or name an existing "
                "parent directory.",
            )
        real = _real(absolute)
        if real not in resolved:
            resolved.append(real)
    return tuple(resolved)


def with_run_writable(settings: WriteGuardSettings, paths: Sequence[str]) -> WriteGuardSettings:
    if not paths:
        return settings
    merged = tuple(dict.fromkeys((*settings.run_writable, *paths)))
    return replace(settings, run_writable=merged)


def protected_candidates(settings: WriteGuardSettings, home: str) -> list[str]:
    """Expanded protected paths before the existence filter, in stable order."""
    removed = {_real(expand(entry, home)) for entry in settings.remove}
    removed |= {os.path.normpath(expand(entry, home)) for entry in settings.remove}
    candidates: list[str] = [expand(entry, home) for entry in (*DEFAULT_PROTECTED, *settings.add)]
    delegate_home = os.path.join(home, DELEGATE_HOME_RELATIVE)
    candidates.extend(os.path.join(delegate_home, name) for name in DELEGATE_INSTALL_PROTECTED)
    candidates.extend(
        sorted(glob.glob(os.path.join(glob.escape(delegate_home), DELEGATE_CONFIG_GLOB)))
    )
    if settings.code_root:
        candidates.append(expand(settings.code_root, home))
    kept: list[str] = []
    for candidate in candidates:
        if os.path.normpath(candidate) in removed or _real(candidate) in removed:
            continue
        if candidate not in kept:
            kept.append(candidate)
    return kept


# A directory is another engine or profile home when it holds one of these
# identity files. Claude keeps ``.claude.json`` (and ``.credentials.json`` on
# Linux) at the top of its config directory; Codex keeps ``auth.json`` beside
# ``config.toml``. ``auth.json`` alone is too common to mean anything.
_HOME_IDENTITY_FILES = (".claude.json", ".credentials.json")
_HOME_IDENTITY_PAIR = ("auth.json", "config.toml")
# Engine content directories never hold a sibling profile, and can be huge.
_HOME_SCAN_SKIP = frozenset(
    {
        ".git",
        "backups",
        "cache",
        "file-history",
        "node_modules",
        "plugins",
        "projects",
        "sessions",
        "shell-snapshots",
        "skills",
        "todos",
    }
)
_HOME_SCAN_DEPTH = 3
_HOME_SCAN_MAX_DIRS = 2000


def _looks_like_engine_home(directory: str) -> bool:
    if any(os.path.lexists(os.path.join(directory, name)) for name in _HOME_IDENTITY_FILES):
        return True
    return all(os.path.lexists(os.path.join(directory, name)) for name in _HOME_IDENTITY_PAIR)


@dataclass(frozen=True)
class NestedHomeScan:
    """What a bounded scan below an engine-home candidate established."""

    found: str | None = None
    incomplete: str | None = None


def scan_for_nested_home(directory: str) -> NestedHomeScan:
    """Look below ``directory`` for another engine or profile home.

    The scan does not follow symlinks (a write through one lands on its real
    target, which a reopen by real path does not cover). Directories deeper than
    ``_HOME_SCAN_DEPTH`` are checked for identity files but not listed. A scan
    that hits ``_HOME_SCAN_MAX_DIRS`` or cannot list a directory is reported as
    incomplete, and the caller fails closed: an unproven scope is never reopened.
    """
    pending: list[tuple[str, int]] = [(directory, 0)]
    scanned = 0
    while pending:
        current, depth = pending.pop()
        if depth >= _HOME_SCAN_DEPTH:
            continue
        if scanned >= _HOME_SCAN_MAX_DIRS:
            return NestedHomeScan(incomplete=f"more than {_HOME_SCAN_MAX_DIRS} directories")
        scanned += 1
        try:
            with os.scandir(current) as entries:
                children = sorted(
                    entry.path
                    for entry in entries
                    if entry.name not in _HOME_SCAN_SKIP and entry.is_dir(follow_symlinks=False)
                )
        except OSError as exc:
            return NestedHomeScan(incomplete=f"{current} could not be listed ({exc.strerror})")
        for child in children:
            if _looks_like_engine_home(child):
                return NestedHomeScan(found=child)
            pending.append((child, depth + 1))
    return NestedHomeScan()


def other_profile_home_within(directory: str) -> str | None:
    """A directory below ``directory`` that looks like another engine or profile home."""
    return scan_for_nested_home(directory).found


def home_candidate_problem(real: str, protected: Sequence[str]) -> str | None:
    """Why an engine-home candidate must not reopen protected ground, or None.

    A candidate outside every protected path reopens nothing, so it is fine. One
    inside a protected path is reopened only when it is provably a single
    profile: it must itself carry an engine identity file (``.claude.json``,
    ``.credentials.json``, or Codex's ``auth.json`` with ``config.toml``), and a
    complete bounded scan below it must find no other profile home. So an
    environment variable naming a parent such as ``~/.ai-profiles/accounts/claude``
    (no identity file of its own) is refused even when its children carry no
    markers, and a scan that could not finish refuses rather than allows. A
    protected path nested inside the candidate stays protected: mounts and rules
    apply deepest-last.
    """
    if not any(_is_within(real, path) for path in protected):
        return None
    if real in protected:
        return "is itself a protected path; use writable or --writable to lift it"
    if not _looks_like_engine_home(real):
        return (
            "is not itself a profile home (no .claude.json, .credentials.json, or "
            "auth.json with config.toml), so it may hold other profiles; name it with "
            "writable or --writable to open it deliberately"
        )
    scan = scan_for_nested_home(real)
    if scan.found is not None:
        return f"contains another profile home ({scan.found})"
    if scan.incomplete is not None:
        return f"could not be proven to hold no other profile ({scan.incomplete})"
    return None


def plan_guard(settings: WriteGuardSettings, facts: GuardFacts, *, backend: str) -> GuardPlan:
    """Build the protect/re-open plan from settings and launch facts.

    Protected paths that do not exist are dropped (a bind of a missing source
    fails, and there is nothing to lose); so are re-opens that do not exist.
    The payload lists only re-opens that matter: those inside a protected path,
    plus any explicit re-open of a protected path itself. Engine-home candidates
    are checked first (``home_candidate_problem``); refused ones are reported in
    the plan and reopen nothing.
    """
    home = facts.home
    protected: list[str] = []
    for candidate in protected_candidates(settings, home):
        real = _real(candidate)
        if os.path.exists(real) and real not in protected:
            protected.append(real)
    reopens: dict[str, str] = {}

    def reopen(path: str | None, reason: str) -> None:
        if not path:
            return
        if not os.path.exists(path):
            return
        real = _real(path)
        reopens.setdefault(real, reason)

    reopen(facts.exec_root, "execution root")
    reopen(facts.git_common_dir, "git common dir")
    reopen(facts.registry_root, "run registry")
    for root in facts.run_roots:
        reopen(root.path, root.reason)
    refused: list[Reopen] = []
    for candidate in facts.home_candidates:
        if not candidate.path or not os.path.isdir(candidate.path):
            continue
        real = _real(candidate.path)
        problem = home_candidate_problem(real, protected)
        if problem is None:
            reopens.setdefault(real, candidate.reason)
        else:
            refused.append(Reopen(real, f"{candidate.reason} not reopened: {problem}"))
    for entry in settings.writable:
        reopen(expand(entry, home), "isolation.writeGuard.writable")
    for entry in settings.run_writable:
        reopen(entry, "--writable")
    # An explicit re-open of a protected path replaces the protection; it stays
    # listed so the manifest shows the operator's choice.
    lifted = {path for path in protected if path in reopens}
    protected = [path for path in protected if path not in lifted]
    plan = GuardPlan(
        backend=backend,
        protected=tuple(protected),
        writable=tuple(Reopen(path, reason) for path, reason in reopens.items()),
        code_root=_real(expand(settings.code_root, home)) if settings.code_root else None,
        refused=tuple(refused),
    )
    effective = {path for mode, path in plan.mounts() if mode == "rw"} | lifted
    return replace(
        plan, writable=tuple(entry for entry in plan.writable if entry.path in effective)
    )


def native_writable_roots(settings: WriteGuardSettings, facts: GuardFacts) -> tuple[Reopen, ...]:
    """Extra writable roots for the Codex native ``workspace-write`` sandbox.

    Codex already writes its cwd, ``/tmp`` and ``$TMPDIR``. It protects ``.git``
    even under a writable root, so the git common dir is always added as its own
    root. The rest are omitted when they already sit inside the execution root.
    """
    home = facts.home
    exec_real = _real(facts.exec_root) if facts.exec_root else ""
    roots: dict[str, str] = {}

    def add(path: str | None, reason: str, *, force: bool = False) -> None:
        if not path or not os.path.isdir(path):
            return
        real = _real(path)
        if not force and exec_real and _is_within(real, exec_real):
            return
        roots.setdefault(real, reason)

    add(facts.git_common_dir, "git common dir", force=True)
    add(facts.registry_root, "run registry")
    for root in facts.run_roots:
        add(root.path, root.reason)
    for entry in settings.writable:
        add(expand(entry, home), "isolation.writeGuard.writable")
    for entry in settings.run_writable:
        add(entry, "--writable")
    for entry in settings.home_caches:
        add(expand(entry, home), "home cache")
    return tuple(Reopen(path, reason) for path, reason in roots.items())


def codex_native_sandbox_on(argv: Sequence[str]) -> bool:
    """Whether a Codex argv keeps Codex's own ``workspace-write`` sandbox on."""
    if "--dangerously-bypass-approvals-and-sandbox" in argv:
        return False
    for index, token in enumerate(argv[:-1]):
        if token == "--sandbox" and argv[index + 1] == "workspace-write":
            return True
    return False


def predicted_backend(
    settings: WriteGuardSettings,
    *,
    engine: str,
    argv: Sequence[str] = (),
    codex_native: bool | None = None,
) -> str | None:
    """The backend this host would use, from cheap probes only (no launch).

    ``codex_native`` overrides the argv probe when the argv is not built yet
    (prompt framing runs before the engine argv exists).
    """
    if not settings.enabled:
        return None
    native = codex_native_sandbox_on(argv) if codex_native is None else codex_native
    if engine == "codex" and native:
        return BACKEND_CODEX_NATIVE
    if sys.platform.startswith("linux"):
        return BACKEND_BWRAP if shutil.which("bwrap") else None
    if sys.platform == "darwin":
        if settings.macos_seatbelt and shutil.which("sandbox-exec"):
            return BACKEND_SEATBELT
        return None
    return None


def prompt_note(
    settings: WriteGuardSettings, backend: str | None, *, home: str | None = None
) -> str | None:
    """The two-sentence work-mode prompt note for the predicted backend."""
    if backend is None:
        return None
    if backend == BACKEND_CODEX_NATIVE:
        return NOTE_CODEX_NATIVE
    code_root = ""
    if settings.code_root:
        code_root = (
            f", and other checkouts under {expand(settings.code_root, home or default_home())}"
        )
    return NOTE_PROTECT_LIST.format(code_root=code_root)


STATUS_OFF = "off"
STATUS_UNAVAILABLE = "unavailable"


def unguarded(settings: WriteGuardSettings) -> tuple[str, str]:
    """``(status, reason)`` for a host with no guard backend, for dry-run and warnings.

    ``off`` means the operator (or the platform default) chose no guard; only
    ``unavailable`` means a wanted backend is missing and ``onUnavailable`` applies.
    """
    if not settings.enabled:
        return STATUS_OFF, "disabled by isolation.writeGuard.enabled or DELEGATE_WRITE_GUARD"
    if sys.platform.startswith("linux"):
        return STATUS_UNAVAILABLE, "bubblewrap (bwrap) is not installed"
    if sys.platform == "darwin":
        if not settings.macos_seatbelt:
            return STATUS_OFF, "isolation.writeGuard.macosSeatbelt is off"
        return STATUS_UNAVAILABLE, "sandbox-exec is not installed"
    return STATUS_OFF, "no write guard backend on this platform"


# --- --forbid-commit: git hooks that refuse commits, in every isolation mode ---


def install_forbid_commit_hooks(directory: Path) -> Path:
    """Write refusing hooks into a run-owned directory and return it."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    script = f"#!/bin/sh\necho '{FORBID_COMMIT_MESSAGE}' >&2\nexit 1\n"
    for name in FORBID_COMMIT_HOOKS:
        hook = directory / name
        hook.write_text(script, encoding="utf-8")
        hook.chmod(0o700)
    return directory


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def forbid_commit_env(hooks_dir: str, env: Mapping[str, str]) -> dict[str, str]:
    """Env additions pointing git at the refusing hooks for this child.

    One variable, ``GIT_CONFIG_PARAMETERS``, carries the whole setting, and its
    name has no KEY, SECRET or TOKEN in it. That matters because Codex's default
    shell environment policy drops every variable with such a name before a tool
    call runs. The indexed ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n`` interface
    loses its KEY_n that way, and git then fails every command with "missing
    config key". A per-run ``GIT_CONFIG_GLOBAL`` file would also survive the
    filter, but the repository's own ``core.hooksPath`` (a hook manager's) beats
    a global file, while parameters outrank repository config. Existing
    parameters and indexed variables are left untouched.
    """
    existing = env.get("GIT_CONFIG_PARAMETERS", "").strip()
    entry = _sq(f"core.hooksPath={hooks_dir}")
    return {"GIT_CONFIG_PARAMETERS": f"{existing} {entry}" if existing else entry}
