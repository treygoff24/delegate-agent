"""One definition of "dirty", and the live-owner override, across remove/prune/retire.

A worktree that a launch seeded (synced files, the beads/papercuts ledgers) is
clean by the completion-retirement definition. ``prune`` and ``remove`` used raw
``git status``, so the operator's own cleanup verbs dead-ended on worktrees that
retirement would have removed. They now share ``inspect_worktree``.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path

from delegate_agent import config as config_api
from delegate_agent import errors as errors_api
from delegate_agent import run_registry as registry_api
from delegate_agent import worktree_commands, worktree_mgmt
from delegate_agent import worktree_gc as worktree_gc_api
from delegate_agent import worktree_records as records_api
from delegate_agent import worktree_remove as worktree_remove_api
from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git


class EffectiveDirtTestBase(WorktreeMgmtTestBase):
    def _set_run_state(self, repo_path: str, run_id: str, **fields) -> None:
        state_path = (
            registry_api.run_directory(self._registry_root(repo_path), run_id) / "state.json"
        )
        state = registry_api.read_json_object(state_path) or {}
        state.update(fields)
        registry_api.write_json_atomic(state_path, state)

    def _seeded_tree(self, repo_path: str, fake_home: str, alias: str) -> tuple[str, str]:
        """A merged worktree whose only dirt is what a launch seeds or a ledger holds."""
        branch = f"delegate/{alias}"
        wt_path = str(Path(fake_home) / "wt" / alias)
        run_id, _alias = self._seed_persistent_run(
            repo_path, alias=alias, branch=branch, execution_cwd=wt_path
        )
        self._create_worktree_at(repo_path, branch, wt_path)
        wt = Path(wt_path)
        (wt / "seed.txt").write_text("synced from source\n", encoding="utf-8")
        (wt / ".beads").mkdir()
        (wt / ".beads" / "issues.jsonl").write_text('{"id":"x"}\n', encoding="utf-8")
        (wt / ".papercuts.jsonl").write_text('{"cut":1}\n', encoding="utf-8")
        registry_root = self._registry_root(repo_path)
        manifest_path = registry_api.run_directory(registry_root, run_id) / "manifest.json"
        manifest = registry_api.read_json_object(manifest_path) or {}
        manifest["creationContext"][records_api.SYNCED_FILE_DIGESTS_KEY] = (
            records_api.capture_file_content_digests(wt, ["seed.txt"])
        )
        registry_api.write_json_atomic(manifest_path, manifest)
        return run_id, wt_path


class EffectiveDirtTests(EffectiveDirtTestBase):
    def test_prune_plans_and_removes_a_worktree_dirty_only_by_seeded_and_ledger_files(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-seeded")
            root = self._registry_root(path)

            dry = worktree_gc_api.prune_worktrees(root, merged=True, dry_run=True)
            self.assertEqual([entry["alias"] for entry in dry["planned"]], ["cursor-seeded"], dry)
            self.assertEqual(dry["skipped"], [])
            self.assertTrue(Path(wt_path).exists())

            result = worktree_gc_api.prune_worktrees(root, merged=True)

            self.assertTrue(result["ok"], result)
            self.assertEqual([entry["alias"] for entry in result["removed"]], ["cursor-seeded"])
            self.assertFalse(Path(wt_path).exists())

    def test_prune_still_refuses_when_a_seeded_file_was_edited(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-edited")
            (Path(wt_path) / "seed.txt").write_text("the lane changed this\n", encoding="utf-8")

            result = worktree_gc_api.prune_worktrees(
                self._registry_root(path), merged=True, dry_run=True
            )

            self.assertEqual(result["planned"], [])
            self.assertEqual(
                [(entry["alias"], entry["reason"]) for entry in result["skipped"]],
                [("cursor-edited", "dirty")],
            )

    def test_prune_still_refuses_a_file_the_lane_created(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-created")
            (Path(wt_path) / "lane-work.txt").write_text("real work\n", encoding="utf-8")

            result = worktree_gc_api.prune_worktrees(
                self._registry_root(path), merged=True, dry_run=True
            )

            self.assertEqual(result["planned"], [])
            self.assertEqual(result["skipped"][0]["reason"], "dirty")

    def test_remove_succeeds_without_discard_when_dirt_is_only_seeded_or_ledger(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-rm")

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-rm"], home=fake_home
            )

            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertTrue(payload["pathRemoved"])
            self.assertFalse(Path(wt_path).exists())
            # Nothing the lane made was thrown away, so nothing is reported discarded.
            self.assertNotIn("discardedDirtyPaths", payload)
            state = registry_api.load_run_state(self._registry_root(path), run_id)
            self.assertEqual(state["worktreeStatus"], "removed")

    def test_remove_api_default_is_effective_dirt(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-api")

            result = worktree_remove_api.remove_worktree(
                self._registry_root(path), handle="cursor-api", keep_branch=True
            )

            self.assertTrue(result["pathRemoved"], result)
            self.assertFalse(Path(wt_path).exists())

    def test_remove_names_only_lane_work_when_it_refuses(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-rm-dirty")
            (Path(wt_path) / "lane-work.txt").write_text("real work\n", encoding="utf-8")

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-rm-dirty"],
                home=fake_home,
            )

            self.assertEqual(code, errors_api.EXIT_USAGE, out)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "dirty_worktree")
            self.assertEqual(payload["dirtyPaths"], ["lane-work.txt"])
            self.assertTrue(Path(wt_path).exists())

    def test_inspect_worktree_defaults_to_effective_dirt(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seeded_tree(path, fake_home, "cursor-inspect")
            root = self._registry_root(path)
            record = worktree_mgmt.load_persistent_records(root)[0]

            default = worktree_mgmt.inspect_worktree(root, record, check_merge=False)
            self.assertIs(default.dirty, False, default)
            self.assertEqual(default.dirty_paths, ())

            # An explicit empty glob list turns the ledger discount off; the
            # unchanged seeded file is still discounted, the ledgers are not.
            no_globs = worktree_mgmt.inspect_worktree(
                root, record, check_merge=False, retirement_ignore_globs=()
            )
            self.assertIs(no_globs.dirty, True)
            self.assertEqual(
                sorted(no_globs.dirty_paths), [".beads/issues.jsonl", ".papercuts.jsonl"]
            )

    def test_cli_verbs_honour_configured_ledger_globs(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-globs")
            config = config_api.embedded_default_config()
            config["worktrees"]["retirementIgnoreGlobs"] = [".papercuts.jsonl"]
            command = worktree_commands.WorktreeCommand(
                action="prune", merged=True, dry_run=True, json_mode=True
            )
            stdout = io.StringIO()

            worktree_commands.emit(command, workspace_path=path, config=config, stdout=stdout)

            payload = json.loads(stdout.getvalue())
            # The configured list replaces the default, so `.beads/**` now counts as dirt.
            self.assertEqual(payload["planned"], [])
            self.assertEqual(payload["skipped"][0]["reason"], "dirty")
            self.assertTrue(Path(wt_path).exists())

            config["worktrees"]["retirementIgnoreGlobs"] = [".papercuts.jsonl", ".beads/**"]
            stdout = io.StringIO()
            worktree_commands.emit(command, workspace_path=path, config=config, stdout=stdout)
            self.assertEqual(
                [entry["alias"] for entry in json.loads(stdout.getvalue())["planned"]],
                ["cursor-globs"],
            )


class KillLiveTests(EffectiveDirtTestBase):
    def _live_tree(self, path: str, fake_home: str, alias: str) -> tuple[str, str]:
        branch = f"delegate/{alias}"
        wt_path = str(Path(fake_home) / "wt" / alias)
        run_id, _alias = self._seed_persistent_run(
            path, alias=alias, branch=branch, execution_cwd=wt_path
        )
        self._create_worktree_at(path, branch, wt_path)
        self._set_run_state(path, run_id, status="running", pid=os.getpid())
        return run_id, wt_path

    def test_force_on_a_live_lease_refuses_and_names_the_new_flag(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._live_tree(path, fake_home, "cursor-live")

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-live", "--force"],
                home=fake_home,
            )

            self.assertEqual(code, errors_api.EXIT_USAGE, out)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "run_active")
            self.assertIn("--kill-live", payload["message"])
            self.assertTrue(Path(wt_path).exists())
            self.assertEqual(
                git("rev-parse", "--verify", "delegate/cursor-live", cwd=path).returncode, 0
            )

    def test_kill_live_removes_a_live_worktree(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._live_tree(path, fake_home, "cursor-live-kill")

            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    path,
                    "--json",
                    "worktree",
                    "remove",
                    "cursor-live-kill",
                    "--kill-live",
                    "--keep-branch",
                ],
                home=fake_home,
            )

            self.assertEqual(code, 0, out)
            self.assertFalse(Path(wt_path).exists())

    def test_kill_live_alone_does_not_discard_uncommitted_work(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._live_tree(path, fake_home, "cursor-live-dirty")
            (Path(wt_path) / "lane-work.txt").write_text("real work\n", encoding="utf-8")

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-live-dirty", "--kill-live"],
                home=fake_home,
            )

            self.assertEqual(code, errors_api.EXIT_USAGE, out)
            self.assertEqual(json.loads(out)["code"], "dirty_worktree")
            self.assertTrue((Path(wt_path) / "lane-work.txt").exists())

    def test_prune_force_via_cli_skips_live_tree_with_hint(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._live_tree(path, fake_home, "cursor-live-prune")

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "prune", "--merged", "--force"],
                home=fake_home,
            )

            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertEqual(payload["removed"], [])
            self.assertEqual(payload["skipped"][0]["reason"], "run_active")
            self.assertIn("--kill-live", payload["skipped"][0]["hint"])
            self.assertTrue(Path(wt_path).exists())

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "prune", "--merged", "--kill-live"],
                home=fake_home,
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(Path(wt_path).exists())


class NestedRegistryTests(EffectiveDirtTestBase):
    """A run launched with --cwd inside a delegate worktree registers under that worktree."""

    def _tree_with_child_run(
        self, path: str, fake_home: str, alias: str, *, child_status: str
    ) -> tuple[str, str]:
        branch = f"delegate/{alias}"
        wt_path = str(Path(fake_home) / "wt" / alias)
        run_id, _alias = self._seed_persistent_run(
            path, alias=alias, branch=branch, execution_cwd=wt_path
        )
        self._create_worktree_at(path, branch, wt_path)
        # The child was launched with `--cwd <worktree>`, so its Registry lives at
        # <worktree>/.delegate and the owning Registry never sees it.
        child_id, _child_alias = self._seed_persistent_run(
            wt_path,
            alias="child-1",
            branch="delegate/child-1",
            execution_cwd=str(Path(fake_home) / "child-wt"),
        )
        self._set_run_state(wt_path, child_id, status=child_status, pid=os.getpid())
        # Keep the nested Registry from reading as dirt so only the guard is under test.
        exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=wt_path).stdout.strip())
        if not exclude.is_absolute():
            exclude = Path(wt_path) / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".delegate/\n", encoding="utf-8")
        return run_id, wt_path

    def test_remove_refuses_while_a_child_run_registered_inside_is_running(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._tree_with_child_run(
                path, fake_home, "cursor-parent", child_status="running"
            )

            code, out, _err = self._run_cli(
                ["--cwd", path, "--json", "worktree", "remove", "cursor-parent", "--force"],
                home=fake_home,
            )

            self.assertEqual(code, errors_api.EXIT_USAGE, out)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "nested_run_active")
            self.assertIn(".delegate", payload["message"])
            self.assertIn("--kill-live", payload["message"])
            self.assertTrue(Path(wt_path).exists())

    def test_prune_reports_nested_run_as_live_owner(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._tree_with_child_run(path, fake_home, "cursor-parent", child_status="running")

            result = worktree_gc_api.prune_worktrees(
                self._registry_root(path), merged=True, dry_run=True
            )

            self.assertEqual(result["planned"], [])
            self.assertEqual(result["skipped"][0]["reason"], "nested_run_active")
            self.assertIn("--kill-live", result["skipped"][0]["hint"])

    def test_remove_allows_a_worktree_whose_nested_runs_are_all_finished(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._tree_with_child_run(
                path, fake_home, "cursor-parent", child_status="succeeded"
            )

            result = worktree_remove_api.remove_worktree(
                self._registry_root(path), handle="cursor-parent", keep_branch=True
            )

            self.assertTrue(result["pathRemoved"], result)
            self.assertFalse(Path(wt_path).exists())

    def test_kill_live_overrides_a_running_nested_run(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._tree_with_child_run(
                path, fake_home, "cursor-parent", child_status="running"
            )

            result = worktree_remove_api.remove_worktree(
                self._registry_root(path),
                handle="cursor-parent",
                keep_branch=True,
                kill_live=True,
            )

            self.assertTrue(result["pathRemoved"], result)
            self.assertFalse(Path(wt_path).exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
