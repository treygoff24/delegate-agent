import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git


class WorktreeRemoveTests(WorktreeMgmtTestBase):
    def test_worktree_remove_refuses_source_root_target(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(
                path,
                alias="cursor-guarded",
                branch=git("branch", "--show-current", cwd=path).stdout.strip(),
                execution_cwd=path,
            )
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-guarded"],
                home=fake_home,
            )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertEqual(json.loads(out)["code"], "source_root_guard")
        self.assertTrue(Path(path).exists())

    def test_worktree_remove_dirty_fails_with_envelope(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-dirty"
            wt_path = str(Path(fake_home) / "wt" / "cursor-dirty")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-4",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path, dirty_file="scratch.txt")
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4"],
                home=fake_home,
            )
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "dirty_worktree")
            self.assertIn("?? scratch.txt", payload["dirtyPaths"])
            self.assertEqual(
                payload["nextActions"],
                [
                    "delegate worktree show cursor-4",
                    "delegate worktree remove cursor-4 --discard-uncommitted",
                ],
            )
            self.assertTrue(Path(wt_path).exists())
            self.assertEqual((Path(wt_path) / "scratch.txt").read_text(encoding="utf-8"), "dirty\n")
            self.assertEqual(git("rev-parse", "--verify", branch, cwd=path).returncode, 0)
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "present")
            self.assertNotIn("worktreeRemovedAt", state)
            self.assertNotIn("discardedDirtyPaths", state)

    def test_worktree_remove_discard_records_dirty_paths(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-discard"
            wt_path = str(Path(fake_home) / "wt" / "cursor-discard")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-4",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path, dirty_file="scratch.txt")
            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    path,
                    "--json",
                    "worktree",
                    "remove",
                    "cursor-4",
                    "--discard-uncommitted",
                ],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["pathRemoved"])
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "removed")
            self.assertIn("discardedDirtyPaths", state)

    def test_worktree_remove_clean_removes_path_and_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-clean"
            wt_path = str(Path(fake_home) / "wt" / "cursor-clean")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-4",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["removed"])
            self.assertTrue(payload["pathRemoved"])
            self.assertTrue(payload["branchRemoved"])
            self.assertFalse(Path(wt_path).exists())
            branch_check = git("rev-parse", "--verify", branch, cwd=path, check=False)
            self.assertNotEqual(branch_check.returncode, 0)
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "removed")

    def test_worktree_remove_group_removes_matching_runs_only(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            first_branch = "delegate/cursor-group-a"
            first_wt = str(Path(fake_home) / "wt" / "cursor-group-a")
            first_run_id, _first_alias = self._seed_persistent_run(
                path,
                alias="cursor-group-a",
                branch=first_branch,
                execution_cwd=first_wt,
            )
            second_branch = "delegate/cursor-group-b"
            second_wt = str(Path(fake_home) / "wt" / "cursor-group-b")
            second_run_id, _second_alias = self._seed_persistent_run(
                path,
                alias="cursor-group-b",
                branch=second_branch,
                execution_cwd=second_wt,
            )
            other_branch = "delegate/cursor-other"
            other_wt = str(Path(fake_home) / "wt" / "cursor-other")
            other_run_id, _other_alias = self._seed_persistent_run(
                path,
                alias="cursor-other",
                branch=other_branch,
                execution_cwd=other_wt,
            )
            self._tag_run_group(path, first_run_id, "wave4")
            self._tag_run_group(path, second_run_id, "wave4")
            self._tag_run_group(path, other_run_id, "other")
            self._create_worktree_at(path, first_branch, first_wt)
            self._create_worktree_at(path, second_branch, second_wt)
            self._create_worktree_at(path, other_branch, other_wt)

            code, out, err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "--group", "wave4"],
                home=fake_home,
            )

            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["group"], "wave4")
            self.assertEqual(payload["matched"], 2)
            self.assertEqual(len(payload["removed"]), 2)
            self.assertFalse(Path(first_wt).exists())
            self.assertFalse(Path(second_wt).exists())
            self.assertTrue(Path(other_wt).exists())

    def test_worktree_remove_group_zero_matches_errors(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            # Seed one run in a different group so the registry exists, then
            # remove a group with zero matches.
            other_branch = "delegate/cursor-other"
            other_wt = str(Path(fake_home) / "wt" / "cursor-other")
            other_run_id, _other_alias = self._seed_persistent_run(
                path,
                alias="cursor-other",
                branch=other_branch,
                execution_cwd=other_wt,
            )
            self._tag_run_group(path, other_run_id, "other")
            self._create_worktree_at(path, other_branch, other_wt)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "--group", "nope"],
                home=fake_home,
            )
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "no_matching_worktrees")
            self.assertEqual(payload["group"], "nope")
            self.assertEqual(payload["matched"], 0)
            # The unrelated run is untouched.
            self.assertTrue(Path(other_wt).exists())

    def test_worktree_remove_keep_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-keep"
            wt_path = str(Path(fake_home) / "wt" / "cursor-keep")
            self._seed_persistent_run(path, alias="cursor-4", branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4", "--keep-branch"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            # Branch is merged; branchKept="requested" reflects the keep-branch
            # request on a merged branch (not the "unmerged" per-spec state).
            self.assertEqual(payload["branchKept"], "requested")
            self.assertEqual(git("rev-parse", "--verify", branch, cwd=path).returncode, 0)

    def test_worktree_remove_unmerged_branch_refuses_by_default(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-unmerged"
            wt_path = str(Path(fake_home) / "wt" / "cursor-unmerged")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-4",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4"],
                home=fake_home,
            )
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "unmerged_branch")
            self.assertIn("delegate worktree remove cursor-4 --keep-branch", payload["nextActions"])
            self.assertTrue(Path(wt_path).exists())
            self.assertEqual(git("rev-parse", "--verify", branch, cwd=path).returncode, 0)
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "present")

    def test_worktree_remove_force_branch_after_path_removed_deletes_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-force-later"
            wt_path = str(Path(fake_home) / "wt" / "cursor-force-later")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-4",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            (Path(wt_path) / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", "feature.txt", cwd=wt_path)
            git("commit", "-m", "feature", cwd=wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4", "--keep-branch"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            first_payload = json.loads(out)
            self.assertTrue(first_payload["pathRemoved"])
            self.assertFalse(first_payload["branchRemoved"])
            # Branch is unmerged and kept (spec L673); branchKept reflects
            # the branch state, not the --keep-branch origin.
            self.assertEqual(first_payload["branchKept"], "unmerged")
            self.assertFalse(Path(wt_path).exists())
            self.assertEqual(git("rev-parse", "--verify", branch, cwd=path).returncode, 0)
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "removed")
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4", "--force-branch"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertFalse(payload["pathRemoved"])
            self.assertTrue(payload["branchRemoved"])
            self.assertFalse(payload["noop"])
            self.assertEqual(payload["worktreeStatus"], "removed")
            branch_check = git("rev-parse", "--verify", branch, cwd=path, check=False)
            self.assertNotEqual(branch_check.returncode, 0)

    def test_worktree_remove_noop_when_already_removed(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(
                path,
                alias="cursor-4",
                worktree_status="removed",
            )
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["noop"])

    def test_worktree_remove_clean_repeat_is_noop(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-repeat"
            wt_path = str(Path(fake_home) / "wt" / "cursor-repeat")
            self._seed_persistent_run(path, alias="cursor-4", branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)
            first_code, _first_out, _first_err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4"],
                home=fake_home,
            )
            self.assertEqual(first_code, 0)
            second_code, second_out, _second_err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-4"],
                home=fake_home,
            )
            self.assertEqual(second_code, 0)
            second_payload = json.loads(second_out)
            self.assertTrue(second_payload["noop"])
            self.assertFalse(second_payload["pathRemoved"])
            self.assertFalse(second_payload["branchRemoved"])

    def test_not_worktree_run_error(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            registry_root = self.delegate.run_registry.ensure_registry(
                Path(path),
                workspace_kind="git",
            )
            _run_id, alias = self.delegate.run_registry.register_run(
                registry_root,
                harness="cursor",
                metadata={"mode": "safe", "cwd": path},
            )
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "show", alias],
                home=fake_home,
            )
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(json.loads(out)["code"], "not_worktree_run")

    def test_branch_collision_does_not_delete_preexisting_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            fixed_run_id = "del_20260101T000000Z_abcdef"
            short_id = self.delegate.worktree_execution.short_run_id(fixed_run_id)
            branch = f"delegate/cursor-{short_id}"
            git("branch", branch, cwd=path)
            before = git("rev-parse", branch, cwd=path).stdout.strip()
            marker = Path(fake_home) / "child-launched"
            config_path = Path(fake_home) / "delegate-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "cursor": {
                            "argvPrefix": [
                                "python3",
                                "-c",
                                (
                                    "import pathlib, sys; "
                                    "pathlib.Path(sys.argv[1]).write_text('launched\\n'); "
                                    "sys.exit(0)"
                                ),
                                str(marker),
                            ],
                            "defaultModel": "composer-2.5",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"HOME": fake_home, "DELEGATE_CONFIG": str(config_path)},
                    clear=False,
                ),
                mock.patch.object(
                    self.delegate.run_registry,
                    "generate_run_id",
                    return_value=fixed_run_id,
                ),
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = self.delegate.main(
                    [
                        "--cwd",
                        path,
                        "--json",
                        "--isolation",
                        "worktree",
                        "cursor",
                        "work",
                        "trigger branch collision",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(json.loads(stdout.getvalue())["error"], "branch_collision")
            after = git("rev-parse", branch, cwd=path).stdout.strip()
            self.assertEqual(after, before)
            self.assertFalse(marker.exists())
            worktree_root = Path(fake_home) / ".delegate" / "worktrees"
            wt_dirs = list(worktree_root.glob("*/*")) if worktree_root.exists() else []
            self.assertEqual(wt_dirs, [])

    def test_worktree_remove_missing_path(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            wt_path = str(Path(fake_home) / "nonexistent" / "path")
            self._seed_persistent_run(
                path, alias="cursor-missing", execution_cwd=wt_path, worktree_status="missing"
            )
            result = self.delegate.worktree_mgmt.remove_worktree(
                self._registry_root(path),
                handle="cursor-missing",
            )
            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("removed"))
            self.assertFalse(result.get("pathRemoved"))
            self.assertFalse(result.get("branchRemoved"))
            self.assertEqual(result.get("worktreeStatus"), "removed")

    def test_worktree_remove_missing_path_force_branch_deletes_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-missing-force"
            wt_path = str(Path(fake_home) / "nonexistent" / "path")
            git("branch", branch, cwd=path)
            self._seed_persistent_run(
                path,
                alias="cursor-missing",
                branch=branch,
                execution_cwd=wt_path,
                worktree_status="missing",
            )
            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    path,
                    "--json",
                    "worktree",
                    "remove",
                    "cursor-missing",
                    "--force-branch",
                ],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload.get("ok"))
            self.assertTrue(payload.get("branchRemoved"))
            self.assertNotIn("branchKept", payload)
            self.assertNotEqual(
                git("rev-parse", "--verify", branch, cwd=path, check=False).returncode,
                0,
            )

    def test_worktree_remove_branch_delete_error_uses_error_field(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory():
            self._seed_persistent_run(
                path,
                alias="cursor-removed",
                branch="delegate/cursor-delete-error",
                worktree_status="removed",
            )
            failed_delete = subprocess.CompletedProcess(
                ["git", "branch", "-D", "delegate/cursor-delete-error"],
                1,
                "",
                "fatal: cannot delete branch\n",
            )
            with mock.patch.object(
                self.delegate.worktree_mgmt, "_run_git", return_value=failed_delete
            ):
                result = self.delegate.worktree_mgmt.remove_worktree(
                    self._registry_root(path),
                    handle="cursor-removed",
                    force_branch=True,
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "branch_remove_failed")
            self.assertEqual(result["error"], "branch_remove_failed")
            self.assertEqual(result["exitCode"], self.delegate.EXIT_USAGE)
            self.assertFalse(result["branchRemoved"])
            self.assertEqual(result["branchRemovalError"], "fatal: cannot delete branch")
            self.assertNotIn("branchKept", result)

    def test_worktree_remove_present_path_branch_delete_error_reports_partial_success(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-partial-branch-error"
            wt_path = str(Path(fake_home) / "wt" / "cursor-partial-branch-error")
            run_id, _alias = self._seed_persistent_run(
                path,
                alias="cursor-partial-branch-error",
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
                result = self.delegate.worktree_mgmt.remove_worktree(
                    self._registry_root(path),
                    handle="cursor-partial-branch-error",
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "branch_remove_failed")
            self.assertTrue(result["removed"])
            self.assertTrue(result["pathRemoved"])
            self.assertTrue(result["partialSuccess"])
            self.assertFalse(result["branchRemoved"])
            self.assertEqual(result["branchRemovalError"], "fatal: cannot delete branch")
            self.assertEqual(
                result["nextActions"],
                ["delegate worktree remove cursor-partial-branch-error --force-branch"],
            )
            self.assertFalse(Path(wt_path).exists())
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertIsNotNone(state)
            self.assertEqual(state.get("worktreeStatus"), "removed")

    def test_worktree_remove_branch_delete_error_exits_nonzero(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(
                path,
                alias="cursor-removed",
                branch="delegate/cursor-delete-error",
                worktree_status="removed",
            )
            failed_delete = subprocess.CompletedProcess(
                ["git", "branch", "-D", "delegate/cursor-delete-error"],
                1,
                "",
                "fatal: cannot delete branch\n",
            )
            with mock.patch.object(
                self.delegate.worktree_mgmt, "_run_git", return_value=failed_delete
            ):
                code, out, _err = self._run_cli(
                    [
                        "--cwd",
                        path,
                        "--json",
                        "worktree",
                        "remove",
                        "cursor-removed",
                        "--force-branch",
                    ],
                    home=fake_home,
                )
            payload = json.loads(out)
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "branch_remove_failed")
            self.assertEqual(payload["exitCode"], self.delegate.EXIT_USAGE)

    def test_worktree_remove_branch_delete_timeout_uses_git_timeout_code(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(
                path,
                alias="cursor-removed",
                branch="delegate/cursor-delete-timeout",
                worktree_status="removed",
            )
            timeout = subprocess.CompletedProcess(
                ["git", "branch", "-D", "delegate/cursor-delete-timeout"],
                124,
                "",
                "git command timed out after 30s\n",
            )
            with mock.patch.object(self.delegate.worktree_mgmt, "_run_git", return_value=timeout):
                code, out, _err = self._run_cli(
                    [
                        "--cwd",
                        path,
                        "--json",
                        "worktree",
                        "remove",
                        "cursor-removed",
                        "--force-branch",
                    ],
                    home=fake_home,
                )
            payload = json.loads(out)
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "git_timeout")
            self.assertEqual(payload["error"], "git_timeout")
            self.assertEqual(payload["exitCode"], self.delegate.EXIT_USAGE)

    def test_worktree_remove_refuses_when_dirty_check_fails_without_discard(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-dirty-check-failed"
            wt_path = str(Path(fake_home) / "wt" / "cursor-dirty-check-failed")
            self._seed_persistent_run(
                path,
                alias="cursor-dirty-check-failed",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)

            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "porcelain_status",
                    return_value=(None, None, ["git status failed: boom"]),
                ),
                self.assertRaises(self.delegate.worktree_mgmt.WorktreeManagementError) as ctx,
            ):
                self.delegate.worktree_mgmt.remove_worktree(
                    self._registry_root(path),
                    handle="cursor-dirty-check-failed",
                )

            self.assertEqual(ctx.exception.code, "dirty_check_failed")
            self.assertIn("git status failed: boom", ctx.exception.payload["warnings"])
            self.assertTrue(Path(wt_path).exists())

    def test_double_remove_noop_under_5s(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-double"
            wt_path = str(Path(fake_home) / "wt" / "cursor-double")
            self._seed_persistent_run(
                path, alias="cursor-dbl", branch=branch, execution_cwd=wt_path
            )
            self._create_worktree_at(path, branch, wt_path)
            registry_root = self._registry_root(path)
            first = self.delegate.worktree_mgmt.remove_worktree(registry_root, handle="cursor-dbl")
            self.assertTrue(first.get("removed"))
            t0 = time.monotonic()
            second = self.delegate.worktree_mgmt.remove_worktree(registry_root, handle="cursor-dbl")
            elapsed = time.monotonic() - t0
            self.assertTrue(second.get("noop"))
            self.assertLess(elapsed, 5.0)

    def test_worktree_remove_unknown_dirty_status_refuses_without_discard(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-unknown-remove-dirty"
            wt_path = str(Path(fake_home) / "wt" / "cursor-unknown-remove-dirty")
            self._seed_persistent_run(
                path,
                alias="cursor-unknown-remove-dirty",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path, dirty_file="scratch.txt")

            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "detect_worktree_status",
                    return_value=("unknown", ["forced unknown"]),
                ),
                self.assertRaises(self.delegate.worktree_mgmt.WorktreeManagementError) as ctx,
            ):
                self.delegate.worktree_mgmt.remove_worktree(
                    self._registry_root(path),
                    handle="cursor-unknown-remove-dirty",
                )

            self.assertEqual(ctx.exception.code, "dirty_worktree")

    def test_worktree_remove_unknown_status_still_checks_unmerged_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-unknown-unmerged"
            wt_path = str(Path(fake_home) / "wt" / "cursor-unknown-unmerged")
            self._seed_persistent_run(
                path,
                alias="cursor-unknown-unmerged",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)

            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "detect_worktree_status",
                    return_value=("unknown", ["forced unknown"]),
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "merged_into_source",
                    return_value=(False, []),
                ),
                self.assertRaises(self.delegate.worktree_mgmt.WorktreeManagementError) as ctx,
            ):
                self.delegate.worktree_mgmt.remove_worktree(
                    self._registry_root(path),
                    handle="cursor-unknown-unmerged",
                )

            self.assertEqual(ctx.exception.code, "unmerged_branch")

    def test_worktree_remove_reports_merge_check_failed_when_merge_unknown(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-merge-check-unknown"
            wt_path = str(Path(fake_home) / "wt" / "cursor-merge-check-unknown")
            self._seed_persistent_run(
                path,
                alias="cursor-merge-check-unknown",
                branch=branch,
                execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)

            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "merged_into_source",
                    return_value=(None, ["could not determine whether branch is merged"]),
                ),
                self.assertRaises(self.delegate.worktree_mgmt.WorktreeManagementError) as ctx,
            ):
                self.delegate.worktree_mgmt.remove_worktree(
                    self._registry_root(path),
                    handle="cursor-merge-check-unknown",
                )

            self.assertEqual(ctx.exception.code, "merge_check_failed")
            self.assertIn(
                "could not determine whether branch is merged", ctx.exception.payload["warnings"]
            )

    def test_dirty_info_returns_null_when_git_unavailable(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            plain_dir = Path(fake_home) / "plain-worktree"
            plain_dir.mkdir()
            (plain_dir / "somefile.txt").write_text("content\n", encoding="utf-8")
            self._seed_persistent_run(path, alias="cursor-gitfail", execution_cwd=str(plain_dir))
            index = self.delegate.run_registry.load_index(self._registry_root(path))
            run_id = index["aliases"].get("cursor-gitfail")
            record = self.delegate.worktree_mgmt._record_for_run(
                self._registry_root(path), run_id, {}
            )
            result, _paths, _total, warnings = self.delegate.worktree_mgmt.dirty_info(
                record, "present"
            )
            self.assertIsNone(result)
            self.assertTrue(len(warnings) > 0)
            self.assertTrue(warnings[0].startswith("git status failed:"))

    def test_dirty_info_checks_unknown_status_when_path_exists(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-unknown-dirty"
            wt_path = str(Path(fake_home) / "wt" / "cursor-unknown-dirty")
            run_id, _alias = self._seed_persistent_run(path, branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path, dirty_file="scratch.txt")
            record = self.delegate.worktree_mgmt._record_for_run(
                self._registry_root(path), run_id, {}
            )

            dirty, paths, total, warnings = self.delegate.worktree_mgmt.dirty_info(
                record,
                "unknown",
            )

            self.assertTrue(dirty)
            self.assertEqual(total, 1)
            self.assertEqual(warnings, [])
            self.assertIn("scratch.txt", paths[0])


if __name__ == "__main__":
    unittest.main()
