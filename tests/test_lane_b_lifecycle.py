import io
import json
import os
import tempfile
import unittest
from unittest import mock

from delegate_agent import cli, request_build, stall_watchdog
from delegate_agent import config as delegate_config
from tests.delegate_commands_test_base import make_git_repo


class SilentHarnessStallPolicyTests(unittest.TestCase):
    def _captured_request(
        self,
        engine: str,
        *,
        timeout: int | None,
        stall_minutes: float | None = None,
        flag_minutes: str | None = None,
        effort: str | None = None,
        dry_run_payload: bool = False,
    ):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        if stall_minutes is not None:
            config["stallMinutes"] = stall_minutes
        observed = {}
        args = ["--cwd", repo.name, engine, "work"]
        if timeout is not None:
            args.extend(("--timeout", str(timeout)))
        if flag_minutes is not None:
            args.extend(("--stall-minutes", flag_minutes))
        if effort is not None:
            args.extend(("--reasoning-effort", effort))
        args.append("check the workspace")

        with tempfile.TemporaryDirectory() as home_tmp:
            env = {"HOME": home_tmp, "AI_PROFILE": ""}

            def capture(request, _json_mode, **_kwargs):
                observed["request"] = request
                return 0, None

            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(request_build, "load_config", return_value=(config, "test")),
                mock.patch.object(cli, "execute_request", side_effect=capture),
            ):
                code = cli.main(args, stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(code, 0)
        return observed["request"]

    def test_finite_timeout_disables_engine_default_for_silent_harnesses(self):
        for engine in ("kimi", "devin"):
            with self.subTest(engine=engine):
                request = self._captured_request(engine, timeout=900)
                self.assertEqual(request.stall_seconds, 0.0)

    def test_default_stays_enabled_without_a_finite_timeout(self):
        for engine in ("kimi", "devin"):
            with self.subTest(engine=engine):
                request = self._captured_request(engine, timeout=None)
                self.assertEqual(
                    request.stall_seconds,
                    stall_watchdog.stall_seconds_from_minutes(stall_watchdog.STALL_MINUTES_DEFAULT),
                )

    def test_explicit_stall_minutes_is_honored_with_or_without_timeout(self):
        for engine in ("kimi", "devin"):
            for timeout in (None, 900):
                with self.subTest(engine=engine, timeout=timeout):
                    request = self._captured_request(engine, timeout=timeout, stall_minutes=2.5)
                    self.assertEqual(request.stall_seconds, 150.0)

    def test_stall_minutes_flag_pins_this_run_over_config_and_policy(self):
        for engine in ("kimi", "claude"):
            with self.subTest(engine=engine):
                request = self._captured_request(
                    engine, timeout=900, stall_minutes=2.5, flag_minutes="1.5"
                )
                self.assertEqual(request.stall_seconds, 90.0)
                self.assertTrue(request.stall_seconds_pinned)

    def test_stall_minutes_flag_alone_survives_the_silent_harness_default(self):
        for engine in ("kimi", "devin"):
            with self.subTest(engine=engine):
                request = self._captured_request(engine, timeout=900, flag_minutes="1.5")
                self.assertEqual(request.stall_seconds, 90.0)

    def test_stall_minutes_flag_zero_disables(self):
        request = self._captured_request("claude", timeout=None, flag_minutes="0")
        self.assertEqual(request.stall_seconds, 0.0)
        self.assertTrue(request.stall_seconds_pinned)

    def test_without_the_flag_the_threshold_is_not_pinned(self):
        request = self._captured_request("claude", timeout=None)
        self.assertFalse(request.stall_seconds_pinned)


class EffortAwareStallWindowTests(unittest.TestCase):
    _captured_request = SilentHarnessStallPolicyTests._captured_request

    LONG = stall_watchdog.stall_seconds_from_minutes(
        stall_watchdog.STALL_MINUTES_LONG_THINKING_DEFAULT
    )
    FLAT = stall_watchdog.stall_seconds_from_minutes(stall_watchdog.STALL_MINUTES_DEFAULT)

    def test_xhigh_and_max_get_the_longer_default_window(self):
        for engine, effort in (("codex", "xhigh"), ("claude", "max"), ("claude", "xhigh")):
            with self.subTest(engine=engine, effort=effort):
                request = self._captured_request(engine, timeout=None, effort=effort)
                self.assertEqual(request.stall_seconds, self.LONG)
                self.assertEqual(request.stall_source, "effort_default")
                self.assertFalse(request.stall_seconds_pinned)

    def test_lower_efforts_keep_the_flat_default(self):
        request = self._captured_request("codex", timeout=None, effort="high")
        self.assertEqual(request.stall_seconds, self.FLAT)
        self.assertEqual(request.stall_source, "default")

    def test_flag_and_config_beat_the_effort_default(self):
        flagged = self._captured_request("codex", timeout=None, effort="xhigh", flag_minutes="3")
        self.assertEqual((flagged.stall_seconds, flagged.stall_source), (180.0, "flag"))
        configured = self._captured_request("codex", timeout=None, effort="xhigh", stall_minutes=4)
        self.assertEqual((configured.stall_seconds, configured.stall_source), (240.0, "config"))

    def test_an_effort_suffixed_model_id_counts(self):
        self.assertTrue(stall_watchdog.is_long_thinking_effort(None, "grok-4.7-xhigh-fast"))
        self.assertFalse(stall_watchdog.is_long_thinking_effort(None, "grok-4.7-high"))
        self.assertFalse(stall_watchdog.is_long_thinking_effort("high", "grok-4.7-xhigh"))

    def test_env_override_is_reported_as_the_source(self):
        seconds, source = stall_watchdog.apply_env_override(
            self.LONG, "effort_default", pinned=False, raw_env="2"
        )
        self.assertEqual((seconds, source), (120.0, "env"))
        self.assertEqual(
            stall_watchdog.apply_env_override(self.LONG, "flag", pinned=True, raw_env="2"),
            (self.LONG, "flag"),
        )
        self.assertEqual(
            stall_watchdog.apply_env_override(self.LONG, "default", pinned=False, raw_env="junk"),
            (self.LONG, "default"),
        )

    def test_dry_run_shows_the_window_and_its_source(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        out = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as home_tmp,
            mock.patch.dict(os.environ, {"HOME": home_tmp, "AI_PROFILE": ""}),
        ):
            cli.main(
                [
                    "--json",
                    "--cwd",
                    repo.name,
                    "dry-run",
                    "codex",
                    "work",
                    "--reasoning-effort",
                    "xhigh",
                    "hi",
                ],
                stdout=out,
                stderr=io.StringIO(),
            )
        self.assertEqual(
            json.loads(out.getvalue())["stallWindow"],
            {"minutes": 20.0, "source": "effort_default"},
        )


class StallMinutesParserTests(unittest.TestCase):
    def test_invalid_values_are_refused(self):
        for value in ("-1", "abc", "inf", "nan"):
            with self.subTest(value=value):
                stderr = io.StringIO()
                stdout = io.StringIO()
                code = cli.main(
                    ["--json", "claude", "work", "--stall-minutes", value, "hi"],
                    stdout=stdout,
                    stderr=stderr,
                )
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stdout.getvalue())["error"], "invalid_stall_minutes")


if __name__ == "__main__":
    unittest.main()
