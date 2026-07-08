"""Wave 2: per-engine model discovery (bundled + config + --live probes)."""

from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from tests.test_model_selection_wave1b import (
    _MODELS_KEYS,
    _MODELS_SUMMARY_KEYS,
    _assert_payload_superset,
)


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class BundledModelsTests(unittest.TestCase):
    def test_bundled_tables_cover_known_engines(self):
        from delegate_agent.bundled_models import BUNDLED_MODELS
        from delegate_agent.constants import KNOWN_ENGINES

        for engine in KNOWN_ENGINES:
            self.assertIn(engine, BUNDLED_MODELS)
            self.assertGreater(len(BUNDLED_MODELS[engine]), 0)

    def test_codex_mirrors_reasoning_ids(self):
        from delegate_agent.bundled_models import BUNDLED_MODELS
        from delegate_agent.reasoning import BUNDLED_REASONING_CAPABILITIES

        codex_ids = {item["id"] for item in BUNDLED_MODELS["codex"]}
        self.assertEqual(codex_ids, set(BUNDLED_REASONING_CAPABILITIES["codex"]))

    def test_devin_has_twenty_seven_models(self):
        from delegate_agent.bundled_models import BUNDLED_MODELS

        self.assertEqual(len(BUNDLED_MODELS["devin"]), 27)


