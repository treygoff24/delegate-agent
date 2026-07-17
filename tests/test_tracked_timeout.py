import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from tests.execution_test_base import ExecutionTestBase, make_git_repo


class TrackedTimeoutTests(ExecutionTestBase):
    def make_sleeping_fake_bin(self, *names: str):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        for name in names:
            path = bin_dir / name
            path.write_text("#!/usr/bin/env bash\nsleep 60\n")
            path.chmod(0o755)
        return bin_dir

    def make_partial_output_fake_bin(self, *names: str):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        for name in names:
            path = bin_dir / name
            path.write_text(
                "#!/usr/bin/env bash\n"
                'printf \'%s\\n\' \'{"type":"tool_call","tool":"Read",'
                '"path":"partial.txt"}\'\n'
                'printf \'%s\\n\' \'{"type":"message","role":"assistant",'
                '"content":"partial timeout answer"}\'\n'
                "printf 'partial timeout stderr\\n' >&2\n"
                "sleep 60\n"
            )
            path.chmod(0o755)
        return bin_dir

    # -- Ordinary tracked execution (cli.py execute_tracked call site) --------

    def test_safe_tracked_timeout_terminates_child_and_fails_run(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        fake_bin = self.make_partial_output_fake_bin("droid")
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self.delegate.Request(
            "droid",
            "safe",
            repo.name,
            "hello",
            ["droid", "exec", "--cwd", repo.name, "--model", "model-id", "hello"],
            "model-id",
            timeout=1,
        )
        with (
            mock.patch.dict(os.environ, {"PATH": env_path}),
            self.assertRaises(self.delegate.DelegateError) as ctx,
        ):
            self.delegate.execute_request(
                request,
                json_mode=True,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        # Historical error-code name, kept for API stability across modes.
        self.assertEqual(ctx.exception.error, "call_timeout")
        self.assertEqual(ctx.exception.exit_code, 1)
        registry_root = Path(repo.name) / ".delegate"
        run_dirs = list((registry_root / "runs").glob("del_*"))
        self.assertTrue(run_dirs)
        state = json.loads((run_dirs[0] / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "failed")
        self.assertEqual(state.get("error"), "call_timeout")
        self.assertEqual(state.get("message"), "Child command exceeded the configured timeout.")
        self.assertEqual(state.get("exitCode"), 1)
        self.assertIn("finishedAt", state)
        self.assertGreater(state.get("stdoutBytes", 0), 0)
        self.assertGreater(state.get("stderrBytes", 0), 0)
        self.assertIn("partial timeout answer", state.get("current", ""))
        self.assertEqual(state.get("resultQuality"), "ok")
        self.assertTrue(state.get("completionReportWritten"))
        self.assertEqual(state.get("completionReportSource"), "delegate_synthesized")

        snapshot = json.loads((run_dirs[0] / "snapshot.json").read_text(encoding="utf-8"))
        self.assertFalse(snapshot.get("ok"))
        self.assertEqual(snapshot.get("status"), "failed")
        self.assertEqual(snapshot.get("exitCode"), 1)
        self.assertEqual(snapshot.get("resultQuality"), "ok")
        self.assertIn("partial timeout answer", snapshot.get("assistantText", ""))
        self.assertTrue(snapshot.get("recentEvents"))
        self.assertTrue(snapshot.get("completionReportWritten"))
        report = (run_dirs[0] / "completion-report.md").read_text(encoding="utf-8")
        self.assertIn("Failure reason: call_timeout", report)

    def test_safe_tracked_timeout_cli_exits_one(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        fake_bin = self.make_sleeping_fake_bin("agent")
        with tempfile.TemporaryDirectory() as home:
            config_path = Path(home) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            env = os.environ.copy()
            env.pop("AI_PROFILE", None)
            env.update(
                {
                    "DELEGATE_CONFIG": str(config_path),
                    "HOME": home,
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                }
            )
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True):
                code = self.delegate.main(
                    [
                        "--json",
                        "--cwd",
                        repo.name,
                        "cursor",
                        "safe",
                        "--timeout",
                        "1",
                        "hello",
                    ],
                    stdout=stdout,
                    stderr=io.StringIO(),
                )

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "call_timeout")

    # -- Persistent worktree execution (worktree_execution call site) ---------

    def test_persistent_worktree_work_timeout_terminates_child_and_fails_run(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_sleeping_fake_bin("agent")
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [str(fake_bin / "agent"), "--workspace", repo.name, "hello"],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
                timeout=1,
            )
            with (
                mock.patch.dict(
                    os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
                ),
                self.assertRaises(self.delegate.DelegateError) as ctx,
            ):
                self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(ctx.exception.error, "call_timeout")
            self.assertEqual(ctx.exception.exit_code, 1)
            registry_root = Path(repo.name) / ".delegate"
            run_dirs = list((registry_root / "runs").glob("del_*"))
            self.assertTrue(run_dirs)
            state = json.loads((run_dirs[0] / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state.get("status"), "failed")
            self.assertEqual(state.get("error"), "call_timeout")

    def test_persistent_worktree_timeout_preserves_realized_worktree_metadata(self):
        # The worktree and branch are created before the child launches, so a
        # child-launch failure (here: timeout) must keep the realized metadata
        # for `delegate worktree show|remove` instead of erasing it.
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_sleeping_fake_bin("agent")
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [str(fake_bin / "agent"), "--workspace", repo.name, "hello"],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
                timeout=1,
            )
            with (
                mock.patch.dict(
                    os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
                ),
                self.assertRaises(self.delegate.DelegateError) as ctx,
            ):
                self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(ctx.exception.error, "call_timeout")
            self.assertEqual(ctx.exception.exit_code, 1)
            registry_root = Path(repo.name) / ".delegate"
            run_dirs = list((registry_root / "runs").glob("del_*"))
            self.assertTrue(run_dirs)
            state = json.loads((run_dirs[0] / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state.get("status"), "failed")
            self.assertEqual(state.get("error"), "call_timeout")
            self.assertEqual(state.get("worktreeStatus"), "present")
            snapshot = json.loads((run_dirs[0] / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot.get("status"), "failed")
            self.assertFalse(snapshot.get("ok"))
            self.assertEqual(snapshot.get("exitCode"), 1)
            self.assertEqual(snapshot.get("resultQuality"), "ok")
            self.assertTrue(snapshot.get("completionReportWritten"))
            self.assertEqual(snapshot.get("completionReportSource"), "delegate_synthesized")
            self.assertIn("finishedAt", state)
            self.assertTrue(state.get("completionReportWritten"))
            self.assertEqual(state.get("completionReportSource"), "delegate_synthesized")
            self.assertEqual(snapshot.get("worktreeStatus"), "present")
            execution_cwd = snapshot.get("executionCwd")
            self.assertIsInstance(execution_cwd, str)
            # The realized worktree still exists on disk for operator cleanup.
            self.assertTrue(Path(execution_cwd).is_dir())
            self.assertIsInstance(snapshot.get("branch"), str)
            self.assertTrue(snapshot.get("branch"))
            cleanup_commands = snapshot.get("worktreeCleanupCommands")
            self.assertIsInstance(cleanup_commands, dict)
            self.assertIn("safe", cleanup_commands)

    # -- --timeout + --pass-through rejection ---------------------------------

    def test_grouped_call_timeout_preserves_runtime_exit_code(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        fake_bin = self.make_sleeping_fake_bin("droid")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self.delegate.Request(
            "droid",
            "call",
            repo.name,
            "hello",
            [str(fake_bin / "droid"), "hello"],
            "model-id",
            timeout=1,
            group="timeout-test",
        )

        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request,
                json_mode=True,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(ctx.exception.error, "call_timeout")
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_timeout_rejected_with_pass_through(self):
        for argv in (
            ["--pass-through", "cursor", "safe", "--timeout", "5", "x"],
            ["--pass-through", "cursor", "work", "--timeout", "5", "x"],
            ["--pass-through", "cursor", "call", "--timeout", "5", "x"],
            ["--pass-through", "droid", "work", "--timeout", "5", "x"],
        ):
            with self.subTest(argv=argv), self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.parse_cli(argv)
            self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_input_json_timeout_rejected_with_pass_through(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        for mode in ("safe", "work"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                task = Path(tmp) / "task.json"
                task.write_text(
                    json.dumps(
                        {
                            "engine": "cursor",
                            "mode": mode,
                            "cwd": repo.name,
                            "prompt": "hello",
                            "timeout": 30,
                        }
                    )
                )
                parsed = self.delegate.ParsedCommand(
                    "run",
                    global_options=self.delegate.GlobalOptions(pass_through=True),
                    run_json=self.delegate.RunJsonOptions(str(task)),
                )
                with self.assertRaises(self.delegate.DelegateError) as ctx:
                    self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
                self.assertEqual(ctx.exception.error, "invalid_option_combination")

    # -- Acceptance: timeout flows into safe/work requests --------------------

    def test_cli_timeout_flows_into_safe_and_work_requests(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        for mode in ("safe", "work"):
            with self.subTest(mode=mode):
                parsed = self.delegate.parse_cli(
                    ["--cwd", repo.name, "cursor", mode, "--timeout", "30", "hello"]
                )
                request = self.delegate.request_from_parsed(
                    parsed, self.delegate.DEFAULT_CONFIG, io.StringIO("")
                )
                self.assertEqual(request.mode, mode)
                self.assertEqual(request.timeout, 30)

    def test_input_json_timeout_accepted_for_safe_and_work(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        for mode in ("safe", "work"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                task = Path(tmp) / "task.json"
                task.write_text(
                    json.dumps(
                        {
                            "engine": "cursor",
                            "mode": mode,
                            "cwd": repo.name,
                            "prompt": "hello",
                            "timeout": 30,
                        }
                    )
                )
                parsed = self.delegate.ParsedCommand(
                    "run",
                    global_options=self.delegate.GlobalOptions(json_mode=True),
                    run_json=self.delegate.RunJsonOptions(str(task)),
                )
                request = self.delegate.request_from_input_json(
                    parsed, self.delegate.DEFAULT_CONFIG
                )
                self.assertEqual(request.mode, mode)
                self.assertEqual(request.timeout, 30)
