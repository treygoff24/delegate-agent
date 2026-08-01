from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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

    def test_rejects_flag_like_model_selectors_and_efforts(self):
        snapshot = copy.deepcopy(self.snapshot)
        model = snapshot["harnesses"]["codex"]["models"].pop("gpt-test")
        snapshot["harnesses"]["codex"]["models"]["--unsafe"] = model
        with self.assertRaises(ValueError):
            self.validate_snapshot(snapshot)

        snapshot = copy.deepcopy(self.snapshot)
        reasoning = snapshot["harnesses"]["codex"]["models"]["gpt-test"]["reasoning"]
        reasoning["supported"] = ["--unsafe"]
        reasoning.pop("default")
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

    def test_opencode_malformed_entry_never_resyncs_inside_its_body(self):
        valid = (FIXTURES / "opencode_models_verbose.txt").read_text()
        tail = (
            "fixture/broken\n"
            "{\n"
            '  "id": "broken",\n'
            '  "providerID": "fixture",\n'
            '  "note": oops,\n'
            "nested/model\n"
            '{"id":"model","providerID":"nested","variants":{"high":{}}}\n'
            "}\n"
            'fixture/ok\n{"id":"ok","providerID":"fixture","name":"OK"}\n'
        )
        fragment = self.parse_opencode(valid + tail)
        self.assertNotIn("nested/model", fragment["models"])
        self.assertIn("fixture/ok", fragment["models"])
        self.assertEqual(fragment["probeStatus"], "partial")
        self.assertIn("ignored malformed OpenCode entry for 'fixture/broken'", fragment["warnings"])

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

    def test_cursor_colliding_route_names_drop_without_failing_the_record(self):
        from delegate_agent import harness_discovery

        raw = (
            "Available models:\n"
            "  fixture-fast-high - Fixture Fast High\n"
            "  fixture-high-fast - Fixture High Fast\n"
            "  fixture-low - Fixture Low\n"
        )
        fragment = self.parse_cursor(raw)
        models = fragment["models"]

        self.assertNotIn("routeFamily", models["fixture-fast-high"])
        self.assertNotIn("routeFamily", models["fixture-high-fast"])
        self.assertEqual(models["fixture-low"]["routeFamily"], "fixture")
        self.assertEqual(fragment["probeStatus"], "partial")
        self.assertEqual(
            fragment["warnings"],
            [
                "ignored ambiguous Cursor route fixture-fast/high claimed by "
                "fixture-fast-high, fixture-high-fast"
            ],
        )
        harness_discovery._validate_harness_record(
            "cursor",
            {
                "installed": True,
                "selector": ["cursor-agent"],
                "version": "2026.07.17-3e2a980",
                **fragment,
            },
        )

    def test_cursor_stops_before_trailing_help_sections(self):
        raw = (FIXTURES / "cursor_models.txt").read_text() + ("\nOptions:\n  --help - Show help\n")
        models = self.parse_cursor(raw)["models"]
        self.assertNotIn("--help", models)
        self.assertEqual(len(models), 8)

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

    def test_droid_stops_before_trailing_help_sections(self):
        raw = (FIXTURES / "droid_help.txt").read_text() + (
            "\nOptions:\n  --model                                  Choose a model\n"
        )
        models = self.parse_droid(raw)["models"]
        self.assertNotIn("--model", models)
        self.assertEqual(len(models), 4)

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

    def test_pi_ragged_rows_are_skipped_instead_of_column_shifted(self):
        lines = (FIXTURES / "pi_models.txt").read_text().splitlines()
        header_index = next(
            index for index, line in enumerate(lines) if line.split()[:2] == ["provider", "model"]
        )
        # Both directions of the blank-cell shift: with max-out empty the images
        # flag lands under thinking, so a thinking model reads as "no" and a
        # non-thinking model reads as "yes".
        ragged = [
            "fixture       blank-max-out-thinking      272K              yes       no",
            "fixture       blank-max-out-plain         272K              no        yes",
        ]
        raw = "\n".join(lines[: header_index + 1] + ragged + lines[header_index + 1 :]) + "\n"

        fragment = self.parse_pi(raw)
        models = fragment["models"]

        self.assertNotIn("fixture/blank-max-out-thinking", models)
        self.assertNotIn("fixture/blank-max-out-plain", models)
        self.assertEqual(
            models["google/gemini-2.0-flash"]["reasoning"],
            {"supported": [], "evidence": "exact"},
        )
        self.assertEqual(models["openai-codex/gpt-5.5"]["reasoning"]["evidence"], "harness")
        self.assertEqual(fragment["probeStatus"], "partial")
        self.assertEqual(
            fragment["warnings"], ["Pi catalog skipped 2 row(s) that did not match the header"]
        )

    def test_grok_stops_before_trailing_help_sections(self):
        raw = (FIXTURES / "grok_models.txt").read_text() + "Options:\n  --help\n"
        models = self.parse_grok(raw)["models"]
        self.assertEqual(models, {"grok-4.5": {}})


