from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
if SRC not in sys.path:
    sys.path.insert(0, SRC)

GIT_TEST_IDENTITY = (
    "-c",
    "user.name=Delegate Test",
    "-c",
    "user.email=delegate-test@example.com",
)


def load_delegate():
    spec = importlib.util.spec_from_file_location("delegate_cli_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(*args: str, cwd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *GIT_TEST_IDENTITY, *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


class WorktreeMgmtTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def _make_repo(self, *, prefix: str = "delegate-wt-test-") -> tuple[tempfile.TemporaryDirectory, str]:
        repo = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(repo.cleanup)
        git("init", cwd=repo.name)
        readme = Path(repo.name) / "README.md"
        readme.write_text("hello\n", encoding="utf-8")
        git("add", "README.md", cwd=repo.name)
        git("commit", "-m", "init", cwd=repo.name)
        return repo, repo.name

    def _registry_root(self, repo_path: str) -> Path:
        return Path(repo_path) / ".delegate"

    def _run_cli(self, args: list[str], *, home: str) -> tuple[int, str, str]:
        self.assertNotEqual(Path(home).expanduser().resolve(), Path.home().resolve())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
            code = self.delegate.main(args, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def _seed_persistent_run(
        self,
        repo_path: str,
        *,
        alias: str = "cursor-4",
        harness: str = "cursor",
        branch: str | None = None,
        execution_cwd: str | None = None,
        worktree_status: str = "present",
        creation_oid: str | None = None,
        source_head_ref: str | None | object = object(),
        last_activity_at: str | None = None,
    ) -> tuple[str, str]:
        registry_root = self.delegate.run_registry.ensure_registry(
            Path(repo_path),
            workspace_kind="git",
        )
        run_id, allocated_alias = self.delegate.run_registry.register_run(
            registry_root,
            harness=harness,
            metadata={"mode": "work", "cwd": repo_path},
        )
        if branch is None:
            branch = f"delegate/cursor-{self.delegate.short_run_id(run_id)}"
        if execution_cwd is None:
            execution_cwd = str(Path(repo_path).parent / "worktree" / allocated_alias)
        if creation_oid is None:
            creation_oid = git("rev-parse", "HEAD", cwd=repo_path).stdout.strip()
        if not isinstance(source_head_ref, (str, type(None))):
            ref_result = subprocess.run(
                ["git", "-C", repo_path, "symbolic-ref", "--quiet", "HEAD"],
                text=True,
                capture_output=True,
                check=False,
            )
            source_head_ref = ref_result.stdout.strip() if ref_result.returncode == 0 else None
        source_branch = (
            source_head_ref[len("refs/heads/"):]
            if isinstance(source_head_ref, str) and source_head_ref.startswith("refs/heads/")
            else None
        )
        common_dir = git("rev-parse", "--git-common-dir", cwd=repo_path).stdout.strip()
        if not Path(common_dir).is_absolute():
            common_dir = str(Path(repo_path) / common_dir)
        creation_context = {
            "sourceHeadOid": creation_oid,
            "sourceHeadRef": source_head_ref,
            "sourceBranch": source_branch,
            "sourceGitCommonDir": common_dir,
            "branch": branch,
            "plannedBranch": branch,
            "plannedExecutionCwd": execution_cwd,
        }
        run_path = self.delegate.run_registry.run_directory(registry_root, run_id)
        run_path.mkdir(parents=True, exist_ok=True)
        manifest_alias = alias if alias != allocated_alias else allocated_alias
        manifest = {
            "schema": self.delegate.run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": manifest_alias,
            "harness": harness,
            "engine": harness,
            "mode": "work",
            "model": "composer-2.5",
            "cwd": repo_path,
            "executionCwd": execution_cwd,
            "sourceGitRoot": repo_path,
            "isolatedWorkspace": True,
            "isolationMode": "worktree",
            "effectiveIsolation": "worktree",
            "isolationLifecycle": "persistent",
            "preservedWorkspace": True,
            "branch": branch,
            "worktreeStatus": worktree_status,
            "creationContext": creation_context,
            "startedAt": self.delegate.run_registry.utc_now_iso(),
        }
        if last_activity_at is None:
            last_activity_at = self.delegate.run_registry.utc_now_iso()
        state = {
            "schema": self.delegate.run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": manifest_alias,
            "status": "succeeded",
            "worktreeStatus": worktree_status,
            "lastActivityAt": last_activity_at,
        }
        self.delegate.run_registry.write_json_atomic(run_path / "manifest.json", manifest)
        self.delegate.run_registry.write_json_atomic(run_path / "state.json", state)
        if alias != allocated_alias:
            index = self.delegate.run_registry.load_index(registry_root)
            index["aliases"].pop(allocated_alias, None)
            (registry_root / "aliases" / allocated_alias).unlink(missing_ok=True)
            index["aliases"][alias] = run_id
            entry = index["runs"][run_id]
            if isinstance(entry, dict):
                entry["alias"] = alias
            self.delegate.run_registry.bind_alias_claim(registry_root, alias, run_id)
            self.delegate.run_registry.save_index(registry_root, index)
        return run_id, alias

    def _create_worktree_at(
        self,
        repo_path: str,
        branch: str,
        worktree_path: str,
        *,
        dirty_file: str | None = None,
    ) -> None:
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-B", branch, worktree_path, "HEAD", cwd=repo_path)
        if dirty_file:
            (Path(worktree_path) / dirty_file).write_text("dirty\n", encoding="utf-8")

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

            clean_branch, clean_wt = cases["clean-merged"]
            dirty_branch, dirty_wt = cases["dirty-merged"]
            detached_branch, detached_wt = cases["detached"]
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
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "missing")

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
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "unknown")

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
            short_id = self.delegate.short_run_id(fixed_run_id)
            branch = f"delegate/cursor-{short_id}"
            git("branch", branch, cwd=path)
            before = git("rev-parse", branch, cwd=path).stdout.strip()
            marker = Path(fake_home) / "child-launched"
            registry = Path(path) / ".delegate"
            registry.mkdir(parents=True, exist_ok=True)
            (registry / "config.json").write_text(
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
                mock.patch.dict(os.environ, {"HOME": fake_home}, clear=False),
                mock.patch.object(self.delegate.run_registry, "generate_run_id", return_value=fixed_run_id),
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

    def test_runs_json_exposes_persistent_worktree_metadata(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-runs-meta"
            wt_path = str(Path(fake_home) / "wt" / "cursor-runs-meta")
            self._seed_persistent_run(path, alias="cursor-runs", branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)
            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "runs"],
                home=fake_home,
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            entries = payload.get("runs", [])
            matching = [
                e for e in entries
                if e.get("alias") == "cursor-runs"
            ]
            self.assertEqual(len(matching), 1)
            entry = matching[0]
            self.assertEqual(entry.get("isolationLifecycle"), "persistent")
            self.assertIn("executionCwd", entry)
            self.assertIn("worktreeStatus", entry)

    def test_worktree_list_harness_filter(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
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

    def test_worktree_list_status_filter(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch_a = "delegate/cursor-present"
            wt_path_a = str(Path(fake_home) / "wt" / "cursor-present")
            branch_b = "delegate/cursor-removed"
            wt_path_b = str(Path(fake_home) / "wt" / "cursor-removed")
            self._seed_persistent_run(
                path, alias="cursor-present", branch=branch_a, execution_cwd=wt_path_a,
                worktree_status="present",
            )
            self._seed_persistent_run(
                path, alias="cursor-removed", branch=branch_b, execution_cwd=wt_path_b,
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

    def test_worktree_public_payloads_do_not_expose_private_record_keys(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-public"
            wt_path = str(Path(fake_home) / "wt" / "cursor-public")
            self._seed_persistent_run(path, alias="cursor-public", branch=branch, execution_cwd=wt_path)
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
        with tempfile.TemporaryDirectory() as fake_home:
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
            with mock.patch.object(self.delegate.worktree_mgmt, "_run_git", return_value=failed_delete):
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
            with mock.patch.object(self.delegate.worktree_mgmt, "_run_git", return_value=failed_delete):
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
                path, alias="cursor-old", branch=branch_old, execution_cwd=wt_path_old,
                last_activity_at=old_ts,
            )
            self._seed_persistent_run(
                path, alias="cursor-recent", branch=branch_recent, execution_cwd=wt_path_recent,
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
                path, alias="cursor-ap", branch=branch, execution_cwd=wt_path,
                last_activity_at=old_ts,
            )
            self._create_worktree_at(path, branch, wt_path)
            config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
            result = self.delegate.worktree_mgmt.maybe_auto_prune(
                self._registry_root(path), config
            )
            self.assertIsNotNone(result)
            index = self.delegate.run_registry.load_index(self._registry_root(path))
            run_id = index["aliases"].get("cursor-ap")
            self.assertIsNotNone(run_id)
            st = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(st.get("worktreeStatus"), "removed")

    def test_maybe_auto_prune_skipped_when_disabled(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seed_persistent_run(path, alias="cursor-ap-off")
            config = {"worktrees": {"autoPrune": {"enabled": False}}}
            result = self.delegate.worktree_mgmt.maybe_auto_prune(
                self._registry_root(path), config
            )
            self.assertIsNone(result)

    def test_worktree_list_no_auto_prune_skips_opportunistic_pass(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-noap"
            wt_path = str(Path(fake_home) / "wt" / "cursor-noap")
            self._seed_persistent_run(path, alias="cursor-noap", branch=branch, execution_cwd=wt_path)
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
            self._seed_persistent_run(path, alias="cursor-noap-cli", branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)
            config_path = self._registry_root(path) / "config.json"
            config_path.write_text(
                json.dumps({"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}),
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
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state.get("worktreeStatus"), "present")
            self.assertTrue(Path(wt_path).exists())

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
            self.assertEqual([entry["alias"] for entry in payload["removed"]], ["cursor-detached-cli-remove"])
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

            self.assertEqual({entry["alias"] for entry in result["planned"]}, {"droid-prune-filter"})
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
            self.assertEqual([entry["alias"] for entry in result["removed"]], ["cursor-prune-force"])
            removed = result["removed"][0]
            self.assertTrue(removed["pathRemoved"])
            self.assertTrue(removed["branchRemoved"])
            self.assertFalse(Path(wt_path).exists())
            self.assertNotEqual(git("rev-parse", "--verify", branch, cwd=path, check=False).returncode, 0)
            state = self.delegate.run_registry.load_run_state(self._registry_root(path), run_id)
            self.assertIn("discardedDirtyPaths", state)

    def test_maybe_auto_prune_skips_when_lock_contended(self):
        """maybe_auto_prune returns within ~1s when registry lock is contended."""
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-locked"
            wt_path = str(Path(fake_home) / "wt" / "cursor-locked")
            self._seed_persistent_run(
                path, alias="cursor-locked", branch=branch, execution_cwd=wt_path,
            )
            self._create_worktree_at(path, branch, wt_path)
            registry_root = self._registry_root(path)
            config = {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}

            # Hold the registry lock in a background thread.
            import threading
            lock_held = threading.Event()
            def hold_lock():
                with self.delegate.run_registry.registry_lock(registry_root, timeout_seconds=30):
                    lock_held.set()
                    # Keep lock for 5 seconds — long enough for maybe_auto_prune to notice.
                    time.sleep(5)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            lock_held.wait(timeout=5)  # wait for lock to be acquired

            # now maybe_auto_prune should return quickly (within 1s) since lock is contended.
            t0 = time.monotonic()
            result = self.delegate.worktree_mgmt.maybe_auto_prune(registry_root, config)
            elapsed = time.monotonic() - t0

            holder.join(timeout=10)

            # Result should indicate skip/unavailable (lock contention).
            self.assertIsNotNone(result)
            self.assertTrue(result.get("skipped") or not result.get("ok"))
            # Should return within 1s (not block for the 30s default lock timeout).
            self.assertLess(elapsed, 3.0, msg="maybe_auto_prune blocked on lock contention")

    def test_double_remove_noop_under_5s(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            branch = "delegate/cursor-double"
            wt_path = str(Path(fake_home) / "wt" / "cursor-double")
            self._seed_persistent_run(path, alias="cursor-dbl", branch=branch, execution_cwd=wt_path)
            self._create_worktree_at(path, branch, wt_path)
            registry_root = self._registry_root(path)
            first = self.delegate.worktree_mgmt.remove_worktree(registry_root, handle="cursor-dbl")
            self.assertTrue(first.get("removed"))
            t0 = time.monotonic()
            second = self.delegate.worktree_mgmt.remove_worktree(registry_root, handle="cursor-dbl")
            elapsed = time.monotonic() - t0
            self.assertTrue(second.get("noop"))
            self.assertLess(elapsed, 5.0)

    def test_dirty_info_returns_null_when_git_unavailable(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            # Create a plain (non-git) directory as execution_cwd
            plain_dir = Path(fake_home) / "plain-worktree"
            plain_dir.mkdir()
            (plain_dir / "somefile.txt").write_text("content\n", encoding="utf-8")
            self._seed_persistent_run(
                path, alias="cursor-gitfail", execution_cwd=str(plain_dir)
            )
            index = self.delegate.run_registry.load_index(self._registry_root(path))
            run_id = index["aliases"].get("cursor-gitfail")
            record = self.delegate.worktree_mgmt._record_for_run(
                self._registry_root(path), run_id, {}
            )
            result, paths, total, warnings = self.delegate.worktree_mgmt.dirty_info(
                record, "present"
            )
            self.assertIsNone(result)
            self.assertTrue(len(warnings) > 0)
            self.assertTrue(warnings[0].startswith("git status failed:"))

    def test_run_git_timeout_returns_structured_failure(self):
        _repo, path = self._make_repo()
        with mock.patch.object(
            self.delegate.worktree_mgmt.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git", "status"], 30),
        ):
            result = self.delegate.worktree_mgmt._run_git(path, ["status"])
        self.assertEqual(result.returncode, 124)
        self.assertIn("git command timed out", result.stderr)

    def test_merged_into_source_returns_null_when_git_unavailable(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            # Create a plain (non-git) directory as execution_cwd
            plain_dir = Path(fake_home) / "plain-worktree2"
            plain_dir.mkdir()
            (plain_dir / "somefile.txt").write_text("content\n", encoding="utf-8")
            self._seed_persistent_run(
                path, alias="cursor-mgitfail", execution_cwd=str(plain_dir)
            )
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
            from delegate_agent import rendering as delegate_rendering
            loaded_snapshot = self.delegate.run_registry.load_run_snapshot(registry_root, run_id)
            view = delegate_rendering.merge_snapshot_view(
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
            registry_root = self._registry_root(path)
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
                "mergedIntoSource": False,
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
            idx_ahead = lines.find("vs creation base")
            idx_porcelain = lines.find("porcelain: clean")
            idx_suggested = lines.find("suggested commands")
            idx_trail_meta = lines.find("execution:")  # start of trailing metadata

            self.assertGreater(idx_creation, -1, "creation-context line missing")
            self.assertGreater(idx_dirty, -1, "dirty line missing")
            self.assertGreater(idx_merged, -1, "merged line missing")
            self.assertGreater(idx_ahead, -1, "ahead/behind line missing")
            self.assertGreater(idx_porcelain, -1, "porcelain: clean line missing")
            self.assertGreater(idx_suggested, -1, "suggested commands missing")
            self.assertGreater(idx_trail_meta, -1, "trailing metadata missing")

            # Assert each section appears after the previous one.
            self.assertLess(idx_creation, idx_dirty)
            self.assertLess(idx_dirty, idx_merged)
            self.assertLess(idx_merged, idx_ahead)
            self.assertLess(idx_ahead, idx_porcelain)
            self.assertLess(idx_porcelain, idx_suggested)
            # Trailing metadata should come AFTER suggested commands (spec L621 order).
            self.assertLess(idx_suggested, idx_trail_meta)


if __name__ == "__main__":
    unittest.main()
