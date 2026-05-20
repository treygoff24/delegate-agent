import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"

def load_delegate():
    spec = importlib.util.spec_from_file_location("delegate_cli_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def make_git_repo():
    temp = tempfile.TemporaryDirectory()
    subprocess.run(["git", "-C", temp.name, "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return temp


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def test_cursor_safe_argv_agent_prefix(self):
        argv = self.delegate.build_cursor_argv(["agent"], "safe", "/repo", "composer-2.5", "hello")
        self.assertEqual(argv[0], "agent")
        self.assertEqual(argv[1:8], ["--workspace", "/repo", "-p", "--trust", "--model", "composer-2.5", "--output-format"])
        self.assertEqual(argv[8], "text")
        self.assertTrue(argv[9].startswith(self.delegate.CURSOR_SAFE_REVIEW_PREFIX))
        self.assertTrue(argv[9].endswith("hello"))
        self.assertNotIn("--mode=plan", argv)
        self.assertNotIn("--mode=ask", argv)
        self.assertNotIn("--force", argv)
        self.assertNotIn("--approve-mcps", argv)

    def test_cursor_work_argv_cursor_agent_prefix(self):
        argv = self.delegate.build_cursor_argv(["cursor", "agent"], "work", "/repo", "composer-2.5", "hello")
        self.assertEqual(argv, [
            "cursor", "agent", "--workspace", "/repo", "-p", "--trust", "--approve-mcps", "--force",
            "--model", "composer-2.5", "--output-format", "text", "hello"
        ])
        self.assertNotIn("--mode=agent", argv)
        self.assertNotIn("--mode=plan", argv)
        self.assertNotIn("--mode=ask", argv)

    def test_droid_safe_argv(self):
        argv = self.delegate.build_droid_argv("droid", "safe", "/repo", "model-id", "hello")
        self.assertEqual(argv, ["droid", "exec", "--cwd", "/repo", "--model", "model-id", "hello"])
        self.assertNotIn("--auto", argv)
        self.assertNotIn("--use-spec", argv)
        self.assertNotIn("--skip-permissions-unsafe", argv)

    def test_droid_work_argv(self):
        argv = self.delegate.build_droid_argv("droid", "work", "/repo", "model-id", "hello")
        self.assertEqual(argv, [
            "droid", "exec", "--cwd", "/repo", "--skip-permissions-unsafe",
            "--model", "model-id", "hello"
        ])

    def test_invalid_alias_rejected_before_argv(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.build_request("droid", "safe", "nope", "/repo", "hello", self.delegate.DEFAULT_CONFIG, True)
        self.assertEqual(ctx.exception.error, "invalid_alias")

    def test_describe_preserves_safe_read_only_modes(self):
        payload = self.delegate.describe_payload(self.delegate.DEFAULT_CONFIG, "embedded-default")
        cursor_safe = payload["modeMapping"]["cursor"]["safe"]
        self.assertNotIn("--mode=plan", cursor_safe)
        self.assertNotIn("--mode=ask", cursor_safe)
        self.assertNotIn("--force", cursor_safe)
        self.assertNotIn("--approve-mcps", cursor_safe)
        self.assertIn("<isolated-workspace>", cursor_safe)
        self.assertIn("safeNotes", payload["modeMapping"]["cursor"])
        self.assertNotIn("--auto", payload["modeMapping"]["droid"]["safe"])
        self.assertNotIn("--use-spec", payload["modeMapping"]["droid"]["safe"])
        self.assertNotIn("--skip-permissions-unsafe", payload["modeMapping"]["droid"]["safe"])

    def test_cursor_safe_dry_run_reports_isolation(self):
        request = self.delegate.build_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertIn("isolation", payload)
        self.assertNotIn("--mode=plan", payload["argv"])
        self.assertNotIn("--approve-mcps", payload["argv"])

    def test_cursor_safe_cli_config_omits_mutating_shell(self):
        allow = self.delegate.CURSOR_SAFE_CLI_CONFIG["permissions"]["allow"]
        self.assertIn("Read(**)", allow)
        self.assertNotIn("Shell(git)", allow)
        self.assertNotIn("Shell(find)", allow)
        self.assertNotIn("Shell(ls)", allow)

    def test_mirror_path_preserving_symlinks_keeps_link_target(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            link = Path(workspace) / "link.txt"
            link.symlink_to(secret)

            destination = Path(workspace) / "mirror" / "link.txt"
            self.delegate.mirror_path_preserving_symlinks(link, destination)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(os.readlink(destination), str(secret))

    def test_create_directory_safe_workspace_preserves_symlinks(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(workspace) / "link.txt").symlink_to(secret)

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                copied = Path(copy_path) / "link.txt"
                self.assertTrue(copied.is_symlink())
                self.assertEqual(os.readlink(copied), str(secret))
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)
