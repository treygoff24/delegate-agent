import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git


class WorktreePruneGcTests(WorktreeMgmtTestBase):
    def test_worktree_prune_requires_filter(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "prune"],
                home=fake_home,
            )
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(json.loads(out)["code"], "prune_filter_required")

    def test_worktree_prune_dry_run_mutates_nothing(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-prune"
            wt_path = str(Path(fake_home) / "wt" / "cursor-prune")
            self._seed_persistent_run(path, alias="cursor-4", branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "prune", "--merged", "--dry-run"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["dryRun"])
            self.assertEqual(len(payload["planned"]), 1)
            self.assertTrue(Path(wt_path).exists())

    def test_worktree_prune_group_filters_candidates(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            matching_branch = "delegate/cursor-prune-group"
            matching_wt = str(Path(fake_home) / "wt" / "cursor-prune-group")
            matching_run_id, _matching_alias = self._seed_persistent_run(
                path,
                alias="cursor-prune-group",
                branch=matching_branch,
                execution_cwd=matching_wt,
            )
            other_branch = "delegate/cursor-prune-other"
            other_wt = str(Path(fake_home) / "wt" / "cursor-prune-other")
            other_run_id, _other_alias = self._seed_persistent_run(
                path,
                alias="cursor-prune-other",
                branch=other_branch,
                execution_cwd=other_wt,
            )
            self._tag_run_group(path, matching_run_id, "wave4")
            self._tag_run_group(path, other_run_id, "other")
            self._create_worktree_at(path, matching_branch, matching_wt)
            self._create_worktree_at(path, other_branch, other_wt)

            result = self.delegate.worktree_mgmt.prune_worktrees(
                self._registry_root(path),
                merged=True,
                group="wave4",
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                [entry["alias"] for entry in result["removed"]], ["cursor-prune-group"]
            )
            self.assertFalse(Path(matching_wt).exists())
            self.assertTrue(Path(other_wt).exists())

    def test_worktree_prune_merged_removes_only_safe_mixed_set(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            cases = {
                "clean-merged": (
                    "delegate/cursor-clean-merged",
                    str(Path(fake_home) / "wt" / "clean-merged"),
                ),
                "dirty-merged": (
                    "delegate/cursor-dirty-merged",
                    str(Path(fake_home) / "wt" / "dirty-merged"),
                ),
                "unmerged": (
                    "delegate/cursor-unmerged",
                    str(Path(fake_home) / "wt" / "unmerged"),
                ),
                "detached": (
                    "delegate/cursor-detached",
                    str(Path(fake_home) / "wt" / "detached"),
                ),
            }
            for alias, (branch, wt_path) in cases.items():
                source_ref = None if alias == "detached" else object()
                self._seed_persistent_run(
                    path,
                    alias=alias,
                    branch=branch,
                    execution_cwd=wt_path,
                    source_head_ref=source_ref,
                )
                self._create_worktree_at(
                    path,
                    branch,
                    wt_path,
                    dirty_file="scratch.txt" if alias == "dirty-merged" else None,
                )
            unmerged_branch, unmerged_wt = cases["unmerged"]
            (Path(unmerged_wt) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=unmerged_wt)
            git("commit", "-m", "feature", cwd=unmerged_wt)

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "prune", "--merged"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            removed_aliases = {entry["alias"] for entry in payload["removed"]}
            removed_with_branch_kept = {
                entry["alias"]: entry.get("branchKept")
                for entry in payload["removed"]
                if "branchKept" in entry
            }
            skipped = {entry["alias"]: entry["reason"] for entry in payload["skipped"]}
            self.assertIn("clean-merged", removed_aliases)
            # Per spec L673: clean unmerged worktrees are removed with path gone
            # but branch kept (branchKept: "unmerged"), not skipped.
            self.assertEqual(removed_with_branch_kept.get("unmerged"), "unmerged")
            self.assertEqual(skipped["dirty-merged"], "dirty")
            self.assertNotIn("unmerged", skipped)
            self.assertEqual(skipped["detached"], "detached_source")

            _clean_branch, clean_wt = cases["clean-merged"]
            dirty_branch, dirty_wt = cases["dirty-merged"]
            detached_branch, _detached_wt = cases["detached"]
            self.assertFalse(Path(clean_wt).exists())
            self.assertTrue(Path(dirty_wt).exists())
            self.assertTrue((Path(dirty_wt) / "scratch.txt").exists())
            # Per spec L673: path is removed but branch is kept.
            self.assertFalse(Path(unmerged_wt).exists())
            self.assertEqual(git("rev-parse", "--verify", unmerged_branch, cwd=path).returncode, 0)
            self.assertEqual(git("rev-parse", "--verify", dirty_branch, cwd=path).returncode, 0)
            self.assertEqual(git("rev-parse", "--verify", detached_branch, cwd=path).returncode, 0)

    def test_worktree_gc_missing_path_reconciles(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-gc"
            wt_path = str(Path(fake_home) / "wt" / "cursor-gc")
            run_id, _alias = self._seed_persistent_run(path, branch=branch, execution_cwd=wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "gc"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertGreaterEqual(payload["reconciled"], 1)
            self.assertEqual(payload["mode"], "reconcile-registry")
            self.assertFalse(payload["effects"]["deletesWorktreePaths"])
            self.assertEqual(payload["reconciledEntries"][0]["reason"], "path_missing")
            self.assertEqual(payload["reconciledEntries"][0]["action"], "marked_missing")
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "missing")
            self.assertNotIn("worktreeRemovedAt", state)

    def test_worktree_gc_dry_run_reports_would_prune_source_roots(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            wt_path = str(Path(fake_home) / "wt" / "missing")
            self._seed_persistent_run(path, execution_cwd=wt_path)

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "gc", "--dry-run"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["wouldPruneSourceRoots"], 1)
            self.assertEqual(payload["prunedSourceRoots"], 0)
            self.assertFalse(payload["effects"]["deletesWorktreePaths"])

    def test_worktree_gc_classifies_missing_source_root(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            execution = Path(fake_home) / "wt" / "orphan"
            run_id, _alias = self._seed_persistent_run(path, execution_cwd=str(execution))
            registry_root = self._registry_root(path)
            manifest = self.delegate.run_registry.load_run_manifest(registry_root, run_id)
            manifest["sourceGitRoot"] = str(Path(fake_home) / "missing-source")
            self.delegate.run_registry.write_json_atomic(
                self.delegate.run_registry.run_directory(registry_root, run_id) / "manifest.json",
                manifest,
            )

            result = self.delegate.worktree_mgmt.gc_worktrees(registry_root, dry_run=True)

            self.assertEqual(result["orphans"][0]["reason"], "source_root_missing")
            result = self.delegate.worktree_mgmt.gc_worktrees(registry_root)
            self.assertEqual(result["orphans"][0]["reason"], "source_root_missing")
            state = self.delegate.run_registry.load_run_state(registry_root, run_id)
            self.assertEqual(state["worktreeStatus"], "unknown")

    def test_worktree_gc_classifies_detached_backlink(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            execution = Path(fake_home) / "standalone"
            subprocess.run(
                ["git", "init", str(execution)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._seed_persistent_run(path, execution_cwd=str(execution))

            result = self.delegate.worktree_mgmt.gc_worktrees(
                self._registry_root(path), dry_run=True
            )

            self.assertEqual(result["orphans"][0]["reason"], "detached_backlink")

    def test_worktree_gc_classifies_missing_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            execution = Path(fake_home) / "wt" / "missing-branch"
            execution.mkdir(parents=True)
            self._seed_persistent_run(path, execution_cwd=str(execution))
            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "_worktree_list_paths_with_warning",
                    return_value=({str(execution)}, None),
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "_branch_exists",
                    return_value=False,
                ),
            ):
                result = self.delegate.worktree_mgmt.gc_worktrees(
                    self._registry_root(path), dry_run=True
                )

            self.assertEqual(result["orphans"][0]["reason"], "branch_missing")

    def test_worktree_gc_existing_orphan_path_is_not_deleted_and_marked_unknown(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-orphan"
            orphan_path = Path(fake_home) / "wt" / "orphan"
            orphan_path.mkdir(parents=True)
            sentinel = orphan_path / "DO_NOT_DELETE.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-4",
                branch=branch,
                execution_cwd=str(orphan_path),
            )
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "gc"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(orphan_path.exists())
            self.assertTrue(sentinel.exists())
            self.assertEqual(payload["orphans"][0]["reason"], "worktree_metadata_missing")
            self.assertEqual(
                payload["orphans"][0]["safeAction"], "inspect_path_before_manual_cleanup"
            )
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "unknown")
            listed = self.delegate.worktree_mgmt.list_worktrees(self._registry_root(path))
            entry = listed["entries"][0]
            self.assertEqual(entry["worktreeStatus"], "unknown")
            self.assertIn("worktree path is not registered with git", entry["warnings"])

    def test_worktree_prune_skips_when_dirty_check_fails(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-prune-dirty-check-failed"
            wt_path = str(Path(fake_home) / "wt" / "cursor-prune-dirty-check-failed")
            self._seed_persistent_run(
                path,
                alias="cursor-prune-dirty-check-failed",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)

            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "porcelain_status",
                return_value=(None, None, ["git status failed: boom"]),
            ):
                result = self.delegate.worktree_mgmt.prune_worktrees(
                    self._registry_root(path),
                    merged=True,
                    dry_run=True,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["planned"], [])
            skipped = {entry["alias"]: entry["reason"] for entry in result["skipped"]}
            self.assertEqual(skipped["cursor-prune-dirty-check-failed"], "dirty_check_failed")

    def test_worktree_prune_reports_nested_remove_payload_failure(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-prune-branch-error"
            wt_path = str(Path(fake_home) / "wt" / "cursor-prune-branch-error")
            self._seed_persistent_run(
                path,
                alias="cursor-prune-branch-error",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            branch_failure = self.delegate.worktree_mgmt.BranchRemovalResult(
                removed=False,
                error="fatal: cannot delete branch",
            )
            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "_remove_branch",
                return_value=branch_failure,
            ):
                result = self.delegate.worktree_mgmt.prune_worktrees(
                    self._registry_root(path),
                    merged=True,
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["exitCode"], self.delegate.EXIT_USAGE)
            self.assertEqual(len(result["errors"]), 1)
            self.assertEqual(result["errors"][0]["code"], "branch_remove_failed")
            self.assertEqual(result["removed"][0]["code"], "branch_remove_failed")

    def test_prune_includes_detached_with_flag(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-detached"
            wt_path = str(Path(fake_home) / "wt" / "cursor-detached")
            self._seed_persistent_run(
                path,
                alias="cursor-detached",
                branch=branch,
                execution_cwd=wt_path,
                source_head_ref=None,
            )
            self._create_worktree_at(path, branch, wt_path)
            # sourceHeadRef=None makes creationContext.sourceHeadRef null
            # include_detached=True should override the detached skip
            result = self.delegate.worktree_mgmt.prune_worktrees(
                self._registry_root(path),
                merged=True,
                include_detached=True,
                dry_run=True,
            )
            planned_aliases = {e["alias"] for e in result.get("planned", [])}
            self.assertIn("cursor-detached", planned_aliases)
            skipped_reasons = {e["alias"]: e["reason"] for e in result.get("skipped", [])}
            self.assertNotIn("cursor-detached", skipped_reasons)

    def test_prune_skips_when_merge_status_unknown(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-merge-unknown"
            wt_path = str(Path(fake_home) / "wt" / "cursor-merge-unknown")
            self._seed_persistent_run(
                path,
                alias="cursor-merge-unknown",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "merged_into_source",
                return_value=(None, ["merge state unavailable"]),
            ):
                result = self.delegate.worktree_mgmt.prune_worktrees(
                    self._registry_root(path),
                    merged=True,
                    dry_run=True,
                )
            self.assertEqual(result["planned"], [])
            skipped_reasons = {entry["alias"]: entry["reason"] for entry in result["skipped"]}
            self.assertEqual(skipped_reasons["cursor-merge-unknown"], "merge_check_failed")

    def test_prune_older_than_filters(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            from datetime import UTC, datetime, timedelta

            old_ts = (datetime.now(UTC) - timedelta(days=10)).strftime(
                self.delegate.run_registry.UTC_TIMESTAMP_FORMAT
            )
            recent_ts = (datetime.now(UTC) - timedelta(hours=1)).strftime(
                self.delegate.run_registry.UTC_TIMESTAMP_FORMAT
            )
            branch_old = "delegate/cursor-old"
            wt_path_old = str(Path(fake_home) / "wt" / "cursor-old")
            branch_recent = "delegate/cursor-recent"
            wt_path_recent = str(Path(fake_home) / "wt" / "cursor-recent")
            self._seed_persistent_run(
                path,
                alias="cursor-old",
                branch=branch_old,
                execution_cwd=wt_path_old,
                last_activity_at=old_ts,
            )
            self._seed_persistent_run(
                path,
                alias="cursor-recent",
                branch=branch_recent,
                execution_cwd=wt_path_recent,
                last_activity_at=recent_ts,
            )
            self._create_worktree_at(path, branch_old, wt_path_old)
            self._create_worktree_at(path, branch_recent, wt_path_recent)
            result = self.delegate.worktree_mgmt.prune_worktrees(
                self._registry_root(path),
                merged=True,
                older_than_days=7,
                dry_run=True,
            )
            planned_aliases = {e["alias"] for e in result.get("planned", [])}
            skipped_aliases = {e["alias"]: e["reason"] for e in result.get("skipped", [])}
            self.assertIn("cursor-old", planned_aliases)
            self.assertEqual(skipped_aliases.get("cursor-recent"), "not_yet_old_enough")

    def test_prune_older_than_reports_invalid_last_activity(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-invalid-activity"
            wt_path = str(Path(fake_home) / "wt" / "cursor-invalid-activity")
            self._seed_persistent_run(
                path,
                alias="cursor-invalid-activity",
                branch=branch,
                execution_cwd=wt_path,
                last_activity_at="not-a-timestamp",
            )
            self._create_worktree_at(path, branch, wt_path)

            result = self.delegate.worktree_mgmt.prune_worktrees(
                self._registry_root(path),
                merged=True,
                older_than_days=7,
                dry_run=True,
            )

            self.assertEqual(result["planned"], [])
            skipped_aliases = {entry["alias"]: entry["reason"] for entry in result["skipped"]}
            self.assertEqual(skipped_aliases["cursor-invalid-activity"], "invalid_last_activity")

    def test_maybe_auto_prune_runs_when_enabled(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-ap"
            wt_path = str(Path(fake_home) / "wt" / "cursor-ap")
            from datetime import UTC, datetime, timedelta

            old_ts = (datetime.now(UTC) - timedelta(days=2)).strftime(
                self.delegate.run_registry.UTC_TIMESTAMP_FORMAT
            )
            self._seed_persistent_run(
                path,
                alias="cursor-ap",
                branch=branch,
                execution_cwd=wt_path,
                last_activity_at=old_ts,
            )
            self._create_worktree_at(path, branch, wt_path)
            config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
            result = self.delegate.worktree_mgmt.maybe_auto_prune(self._registry_root(path), config)
            self.assertIsNotNone(result)
            index = self.delegate.run_registry.load_index(self._registry_root(path))
            run_id = index["aliases"].get("cursor-ap")
            self.assertIsNotNone(run_id)
            st = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(st.get("worktreeStatus"), "removed")

    def test_maybe_auto_prune_skipped_when_disabled(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            self._seed_persistent_run(path, alias="cursor-ap-off")
            config = {"worktrees": {"autoPrune": {"enabled": False}}}
            result = self.delegate.worktree_mgmt.maybe_auto_prune(self._registry_root(path), config)
            self.assertIsNone(result)

    def test_maybe_auto_prune_bool_days_falls_back_to_default(self):
        _repo, path = self._make_repo()
        config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": True}}}
        with mock.patch.object(
            self.delegate.worktree_mgmt,
            "prune_worktrees",
            return_value={"ok": True},
        ) as prune:
            result = self.delegate.worktree_mgmt.maybe_auto_prune(self._registry_root(path), config)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(prune.call_args.kwargs["older_than_days"], 7)

    def test_worktree_list_no_auto_prune_skips_opportunistic_pass(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-noap"
            wt_path = str(Path(fake_home) / "wt" / "cursor-noap")
            self._seed_persistent_run(
                path, alias="cursor-noap", branch=branch, execution_cwd=wt_path
            )
            self._create_worktree_at(path, branch, wt_path)
            config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
            index = self.delegate.run_registry.load_index(self._registry_root(path))
            run_id = index["aliases"].get("cursor-noap")
            self.assertIsNotNone(run_id)
            self.delegate.worktree_mgmt.maybe_auto_prune(
                self._registry_root(path), config, no_auto_prune=True
            )
            st = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(st.get("worktreeStatus"), "present")

    def test_worktree_list_no_auto_prune_cli_skips_opportunistic_pass(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-noap-cli"
            wt_path = str(Path(fake_home) / "wt" / "cursor-noap-cli")
            self._seed_persistent_run(
                path, alias="cursor-noap-cli", branch=branch, execution_cwd=wt_path
            )
            self._create_worktree_at(path, branch, wt_path)
            config_path = self._registry_root(path) / "config.json"
            config_path.write_text(
                json.dumps(
                    {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
                ),
                encoding="utf-8",
            )
            run_id = self.delegate.run_registry.load_index(self._registry_root(path))["aliases"][
                "cursor-noap-cli"
            ]

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "list", "--no-auto-prune"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertNotIn("autoPrune", payload)
            self.assertEqual(payload["summary"]["autoPruneMode"], "suppressed")
            self.assertTrue(payload["summary"]["readOnly"])
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state.get("worktreeStatus"), "present")
            self.assertTrue(Path(wt_path).exists())

    def test_worktree_list_json_exits_nonzero_when_auto_prune_fails(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(path, alias="cursor-auto-prune-fails")
            config_path = self._registry_root(path) / "config.json"
            config_path.write_text(
                json.dumps(
                    {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
                ),
                encoding="utf-8",
            )
            failed_auto_prune = {
                "ok": False,
                "code": "branch_remove_failed",
                "error": "branch_remove_failed",
                "message": "branch cleanup failed",
            }
            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "maybe_auto_prune",
                return_value=failed_auto_prune,
            ):
                code, out, _err = self._run_cli(
                    ["--cwd", path, "--json", "worktree", "list"],
                    home=fake_home,
                )

            payload = json.loads(out)
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["exitCode"], self.delegate.worktree_mgmt.WORKTREE_ERROR_EXIT_CODE
            )
            self.assertEqual(payload["autoPrune"]["code"], "branch_remove_failed")
            self.assertNotIn("exitCode", payload["autoPrune"])

    def test_worktree_prune_include_detached_cli_plans_detached_worktree(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-detached-cli"
            wt_path = str(Path(fake_home) / "wt" / "cursor-detached-cli")
            self._seed_persistent_run(
                path,
                alias="cursor-detached-cli",
                branch=branch,
                execution_cwd=wt_path,
                source_head_ref=None,
            )
            self._create_worktree_at(path, branch, wt_path)

            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    path,
                    "--json",
                    "worktree",
                    "prune",
                    "--merged",
                    "--include-detached",
                    "--dry-run",
                ],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            planned_aliases = {entry["alias"] for entry in payload["planned"]}
            self.assertIn("cursor-detached-cli", planned_aliases)

    def test_worktree_prune_include_detached_cli_removes_detached_worktree(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-detached-cli-remove"
            wt_path = str(Path(fake_home) / "wt" / "cursor-detached-cli-remove")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-detached-cli-remove",
                branch=branch,
                execution_cwd=wt_path,
                source_head_ref=None,
            )
            self._create_worktree_at(path, branch, wt_path)

            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    path,
                    "--json",
                    "worktree",
                    "prune",
                    "--merged",
                    "--include-detached",
                ],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual(
                [entry["alias"] for entry in payload["removed"]], ["cursor-detached-cli-remove"]
            )
            self.assertFalse(Path(wt_path).exists())
            self.assertNotEqual(
                git("rev-parse", "--verify", branch, cwd=path, check=False).returncode,
                0,
            )
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "removed")

    def test_worktree_prune_harness_filter_limits_candidates(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            cursor_branch = "delegate/cursor-prune-filter"
            droid_branch = "delegate/droid-prune-filter"
            cursor_wt = str(Path(fake_home) / "wt" / "cursor-prune-filter")
            droid_wt = str(Path(fake_home) / "wt" / "droid-prune-filter")
            self._seed_persistent_run(
                path,
                alias="cursor-prune-filter",
                harness="cursor",
                branch=cursor_branch,
                execution_cwd=cursor_wt,
            )
            self._seed_persistent_run(
                path,
                alias="droid-prune-filter",
                harness="droid",
                branch=droid_branch,
                execution_cwd=droid_wt,
            )
            self._create_worktree_at(path, cursor_branch, cursor_wt)
            self._create_worktree_at(path, droid_branch, droid_wt)

            result = self.delegate.worktree_mgmt.prune_worktrees(
                self._registry_root(path),
                merged=True,
                harness="droid",
                dry_run=True,
            )

            self.assertEqual(
                {entry["alias"] for entry in result["planned"]}, {"droid-prune-filter"}
            )
            skipped = {entry["alias"]: entry["reason"] for entry in result["skipped"]}
            self.assertEqual(skipped["cursor-prune-filter"], "harness_filter")

    def test_worktree_prune_force_discards_dirty_and_removes_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-prune-force"
            wt_path = str(Path(fake_home) / "wt" / "cursor-prune-force")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-prune-force",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path, dirty_file="scratch.txt")

            result = self.delegate.worktree_mgmt.prune_worktrees(
                self._registry_root(path),
                merged=True,
                force=True,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                [entry["alias"] for entry in result["removed"]], ["cursor-prune-force"]
            )
            removed = result["removed"][0]
            self.assertTrue(removed["pathRemoved"])
            self.assertTrue(removed["branchRemoved"])
            self.assertFalse(Path(wt_path).exists())
            self.assertNotEqual(
                git("rev-parse", "--verify", branch, cwd=path, check=False).returncode, 0
            )
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertIn("discardedDirtyPaths", state)

    def test_worktree_prune_force_branch_removes_clean_unmerged_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-prune-force-branch"
            wt_path = str(Path(fake_home) / "wt" / "cursor-prune-force-branch")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-prune-force-branch",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)

            result = self.delegate.worktree_mgmt.prune_worktrees(
                self._registry_root(path),
                merged=True,
                force_branch=True,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                [entry["alias"] for entry in result["removed"]],
                ["cursor-prune-force-branch"],
            )
            removed = result["removed"][0]
            self.assertTrue(removed["pathRemoved"])
            self.assertTrue(removed["branchRemoved"])
            self.assertFalse(Path(wt_path).exists())
            self.assertNotEqual(
                git("rev-parse", "--verify", branch, cwd=path, check=False).returncode,
                0,
            )
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "removed")

    def test_maybe_auto_prune_skips_when_lock_contended(self):
        """maybe_auto_prune returns within ~1s when registry lock is contended."""
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-locked"
            wt_path = str(Path(fake_home) / "wt" / "cursor-locked")
            self._seed_persistent_run(
                path,
                alias="cursor-locked",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            registry_root = self._registry_root(path)
            config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}

            import threading

            lock_held = threading.Event()
            release_lock = threading.Event()

            def hold_lock():
                with self.delegate.run_registry.registry_lock(registry_root, timeout_seconds=30):
                    lock_held.set()
                    release_lock.wait(timeout=10)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            self.assertTrue(lock_held.wait(timeout=5), "registry lock was not acquired")
            try:
                t0 = time.monotonic()
                result = self.delegate.worktree_mgmt.maybe_auto_prune(registry_root, config)
                elapsed = time.monotonic() - t0
            finally:
                release_lock.set()
                holder.join(timeout=10)

            # Result should indicate skip/unavailable (lock contention).
            self.assertIsNotNone(result)
            self.assertTrue(result.get("skipped") or not result.get("ok"))
            self.assertEqual(result.get("reason"), "lock_contended")
            # Should return within 1s (not block for the 30s default lock timeout).
            self.assertLess(elapsed, 3.0, msg="maybe_auto_prune blocked on lock contention")

    def test_maybe_auto_prune_preserves_management_error_payload(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            registry_root = self.delegate.run_registry.ensure_registry(
                Path(path), workspace_kind="git"
            )
            config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
            error = self.delegate.worktree_mgmt.WorktreeManagementError(
                {
                    "ok": False,
                    "code": "bad_auto_prune",
                    "message": "auto-prune failed",
                    "exitCode": self.delegate.EXIT_USAGE,
                }
            )
            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "prune_worktrees",
                side_effect=error,
            ):
                result = self.delegate.worktree_mgmt.maybe_auto_prune(registry_root, config)

            self.assertIsNotNone(result)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "bad_auto_prune")
            self.assertNotEqual(result.get("reason"), "auto_prune_unavailable")

    def test_maybe_auto_prune_uses_normal_per_entry_locks_after_probe(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            registry_root = self.delegate.run_registry.ensure_registry(
                Path(path), workspace_kind="git"
            )
            config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "prune_worktrees",
                return_value={"ok": True, "removed": [], "skipped": [], "errors": []},
            ) as prune:
                result = self.delegate.worktree_mgmt.maybe_auto_prune(registry_root, config)

            self.assertTrue(result["ok"])
            self.assertNotIn("_skip_lock", prune.call_args.kwargs)

    def test_worktree_gc_warns_when_git_worktree_list_fails(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-gc-warning"
            wt_path = str(Path(fake_home) / "wt" / "cursor-gc-warning")
            run_id, _alias = self._seed_persistent_run(path, branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)

            with mock.patch.object(
                self.delegate.worktree_mgmt,
                "_worktree_list_paths_with_warning",
                return_value=(None, "fatal: worktree list failed"),
            ):
                result = self.delegate.worktree_mgmt.gc_worktrees(self._registry_root(path))

            self.assertTrue(result["warnings"])
            self.assertEqual(result["warnings"][0]["sourceGitRoot"], path)
            self.assertIn("worktree list failed", result["warnings"][0]["message"])
            self.assertEqual(result["orphans"][0]["reason"], "worktree_list_failed")
            self.assertIn("worktree list failed", result["orphans"][0]["message"])
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "unknown")

    def test_merge_base_checks_qualified_branch_ref(self):
        completed = subprocess.CompletedProcess(["git"], 0, "", "")
        with mock.patch.object(
            self.delegate.worktree_mgmt,
            "_run_git",
            return_value=completed,
        ) as run_git:
            result = self.delegate.worktree_mgmt._merge_base_is_ancestor(
                "/repo",
                "delegate/cursor-demo",
            )

        self.assertTrue(result)
        run_git.assert_called_once_with(
            "/repo",
            ["merge-base", "--is-ancestor", "refs/heads/delegate/cursor-demo", "HEAD"],
            timeout_seconds=self.delegate.worktree_mgmt.GIT_QUICK_TIMEOUT_SECONDS,
        )

    def test_run_git_timeout_returns_structured_failure(self):
        _repo, path = self._make_repo()
        from delegate_agent import git_utils

        with mock.patch.object(
            git_utils.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git", "status"], 30),
        ):
            result = self.delegate.worktree_mgmt._run_git(path, ["status"])
        self.assertEqual(result.returncode, 124)
        self.assertIn("git command timed out", result.stderr)

    def test_porcelain_status_uses_quick_git_timeout(self):
        _repo, path = self._make_repo()
        completed = subprocess.CompletedProcess(["git"], 0, "", "")
        from delegate_agent import git_utils

        with mock.patch.object(
            git_utils.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.delegate.worktree_mgmt.porcelain_status(path)

        self.assertEqual(
            run.call_args.kwargs["timeout"],
            self.delegate.worktree_mgmt.GIT_QUICK_TIMEOUT_SECONDS,
        )

    def test_merged_into_source_returns_null_when_git_unavailable(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            plain_dir = Path(fake_home) / "plain-worktree2"
            plain_dir.mkdir()
            (plain_dir / "somefile.txt").write_text("content\n", encoding="utf-8")
            self._seed_persistent_run(path, alias="cursor-mgitfail", execution_cwd=str(plain_dir))
            index = self.delegate.run_registry.load_index(self._registry_root(path))
            run_id = index["aliases"].get("cursor-mgitfail")
            record = self.delegate.worktree_mgmt._record_for_run(
                self._registry_root(path), run_id, {}
            )
            result, warnings = self.delegate.worktree_mgmt.merged_into_source(record, "present")
            self.assertIsNone(result)
            self.assertTrue(len(warnings) > 0)
            self.assertIn("could not determine whether branch is merged", warnings[0])

    def test_snapshot_json_has_cleanup_hints(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-cleanup"
            wt_path = str(Path(fake_home) / "wt" / "cursor-cleanup")
            run_id, _alias = self._seed_persistent_run(
                path, alias="cursor-cleanup", branch=branch, execution_cwd=wt_path
            )
            self._create_worktree_at(path, branch, wt_path)
            registry_root = self._registry_root(path)
            run_path = self.delegate.run_registry.run_directory(registry_root, run_id)

            # Add worktreeCleanupCommands to the MANIFEST (not the snapshot).
            # This proves merge_snapshot_view lifts it from manifest → view.
            manifest = self.delegate.run_registry.load_run_manifest(registry_root, run_id)
            assert isinstance(manifest, dict)
            manifest["worktreeCleanupCommands"] = {
                "safe": "delegate worktree remove cursor-cleanup",
                "forceBranch": "delegate worktree remove cursor-cleanup --force-branch",
                "discardUncommitted": "delegate worktree remove cursor-cleanup --discard-uncommitted",
                "rawGit": "git -C /src worktree remove /wt/cursor-cleanup && git -C /src branch -d delegate/cursor-cleanup",
            }
            self.delegate.run_registry.write_json_atomic(run_path / "manifest.json", manifest)

            # Write snapshot WITHOUT worktreeCleanupCommands (proves lift from manifest).
            snapshot_data = {
                "runId": run_id,
                "alias": "cursor-cleanup",
            }
            self.delegate.run_registry.write_json_atomic(run_path / "snapshot.json", snapshot_data)

            # merge_snapshot_view should lift worktreeCleanupCommands from manifest.
            from delegate_agent import snapshot_view

            loaded_snapshot = self.delegate.run_registry.load_run_snapshot(registry_root, run_id)
            view = snapshot_view.merge_snapshot_view(
                registry_root,
                run_id,
                loaded_snapshot,
                redact=False,
            )
            cleanup = view.get("worktreeCleanupCommands", {})
            self.assertIn("safe", cleanup)
            self.assertIn("forceBranch", cleanup)
            self.assertIn("discardUncommitted", cleanup)
            self.assertIn("rawGit", cleanup)


class WorktreePoolGcTests(WorktreeMgmtTestBase):
    """`worktree gc --all`: the machine-wide pool walk for repo-less orphans."""

    def _pool_worktree(
        self,
        pool: Path,
        fingerprint: str,
        name: str,
        *,
        gitdir: str | None,
        contents: str | None = None,
    ) -> Path:
        worktree = pool / fingerprint / name
        worktree.mkdir(parents=True)
        if gitdir is not None:
            (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        if contents is not None:
            (worktree / contents).write_text("unique work\n", encoding="utf-8")
        return worktree

    def _live_gitdir(self, source: Path, name: str, worktree: Path) -> str:
        """Build the admin directory Git keeps for a live worktree, backfile included."""
        admin = source / ".git" / "worktrees" / name
        admin.mkdir(parents=True)
        (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
        return str(admin)

    def _scan(self, pool: Path, **kwargs) -> dict:
        return self.delegate.worktree_mgmt.scan_worktree_pool(pool, **kwargs)

    def test_parse_worktree_backlink_reads_git_file_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(pool, "abc123", "cursor-1", gitdir="/gone/.git/x")
            self.assertEqual(
                self.delegate.worktree_mgmt._parse_worktree_backlink(worktree),
                "/gone/.git/x",
            )

    def test_parse_worktree_backlink_returns_none_without_git_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(pool, "abc123", "cursor-1", gitdir=None)
            self.assertIsNone(self.delegate.worktree_mgmt._parse_worktree_backlink(worktree))

    def test_parse_worktree_backlink_returns_none_for_git_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(pool, "abc123", "cursor-1", gitdir=None)
            (worktree / ".git").mkdir()
            self.assertIsNone(self.delegate.worktree_mgmt._parse_worktree_backlink(worktree))

    def test_source_root_recovered_from_standard_backlink(self):
        self.assertEqual(
            self.delegate.worktree_mgmt._source_root_from_backlink(
                "/tmp/src/.git/worktrees/cursor-1"
            ),
            "/tmp/src",
        )

    def test_source_root_is_none_for_non_standard_backlink(self):
        self.assertIsNone(
            self.delegate.worktree_mgmt._source_root_from_backlink("/elsewhere/gitdir/worktrees/x")
        )

    def test_pool_scan_reports_orphan_whose_source_repo_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            gone = Path(tmp) / "deleted-repo"
            worktree = self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(gone / ".git" / "worktrees" / "cursor-1"),
            )

            result = self._scan(pool)

            self.assertEqual(result["scannedWorktrees"], 1)
            self.assertEqual(len(result["orphans"]), 1)
            orphan = result["orphans"][0]
            self.assertEqual(orphan["worktreePath"], str(worktree))
            self.assertEqual(orphan["fingerprint"], "abc123")
            self.assertEqual(orphan["sourceGitRoot"], str(gone))
            self.assertEqual(orphan["reason"], "source_root_missing")
            self.assertEqual(
                orphan["safeAction"],
                "restore_source_root_or_inspect_path_before_manual_cleanup",
            )

    def test_pool_scan_ignores_live_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            worktree = pool / "abc123" / "cursor-1"
            self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=self._live_gitdir(source, "cursor-1", worktree),
            )

            result = self._scan(pool)

            self.assertEqual(result["scannedWorktrees"], 1)
            self.assertEqual(result["orphans"], [])

    def test_pool_scan_flags_live_source_with_missing_admin_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            (source / ".git").mkdir(parents=True)
            self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(source / ".git" / "worktrees" / "cursor-1"),
            )

            orphan = self._scan(pool)["orphans"][0]

            self.assertEqual(orphan["reason"], "worktree_metadata_missing")
            self.assertEqual(orphan["sourceGitRoot"], str(source))
            self.assertEqual(orphan["safeAction"], "inspect_path_before_manual_cleanup")

    def test_pool_scan_flags_worktree_without_backlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            self._pool_worktree(pool, "abc123", "cursor-1", gitdir=None)

            orphan = self._scan(pool)["orphans"][0]

            self.assertEqual(orphan["reason"], "worktree_metadata_missing")
            self.assertIsNone(orphan["sourceGitRoot"])
            self.assertIn("gitdir", orphan)
            self.assertIsNone(orphan["gitdir"])

    def test_pool_scan_degrades_on_non_standard_backlink_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(Path(tmp) / "separate-gitdir" / "worktrees" / "cursor-1"),
            )

            orphan = self._scan(pool)["orphans"][0]

            self.assertEqual(orphan["reason"], "source_root_missing")
            self.assertIsNone(orphan["sourceGitRoot"])

    def test_pool_scan_never_deletes_orphan_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(Path(tmp) / "gone" / ".git" / "worktrees" / "cursor-1"),
                contents="dirty.txt",
            )

            self._scan(pool)

            self.assertTrue(worktree.is_dir())
            self.assertEqual((worktree / "dirty.txt").read_text(encoding="utf-8"), "unique work\n")

    def test_empty_fingerprint_dir_is_reported_and_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            empty = pool / "abc123"
            empty.mkdir(parents=True)

            result = self._scan(pool)

            self.assertEqual(result["scannedWorktrees"], 0)
            self.assertEqual(
                result["emptyFingerprintDirs"],
                [{"fingerprint": "abc123", "path": str(empty)}],
            )
            self.assertTrue(empty.is_dir())

    def test_fingerprint_dir_with_loose_files_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            fingerprint = pool / "abc123"
            fingerprint.mkdir(parents=True)
            (fingerprint / ".DS_Store").write_text("junk\n", encoding="utf-8")

            entry = self._scan(pool)["emptyFingerprintDirs"][0]

            self.assertEqual(entry, {"fingerprint": "abc123", "path": str(fingerprint)})
            self.assertTrue(fingerprint.is_dir())
            self.assertTrue((fingerprint / ".DS_Store").is_file())

    def test_pool_scan_never_removes_anything_under_the_pool(self):
        """The whole pool path is read-only — no rmdir, no unlink, at any depth."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            empty = pool / "empty-fp"
            empty.mkdir(parents=True)
            orphan = self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(Path(tmp) / "gone" / ".git" / "worktrees" / "cursor-1"),
                contents="work.txt",
            )

            with (
                mock.patch.object(Path, "rmdir", side_effect=AssertionError("gc removed a dir")),
                mock.patch.object(Path, "unlink", side_effect=AssertionError("gc removed a file")),
                mock.patch.object(os, "rmdir", side_effect=AssertionError("gc removed a dir")),
                mock.patch.object(os, "remove", side_effect=AssertionError("gc removed a file")),
            ):
                result = self._scan(pool)

            self.assertEqual(len(result["orphans"]), 1)
            self.assertTrue(empty.is_dir())
            self.assertTrue(orphan.is_dir())

    def test_gc_effects_no_longer_advertise_pool_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            (pool / "abc123").mkdir(parents=True)

            payload = self.delegate.worktree_mgmt.gc_worktrees(None, pool_data_home=pool)

            self.assertNotIn("removesEmptyPoolDirs", payload["effects"])
            self.assertFalse(payload["effects"]["deletesWorktreePaths"])

    def test_relative_backlink_resolves_against_the_worktree_not_the_cwd(self):
        """A relative backlink is valid Git; resolving it against the CWD invents an orphan."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = pool / "abc123" / "cursor-1"
            worktree.mkdir(parents=True)
            admin = worktree / "admin"
            admin.mkdir()
            (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
            (worktree / ".git").write_text("gitdir: ./admin\n", encoding="utf-8")

            result = self._scan(pool)

            self.assertEqual(result["scannedWorktrees"], 1)
            self.assertEqual(result["orphans"], [])

    def test_relative_backfile_in_admin_dir_resolves_against_the_admin_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            worktree = pool / "abc123" / "cursor-1"
            worktree.mkdir(parents=True)
            admin = source / ".git" / "worktrees" / "cursor-1"
            admin.mkdir(parents=True)
            relative_back = Path("..", "..", "..", "..") / worktree.relative_to(tmp) / ".git"
            (admin / "gitdir").write_text(f"{relative_back}\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")

            self.assertEqual(self._scan(pool)["orphans"], [])

    def test_real_git_relative_worktree_is_not_an_orphan(self):
        """End-to-end guard: Git writes both sides of a relative backlink relatively.

        Nothing here is reachable from the process working directory, so a
        CWD-relative resolution reports this live worktree as an orphan.
        """
        _repo, source = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "pool" / "abc123" / "cursor-1"
            worktree.parent.mkdir(parents=True)
            added = git(
                "worktree",
                "add",
                "--relative-paths",
                "-b",
                "delegate/cursor-1",
                str(worktree),
                cwd=source,
                check=False,
            )
            if added.returncode != 0:
                self.skipTest("git is too old for worktree add --relative-paths")
            self.assertTrue(
                (worktree / ".git").read_text(encoding="utf-8").startswith("gitdir: ..")
            )

            result = self._scan(Path(tmp) / "pool")

            self.assertEqual(result["scannedWorktrees"], 1)
            self.assertEqual(result["orphans"], [])

    def test_symlinked_git_entry_is_refused(self):
        """A `.git` symlink could point the walk anywhere; it is not followed."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            worktree = pool / "abc123" / "cursor-1"
            worktree.mkdir(parents=True)
            elsewhere = Path(tmp) / "elsewhere-git"
            elsewhere.write_text(
                f"gitdir: {self._live_gitdir(source, 'cursor-1', worktree)}\n",
                encoding="utf-8",
            )
            (worktree / ".git").symlink_to(elsewhere)

            self.assertIsNone(self.delegate.worktree_mgmt._parse_worktree_backlink(worktree))
            orphan = self._scan(pool)["orphans"][0]
            self.assertEqual(orphan["reason"], "worktree_metadata_missing")
            self.assertIsNone(orphan["gitdir"])

    def test_oversized_git_entry_is_refused_rather_than_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(pool, "abc123", "cursor-1", gitdir=None)
            limit = self.delegate.worktree_mgmt.BACKLINK_MAX_BYTES
            (worktree / ".git").write_bytes(b"gitdir: /somewhere\n" + b"x" * limit)

            self.assertIsNone(self.delegate.worktree_mgmt._parse_worktree_backlink(worktree))
            self.assertIsNone(self._scan(pool)["orphans"][0]["gitdir"])

    def test_git_entry_at_the_size_limit_is_still_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(pool, "abc123", "cursor-1", gitdir=None)
            limit = self.delegate.worktree_mgmt.BACKLINK_MAX_BYTES
            line = b"gitdir: /gone/.git/worktrees/cursor-1\n"
            (worktree / ".git").write_bytes(line + b"#" * (limit - len(line)))

            self.assertEqual(
                self.delegate.worktree_mgmt._parse_worktree_backlink(worktree),
                "/gone/.git/worktrees/cursor-1",
            )

    def test_fifo_git_entry_does_not_block_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(pool, "abc123", "cursor-1", gitdir=None)
            os.mkfifo(worktree / ".git")

            self.assertIsNone(self.delegate.worktree_mgmt._parse_worktree_backlink(worktree))
            self.assertEqual(self._scan(pool)["orphans"][0]["reason"], "worktree_metadata_missing")

    def test_non_utf8_backlink_bytes_survive_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_worktree(pool, "abc123", "cursor-1", gitdir=None)
            (worktree / ".git").write_bytes(b"gitdir: /gone/\xff/.git/worktrees/cursor-1\n")

            parsed = self.delegate.worktree_mgmt._parse_worktree_backlink(worktree)

            self.assertEqual(parsed, "/gone/\udcff/.git/worktrees/cursor-1")
            self.assertEqual(
                parsed.encode("utf-8", "surrogateescape"), b"/gone/\xff/.git/worktrees/cursor-1"
            )
            self.assertEqual(self._scan(pool)["orphans"][0]["gitdir"], parsed)

    def test_admin_dir_without_backfile_is_still_an_orphan(self):
        """A stale, half-built admin directory must not pass as a healthy backlink."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            admin = source / ".git" / "worktrees" / "cursor-1"
            admin.mkdir(parents=True)
            self._pool_worktree(pool, "abc123", "cursor-1", gitdir=str(admin))

            orphan = self._scan(pool)["orphans"][0]

            self.assertEqual(orphan["reason"], "worktree_metadata_missing")
            self.assertEqual(orphan["sourceGitRoot"], str(source))

    def test_admin_dir_serving_another_worktree_is_an_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            admin = source / ".git" / "worktrees" / "cursor-1"
            admin.mkdir(parents=True)
            (admin / "gitdir").write_text(f"{Path(tmp) / 'other' / '.git'}\n", encoding="utf-8")
            self._pool_worktree(pool, "abc123", "cursor-1", gitdir=str(admin))

            self.assertEqual(self._scan(pool)["orphans"][0]["reason"], "worktree_metadata_missing")

    def test_backlink_to_an_existing_non_admin_path_is_an_orphan(self):
        """`gitdir: /` points at something that exists but serves no worktree."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            self._pool_worktree(pool, "abc123", "cursor-1", gitdir="/")

            orphan = self._scan(pool)["orphans"][0]

            self.assertEqual(orphan["reason"], "source_root_missing")
            self.assertEqual(orphan["gitdir"], "/")

    def test_backlink_to_a_file_is_an_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            not_a_dir = Path(tmp) / "not-a-dir"
            not_a_dir.write_text("\n", encoding="utf-8")
            self._pool_worktree(pool, "abc123", "cursor-1", gitdir=str(not_a_dir))

            self.assertEqual(len(self._scan(pool)["orphans"]), 1)

    def test_unverifiable_admin_metadata_is_reported_as_live(self):
        """Ambiguity resolves to live: a false orphan invites deleting real work."""
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            worktree = pool / "abc123" / "cursor-1"
            worktree.mkdir(parents=True)
            admin = source / ".git" / "worktrees" / "cursor-1"
            admin.mkdir(parents=True)
            backfile = admin / "gitdir"
            backfile.write_text(f"{worktree / '.git'}\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
            backfile.chmod(0o000)
            try:
                self.assertEqual(self._scan(pool)["orphans"], [])
            finally:
                backfile.chmod(0o644)

    def test_inaccessible_admin_parent_is_reported_as_live(self):
        """An unsearchable parent hides the admin dir; that is unknown, not gone."""
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            source = Path(tmp) / "live-repo"
            worktree = pool / "abc123" / "cursor-1"
            worktree.mkdir(parents=True)
            admin = source / ".git" / "worktrees" / "cursor-1"
            admin.mkdir(parents=True)
            (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
            (source / ".git").chmod(0o000)
            try:
                result = self._scan(pool)
            finally:
                (source / ".git").chmod(0o755)

            self.assertEqual(result["orphans"], [])

    def test_unreadable_pool_root_is_not_reported_as_missing(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            pool.mkdir()
            pool.chmod(0o000)
            try:
                result = self._scan(pool)
                with self.assertRaises(
                    self.delegate.worktree_mgmt.WorktreeManagementError
                ) as caught:
                    self._scan(pool, required=True)
            finally:
                pool.chmod(0o755)

            self.assertEqual(result["warnings"][0]["reason"], "pool_root_unreadable")
            self.assertEqual(caught.exception.payload["reason"], "pool_root_unreadable")

    def test_unreadable_fingerprint_dir_warns_instead_of_looking_empty(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            fingerprint = pool / "abc123"
            fingerprint.mkdir(parents=True)
            fingerprint.chmod(0o000)
            try:
                result = self._scan(pool)
            finally:
                fingerprint.chmod(0o755)

            self.assertEqual(result["emptyFingerprintDirs"], [])
            self.assertEqual(len(result["warnings"]), 1)
            self.assertEqual(result["warnings"][0]["reason"], "fingerprint_dir_unreadable")
            self.assertEqual(result["warnings"][0]["path"], str(fingerprint))

    def test_missing_configured_pool_root_warns_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "never-created"

            result = self._scan(missing)

            self.assertEqual(result["scannedWorktrees"], 0)
            self.assertEqual(result["warnings"][0]["reason"], "pool_root_missing")

    def test_required_pool_root_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.delegate.worktree_mgmt.WorktreeManagementError) as caught:
                self._scan(Path(tmp) / "never-created", required=True)

            self.assertEqual(caught.exception.payload["code"], "invalid_pool_root")

    def test_required_pool_root_must_be_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_a_dir = Path(tmp) / "file-pool"
            not_a_dir.write_text("\n", encoding="utf-8")

            with self.assertRaises(self.delegate.worktree_mgmt.WorktreeManagementError) as caught:
                self._scan(not_a_dir, required=True)

            self.assertEqual(caught.exception.payload["reason"], "pool_root_not_a_directory")

    def test_gc_all_scans_configured_data_home(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            pool = Path(fake_home) / ".delegate" / "worktrees"
            self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(Path(fake_home) / "gone" / ".git" / "worktrees" / "cursor-1"),
            )
            self._seed_persistent_run(path)

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "gc", "--all", "--dry-run"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["pool"]["dataHome"], str(pool))
            self.assertEqual(payload["pool"]["orphans"][0]["reason"], "source_root_missing")
            self.assertFalse(payload["effects"]["deletesWorktreePaths"])
            self.assertNotIn("removesEmptyPoolDirs", payload["effects"])

    def test_gc_without_all_omits_pool_section(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(path)

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "gc", "--dry-run"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            self.assertNotIn("pool", json.loads(out))

    def test_gc_all_works_without_a_registry(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()
            pool = Path(fake_home) / ".delegate" / "worktrees"
            self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(Path(fake_home) / "gone" / ".git" / "worktrees" / "cursor-1"),
            )

            code, out, _err = self._run_cli(
                ["--cwd", str(workspace), "--json", "worktree", "gc", "--all"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["mode"], "report-pool")
            self.assertEqual(payload["reconciled"], 0)
            self.assertFalse(payload["effects"]["registryWrites"])
            self.assertEqual(len(payload["pool"]["orphans"]), 1)

    def test_gc_without_all_still_requires_a_registry(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()

            code, out, _err = self._run_cli(
                ["--cwd", str(workspace), "--json", "worktree", "gc"],
                home=fake_home,
            )

            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out)["code"], "no_registry")

    def test_gc_pool_flag_overrides_configured_data_home(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()
            legacy_pool = Path(fake_home) / "legacy-worktrees"
            self._pool_worktree(
                legacy_pool,
                "abc123",
                "cursor-1",
                gitdir=str(Path(fake_home) / "gone" / ".git" / "worktrees" / "cursor-1"),
            )

            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    str(workspace),
                    "--json",
                    "worktree",
                    "gc",
                    "--dry-run",
                    "--pool",
                    str(legacy_pool),
                ],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["pool"]["dataHome"], str(legacy_pool))
            self.assertEqual(len(payload["pool"]["orphans"]), 1)

    def test_gc_pool_rejects_empty_path(self):
        with tempfile.TemporaryDirectory() as fake_home:
            code, out, _err = self._run_cli(
                ["--json", "worktree", "gc", "--pool", ""],
                home=fake_home,
            )

            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(json.loads(out)["error"], "invalid_option_value")

    def test_gc_pool_rejects_a_root_that_does_not_exist(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()

            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    str(workspace),
                    "--json",
                    "worktree",
                    "gc",
                    "--pool",
                    str(Path(fake_home) / "never-created"),
                ],
                home=fake_home,
            )

            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out)["code"], "invalid_pool_root")

    def test_gc_pool_rejects_a_root_that_is_not_a_directory(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()
            not_a_dir = Path(fake_home) / "file-pool"
            not_a_dir.write_text("\n", encoding="utf-8")

            code, out, _err = self._run_cli(
                ["--cwd", str(workspace), "--json", "worktree", "gc", "--pool", str(not_a_dir)],
                home=fake_home,
            )

            self.assertNotEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "invalid_pool_root")
            self.assertEqual(payload["reason"], "pool_root_not_a_directory")

    def test_gc_all_warns_when_the_configured_pool_has_never_been_created(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()

            code, out, _err = self._run_cli(
                ["--cwd", str(workspace), "--json", "worktree", "gc", "--all"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["pool"]["warnings"][0]["reason"], "pool_root_missing")

    def test_gc_all_leaves_an_empty_pool_dir_in_place(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()
            empty = Path(fake_home) / ".delegate" / "worktrees" / "abc123"
            empty.mkdir(parents=True)

            code, out, _err = self._run_cli(
                ["--cwd", str(workspace), "--json", "worktree", "gc", "--all"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(
                payload["pool"]["emptyFingerprintDirs"],
                [{"fingerprint": "abc123", "path": str(empty)}],
            )
            self.assertTrue(empty.is_dir())

    def test_gc_all_text_output_offers_manual_cleanup_only(self):
        with tempfile.TemporaryDirectory() as fake_home:
            workspace = Path(fake_home) / "no-registry"
            workspace.mkdir()
            pool = Path(fake_home) / ".delegate" / "worktrees"
            worktree = self._pool_worktree(
                pool,
                "abc123",
                "cursor-1",
                gitdir=str(Path(fake_home) / "gone" / ".git" / "worktrees" / "cursor-1"),
            )

            code, out, _err = self._run_cli(
                ["--cwd", str(workspace), "worktree", "gc", "--all", "--dry-run"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            self.assertIn(str(worktree), out)
            self.assertIn("source_root_missing", out)
            self.assertIn("reported only", out)
            self.assertNotIn("rm ", out)

    def test_gc_all_help_documents_report_only_contract(self):
        with tempfile.TemporaryDirectory() as fake_home:
            code, out, _err = self._run_cli(
                ["--json", "worktree", "gc", "--help"],
                home=fake_home,
            )

            self.assertEqual(code, 0)
            spec = json.loads(out)
            option_names = {option["flag"] for option in spec["options"]}
            self.assertIn("--all", option_names)
            self.assertIn("--pool", option_names)
            self.assertTrue(
                any("reported, never removed" in note for note in spec["notes"]),
                spec["notes"],
            )


if __name__ == "__main__":
    unittest.main()
