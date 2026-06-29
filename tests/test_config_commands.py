import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    return importlib.reload(importlib.import_module("delegate_agent.cli"))


class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def run_main(self, argv, *, env):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            code = self.delegate.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_config_init_writes_editable_starter_config(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init"],
                env={"HOME": home, "PATH": os.environ.get("PATH", "")},
            )
            payload = json.loads(stdout)
            path = Path(home) / ".delegate" / "config.json"
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        self.assertEqual(payload["path"], str(path))
        self.assertEqual(config["droid"]["models"]["reviewer"], "replace-with-read-only-model-id")
        self.assertEqual(
            config["profiles"]["definitions"]["work"]["env"]["CODEX_HOME"],
            "~/replace-with-work-codex-home",
        )

    def test_config_init_refuses_existing_without_force(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".delegate" / "config.json"
            path.parent.mkdir()
            path.write_text('{"old": true}\n', encoding="utf-8")
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init"],
                env={"HOME": home, "PATH": os.environ.get("PATH", "")},
            )
            payload = json.loads(stdout)

        self.assertEqual(code, self.delegate.EXIT_USAGE, stderr)
        self.assertEqual(payload["error"], "config_exists")

    def test_config_init_honors_delegate_config_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init", "--force"],
                env={
                    "HOME": tmp,
                    "DELEGATE_CONFIG": str(path),
                    "PATH": os.environ.get("PATH", ""),
                },
            )
            payload = json.loads(stdout)
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        self.assertEqual(payload["path"], str(path))
        self.assertIn("codex", config)

    def test_config_init_rejects_windows_delegate_config_in_wsl(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init"],
                env={
                    "HOME": home,
                    "PATH": os.environ.get("PATH", ""),
                    "WSL_DISTRO_NAME": "Ubuntu",
                    "DELEGATE_CONFIG": r"C:\Users\trey\.delegate\config.json",
                },
            )
            payload = json.loads(stdout)

        self.assertEqual(code, self.delegate.EXIT_USAGE, stderr)
        self.assertEqual(payload["error"], "windows_path")
        self.assertIn("wslpath", payload["message"])


if __name__ == "__main__":
    unittest.main()