class EngineModelsPayloadTests(unittest.TestCase):
    def setUp(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.model_discovery import engine_models_payload

        self.embedded_default_config = embedded_default_config
        self.engine_models_payload = engine_models_payload

    def test_bundled_only_payload_shape(self):
        config = self.embedded_default_config()
        payload = self.engine_models_payload(config, "codex")
        self.assertEqual(payload["schema"], "delegate.engine-models.v1")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["engine"], "codex")
        self.assertEqual(payload["default"], config["codex"]["defaultModel"])
        self.assertEqual(payload["aliases"], [])
        self.assertIs(payload["live"], False)
        self.assertIsInstance(payload["models"], list)
        self.assertGreater(len(payload["models"]), 0)
        for item in payload["models"]:
            self.assertIn("id", item)
            self.assertEqual(item["source"], "bundled")

    def test_config_aliases_merged(self):
        config = self.embedded_default_config()
        config["codex"]["models"] = {"spark": "gpt-5.3-codex-spark", "custom": "my-private-model"}
        payload = self.engine_models_payload(config, "codex")
        self.assertEqual(
            payload["aliases"],
            [
                {"alias": "custom", "model": "my-private-model"},
                {"alias": "spark", "model": "gpt-5.3-codex-spark"},
            ],
        )
        by_id = {item["id"]: item for item in payload["models"]}
        self.assertEqual(by_id["my-private-model"]["source"], "config")
        self.assertEqual(by_id["my-private-model"]["aliases"], ["custom"])
        self.assertEqual(by_id["gpt-5.3-codex-spark"]["source"], "config")
        self.assertEqual(by_id["gpt-5.3-codex-spark"]["aliases"], ["spark"])

    def test_alias_shadowing_bundled_id_merges_one_entry(self):
        config = self.embedded_default_config()
        config["claude"]["models"] = {"opus": "claude-opus-4-8"}
        payload = self.engine_models_payload(config, "claude")
        matches = [item for item in payload["models"] if item["id"] == "claude-opus-4-8"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source"], "config")
        self.assertEqual(matches[0]["aliases"], ["opus"])

    def test_unknown_engine_errors(self):
        from delegate_agent.errors import DelegateError

        config = self.embedded_default_config()
        with self.assertRaises(DelegateError) as ctx:
            self.engine_models_payload(config, "nope")
        self.assertEqual(ctx.exception.error, "invalid_engine")


class ModelsCommandParseTests(unittest.TestCase):
    def setUp(self):
        from delegate_agent.cli_parser import parse_cli

        self.parse_cli = parse_cli

    def test_live_without_engine_errors(self):
        from delegate_agent.errors import DelegateError

        with self.assertRaises(DelegateError) as ctx:
            self.parse_cli(["models", "--live"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_unknown_engine_errors(self):
        from delegate_agent.errors import DelegateError

        with self.assertRaises(DelegateError) as ctx:
            self.parse_cli(["models", "nope"])
        self.assertEqual(ctx.exception.error, "invalid_engine")

    def test_engine_and_live_parse(self):
        parsed = self.parse_cli(["models", "cursor", "--live"])
        self.assertEqual(parsed.subcommand, "models")
        assert parsed.inspection is not None
        self.assertEqual(parsed.inspection.engine, "cursor")
        self.assertTrue(parsed.inspection.live)
        self.assertFalse(parsed.inspection.summary)


class LiveProbeParseHelpersTests(unittest.TestCase):
    def test_strip_ansi(self):
        from delegate_agent.model_discovery import strip_ansi

        colored = "\x1b[32mcomposer-2.5\x1b[0m - Composer 2.5"
        self.assertEqual(strip_ansi(colored), "composer-2.5 - Composer 2.5")

    def test_parse_cursor_models_output(self):
        from delegate_agent.model_discovery import parse_cursor_models_output

        raw = (
            "Available models\n"
            "\x1b[36mcomposer-2.5\x1b[0m - Composer 2.5\n"
            "\x1b[36mgrok-4.5-fast-xhigh\x1b[0m - Grok 4.5 Fast\n"
        )
        models = parse_cursor_models_output(raw)
        self.assertEqual(
            models,
            [
                {"id": "composer-2.5", "note": "Composer 2.5"},
                {"id": "grok-4.5-fast-xhigh", "note": "Grok 4.5 Fast"},
            ],
        )

    def test_parse_devin_available_line(self):
        from delegate_agent.model_discovery import parse_devin_available_models

        raw = (
            "Error: Unknown model: 'delegate-live-probe-sentinel'\n"
            "Available: adaptive, claude-fable-5, gpt-5.5\n"
        )
        models = parse_devin_available_models(raw)
        self.assertEqual(
            models,
            [
                {"id": "adaptive"},
                {"id": "claude-fable-5"},
                {"id": "gpt-5.5"},
            ],
        )

    def test_parse_droid_custom_models(self):
        from delegate_agent.model_discovery import parse_droid_custom_models

        models = parse_droid_custom_models(
            [
                {"id": "custom:explicit", "displayName": "Explicit"},
                {"displayName": "My Cool Model"},
            ]
        )
        self.assertEqual(models[0]["id"], "custom:explicit")
        self.assertEqual(models[1]["id"], "custom:My-Cool-Model")


class LiveProbeIntegrationTests(unittest.TestCase):
    def setUp(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.model_discovery import engine_models_payload

        self.embedded_default_config = embedded_default_config
        self.engine_models_payload = engine_models_payload

    def test_cursor_live_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            fake = _write_executable(
                bin_dir / "fake-cursor",
                "#!/usr/bin/env bash\n"
                "echo 'Available models'\n"
                "printf '\\033[32mlive-cursor-model\\033[0m - Live Cursor\\n'\n"
                "exit 0\n",
            )
            config = self.embedded_default_config()
            config["cursor"]["argvPrefix"] = [str(fake)]
            payload = self.engine_models_payload(config, "cursor", live=True)
            self.assertIs(payload["live"], True)
            by_id = {item["id"]: item for item in payload["models"]}
            self.assertEqual(by_id["live-cursor-model"]["source"], "live")
            self.assertEqual(by_id["live-cursor-model"]["note"], "Live Cursor")
            self.assertIn("composer-2.5", by_id)

    def test_droid_live_merge_from_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "customModels": [
                            {"id": "custom:live-droid", "displayName": "Live Droid"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config = self.embedded_default_config()
            payload = self.engine_models_payload(
                config,
                "droid",
                live=True,
                factory_settings_path=settings,
            )
            self.assertIs(payload["live"], True)
            by_id = {item["id"]: item for item in payload["models"]}
            self.assertEqual(by_id["custom:live-droid"]["source"], "live")
            self.assertIn("claude-opus-4-8", by_id)

    def test_devin_live_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv_log = Path(tmp) / "argv.log"
            fake = _write_executable(
                Path(tmp) / "fake-devin",
                "#!/usr/bin/env bash\n"
                f'printf \'%s\\n\' "$@" > "{argv_log}"\n'
                'if [ "$1" != "--model" ]; then\n'
                "  echo 'unexpected argv' >&2\n"
                "  exit 2\n"
                "fi\n"
                "echo \"Error: Unknown model: '$2'\" >&2\n"
                "echo 'Available: adaptive, live-devin-model, gpt-5.5' >&2\n"
                "exit 1\n",
            )
            config = self.embedded_default_config()
            config["devin"]["binary"] = str(fake)
            payload = self.engine_models_payload(config, "devin", live=True)
            self.assertIs(payload["live"], True)
            by_id = {item["id"]: item for item in payload["models"]}
            self.assertEqual(by_id["live-devin-model"]["source"], "live")
            self.assertIn("adaptive", by_id)
            from delegate_agent.model_discovery import DEVIN_LIVE_SENTINEL

            recorded = argv_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                recorded,
                ["--model", DEVIN_LIVE_SENTINEL, "-p", "--", "probe"],
            )

    def test_live_degrades_on_missing_binary(self):
        config = self.embedded_default_config()
        config["cursor"]["argvPrefix"] = ["/nonexistent/cursor-agent-xyz"]
        default = config["cursor"]["defaultModel"]
        payload = self.engine_models_payload(config, "cursor", live=True)
        self.assertIs(payload["live"], False)
        self.assertIn("warning", payload)
        by_id = {item["id"]: item for item in payload["models"]}
        # defaultModel is merged as config; other bundled IDs stay bundled.
        self.assertEqual(by_id[default]["source"], "config")
        other = next(mid for mid in by_id if mid != default)
        self.assertEqual(by_id[other]["source"], "bundled")

    def test_live_degrades_on_garbage_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _write_executable(
                Path(tmp) / "fake-cursor",
                "#!/usr/bin/env bash\necho 'totally unrelated'\nexit 0\n",
            )
            config = self.embedded_default_config()
            config["cursor"]["argvPrefix"] = [str(fake)]
            payload = self.engine_models_payload(config, "cursor", live=True)
            self.assertIs(payload["live"], False)
            self.assertIn("warning", payload)

    def test_live_degrades_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _write_executable(
                Path(tmp) / "fake-cursor",
                "#!/usr/bin/env bash\nsleep 30\n",
            )
            config = self.embedded_default_config()
            config["cursor"]["argvPrefix"] = [str(fake)]
            with mock.patch(
                "delegate_agent.model_discovery.LIVE_PROBE_TIMEOUT_SEC",
                0.05,
            ):
                payload = self.engine_models_payload(config, "cursor", live=True)
            self.assertIs(payload["live"], False)
            self.assertIn("warning", payload)

    def test_live_unsupported_engines(self):
        config = self.embedded_default_config()
        for engine in ("codex", "claude", "grok", "kimi"):
            with self.subTest(engine=engine):
                payload = self.engine_models_payload(config, engine, live=True)
                live = payload["live"]
                self.assertIsInstance(live, dict)
                self.assertFalse(live["supported"])
                self.assertIn("no non-interactive model enumeration", live["reason"])
                sources = {item["source"] for item in payload["models"]}
                self.assertTrue(sources <= {"bundled", "config"})
                self.assertNotIn("live", sources)


class PlainModelsUnchangedTests(unittest.TestCase):
    MODELS_KEYS: ClassVar[dict[str, set[str]]] = _MODELS_KEYS
    MODELS_SUMMARY_KEYS: ClassVar[dict[str, set[str]]] = _MODELS_SUMMARY_KEYS

    def test_plain_models_payload_unchanged(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.describe_payload import models_payload, models_summary_payload

        config = embedded_default_config()
        config["droid"]["models"] = {"glm": "glm-5.1"}
        with tempfile.TemporaryDirectory() as workspace:
            payload = models_payload(config, "fixture-config", Path(workspace))
            _assert_payload_superset(self, self.MODELS_KEYS, payload)
            self.assertEqual(payload["ok"], True)
            self.assertEqual(payload["droid"]["models"]["glm"], "glm-5.1")
            summary = models_summary_payload(config, "fixture-config", Path(workspace))
            _assert_payload_superset(self, self.MODELS_SUMMARY_KEYS, summary)

    def test_emit_models_engine_json(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.describe_payload import emit_models

        config = embedded_default_config()
        stdout = io.StringIO()
        code = emit_models(
            config,
            "fixture-config",
            True,
            stdout,
            engine="kimi",
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["engine"], "kimi")
        self.assertEqual(payload["schema"], "delegate.engine-models.v1")


class CommandHelpModelsTests(unittest.TestCase):
    def test_models_spec_mentions_engine_and_live(self):
        from delegate_agent import command_help

        spec = command_help.COMMAND_SPECS["models"]
        usage = " ".join(spec.usage)
        self.assertIn("<engine>", usage)
        self.assertIn("--live", usage)
        flags = {opt.flag for opt in spec.options}
        self.assertIn("--live", flags)
        self.assertTrue(any(arg.name == "engine" for arg in spec.arguments))
        notes = " ".join(spec.notes).lower()
        self.assertIn("advisory", notes)


if __name__ == "__main__":
    unittest.main()
