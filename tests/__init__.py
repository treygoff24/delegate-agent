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
import os
import shutil
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

_TEST_HOME = tempfile.mkdtemp(prefix="delegate-tests-home-")
os.environ["HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, True)
