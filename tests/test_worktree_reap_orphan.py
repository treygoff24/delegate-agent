"""`worktree reap --path` as the exit for a git-linked pool entry with no run record.

`worktree remove` tells operators to use `reap --path` when a worktree's run
record is gone, but reap used to skip such an entry as `live_backlink` even with
`--force`, so a coordinator fell back to `git worktree remove --force` on fifty
of them. `--force --yes` now removes it after the checks below.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import config as config_api
from delegate_agent import worktree_commands, worktree_mgmt, worktree_procs
from delegate_agent import worktree_gc as worktree_gc_api
from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git

FINGERPRINT = "abc123def456"


class ReapLinkedOrphanTests(WorktreeMgmtTestBase):
    def _pool_entry(self, repo_path: str, pool: Path, name: str) -> Path:
        """A real `git worktree add` in the pool whose run record does not exist."""
        worktree = pool / FINGERPRINT / name
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-B", f"delegate/{name}", str(worktree), "HEAD", cwd=repo_path)
        return worktree

    def _reap(self, pool: Path, worktree: Path, **kwargs):
        kwargs.setdefault("older_than_days", 0)
        kwargs.setdefault("yes", True)
        return worktree_gc_api.reap_worktrees(
            None, pool_data_home=pool, path=str(worktree), **kwargs
        )

    def _listed(self, repo_path: str) -> str:
        return git("worktree", "list", "--porcelain", cwd=repo_path).stdout

    def test_without_force_a_linked_recordless_path_is_still_live(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-a")

            result = self._reap(pool, worktree)

            (skipped,) = result["skipped"]
            self.assertEqual(skipped["reason"], "live_backlink")
            self.assertIn("--force", skipped["hint"])
            self.assertTrue(worktree.exists())

    def test_force_yes_removes_a_clean_linked_recordless_path_and_keeps_its_branch(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-b")

            result = self._reap(pool, worktree, force=True)

            self.assertTrue(result["ok"], result)
            (reaped,) = result["reaped"]
            self.assertTrue(reaped["linkedOrphan"])
            self.assertEqual(result["skipped"], [])
            self.assertFalse(worktree.exists())
            self.assertNotIn(str(worktree.name), self._listed(path))
            # The branch is the durable artifact; only the checkout goes.
            self.assertEqual(
                git("rev-parse", "--verify", "delegate/orphan-b", cwd=path).returncode, 0
            )

    def test_force_without_yes_only_plans(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-c")

            result = self._reap(pool, worktree, force=True, yes=False)

            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"][0]["code"], "confirmation_required")
            self.assertTrue(worktree.exists())

    def test_dry_run_with_force_plans_without_removing(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-d")

            result = self._reap(pool, worktree, force=True, dry_run=True)

            self.assertEqual([entry["linkedOrphan"] for entry in result["planned"]], [True])
            self.assertEqual(result["reaped"], [])
            self.assertTrue(worktree.exists())

    def test_uncommitted_work_blocks_until_discard_is_explicit(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-e")
            (worktree / "lane-work.txt").write_text("unique work\n", encoding="utf-8")

            refused = self._reap(pool, worktree, force=True)

            (skipped,) = refused["skipped"]
            self.assertEqual(skipped["reason"], "dirty")
            self.assertEqual(skipped["dirtyPaths"], ["lane-work.txt"])
            self.assertTrue((worktree / "lane-work.txt").exists())

            allowed = self._reap(pool, worktree, force=True, discard_uncommitted=True)

            self.assertTrue(allowed["ok"], allowed)
            self.assertFalse(worktree.exists())

    def test_ledger_only_dirt_does_not_block(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-f")
            (worktree / ".beads").mkdir()
            (worktree / ".beads" / "issues.jsonl").write_text("{}\n", encoding="utf-8")
            (worktree / ".papercuts.jsonl").write_text("{}\n", encoding="utf-8")

            result = self._reap(pool, worktree, force=True)

            self.assertTrue(result["ok"], result)
            self.assertFalse(worktree.exists())

    def test_a_process_working_inside_blocks_removal(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-g")
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"], cwd=worktree
            )
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)

            refused = self._reap(pool, worktree, force=True)

            (skipped,) = refused["skipped"]
            self.assertEqual(skipped["reason"], "process_cwd_inside")
            self.assertEqual([item["pid"] for item in skipped["processes"]], [child.pid])
            self.assertIn("--kill-live", skipped["hint"])
            self.assertTrue(worktree.exists())

            forced = self._reap(pool, worktree, force=True, kill_live=True)

            self.assertTrue(forced["ok"], forced)
            self.assertFalse(worktree.exists())

    def test_a_process_that_arrives_after_planning_stops_the_removal(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-late")
            arrived = worktree_procs.ProcessCwdScan(
                holders=(worktree_procs.ProcessHolder(4242, "late-shell"),)
            )
            # First look (planning) finds nothing; the second look, taken under
            # the locks just before removal, finds a process.
            scans = [worktree_procs.ProcessCwdScan(), arrived]

            with mock.patch.object(worktree_procs, "processes_with_cwd_inside", side_effect=scans):
                result = self._reap(pool, worktree, force=True)

            self.assertEqual(result["reaped"], [])
            self.assertEqual([error["code"] for error in result["errors"]], ["process_cwd_inside"])
            self.assertTrue(worktree.exists())

    def test_a_record_in_the_source_repositorys_own_registry_refuses(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = pool / FINGERPRINT / "owned"
            worktree.parent.mkdir(parents=True)
            branch = "delegate/owned"
            self._seed_persistent_run(
                path, alias="cursor-owned", branch=branch, execution_cwd=str(worktree)
            )
            self._create_worktree_at(path, branch, str(worktree))

            # Called from a workspace whose Registry has no such record, the entry
            # looks record-less; the repository's own Registry says a run owns it.
            result = self._reap(pool, worktree, force=True)

            (skipped,) = result["skipped"]
            self.assertEqual(skipped["reason"], "record_in_other_registry")
            self.assertEqual(skipped["registryWorkspace"], path)
            self.assertTrue(worktree.exists())

    def test_a_path_git_does_not_list_is_not_removed_as_linked(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-h")

            with mock.patch.object(
                worktree_mgmt, "_worktree_list_paths_with_warning", return_value=([path], None)
            ):
                result = self._reap(pool, worktree, force=True)

            (skipped,) = result["skipped"]
            self.assertEqual(skipped["reason"], "not_registered_with_git")
            self.assertEqual(result["reaped"], [])
            self.assertTrue(worktree.exists())

    def test_a_pool_scan_warning_refuses_even_with_force(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-w")
            # Break Git's back-pointer: the entry is freshly changed and its
            # metadata is inconsistent, so the pool scan will not classify it.
            (Path(path) / ".git" / "worktrees" / "orphan-w" / "gitdir").unlink()

            result = self._reap(pool, worktree, force=True)

            (skipped,) = result["skipped"]
            self.assertEqual(skipped["reason"], "worktree_unsettled")
            self.assertTrue(worktree.exists())

    def test_a_pool_scan_warning_still_applies_when_the_pool_is_reached_by_an_alias(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-x")
            (Path(path) / ".git" / "worktrees" / "orphan-x" / "gitdir").unlink()
            alias = Path(tmp) / "alias"
            alias.symlink_to(pool, target_is_directory=True)

            result = worktree_gc_api.reap_worktrees(
                None,
                pool_data_home=alias,
                path=str(alias / FINGERPRINT / "orphan-x"),
                older_than_days=0,
                yes=True,
                force=True,
            )

            (skipped,) = result["skipped"]
            self.assertEqual(skipped["reason"], "worktree_unsettled")
            self.assertTrue(worktree.exists())

    def test_cli_reap_removes_the_orphan_git_still_links(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            pool = Path(fake_home) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-cli")
            config = config_api.embedded_default_config()
            config["worktrees"]["dataHome"] = str(pool)
            command = worktree_commands.WorktreeCommand(
                action="reap",
                path=str(worktree),
                older_than_days=0,
                yes=True,
                force=True,
                json_mode=True,
            )
            stdout = io.StringIO()

            code = worktree_commands.emit(
                command, workspace_path=path, config=config, stdout=stdout
            )

            self.assertEqual(code, 0, stdout.getvalue())
            self.assertFalse(worktree.exists())


class ProcessCwdScanTests(unittest.TestCase):
    def test_finds_a_process_whose_cwd_is_inside_and_ignores_one_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            inside = Path(tmp) / "wt" / "deep"
            inside.mkdir(parents=True)
            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
            holder = subprocess.Popen(sleeper, cwd=inside)
            bystander = subprocess.Popen(sleeper, cwd=outside)
            self.addCleanup(bystander.wait)
            self.addCleanup(bystander.kill)
            self.addCleanup(holder.wait)
            self.addCleanup(holder.kill)
            time.sleep(0.2)

            scan = worktree_procs.processes_with_cwd_inside(Path(tmp) / "wt")

            self.assertTrue(scan.checked, scan)
            pids = {holder_.pid for holder_ in scan.holders}
            self.assertIn(holder.pid, pids)
            self.assertNotIn(bystander.pid, pids)

    def test_a_sibling_with_a_shared_name_prefix_is_not_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wt"
            target.mkdir()
            sibling = Path(tmp) / "wt-other"
            sibling.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"], cwd=sibling
            )
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            time.sleep(0.2)

            scan = worktree_procs.processes_with_cwd_inside(target)

            self.assertNotIn(child.pid, {holder.pid for holder in scan.holders})

    def test_lsof_output_parser(self):
        rows = worktree_procs._parse_lsof_cwds(
            "p10\ncbash\nfcwd\nn/a/b\np11\ncnode\nftxt\nn/not/a/cwd\nfcwd\nn/c\n"
        )
        self.assertEqual(rows, [(10, "bash", "/a/b"), (11, "node", "/c")])

    def test_a_scan_that_could_not_run_says_so_instead_of_reporting_nothing(self):
        with (
            mock.patch.object(worktree_procs, "_scan_proc", return_value=None),
            mock.patch.object(worktree_procs.subprocess, "run", side_effect=FileNotFoundError),
        ):
            scan = worktree_procs.processes_with_cwd_inside(Path.cwd())
        self.assertFalse(scan.checked)
        self.assertEqual(scan.holders, ())
        self.assertIn("lsof", scan.note)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
