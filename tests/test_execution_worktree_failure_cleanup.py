import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests.execution_test_base import ExecutionTestBase


class ExecutionWorktreeFailureCleanupTests(ExecutionTestBase):
    def test_prelaunch_failure_inspectable_via_snapshot(self):
        """Pre-launch worktree creation failure is inspectable via delegate snapshot main()."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            fake_bin = self.make_fake_bin()
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [str(fake_bin / "agent"), "--workspace", repo.name, "hello"],
                request.model,
                model_alias=request.model_alias,
                dry_run=request.dry_run,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            # Force create_persistent_worktree to fail.
            original_create = self.delegate.worktree_execution.create_persistent_worktree

            def failing_create(*args, **kwargs):
                raise self.delegate.worktree_execution.IsolationExecutionError(
                    "worktree_create_failed", "Simulated worktree failure"
                )

            self.delegate.worktree_execution.create_persistent_worktree = failing_create
            try:
                with self.assertRaises(self.delegate.DelegateError) as ctx:
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
                self.assertEqual(ctx.exception.error, "worktree_create_failed")

                registry_root = Path(repo.name) / ".delegate"
                runs_dir = registry_root / "runs"
                run_dirs = list(runs_dir.glob("del_*"))
                self.assertTrue(len(run_dirs) > 0)
                run_id = run_dirs[0].name

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
                self.delegate.worktree_execution.create_persistent_worktree = original_create

    def test_prelaunch_failure_snapshot_omits_unrealized_fields(self):
        """Pre-launch failure snapshot omits executionCwd/worktreeStatus/worktreeCleanupCommands
        so a failed git worktree add doesn't imply the worktree exists."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            fake_bin = self.make_fake_bin()
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [str(fake_bin / "agent"), "--workspace", repo.name, "hello"],
                request.model,
                model_alias=request.model_alias,
                dry_run=request.dry_run,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            original_create = self.delegate.worktree_execution.create_persistent_worktree

            def failing_create(*args, **kwargs):
                raise self.delegate.worktree_execution.IsolationExecutionError(
                    "worktree_create_failed", "Simulated worktree failure"
                )

            self.delegate.worktree_execution.create_persistent_worktree = failing_create
            try:
                with self.assertRaises(self.delegate.DelegateError) as ctx:
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
                self.delegate.worktree_execution.create_persistent_worktree = original_create

    def test_partial_worktree_cleanup_uses_git_timeouts(self):
        """Failure-path cleanup must not run unbounded git subprocesses."""
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree_path = Path(temp_dir) / "partial-worktree"
            worktree_path.mkdir()
            run_path = Path(temp_dir) / "run"
            run_path.mkdir()
            completed = subprocess.CompletedProcess(["git"], 0, "", "")
            with mock.patch.object(
                self.delegate.worktree_execution.subprocess, "run", return_value=completed
            ) as run_mock:
                self.delegate.worktree_execution._cleanup_partial_worktree(
                    "/repo",
                    str(worktree_path),
                    "delegate/cursor-partial",
                    run_path,
                    stderr=io.StringIO(),
                    remove_branch=True,
                )

            self.assertEqual(run_mock.call_count, 2)
            for call in run_mock.call_args_list:
                self.assertEqual(
                    call.kwargs.get("timeout"),
                    self.delegate.worktree_execution.GIT_MUTATION_TIMEOUT_SECONDS,
                )

    def test_partial_worktree_cleanup_records_branch_delete_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree_path = Path(temp_dir) / "partial-worktree"
            worktree_path.mkdir()
            run_path = Path(temp_dir) / "run"
            run_path.mkdir()
            snapshot_path = run_path / self.delegate.run_registry.SNAPSHOT_FILE
            self.delegate.run_registry.write_json_atomic(snapshot_path, {"ok": False})
            worktree_removed = subprocess.CompletedProcess(["git"], 0, "", "")
            branch_failed = subprocess.CompletedProcess(
                ["git"],
                1,
                "",
                "fatal: branch deletion failed\n",
            )
            branch_still_exists = subprocess.CompletedProcess(["git"], 0, "", "")

            with (
                mock.patch.object(
                    self.delegate.worktree_execution.subprocess,
                    "run",
                    side_effect=[worktree_removed, branch_failed, branch_still_exists],
                ),
            ):
                stderr = io.StringIO()
                self.delegate.worktree_execution._cleanup_partial_worktree(
                    "/repo",
                    str(worktree_path),
                    "delegate/cursor-partial",
                    run_path,
                    stderr=stderr,
                    remove_branch=True,
                )

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertTrue(snapshot["cleanupFailed"])
            self.assertIn("branch -D delegate/cursor-partial", snapshot["manualCleanup"])
            self.assertIn("manual cleanup required", stderr.getvalue())

    def test_partial_worktree_cleanup_warns_when_metadata_write_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree_path = Path(temp_dir) / "partial-worktree"
            worktree_path.mkdir()
            run_path = Path(temp_dir) / "run"
            run_path.mkdir()
            snapshot_path = run_path / self.delegate.run_registry.SNAPSHOT_FILE
            self.delegate.run_registry.write_json_atomic(snapshot_path, {"ok": False})
            worktree_removed = subprocess.CompletedProcess(["git"], 0, "", "")
            branch_failed = subprocess.CompletedProcess(
                ["git"],
                1,
                "",
                "fatal: branch deletion failed\n",
            )
            branch_still_exists = subprocess.CompletedProcess(["git"], 0, "", "")

            with (
                mock.patch.object(
                    self.delegate.worktree_execution.subprocess,
                    "run",
                    side_effect=[worktree_removed, branch_failed, branch_still_exists],
                ),
                mock.patch.object(
                    self.delegate.run_registry,
                    "write_json_atomic",
                    side_effect=OSError("disk full"),
                ),
            ):
                stderr = io.StringIO()
                self.delegate.worktree_execution._cleanup_partial_worktree(
                    "/repo",
                    str(worktree_path),
                    "delegate/cursor-partial",
                    run_path,
                    stderr=stderr,
                    remove_branch=True,
                )

            stderr_text = stderr.getvalue()
            self.assertIn("could not record cleanup metadata", stderr_text)
            self.assertIn("manual cleanup required", stderr_text)

    def test_partial_worktree_cleanup_ignores_already_missing_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree_path = Path(temp_dir) / "partial-worktree"
            worktree_path.mkdir()
            run_path = Path(temp_dir) / "run"
            run_path.mkdir()
            snapshot_path = run_path / self.delegate.run_registry.SNAPSHOT_FILE
            self.delegate.run_registry.write_json_atomic(snapshot_path, {"ok": False})
            worktree_removed = subprocess.CompletedProcess(["git"], 0, "", "")
            branch_delete_failed = subprocess.CompletedProcess(
                ["git"],
                1,
                "",
                "error: branch 'delegate/cursor-partial' not found.\n",
            )
            branch_absent = subprocess.CompletedProcess(["git"], 1, "", "")

            with (
                mock.patch.object(
                    self.delegate.worktree_execution.subprocess,
                    "run",
                    side_effect=[worktree_removed, branch_delete_failed, branch_absent],
                ),
            ):
                stderr = io.StringIO()
                self.delegate.worktree_execution._cleanup_partial_worktree(
                    "/repo",
                    str(worktree_path),
                    "delegate/cursor-partial",
                    run_path,
                    stderr=stderr,
                    remove_branch=True,
                )

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertNotIn("cleanupFailed", snapshot)
            self.assertNotIn("manualCleanup", snapshot)
            self.assertEqual(stderr.getvalue(), "")

    def test_popen_failure_after_worktree_create_preserves_worktree(self):
        """Popen-launch failure after git worktree add succeeds preserves worktree
        and records run as failed with error captured."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()

            # An invalid interpreter passes ensure_binary but makes Popen raise.
            bad_bin_dir = tempfile.TemporaryDirectory()
            self.addCleanup(bad_bin_dir.cleanup)
            bad_agent = Path(bad_bin_dir.name) / "agent"
            bad_agent.write_text(
                "#!/usr/bin/env bash_does_not_exist_xyz\necho 'this should never run'\n"
            )
            bad_agent.chmod(0o755)

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
                [
                    str(bad_agent),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ,
                {"PATH": str(bad_bin_dir.name) + os.pathsep + os.environ.get("PATH", "")},
            ):
                # The execute_request should raise or propagate the error
                # because Popen will fail (bad interpreter).
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
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
            state = self.delegate.json.loads((run_dirs[0] / "state.json").read_text())
            self.assertEqual(state.get("status"), "failed")
            self.assertIn("exitCode", state)
            self.assertIsNotNone(state["exitCode"])

    def test_launch_exception_after_manifest_write_records_failed_snapshot(self):
        """Exceptions after persistent registration/manifest write leave an inspectable failure."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            fake_bin = self.make_fake_bin()
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
            )

            def fail_execute_tracked(*_args, **_kwargs):
                raise self.delegate.delegate_runner.RunnerLaunchError(
                    "simulated_setup_error",
                    "simulated setup failure",
                )

            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")},
                ),
                mock.patch.object(
                    self.delegate.worktree_execution.delegate_runner,
                    "execute_tracked",
                    side_effect=fail_execute_tracked,
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

            self.assertEqual(ctx.exception.error, "simulated_setup_error")
            registry_root = Path(repo.name) / ".delegate"
            run_dirs = list((registry_root / "runs").glob("del_*"))
            self.assertTrue(run_dirs)
            snapshot = self.delegate.json.loads((run_dirs[0] / "snapshot.json").read_text())
            self.assertEqual(snapshot.get("status"), "failed")
            self.assertEqual(snapshot.get("error"), "simulated_setup_error")
            self.assertIn("simulated setup failure", snapshot.get("message", ""))

    # -- L837: Real git worktree add collision via pre-existing worktree --------

    def test_persistent_worktree_add_collision_fails_and_cleans_up(self):
        """Real 'git worktree add' collision surfaces as the creation error
        with git stderr surfaced, and no partial artifacts remain.

        Pre-checkout the predicted delegate branch in another git worktree
        so that `git worktree add -b` in create_persistent_worktree fails
        with either branch_collision or worktree_create_failed depending on
        how the code detects the collision.
        """
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()

            # Monkey-patch generate_run_id so we know the predicted branch.
            fixed_run_id = "del_20250101T000000Z_abcdef"
            with mock.patch.object(
                self.delegate.run_registry,
                "generate_run_id",
                return_value=fixed_run_id,
            ):
                from delegate_agent.isolation import (
                    branch_label,
                    plan_branch_name,
                    short_run_id,
                )

                short_id = short_run_id(fixed_run_id)
                label = branch_label("cursor", None)
                predicted_branch = plan_branch_name(label, short_id)

                # Pre-checkout the predicted branch in a separate
                # worktree so git refuses to create it.
                branch_wt_path = tempfile.mkdtemp()
                subprocess.run(
                    ["git", "-C", repo.name, "branch", "--no-track", predicted_branch, "HEAD"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(
                    ["git", "-C", repo.name, "worktree", "add", branch_wt_path, predicted_branch],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Clean up the blocking worktree afterwards.
                self.addCleanup(
                    lambda: subprocess.run(
                        ["git", "-C", repo.name, "worktree", "remove", "--force", branch_wt_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                )

                fake_bin = self.make_fake_bin()
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
                    [
                        str(fake_bin / "agent"),
                        "--workspace",
                        repo.name,
                        "-p",
                        "--trust",
                        "--model",
                        "composer-2.5",
                        "--output-format",
                        "text",
                        "hello",
                    ],
                    request.model,
                    dry_run=False,
                    workspace_kind=request.workspace_kind,
                    isolation_context=request.isolation_context,
                )
                with self.assertRaises(self.delegate.DelegateError) as ctx:
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
                        len(cursor_dirs),
                        0,
                        "No delegate worktree directories should exist after a failed creation",
                    )

    # -- Finding: sync failure mid-include-dirty tears down worktree, no child --

    def test_include_dirty_sync_failure_tears_down_worktree_and_never_launches_child(self):
        """When sync_git_dirty_snapshot fails after the persistent worktree is
        created, the fail-clean branch records a failed snapshot, tears down the
        partial worktree, and the child is never launched."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            repo_path = Path(repo.name)
            # Make the source dirty so include-dirty has something to sync.
            (repo_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", repo.name, "add", "tracked.txt"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo.name,
                    "-c",
                    "user.name=Delegate Test",
                    "-c",
                    "user.email=delegate-test@example.com",
                    "commit",
                    "-m",
                    "base",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (repo_path / "tracked.txt").write_text("dirty-now\n", encoding="utf-8")

            agent_marker_parent = Path(tempfile.mkdtemp())
            self.addCleanup(
                lambda: __import__("shutil").rmtree(agent_marker_parent, ignore_errors=True)
            )
            agent = agent_marker_parent / "agent"
            agent.write_text(
                "#!/usr/bin/env bash\n"
                f"touch {agent_marker_parent / 'child-launched.marker'}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            agent.chmod(0o755)

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
                [str(agent), "--workspace", repo.name, "hello"],
                request.model,
                model_alias=request.model_alias,
                dry_run=request.dry_run,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
                include_dirty=True,
            )

            executed_tracked = {"called": False}
            original_execute_tracked = (
                self.delegate.worktree_execution.delegate_runner.execute_tracked
            )

            def tracking_execute_tracked(*_args, **_kwargs):
                executed_tracked["called"] = True
                return original_execute_tracked(*_args, **_kwargs)

            def failing_sync(*_args, **_kwargs):
                raise self.delegate.DelegateError(
                    "safe_workspace_sync_failed",
                    "Simulated include-dirty sync failure",
                )

            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": str(agent_marker_parent) + os.pathsep + os.environ.get("PATH", "")},
                ),
                mock.patch.object(
                    self.delegate.worktree_execution.safe_workspace,
                    "sync_git_dirty_snapshot",
                    side_effect=failing_sync,
                ),
                mock.patch.object(
                    self.delegate.worktree_execution.delegate_runner,
                    "execute_tracked",
                    side_effect=tracking_execute_tracked,
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

            self.assertEqual(ctx.exception.error, "safe_workspace_sync_failed")
            # Child was never launched.
            self.assertFalse(executed_tracked["called"])
            self.assertFalse((agent_marker_parent / "child-launched.marker").exists())

            # Failed snapshot is inspectable.
            registry_root = Path(repo.name) / ".delegate"
            run_dirs = list((registry_root / "runs").glob("del_*"))
            self.assertTrue(run_dirs)
            snapshot = json.loads((run_dirs[0] / "snapshot.json").read_text())
            self.assertEqual(snapshot.get("status"), "failed")
            self.assertEqual(snapshot.get("error"), "safe_workspace_sync_failed")
            self.assertIn("Simulated include-dirty sync failure", snapshot.get("message", ""))
            state = json.loads((run_dirs[0] / "state.json").read_text())
            self.assertEqual(state.get("status"), "failed")

            # Partial worktree was torn down: no delegate worktree dirs remain.
            data_home = Path(fake_home) / ".delegate" / "worktrees"
            if data_home.exists():
                cursor_dirs = list(data_home.rglob("cursor-*"))
                self.assertEqual(
                    len(cursor_dirs),
                    0,
                    "No delegate worktree directories should remain after a sync failure",
                )


if __name__ == "__main__":
    import unittest

    unittest.main()
