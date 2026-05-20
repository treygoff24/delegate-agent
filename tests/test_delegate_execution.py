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


GIT_TEST_IDENTITY = ("-c", "user.name=Delegate Test", "-c", "user.email=delegate-test@example.com")


def cursor_safe_temp_dirs() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("delegate-cursor-safe-*"))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def make_fake_bin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        for name in ("droid", "agent"):
            path = bin_dir / name
            path.write_text("#!/usr/bin/env bash\nprintf 'OUT:%s\\n' \"$*\"\nprintf 'ERR:%s\\n' \"$*\" >&2\nexit \"${FAKE_EXIT:-0}\"\n")
            path.chmod(0o755)
        return bin_dir

    def test_json_success_shape_with_fake_binary(self):
        repo = make_git_repo()
        fake_bin = self.make_fake_bin()
        self.addCleanup(repo.cleanup)
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        request = self.delegate.Request(
            "droid", "safe", repo.name, "hello",
            ["droid", "exec", "--cwd", repo.name, "--model", "model-id", "hello"],
            "model-id",
        )
        with mock.patch.dict(os.environ, {"PATH": env_path}):
            code, payload = self.delegate.execute_request(request, json_mode=True)
        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)
        self.assertTrue(payload["ok"])
        self.assertIn("OUT:", payload["stdout"])
        self.assertIn("ERR:", payload["stderr"])

    def test_json_failure_shape_with_fake_binary(self):
        repo = make_git_repo()
        fake_bin = self.make_fake_bin()
        self.addCleanup(repo.cleanup)
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        request = self.delegate.Request("droid", "safe", repo.name, "hello", ["droid", "exec", "hello"], "model-id")
        with mock.patch.dict(os.environ, {"PATH": env_path, "FAKE_EXIT": "7"}):
            code, payload = self.delegate.execute_request(request, json_mode=True)
        self.assertEqual(code, 7)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "child_failed")

    def test_missing_binary_exit_3(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.ensure_binary(["delegate-definitely-missing-binary"])
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_text_mode_preserves_child_stdout_stderr(self):
        repo = make_git_repo()
        fake_bin = self.make_fake_bin()
        self.addCleanup(repo.cleanup)
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(self.delegate.DEFAULT_CONFIG))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--cwd", repo.name, "droid", "minimax", "safe", "hello"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("OUT:exec", completed.stdout)
        self.assertIn("ERR:exec", completed.stderr)

    def test_json_validation_error_is_one_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing")
            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--json", "--cwd", missing, "droid", "minimax", "safe", "hello"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid_cwd")
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(completed.stderr, "")

    def test_json_dry_run_allows_non_git_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--json", "--cwd", tmp, "dry-run", "droid", "minimax", "safe", "hello"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["workspaceKind"], "directory")
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(tmp).resolve())
        self.assertIn("--cwd", payload["argv"])

    def test_run_input_json_rejects_unknown_keys(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        task = Path(repo.name) / "task.json"
        task.write_text(json.dumps({
            "engine": "droid",
            "mode": "safe",
            "model": "minimax",
            "cwd": repo.name,
            "prompt": "hello",
            "promtp": "typo"
        }))
        parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(ctx.exception.error, "unknown_input_key")

    def test_static_safety_guards(self):
        source = MODULE_PATH.read_text()
        forbidden = [
            "subprocess.Popen",
            "start_new_session",
            "shell=True",
            "git push",
            "git commit",
            "git merge",
        ]
        for text in forbidden:
            with self.subTest(text=text):
                self.assertNotIn(text, source)

    def make_cursor_safe_fake_agent(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "agent"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "touch mutated-by-agent.txt\n"
            "printf 'OUT:%s\\n' \"$*\"\n"
            "exit \"${FAKE_EXIT:-0}\"\n"
        )
        path.chmod(0o755)
        return bin_dir

    def test_cursor_safe_json_reports_source_workspace_not_temp_copy(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_cursor_safe_fake_agent()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(self.delegate.DEFAULT_CONFIG))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)

        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--json", "--cwd", repo.name, "cursor", "safe", "review"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(Path(payload["executionCwd"]).resolve(), Path(payload["cwd"]).resolve())

    def test_cursor_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        tracked = Path(repo.name) / "tracked.txt"
        tracked.write_text("before\n")
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "add", "tracked.txt"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracked.write_text("dirty\n")
        untracked = Path(repo.name) / "notes.txt"
        untracked.write_text("local-only\n")

        fake_bin = self.make_cursor_safe_fake_agent()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(self.delegate.DEFAULT_CONFIG))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        temp_dirs_before = cursor_safe_temp_dirs()

        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--cwd", repo.name, "cursor", "safe", "review"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
        self.assertEqual(tracked.read_text(), "dirty\n")
        self.assertEqual(untracked.read_text(), "local-only\n")
        self.assertEqual(cursor_safe_temp_dirs() - temp_dirs_before, set())

    def test_cursor_safe_directory_execution_does_not_mutate_original_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "source.txt"
            source.write_text("keep-me\n")
            fake_bin = self.make_cursor_safe_fake_agent()
            config = Path(workspace) / "config.json"
            config.write_text(json.dumps(self.delegate.DEFAULT_CONFIG))
            env = os.environ.copy()
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env["DELEGATE_CONFIG"] = str(config)
            temp_dirs_before = cursor_safe_temp_dirs()

            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--cwd", workspace, "cursor", "safe", "review"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertFalse((Path(workspace) / "mutated-by-agent.txt").exists())
            self.assertEqual(source.read_text(), "keep-me\n")
            self.assertEqual(cursor_safe_temp_dirs() - temp_dirs_before, set())
