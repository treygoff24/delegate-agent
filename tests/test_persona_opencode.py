from __future__ import annotations

import copy
import json
import os
import unittest
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase


class PersonaOpenCodeTests(CommandTestBase):
    _PERSONA = "OPEN CODE PERSONA APPENDED"

    def _config_with_profile(self, config_content: str) -> dict[str, object]:
        config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
        config["profiles"] = {
            "detectFrom": [],
            "default": "work",
            "definitions": {"work": {"env": {"OPENCODE_CONFIG_CONTENT": config_content}}},
        }
        return config

    def _request(self, config: dict[str, object], **kwargs):
        return self.build_git_request(
            "opencode",
            "work",
            None,
            "/repo",
            "review task",
            config,
            False,
            persona="editor",
            persona_text_override=self._PERSONA,
            **kwargs,
        )

    def test_agent_config_merges_profile_keys_and_appends_existing_prompt(self):
        original = {
            "model": "provider/base-model",
            "permission": {"read": "allow", "edit": "deny"},
            "provider": {"fixture": {"npm": "provider-sdk"}},
            "plugin": ["fixture-plugin"],
            "agent": {
                "reviewer": {
                    "mode": "primary",
                    "prompt": "PROFILE AGENT PROMPT",
                }
            },
        }
        request = self._request(self._config_with_profile(json.dumps(original)), agent="reviewer")

        self.assertEqual(request.persona_transport, "agent-config")
        self.assertNotIn("OPENCODE_PERMISSION", request.env_overrides or {})
        merged = json.loads((request.env_overrides or {})["OPENCODE_CONFIG_CONTENT"])
        for key in ("model", "permission", "provider", "plugin"):
            self.assertEqual(merged[key], original[key])
        self.assertEqual(
            merged["agent"]["reviewer"]["prompt"],
            "PROFILE AGENT PROMPT\n\nOPEN CODE PERSONA APPENDED",
        )
        self.assertEqual(merged["agent"]["reviewer"]["mode"], "primary")

    def test_malformed_existing_config_falls_back_without_overwriting_config(self):
        malformed = "{not-json"
        config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": malformed}, clear=False):
            request = self._request(config, agent="reviewer")
            ambient_config_after_build = os.environ["OPENCODE_CONFIG_CONTENT"]

        self.assertEqual(request.persona_transport, "prepend")
        self.assertIn(self._PERSONA, request.stdin_text or "")
        self.assertEqual(
            request.warnings,
            ("malformed OPENCODE_CONFIG_CONTENT; using prepend persona transport.",),
        )
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", request.persona_env_overrides or {})
        self.assertEqual(ambient_config_after_build, malformed)

    def test_synthetic_reserved_agent_receives_persona_when_no_agent_is_selected(self):
        request = self._request(self._config_with_profile("{}"))

        self.assertEqual(request.persona_transport, "agent-config")
        self.assertEqual(request.agent, "delegate-persona")
        merged = json.loads((request.env_overrides or {})["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(merged["agent"]["delegate-persona"]["prompt"], self._PERSONA)

    def test_persona_merge_does_not_depend_on_opencode_permission_environment(self):
        original = {
            "model": "provider/base-model",
            "agent": {"reviewer": {"prompt": "existing"}},
        }
        config = self._config_with_profile(json.dumps(original))
        with mock.patch.dict(os.environ, {"OPENCODE_PERMISSION": "ambient-lockdown"}, clear=False):
            request = self._request(config, agent="reviewer")

        self.assertEqual(request.persona_transport, "agent-config")
        merged = json.loads((request.env_overrides or {})["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(merged["agent"]["reviewer"]["prompt"], "existing\n\n" + self._PERSONA)
        self.assertEqual(merged["model"], original["model"])


if __name__ == "__main__":
    unittest.main()
