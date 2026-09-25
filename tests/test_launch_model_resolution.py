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

        request = self._request("grok", "grok-9", None)
        (warning,) = self._catalog_warnings(request)
        self.assertIn("absent from the bundled catalog", warning)

    def test_claude_family_aliases_are_not_catalog_checked(self):
        request = self._request("claude", "opus", _catalog("claude", "claude-opus-5-5"))
        self.assertEqual(self._catalog_warnings(request), [])


if __name__ == "__main__":
    unittest.main()
