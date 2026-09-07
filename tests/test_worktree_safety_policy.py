from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry as registry_api
from delegate_agent import worktree_gc as worktree_gc_api
from delegate_agent import worktree_mgmt
from delegate_agent import worktree_mgmt as worktree_api
from delegate_agent import worktree_remove as worktree_remove_api
from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase


class WorktreeSafetyPolicyTests(unittest.TestCase):
    def _inspection(self, **changes):
        values = {
            "record": {
                "runId": "del_20260905T000000Z_abcdef",
                "branch": "delegate/test",
            },
            "status": worktree_mgmt.STATUS_PRESENT,
            "status_warnings": (),
            "dirty": False,
            "dirty_paths": (),
            "dirty_warnings": (),
            "branch_merged": True,
            "merge_warnings": (),
            "owner_block": None,
            "attachments": (),
        }
        values.update(changes)
        return worktree_mgmt.WorktreeInspection(**values)

    def test_policy_keeps_command_specific_refusals_distinct(self):
        live_owner = self._inspection(owner_block="run_active")
        attached = self._inspection(attachments=({"runId": "del_attached"},))
        dirty = self._inspection(dirty=True, dirty_paths=("work.txt",))
        unmerged = self._inspection(branch_merged=False)
        unknown = self._inspection(branch_merged=None)

        self.assertEqual(
            worktree_mgmt.evaluate_worktree_safety(live_owner).reason,
            "run_active",
        )
        self.assertEqual(
            worktree_mgmt.evaluate_worktree_safety(attached).reason,
            "live_attachment",
        )
        self.assertEqual(
            worktree_mgmt.evaluate_worktree_safety(dirty).reason,
            "dirty",
        )
        self.assertEqual(
            worktree_mgmt.evaluate_worktree_safety(unmerged, require_merged=True).reason,
            "unmerged_branch",
        )
        self.assertEqual(
            worktree_mgmt.evaluate_worktree_safety(unknown, require_merged=True).reason,
            "merge_check_failed",
        )

    def test_policy_allows_explicit_force_but_not_attachment_bypass(self):
        owner = self._inspection(owner_block="process_group_alive")
        self.assertIsNone(worktree_mgmt.evaluate_worktree_safety(owner, force=True).reason)

        attached = self._inspection(
            owner_block="process_group_alive", attachments=({"runId": "del_attached"},)
        )
        self.assertEqual(
            worktree_mgmt.evaluate_worktree_safety(attached, force=True).reason,
            "live_attachment",
        )

    def test_prune_can_remove_clean_unmerged_path_while_keeping_branch(self):
        decision = worktree_mgmt.evaluate_worktree_safety(
            self._inspection(branch_merged=False),
            require_merged=True,
            allow_unmerged_clean_keep=True,
        )
        self.assertIsNone(decision.reason)
        self.assertTrue(decision.keep_branch)


class LockedWorktreeSafetyTests(WorktreeMgmtTestBase):
    def test_remove_refuses_attached_run_from_fresh_locked_inspection(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/attached-owner"
            worktree = str(Path(fake_home) / "wt" / "attached-owner")
            _owner_run, owner_alias = self._seed_persistent_run(
                path,
                alias="attached-owner",
                branch=branch,
                execution_cwd=worktree,
            )
            self._create_worktree_at(path, branch, worktree)
            root = self._registry_root(path)
            attached_run, attached_alias = registry_api.register_run(
                root,
                harness="cursor",
                metadata={"mode": "work", "cwd": path},
            )
            attached_path = registry_api.run_directory(root, attached_run)
            registry_api.write_json_atomic(
                attached_path / "manifest.json",
                {
                    "runId": attached_run,
                    "alias": attached_alias,
                    "isolationLifecycle": "attached",
                    "worktreeAttachment": {"path": worktree},
                },
            )
            registry_api.write_json_atomic(
                attached_path / "state.json",
                {"runId": attached_run, "status": "running", "pid": os.getpid()},
            )

            with self.assertRaises(worktree_mgmt.WorktreeManagementError) as context:
                worktree_remove_api.remove_worktree(root, handle=owner_alias)

            self.assertEqual(context.exception.code, "worktree_attached")
            self.assertTrue(Path(worktree).exists())

    def test_prune_rechecks_dirty_state_before_removal(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/recheck-dirty"
            worktree = str(Path(fake_home) / "wt" / "recheck-dirty")
            _run_id, alias = self._seed_persistent_run(
                path,
                alias="recheck-dirty",
                branch=branch,
                execution_cwd=worktree,
            )
            self._create_worktree_at(path, branch, worktree)
            root = self._registry_root(path)
            record = worktree_api.resolve_record(root, handle=alias)
            clean = worktree_api.inspect_worktree(root, record)
            dirty = replace(clean, dirty=True, dirty_paths=("changed.txt",))

            with mock.patch.object(
                worktree_api,
                "inspect_worktree",
                side_effect=(clean, dirty),
            ):
                result = worktree_gc_api.prune_worktrees(
                    root,
                    merged=True,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"][0]["code"], "dirty_worktree")
            self.assertTrue(Path(worktree).exists())

    def test_blocked_owner_does_not_probe_dirt_or_merge(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/blocked-before-probes"
            worktree = str(Path(fake_home) / "wt" / "blocked-before-probes")
            run_id, alias = self._seed_persistent_run(
                path,
                alias="blocked-before-probes",
                branch=branch,
                execution_cwd=worktree,
            )
            self._create_worktree_at(path, branch, worktree)
            state_path = (
                registry_api.run_directory(self._registry_root(path), run_id) / "state.json"
            )
            state = registry_api.load_run_state(self._registry_root(path), run_id)
            state.update({"status": "running", "pid": os.getpid()})
            registry_api.write_json_atomic(state_path, state)

            with (
                mock.patch.object(
                    worktree_api,
                    "dirty_info",
                    side_effect=AssertionError("dirty probe should be skipped"),
                ),
                mock.patch.object(
                    worktree_api,
                    "merged_into_source",
                    side_effect=AssertionError("merge probe should be skipped"),
                ),
                self.assertRaises(worktree_api.WorktreeManagementError) as context,
            ):
                worktree_remove_api.remove_worktree(
                    self._registry_root(path),
                    handle=alias,
                )

            self.assertEqual(context.exception.code, "run_active")

    def test_keep_branch_inspection_skips_merge_probe(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/keep-branch-no-merge"
            worktree = str(Path(fake_home) / "wt" / "keep-branch-no-merge")
            _run_id, alias = self._seed_persistent_run(
                path,
                alias="keep-branch-no-merge",
                branch=branch,
                execution_cwd=worktree,
            )
            self._create_worktree_at(path, branch, worktree)
            root = self._registry_root(path)
            record = worktree_api.resolve_record(root, handle=alias)
            with mock.patch.object(
                worktree_api,
                "merged_into_source",
                side_effect=AssertionError("merge probe should be skipped"),
            ):
                inspection = worktree_api.inspect_worktree(
                    root,
                    record,
                    check_merge=False,
                )

            self.assertIsNone(inspection.branch_merged)


if __name__ == "__main__":
    unittest.main()
