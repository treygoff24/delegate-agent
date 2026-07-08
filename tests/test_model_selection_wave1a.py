"""Wave 1a: --model plumbing, alias maps, and shared resolver."""

from __future__ import annotations

import copy
import io
import json
import unittest
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase, make_git_repo


class ResolveModelSelectionTests(unittest.TestCase):
    def setUp(self):
        from delegate_agent import request_build

        self.request_build = request_build

    def test_alias_key_resolves_to_mapped_id(self):
        section = {"models": {"fast": "provider/fast-id"}, "defaultModel": "ignored"}
        self.assertEqual(
            self.request_build.resolve_model_selection(section, "fast"),
            "provider/fast-id",
        )

    def test_unknown_value_passes_through_verbatim(self):
        section = {"models": {"fast": "provider/fast-id"}}
        self.assertEqual(
            self.request_build.resolve_model_selection(section, "raw-model-id"),
            "raw-model-id",
        )

    def test_empty_models_map_passes_through(self):
        self.assertEqual(
            self.request_build.resolve_model_selection({"models": {}}, "gpt-5.5"),
            "gpt-5.5",
        )

    def test_missing_models_key_passes_through(self):
        self.assertEqual(
            self.request_build.resolve_model_selection({}, "gpt-5.5"),
            "gpt-5.5",
        )


class ModelOptionParserTests(CommandTestBase):
    def test_model_option_value_ok(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "--model", "gpt-5.5", "review"])
        self.assertEqual(parsed.launch.model, "gpt-5.5")
        self.assertEqual(parsed.launch.prompt_parts, ["review"])

    def test_model_option_on_droid_after_mode(self):
        parsed = self.delegate.parse_cli(
            ["droid", "reviewer", "safe", "--model", "raw-id", "review"]
        )
        self.assertEqual(parsed.launch.model_alias, "reviewer")
        self.assertEqual(parsed.launch.model, "raw-id")
        self.assertEqual(parsed.launch.prompt_parts, ["review"])

    def test_model_option_duplicate_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--model", "a", "--model", "b", "review"])
        self.assertEqual(ctx.exception.error, "invalid_model")
        self.assertIn("Only one --model is allowed", ctx.exception.message)

    def test_model_option_requires_value(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--model"])
        self.assertEqual(ctx.exception.error, "missing_model")

    def test_model_option_rejects_dash_prefixed_value(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--model", "--prompt-file", "task.md"])
        self.assertEqual(ctx.exception.error, "missing_model")

    def test_model_option_rejects_help_token_as_value(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--model", "--help"])
        self.assertEqual(ctx.exception.error, "missing_model")

    def test_model_after_prompt_is_prompt_text(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "review", "--model", "gpt-5.5"])
        self.assertIsNone(parsed.launch.model)
        self.assertEqual(parsed.launch.prompt_parts, ["review", "--model", "gpt-5.5"])

    def test_droid_model_before_mode_rejected_by_misplaced_option_guard(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["droid", "--model", "X", "safe"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")
        self.assertEqual(
            ctx.exception.message,
            "Global options must appear before the subcommand.",
        )


class EngineModelsConfigTests(unittest.TestCase):
    def setUp(self):
        from delegate_agent import config as config_mod

        self.config_mod = config_mod

    def test_valid_models_map_accepted_for_all_engines(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        for engine in ("cursor", "droid", "codex", "kimi", "claude", "grok", "devin"):
            config[engine]["models"] = {"fast": f"{engine}-model-id"}
        config["droid"]["defaultModel"] = "droid-default"
        self.config_mod.validate_config(config)

    def test_models_must_be_object(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["codex"]["models"] = ["not", "an", "object"]
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_codex_config")
        self.assertIn("codex.models must be an object", ctx.exception.message)

    def test_models_rejects_non_string_key(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["codex"]["models"] = {1: "model-id"}
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_codex_config")
        self.assertIn("non-empty strings", ctx.exception.message)

    def test_models_rejects_non_string_value(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["claude"]["models"] = {"fast": 123}
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_claude_config")
        self.assertIn("non-empty strings", ctx.exception.message)

    def test_models_rejects_empty_alias_or_id(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["grok"]["models"] = {"": "id"}
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_grok_config")

        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["kimi"]["models"] = {"fast": ""}
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_kimi_config")

    def test_models_alias_must_not_equal_mode_name(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["droid"]["models"] = {"safe": "some-model-id"}
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_droid_config")
        self.assertEqual(
            ctx.exception.message,
            "droid.models alias 'safe' collides with a launch mode name; rename the alias.",
        )

    def test_models_alias_must_not_start_with_dash(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["cursor"]["models"] = {"-fast": "composer-2.5"}
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_cursor_config")
        self.assertIn("must not start with '-'", ctx.exception.message)

    def test_droid_default_model_optional_string(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["droid"]["defaultModel"] = "factory/default"
        self.config_mod.validate_config(config)

        config["droid"]["defaultModel"] = 123
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_droid_config")

    def test_embedded_defaults_include_empty_models_maps(self):
        config = self.config_mod.embedded_default_config()
        for engine in ("cursor", "droid", "codex", "kimi", "claude", "grok", "devin"):
            self.assertEqual(config[engine]["models"], {})
        self.assertNotIn("defaultModel", config["droid"])


class ModelOverrideThreadingTests(CommandTestBase):
    def test_model_override_reaches_engine_build_input_for_modeless_and_droid(self):
        from delegate_agent import request_build

        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}

        cases = (
            (["codex", "safe", "--model", "gpt-5.5", "review"], "codex", "gpt-5.5"),
            (
                ["droid", "safe", "--model", "override-id", "review"],
                "droid",
                "override-id",
            ),
        )
        for argv, engine, expected in cases:
            with self.subTest(engine=engine):
                parsed = self.delegate.parse_cli(["--cwd", repo.name, *argv])
                self.assertEqual(parsed.launch.model, expected)
                captured: list[object] = []
                original = request_build._engine_request_parts

                def _capture(eng, *, build, _captured=captured, _original=original):
                    _captured.append(build)
                    return _original(eng, build=build)

                with mock.patch.object(
                    request_build, "_engine_request_parts", side_effect=_capture
                ):
                    self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
                self.assertEqual(len(captured), 1)
                build = captured[0]
                self.assertEqual(build.model_override, expected)


class ModelOptionHelpTests(unittest.TestCase):
    def test_model_option_on_all_engine_specs(self):
        from delegate_agent import command_help

        for engine in ("cursor", "droid", "codex", "kimi", "claude", "grok", "devin"):
            with self.subTest(engine=engine):
                spec = command_help.COMMAND_SPECS[engine]
                flags = {opt.flag for opt in spec.options}
                self.assertIn("--model", flags)
                self.assertTrue(any("--model" in usage for usage in spec.usage))

        dry = command_help.COMMAND_SPECS["dry-run"]
        self.assertIn("--model", {opt.flag for opt in dry.options})
        self.assertTrue(all("--model" in usage for usage in dry.usage))

        model_opt = next(
            opt for opt in command_help.COMMAND_SPECS["codex"].options if opt.flag == "--model"
        )
        self.assertNotIn("delete", model_opt.description.lower())
        self.assertIn("alias", model_opt.description.lower())


if __name__ == "__main__":
    unittest.main()
