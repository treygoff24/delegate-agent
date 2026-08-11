"""Unit tests for child-launch error message hints (_runner_launch_error).

The shared launch-failure builder appends a sandbox remediation hint only when
the underlying OSError is EPERM; every other errno keeps the existing wording
so callers that fail with ENOENT and similar are unchanged.
"""

import errno
import os
import unittest

from delegate_agent import runner


class LaunchErrorHintTest(unittest.TestCase):
    def test_eperm_appends_sandbox_retry_hint(self) -> None:
        error = runner._runner_launch_error(
            ["codex"],
            "/work",
            PermissionError(errno.EPERM, os.strerror(errno.EPERM)),
        )
        self.assertEqual(error.error, "child_launch_failed")
        self.assertIn("Failed to launch child command 'codex'", error.message)
        self.assertIn("sandboxed", error.message)
        self.assertIn("unsandboxed", error.message)

    def test_enoent_keeps_existing_wording(self) -> None:
        error = runner._runner_launch_error(
            ["missing-agent"],
            "/work",
            FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "missing-agent"),
        )
        self.assertEqual(error.error, "child_launch_failed")
        self.assertIn("missing-agent", error.message)
        self.assertNotIn("sandboxed", error.message)
        self.assertNotIn("unsandboxed", error.message)


if __name__ == "__main__":
    unittest.main()