class AdapterOrchestrationTests(unittest.TestCase):
    def test_claude_only_parses_expected_nonzero_sentinel_output(self):
        from delegate_agent import harness_discovery

        valid = (FIXTURES / "claude_effort_help.txt").read_text()
        sentinel = harness_discovery.ProbeResult(("claude",), 1, "", valid, "probe_failed")
        with mock.patch.object(harness_discovery, "run_metadata_probe", return_value=sentinel):
            fragment = harness_discovery._probe_claude(("claude",), {}, None)
        self.assertEqual(
            fragment["harnessReasoning"]["supported"],
            ["low", "medium", "high", "xhigh", "max"],
        )

        for error in (
            "probe_timeout",
            "probe_output_too_large",
            "probe_missing",
            "probe_launch_failed",
        ):
            with self.subTest(error=error):
                failed = harness_discovery.ProbeResult(("claude",), None, valid, "", error)
                with (
                    mock.patch.object(harness_discovery, "run_metadata_probe", return_value=failed),
                    self.assertRaisesRegex(ValueError, error),
                ):
                    harness_discovery._probe_claude(("claude",), {}, None)

    def test_claude_persona_transport_accepts_both_help_spellings(self):
        from delegate_agent import harness_discovery

        base = (FIXTURES / "claude_effort_help.txt").read_text()
        cases = {
            "long form": (base + "\n  --append-system-prompt-file <file>\n", True),
            # claude 2.1.220 collapses the flag pair into bracket notation.
            "collapsed form": (base + "\n  --append-system-prompt[-file], --add-dir\n", True),
            "absent": (base, False),
        }
        for label, (help_text, expected) in cases.items():
            with self.subTest(label=label):
                probe = harness_discovery.ProbeResult(("claude",), 1, "", help_text, "probe_failed")
                with mock.patch.object(harness_discovery, "run_metadata_probe", return_value=probe):
                    fragment = harness_discovery._probe_claude(("claude",), {}, None)
                self.assertEqual(fragment["personaTransports"], {"native-file": expected}, label)

    def test_droid_unreadable_settings_is_not_reported_as_invalid_json(self):
        from delegate_agent import harness_discovery

        help_output = (FIXTURES / "droid_help.txt").read_text(encoding="utf-8")
        probe = harness_discovery.ProbeResult(("droid",), 0, help_output, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            # Reading a directory raises OSError without being FileNotFoundError,
            # standing in for a settings file we lack permission to read.
            unreadable = Path(tmp) / "settings.json"
            unreadable.mkdir()
            with mock.patch.object(harness_discovery, "run_metadata_probe", return_value=probe):
                fragment = harness_discovery._probe_droid(("droid",), {}, unreadable)

        self.assertEqual(fragment["probeStatus"], "partial")
        self.assertIn("Factory settings could not be read", fragment["warnings"])
        self.assertNotIn("Factory settings JSON was invalid", fragment["warnings"])

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


class DiscoveryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent import harness_discovery, profiles

        self.discovery = harness_discovery
        self.profiles = profiles

    def _snapshot(self, profile: str, harness: str = "codex") -> dict[str, object]:
        snapshot = self.discovery.empty_snapshot(
            profile=profile,
            captured_at="2026-07-20T21:00:00Z",
        )
        record = _harness_record()
        record["selector"] = [f"/{harness}"]
        snapshot["harnesses"] = {harness: record}
        return snapshot

    def test_cache_paths_are_home_scoped_and_hash_every_real_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            default = self.discovery.discovery_cache_path(None, home=home)
            literal_default = self.discovery.discovery_cache_path("default", home=home)
            traversal = self.discovery.discovery_cache_path(" ../../work ", home=home)

        root = home / ".delegate" / "cache" / "discovery"
        self.assertEqual(default, root / "default.json")
        self.assertEqual(
            literal_default,
            root / f"profile-{hashlib.sha256(b'default').hexdigest()}.json",
        )
        self.assertEqual(traversal.parent, root)
        self.assertEqual(
            traversal.name,
            f"profile-{hashlib.sha256(b'../../work').hexdigest()}.json",
        )

    def test_private_atomic_round_trip_and_malformed_read_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            snapshot = self._snapshot("work")
            path = self.discovery.write_discovery_cache("work", snapshot, home=home)
            loaded = self.discovery.load_discovery_cache("work", home=home)

            self.assertEqual(loaded, snapshot)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(self.discovery.load_discovery_cache("work", home=home))
            self.assertTrue(path.exists())

    def test_implicit_and_literal_default_profiles_use_independent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            implicit = self._snapshot("default", "codex")
            literal = self._snapshot("default", "droid")
            self.discovery.write_discovery_cache(None, implicit, home=home)
            self.discovery.write_discovery_cache("default", literal, home=home)
            self.assertEqual(self.discovery.load_discovery_cache(None, home=home), implicit)
            self.assertEqual(
                self.discovery.load_discovery_cache("default", home=home),
                literal,
            )

    def test_cache_rejects_credential_shaped_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            snapshot = self._snapshot("work")
            snapshot["harnesses"]["codex"]["models"]["gpt-test"]["apiKey"] = "fixture-secret"
            with self.assertRaises(ValueError):
                self.discovery.write_discovery_cache("work", snapshot, home=home)
            self.assertFalse(self.discovery.discovery_cache_path("work", home=home).exists())

    def test_cache_rejects_extra_fields_at_every_normalized_level(self):
        mutations = (
            lambda snapshot: snapshot.update(rawOutput="raw harness output"),
            lambda snapshot: snapshot["harnesses"]["codex"].update(authPath="/private/account"),
            lambda snapshot: snapshot["harnesses"]["codex"]["models"]["gpt-test"].update(
                provider={"name": "private"}
            ),
            lambda snapshot: snapshot["harnesses"]["codex"]["models"]["gpt-test"][
                "reasoning"
            ].update(environment="work"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for mutate in mutations:
                with self.subTest(mutate=mutate):
                    snapshot = self._snapshot("work")
                    mutate(snapshot)
                    with self.assertRaises(ValueError):
                        self.discovery.write_discovery_cache("work", snapshot, home=home)

    def test_cache_read_filters_valid_unknown_harness_but_rejects_extra_known_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            snapshot = self._snapshot("work")
            snapshot["harnesses"]["future"] = copy.deepcopy(snapshot["harnesses"]["codex"])
            path = self.discovery.discovery_cache_path("work", home=home)
            from delegate_agent import private_io

            private_io.write_json_atomic(path, snapshot)
            loaded = self.discovery.load_discovery_cache("work", home=home)
            self.assertEqual(set(loaded["harnesses"]), {"codex"})

            snapshot["harnesses"]["codex"]["rawStderr"] = "benign but raw"
            private_io.write_json_atomic(path, snapshot)
            self.assertIsNone(self.discovery.load_discovery_cache("work", home=home))

    def test_different_profiles_write_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            barrier = threading.Barrier(2)

            def write(profile: str, harness: str) -> None:
                barrier.wait()
                self.discovery.write_discovery_cache(
                    profile,
                    self._snapshot(profile, harness),
                    home=home,
                )

            threads = [
                threading.Thread(target=write, args=("work", "codex")),
                threading.Thread(target=write, args=("personal", "droid")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            work = self.discovery.load_discovery_cache("work", home=home)
            personal = self.discovery.load_discovery_cache("personal", home=home)
            self.assertEqual(set(work["harnesses"]), {"codex"})
            self.assertEqual(set(personal["harnesses"]), {"droid"})

    def test_same_profile_writes_are_whole_snapshot_last_writer_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.discovery.write_discovery_cache("work", self._snapshot("work", "codex"), home=home)
            expected = self._snapshot("work", "droid")
            self.discovery.write_discovery_cache("work", expected, home=home)
            self.assertEqual(
                self.discovery.load_discovery_cache("work", home=home),
                expected,
            )

    def test_refresh_preserves_failed_last_good_and_uses_profile_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            existing = self._snapshot("work")
            old_codex = copy.deepcopy(existing["harnesses"]["codex"])
            self.discovery.write_discovery_cache("work", existing, home=home)
            profile = self.profiles.ProfileResolution(
                name="work",
                source="test",
                env={"PATH": "/profile/bin", "API_KEY": "fixture-secret"},
            )
            seen_env: list[dict[str, str]] = []

            def probe(_config, harness, *, env, factory_settings_path=None):
                del factory_settings_path
                seen_env.append(dict(env))
                if harness == "codex":
                    record = copy.deepcopy(old_codex)
                    record.update(
                        installed=False,
                        selector=[],
                        version=None,
                        probeStatus="missing",
                    )
                    return record
                record = copy.deepcopy(old_codex)
                record["selector"] = ["/droid-new"]
                record["probeStatus"] = "partial"
                return record

            with mock.patch.object(self.discovery, "probe_harness", side_effect=probe):
                result = self.discovery.refresh_discovery(
                    {},
                    profile=profile,
                    engines=("codex", "droid"),
                    home=home,
                )

            snapshot = result["snapshot"]
            self.assertEqual(snapshot["harnesses"]["codex"], old_codex)
            self.assertEqual(snapshot["harnesses"]["droid"]["selector"], ["/droid-new"])
            self.assertEqual(result["updatedHarnesses"], ["droid"])
            self.assertEqual(result["attempts"]["codex"]["probeStatus"], "missing")
            self.assertEqual(result["staleHarnesses"], ["codex"])
            self.assertTrue(result["wrote"])
            self.assertEqual(len(seen_env), 2)
            self.assertTrue(all(item["PATH"] == "/profile/bin" for item in seen_env))
            persisted = self.discovery.load_discovery_cache("work", home=home)
            self.assertNotIn("fixture-secret", json.dumps(persisted))
            self.assertNotIn("API_KEY", json.dumps(persisted))

    def test_all_failed_refresh_does_not_rewrite_last_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            existing = self._snapshot("work")
            path = self.discovery.write_discovery_cache("work", existing, home=home)
            before = path.read_bytes()
            profile = self.profiles.ProfileResolution(name="work", source="test")
            failed = copy.deepcopy(existing["harnesses"]["codex"])
            failed.update(installed=True, probeStatus="error")

            with mock.patch.object(self.discovery, "probe_harness", return_value=failed):
                result = self.discovery.refresh_discovery(
                    {}, profile=profile, engines=("codex",), home=home
                )

            self.assertFalse(result["wrote"])
            self.assertEqual(result["snapshot"], existing)
            self.assertEqual(result["staleHarnesses"], ["codex"])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(result["snapshot"]["harnesses"]["codex"]["probeStatus"], "ok")

    def test_refresh_can_return_updates_without_persisting_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            existing = self._snapshot("work")
            path = self.discovery.write_discovery_cache("work", existing, home=home)
            before = path.read_bytes()
            profile = self.profiles.ProfileResolution(name="work", source="test")
            updated = copy.deepcopy(existing["harnesses"]["codex"])
            updated["selector"] = ["/new-codex"]

            with mock.patch.object(self.discovery, "probe_harness", return_value=updated):
                result = self.discovery.refresh_discovery(
                    {},
                    profile=profile,
                    engines=("codex",),
                    home=home,
                    persist=False,
                )

            self.assertFalse(result["wrote"])
            self.assertEqual(result["updatedHarnesses"], ["codex"])
            self.assertEqual(result["snapshot"]["harnesses"]["codex"], updated)
            self.assertEqual(path.read_bytes(), before)

    def test_selector_drift_uses_profile_path_without_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = write_version_harness(root / "codex-profile", "0.1.0")
            config = {"codex": {"binary": "codex-profile"}}
            profile = self.profiles.ProfileResolution(
                name="work", source="test", env={"PATH": str(root)}
            )
            record = _harness_record()
            record["selector"] = [str(binary)]

            with mock.patch.object(
                self.discovery,
                "run_metadata_probe",
                side_effect=AssertionError("selector drift must not spawn"),
            ):
                self.assertFalse(
                    self.discovery.selector_has_drifted(config, "codex", record, profile=profile)
                )
                record["selector"] = ["/different/codex"]
                self.assertTrue(
                    self.discovery.selector_has_drifted(config, "codex", record, profile=profile)
                )

    def test_relative_configured_selector_is_cached_absolute_and_detects_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = write_version_harness(root / "custom-codex", "codex-cli 1.0.0")
            second = write_version_harness(root / "other-codex", "codex-cli 1.0.1")
            profile = self.profiles.ProfileResolution(
                name="work", source="test", env={"PATH": str(root)}
            )
            config = {"codex": {"binary": "custom-codex"}}
            env = self.profiles.child_environment(overrides=profile.env)
            resolution = self.discovery.resolve_harness_selector(config, "codex", env=env)
            self.assertEqual(resolution.selector, (str(first),))
            record = _harness_record()
            record["selector"] = list(resolution.selector)
            self.assertFalse(
                self.discovery.selector_has_drifted(config, "codex", record, profile=profile)
            )

            config["codex"]["binary"] = second.name
            self.assertTrue(
                self.discovery.selector_has_drifted(config, "codex", record, profile=profile)
            )

    def test_cursor_cached_implicit_fallback_does_not_drift_when_agent_is_grok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version_harness(root / "agent", "grok 0.2.101 (deadbeef) [alpha]")
            cursor = write_version_harness(root / "cursor-agent", "2026.07.17-3e2a980")
            profile = self.profiles.ProfileResolution(
                name="work", source="test", env={"PATH": str(root)}
            )
            config = self.discovery.delegate_config.embedded_default_config()
            env = self.profiles.child_environment(overrides=profile.env)
            resolution = self.discovery.resolve_harness_selector(config, "cursor", env=env)
            self.assertEqual(resolution.selector, (str(cursor),))
            record = _harness_record()
            record["selector"] = list(resolution.selector)

            with mock.patch.object(
                self.discovery,
                "run_metadata_probe",
                side_effect=AssertionError("selector drift must not spawn"),
            ):
                self.assertFalse(
                    self.discovery.selector_has_drifted(config, "cursor", record, profile=profile)
                )

            explicit = copy.deepcopy(config)
            explicit["cursor"]["argvPrefix"] = ["custom-cursor"]
            self.assertTrue(
                self.discovery.selector_has_drifted(explicit, "cursor", record, profile=profile)
            )

    def test_vanished_session_shim_selector_is_drifted_without_spawning(self):
        """A cached shim path that no longer exists cannot be launched."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim_dir = root / "session-shims"
            shim_dir.mkdir()
            shim = write_version_harness(shim_dir / "codex", "codex-cli 1.0.0")
            config = {"codex": {"binary": str(shim)}}
            profile = self.profiles.ProfileResolution(
                name="work", source="test", env={"PATH": str(root)}
            )
            record = _harness_record()
            record["selector"] = [str(shim)]

            with mock.patch.object(
                self.discovery,
                "run_metadata_probe",
                side_effect=AssertionError("selector existence must not spawn"),
            ):
                self.assertFalse(
                    self.discovery.selector_has_drifted(config, "codex", record, profile=profile)
                )
                shim.rename(shim_dir / "codex.gone")
                self.assertTrue(
                    self.discovery.selector_has_drifted(config, "codex", record, profile=profile)
                )

    def test_relative_cached_selector_is_left_to_the_configured_comparison(self):
        """A bare-name selector is not a filesystem path, so it is not stat-ed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version_harness(root / "codex", "codex-cli 1.0.0")
            profile = self.profiles.ProfileResolution(
                name="work", source="test", env={"PATH": str(root)}
            )
            record = _harness_record()
            record["selector"] = ["codex"]

            with mock.patch.object(
                self.discovery,
                "run_metadata_probe",
                side_effect=AssertionError("selector existence must not spawn"),
            ):
                # Drifted because a persisted record always stores the absolute
                # path, never because a bare name failed an existence check.
                self.assertTrue(
                    self.discovery.selector_has_drifted(
                        {"codex": {"binary": "codex"}}, "codex", record, profile=profile
                    )
                )


class CachedVersionDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent import harness_discovery, profiles

        self.discovery = harness_discovery
        self.profiles = profiles

    def _profile(self, root: Path):
        return self.profiles.ProfileResolution(name="work", source="test", env={"PATH": str(root)})

    def test_in_place_upgrade_is_detected_behind_an_unchanged_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = write_version_harness(root / "codex", "codex-cli 1.0.0")
            record = _harness_record()
            record["selector"] = [str(binary)]
            record["version"] = "codex-cli 1.0.0"

            self.assertFalse(
                self.discovery.cached_version_has_drifted(
                    "codex", record, profile=self._profile(root)
                )
            )
            write_version_harness(binary, "codex-cli 2.0.0")
            self.assertTrue(
                self.discovery.cached_version_has_drifted(
                    "codex", record, profile=self._profile(root)
                )
            )

    def test_only_the_named_harness_is_probed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = write_version_harness(root / "codex", "codex-cli 1.0.0")
            record = _harness_record()
            record["selector"] = [str(binary)]
            probed: list[tuple[str, ...]] = []

            def probe(argv, *, env, **kwargs):
                del env, kwargs
                probed.append(tuple(argv))
                return self.discovery.ProbeResult(tuple(argv), 0, "codex-cli 1.0.0", "", None)

            with mock.patch.object(self.discovery, "run_metadata_probe", side_effect=probe):
                self.discovery.cached_version_has_drifted(
                    "codex", record, profile=self._profile(root)
                )
            self.assertEqual(probed, [(str(binary), "--version")])

    def test_every_probe_failure_keeps_the_cached_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = write_version_harness(root / "codex", "codex-cli 1.0.0")
            base = _harness_record()
            base["selector"] = [str(binary)]

            failures = {
                "probe_failed": self.discovery.ProbeResult((), 1, "", "boom", "probe_failed"),
                "probe_timeout": self.discovery.ProbeResult((), None, "", "", "probe_timeout"),
                "probe_missing": self.discovery.ProbeResult((), None, "", "", "probe_missing"),
                "probe_output_too_large": self.discovery.ProbeResult(
                    (), 0, "codex-cli 9.9.9", "", "probe_output_too_large"
                ),
            }
            for error, result in failures.items():
                with (
                    self.subTest(error=error),
                    mock.patch.object(self.discovery, "run_metadata_probe", return_value=result),
                ):
                    self.assertFalse(
                        self.discovery.cached_version_has_drifted(
                            "codex", copy.deepcopy(base), profile=self._profile(root)
                        )
                    )

            with mock.patch.object(
                self.discovery, "run_metadata_probe", side_effect=OSError("spawn refused")
            ):
                self.assertFalse(
                    self.discovery.cached_version_has_drifted(
                        "codex", copy.deepcopy(base), profile=self._profile(root)
                    )
                )

    def test_unidentifiable_or_unrecorded_versions_are_not_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = write_version_harness(root / "codex", "not a version banner")
            record = _harness_record()
            record["selector"] = [str(binary)]
            record["version"] = "codex-cli 1.0.0"
            self.assertFalse(
                self.discovery.cached_version_has_drifted(
                    "codex", record, profile=self._profile(root)
                )
            )

            no_version = _harness_record()
            no_version["selector"] = [str(binary)]
            no_version["version"] = None
            with mock.patch.object(
                self.discovery,
                "run_metadata_probe",
                side_effect=AssertionError("an unrecorded version must not spawn"),
            ):
                self.assertFalse(
                    self.discovery.cached_version_has_drifted(
                        "codex", no_version, profile=self._profile(root)
                    )
                )
                empty_selector = _harness_record()
                empty_selector["selector"] = []
                self.assertFalse(
                    self.discovery.cached_version_has_drifted(
                        "codex", empty_selector, profile=self._profile(root)
                    )
                )

    def test_a_selector_answering_as_another_harness_refuses_instead_of_drifting(self):
        """Contrary evidence, not uncertainty: this path no longer runs codex."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = write_version_harness(root / "codex", "grok 2.4.0")
            record = _harness_record()
            record["selector"] = [str(binary)]
            record["version"] = "codex-cli 1.0.0"
            with self.assertRaises(self.discovery.HarnessIdentityMismatchError) as caught:
                self.discovery.cached_version_has_drifted(
                    "codex", record, profile=self._profile(root)
                )
            self.assertEqual(caught.exception.harness, "codex")
            self.assertEqual(caught.exception.identified, "grok")
            self.assertEqual(caught.exception.selector, (str(binary),))

    def test_an_ansi_colored_banner_is_read_rather_than_ignored(self):
        """Escapes around a line-anchored banner must not freeze the record."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = write_version_harness(root / "codex", "\x1b[32mcodex-cli 1.0.0\x1b[0m")
            record = _harness_record()
            record["selector"] = [str(binary)]
            record["version"] = "codex-cli 1.0.0"
            self.assertFalse(
                self.discovery.cached_version_has_drifted(
                    "codex", record, profile=self._profile(root)
                )
            )

            upgraded = _harness_record()
            upgraded["selector"] = [str(binary)]
            upgraded["version"] = "codex-cli 0.9.0"
            self.assertTrue(
                self.discovery.cached_version_has_drifted(
                    "codex", upgraded, profile=self._profile(root)
                )
            )


class VersionIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent import harness_discovery

        self.discovery = harness_discovery

    def test_a_banner_is_classified_as_this_harness_another_one_or_neither(self):
        cases = (
            ("codex-cli 1.0.0", "expected", "codex-cli 1.0.0", "codex"),
            ("grok 2.4.0", "known-other", None, "grok"),
            ("some unreadable banner", "unrecognized", None, None),
            ("", "unrecognized", None, None),
        )
        for output, status, version, identified in cases:
            with self.subTest(output=output):
                identity = self.discovery._identify_version("codex", ("/bin/codex",), output)
                self.assertEqual(identity.status, status)
                self.assertEqual(identity.version, version)
                self.assertEqual(identity.identified, identified)

    def test_the_expected_harness_outranks_an_earlier_listed_one(self):
        """A banner naming two harnesses belongs to the one that was asked for."""
        banner = "codex-cli 1.0.0\ngrok 2.4.0"
        codex = self.discovery._identify_version("codex", ("/bin/codex",), banner)
        self.assertEqual((codex.status, codex.version), ("expected", "codex-cli 1.0.0"))
        grok = self.discovery._identify_version("grok", ("/bin/grok",), banner)
        self.assertEqual((grok.status, grok.version), ("expected", "grok 2.4.0"))

        # An unbranded cursor banner is the collision this ordering protects:
        # it would otherwise claim any harness whose own line is listed later.
        collision = "2026.07.25-abcdef\nomp/3.1.0"
        cursor = self.discovery._identify_version("cursor", ("/bin/cursor-agent",), collision)
        self.assertEqual((cursor.status, cursor.version), ("expected", "2026.07.25-abcdef"))
        omp = self.discovery._identify_version("omp", ("/bin/omp",), collision)
        self.assertEqual((omp.status, omp.version), ("expected", "omp/3.1.0"))

    def test_a_generic_banner_counts_only_for_a_harness_named_binary(self):
        expected = self.discovery._identify_version("droid", ("/bin/droid",), "1.2.3")
        self.assertEqual((expected.status, expected.version), ("expected", "1.2.3"))
        renamed = self.discovery._identify_version("droid", ("/bin/something-else",), "1.2.3")
        self.assertEqual((renamed.status, renamed.version), ("unrecognized", None))

    def test_unbranded_cursor_shaped_build_id_is_uncertain_for_other_harnesses(self):
        """A bare build ID names no tool, so it cannot convict a selector.

        Cursor's banner is the one canonical pattern that brands nothing, and
        wrapper shims print build IDs of exactly this shape. Reading it as proof
        that some other harness "is Cursor" refuses a launch that would have
        worked, which is the costlier error of the two.
        """
        banner = "2026.07.25-abcdef"
        for harness, selector in (
            ("codex", ("/opt/shim/codex",)),
            ("droid", ("/opt/shim/droid",)),
            ("claude", ("/opt/shim/claude",)),
        ):
            with self.subTest(harness=harness):
                identity = self.discovery._identify_version(harness, selector, banner)
                self.assertNotEqual(identity.status, "known-other")
                self.assertNotEqual(identity.identified, "cursor")

        # Under a name the expected harness never answers to, the bare-version
        # fallback cannot claim the banner either, so the verdict is the honest
        # one: nothing here is recognizable, keep the cached record and launch.
        wrapper = self.discovery._identify_version("codex", ("/opt/shim/run-codex",), banner)
        self.assertEqual((wrapper.status, wrapper.identified), ("unrecognized", None))

    def test_a_bare_version_outranks_a_foreign_banner_for_an_unpatterned_harness(self):
        """Ordering must not let a mentioned tool beat the harness asked for.

        Droid, opencode, pi and kimi have no canonical banner of their own, so
        every foreign pattern used to be tried before their bare-version
        fallback -- and an update notice naming another CLI was enough to refuse
        the launch.
        """
        banner = "1.2.3\ngrok 2.4.0 is available; run droid upgrade"
        identity = self.discovery._identify_version("droid", ("/bin/droid",), banner)
        self.assertEqual((identity.status, identity.version), ("expected", "1.2.3"))


class FutureSchemaCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent import harness_discovery, private_io, profiles

        self.discovery = harness_discovery
        self.private_io = private_io
        self.profiles = profiles

    def _write_raw(self, home: Path, schema: object) -> Path:
        snapshot = self.discovery.empty_snapshot(profile="work", captured_at="2026-07-25T10:00:00Z")
        snapshot["schema"] = schema
        path = self.discovery.discovery_cache_path("work", home=home)
        self.private_io.write_json_atomic(path, snapshot)
        return path

    def test_newer_schema_is_recognized_and_lower_or_invalid_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for schema, expected in ((2, True), (99, True), (1, False), (0, False), ("2", False)):
                with self.subTest(schema=schema):
                    self._write_raw(home, schema)
                    self.assertEqual(
                        self.discovery.cache_schema_is_future("work", home=home), expected
                    )
            self.assertFalse(self.discovery.cache_schema_is_future("absent", home=home))

    def test_write_refuses_to_replace_a_newer_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._write_raw(home, 2)
            before = path.read_bytes()
            snapshot = self.discovery.empty_snapshot(profile="work")
            with self.assertRaises(self.discovery.FutureCacheSchemaError):
                self.discovery.write_discovery_cache("work", snapshot, home=home)
            self.assertEqual(path.read_bytes(), before)

    def test_the_schema_check_runs_after_the_replacement_is_staged(self):
        """Narrow the check/replace race to the os.replace call itself."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self.discovery.discovery_cache_path("work", home=home)
            path.parent.mkdir(parents=True, exist_ok=True)
            staged: list[list[str]] = []

            def check(profile_name, *, home):
                del profile_name, home
                staged.append(sorted(entry.name for entry in path.parent.iterdir()))
                return True

            with (
                mock.patch.object(self.discovery, "cache_schema_is_future", side_effect=check),
                self.assertRaises(self.discovery.FutureCacheSchemaError),
            ):
                self.discovery.write_discovery_cache(
                    "work", self.discovery.empty_snapshot(profile="work"), home=home
                )

            # Checked exactly once, with the finished replacement already on
            # disk: everything but os.replace happens before the decision.
            self.assertEqual(len(staged), 1)
            self.assertTrue(any(name.endswith(".tmp") for name in staged[0]))
            self.assertEqual(list(path.parent.iterdir()), [])

    def test_refresh_probes_fresh_and_preserves_the_newer_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._write_raw(home, 2)
            before = path.read_bytes()
            profile = self.profiles.ProfileResolution(name="work", source="test")
            probed = copy.deepcopy(_harness_record())
            probed["selector"] = ["/codex"]

            with mock.patch.object(self.discovery, "probe_harness", return_value=probed):
                result = self.discovery.refresh_discovery(
                    {}, profile=profile, engines=("codex",), home=home
                )

            self.assertTrue(result["futureSchemaCache"])
            self.assertFalse(result["wrote"])
            self.assertEqual(result["updatedHarnesses"], ["codex"])
            self.assertEqual(result["snapshot"]["harnesses"]["codex"], probed)
            self.assertEqual(path.read_bytes(), before)

    def test_a_lower_schema_cache_is_still_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._write_raw(home, 0)
            profile = self.profiles.ProfileResolution(name="work", source="test")
            probed = copy.deepcopy(_harness_record())
            probed["selector"] = ["/codex"]

            with mock.patch.object(self.discovery, "probe_harness", return_value=probed):
                result = self.discovery.refresh_discovery(
                    {}, profile=profile, engines=("codex",), home=home
                )

            self.assertNotIn("futureSchemaCache", result)
            self.assertTrue(result["wrote"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema"], 1)

    def test_a_newer_cache_published_mid_probe_degrades_instead_of_failing(self):
        """Losing the race costs persistence only; the probe results still stand."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = self.profiles.ProfileResolution(name="work", source="test")
            probed = copy.deepcopy(_harness_record())
            probed["selector"] = ["/codex"]
            published: list[bytes] = []

            def probe_then_lose_the_race(*args, **kwargs):
                del args, kwargs
                published.append(self._write_raw(home, 2).read_bytes())
                return probed

            with mock.patch.object(
                self.discovery, "probe_harness", side_effect=probe_then_lose_the_race
            ):
                result = self.discovery.refresh_discovery(
                    {}, profile=profile, engines=("codex",), home=home
                )

            self.assertTrue(result["futureSchemaCache"])
            self.assertFalse(result["wrote"])
            self.assertEqual(result["updatedHarnesses"], ["codex"])
            self.assertEqual(result["snapshot"]["harnesses"]["codex"], probed)
            path = self.discovery.discovery_cache_path("work", home=home)
            self.assertEqual(path.read_bytes(), published[0])


class ProbeProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        from delegate_agent import harness_discovery, profiles

        self.discovery = harness_discovery
        self.profiles = profiles

    def test_each_harness_is_named_before_it_is_probed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = self.profiles.ProfileResolution(name="work", source="test")
            events: list[str] = []

            def probe(_config, harness, *, env, factory_settings_path=None):
                del env, factory_settings_path
                events.append(f"probe:{harness}")
                return copy.deepcopy(_harness_record())

            with mock.patch.object(self.discovery, "probe_harness", side_effect=probe):
                self.discovery.refresh_discovery(
                    {},
                    profile=profile,
                    engines=("codex", "droid"),
                    home=home,
                    persist=False,
                    progress=lambda harness: events.append(f"progress:{harness}"),
                )

            self.assertEqual(
                events,
                ["progress:codex", "probe:codex", "progress:droid", "probe:droid"],
            )

    def test_probe_all_harnesses_reports_every_engine_in_order(self):
        from delegate_agent.constants import KNOWN_ENGINES

        seen: list[str] = []
        with mock.patch.object(
            self.discovery, "probe_harness", return_value=copy.deepcopy(_harness_record())
        ):
            records = self.discovery.probe_all_harnesses({}, env={"PATH": ""}, progress=seen.append)
        self.assertEqual(seen, list(KNOWN_ENGINES))
        self.assertEqual(list(records), list(KNOWN_ENGINES))

    def test_stderr_callback_writes_one_named_line_per_harness(self):
        stream = io.StringIO()
        report = self.discovery.stderr_probe_progress(stream)
        report("codex")
        report("droid")
        self.assertEqual(
            stream.getvalue().splitlines(),
            ["discovery: probing codex", "discovery: probing droid"],
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

    def test_dead_configured_selector_is_distinguished_from_a_missing_harness(self):
        from delegate_agent import harness_discovery

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version_harness(root / "codex", "codex-cli 1.2.3")
            empty = root / "empty"
            empty.mkdir()
            config = self.embedded_default_config()
            config["codex"]["binary"] = str(root / "deleted-codex")

            resolution = self.resolve_harness_selector(config, "codex", env={"PATH": str(root)})
            record = harness_discovery.probe_harness(config, "codex", env={"PATH": str(root)})
            uninstalled = self.resolve_harness_selector(config, "codex", env={"PATH": str(empty)})

        self.assertEqual(resolution.error, harness_discovery.CONFIGURED_SELECTOR_MISSING)
        self.assertIsNone(resolution.selector)
        self.assertFalse(record["installed"])
        self.assertEqual(record["probeStatus"], "error")
        self.assertIn(harness_discovery.CONFIGURED_SELECTOR_MISSING, record["warnings"])
        self.assertEqual(uninstalled.error, "probe_missing")

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
