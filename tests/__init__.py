"""Test package initializer.

Strips ``AI_PROFILE``/``DELEGATE_CONFIG`` from the process environment at test
collection time so the suite is hermetic regardless of the invoking shell's
ambient environment. This matters now that ``delegate_agent.cli.main`` reads
``AI_PROFILE``/``DELEGATE_CONFIG`` directly (the profile-crossover guard in
``delegate_agent.profile_guard``): a dev shell that routes real launches
through ``AI_PROFILE=work|personal`` would otherwise leak into every
in-process ``self.delegate.main(...)`` call that doesn't explicitly patch the
environment, and fail closed on tests that never meant to exercise the guard.

Individual tests that need these vars set them explicitly via
``mock.patch.dict``, which overrides this baseline for the duration of the
``with`` block regardless of what this module clears at import time.

``HOME`` is redirected for the same reason, one layer down. Several data-home
resolvers fall through to ``Path.home()`` with no other seam --
``isolation.worktrees_data_home`` most notably -- so a test that exercised a
persistent-worktree run wrote real worktrees into the developer's own
``~/.delegate/worktrees`` and orphaned them there permanently (observed
2026-07-16: ten pooled worktrees whose temp source repos were long gone).
``Path.home()`` honors ``$HOME`` on POSIX, so redirecting it once here reaches
every such derivation at once, including stores that grow their own resolvers
later.
"""

import atexit
import contextlib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

# Make the src layout importable for focused invocations
# (`python3 -m unittest tests.test_x`), which otherwise fail before per-module
# sys.path shims run because `tests.delegate_fixtures` imports delegate_agent
# at import time. This package always loads first, so the shim lands in time.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

os.environ.pop("AI_PROFILE", None)
os.environ.pop("DELEGATE_CONFIG", None)
# A workflow-pinned parent leaks PYTHONPATH=<pin src> plus DELEGATE_WORKFLOW_PIN
# into every child. The pin src ships a sitecustomize.py that, seeing the pin
# var, imports delegate_agent at interpreter startup -- BEFORE this package can
# insert _SRC -- so sys.modules holds the pinned (stale) build and every test
# silently exercises the wrong code (observed 2026-08-31: a compiled-plan close
# refused 11 green rows because verify subprocesses tested the pre-plan pin).
# Evict foreign delegate_agent modules so imports resolve from _SRC, and scrub
# the pin env so subprocesses the suite spawns start clean.
for _name, _module in list(sys.modules.items()):
    if _name == "delegate_agent" or _name.startswith("delegate_agent."):
        _file = getattr(_module, "__file__", None)
        if _file is not None and not str(_file).startswith(_SRC + os.sep):
            del sys.modules[_name]
os.environ.pop("DELEGATE_WORKFLOW_PIN", None)
# A parent workflow's immutable operational attempt must not select config or
# numeric overrides inside the test process after HOME has been redirected.
for _name in (
    "DELEGATE_WORKFLOW_ATTEMPT",
    "DELEGATE_STALL_MINUTES",
    "DELEGATE_PROCESS_GROUP_TERMINATION_GRACE_SEC",
    "DELEGATE_REGISTRY_LOCK_TIMEOUT_SECONDS",
    "DELEGATE_PROGRESS_INITIAL_DELAY_SEC",
    "DELEGATE_PROGRESS_INTERVAL_SEC",
):
    os.environ.pop(_name, None)
_pythonpath = [
    entry
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if entry and entry != _SRC and not (Path(entry) / "sitecustomize.py").exists()
]
if _pythonpath:
    os.environ["PYTHONPATH"] = os.pathsep.join(_pythonpath)
else:
    os.environ.pop("PYTHONPATH", None)
