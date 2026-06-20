import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase, make_git_repo

# Imported after the base (which bootstraps sys.path); the two timeout tests
# monkeypatch safe_workspace._run_git / _run_git_bytes directly on this module.
from delegate_agent import safe_workspace


class SafeWorkspaceIsolationTests(CommandTestBase):
    def test_cursor_safe_dry_run_reports_isolation(self):
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertIn("isolation", payload)
        self.assertNotIn("--mode=plan", payload["argv"])
        self.assertNotIn("--approve-mcps", payload["argv"])

    def test_cursor_safe_cli_config_omits_mutating_shell(self):
        allow = self.delegate.CURSOR_SAFE_CLI_CONFIG["permissions"]["allow"]
        self.assertIn("Read(**)", allow)
        self.assertNotIn("Shell(git)", allow)
        self.assertNotIn("Shell(find)", allow)
        self.assertNotIn("Shell(ls)", allow)

    def test_cursor_safe_cli_config_is_permissions_only(self):
        self.assertEqual(set(self.delegate.CURSOR_SAFE_CLI_CONFIG), {"permissions"})

    def test_write_cursor_safe_project_config_serializes_permissions_only(self):
        with tempfile.TemporaryDirectory() as workspace:
            self.delegate.write_cursor_safe_project_config(Path(workspace))
            config = self.delegate.json.loads(
                (Path(workspace) / ".cursor" / "cli.json").read_text()
            )
        self.assertEqual(set(config), {"permissions"})
        self.assertIn("allow", config["permissions"])
        self.assertIn("deny", config["permissions"])

    def test_mirror_path_preserving_symlinks_keeps_link_target(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            link = Path(workspace) / "link.txt"
            link.symlink_to(secret)

            destination = Path(workspace) / "mirror" / "link.txt"
            self.delegate.mirror_path_preserving_symlinks(link, destination)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(os.readlink(destination), str(secret))

    def test_create_directory_safe_workspace_blocks_external_symlinks(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(workspace) / "link.txt").symlink_to(secret)

            copy_path, temp_base, warnings = self.delegate.create_directory_safe_workspace(
                workspace,
                include_warnings=True,
            )
            try:
                copied = Path(copy_path) / "link.txt"
                self.assertFalse(copied.is_symlink())
                self.assertEqual(
                    copied.read_text(encoding="utf-8"),
                    self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
                )
                self.assertNotIn(str(secret), copied.read_text(encoding="utf-8"))
                self.assertEqual(len(warnings), 1)
                self.assertIn(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX, warnings[0])
                self.assertIn("link.txt", warnings[0])
                self.assertNotIn(str(secret), warnings[0])
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    def test_create_directory_safe_workspace_preserves_internal_symlinks(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "target.txt").write_text("inside\n", encoding="utf-8")
            (root / "link.txt").symlink_to("target.txt")

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                copied = Path(copy_path) / "link.txt"
                self.assertTrue(copied.is_symlink())
                self.assertEqual(os.readlink(copied), "target.txt")
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    def test_create_directory_safe_workspace_omits_delegate_registry(self):
        with tempfile.TemporaryDirectory() as workspace:
            delegate_dir = Path(workspace) / ".delegate"
            delegate_dir.mkdir()
            (delegate_dir / "stdout.log").write_text("private run output\n", encoding="utf-8")

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                self.assertFalse((Path(copy_path) / ".delegate").exists())
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo unavailable")
    def test_create_directory_safe_workspace_skips_fifo_files(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "normal.txt").write_text("copy me\n", encoding="utf-8")
            os.mkfifo(root / "signal.fifo")

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                copied = Path(copy_path)
                self.assertEqual((copied / "normal.txt").read_text(encoding="utf-8"), "copy me\n")
                self.assertFalse((copied / "signal.fifo").exists())
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    def test_external_symlink_audit_warns_without_target_contents(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(workspace) / "link.txt").symlink_to(secret)

            warnings = self.delegate.external_symlink_warnings(workspace)
            self.assertEqual(len(warnings), 1)
            self.assertIn(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX, warnings[0])
            self.assertIn("link.txt", warnings[0])
            self.assertNotIn(str(secret), warnings[0])

    def test_git_safe_workspace_blocks_untracked_external_symlink(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            link = Path(repo.name) / "external-link.txt"
            link.symlink_to(secret)

            worktree_path, temp_base, warnings = self.delegate.create_git_safe_workspace(
                repo.name,
                include_warnings=True,
            )
            try:
                copied = Path(worktree_path) / "external-link.txt"
                self.assertFalse(copied.is_symlink())
                self.assertEqual(
                    copied.read_text(encoding="utf-8"),
                    self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
                )
                self.assertNotIn(str(secret), copied.read_text(encoding="utf-8"))
                self.assertEqual(len(warnings), 1)
                self.assertIn(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX, warnings[0])
                self.assertIn("external-link.txt", warnings[0])
                self.assertNotIn(str(secret), warnings[0])
            finally:
                self.delegate.cleanup_safe_isolated_workspace(
                    git_root=repo.name,
                    isolated_workspace=worktree_path,
                    temp_base=temp_base,
                )

    def test_create_git_safe_workspace_reports_worktree_timeout(self):
        timeout = subprocess.CompletedProcess(
            ["git"],
            124,
            "",
            "git command timed out after 30s",
        )
        with (
            mock.patch.object(safe_workspace, "_run_git", return_value=timeout),
            self.assertRaises(self.delegate.DelegateError) as ctx,
        ):
            self.delegate.create_git_safe_workspace("/repo")

        self.assertEqual(ctx.exception.error, "safe_workspace_create_failed")
        self.assertIn("timed out", ctx.exception.message)

    def test_read_git_tracked_diff_reports_timeout_from_binary_runner(self):
        timeout = subprocess.CompletedProcess(
            ["git"],
            124,
            b"",
            b"git command timed out after 30s",
        )
        with (
            mock.patch.object(safe_workspace, "_run_git_bytes", return_value=timeout),
            self.assertRaises(self.delegate.DelegateError) as ctx,
        ):
            self.delegate.read_git_tracked_diff("/repo")

        self.assertEqual(ctx.exception.error, "safe_workspace_sync_failed")
        self.assertIn("timed out", ctx.exception.message)

    def test_safe_isolation_surfaces_external_symlink_warning(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(repo.name) / "external-link.txt").symlink_to(secret)
            workspace = self.delegate.ResolvedWorkspace(repo.name, "git")
            iso_ctx = self.delegate.build_isolation_context(
                source_workspace=repo.name,
                resolved_isolation="auto",
                engine="cursor",
                mode="safe",
                source_git_root=repo.name,
            )
            request = self.delegate.build_request(
                "cursor",
                "safe",
                None,
                workspace,
                "review",
                self.delegate.DEFAULT_CONFIG,
                dry_run=False,
                isolation_context=iso_ctx,
            )

            with self.delegate.safe_isolated_request(request) as isolated:
                warnings = isolated.isolation_context.warnings
                self.assertEqual(isolated.isolation_context.safe_workspace_method, "git-worktree")
                self.assertTrue(
                    any(
                        self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX in item
                        for item in warnings
                    )
                )

    def test_unborn_git_safe_isolation_falls_back_to_directory_copy(self):
        repo = make_git_repo(with_commit=False)
        self.addCleanup(repo.cleanup)
        delegate_dir = Path(repo.name) / ".delegate"
        delegate_dir.mkdir()
        (delegate_dir / "stdout.log").write_text("private run output\n", encoding="utf-8")
        workspace = self.delegate.ResolvedWorkspace(repo.name, "git")
        iso_ctx = self.delegate.build_isolation_context(
            source_workspace=repo.name,
            resolved_isolation="auto",
            engine="codex",
            mode="safe",
            source_git_root=repo.name,
        )
        request = self.delegate.build_request(
            "codex",
            "safe",
            None,
            workspace,
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=False,
            isolation_context=iso_ctx,
        )

        with self.delegate.safe_isolated_request(request) as isolated:
            self.assertEqual(isolated.workspace_kind, "directory")
            self.assertFalse((Path(isolated.workspace) / ".git").exists())
            self.assertFalse((Path(isolated.workspace) / ".delegate").exists())
            self.assertEqual(
                isolated.isolation_context.safe_workspace_method,
                "directory-copy",
            )
            self.assertIn(
                self.delegate.SAFE_UNBORN_GIT_WARNING,
                isolated.isolation_context.warnings,
            )
            self.assertIn("--skip-git-repo-check", isolated.argv)
            self.assertEqual(isolated.isolation_context.source_git_root, repo.name)

    def test_dry_run_codex_safe_does_not_create_delegate_dir(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        workspace = Path(repo.name)
        delegate_dir = workspace / ".delegate"
        self.assertFalse(delegate_dir.exists())
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(workspace), "dry-run", "codex", "safe", "review"],
            stdout=stdout,
        )
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertFalse(delegate_dir.exists())


if __name__ == "__main__":
    unittest.main()
