"""Fixture-based call-mode text extraction pins for opencode and droid.

The opencode fixtures are real captures (see fixtures/opencode/README.md);
the droid fixture is synthetic because droid call-mode text rides the
harness-generic message/completion envelope, so a hand-written record suffices
to pin that generic path staying green for droid call runs (see
fixtures/droid/README.md).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
RUNNER_PATH = ROOT / "src" / "delegate_agent" / "runner.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CallTextFixtureTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module(RUNNER_PATH, "delegate_runner_call_text_fixtures")

    def _execute_fixture_call(self, harness: str, fixture: Path):
        with tempfile.TemporaryDirectory() as workspace:
            return self.runner.execute_call(
                [
                    sys.executable,
                    "-c",
                    "import sys; print(open(sys.argv[1]).read(), end='')",
                    str(fixture),
                ],
                workspace,
                harness=harness,
            )

    def test_opencode_call_fixture_returns_assistant_text(self):
        fixture = ROOT / "tests" / "fixtures" / "opencode" / "simple_text.ndjson"
        result = self._execute_fixture_call("opencode", fixture)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("pong", result.text)
        self.assertNotEqual(result.result_quality, "no_assistant_text")

    def test_droid_call_fixture_returns_assistant_text(self):
        fixture = ROOT / "tests" / "fixtures" / "droid" / "simple_text.jsonl"
        result = self._execute_fixture_call("droid", fixture)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("DROID_CALL_OK", result.text)
        self.assertNotEqual(result.result_quality, "no_assistant_text")
