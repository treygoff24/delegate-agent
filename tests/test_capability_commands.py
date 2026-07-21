import errno
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import (  # noqa: E402
    capability_commands,
    harness_discovery,
    profiles,
    reasoning,
)
from delegate_agent.config import harness_binary  # noqa: E402


class CapabilityCommandTests(unittest.TestCase):
    def test_harness_binary_uses_embedded_default_when_section_missing(self):
        self.assertEqual(harness_binary({}, "codex"), "codex")
        self.assertEqual(harness_binary({}, "droid"), "droid")
        self.assertEqual(harness_binary({}, "kimi"), "kimi")
        self.assertEqual(harness_binary({}, "devin"), "devin")

    def test_harness_binary_uses_configured_binary(self):
        self.assertEqual(
            harness_binary({"codex": {"binary": "codex-test"}}, "codex"),
            "codex-test",
        )

    def test_emit_json_reports_reasoning_payload_without_refreshing(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = {
                "codex": {"defaultModel": "gpt-5.5", "binary": "codex"},
                "cursor": {
                    "defaultModel": "composer-2.5",
                    "argvPrefix": ["agent"],
                    "reasoningEffortModels": {"high": "cursor-thinking"},
                },
                "droid": {"models": {"glm": "glm-5.1"}, "binary": "droid"},
            }
            stdout = io.StringIO()

            code = capability_commands.emit(
                capability_commands.CapabilitiesCommand(json_mode=True),
                config=config,
                config_source="test-config",
                workspace=workspace,
                stdout=stdout,
            )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["configSource"], "test-config")
            self.assertEqual(
                payload["cachePath"],
                str(harness_discovery.discovery_cache_path(None)),
            )
            self.assertIn("codex", payload["reasoning"]["harnesses"])
            cursor_models = payload["reasoning"]["harnesses"]["cursor"]["models"]
            self.assertEqual(cursor_models["cursor-thinking"]["supported"], ["high"])
            self.assertIn("aliases", payload["reasoning"])
            self.assertIn("glm", payload["reasoning"]["aliases"]["droid"])

    def test_emit_json_includes_reasoning_aliases_in_models_payload(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.describe_payload import (
            describe_summary_payload,
            models_payload,
            models_summary_payload,
        )

        with tempfile.TemporaryDirectory() as workspace:
            config = embedded_default_config()
            config["droid"]["models"] = {"glm": "glm-5.1"}
            config["cursor"]["reasoningEffortModels"] = {"high": "cursor-thinking"}
            payload = models_payload(config, "test-config", Path(workspace))
            self.assertIn("reasoningAliases", payload)
            self.assertEqual(payload["reasoningAliases"]["droid"]["glm"]["source"], "bundled")
            cursor_alias = payload["reasoningAliases"]["cursor"][config["cursor"]["defaultModel"]]
            self.assertNotIn("supported", cursor_alias)
            self.assertEqual(
                cursor_alias["effortModelRouting"],
                [{"effort": "high", "model": "cursor-thinking"}],
            )
            kimi_key = "(default)"
            kimi = payload["reasoningAliases"]["kimi"][kimi_key]
            self.assertIsNone(kimi["supported"])
            self.assertIn("not supported", kimi["warning"])
            devin_key = "(default)"
            devin = payload["reasoningAliases"]["devin"][devin_key]
            self.assertIsNone(devin["supported"])
            self.assertIn("not supported", devin["warning"])
            opencode_key = "(default)"
            opencode = payload["reasoningAliases"]["opencode"][opencode_key]
            self.assertIsNone(opencode["supported"])
            self.assertEqual(opencode["transport"], "variant-flag")

            summary = models_summary_payload(
                config,
                "/private/test-config.json",
                Path(workspace),
            )
            self.assertTrue(summary["summary"])
            self.assertEqual(summary["configSource"], "/private/test-config.json")
            by_provider_alias = {
                (item["provider"], item["alias"]): item for item in summary["aliases"]
            }
            self.assertEqual(by_provider_alias[("droid", "glm")]["model"], "glm-5.1")
            self.assertEqual(
                by_provider_alias[("droid", "glm")]["reasoningEfforts"],
                ["off", "high"],
            )
            self.assertEqual(
                by_provider_alias[("cursor", "cursor")]["reasoningEffortRouting"],
                [{"effort": "high", "model": "cursor-thinking"}],
            )
            self.assertEqual(
                by_provider_alias[("opencode", "opencode")]["command"],
                "delegate opencode {safe,work,call}",
            )

            describe_summary = describe_summary_payload(
                config,
                "/private/test-config.json",
                Path(workspace),
            )
            self.assertTrue(describe_summary["summary"])
            self.assertEqual(describe_summary["configSource"], "/private/test-config.json")
            self.assertIn(
                "delegate --json models --summary",
                describe_summary["recommendedDiscovery"],
            )
            self.assertIn("delegate setup", describe_summary["recommendedDiscovery"])

    def test_opencode_describe_and_models_surfaces(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.describe_payload import (
            describe_payload,
            models_payload,
            models_summary_payload,
        )

        config = embedded_default_config()
        config["opencode"]["defaultModel"] = "openai/gpt-5"
        config["opencode"]["defaultAgent"] = "reviewer"
        config["opencode"]["defaultReasoningEffort"] = "high"
        config["opencode"]["models"] = {
            "deep": {"model": "anthropic/claude-sonnet-4-5", "variant": "xhigh"}
        }
        models = models_payload(config, "test-config")
        self.assertEqual(
            models["opencode"],
            {
                "binary": "opencode",
                "defaultModel": "openai/gpt-5",
                "defaultReasoningEffort": "high",
                "defaultAgent": "reviewer",
                "models": {"deep": {"model": "anthropic/claude-sonnet-4-5", "variant": "xhigh"}},
            },
        )

        describe = describe_payload(config, "test-config")
        self.assertEqual(describe["engineDefaults"]["opencode"]["binary"], "opencode")
        self.assertEqual(describe["engineDefaults"]["opencode"]["defaultAgent"], "reviewer")
        self.assertEqual(describe["promptTransports"]["opencode"], "stdin")
        self.assertFalse(describe["isolation"]["safeNoneAllowed"]["opencode"])
        self.assertIn("opencode", describe["policyFieldSupport"])
        self.assertIn("opencode", describe["modeMapping"])
        self.assertIn("--dir", describe["modeMapping"]["opencode"]["safe"])
        self.assertIn("--pure", describe["modeMapping"]["opencode"]["safe"])
        self.assertNotIn("--auto", describe["modeMapping"]["opencode"]["safe"])
        self.assertIn("--auto", describe["modeMapping"]["opencode"]["work"])
        capabilities = describe["engineCapabilities"]
        self.assertTrue(capabilities["claude"]["pureCall"])
        self.assertFalse(capabilities["opencode"]["pureCall"])
        self.assertFalse(capabilities["codex"]["pureCall"])
        self.assertTrue(capabilities["claude"]["pureTripwire"])
        self.assertFalse(capabilities["opencode"]["pureTripwire"])
        self.assertFalse(capabilities["codex"]["pureTripwire"])
        self.assertTrue(capabilities["claude"]["structuredOutput"])
        self.assertTrue(capabilities["codex"]["structuredOutput"])
        # Legacy outputSchema alias must mirror structuredOutput, not contradict it.
        for engine, caps in capabilities.items():
            self.assertEqual(
                caps["outputSchema"], caps["structuredOutput"], f"{engine} outputSchema drift"
            )
        self.assertTrue(capabilities["claude"]["usageEvents"])
        self.assertTrue(capabilities["claude"]["promptStdin"])
        self.assertTrue(capabilities["opencode"]["promptStdin"])
        self.assertFalse(capabilities["pi"]["pureCall"])
        self.assertFalse(capabilities["pi"]["pureTripwire"])
        self.assertFalse(capabilities["pi"]["structuredOutput"])
        self.assertTrue(capabilities["pi"]["noSessionPersistence"])
        self.assertFalse(capabilities["pi"]["usageEvents"])
        self.assertTrue(capabilities["pi"]["promptStdin"])
        self.assertFalse(capabilities["omp"]["pureCall"])
        self.assertFalse(capabilities["omp"]["pureTripwire"])
        self.assertFalse(capabilities["omp"]["structuredOutput"])
        self.assertTrue(capabilities["omp"]["noSessionPersistence"])
        self.assertFalse(capabilities["omp"]["usageEvents"])
        self.assertFalse(capabilities["omp"]["promptStdin"])

        summary = models_summary_payload(config, "test-config")
        by_provider_alias = {(item["provider"], item["alias"]): item for item in summary["aliases"]}
        deep = by_provider_alias[("opencode", "deep")]
        self.assertEqual(deep["model"], "anthropic/claude-sonnet-4-5")
        self.assertEqual(deep["pinnedVariant"], "xhigh")
        self.assertTrue(deep["available"])

    def test_describe_summary_matches_full_slice_without_building_argv(self):
        from delegate_agent import describe_payload as describe_module
        from delegate_agent.config import embedded_default_config

        config = embedded_default_config()
        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)
            full = describe_module.describe_payload(
                config,
                "/private/test-config.json",
                workspace_path,
            )

            def fail_argv_build(*_args, **_kwargs):
                raise AssertionError("describe --summary must not build harness argv")

            with (
                mock.patch.object(
                    describe_module,
                    "build_codex_argv",
                    side_effect=fail_argv_build,
                ),
                mock.patch.object(
                    describe_module,
                    "build_claude_argv",
                    side_effect=fail_argv_build,
                ),
            ):
                summary = describe_module.describe_summary_payload(
                    config,
                    "/private/test-config.json",
                    workspace_path,
                )

            self.assertEqual(summary["configResolution"], full["configResolution"])
            self.assertEqual(summary["commands"], full["commands"])

    def _discovery_scrub_config(self):
        from delegate_agent.config import embedded_default_config

        fake_secret = "sk-proj-" + "abc123456789012345678901234567890"
        conn_password = "db-" + "secret-password-999"
        model_id = "vendor/real-model"
        wrapper_path = "/Users/x/bin/wrapper"
        config = embedded_default_config()
        config["cursor"]["defaultModel"] = model_id
        config["cursor"]["argvPrefix"] = [
            wrapper_path,
            f"OPENAI_API_KEY={fake_secret}",
            f"postgres://user:{conn_password}@db.internal:5432/app",
        ]
        config["droid"]["models"] = {"reviewer": model_id}
        config["codex"]["binary"] = wrapper_path
        config["codex"]["defaultModel"] = model_id
        return config, fake_secret, conn_password, model_id, wrapper_path

    def test_discovery_payload_scrub_masks_secrets_and_preserves_model_ids_and_paths(self):
        from delegate_agent import redaction
        from delegate_agent.describe_payload import describe_payload, models_payload

        config, fake_secret, conn_password, model_id, wrapper_path = self._discovery_scrub_config()
        with tempfile.TemporaryDirectory() as workspace:
            models = redaction.redact_value(
                models_payload(config, "/Users/x/config.json", Path(workspace))
            )
            describe = redaction.redact_value(
                describe_payload(config, "/Users/x/config.json", Path(workspace))
            )
            combined = json.dumps([models, describe])

            self.assertIn("***", combined)
            self.assertNotIn(fake_secret, combined)
            self.assertNotIn(conn_password, combined)
            self.assertIn(model_id, combined)
            self.assertIn(wrapper_path, combined)

    def test_discovery_scrub_matrix(self):
        from delegate_agent.describe_payload import emit_describe, emit_models

        config, fake_secret, conn_password, model_id, wrapper_path = self._discovery_scrub_config()
        cases = (
            ("emit_models", emit_models, True, True, False, False),
            ("emit_models", emit_models, True, False, True, True),
            ("emit_models", emit_models, False, True, False, False),
            ("emit_models", emit_models, False, False, True, True),
            ("emit_describe", emit_describe, True, True, False, False),
            ("emit_describe", emit_describe, True, False, True, False),
            ("emit_describe", emit_describe, False, True, False, False),
            ("emit_describe", emit_describe, False, False, False, False),
        )
        with tempfile.TemporaryDirectory() as workspace:
            for name, emitter, json_mode, summary, expect_masked, expect_model_path in cases:
                with self.subTest(emitter=name, json_mode=json_mode, summary=summary):
                    stdout = io.StringIO()
                    code = emitter(
                        config,
                        "/Users/x/config.json",
                        json_mode,
                        stdout,
                        workspace=Path(workspace),
                        summary=summary,
                    )
                    output = stdout.getvalue()
                    self.assertEqual(code, 0)
                    self.assertNotIn(fake_secret, output)
                    self.assertNotIn(conn_password, output)
                    if expect_masked:
                        self.assertIn("***", output)
                    if expect_model_path:
                        self.assertIn(model_id, output)
                        self.assertIn(wrapper_path, output)

    def test_emit_text_summarizes_harness_model_counts(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = {"cursor": {"reasoningEffortModels": {"high": "cursor-thinking"}}}
            stdout = io.StringIO()

            code = capability_commands.emit(
                capability_commands.CapabilitiesCommand(),
                config=config,
                config_source="test-config",
                workspace=workspace,
                stdout=stdout,
            )

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("reasoning capabilities:", output)
            self.assertIn("cursor: 1 model(s)", output)

    def test_refresh_with_no_installed_harnesses_raises_specific_error(self):
        with tempfile.TemporaryDirectory() as workspace:
            with (
                mock.patch.object(
                    capability_commands.harness_discovery,
                    "refresh_discovery",
                    return_value={
                        "snapshot": harness_discovery.empty_snapshot(),
                        "attempts": {"codex": {"installed": False, "probeStatus": "missing"}},
                        "updatedHarnesses": [],
                        "staleHarnesses": [],
                        "cachePath": "/tmp/default.json",
                    },
                ),
                self.assertRaises(capability_commands.CapabilitiesError) as caught,
            ):
                capability_commands.emit(
                    capability_commands.CapabilitiesCommand(refresh=True, json_mode=True),
                    config={"codex": {"binary": "codex-test"}},
                    config_source="test-config",
                    workspace=workspace,
                    stdout=io.StringIO(),
                )

            self.assertEqual(caught.exception.error, "no_harnesses_installed")
            self.assertIn("codex", caught.exception.diagnostics["attempts"])

    def test_cached_payload_uses_selected_profile_cache_and_never_spawns(self):
        profile = profiles.ProfileResolution(name="work", source="test")
        discovery = {
            "harnesses": {
                "codex": {
                    "models": {
                        "profile-model": {
                            "reasoning": {
                                "supported": ["max"],
                                "evidence": "exact",
                            }
                        }
                    }
                }
            }
        }
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.object(
                capability_commands.harness_discovery,
                "refresh_discovery",
                side_effect=AssertionError("cached capabilities must not probe"),
            ),
        ):
            payload = capability_commands.capabilities_payload(
                {},
                "fixture",
                workspace,
                profile=profile,
                discovery=discovery,
            )
        self.assertEqual(
            payload["cachePath"],
            str(harness_discovery.discovery_cache_path("work")),
        )
        model = payload["reasoning"]["harnesses"]["codex"]["models"]["profile-model"]
        self.assertEqual(model["source"], "discovery")

    def test_legacy_workspace_fields_report_existence_and_actual_contribution(self):
        cache = {
            "schema": 1,
            "harnesses": {
                "codex": {
                    "models": {
                        "legacy-only": {"supported": ["high"]},
                        "overridden": {"supported": ["low"]},
                    }
                }
            },
        }
        discovery = {
            "harnesses": {
                "codex": {
                    "models": {
                        "overridden": {
                            "reasoning": {
                                "supported": ["max"],
                                "evidence": "exact",
                            }
                        }
                    }
                }
            }
        }
        with tempfile.TemporaryDirectory() as workspace:
            legacy_path = reasoning.write_reasoning_capability_cache(workspace, cache)
            payload = capability_commands.capabilities_payload(
                {}, "fixture", workspace, discovery=discovery
            )
            self.assertEqual(payload["legacyWorkspaceCachePath"], str(legacy_path))
            self.assertTrue(payload["legacyWorkspaceCache"])
            models = payload["reasoning"]["harnesses"]["codex"]["models"]
            self.assertEqual(models["overridden"]["source"], "discovery")
            self.assertEqual(models["legacy-only"]["source"], "cache")

            cache["harnesses"]["codex"]["models"].pop("legacy-only")
            reasoning.write_reasoning_capability_cache(workspace, cache)
            overridden_only = capability_commands.capabilities_payload(
                {}, "fixture", workspace, discovery=discovery
            )
            self.assertIn("legacyWorkspaceCachePath", overridden_only)
            self.assertNotIn("legacyWorkspaceCache", overridden_only)

    def test_refresh_partial_success_uses_unified_snapshot_and_never_writes_legacy(self):
        profile = profiles.ProfileResolution(name="work", source="test")
        snapshot = harness_discovery.empty_snapshot(profile="work")
        snapshot["harnesses"] = {
            "codex": {
                "models": {
                    "fresh": {
                        "reasoning": {
                            "supported": ["high"],
                            "evidence": "exact",
                        }
                    }
                }
            }
        }
        result = {
            "snapshot": snapshot,
            "attempts": {
                "codex": {"installed": True, "probeStatus": "ok", "warnings": []},
                "droid": {
                    "installed": False,
                    "probeStatus": "missing",
                    "warnings": ["not found"],
                },
            },
            "updatedHarnesses": ["codex"],
            "staleHarnesses": ["droid"],
            "cachePath": "/user/discovery/work.json",
        }
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.object(
                capability_commands.harness_discovery,
                "refresh_discovery",
                return_value=result,
            ) as refresh,
            mock.patch.object(
                capability_commands.reasoning,
                "write_reasoning_capability_cache",
                side_effect=AssertionError("refresh must not write the legacy cache"),
            ),
        ):
            payload = capability_commands._refresh_payload({}, workspace, profile=profile)

        refresh.assert_called_once_with({}, profile=profile)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["updatedHarnesses"], ["codex"])
        self.assertEqual(payload["staleHarnesses"], ["droid"])
        self.assertEqual(payload["cachePath"], "/user/discovery/work.json")
        fresh = payload["reasoning"]["harnesses"]["codex"]["models"]["fresh"]
        self.assertEqual(fresh["source"], "discovery")

    def test_refresh_projects_bounded_attempts_and_scrubs_reasoning_keys_and_values(self):
        profile = profiles.ProfileResolution(name="work", source="test")
        secret = "sk-proj-abc123456789012345678901234567890"
        secret_model = f"API_TOKEN_{secret}"
        selector_path = "/private/account/bin/codex-wrapper"
        snapshot = harness_discovery.empty_snapshot(profile="work")
        snapshot["harnesses"] = {
            "codex": {
                "models": {
                    secret_model: {
                        "reasoning": {
                            "supported": ["high"],
                            "evidence": secret,
                        }
                    }
                }
            }
        }
        result = {
            "snapshot": snapshot,
            "attempts": {
                "codex": {
                    "installed": True,
                    "probeStatus": "ok",
                    "selector": [selector_path],
                    "version": f"codex 1.0 {secret}",
                    "modelScope": "account",
                    "models": {secret_model: {"displayName": secret}},
                    "warnings": [f"loaded {selector_path}"],
                    "rawOutput": secret,
                }
            },
            "updatedHarnesses": ["codex"],
            "staleHarnesses": [],
            "cachePath": "/user/discovery/work.json",
        }
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.object(
                capability_commands.harness_discovery,
                "refresh_discovery",
                return_value=result,
            ),
        ):
            payload = capability_commands._refresh_payload({}, workspace, profile=profile)

        rendered = json.dumps(payload)
        attempt = payload["attempts"]["codex"]
        self.assertEqual(
            set(attempt),
            {"installed", "probeStatus", "version", "modelScope", "catalogCount"},
        )
        self.assertEqual(attempt["catalogCount"], 1)
        self.assertNotIn("selector", attempt)
        self.assertNotIn("models", attempt)
        self.assertNotIn("warnings", attempt)
        self.assertNotIn("rawOutput", attempt)
        self.assertNotIn(selector_path, rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(secret_model, rendered)
        self.assertIn("***", rendered)
        self.assertEqual(payload["cachePath"], "/user/discovery/work.json")

    def test_refresh_cache_write_oserrors_are_generic_and_path_safe(self):
        profile = profiles.ProfileResolution(name="work", source="test")
        secret_path = "/private/cache/sk-proj-abc123456789012345678901234567890.json"
        failures = (
            PermissionError(errno.EACCES, "permission denied", secret_path),
            OSError(errno.ELOOP, "registry path component is a symlink", secret_path),
            OSError(errno.ENOSPC, "no space left on device", secret_path),
        )
        for failure in failures:
            with (
                self.subTest(error=failure.errno),
                tempfile.TemporaryDirectory() as workspace,
                mock.patch.object(
                    capability_commands.harness_discovery,
                    "refresh_discovery",
                    side_effect=failure,
                ),
                self.assertRaises(capability_commands.CapabilitiesError) as caught,
            ):
                capability_commands._refresh_payload({}, workspace, profile=profile)
            self.assertEqual(caught.exception.error, "capability_refresh_failed")
            self.assertEqual(
                caught.exception.message,
                "capability refresh could not safely update the discovery cache.",
            )
            self.assertNotIn(secret_path, caught.exception.message)

    def test_refresh_installed_but_all_failed_has_distinct_error(self):
        profile = profiles.ProfileResolution(name="work", source="test")
        result = {
            "snapshot": harness_discovery.empty_snapshot(profile="work"),
            "attempts": {"codex": {"installed": True, "probeStatus": "error", "warnings": []}},
            "updatedHarnesses": [],
            "staleHarnesses": [],
            "cachePath": "/user/discovery/work.json",
        }
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.object(
                capability_commands.harness_discovery,
                "refresh_discovery",
                return_value=result,
            ),
            self.assertRaises(capability_commands.CapabilitiesError) as caught,
        ):
            capability_commands._refresh_payload({}, workspace, profile=profile)
        self.assertEqual(caught.exception.error, "capability_refresh_failed")
        self.assertEqual(
            caught.exception.diagnostics["attempts"]["codex"]["probeStatus"],
            "error",
        )


if __name__ == "__main__":
    unittest.main()
