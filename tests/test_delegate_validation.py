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


class TtyStdin(io.StringIO):
    def isatty(self):
        return True

class NonTtyStdin(io.StringIO):
    def isatty(self):
        return False

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def test_non_git_temp_directory_resolves_as_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.delegate.resolve_workspace(tmp)
            self.assertEqual(Path(workspace.path).resolve(), Path(tmp).resolve())
            self.assertEqual(workspace.kind, "directory")

    def test_git_repo_resolves_from_nested_directory(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        nested = Path(repo.name) / "a" / "b"
        nested.mkdir(parents=True)
        workspace = self.delegate.resolve_workspace(str(nested))
        self.assertEqual(Path(workspace.path).resolve(), Path(repo.name).resolve())
        self.assertEqual(workspace.kind, "git")

    def test_prompt_file_works(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write("from file")
            path = f.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        self.assertEqual(self.delegate.resolve_prompt([], path, TtyStdin()), "from file")

    def test_stdin_works(self):
        self.assertEqual(self.delegate.resolve_prompt([], None, NonTtyStdin("from stdin")), "from stdin")

    def test_direct_plus_prompt_file_fails(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt(["direct"], "/tmp/task.md", TtyStdin())
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_direct_plus_stdin_fails(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt(["direct"], None, NonTtyStdin("from stdin"))
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_prompt_file_plus_stdin_fails(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write("from file")
            path = f.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt([], path, NonTtyStdin("from stdin"))
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_no_prompt_with_tty_fails_without_blocking(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt([], None, TtyStdin())
        self.assertEqual(ctx.exception.error, "missing_prompt")

    def test_control_characters_fail(self):
        for bad in ["hello\x00", "hello\x01"]:
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(self.delegate.DelegateError):
                    self.delegate.validate_prompt(bad)

    def test_run_input_json_cwd_same_workspace_succeeds(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        nested = Path(repo.name) / "nested"
        nested.mkdir()
        task = Path(repo.name) / "task.json"
        task.write_text(json.dumps({
            "engine": "droid",
            "mode": "safe",
            "model": "minimax",
            "cwd": repo.name,
            "prompt": "hello"
        }))
        parsed = self.delegate.ParsedCommand("run", json_mode=True, cwd=str(nested), input_json=str(task))
        request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(Path(request.workspace).resolve(), Path(repo.name).resolve())
        self.assertEqual(request.workspace_kind, "git")

    def test_run_input_json_non_git_cwd_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(json.dumps({
                "engine": "droid",
                "mode": "safe",
                "model": "minimax",
                "cwd": tmp,
                "prompt": "hello"
            }))
            parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(Path(request.workspace).resolve(), Path(tmp).resolve())
            self.assertEqual(request.workspace_kind, "directory")

    def test_run_input_json_cwd_conflict_fails(self):
        repo1 = make_git_repo()
        repo2 = make_git_repo()
        self.addCleanup(repo1.cleanup)
        self.addCleanup(repo2.cleanup)
        task = Path(repo1.name) / "task.json"
        task.write_text(json.dumps({
            "engine": "droid",
            "mode": "safe",
            "model": "minimax",
            "cwd": repo1.name,
            "prompt": "hello"
        }))
        parsed = self.delegate.ParsedCommand("run", json_mode=True, cwd=repo2.name, input_json=str(task))
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(ctx.exception.error, "ambiguous_cwd")
