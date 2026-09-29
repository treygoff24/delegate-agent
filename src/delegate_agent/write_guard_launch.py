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
# Guarded, but at least one protected path could not be bound and is open this run.
STATUS_PARTIAL = "partial"
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


def engine_home_candidates(engine: str, env: Mapping[str, str], home: str) -> tuple[Reopen, ...]:
    """The selected engine's own home directory, from the final child env.

    Only the engine's own home variable (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``,
    ``KIMI_CODE_HOME``) or default counts. No other environment value ever
    reopens anything: an ambient variable that happens to name a directory under
    ``~/.ai-profiles`` must not make that tree writable. The plan still checks
    the candidate before using it (``write_guard.home_candidate_problem``).
    """
    engine_home = sandbox_bwrap.engine_home(engine, env, home)
    return (Reopen(engine_home, "engine home"),) if engine_home else ()


def _facts(
    *,
    argv: Sequence[str],
    cwd: str,
    env: Mapping[str, str],
    engine: str,
    registry_root: str | None,
    run_roots: Sequence[Reopen],
    common_dir: str | None,
) -> GuardFacts:
    home = env.get("HOME") or write_guard.default_home()
    return GuardFacts(
        home=home,
        exec_root=cwd,
        git_common_dir=common_dir if common_dir is not None else git_common_dir(cwd),
        registry_root=registry_root,
        run_roots=tuple(run_roots),
        home_candidates=engine_home_candidates(engine, env, home),
        launcher_roots=write_guard.estate_launcher_roots(argv, env, home),
    )


def _refusal_warning(plan: write_guard.GuardPlan) -> str | None:
    if not plan.refused:
        return None
    detail = "; ".join(f"{entry.path} ({entry.reason})" for entry in plan.refused)
    return (
        f"work write guard: {detail}. The engine may be unable to write its own state; "
        "name the directory with isolation.writeGuard.writable or --writable to open it."
    )


def _unbound_warning(unbound: Sequence[tuple[str, str, str]]) -> str:
    detail = "; ".join(
        f"{path} ({'left unprotected' if mode == 'ro' else 'not reopened'}: {reason})"
        for mode, path, reason in unbound
    )
    lead = (
        "work write guard is PARTIAL: "
        if any(mode == "ro" for mode, _path, _reason in unbound)
        else "work write guard "
    )
    return (
        f"{lead}could not bind {detail}. Every other protected path is still "
        'guarded. Set isolation.writeGuard.onUnavailable to "refuse" to stop a run instead.'
    )


def _record_unbound(record: JsonObject, unbound: Sequence[tuple[str, str, str]]) -> None:
    """Make the manifest say what the guard did not do, not what it planned."""
    unprotected = {path for mode, path, _reason in unbound if mode == "ro"}
    unopened = {path for mode, path, _reason in unbound if mode == "rw"}
    protected = record.get("protected")
    if isinstance(protected, list):
        record["protected"] = [path for path in protected if path not in unprotected]
    writable = record.get("writable")
    if isinstance(writable, list):
        record["writable"] = [
            entry
            for entry in writable
            if not (isinstance(entry, dict) and entry.get("path") in unopened)
        ]
    record["unbound"] = [
        {"path": path, "mode": mode, "reason": reason} for mode, path, reason in unbound
    ]


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
            argv=argv,
            cwd=cwd,
            env=env,
            engine=engine,
            registry_root=registry_root,
            run_roots=run_roots,
            common_dir=common_dir,
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
    # The execution root is pinned as a mount of its own so a lane cannot rename it away.
    mounts = list(plan.mounts(pin=[os.path.realpath(cwd)]))
    wrapped = sandbox_bwrap.build_work_guard_argv(
        mounts=mounts, engine_argv=argv, cwd=cwd, bwrap_path=bwrap_path
    )
    unbound: list[tuple[str, str, str]] = []
    try:
        sandbox_bwrap.preflight_plan(wrapped)
    except DelegateError as exc:
        if settings.on_unavailable == write_guard.ON_UNAVAILABLE_REFUSE:
            return _fallback(settings, exc.message, argv, backend="bwrap")
        # One path the kernel will not bind must not switch the whole guard off (~/.ssh
        # would lose its protection because some other path is unbindable). Retry
        # without exactly the paths that fail on their own, and say which.
        unbound = sandbox_bwrap.unbindable_mounts(mounts, bwrap_path=bwrap_path)
        if not unbound:
            return _fallback(settings, exc.message, argv, backend="bwrap")
        dropped = {(mode, path) for mode, path, _reason in unbound}
        wrapped = sandbox_bwrap.build_work_guard_argv(
            mounts=[mount for mount in mounts if mount not in dropped],
            engine_argv=argv,
            cwd=cwd,
            bwrap_path=bwrap_path,
        )
        try:
            sandbox_bwrap.preflight_plan(wrapped)
        except DelegateError as again:
            return _fallback(settings, again.message, argv, backend="bwrap")
    record = plan.payload()
    # A protected path left unbound means the run is not what "enforced" promises.
    record["status"] = (
        STATUS_PARTIAL if any(mode == "ro" for mode, _p, _r in unbound) else STATUS_ENFORCED
    )
    warnings = [message for message in (_refusal_warning(plan),) if message]
    if unbound:
        _record_unbound(record, unbound)
        warnings.append(_unbound_warning(unbound))
    return GuardLaunch(argv=wrapped, record=record, warning=" ".join(warnings) or None)


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
    return GuardLaunch(
        argv=seatbelt.work_guard_argv(profile, argv),
        record=record,
        warning=_refusal_warning(plan),
    )


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
    guard_home = home or write_guard.default_home()
    facts = GuardFacts(
        home=guard_home,
        launcher_roots=write_guard.estate_launcher_roots(argv, os.environ, guard_home),
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
