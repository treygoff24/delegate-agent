import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent.reasoning import (  # noqa: E402
    CLAUDE_NATIVE_EFFORTS,
    DEVIN_UNSUPPORTED_REASONING_WARNING,
    INSPECT_REASONING_DISCOVERY_HINT,
    KIMI_UNSUPPORTED_REASONING_WARNING,
    REASONING_PROFILES,
    TRANSPORT_BY_HARNESS,
    TRANSPORT_CLAUDE_EFFORT_FLAG,
    TRANSPORT_CODEX_CONFIG,
    TRANSPORT_CURSOR_MODEL_SELECTION,
    TRANSPORT_DROID_FLAG,
    TRANSPORT_OPENCODE_VARIANT_FLAG,
    TRANSPORT_PI_THINKING_FLAG,
    ReasoningCapabilityError,
    _alias_key_for_default_model,
    build_alias_reasoning_summaries,
    build_reasoning_capabilities_payload,
    format_explicit_reasoning_effort_error,
    normalize_effort,
    resolve_discovered_model_capability,
    resolve_grok_reasoning_capability,
    resolve_native_effort,
    resolve_pi_native_effort,
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

    def test_codex_sol_accepts_bundled_max_effort(self):
        capability = resolve_reasoning_capability(
            harness="codex",
            model="gpt-5.6-sol",
            requested_effort="max",
            config={},
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.effort, "max")
        self.assertEqual(capability.source, "bundled")

    def test_codex_terra_rejects_max_effort(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="codex",
                model="gpt-5.6-terra",
                requested_effort="max",
                config={},
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")
        self.assertIn("gpt-5.6-terra", ctx.exception.message)
        self.assertIn(INSPECT_REASONING_DISCOVERY_HINT, ctx.exception.message)

    def test_codex_declared_model_rejects_max_effort(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="codex",
                model="gpt-5.5",
                requested_effort="max",
                config={},
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")
        self.assertIn("Supported values: low, medium, high, xhigh", ctx.exception.message)

    def test_non_codex_model_rejects_max_effort(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="droid",
                model="gpt-5.5",
                requested_effort="max",
                config={},
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_config_can_extend_codex_max_effort_support(self):
        config = {
            "reasoning": {
                "capabilities": {
                    "codex": {
                        "gpt-5.6-terra": {
                            "supported": ["low", "medium", "high", "xhigh", "max"],
                            "default": "medium",
                        }
                    }
                }
            }
        }
        capability = resolve_reasoning_capability(
            harness="codex",
            model="gpt-5.6-terra",
            requested_effort="max",
            config=config,
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.effort, "max")
        self.assertEqual(capability.source, "config")

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
        for bad in ('hi"gh', "hi\\gh", "hi gh", "--high", "", None, 3):
            with self.assertRaises(ReasoningCapabilityError):
                normalize_effort(bad)
        self.assertEqual(normalize_effort("xhigh"), "xhigh")

    def test_claude_native_effort_accepts_static_cli_levels(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                self.assertEqual(resolve_native_effort("claude", effort), effort)

    def test_claude_native_effort_rejects_non_cli_levels(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_native_effort("claude", "off")
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_grok_native_effort_accepts_static_cli_levels(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                self.assertEqual(resolve_native_effort("grok", effort), effort)

    def test_grok_native_effort_rejects_invalid_levels(self):
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_native_effort("grok", "off")
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_grok_native_effort_rejects_malformed_values(self):
        for bad in ("", 'hi"gh', "hi\\gh", "hi gh"):
            with self.subTest(effort=bad):
                with self.assertRaises(ReasoningCapabilityError) as ctx:
                    resolve_native_effort("grok", bad)
                self.assertEqual(ctx.exception.error, "invalid_reasoning_effort")

    def test_pi_native_effort_accepts_delegate_levels_only(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                self.assertEqual(resolve_pi_native_effort(effort), effort)
        for unsupported in ("off", "minimal"):
            with self.subTest(effort=unsupported), self.assertRaises(ReasoningCapabilityError):
                resolve_pi_native_effort(unsupported)

    def test_capabilities_payload_includes_static_pi_thinking_levels(self):
        payload = build_reasoning_capabilities_payload({}, cache=None)
        pi = payload["harnesses"]["pi"]
        self.assertEqual(pi["transport"], "pi-thinking-flag")
        self.assertEqual(pi["source"], "static")
        self.assertEqual(pi["supported"], ["low", "medium", "high", "xhigh", "max"])

        omp = payload["harnesses"]["omp"]
        self.assertEqual(omp["transport"], "pi-thinking-flag")
        self.assertEqual(omp["source"], "static")
        self.assertEqual(omp["supported"], ["low", "medium", "high", "xhigh", "max"])

    def test_capabilities_payload_includes_static_grok_efforts(self):
        payload = build_reasoning_capabilities_payload({}, cache=None)
        grok = payload["harnesses"]["grok"]
        self.assertEqual(grok["transport"], "grok-effort-flag")
        self.assertEqual(grok["source"], "harness-compatibility")
        self.assertEqual(grok["supported"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(grok["models"]["grok-4.5"]["supported"], ["low", "medium", "high"])
        self.assertEqual(grok["models"]["grok-4.5"]["source"], "bundled")

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

    def test_capabilities_payload_marks_devin_unsupported(self):
        payload = build_reasoning_capabilities_payload({}, cache=None)
        devin = payload["harnesses"]["devin"]
        self.assertIsNone(devin["transport"])
        self.assertIsNone(devin["supported"])
        self.assertEqual(devin["source"], "none")
        self.assertEqual(devin["warning"], DEVIN_UNSUPPORTED_REASONING_WARNING)

    def test_capabilities_payload_marks_opencode_pass_through(self):
        payload = build_reasoning_capabilities_payload({}, cache=None)
        opencode = payload["harnesses"]["opencode"]
        self.assertEqual(opencode["transport"], TRANSPORT_OPENCODE_VARIANT_FLAG)
        self.assertIsNone(opencode["supported"])
        self.assertEqual(opencode["source"], "pass-through")
        self.assertEqual(opencode["models"], {})

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

    def test_alias_summary_renders_opencode_pinned_variant(self):
        config = {
            "opencode": {
                "defaultModel": "openai/gpt-5.5",
                "defaultReasoningEffort": "medium",
                "models": {"deep": {"model": "anthropic/claude-sonnet", "variant": "xhigh"}},
            }
        }
        summaries = build_alias_reasoning_summaries(config, cache=None)
        default = summaries["opencode"]["openai/gpt-5.5"]
        self.assertEqual(default["transport"], TRANSPORT_OPENCODE_VARIANT_FLAG)
        self.assertEqual(default["configDefault"], "medium")
        deep = summaries["opencode"]["deep"]
        self.assertEqual(deep["model"], "anthropic/claude-sonnet")
        self.assertEqual(deep["pinnedVariant"], "xhigh")
        self.assertEqual(deep["source"], "alias")

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
            "devin": {"defaultModel": "swe-1.7"},
            "opencode": {
                "defaultModel": "openai/gpt-5.5",
                "defaultReasoningEffort": "medium",
                "models": {"deep": {"model": "anthropic/claude-sonnet", "variant": "xhigh"}},
            },
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
                    "transport": None,
                    "warning": KIMI_UNSUPPORTED_REASONING_WARNING,
                }
            },
            "devin": {
                "swe-1.7": {
                    "alias": "swe-1.7",
                    "supported": None,
                    "source": "none",
                    "warning": DEVIN_UNSUPPORTED_REASONING_WARNING,
                    "model": "swe-1.7",
                }
            },
            "opencode": {
                "openai/gpt-5.5": {
                    "alias": "openai/gpt-5.5",
                    "supported": None,
                    "source": "pass-through",
                    "transport": "variant-flag",
                    "model": "openai/gpt-5.5",
                    "configDefault": "medium",
                },
                "deep": {
                    "alias": "deep",
                    "supported": None,
                    "source": "alias",
                    "transport": "variant-flag",
                    "model": "anthropic/claude-sonnet",
                    "configDefault": "medium",
                    "pinnedVariant": "xhigh",
                },
            },
            "grok": {},
            "pi": {},
            "omp": {},
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
            capabilities["harnesses"]["devin"],
            {
                "transport": None,
                "supported": None,
                "source": "none",
                "warning": DEVIN_UNSUPPORTED_REASONING_WARNING,
                "models": {},
            },
        )
        self.assertEqual(
            capabilities["harnesses"]["opencode"],
            {
                "transport": "variant-flag",
                "supported": None,
                "source": "pass-through",
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
        self.assertIsNone(REASONING_PROFILES["devin"].transport)
        self.assertIsNone(REASONING_PROFILES["opencode"].transport)
        self.assertEqual(REASONING_PROFILES["pi"].transport, TRANSPORT_PI_THINKING_FLAG)
        self.assertEqual(REASONING_PROFILES["omp"].transport, TRANSPORT_PI_THINKING_FLAG)
        self.assertEqual(REASONING_PROFILES["claude"].strategy, "static-enum")
        self.assertEqual(REASONING_PROFILES["opencode"].strategy, "pass-through")
        self.assertEqual(REASONING_PROFILES["pi"].strategy, "static-enum")
        self.assertEqual(REASONING_PROFILES["omp"].strategy, "static-enum")
        self.assertEqual(REASONING_PROFILES["claude"].static_efforts, CLAUDE_NATIVE_EFFORTS)
        self.assertEqual(
            REASONING_PROFILES["kimi"].unsupported_warning,
            KIMI_UNSUPPORTED_REASONING_WARNING,
        )
        self.assertEqual(
            REASONING_PROFILES["devin"].unsupported_warning,
            DEVIN_UNSUPPORTED_REASONING_WARNING,
        )

    def test_transport_by_harness_derived_set(self):
        # catches a strategy flip that changes membership (e.g. claude -> model-table
        # would inject claude into the derived dict)
        self.assertEqual(set(TRANSPORT_BY_HARNESS), {"codex", "droid", "cursor"})
        self.assertEqual(TRANSPORT_BY_HARNESS["codex"], TRANSPORT_CODEX_CONFIG)
        self.assertEqual(TRANSPORT_BY_HARNESS["droid"], TRANSPORT_DROID_FLAG)
        self.assertEqual(TRANSPORT_BY_HARNESS["cursor"], TRANSPORT_CURSOR_MODEL_SELECTION)

    def test_projection_precedence_is_config_discovery_cache_bundled(self):
        config = {"reasoning": {"capabilities": {"codex": {"gpt-5.5": {"supported": ["config"]}}}}}
        cache = {"harnesses": {"codex": {"models": {"gpt-5.5": {"supported": ["cache"]}}}}}
        discovery = {
            "harnesses": {
                "codex": {
                    "models": {
                        "gpt-5.5": {
                            "reasoning": {
                                "supported": ["discovery"],
                                "evidence": "exact",
                            }
                        }
                    }
                }
            }
        }

        def projected(current_config, current_cache, current_discovery):
            return build_reasoning_capabilities_payload(
                current_config,
                current_cache,
                discovery=current_discovery,
            )["harnesses"]["codex"]["models"]["gpt-5.5"]

        self.assertEqual(projected(config, cache, discovery)["source"], "config")
        self.assertEqual(projected({}, cache, discovery)["source"], "discovery")
        self.assertEqual(projected({}, cache, None)["source"], "cache")
        self.assertEqual(projected({}, None, None)["source"], "bundled")

    def test_discovery_preserves_empty_support_evidence_and_harness_enum(self):
        discovery = {
            "harnesses": {
                "codex": {
                    "models": {
                        "no-reasoning": {"reasoning": {"supported": [], "evidence": "exact"}}
                    }
                },
                "claude": {
                    "models": {},
                    "harnessReasoning": {
                        "supported": ["low", "max"],
                        "default": "low",
                        "evidence": "harness",
                    },
                },
            }
        }
        payload = build_reasoning_capabilities_payload({}, cache=None, discovery=discovery)[
            "harnesses"
        ]
        empty = payload["codex"]["models"]["no-reasoning"]
        self.assertEqual(empty["supported"], [])
        self.assertEqual(empty["evidence"], "exact")
        self.assertEqual(empty["source"], "discovery")
        self.assertEqual(payload["claude"]["supported"], ["low", "max"])
        self.assertEqual(payload["claude"]["default"], "low")
        self.assertEqual(payload["claude"]["evidence"], "harness")

    def test_kimi_discovery_is_visible_but_transport_remains_unsupported(self):
        config = {
            "kimi": {
                "defaultModel": "kimi-code/default",
                "models": {"fast": "kimi-code/fast"},
            }
        }
        discovery = {
            "harnesses": {
                "kimi": {
                    "models": {
                        "kimi-code/default": {
                            "reasoning": {
                                "supported": ["low"],
                                "evidence": "exact",
                            }
                        },
                        "kimi-code/fast": {
                            "reasoning": {
                                "supported": ["high", "max"],
                                "evidence": "exact",
                            }
                        },
                    }
                }
            }
        }
        payload = build_reasoning_capabilities_payload(config, cache=None, discovery=discovery)
        kimi = payload["harnesses"]["kimi"]
        self.assertIsNone(kimi["transport"])
        self.assertEqual(kimi["models"]["kimi-code/fast"]["supported"], ["high", "max"])
        self.assertEqual(kimi["warning"], KIMI_UNSUPPORTED_REASONING_WARNING)
        fast = payload["aliases"]["kimi"]["fast"]
        self.assertEqual(fast["model"], "kimi-code/fast")
        self.assertEqual(fast["supported"], ["high", "max"])
        self.assertEqual(fast["warning"], KIMI_UNSUPPORTED_REASONING_WARNING)
        for alias in payload["aliases"]["kimi"].values():
            self.assertIn("transport", alias)
            self.assertIsNone(alias["transport"])

    def test_runtime_exact_discovery_overrides_bundled_and_preserves_empty_negative(self):
        discovery = {
            "harnesses": {
                "codex": {
                    "models": {"gpt-5.5": {"reasoning": {"supported": [], "evidence": "exact"}}}
                }
            }
        }
        with self.assertRaises(ReasoningCapabilityError) as ctx:
            resolve_reasoning_capability(
                harness="codex",
                model="gpt-5.5",
                requested_effort="high",
                config={},
                discovery=discovery,
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_runtime_config_overrides_discovery(self):
        config = {
            "reasoning": {
                "capabilities": {
                    "codex": {"gpt-5.5": {"supported": ["max"], "evidence": "unknown"}}
                }
            }
        }
        discovery = {
            "harnesses": {
                "codex": {
                    "models": {
                        "gpt-5.5": {"reasoning": {"supported": ["low"], "evidence": "exact"}}
                    }
                }
            }
        }
        capability = resolve_reasoning_capability(
            harness="codex",
            model="gpt-5.5",
            requested_effort="max",
            config=config,
            discovery=discovery,
        )
        assert capability is not None
        self.assertEqual(capability.source, "config")
        self.assertEqual(capability.evidence, "exact")

    def test_grok_exact_bundle_precedes_harness_compatibility(self):
        with self.assertRaises(ReasoningCapabilityError):
            resolve_grok_reasoning_capability(
                model="grok-4.5",
                requested_effort="xhigh",
                config={},
                discovery=None,
            )
        capability, warnings = resolve_grok_reasoning_capability(
            model="future-grok",
            requested_effort="xhigh",
            config={},
            discovery=None,
        )
        assert capability is not None
        self.assertEqual(capability.source, "harness-compatibility")
        self.assertEqual(capability.evidence, "harness")
        self.assertTrue(warnings)

    def test_grok_manual_exact_declaration_precedes_compatibility(self):
        config = {
            "reasoning": {"capabilities": {"grok": {"future-grok": {"supported": ["ultra"]}}}}
        }
        capability, warnings = resolve_grok_reasoning_capability(
            model="future-grok",
            requested_effort="ultra",
            config=config,
            discovery=None,
        )
        assert capability is not None
        self.assertEqual(capability.source, "config")
        self.assertEqual(capability.evidence, "exact")
        self.assertEqual(warnings, ())

    def test_grok_non_exact_discovery_does_not_suppress_bundled_exact_model(self):
        discovery = {
            "harnesses": {
                "grok": {
                    "models": {
                        "grok-4.5": {"reasoning": {"supported": None, "evidence": "unknown"}}
                    }
                }
            }
        }
        with self.assertRaises(ReasoningCapabilityError):
            resolve_grok_reasoning_capability(
                model="grok-4.5",
                requested_effort="xhigh",
                config={},
                discovery=discovery,
            )

    def test_non_exact_discovery_does_not_suppress_generic_fallback(self):
        discovery = {
            "harnesses": {
                "codex": {
                    "models": {"gpt-5.5": {"reasoning": {"supported": None, "evidence": "unknown"}}}
                }
            }
        }
        capability = resolve_reasoning_capability(
            harness="codex",
            model="gpt-5.5",
            requested_effort="high",
            config={},
            discovery=discovery,
        )
        assert capability is not None
        self.assertEqual(capability.source, "bundled")

    def test_opencode_exact_variants_fail_closed_and_unknown_passes_through(self):
        discovery = {
            "harnesses": {
                "opencode": {
                    "models": {
                        "provider/model": {
                            "reasoning": {"supported": ["fast"], "evidence": "exact"}
                        }
                    }
                }
            }
        }
        with self.assertRaises(ReasoningCapabilityError):
            resolve_discovered_model_capability(
                harness="opencode",
                model="provider/model",
                requested_effort="slow",
                discovery=discovery,
            )
        capability, warnings = resolve_discovered_model_capability(
            harness="opencode",
            model="provider/unknown",
            requested_effort="future",
            discovery=discovery,
        )
        assert capability is not None
        self.assertEqual(capability.source, "pass-through")
        self.assertEqual(warnings, ("opencode_variant_unvalidated",))

    def test_pi_and_omp_preserve_exact_and_partial_evidence(self):
        discovery = {
            "harnesses": {
                "pi": {
                    "models": {
                        "provider/off": {"reasoning": {"supported": [], "evidence": "exact"}},
                        "provider/on": {
                            "reasoning": {
                                "supported": ["low", "high"],
                                "evidence": "harness",
                            }
                        },
                    },
                    "harnessReasoning": {"supported": ["low", "high"], "evidence": "harness"},
                },
                "omp": {
                    "models": {
                        "provider/exact": {
                            "reasoning": {"supported": ["minimal"], "evidence": "exact"}
                        },
                        "provider/unknown": {
                            "reasoning": {"supported": None, "evidence": "unknown"}
                        },
                    }
                },
            }
        }
        with self.assertRaises(ReasoningCapabilityError):
            resolve_discovered_model_capability(
                harness="pi",
                model="provider/off",
                requested_effort="low",
                discovery=discovery,
            )
        pi, pi_warnings = resolve_discovered_model_capability(
            harness="pi",
            model="provider/on",
            requested_effort="high",
            discovery=discovery,
        )
        assert pi is not None
        self.assertEqual(pi.evidence, "model-partial")
        self.assertTrue(pi_warnings)
        omp, omp_warnings = resolve_discovered_model_capability(
            harness="omp",
            model="provider/unknown",
            requested_effort="high",
            discovery=discovery,
        )
        assert omp is not None
        self.assertEqual(omp.evidence, "harness-partial")
        self.assertTrue(omp_warnings)


if __name__ == "__main__":
    unittest.main()
