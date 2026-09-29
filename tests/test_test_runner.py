"""The shared test entry point bounds workers and refuses overlapping runs."""

import contextlib
import fcntl
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "delegate_test_runner", Path(__file__).resolve().parents[1] / "scripts/test.py"
)
assert SPEC is not None and SPEC.loader is not None
test_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(test_runner)


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lock = Path(self.temp.name) / "tests.lock"
        self.enterContext(mock.patch.object(test_runner, "lock_path", return_value=self.lock))
        self.enterContext(mock.patch.object(test_runner, "contain"))
        self.command = self.enterContext(mock.patch.object(test_runner, "run", return_value=0))

    def main(self, *argv):
        with (
            mock.patch.object(test_runner.sys, "argv", ["test.py", *argv]),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return test_runner.main()

    def test_focused_run_preserves_selection_and_explicit_worker_limit(self):
        self.assertEqual(
            self.main("--workers", "2", "--scheduler", "load", "--", "tests/test_config.py"), 0
        )
        argv = self.command.call_args.args[0]
        self.assertEqual(argv[argv.index("-n") + 1], "2")
        self.assertEqual(argv[argv.index("--dist") + 1], "load")
        self.assertEqual(argv[-1], "tests/test_config.py")

    def test_worker_and_cpu_limits_cannot_be_bypassed_through_pytest_arguments(self):
        for argv in (
            ("--workers", "auto"),
            ("--workers", "9"),
            ("--cpu-limit", "0"),
            ("--", "-n", "auto"),
            ("--", "--numprocesses=20"),
            ("--", "--dist=each"),
            ("--", "--tx=4*popen"),
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as caught:
                self.main(*argv)
            self.assertEqual(caught.exception.code, 2)
        self.command.assert_not_called()

    def test_an_existing_repository_lock_refuses_to_start_tests(self):
        with self.lock.open("a") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.main(), 75)
        self.command.assert_not_called()

    def test_failed_tests_return_their_exit_code_and_release_the_lock(self):
        self.command.side_effect = [1, 0]
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.main(), 0)
