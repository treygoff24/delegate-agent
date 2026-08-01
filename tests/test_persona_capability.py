from __future__ import annotations

import copy
import unittest
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase


class PersonaCapabilityTests(CommandTestBase):
    _PERSONA_TEXT = "CAPABILITY PERSONA SENTINEL"
    _FALLBACK_WARNING = (
        "claude native-file persona transport was not proven by discovery; using prepend."
    )

    def _request(
        self, discovery: dict[str, object], *, force: str | None = None, dry_run: bool = False
    ):
        config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
        config["claude"]["binary"] = "old-claude-binary"
        if force is not None:
            config["personas"]["forceTransport"] = force
        with mock.patch.object(
            self.delegate.harness_discovery,
            "load_discovery_cache",
            return_value=discovery,
        ):
            return self.build_git_request(
                "claude",
                "work",
                None,
                "/repo",
                "review task",
                config,
                dry_run,
                persona="editor",
                persona_text_override=self._PERSONA_TEXT,
            )

    @staticmethod
    def _discovery(persona_transports: object = None, *, include_field: bool = True):
        record: dict[str, object] = {}
        if include_field:
            record["personaTransports"] = persona_transports
        return {"harnesses": {"claude": record}}

    @staticmethod
    def _production_discovery(native: bool):
        return {
            "harnesses": {
                "claude": {
                    "installed": True,
                    "selector": ["/opt/delegate-test/claude"],
                    "version": "2.1.220",
                    "personaTransports": {"native-file": native},
                }
            }
        }

    def test_present_and_true_capability_selects_native_file(self):
        request = self._request(self._discovery({"native-file": True}))

        self.assertEqual(request.persona_transport, "native-file")
        self.assertIn("--append-system-prompt-file", request.argv)
        self.assertEqual(request.warnings, ())

    def test_absent_capability_falls_back_with_one_line_warning(self):
        request = self._request(self._discovery(include_field=False))

        self.assertEqual(request.persona_transport, "prepend")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertEqual(request.warnings, (self._FALLBACK_WARNING,))

    def test_stale_false_capability_falls_back_with_warning(self):
        request = self._request(self._discovery({"native-file": False}))

        self.assertEqual(request.persona_transport, "prepend")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertEqual(request.warnings, (self._FALLBACK_WARNING,))

    def test_malformed_capability_falls_back_with_warning(self):
        request = self._request(self._discovery({"native-file": "true"}))

        self.assertEqual(request.persona_transport, "prepend")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertEqual(request.warnings, (self._FALLBACK_WARNING,))

    def test_force_transport_pins_prepend_even_when_native_is_proven(self):
        request = self._request(self._discovery({"native-file": True}), force="prepend")

        self.assertEqual(request.persona_transport, "prepend")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertEqual(request.warnings, ())

    def test_force_transport_pins_native_file_without_discovery_proof(self):
        request = self._request(self._discovery(include_field=False), force="native-file")

        self.assertEqual(request.persona_transport, "native-file")
        self.assertIn("--append-system-prompt-file", request.argv)

    def test_old_claude_binary_never_receives_unknown_flag_on_unproven_cache(self):
        request = self._request(self._discovery({"native-file": "not-a-bool"}))

        self.assertEqual(request.argv[0], "old-claude-binary")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertNotIn("<delegate-persona-file>", request.argv)

    def test_selector_drift_drops_a_positive_native_capability(self):
        with (
            mock.patch.object(
                self.delegate.harness_discovery, "selector_has_drifted", return_value=True
            ),
            mock.patch.object(
                self.delegate.harness_discovery, "cached_version_has_drifted"
            ) as version,
        ):
            request = self._request(self._production_discovery(True))
        version.assert_not_called()
        self.assertEqual(request.persona_transport, "prepend")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertEqual(request.warnings, (self._FALLBACK_WARNING,))

    def test_version_drift_drops_a_positive_native_capability(self):
        with (
            mock.patch.object(
                self.delegate.harness_discovery, "selector_has_drifted", return_value=False
            ),
            mock.patch.object(
                self.delegate.harness_discovery, "cached_version_has_drifted", return_value=True
            ),
        ):
            request = self._request(self._production_discovery(True))
        self.assertEqual(request.persona_transport, "prepend")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertEqual(len(request.warnings), 2)
        self.assertEqual(request.warnings[-1], self._FALLBACK_WARNING)

    def test_cached_native_dry_run_never_probes_the_claude_binary(self):
        with (
            mock.patch.object(
                self.delegate.harness_discovery, "selector_has_drifted", return_value=False
            ),
            mock.patch.object(
                self.delegate.harness_discovery, "cached_version_has_drifted"
            ) as version_probe,
        ):
            request = self._request(self._production_discovery(True), dry_run=True)
        version_probe.assert_not_called()
        self.assertEqual(request.persona_transport, "native-file")

    def test_production_shaped_false_capability_stays_prepend(self):
        with mock.patch.object(
            self.delegate.harness_discovery, "selector_has_drifted", return_value=False
        ):
            request = self._request(self._production_discovery(False))
        self.assertEqual(request.persona_transport, "prepend")
        self.assertNotIn("--append-system-prompt-file", request.argv)


if __name__ == "__main__":
    unittest.main()
