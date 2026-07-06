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
"""

import os

os.environ.pop("AI_PROFILE", None)
os.environ.pop("DELEGATE_CONFIG", None)
