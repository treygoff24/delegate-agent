import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import (  # noqa: E402
    capability_commands,
    reasoning,
)
from delegate_agent.config import harness_binary  # noqa: E402


class CapabilityCommandTests(unittest.TestCase):
    def test_harness_binary_uses_embedded_default_when_section_missing(self):
        self.assertEqual(harness_binary({}, "codex"), "codex")
        self.assertEqual(harness_binary({}, "droid"), "droid")
        self.assertEqual(harness_binary({}, "kimi"), "kimi")

    def test_harness_binary_uses_configured_binary(self):
        self.assertEqual(
            harness_binary({"codex": {"binary": "codex-test"}}, "codex"),
            "codex-test",
        )

    def test_emit_json_reports_reasoning_payload_without_refreshing(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = {
                "codex": {"defaultModel": "gpt-5.5"},
                "cursor": {"reasoningEffortModels": {"high": "cursor-thinking"}},
            }
            stdout = io.StringIO()

            code = capability_commands.emit(
                capability_commands.CapabilitiesCommand(json_mode=True),
                config=config,
                config_source="test-config",
                workspace=workspace,
                stdout=stdout,
            )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["configSource"], "test-config")
            self.assertEqual(
                payload["cachePath"],
                str(Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"),
            )
            self.assertIn("codex", payload["reasoning"]["harnesses"])
            cursor_models = payload["reasoning"]["harnesses"]["cursor"]["models"]
            self.assertEqual(cursor_models["cursor-thinking"]["supported"], ["high"])

    def test_emit_text_summarizes_harness_model_counts(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = {"cursor": {"reasoningEffortModels": {"high": "cursor-thinking"}}}
            stdout = io.StringIO()

            code = capability_commands.emit(
                capability_commands.CapabilitiesCommand(),
                config=config,
                config_source="test-config",
                workspace=workspace,
                stdout=stdout,
            )

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("reasoning capabilities:", output)
            self.assertIn("cursor: 1 model(s)", output)

    def test_refresh_failure_raises_command_error_with_original_code(self):
        with tempfile.TemporaryDirectory() as workspace:
            failure = reasoning.ReasoningCapabilityError("refresh_failed", "codex failed")

            with (
                mock.patch.object(
                    capability_commands.reasoning,
                    "refresh_reasoning_capabilities",
                    side_effect=failure,
                ),
                self.assertRaises(capability_commands.CapabilitiesError) as caught,
            ):
                capability_commands.emit(
                    capability_commands.CapabilitiesCommand(refresh=True, json_mode=True),
                    config={"codex": {"binary": "codex-test"}},
                    config_source="test-config",
                    workspace=workspace,
                    stdout=io.StringIO(),
                )

            self.assertEqual(caught.exception.error, "refresh_failed")
            self.assertEqual(caught.exception.message, "codex failed")


if __name__ == "__main__":
    unittest.main()
