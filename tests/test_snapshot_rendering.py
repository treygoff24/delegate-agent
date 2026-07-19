import io
import unittest

from tests.snapshot_commands_test_base import SnapshotCommandTestBase


class SnapshotRenderingTests(SnapshotCommandTestBase):
    def test_render_worktree_cleanup_commands_formats_all_lines(self):
        cleanup = {
            "safe": "delegate worktree remove demo",
            "forceBranch": "delegate worktree remove demo --force-branch",
            "discardUncommitted": "delegate worktree remove demo --discard-uncommitted",
            "force": "delegate worktree remove demo --force",
            "rawGit": "git -C /repo worktree remove /wt && git -C /repo branch -d demo",
        }
        stdout = io.StringIO()
        self.rendering.render_worktree_cleanup_commands(cleanup, stdout)
        output = stdout.getvalue()
        self.assertEqual(
            output,
            (
                "cleanup (refuses dirty / unmerged):       delegate worktree remove demo\n"
                "cleanup (allow unmerged branch deletion): delegate worktree remove demo --force-branch\n"
                "cleanup (DISCARD uncommitted edits):      delegate worktree remove demo --discard-uncommitted\n"
                "cleanup (DISCARD edits + delete branch):  delegate worktree remove demo --force\n"
                "raw git equivalent:                       git -C /repo worktree remove /wt && git -C /repo branch -d demo\n"
            ),
        )

    def test_render_snapshot_text_uses_shared_cleanup_renderer(self):
        stdout = io.StringIO()
        self.rendering.render_snapshot_text(
            {
                "alias": "demo",
                "status": "running",
                "worktreeCleanupCommands": {
                    "safe": "delegate worktree remove demo",
                    "forceBranch": "delegate worktree remove demo --force-branch",
                    "discardUncommitted": "delegate worktree remove demo --discard-uncommitted",
                    "force": "delegate worktree remove demo --force",
                    "rawGit": "git -C /repo worktree remove /wt",
                },
            },
            stdout,
        )
        output = stdout.getvalue()
        self.assertIn(
            "cleanup (refuses dirty / unmerged):       delegate worktree remove demo", output
        )
        self.assertIn(
            "cleanup (DISCARD edits + delete branch):  delegate worktree remove demo --force",
            output,
        )
        self.assertIn(
            "raw git equivalent:                       git -C /repo worktree remove /wt", output
        )

    def test_render_worktree_list_text_includes_auto_prune_footer(self):
        stdout = io.StringIO()
        self.rendering.render_worktree_list_text(
            {
                "entries": [],
                "autoPrune": {
                    "ok": True,
                    "removed": [{"alias": "cursor-1"}],
                    "skipped": [],
                    "errors": [],
                },
            },
            stdout,
        )
        self.assertIn("auto-prune: removed 1, skipped 0, errors 0", stdout.getvalue())

    def test_render_worktree_list_text_includes_auto_prune_skip_and_failure(self):
        skipped = io.StringIO()
        self.rendering.render_worktree_list_text(
            {
                "entries": [],
                "autoPrune": {"ok": False, "skipped": True, "reason": "lock_contended"},
            },
            skipped,
        )
        self.assertIn("auto-prune: skipped (lock_contended)", skipped.getvalue())

        failed = io.StringIO()
        self.rendering.render_worktree_list_text(
            {
                "entries": [],
                "autoPrune": {
                    "ok": False,
                    "code": "branch_remove_failed",
                    "errors": [{"code": "branch_remove_failed"}],
                },
            },
            failed,
        )
        self.assertIn("auto-prune: failed (branch_remove_failed, errors=1)", failed.getvalue())

    def test_render_worktree_remove_text_includes_branch_error(self):
        stdout = io.StringIO()
        self.rendering.render_worktree_remove_text(
            {
                "alias": "cursor-1",
                "ok": False,
                "removed": True,
                "pathRemoved": True,
                "branchRemoved": False,
                "branchRemovalError": "fatal: cannot delete branch",
                "nextActions": ["delete branch manually"],
            },
            stdout,
        )
        output = stdout.getvalue()
        self.assertIn("branchRemoved=False", output)
        self.assertIn("error: fatal: cannot delete branch", output)
        self.assertIn("delete branch manually", output)

    def test_render_worktree_gc_text_includes_warnings(self):
        stdout = io.StringIO()
        self.rendering.render_worktree_gc_text(
            {
                "reconciled": 0,
                "prunedSourceRoots": 0,
                "orphans": [],
                "warnings": [{"sourceGitRoot": "/repo", "message": "fatal: worktree list failed"}],
            },
            stdout,
        )
        output = stdout.getvalue()
        self.assertIn("warnings:", output)
        self.assertIn("/repo: fatal: worktree list failed", output)

    def test_render_worktree_gc_dry_run_reports_would_prune_and_reasons(self):
        stdout = io.StringIO()
        self.rendering.render_worktree_gc_text(
            {
                "dryRun": True,
                "reconciled": 1,
                "prunedSourceRoots": 0,
                "wouldPruneSourceRoots": 1,
                "orphans": [
                    {
                        "alias": "cursor-1",
                        "executionCwd": "/pool/cursor-1",
                        "reason": "detached_backlink",
                    }
                ],
                "warnings": [],
            },
            stdout,
        )

        output = stdout.getvalue()
        self.assertIn("would prune source roots: 1", output)
        self.assertIn("cursor-1 detached_backlink", output)
        self.assertIn("/pool/cursor-1", output)


if __name__ == "__main__":
    unittest.main()
