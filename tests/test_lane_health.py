"""Known-bad lane markers: lane identity, the marker store, and the refusal text."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import stat
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
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


SECRET_A = "sk-live-AAAA1111secretvalue"
SECRET_B = "sk-live-BBBB2222secretvalue"


class LaneCredentialTests(HomeCase):
    """Two launches on one model with different credentials are different lanes."""

    def derive(self, *, env=None, process=None, **launch):
        launch = {"engine": "omp", "model": "openai/gpt-5", **launch}
        # A clean process environment: what the launch inherits is part of the answer.
        with mock.patch.dict(
            os.environ, {"HOME": os.environ["HOME"], **(process or {})}, clear=True
        ):
            return lane_health.derive_lane(env=env, **launch)

    def test_a_different_api_key_override_is_a_different_lane(self):
        a = self.derive(env={"OPENAI_API_KEY": SECRET_A})
        b = self.derive(env={"OPENAI_API_KEY": SECRET_B})
        again = self.derive(env={"OPENAI_API_KEY": SECRET_A})

        self.assertNotEqual(a.key, b.key)
        self.assertEqual(a.key, again.key)

    def test_a_different_inherited_api_key_is_a_different_lane(self):
        a = self.derive(process={"OPENAI_API_KEY": SECRET_A})
        b = self.derive(process={"OPENAI_API_KEY": SECRET_B})

        self.assertNotEqual(a.key, b.key)
        self.assertEqual(a.key, self.derive(process={"OPENAI_API_KEY": SECRET_A}).key)
        self.assertNotEqual(a.key, self.derive().key, "no key at all is not the same lane")

    def test_an_api_key_inside_the_opencode_config_is_part_of_the_lane(self):
        def config(key, prompt="persona one", base_url="https://api.example/v1"):
            return json.dumps(
                {
                    "provider": {"openai": {"options": {"apiKey": key, "baseURL": base_url}}},
                    "agent": {"delegate": {"prompt": prompt}},
                    "permission": {"edit": "allow" if prompt == "persona one" else "deny"},
                }
            )

        def derive(content):
            return self.derive(engine="opencode", env={"OPENCODE_CONFIG_CONTENT": content})

        a = derive(config(SECRET_A))
        self.assertNotEqual(a.key, derive(config(SECRET_B)).key)
        self.assertNotEqual(a.key, derive(config(SECRET_A, base_url="https://b.example")).key)
        # Per-run persona and permission content is not an account.
        self.assertEqual(a.key, derive(config(SECRET_A, prompt="persona two")).key)
        self.assertEqual(a.key, derive(config(SECRET_A)).key)
        # An Authorization header counts too, and nothing secret reaches the label.
        header = json.dumps(
            {"provider": {"x": {"options": {"headers": {"Authorization": SECRET_A}}}}}
        )
        other = json.dumps(
            {"provider": {"x": {"options": {"headers": {"Authorization": SECRET_B}}}}}
        )
        self.assertNotEqual(derive(header).key, derive(other).key)
        self.assertNotIn(SECRET_A, json.dumps(a.public()) + a.label)

    def test_unparseable_opencode_config_names_no_account(self):
        a = self.derive(engine="opencode", env={"OPENCODE_CONFIG_CONTENT": "{not json"})
        b = self.derive(engine="opencode", env={"OPENCODE_CONFIG_CONTENT": "{also not json"})
        self.assertEqual(a.key, b.key)
        self.assertIsNone(a.credentials)

    def test_the_broker_realm_or_socket_is_part_of_the_lane(self):
        base = self.derive(env={"OPENAI_API_KEY": SECRET_A})
        for extra in (
            {"ESTATE_BROKER_REALM": "team-a"},
            {"BROKER_SOCKET": "/run/broker-a.sock"},
            {"OPENAI_BASE_URL": "https://broker-a.example/v1"},
        ):
            with self.subTest(extra):
                other = self.derive(env={"OPENAI_API_KEY": SECRET_A, **extra})
                self.assertNotEqual(base.key, other.key)

    def test_a_model_with_no_provider_prefix_counts_every_inherited_secret(self):
        a = self.derive(model="glm-5", process={"ZAI_API_KEY": SECRET_A})
        b = self.derive(model="glm-5", process={"ZAI_API_KEY": SECRET_B})

        self.assertNotEqual(a.key, b.key)

    def test_other_engines_read_their_own_credentials(self):
        for engine, name in (("claude", "ANTHROPIC_API_KEY"), ("grok", "XAI_API_KEY")):
            with self.subTest(engine):
                a = self.derive(engine=engine, model=None, env={name: SECRET_A})
                b = self.derive(engine=engine, model=None, env={name: SECRET_B})
                self.assertNotEqual(a.key, b.key)

    def test_environment_that_is_not_a_credential_does_not_split_a_lane(self):
        base = self.derive(env={"OPENAI_API_KEY": SECRET_A})
        for noise in (
            {
                "DELEGATE_RUN_ID": "del_1",
                "DELEGATE_MAIL_TOKEN": "per-run-1",
                "WORKSPACE_ROOT": "/w/1",
            },
            # Delegate's own per-mode permission JSON is not an account.
            {"OPENCODE_CONFIG_CONTENT": '{"permission":{"edit":"deny"}}', "TERM": "xterm"},
        ):
            with self.subTest(noise):
                other = self.derive(env={"OPENAI_API_KEY": SECRET_A, **noise})
                self.assertEqual(base.key, other.key)

    def test_no_secret_material_reaches_the_key_the_label_or_the_disk(self):
        lane = self.derive(
            env={"OPENAI_API_KEY": SECRET_A, "BROKER_SOCKET": "/run/broker-secret-path.sock"}
        )
        lane_health.write(lane, PERSISTENT, seconds=900)

        blobs = [lane.key, lane.label, json.dumps(lane.public())]
        home = Path(os.environ["HOME"])
        blobs += [path.read_text(errors="replace") for path in home.rglob("*") if path.is_file()]
        for secret in (SECRET_A, "broker-secret-path", "AAAA1111"):
            for blob in blobs:
                self.assertNotIn(secret, blob)

    def test_launches_that_race_to_create_the_salt_agree_on_the_winners(self):
        env = {"OPENAI_API_KEY": SECRET_A}
        real_link = os.link

        def another_launch_publishes_first(src, dst, *args, **kwargs):
            Path(dst).write_bytes(b"the-other-launchs-salt")
            return real_link(src, dst, *args, **kwargs)  # now raises FileExistsError

        with mock.patch.object(os, "link", another_launch_publishes_first):
            first = self.derive(env=env)

        self.assertEqual(first.key, self.derive(env=env).key)

    def test_the_credential_digest_is_salted_per_machine(self):
        env = {"OPENAI_API_KEY": SECRET_A}
        before = self.derive(env=env).key
        self.assertEqual(before, self.derive(env=env).key)

        for salt in Path(os.environ["HOME"]).rglob("*.salt"):
            salt.unlink()

        self.assertNotEqual(before, self.derive(env=env).key)


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

    def test_an_email_named_account_is_masked_where_it_is_shown_but_still_keys_the_lane(self):
        alice = lane_health.Lane("omp", "openai", "openai/gpt-5", "alice@example.com")
        bob = lane_health.Lane("omp", "openai", "openai/gpt-5", "bob@example.com")
        self.assertNotEqual(alice.key, bob.key)
        lane_health.write(alice, PERSISTENT, seconds=900)
        (path,) = lane_health.store_dir().iterdir()
        stored = path.read_text(encoding="utf-8")
        self.assertNotIn("alice@example.com", stored)
        self.assertIn(provider_errors.EMAIL_MASK, stored)
        self.assertNotIn("alice@example.com", alice.label)
        self.assertNotIn("alice@example.com", json.dumps(alice.public()))
        self.assertIsNotNone(lane_health.check(alice)[0])
        self.assertIsNone(lane_health.check(bob)[0])

    def test_a_success_clears_the_marker_even_when_the_store_lock_is_stuck(self):
        lane_health.write(self.lane, PERSISTENT, seconds=900)
        lock_path = lane_health.store_dir().parent / "lane-health.lock"
        holder = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        import fcntl

        fcntl.flock(holder, fcntl.LOCK_EX)
        with mock.patch.object(lane_health, "_LOCK_TIMEOUT_SECONDS", 0.05):
            self.assertEqual(
                lane_health.observe(self.lane, self.policy, succeeded=True, record=None), "cleared"
            )
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

    def test_a_marker_never_stores_an_email_address(self):
        rec = dict(PERSISTENT, message="the token for alice@example.com was rejected")

        marker = lane_health.write(self.lane, rec, seconds=900)

        assert marker is not None
        (path,) = lane_health.store_dir().iterdir()
        for blob in (marker.message, path.read_text(encoding="utf-8")):
            self.assertNotIn("alice@example.com", blob)
        self.assertIn("was rejected", marker.message)


class StoreConcurrencyTests(HomeCase):
    """One machine-wide store, many launches: writes and clears on a lane must not tear."""

    def slow_replace(self, delay=0.15):
        """Patch os.replace to widen the write window and count writes in flight."""
        real = os.replace
        counter = threading.Lock()
        seen = {"inflight": 0, "peak": 0, "sources": []}

        def replace(src, dst, *args, **kwargs):
            with counter:
                seen["inflight"] += 1
                seen["peak"] = max(seen["peak"], seen["inflight"])
                seen["sources"].append(str(src))
            time.sleep(delay)
            try:
                return real(src, dst, *args, **kwargs)
            finally:
                with counter:
                    seen["inflight"] -= 1

        patcher = mock.patch.object(os, "replace", replace)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def run_together(self, *callables):
        results = [None] * len(callables)
        errors = []
        start = threading.Barrier(len(callables))

        def runner(index, call):
            try:
                start.wait(5)
                results[index] = call()
            except BaseException as exc:  # the test reports whatever a worker raised
                errors.append(exc)

        threads = [
            threading.Thread(target=runner, args=(index, call))
            for index, call in enumerate(callables)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertFalse([thread for thread in threads if thread.is_alive()])
        return results

    def test_overlapping_atomic_writes_never_share_a_temp_file(self):
        path = lane_health.store_dir() / "one-lane.json"
        arrived = threading.Barrier(2)
        real = os.replace

        def rendezvous(src, dst, *args, **kwargs):
            # Both writers finish writing their temp file before either publishes.
            with contextlib.suppress(threading.BrokenBarrierError):
                arrived.wait(2)
            return real(src, dst, *args, **kwargs)

        with mock.patch.object(os, "replace", rendezvous):
            self.run_together(
                lambda: lane_health._write_atomic(path, {"writer": 1}),
                lambda: lane_health._write_atomic(path, {"writer": 2}),
            )

        self.assertIn(json.loads(path.read_text(encoding="utf-8"))["writer"], (1, 2))
        self.assertEqual(sorted(path.parent.glob("*.tmp.*")), [])

    def test_each_write_uses_a_temp_file_of_its_own(self):
        seen = self.slow_replace(delay=0)

        lane_health.write(self.lane, PERSISTENT, seconds=900)
        lane_health.write(self.lane, PERSISTENT, seconds=900)

        self.assertEqual(len(seen["sources"]), 2)
        self.assertEqual(len(set(seen["sources"])), 2, "a reused temp name is what tore writes")

    def test_writes_to_one_lane_take_turns(self):
        seen = self.slow_replace()

        first, second = self.run_together(
            lambda: lane_health.write(self.lane, PERSISTENT, seconds=900, run_id="a"),
            lambda: lane_health.write(self.lane, TRANSIENT, seconds=900, run_id="b"),
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(seen["peak"], 1, "two writers were inside the store at once")
        marker, warnings = lane_health.check(self.lane)
        self.assertEqual(warnings, [])
        assert marker is not None
        self.assertIn(marker.run_id, ("a", "b"))

    def test_a_clear_waits_for_a_write_in_flight_instead_of_being_undone_by_it(self):
        entered, release = threading.Event(), threading.Event()
        real = os.replace

        def held(src, dst, *args, **kwargs):
            entered.set()
            release.wait(10)
            return real(src, dst, *args, **kwargs)

        outcome: dict[str, object] = {}
        with mock.patch.object(os, "replace", held):
            writer = threading.Thread(
                target=lambda: outcome.setdefault(
                    "write", lane_health.write(self.lane, PERSISTENT, seconds=900)
                )
            )
            writer.start()
            self.assertTrue(entered.wait(5), "the write never reached the publish step")
            clearer = threading.Thread(
                target=lambda: outcome.setdefault("clear", lane_health.clear(self.lane))
            )
            clearer.start()
            clearer.join(0.4)
            still_waiting = clearer.is_alive()
            release.set()
            writer.join(10)
            clearer.join(10)

        self.assertTrue(still_waiting, "the clear ran while a write was half-published")
        self.assertTrue(outcome["clear"], "the clear should have removed the finished write")
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_a_reader_never_removes_a_marker_written_after_it_read_a_stale_one(self):
        now = time.time() + 901
        lane_health.write(self.lane, PERSISTENT, seconds=900)  # stale by `now`
        real_parse = lane_health._parse_marker
        calls = []

        def parse_then_get_replaced(raw):
            marker = real_parse(raw)
            if not calls:
                calls.append(1)
                # A writer publishes a fresh marker between the reader's read and its unlink.
                lane_health.write(self.lane, TRANSIENT, seconds=900, run_id="fresh", now=now)
            return marker

        with mock.patch.object(lane_health, "_parse_marker", parse_then_get_replaced):
            lane_health.check(self.lane, now=now)

        marker, _ = lane_health.check(self.lane, now=now)
        assert marker is not None, "the reader deleted a fresh marker"
        self.assertEqual(marker.run_id, "fresh")

    def test_a_storm_of_writes_clears_and_reads_never_tears_the_marker(self):
        self.slow_replace(delay=0.002)  # widen the publish window so an overlap shows up
        failures: list[str] = []
        writes = []

        def worker(seed):
            for step in range(25):
                turn = (seed + step) % 3
                if turn == 0:
                    writes.append(
                        lane_health.write(
                            self.lane, PERSISTENT, seconds=900, run_id=f"{seed}-{step}"
                        )
                    )
                elif turn == 1:
                    lane_health.clear(self.lane)
                else:
                    failures.extend(lane_health.check(self.lane)[1])

        self.run_together(*[lambda seed=seed: worker(seed) for seed in range(6)])

        self.assertEqual(failures, [], "a reader saw a partial or unreadable marker")
        self.assertTrue(writes)
        self.assertNotIn(None, writes, "a write gave up under contention")
        marker, warnings = lane_health.check(self.lane)
        self.assertEqual(warnings, [])
        if marker is not None:
            self.assertEqual(marker.signature, "auth_rejected")
        self.assertEqual(sorted(lane_health.store_dir().glob("*.tmp.*")), [])


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

    def observe(
        self, *, status="failed", rec=PERSISTENT, established=False, fallback=None, retried=False
    ):
        ctx = types.SimpleNamespace(
            auto_resume={"automatic": True} if retried else None,
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


class BrokerBindingMarkerTests(HomeCase):
    """One broker `binding_not_active` is launch-slot contention, not a bad lane."""

    observe = RunnerObservationTests.observe

    BROKER = record(
        "codex",
        "estate-harness: binding_not_active: Broker returned HTTP 403: binding_not_active",
        status=403,
    )

    def test_the_signature_is_the_persistent_lane_scoped_broker_row(self):
        self.assertEqual(self.BROKER["signature"], "broker_binding_inactive")
        self.assertTrue(lane_health.earns_marker(self.BROKER))

    def test_a_first_refusal_is_deferred_to_the_retry_and_marks_nothing(self):
        extra = self.observe(rec=self.BROKER)

        self.assertEqual(extra, {"laneMarkerDeferred": "broker_binding_retry"})
        self.assertIsNone(lane_health.check(self.lane)[0])

    def test_the_retry_failing_too_marks_the_lane_as_before(self):
        extra = self.observe(rec=self.BROKER, retried=True)

        self.assertIn("laneMarked", extra)
        self.assertIsNotNone(lane_health.check(self.lane)[0])

    def test_with_auto_resume_off_a_refusal_marks_immediately(self):
        self.policy = dataclasses.replace(self.policy, auto_resume=False)

        self.assertIn("laneMarked", self.observe(rec=self.BROKER))


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
