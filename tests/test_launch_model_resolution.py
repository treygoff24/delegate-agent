"""Launch-time model resolution: bare family names and catalog-absent ids (dlg-qd1).

cursor-agent rejects a bare family word ("Cannot use this model: grok"), so a
pin like ``--model grok`` resolves to the family's newest catalog selector. A
concrete id no catalog lists still launches, but the launch now says the
harness may reject it instead of leaving the child's failure to explain it.
"""

from __future__ import annotations

import unittest
from unittest import mock

from delegate_agent import config as delegate_config
from delegate_agent import harness_discovery, model_discovery
from tests.delegate_commands_test_base import CommandTestBase

CURSOR_GROK_CATALOG = (
    "auto",
    "composer-2.5",
    "cursor-grok-4.5-high",
    "cursor-grok-4.5-high-fast",
    "cursor-grok-4.6-high",
    "cursor-grok-4.6-high-fast",
    "cursor-grok-4.6-low",
    "cursor-grok-4.6-xhigh",
)


def _catalog(engine: str, *selectors: str) -> dict:
    return {
        "schema": 1,
        "profile": "default",
        "harnesses": {engine: {"models": {selector: {} for selector in selectors}}},
    }


class NewestFamilySelectorTests(unittest.TestCase):
    def test_newest_version_non_fast_balanced_tier_wins(self):
        self.assertEqual(
            model_discovery.newest_family_selector("grok", CURSOR_GROK_CATALOG),
            "cursor-grok-4.6-high",
        )

    def test_versions_compare_numerically_not_lexically(self):
        self.assertEqual(
            model_discovery.newest_family_selector("grok", ("grok-4.9-high", "grok-4.10-high")),
            "grok-4.10-high",
        )

    def test_non_words_and_unknown_families_do_not_resolve(self):
        self.assertIsNone(model_discovery.newest_family_selector("grok-4", CURSOR_GROK_CATALOG))
        self.assertIsNone(model_discovery.newest_family_selector("gemini", CURSOR_GROK_CATALOG))


class CursorFamilyNameTests(CommandTestBase):
    def _request(self, model, discovery, config=None):
        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=discovery):
            return self.build_git_request(
                "cursor",
                "work",
                None,
                "/repo",
                "implement",
                config or delegate_config.embedded_default_config(),
                dry_run=True,
                model_override=model,
            )

    def test_bare_grok_resolves_to_newest_discovered_selector(self):
        request = self._request("grok", _catalog("cursor", *CURSOR_GROK_CATALOG))

        self.assertEqual(request.model, "cursor-grok-4.6-high")
        self.assertEqual(request.argv[request.argv.index("--model") + 1], "cursor-grok-4.6-high")
        family = [warning for warning in request.warnings if "family name" in warning]
        self.assertEqual(len(family), 1, request.warnings)
        self.assertIn("'grok'", family[0])
        self.assertIn("newest discovered grok selector", family[0])

    def test_bare_grok_without_discovery_uses_the_bundled_table(self):
        request = self._request("grok", None)

        self.assertEqual(request.model, "grok-4.7-xhigh")
        self.assertTrue(
            any("newest bundled grok selector" in warning for warning in request.warnings),
            request.warnings,
        )

    def test_configured_alias_and_catalog_selectors_are_not_rewritten(self):
        config = delegate_config.embedded_default_config()
        config["cursor"]["models"] = {"grok": "cursor-grok-4.5-high"}
        request = self._request("grok", _catalog("cursor", *CURSOR_GROK_CATALOG), config)
        self.assertEqual(request.model, "cursor-grok-4.5-high")

        request = self._request("auto", _catalog("cursor", *CURSOR_GROK_CATALOG))
        self.assertEqual(request.model, "auto")
        self.assertFalse(any("family name" in warning for warning in request.warnings))

    def test_configured_default_family_resolves_before_argv(self):
        config = delegate_config.embedded_default_config()
        config["cursor"]["defaultModel"] = "grok"
        for discovery, expected in (
            (None, "grok-4.7-xhigh"),
            (_catalog("cursor", *CURSOR_GROK_CATALOG), "cursor-grok-4.6-high"),
        ):
            with self.subTest(discovery=discovery is not None):
                request = self._request(None, discovery, config)
                self.assertEqual(request.model, expected)
                self.assertEqual(request.argv[request.argv.index("--model") + 1], expected)
                self.assertEqual(sum("family name" in warning for warning in request.warnings), 1)

    def test_configured_default_family_uses_discovered_effort_route(self):
        config = delegate_config.embedded_default_config()
        config["cursor"]["defaultModel"] = "grok"
        config["cursor"]["defaultReasoningEffort"] = "low"
        discovery = _catalog("cursor", "cursor-grok-4.6-high", "cursor-grok-4.6-low")
        for model, entry in discovery["harnesses"]["cursor"]["models"].items():
            entry.update(
                routeFamily="grok-4.6",
                routeEffort=model.rsplit("-", 1)[1],
                reasoning={"evidence": "inferred-route"},
            )

        request = self._request(None, discovery, config)

        self.assertEqual(request.model, "cursor-grok-4.6-low")
        self.assertEqual(request.argv[request.argv.index("--model") + 1], "cursor-grok-4.6-low")
        self.assertEqual(request.reasoning_effort, "low")
        self.assertEqual(sum("family name" in warning for warning in request.warnings), 1)

    def test_configured_reasoning_route_family_resolves_before_argv(self):
        config = delegate_config.embedded_default_config()
        config["cursor"]["defaultReasoningEffort"] = "high"
        config["cursor"]["reasoningEffortModels"] = {"high": "grok"}

        request = self._request(None, _catalog("cursor", *CURSOR_GROK_CATALOG), config)

        self.assertEqual(request.model, "cursor-grok-4.6-high")
        self.assertEqual(request.argv[request.argv.index("--model") + 1], "cursor-grok-4.6-high")
        self.assertEqual(request.reasoning_effort, "high")
        self.assertTrue(any("family name" in warning for warning in request.warnings))


