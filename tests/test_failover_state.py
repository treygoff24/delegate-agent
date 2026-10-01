from __future__ import annotations

import datetime
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from delegate_agent import failover_state


class FailoverStateTests(unittest.TestCase):
    def test_absent_estate_uses_delegate_state_without_creating_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            expires = int(time.time()) + 60
            identity = "auth=/ordinary/codex/auth.json\0profile="
            self.assertEqual(failover_state.check_blocked("codex", identity), (False, None))
            failover_state.write_block("codex", identity, expires)
            self.assertEqual(failover_state.check_blocked("codex", identity), (True, expires))
            states = list((Path(home) / ".delegate/failover").glob("*.blocked-until"))
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0].stat().st_mode & 0o777, 0o600)
            failover_state.clear_block("codex", identity)
            self.assertEqual(failover_state.check_blocked("codex", identity), (False, None))
            self.assertFalse((Path(home) / ".ai-profiles").exists())

    def test_absent_personal_home_does_not_use_legacy_profile_state(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            root = Path(home) / ".ai-profiles/runtime/failover"
            root.mkdir(parents=True)
            expires = int(time.time()) + 60
            legacy = root / "codex-personal.blocked-until"
            legacy.write_text(f"{expires}\n")
            personal = Path(home) / ".ai-profiles/runtime/codex/personal"
            identity = f"auth={(personal / 'auth.json').resolve(strict=False)}\0profile="
            self.assertEqual(
                failover_state.check_blocked("codex", identity, profile_alias="personal"),
                (False, None),
            )
            failover_state.write_block("codex", identity, expires + 60, profile_alias="personal")
            self.assertEqual(
                failover_state.check_blocked("codex", identity, profile_alias="personal"),
                (True, expires + 60),
            )
            failover_state.clear_block("codex", identity, profile_alias="personal")
            self.assertEqual(legacy.read_text(), f"{expires}\n")
            self.assertFalse(personal.exists())

    def test_present_personal_home_keeps_legacy_profile_state(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            personal = Path(home) / ".ai-profiles/runtime/codex/personal"
            personal.mkdir(parents=True)
            identity = f"auth={(personal / 'auth.json').resolve(strict=False)}\0profile="
            expires = int(time.time()) + 60
            failover_state.write_block("codex", identity, expires, profile_alias="personal")
            legacy = Path(home) / ".ai-profiles/runtime/failover/codex-personal.blocked-until"
            self.assertEqual(legacy.read_text(), f"{expires}\n")
            self.assertEqual(
                failover_state.check_blocked("codex", identity, profile_alias="personal"),
                (True, expires),
            )
            failover_state.clear_block("codex", identity, profile_alias="personal")
            self.assertFalse(legacy.exists())

    def test_block_is_persistent_monotonic_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            (Path(home) / ".ai-profiles").mkdir()
            near = int(time.time()) + 60
            far = near + 60
            identity = "auth=/tmp/private/codex-home/auth.json\0profile=ops"
            failover_state.write_block("codex", identity, far)
            failover_state.write_block("codex", identity, near)
            self.assertEqual(failover_state.check_blocked("codex", identity), (True, far))
            other_identity = "auth=/b/auth.json\0profile="
            self.assertEqual(failover_state.check_blocked("codex", other_identity), (False, None))
            states = list((Path(home) / ".ai-profiles/runtime/failover").glob("*.blocked-until"))
            self.assertEqual(len(states), 1)
            state = states[0]
            self.assertNotIn("private", state.name)
            self.assertNotIn("ops", state.name)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            failover_state.clear_block("codex", identity)
            self.assertEqual(failover_state.check_blocked("codex", identity), (False, None))

    def test_default_block_expiry_ignores_ai_failover_cooldown_env(self) -> None:
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"HOME": home, "AI_FAILOVER_COOLDOWN": "1"}),
            patch("delegate_agent.failover_state.time.time", return_value=1000),
        ):
            (Path(home) / ".ai-profiles").mkdir()
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
        now = datetime.datetime(2026, 1, 15, 12, 0, 0)

        class FrozenDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        frozen = types.SimpleNamespace(datetime=FrozenDatetime, timedelta=datetime.timedelta)

        def epoch(day: int, hour: int, minute: int) -> int:
            return int(datetime.datetime(2026, 1, day, hour, minute).timestamp())

        cases = (
            ("Try again at 6:30 PM", epoch(15, 18, 30)),  # PM -> 24h, still today
            ("Try again at 12:05 AM", epoch(16, 0, 5)),  # 12 AM -> hour 0, already past: tomorrow
            ("Try again at 12:05 PM", epoch(15, 12, 5)),  # 12 PM stays noon
            ("Try again at 9:15 AM", epoch(16, 9, 15)),  # past time rolls to the next day
            ("Try again at 12:00 PM", epoch(16, 12, 0)),  # exactly now is not in the future
            ("Try again at 0:30 PM", None),
            ("Try again at 6:75 PM", None),
        )
        with patch.object(failover_state, "datetime", frozen):
            for text, expected in cases:
                with self.subTest(text=text):
                    self.assertEqual(failover_state.parse_reset_epoch(text), expected)


if __name__ == "__main__":
    unittest.main()
