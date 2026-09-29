"""Model plumbing, alias maps, and shared resolver tests."""

from __future__ import annotations

import copy
import unittest

from delegate_agent import cli_parser, errors
from tests.delegate_commands_test_base import CommandTestBase


class ResolveModelSelectionTests(unittest.TestCase):
    def setUp(self):
        from delegate_agent import request_build

        self.request_build = request_build

    def test_missing_models_key_passes_through(self):
        self.assertEqual(
            self.request_build.resolve_model_selection({}, "gpt-5.5"),
            "gpt-5.5",
        )


class ModelOptionParserTests(CommandTestBase):
    def test_model_option_value_ok(self):
        parsed = cli_parser.parse_cli(["codex", "safe", "--model", "gpt-5.5", "review"])
        self.assertEqual(parsed.payload.model, "gpt-5.5")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

    def test_model_option_duplicate_rejected(self):
        with self.assertRaises(errors.DelegateError) as ctx:
            cli_parser.parse_cli(["codex", "safe", "--model", "a", "--model", "b", "review"])
        self.assertEqual(ctx.exception.error, "invalid_model")
        self.assertIn("Only one --model is allowed", ctx.exception.message)

    def test_model_option_requires_value(self):
        with self.assertRaises(errors.DelegateError) as ctx:
            cli_parser.parse_cli(["codex", "safe", "--model"])
        self.assertEqual(ctx.exception.error, "missing_model")

    def test_model_option_rejects_dash_prefixed_value(self):
        for value_argv in (["--prompt-file", "task.md"], ["--help"], ["-h"]):
            with self.subTest(value=value_argv[0]):
                with self.assertRaises(errors.DelegateError) as ctx:
                    cli_parser.parse_cli(["codex", "safe", "--model", *value_argv])
                self.assertEqual(ctx.exception.error, "missing_model")

    def test_model_after_prompt_is_prompt_text(self):
        parsed = cli_parser.parse_cli(["codex", "safe", "review", "--model", "gpt-5.5"])
        self.assertIsNone(parsed.payload.model)
        self.assertEqual(parsed.payload.prompt_parts, ["review", "--model", "gpt-5.5"])

    def test_droid_model_before_mode_is_not_a_launch_option(self):
        with self.assertRaises(errors.DelegateError) as ctx:
            cli_parser.parse_cli(["droid", "--model", "X", "safe"])
        self.assertEqual(ctx.exception.error, "invalid_droid_model_syntax")