# Initiator-root provenance reads these from os.environ; a suite run inside a
# Claude Code or Codex session would otherwise make every initiator resolution
# ambiguous (two native keys present -> None) and fail provenance tests.
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
os.environ.pop("CODEX_THREAD_ID", None)
os.environ.pop("DELEGATE_INITIATOR_ROOT", None)
# Mail identity binding reads DELEGATE_RUN_ID/DELEGATE_MAIL_SELF/DELEGATE_SOURCE_ROOT
# from ambient env (mail_core.py); a suite run inside a live delegate lane would
# otherwise bind to the outer run and fail with unknown_sender/conflicting_cwd.
# WORKSPACE_ROOT is treated as authoritative in child-env derivation (cli.py) and
# DELEGATE_PROFILE is read by config resolution (config.py). Tests that need these
# set them explicitly via mock.patch.dict.
os.environ.pop("DELEGATE_RUN_ID", None)
os.environ.pop("DELEGATE_MAIL_SELF", None)
os.environ.pop("DELEGATE_SOURCE_ROOT", None)
os.environ.pop("DELEGATE_EXECUTION_ROOT", None)
os.environ.pop("WORKSPACE_ROOT", None)
os.environ.pop("DELEGATE_PROFILE", None)
os.environ.pop("TMPDIR", None)
os.environ.pop("TMP", None)
os.environ.pop("TEMP", None)
tempfile.tempdir = None
# `capabilities refresh` runs the configured auth probes (`estate-cursor status`,
# `estate-omp usage`) when they are on PATH. On a developer machine they are, and
# they reach real accounts; the suite must never depend on them. Tests of the
# probes themselves clear this switch and supply their own scripts.
os.environ["DELEGATE_AUTH_PROBES"] = "off"

# Stashed for the rare test that must run a real credentialed binary (the
# live omp write-probe): everything credential-bearing lives under the real
# home, so a probe subprocess launched with the hermetic HOME dies keyless in
# milliseconds and reads as a dead lane. pytest-xdist workers are spawned after
# this module has already redirected HOME in the controller, so the real home
# travels to them through an env var; without it every worker would stash the
# throwaway HOME here (observed: the real-binary codex contract tests errored
# under `-n 2` because the codex shim looked for its helpers in the temp HOME).
ORIGINAL_HOME = os.environ.get("DELEGATE_TESTS_ORIGINAL_HOME") or os.environ.get("HOME")
if ORIGINAL_HOME:
    os.environ["DELEGATE_TESTS_ORIGINAL_HOME"] = ORIGINAL_HOME

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="dt-"))
_TEST_HOME = str(_TEST_ROOT / "home")
_TEST_TEMP = str(_TEST_ROOT / "tmp")
Path(_TEST_HOME).mkdir()
Path(_TEST_TEMP).mkdir()
os.environ["HOME"] = _TEST_HOME
for _name in ("TMPDIR", "TMP", "TEMP"):
    os.environ[_name] = _TEST_TEMP
tempfile.tempdir = _TEST_TEMP

# `python3` resolves through mise on the agent image, and every subprocess this
# suite spawns runs with the temporary HOME above.  Point mise's data/cache
# roots at the real user's existing installs once here: otherwise a shim that
# needs an interpreter version downloads one into the throwaway HOME and writes
# install progress to stderr, which fails any test asserting a clean child
# stderr (observed as an unrelated launcher-shim test failing the CI gate).
# Per-test pinning cannot cover the subprocess-bearing tests that never touch
# the helper that used to set these.
_real_home = Path(ORIGINAL_HOME) if ORIGINAL_HOME else Path.home()
os.environ.setdefault(
    "MISE_DATA_DIR",
    str(Path(os.environ.get("XDG_DATA_HOME", _real_home / ".local/share")) / "mise"),
)
os.environ.setdefault(
    "MISE_CACHE_DIR",
    str(Path(os.environ.get("XDG_CACHE_HOME", _real_home / ".cache")) / "mise"),
)

