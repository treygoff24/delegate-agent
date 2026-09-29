"""Symlink placeholders in a persistent worktree cover only what the sync mirrored.

A persistent worktree is a real checkout that gets committed from. Its launch
sync used to end with the safe-copy sweep that replaces every symlink pointing
outside the source with a placeholder file, tracked ones included, so a repo
with a committed absolute link (a shared cache, a dotfile) started every
worktree run with that link shown as a typechange. Only untracked paths the
sync itself mirrored in may be placeholdered; safe copies keep the whole sweep.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import safe_workspace as safe_api
from tests.execution_test_base import ExecutionTestBase

IDENTITY = ("-c", "user.name=Delegate Test", "-c", "user.email=delegate-test@example.com")


def git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *IDENTITY, *args], text=True, capture_output=True, check=True
    )


class SymlinkFixtureMixin:
    """A repo with a committed absolute link, a committed relative link, and one dirty file."""

    def _repo_with_tracked_links(self, outside: Path) -> tuple[Path, Path]:
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        root = Path(repo.name)
        git(repo.name, "init")
        host_file = outside / "host-secret.txt"
        host_file.write_text("host secret\n", encoding="utf-8")
        (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
        (root / "inside.txt").write_text("inside\n", encoding="utf-8")
        os.symlink(host_file, root / "abs-link")
        os.symlink("inside.txt", root / "rel-link")
        git(repo.name, "add", "tracked.txt", "inside.txt", "abs-link", "rel-link")
        git(repo.name, "commit", "-m", "base")
        (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        return root, host_file


class PersistentWorktreeSyncTests(SymlinkFixtureMixin, unittest.TestCase):
    def _add_worktree(self, root: Path) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        worktree = Path(holder.name) / "wt"
        git(str(root), "worktree", "add", "-B", "delegate/sync-test", str(worktree), "HEAD")
        return worktree

    def _porcelain(self, worktree: Path) -> list[str]:
        return git(str(worktree), "status", "--porcelain").stdout.splitlines()

    def test_tracked_absolute_symlink_survives_the_launch_sync(self):
        with tempfile.TemporaryDirectory() as outside:
            root, host_file = self._repo_with_tracked_links(Path(outside))
            worktree = self._add_worktree(root)

            safe_api.sync_git_dirty_snapshot(str(root), str(worktree), persistent_worktree=True)

            self.assertTrue((worktree / "abs-link").is_symlink())
            self.assertEqual(os.readlink(worktree / "abs-link"), str(host_file))
            self.assertTrue((worktree / "rel-link").is_symlink())
            self.assertEqual((worktree / "tracked.txt").read_text(encoding="utf-8"), "dirty\n")
            self.assertEqual(self._porcelain(worktree), [" M tracked.txt"])

    def test_untracked_external_symlink_is_still_placeholdered_and_reported(self):
        with tempfile.TemporaryDirectory() as outside:
            root, host_file = self._repo_with_tracked_links(Path(outside))
            os.symlink(host_file, root / "new-link")
            worktree = self._add_worktree(root)

            warnings = safe_api.sync_git_dirty_snapshot(
                str(root), str(worktree), persistent_worktree=True
            )[3]

            placeholder = worktree / "new-link"
            self.assertFalse(placeholder.is_symlink())
            self.assertEqual(
                placeholder.read_text(encoding="utf-8"), safe_api.SAFE_BLOCKED_SYMLINK_PLACEHOLDER
            )
            self.assertTrue((worktree / "abs-link").is_symlink())
            reported = "\n".join(warnings)
            self.assertIn("new-link", reported)
            self.assertNotIn("abs-link", reported)
            self.assertNotIn(str(host_file), reported)

    def test_untracked_internal_symlink_stays_a_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            root, _host_file = self._repo_with_tracked_links(Path(outside))
            os.symlink("inside.txt", root / "fresh-link")
            worktree = self._add_worktree(root)

            safe_api.sync_git_dirty_snapshot(str(root), str(worktree), persistent_worktree=True)

            self.assertTrue((worktree / "fresh-link").is_symlink())
            self.assertEqual(os.readlink(worktree / "fresh-link"), "inside.txt")

    def test_the_safe_copy_sweep_still_placeholders_a_tracked_external_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            root, host_file = self._repo_with_tracked_links(Path(outside))

            worktree_path, temp_base, warnings = safe_api.create_git_safe_workspace(
                str(root), include_warnings=True
            )
            try:
                link = Path(worktree_path) / "abs-link"
                self.assertFalse(link.is_symlink())
                self.assertEqual(
                    link.read_text(encoding="utf-8"), safe_api.SAFE_BLOCKED_SYMLINK_PLACEHOLDER
                )
                self.assertTrue((Path(worktree_path) / "rel-link").is_symlink())
                self.assertNotIn(str(host_file), "\n".join(warnings) + link.read_text("utf-8"))
            finally:
                safe_api.discard_git_safe_workspace(
                    str(root), worktree_path, temp_base, worktree_added=True
                )


class PersistentWorktreeLaunchTests(SymlinkFixtureMixin, ExecutionTestBase):
    def _config(self, agent: Path, data_home: str) -> dict:
        from delegate_agent import config as config_api

        config = config_api.embedded_default_config()
        config["cursor"]["argvPrefix"] = [str(agent)]
        config["worktrees"]["dataHome"] = data_home
        # The test inspects the realized worktree after completion.
        config["worktrees"]["retireWorktreeOnCompletion"] = False
        return config

    def test_worktree_isolated_launch_shows_no_symlink_in_git_status(self):
        with tempfile.TemporaryDirectory() as fake_home, tempfile.TemporaryDirectory() as outside:
            root, host_file = self._repo_with_tracked_links(Path(outside))
            agent = Path(fake_home) / "agent"
            agent.write_text(
                "#!/usr/bin/env bash\n"
                'printf \'{"type":"result","result":"Status: completed\\\\n- symlink fake"}\\n\'\n'
                "exit 0\n",
                encoding="utf-8",
            )
            agent.chmod(0o755)
            config_path = Path(fake_home) / "config.json"
            config_path.write_text(
                json.dumps(self._config(agent, str(Path(fake_home) / "worktrees"))),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(
                os.environ,
                {"HOME": fake_home, "DELEGATE_CONFIG": str(config_path)},
                clear=False,
            ):
                code = self.delegate.main(
                    [
                        "--cwd",
                        str(root),
                        "--json",
                        "--isolation",
                        "worktree",
                        "cursor",
                        "work",
                        "hello",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(code, 0, stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            execution_cwd = Path(payload["executionCwd"])
            porcelain = git(str(execution_cwd), "status", "--porcelain").stdout.splitlines()
            self.assertEqual(porcelain, [" M tracked.txt"])
            self.assertEqual(os.readlink(execution_cwd / "abs-link"), str(host_file))
            self.assertNotIn("abs-link", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
