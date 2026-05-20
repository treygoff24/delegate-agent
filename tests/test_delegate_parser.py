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


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def test_documented_examples_parse(self):
        examples = [
            ["cursor", "safe", "analyze this"],
            ["cursor", "work", "fix this"],
            ["droid", "minimax", "safe", "analyze this"],
            ["droid", "minimax", "work", "fix this"],
            ["--json", "run", "--input-json", "task.json"],
            ["models"],
            ["--json", "models"],
            ["describe"],
            ["--json", "describe"],
            ["agent-help"],
            ["dry-run", "cursor", "work", "prompt"],
            ["--json", "dry-run", "droid", "minimax", "safe", "--prompt-file", "task.md"],
        ]
        for argv in examples:
            with self.subTest(argv=argv):
                parsed = self.delegate.parse_cli(argv)
                self.assertIsNotNone(parsed.subcommand)

    def test_global_flags_before_subcommand(self):
        parsed = self.delegate.parse_cli(["--json", "--cwd", "/tmp/repo", "models"])
        self.assertTrue(parsed.json_mode)
        self.assertEqual(parsed.cwd, "/tmp/repo")

    def test_trailing_json_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["dry-run", "droid", "minimax", "work", "hello", "--json"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_prompt_file_before_prompt_text(self):
        parsed = self.delegate.parse_cli(["cursor", "safe", "--prompt-file", "task.md"])
        self.assertEqual(parsed.prompt_file, "task.md")
        self.assertEqual(parsed.prompt_parts, [])

    def test_prompt_file_after_prompt_text_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["cursor", "safe", "hello", "--prompt-file", "task.md"])
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["cursor", "agent", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_mode")

    def test_json_describe_shape(self):
        payload = self.delegate.describe_payload(self.delegate.DEFAULT_CONFIG, "embedded-default")
        self.assertTrue(payload["ok"])
        self.assertIn("safe", payload["modes"])
        self.assertIn("work", payload["modes"])
        self.assertIn("cursor", payload["modeMapping"])
