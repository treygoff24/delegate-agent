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
                config=self.delegate.DEFAULT_CONFIG,
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
        with mock.patch.dict(os.environ, {"PATH": env_path, "FAKE_EXIT": "7"}):
            code, payload = self.delegate.execute_request(
                request,
                json_mode=True,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 7)
        self.assertIsNotNone(payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "child_failed")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exitCode"], 7)

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
            [
                sys.executable,
                str(MODULE_PATH),
                "--cwd",
                repo.name,
                "--json",
                "cursor",
                "safe",
                "review",
            ],
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
            "#!/usr/bin/env bash\n"
            'dir="$PWD"\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            '    --cd) dir="$2"; shift 2 ;;\n'
            '    -C) dir="$2"; shift 2 ;;\n'
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            'touch "$dir/mutated-by-codex.txt"\n'
            'printf \'{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Codex completed"}]}\n\'\n'
            "exit 0\n"
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
            [
                sys.executable,
                str(MODULE_PATH),
                "--cwd",
                repo.name,
                "--json",
                "codex",
                "safe",
                "review",
            ],
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

    def test_codex_missing_binary_exit_3(self):
        request = self.delegate.Request(
            "codex",
            "work",
            "/repo",
            "hello",
            ["delegate-definitely-missing-codex", "exec", "hello"],
            None,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.ensure_binary(request.argv)
        self.assertEqual(ctx.exception.exit_code, 3)

    # -- Wave 2: dry-run structured isolation fields --------------------------------

    def test_dry_run_cursor_safe_includes_structured_isolation_fields(self):
        request = self.delegate.build_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("isolationMode", payload)
        self.assertIn("effectiveIsolation", payload)
        self.assertIn("isolationLifecycle", payload)
        self.assertIn("preservedWorkspace", payload)
        self.assertIn("plannedExecutionCwd", payload)
        self.assertIn("plannedBranch", payload)

    def test_dry_run_cursor_work_auto_isolation_fields(self):
        """Work mode with auto isolation reports none lifecycle."""
        request = self.delegate.build_request(
            "cursor",
            "work",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["isolationMode"], "none")
        self.assertEqual(payload["isolationLifecycle"], "none")
        self.assertFalse(payload["preservedWorkspace"])
        self.assertIsNone(payload["plannedBranch"])
        self.assertIsNone(payload["plannedExecutionCwd"])

    def test_dry_run_codex_safe_uses_worktree_temporary(self):
        request = self.delegate.build_request(
            "codex",
            "safe",
            None,
            "/repo",
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["isolationMode"], "worktree")
        self.assertEqual(payload["isolationLifecycle"], "temporary")
        self.assertFalse(payload["preservedWorkspace"])

    def test_dry_run_isolation_field_is_not_repurposed(self):
        """The existing isolation field is kept as a human-readable note, not an enum."""
        request = self.delegate.build_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("isolation", payload)
        self.assertIsInstance(payload["isolation"], str)
        self.assertGreater(len(payload["isolation"]), 5)

    # -- Wave 2: dry-run no-artifact assertions ------------------------------------

    def test_dry_run_isolation_worktree_creates_no_filesystem_artifacts_under_tmp_home(self):
        """Assert dry-run with --isolation worktree creates NO filesystem entries
        (no worktree dir, no branches, no registry)."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                with tempfile.TemporaryDirectory() as repo_dir:
                    subprocess.run(
                        ["git", "-C", repo_dir, "init"],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    subprocess.run(
                        ["git", "-C", repo_dir, "config", "user.name", "Test"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    subprocess.run(
                        ["git", "-C", repo_dir, "config", "user.email", "test@example.com"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    subprocess.run(
                        ["git", "-C", repo_dir, "commit", "--allow-empty", "-m", "init"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    parsed = self.delegate.ParsedCommand(
                        "cursor",
                        json_mode=True,
                        cwd=repo_dir,
                        engine="cursor",
                        mode="work",
                        prompt_parts=["hello"],
                        dry_run=True,
                        isolation="worktree",
                    )
                    config, _ = self.delegate.load_config()
                    stdout_buf = io.StringIO()
                    code = self.delegate.main(
                        [
                            "--cwd",
                            repo_dir,
                            "--json",
                            "--isolation",
                            "worktree",
                            "dry-run",
                            "cursor",
                            "work",
                            "hello",
                        ],
                        stdout=stdout_buf,
                    )
                    self.assertEqual(code, self.delegate.EXIT_OK)
                    # Verify no worktree dir was created under fake home
                    worktree_dir = Path(fake_home) / ".delegate" / "worktrees"
                    self.assertFalse(worktree_dir.exists())
                    # Verify no delegate/* branches were created
                    branch_result = subprocess.run(
                        ["git", "-C", repo_dir, "branch", "--list", "delegate/*"],
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(branch_result.stdout.strip(), "")
                    # Verify no .delegate/ registry was written in source workspace
                    self.assertFalse((Path(repo_dir) / ".delegate").exists())

    def test_dry_run_isolation_worktree_codex_creates_no_artifacts(self):
        """Assert dry-run with codex+isolation worktree creates NO filesystem entries
        (no worktree dir, no branches, no registry)."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                with tempfile.TemporaryDirectory() as repo_dir:
                    subprocess.run(
                        ["git", "-C", repo_dir, "init"],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    subprocess.run(
                        ["git", "-C", repo_dir, "config", "user.name", "Test"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    subprocess.run(
                        ["git", "-C", repo_dir, "config", "user.email", "test@example.com"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    subprocess.run(
                        ["git", "-C", repo_dir, "commit", "--allow-empty", "-m", "init"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    stdout_buf = io.StringIO()
                    code = self.delegate.main(
                        [
                            "--cwd",
                            repo_dir,
                            "--json",
                            "--isolation",
                            "worktree",
                            "dry-run",
                            "codex",
                            "work",
                            "hello",
                        ],
                        stdout=stdout_buf,
                    )
                    self.assertEqual(code, self.delegate.EXIT_OK)
                    worktree_dir = Path(fake_home) / ".delegate" / "worktrees"
                    self.assertFalse(worktree_dir.exists())
                    # Verify no delegate/* branches were created
                    branch_result = subprocess.run(
                        ["git", "-C", repo_dir, "branch", "--list", "delegate/*"],
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(branch_result.stdout.strip(), "")
                    # Verify no .delegate/ registry was written in source workspace
                    self.assertFalse((Path(repo_dir) / ".delegate").exists())


    # -- Finding A: isolatedWorkspace always emitted as explicit boolean ----------

    def test_dry_run_cursor_work_isolated_workspace_false(self):
        """Cursor work mode dry-run JSON has isolatedWorkspace: false."""
        request = self.delegate.build_request(
            "cursor", "work", None, "/repo", "hello",
            self.delegate.DEFAULT_CONFIG, dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("isolatedWorkspace", payload)
        self.assertFalse(payload["isolatedWorkspace"])

    def test_dry_run_droid_safe_isolated_workspace_false(self):
        """Droid safe mode dry-run JSON has isolatedWorkspace: false."""
        config = dict(self.delegate.DEFAULT_CONFIG)
        config["droid"] = dict(config["droid"])
        config["droid"]["models"] = {"test-model": "real-model-id"}
        request = self.delegate.build_request(
            "droid", "safe", "test-model", "/repo", "hello",
            config, dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("isolatedWorkspace", payload)
        self.assertFalse(payload["isolatedWorkspace"])

    def test_dry_run_cursor_safe_isolated_workspace_true(self):
        """Cursor safe mode dry-run JSON has isolatedWorkspace: true."""
        request = self.delegate.build_request(
            "cursor", "safe", None, "/repo", "review",
            self.delegate.DEFAULT_CONFIG, dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("isolatedWorkspace", payload)
        self.assertTrue(payload["isolatedWorkspace"])

    def test_dry_run_isolation_worktree_isolated_workspace_true(self):
        """--isolation worktree cursor work dry-run JSON has isolatedWorkspace: true."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.delegate.ParsedCommand(
            "cursor", json_mode=True, cwd=repo.name,
            engine="cursor", mode="work", prompt_parts=["hello"],
            dry_run=True, isolation="worktree",
        )
        request = self.delegate.request_from_parsed(
            parsed, self.delegate.DEFAULT_CONFIG, io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("isolatedWorkspace", payload)
        self.assertTrue(payload["isolatedWorkspace"])

    # -- Finding C: git metadata captured in request_from_parsed path ---------------

    def _make_git_repo_with_commit(self):
        """Create a temp git repo with one commit, return the path and common dir."""
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, "init"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "config", "user.name", "Test"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "config", "user.email", "test@example.com"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "commit", "--allow-empty", "-m", "init"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        gd = subprocess.run(
            ["git", "-C", repo.name, "rev-parse", "--git-common-dir"],
            text=True, capture_output=True, check=True,
        )
        git_common_dir = gd.stdout.strip()
        if not git_common_dir.startswith("/"):
            git_common_dir = str(Path(repo.name) / git_common_dir)
        return repo, git_common_dir

    def test_dry_run_worktree_planned_paths_not_placeholders_cursor(self):
        """Cursor work --isolation worktree dry-run via request_from_parsed yields
        concrete planned paths (real fingerprint + label), not the literal
        '<planned-worktree-path>' or '<planned-branch>' sentinel strings.
        The run-id may still be a placeholder since no run is allocated."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.delegate.ParsedCommand(
            "cursor", json_mode=True, cwd=repo.name,
            engine="cursor", mode="work", prompt_parts=["hello"],
            dry_run=True, isolation="worktree",
        )
        request = self.delegate.request_from_parsed(
            parsed, self.delegate.DEFAULT_CONFIG, io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("plannedExecutionCwd", payload)
        self.assertIsNotNone(payload["plannedExecutionCwd"])
        # Must not be the literal sentinel string
        self.assertNotEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
        # Must contain a real 12-hex fingerprint and engine label
        import re
        self.assertRegex(payload["plannedExecutionCwd"], r"worktrees/[0-9a-f]{12}/cursor-")
        self.assertIn("plannedBranch", payload)
        self.assertIsNotNone(payload["plannedBranch"])
        self.assertNotEqual(payload["plannedBranch"], "<planned-branch>")
        self.assertRegex(payload["plannedBranch"], r"^delegate/cursor-")

    def test_dry_run_worktree_planned_paths_not_placeholders_codex(self):
        """Codex work --isolation worktree dry-run yields concrete planned paths."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.delegate.ParsedCommand(
            "codex", json_mode=True, cwd=repo.name,
            engine="codex", mode="work", prompt_parts=["hello"],
            dry_run=True, isolation="worktree",
        )
        request = self.delegate.request_from_parsed(
            parsed, self.delegate.DEFAULT_CONFIG, io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIsNotNone(payload["plannedExecutionCwd"])
        self.assertNotEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
        import re
        self.assertRegex(payload["plannedExecutionCwd"], r"worktrees/[0-9a-f]{12}/codex-")
        self.assertIsNotNone(payload["plannedBranch"])
        self.assertNotEqual(payload["plannedBranch"], "<planned-branch>")
        self.assertRegex(payload["plannedBranch"], r"^delegate/codex-")

    def test_dry_run_worktree_planned_paths_not_placeholders_droid_qwen(self):
        """Droid qwen work --isolation worktree dry-run yields concrete planned paths."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.delegate.ParsedCommand(
            "droid", json_mode=True, cwd=repo.name,
            engine="droid", mode="work", model_alias="qwen",
            prompt_parts=["hello"], dry_run=True, isolation="worktree",
        )
        config = dict(self.delegate.DEFAULT_CONFIG)
        config["droid"] = dict(config["droid"])
        config["droid"]["models"] = dict(config["droid"]["models"])
        config["droid"]["models"]["qwen"] = "real-model-id"
        request = self.delegate.request_from_parsed(
            parsed, config, io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIsNotNone(payload["plannedExecutionCwd"])
        self.assertNotEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
        import re
        self.assertRegex(payload["plannedExecutionCwd"], r"worktrees/[0-9a-f]{12}/droid-qwen-")
        self.assertIsNotNone(payload["plannedBranch"])
        self.assertNotEqual(payload["plannedBranch"], "<planned-branch>")
        self.assertRegex(payload["plannedBranch"], r"^delegate/droid-qwen-")

    def test_dry_run_worktree_text_output_shows_planned_path(self):
        """Text dry-run output shows planned execution path, not source workspace."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                stdout_buf = io.StringIO()
                code = self.delegate.main(
                    [
                        "--cwd", repo.name,
                        "--isolation", "worktree",
                        "dry-run", "cursor", "work", "hello",
                    ],
                    stdout=stdout_buf,
                )
                self.assertEqual(code, self.delegate.EXIT_OK)
                output = stdout_buf.getvalue()
                # The argv line should show the planned path in --workspace, not the source
                self.assertIn("--workspace", output)
                # Should contain worktree dir pattern, not the source repo path
                self.assertIn("worktrees", output)
                self.assertIn("cursor-", output)

    def test_dry_run_worktree_non_git_workspace_shows_placeholders(self):
        """Non-Git workspace with --isolation worktree dry-run shows placeholder paths
        since fingerprint and branch cannot be computed (would fail at execution)."""
        with tempfile.TemporaryDirectory() as non_git_dir:
            parsed = self.delegate.ParsedCommand(
                "cursor", json_mode=True, cwd=non_git_dir,
                engine="cursor", mode="work", prompt_parts=["hello"],
                dry_run=True, isolation="worktree",
            )
            request = self.delegate.request_from_parsed(
                parsed, self.delegate.DEFAULT_CONFIG, io.StringIO(),
            )
            payload = self.delegate.dry_run_payload(request)
            # Falls back to sentinel placeholders for non-Git workspaces
            self.assertEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
            self.assertEqual(payload["plannedBranch"], "<planned-branch>")
            self.assertIn("isolatedWorkspace", payload)
            self.assertTrue(payload["isolatedWorkspace"])

    # ==========================================================================
    # Wave 3: persistent and temporary execution tests
    # ==========================================================================

    # -- Persistent worktree execution helpers --------------------------------

    def _make_persistent_worktree_request(self, engine, mode, repo_dir, config, model_alias=None):
        """Build a Request with persistent worktree isolation context."""
        workspace = self.delegate.resolve_workspace(repo_dir)
        effective_isolation = self.delegate.delegate_config.resolve_isolation(
            cli_value="worktree", loaded_config=config, engine=engine, mode=mode,
        )
        git_root, git_common_dir, head_oid, head_ref, branch_name = (
            self.delegate.capture_git_metadata(repo_dir)
        )
        isolation_context = self.delegate.build_isolation_context(
            source_workspace=workspace.path,
            resolved_isolation=effective_isolation,
            engine=engine, mode=mode, model_alias=model_alias,
            config=config, run_short_id=None,
            source_git_root=git_root, source_git_common_dir=git_common_dir,
            source_head_oid=head_oid, source_head_ref=head_ref,
            source_branch=branch_name,
        )
        return self.delegate.build_request(
            engine, mode, model_alias, workspace, "hello", config, dry_run=False,
            isolation_context=isolation_context,
        )

    # -- Non-Git workspace fails clearly --------------------------------------

    def test_persistent_worktree_non_git_fails_clearly(self):
        """Non-Git workspace with --isolation worktree fails before creating artifacts."""
        with tempfile.TemporaryDirectory() as non_git:
            workspace = self.delegate.resolve_workspace(non_git)
            request = self.delegate.Request(
                "cursor", "work", non_git, "hello",
                ["agent", "--workspace", non_git, "-p", "--trust", "hello"],
                "composer-2.5", workspace_kind="directory",
                isolation_context=self.delegate.IsolationContext(
                    source_workspace=non_git,
                    effective_isolation="worktree",
                    isolation_mode="worktree",
                    isolation_lifecycle="persistent",
                    preserved_workspace=True,
                ),
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.execute_request(
                    request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False, completion_report_mode="markdown",
                    source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                )
            self.assertEqual(ctx.exception.error, "worktree_requires_git")

    # -- Unborn Git repo fails with missing_git_head --------------------------

    def test_persistent_worktree_unborn_repo_fails(self):
        """Clean but unborn Git repo fails with missing_git_head."""
        repo = make_git_repo()  # no commits
        self.addCleanup(repo.cleanup)
        workspace = self.delegate.resolve_workspace(repo.name)
        isolation_context = self.delegate.IsolationContext(
            source_workspace=repo.name,
            effective_isolation="worktree",
            isolation_mode="worktree",
            isolation_lifecycle="persistent",
            preserved_workspace=True,
        )
        request = self.delegate.Request(
            "cursor", "work", repo.name, "hello",
            ["agent", "--workspace", repo.name, "-p", "--trust", "hello"],
            "composer-2.5", workspace_kind="git",
            isolation_context=isolation_context,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                pass_through=False, completion_report_mode="markdown",
                source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "missing_git_head")

    # -- Dirty source fails clearly -------------------------------------------

    def test_persistent_worktree_dirty_source_fails(self):
        """Dirty source workspace fails clean-source check before creating artifacts."""
        repo, _git_cd = self._make_git_repo_with_commit()
        (Path(repo.name) / "dirty.txt").write_text("untracked\n")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                pass_through=False, completion_report_mode="markdown",
                source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "dirty_source_workspace")
        # Registry base dir may exist (step 5 happens before step 6),
        # but no run-specific directories should exist.
        delegate_dir = Path(repo.name) / ".delegate"
        runs_dir = delegate_dir / "runs"
        if runs_dir.exists():
            run_dirs = list(runs_dir.glob("del_*"))
            self.assertEqual(len(run_dirs), 0, "No run dirs should exist on dirty-source failure")

    # -- Pass-through rejected for persistent worktree ------------------------

    def test_persistent_worktree_rejects_pass_through(self):
        """--pass-through with persistent worktree fails before creating artifacts."""
        repo, _git_cd = self._make_git_repo_with_commit()
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                pass_through=True, completion_report_mode="markdown",
                source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "pass_through_with_persistent_isolation")
        self.assertIn("persistent worktree runs", ctx.exception.message)

    # -- Persistent worktree: cursor work runs in isolated worktree -----------

    def test_cursor_work_persistent_worktree_runs_in_worktree(self):
        """Cursor work with --isolation worktree runs in persistent Git worktree."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_cursor_safe_fake_agent()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                # Replace argv with fake binary
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, payload = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                # Source workspace should NOT be mutated
                self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
                # Worktree should exist under fake home
                worktree_root = Path(fake_home) / ".delegate" / "worktrees"
                self.assertTrue(worktree_root.exists())
                worktrees = list(worktree_root.glob("*/*"))
                self.assertTrue(len(worktrees) > 0, "No worktree directories found")
                # The worktree should contain the mutated file (child ran inside it)
                for wt in worktrees:
                    if wt.is_dir():
                        mutated = wt / "mutated-by-agent.txt"
                        if mutated.exists():
                            break
                else:
                    self.fail("No worktree contained mutated-by-agent.txt")

    # -- Persistent worktree remains after child exit -------------------------

    def test_persistent_worktree_preserved_after_child_exit(self):
        """Persistent worktree remains after successful child run."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_cursor_safe_fake_agent()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                # Worktree directory should still exist
                worktree_root = Path(fake_home) / ".delegate" / "worktrees"
                worktrees = list(worktree_root.glob("*/*"))
                self.assertTrue(len(worktrees) > 0, "Worktree should be preserved")
                # Source should NOT have mutated file
                self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
                # Worktree should HAVE mutated file
                any_mutated = any(
                    (wt / "mutated-by-agent.txt").exists()
                    for wt in worktrees if wt.is_dir()
                )
                self.assertTrue(any_mutated, "Worktree should have mutated-by-agent.txt")

    # -- Persistent worktree preserved after child failure --------------------

    def test_persistent_worktree_preserved_after_child_failure(self):
        """Persistent worktree remains even when child exits non-zero."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_cursor_safe_fake_agent()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "FAKE_EXIT": "3",
                }):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 3)
                # Worktree should STILL exist after failure
                worktree_root = Path(fake_home) / ".delegate" / "worktrees"
                worktrees = list(worktree_root.glob("*/*"))
                self.assertTrue(len(worktrees) > 0, "Worktree should be preserved after failure")

    # -- Droid argv rewritten for worktree ------------------------------------

    def test_droid_work_persistent_worktree_rewrites_cwd(self):
        """Droid work --isolation worktree rewrites --cwd to execution worktree."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_fake_bin()
                config = dict(self.delegate.DEFAULT_CONFIG)
                config["droid"] = dict(config["droid"])
                config["droid"]["models"] = {"qwen": "real-model-id"}
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "droid", "work", repo.name, config, model_alias="qwen",
                )
                # Replace argv with fake binary (pointed at source cwd, will be rewritten)
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "droid"), "exec", "--cwd", repo.name,
                     "--model", "real-model-id", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=config,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                # The worktree should exist
                worktree_root = Path(fake_home) / ".delegate" / "worktrees"
                worktrees = list(worktree_root.glob("*/*"))
                self.assertTrue(len(worktrees) > 0)

    # -- Safe + worktree passthrough allowed and cleans up --------------------

    def test_safe_worktree_passthrough_allowed_and_cleans_up(self):
        """--pass-through --isolation worktree cursor safe is allowed and cleans up."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_cursor_safe_fake_agent()
                config = dict(self.delegate.DEFAULT_CONFIG)
                workspace = self.delegate.resolve_workspace(repo.name)
                git_root, git_common_dir, head_oid, head_ref, branch_name = (
                    self.delegate.capture_git_metadata(repo.name)
                )
                effective = self.delegate.delegate_config.resolve_isolation(
                    cli_value="worktree", loaded_config=config, engine="cursor", mode="safe",
                )
                isolation_context = self.delegate.build_isolation_context(
                    source_workspace=workspace.path,
                    resolved_isolation=effective,
                    engine="cursor", mode="safe",
                    config=config, run_short_id=None,
                    source_git_root=git_root, source_git_common_dir=git_common_dir,
                    source_head_oid=head_oid, source_head_ref=head_ref,
                    source_branch=branch_name,
                )
                request = self.delegate.Request(
                    "cursor", "safe", repo.name,
                    self.delegate.prefix_cursor_safe_prompt(
                        self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"),
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text",
                     self.delegate.prefix_cursor_safe_prompt(
                         self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello")],
                    "composer-2.5", workspace_kind="git",
                    isolation_context=isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=config,
                        pass_through=True, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                # Source should NOT have mutated file
                self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
                # No temp dirs should remain (safe_temp_dirs returns dirs under tempfile)
                # The cleanup is handled by the context manager's finally block

    # -- Cursor safe --isolation none does not write source .cursor/cli.json --

    def test_cursor_safe_isolation_none_no_source_cursor_config(self):
        """Cursor safe with --isolation none does not write .cursor/cli.json in source."""
        repo, _git_cd = self._make_git_repo_with_commit()
        source_cursor_config = Path(repo.name) / ".cursor" / "cli.json"
        self.assertFalse(source_cursor_config.exists())
        # The safe_isolated_request context manager with isolation "none"
        # skips isolation entirely, so no .cursor/cli.json is written.
        config = dict(self.delegate.DEFAULT_CONFIG)
        workspace = self.delegate.resolve_workspace(repo.name)
        git_root, git_common_dir, head_oid, head_ref, branch_name = (
            self.delegate.capture_git_metadata(repo.name)
        )
        effective = self.delegate.delegate_config.resolve_isolation(
            cli_value="none", loaded_config=config, engine="cursor", mode="safe",
        )
        isolation_context = self.delegate.build_isolation_context(
            source_workspace=workspace.path,
            resolved_isolation=effective,
            engine="cursor", mode="safe",
            config=config, run_short_id=None,
            source_git_root=git_root, source_git_common_dir=git_common_dir,
            source_head_oid=head_oid, source_head_ref=head_ref,
            source_branch=branch_name,
        )
        request = self.delegate.Request(
            "cursor", "safe", repo.name,
            self.delegate.prefix_cursor_safe_prompt(
                self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"),
            ["agent", "--workspace", repo.name, "-p", "--trust", "--model",
             "composer-2.5", "--output-format", "text",
             self.delegate.prefix_cursor_safe_prompt(
                 self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello")],
            "composer-2.5", workspace_kind="git",
            isolation_context=isolation_context,
        )
        # safe_isolated_request should skip isolation for none
        with self.delegate.safe_isolated_request(request) as isolated:
            self.assertEqual(isolated.workspace, repo.name)
        # After context exits, source .cursor/cli.json should NOT exist
        self.assertFalse(source_cursor_config.exists(),
                         "Source .cursor/cli.json should NOT be written for --isolation none")

    def test_safe_isolated_request_preserves_request_metadata(self):
        """Temporary safe isolation must not shift Request dataclass fields."""
        repo, _git_cd = self._make_git_repo_with_commit()
        isolation_context = self.delegate.IsolationContext(
            source_workspace=repo.name,
            effective_isolation="worktree",
            isolation_mode="worktree",
            isolation_lifecycle="temporary",
            preserved_workspace=False,
            source_git_root=repo.name,
        )
        request = self.delegate.Request(
            engine="cursor",
            mode="safe",
            workspace=repo.name,
            prompt="review this",
            argv=[
                "agent",
                "--workspace",
                repo.name,
                "-p",
                "--trust",
                "--model",
                "composer-2.5",
                "review this",
            ],
            model="composer-2.5",
            model_alias="composer",
            dry_run=True,
            workspace_kind="git",
            isolation_context=isolation_context,
        )

        with self.delegate.safe_isolated_request(request) as isolated:
            self.assertEqual(isolated.model, "composer-2.5")
            self.assertEqual(isolated.model_alias, "composer")
            self.assertTrue(isolated.dry_run)
            self.assertEqual(isolated.workspace_kind, "git")
            self.assertNotEqual(isolated.workspace, repo.name)
            self.assertIn(isolated.workspace, isolated.argv)
            self.assertNotIn(repo.name, isolated.argv)

    # -- Persistent prompt note ordering --------------------------------------

    def test_persistent_worktree_prompt_note_after_skill_review(self):
        """Persistent worktree context note appears after skill-review and before prompt."""
        from delegate_agent.isolation import prepend_persistent_worktree_context, PERSISTENT_WORKTREE_CONTEXT_NOTE

        user_prompt = "Implement the fix."
        skill_prefix = self.delegate.delegate_runner.SKILL_REVIEW_PREFIX
        full_prompt = skill_prefix + user_prompt
        # Then inject worktree context
        result = prepend_persistent_worktree_context(full_prompt)

        skill_idx = result.index("Delegate sub-agent skill review")
        worktree_idx = result.index("You are running in a Delegate-created")
        user_idx = result.index("Implement the fix")

        self.assertGreater(worktree_idx, skill_idx)
        self.assertGreater(user_idx, worktree_idx)
        self.assertIn("delegate worktree remove <alias> --force", PERSISTENT_WORKTREE_CONTEXT_NOTE)

    # -- worktreeStatus is set to "present" after run -------------------------

    def test_persistent_worktree_state_includes_worktree_status_present(self):
        """After successful persistent worktree run, state.json includes worktreeStatus: present."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_cursor_safe_fake_agent()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                # Check state.json in registry for worktreeStatus
                registry_root = Path(repo.name) / ".delegate"
                runs_dir = registry_root / "runs"
                run_dirs = list(runs_dir.glob("del_*"))
                self.assertTrue(len(run_dirs) > 0, "No run directory found")
                state = self.delegate.json.loads(
                    (run_dirs[0] / "state.json").read_text()
                )
                self.assertEqual(state.get("worktreeStatus"), "present")

    # -- Manifest includes creationContext ------------------------------------

    def test_persistent_worktree_manifest_includes_creation_context(self):
        """Manifest includes creationContext with sourceHeadOid, plannedBranch, etc."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_cursor_safe_fake_agent()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                # Check manifest.json
                registry_root = Path(repo.name) / ".delegate"
                runs_dir = registry_root / "runs"
                run_dirs = list(runs_dir.glob("del_*"))
                self.assertTrue(len(run_dirs) > 0)
                manifest = self.delegate.json.loads(
                    (run_dirs[0] / "manifest.json").read_text()
                )
                self.assertIn("creationContext", manifest)
                cc = manifest["creationContext"]
                self.assertIn("sourceHeadOid", cc)
                self.assertIn("plannedBranch", cc)
                self.assertIn("plannedExecutionCwd", cc)
                self.assertTrue(cc["plannedBranch"].startswith("delegate/cursor-"))
                self.assertIn("/worktrees/", cc["plannedExecutionCwd"])

    # -- Finding 1: Prompt injection reaches the child ------------------------

    def _make_logging_fake_bin(self, name, log_file):
        """Make a fake binary that logs its argv to a file."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / name
        path.write_text(
            f'#!/usr/bin/env bash\n'
            f'echo "$@" >> {log_file}\n'
            f'exit 0\n'
        )
        path.chmod(0o755)
        return bin_dir

    def test_prompt_context_note_reaches_child_cursor(self):
        """Persistent worktree prompt context note is present in child argv for cursor."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                log_file = str(Path(fake_home) / "child-argv.log")
                fake_bin = self._make_logging_fake_bin("agent", log_file)
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                logged = Path(log_file).read_text() if Path(log_file).exists() else ""
                self.assertIn("You are running in a Delegate-created isolated Git worktree", logged)

    def test_prompt_context_note_reaches_child_codex(self):
        """Persistent worktree prompt context note is present in child argv for codex."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                log_file = str(Path(fake_home) / "child-argv.log")
                fake_bin = self._make_logging_fake_bin("codex", log_file)
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "codex", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "codex"), "exec", "--cd", repo.name, "--sandbox",
                     "workspace-write", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                logged = Path(log_file).read_text() if Path(log_file).exists() else ""
                self.assertIn("You are running in a Delegate-created isolated Git worktree", logged)

    def test_prompt_context_note_reaches_child_droid(self):
        """Persistent worktree prompt context note is present in child argv for droid."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                log_file = str(Path(fake_home) / "child-argv.log")
                fake_bin = self._make_logging_fake_bin("droid", log_file)
                config = dict(self.delegate.DEFAULT_CONFIG)
                config["droid"] = dict(config["droid"])
                config["droid"]["models"] = {"qwen": "real-model-id"}
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "droid", "work", repo.name, config, model_alias="qwen",
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "droid"), "exec", "--cwd", repo.name,
                     "--model", "real-model-id", "--output-format", "text", "hello"],
                    request.model, model_alias="qwen", dry_run=False,
                    workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=config,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                logged = Path(log_file).read_text() if Path(log_file).exists() else ""
                self.assertIn("You are running in a Delegate-created isolated Git worktree", logged)

    # -- Finding 2: Pre-launch failure inspectable via snapshot ----------------

    def test_prelaunch_failure_inspectable_via_snapshot(self):
        """Pre-launch worktree creation failure is inspectable via delegate snapshot main()."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                # Force create_persistent_worktree to fail.
                original_create = self.delegate.create_persistent_worktree
                def failing_create(*args, **kwargs):
                    raise self.delegate.IsolationExecutionError(
                        "worktree_create_failed", "Simulated worktree failure"
                    )
                self.delegate.create_persistent_worktree = failing_create
                try:
                    with self.assertRaises(self.delegate.DelegateError) as ctx:
                        self.delegate.execute_request(
                            request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                            pass_through=False, completion_report_mode="none",
                            source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                        )
                    self.assertEqual(ctx.exception.error, "worktree_create_failed")

                    # Now invoke delegate snapshot <alias> via main() to verify
                    # it surfaces the pre-launch failure fields.
                    registry_root = Path(repo.name) / ".delegate"
                    runs_dir = registry_root / "runs"
                    run_dirs = list(runs_dir.glob("del_*"))
                    self.assertTrue(len(run_dirs) > 0)
                    run_id = run_dirs[0].name

                    # Invoke main() with the snapshot subcommand.
                    snapshot_stdout = io.StringIO()
                    snapshot_code = self.delegate.main(
                        ["--cwd", repo.name, "--json", "snapshot", run_id],
                        stdout=snapshot_stdout,
                    )
                    self.assertEqual(snapshot_code, 0)
                    snapshot_payload = json.loads(snapshot_stdout.getvalue())

                    # Assert snapshot contains the expected pre-launch failure fields.
                    self.assertEqual(snapshot_payload.get("status"), "failed")
                    self.assertIn("error", snapshot_payload)
                    self.assertIn("message", snapshot_payload)
                    self.assertIn("plannedBranch", snapshot_payload)
                    self.assertIn("plannedExecutionCwd", snapshot_payload)
                finally:
                    self.delegate.create_persistent_worktree = original_create

    def test_prelaunch_failure_snapshot_omits_unrealized_fields(self):
        """Pre-launch failure snapshot omits executionCwd/worktreeStatus/worktreeCleanupCommands
        so a failed git worktree add doesn't imply the worktree exists."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                original_create = self.delegate.create_persistent_worktree
                def failing_create(*args, **kwargs):
                    raise self.delegate.IsolationExecutionError(
                        "worktree_create_failed", "Simulated worktree failure"
                    )
                self.delegate.create_persistent_worktree = failing_create
                try:
                    with self.assertRaises(self.delegate.DelegateError) as ctx:
                        self.delegate.execute_request(
                            request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                            pass_through=False, completion_report_mode="none",
                            source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                        )
                    self.assertEqual(ctx.exception.error, "worktree_create_failed")

                    # Load the failed snapshot directly.
                    registry_root = Path(repo.name) / ".delegate"
                    runs_dir = registry_root / "runs"
                    run_dirs = list(runs_dir.glob("del_*"))
                    self.assertTrue(len(run_dirs) > 0)
                    run_id = run_dirs[0].name
                    snapshot = self.delegate.run_registry.load_run_snapshot(registry_root, run_id)

                    # Planned fields MUST be present (they carry the intent).
                    self.assertIn("plannedBranch", snapshot)
                    self.assertIn("plannedExecutionCwd", snapshot)

                    # Unrealized fields must NOT be present (worktree was never created).
                    self.assertNotIn("executionCwd", snapshot)
                    self.assertNotIn("worktreeStatus", snapshot)
                    self.assertNotIn("worktreeCleanupCommands", snapshot)
                    self.assertNotIn("branch", snapshot)
                finally:
                    self.delegate.create_persistent_worktree = original_create

    def test_partial_worktree_cleanup_uses_git_timeouts(self):
        """Failure-path cleanup must not run unbounded git subprocesses."""
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree_path = Path(temp_dir) / "partial-worktree"
            worktree_path.mkdir()
            run_path = Path(temp_dir) / "run"
            run_path.mkdir()
            completed = subprocess.CompletedProcess(["git"], 0, "", "")
            with mock.patch.object(self.delegate.subprocess, "run", return_value=completed) as run_mock:
                self.delegate._cleanup_partial_worktree(
                    "/repo",
                    str(worktree_path),
                    "delegate/cursor-partial",
                    run_path,
                    remove_branch=True,
                )

            self.assertEqual(run_mock.call_count, 2)
            for call in run_mock.call_args_list:
                self.assertEqual(
                    call.kwargs.get("timeout"),
                    self.delegate.GIT_MUTATION_TIMEOUT_SECONDS,
                )

    # -- Finding 4: Popen-launch failure after git worktree add succeeds --------

    def test_popen_failure_after_worktree_create_preserves_worktree(self):
        """Popen-launch failure after git worktree add succeeds preserves worktree
        and records run as failed with error captured."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()

                # Create a fake binary that exists but has a bad shebang
                # (this will cause Popen to fail after ensure_binary passes).
                bad_bin_dir = tempfile.TemporaryDirectory()
                self.addCleanup(bad_bin_dir.cleanup)
                bad_agent = Path(bad_bin_dir.name) / "agent"
                # Write a binary that fails after Popen (invalid shebang
                # causes OSError / ENOEXEC when subprocess tries to exec).
                bad_agent.write_text(
                    "#!/usr/bin/env bash_does_not_exist_xyz\n"
                    "echo 'this should never run'\n"
                )
                bad_agent.chmod(0o755)

                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(bad_agent), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(bad_bin_dir.name) + os.pathsep + os.environ.get("PATH", "")}):
                    # The execute_request should raise or propagate the error
                    # because Popen will fail (bad interpreter).
                    code, payload = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                    # The exit code will be non-zero because Popen failed.
                    # The code path catches the exception and writes a failed state
                    # while preserving the worktree.
                    self.assertNotEqual(code, 0)

                # Assert worktree directory is still present.
                worktree_root = Path(fake_home) / ".delegate" / "worktrees"
                worktrees_before = list(worktree_root.glob("*/*")) if worktree_root.exists() else []
                self.assertTrue(
                    len(worktrees_before) > 0,
                    "Worktree should be preserved even after Popen failure",
                )

                # Assert the run state is "failed" with exit code captured.
                registry_root = Path(repo.name) / ".delegate"
                runs_dir = registry_root / "runs"
                run_dirs = list(runs_dir.glob("del_*"))
                self.assertTrue(len(run_dirs) > 0)
                state = self.delegate.json.loads(
                    (run_dirs[0] / "state.json").read_text()
                )
                self.assertEqual(state.get("status"), "failed")
                self.assertIn("exitCode", state)
                self.assertIsNotNone(state["exitCode"])

    # -- Finding 3: Droid branch labels use alias, not resolved id -------------

    def test_droid_branch_uses_alias_not_resolved_id(self):
        """Droid persistent worktree branch label uses alias (e.g. qwen), not resolved model id."""
        from delegate_agent.isolation import branch_label, plan_branch_name, short_run_id
        # Verify that branch_label uses the alias, not the resolved id.
        alias = "qwen"
        resolved_id = "custom:OpenRouter-:-Qwen-3.7-Max-0"
        label_alias = branch_label("droid", alias)
        label_resolved = branch_label("droid", resolved_id)
        self.assertEqual(label_alias, "droid-qwen")
        # resolved_id produces a different slug
        self.assertNotEqual(label_alias, label_resolved)

    def test_droid_persistent_worktree_creates_alias_based_branch(self):
        """Droid persistent worktree creates a branch based on alias, not resolved model id."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                fake_bin = self.make_fake_bin()
                config = dict(self.delegate.DEFAULT_CONFIG)
                config["droid"] = dict(config["droid"])
                config["droid"]["models"] = {"qwen": "custom:OpenRouter-:-Qwen-3.7-Max-0"}
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "droid", "work", repo.name, config, model_alias="qwen",
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "droid"), "exec", "--cwd", repo.name,
                     "--model", "custom:OpenRouter-:-Qwen-3.7-Max-0",
                     "--output-format", "text", "hello"],
                    request.model, model_alias="qwen", dry_run=False,
                    workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=config,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                # Verify branch contains "droid-qwen" not the resolved id slug.
                branches = subprocess.run(
                    ["git", "-C", repo.name, "branch", "--list", "delegate/droid-*"],
                    capture_output=True, text=True, check=False,
                )
                branch_output = branches.stdout.strip()
                self.assertIn("droid-qwen-", branch_output)
                self.assertNotIn("custom", branch_output)
                self.assertNotIn("OpenRouter", branch_output)

    # -- Finding 5: Clean-source coverage --------------------------------------

    def test_persistent_worktree_dirty_staged_changes_fails(self):
        """Staged changes cause clean-source failure."""
        repo, _git_cd = self._make_git_repo_with_commit()
        staged = Path(repo.name) / "staged.txt"
        staged.write_text("staged\n")
        subprocess.run(
            ["git", "-C", repo.name, "add", "staged.txt"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                pass_through=False, completion_report_mode="markdown",
                source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "dirty_source_workspace")

    def test_persistent_worktree_dirty_unstaged_tracked_changes_fails(self):
        """Unstaged changes to tracked files cause clean-source failure."""
        repo, _git_cd = self._make_git_repo_with_commit()
        tracked = Path(repo.name) / "tracked.txt"
        tracked.write_text("initial\n")
        subprocess.run(
            ["git", "-C", repo.name, "add", "tracked.txt"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "-m", "add tracked"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        tracked.write_text("modified-but-not-staged\n")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                pass_through=False, completion_report_mode="markdown",
                source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "dirty_source_workspace")

    def test_persistent_worktree_dirty_submodule_fails(self):
        """Submodule dirtiness causes clean-source failure."""
        # Create a separate repo to use as the submodule source.
        sub_repo = tempfile.TemporaryDirectory()
        self.addCleanup(sub_repo.cleanup)
        subprocess.run(
            ["git", "-C", sub_repo.name, "init"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", sub_repo.name, *GIT_TEST_IDENTITY, "commit",
             "--allow-empty", "-m", "sub init"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        repo, _git_cd = self._make_git_repo_with_commit()
        # Add the submodule pointing to the separate repo.
        # protocol.file.allow=always needed on modern git for local file:// clones.
        subprocess.run(
            ["git", "-C", repo.name, "-c", "protocol.file.allow=always",
             *GIT_TEST_IDENTITY, "submodule", "add",
             sub_repo.name, "mysub"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Commit the submodule addition so the main repo is clean.
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit",
             "-m", "add submodule"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Modify a file inside the submodule to make it dirty.
        sub_file = Path(repo.name) / "mysub" / "dirty-in-sub.txt"
        sub_file.write_text("sub dirty\n")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                pass_through=False, completion_report_mode="markdown",
                source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "dirty_source_workspace")

    # ==========================================================================
    # Wave 3B: execution end-to-end acceptance tests
    # ==========================================================================

    # -- L836: Detached source HEAD persistent worktree -----------------------

    def test_persistent_worktree_detached_head_source(self):
        """Persistent worktree with detached source HEAD runs and
        records sourceHeadRef: null with 'integration target unknown' warning."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()

                # Detach HEAD: checkout the lone commit OID in detached state.
                oid = subprocess.run(
                    ["git", "-C", repo.name, "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip()
                subprocess.run(
                    ["git", "-C", repo.name, "checkout", "--detach", oid],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                # Confirm detached state: symbolic-ref must fail.
                sym_ref = subprocess.run(
                    ["git", "-C", repo.name, "symbolic-ref", "--quiet", "HEAD"],
                    capture_output=True, check=False,
                )
                self.assertNotEqual(sym_ref.returncode, 0,
                                    "HEAD should be detached")

                fake_bin = self.make_cursor_safe_fake_agent()
                workspace = self.delegate.resolve_workspace(repo.name)
                request = self._make_persistent_worktree_request(
                    "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                )
                request = self.delegate.Request(
                    request.engine, request.mode, request.workspace, request.prompt,
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text", "hello"],
                    request.model, dry_run=False, workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}):
                    code, payload = self.delegate.execute_request(
                        request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                        pass_through=False, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertEqual(code, 0, "Run should succeed even with detached HEAD")

                # Assert creationContext.sourceHeadRef is null in state.json
                registry_root = Path(repo.name) / ".delegate"
                runs_dir = registry_root / "runs"
                run_dirs = list(runs_dir.glob("del_*"))
                self.assertTrue(len(run_dirs) > 0, "No run directory found")
                state = self.delegate.json.loads(
                    (run_dirs[0] / "state.json").read_text()
                )
                cc = state.get("creationContext", {})
                self.assertIsNone(
                    cc.get("sourceHeadRef"),
                    "sourceHeadRef must be null when source HEAD is detached",
                )

                # Assert worktree_mgmt.show_worktree includes the warning
                alias = state.get("alias")
                self.assertIsNotNone(alias, "state must record alias")
                show_payload = self.delegate.worktree_mgmt.show_worktree(
                    registry_root, handle=alias,
                )
                self.assertIn("warnings", show_payload)
                self.assertIn(
                    "source was detached at creation; integration target unknown",
                    show_payload["warnings"],
                )

    # -- L837: Real git worktree add collision via pre-existing worktree --------

    def test_persistent_worktree_add_collision_fails_and_cleans_up(self):
        """Real 'git worktree add' collision surfaces as the creation error
        with git stderr surfaced, and no partial artifacts remain.

        Pre-checkout the predicted delegate branch in another git worktree
        so that `git worktree add -b` in create_persistent_worktree fails
        with either branch_collision or worktree_create_failed depending on
        how the code detects the collision.
        """
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()

                # Monkey-patch generate_run_id so we know the predicted branch.
                fixed_run_id = "del_20250101T000000Z_abcdef"
                with mock.patch.object(
                    self.delegate.run_registry, "generate_run_id",
                    return_value=fixed_run_id,
                ):
                    from delegate_agent.isolation import (
                        short_run_id, branch_label, plan_branch_name,
                    )
                    short_id = short_run_id(fixed_run_id)
                    label = branch_label("cursor", None)
                    predicted_branch = plan_branch_name(label, short_id)

                    # Pre-checkout the predicted branch in a separate
                    # worktree so git refuses to create it.
                    branch_wt_path = tempfile.mkdtemp()
                    # Create the branch reference first; then check it out.
                    subprocess.run(
                        ["git", "-C", repo.name, "branch", "--no-track",
                         predicted_branch, "HEAD"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    subprocess.run(
                        ["git", "-C", repo.name, "worktree", "add",
                         branch_wt_path, predicted_branch],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    # Clean up the blocking worktree afterwards.
                    self.addCleanup(lambda: subprocess.run(
                        ["git", "-C", repo.name, "worktree", "remove", "--force",
                         branch_wt_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    ))

                    fake_bin = self.make_fake_bin()
                    workspace = self.delegate.resolve_workspace(repo.name)
                    request = self._make_persistent_worktree_request(
                        "cursor", "work", repo.name, self.delegate.DEFAULT_CONFIG,
                    )
                    request = self.delegate.Request(
                        request.engine, request.mode, request.workspace, request.prompt,
                        [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                         "--model", "composer-2.5", "--output-format", "text", "hello"],
                        request.model, dry_run=False, workspace_kind=request.workspace_kind,
                        isolation_context=request.isolation_context,
                    )
                    with self.assertRaises(self.delegate.DelegateError) as ctx:
                        self.delegate.execute_request(
                            request, json_mode=False, config=self.delegate.DEFAULT_CONFIG,
                            pass_through=False, completion_report_mode="none",
                            source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                        )

                    # Accept either error code: branch_collision is the
                    # pre-launch check in create_persistent_worktree, while
                    # worktree_create_failed would come from the actual
                    # git worktree add failure.
                    self.assertIn(
                        ctx.exception.error,
                        ["worktree_create_failed", "branch_collision"],
                        f"Expected git-collision error, got {ctx.exception.error}",
                    )
                    # Git stderr text must be present in the error message.
                    self.assertTrue(
                        len(ctx.exception.message) > 0,
                        "Error message should not be empty",
                    )

                    # Assert NO partial branch/worktree was left behind
                    # by the delegate run (the pre-existing branch and
                    # worktree are not delegate artifacts).
                    data_home = Path(fake_home) / ".delegate" / "worktrees"
                    if data_home.exists():
                        cursor_dirs = list(data_home.rglob("cursor-*"))
                        self.assertEqual(
                            len(cursor_dirs), 0,
                            "No delegate worktree directories should exist after a "
                            "failed creation",
                        )

    # -- L841: safe + worktree + pass-through cleanup on child failure ---------

    def test_safe_worktree_passthrough_cleans_up_on_child_failure(self):
        """--pass-through --isolation worktree cursor safe cleans up temp
        worktree even when the child exits non-zero."""
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                repo, _git_cd = self._make_git_repo_with_commit()
                # Fake binary that exits non-zero (child failure).
                fake_bin = self.make_cursor_safe_fake_agent()
                config = dict(self.delegate.DEFAULT_CONFIG)
                workspace = self.delegate.resolve_workspace(repo.name)
                git_root, git_common_dir, head_oid, head_ref, branch_name = (
                    self.delegate.capture_git_metadata(repo.name)
                )
                effective = self.delegate.delegate_config.resolve_isolation(
                    cli_value="worktree", loaded_config=config, engine="cursor", mode="safe",
                )
                isolation_context = self.delegate.build_isolation_context(
                    source_workspace=workspace.path,
                    resolved_isolation=effective,
                    engine="cursor", mode="safe",
                    config=config, run_short_id=None,
                    source_git_root=git_root, source_git_common_dir=git_common_dir,
                    source_head_oid=head_oid, source_head_ref=head_ref,
                    source_branch=branch_name,
                )
                request = self.delegate.Request(
                    "cursor", "safe", repo.name,
                    self.delegate.prefix_cursor_safe_prompt(
                        self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"),
                    [str(fake_bin / "agent"), "--workspace", repo.name, "-p", "--trust",
                     "--model", "composer-2.5", "--output-format", "text",
                     self.delegate.prefix_cursor_safe_prompt(
                         self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello")],
                    "composer-2.5", workspace_kind="git",
                    isolation_context=isolation_context,
                )
                temp_dirs_before = safe_temp_dirs()
                branches_before = subprocess.run(
                    ["git", "-C", repo.name, "branch", "--list", "delegate/*"],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()

                with mock.patch.dict(os.environ, {
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "FAKE_EXIT": "7",
                }):
                    code, _ = self.delegate.execute_request(
                        request, json_mode=False, config=config,
                        pass_through=True, completion_report_mode="none",
                        source_workspace=workspace, stdout=io.StringIO(), stderr=io.StringIO(),
                    )

                # 1. Child exit code reflects failure.
                self.assertNotEqual(code, 0,
                                    "Child non-zero exit must propagate")

                # 2. Temporary worktree directory must NOT exist after exit
                #    (cleaned up by the finally block).
                self.assertEqual(
                    safe_temp_dirs() - temp_dirs_before, set(),
                    "No new delegate-safe-* temp dirs should remain after cleanup",
                )

                # 3. The branch the temporary worktree used must NOT exist
                #    (safe-mode worktrees are detached, no branch created).
                branches_after = subprocess.run(
                    ["git", "-C", repo.name, "branch", "--list", "delegate/*"],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                self.assertEqual(
                    branches_before, branches_after,
                    "No delegate/* branches should be created by safe mode",
                )


if __name__ == "__main__":
    unittest.main()