# `run_scratch` derives the child's compact temp root from a fixed
# `/var/tmp/dlg-<euid>/<token>` location that only `runs prune` removes, and a
# tracked-run test does not prune: an unpinned root orphaned one retained
# directory per test run under `/var/tmp` forever (observed 2026-09-22: 699
# entries after one suite run). The same reasoning that redirected `HOME` above
# applies, one layer lower -- in-process derivations move under the suite root.
# A launch the suite spawns as a real subprocess imports the production default
# before any patch here can reach it, so those runs are contained instead by
# `reap_recorded_compact_temps` below, which removes exactly the per-run
# directory each run manifest records while that registry still exists -- and
# only once the fixture that owns the run's producer process has proven it
# exited, because one attempt child going quiet speaks for nothing while the
# producer that launched it may still launch another.
from delegate_agent import record_io as _record_io  # noqa: E402
from delegate_agent import run_registry as _run_registry  # noqa: E402
from delegate_agent import run_scratch as _run_scratch  # noqa: E402

_TEST_VAR_TMP = Path(_TEST_ROOT) / "var-tmp"
_TEST_VAR_TMP.mkdir()

# Captured before the pin below, so the containment helpers keep naming the
# root a subprocess launch really uses. Resolved the way
# `run_scratch._compact_temp_root` resolves it: macOS spells `/var/tmp` as
# `/private/var/tmp`, and an unresolved root never matched a recorded
# `tempPath` there, so containment silently skipped every Mac run (bead dlg-4l5).
_PRODUCTION_TEMP_ROOT = _run_scratch.PERSISTENT_TEMP_ROOT.resolve(strict=False)
PRODUCTION_COMPACT_TEMP_ROOT = _PRODUCTION_TEMP_ROOT / (
    f"{_run_scratch.COMPACT_TEMP_DIR_PREFIX}{os.geteuid()}"
)
_COMPACT_TEMP_TOKEN_RE = re.compile(rf"[0-9a-f]{{{_run_scratch.COMPACT_TEMP_TOKEN_CHARS}}}\Z")

_run_scratch.PERSISTENT_TEMP_ROOT = _TEST_VAR_TMP

# Known-bad lane markers live under the (process-wide, redirected) home, so one
# in-process test's persistent provider failure would refuse a later test's launch
# on the same lane. Wipe the store before every test case runs, under either runner.
import unittest as _unittest  # noqa: E402

from delegate_agent import lane_health as _lane_health  # noqa: E402

_ORIGINAL_TESTCASE_RUN = _unittest.TestCase.run


def _run_with_clean_lane_health(self, result=None):
    shutil.rmtree(_lane_health.store_dir(), ignore_errors=True)
    return _ORIGINAL_TESTCASE_RUN(self, result)


_unittest.TestCase.run = _run_with_clean_lane_health


def compact_temp_names() -> set[str]:
    """Directory names present under the production compact child-temp root."""
    if not PRODUCTION_COMPACT_TEMP_ROOT.is_dir():
        return set()
    return {entry.name for entry in PRODUCTION_COMPACT_TEMP_ROOT.iterdir()}


# The grace a recorded run child gets to reach its own exit before containment
# reaps it. A fixture child that is deliberately still writing inside its TMPDIR
# must be allowed to finish doing so; only a child that outstays the grace is
# signalled.
COMPACT_TEMP_CHILD_GRACE_SECONDS = 2.0

# How long the deletion decision waits for the run's own registry lock. The
# runner holds that lock across each attempt's launch and pid/pgid publication
# (`runner._run_single_tracked_attempt`), so the wait ends as soon as that
# publication is done; a lock held longer leaves the record unknown, and an
# unread record retains its temp rather than blocking teardown.
CONTAINMENT_LOCK_SECONDS = 5.0

# The manifest keys whose recorded paths prove which command line owns a run's
# child: the runner records the child's execution cwd plus the scratch and
# compact temp it allocated for the run, and the child's own command line
# carries the workspace or safe copy it was launched into.
_RUN_CHILD_OWNERSHIP_KEYS = ("executionCwd", "cwd", "scratchPath", "tempPath")


def _recorded_group_id(manifest: dict[str, object]) -> int | None:
    """The process group a run manifest records for its own child, when usable."""
    pgid = manifest.get("pgid")
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1:
        return pgid
    return None


