"""Launch-time application of the work write guard.

``write_guard`` decides what to protect; this module turns that plan into a
bubblewrap or Seatbelt prefix for one child launch, proves the final boundary
works before the child starts, and applies the configured fallback when it does
not. It also builds the previews shown by ``--dry-run`` and recorded in the run
manifest, and adds the Codex native-sandbox writable roots.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed sandbox-exec probe argv, shell=False.
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from delegate_agent import sandbox_bwrap, seatbelt, write_guard
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS, run_git
from delegate_agent.json_types import JsonObject
from delegate_agent.write_guard import GuardFacts, Reopen, WriteGuardSettings

STATUS_ENFORCED = "enforced"
STATUS_UNAVAILABLE = write_guard.STATUS_UNAVAILABLE
STATUS_OFF = write_guard.STATUS_OFF
STATUS_PLANNED = "planned"


@dataclass(frozen=True)
class GuardLaunch:
    """The result of applying the guard to one launch."""

    argv: list[str]
    record: JsonObject
    warning: str | None = None


def git_common_dir(cwd: str) -> str | None:
    """Absolute git common dir of ``cwd``, or None outside a repository."""
    result = run_git(
        cwd, ["rev-parse", "--git-common-dir"], timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS
    )
    value = result.stdout.strip() if result.returncode == 0 else ""
    if not value:
        return None
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(cwd, value))


def engine_home_roots(
    engine: str, env: Mapping[str, str], home: str, extra: Sequence[str] = ()
) -> tuple[Reopen, ...]:
    """Directories the selected engine keeps its state in, from the final child env."""
    roots: list[Reopen] = []
    engine_home = sandbox_bwrap.engine_home(engine, env, home)
    if engine_home:
        roots.append(Reopen(engine_home, "engine home"))
    for path in extra:
        if path and os.path.isdir(path) and all(path != root.path for root in roots):
            roots.append(Reopen(path, "profile home"))
    return tuple(roots)


def profile_home_roots(env: Mapping[str, str], home: str) -> tuple[Reopen, ...]:
    """Profile directories the child's environment points at, inside ``~/.ai-profiles``.

    A profile's engine state lives under ``~/.ai-profiles`` and is selected through
    environment variables (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR`` and the other
    engines' equivalents). The tree is protected, so exactly the directories this
    child's own environment names stay writable; sibling profiles do not. Only
    ``~/.ai-profiles`` gets this treatment: an env var naming ``~/.gnupg`` or
    ``~/.ssh`` must not lift that protection.
    """
    profiles_root = os.path.realpath(os.path.join(home, ".ai-profiles"))
    seen: dict[str, Reopen] = {}
    for value in env.values():
        if not value or not os.path.isabs(value) or "\0" in value or not os.path.isdir(value):
            continue
        real = os.path.realpath(value)
        if real != profiles_root and real.startswith(profiles_root + os.sep):
            seen.setdefault(real, Reopen(real, "profile home"))
    return tuple(seen.values())


def _facts(
    *,
    cwd: str,
    env: Mapping[str, str],
    engine: str,
    registry_root: str | None,
    run_roots: Sequence[Reopen],
    common_dir: str | None,
    profile_homes: Sequence[str],
) -> GuardFacts:
    home = env.get("HOME") or write_guard.default_home()
    return GuardFacts(
        home=home,
        exec_root=cwd,
        git_common_dir=common_dir if common_dir is not None else git_common_dir(cwd),
        registry_root=registry_root,
        run_roots=(
            *run_roots,
            *engine_home_roots(engine, env, home, profile_homes),
            *profile_home_roots(env, home),
        ),
    )


def _fallback(
    settings: WriteGuardSettings, reason: str, argv: list[str], *, backend: str | None
) -> GuardLaunch:
    """The unavailable-backend outcome: refuse the run, or run unguarded with a warning."""
    message = f"work write guard unavailable: {reason}."
    if settings.on_unavailable == write_guard.ON_UNAVAILABLE_REFUSE:
        raise DelegateError(
            "write_guard_unavailable",
            f'{message} isolation.writeGuard.onUnavailable is "refuse", so the run did not '
            'start. Install the backend, or set onUnavailable to "warn" or enabled to false.',
        )
    record: JsonObject = {
        "status": STATUS_UNAVAILABLE,
        "backend": backend,
        "reason": reason,
        "onUnavailable": settings.on_unavailable,
    }
    return GuardLaunch(argv=list(argv), record=record, warning=f"{message} The run is not guarded.")


def apply_write_guard(
    settings: WriteGuardSettings,
    *,
    argv: list[str],
    cwd: str,
    env: Mapping[str, str],
    engine: str,
    registry_root: str | None = None,
    run_roots: Sequence[Reopen] = (),
    common_dir: str | None = None,
    profile_homes: Sequence[str] = (),
) -> GuardLaunch:
    """Wrap ``argv`` in the write guard for this host, or apply the fallback.

    Raises ``DelegateError('write_guard_unavailable')`` only when the settings say
    ``onUnavailable: refuse``. Otherwise an unavailable backend yields the
    original argv with ``record['status'] == 'unavailable'`` and a warning.
    A bwrap or Seatbelt launch that fails its own preflight counts as unavailable:
    the child never starts half-guarded.
    """

    def facts() -> GuardFacts:
        return _facts(
            cwd=cwd,
            env=env,
            engine=engine,
            registry_root=registry_root,
            run_roots=run_roots,
            common_dir=common_dir,
            profile_homes=profile_homes,
        )

    if engine == "codex" and write_guard.codex_native_sandbox_on(argv):
        # Codex's own sandbox is the boundary; wrapping it would nest sandbox_apply
        # (macOS) or a user namespace inside a sandbox (Linux).
        roots = write_guard.native_writable_roots(settings, facts())
        return GuardLaunch(argv=list(argv), record=native_record(roots))
    if sys.platform.startswith("linux"):
        return _apply_bwrap(settings, argv, cwd, facts())
    if sys.platform == "darwin":
        if settings.macos_seatbelt:
            return _apply_seatbelt(settings, argv, cwd, facts())
    status, reason = write_guard.unguarded(settings)
    if status == STATUS_OFF:
        # Not wanted here (Seatbelt guard is opt-in on macOS): nothing is unavailable, so
        # neither the warning nor onUnavailable=refuse applies.
        return GuardLaunch(
            argv=list(argv),
            record={"status": STATUS_OFF, "backend": None, "reason": reason},
        )
    return _fallback(settings, reason, argv, backend=None)


def _apply_bwrap(
    settings: WriteGuardSettings, argv: list[str], cwd: str, facts: GuardFacts
) -> GuardLaunch:
    bwrap_path = shutil.which(sandbox_bwrap.BWRAP_BINARY)
    if bwrap_path is None:
        return _fallback(settings, "bubblewrap (bwrap) is not installed", argv, backend="bwrap")
    plan = write_guard.plan_guard(settings, facts, backend=write_guard.BACKEND_BWRAP)
    wrapped = sandbox_bwrap.build_work_guard_argv(
        mounts=plan.mounts(), engine_argv=argv, cwd=cwd, bwrap_path=bwrap_path
    )
    try:
        sandbox_bwrap.preflight_plan(wrapped)
    except DelegateError as exc:
        return _fallback(settings, exc.message, argv, backend="bwrap")
    record = plan.payload()
    record["status"] = STATUS_ENFORCED
    return GuardLaunch(argv=wrapped, record=record)


def _apply_seatbelt(
    settings: WriteGuardSettings, argv: list[str], cwd: str, facts: GuardFacts
) -> GuardLaunch:
    if shutil.which("sandbox-exec") is None:
        return _fallback(settings, "sandbox-exec is not installed", argv, backend="seatbelt")
    plan = write_guard.plan_guard(settings, facts, backend=write_guard.BACKEND_SEATBELT)
    profile = seatbelt.build_work_guard_profile(plan.mounts(), no_unlink=[cwd])
    try:
        probe = subprocess.run(  # nosec B603 B607 - fixed probe argv, shell=False.
            seatbelt.work_guard_argv(profile, ["/usr/bin/true"]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fallback(settings, f"sandbox-exec could not run: {exc}", argv, backend="seatbelt")
    if probe.returncode != 0:
        detail = os.fsdecode(probe.stderr or b"").strip().splitlines()
        reason = detail[0][:200] if detail else f"exit {probe.returncode}"
        return _fallback(
            settings, f"sandbox-exec preflight failed ({reason})", argv, backend="seatbelt"
        )
    record = plan.payload()
    record["status"] = STATUS_ENFORCED
    return GuardLaunch(argv=seatbelt.work_guard_argv(profile, argv), record=record)


def native_record(roots: Sequence[Reopen]) -> JsonObject:
    """Manifest record for the Codex native ``workspace-write`` sandbox."""
    return {
        "backend": write_guard.BACKEND_CODEX_NATIVE,
        "status": STATUS_ENFORCED,
        "protected": [],
        "writable": [{"path": root.path, "reason": root.reason} for root in roots],
    }


def codex_argv_with_work_roots(argv: list[str], roots: Sequence[Reopen]) -> list[str]:
    """Add ``--add-dir`` roots to a Codex ``exec`` argv that keeps its own sandbox on.

    ``--add-dir`` is additive with ``sandbox_workspace_write.writable_roots``,
    which the mail-push launch already sets with ``-c`` (last one wins), so the
    roots never collide with it. They go right after ``exec``: options for
    ``exec resume`` precede the ``resume`` token as well.
    """
    if not roots or not write_guard.codex_native_sandbox_on(argv) or "exec" not in argv:
        return list(argv)
    insert_at = argv.index("exec") + 1
    added: list[str] = []
    for root in roots:
        added.extend(("--add-dir", root.path))
    return [*argv[:insert_at], *added, *argv[insert_at:]]


@dataclass(frozen=True)
class NativeLaunch:
    """Codex native-sandbox roots for one launch, and the manifest record."""

    roots: tuple[Reopen, ...]
    record: JsonObject


def codex_native_launch(
    settings: WriteGuardSettings,
    *,
    argv: Sequence[str],
    cwd: str,
    registry_root: str | None,
    run_roots: Sequence[Reopen] = (),
    home: str | None = None,
) -> NativeLaunch | None:
    """Roots to hand Codex's own sandbox, or None when the argv does not keep it on."""
    if not write_guard.codex_native_sandbox_on(argv):
        return None
    facts = GuardFacts(
        home=home or write_guard.default_home(),
        exec_root=cwd,
        git_common_dir=git_common_dir(cwd),
        registry_root=registry_root,
        run_roots=tuple(run_roots),
    )
    roots = write_guard.native_writable_roots(settings, facts)
    return NativeLaunch(roots=roots, record=native_record(roots))


def preview_payload(
    settings: WriteGuardSettings,
    *,
    engine: str,
    argv: Sequence[str],
    exec_root: str,
    registry_root: str | None,
    home: str | None = None,
    git_cwd: str | None = None,
) -> JsonObject:
    """The guard plan as ``--dry-run`` and the initial manifest show it.

    Uses only facts known before launch; run scratch, the child's TMPDIR and the
    engine home are added when the child launches.
    """
    backend = write_guard.predicted_backend(settings, engine=engine, argv=argv)
    if backend is None:
        status, reason = write_guard.unguarded(settings)
        return {
            "status": status,
            "backend": None,
            "reason": reason,
            "onUnavailable": settings.on_unavailable,
        }
    facts = GuardFacts(
        home=home or write_guard.default_home(),
        exec_root=exec_root,
        git_common_dir=git_common_dir(git_cwd or exec_root)
        if os.path.isdir(git_cwd or exec_root)
        else None,
        registry_root=registry_root if registry_root and os.path.isdir(registry_root) else None,
    )
    if backend == write_guard.BACKEND_CODEX_NATIVE:
        payload = native_record(write_guard.native_writable_roots(settings, facts))
        payload["status"] = STATUS_PLANNED
    else:
        payload = write_guard.plan_guard(settings, facts, backend=backend).payload()
        payload["status"] = STATUS_PLANNED
    payload["onUnavailable"] = settings.on_unavailable
    return payload
