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


if __name__ == "__main__":
    unittest.main()
