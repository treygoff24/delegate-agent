import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent.reasoning import (  # noqa: E402
    ReasoningCapabilityError,
    build_reasoning_capabilities_payload,
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


if __name__ == "__main__":
    unittest.main()
