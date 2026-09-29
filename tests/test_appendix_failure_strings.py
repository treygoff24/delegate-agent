"""The literal failure strings from the 2026-09-28 papercuts audit, as fixtures.

Every string below came from a real cut (docs/audits/2026-09-28-papercuts/appendix/
01 and 02). Before the signature table, `child_failures.classify` returned None for
all of them, so each landed as a generic `exit_nonzero` "Child command failed."
These tests use only `child_failures.classify`, the classifier that exists on the
base code, so they fail there by assertion.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import child_failures, outcome  # noqa: E402

# (literal text from the audit, expected failureReason, expected failureKind)
APPENDIX_STRINGS = (
    (
        "Authentication required. Please run agent login first",
        "auth_failed",
        outcome.FAILURE_PROVIDER_AUTH,
    ),
    ("No API key found for opencode-go", "auth_failed", outcome.FAILURE_PROVIDER_AUTH),
    ("402 Insufficient account funds", "usage_limit", outcome.FAILURE_PROVIDER_QUOTA),
    ("Grok Build usage balance exhausted", "usage_limit", outcome.FAILURE_PROVIDER_QUOTA),
    (
        "websocket closed by server before response.completed",
        "provider_error",
        outcome.FAILURE_PROVIDER_ERROR,
    ),
    (
        "Error loading configuration: invalid type: boolean true, expected struct HooksToml",
        "harness_config_rejected",
        outcome.FAILURE_EXIT_NONZERO,
    ),
    (
        "invalid type: boolean true, expected struct HooksToml",
        "harness_config_rejected",
        outcome.FAILURE_EXIT_NONZERO,
    ),
    (
        "estate-harness: uid_unmapped: Broker returned HTTP 403: uid_unmapped",
        "broker_rejected",
        outcome.FAILURE_PROVIDER_AUTH,
    ),
    ("uid_unmapped: Broker returned HTTP 403", "broker_rejected", outcome.FAILURE_PROVIDER_AUTH),
    ("principal_not_cell_principal", "broker_rejected", outcome.FAILURE_PROVIDER_AUTH),
    ("Cannot use this model: grok", "provider_error", outcome.FAILURE_PROVIDER_ERROR),
    (
        "Too many images in request: 8 > 4",
        "provider_error",
        outcome.FAILURE_PROVIDER_ERROR,
    ),
    (
        "This request was flagged for possible cybersecurity risk",
        "provider_refusal",
        outcome.FAILURE_PROVIDER_REFUSAL,
    ),
    ("18+ age confirmation required", "provider_error", outcome.FAILURE_PROVIDER_ERROR),
    ("Selected model is at capacity", "provider_error", outcome.FAILURE_PROVIDER_ERROR),
)


class AppendixFailureStringTests(unittest.TestCase):
    def test_every_appendix_string_classifies_to_a_named_reason_and_kind(self):
        for text, reason, kind in APPENDIX_STRINGS:
            with self.subTest(text=text):
                failure = child_failures.classify(text)
                self.assertIsNotNone(failure, f"{text!r} did not classify")
                self.assertEqual(failure.code, reason)
                self.assertEqual(outcome.failure_kind_for_reason(failure.code), kind)

    def test_each_classified_message_names_the_fix(self):
        for text, _reason, _kind in APPENDIX_STRINGS:
            with self.subTest(text=text):
                failure = child_failures.classify(text)
                self.assertIsNotNone(failure)
                self.assertGreater(len(failure.message.split()), 5)

    def test_the_omp_missing_key_message_names_the_provider(self):
        failure = child_failures.classify("No API key found for opencode-go")
        self.assertIsNotNone(failure)
        self.assertIn("opencode-go", failure.message)
        self.assertIn("pick another alias", failure.message)


if __name__ == "__main__":
    unittest.main()
