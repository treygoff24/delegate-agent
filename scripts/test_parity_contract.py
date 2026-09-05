"""Explicit parser/runner contract tests; never runs the application's full suites.

Run with python3 scripts/test_parity_contract.py.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ParityScriptTests(unittest.TestCase):
    def run_parity(self, unit, pytest, *, unit_exit=0, pytest_exit=0):
        with tempfile.TemporaryDirectory(prefix="delegate parity ") as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "bin").mkdir()
            (root / "tmp").mkdir()
            shutil.copyfile(
                Path(__file__).with_name("test-parity.sh"), root / "scripts/test-parity.sh"
            )
            runner = (
                f"#!{sys.executable}\n"
                + """import json,os,sys
from pathlib import Path
kind='unit' if Path(sys.argv[0]).name=='python3' else 'pytest'
fixture=json.loads(os.environ['PARITY_FIXTURE'])
with open(os.environ['PARITY_TRACE'],'a') as trace: trace.write(kind+'\\n')
sys.stdout.write(fixture[kind]['output'])
raise SystemExit(fixture[kind]['exit'])
"""
            )
            for name in ("python3", "uv"):
                path = root / "bin" / name
                path.write_text(runner)
                path.chmod(0o700)
            result = subprocess.run(
                ["bash", str(root / "scripts/test-parity.sh")],
                env={
                    **os.environ,
                    "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                    "TMPDIR": str(root / "tmp"),
                    "PARITY_TRACE": str(root / "trace"),
                    "PARITY_FIXTURE": json.dumps(
                        {
                            "unit": {"output": unit, "exit": unit_exit},
                            "pytest": {"output": pytest, "exit": pytest_exit},
                        }
                    ),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            trace = (root / "trace").read_text().splitlines()
            logs = {path.name: path.read_text() for path in (root / "tmp").rglob("*.log")}
            return result, trace, logs

    def test_late_stdout_diagnostics_do_not_hide_runner_summaries(self):
        unit = "early output\nRan 2801 tests in 359.076s\n\nOK (skipped=15)\nreplace_churn_filesystem=fixture reads=20000\n"
        pytest = "progress\n2786 passed, 15 skipped, 30 subtests passed in 5.00s\nlate cleanup diagnostic\n"
        result, trace, logs = self.run_parity(unit, pytest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("parity ok", result.stdout)
        self.assertEqual(trace, ["unit", "pytest"])
        self.assertEqual(logs["unittest.log"], unit)
        self.assertEqual(logs["pytest.log"], pytest)

    def test_zero_skips_singular_test_and_colored_summary(self):
        result, _, _ = self.run_parity(
            "Ran 1 test in 0.01s\n\nOK\n", "\x1b[32m1 passed\x1b[0m in 0.01s\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped=0", result.stdout)

    def test_all_skipped_has_zero_passed(self):
        result, _, _ = self.run_parity(
            "Ran 3 tests in 0.01s\n\nOK (skipped=3)\n", "3 skipped in 0.01s\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed=0 skipped=3 total=3", result.stdout)

    def test_nonzero_runner_exits_are_preserved_even_with_success_text(self):
        for unit_exit, pytest_exit, expected_trace in (
            (7, 0, ["unit"]),
            (0, 9, ["unit", "pytest"]),
        ):
            with self.subTest(unit_exit=unit_exit, pytest_exit=pytest_exit):
                result, trace, logs = self.run_parity(
                    "Ran 2 tests in 0.01s\nOK\n",
                    "2 passed in 0.01s\n",
                    unit_exit=unit_exit,
                    pytest_exit=pytest_exit,
                )
                self.assertEqual(result.returncode, unit_exit or pytest_exit)
                self.assertEqual(trace, expected_trace)
                self.assertNotIn("parity ok", result.stdout)
                self.assertIn("exit", result.stderr)
                self.assertIn("unittest.log", logs)

    def test_missing_or_failed_summaries_fail_clearly(self):
        for unit, pytest in (
            ("no unittest summary\n", "2 passed in 0.01s\n"),
            ("Ran 2 tests in 0.01s\nFAILED (failures=1)\n", "2 passed in 0.01s\n"),
            ("Ran 2 tests in 0.01s\nOK\n", "no pytest summary\n"),
            ("Ran 2 tests in 0.01s\nOK\n", "1 failed, 1 passed in 0.01s\n"),
        ):
            with self.subTest(unit=unit, pytest=pytest):
                result, _, _ = self.run_parity(unit, pytest)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("parity ok", result.stdout)
                self.assertTrue(result.stderr.strip())

    def test_count_drift_remains_a_failure(self):
        result, _, _ = self.run_parity("Ran 2 tests in 0.01s\nOK\n", "3 passed in 0.01s\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PARITY DRIFT", result.stderr)


if __name__ == "__main__":
    unittest.main()