def _recorded_child_markers(manifest: dict[str, object]) -> tuple[str, ...]:
    """The paths a run record names for the command line of its own child.

    A run can record a live child whose command line names none of them -- Pi
    takes its prompt on stdin, and Claude or Kimi need not carry a workspace flag
    either -- so an empty tuple is reported rather than dropped: awaiting the
    group its own record names is always safe, and the empty tuple is exactly
    what keeps it from authorizing a signal.
    """
    return tuple(
        marker
        for key in _RUN_CHILD_OWNERSHIP_KEYS
        if isinstance((marker := manifest.get(key)), str) and marker
    )


def _recorded_compact_temp_path(manifest: dict[str, object]) -> Path | None:
    """The production compact temp a run record names, when it names one.

    A real subprocess launch resolves the production compact child temp root,
    and only `runs prune` removes what it allocated. An in-process run records
    the suite-pinned root instead, and the suite root's own teardown removes
    that, so those manifests are not this containment's.
    """
    recorded = manifest.get("tempPath")
    if not isinstance(recorded, str) or not recorded:
        return None
    path = Path(recorded)
    return path if path.parent == PRODUCTION_COMPACT_TEMP_ROOT else None


def _compact_temp_state(path: Path) -> str:
    """``"absent"``, ``"removable"``, or ``"surviving"`` for a recorded temp.

    Only a real directory owned by this user is removed; anything else is
    reported as surviving evidence rather than deleted on weaker terms.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "surviving"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return "surviving"
    return "removable"


def _quiesce_recorded_run_children(children: list[tuple[int, tuple[str, ...]]]) -> set[int]:
    """Settle every recorded run child; return the groups not confirmed quiet.

    Each recorded child is given `COMPACT_TEMP_CHILD_GRACE_SECONDS` to reach its
    own exit -- a fixture child that is deliberately still writing through
    `$TMPDIR` finishes, and the directory it writes into stays put until then.
    The grace is one deadline for the whole set, so a batch of live children
    cannot multiply it.

    Every recorded child is awaited, marker or not: the manifest names the group
    its own runner launched in a new session, so waiting on it cannot reach an
    unrelated process, and awaiting is what makes removing that child's temp
    safe when its command line proves nothing about ownership. Nothing is
    signalled on a group id alone: a child that outstays the grace is signalled
    only when a live member still carries one of the paths this run's own record
    names, so a reused group id cannot authorize a signal to an unrelated group,
    no production process is ever matched by name, and a child whose command
    line carries no recorded path is never signalled at all.

    A signal request is not an exit: SIGKILL delivery, a delayed child that
    outruns its grace, and a member that could not be signalled all look the
    same to the caller. The groups are therefore re-checked afterwards, and any
    group that still has a live member is returned as unconfirmed so the caller
    retains that run's temp instead of deleting a directory a live child may
    still be writing into.

    The children are resolved by `_contain_recorded_compact_temps` from the same
    record read that names each temp; a record that names no group is not one of
    them, because a group that could not be observed is unknown rather than
    absent.
    """
    from tests import proc_harness

    settled: set[int] = set()
    deadline = time.monotonic() + COMPACT_TEMP_CHILD_GRACE_SECONDS
    for pgid, markers in children:
        if proc_harness.await_process_group(pgid, timeout=max(deadline - time.monotonic(), 0)):
            settled.add(pgid)
            continue
        for marker in markers:
            proc_harness.reap_recorded_group_matching(pgid, marker)
    settle_deadline = time.monotonic() + COMPACT_TEMP_CHILD_GRACE_SECONDS
    return {
        pgid
        for pgid, _markers in children
        if pgid not in settled
        and not proc_harness.await_process_group(
            pgid, timeout=max(settle_deadline - time.monotonic(), 0)
        )
    }


def _delete_confirmed_generation(manifest_path: Path, path: Path, pgid: int) -> bool:
    """Remove one run's temp when its record still names the confirmed generation.

    ``path`` and ``pgid`` are the temp and the child process group whose
    quiescence was confirmed just above. One run's compact temp is not owned by
    one attempt: `runner._run_single_tracked_attempt` reuses it for the primary
    attempt, a thread retry, an auth fallback, and an empty-success retry, and
    each of those publishes a new child's pid/pgid into the same manifest under
    the registry lock. A mapping from "manifest to the group awaited at the start
    of teardown" therefore proves only which group was awaited, not which group
    owns the temp now.

    This re-read under that same lock -- held across the whole decision,
    including the removal -- is what authorizes it: a record that now names a
    different group, a different temp, or no group is a different generation, so
    its temp is retained and reported instead. A record that cannot be read and a
    lock that cannot be taken are unknown rather than unchanged, so both retain.
    Returns whether this exact confirmed temp is gone afterwards.
    """
    registry_root = manifest_path.parent.parent.parent
    try:
        with _run_registry.registry_lock(registry_root, timeout_seconds=CONTAINMENT_LOCK_SECONDS):
            manifest = _record_io.read_json_object_or_none(manifest_path)
            if not isinstance(manifest, dict):
                return False
            current = manifest.get("tempPath")
            if not isinstance(current, str) or not current or Path(current) != path:
                return False
            if _recorded_group_id(manifest) != pgid:
                return False
            shutil.rmtree(path)
            return not path.exists() and not path.is_symlink()
    except (OSError, TimeoutError):
        return False


def _contain_recorded_compact_temps(
    manifests: Iterable[Path],
    *,
    producers: Sequence[object] = (),
) -> tuple[list[Path], list[Path]]:
    """Remove the compact child temp every given run manifest records.

    Returns ``(removed, surviving)``. A real subprocess launch resolves the
    production compact root, and only `runs prune` removes what it allocated:
    the harness has to do it while the temporary registry that records the path
    still exists, because the path is derived from that registry's identity and
    the run id. Only a path the record itself names, under the production owner
    root, shaped exactly like `run_scratch.compact_temp_plan` output, still a
    real directory owned by this user, is removed. A recorded path that still
    exists afterwards is returned as surviving evidence rather than deleted on
    weaker terms.

    The deletion boundary is the producer, not one attempt child. A run's
    compact temp is the ``TMPDIR``/``TMP``/``TEMP`` of every attempt of that run
    (`runner._prepare_tracked_run` allocates it once per run and
    `_env_overrides_with_temp_dir` hands it to each attempt), and the producer --
    the test-owned Delegate process driving the run -- may launch another attempt
    into it at any time until that process exits. ``producers`` is the caller's
    positive evidence that those processes are gone: the reaped
    `Popen`/`CompletedProcess` of each producer the fixture spawned, or a
    `proc_harness` proof for a producer without a handle. With no usable
    evidence nothing here is deleted: every recorded temp is retained and
    reported, because "the group I once awaited is quiet" says nothing about the
    run's own producer.

    A proof only speaks for the population it declares, and it is asked again at
    the deletion boundary: a producer whose command line carries none of the
    roots a scan was given is outside that proof, so the caller has to name every
    root its own producers carry (`proc_harness.reaped_owned_producers`). A scan
    that cannot be completed raises out of here rather than answering, which
    leaves the temps it had not yet reached retained and named.

    Once the producer is confirmed gone, the child each record currently names is
    settled (`_quiesce_recorded_run_children`): removing the temp of a live child
    that is still writing through its TMPDIR is exactly the failure this
    containment must not cause. That child counts as settled only when its
    recorded group has no live member left, so a child whose command line carries
    no recorded path, a child that outran the grace, and a child whose reap could
    not be confirmed all keep their temp: the path is returned as surviving
    instead, which fails the calling test and, for the suite-wide call below, the
    gate. Deleting a directory a live child still writes into would change that
    child's outcome silently, which no assertion here could then detect.

    The last step is `_delete_confirmed_generation`: under the run's own registry
    lock, the record must still name the group and the temp whose quiescence was
    just confirmed. A run that records a temp but no child group is unknown, not
    empty -- its manifest is written before its launch, and the child's pid/pgid
    reach it afterwards -- so its exact temp is retained and reported rather than
    deleted on the strength of a missing group.
    """
    from tests import proc_harness

    def producers_proven_gone() -> bool:
        """Ask every proof again; the answer is only valid where it is asked.

        A proof re-checks its own producer (a `Popen`, or a live scan/lock
        probe), so this is asked once before the boundary work and once per
        deletion decision: a producer that appears while the children are being
        settled leaves its run's temp retained rather than deleted behind it.
        """
        return proc_harness.producers_exited(producers)

    records: list[tuple[Path, dict[str, object], Path]] = []
    for manifest_path in manifests:
        manifest = _record_io.read_json_object_or_none(manifest_path)
        if not isinstance(manifest, dict):
            continue
        path = _recorded_compact_temp_path(manifest)
        if path is not None:
            records.append((manifest_path, manifest, path))

    if not producers_proven_gone():
        # The processes that drive these runs are not proven gone, so they may
        # each still launch another attempt into their run's temp: retain and
        # report every recorded temp instead of deleting one out from under it.
        return [], [
            path
            for _manifest_path, _manifest, path in records
            if _compact_temp_state(path) != "absent"
        ]

    surviving: list[Path] = []
    pending: list[tuple[Path, Path, int]] = []
    children: list[tuple[int, tuple[str, ...]]] = []
    seen: set[int] = set()
    for manifest_path, manifest, path in records:
        if _COMPACT_TEMP_TOKEN_RE.fullmatch(path.name) is None:
            surviving.append(path)
            continue
        state = _compact_temp_state(path)
        if state == "absent":
            continue
        if state != "removable":
            surviving.append(path)
            continue
        pgid = _recorded_group_id(manifest)
        if pgid is None:
            # The record names no group, so the child that owns this temp is
            # unknown: retain the exact path and report it.
            surviving.append(path)
            continue
        pending.append((manifest_path, path, pgid))
        if pgid not in seen:
            seen.add(pgid)
            children.append((pgid, _recorded_child_markers(manifest)))

    unquiesced = _quiesce_recorded_run_children(children)
    removed: list[Path] = []
    for manifest_path, path, pgid in pending:
        if pgid in unquiesced:
            # The child this record names is not confirmed gone: its temp is
            # retained and reported rather than deleted under a live writer.
            surviving.append(path)
            continue
        if not producers_proven_gone() or not _delete_confirmed_generation(
            manifest_path, path, pgid
        ):
            surviving.append(path)
        else:
            removed.append(path)
    return removed, surviving


def _run_manifest_paths(search_root: Path) -> list[Path]:
    """Run manifests under a root, by the ``runs/<runId>/manifest.json`` layout."""
    return sorted(
        path
        for path in search_root.glob(f"**/{_record_io.RUNS_DIR_NAME}/*/{_record_io.MANIFEST_FILE}")
        if path.is_file()
    )


def recorded_compact_temps(registry_root: Path) -> list[tuple[str, Path]]:
    """``(run_id, path)`` for every run manifest of a registry that records one."""
    runs_dir = _record_io.runs_dir(registry_root)
    if not runs_dir.is_dir():
        return []
    recorded: list[tuple[str, Path]] = []
    for manifest_path in sorted(runs_dir.glob(f"*/{_record_io.MANIFEST_FILE}")):
        manifest = _record_io.read_json_object_or_none(manifest_path)
        if not isinstance(manifest, dict):
            continue
        run_id = manifest.get("runId")
        temp_path = manifest.get("tempPath")
        if isinstance(run_id, str) and isinstance(temp_path, str) and temp_path:
            recorded.append((run_id, Path(temp_path)))
    return recorded


def derived_production_compact_temp(registry_root: Path, run_id: str) -> Path:
    """The production compact temp a run derives, independently of any record.

    Mirrors ``run_scratch.compact_temp_plan``'s deterministic derivation, only
    at the production root this process pins away, so a test can name the exact
    directory a subprocess run owns without trusting the manifest that points at
    it. Evaluate it with the same ``HOME`` the child received.
    """
    base = _run_scratch.plan(registry_root, run_id)
    token = hashlib.sha256(str(base.path).encode("utf-8")).hexdigest()[
        : _run_scratch.COMPACT_TEMP_TOKEN_CHARS
    ]
    return PRODUCTION_COMPACT_TEMP_ROOT / token


def reap_recorded_compact_temps(
    registry_root: Path, *, producers: Sequence[object] = ()
) -> tuple[list[Path], list[Path]]:
    """Contain one registry's recorded compact temps; see the helper above.

    ``producers`` is the caller's positive evidence that the processes that
    drive those runs cannot launch another attempt into them; without it every
    recorded temp is retained and reported rather than deleted.
    """
    runs_dir = _record_io.runs_dir(registry_root)
    if not runs_dir.is_dir():
        return [], []
    return _contain_recorded_compact_temps(
        sorted(runs_dir.glob(f"*/{_record_io.MANIFEST_FILE}")), producers=producers
    )


def assert_compact_temps_contained(
    registry_root: Path, *, producers: Sequence[object] = ()
) -> list[Path]:
    """Fail a test whose registry still owns a production compact temp.

    Called from a fixture teardown while the test's temporary registry (and so
    each run manifest's recorded ``tempPath``) still exists. Parallel workers
    that create their own compact temps in the same window are not this test's
    residue, so the assertion is scoped to the recorded paths this registry
    names rather than to the whole production root.

    ``producers`` carries the caller's positive evidence that the Delegate
    processes which drive those runs have exited -- the reaped handle of each
    producer the fixture spawned, or a `proc_harness` proof for one without a
    handle. A caller that passes none of it does not get a quiet deletion: every
    recorded temp is retained and named here, because the producer may still
    launch another attempt into the run's shared temp.

    A survivor is either a path that could not be removed on the record's own
    terms, a temp retained while the producer was not proven gone, or a temp
    whose record named no group or had changed generation by the time the
    deletion was decided; the message names the possibilities, so a leaked temp
    is not mistaken for a retention and the retained path is on record for
    investigation.
    """
    removed, surviving = reap_recorded_compact_temps(registry_root, producers=producers)
    if surviving:
        raise AssertionError(
            "compact child temp directories recorded by "
            f"{registry_root} survived containment under "
            f"{PRODUCTION_COMPACT_TEMP_ROOT}: {[str(path) for path in surviving]}"
            "; each survivor is either unremovable on its record's own terms or"
            " retained because the process driving the run was not proven gone"
            " (its producer may still launch another attempt into this temp) or"
            " because the record's own group/temp generation was not confirmed"
            " quiet (a record naming no group is unknown child state, not proof"
            " that no child exists)"
        )
    return removed


def _finish_test_environment() -> None:
    # Both runners use the same ownership guard. Pytest also invokes it for
    # each test; unittest gets this suite-level backstop before temp cleanup.
    from tests import proc_harness
    from tests.process_guard import reap_delegate_processes

    try:
        reap_delegate_processes(_TEST_ROOT)
    except Exception as exc:
        os.write(2, f"test process cleanup failed; retaining {_TEST_ROOT}: {exc}\n".encode())
        # atexit exceptions otherwise do not make the authoritative gate fail.
        os._exit(1)
    # Every registry still under the suite root belongs to this run: remove the
    # production compact temp it records before that registry disappears. The
    # suite's own producer boundary is the reaped owned-process scan above --
    # re-checked by the proof, since a producer this suite still owns may launch
    # another attempt into its run's temp -- and a temp whose child group or
    # generation could not be confirmed keeps its temp, so the suite fails and
    # names the paths instead of deleting a live child's TMPDIR.
    #
    # That scan's population is the Delegate processes whose command line
    # carries a path under this suite root: the fixtures here put HOME and
    # TMPDIR under it, and a launcher they spawn as `bin/delegate.py` with a
    # relative entrypoint (`test_codex_pure_sandbox`) is awaited by the call that
    # started it, so it cannot be running now. A process outside that population
    # is not evidence in either direction; a scan that cannot be completed
    # raises rather than passing for a quiet suite.
    try:
        _, surviving = _contain_recorded_compact_temps(
            _run_manifest_paths(_TEST_ROOT),
            producers=[proc_harness.reaped_owned_producers(_TEST_ROOT)],
        )
    except Exception as exc:
        os.write(2, f"compact temp containment failed; retaining {_TEST_ROOT}: {exc}\n".encode())
        os._exit(1)
    if surviving:
        os.write(
            2,
            "test suite left recorded compact child temp directories in "
            f"{PRODUCTION_COMPACT_TEMP_ROOT}: {[str(path) for path in surviving]}"
            " (either unremovable on their record's terms, or retained because"
            " the run's producer or child group was not confirmed gone, or the"
            " record's group/temp generation changed before deletion)\n".encode(),
        )
        os._exit(1)
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)


atexit.register(_finish_test_environment)

# unittest, which is the CI gate, never imports pytest's conftest.py.  Start
# the linked-worktree flock watcher at package import so both runners observe
# the same process tree.  pytest calls assert_linked_worktree_lock_clean below
# for a normal test failure; unittest receives a non-zero process result at
# teardown if the watcher recorded an escape.
from tests.registry_lock_guard import source_lock_for_linked_worktree  # noqa: E402

_LOCK_GUARD_DIR: Path | None = None
_LOCK_GUARD_REPORT: Path | None = None
_LOCK_GUARD_STOP: Path | None = None
_LOCK_GUARD_PROCESS: subprocess.Popen[bytes] | None = None


def _start_linked_worktree_lock_guard() -> None:
    global _LOCK_GUARD_DIR, _LOCK_GUARD_REPORT, _LOCK_GUARD_STOP, _LOCK_GUARD_PROCESS
    target = source_lock_for_linked_worktree(Path(__file__).resolve().parent.parent)
    if target is None:
        return
    _LOCK_GUARD_DIR = Path(tempfile.mkdtemp(prefix="delegate-lock-guard-"))
    _LOCK_GUARD_REPORT = _LOCK_GUARD_DIR / "violations.jsonl"
    _LOCK_GUARD_STOP = _LOCK_GUARD_DIR / "stop"
    _LOCK_GUARD_PROCESS = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "registry_lock_guard.py"),
            "--target",
            str(target),
            "--report",
            str(_LOCK_GUARD_REPORT),
            "--stop",
            str(_LOCK_GUARD_STOP),
            # Scope the watcher to this suite's own process tree: the same
            # target is held by concurrent unrelated Delegate launchers, which
            # are not suite escapes and must not fail the gate.
            "--suite-pid",
            str(os.getpid()),
        ],
        close_fds=True,
    )


def assert_linked_worktree_lock_clean() -> None:
    if _LOCK_GUARD_STOP is not None:
        _LOCK_GUARD_STOP.touch()
    if _LOCK_GUARD_PROCESS is not None:
        try:
            _LOCK_GUARD_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _LOCK_GUARD_PROCESS.kill()
            _LOCK_GUARD_PROCESS.wait(timeout=5)
    if _LOCK_GUARD_REPORT is not None and _LOCK_GUARD_REPORT.exists():
        details = _LOCK_GUARD_REPORT.read_text(encoding="utf-8").strip()
        if details:
            raise AssertionError(
                "linked-worktree suite flocks source registry lock; offending caller(s): " + details
            )


def _finish_linked_worktree_lock_guard() -> None:
    try:
        assert_linked_worktree_lock_clean()
    except AssertionError as exc:
        os.write(2, f"{exc}\n".encode("utf-8", "replace"))
        # atexit suppresses ordinary exceptions, so make unittest's real CI
        # process fail if the session-wide watcher observed a violation.
        with contextlib.suppress(OSError):
            shutil.rmtree(_TEST_HOME, ignore_errors=True)
        os._exit(1)


_start_linked_worktree_lock_guard()
atexit.register(_finish_linked_worktree_lock_guard)
