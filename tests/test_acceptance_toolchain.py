"""Fake tools verify gate selection/order, not the application test results."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class AcceptanceToolchainTests(unittest.TestCase):
    def run_gate(self, *, local_version=None, path_version="9.9.9", fail_tests=False):
        with tempfile.TemporaryDirectory(prefix="delegate gate ") as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            shutil.copyfile(Path(__file__).with_name("acceptance.sh"), root / "tests/acceptance.sh")
            (root / "pyproject.toml").write_text(
                '[project.optional-dependencies]\ndev = ["ruff==0.15.15"]\n'
            )
            tools = root / "bin"
            tools.mkdir()
            trace = root / "trace"
            python = tools / "python3"
            python.write_text(
                '#!/bin/sh\nif [ "$1" = "-c" ] || [ "$1" = "--version" ]; then\n'
                f'  exec "{sys.executable}" "$@"\nfi\n'
                'printf "python %s\\n" "$*" >> "$TRACE"\n'
                + ('[ "$2" != "unittest" ]\n' if fail_tests else "exit 0\n")
            )
            python.chmod(0o755)

            def make_ruff(path, version, label):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "ruff {version}"; exit 0; fi\n'
                    f'printf "{label} %s\\n" "$*" >> "$TRACE"\n'
                )
                path.chmod(0o755)

            make_ruff(tools / "ruff", path_version, "path-ruff")
            if local_version is not None:
                make_ruff(root / ".venv/bin/ruff", local_version, "project-ruff")
            env = {**os.environ, "PATH": f"{tools}:/usr/bin:/bin", "TRACE": str(trace)}
            result = subprocess.run(
                ["/bin/sh", str(root / "tests/acceptance.sh")],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
            return result, trace.read_text().splitlines() if trace.exists() else []

    def test_prefers_project_pin_and_runs_all_four_gates(self):
        result, trace = self.run_gate(local_version="0.15.15")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            trace,
            [
                "python -m unittest discover -s tests -t .",
                "python -m compileall -q src tests bin",
                "project-ruff check .",
                "project-ruff format --check .",
            ],
        )
        self.assertIn("ruff 0.15.15", result.stdout)
        self.assertIn("Python", result.stdout)
        self.assertIn("acceptance: OK", result.stdout)

    def test_refuses_wrong_ambient_version_before_any_gate(self):
        result, trace = self.run_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(trace, [])
        self.assertIn("0.15.15", result.stderr)
        self.assertNotIn("acceptance: OK", result.stdout)

    def test_accepts_correct_path_tool_when_no_project_environment(self):
        result, trace = self.run_gate(path_version="0.15.15")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("path-ruff check .", trace)

    def test_failure_stops_later_gates(self):
        result, trace = self.run_gate(local_version="0.15.15", fail_tests=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(trace, ["python -m unittest discover -s tests -t ."])
        self.assertNotIn("acceptance: OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
