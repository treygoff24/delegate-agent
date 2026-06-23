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
        from delegate_agent.describe_payload import (
            describe_summary_payload,
            models_payload,
            models_summary_payload,
            redact_discovery_payload,
        )

        with tempfile.TemporaryDirectory() as workspace:
            config = embedded_default_config()
            config["droid"]["models"] = {"glm": "glm-5.1"}
            config["cursor"]["reasoningEffortModels"] = {"high": "cursor-thinking"}
            payload = models_payload(config, "test-config", Path(workspace))
            self.assertIn("reasoningAliases", payload)
            self.assertEqual(payload["reasoningAliases"]["droid"]["glm"]["source"], "bundled")
            cursor_alias = payload["reasoningAliases"]["cursor"][config["cursor"]["defaultModel"]]
            self.assertNotIn("supported", cursor_alias)
            self.assertEqual(
                cursor_alias["effortModelRouting"],
                [{"effort": "high", "model": "cursor-thinking"}],
            )
            kimi_key = config["kimi"]["defaultModel"]
            kimi = payload["reasoningAliases"]["kimi"][kimi_key]
            self.assertIsNone(kimi["supported"])
            self.assertIn("not supported", kimi["warning"])

            summary = models_summary_payload(
                config,
                "/private/test-config.json",
                Path(workspace),
                redacted=True,
            )
            self.assertTrue(summary["summary"])
            self.assertTrue(summary["redacted"])
            self.assertEqual(summary["configSource"], "<redacted-path>")
            by_provider_alias = {
                (item["provider"], item["alias"]): item for item in summary["aliases"]
            }
            self.assertTrue(by_provider_alias[("droid", "glm")]["modelConfigured"])
            self.assertNotIn("glm-5.1", json.dumps(summary))
            self.assertEqual(
                by_provider_alias[("droid", "glm")]["reasoningEfforts"],
                ["off", "high"],
            )
            self.assertEqual(
                by_provider_alias[("cursor", "cursor")]["reasoningEffortRouting"],
                [{"effort": "high", "modelConfigured": True}],
            )
            self.assertNotIn("cursor-thinking", json.dumps(summary))

            redacted = redact_discovery_payload(payload)
            self.assertEqual(redacted["droid"]["models"]["glm"], "<redacted-model-id>")
            self.assertEqual(redacted["runtime"]["modulePath"], "<redacted-path>")

            describe_summary = describe_summary_payload(
                config,
                "/private/test-config.json",
                Path(workspace),
                redacted=True,
            )
            self.assertTrue(describe_summary["summary"])
            self.assertEqual(describe_summary["configSource"], "<redacted-path>")
            self.assertIn(
                "delegate --json models --summary --redacted",
                describe_summary["recommendedDiscovery"],
            )

    def test_full_redacted_discovery_scrubs_paths_model_keys_and_argv_values(self):
        from delegate_agent.describe_payload import (
            describe_payload,
            models_payload,
            redact_discovery_payload,
        )

        with tempfile.TemporaryDirectory() as workspace:
            private_model = "private-provider/private-model-123"
            private_wrapper = "/Users/example/private/bin/wrapper"
            from delegate_agent.config import embedded_default_config

            config = embedded_default_config()
            config["cursor"]["defaultModel"] = private_model
            config["cursor"]["argvPrefix"] = [
                private_wrapper,
                "--profile",
                "private",
                "OPENAI_API_KEY=sk-proj-abc123456789",
                "--model",
                private_model,
                f"--model={private_model}",
            ]
            config["cursor"]["reasoningEffortModels"] = {"high": private_model}
            config["droid"]["models"] = {"private": private_model}
            config["codex"]["binary"] = private_wrapper
            config["codex"]["defaultModel"] = private_model
            config["claude"]["binary"] = private_wrapper
            config["claude"]["defaultModel"] = private_model
            config["kimi"]["binary"] = private_wrapper
            config["kimi"]["defaultModel"] = private_model

            redacted_models = redact_discovery_payload(
                models_payload(config, "/Users/example/private/config.json", Path(workspace))
            )
            redacted_describe = redact_discovery_payload(
                describe_payload(config, "/Users/example/private/config.json", Path(workspace))
            )
            combined = json.dumps([redacted_models, redacted_describe])

            self.assertNotIn(private_model, combined)
            self.assertNotIn(private_wrapper, combined)
            self.assertNotIn("sk-proj-abc123456789", combined)
            self.assertIn("<redacted-model-id>", combined)
            self.assertIn("<redacted-path>", combined)

    def test_models_text_redacted_hides_model_ids_and_paths(self):
        from delegate_agent.describe_payload import emit_models

        private_model = "private-provider/private-model-123"
        private_wrapper = "/Users/example/private/bin/wrapper"
        config = {
            "cursor": {"defaultModel": private_model, "argvPrefix": [private_wrapper]},
            "droid": {"models": {"private": private_model}},
            "codex": {"binary": private_wrapper, "defaultModel": private_model, "profile": ""},
            "claude": {
                "binary": private_wrapper,
                "defaultModel": private_model,
                "workPermissionMode": "auto",
            },
            "kimi": {"binary": private_wrapper, "defaultModel": private_model},
        }
        stdout = io.StringIO()

        code = emit_models(
            config,
            "/Users/example/private/config.json",
            json_mode=False,
            stdout=stdout,
            redacted=True,
        )

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertNotIn(private_model, output)
        self.assertNotIn(private_wrapper, output)
        self.assertIn("<redacted-model-id>", output)
        self.assertIn("<redacted-path>", output)

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
