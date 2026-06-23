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
                "codex": {"defaultModel": "gpt-5.5", "binary": "codex"},
                "cursor": {
                    "defaultModel": "composer-2.5",
                    "argvPrefix": ["agent"],
                    "reasoningEffortModels": {"high": "cursor-thinking"},
                },
                "droid": {"models": {"glm": "glm-5.1"}, "binary": "droid"},
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
            self.assertIn("aliases", payload["reasoning"])
            self.assertIn("glm", payload["reasoning"]["aliases"]["droid"])

    def test_emit_json_includes_reasoning_aliases_in_models_payload(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.describe_payload import models_payload

        with tempfile.TemporaryDirectory() as workspace:
            config = embedded_default_config()
            config["droid"]["models"] = {"glm": "glm-5.1"}
            config["cursor"]["reasoningEffortModels"] = {"high": "cursor-thinking"}
            payload = models_payload(config, "test-config", Path(workspace))
            self.assertIn("reasoningAliases", payload)
            self.assertEqual(payload["reasoningAliases"]["droid"]["glm"]["source"], "bundled")
            self.assertEqual(
                payload["reasoningAliases"]["cursor"][config["cursor"]["defaultModel"]][
                    "supported"
                ],
                ["high"],
            )
            kimi_key = config["kimi"]["defaultModel"]
            kimi = payload["reasoningAliases"]["kimi"][kimi_key]
            self.assertIsNone(kimi["supported"])
            self.assertIn("not supported", kimi["warning"])

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
