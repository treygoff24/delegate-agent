import contextlib
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests.execution_test_base import ExecutionTestBase


class ExecutionDryRunTests(ExecutionTestBase):
    def test_call_dry_run_reports_temporary_call_cwd_not_source_workspace(self):
        parsed = self.delegate.parse_cli(["--json", "dry-run", "codex", "call", "summarize"])
        request = self.delegate.request_from_parsed(
            parsed,
            self.delegate.DEFAULT_CONFIG,
            io.StringIO(""),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["mode"], "call")
        self.assertEqual(payload["cwd"], "<delegate-call-temp-cwd>")
        self.assertEqual(payload["isolation"], "call temporary cwd")
        self.assertEqual(payload["effectiveIsolation"], "none")
        self.assertFalse(payload["isolatedWorkspace"])

    def test_codex_safe_dry_run_reports_isolated_workspace(self):
        request = self.build_git_request(
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

    def test_dry_run_reports_reasoning_fields(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"] = dict(config["codex"])
        config["codex"]["defaultModel"] = "gpt-5.5"
        with tempfile.TemporaryDirectory() as tmp:
            parsed = self.delegate.parse_cli(
                [
                    "--cwd",
                    tmp,
                    "--json",
                    "dry-run",
                    "codex",
                    "safe",
                    "--reasoning-effort",
                    "high",
                    "review",
                ]
            )
            request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
            payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["requestedReasoningEffort"], "high")
        self.assertEqual(payload["resolvedReasoningEffort"], "high")
        self.assertEqual(payload["reasoningTransport"], "codex-config")
        self.assertEqual(payload["reasoningEffortSource"], "cli")
        self.assertEqual(payload["reasoningCapabilitySource"], "bundled")
        self.assertIn('model_reasoning_effort="high"', payload["argv"])

    def test_dry_run_reports_explicit_fast_but_not_inherited_fast(self):
        explicit = self.build_git_request(
            "codex",
            "safe",
            None,
            "/repo",
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
            fast=False,
        )
        inherited = self.build_git_request(
            "codex",
            "safe",
            None,
            "/repo",
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        self.assertIs(self.delegate.dry_run_payload(explicit)["requestedFast"], False)
        self.assertNotIn("requestedFast", self.delegate.dry_run_payload(inherited))

    def test_dry_run_reports_progress_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = self.delegate.parse_cli(
                ["--cwd", tmp, "--json", "dry-run", "codex", "safe", "--progress", "review"]
            )
            request = self.delegate.request_from_parsed(
                parsed,
                self.delegate.DEFAULT_CONFIG,
                io.StringIO(""),
            )
            payload = self.delegate.dry_run_payload(request)
        self.assertTrue(payload["progressRequested"])

    def test_dry_run_codex_output_schema_uses_launch_cwd_absolute_path(self):
        with (
            tempfile.TemporaryDirectory() as launch_dir,
            tempfile.TemporaryDirectory() as repo_dir,
        ):
            subprocess.run(
                ["git", "-C", repo_dir, "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            nested = Path(repo_dir) / "nested"
            nested.mkdir()
            schema = Path(launch_dir) / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            with contextlib.chdir(launch_dir):
                parsed = self.delegate.parse_cli(
                    [
                        "--cwd",
                        str(nested),
                        "--json",
                        "dry-run",
                        "codex",
                        "safe",
                        "--output-schema",
                        "schema.json",
                        "review",
                    ]
                )
                request = self.delegate.request_from_parsed(
                    parsed,
                    self.delegate.DEFAULT_CONFIG,
                    io.StringIO(""),
                )
            payload = self.delegate.dry_run_payload(request)
        argv = payload["argv"]
        schema_index = argv.index("--output-schema")
        self.assertGreater(schema_index, argv.index("exec"))
        self.assertEqual(argv[schema_index + 1], str(schema.resolve()))
        self.assertEqual(argv[argv.index("--cd") + 1], str(Path(repo_dir).resolve()))

    def test_dry_run_reports_forbid_commit_policy_for_persistent_worktree(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
            tempfile.TemporaryDirectory() as repo_dir,
        ):
            subprocess.run(
                ["git", "-C", repo_dir, "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.name", "Test"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.email", "test@example.com"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "commit", "--allow-empty", "-m", "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            parsed = self.delegate.parse_cli(
                [
                    "--cwd",
                    repo_dir,
                    "--isolation",
                    "worktree",
                    "--json",
                    "dry-run",
                    "cursor",
                    "work",
                    "--forbid-commit",
                    "fix",
                ]
            )
            request = self.delegate.request_from_parsed(
                parsed,
                self.delegate.DEFAULT_CONFIG,
                io.StringIO(""),
            )
            payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["commitPolicy"], {"forbidCommit": True})

    def test_forbid_commit_requires_persistent_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = self.delegate.parse_cli(
                ["--cwd", tmp, "--json", "dry-run", "cursor", "work", "--forbid-commit", "fix"]
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_parsed(
                    parsed,
                    self.delegate.DEFAULT_CONFIG,
                    io.StringIO(""),
                )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("not a Git repo", ctx.exception.message)

    def test_git_forbid_commit_without_worktree_names_fix(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            subprocess.run(
                ["git", "-C", repo_dir, "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.parse_cli(
                    [
                        "--cwd",
                        repo_dir,
                        "--isolation",
                        "none",
                        "--json",
                        "dry-run",
                        "cursor",
                        "work",
                        "--forbid-commit",
                        "fix",
                    ]
                )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("Corrected command:", ctx.exception.message)
        self.assertIn("--isolation worktree", ctx.exception.message)

    def test_codex_reasoning_without_model_uses_harness_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = self.delegate.parse_cli(
                [
                    "--cwd",
                    tmp,
                    "--json",
                    "dry-run",
                    "codex",
                    "safe",
                    "--reasoning-effort",
                    "high",
                    "review",
                ]
            )
            request = self.delegate.request_from_parsed(
                parsed,
                self.delegate.DEFAULT_CONFIG,
                io.StringIO(""),
            )
        payload = self.delegate.dry_run_payload(request)
        self.assertIsNone(payload["model"])
        self.assertIn('model_reasoning_effort="high"', payload["argv"])
        self.assertEqual(payload["reasoningCapabilitySource"], "harness-default")

    def test_forbid_commit_rejects_safe_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = self.delegate.parse_cli(
                [
                    "--cwd",
                    tmp,
                    "--isolation",
                    "worktree",
                    "--json",
                    "dry-run",
                    "cursor",
                    "safe",
                    "--forbid-commit",
                    "review",
                ]
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_parsed(
                    parsed,
                    self.delegate.DEFAULT_CONFIG,
                    io.StringIO(""),
                )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    # -- Wave 2: dry-run structured isolation fields --------------------------------

    def test_dry_run_cursor_safe_includes_structured_isolation_fields(self):
        request = self.build_git_request(
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
        request = self.build_git_request(
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
        request = self.build_git_request(
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
        request = self.build_git_request(
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
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
            tempfile.TemporaryDirectory() as repo_dir,
        ):
            subprocess.run(
                ["git", "-C", repo_dir, "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.name", "Test"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.email", "test@example.com"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "commit", "--allow-empty", "-m", "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _config, _ = self.delegate.load_config()
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
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(branch_result.stdout.strip(), "")
            # Verify no .delegate/ registry was written in source workspace
            self.assertFalse((Path(repo_dir) / ".delegate").exists())

    def test_dry_run_isolation_worktree_codex_creates_no_artifacts(self):
        """Assert dry-run with codex+isolation worktree creates NO filesystem entries
        (no worktree dir, no branches, no registry)."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
            tempfile.TemporaryDirectory() as repo_dir,
        ):
            subprocess.run(
                ["git", "-C", repo_dir, "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.name", "Test"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.email", "test@example.com"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "commit", "--allow-empty", "-m", "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(branch_result.stdout.strip(), "")
            # Verify no .delegate/ registry was written in source workspace
            self.assertFalse((Path(repo_dir) / ".delegate").exists())

    # -- Finding A: isolatedWorkspace always emitted as explicit boolean ----------

    def test_dry_run_isolated_workspace_contract_by_harness_mode(self):
        """Work mode stays in place; all safe harnesses with auto isolation are isolated."""
        droid_config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        droid_config["droid"]["models"] = {"test-model": "real-model-id"}
        cases = (
            ("cursor", "work", None, self.delegate.DEFAULT_CONFIG, False),
            ("cursor", "safe", None, self.delegate.DEFAULT_CONFIG, True),
            ("droid", "safe", "test-model", droid_config, True),
        )

        for engine, mode, model_alias, config, expected in cases:
            with self.subTest(engine=engine, mode=mode):
                request = self.build_git_request(
                    engine,
                    mode,
                    model_alias,
                    "/repo",
                    "hello",
                    config,
                    dry_run=True,
                )
                payload = self.delegate.dry_run_payload(request)
                self.assertIn("isolatedWorkspace", payload)
                self.assertIs(payload["isolatedWorkspace"], expected)

    def test_dry_run_isolation_worktree_isolated_workspace_true(self):
        """--isolation worktree cursor work dry-run JSON has isolatedWorkspace: true."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.parsed_launch(
            "cursor",
            cwd=repo.name,
            engine="cursor",
            mode="work",
            prompt_parts=["hello"],
            dry_run=True,
            isolation="worktree",
        )
        request = self.delegate.request_from_parsed(
            parsed,
            self.delegate.DEFAULT_CONFIG,
            io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("isolatedWorkspace", payload)
        self.assertTrue(payload["isolatedWorkspace"])

    def test_dry_run_worktree_planned_paths_not_placeholders_cursor(self):
        """Cursor work --isolation worktree dry-run via request_from_parsed yields
        concrete planned paths (real fingerprint + label), not the literal
        '<planned-worktree-path>' or '<planned-branch>' sentinel strings.
        The run-id may still be a placeholder since no run is allocated."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.parsed_launch(
            "cursor",
            cwd=repo.name,
            engine="cursor",
            mode="work",
            prompt_parts=["hello"],
            dry_run=True,
            isolation="worktree",
        )
        request = self.delegate.request_from_parsed(
            parsed,
            self.delegate.DEFAULT_CONFIG,
            io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIn("plannedExecutionCwd", payload)
        self.assertIsNotNone(payload["plannedExecutionCwd"])
        # Must not be the literal sentinel string
        self.assertNotEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
        # Must contain a real 12-hex fingerprint and engine label
        self.assertRegex(payload["plannedExecutionCwd"], r"worktrees/[0-9a-f]{12}/cursor-")
        self.assertIn("plannedBranch", payload)
        self.assertIsNotNone(payload["plannedBranch"])
        self.assertNotEqual(payload["plannedBranch"], "<planned-branch>")
        self.assertRegex(payload["plannedBranch"], r"^delegate/cursor-")

    def _assert_safe_dry_run_does_not_report_persistent_worktree_plan(
        self,
        *,
        engine: str,
        branch_prefix: str,
    ) -> None:
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.parsed_launch(
            engine,
            cwd=repo.name,
            engine=engine,
            mode="safe",
            prompt_parts=["review"],
            dry_run=True,
        )
        request = self.delegate.request_from_parsed(
            parsed,
            self.delegate.DEFAULT_CONFIG,
            io.StringIO(),
        )

        payload = self.delegate.dry_run_payload(request)

        self.assertEqual(payload["isolationLifecycle"], "temporary")
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertIsNone(payload["plannedExecutionCwd"])
        self.assertIsNone(payload["plannedBranch"])
        self.assertNotIn(branch_prefix, " ".join(payload["argv"]))
        self.assertNotIn("/worktrees/", " ".join(payload["argv"]))

    def test_dry_run_cursor_safe_does_not_report_persistent_worktree_plan(self):
        self._assert_safe_dry_run_does_not_report_persistent_worktree_plan(
            engine="cursor",
            branch_prefix="delegate/cursor-",
        )

    def test_dry_run_codex_safe_does_not_report_persistent_worktree_plan(self):
        self._assert_safe_dry_run_does_not_report_persistent_worktree_plan(
            engine="codex",
            branch_prefix="delegate/codex-",
        )

    def test_dry_run_worktree_planned_paths_not_placeholders_codex(self):
        """Codex work --isolation worktree dry-run yields concrete planned paths."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.parsed_launch(
            "codex",
            cwd=repo.name,
            engine="codex",
            mode="work",
            prompt_parts=["hello"],
            dry_run=True,
            isolation="worktree",
        )
        request = self.delegate.request_from_parsed(
            parsed,
            self.delegate.DEFAULT_CONFIG,
            io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIsNotNone(payload["plannedExecutionCwd"])
        self.assertNotEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
        self.assertRegex(payload["plannedExecutionCwd"], r"worktrees/[0-9a-f]{12}/codex-")
        self.assertIsNotNone(payload["plannedBranch"])
        self.assertNotEqual(payload["plannedBranch"], "<planned-branch>")
        self.assertRegex(payload["plannedBranch"], r"^delegate/codex-")

    def test_dry_run_worktree_planned_paths_not_placeholders_droid_qwen(self):
        """Droid qwen work --isolation worktree dry-run yields concrete planned paths."""
        repo, _git_cd = self._make_git_repo_with_commit()
        parsed = self.parsed_launch(
            "droid",
            cwd=repo.name,
            engine="droid",
            mode="work",
            model_alias="qwen",
            prompt_parts=["hello"],
            dry_run=True,
            isolation="worktree",
        )
        config = dict(self.delegate.DEFAULT_CONFIG)
        config["droid"] = dict(config["droid"])
        config["droid"]["models"] = dict(config["droid"]["models"])
        config["droid"]["models"]["qwen"] = "real-model-id"
        request = self.delegate.request_from_parsed(
            parsed,
            config,
            io.StringIO(),
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIsNotNone(payload["plannedExecutionCwd"])
        self.assertNotEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
        self.assertRegex(payload["plannedExecutionCwd"], r"worktrees/[0-9a-f]{12}/droid-qwen-")
        self.assertIsNotNone(payload["plannedBranch"])
        self.assertNotEqual(payload["plannedBranch"], "<planned-branch>")
        self.assertRegex(payload["plannedBranch"], r"^delegate/droid-qwen-")

    def test_dry_run_worktree_text_output_shows_planned_path(self):
        """Text dry-run output shows planned execution path, not source workspace."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            stdout_buf = io.StringIO()
            code = self.delegate.main(
                [
                    "--cwd",
                    repo.name,
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
            parsed = self.parsed_launch(
                "cursor",
                cwd=non_git_dir,
                engine="cursor",
                mode="work",
                prompt_parts=["hello"],
                dry_run=True,
                isolation="worktree",
            )
            request = self.delegate.request_from_parsed(
                parsed,
                self.delegate.DEFAULT_CONFIG,
                io.StringIO(),
            )
            payload = self.delegate.dry_run_payload(request)
            # Falls back to sentinel placeholders for non-Git workspaces
        self.assertEqual(payload["plannedExecutionCwd"], "<planned-worktree-path>")
        self.assertEqual(payload["plannedBranch"], "<planned-branch>")
        self.assertIn("isolatedWorkspace", payload)
        self.assertTrue(payload["isolatedWorkspace"])

    def test_dry_run_unsupported_reasoning_effort_includes_discovery_hint(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"] = dict(config["codex"])
        config["codex"]["defaultModel"] = "gpt-5.5"
        with self.assertRaises(self.delegate.DelegateError) as caught:
            self.build_git_request(
                "codex",
                "safe",
                None,
                "/repo",
                "review",
                config,
                dry_run=True,
                reasoning_effort="max",
            )
        self.assertEqual(caught.exception.error, "unsupported_reasoning_effort")
        self.assertIn("harness codex", caught.exception.message)
        self.assertIn("model 'gpt-5.5'", caught.exception.message)
        self.assertIn("delegate --json capabilities", caught.exception.message)

    def test_dry_run_kimi_reasoning_effort_reports_unsupported_alias_summary(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        with self.assertRaises(self.delegate.DelegateError) as caught:
            self.build_git_request(
                "kimi",
                "safe",
                None,
                "/repo",
                "hello",
                config,
                dry_run=True,
                reasoning_effort="high",
            )
        self.assertEqual(caught.exception.error, "unsupported_reasoning_effort")
        self.assertIn("harness kimi", caught.exception.message)
        self.assertIn("reasoning effort is not supported", caught.exception.message)
        self.assertIn("delegate --json models", caught.exception.message)


if __name__ == "__main__":
    import unittest

    unittest.main()
