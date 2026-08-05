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
            identity = "auth=/tmp/private/codex-home/auth.json\0profile=ops"
            failover_state.write_block("codex", identity, far)
            failover_state.write_block("codex", identity, near)
            self.assertEqual(failover_state.check_blocked("codex", identity), (True, far))
            states = list((Path(home) / ".ai-profiles/runtime/failover").glob("*.blocked-until"))
            self.assertEqual(len(states), 1)
            state = states[0]
            self.assertNotIn("private", state.name)
            self.assertNotIn("ops", state.name)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            failover_state.clear_block("codex", identity)
            self.assertEqual(failover_state.check_blocked("codex", identity), (False, None))

    def test_arbitrary_identities_do_not_cross_contaminate(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            expires = int(time.time()) + 60
            failover_state.write_block("codex", "auth=/a/auth.json\0profile=", expires)
            self.assertEqual(
                failover_state.check_blocked("codex", "auth=/a/auth.json\0profile="),
                (True, expires),
            )
            self.assertEqual(
                failover_state.check_blocked("codex", "auth=/b/auth.json\0profile="),
                (False, None),
            )

    def test_default_block_expiry_ignores_ai_failover_cooldown_env(self) -> None:
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"HOME": home, "AI_FAILOVER_COOLDOWN": "1"}),
            patch("delegate_agent.failover_state.time.time", return_value=1000),
        ):
            identity = "auth=/a/auth.json\0profile="
            failover_state.write_block("codex", identity)

            state = next((Path(home) / ".ai-profiles/runtime/failover").glob("*.blocked-until"))
            self.assertEqual(state.read_text().strip(), "2800")

    def test_known_codex_profile_alias_interops_with_legacy_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            expires = int(time.time()) + 60
            identity = f"auth={(Path(home) / '.codex/auth.json').resolve(strict=False)}\0profile="
            root = Path(home) / ".ai-profiles/runtime/failover"
            root.mkdir(parents=True)
            legacy = root / "codex-work.blocked-until"
            legacy.write_text(f"{expires}\n")
            legacy.chmod(0o600)

            self.assertEqual(
                failover_state.check_blocked("codex", identity, profile_alias="work"),
                (True, expires),
            )
            self.assertEqual(failover_state.check_blocked("codex", identity), (False, None))

            later = expires + 60
            failover_state.write_block("codex", identity, later, profile_alias="work")
            self.assertEqual(legacy.read_text().strip(), str(later))

            failover_state.clear_block("codex", identity, profile_alias="work")
            self.assertFalse(legacy.exists())
            self.assertEqual(
                failover_state.check_blocked("codex", identity, profile_alias="work"),
                (False, None),
            )

    def test_remapped_profile_alias_does_not_read_legacy_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            expires = int(time.time()) + 60
            root = Path(home) / ".ai-profiles/runtime/failover"
            root.mkdir(parents=True)
            legacy = root / "codex-work.blocked-until"
            legacy.write_text(f"{expires}\n")
            identity = "auth=/remapped/work/auth.json\0profile="

            self.assertEqual(
                failover_state.check_blocked("codex", identity, profile_alias="work"),
                (False, None),
            )
            failover_state.write_block("codex", identity, expires + 60, profile_alias="work")
            self.assertEqual(legacy.read_text().strip(), str(expires))

    def test_reset_parser(self) -> None:
        self.assertIsNotNone(failover_state.parse_reset_epoch("Try again at 6:30 PM"))
        self.assertIsNone(failover_state.parse_reset_epoch("Try again at 0:30 PM"))


if __name__ == "__main__":
    unittest.main()