class LaunchCatalogWarningTests(CommandTestBase):
    def _request(self, engine, model, discovery):
        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=discovery):
            return self.build_git_request(
                engine,
                "safe",
                None,
                "/repo",
                "review",
                delegate_config.embedded_default_config(),
                dry_run=True,
                model_override=model,
            )

    @staticmethod
    def _catalog_warnings(request):
        return [warning for warning in request.warnings if "catalog" in warning]

    def test_concrete_id_absent_from_discovered_catalog_warns_and_launches(self):
        request = self._request("codex", "gpt-9-nova", _catalog("codex", "gpt-6-sol", "gpt-5.5"))

        self.assertEqual(request.model, "gpt-9-nova")
        (warning,) = self._catalog_warnings(request)
        self.assertIn("codex model 'gpt-9-nova' is absent from the discovered catalog", warning)
        self.assertIn("may reject it", warning)
        self.assertIn("delegate capabilities refresh", warning)

    def test_listed_id_is_silent(self):
        request = self._request("codex", "gpt-5.5", _catalog("codex", "gpt-6-sol", "gpt-5.5"))
        self.assertEqual(self._catalog_warnings(request), [])

    def test_bundled_table_stands_in_when_discovery_has_no_catalog(self):
        request = self._request("grok", "grok-4.7", None)
        self.assertEqual(self._catalog_warnings(request), [])

        request = self._request("grok", "grok-4.77", None)
        (warning,) = self._catalog_warnings(request)
        self.assertIn("not in Delegate's small built-in list", warning)

    def test_unrelated_selectors_without_discovery_are_silent(self):
        for engine, model in (
            ("cursor", "auto"),
            ("cursor", "custom-provider-model"),
            ("codex", "custom-provider-model"),
            ("grok", "custom-provider-model"),
        ):
            with self.subTest(engine=engine, model=model):
                request = self._request(engine, model, None)
                self.assertEqual(request.model, model)
                self.assertEqual(self._catalog_warnings(request), [])

    def test_claude_family_aliases_are_not_catalog_checked(self):
        request = self._request("claude", "opus", _catalog("claude", "claude-opus-5-5"))
        self.assertEqual(self._catalog_warnings(request), [])

    def test_claude_context_window_suffix_is_checked_by_its_base_id(self):
        # `[1m]` is Delegate-side decoration the catalog never stores, so the
        # listed base id must stay silent while a real miss still warns.
        request = self._request(
            "claude", "claude-opus-5-5[1m]", _catalog("claude", "claude-opus-5-5")
        )
        self.assertEqual(self._catalog_warnings(request), [])
        self.assertEqual(request.model, "claude-opus-5-5[1m]")

        request = self._request(
            "claude", "claude-opus-55[1m]", _catalog("claude", "claude-opus-5-5")
        )
        (warning,) = self._catalog_warnings(request)
        self.assertIn("claude-opus-55", warning)
        self.assertIn("claude-opus-5-5", warning)

    def test_cursor_miss_warns_with_or_without_a_discovery_snapshot(self):
        # cursor's own launch check was silent with no snapshot (it read only the
        # discovered catalog), so the shared check now covers the engine on the
        # resolved selector: one warning either way, never two.
        request = self._request("cursor", "cursor-grok-4.6-hihg", None)
        self.assertEqual(request.model, "cursor-grok-4.6-hihg")
        (warning,) = self._catalog_warnings(request)
        self.assertIn("cursor model 'cursor-grok-4.6-hihg' is not in", warning)
        self.assertIn("Delegate's small built-in list", warning)
        self.assertIn("no discovery snapshot catalog exists for cursor", warning)
        self.assertIn("It resembles grok-4.7-xhigh", warning)
        self.assertIn("The launch proceeds", warning)
        self.assertIn("delegate capabilities refresh", warning)
        self.assertNotIn("may reject", warning)

        snapshot = self._request(
            "cursor", "cursor-grok-4.6-hihg", _catalog("cursor", *CURSOR_GROK_CATALOG)
        )
        (warning,) = self._catalog_warnings(snapshot)
        self.assertIn("absent from the discovered catalog", warning)

        resolved = self._request("cursor", "grok", _catalog("cursor", *CURSOR_GROK_CATALOG))
        self.assertEqual(resolved.model, "cursor-grok-4.6-high")
        self.assertEqual(self._catalog_warnings(resolved), [])

        listed = self._request("cursor", "grok-4.7-xhigh", None)
        self.assertEqual(self._catalog_warnings(listed), [])

        resolved = self._request("cursor", "grok", None)
        self.assertEqual(self._catalog_warnings(resolved), [])


if __name__ == "__main__":
    unittest.main()
