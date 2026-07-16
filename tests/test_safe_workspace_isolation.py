import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase, make_git_repo

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Imported after the base (which bootstraps sys.path); the two timeout tests
# monkeypatch safe_workspace._run_git / _run_git_bytes directly on this module.
from delegate_agent import safe_workspace  # noqa: E402
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_SLASH  # noqa: E402


class SafeWorkspaceIsolationTests(CommandTestBase):
    def make_dirty_repo(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        tracked = root / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", repo.name, "add", "tracked.txt", ".gitignore"],
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
                "tracked",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracked.write_text("dirty\n", encoding="utf-8")
        (root / "notes.txt").write_text("local-only\n", encoding="utf-8")
        (root / "ignored.txt").write_text("secret\n", encoding="utf-8")
        return repo

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

    def test_cleanup_refuses_target_containing_source_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            source.mkdir()
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.cleanup_safe_isolated_workspace(
                    git_root=None,
                    isolated_workspace=str(source),
                    temp_base=temp_dir,
                    source_root=str(source),
                )
        self.assertEqual(ctx.exception.error, "source_root_guard")

    def _submodule_paths_from_status(self, status: bytes) -> tuple[str, ...]:
        completed = subprocess.CompletedProcess(["git"], 0, status, b"")
        with mock.patch.object(safe_workspace, "_run_git_bytes", return_value=completed):
            return safe_workspace.dirty_submodule_paths("/repo")

    def test_submodule_new_commit_is_blocked_when_gitlink_diff_cannot_sync(self):
        status = b"1 .M SC.. 160000 160000 160000 old new sub\0"
        self.assertEqual(self._submodule_paths_from_status(status), ("sub",))

    def test_git_check_ignore_preserves_surrogateescaped_paths(self):
        path = os.fsdecode(b"bad-\xff-name")
        completed = subprocess.CompletedProcess(["git"], 0, os.fsencode(path) + b"\0", b"")
        with mock.patch.object(safe_workspace, "_run_git_bytes", return_value=completed) as run_git:
            ignored, failed_closed = safe_workspace._git_check_ignore("/repo", [path])

        self.assertEqual(ignored, {path})
        self.assertFalse(failed_closed)
        self.assertEqual(run_git.call_args.kwargs["input_bytes"], os.fsencode(path) + b"\0")

    def test_submodule_nested_content_dirt_is_blocked(self):
        status = b"1 .M S.M. 160000 160000 160000 old old sub\0"
        self.assertEqual(self._submodule_paths_from_status(status), ("sub",))

    def test_staged_gitlink_update_is_blocked_when_gitlink_diff_cannot_sync(self):
        status = b"1 M. S... 160000 160000 160000 old new sub\0"
        self.assertEqual(self._submodule_paths_from_status(status), ("sub",))

    def test_renamed_gitlink_porcelain_record_consumes_origin_path(self):
        status = b"2 R. S... 160000 160000 160000 old new R100 renamed\0sub\0"
        self.assertEqual(self._submodule_paths_from_status(status), ("renamed",))

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

    def test_git_safe_workspace_syncs_tracked_and_untracked_changes(self):
        repo = self.make_dirty_repo()

        worktree_path, temp_base = self.delegate.create_git_safe_workspace(repo.name)
        try:
            isolated = Path(worktree_path)
            self.assertEqual((isolated / "tracked.txt").read_text(encoding="utf-8"), "dirty\n")
            self.assertEqual((isolated / "notes.txt").read_text(encoding="utf-8"), "local-only\n")
            self.assertFalse((isolated / "ignored.txt").exists())
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_changed_files_vs_head_lists_tracked_and_untracked_non_ignored(self):
        repo = self.make_dirty_repo()

        self.assertEqual(
            safe_workspace.changed_files_vs_head(repo.name),
            ("tracked.txt", "notes.txt"),
        )

    def test_dirty_sync_counts_track_git_rm_cached_as_both_changes(self):
        repo = self.make_dirty_repo()
        subprocess.run(
            ["git", "-C", repo.name, "rm", "--cached", "tracked.txt"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        snapshot = safe_workspace.dirty_sync_snapshot(repo.name)

        self.assertIn("tracked.txt", snapshot.diff_names)
        self.assertIn("tracked.txt", snapshot.untracked_names)
        self.assertEqual(safe_workspace.dirty_sync_counts(repo.name), (1, 2))

    def test_dirty_sync_preserves_unicode_and_tab_filenames(self):
        repo = self.make_dirty_repo()
        root = Path(repo.name)
        names = ("caf\u00e9.txt", "tab\tname.txt")
        for name in names:
            (root / name).write_text(name, encoding="utf-8")

        worktree_path, temp_base = self.delegate.create_git_safe_workspace(repo.name)
        try:
            isolated = Path(worktree_path)
            self.assertTrue(set(names).issubset(safe_workspace.changed_files_vs_head(repo.name)))
            for name in names:
                self.assertEqual((isolated / name).read_text(encoding="utf-8"), name)
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_git_safe_workspace_excludes_nested_gitignored_subdir_secret(self):
        """A secret in a subdirectory that a nested .gitignore excludes is not
        synced into the isolated workspace (the ignore rule is honored even when
        it lives in a subdirectory rather than the repo root)."""
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        (root / "tracked.txt").write_text("base\n", encoding="utf-8")
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
                "tracked",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sub = root / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
        (sub / "secret.txt").write_text("subdir-secret\n", encoding="utf-8")
        (sub / "kept.txt").write_text("kept\n", encoding="utf-8")

        worktree_path, temp_base = self.delegate.create_git_safe_workspace(repo.name)
        try:
            isolated = Path(worktree_path)
            self.assertFalse((isolated / "sub" / "secret.txt").exists())
            self.assertEqual(
                (isolated / "sub" / "kept.txt").read_text(encoding="utf-8"),
                "kept\n",
            )
            # The nested .gitignore itself is untracked and non-ignored, so it syncs.
            self.assertTrue((isolated / "sub" / ".gitignore").exists())
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_git_safe_workspace_blocks_untracked_symlink_to_gitignored_target(self):
        """An untracked symlink whose readlink target is RELATIVE and resolves
        inside the repo to a gitignored file is replaced with the inert
        placeholder (the leak rule), and the blocked-symlink warning is emitted.
        The gitignored secret stays unreadable from the isolated workspace."""
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        (root / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
        (root / "secret.txt").write_text("host secret\n", encoding="utf-8")
        (root / "leak.link").symlink_to("secret.txt")

        worktree_path, temp_base, warnings = self.delegate.create_git_safe_workspace(
            repo.name,
            include_warnings=True,
        )
        try:
            isolated = Path(worktree_path)
            blocked = isolated / "leak.link"
            self.assertFalse(blocked.is_symlink())
            self.assertEqual(
                blocked.read_text(encoding="utf-8"),
                self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
            )
            self.assertTrue(
                any(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX in item for item in warnings)
            )
            self.assertTrue(any("leak.link" in item for item in warnings))
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_git_safe_workspace_recreates_legit_relative_symlink_to_non_ignored_file(self):
        """An untracked relative symlink whose target resolves inside the repo
        to a non-ignored file is recreated (not placeholdered)."""
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        (root / "target.txt").write_text("inside\n", encoding="utf-8")
        (root / "good.link").symlink_to("target.txt")

        worktree_path, temp_base, warnings = self.delegate.create_git_safe_workspace(
            repo.name,
            include_warnings=True,
        )
        try:
            isolated = Path(worktree_path)
            copied = isolated / "good.link"
            self.assertTrue(copied.is_symlink())
            self.assertEqual(os.readlink(copied), "target.txt")
            self.assertEqual(
                copied.resolve().read_text(encoding="utf-8"),
                "inside\n",
            )
            self.assertNotIn(
                self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX,
                warnings,
            )
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_git_safe_workspace_blocks_untracked_absolute_symlink_to_repo_file(self):
        """An untracked symlink with an ABSOLUTE readlink target is replaced with
        the inert placeholder even when the target resolves inside the repo, and
        the blocked-symlink warning is emitted."""
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        (root / "target.txt").write_text("inside\n", encoding="utf-8")
        absolute_target = (root / "target.txt").resolve()
        (root / "abs.link").symlink_to(absolute_target)

        worktree_path, temp_base, warnings = self.delegate.create_git_safe_workspace(
            repo.name,
            include_warnings=True,
        )
        try:
            isolated = Path(worktree_path)
            blocked = isolated / "abs.link"
            self.assertFalse(blocked.is_symlink())
            self.assertEqual(
                blocked.read_text(encoding="utf-8"),
                self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
            )
            self.assertTrue(
                any(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX in item for item in warnings)
            )
            self.assertTrue(any("abs.link" in item for item in warnings))
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_git_safe_workspace_blocks_symlink_to_gitignored_target_with_newline_in_path(self):
        """A symlink whose relative target resolves inside the repo to a
        gitignored file whose path contains a newline is placeholdered.

        This exercises the NUL-separated ``git check-ignore -z --stdin`` batch:
        the old newline-separated parser would split ``secrets/sec\\nret.txt``
        into ``secrets/sec`` and ``ret.txt``, neither matching the queried
        path, so the symlink would NOT be placeholdered (a false negative that
        leaked a gitignored secret). The NUL-delimited protocol bounds each
        path correctly and detects the ignore match.
        """
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        (root / ".gitignore").write_text("secrets/\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", repo.name, "add", ".gitignore"],
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
                "gitignore",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        secrets = root / "secrets"
        secrets.mkdir()
        # File name contains a literal newline.
        newline_name = "sec\nret.txt"
        (secrets / newline_name).write_text("host secret\n", encoding="utf-8")
        # Relative symlink whose readlink target contains the newline path.
        (root / "weird.link").symlink_to(f"secrets/{newline_name}")

        worktree_path, temp_base, warnings = self.delegate.create_git_safe_workspace(
            repo.name,
            include_warnings=True,
        )
        try:
            isolated = Path(worktree_path)
            blocked = isolated / "weird.link"
            self.assertFalse(
                blocked.is_symlink(),
                "newline-target symlink to gitignored file must be placeholdered",
            )
            self.assertEqual(
                blocked.read_text(encoding="utf-8"),
                self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
            )
            self.assertTrue(
                any(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX in item for item in warnings)
            )
            self.assertTrue(any("weird.link" in item for item in warnings))
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_git_safe_workspace_fail_closes_when_check_ignore_errors(self):
        """An unexpected ``git check-ignore`` failure (exit 128) FAILS CLOSED:
        every queried inside-root symlink is placeholdered and a distinct
        warning is emitted, but the sync still completes (no exception)."""
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        # Two non-ignored inside-root relative symlinks that would normally be
        # recreated; under fail-closed they must both be placeholdered.
        (root / "target_a.txt").write_text("inside-a\n", encoding="utf-8")
        (root / "target_b.txt").write_text("inside-b\n", encoding="utf-8")
        (root / "good_a.link").symlink_to("target_a.txt")
        (root / "good_b.link").symlink_to("target_b.txt")

        original_run_git_bytes = safe_workspace._run_git_bytes

        def failing_check_ignore(cwd, args, *, input_bytes=None, timeout_seconds=None):
            if args and args[0] == "check-ignore":
                return subprocess.CompletedProcess(
                    ["git", *args],
                    128,
                    b"",
                    b"fatal: simulated check-ignore failure",
                )
            return original_run_git_bytes(
                cwd, args, input_bytes=input_bytes, timeout_seconds=timeout_seconds
            )

        with mock.patch.object(safe_workspace, "_run_git_bytes", side_effect=failing_check_ignore):
            worktree_path, temp_base, warnings = self.delegate.create_git_safe_workspace(
                repo.name,
                include_warnings=True,
            )
        try:
            isolated = Path(worktree_path)
            for name in ("good_a.link", "good_b.link"):
                blocked = isolated / name
                self.assertFalse(
                    blocked.is_symlink(),
                    f"{name} must be placeholdered under check-ignore fail-closed",
                )
                self.assertEqual(
                    blocked.read_text(encoding="utf-8"),
                    self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
                )
            # Fail-closed notice is emitted alongside the per-path blocked warning.
            self.assertIn(
                safe_workspace.SAFE_CHECK_IGNORE_FAIL_CLOSED_WARNING,
                warnings,
            )
            # Sync completed (no exception); the tracked target file is still
            # mirrored so the workspace is otherwise intact.
            self.assertEqual(
                (isolated / "target_a.txt").read_text(encoding="utf-8"),
                "inside-a\n",
            )
        finally:
            self.delegate.cleanup_safe_isolated_workspace(
                git_root=repo.name,
                isolated_workspace=worktree_path,
                temp_base=temp_base,
            )

    def test_safe_dirty_tree_note_reaches_prompt_transport(self):
        repo = self.make_dirty_repo()
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

        self.assertIn("2 file(s) have uncommitted/untracked changes", request.stdin_text)
        self.assertIn("`tracked.txt`", request.stdin_text)
        self.assertIn("`notes.txt`", request.stdin_text)
        self.assertIn("git diff HEAD", request.stdin_text)

    def test_safe_clean_tree_omits_dirty_tree_note(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
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

        self.assertNotIn("uncommitted/untracked changes synced", request.stdin_text or "")

    def _assert_safe_prompt_paths_are_rerooted(self, engine, transport_field):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        source = repo.name
        external = f"/delegate-external{source}/reference.txt"
        prompt = (
            f"Review {source}/src/module.py and {external}; "
            f"leave {source}-backup/src/module.py unchanged."
        )
        iso_ctx = self.delegate.build_isolation_context(
            source_workspace=source,
            resolved_isolation="auto",
            engine=engine,
            mode="safe",
            source_git_root=source,
        )
        request = self.delegate.build_request(
            engine,
            "safe",
            None,
            self.delegate.ResolvedWorkspace(source, "git"),
            prompt,
            self.delegate.DEFAULT_CONFIG,
            dry_run=False,
            isolation_context=iso_ctx,
        )

        with self.delegate.safe_isolated_request(request) as isolated:
            if transport_field == "argv":
                transported = isolated.argv[-1]
            else:
                transported = getattr(isolated, transport_field)
            self.assertIsNotNone(transported)
            self.assertIn(f"{isolated.workspace}/src/module.py", transported)
            self.assertNotIn(f"{source}/src/module.py", transported)
            self.assertIn(external, transported)
            self.assertIn(f"{source}-backup/src/module.py", transported)
            self.assertIn("cite files relative to the workspace", transported)
            self.assertIn(f"{isolated.workspace}/src/module.py", isolated.prompt)

    def test_safe_isolation_reroots_argv_prompt_paths(self):
        self._assert_safe_prompt_paths_are_rerooted("cursor", "argv")

    def test_safe_isolation_reroots_stdin_prompt_paths(self):
        self._assert_safe_prompt_paths_are_rerooted("codex", "stdin_text")

    def test_safe_isolation_reroots_prompt_file_paths(self):
        self._assert_safe_prompt_paths_are_rerooted("grok", "prompt_file_text")

    def _assert_safe_slash_prompt_is_verbatim(self, engine, transport_field, workspace_flag):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        source = repo.name
        prompt = f"/goal inspect {source}/src/module.py byte-for-byte"
        iso_ctx = self.delegate.build_isolation_context(
            source_workspace=source,
            resolved_isolation="auto",
            engine=engine,
            mode="safe",
            source_git_root=source,
        )
        request = self.delegate.build_request(
            engine,
            "safe",
            None,
            self.delegate.ResolvedWorkspace(source, "git"),
            prompt,
            self.delegate.DEFAULT_CONFIG,
            dry_run=False,
            isolation_context=iso_ctx,
            prompt_instruction_mode=PROMPT_INSTRUCTION_MODE_SLASH,
        )
        if transport_field == "argv":
            # Cursor safe rejects slash prompts before this isolation layer, so
            # provide the already-resolved verbatim payload directly here.
            request.argv[-1] = prompt

        with self.delegate.safe_isolated_request(request) as isolated:
            transported = (
                isolated.argv[-1]
                if transport_field == "argv"
                else getattr(isolated, transport_field)
            )
            self.assertEqual(isolated.prompt, prompt)
            self.assertEqual(transported, prompt)
            self.assertNotIn("Safe-isolation note", transported)
            self.assertEqual(
                isolated.argv[isolated.argv.index(workspace_flag) + 1],
                isolated.workspace,
            )

    def test_safe_isolation_preserves_slash_argv_prompt(self):
        self._assert_safe_slash_prompt_is_verbatim("cursor", "argv", "--workspace")

    def test_safe_isolation_preserves_slash_stdin_prompt(self):
        self._assert_safe_slash_prompt_is_verbatim("codex", "stdin_text", "--cd")

    def test_safe_isolation_preserves_slash_prompt_file(self):
        self._assert_safe_slash_prompt_is_verbatim("grok", "prompt_file_text", "--cwd")

    def test_safe_workspace_path_replacement_requires_exact_prefix_boundary(self):
        source = "/workspace/repo"
        isolated = "/tmp/delegate-safe/wt"
        self.assertEqual(
            safe_workspace.replace_workspace_path_prefix(source, source, isolated),
            isolated,
        )
        self.assertEqual(
            safe_workspace.replace_workspace_path_prefix(
                f"{source}/src/main.py {source}-backup/src/main.py /external{source}/main.py",
                source,
                isolated,
            ),
            f"{isolated}/src/main.py {source}-backup/src/main.py /external{source}/main.py",
        )

    def test_safe_isolated_request_does_not_rewrite_work_mode(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        prompt = f"Edit {repo.name}/src/module.py"
        iso_ctx = self.delegate.build_isolation_context(
            source_workspace=repo.name,
            resolved_isolation="auto",
            engine="cursor",
            mode="work",
            source_git_root=repo.name,
        )
        request = self.delegate.build_request(
            "cursor",
            "work",
            None,
            self.delegate.ResolvedWorkspace(repo.name, "git"),
            prompt,
            self.delegate.DEFAULT_CONFIG,
            dry_run=False,
            isolation_context=iso_ctx,
        )

        with self.delegate.safe_isolated_request(request) as unchanged:
            self.assertIs(unchanged, request)
            self.assertEqual(unchanged.prompt, prompt)

    def test_safe_isolated_request_preserves_call_metadata(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
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
            self.delegate.ResolvedWorkspace(repo.name, "git"),
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=False,
            isolation_context=iso_ctx,
        )
        request.call_read_only = True
        request.pure = True
        request.timeout = 42
        request.model_requested = "requested-model"

        with self.delegate.safe_isolated_request(request) as isolated:
            self.assertTrue(isolated.call_read_only)
            self.assertTrue(isolated.pure)
            self.assertEqual(isolated.timeout, 42)
            self.assertEqual(isolated.model_requested, "requested-model")

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
