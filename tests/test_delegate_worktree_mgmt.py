from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
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
        state = {
            "schema": self.delegate.run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": manifest_alias,
            "status": "succeeded",
            "worktreeStatus": worktree_status,
            "lastActivityAt": self.delegate.run_registry.utc_now_iso(),
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


if __name__ == "__main__":
    unittest.main()
