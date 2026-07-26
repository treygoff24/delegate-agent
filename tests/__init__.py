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
import tempfile

os.environ.pop("AI_PROFILE", None)
os.environ.pop("DELEGATE_CONFIG", None)

_TEST_HOME = tempfile.mkdtemp(prefix="delegate-tests-home-")
os.environ["HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, True)
