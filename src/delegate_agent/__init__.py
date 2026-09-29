"""Delegate Agent CLI package."""

import sys

# Keep this guard first, and keep everything above and inside it parseable by
# old interpreters (no f-strings, no annotations, no `from __future__` import,
# no 3.11-only syntax). On Python < 3.11 the package's own modules fail with a
# raw ImportError from deep in the tree (`TypeAlias`, `datetime.UTC`) that never
# names the interpreter as the cause; this names it and stops first.
if sys.version_info < (3, 11):  # noqa: UP036 - the point is to run on old Pythons
    sys.stderr.write(
        "delegate: Python 3.11 or newer is required, but this is Python "
        + ".".join(str(part) for part in sys.version_info[:3])
        + " ("
        + (sys.executable or "python")
        + ").\nRun it with a newer interpreter (for example python3.12). The profile "
        + "launcher shim, bin/delegate-profile-shim, picks one automatically and "
        + "honours DELEGATE_PYTHON.\n"
    )
    raise SystemExit(2)

VERSION = "0.31.0"

__all__ = ["VERSION"]
