"""Auth health recorded by `capabilities refresh`: read what a probe clearly says, else unknown."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import auth_health, capability_commands, harness_discovery, profiles
from delegate_agent import config as delegate_config
from delegate_agent import workflow_pinning as pinning

CURSOR_IN = "Logged in as someone@example.com\n"
CURSOR_OUT = "Not logged in. Run `estate-cursor login`.\n"
OMP_OK = (
    "Usage · fetched 364ms ago\n\nOpencode Go — 1 account\n"
    "  ● account 1 · plan: OpenCode Go\n"
    "      ● 5 Hour limit   ░░░  0.0% used · resets in 4h59m\n"
    "      ● Monthly limit  ███  50.0% used · resets in 21d14h\n"
)
OMP_FULL = OMP_OK.replace("50.0% used", "100.0% used")
# Two providers in one report: the first is exhausted, the second is untouched.
OMP_ONE_EXHAUSTED = (
    "Usage · fetched 1ms ago\n\n"
    "Opencode Go — 1 account\n"
    "  ● account 1 · plan: OpenCode Go\n"
    "      ● 5 Hour limit   ░░░  100.0% used · resets in 4h59m\n"
    "      ● Monthly limit  ███  50.0% used · resets in 21d14h\n"
    "  capacity: 5h → 1.00/1 account used (no quota left)\n\n"
    "Zed Plus — 1 account\n"
    "  ● account 1 · plan: Plus\n"
    "      ● 5 Hour limit   ░░░  0.0% used · resets in 4h59m\n"
    "  capacity: 5h → 0.00/1 account used (all quota left)\n"
)


class HomeCase(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        # tests/__init__.py turns the probes off suite-wide; these tests own them.
        os.environ.pop("DELEGATE_AUTH_PROBES", None)
        scripts = tempfile.TemporaryDirectory()
        self.addCleanup(scripts.cleanup)
        self.scripts = Path(scripts.name)

    def script(self, name: str, body: str, code: int = 0) -> str:
        path = self.scripts / name
        path.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}EOF\nexit {code}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)


class ClassifyTests(unittest.TestCase):
    def test_cursor_reads_signed_in_and_signed_out(self):
        self.assertEqual(auth_health.classify("cursor", 0, CURSOR_IN), "ok")
        self.assertEqual(auth_health.classify("cursor", 1, CURSOR_OUT), "logged_out")
        self.assertEqual(auth_health.classify("cursor", 0, CURSOR_OUT), "logged_out")

    def test_omp_reads_quota_windows(self):
        self.assertEqual(auth_health.classify("omp", 0, OMP_OK), "ok")
        self.assertEqual(auth_health.classify("omp", 0, OMP_FULL), "limit_reached")

    def test_omp_health_is_per_provider_and_account_not_omp_wide(self):
        lanes = auth_health.classify_lanes("omp", 0, OMP_ONE_EXHAUSTED)

        self.assertEqual(
            lanes,
            {"Opencode Go/account 1": "limit_reached", "Zed Plus/account 1": "ok"},
        )
        # One exhausted provider must not read as an OMP-wide outage.
        self.assertEqual(auth_health.classify("omp", 0, OMP_ONE_EXHAUSTED), "partial")

    def test_omp_reports_limit_reached_only_when_every_lane_is_exhausted(self):
        every = OMP_ONE_EXHAUSTED.replace("0.0% used", "100.0% used")

        self.assertEqual(auth_health.classify("omp", 0, every), "limit_reached")
        self.assertEqual(
            set(auth_health.classify_lanes("omp", 0, every).values()), {"limit_reached"}
        )

    def test_one_exhausted_account_of_several_leaves_its_siblings_healthy(self):
        text = (
            "Usage · fetched 1ms ago\n\nOpencode Go — 2 accounts\n"
            "  ● account 1 · plan: OpenCode Go\n"
            "      ● 5 Hour limit   ░░░  100.0% used · resets in 1h\n"
            "  ● account 2 · plan: OpenCode Go\n"
            "      ● 5 Hour limit   ░░░  12.5% used · resets in 1h\n"
        )

        self.assertEqual(
            auth_health.classify_lanes("omp", 0, text),
            {"Opencode Go/account 1": "limit_reached", "Opencode Go/account 2": "ok"},
        )

    def test_a_lane_the_report_does_not_describe_is_unknown_never_ok_or_limited(self):
        for text in (
            # Windows with no provider header to attribute them to.
            "5 Hour limit   ░░░  100.0% used · resets in 4h59m\n",
            # A header that could be carrying an account name is not a provider.
            "alice@example.com — 1 account\n  ● account 1\n      ● 5 Hour limit  100.0% used\n",
            # A provider with an account but no readable window.
            "Zed Plus — 1 account\n  ● account 1 · plan: Plus\n      ● limit  unavailable\n",
        ):
            with self.subTest(text=text):
                self.assertNotIn(
                    "limit_reached", auth_health.classify_lanes("omp", 0, text).values()
                )
                self.assertNotIn("ok", auth_health.classify_lanes("omp", 0, text).values())
                self.assertEqual(auth_health.classify("omp", 0, text), "unknown")

    def test_an_unrecognised_header_ends_the_previous_providers_block(self):
        text = (
            "Zed Plus — 1 account\n  ● account 1 · plan: Plus\n"
            "      ● 5 Hour limit   ░░░  0.0% used · resets in 4h59m\n"
            "alice@example.com — 1 account\n  ● account 1\n"
            "      ● 5 Hour limit   ░░░  100.0% used · resets in 1h\n"
        )

        self.assertEqual(auth_health.classify_lanes("omp", 0, text), {"Zed Plus/account 1": "ok"})

    def test_anything_unrecognised_is_unknown_never_a_failure(self):
        for engine, code, text in (
            ("cursor", 0, "all good"),
            ("cursor", 3, "boom"),
            ("cursor", 0, ""),
            ("omp", 0, "no windows here"),
            ("omp", 1, OMP_OK),
            ("omp", 0, ""),
            ("codex", 0, CURSOR_IN),
        ):
            with self.subTest(engine=engine, code=code, text=text):
                self.assertEqual(auth_health.classify(engine, code, text), "unknown")


class ProbeTests(HomeCase):
    def test_a_missing_probe_command_is_unknown_not_a_failure(self):
        record = auth_health.probe_engine(
            "cursor", ["estate-cursor", "status"], which=lambda name: None
        )

        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["reason"], "probe_not_installed")

    def test_a_probe_that_times_out_or_cannot_run_is_unknown(self):
        cases = (
            (subprocess.TimeoutExpired("x", 1), "probe_timeout"),
            (PermissionError("no"), "probe_not_runnable"),
        )
        for error, reason in cases:
            with self.subTest(reason):
                record = auth_health.probe_engine(
                    "cursor",
                    ["estate-cursor", "status"],
                    which=lambda name: "/bin/x",
                    run=mock.Mock(side_effect=error),
                )
                self.assertEqual((record["status"], record["reason"]), ("unknown", reason))

    def test_a_real_probe_process_is_read_and_its_output_is_not_stored(self):
        argv = [self.script("estate-cursor", CURSOR_IN), "status"]

        record = auth_health.probe_engine("cursor", argv)

        self.assertEqual(record["status"], "ok")
        self.assertNotIn("someone@example.com", json.dumps(record))

    def test_an_omp_probe_records_each_lane_and_stores_no_account_names(self):
        text = OMP_ONE_EXHAUSTED.replace("plan: Plus", "plan: Plus · alice@example.com")
        record = auth_health.probe_engine("omp", [self.script("estate-omp", text), "usage"])

        self.assertEqual(record["status"], "partial")
        self.assertEqual(
            record["lanes"],
            {"Opencode Go/account 1": "limit_reached", "Zed Plus/account 1": "ok"},
        )
        self.assertNotIn("alice@example.com", json.dumps(record))
        self.assertIn("Opencode Go/account 1", auth_health.describe(record))

    def test_a_signed_out_probe_that_exits_nonzero_is_logged_out(self):
        argv = [self.script("estate-cursor", CURSOR_OUT, code=1), "status"]

        self.assertEqual(auth_health.probe_engine("cursor", argv)["status"], "logged_out")


class ConfigTests(unittest.TestCase):
    def test_no_configuration_means_no_probes_so_a_bare_config_runs_nothing(self):
        self.assertEqual(auth_health.probes_from_config({}), {})
        self.assertEqual(auth_health.probes_from_config({"providerErrors": {}}), {})

    def test_the_embedded_default_names_the_two_existing_probes(self):
        probes = auth_health.probes_from_config(delegate_config.embedded_default_config())

        self.assertEqual(
            probes,
            {"cursor": ["estate-cursor", "status"], "omp": ["estate-omp", "usage"]},
        )

    def test_null_disables_one_probe_and_junk_entries_are_ignored(self):
        config = {
            "providerErrors": {
                "authProbes": {"cursor": None, "omp": ["x", "y"], "codex": ["a"], "bad": 1}
            }
        }

        self.assertEqual(auth_health.probes_from_config(config), {"omp": ["x", "y"]})

    def test_validation_accepts_the_default_and_rejects_malformed_probes(self):
        delegate_config.validate_config(delegate_config.embedded_default_config())
        for bad in ({"codex": ["a"]}, {"cursor": []}, {"cursor": "x"}, {"cursor": [1]}, ["a"]):
            with self.subTest(bad=bad):
                config = delegate_config.embedded_default_config()
                config["providerErrors"]["authProbes"] = bad
                with self.assertRaises(delegate_config.ConfigError):
                    delegate_config.validate_config(config)


class RefreshTests(HomeCase):
    def config(self, **probes):
        return {"providerErrors": {"authProbes": probes}}

    def test_refresh_records_each_configured_engine_and_persists_it(self):
        config = self.config(
            cursor=[self.script("estate-cursor", CURSOR_IN), "status"],
            omp=[self.script("estate-omp", OMP_FULL), "usage"],
        )

        results = auth_health.refresh(config)

        self.assertEqual(results["cursor"]["status"], "ok")
        self.assertEqual(results["omp"]["status"], "limit_reached")
        saved = auth_health.load()
        self.assertEqual(
            {e: r["status"] for e, r in saved.items()}, {"cursor": "ok", "omp": "limit_reached"}
        )
        self.assertEqual(stat.S_IMODE(auth_health.state_path().stat().st_mode), 0o600)

    def test_a_subset_refresh_keeps_the_other_engines_last_reading(self):
        config = self.config(
            cursor=[self.script("estate-cursor", CURSOR_IN), "status"],
            omp=[self.script("estate-omp", OMP_OK), "usage"],
        )
        auth_health.refresh(config)

        auth_health.refresh(
            self.config(cursor=[self.script("estate-cursor2", CURSOR_OUT, code=1), "status"]),
            ["cursor"],
        )

        saved = auth_health.load()
        self.assertEqual(saved["cursor"]["status"], "logged_out")
        self.assertEqual(saved["omp"]["status"], "ok")

    def test_requesting_an_engine_with_no_probe_probes_nothing_and_writes_nothing(self):
        config = self.config(cursor=[self.script("estate-cursor", CURSOR_IN), "status"])

        self.assertEqual(auth_health.refresh(config, ["codex"]), {})
        self.assertFalse(auth_health.state_path().exists())

    def test_the_environment_switch_skips_every_probe(self):
        config = self.config(cursor=[self.script("estate-cursor", CURSOR_IN), "status"])
        for value in ("off", "0", "False", " no "):
            with self.subTest(value), mock.patch.dict(os.environ, {"DELEGATE_AUTH_PROBES": value}):
                self.assertEqual(auth_health.refresh(config), {})
                self.assertFalse(auth_health.state_path().exists())
        with mock.patch.dict(os.environ, {"DELEGATE_AUTH_PROBES": "on"}):
            self.assertEqual(auth_health.refresh(config)["cursor"]["status"], "ok")

    def test_an_unreadable_state_file_reads_as_empty(self):
        path = auth_health.state_path()
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        self.assertEqual(auth_health.load(), {})


class CapabilitiesRefreshTests(HomeCase):
    def refresh(self, config, *, json_mode=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        snapshot = harness_discovery.empty_snapshot()
        snapshot["harnesses"] = {"codex": {"models": {}}}
        result = {
            "snapshot": snapshot,
            "attempts": {"codex": {"installed": True, "probeStatus": "ok", "warnings": []}},
            "updatedHarnesses": ["codex"],
            "staleHarnesses": [],
            "cachePath": "/user/discovery/default.json",
        }
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.object(
                capability_commands.harness_discovery, "refresh_discovery", return_value=result
            ),
        ):
            code = capability_commands.emit(
                capability_commands.CapabilitiesCommand(refresh=True, json_mode=json_mode),
                config=config,
                config_source="test",
                workspace=workspace,
                profile=profiles.empty_profile_resolution(),
                stdout=stdout,
                stderr=stderr,
            )
        return code, stdout.getvalue()

    def test_refresh_reports_auth_health_in_json_and_text(self):
        config = {
            "providerErrors": {
                "authProbes": {
                    "cursor": [self.script("estate-cursor", CURSOR_OUT, code=1), "status"]
                }
            }
        }

        code, out = self.refresh(config)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["authHealth"]["cursor"]["status"], "logged_out")

        code, out = self.refresh(config, json_mode=False)
        self.assertEqual(code, 0)
        self.assertIn("auth health - cursor: logged_out", out)

    def test_a_missing_probe_never_fails_the_refresh(self):
        config = {"providerErrors": {"authProbes": {"cursor": ["no-such-probe-command", "status"]}}}

        code, out = self.refresh(config)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["authHealth"]["cursor"]["status"], "unknown")

    def test_refresh_without_probe_configuration_adds_no_auth_health(self):
        code, out = self.refresh({})

        self.assertEqual(code, 0)
        self.assertNotIn("authHealth", json.loads(out))


class DoctorTests(HomeCase):
    def test_doctor_reports_the_recorded_readings(self):
        readings = {
            "cursor": {"engine": "cursor", "status": "logged_out", "probe": "estate-cursor status"}
        }

        payload = pinning.doctor(home=self.scripts, auth_health=readings)

        self.assertEqual(payload["authHealth"], readings)
        self.assertEqual(pinning.doctor(home=self.scripts)["authHealth"], {})

    def test_doctor_text_lists_each_reading(self):
        readings = {
            "cursor": {"engine": "cursor", "status": "logged_out", "probe": "estate-cursor status"}
        }
        out = io.StringIO()

        pinning.emit_doctor(home=self.scripts, stdout=out, auth_health=readings)

        self.assertIn("auth health - cursor: logged_out (estate-cursor status)", out.getvalue())
