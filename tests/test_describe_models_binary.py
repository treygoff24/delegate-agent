"""`models` marks a configured harness binary that this process cannot resolve."""

from __future__ import annotations

import io
import os
import sys
import unittest

from delegate_agent import describe_payload
from delegate_agent.config import embedded_default_config


class ModelsBinaryMissingTests(unittest.TestCase):
    def test_payload_marks_unresolvable_binaries(self):
        config = embedded_default_config()
        config["opencode"]["binary"] = "~/definitely/not/here/opencode"
        config["kimi"]["binary"] = "delegate-test-missing-binary-xyz"
        config["claude"]["binary"] = sys.executable

        payload = describe_payload.models_payload(config, "test")

        self.assertIs(payload["opencode"]["binaryMissing"], True)
        self.assertIs(payload["kimi"]["binaryMissing"], True)
        self.assertIs(payload["claude"]["binaryMissing"], False)
        # cursor has no binary row; it must not grow one.
        self.assertNotIn("binaryMissing", payload["cursor"])

    def test_text_view_labels_missing_binaries(self):
        config = embedded_default_config()
        config["opencode"]["binary"] = "~/definitely/not/here/opencode"
        config["claude"]["binary"] = sys.executable
        payload = describe_payload.models_payload(config, "test")

        out = io.StringIO()
        describe_payload._emit_models_text(payload, "test", out)
        text = out.getvalue()

        self.assertIn("opencode: binary=~/definitely/not/here/opencode (missing)", text)
        self.assertIn(f"claude: binary={sys.executable} defaultModel=", text)
        self.assertNotIn(f"{sys.executable} (missing)", text)

    def test_binary_missing_predicate(self):
        self.assertFalse(describe_payload.binary_missing(None))
        self.assertFalse(describe_payload.binary_missing(""))
        self.assertFalse(describe_payload.binary_missing(sys.executable))
        self.assertTrue(describe_payload.binary_missing(os.path.dirname(sys.executable)))
        self.assertTrue(describe_payload.binary_missing("delegate-test-missing-binary-xyz"))


if __name__ == "__main__":
    unittest.main()
