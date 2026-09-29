"""Fake tools verify gate selection/order, not the application test results."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class AcceptanceToolchainTests(unittest.TestCase):
    def copy_runner(self, root):
        (root / "scripts").mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "scripts/test.py", root / "scripts/test.py"
        )

    def write_python(self, path):
        path.write_text(
            f"#!{sys.executable}\n"
            "import os, platform, runpy, sys\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print('Python ' + platform.python_version())\n"
            "    raise SystemExit(0)\n"
            "if sys.argv[1].endswith('scripts/test.py'):\n"
            "    sys.executable = os.path.abspath(__file__)\n"
            "    sys.argv = sys.argv[1:]\n"
            "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
            "else:\n"
            "    with open(os.environ['TRACE'], 'a') as trace:\n"
            "        trace.write('python ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        )
        path.chmod(0o755)

    def run_gate(self, *, local_version=None, path_version="9.9.9", fail_tests=False):
        with tempfile.TemporaryDirectory(prefix="delegate gate ") as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            shutil.copyfile(Path(__file__).with_name("acceptance.sh"), root / "tests/acceptance.sh")
            self.copy_runner(root)
            (root / "pyproject.toml").write_text(
                '[project.optional-dependencies]\ndev = ["ruff==0.15.15"]\n'
            )
            tools = root / "bin"
            tools.mkdir()
            trace = root / "trace"
            python = tools / "python3"
            self.write_python(python)

            def make_tool(path, label, *, version=None, exit_code=0):
                path.parent.mkdir(parents=True, exist_ok=True)
                version_branch = (
                    f'if [ "$1" = "--version" ]; then echo "{version}"; exit 0; fi\n'
                    if version is not None
                    else ""
                )
                path.write_text(
                    f"#!/bin/sh\n{version_branch}"
                    f'printf "{label} %s\\n" "$*" >> "$TRACE"\nexit {exit_code}\n'
                )
                path.chmod(0o755)

            pytest_exit = 1 if fail_tests else 0
            make_tool(tools / "ruff", "path-ruff", version=f"ruff {path_version}")
            make_tool(tools / "pytest", "path-pytest", exit_code=pytest_exit)
            if local_version is not None:
                make_tool(root / ".venv/bin/ruff", "project-ruff", version=f"ruff {local_version}")
                make_tool(root / ".venv/bin/pytest", "project-pytest", exit_code=pytest_exit)
            env = {**os.environ, "PATH": f"{tools}:/usr/bin:/bin", "TRACE": str(trace)}
            result = subprocess.run(
                ["/bin/sh", str(root / "tests/acceptance.sh"), "--workers", "4"],
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
                "project-pytest -q -n 4 --dist worksteal --durations=25",
                "python -m compileall -q src tests bin scripts bench",
                "project-ruff check .",
                "project-ruff format --check .",
            ],
        )
        self.assertIn("ruff 0.15.15", result.stdout)
        self.assertIn("Python", result.stdout)
        self.assertIn("GATE PASS", result.stdout)

    def test_refuses_wrong_ambient_version_before_any_gate(self):
        result, trace = self.run_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(trace, [])
        self.assertIn("0.15.15", result.stderr)
        self.assertNotIn("GATE PASS", result.stdout)

    def test_accepts_correct_path_tool_when_no_project_environment(self):
        result, trace = self.run_gate(path_version="0.15.15")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("path-pytest -q -n 4 --dist worksteal --durations=25", trace)
        self.assertIn("path-ruff check .", trace)

    def test_missing_pytest_refuses_before_any_gate(self):
        with tempfile.TemporaryDirectory(prefix="delegate gate ") as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            shutil.copyfile(Path(__file__).with_name("acceptance.sh"), root / "tests/acceptance.sh")
            self.copy_runner(root)
            (root / "pyproject.toml").write_text(
                '[project.optional-dependencies]\ndev = ["ruff==0.15.15"]\n'
            )
            tools = root / "bin"
            tools.mkdir()
            trace = root / "trace"
            ruff = tools / "ruff"
            ruff.write_text(
                '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "ruff 0.15.15"; exit 0; fi\n'
                'printf "path-ruff %s\\n" "$*" >> "$TRACE"\nexit 0\n'
            )
            ruff.chmod(0o755)
            python = tools / "python3"
            self.write_python(python)
            # Only the fake tools and the one coreutil the script needs are on
            # PATH, so no ambient pytest can be resolved and the refusal is
            # unambiguously the reason the gate stops.
            dirname = shutil.which("dirname")
            self.assertIsNotNone(dirname)
            assert dirname is not None
            (tools / "dirname").symlink_to(dirname)
            env = {**os.environ, "PATH": str(tools), "TRACE": str(trace)}
            result = subprocess.run(
                ["/bin/sh", str(root / "tests/acceptance.sh"), "--workers", "4"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires pytest", result.stderr)
        self.assertFalse(trace.exists())

    def test_failure_stops_later_gates(self):
        result, trace = self.run_gate(local_version="0.15.15", fail_tests=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(trace, ["project-pytest -q -n 4 --dist worksteal --durations=25"])
        self.assertNotIn("GATE PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
