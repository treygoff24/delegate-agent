import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests.execution_test_base import (
    GIT_TEST_IDENTITY,
    ExecutionTestBase,
    make_git_repo,
)


class ExecutionWorktreePreflightTests(ExecutionTestBase):
    def _write_missing_cursor_worktree_config(self, directory: str) -> Path:
        config_path = Path(directory) / "delegate-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "cursor": {
                        "argvPrefix": ["delegate-definitely-missing-agent"],
                        "defaultModel": "composer-2.5",
                    },
                    "isolation": {"work": "worktree"},
                }
            )
        )
        return config_path

    def test_persistent_worktree_missing_binary_fails_before_artifacts(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(
                os.environ,
                {"HOME": fake_home},
            ),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            config_path = self._write_missing_cursor_worktree_config(fake_home)
            stdout_buf = io.StringIO()

            with mock.patch.dict(os.environ, {"DELEGATE_CONFIG": str(config_path)}):
                code = self.delegate.main(
                    ["--cwd", repo.name, "--json", "cursor", "work", "hello"],
                    stdout=stdout_buf,
                )

            payload = json.loads(stdout_buf.getvalue())
            self.assertEqual(code, self.delegate.EXIT_MISSING_BINARY)
            self.assertEqual(payload["exitCode"], self.delegate.EXIT_MISSING_BINARY)
            self.assertEqual(payload["error"], "missing_binary")
            self.assertEqual(payload["configPath"], str(config_path))
            self.assertEqual(payload["configKey"], "cursor.argvPrefix")
            self.assertFalse((Path(fake_home) / ".delegate" / "worktrees").exists())
            branches = subprocess.run(
                ["git", "-C", repo.name, "branch", "--list", "delegate/*"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(branches.stdout.strip(), "")
            self.assertFalse((Path(repo.name) / ".delegate" / "runs").exists())

    def test_persistent_worktree_dirty_source_preflight_beats_missing_binary(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(
                os.environ,
                {"HOME": fake_home},
            ),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            (Path(repo.name) / "dirty.txt").write_text("untracked\n")
            config_path = self._write_missing_cursor_worktree_config(fake_home)
            stdout_buf = io.StringIO()

            with mock.patch.dict(os.environ, {"DELEGATE_CONFIG": str(config_path)}):
                code = self.delegate.main(
                    ["--cwd", repo.name, "--json", "cursor", "work", "hello"],
                    stdout=stdout_buf,
                )

            payload = json.loads(stdout_buf.getvalue())
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(payload["error"], "dirty_source_workspace")
            self.assertNotEqual(payload["error"], "missing_binary")
            self.assertFalse((Path(fake_home) / ".delegate" / "worktrees").exists())
            self.assertFalse((Path(repo.name) / ".delegate" / "runs").exists())

    def test_persistent_worktree_missing_head_preflight_beats_missing_binary(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(
                os.environ,
                {"HOME": fake_home},
            ),
        ):
            repo = make_git_repo()
            self.addCleanup(repo.cleanup)
            config_path = self._write_missing_cursor_worktree_config(fake_home)
            stdout_buf = io.StringIO()

            with mock.patch.dict(os.environ, {"DELEGATE_CONFIG": str(config_path)}):
                code = self.delegate.main(
                    ["--cwd", repo.name, "--json", "cursor", "work", "hello"],
                    stdout=stdout_buf,
                )

            payload = json.loads(stdout_buf.getvalue())
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(payload["error"], "missing_git_head")
            self.assertNotEqual(payload["error"], "missing_binary")
            self.assertFalse((Path(fake_home) / ".delegate" / "worktrees").exists())
            self.assertFalse((Path(repo.name) / ".delegate" / "runs").exists())

    # -- Non-Git workspace fails clearly --------------------------------------

    def test_persistent_worktree_non_git_fails_clearly(self):
        """Non-Git workspace with --isolation worktree fails before creating artifacts."""
        with tempfile.TemporaryDirectory() as non_git:
            workspace = self.delegate.resolve_workspace(non_git)
            request = self.delegate.Request(
                "cursor",
                "work",
                non_git,
                "hello",
                ["agent", "--workspace", non_git, "-p", "--trust", "hello"],
                "composer-2.5",
                workspace_kind="directory",
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
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="markdown",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
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
            "cursor",
            "work",
            repo.name,
            "hello",
            ["agent", "--workspace", repo.name, "-p", "--trust", "hello"],
            "composer-2.5",
            workspace_kind="git",
            isolation_context=isolation_context,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request,
                json_mode=False,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "missing_git_head")

    # -- Dirty source fails clearly -------------------------------------------

    def test_persistent_worktree_dirty_source_fails(self):
        """Dirty source workspace fails clean-source check before creating artifacts."""
        repo, _git_cd = self._make_git_repo_with_commit()
        (Path(repo.name) / "dirty.txt").write_text("untracked\n")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor",
            "work",
            repo.name,
            self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request,
                json_mode=False,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
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
            "cursor",
            "work",
            repo.name,
            self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request,
                json_mode=False,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=True,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "pass_through_with_persistent_isolation")
        self.assertIn("persistent worktree runs", ctx.exception.message)

    # -- Cursor safe --isolation none normalizes before source workspace use --

    def test_cursor_safe_isolation_none_normalizes_to_auto(self):
        """Cursor safe cannot disable the temporary isolation boundary."""
        repo, _git_cd = self._make_git_repo_with_commit()
        source_cursor_config = Path(repo.name) / ".cursor" / "cli.json"
        self.assertFalse(source_cursor_config.exists())
        self.assertEqual(
            self.delegate.delegate_config.resolve_isolation(
                cli_value="none",
                loaded_config=self.delegate.DEFAULT_CONFIG,
                engine="cursor",
                mode="safe",
            ),
            "auto",
        )
        self.assertFalse(
            source_cursor_config.exists(),
            "Normalized --isolation none must not write source .cursor/cli.json",
        )

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
            fast=False,
        )

        with self.delegate.safe_isolated_request(request) as isolated:
            self.assertEqual(isolated.model, "composer-2.5")
            self.assertEqual(isolated.model_alias, "composer")
            self.assertIs(isolated.fast, False)
            self.assertTrue(isolated.dry_run)
            self.assertEqual(isolated.workspace_kind, "git")
            self.assertNotEqual(isolated.workspace, repo.name)
            self.assertIn(isolated.workspace, isolated.argv)
            self.assertNotIn(repo.name, isolated.argv)

    # -- Persistent prompt note ordering --------------------------------------

    def test_persistent_worktree_prompt_note_after_skill_review(self):
        """Persistent worktree context note appears after skill-review and before prompt."""
        from delegate_agent.isolation import (
            PERSISTENT_WORKTREE_CONTEXT_NOTE,
            prepend_persistent_worktree_context,
        )

        user_prompt = "Implement the fix."
        skill_prefix = self.delegate.delegate_runner.SKILL_REVIEW_PREFIX
        full_prompt = skill_prefix + user_prompt
        result = prepend_persistent_worktree_context(full_prompt)

        skill_idx = result.index("Delegate sub-agent skill review")
        worktree_idx = result.index("You are running in a Delegate-created")
        user_idx = result.index("Implement the fix")

        self.assertGreater(worktree_idx, skill_idx)
        self.assertGreater(user_idx, worktree_idx)
        self.assertIn("delegate worktree remove <alias> --force", PERSISTENT_WORKTREE_CONTEXT_NOTE)

    def test_persistent_worktree_dirty_staged_changes_fails(self):
        """Staged changes cause clean-source failure."""
        repo, _git_cd = self._make_git_repo_with_commit()
        staged = Path(repo.name) / "staged.txt"
        staged.write_text("staged\n")
        subprocess.run(
            ["git", "-C", repo.name, "add", "staged.txt"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor",
            "work",
            repo.name,
            self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request,
                json_mode=False,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "dirty_source_workspace")

    def test_persistent_worktree_dirty_unstaged_tracked_changes_fails(self):
        """Unstaged changes to tracked files cause clean-source failure."""
        repo, _git_cd = self._make_git_repo_with_commit()
        tracked = Path(repo.name) / "tracked.txt"
        tracked.write_text("initial\n")
        subprocess.run(
            ["git", "-C", repo.name, "add", "tracked.txt"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "-m", "add tracked"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracked.write_text("modified-but-not-staged\n")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor",
            "work",
            repo.name,
            self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request,
                json_mode=False,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "dirty_source_workspace")

    def test_persistent_worktree_dirty_submodule_fails(self):
        """Submodule dirtiness causes clean-source failure."""
        sub_repo = tempfile.TemporaryDirectory()
        self.addCleanup(sub_repo.cleanup)
        subprocess.run(
            ["git", "-C", sub_repo.name, "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "git",
                "-C",
                sub_repo.name,
                *GIT_TEST_IDENTITY,
                "commit",
                "--allow-empty",
                "-m",
                "sub init",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        repo, _git_cd = self._make_git_repo_with_commit()
        # Add the submodule pointing to the separate repo.
        # protocol.file.allow=always needed on modern git for local file:// clones.
        subprocess.run(
            [
                "git",
                "-C",
                repo.name,
                "-c",
                "protocol.file.allow=always",
                *GIT_TEST_IDENTITY,
                "submodule",
                "add",
                sub_repo.name,
                "mysub",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Commit the submodule addition so the main repo is clean.
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "-m", "add submodule"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Modify a file inside the submodule to make it dirty.
        sub_file = Path(repo.name) / "mysub" / "dirty-in-sub.txt"
        sub_file.write_text("sub dirty\n")
        workspace = self.delegate.resolve_workspace(repo.name)
        request = self._make_persistent_worktree_request(
            "cursor",
            "work",
            repo.name,
            self.delegate.DEFAULT_CONFIG,
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.execute_request(
                request,
                json_mode=False,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "dirty_source_workspace")


if __name__ == "__main__":
    import unittest

    unittest.main()
