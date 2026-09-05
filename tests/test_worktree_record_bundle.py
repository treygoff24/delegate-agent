from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from delegate_agent import run_registry, worktree_records
from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase


class WorktreeRecordBundleTests(unittest.TestCase):
    def test_partial_legacy_fallback_and_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "del_20260905T000000Z_abcdef"
            run_path = run_registry.run_directory(root, run_id)
            run_path.mkdir(parents=True)
            snapshot = {
                "isolationLifecycle": "persistent",
                "alias": "snapshot",
                "branch": "delegate/x",
                "executionCwd": str(root / "tree"),
                "sourceGitRoot": str(root / "source"),
                "creationContext": {"plannedBranch": "delegate/x"},
            }
            run_registry.write_json_atomic(run_path / run_registry.SNAPSHOT_FILE, snapshot)
            record = worktree_records._record_for_run(
                root, run_id, {"alias": "index", "harness": "codex"}
            )
            self.assertEqual(record["alias"], "snapshot")
            self.assertEqual(record["branch"], "delegate/x")
            self.assertEqual(record["harness"], "codex")
            self.assertNotIn("recordWarnings", record)
            run_registry.write_json_atomic(
                run_path / run_registry.STATE_FILE, {"alias": "state", "worktreeStatus": "present"}
            )
            run_registry.write_json_atomic(
                run_path / run_registry.MANIFEST_FILE, {**snapshot, "alias": "manifest"}
            )
            record = worktree_records._record_for_run(root, run_id, {"alias": "index"})
            self.assertEqual(record["alias"], "state")
            self.assertNotIn("recordWarnings", record)

    def test_conflicting_or_corrupt_evidence_has_no_destructive_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "del_20260905T000000Z_abcdef"
            run_path = run_registry.run_directory(root, run_id)
            run_path.mkdir(parents=True)
            manifest = {
                "isolationLifecycle": "persistent",
                "branch": "delegate/x",
                "sourceGitRoot": str(root),
                "executionCwd": str(root / "tree"),
            }
            run_registry.write_json_atomic(run_path / run_registry.MANIFEST_FILE, manifest)
            for snapshot in (
                {**manifest, "branch": "delegate/other"},
                {**manifest, "runId": "other"},
            ):
                with self.subTest(snapshot=snapshot):
                    run_registry.write_json_atomic(run_path / run_registry.SNAPSHOT_FILE, snapshot)
                    record = worktree_records._record_for_run(root, run_id, {})
                    self.assertTrue(record["recordWarnings"])
                    self.assertIsNone(record["sourceGitRoot"])
                    self.assertEqual(record["registryWorktreeStatus"], "unknown")
            (run_path / run_registry.SNAPSHOT_FILE).write_text("not json")
            record = worktree_records._record_for_run(root, run_id, {})
            self.assertIn("unreadable snapshot.json", record["recordWarnings"])
            self.assertIsNone(record["sourceGitRoot"])


class WorktreeRecordConflictRemovalTests(WorktreeMgmtTestBase):
    def test_force_removal_retains_actual_worktree_with_conflicting_records(self) -> None:
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/conflict"
            tree = str(Path(fake_home) / "tree")
            run_id, alias = self._seed_persistent_run(path, branch=branch, execution_cwd=tree)
            self._create_worktree_at(path, branch, tree)
            root = self._registry_root(path)
            snapshot_path = run_registry.run_directory(root, run_id) / run_registry.SNAPSHOT_FILE
            snapshot = run_registry.read_json_object(snapshot_path) or {}
            snapshot["executionCwd"] = str(Path(fake_home) / "other-tree")
            run_registry.write_json_atomic(snapshot_path, snapshot)
            code, _out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", alias, "--force"], home=fake_home
            )
            self.assertNotEqual(code, 0)
            self.assertTrue(Path(tree).is_dir())
            self.assertTrue((Path(tree) / ".git").is_file())


if __name__ == "__main__":
    unittest.main()
