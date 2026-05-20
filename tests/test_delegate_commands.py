import importlib.util
import io
import json
import os
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
        self.assertEqual(argv, [
            "agent", "--workspace", "/repo", "-p", "--trust", "--approve-mcps", "--mode=plan",
            "--model", "composer-2.5", "--output-format", "text", "hello"
        ])

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