class EngineModelsConfigTests(unittest.TestCase):
    def setUp(self):
        from delegate_agent import config as config_mod

        self.config_mod = config_mod

    def test_valid_models_map_accepted_for_all_engines(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        for engine in (
            "cursor",
            "droid",
            "codex",
            "kimi",
            "claude",
            "grok",
            "devin",
            "opencode",
            "pi",
            "omp",
        ):
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

    def test_models_rejects_whitespace_only_alias_or_id(self):
        cases = (
            ("codex", {"   ": "gpt-5.5"}),
            ("devin", {"fast": "  \t"}),
            ("grok", {"": "id"}),
            ("kimi", {"fast": ""}),
        )
        for engine, models in cases:
            with self.subTest(engine=engine, models=models):
                config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                config[engine]["models"] = models
                with self.assertRaises(self.config_mod.ConfigError) as ctx:
                    self.config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, f"invalid_{engine}_config")

    def test_models_alias_must_not_equal_own_engine_name(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["codex"]["models"] = {"codex": "private-codex"}
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_codex_config")
        self.assertEqual(
            ctx.exception.message,
            "codex.models alias 'codex' collides with its own engine name "
            "(shadowing the engine's summary entry); rename the alias.",
        )

    def test_models_alias_may_name_another_engine(self):
        # droid.models.grok pointing droid at a Grok model is a real-world
        # BYOK pattern and must stay valid; only alias == its OWN engine is
        # rejected.
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["droid"]["models"] = {"grok": "custom:grok-4.5"}
        self.config_mod.validate_config(config)

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
        for engine in ("cursor", "droid", "codex", "kimi", "claude", "grok", "devin", "opencode"):
            self.assertEqual(config[engine]["models"], {})
        self.assertNotIn("defaultModel", config["droid"])

    def test_opencode_config_accepts_alias_object_and_defaults(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["opencode"] = {
            "binary": "opencode",
            "defaultModel": "openai/gpt-5.5",
            "defaultReasoningEffort": "high",
            "defaultAgent": "builder",
            "models": {
                "fast": "openai/gpt-5.5-mini",
                "deep": {"model": "anthropic/claude-sonnet", "variant": "xhigh"},
            },
        }
        self.config_mod.validate_config(config)

    def test_opencode_config_rejects_bad_alias_object_shapes(self):
        bad_models = (
            {"fast": ["openai/gpt-5.5"]},
            {"fast": {"variant": "high"}},
            {"fast": {"model": "openai/gpt-5.5", "variant": "high", "extra": "nope"}},
            {"fast": {"model": "openai/gpt-5.5", "variant": 123}},
            {"fast": {"model": "openai/gpt-5.5"}},
        )
        for models in bad_models:
            with self.subTest(models=models):
                config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                config["opencode"]["models"] = models
                with self.assertRaises(self.config_mod.ConfigError) as ctx:
                    self.config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, "invalid_opencode_config")

    def test_opencode_config_rejects_leading_dash_flag_injection(self):
        for field in ("defaultAgent", "defaultModel", "defaultReasoningEffort"):
            for token in ("--auto", "--session"):
                with self.subTest(field=field, token=token):
                    config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                    config["opencode"][field] = token
                    with self.assertRaises(self.config_mod.ConfigError) as ctx:
                        self.config_mod.validate_config(config)
                    self.assertEqual(ctx.exception.error, "invalid_opencode_config")
                    self.assertIn("does not start with '-'", ctx.exception.message)

        for token in ("--auto", "--session"):
            with self.subTest(alias="string", token=token):
                config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                config["opencode"]["models"] = {"fast": token}
                with self.assertRaises(self.config_mod.ConfigError) as ctx:
                    self.config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, "invalid_opencode_config")

            with self.subTest(alias="object.model", token=token):
                config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                config["opencode"]["models"] = {
                    "fast": {"model": token, "variant": "high"},
                }
                with self.assertRaises(self.config_mod.ConfigError) as ctx:
                    self.config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, "invalid_opencode_config")

            with self.subTest(alias="object.variant", token=token):
                config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                config["opencode"]["models"] = {
                    "fast": {"model": "openai/gpt-5.5", "variant": token},
                }
                with self.assertRaises(self.config_mod.ConfigError) as ctx:
                    self.config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, "invalid_opencode_config")

    def test_pi_config_accepts_string_and_structured_aliases(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["pi"] = {
            "binary": "pi",
            "defaultModel": "openai-codex/gpt-5.6-sol",
            "defaultReasoningEffort": "high",
            "models": {
                "plain": "anthropic/claude-opus-4-8",
                "quick": {
                    "model": "openai-codex/gpt-5.6-sol",
                    "thinking": "minimal",
                },
            },
        }
        self.config_mod.validate_config(config)

    def test_pi_config_rejects_bad_aliases_and_flag_injection(self):
        bad_models = (
            {"quick": ["openai-codex/gpt-5.6-sol"]},
            {"quick": {"thinking": "minimal"}},
            {"quick": {"model": "openai-codex/gpt-5.6-sol"}},
            {"quick": {"model": "--api-key", "thinking": "minimal"}},
            {"quick": {"model": "openai-codex/gpt-5.6-sol", "thinking": "turbo"}},
            {
                "quick": {
                    "model": "openai-codex/gpt-5.6-sol",
                    "thinking": "minimal",
                    "extra": True,
                }
            },
        )
        for models in bad_models:
            with self.subTest(models=models):
                config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                config["pi"]["models"] = models
                with self.assertRaises(self.config_mod.ConfigError) as ctx:
                    self.config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, "invalid_pi_config")

        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["pi"]["defaultModel"] = "--api-key"
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_pi_config")

    def test_pi_config_rejects_model_thinking_suffix(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["pi"]["models"] = {
            "quick": {"model": "openai-codex/gpt-5.6-sol:off", "thinking": "high"}
        }

        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)

        self.assertEqual(ctx.exception.error, "invalid_pi_config")
        self.assertIn("thinking", ctx.exception.message)

    def test_omp_config_accepts_pi_family_shape_and_rejects_thinking_suffix(self):
        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["omp"] = {
            "binary": "omp",
            "defaultModel": "openai-codex/gpt-5.6-sol",
            "defaultReasoningEffort": "high",
            "models": {
                "plain": "anthropic/claude-opus-4-8",
                "quick": {
                    "model": "openai-codex/gpt-5.6-sol",
                    "thinking": "minimal",
                },
            },
        }
        self.config_mod.validate_config(config)

        config["omp"]["models"] = {
            "quick": {"model": "openai-codex/gpt-5.6-sol:off", "thinking": "high"}
        }
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_omp_config")
        self.assertIn("thinking", ctx.exception.message)

    def test_omp_config_rejects_bad_aliases_and_flag_injection(self):
        bad_models = (
            {"quick": ["openai-codex/gpt-5.6-sol"]},
            {"quick": {"thinking": "minimal"}},
            {"quick": {"model": "openai-codex/gpt-5.6-sol"}},
            {"quick": {"model": "--smol", "thinking": "minimal"}},
            {"quick": {"model": "openai-codex/gpt-5.6-sol", "thinking": "turbo"}},
            {
                "quick": {
                    "model": "openai-codex/gpt-5.6-sol",
                    "thinking": "minimal",
                    "modelRoles": True,
                }
            },
        )
        for models in bad_models:
            with self.subTest(models=models):
                config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
                config["omp"]["models"] = models
                with self.assertRaises(self.config_mod.ConfigError) as ctx:
                    self.config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, "invalid_omp_config")

        config = copy.deepcopy(self.config_mod.DEFAULT_CONFIG)
        config["omp"]["defaultModel"] = "--plan"
        with self.assertRaises(self.config_mod.ConfigError) as ctx:
            self.config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_omp_config")


class ModelOptionHelpTests(unittest.TestCase):
    def test_model_option_on_all_engine_specs(self):
        from delegate_agent import command_help

        for engine in (
            "cursor",
            "droid",
            "codex",
            "kimi",
            "claude",
            "grok",
            "devin",
            "opencode",
            "pi",
            "omp",
        ):
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
        self.assertIn("alias", model_opt.description.lower())


if __name__ == "__main__":
    unittest.main()
