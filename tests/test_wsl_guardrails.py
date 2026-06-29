import importlib
import io
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


class WslGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.config = importlib.reload(importlib.import_module("delegate_agent.config"))
        self.cli = importlib.reload(importlib.import_module("delegate_agent.cli"))
        self.request_build = importlib.reload(
            importlib.import_module("delegate_agent.request_build")
        )
        self.wsl = importlib.reload(importlib.import_module("delegate_agent.wsl"))

    def test_windows_path_text_detection(self):
        self.assertTrue(self.wsl.is_windows_path_text(r"C:\Users\trey\repo"))
        self.assertTrue(self.wsl.is_windows_path_text("C:/Users/trey/repo"))
        self.assertTrue(self.wsl.is_windows_path_text(r"%USERPROFILE%\repo"))
        self.assertFalse(self.wsl.is_windows_path_text("/mnt/c/Users/trey/repo"))

    def test_workspace_rejects_windows_style_cwd(self):
        with (
            mock.patch("delegate_agent.wsl.is_wsl", return_value=True),
            self.assertRaises(self.cli.DelegateError) as ctx,
        ):
            self.request_build.workspace_for(r"C:\Users\trey\repo")
        self.assertEqual(ctx.exception.error, "windows_path")
        self.assertIn("wslpath", ctx.exception.message)

    def test_windows_style_prompt_file_is_allowed_outside_wsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                path = Path("%USERPROFILE%")
                path.mkdir()
                prompt_file = path / "task.md"
                prompt_file.write_text("review this\n", encoding="utf-8")
                with mock.patch("delegate_agent.wsl.is_wsl", return_value=False):
                    prompt = self.request_build.resolve_prompt([], str(prompt_file), io.StringIO())
            finally:
                os.chdir(original_cwd)
        self.assertEqual(prompt, "review this\n")

    def test_config_rejects_windows_style_codex_home_in_wsl(self):
        config = self.config.embedded_default_config()
        config["profiles"]["definitions"] = {
            "work": {"env": {"CODEX_HOME": r"C:\Users\trey\.codex"}}
        }
        with (
            mock.patch("delegate_agent.wsl.is_wsl", return_value=True),
            self.assertRaises(self.config.ConfigError) as ctx,
        ):
            self.config.validate_config(config)
        self.assertEqual(ctx.exception.error, "windows_path")

    def test_config_allows_windows_style_generic_profile_env_in_wsl(self):
        config = self.config.embedded_default_config()
        config["profiles"]["definitions"] = {
            "work": {"env": {"SOME_TOOL_ARG": r"C:\not\delegate\path"}}
        }
        with mock.patch("delegate_agent.wsl.is_wsl", return_value=True):
            self.config.validate_config(config)

    def test_windows_git_in_wsl_fails_loudly(self):
        with (
            mock.patch("delegate_agent.wsl.is_wsl", return_value=True),
            mock.patch(
                "delegate_agent.request_build.shutil.which",
                return_value="/mnt/c/Program Files/Git/cmd/git.exe",
            ),
            self.assertRaises(self.cli.DelegateError) as ctx,
        ):
            self.request_build.git_root_for(Path("/home/trey/repo"))
        self.assertEqual(ctx.exception.error, "windows_git_in_wsl")
        self.assertIn("Install Git inside WSL", ctx.exception.message)

    def test_drivefs_workspace_warning_is_attached_to_request(self):
        with mock.patch("delegate_agent.wsl.is_wsl", return_value=True):
            request = self.cli.build_request(
                "cursor",
                "safe",
                None,
                self.cli.ResolvedWorkspace("/mnt/c/Users/trey/repo", "directory"),
                "review",
                self.cli.DEFAULT_CONFIG,
                dry_run=True,
            )
        self.assertTrue(any("/mnt/c" in warning for warning in request.warnings))

    def test_drivefs_workspace_warning_covers_mount_root(self):
        with mock.patch("delegate_agent.wsl.is_wsl", return_value=True):
            self.assertIn("/mnt/c", self.wsl.drivefs_workspace_warning("/mnt/c") or "")


if __name__ == "__main__":
    unittest.main()
