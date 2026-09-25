"""`models` marks a configured harness binary that this process cannot resolve."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_tilde_path_reads_as_missing_because_launch_does_not_expand_it(self):
        """A `~/bin/x` that exists on disk still fails at launch; say so here too."""
        with tempfile.TemporaryDirectory() as home:
            exe = Path(home) / "bin" / "delegate-test-tilde-binary"
            exe.parent.mkdir()
            exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            exe.chmod(0o755)
            with mock.patch.dict(os.environ, {"HOME": home}):
                self.assertTrue(describe_payload.binary_missing("~/bin/delegate-test-tilde-binary"))
                self.assertFalse(describe_payload.binary_missing(str(exe)))


class ModelsDroidRowTests(unittest.TestCase):
    """dlg-qd1: a retired droid printed a bare `droid:` row no config edit removed."""

    def _text(self, config, *, discovery=None):
        payload = describe_payload.models_payload(config, "test", discovery=discovery)
        out = io.StringIO()
        describe_payload._emit_models_text(payload, "test", out, discovery=discovery)
        rows = [line for line in out.getvalue().splitlines() if line.startswith("droid")]
        return payload, rows

    def test_retired_droid_prints_no_row_but_stays_in_the_payload(self):
        config = embedded_default_config()
        config["droid"]["models"] = None
        with mock.patch.dict(os.environ, {"PATH": "/definitely/not/on/path"}):
            payload, rows = self._text(config)

        self.assertEqual(rows, [])
        # Display-only omission: a compact consumer still learns droid is
        # configured and that its binary does not resolve here.
        self.assertIsNone(payload["droid"]["models"])
        self.assertEqual(payload["droid"]["binary"], "droid")
        self.assertIs(payload["droid"]["binaryMissing"], True)

    def test_configured_droid_models_keep_the_row(self):
        config = embedded_default_config()
        config["droid"]["models"] = {"glm": "glm-5.1"}
        payload, rows = self._text(config)

        self.assertEqual(rows, ["droid:"])
        self.assertEqual(payload["droid"]["models"], {"glm": "glm-5.1"})

    def test_discovered_droid_models_keep_the_row(self):
        # A discovery snapshot that knows droid is a live harness, even when the
        # config lists nothing: the row is not noise about a retired engine.
        config = embedded_default_config()
        config["droid"]["models"] = None
        discovery = {
            "schema": 1,
            "profile": "default",
            "harnesses": {"droid": {"models": {"glm-5.1": {"displayName": "GLM 5.1"}}}},
        }
        _payload, rows = self._text(config, discovery=discovery)

        self.assertEqual(rows, ["droid:"])

    def test_droid_reports_its_binary_like_the_other_engines(self):
        config = embedded_default_config()
        config["droid"]["binary"] = "delegate-test-missing-binary-xyz"
        payload = describe_payload.models_payload(config, "test")
        self.assertEqual(payload["droid"]["binary"], "delegate-test-missing-binary-xyz")
        self.assertIs(payload["droid"]["binaryMissing"], True)

        config["droid"]["binary"] = sys.executable
        payload = describe_payload.models_payload(config, "test")
        self.assertIs(payload["droid"]["binaryMissing"], False)


if __name__ == "__main__":
    unittest.main()
