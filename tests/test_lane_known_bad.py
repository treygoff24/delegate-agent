"""End-to-end: a persistent provider failure marks the lane; the next launch refuses fast.

These run the real CLI against a scripted fake `codex` (tests/provider_error_fakes.py),
each in its own HOME, so the marker store is per test.
"""

from __future__ import annotations

import json
import time

from tests.provider_error_fakes import FakeCodexCase


class KnownBadLaneTests(FakeCodexCase):
    def fail_then_ok(self, failing_step="auth401"):
        self.set_plan(failing_step, "ok")

    def test_a_persistent_failure_is_data_and_writes_a_marker(self):
        self.fail_then_ok()

        completed, payload = self.json_cli("codex", "work", "do the thing")

        self.assertEqual(completed.returncode, 1)
        self.assertFalse(payload["ok"])
        provider_error = payload["providerError"]
        self.assertEqual(provider_error["signature"], "auth_rejected")
        self.assertEqual(provider_error["class"], "persistent")
        self.assertEqual(provider_error["status"], 401)
        self.assertEqual(provider_error["engine"], "codex")
        self.assertTrue(provider_error["hint"])
        self.assertEqual(len(self.marker_files()), 1)
        self.assertIn("laneMarked", payload)

    def test_the_next_launch_refuses_before_spawning_anything(self):
        self.fail_then_ok()
        self.json_cli("codex", "work", "first")
        self.assertEqual(self.invocation_count(), 1)

        completed, payload = self.json_cli("codex", "work", "second")

        self.assertEqual(completed.returncode, 4)
        self.assertEqual(payload["error"], "lane_known_bad")
        self.assertEqual(payload["exitCode"], 4)
        self.assertEqual(payload["signature"], "auth_rejected")
        self.assertIn("--force-launch", payload["message"])
        self.assertTrue(payload["hint"])
        self.assertIn("expiresAt", payload)
        self.assertGreater(payload["secondsLeft"], 0)
        self.assertEqual(self.invocation_count(), 1, "the refusal must not spawn the child")

    def test_the_refusal_is_readable_without_json(self):
        self.fail_then_ok()
        self.cli("codex", "work", "first")

        completed = self.cli("codex", "work", "second")

        self.assertEqual(completed.returncode, 4)
        self.assertIn("lane_known_bad", completed.stderr)
        self.assertIn("auth_rejected", completed.stderr)
        self.assertIn("--force-launch", completed.stderr)
        self.assertEqual(self.invocation_count(), 1)

    def test_force_launch_overrides_and_a_success_clears_the_marker(self):
        self.fail_then_ok()
        self.json_cli("codex", "work", "first")

        completed, payload = self.json_cli("--force-launch", "codex", "work", "second")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.invocation_count(), 2)
        self.assertEqual(self.marker_files(), [])
        self.assertTrue(any("--force-launch" in warning for warning in payload["warnings"]))
        # With the marker gone, an ordinary launch goes through again.
        third, _ = self.json_cli("codex", "work", "third")
        self.assertEqual(third.returncode, 0)

    def test_a_forced_launch_that_fails_persistently_keeps_the_lane_marked(self):
        self.set_plan("auth401")
        self.json_cli("codex", "work", "first")

        completed, _ = self.json_cli("--force-launch", "codex", "work", "second")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.invocation_count(), 2)
        _refused, payload = self.json_cli("codex", "work", "third")
        self.assertEqual(payload["error"], "lane_known_bad")
        self.assertEqual(self.invocation_count(), 2)

    def test_an_email_in_the_provider_message_is_masked_everywhere_it_is_kept(self):
        address = "alice@example.com"
        self.set_plan("email401", "ok")

        completed, payload = self.json_cli("codex", "work", "first")

        self.assertEqual(completed.returncode, 1)
        provider_error = payload["providerError"]
        self.assertEqual(provider_error["signature"], "auth_rejected")
        self.assertNotIn(address, provider_error["message"])
        self.assertIn("was rejected", provider_error["message"])
        (marker_path,) = self.marker_files()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertNotIn(address, marker["message"])
        self.assertNotIn(address, marker_path.read_text(encoding="utf-8"))
        # The places the marker is echoed back: the refusal and doctor.
        _refused, refusal = self.json_cli("codex", "work", "second")
        self.assertEqual(refusal["error"], "lane_known_bad")
        self.assertNotIn(address, json.dumps(refusal))
        doctor = self.bare_cli("--json", "doctor")
        self.assertNotIn(address, doctor.stdout)
        self.assertNotIn(address, self.bare_cli("doctor").stdout)

    def test_a_different_credential_on_the_same_model_is_not_refused(self):
        self.set_claude_plan("auth401", "ok")
        self.extra_env = {"ANTHROPIC_API_KEY": "sk-ant-test-key-AAAA1111"}
        first, first_payload = self.json_cli("claude", "work", "first")
        self.assertEqual(first.returncode, 1)
        self.assertEqual(first_payload["providerError"]["class"], "persistent")
        self.assertEqual(len(self.marker_files()), 1)

        # A healthy account: same engine, same model, another key.
        self.extra_env = {"ANTHROPIC_API_KEY": "sk-ant-test-key-BBBB2222"}
        second, second_payload = self.json_cli("claude", "work", "second")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertEqual(len(self.claude_invocations()), 2)

        # The failing account stays refused, without spawning anything.
        self.extra_env = {"ANTHROPIC_API_KEY": "sk-ant-test-key-AAAA1111"}
        third, third_payload = self.json_cli("claude", "work", "third")
        self.assertEqual(third.returncode, 4)
        self.assertEqual(third_payload["error"], "lane_known_bad")
        self.assertEqual(len(self.claude_invocations()), 2)
        for path in self.marker_files():
            self.assertNotIn("AAAA1111", path.read_text(encoding="utf-8"))
        self.assertNotIn("AAAA1111", json.dumps(third_payload))

    def test_a_transient_failure_never_marks_the_lane(self):
        self.config["providerErrors"] = {"autoResume": False}
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("codex", "work", "first")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["providerError"]["class"], "transient")
        self.assertEqual(self.marker_files(), [])
        second, _ = self.json_cli("codex", "work", "second")
        self.assertEqual(second.returncode, 0)

    def test_a_request_scoped_error_never_marks_the_lane(self):
        self.set_plan("image_limit", "ok")

        completed, payload = self.json_cli("codex", "work", "first")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["providerError"]["signature"], "request_image_limit")
        self.assertEqual(payload["providerError"]["class"], "persistent")
        self.assertEqual(self.marker_files(), [])
        second, _ = self.json_cli("codex", "work", "second")
        self.assertEqual(second.returncode, 0)

    def test_an_unclassified_failure_never_marks_the_lane(self):
        self.set_plan("novel", "ok")

        completed, payload = self.json_cli("codex", "work", "first")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["providerError"]["class"], "unknown")
        self.assertEqual(self.marker_files(), [])

    def test_another_model_is_another_lane(self):
        self.fail_then_ok()
        self.json_cli("codex", "work", "--model", "model-a", "first")

        completed, _ = self.json_cli("codex", "work", "--model", "model-b", "second")

        self.assertEqual(completed.returncode, 0)
        _refused, payload = self.json_cli("codex", "work", "--model", "model-a", "third")
        self.assertEqual(payload["error"], "lane_known_bad")

    def test_a_call_failure_marks_the_lane_for_every_mode(self):
        self.fail_then_ok()
        call = self.bare_cli("--json", "codex", "call", "hello")
        self.assertEqual(call.returncode, 1, call.stderr)
        self.assertEqual(json.loads(call.stdout)["providerError"]["signature"], "auth_rejected")
        self.assertEqual(len(self.marker_files()), 1)

        _completed, payload = self.json_cli("codex", "work", "after the call")

        self.assertEqual(payload["error"], "lane_known_bad")
        self.assertEqual(self.invocation_count(), 1)

    def test_known_bad_minutes_zero_turns_the_feature_off(self):
        self.config["providerErrors"] = {"knownBadLaneMinutes": 0}
        self.set_plan("auth401", "ok")
        self.json_cli("codex", "work", "first")
        self.assertEqual(self.marker_files(), [])

        completed, _ = self.json_cli("codex", "work", "second")

        self.assertEqual(completed.returncode, 0)

    def test_the_marker_expires_after_the_configured_minutes(self):
        self.config["providerErrors"] = {"knownBadLaneMinutes": 2}
        self.fail_then_ok()
        self.json_cli("codex", "work", "first")
        (path,) = self.marker_files()
        marker = json.loads(path.read_text())
        self.assertAlmostEqual(marker["expiresAt"] - marker["writtenAt"], 120, delta=1)

        marker["expiresAt"] = time.time() - 5
        path.write_text(json.dumps(marker), encoding="utf-8")

        completed, _ = self.json_cli("codex", "work", "second")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.marker_files(), [])

    def test_a_corrupt_marker_is_ignored_and_the_launch_proceeds(self):
        self.fail_then_ok()
        self.json_cli("codex", "work", "first")
        (path,) = self.marker_files()
        path.write_text("{ this is not json", encoding="utf-8")

        completed, payload = self.json_cli("codex", "work", "second")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(any("corrupt" in warning for warning in payload["warnings"]))

    def test_doctor_lists_the_live_markers(self):
        self.fail_then_ok()
        self.json_cli("codex", "work", "first")

        completed = self.bare_cli("--json", "doctor")
        text = self.bare_cli("doctor")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        (lane,) = json.loads(completed.stdout)["knownBadLanes"]
        self.assertEqual(lane["signature"], "auth_rejected")
        self.assertEqual(lane["lane"]["engine"], "codex")
        self.assertGreater(lane["secondsLeft"], 0)
        self.assertIn("known-bad lanes: 1", text.stdout)
        self.assertIn("auth_rejected", text.stdout)

    def test_doctor_reports_no_lanes_when_none_are_marked(self):
        completed = self.bare_cli("--json", "doctor")

        self.assertEqual(json.loads(completed.stdout)["knownBadLanes"], [])

    def test_dry_run_never_refuses(self):
        self.fail_then_ok()
        self.json_cli("codex", "work", "first")

        completed = self.cli("dry-run", "codex", "work", "plan only")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.invocation_count(), 1)

    def test_force_launch_is_refused_on_commands_that_do_not_launch(self):
        completed = self.cli("--json", "--force-launch", "runs")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("--force-launch is not supported", completed.stdout)

    def test_a_workspace_registry_is_not_where_markers_live(self):
        self.fail_then_ok()
        self.json_cli("codex", "work", "first")

        self.assertEqual(len(self.marker_files()), 1)
        self.assertEqual(list((self.workspace / ".delegate").rglob("lane-health*")), [])
