import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from tests.execution_test_base import (
    GIT_TEST_IDENTITY,
    MODULE_PATH,
    SCRIPT_PATH,
    ExecutionTestBase,
    make_git_repo,
    safe_temp_dirs,
)


class ExecutionArgvAndPromptTests(ExecutionTestBase):
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
        parsed = self.delegate.ParsedCommand(
            "run",
            global_options=self.delegate.GlobalOptions(json_mode=True),
            run_json=self.delegate.RunJsonOptions(str(task)),
        )
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
                str(SCRIPT_PATH),
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
            [sys.executable, str(SCRIPT_PATH), "--cwd", repo.name, "cursor", "safe", "review"],
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
                [sys.executable, str(SCRIPT_PATH), "--cwd", workspace, "cursor", "safe", "review"],
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

    def make_claude_safe_fake(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "claude"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "touch mutated-by-claude.txt\n"
            'prompt="$(cat)"\n'
            'printf \'%s\\n\' \'{"type":"system","cwd":"fake"}\'\n'
            'printf \'{"type":"assistant","message":{"content":[{"type":"text","text":"saw stdin: %s"}]}}\\n\' "$prompt"\n'
            'printf \'%s\\n\' \'{"type":"result","subtype":"success","result":"Status: completed\\\\n- final from claude"}\'\n'
            "exit 0\n",
            encoding="utf-8",
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

    def test_codex_safe_argv_never_emits_bypass_flags_even_if_policy_sets_them(self):
        # Config validation rejects bypass flags under safe mode, but the argv
        # builder must also refuse to emit them structurally — safe mode stays
        # read-only no matter what a policy dict carries.
        argv = self.delegate.build_codex_argv(
            self.delegate.DEFAULT_CONFIG["codex"],
            "safe",
            "/repo",
            None,
            "review only",
            {
                "bypassApprovalsAndSandbox": True,
                "bypassHookTrust": True,
            },
            workspace_kind="git",
        )
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--dangerously-bypass-hook-trust", argv)
        self.assertIn("--ask-for-approval", argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("read-only", argv)

    def test_claude_safe_default_argv_uses_plan_permissions_and_stdin(self):
        argv = self.delegate.build_claude_argv(
            self.delegate.DEFAULT_CONFIG["claude"],
            "safe",
            "claude-opus-4-8",
            {"bypassApprovalsAndSandbox": True},
            stream_capture=True,
            reasoning_effort="xhigh",
        )
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--input-format", argv)
        self.assertIn("text", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("plan", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--tools", argv)
        tools = argv[argv.index("--tools") + 1]
        self.assertIn("Read", tools)
        self.assertIn("Grep", tools)
        self.assertIn("Glob", tools)
        self.assertIn("Bash", tools)
        self.assertIn("--allowedTools", argv)
        allowed_tools = argv[argv.index("--allowedTools") + 1]
        self.assertIn("Bash(git diff:*)", allowed_tools)
        self.assertIn("Bash(git status:*)", allowed_tools)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--model", argv)
        self.assertIn("claude-opus-4-8", argv)
        self.assertIn("--effort", argv)
        self.assertIn("xhigh", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_claude_work_does_not_bypass_from_global_policy(self):
        argv = self.delegate.build_claude_argv(
            self.delegate.DEFAULT_CONFIG["claude"],
            "work",
            None,
            {"bypassApprovalsAndSandbox": True},
        )
        self.assertIn("--permission-mode", argv)
        self.assertIn("auto", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_claude_work_uses_harness_scoped_policy_bypass(self):
        argv = self.delegate.build_claude_argv(
            self.delegate.DEFAULT_CONFIG["claude"],
            "work",
            None,
            {"bypassApprovalsAndSandbox": True},
            allow_bypass_permissions=True,
        )
        self.assertIn("--permission-mode", argv)
        self.assertIn("bypassPermissions", argv)

    def test_claude_work_external_sandbox_profile_does_not_bypass(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["policy"]["profile"] = "external-sandbox"
        request = self.build_git_request(
            "claude",
            "work",
            None,
            "/repo",
            "ship",
            config,
            dry_run=True,
        )
        self.assertIn("--permission-mode", request.argv)
        self.assertIn("auto", request.argv)
        self.assertNotIn("bypassPermissions", request.argv)

    def test_claude_describe_runtime_bypass_no_drift(self):
        def assert_bypass(config, expected):
            runtime = self.delegate._claude_runtime_policy(config, "work")
            harness = self.delegate._claude_harness_bypass_enabled(config, "work")
            self.assertEqual(runtime["bypassApprovalsAndSandbox"], expected)
            self.assertEqual(harness, expected)
            self.assertEqual(runtime["bypassApprovalsAndSandbox"], harness)

        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        assert_bypass(config, False)

        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config.setdefault("policy", {})
        config["policy"].setdefault("harness", {})
        config["policy"]["harness"].setdefault("claude", {})
        config["policy"]["harness"]["claude"]["work"] = {
            "bypassApprovalsAndSandbox": True,
        }
        assert_bypass(config, True)

        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config.setdefault("policy", {})
        config["policy"]["profile"] = "external-sandbox"
        config["policy"].setdefault("work", {})
        config["policy"]["work"]["bypassApprovalsAndSandbox"] = True
        assert_bypass(config, False)

    def test_claude_work_harness_policy_allows_bypass(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["policy"]["harness"] = {"claude": {"work": {"bypassApprovalsAndSandbox": True}}}
        request = self.build_git_request(
            "claude",
            "work",
            None,
            "/repo",
            "ship",
            config,
            dry_run=True,
        )
        self.assertIn("--permission-mode", request.argv)
        self.assertIn("bypassPermissions", request.argv)

    def test_claude_config_rejects_bypass_permission_mode(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["claude"]["workPermissionMode"] = "bypassPermissions"
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_claude_config")
        self.assertIn(
            "policy.harness.claude.work.bypassApprovalsAndSandbox",
            ctx.exception.message,
        )

    def test_claude_request_uses_stdin_transport_without_prompt_in_argv(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["claude"]["defaultModel"] = "claude-sonnet-4-6"
        request = self.build_git_request(
            "claude",
            "safe",
            None,
            "/repo",
            "SECRET CLAUDE PROMPT",
            config,
            dry_run=True,
            reasoning_effort="high",
            reasoning_effort_source="cli",
        )
        self.assertEqual(request.prompt_transport, self.delegate.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(request.stdin_text, "SECRET CLAUDE PROMPT")
        self.assertNotIn("SECRET CLAUDE PROMPT", request.argv)
        self.assertEqual(request.reasoning_effort, "high")
        self.assertEqual(request.reasoning_transport, "claude-effort-flag")
        self.assertEqual(request.reasoning_capability_source, "static")
        self.assertIn("--effort", request.argv)
        self.assertIn("high", request.argv)

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
                str(SCRIPT_PATH),
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

    def test_claude_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_claude_safe_fake()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(self.delegate.DEFAULT_CONFIG))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        temp_dirs_before = safe_temp_dirs()

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                repo.name,
                "--json",
                "claude",
                "safe",
                "review",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse((Path(repo.name) / "mutated-by-claude.txt").exists())
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        self.assertEqual(safe_temp_dirs() - temp_dirs_before, set())

    def make_kimi_safe_fake(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "kimi"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "touch mutated-by-kimi.txt\n"
            "printf 'OUT:%s\\n' \"$*\"\n"
            'exit "${FAKE_EXIT:-0}"\n'
        )
        path.chmod(0o755)
        return bin_dir

    def test_kimi_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_kimi_safe_fake()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(self.delegate.DEFAULT_CONFIG))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        temp_dirs_before = safe_temp_dirs()

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                repo.name,
                "--json",
                "kimi",
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
        self.assertFalse((Path(repo.name) / "mutated-by-kimi.txt").exists())
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        self.assertEqual(safe_temp_dirs() - temp_dirs_before, set())

    def test_effective_prompt_codex_safe_order(self):
        user = "review the diff"
        p = self.delegate.effective_prompt(
            user, engine="codex", mode="safe", completion_report_mode="markdown"
        )
        self.assertIn("Delegate sub-agent skill review", p)
        self.assertIn("safe/read-only mode", p)
        self.assertIn("must not override the read-only requirement", p)
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

    def test_effective_prompt_claude_safe_order_and_idempotence(self):
        once = self.delegate.effective_prompt(
            "review the diff",
            engine="claude",
            mode="safe",
            completion_report_mode="none",
        )
        twice = self.delegate.effective_prompt(
            once,
            engine="claude",
            mode="safe",
            completion_report_mode="none",
        )
        claude_idx = once.find("Delegate Claude safe mode")
        user_idx = once.find("review the diff")
        self.assertGreater(claude_idx, 0)
        self.assertGreater(user_idx, claude_idx)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("Delegate Claude safe mode"), 1)

    def test_effective_prompt_droid_safe_order_and_idempotence(self):
        once = self.delegate.effective_prompt(
            "review the diff",
            engine="droid",
            mode="safe",
            completion_report_mode="none",
        )
        twice = self.delegate.effective_prompt(
            once,
            engine="droid",
            mode="safe",
            completion_report_mode="none",
        )
        droid_idx = once.find("Delegate Droid safe mode")
        user_idx = once.find("review the diff")
        self.assertGreater(droid_idx, 0)
        self.assertGreater(user_idx, droid_idx)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("Delegate Droid safe mode"), 1)

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

    def test_missing_binary_error_includes_config_fix_diagnostics(self):
        config_path = "/tmp/delegate-config.json"
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.ensure_binary(
                ["delegate-definitely-missing-claude", "-p"],
                engine="claude",
                config_source=config_path,
            )
        error = ctx.exception
        self.assertEqual(error.error, "missing_binary")
        self.assertEqual(error.exit_code, self.delegate.EXIT_MISSING_BINARY)
        self.assertIn("searched PATH of the delegate process", error.message)
        self.assertIn("claude.binary", error.message)
        self.assertEqual(error.diagnostics["configPath"], config_path)
        self.assertEqual(error.diagnostics["configKey"], "claude.binary")

    def test_missing_binary_json_includes_candidate_path(self):
        with tempfile.TemporaryDirectory() as home:
            config_path = Path(home) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "kimi": {"binary": "delegate-test-kimi"},
                        "isolation": {"safe": "auto"},
                    }
                ),
                encoding="utf-8",
            )
            candidate_dir = Path(home) / ".kimi-code" / "bin"
            candidate_dir.mkdir(parents=True)
            candidate = candidate_dir / "delegate-test-kimi"
            candidate.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            candidate.chmod(0o755)
            empty_path = Path(home) / "empty-path"
            empty_path.mkdir()
            stdout_buf = io.StringIO()

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": home,
                    "DELEGATE_CONFIG": str(config_path),
                    "PATH": str(empty_path),
                },
            ):
                code = self.delegate.main(
                    ["--json", "--cwd", home, "kimi", "safe", "hello"],
                    stdout=stdout_buf,
                )

        payload = json.loads(stdout_buf.getvalue())
        self.assertEqual(code, self.delegate.EXIT_MISSING_BINARY)
        self.assertEqual(payload["error"], "missing_binary")
        self.assertEqual(payload["configPath"], str(config_path))
        self.assertEqual(payload["configKey"], "kimi.binary")
        self.assertEqual(payload["suggestedBinaryPath"], str(candidate))
        self.assertIn(str(candidate), payload["message"])


if __name__ == "__main__":
    import unittest

    unittest.main()
