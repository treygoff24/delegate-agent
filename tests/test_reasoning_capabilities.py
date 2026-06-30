import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent.reasoning import (  # noqa: E402
    CLAUDE_NATIVE_EFFORTS,
    INSPECT_REASONING_DISCOVERY_HINT,
    KIMI_UNSUPPORTED_REASONING_WARNING,
    REASONING_PROFILES,
    TRANSPORT_BY_HARNESS,
    TRANSPORT_CLAUDE_EFFORT_FLAG,
    TRANSPORT_CODEX_CONFIG,
    TRANSPORT_CURSOR_MODEL_SELECTION,
    TRANSPORT_DROID_FLAG,
    ReasoningCapabilityError,
    _alias_key_for_default_model,
    build_alias_reasoning_summaries,
    build_reasoning_capabilities_payload,
    format_explicit_reasoning_effort_error,
    normalize_effort,
    resolve_claude_native_effort,
    resolve_grok_native_effort,
    resolve_reasoning_capability,
)


class ReasoningCapabilityTests(unittest.TestCase):
    def test_no_requested_effort_returns_none_without_model(self):
        capability = resolve_reasoning_capability(
            harness="codex",
            model=None,
            requested_effort=None,
            config={},
        )
        self.assertIsNone(capability)

    def test_codex_known_model_accepts_supported_effort(self):
        capability = resolve_reasoning_capability(
            harness="codex",
            model="gpt-5.5",
            requested_effort="high",
            config={},
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.transport, "codex-config")
        self.assertEqual(capability.source, "bundled")

    def test_codex_spark_model_accepts_bundled_effort(self):
        capability = resolve_reasoning_capability(
            harness="codex",
            model="gpt-5.3-codex-spark",
            requested_effort="high",
            config={},
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.transport, "codex-config")
        self.assertEqual(capability.source, "bundled")

    def test_codex_effort_requires_resolved_model(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="codex",
                model=None,
                requested_effort="high",
                config={},
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_droid_model_rejects_unsupported_effort(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="droid",
                model="glm-5.1",
                requested_effort="medium",
                config={},
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_custom_model_requires_configured_levels(self):
        with self.assertRaises(ReasoningCapabilityError):
            resolve_reasoning_capability(
                harness="droid",
                model="custom:unknown",
                requested_effort="high",
                config={},
            )

    def test_custom_model_accepts_configured_levels(self):
        config = {
            "reasoning": {
                "capabilities": {
                    "droid": {
                        "custom:unknown": {
                            "supported": ["off", "high"],
                            "default": "off",
                        }
                    }
                }
            }
        }
        capability = resolve_reasoning_capability(
            harness="droid",
            model="custom:unknown",
            requested_effort="high",
            config=config,
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.source, "config")
        self.assertEqual(capability.transport, "droid-flag")

    def test_config_source_overrides_cache_and_bundled(self):
        config = {
            "reasoning": {
                "capabilities": {
                    "droid": {
                        "glm-5.1": {
                            "supported": ["off"],
                            "default": "off",
                        }
                    }
                }
            }
        }
        cache = {
            "harnesses": {
                "droid": {
                    "models": {
                        "glm-5.1": {
                            "supported": ["high"],
                            "default": "high",
                        }
                    }
                }
            }
        }
        capability = resolve_reasoning_capability(
            harness="droid",
            model="glm-5.1",
            requested_effort="off",
            config=config,
            cache=cache,
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.source, "config")

    def test_cache_source_overrides_bundled_for_custom_model(self):
        cache = {
            "harnesses": {
                "droid": {
                    "models": {
                        "custom:cached": {
                            "supported": ["high"],
                            "default": "high",
                        }
                    }
                }
            }
        }
        capability = resolve_reasoning_capability(
            harness="droid",
            model="custom:cached",
            requested_effort="high",
            config={},
            cache=cache,
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.source, "cache")

    def test_effort_values_are_literal_not_coerced(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="codex",
                model="gpt-5.5",
                requested_effort="max",
                config={},
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")
        self.assertIn("Requested effort: 'max'.", ctx.exception.message)

    def test_effort_strings_reject_toml_quoting_hazards(self):
        # Effort values are interpolated into a quoted Codex TOML override, so
        # quote/backslash characters must be rejected at the input boundary.
        for bad in ('hi"gh', "hi\\gh", "hi gh", "", None, 3):
            with self.assertRaises(ReasoningCapabilityError):
                normalize_effort(bad)
        self.assertEqual(normalize_effort("xhigh"), "xhigh")

    def test_claude_native_effort_accepts_static_cli_levels(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                self.assertEqual(resolve_claude_native_effort(effort), effort)

    def test_claude_native_effort_rejects_non_cli_levels(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_claude_native_effort("off")
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_grok_native_effort_accepts_static_cli_levels(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                self.assertEqual(resolve_grok_native_effort(effort), effort)

    def test_grok_native_effort_rejects_invalid_levels(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_grok_native_effort("off")
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_grok_native_effort_rejects_malformed_values(self):
        for bad in ("", 'hi"gh', "hi\\gh", "hi gh"):
            with self.subTest(effort=bad):
                with self.assertRaises(ReasoningCapabilityError) as ctx:
                    resolve_grok_native_effort(bad)
                self.assertEqual(ctx.exception.error, "invalid_reasoning_effort")

    def test_capabilities_payload_includes_static_grok_efforts(self):
        payload = build_reasoning_capabilities_payload({}, cache=None)
        grok = payload["harnesses"]["grok"]
        self.assertEqual(grok["transport"], "grok-effort-flag")
        self.assertEqual(grok["source"], "static")
        self.assertEqual(grok["supported"], ["low", "medium", "high", "xhigh", "max"])

    def test_cursor_capabilities_aggregate_efforts_by_model(self):
        payload = build_reasoning_capabilities_payload(
            {
                "cursor": {
                    "reasoningEffortModels": {
                        "low": "gpt-5",
                        "medium": "gpt-5",
                        "high": "sonnet-thinking",
                    }
                }
            },
            cache=None,
        )
        cursor_models = payload["harnesses"]["cursor"]["models"]
        self.assertEqual(cursor_models["gpt-5"]["supported"], ["low", "medium"])
        self.assertEqual(cursor_models["gpt-5"]["source"], "config")
        self.assertEqual(cursor_models["sonnet-thinking"]["supported"], ["high"])

    def test_capabilities_payload_includes_static_claude_efforts(self):
        payload = build_reasoning_capabilities_payload({}, cache=None)
        claude = payload["harnesses"]["claude"]
        self.assertEqual(claude["transport"], "claude-effort-flag")
        self.assertEqual(claude["source"], "static")
        self.assertEqual(claude["supported"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(claude["models"], {})

    def test_capabilities_payload_marks_kimi_unsupported(self):
        payload = build_reasoning_capabilities_payload({}, cache=None)
        kimi = payload["harnesses"]["kimi"]
        self.assertIsNone(kimi["transport"])
        self.assertIsNone(kimi["supported"])
        self.assertEqual(kimi["source"], "none")
        self.assertEqual(kimi["warning"], KIMI_UNSUPPORTED_REASONING_WARNING)

    def test_alias_summary_reports_supported_droid_alias(self):
        config = {
            "droid": {
                "models": {"glm": "glm-5.1"},
                "defaultReasoningEffort": "high",
            }
        }
        summaries = build_alias_reasoning_summaries(config, cache=None)
        glm = summaries["droid"]["glm"]
        self.assertEqual(glm["alias"], "glm")
        self.assertEqual(glm["model"], "glm-5.1")
        self.assertEqual(glm["supported"], ["off", "high"])
        self.assertEqual(glm["default"], "high")
        self.assertEqual(glm["source"], "bundled")
        self.assertEqual(glm["configDefault"], "high")

    def test_alias_summary_warns_when_model_has_no_declaration(self):
        config = {"droid": {"models": {"custom": "custom:missing"}}}
        summaries = build_alias_reasoning_summaries(config, cache=None)
        custom = summaries["droid"]["custom"]
        self.assertIsNone(custom["supported"])
        self.assertEqual(custom["source"], "none")
        self.assertIn("no declared reasoning-effort capability", custom["warning"])

    def test_alias_summary_includes_claude_native_efforts(self):
        config = {"claude": {"defaultModel": "claude-sonnet-4-6"}}
        summaries = build_alias_reasoning_summaries(config, cache=None)
        claude = summaries["claude"]["claude-sonnet-4-6"]
        self.assertEqual(claude["supported"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(claude["source"], "static")

    def test_alias_summary_maps_cursor_efforts_from_config(self):
        config = {
            "cursor": {
                "defaultModel": "composer-2.5",
                "reasoningEffortModels": {"low": "gpt-5", "high": "sonnet-thinking"},
                "defaultReasoningEffort": "high",
            }
        }
        summaries = build_alias_reasoning_summaries(config, cache=None)
        cursor = summaries["cursor"]["composer-2.5"]
        self.assertNotIn("supported", cursor)
        self.assertEqual(cursor["defaultModel"], "composer-2.5")
        self.assertEqual(
            cursor["effortModelRouting"],
            [
                {"effort": "high", "model": "sonnet-thinking"},
                {"effort": "low", "model": "gpt-5"},
            ],
        )
        self.assertEqual(cursor["source"], "config")
        self.assertEqual(cursor["configDefault"], "high")

    def test_alias_summary_marks_kimi_unsupported(self):
        config = {"kimi": {"defaultModel": "kimi-code/kimi-for-coding"}}
        summaries = build_alias_reasoning_summaries(config, cache=None)
        kimi = summaries["kimi"]["kimi-code/kimi-for-coding"]
        self.assertIsNone(kimi["supported"])
        self.assertEqual(kimi["warning"], KIMI_UNSUPPORTED_REASONING_WARNING)

    def test_alias_key_for_default_model_preserves_placeholder_semantics(self):
        self.assertEqual(_alias_key_for_default_model("gpt-5.5"), "gpt-5.5")
        for value in ("", None, 0, [], object()):
            with self.subTest(value=value):
                self.assertEqual(_alias_key_for_default_model(value), "(default)")

    def test_reasoning_summary_payloads_preserve_representative_shape(self):
        config = {
            "reasoning": {
                "capabilities": {
                    "codex": {
                        "gpt-5.5": {
                            "supported": ["low", "high"],
                            "default": "low",
                        }
                    }
                }
            },
            "codex": {
                "defaultModel": "gpt-5.5",
                "defaultReasoningEffort": "high",
            },
            "cursor": {
                "defaultModel": "",
                "reasoningEffortModels": {"high": "cursor-high"},
            },
            "droid": {
                "models": {"glm": "glm-5.1"},
                "defaultReasoningEffort": "high",
            },
            "claude": {
                "defaultModel": "claude-sonnet-4-6",
                "defaultReasoningEffort": "medium",
            },
            "kimi": {"defaultModel": ""},
        }
        expected_aliases = {
            "droid": {
                "glm": {
                    "alias": "glm",
                    "model": "glm-5.1",
                    "supported": ["off", "high"],
                    "source": "bundled",
                    "default": "high",
                    "configDefault": "high",
                }
            },
            "codex": {
                "gpt-5.5": {
                    "alias": "gpt-5.5",
                    "model": "gpt-5.5",
                    "supported": ["low", "high"],
                    "source": "config",
                    "default": "low",
                    "configDefault": "high",
                }
            },
            "cursor": {
                "(default)": {
                    "alias": "(default)",
                    "effortModelRouting": [{"effort": "high", "model": "cursor-high"}],
                    "source": "config",
                }
            },
            "claude": {
                "claude-sonnet-4-6": {
                    "alias": "claude-sonnet-4-6",
                    "supported": ["low", "medium", "high", "xhigh", "max"],
                    "source": "static",
                    "transport": "claude-effort-flag",
                    "model": "claude-sonnet-4-6",
                    "configDefault": "medium",
                }
            },
            "kimi": {
                "(default)": {
                    "alias": "(default)",
                    "supported": None,
                    "source": "none",
                    "warning": KIMI_UNSUPPORTED_REASONING_WARNING,
                }
            },
            "grok": {},
        }

        self.assertEqual(build_alias_reasoning_summaries(config, cache=None), expected_aliases)

        capabilities = build_reasoning_capabilities_payload(config, cache=None)
        self.assertEqual(capabilities["aliases"], expected_aliases)
        self.assertEqual(
            capabilities["harnesses"]["kimi"],
            {
                "transport": None,
                "supported": None,
                "source": "none",
                "warning": KIMI_UNSUPPORTED_REASONING_WARNING,
                "models": {},
            },
        )
        self.assertEqual(
            capabilities["harnesses"]["cursor"]["models"],
            {"cursor-high": {"supported": ["high"], "source": "config"}},
        )
        self.assertEqual(
            capabilities["harnesses"]["codex"]["models"]["gpt-5.5"],
            {"supported": ["low", "high"], "source": "config", "default": "low"},
        )

    def test_unsupported_effort_error_includes_context_and_discovery_hint(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="droid",
                model="glm-5.1",
                requested_effort="medium",
                config={},
                alias="glm",
            )
        message = ctx.exception.message
        self.assertIn("alias 'glm'", message)
        self.assertIn("harness droid", message)
        self.assertIn("model 'glm-5.1'", message)
        self.assertIn("Requested effort: 'medium'.", message)
        self.assertIn("Supported values: off, high", message)
        self.assertIn(INSPECT_REASONING_DISCOVERY_HINT, message)

    def test_explicit_error_formatter_includes_discovery_hint(self):
        message = format_explicit_reasoning_effort_error(
            harness="codex",
            detail="reasoning effort requires a resolved model",
            alias="gpt-5.5",
        )
        self.assertIn("alias 'gpt-5.5'", message)
        self.assertIn(INSPECT_REASONING_DISCOVERY_HINT, message)

    def test_reasoning_profiles_table_rows(self):
        # transport + strategy per harness
        self.assertEqual(REASONING_PROFILES["codex"].transport, TRANSPORT_CODEX_CONFIG)
        self.assertEqual(REASONING_PROFILES["droid"].transport, TRANSPORT_DROID_FLAG)
        self.assertEqual(REASONING_PROFILES["cursor"].transport, TRANSPORT_CURSOR_MODEL_SELECTION)
        self.assertEqual(REASONING_PROFILES["claude"].transport, TRANSPORT_CLAUDE_EFFORT_FLAG)
        self.assertIsNone(REASONING_PROFILES["kimi"].transport)
        self.assertEqual(REASONING_PROFILES["claude"].strategy, "static-enum")
        self.assertEqual(REASONING_PROFILES["claude"].static_efforts, CLAUDE_NATIVE_EFFORTS)
        self.assertEqual(
            REASONING_PROFILES["kimi"].unsupported_warning,
            KIMI_UNSUPPORTED_REASONING_WARNING,
        )

    def test_transport_by_harness_derived_set(self):
        # catches a strategy flip that changes membership (e.g. claude -> model-table
        # would inject claude into the derived dict)
        self.assertEqual(set(TRANSPORT_BY_HARNESS), {"codex", "droid", "cursor"})
        self.assertEqual(TRANSPORT_BY_HARNESS["codex"], TRANSPORT_CODEX_CONFIG)
        self.assertEqual(TRANSPORT_BY_HARNESS["droid"], TRANSPORT_DROID_FLAG)
        self.assertEqual(TRANSPORT_BY_HARNESS["cursor"], TRANSPORT_CURSOR_MODEL_SELECTION)


if __name__ == "__main__":
    unittest.main()
