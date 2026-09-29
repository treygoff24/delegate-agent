"""Ledger edits are saved before any worktree removal that would delete them.

The ledger globs (``.beads/**``, ``.papercuts.jsonl``) are discounted from
"uncommitted work" so harness churn cannot pin a worktree. That discount cannot
tell churn from a real edit, so every removal first copies the changed ledger
files to ``<registry>/salvage/<worktree>-<timestamp>/`` and refuses if it cannot.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from delegate_agent import errors as errors_api
from delegate_agent import run_registry as registry_api
from delegate_agent import worktree_gc as worktree_gc_api
from delegate_agent import worktree_records as records_api
from delegate_agent import worktree_remove as worktree_remove_api
from delegate_agent import worktree_salvage
from tests.test_worktree_effective_dirt import EffectiveDirtTestBase
from tests.worktree_mgmt_test_base import git

POOL_FINGERPRINT = "abc123def456"


class SalvageTestBase(EffectiveDirtTestBase):
    def _salvage_root(self, path: str) -> Path:
        return (self._registry_root(path) / "salvage").resolve()

    def _remove_cli(self, path: str, fake_home: str, alias: str, *extra: str, json_mode=True):
        args = ["--cwd", path]
        if json_mode:
            args.append("--json")
        args += ["worktree", "remove", alias, "--keep-branch", *extra]
        return self._run_cli(args, home=fake_home)

    def _commit_ledger(self, path: str, relative: str, text: str) -> None:
        target = Path(path) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git("add", relative, cwd=path)
        git("commit", "-m", f"track {relative}", cwd=path)

    def _worktree(self, path: str, fake_home: str, alias: str) -> tuple[str, str]:
        branch = f"delegate/{alias}"
        wt_path = str(Path(fake_home) / "wt" / alias)
        run_id, _alias = self._seed_persistent_run(
            path, alias=alias, branch=branch, execution_cwd=wt_path
        )
        self._create_worktree_at(path, branch, wt_path)
        return run_id, wt_path


class RemoveSalvageTests(SalvageTestBase):
    def test_remove_saves_untracked_ledger_files_byte_for_byte_and_reports_where(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-salv")

            code, out, _err = self._remove_cli(path, fake_home, "cursor-salv")

            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertFalse(Path(wt_path).exists())
            salvage = Path(payload["salvagePath"])
            self.assertEqual(salvage.parent.resolve(), self._salvage_root(path))
            self.assertTrue(salvage.name.startswith("cursor-salv-"), salvage.name)
            self.assertEqual((salvage / ".beads" / "issues.jsonl").read_bytes(), b'{"id":"x"}\n')
            self.assertEqual((salvage / ".papercuts.jsonl").read_bytes(), b'{"cut":1}\n')
            # The seeded file is unchanged since launch; the source checkout holds it.
            self.assertFalse((salvage / "seed.txt").exists())
            self.assertEqual(
                sorted(payload["salvagedPaths"]), [".beads/issues.jsonl", ".papercuts.jsonl"]
            )

    def test_an_edited_tracked_ledger_file_is_saved_with_the_edit_not_the_committed_bytes(self):
        _repo, path = self._make_repo()
        self._commit_ledger(path, ".beads/issues.jsonl", "committed\n")
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._worktree(path, fake_home, "cursor-tracked")
            edited = b'{"id":"edited"}\n{"id":"more"}\n'
            (Path(wt_path) / ".beads" / "issues.jsonl").write_bytes(edited)

            code, out, _err = self._remove_cli(path, fake_home, "cursor-tracked")

            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertFalse(Path(wt_path).exists())
            saved = Path(payload["salvagePath"]) / ".beads" / "issues.jsonl"
            self.assertEqual(saved.read_bytes(), edited)
            self.assertEqual(payload["salvagedPaths"], [".beads/issues.jsonl"])

    def test_text_output_says_where_the_files_went(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seeded_tree(path, fake_home, "cursor-text")

            code, out, _err = self._remove_cli(path, fake_home, "cursor-text", json_mode=False)

            self.assertEqual(code, 0, out)
            (line,) = [item for item in out.splitlines() if item.startswith("saved ")]
            self.assertIn("2 changed ledger file(s) to ", line)
            self.assertIn(str(self._salvage_root(path)), line)

    def test_a_worktree_with_no_ledger_changes_leaves_no_salvage_directory(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._worktree(path, fake_home, "cursor-clean")

            code, out, _err = self._remove_cli(path, fake_home, "cursor-clean")

            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertNotIn("salvagePath", payload)
            self.assertNotIn("salvagedPaths", payload)
            self.assertFalse(Path(wt_path).exists())
            self.assertFalse(self._salvage_root(path).exists())

    def test_a_ledger_file_unchanged_since_launch_is_not_copied(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-seeded-ledger")
            manifest_path = (
                registry_api.run_directory(self._registry_root(path), run_id) / "manifest.json"
            )
            manifest = registry_api.read_json_object(manifest_path) or {}
            manifest["creationContext"][records_api.SYNCED_FILE_DIGESTS_KEY] = (
                records_api.capture_file_content_digests(
                    Path(wt_path), ["seed.txt", ".papercuts.jsonl"]
                )
            )
            registry_api.write_json_atomic(manifest_path, manifest)

            code, out, _err = self._remove_cli(path, fake_home, "cursor-seeded-ledger")

            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["salvagedPaths"], [".beads/issues.jsonl"])

    def test_a_deleted_ledger_file_has_nothing_to_copy_and_does_not_block(self):
        _repo, path = self._make_repo()
        self._commit_ledger(path, ".papercuts.jsonl", "old cut\n")
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._worktree(path, fake_home, "cursor-deleted")
            (Path(wt_path) / ".papercuts.jsonl").unlink()

            code, out, _err = self._remove_cli(path, fake_home, "cursor-deleted")

            self.assertEqual(code, 0, out)
            self.assertNotIn("salvagePath", json.loads(out))
            self.assertFalse(Path(wt_path).exists())

    def test_discard_uncommitted_still_saves_the_ledger_and_discards_real_work(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-discard")
            (Path(wt_path) / "lane-work.txt").write_text("real work\n", encoding="utf-8")

            code, out, _err = self._remove_cli(
                path, fake_home, "cursor-discard", "--discard-uncommitted"
            )

            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertEqual(payload["discardedDirtyPaths"], ["lane-work.txt"])
            salvage = Path(payload["salvagePath"])
            self.assertEqual((salvage / ".papercuts.jsonl").read_bytes(), b'{"cut":1}\n')
            self.assertFalse((salvage / "lane-work.txt").exists())
            self.assertFalse(Path(wt_path).exists())

    def test_a_refused_removal_saves_nothing(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-refused")
            (Path(wt_path) / "lane-work.txt").write_text("real work\n", encoding="utf-8")

            code, out, _err = self._remove_cli(path, fake_home, "cursor-refused")

            self.assertEqual(code, errors_api.EXIT_USAGE, out)
            self.assertEqual(json.loads(out)["code"], "dirty_worktree")
            self.assertFalse(self._salvage_root(path).exists())
            self.assertTrue(Path(wt_path).exists())

    def test_a_failed_copy_refuses_the_removal_and_loses_nothing(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-nocopy")
            # A regular file where the salvage directory has to go.
            self._registry_root(path).joinpath("salvage").write_text("in the way", encoding="utf-8")

            code, out, _err = self._remove_cli(path, fake_home, "cursor-nocopy")

            self.assertNotEqual(code, 0, out)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "ledger_salvage_failed")
            self.assertIn("nothing was removed", payload["message"])
            self.assertTrue(Path(wt_path).exists())
            self.assertEqual(
                (Path(wt_path) / ".beads" / "issues.jsonl").read_bytes(), b'{"id":"x"}\n'
            )

    def test_a_copy_that_does_not_match_the_original_refuses_and_leaves_no_partial_directory(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-mismatch")

            with mock.patch.object(worktree_salvage.filecmp, "cmp", return_value=False):
                code, out, _err = self._remove_cli(path, fake_home, "cursor-mismatch")

            self.assertNotEqual(code, 0, out)
            self.assertEqual(json.loads(out)["code"], "ledger_salvage_failed")
            self.assertTrue(Path(wt_path).exists())
            self.assertEqual(list(self._salvage_root(path).iterdir()), [])

    def test_a_path_outside_the_worktree_is_never_copied(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-escape")
            outside = Path(fake_home) / "outside.txt"
            outside.write_text("not a ledger file\n", encoding="utf-8")

            with (
                mock.patch.object(
                    worktree_salvage, "ledger_changes", return_value=["../../outside.txt"]
                ),
                self.assertRaises(worktree_salvage.wm.WorktreeManagementError) as raised,
            ):
                worktree_salvage.salvage_ledger_changes(
                    execution_cwd=wt_path,
                    registry_root=self._registry_root(path),
                    creation_context=None,
                    ignore_globs=(".beads/**",),
                )

            self.assertEqual(raised.exception.code, "ledger_salvage_failed")
            self.assertEqual(list(self._salvage_root(path).iterdir()), [])

    def test_salvage_survives_prune_and_a_later_removal_gets_its_own_directory(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seeded_tree(path, fake_home, "cursor-first")
            first = worktree_remove_api.remove_worktree(
                self._registry_root(path), handle="cursor-first", keep_branch=True
            )
            kept = Path(first["salvagePath"])

            # Nothing left to prune; the run directory is what prune and retention
            # ever delete, never the salvage directory.
            pruned = worktree_gc_api.prune_worktrees(self._registry_root(path), merged=True)
            runs_code, runs_out, _err = self._run_cli(
                ["--cwd", path, "--json", "runs", "prune", "--older-than", "0"],
                home=fake_home,
            )

            self.assertTrue(pruned["ok"], pruned)
            self.assertEqual(runs_code, 0, runs_out)
            # The prune really deleted the removed worktree's run directory.
            self.assertTrue(json.loads(runs_out)["removed"], runs_out)
            self.assertEqual((kept / ".papercuts.jsonl").read_bytes(), b'{"cut":1}\n')
            self._seeded_tree(path, fake_home, "cursor-second")
            second = worktree_remove_api.remove_worktree(
                self._registry_root(path), handle="cursor-second", keep_branch=True
            )
            self.assertNotEqual(second["salvagePath"], first["salvagePath"])
            self.assertTrue(kept.exists())


class PruneSalvageTests(SalvageTestBase):
    def test_prune_saves_ledger_files_and_reports_the_path_on_each_removed_entry(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._seeded_tree(path, fake_home, "cursor-pruned")

            result = worktree_gc_api.prune_worktrees(self._registry_root(path), merged=True)

            self.assertTrue(result["ok"], result)
            (removed,) = result["removed"]
            saved = Path(removed["salvagePath"])
            self.assertEqual((saved / ".beads" / "issues.jsonl").read_bytes(), b'{"id":"x"}\n')
            self.assertEqual(
                sorted(removed["salvagedPaths"]), [".beads/issues.jsonl", ".papercuts.jsonl"]
            )

    def test_prune_keeps_a_worktree_whose_ledger_cannot_be_saved(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            _run_id, wt_path = self._seeded_tree(path, fake_home, "cursor-prune-nocopy")
            self._registry_root(path).joinpath("salvage").write_text("in the way", encoding="utf-8")

            result = worktree_gc_api.prune_worktrees(self._registry_root(path), merged=True)

            self.assertFalse(result["ok"])
            self.assertEqual(result["removed"], [])
            self.assertEqual(result["errors"][0]["code"], "ledger_salvage_failed")
            self.assertTrue(Path(wt_path).exists())


class ReapSalvageTests(SalvageTestBase):
    def _pooled(self, path: str, fake_home: str, alias: str) -> tuple[Path, Path]:
        pool = Path(fake_home) / "pool"
        wt = pool / POOL_FINGERPRINT / alias
        self._seed_persistent_run(
            path, alias=alias, branch=f"delegate/{alias}", execution_cwd=str(wt)
        )
        self._create_worktree_at(path, f"delegate/{alias}", str(wt))
        (wt / ".beads").mkdir()
        (wt / ".beads" / "issues.jsonl").write_bytes(b'{"id":"reap"}\n')
        return pool, wt

    def _reap(self, path: str, pool: Path, alias: str):
        return worktree_gc_api.reap_worktrees(
            self._registry_root(path),
            pool_data_home=pool,
            handle=alias,
            older_than_days=0,
            yes=True,
            force=True,
        )

    def test_reap_of_a_recorded_worktree_saves_the_ledger_first(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            pool, wt = self._pooled(path, fake_home, "cursor-reap")

            result = self._reap(path, pool, "cursor-reap")

            self.assertTrue(result["ok"], result)
            (reaped,) = result["reaped"]
            saved = Path(reaped["salvagePath"])
            self.assertEqual(saved.parent.resolve(), self._salvage_root(path))
            self.assertEqual((saved / ".beads" / "issues.jsonl").read_bytes(), b'{"id":"reap"}\n')
            self.assertEqual(reaped["salvagedPaths"], [".beads/issues.jsonl"])
            self.assertFalse(wt.exists())

    def test_reap_keeps_a_recorded_worktree_whose_ledger_cannot_be_saved(self):
        _repo, path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            pool, wt = self._pooled(path, fake_home, "cursor-reap-nocopy")
            self._registry_root(path).joinpath("salvage").write_text("in the way", encoding="utf-8")

            result = self._reap(path, pool, "cursor-reap-nocopy")

            self.assertFalse(result["ok"])
            self.assertEqual(result["reaped"], [])
            self.assertEqual(
                [error["code"] for error in result["errors"]], ["ledger_salvage_failed"]
            )
            self.assertTrue(wt.exists())
            self.assertEqual((wt / ".beads" / "issues.jsonl").read_bytes(), b'{"id":"reap"}\n')
