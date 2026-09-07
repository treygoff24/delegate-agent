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
import os
import shutil
import subprocess
import sys
import tempfile
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

# Stashed for the rare test that must run a real credentialed binary (the
# live omp write-probe): everything credential-bearing lives under the real
# home, so a probe subprocess launched with the hermetic HOME dies keyless in
# milliseconds and reads as a dead lane.
ORIGINAL_HOME = os.environ.get("HOME")

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="dt-"))
_TEST_HOME = str(_TEST_ROOT / "home")
_TEST_TEMP = str(_TEST_ROOT / "tmp")
Path(_TEST_HOME).mkdir()
Path(_TEST_TEMP).mkdir()
os.environ["HOME"] = _TEST_HOME
for _name in ("TMPDIR", "TMP", "TEMP"):
    os.environ[_name] = _TEST_TEMP
tempfile.tempdir = _TEST_TEMP


def _finish_test_environment() -> None:
    # Both runners use the same ownership guard. Pytest also invokes it for
    # each test; unittest gets this suite-level backstop before temp cleanup.
    from tests.process_guard import reap_delegate_processes

    try:
        reap_delegate_processes(_TEST_ROOT)
    except Exception as exc:
        os.write(2, f"test process cleanup failed; retaining {_TEST_ROOT}: {exc}\n".encode())
        # atexit exceptions otherwise do not make the authoritative gate fail.
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
