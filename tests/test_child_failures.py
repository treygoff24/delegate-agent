import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import child_failures  # noqa: E402


class ChildFailureClassifierTests(unittest.TestCase):
    def test_usage_limit_preserves_reset_time(self):
        failure = child_failures.classify(
            "Usage limit reached. Your allowance resets at 2026-07-22 01:00 UTC."
        )

        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, "usage_limit")
        self.assertIn("2026-07-22 01:00 UTC", failure.message)

    def test_expired_token_and_401_are_auth_failures(self):
        for text in (
            "Authentication failed: access token expired.",
            "401 Unauthorized: invalid token.",
        ):
            with self.subTest(text=text):
                failure = child_failures.classify(text)
                self.assertIsNotNone(failure)
                self.assertEqual(failure.code, "auth_failed")

    def test_state_database_thread_lookup_is_typed(self):
        for text in (
            "no thread with id: synthetic-thread",
            "state database thread lookup failed for synthetic-thread",
        ):
            with self.subTest(text=text):
                failure = child_failures.classify(text)
                self.assertIsNotNone(failure)
                self.assertEqual(failure.code, "codex_thread_lost")

    def test_unknown_failure_is_not_misclassified(self):
        self.assertIsNone(child_failures.classify("child command exited unexpectedly"))


if __name__ == "__main__":
    unittest.main()
