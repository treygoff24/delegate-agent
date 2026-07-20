from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.discovery_fakes import (
    FIXTURES,
    materialize_minimum_harnesses,
    write_executable,
    write_version_harness,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _harness_record() -> dict[str, object]:
    return {
        "installed": True,
        "selector": ["codex"],
        "version": "codex-cli 1.0.0",
        "probeStatus": "ok",
        "modelScope": "account",
        "defaultModel": "gpt-test",
        "models": {
            "gpt-test": {
                "displayName": "GPT Test",
                "reasoning": {
                    "supported": ["low", "high"],
                    "default": "low",
                    "evidence": "exact",
                },
            }
        },
        "harnessReasoning": None,
        "warnings": [],
    }


class SnapshotSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent.harness_discovery import empty_snapshot, validate_snapshot

        self.empty_snapshot = empty_snapshot
        self.validate_snapshot = validate_snapshot
        self.snapshot = empty_snapshot(captured_at="2026-07-20T21:00:00Z")
        self.snapshot["harnesses"] = {"codex": _harness_record()}

    def test_empty_snapshot_is_valid(self):
        self.validate_snapshot(self.empty_snapshot(), expected_profile="default")

    def test_rejects_bad_selector_status_scope_and_evidence(self):
        mutations = (
            ("selector", "codex"),
            ("probeStatus", "broken"),
            ("modelScope", "private"),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["harnesses"]["codex"][key] = value
                with self.assertRaises(ValueError):
                    self.validate_snapshot(snapshot)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["harnesses"]["codex"]["models"]["gpt-test"]["reasoning"]["evidence"] = "guess"
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)

    def test_rejects_non_string_effort_and_unsupported_default(self):
        for supported, default in ((["low", 7], "low"), (["low"], "high")):
            with self.subTest(supported=supported, default=default):
                snapshot = copy.deepcopy(self.snapshot)
                reasoning = snapshot["harnesses"]["codex"]["models"]["gpt-test"]["reasoning"]
                reasoning["supported"] = supported
                reasoning["default"] = default
                with self.assertRaises(ValueError):
                    self.validate_snapshot(snapshot)

    def test_rejects_incomplete_or_non_cursor_route_pair(self):
        snapshot = copy.deepcopy(self.snapshot)
        model = snapshot["harnesses"]["codex"]["models"]["gpt-test"]
        model["routeFamily"] = "gpt-test"
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)

        snapshot = copy.deepcopy(self.snapshot)
        cursor_record = snapshot["harnesses"].pop("codex")
        cursor_record["selector"] = ["cursor-agent"]
        cursor_model = cursor_record["models"]["gpt-test"]
        cursor_model["routeFamily"] = "gpt-test"
        cursor_model["routeEffort"] = 3
        snapshot["harnesses"]["cursor"] = cursor_record
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)

    def test_accepts_complete_cursor_route_pair(self):
        snapshot = copy.deepcopy(self.snapshot)
        cursor_record = snapshot["harnesses"].pop("codex")
        cursor_record["selector"] = ["cursor-agent"]
        cursor_model = cursor_record["models"]["gpt-test"]
        cursor_model["routeFamily"] = "gpt-test"
        cursor_model["routeEffort"] = "none"
        cursor_model["reasoning"] = {
            "supported": ["none"],
            "default": "none",
            "evidence": "inferred-route",
        }
        snapshot["harnesses"]["cursor"] = cursor_record
        self.validate_snapshot(snapshot)

    def test_rejects_cursor_route_without_matching_reasoning_evidence(self):
        for evidence, supported in (("exact", ["high"]), ("inferred-route", ["low"])):
            with self.subTest(evidence=evidence, supported=supported):
                snapshot = copy.deepcopy(self.snapshot)
                cursor_record = snapshot["harnesses"].pop("codex")
                cursor_record["selector"] = ["cursor-agent"]
                cursor_model = cursor_record["models"]["gpt-test"]
                cursor_model["routeFamily"] = "gpt-test"
                cursor_model["routeEffort"] = "high"
                cursor_model["reasoning"] = {
                    "supported": supported,
                    "default": supported[0],
                    "evidence": evidence,
                }
                snapshot["harnesses"]["cursor"] = cursor_record
                with self.assertRaises(ValueError):
                    self.validate_snapshot(snapshot)

    def test_rejects_duplicate_cursor_family_effort_route(self):
        snapshot = copy.deepcopy(self.snapshot)
        cursor_record = snapshot["harnesses"].pop("codex")
        cursor_record["selector"] = ["cursor-agent"]
        route = {
            "displayName": "GPT Test High",
            "routeFamily": "gpt-test",
            "routeEffort": "high",
            "reasoning": {
                "supported": ["high"],
                "default": "high",
                "evidence": "inferred-route",
            },
        }
        cursor_record["models"] = {
            "gpt-test-high": route,
            "gpt-test-high-duplicate": copy.deepcopy(route),
        }
        snapshot["harnesses"]["cursor"] = cursor_record
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)

    def test_rejects_transport_unsafe_effort_strings(self):
        for effort in ("two words", 'bad"quote', "bad\\slash"):
            with self.subTest(effort=effort):
                snapshot = copy.deepcopy(self.snapshot)
                reasoning = snapshot["harnesses"]["codex"]["models"]["gpt-test"]["reasoning"]
                reasoning["supported"] = [effort]
                reasoning["default"] = effort
                with self.assertRaises(ValueError):
                    self.validate_snapshot(snapshot)

        snapshot = copy.deepcopy(self.snapshot)
        cursor_record = snapshot["harnesses"].pop("codex")
        cursor_record["selector"] = ["cursor-agent"]
        cursor_model = cursor_record["models"]["gpt-test"]
        cursor_model["routeFamily"] = "gpt-test"
        cursor_model["routeEffort"] = "bad effort"
        cursor_model["reasoning"] = {
            "supported": ["high"],
            "default": "high",
            "evidence": "inferred-route",
        }
        snapshot["harnesses"]["cursor"] = cursor_record
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)

    def test_rejects_profile_mismatch_and_non_object_maps(self):
        with self.assertRaises(ValueError):
            self.validate_snapshot(self.snapshot, expected_profile="work")
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["profile"] = 7
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["schema"] = True
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)
        for key in ("harnesses",):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot[key] = []
            with self.assertRaises(ValueError):
                self.validate_snapshot(snapshot)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["harnesses"]["codex"]["models"] = []
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)

    def test_read_allows_valid_unknown_harness_with_warning(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["harnesses"]["future"] = snapshot["harnesses"].pop("codex")
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)
        warnings = self.validate_snapshot(snapshot, allow_unknown_harnesses=True)
        self.assertEqual(warnings, ("ignored unknown discovery harness 'future'",))
        snapshot["harnesses"]["future"]["models"] = []
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot, allow_unknown_harnesses=True)

    def test_fixture_provenance_has_safe_complete_shape(self):
        provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        self.assertRegex(provenance["capturedAt"], r"^\d{4}-\d{2}-\d{2}$")
        entries = provenance["fixtures"]
        fixture_names = {path.name for path in FIXTURES.iterdir() if path.name != "provenance.json"}
        self.assertEqual(set(entries), fixture_names)
        for fixture_name, entry in entries.items():
            with self.subTest(fixture=fixture_name):
                self.assertTrue((FIXTURES / fixture_name).is_file())
                self.assertIsInstance(entry["command"], list)
                self.assertTrue(entry["command"])
                self.assertTrue(all(isinstance(item, str) and item for item in entry["command"]))
                self.assertIsInstance(entry["version"], str)
                self.assertTrue(entry["version"])


class ProbeRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent.harness_discovery import ProbeResult, run_metadata_probe

        self.ProbeResult = ProbeResult
        self.run_metadata_probe = run_metadata_probe

    def test_successful_prefix_execution_preserves_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = write_executable(
                Path(tmp) / "probe",
                "import os, sys\nprint(os.environ['DISCOVERY_MARKER'])\nprint(sys.argv[1])\n",
            )
            result = self.run_metadata_probe(
                [str(fake), "catalog"],
                env={**os.environ, "DISCOVERY_MARKER": "profile-env"},
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["profile-env", "catalog"])
        self.assertIsNone(result.error)

    def test_timeout_and_missing_executable_are_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleepy = write_executable(Path(tmp) / "sleepy", "import time\ntime.sleep(2)\n")
            timed_out = self.run_metadata_probe([str(sleepy)], env=os.environ, timeout_sec=1)
            missing = self.run_metadata_probe([str(Path(tmp) / "missing")], env=os.environ)
        self.assertEqual(timed_out.error, "probe_timeout")
        self.assertIsNone(timed_out.returncode)
        self.assertEqual(missing.error, "probe_missing")

    def test_nonzero_diagnostic_is_bounded_and_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = write_executable(
                Path(tmp) / "failed",
                "import sys\nsys.stderr.write('apiKey=fixture-secret')\nsys.exit(2)\n",
            )
            result = self.run_metadata_probe([str(fake)], env=os.environ)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.error, "probe_failed")
        self.assertNotIn("fixture-secret", result.stderr)
        self.assertIn("***", result.stderr)

    def test_oversized_output_is_classified_and_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = write_executable(Path(tmp) / "noisy", "print('x' * 100)\n")
            result = self.run_metadata_probe([str(fake)], env=os.environ, output_limit_bytes=16)
        self.assertEqual(result.error, "probe_output_too_large")
        self.assertEqual(len(result.stdout), 16)

    def test_child_uses_neutral_temporary_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = write_executable(Path(tmp) / "cwd", "import os\nprint(os.getcwd())\n")
            result = self.run_metadata_probe([str(fake)], env=os.environ)
        self.assertEqual(result.error, None)
        self.assertNotEqual(Path(result.stdout.strip()).resolve(), Path.cwd().resolve())

    def test_metacharacters_are_literal_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "should-not-exist"
            fake = write_executable(Path(tmp) / "argv", "import sys\nprint(sys.argv[1])\n")
            argument = f";touch {marker}"
            result = self.run_metadata_probe([str(fake), argument], env=os.environ)
            self.assertEqual(result.stdout.strip(), argument)
            self.assertFalse(marker.exists())

    def test_result_contract_is_frozen_dataclass(self):
        result = self.ProbeResult(("missing",), None, "", "", "probe_missing")
        with self.assertRaises(AttributeError):
            result.error = None  # type: ignore[misc]


class StructuredParserTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent.harness_discovery import (
            parse_codex_catalog,
            parse_kimi_catalog,
            parse_omp_catalog,
            parse_opencode_catalog,
        )

        self.parse_codex = parse_codex_catalog
        self.parse_omp = parse_omp_catalog
        self.parse_opencode = parse_opencode_catalog
        self.parse_kimi = parse_kimi_catalog

    def test_codex_reuses_reasoning_payload_contract(self):
        fragment = self.parse_codex((FIXTURES / "codex_models.json").read_text())
        model = fragment["models"]["gpt-5.6-sol"]
        self.assertEqual(model["displayName"], "GPT-5.6-Sol")
        self.assertEqual(
            model["reasoning"]["supported"],
            ["low", "medium", "high", "xhigh", "max", "ultra"],
        )
        self.assertEqual(model["reasoning"]["default"], "low")
        self.assertEqual(model["reasoning"]["evidence"], "exact")

    def test_omp_exact_thinking_and_false_vs_unknown_reasoning(self):
        payload = json.loads((FIXTURES / "omp_models.json").read_text())
        payload["models"].extend(
            [
                {"selector": "fixture/no-reasoning", "reasoning": False},
                {"selector": "fixture/unknown-reasoning", "reasoning": True},
            ]
        )
        models = self.parse_omp(json.dumps(payload))["models"]
        self.assertEqual(
            models["cerebras/gemma-4-31b"]["reasoning"]["supported"],
            ["minimal", "low", "medium", "high", "xhigh"],
        )
        self.assertEqual(
            models["fixture/no-reasoning"]["reasoning"],
            {"supported": [], "evidence": "exact"},
        )
        self.assertEqual(
            models["fixture/unknown-reasoning"]["reasoning"],
            {"supported": None, "evidence": "unknown"},
        )

    def test_opencode_raw_decode_agreement_and_partial_recovery(self):
        valid = (FIXTURES / "opencode_models_verbose.txt").read_text()
        tail = (
            "fixture/broken\n{not-json\n"
            'fixture/ok\n{"id":"ok","providerID":"fixture","name":"OK",'
            '"variants":{"high":{}}}\n'
            'wrong/selector\n{"id":"other","providerID":"wrong"}\n'
        )
        fragment = self.parse_opencode(valid + tail)
        self.assertEqual(fragment["probeStatus"], "partial")
        self.assertIn("openai/gpt-5.5", fragment["models"])
        self.assertEqual(fragment["models"]["fixture/ok"]["reasoning"]["supported"], ["high"])
        self.assertNotIn("wrong/selector", fragment["models"])
        self.assertGreaterEqual(len(fragment["warnings"]), 2)

    def test_kimi_allowlists_top_level_models_only(self):
        raw = (FIXTURES / "kimi_providers.json").read_text()
        fragment = self.parse_kimi(raw)
        serialized = json.dumps(fragment)
        self.assertEqual(fragment["modelScope"], "configured")
        self.assertEqual(
            fragment["models"]["kimi-code/k3"]["reasoning"],
            {"supported": ["max"], "evidence": "exact", "default": "max"},
        )
        self.assertNotIn("fixture-secret-sentinel", serialized)
        self.assertNotIn("providers", serialized)

        payload = json.loads(raw)
        payload["models"]["kimi-code/k3"]["defaultEffort"] = "unsupported"
        partial = self.parse_kimi(json.dumps(payload))
        self.assertEqual(partial["probeStatus"], "partial")
        self.assertNotIn("default", partial["models"]["kimi-code/k3"]["reasoning"])


class TextParserTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent.harness_discovery import (
            merge_droid_settings,
            parse_claude_efforts,
            parse_cursor_catalog,
            parse_droid_help,
            parse_grok_catalog,
            parse_pi_catalog,
        )

        self.parse_cursor = parse_cursor_catalog
        self.parse_droid = parse_droid_help
        self.merge_droid = merge_droid_settings
        self.parse_claude = parse_claude_efforts
        self.parse_grok = parse_grok_catalog
        self.parse_pi = parse_pi_catalog

    def test_cursor_routes_direct_levels_and_corroborated_high_per_family(self):
        fragment = self.parse_cursor((FIXTURES / "cursor_models.txt").read_text())
        models = fragment["models"]
        self.assertEqual(fragment["defaultModel"], "auto")
        self.assertEqual(models["cursor-grok-4.5-low"]["routeEffort"], "low")
        self.assertEqual(models["cursor-grok-4.5-medium"]["routeEffort"], "medium")
        self.assertEqual(models["cursor-grok-4.5-high"]["routeEffort"], "high")
        self.assertEqual(
            models["cursor-grok-4.5-high-fast"]["routeFamily"],
            "cursor-grok-4.5-fast",
        )
        self.assertNotIn("routeEffort", models["composer-2.5"])

        no_medium = (
            (FIXTURES / "cursor_models.txt")
            .read_text()
            .replace("cursor-grok-4.5-medium - Cursor Grok 4.5 Medium\n", "")
        )
        self.assertNotIn(
            "routeEffort", self.parse_cursor(no_medium)["models"]["cursor-grok-4.5-high"]
        )
        mislabeled_high = (
            (FIXTURES / "cursor_models.txt")
            .read_text()
            .replace(
                "cursor-grok-4.5-high - Cursor Grok 4.5\n",
                "cursor-grok-4.5-high - Something Else\n",
            )
        )
        self.assertNotIn(
            "routeEffort",
            self.parse_cursor(mislabeled_high)["models"]["cursor-grok-4.5-high"],
        )

    def test_droid_help_metadata_wins_settings_collision(self):
        fragment = self.parse_droid((FIXTURES / "droid_help.txt").read_text())
        merged = self.merge_droid(
            fragment,
            json.dumps(
                {
                    "customModels": [
                        {"id": "claude-opus-4-8", "displayName": "Wrong"},
                        {"id": "custom:settings", "displayName": "Settings Model"},
                    ]
                }
            ),
        )
        self.assertEqual(merged["defaultModel"], "claude-opus-4-8")
        self.assertEqual(merged["models"]["claude-opus-4-8"]["displayName"], "Opus 4.8")
        self.assertEqual(merged["models"]["claude-opus-4-8"]["reasoning"]["default"], "high")
        self.assertEqual(merged["models"]["auto"]["reasoning"]["supported"], [])
        self.assertIn("custom:settings", merged["models"])

        duplicate_labels = (
            "Available Models:\n"
            "  first  Same Label\n"
            "  second  Same Label\n\n"
            "Model details:\n"
            "  - Same Label: supports reasoning: Yes; supported: [high]; default: high\n"
        )
        ambiguous = self.parse_droid(duplicate_labels)
        self.assertNotIn("reasoning", ambiguous["models"]["first"])
        self.assertNotIn("reasoning", ambiguous["models"]["second"])
        self.assertEqual(ambiguous["probeStatus"], "partial")

    def test_claude_grok_and_pi_partial_capabilities(self):
        claude = self.parse_claude((FIXTURES / "claude_effort_help.txt").read_text())
        self.assertEqual(
            claude["harnessReasoning"]["supported"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        grok = self.parse_grok((FIXTURES / "grok_models.txt").read_text())
        self.assertEqual(grok["defaultModel"], "grok-4.5")
        self.assertIsNone(grok["harnessReasoning"])
        bad_default = self.parse_grok(
            (FIXTURES / "grok_models.txt")
            .read_text()
            .replace("Default model: grok-4.5", "Default model: missing-model")
        )
        self.assertEqual(bad_default["probeStatus"], "partial")
        self.assertIsNone(bad_default["defaultModel"])
        pi = self.parse_pi((FIXTURES / "pi_models.txt").read_text())
        self.assertEqual(
            pi["models"]["google/gemini-2.0-flash"]["reasoning"],
            {"supported": [], "evidence": "exact"},
        )
        self.assertEqual(pi["models"]["openai-codex/gpt-5.5"]["reasoning"]["evidence"], "harness")


class AdapterOrchestrationTests(unittest.TestCase):
    def test_fixture_harnesses_probe_without_prompting_devin(self):
        from delegate_agent.config import embedded_default_config
        from delegate_agent.harness_discovery import probe_all_harnesses, probe_harness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bin"
            materialize_minimum_harnesses(root)
            env = {"PATH": str(root), "HOME": str(Path(tmp) / "home")}
            config = embedded_default_config()
            for harness in ("codex", "omp", "opencode", "kimi", "cursor", "claude", "grok", "pi"):
                with self.subTest(harness=harness):
                    record = probe_harness(config, harness, env=env)
                    self.assertTrue(record["installed"])
                    self.assertIn(record["probeStatus"], {"ok", "partial"})
            devin = probe_harness(config, "devin", env=env)
            self.assertEqual(devin["probeStatus"], "partial")
            self.assertEqual(devin["models"], {})

            all_missing = probe_all_harnesses(config, env={"PATH": "", "HOME": env["HOME"]})
            self.assertEqual(len(all_missing), 10)
            self.assertTrue(
                all(record["probeStatus"] == "missing" for record in all_missing.values())
            )


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent.config import embedded_default_config
        from delegate_agent.harness_discovery import (
            resolve_harness_selector,
            selector_candidates,
        )

        self.embedded_default_config = embedded_default_config
        self.resolve_harness_selector = resolve_harness_selector
        self.selector_candidates = selector_candidates

    def test_embedded_agent_collision_falls_through_to_cursor_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version_harness(root / "agent", "grok 0.2.101 (deadbeef) [alpha]")
            cursor = write_version_harness(root / "cursor-agent", "2026.07.17-3e2a980")
            env = {**os.environ, "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}"}
            result = self.resolve_harness_selector(
                self.embedded_default_config(), "cursor", env=env
            )
        self.assertEqual(result.selector, (str(cursor),))
        self.assertIsNone(result.error)
        self.assertEqual(len(result.warnings), 1)

    def test_explicit_wrong_fingerprint_errors_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong = write_version_harness(root / "wrong", "grok 0.2.101 (deadbeef) [alpha]")
            write_version_harness(root / "cursor-agent", "2026.07.17-3e2a980")
            config = self.embedded_default_config()
            config["cursor"]["argvPrefix"] = [str(wrong)]
            env = {**os.environ, "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}"}
            result = self.resolve_harness_selector(config, "cursor", env=env)
        self.assertEqual(result.error, "fingerprint_mismatch")
        self.assertIsNone(result.selector)

    def test_candidates_preserve_configured_argv_prefix(self):
        config = self.embedded_default_config()
        config["cursor"]["argvPrefix"] = ["wrapper", "cursor"]
        candidates = self.selector_candidates(config, "cursor")
        self.assertEqual(candidates[0], (("wrapper", "cursor"), True))

    def test_ambiguous_agent_rejects_generic_semver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version_harness(root / "agent", "1.2.3")
            result = self.resolve_harness_selector(
                self.embedded_default_config(), "cursor", env={"PATH": str(root)}
            )
        self.assertEqual(result.error, "probe_missing")
        self.assertIsNone(result.selector)
        self.assertEqual(len(result.warnings), 1)

    def test_standard_harness_accepts_only_canonical_version_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = write_executable(
                root / "codex",
                "import sys\n"
                "print('codex-cli 0.144.6')\n"
                "print('CODEX_HOME=/private/account API_KEY=fixture-secret', file=sys.stderr)\n",
            )
            result = self.resolve_harness_selector(
                self.embedded_default_config(), "codex", env={"PATH": str(root)}
            )
        self.assertEqual(result.selector, (str(fake),))
        self.assertEqual(result.version, "codex-cli 0.144.6")
        self.assertNotIn("private", result.version)
        self.assertNotIn("fixture-secret", result.version)

    def test_unambiguous_standard_harness_accepts_generic_semver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = write_version_harness(root / "droid", "0.175.0")
            result = self.resolve_harness_selector(
                self.embedded_default_config(), "droid", env={"PATH": str(root)}
            )
        self.assertEqual(result.selector, (str(fake),))
        self.assertEqual(result.version, "0.175.0")


class DiscoveryFakesTests(unittest.TestCase):
    def test_helper_and_cli_materialize_fixture_backed_harnesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            direct = Path(tmp) / "direct"
            harnesses = materialize_minimum_harnesses(direct)
            self.assertIn("codex", harnesses)
            codex = subprocess.run(
                [str(harnesses["codex"]), "debug", "models"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(codex.returncode, 0)
            self.assertEqual(
                json.loads(codex.stdout),
                json.loads((FIXTURES / "codex_models.json").read_text(encoding="utf-8")),
            )

            cli_target = Path(tmp) / "cli"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "discovery_fakes.py"), str(cli_target)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((cli_target / "cursor-agent").is_file())
            self.assertTrue(os.access(cli_target / "cursor-agent", os.X_OK))


if __name__ == "__main__":
    unittest.main()
