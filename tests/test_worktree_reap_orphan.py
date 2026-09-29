"""`worktree reap --path` as the exit for a git-linked pool entry with no run record.

`worktree remove` tells operators to use `reap --path` when a worktree's run
record is gone, but reap used to skip such an entry as `live_backlink` even with
`--force`, so a coordinator fell back to `git worktree remove --force` on fifty
of them. `--force --yes` now removes it after the checks below.
"""

from __future__ import annotations

import contextlib
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

    def _reap(self, pool: Path, worktree: Path, *, registry_root: Path | None = None, **kwargs):
        kwargs.setdefault("older_than_days", 0)
        kwargs.setdefault("yes", True)
        return worktree_gc_api.reap_worktrees(
            registry_root, pool_data_home=pool, path=str(worktree), **kwargs
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
            # The ledger edits were saved first, under the source repository's
            # Registry (the caller here has none).
            (reaped,) = result["reaped"]
            saved = Path(reaped["salvagePath"])
            self.assertEqual(
                saved.parent.resolve(), (Path(path) / ".delegate" / "salvage").resolve()
            )
            self.assertEqual((saved / ".beads" / "issues.jsonl").read_text(), "{}\n")
            self.assertEqual((saved / ".papercuts.jsonl").read_text(), "{}\n")

    def test_ledger_files_that_cannot_be_saved_refuse_the_removal(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-nocopy")
            (worktree / ".papercuts.jsonl").write_text("{}\n", encoding="utf-8")
            (Path(path) / ".delegate").mkdir()
            (Path(path) / ".delegate" / "salvage").write_text("in the way", encoding="utf-8")

            result = self._reap(pool, worktree, force=True)

            self.assertEqual(result["reaped"], [])
            self.assertEqual(
                [error["code"] for error in result["errors"]], ["ledger_salvage_failed"]
            )
            self.assertTrue(worktree.exists())
            self.assertIn(worktree.name, self._listed(path))

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

    def test_a_run_record_that_appears_after_planning_stops_the_removal(self):
        _repo, path = self._make_repo()
        registry = self._registry_root(path)
        # The caller's Registry exists but holds no record for the pool entry.
        self._seed_persistent_run(path, alias="cursor-other", branch="delegate/other")
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-race")
            real_lock = worktree_gc_api._reap_pool_lock

            @contextlib.contextmanager
            def lock_then_register_a_run(lock_path):
                # Planning is done and the pool lock is held; a launch now
                # registers a run whose worktree is this path.
                with real_lock(lock_path):
                    self._seed_persistent_run(
                        path,
                        alias="cursor-late",
                        branch="delegate/orphan-race",
                        execution_cwd=str(worktree),
                    )
                    yield

            with mock.patch.object(worktree_gc_api, "_reap_pool_lock", lock_then_register_a_run):
                result = self._reap(pool, worktree, force=True, registry_root=registry)

            self.assertEqual(result["reaped"], [])
            (error,) = result["errors"]
            self.assertEqual(error["code"], "record_owns_path")
            self.assertIn("liveness", error["hint"])
            self.assertTrue(worktree.exists())
            self.assertIn(worktree.name, self._listed(path))

    def test_the_same_reap_without_a_racing_record_still_removes_the_path(self):
        _repo, path = self._make_repo()
        registry = self._registry_root(path)
        self._seed_persistent_run(path, alias="cursor-other", branch="delegate/other")
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-calm")

            result = self._reap(pool, worktree, force=True, registry_root=registry)

            self.assertTrue(result["ok"], result)
            self.assertFalse(worktree.exists())

    def test_a_scan_that_cannot_run_refuses_the_removal_until_kill_live(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-blind")
            real_run = subprocess.run

            def no_lsof(argv, *args, **kwargs):
                if argv and argv[0] == "lsof":
                    raise FileNotFoundError("lsof")
                return real_run(argv, *args, **kwargs)

            # Neither /proc nor lsof: the machine cannot say who works in the path.
            with (
                mock.patch.object(worktree_procs, "_scan_proc", return_value=None),
                mock.patch.object(subprocess, "run", side_effect=no_lsof),
            ):
                refused = self._reap(pool, worktree, force=True)
                (skipped,) = refused["skipped"]
                self.assertEqual(skipped["reason"], "process_scan_unavailable")
                self.assertIn("lsof is not installed", skipped["processScan"])
                self.assertIn("--kill-live", skipped["hint"])
                self.assertEqual(refused["reaped"], [])
                self.assertTrue(worktree.exists())
                self.assertIn(worktree.name, self._listed(path))

                allowed = self._reap(pool, worktree, force=True, kill_live=True)

            self.assertTrue(allowed["ok"], allowed)
            self.assertFalse(worktree.exists())

    def test_a_scan_that_becomes_unavailable_after_planning_stops_the_removal(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            worktree = self._pool_entry(path, pool, "orphan-blind-late")
            scans = [
                worktree_procs.ProcessCwdScan(),
                worktree_procs.ProcessCwdScan(checked=False, note="lsof failed: boom"),
            ]

            with mock.patch.object(worktree_procs, "processes_with_cwd_inside", side_effect=scans):
                result = self._reap(pool, worktree, force=True)

            self.assertEqual(result["reaped"], [])
            self.assertEqual(
                [error["code"] for error in result["errors"]], ["process_scan_unavailable"]
            )
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

    def _fake_proc(self, tmp: str, processes: dict[str, str | None]) -> Path:
        """A procfs-shaped tree: pid -> the directory its cwd link points at (None: no link)."""
        root = Path(tmp) / "proc"
        (root / "self").mkdir(parents=True)
        (root / "self" / "cwd").symlink_to(tmp)
        for pid, cwd in processes.items():
            (root / pid).mkdir()
            (root / pid / "comm").write_text("shell\n", encoding="utf-8")
            if cwd is not None:
                (root / pid / "cwd").symlink_to(cwd)
        return root

    def _proc_scan(self, root: Path, target: Path, *, unreadable: tuple[str, ...] = ()):
        real_readlink = os.readlink

        def readlink(path, *args, **kwargs):
            if Path(path).parent.name in unreadable:
                raise PermissionError(13, "Permission denied", str(path))
            return real_readlink(path, *args, **kwargs)

        with (
            mock.patch.object(worktree_procs, "PROC_ROOT", root),
            mock.patch.object(worktree_procs.os, "readlink", side_effect=readlink),
        ):
            return worktree_procs.processes_with_cwd_inside(target)

    def test_proc_scan_finds_the_holder_and_ignores_the_bystander(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wt"
            (target / "deep").mkdir(parents=True)
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            root = self._fake_proc(
                tmp, {"101": str(target / "deep"), "102": str(elsewhere), "103": None}
            )

            scan = self._proc_scan(root, target)

            self.assertTrue(scan.checked, scan)
            self.assertEqual([(h.pid, h.command) for h in scan.holders], [(101, "shell")])

    def test_proc_scan_that_cannot_read_a_process_of_this_user_is_not_a_clean_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wt"
            target.mkdir()
            root = self._fake_proc(tmp, {"201": str(Path(tmp)), "202": str(Path(tmp))})

            scan = self._proc_scan(root, target, unreadable=("202",))

            self.assertEqual(scan.holders, ())
            self.assertFalse(scan.checked, scan)
            self.assertIn("1 process(es) of this user could not be inspected", scan.note)

    def test_proc_scan_notes_but_does_not_block_on_other_users_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wt"
            target.mkdir()
            root = self._fake_proc(tmp, {"301": str(Path(tmp)), "302": str(Path(tmp))})

            with mock.patch.object(worktree_procs, "_owned_by_another_user", return_value=True):
                scan = self._proc_scan(root, target, unreadable=("302",))

            self.assertTrue(scan.checked, scan)
            self.assertIn("belong to other users", scan.note)

    def _lsof(self, returncode: int, stdout: str, stderr: str = ""):
        completed = subprocess.CompletedProcess(["lsof"], returncode, stdout, stderr)
        with (
            mock.patch.object(worktree_procs, "_scan_proc", return_value=None),
            mock.patch.object(worktree_procs.subprocess, "run", return_value=completed),
        ):
            return worktree_procs.processes_with_cwd_inside(Path("/nonexistent/wt"))

    def test_lsof_that_prints_nothing_is_not_a_clean_answer(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                scan = self._lsof(returncode, "")
                self.assertFalse(scan.checked, scan)
                self.assertIn("no output", scan.note)

    def test_lsof_failing_outright_is_not_a_clean_answer_even_with_stray_output(self):
        scan = self._lsof(2, "p10\ncbash\nfcwd\nn/elsewhere\n", "lsof: WARNING: boom")
        self.assertFalse(scan.checked, scan)
        self.assertIn("boom", scan.note)

    def test_lsof_partial_answer_with_exit_one_still_counts_and_finds_holders(self):
        # Exit 1 is lsof's "some processes were unreadable"; the rest is printed.
        scan = self._lsof(1, "p10\ncbash\nfcwd\nn/nonexistent/wt/deep\np11\ncsh\nfcwd\nn/other\n")
        self.assertTrue(scan.checked, scan)
        self.assertEqual([(h.pid, h.command) for h in scan.holders], [(10, "bash")])


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
