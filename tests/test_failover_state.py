from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from delegate_agent import failover_state


class FailoverStateTests(unittest.TestCase):
    def test_block_is_persistent_monotonic_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            near = int(time.time()) + 60
            far = near + 60
            failover_state.write_block("codex", "work", far)
            failover_state.write_block("codex", "work", near)
            self.assertEqual(failover_state.check_blocked("codex", "work"), (True, far))
            state = Path(home) / ".ai-profiles/runtime/failover/codex-work.blocked-until"
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            failover_state.clear_block("codex", "work")
            self.assertEqual(failover_state.check_blocked("codex", "work"), (False, None))

    def test_kill_switch_and_reset_parser(self) -> None:
        with patch.dict(os.environ, {"AI_FAILOVER": "0"}):
            self.assertFalse(failover_state.failover_enabled())
        self.assertIsNotNone(failover_state.parse_reset_epoch("Try again at 6:30 PM"))
        self.assertIsNone(failover_state.parse_reset_epoch("Try again at 0:30 PM"))

    def test_classifiers_reject_transient_guard(self) -> None:
        self.assertTrue(
            failover_state.classify_codex_usage_limit_narrow("You've hit your usage limit")
        )
        self.assertFalse(failover_state.classify_codex_usage_limit_narrow("not your usage limit"))
        self.assertTrue(failover_state.classify_claude_usage_limit("usage limit reached"))


if __name__ == "__main__":
    unittest.main()
