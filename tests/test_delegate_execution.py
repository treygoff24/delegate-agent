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
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    spec = importlib.util.spec_from_file_location("delegate_cli_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_git_repo():
    temp = tempfile.TemporaryDirectory()
    subprocess.run(
        ["git", "-C", temp.name, "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return temp


GIT_TEST_IDENTITY = ("-c", "user.name=Delegate Test", "-c", "user.email=delegate-test@example.com")


def safe_temp_dirs() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("delegate-safe-*"))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def make_fake_bin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        for name in ("droid", "agent"):
            path = bin_dir / name
            path.write_text(
                '#!/usr/bin/env bash\nprintf \'OUT:%s\\n\' "$*"\nprintf \'ERR:%s\\n\' "$*" >&2\nexit "${FAKE_EXIT:-0}"\n'
            )
            path.chmod(0o755)
        return bin_dir

    def test_json_success_shape_with_fake_binary(self):
        repo = make_git_repo()
        fake_bin = self.make_fake_bin()
        self.addCleanup(repo.cleanup)
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self.delegate.Request(
            "droid",
            "safe",
            repo.name,
            "hello",
            ["droid", "exec", "--cwd", repo.name, "--model", "model-id", "hello"],
            "model-id",
        )
        with mock.patch.dict(os.environ, {"PATH": env_path}):
            code, payload = self.delegate.execute_request(
                request,
                json_mode=True,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)
        self.assertTrue(payload["ok"])
        self.assertIn("alias", payload)
        self.assertIn("runId", payload)
        self.assertIn("snapshotCommand", payload)
        self.assertEqual(payload["exitCode"], 0)
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)

    def test_json_failure_shape_with_fake_binary(self):
        repo = make_git_repo()
        fake_bin = self.make_fake_bin()
        self.addCleanup(repo.cleanup)
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self.delegate.Request(
            "droid",
            "safe",
            repo.name,
            "hello",
            ["droid", "exec", "--cwd", repo.name, "--model", "model-id", "hello"],
            "model-id",
        )
        with mock.patch.dict(os.environ, {"PATH": env_path}):
            code, payload = self.delegate.execute_request(
                request,
                json_mode=True,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)

    def test_run_input_json_rejects_unknown_keys(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        task = Path(repo.name) / "task.json"
        task.write_text(
            json.dumps(
                {
                    "engine": "droid",
                    "mode": "safe",
                    "model": "minimax",
                    "cwd": repo.name,
                    "prompt": "hello",
                    "promtp": "typo",
                }
            )
        )
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
            'exit "${FAKE_EXIT:-0}"\n'
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
        temp_dirs_before = safe_temp_dirs()

        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--cwd", repo.name, "--json", "cursor", "safe", "review"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["workspaceKind"], "git")
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertEqual(safe_temp_dirs() - temp_dirs_before, set())

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
        temp_dirs_before = safe_temp_dirs()

        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--cwd", repo.name, "cursor", "safe", "review"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
        self.assertEqual(tracked.read_text(), "dirty\n")
        self.assertEqual(untracked.read_text(), "local-only\n")
        self.assertEqual(safe_temp_dirs() - temp_dirs_before, set())

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
            temp_dirs_before = safe_temp_dirs()

            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--cwd", workspace, "cursor", "safe", "review"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertFalse((Path(workspace) / "mutated-by-agent.txt").exists())
            self.assertEqual(source.read_text(), "keep-me\n")
            self.assertEqual(safe_temp_dirs() - temp_dirs_before, set())

    def make_codex_safe_fake(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "codex"
        path.write_text(
            '#!/usr/bin/env bash\n'
            'dir="$PWD"\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            '    --cd) dir="$2"; shift 2 ;;\n'
            '    -C) dir="$2"; shift 2 ;;\n'
            '    *) shift ;;\n'
            '  esac\n'
            'done\n'
            'touch "$dir/mutated-by-codex.txt"\n'
            'printf \'{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Codex completed"}]}\n\'\n'
            'exit 0\n'
        )
        path.chmod(0o755)
        return bin_dir

    def test_codex_safe_default_argv_uses_read_only_sandbox_without_network_or_bypasses(self):
        policy = self.delegate.delegate_config.effective_policy(
            self.delegate.DEFAULT_CONFIG,
            engine="codex",
            mode="safe",
        )
        argv = self.delegate.build_codex_argv(
            self.delegate.DEFAULT_CONFIG["codex"],
            "safe",
            "/repo",
            None,
            "review only",
            policy,
            workspace_kind="git",
        )
        self.assertIn("--ask-for-approval", argv[: argv.index("exec")])
        self.assertIn("never", argv[: argv.index("exec")])
        self.assertIn("--sandbox", argv)
        self.assertIn("read-only", argv)
        self.assertNotIn("sandbox_workspace_write.network_access=true", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--dangerously-bypass-hook-trust", argv)

    def test_codex_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_codex_safe_fake()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(self.delegate.DEFAULT_CONFIG))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        temp_dirs_before = safe_temp_dirs()

        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--cwd", repo.name, "--json", "codex", "safe", "review"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse((Path(repo.name) / "mutated-by-codex.txt").exists())
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        # argv structure assertions live in the dry-run and unit tests; the tracked
        # run JSON summary does not surface argv at the top level (matches Cursor's
        # safe-mutation test).
        self.assertEqual(safe_temp_dirs() - temp_dirs_before, set())

    def test_codex_safe_dry_run_reports_isolated_workspace(self):
        request = self.delegate.build_request(
            "codex",
            "safe",
            None,
            "/repo",
            "review only",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertIn("isolation", payload)
        self.assertIn("temporary detached git worktree", payload["isolation"])

    def test_effective_prompt_codex_safe_order(self):
        user = "review the diff"
        p = self.delegate.effective_prompt(
            user, engine="codex", mode="safe", completion_report_mode="markdown"
        )
        self.assertIn("Delegate sub-agent skill review", p)
        codex_idx = p.find("Delegate Codex safe mode")
        user_idx = p.find("review the diff")
        suffix_idx = p.find("Delegate completion report requirement")
        self.assertGreater(codex_idx, 0)
        self.assertGreater(user_idx, codex_idx)
        self.assertGreater(suffix_idx, user_idx)

    def test_effective_prompt_codex_safe_idempotent(self):
        # effective_prompt run twice on the same string must not double-inject the
        # codex safe prefix. prepend_skill_review_instructions is already idempotent;
        # the codex inject must be too.
        once = self.delegate.effective_prompt(
            "review the diff",
            engine="codex",
            mode="safe",
            completion_report_mode="none",
        )
        twice = self.delegate.effective_prompt(
            once,
            engine="codex",
            mode="safe",
            completion_report_mode="none",
        )
        self.assertEqual(once, twice)
        self.assertEqual(once.count("Delegate Codex safe mode"), 1)

    def test_effective_prompt_codex_work_omits_safe_prefix(self):
        p = self.delegate.effective_prompt(
            "ship the fix",
            engine="codex",
            mode="work",
            completion_report_mode="none",
        )
        self.assertNotIn("Delegate Codex safe mode", p)

    def test_effective_prompt_cursor_safe_omits_codex_prefix(self):
        p = self.delegate.effective_prompt(
            "review the diff",
            engine="cursor",
            mode="safe",
            completion_report_mode="none",
        )
        self.assertNotIn("Delegate Codex safe mode", p)


if __name__ == "__main__":
    unittest.main()
