"""Known-bad lane markers: lane identity, the marker store, and the refusal text."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import types
import unittest
from unittest import mock

from delegate_agent import config as delegate_config
from delegate_agent import lane_health, provider_errors, runner
from delegate_agent.errors import EXIT_LANE_KNOWN_BAD


def record(engine, message, *, status=None, code=None):
    raw = provider_errors.raw_error(message=message, status=status, code=code, source="test")
    return provider_errors.provider_error_record(engine=engine, raw=raw, fallback_text="")


PERSISTENT = record("codex", "unexpected status 401 Unauthorized", status=401)
TRANSIENT = record(
    "codex", "stream disconnected: websocket closed by server before response.completed"
)
REQUEST_SCOPED = record("omp", "Too many images in request: 8 > 4", status=400)
UNKNOWN = record("codex", "something the table has never seen")


class HomeCase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self._home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lane = lane_health.Lane("codex", None, "gpt-5.5", "work", "auth=/x\0profile=")
        self.policy = lane_health.Policy()


class PolicyTests(unittest.TestCase):
    def test_defaults_are_fifteen_minutes_three_results_and_auto_resume_on(self):
        policy = lane_health.policy_from_config(delegate_config.embedded_default_config())

        self.assertEqual(policy.known_bad_seconds, 15 * 60)
        self.assertEqual(policy.stage_stop_after, 3)
        self.assertTrue(policy.auto_resume)
        self.assertFalse(policy.force_launch)
        self.assertTrue(policy.markers_enabled)

    def test_config_keys_override_and_zero_turns_markers_off(self):
        config = {
            "providerErrors": {"knownBadLaneMinutes": 0, "autoResume": False, "stageStopAfter": 5}
        }

        policy = lane_health.policy_from_config(config, force_launch=True)

        self.assertFalse(policy.markers_enabled)
        self.assertFalse(policy.auto_resume)
        self.assertEqual(policy.stage_stop_after, 5)
        self.assertTrue(policy.force_launch)

    def test_a_missing_or_malformed_section_falls_back_to_defaults(self):
        for config in ({}, {"providerErrors": "nope"}, {"providerErrors": {"autoResume": "yes"}}):
            with self.subTest(config=config):
                policy = lane_health.policy_from_config(config)
                self.assertEqual(policy.known_bad_seconds, 15 * 60)
                self.assertTrue(policy.auto_resume)

    def test_config_validation_rejects_bad_values_and_unknown_keys(self):
        for section in (
            {"knownBadLaneMinutes": -1},
            {"knownBadLaneMinutes": True},
            {"knownBadLaneMinutes": "15"},
            {"autoResume": 1},
            {"stageStopAfter": -1},
            {"stageStopAfter": 1.5},
            {"unknownKey": 1},
        ):
            with self.subTest(section=section):
                config = delegate_config.embedded_default_config()
                config["providerErrors"] = section
                with self.assertRaises(delegate_config.ConfigError) as caught:
                    delegate_config.validate_config(config)
                self.assertEqual(caught.exception.error, "invalid_provider_errors_config")

    def test_the_embedded_defaults_validate(self):
        delegate_config.validate_config(delegate_config.embedded_default_config())
        delegate_config.validate_config(delegate_config.example_config())


class LaneIdentityTests(unittest.TestCase):
    def test_the_key_separates_engine_model_and_account(self):
        base = lane_health.derive_lane(engine="codex", model="a", auth_profile="work")
        self.assertEqual(
            base.key, lane_health.derive_lane(engine="codex", model="a", auth_profile="work").key
        )
        for other in (
            lane_health.derive_lane(engine="claude", model="a", auth_profile="work"),
            lane_health.derive_lane(engine="codex", model="b", auth_profile="work"),
            lane_health.derive_lane(engine="codex", model="a", auth_profile="personal"),
            lane_health.derive_lane(
                engine="codex", model="a", auth_profile="work", codex_identity="auth=/other"
            ),
        ):
            self.assertNotEqual(base.key, other.key)

    def test_provider_prefixed_engines_carry_the_provider(self):
        lane = lane_health.derive_lane(engine="omp", model="opencode-go/kimi-k2.7")
        self.assertEqual(lane.provider, "opencode-go")
        self.assertIsNone(lane_health.derive_lane(engine="codex", model="a/b").provider)

    def test_claude_account_comes_from_its_config_dir(self):
        lane = lane_health.derive_lane(
            engine="claude", model=None, env={"CLAUDE_CONFIG_DIR": "/home/x/.claude-work/"}
        )
        self.assertEqual(lane.account, ".claude-work")
        other = lane_health.derive_lane(
            engine="claude", model=None, env={"CLAUDE_CONFIG_DIR": "/home/x/.claude-personal"}
        )
        self.assertNotEqual(lane.key, other.key)

    def test_the_public_view_never_carries_the_raw_identity(self):
        lane = lane_health.Lane("codex", None, "m", "work", "auth=/secret/path\0profile=")
        self.assertNotIn("/secret/path", json.dumps(lane.public()))


class StoreTests(HomeCase):
    def test_a_written_marker_is_read_back_until_it_expires(self):
        lane_health.write(self.lane, PERSISTENT, seconds=900, run_id="del_1", alias="codex-1")

        marker, warnings = lane_health.check(self.lane)

        self.assertEqual(warnings, [])
        assert marker is not None
        self.assertEqual(marker.signature, "auth_rejected")
        self.assertEqual(marker.status, 401)
        self.assertEqual(marker.run_id, "del_1")
        self.assertGreater(marker.seconds_left(), 890)
        later, _ = lane_health.check(self.lane, now=time.time() + 901)
        self.assertIsNone(later)
        self.assertEqual(list((lane_health.store_dir()).glob("*.json")), [])

    def test_other_lanes_are_not_affected(self):
        lane_health.write(self.lane, PERSISTENT, seconds=900)

        other = lane_health.Lane("codex", None, "another-model", "work", "auth=/x\0profile=")

        self.assertIsNone(lane_health.check(other)[0])

    def test_clear_removes_the_marker_and_reports_whether_one_existed(self):
        lane_health.write(self.lane, PERSISTENT, seconds=900)

        self.assertTrue(lane_health.clear(self.lane))
        self.assertFalse(lane_health.clear(self.lane))
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_files_are_private_and_writes_leave_no_temp_files(self):
        lane_health.write(self.lane, PERSISTENT, seconds=900)

        (path,) = lane_health.store_dir().iterdir()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(lane_health.store_dir().stat().st_mode), 0o700)
        self.assertFalse(list(lane_health.store_dir().glob("*.tmp.*")))

    def test_a_corrupt_marker_is_ignored_with_a_warning_and_removed(self):
        lane_health.write(self.lane, PERSISTENT, seconds=900)
        (path,) = lane_health.store_dir().iterdir()
        for garbage in ("{not json", "[]", '{"version": 1}', '{"version": 99, "lane": {}}', ""):
            with self.subTest(garbage=garbage):
                path.write_text(garbage, encoding="utf-8")

                marker, warnings = lane_health.check(self.lane)

                self.assertIsNone(marker)
                self.assertEqual(len(warnings), 1)
                self.assertIn("corrupt", warnings[0])
                self.assertFalse(path.exists())
                path.write_text("", encoding="utf-8")

    def test_an_unreadable_store_directory_never_raises(self):
        # A regular file where the directory belongs: reads see nothing, writes give up.
        lane_dir = lane_health.store_dir()
        lane_dir.parent.mkdir(parents=True)
        lane_dir.write_text("in the way", encoding="utf-8")

        self.assertIsNone(lane_health.check(self.lane)[0])
        self.assertIsNone(lane_health.write(self.lane, PERSISTENT, seconds=900))
        self.assertEqual(lane_health.list_live(), ([], []))

    def test_list_live_is_soonest_expiry_first_and_skips_the_expired(self):
        soon = lane_health.Lane("codex", None, "soon", None, None)
        late = lane_health.Lane("codex", None, "late", None, None)
        gone = lane_health.Lane("codex", None, "gone", None, None)
        now = time.time()
        lane_health.write(late, PERSISTENT, seconds=900, now=now)
        lane_health.write(soon, PERSISTENT, seconds=60, now=now)
        lane_health.write(gone, PERSISTENT, seconds=1, now=now - 100)

        markers, warnings = lane_health.list_live(now=now)

        self.assertEqual(warnings, [])
        self.assertEqual([marker.lane["model"] for marker in markers], ["soon", "late"])


class ObserveTests(HomeCase):
    def observe(self, *, succeeded=False, rec=None, policy=None):
        return lane_health.observe(
            self.lane, policy or self.policy, succeeded=succeeded, record=rec
        )

    def test_a_persistent_lane_failure_writes_the_marker(self):
        self.assertEqual(self.observe(rec=PERSISTENT), "written")
        marker, _ = lane_health.check(self.lane)
        assert marker is not None
        self.assertEqual(marker.signature, "auth_rejected")

    def test_a_transient_failure_never_writes(self):
        self.assertEqual(TRANSIENT["class"], "transient")
        self.assertIsNone(self.observe(rec=TRANSIENT))
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_a_request_scoped_error_never_marks_the_lane(self):
        # The audit's OMP image-count 400: persistent for that prompt, not the lane.
        self.assertEqual(REQUEST_SCOPED["class"], "persistent")
        self.assertIsNone(self.observe(rec=REQUEST_SCOPED))
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_an_unclassified_error_never_writes(self):
        self.assertIsNone(self.observe(rec=UNKNOWN))
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_no_provider_error_writes_nothing(self):
        self.assertIsNone(self.observe(rec=None))
        self.assertEqual(lane_health.list_live(), ([], []))

    def test_a_success_clears_the_marker(self):
        self.observe(rec=PERSISTENT)

        self.assertEqual(self.observe(succeeded=True), "cleared")
        self.assertIsNone(lane_health.check(self.lane)[0])
        self.assertIsNone(self.observe(succeeded=True))

    def test_a_transient_failure_leaves_an_existing_marker_alone(self):
        self.observe(rec=PERSISTENT)

        self.observe(rec=TRANSIENT)

        self.assertIsNotNone(lane_health.check(self.lane)[0])

    def test_zero_minutes_turns_marker_writes_off(self):
        off = lane_health.policy_from_config({"providerErrors": {"knownBadLaneMinutes": 0}})

        self.assertIsNone(self.observe(rec=PERSISTENT, policy=off))
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_no_lane_is_a_no_op(self):
        self.assertIsNone(
            lane_health.observe(None, self.policy, succeeded=False, record=PERSISTENT)
        )


USAGE_LIMIT = record("codex", "You've hit your usage limit", status=429)


class RunnerObservationTests(HomeCase):
    """`runner._observe_lane_health`: which finished runs are allowed to speak for the lane."""

    def observe(self, *, status="failed", rec=PERSISTENT, established=False, fallback=None):
        ctx = types.SimpleNamespace(
            lane=self.lane,
            provider_policy=self.policy,
            engine="codex",
            fallback_env_overrides=fallback or {},
            run_id="del_run",
            alias="codex-1",
        )
        extra: dict = {}
        runner._observe_lane_health(
            ctx,
            status=status,
            provider_error=rec,
            delegate_established=established,
            merged_extra=extra,
        )
        return extra

    def test_a_provider_reported_persistent_failure_marks_the_lane(self):
        extra = self.observe()

        self.assertIn("laneMarked", extra)
        self.assertIsNotNone(lane_health.check(self.lane)[0])

    def test_a_failure_delegate_established_says_nothing_about_the_lane(self):
        # A timeout, stall, or output cap that also saw a stale provider error must not
        # mark the lane; the control above proves the same record does mark it.
        extra = self.observe(established=True)

        self.assertEqual(extra, {})
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_a_cancelled_run_says_nothing_about_the_lane(self):
        self.assertEqual(self.observe(status="cancelled"), {})
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_a_codex_usage_limit_with_a_fallback_profile_stays_with_the_failover_store(self):
        self.assertEqual(provider_errors.reason_for_record(USAGE_LIMIT), "usage_limit")
        self.assertTrue(lane_health.earns_marker(USAGE_LIMIT))

        extra = self.observe(rec=USAGE_LIMIT, fallback={"CODEX_HOME": "/somewhere"})

        self.assertEqual(extra, {})
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_a_codex_usage_limit_without_a_fallback_profile_marks_the_lane(self):
        self.assertIn("laneMarked", self.observe(rec=USAGE_LIMIT))

    def test_other_persistent_failures_still_mark_a_lane_that_has_a_fallback_profile(self):
        self.assertIn("laneMarked", self.observe(fallback={"CODEX_HOME": "/somewhere"}))


class RefusalTests(unittest.TestCase):
    def test_the_refusal_names_the_signature_hint_expiry_and_the_override(self):
        lane = lane_health.derive_lane(engine="codex", model="gpt-5.5", auth_profile="work")
        now = 1_700_000_000.0
        marker = lane_health.Marker(
            lane=lane.public(),
            signature="auth_rejected",
            klass="persistent",
            status=401,
            provider_code=None,
            message="Missing bearer authentication",
            hint="Re-authenticate the codex account.",
            written_at=now,
            expires_at=now + 600,
            run_id="del_9",
        )

        error = lane_health.refusal(marker, lane, now=now + 30)

        self.assertEqual(error.error, "lane_known_bad")
        self.assertEqual(error.exit_code, EXIT_LANE_KNOWN_BAD)
        self.assertEqual(EXIT_LANE_KNOWN_BAD, 4)
        for needle in (
            "codex gpt-5.5",
            "auth_rejected",
            "HTTP 401",
            "9m30s",
            "Re-authenticate the codex account.",
            "--force-launch",
            "No child process was started",
        ):
            self.assertIn(needle, error.message)
        assert error.diagnostics is not None
        self.assertEqual(error.diagnostics["signature"], "auth_rejected")
        self.assertEqual(error.diagnostics["secondsLeft"], 570)
        self.assertIn("expiresAt", error.diagnostics)
        self.assertTrue(error.next_actions)


if __name__ == "__main__":
    unittest.main()
