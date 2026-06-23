import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git


class WorktreeListShowTests(WorktreeMgmtTestBase):
    def test_worktree_list_no_registry(self):
        repo, path = self._make_repo()
        code, out, _err = self._run_cli(
            ["--cwd", path, "--json", "worktree", "list"],
            home=repo.name,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        payload = json.loads(out)
        self.assertEqual(payload["code"], "no_registry")
        self.assertEqual(payload["error"], "no_registry")
        self.assertEqual(payload["exitCode"], self.delegate.EXIT_USAGE)

    def test_worktree_list_json_schema_and_status(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-list"
            wt_path = str(Path(fake_home) / "wt" / "cursor-list")
            self._seed_persistent_run(path, branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "list"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["schema"], "delegate.worktree-list.v1")
            self.assertEqual(len(payload["entries"]), 1)
            entry = payload["entries"][0]
            self.assertEqual(entry["worktreeStatus"], "present")
            self.assertFalse(entry["dirty"])

    def test_worktree_show_includes_porcelain_ahead_behind_and_commands(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            branch = "delegate/cursor-show"
            wt_path = str(Path(fake_home) / "wt" / "cursor-show")
            self._seed_persistent_run(
                path,
                alias="cursor-4",
                branch=branch,
                execution_cwd=wt_path,
                creation_oid=base_oid,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            (Path(wt_path) / "scratch.txt").write_text("scratch\n", encoding="utf-8")
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "show", "cursor-4"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["schema"], "delegate.worktree-show.v1")
            self.assertGreaterEqual(payload["aheadBehind"]["vsCreationBase"]["ahead"], 1)
            self.assertIn("?? scratch.txt", payload["porcelainStatus"])
            self.assertIn("safeRemove", payload["suggestedCommands"])
            self.assertEqual(payload["workSummary"]["commitsCreatedCount"], 1)
            self.assertEqual(payload["workSummary"]["changedFilesCount"], 1)

    def test_worktree_show_unknown_status_still_includes_inspection_data(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            branch = "delegate/cursor-show-unknown"
            wt_path = str(Path(fake_home) / "wt" / "cursor-show-unknown")
            self._seed_persistent_run(
                path,
                alias="cursor-show-unknown",
                branch=branch,
                execution_cwd=wt_path,
                creation_oid=base_oid,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            (Path(wt_path) / "scratch.txt").write_text("scratch\n", encoding="utf-8")

            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "detect_worktree_status",
                return_value=("unknown", ["forced unknown for inspection"]),
            ):
                payload = self.delegate.worktree_mgmt.show_worktree(
                    self._registry_root(path),
                    handle="cursor-show-unknown",
                )

            self.assertEqual(payload["worktreeStatus"], "unknown")
            self.assertGreaterEqual(payload["aheadBehind"]["vsCreationBase"]["ahead"], 1)
            self.assertIn("?? scratch.txt", payload["porcelainStatus"])
            self.assertIn("reviewDiff", payload["suggestedCommands"])

    def test_runs_json_exposes_persistent_worktree_metadata(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-runs-meta"
            wt_path = str(Path(fake_home) / "wt" / "cursor-runs-meta")
            self._seed_persistent_run(
                path, alias="cursor-runs", branch=branch, execution_cwd=wt_path
            )
            self._create_worktree_at(path, branch, wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "runs"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            entries = payload.get("runs", [])
            matching = [e for e in entries if e.get("alias") == "cursor-runs"]
            self.assertEqual(len(matching), 1)
            entry = matching[0]
            self.assertEqual(entry.get("isolationLifecycle"), "persistent")
            self.assertIn("executionCwd", entry)
            self.assertIn("worktreeStatus", entry)

    def test_worktree_list_harness_filter(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            self._seed_persistent_run(path, alias="cursor-a", harness="cursor")
            self._seed_persistent_run(path, alias="cursor-b", harness="cursor")
            self._seed_persistent_run(path, alias="droid-c", harness="droid")
            result = self.delegate.worktree_mgmt.list_worktrees(
                self._registry_root(path),
                harness="cursor",
            )
            entries = result.get("entries", [])
            self.assertEqual(len(entries), 2)
            for e in entries:
                self.assertEqual(e.get("harness"), "cursor")
            summary = result["summary"]
            # totalPersistentWorktrees is registry-wide; allStatusCounts is
            # scoped to the harness filter (pre-status-filter).
            self.assertEqual(summary["totalPersistentWorktrees"], 3)
            self.assertEqual(sum(summary["allStatusCounts"].values()), 2)
            self.assertEqual(summary["matched"], 2)

    def test_worktree_list_tolerates_corrupt_per_run_state_json(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            healthy_run_id, healthy_alias = self._seed_persistent_run(
                path,
                alias="cursor-healthy",
            )
            corrupt_run_id, corrupt_alias = self._seed_persistent_run(
                path,
                alias="cursor-corrupt",
            )
            corrupt_run_path = self.delegate.run_registry.run_directory(
                self._registry_root(path),
                corrupt_run_id,
            )
            (corrupt_run_path / "state.json").write_text("{garbage", encoding="utf-8")

            result = self.delegate.worktree_mgmt.list_worktrees(self._registry_root(path))

            self.assertEqual(result["summary"]["totalPersistentWorktrees"], 2)
            by_alias = {entry["alias"]: entry for entry in result["entries"]}
            self.assertIn(healthy_alias, by_alias)
            self.assertIn(corrupt_alias, by_alias)
            self.assertEqual(by_alias[healthy_alias]["runId"], healthy_run_id)
            self.assertEqual(by_alias[corrupt_alias]["runId"], corrupt_run_id)

    def test_worktree_list_status_filter(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch_a = "delegate/cursor-present"
            wt_path_a = str(Path(fake_home) / "wt" / "cursor-present")
            branch_b = "delegate/cursor-removed"
            wt_path_b = str(Path(fake_home) / "wt" / "cursor-removed")
            self._seed_persistent_run(
                path,
                alias="cursor-present",
                branch=branch_a,
                execution_cwd=wt_path_a,
                worktree_status="present",
            )
            self._seed_persistent_run(
                path,
                alias="cursor-removed",
                branch=branch_b,
                execution_cwd=wt_path_b,
                worktree_status="removed",
            )
            self._create_worktree_at(path, branch_a, wt_path_a)
            # Worktree at wt_path_b doesn't exist (simulates removed)
            present_result = self.delegate.worktree_mgmt.list_worktrees(
                self._registry_root(path),
                status="present",
            )
            self.assertEqual(len(present_result["entries"]), 1)
            self.assertEqual(present_result["entries"][0]["alias"], "cursor-present")
            removed_result = self.delegate.worktree_mgmt.list_worktrees(
                self._registry_root(path),
                status="removed",
            )
            self.assertEqual(len(removed_result["entries"]), 1)
            self.assertEqual(removed_result["entries"][0]["alias"], "cursor-removed")
            self.assertEqual(removed_result["summary"]["allStatusCounts"]["present"], 1)
            self.assertEqual(removed_result["summary"]["allStatusCounts"]["removed"], 1)
            self.assertEqual(removed_result["summary"]["statusCounts"], {"removed": 1})

    def test_worktree_list_summary_reports_registry_status_drift(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            missing_path = str(Path(fake_home) / "wt" / "missing-list")
            self._seed_persistent_run(
                path,
                alias="cursor-missing-list",
                execution_cwd=missing_path,
                worktree_status="present",
            )

            result = self.delegate.worktree_mgmt.list_worktrees(self._registry_root(path))

            entry = result["entries"][0]
            self.assertEqual(entry["registryWorktreeStatus"], "present")
            self.assertEqual(entry["worktreeStatus"], "missing")
            self.assertTrue(entry["registryStatusDiffers"])
            self.assertEqual(result["summary"]["registryStatusDriftCount"], 1)
            self.assertEqual(result["summary"]["statusCounts"], {"missing": 1})

    def test_worktree_public_payloads_do_not_expose_private_record_keys(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-public"
            wt_path = str(Path(fake_home) / "wt" / "cursor-public")
            self._seed_persistent_run(
                path, alias="cursor-public", branch=branch, execution_cwd=wt_path
            )
            self._create_worktree_at(path, branch, wt_path)

            list_payload = self.delegate.worktree_mgmt.list_worktrees(self._registry_root(path))
            show_payload = self.delegate.worktree_mgmt.show_worktree(
                self._registry_root(path),
                handle="cursor-public",
            )

            for payload in (list_payload["entries"][0], show_payload):
                private_keys = [key for key in payload if key.startswith("_")]
                self.assertEqual(private_keys, [])

    def test_worktree_show_latest_harness(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            from datetime import UTC, datetime, timedelta

            old_ts = (datetime.now(UTC) - timedelta(days=2)).strftime(
                self.delegate.run_registry.UTC_TIMESTAMP_FORMAT
            )
            self._seed_persistent_run(
                path, alias="droid-old", harness="droid", last_activity_at=old_ts
            )
            recent_ts = (datetime.now(UTC) - timedelta(hours=1)).strftime(
                self.delegate.run_registry.UTC_TIMESTAMP_FORMAT
            )
            self._seed_persistent_run(
                path, alias="droid-recent", harness="droid", last_activity_at=recent_ts
            )
            result = self.delegate.worktree_mgmt.show_worktree(
                self._registry_root(path),
                handle=None,
                latest_harness="droid",
            )
            self.assertEqual(result.get("alias"), "droid-recent")

    def test_worktree_show_latest_harness_ignores_newer_non_worktree_run(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            from datetime import UTC, datetime, timedelta

            worktree_ts = (datetime.now(UTC) - timedelta(hours=2)).strftime(
                self.delegate.run_registry.UTC_TIMESTAMP_FORMAT
            )
            self._seed_persistent_run(
                path,
                alias="droid-worktree",
                harness="droid",
                last_activity_at=worktree_ts,
            )
            plain_ts = (datetime.now(UTC) - timedelta(minutes=5)).strftime(
                self.delegate.run_registry.UTC_TIMESTAMP_FORMAT
            )
            self._seed_plain_run(path, harness="droid", last_activity_at=plain_ts)

            result = self.delegate.worktree_mgmt.show_worktree(
                self._registry_root(path),
                handle=None,
                latest_harness="droid",
            )

            self.assertEqual(result.get("alias"), "droid-worktree")

    def test_worktree_unknown_handle_suggestions_are_scoped_to_worktrees(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            self._seed_plain_run(path, harness="cursor")
            self._seed_persistent_run(path, alias="cursor-worktree", harness="cursor")

            with self.assertRaises(self.delegate.worktree_mgmt.WorktreeManagementError) as ctx:
                self.delegate.worktree_mgmt.show_worktree(
                    self._registry_root(path),
                    handle="cursorx",
                )

            payload = ctx.exception.payload
            self.assertEqual(payload["code"], "unknown_handle")
            self.assertEqual(payload["suggestionScope"], "worktrees")
            self.assertEqual(payload["suggestions"], ["cursor-worktree"])
            self.assertEqual(
                payload["nextActions"],
                ["delegate worktree show cursor-worktree"],
            )
            self.assertEqual(payload["listCommand"], "delegate worktree list")
            self.assertNotIn("cursor, ", payload["message"])

    def test_worktree_show_text_render_order(self):
        """render_worktree_show_text outputs lines in spec L621 order:
        creation-context → dirty → merged → ahead/behind → porcelain → suggested-commands → trailing metadata."""
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-order"
            wt_path = str(Path(fake_home) / "wt" / "cursor-order")
            base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-order",
                branch=branch,
                execution_cwd=wt_path,
                creation_oid=base_oid,
            )
            self._create_worktree_at(path, branch, wt_path)
            # Build a payload with all fields populated.
            from delegate_agent import rendering as delegate_rendering

            payload = {
                "alias": "cursor-order",
                "runId": run_id,
                "worktreeStatus": "present",
                "creationContext": {
                    "sourceHeadOid": base_oid,
                    "sourceHeadRef": "refs/heads/main",
                    "sourceBranch": "main",
                    "sourceGitCommonDir": str(Path(path) / ".git"),
                },
                "currentSourceHeadRef": "refs/heads/main",
                "dirty": True,
                "branchMergedIntoSource": False,
                "mergedIntoSource": False,
                "fullyIntegrated": False,
                "integrationStatus": "branch-unmerged-worktree-dirty",
                "aheadBehind": {
                    "vsCreationBase": {"ahead": 3, "behind": 0, "baseOid": base_oid},
                    "vsCurrentHead": {"ahead": 3, "behind": 2, "baseOid": base_oid},
                },
                "porcelainStatus": [],  # clean worktree
                "suggestedCommands": {
                    "reviewDiff": "git diff HEAD",
                },
                "executionCwd": wt_path,
                "sourceGitRoot": path,
                "branch": branch,
                "harness": "cursor",
            }
            output = io.StringIO()
            delegate_rendering.render_worktree_show_text(payload, output)
            lines = output.getvalue()

            # Assert ordering per spec L621.
            idx_creation = lines.find("created from")
            idx_dirty = lines.find("dirty: yes")
            idx_merged = lines.find("merged: no")
            idx_branch_merged = lines.find("branch merged: no")
            idx_fully_integrated = lines.find("fully integrated: no")
            idx_integration_status = lines.find("integration status:")
            idx_ahead = lines.find("vs creation base")
            idx_porcelain = lines.find("porcelain: clean")
            idx_suggested = lines.find("suggested commands")
            idx_trail_meta = lines.find("execution:")  # start of trailing metadata

            self.assertGreater(idx_creation, -1, "creation-context line missing")
            self.assertGreater(idx_dirty, -1, "dirty line missing")
            self.assertGreater(idx_merged, -1, "legacy merged line missing")
            self.assertGreater(idx_branch_merged, -1, "branch merged line missing")
            self.assertGreater(idx_fully_integrated, -1, "fully integrated line missing")
            self.assertGreater(idx_integration_status, -1, "integration status line missing")
            self.assertGreater(idx_ahead, -1, "ahead/behind line missing")
            self.assertGreater(idx_porcelain, -1, "porcelain: clean line missing")
            self.assertGreater(idx_suggested, -1, "suggested commands missing")
            self.assertGreater(idx_trail_meta, -1, "trailing metadata missing")

            # Assert each section appears after the previous one.
            self.assertLess(idx_creation, idx_dirty)
            self.assertLess(idx_dirty, idx_merged)
            self.assertLess(idx_merged, idx_branch_merged)
            self.assertLess(idx_branch_merged, idx_fully_integrated)
            self.assertLess(idx_fully_integrated, idx_integration_status)
            self.assertLess(idx_integration_status, idx_ahead)
            self.assertLess(idx_ahead, idx_porcelain)
            self.assertLess(idx_porcelain, idx_suggested)
            # Trailing metadata should come AFTER suggested commands (spec L621 order).
            self.assertLess(idx_suggested, idx_trail_meta)

    def test_ahead_behind_vs_creation_base_and_current_head(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            branch = "delegate/cursor-ahead"
            wt_path = str(Path(fake_home) / "wt" / "cursor-ahead")
            self._seed_persistent_run(
                path,
                alias="cursor-ahead",
                branch=branch,
                execution_cwd=wt_path,
                creation_oid=base_oid,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            # Make a new commit on the SOURCE to advance HEAD (not the worktree branch)
            (Path(path) / "newfile.txt").write_text("new\n", encoding="utf-8")
            git("add", "newfile.txt", cwd=path)
            git("commit", "-m", "new source commit", cwd=path)
            new_commit = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            self.assertNotEqual(new_commit, base_oid)
            index = self.delegate.run_registry.load_index(self._registry_root(path))
            run_id = index["aliases"].get("cursor-ahead")
            record = self.delegate.worktree_mgmt._record_for_run(
                self._registry_root(path), run_id, {}
            )
            result = self.delegate.worktree_mgmt.ahead_behind(record, "present")
            self.assertIsNotNone(result)
            self.assertEqual(result["vsCreationBase"]["baseOid"], base_oid)
            self.assertEqual(result["vsCurrentHead"]["baseOid"], new_commit)
            self.assertNotEqual(
                result["vsCreationBase"]["baseOid"],
                result["vsCurrentHead"]["baseOid"],
            )

    def test_branch_merged_dirty_worktree_reports_partial_integration(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            branch = "delegate/cursor-merged-dirty"
            wt_path = str(Path(fake_home) / "wt" / "cursor-merged-dirty")
            self._seed_persistent_run(
                path,
                alias="cursor-merged-dirty",
                branch=branch,
                execution_cwd=wt_path,
                creation_oid=base_oid,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            git("merge", "--no-ff", branch, cwd=path, check=False)
            (Path(wt_path) / "scratch.txt").write_text("scratch\n", encoding="utf-8")

            payload = self.delegate.worktree_mgmt.show_worktree(
                self._registry_root(path),
                handle="cursor-merged-dirty",
            )

            self.assertTrue(payload["branchMergedIntoSource"])
            self.assertTrue(payload["mergedIntoSource"])
            self.assertFalse(payload["fullyIntegrated"])
            self.assertTrue(payload["hasUncommittedChanges"])
            self.assertFalse(payload["uncommittedChangesIntegrated"])
            self.assertEqual(payload["integrationStatus"], "branch-merged-worktree-dirty")
            self.assertEqual(payload["aheadBehind"]["vsCurrentHead"]["ahead"], 0)
            commands = payload["suggestedCommands"]
            self.assertIsNotNone(commands["reviewDiff"])
            self.assertIsNone(commands["mergeIntoSource"])
            self.assertIsNone(commands["cherryPickRange"])

    def test_branch_merged_clean_worktree_reports_fully_integrated(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            branch = "delegate/cursor-merged-clean"
            wt_path = str(Path(fake_home) / "wt" / "cursor-merged-clean")
            self._seed_persistent_run(
                path,
                alias="cursor-merged-clean",
                branch=branch,
                execution_cwd=wt_path,
                creation_oid=base_oid,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            git("merge", "--no-ff", branch, cwd=path, check=False)

            payload = self.delegate.worktree_mgmt.show_worktree(
                self._registry_root(path),
                handle="cursor-merged-clean",
            )

            self.assertTrue(payload["branchMergedIntoSource"])
            self.assertTrue(payload["mergedIntoSource"])
            self.assertTrue(payload["fullyIntegrated"])
            self.assertFalse(payload["hasUncommittedChanges"])
            self.assertTrue(payload["uncommittedChangesIntegrated"])
            self.assertEqual(payload["integrationStatus"], "fully-integrated")
            commands = payload["suggestedCommands"]
            self.assertIsNone(commands["mergeIntoSource"])
            self.assertIsNone(commands["cherryPickRange"])

    def test_list_entry_exposes_branch_and_full_integration_fields(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-list-integration"
            wt_path = str(Path(fake_home) / "wt" / "cursor-list-integration")
            self._seed_persistent_run(
                path,
                alias="cursor-list-integration",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            (Path(wt_path) / "local.txt").write_text("dirty\n", encoding="utf-8")

            result = self.delegate.worktree_mgmt.list_worktrees(self._registry_root(path))
            entry = result["entries"][0]

            self.assertFalse(entry["branchMergedIntoSource"])
            self.assertFalse(entry["mergedIntoSource"])
            self.assertFalse(entry["fullyIntegrated"])
            self.assertTrue(entry["hasUncommittedChanges"])
            self.assertEqual(entry["integrationStatus"], "branch-unmerged-worktree-dirty")

    def test_branch_unmerged_clean_worktree_reports_review_ready_state(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            branch = "delegate/cursor-unmerged-clean"
            wt_path = str(Path(fake_home) / "wt" / "cursor-unmerged-clean")
            self._seed_persistent_run(
                path,
                alias="cursor-unmerged-clean",
                branch=branch,
                execution_cwd=wt_path,
                creation_oid=base_oid,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)

            payload = self.delegate.worktree_mgmt.show_worktree(
                self._registry_root(path),
                handle="cursor-unmerged-clean",
            )

            self.assertFalse(payload["branchMergedIntoSource"])
            self.assertFalse(payload["mergedIntoSource"])
            self.assertFalse(payload["fullyIntegrated"])
            self.assertFalse(payload["hasUncommittedChanges"])
            self.assertTrue(payload["uncommittedChangesIntegrated"])
            self.assertEqual(payload["integrationStatus"], "branch-unmerged")
            self.assertIsNotNone(payload["suggestedCommands"]["mergeIntoSource"])
            self.assertIsNotNone(payload["suggestedCommands"]["cherryPickRange"])

    def test_worktree_list_does_not_build_deep_work_summaries(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-list-light"
            wt_path = str(Path(fake_home) / "wt" / "cursor-list-light")
            self._seed_persistent_run(
                path,
                alias="cursor-list-light",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)

            with mock.patch.object(
                self.delegate.worktree_mgmt.worktree_summary,
                "build_work_summary",
            ) as summary_mock:
                result = self.delegate.worktree_mgmt.list_worktrees(self._registry_root(path))

            self.assertEqual(len(result["entries"]), 1)
            self.assertNotIn("workSummary", result["entries"][0])
            summary_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
