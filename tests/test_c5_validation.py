"""Catch bad model and config input before launch, with the fix in the message."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import command_help, harness_enabled, provider_errors
from delegate_agent import config as delegate_config
from delegate_agent.errors import DelegateError
from tests.delegate_commands_test_base import CommandTestBase, make_git_repo


class ClaudeModelPreflightTests(CommandTestBase):
    def _build(self, model, config=None, discovery=None):
        from delegate_agent import harness_discovery

        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=discovery):
            return self.build_git_request(
                "claude",
                "safe",
                None,
                "/repo",
                "review",
                config or delegate_config.embedded_default_config(),
                dry_run=True,
                model_override=model,
                preflight_claude_model=True,
            )

    def test_unknown_selector_is_refused_with_the_closest_valid_values(self):
        with self.assertRaises(DelegateError) as caught:
            self._build("opus-5.5")
        self.assertEqual(caught.exception.error, "invalid_alias")
        message = caught.exception.message
        self.assertIn("Unknown Claude model 'opus-5.5'", message)
        self.assertIn("did you mean opus (claude-opus-5-5)?", message)
        self.assertIn("Valid: aliases opus, sonnet", message)

    def test_claude_prefix_alone_and_empty_brackets_are_refused_the_rest_stays_permissive(self):
        for model in ("claude-", "opus[]", "claude-opus-5-5[]"):
            with self.subTest(model=model), self.assertRaises(DelegateError):
                self._build(model)
        for model in ("opus[garbage]", "claude-x", "sonnet[1m]"):
            with self.subTest(model=model):
                self.assertEqual(self._build(model).model, model)

    def test_input_json_launch_gets_the_same_refusal(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        path = Path(repo.name) / "in.json"
        path.write_text(
            json.dumps(
                {
                    "engine": "claude",
                    "mode": "safe",
                    "model": "opus-5.5",
                    "prompt": "hi",
                    "cwd": repo.name,
                }
            ),
            encoding="utf-8",
        )
        code, stdout, _ = self.run_main(["--json", "run", "--input-json", str(path)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["error"], "invalid_alias")

    def test_cli_dry_run_refuses_before_launch_with_a_structured_error(self):
        code, stdout, _ = self.run_main(
            ["--json", "dry-run", "claude", "safe", "--model", "opus-5.5", "hi"]
        )
        self.assertEqual(code, 2)
        error = json.loads(stdout)
        self.assertEqual(error["error"], "invalid_alias")
        self.assertIn("did you mean opus (claude-opus-5-5)?", error["message"])

    def test_replayed_manifest_model_is_not_a_typed_selector(self):
        # followup/resume rebuild the request from a recorded model; only a
        # fresh CLI launch opts into the typed-selector preflight.
        from delegate_agent import harness_discovery

        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=None):
            request = self.build_git_request(
                "claude",
                "safe",
                None,
                "/repo",
                "review",
                delegate_config.embedded_default_config(),
                dry_run=True,
                model_override="opus-5.5",
            )
        self.assertEqual(request.model, "opus-5.5")

    def test_valid_shapes_still_launch(self):
        for model in ("opus", "opusplan", "sonnet[1m]", "claude-opus-5-5", "claude-future-9-9"):
            with self.subTest(model=model):
                self.assertEqual(self._build(model).model, model)

    def test_configured_alias_target_is_the_operators_own_and_not_refused(self):
        config = delegate_config.embedded_default_config()
        config["claude"]["models"] = {"gateway": "team-gateway-sonnet"}
        self.assertEqual(self._build("gateway", config).model, "team-gateway-sonnet")

    def test_discovered_catalog_id_without_claude_prefix_is_accepted(self):
        discovery = {
            "schema": 1,
            "profile": "default",
            "harnesses": {"claude": {"models": {"custom-thing": {}}}},
        }
        self.assertEqual(self._build("custom-thing", discovery=discovery).model, "custom-thing")


class OmpAliasKeyTests(unittest.TestCase):
    def _validate(self, entry):
        config = delegate_config.embedded_default_config()
        config["omp"]["models"] = {"builder": entry}
        delegate_config.validate_config(config)

    def test_effort_synonym_names_thinking_and_the_allowed_keys(self):
        with self.assertRaises(delegate_config.ConfigError) as caught:
            self._validate({"model": "a/b", "reasoningEffort": "high"})
        message = caught.exception.message
        self.assertIn("omp.models.builder has unknown keys: reasoningEffort", message)
        self.assertIn("Allowed keys are model and thinking", message)
        self.assertIn("rename reasoningEffort to `thinking`", message)

    def test_other_unknown_key_names_allowed_keys_without_the_rename_hint(self):
        with self.assertRaises(delegate_config.ConfigError) as caught:
            self._validate({"model": "a/b", "thinking": "high", "temperature": 1})
        self.assertIn("Allowed keys are model and thinking", caught.exception.message)
        self.assertNotIn("rename", caught.exception.message)

    def test_valid_alias_object_passes_and_help_shows_it(self):
        self._validate({"model": "a/b", "thinking": "high"})
        notes = " ".join(command_help.COMMAND_SPECS["omp"].notes)
        self.assertIn('{"model": "<provider>/<model-id>", "thinking": "high"}', notes)


class CursorAuthHintTests(unittest.TestCase):
    def _hint(self, env, profile=None):
        raw = provider_errors.raw_error(
            message="Authentication required. Please run agent login first",
            status=None,
            code=None,
            source="test",
        )
        with mock.patch.dict(os.environ, env, clear=False):
            record = provider_errors.provider_error_record(
                engine="cursor", raw=raw, profile=profile
            )
        return record["hint"]

    def test_personal_realm_names_realm_and_the_work_alternative(self):
        hint = self._hint({"AI_PROFILE": "personal"})
        self.assertIn("not logged in for the personal realm this run used", hint)
        self.assertIn("--auth-profile work", hint)
        self.assertIn("unset DELEGATE_CONFIG", hint)
        self.assertIn("`estate-cursor login`", hint)

    def test_resolved_run_profile_beats_the_inherited_environment(self):
        # --auth-profile work with AI_PROFILE=personal: the run used work.
        hint = self._hint({"AI_PROFILE": "personal"}, profile="work")
        self.assertIn("for the work realm this run used", hint)
        self.assertIn("--auth-profile personal", hint)
        self.assertNotIn("personal realm this run used", hint)

    def test_work_realm_points_at_personal(self):
        self.assertIn("--auth-profile personal", self._hint({"AI_PROFILE": "work"}))

    def test_unknown_realm_stays_generic_but_keeps_both_actions(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_PROFILE", None)
            os.environ.pop("DELEGATE_PROFILE", None)
            hint = self._hint({})
        self.assertIn("the realm this run used", hint)
        self.assertIn("`estate-cursor login`", hint)


class HarnessEnabledTests(CommandTestBase):
    def _use_config(self, body):
        path = Path(self._config_env["DELEGATE_CONFIG"])
        path.write_text(json.dumps(body), encoding="utf-8")

    def _json(self, argv):
        code, stdout, _ = self.run_main(argv)
        self.assertEqual(code, 0, stdout)
        return json.loads(stdout)

    def test_type_is_validated_in_the_engine_block(self):
        for engine in ("droid", "omp", "claude"):
            with self.subTest(engine=engine):
                config = delegate_config.embedded_default_config()
                config[engine]["enabled"] = "no"
                with self.assertRaises(delegate_config.ConfigError) as caught:
                    delegate_config.validate_config(config)
                self.assertEqual(caught.exception.error, f"invalid_{engine}_config")
                self.assertIn(f"{engine}.enabled must be a boolean", caught.exception.message)

    def test_disabled_harness_leaves_listings_and_fails_launch_fast(self):
        self._use_config({"droid": {"enabled": False}})
        for argv in (
            ["--json", "models"],
            ["--json", "models", "--summary"],
            ["--json", "describe", "--summary"],
            ["--json", "describe", "--full"],
            ["--json", "capabilities"],
        ):
            with self.subTest(argv=argv):
                payload = self._json(argv)
                self.assertNotIn("droid", payload)
                self.assertNotIn("droid", payload.get("engines", []))
                self.assertNotIn("droid", (payload.get("harnesses") or {}))
                for row in payload.get("aliases", []):
                    self.assertNotEqual(row.get("provider"), "droid")
                for row in payload.get("commands", []):
                    self.assertNotEqual(str(row.get("command")).split(" ")[0], "droid")
        self.assertIn("codex", self._json(["--json", "describe", "--summary"])["engines"])

        for argv in (
            ["--json", "dry-run", "droid", "safe", "--model", "glm", "hi"],
            ["--json", "models", "droid"],
        ):
            with self.subTest(argv=argv):
                code, stdout, _ = self.run_main(argv)
                self.assertNotEqual(code, 0)
                error = json.loads(stdout)
                self.assertEqual(error["error"], "harness_disabled")
                self.assertIn("droid.enabled", error["message"])

    def test_engine_keyed_locations_lose_the_disabled_harness(self):
        self._use_config({"droid": {"enabled": False}})
        full = self._json(["--json", "describe", "--full"])
        for path in (
            ("effectivePolicy",),
            ("engineCapabilities",),
            ("engineDefaults",),
            ("promptTransports",),
            ("personaTransports", "safe"),
            ("isolation", "safeNoneAllowed"),
        ):
            node = full
            for key in path:
                node = node[key]
            with self.subTest(path=path):
                self.assertNotIn("droid", node)
                self.assertTrue(node)  # other harnesses are still listed
        caps = self._json(["--json", "capabilities"])
        self.assertNotIn("droid", caps["reasoning"]["harnesses"])
        self.assertNotIn("droid", caps["reasoning"]["aliases"])

    def test_same_named_keys_elsewhere_are_operator_data_not_harness_rows(self):
        payload = {
            "engines": ["droid", "omp"],
            "droid": {},
            "omp": {"models": {"droid": "provider/model", "fast": "provider/fast"}},
            "aliases": [{"provider": "droid"}, {"provider": "omp", "alias": "droid"}],
        }
        stripped = harness_enabled.strip_disabled(payload, frozenset({"droid"}))
        self.assertEqual(stripped["engines"], ["omp"])
        self.assertNotIn("droid", stripped)
        self.assertEqual(stripped["omp"]["models"]["droid"], "provider/model")
        self.assertEqual(stripped["aliases"], [{"provider": "omp", "alias": "droid"}])

    def test_omp_alias_named_for_a_disabled_harness_survives_in_models(self):
        self._use_config(
            {
                "droid": {"enabled": False},
                "omp": {"models": {"droid": "provider/model"}},
            }
        )
        payload = self._json(["--json", "models"])
        self.assertNotIn("droid", payload)
        self.assertEqual(payload["omp"]["models"]["droid"], "provider/model")

    def test_summary_counts_are_recomputed_after_filtering(self):
        self._use_config({"droid": {"models": {"glm": "glm-5.1"}}})
        baseline = self._json(["--json", "models", "--summary"])
        self.assertIn("droid", {row["provider"] for row in baseline["aliases"]})
        self._use_config({"droid": {"enabled": False, "models": {"glm": "glm-5.1"}}})
        summary = self._json(["--json", "models", "--summary"])
        self.assertEqual(summary["counts"]["aliases"], len(summary["aliases"]))
        self.assertEqual(
            summary["counts"]["providers"], len({row["provider"] for row in summary["aliases"]})
        )
        self.assertEqual(summary["counts"]["providers"], baseline["counts"]["providers"] - 1)
        self.assertEqual(summary["counts"]["aliases"], baseline["counts"]["aliases"] - 1)

    def test_enabled_true_or_absent_changes_nothing(self):
        self._use_config({"droid": {"enabled": True}})
        self.assertIn("droid", self._json(["--json", "describe", "--summary"])["engines"])

    def test_strip_disabled_is_a_noop_when_nothing_is_disabled(self):
        payload = {"engines": ["droid"], "droid": {}}
        self.assertIs(harness_enabled.strip_disabled(payload, frozenset()), payload)


if __name__ == "__main__":
    unittest.main()
