"""dlg-3pl.7: finished runs give their scratch back, independent of record pruning."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import cli, retention, run_registry, run_scratch  # noqa: E402

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ScratchReclaimTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        home = self.base / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        temp_root = self.base / "var-tmp"
        temp_root.mkdir()
        temp_patcher = mock.patch.object(run_scratch, "PERSISTENT_TEMP_ROOT", temp_root)
        temp_patcher.start()
        self.addCleanup(temp_patcher.stop)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        self.now = datetime.now(UTC)

    def stamp(self, age_days: float) -> str:
        return (self.now - timedelta(days=age_days)).strftime(TS_FORMAT)

    def make_run(
        self,
        *,
        status: str = "succeeded",
        age_days: float = 10,
        scratch: bool = True,
        sidecar: bool = False,
        compact: bool = False,
        size: int = 100,
        pid: int | None = None,
        record_pointers: bool = True,
    ) -> str:
        run_id, alias = run_registry.register_run(self.registry_root, harness="codex")
        run_path = run_registry.run_directory(self.registry_root, run_id)
        when = self.stamp(age_days)
        state: dict = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "lastActivityAt": when,
        }
        if status != "running":
            state["finishedAt"] = when
        if pid is not None:
            state["pid"] = pid
        manifest: dict = {
            "schema": run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": "codex",
            "startedAt": when,
        }
        if scratch:
            path = run_scratch.allocate(self.registry_root, run_id)
            (path / "artifact.bin").write_bytes(b"x" * size)
            if record_pointers:
                manifest["scratchPath"] = str(path)
        if sidecar:
            side = run_scratch.allocate_sidecar(self.registry_root, run_id, "home")
            (side / "engine.cfg").write_bytes(b"y" * size)
        if compact:
            temp = run_scratch.allocate_compact_temp(self.registry_root, run_id)
            (temp / "child.tmp").write_bytes(b"z" * size)
            if record_pointers:
                manifest["tempPath"] = str(temp)
        run_registry.write_json_atomic(run_path / "state.json", state)
        run_registry.write_json_atomic(run_path / "manifest.json", manifest)
        return run_id

    def scratch_path(self, run_id: str) -> Path:
        return run_scratch.expected_path(self.registry_root, run_id)

    def state_of(self, run_id: str) -> dict:
        path = run_registry.run_directory(self.registry_root, run_id) / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def reclaim(self, **kwargs) -> dict:
        kwargs.setdefault("now", self.now)
        return retention.reclaim_scratch(self.registry_root, **kwargs)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli.main(["--cwd", str(self.workspace), *argv], stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()


class ReclaimScratchTests(ScratchReclaimTestCase):
    def test_terminal_old_run_gives_back_scratch_sidecar_and_compact_temp(self) -> None:
        run_id = self.make_run(sidecar=True, compact=True, size=100)
        scratch = self.scratch_path(run_id)
        sidecar = scratch.parent / f"{run_id}{run_scratch.SIDECAR_SEPARATOR}home"
        compact = run_scratch.expected_compact_temp_path(self.registry_root, run_id)
        for path in (scratch, sidecar, compact):
            self.assertTrue(path.exists(), path)

        result = self.reclaim()

        self.assertTrue(result["ok"], result)
        self.assertEqual([item["runId"] for item in result["reclaimed"]], [run_id])
        self.assertEqual(result["totalBytes"], 300)
        for path in (scratch, sidecar, compact):
            self.assertFalse(path.exists(), path)

    def test_the_run_record_stays_and_says_the_scratch_was_reclaimed(self) -> None:
        run_id = self.make_run(size=64)
        before = self.state_of(run_id)

        self.reclaim()

        after = self.state_of(run_id)
        self.assertEqual(after[retention.SCRATCH_RECLAIMED_BYTES_KEY], 64)
        self.assertRegex(after[retention.SCRATCH_RECLAIMED_AT_KEY], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(after["status"], "succeeded")
        self.assertEqual(after["finishedAt"], before["finishedAt"])
        run_path = run_registry.run_directory(self.registry_root, run_id)
        self.assertTrue((run_path / "manifest.json").exists())
        self.assertIn(run_id, run_registry.load_index(self.registry_root)["runs"])

    def test_failed_and_cancelled_runs_qualify_too(self) -> None:
        failed = self.make_run(status="failed")
        cancelled = self.make_run(status="cancelled")

        result = self.reclaim()

        self.assertEqual({item["runId"] for item in result["reclaimed"]}, {failed, cancelled})

    def test_a_running_run_is_never_touched_however_old(self) -> None:
        run_id = self.make_run(status="running", age_days=90, pid=os.getpid(), compact=True)

        result = self.reclaim(older_than_days=0)

        self.assertEqual(result["reclaimed"], [])
        self.assertEqual(
            [(s["runId"], s["reason"]) for s in result["skipped"]], [(run_id, "running")]
        )
        self.assertTrue(self.scratch_path(run_id).exists())
        self.assertTrue(run_scratch.expected_compact_temp_path(self.registry_root, run_id).exists())
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

    def test_a_stale_run_is_left_for_the_operator(self) -> None:
        run_id = self.make_run(status="running", age_days=90, pid=None)

        result = self.reclaim(older_than_days=0)

        self.assertEqual(result["reclaimed"], [])
        self.assertEqual(
            [(s["runId"], s["reason"]) for s in result["skipped"]], [(run_id, "non_terminal")]
        )
        self.assertTrue(self.scratch_path(run_id).exists())

    def test_a_run_only_reclaims_after_the_configured_age(self) -> None:
        run_id = self.make_run(age_days=10)

        young = self.reclaim(older_than_days=30)
        self.assertEqual(young["reclaimed"], [])
        self.assertEqual(young["skipped"][0]["reason"], "not_yet_old_enough")
        self.assertTrue(self.scratch_path(run_id).exists())

        old = self.reclaim(older_than_days=5)
        self.assertEqual([item["runId"] for item in old["reclaimed"]], [run_id])
        self.assertFalse(self.scratch_path(run_id).exists())

    def test_a_run_that_never_recorded_scratch_is_skipped(self) -> None:
        run_id = self.make_run(scratch=False)

        result = self.reclaim()

        self.assertEqual(
            [(s["runId"], s["reason"]) for s in result["skipped"]], [(run_id, "no_scratch")]
        )
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

    def manifest_of(self, run_id: str) -> dict:
        path = run_registry.run_directory(self.registry_root, run_id) / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_a_sidecar_only_leftover_is_reclaimed_though_the_manifest_records_no_paths(
        self,
    ) -> None:
        run_id = self.make_run(scratch=False, sidecar=True, size=40)
        manifest = self.manifest_of(run_id)
        self.assertNotIn("scratchPath", manifest)
        self.assertNotIn("tempPath", manifest)
        scratch = self.scratch_path(run_id)
        sidecar = scratch.parent / f"{run_id}{run_scratch.SIDECAR_SEPARATOR}home"
        self.assertTrue(sidecar.exists())

        result = self.reclaim()

        self.assertTrue(result["ok"], result)
        self.assertEqual([item["runId"] for item in result["reclaimed"]], [run_id])
        self.assertEqual(result["totalBytes"], 40)
        self.assertFalse(sidecar.exists())
        self.assertIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

    def test_owned_paths_are_reclaimed_even_when_the_manifest_records_no_pointer(self) -> None:
        run_id = self.make_run(sidecar=True, compact=True, record_pointers=False, size=10)
        manifest = self.manifest_of(run_id)
        self.assertNotIn("scratchPath", manifest)
        self.assertNotIn("tempPath", manifest)
        scratch = self.scratch_path(run_id)
        sidecar = scratch.parent / f"{run_id}{run_scratch.SIDECAR_SEPARATOR}home"
        compact = run_scratch.expected_compact_temp_path(self.registry_root, run_id)

        result = self.reclaim()

        self.assertEqual([item["runId"] for item in result["reclaimed"]], [run_id])
        self.assertEqual(result["totalBytes"], 30)
        for path in (scratch, sidecar, compact):
            self.assertFalse(path.exists(), path)

    def test_a_second_pass_finds_nothing_left_to_do(self) -> None:
        run_id = self.make_run()
        self.reclaim()

        again = self.reclaim()

        self.assertEqual(again["reclaimed"], [])
        self.assertEqual(again["planned"], [])
        self.assertEqual(
            [(s["runId"], s["reason"]) for s in again["skipped"]], [(run_id, "already_reclaimed")]
        )

    def test_a_marked_run_is_not_walked_again(self) -> None:
        run_id = self.make_run()
        self.reclaim()
        # Something recreates the path after the marker; the marker gates the walk.
        self.scratch_path(run_id).parent.mkdir(parents=True, exist_ok=True)
        recreated = run_scratch.allocate(self.registry_root, run_id)

        self.reclaim()

        self.assertTrue(recreated.exists())

    def test_dry_run_lists_sizes_and_changes_nothing(self) -> None:
        run_id = self.make_run(sidecar=True, compact=True, size=50)
        state_before = (
            run_registry.run_directory(self.registry_root, run_id) / "state.json"
        ).read_bytes()

        result = self.reclaim(dry_run=True)

        self.assertTrue(result["dryRun"])
        self.assertEqual(result["reclaimed"], [])
        self.assertEqual([item["runId"] for item in result["planned"]], [run_id])
        self.assertEqual(result["planned"][0]["scratchBytes"], 150)
        self.assertEqual(len(result["planned"][0]["paths"]), 3)
        self.assertEqual(result["totalBytes"], 150)
        self.assertTrue(self.scratch_path(run_id).exists())
        state_after = (
            run_registry.run_directory(self.registry_root, run_id) / "state.json"
        ).read_bytes()
        self.assertEqual(state_after, state_before)

    def test_a_run_whose_scratch_is_already_gone_is_marked_without_error(self) -> None:
        run_id = self.make_run(scratch=False)
        manifest_path = run_registry.run_directory(self.registry_root, run_id) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scratchPath"] = str(run_scratch.expected_path(self.registry_root, run_id))
        run_registry.write_json_atomic(manifest_path, manifest)

        dry = self.reclaim(dry_run=True)
        self.assertEqual(dry["skipped"][0]["reason"], "nothing_to_reclaim")
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

        result = self.reclaim()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["skipped"][0]["reason"], "nothing_to_reclaim")
        self.assertEqual(self.state_of(run_id)[retention.SCRATCH_RECLAIMED_BYTES_KEY], 0)

    def test_mismatched_recorded_path_refuses_and_other_runs_still_reclaim(self) -> None:
        bad = self.make_run(record_pointers=False)
        good = self.make_run()
        outside = self.base / "outside-canary"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
        manifest_path = run_registry.run_directory(self.registry_root, bad) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scratchPath"] = str(outside)
        run_registry.write_json_atomic(manifest_path, manifest)

        result = self.reclaim()

        self.assertFalse(result["ok"])
        self.assertEqual(result["exitCode"], run_registry.RUN_PRUNE_ERROR_EXIT_CODE)
        self.assertEqual([error["runId"] for error in result["errors"]], [bad])
        self.assertEqual(result["errors"][0]["code"], "scratch_reclaim_failed")
        self.assertIn("reclaim", result["errors"][0]["message"])
        self.assertTrue(self.scratch_path(bad).exists())
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(bad))
        self.assertFalse(self.scratch_path(good).exists())
        self.assertIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(good))

    def test_a_foreign_owned_entry_inside_scratch_refuses_removal(self) -> None:
        run_id = self.make_run()
        scratch = self.scratch_path(run_id)

        # Directory checks are stubbed so the per-entry ownership walk is the
        # guard under test: every entry now looks owned by someone else.
        with (
            mock.patch.object(run_scratch, "_require_owned_directory"),
            mock.patch.object(run_scratch.os, "geteuid", return_value=os.geteuid() + 1),
        ):
            result = self.reclaim()

        self.assertFalse(result["ok"])
        self.assertIn("foreign owner", result["errors"][0]["message"])
        self.assertTrue((scratch / "artifact.bin").exists())
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

    def test_a_symlink_inside_scratch_is_removed_but_never_followed(self) -> None:
        run_id = self.make_run()
        outside = self.base / "outside-target"
        outside.mkdir()
        (outside / "precious.txt").write_bytes(b"p" * 500)
        link = self.scratch_path(run_id) / "escape"
        link.symlink_to(outside, target_is_directory=True)

        dry = self.reclaim(dry_run=True)
        self.assertEqual(dry["planned"][0]["scratchBytes"], 100)

        result = self.reclaim()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["totalBytes"], 100)
        self.assertFalse(self.scratch_path(run_id).exists())
        self.assertEqual((outside / "precious.txt").read_bytes(), b"p" * 500)

    def test_tree_bytes_counts_regular_files_and_no_symlink_targets(self) -> None:
        tree = self.base / "tree"
        (tree / "nested").mkdir(parents=True)
        (tree / "a.bin").write_bytes(b"1" * 10)
        (tree / "nested" / "b.bin").write_bytes(b"2" * 20)
        other = self.base / "other"
        other.mkdir()
        (other / "big.bin").write_bytes(b"3" * 1000)
        (tree / "link-dir").symlink_to(other, target_is_directory=True)
        (tree / "link-file").symlink_to(other / "big.bin")

        self.assertEqual(run_scratch.tree_bytes(tree), 30)
        self.assertEqual(run_scratch.tree_bytes(self.base / "missing"), 0)

    def test_negative_age_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.reclaim(older_than_days=-1)

    def test_prune_still_removes_scratch_that_reclaim_left_behind(self) -> None:
        run_id = self.make_run(record_pointers=True, age_days=40)
        run_path = run_registry.run_directory(self.registry_root, run_id)

        result = run_registry.prune_runs(self.registry_root, older_than_days=30, now=self.now)

        self.assertTrue(result["ok"], result)
        self.assertFalse(run_path.exists())
        self.assertFalse(self.scratch_path(run_id).exists())


class RetentionPassScratchTests(ScratchReclaimTestCase):
    def config(self, **retention_settings) -> dict:
        return {"tracking": {"retention": {"enabled": True, "rawLogDays": 7, **retention_settings}}}

    def test_defaults_to_three_days(self) -> None:
        self.assertEqual(retention.DEFAULT_SCRATCH_RETENTION_DAYS, 3)
        self.assertEqual(retention.scratch_retention_days({}), 3)
        self.assertEqual(retention.scratch_retention_days(self.config(scratchDays=9)), 9)
        self.assertEqual(retention.scratch_retention_days(self.config(scratchDays=0)), 0)
        self.assertEqual(retention.scratch_retention_days(self.config(scratchDays=True)), 3)
        self.assertEqual(retention.scratch_retention_days(self.config(scratchDays=-2)), 3)

    def test_the_retention_pass_reclaims_scratch_on_its_own_age(self) -> None:
        old = self.make_run(age_days=4)
        recent = self.make_run(age_days=1)

        result = retention.run_retention_pass(self.registry_root, self.config(), now=self.now)

        self.assertEqual(result["scratchReclaimed"], 1)
        self.assertFalse(self.scratch_path(old).exists())
        self.assertTrue(self.scratch_path(recent).exists())
        self.assertIn(old, run_registry.load_index(self.registry_root)["runs"])

    def test_scratch_days_setting_moves_the_cutoff(self) -> None:
        run_id = self.make_run(age_days=4)

        result = retention.run_retention_pass(
            self.registry_root, self.config(scratchDays=30), now=self.now
        )

        self.assertEqual(result["scratchReclaimed"], 0)
        self.assertTrue(self.scratch_path(run_id).exists())

    def test_disabled_retention_reclaims_nothing(self) -> None:
        run_id = self.make_run(age_days=40)

        result = retention.run_retention_pass(
            self.registry_root, self.config(enabled=False), now=self.now
        )

        self.assertEqual(result["scratchReclaimed"], 0)
        self.assertTrue(self.scratch_path(run_id).exists())

    def test_a_failing_reclaim_never_fails_the_pass(self) -> None:
        self.make_run(age_days=40)

        with mock.patch.object(retention, "_reclaim_scratch_locked", side_effect=OSError("boom")):
            result = retention.run_retention_pass(self.registry_root, self.config(), now=self.now)

        self.assertEqual(result["scratchReclaimed"], 0)
        self.assertEqual(result["scanned"], 1)

    def test_a_pass_cut_short_by_its_budget_does_not_start_the_cadence_window(self) -> None:
        run_id = self.make_run(age_days=40)

        with mock.patch.object(retention, "SCRATCH_RECLAIM_BUDGET_SECONDS", 0.0):
            first = retention.run_retention_pass(self.registry_root, self.config(), now=self.now)

        self.assertEqual(first["scratchReclaimed"], 0)
        self.assertTrue(self.scratch_path(run_id).exists())

        second = retention.run_retention_pass(self.registry_root, self.config(), now=self.now)

        self.assertEqual(second["scratchReclaimed"], 1)
        self.assertFalse(self.scratch_path(run_id).exists())

    def test_a_completed_pass_starts_the_cadence_window(self) -> None:
        run_id = self.make_run(age_days=1)
        retention.run_retention_pass(self.registry_root, self.config(), now=self.now)
        # Age the run past the cutoff without changing the run count.
        state_path = run_registry.run_directory(self.registry_root, run_id) / "state.json"
        state = self.state_of(run_id)
        state["finishedAt"] = state["lastActivityAt"] = self.stamp(40)
        run_registry.write_json_atomic(state_path, state)

        throttled = retention.run_retention_pass(self.registry_root, self.config(), now=self.now)

        self.assertEqual(throttled["scratchReclaimed"], 0)
        self.assertTrue(self.scratch_path(run_id).exists())


class RunsReclaimCommandTests(ScratchReclaimTestCase):
    def test_dry_run_json_lists_sizes_and_keeps_scratch(self) -> None:
        run_id = self.make_run(size=2 * 1024 * 1024)

        code, out, err = self.run_cli("--json", "runs", "reclaim", "--dry-run")

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["schema"], "delegate.runs-reclaim.v1")
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["olderThanDays"], 3)
        self.assertEqual(payload["planned"][0]["runId"], run_id)
        self.assertEqual(payload["planned"][0]["scratchBytes"], 2 * 1024 * 1024)
        self.assertEqual(payload["totalBytes"], 2 * 1024 * 1024)
        self.assertTrue(self.scratch_path(run_id).exists())
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

    def test_dry_run_text_shows_the_size_in_mib(self) -> None:
        self.make_run(size=2 * 1024 * 1024)

        code, out, err = self.run_cli("runs", "reclaim", "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        self.assertIn("planned: 1", out)
        self.assertIn("2.0 MiB", out)

    def test_reclaim_now_removes_scratch_and_older_than_overrides_the_default(self) -> None:
        run_id = self.make_run(age_days=1)

        code, out, err = self.run_cli("--json", "runs", "reclaim")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["reclaimed"], [])
        self.assertTrue(self.scratch_path(run_id).exists())

        code, out, err = self.run_cli("--json", "runs", "reclaim", "--older-than", "0")

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([item["runId"] for item in payload["reclaimed"]], [run_id])
        self.assertFalse(self.scratch_path(run_id).exists())
        self.assertIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

    def test_reclaim_reports_failures_with_a_nonzero_exit(self) -> None:
        run_id = self.make_run(record_pointers=False)
        manifest_path = run_registry.run_directory(self.registry_root, run_id) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scratchPath"] = str(self.base / "elsewhere")
        run_registry.write_json_atomic(manifest_path, manifest)

        code, out, _err = self.run_cli("--json", "runs", "reclaim")

        self.assertEqual(code, run_registry.RUN_PRUNE_ERROR_EXIT_CODE)
        self.assertFalse(json.loads(out)["ok"])
        self.assertTrue(self.scratch_path(run_id).exists())

    def test_a_workspace_without_a_registry_reclaims_nothing(self) -> None:
        bare = self.base / "bare"
        bare.mkdir()
        stdout, stderr = io.StringIO(), io.StringIO()

        code = cli.main(
            ["--cwd", str(bare), "--json", "runs", "reclaim"], stdout=stdout, stderr=stderr
        )

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["planned"], [])

    def test_negative_older_than_is_a_usage_error(self) -> None:
        code, out, _err = self.run_cli("--json", "runs", "reclaim", "--older-than", "-1")

        self.assertNotEqual(code, 0)
        self.assertFalse(json.loads(out)["ok"])

    def test_an_ordinary_read_command_reclaims_old_scratch_through_the_retention_pass(self) -> None:
        old = self.make_run(age_days=10)
        recent = self.make_run(age_days=1)

        code, _out, err = self.run_cli("--json", "runs")

        self.assertEqual(code, 0, err)
        self.assertFalse(self.scratch_path(old).exists())
        self.assertTrue(self.scratch_path(recent).exists())

    def test_the_snapshot_of_a_reclaimed_run_says_so(self) -> None:
        run_id = self.make_run(age_days=10)
        self.reclaim()

        code, out, err = self.run_cli("--json", "snapshot", run_id)

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn(retention.SCRATCH_RECLAIMED_AT_KEY, payload)
        self.assertIn(retention.SCRATCH_RECLAIMED_BYTES_KEY, payload)

    def write_persisted_snapshot(self, run_id: str) -> None:
        """A finished run leaves snapshot.json behind, written before any reclaim."""
        run_path = run_registry.run_directory(self.registry_root, run_id)
        state = self.state_of(run_id)
        run_registry.write_json_atomic(
            run_path / "snapshot.json",
            {
                "schema": run_registry.SNAPSHOT_SCHEMA,
                "ok": True,
                "alias": state["alias"],
                "runId": run_id,
                "harness": "codex",
                "status": "succeeded",
                "startedAt": state["finishedAt"],
                "assistantText": "done",
                "assistantTextChars": 4,
                "assistantTextTruncated": False,
                "recentEvents": [],
                "warnings": [],
            },
        )

    def test_a_persisted_snapshot_still_shows_the_reclaim_markers_in_json(self) -> None:
        run_id = self.make_run(age_days=10, size=64)
        self.write_persisted_snapshot(run_id)
        self.reclaim()

        code, out, err = self.run_cli("--json", "snapshot", run_id)

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["assistantText"], "done")
        self.assertEqual(payload[retention.SCRATCH_RECLAIMED_BYTES_KEY], 64)
        self.assertEqual(
            payload[retention.SCRATCH_RECLAIMED_AT_KEY],
            self.state_of(run_id)[retention.SCRATCH_RECLAIMED_AT_KEY],
        )

    def test_a_persisted_snapshot_still_shows_the_reclaim_markers_in_text(self) -> None:
        run_id = self.make_run(age_days=10, size=3 * 1024 * 1024)
        self.write_persisted_snapshot(run_id)
        self.reclaim()

        code, out, err = self.run_cli("snapshot", run_id)

        self.assertEqual(code, 0, err)
        stamp = self.state_of(run_id)[retention.SCRATCH_RECLAIMED_AT_KEY]
        self.assertIn(f"scratch reclaimed: {stamp} (3.0 MiB)", out)

    def test_an_unreclaimed_run_shows_no_reclaim_line(self) -> None:
        run_id = self.make_run(age_days=1)
        self.write_persisted_snapshot(run_id)

        code, out, err = self.run_cli("snapshot", run_id)

        self.assertEqual(code, 0, err)
        self.assertNotIn("scratch reclaimed", out)
        code, out, err = self.run_cli("--json", "snapshot", run_id)
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, json.loads(out))


class TreeClock:
    """Reads as the number of files removed from ``tree`` so far.

    A budget of N seconds then means "stop after N files are gone", whatever
    the machine's speed, and it is measured on the real tree.
    """

    def __init__(self, tree: Path, total: int) -> None:
        self.tree = tree
        self.total = total

    def __call__(self) -> float:
        remaining = sum(len(files) for _root, _dirs, files in os.walk(self.tree))
        return float(self.total - remaining)


class Ticker:
    """A clock that advances one second every time it is read."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return float(self.reads)


class BudgetedRemovalTests(ScratchReclaimTestCase):
    FILE_SIZE = 10
    BUDGET = 20

    def big_run(self, **kwargs) -> tuple[str, Path, int]:
        """A run whose scratch holds 61 same-sized files in 7 directories."""
        kwargs.setdefault("size", self.FILE_SIZE)
        run_id = self.make_run(**kwargs)
        scratch = self.scratch_path(run_id)
        for index in range(6):
            folder = scratch / f"dir{index}"
            folder.mkdir()
            for leaf in range(10):
                (folder / f"f{leaf}.bin").write_bytes(b"q" * self.FILE_SIZE)
        return run_id, scratch, 61

    def count_files(self, tree: Path) -> int:
        return sum(len(files) for _root, _dirs, files in os.walk(tree))

    def config(self) -> dict:
        return {"tracking": {"retention": {"enabled": True, "rawLogDays": 7}}}

    def reclaim_ambiently(self, clock, budget: float | None = None) -> dict:
        with mock.patch.object(retention, "_monotonic", clock):
            return retention._reclaim_scratch_locked(
                self.registry_root,
                older_than_days=3,
                dry_run=False,
                now=self.now,
                budget_seconds=float(self.BUDGET) if budget is None else budget,
            )

    def test_listing_a_wide_directory_stops_at_the_deadline(self) -> None:
        folder = self.base / "wide"
        folder.mkdir()
        for index in range(30):
            (folder / f"f{index}").write_bytes(b"x")
        fd = os.open(folder, os.O_RDONLY)
        self.addCleanup(os.close, fd)

        with self.assertRaises(run_scratch.ScratchBudgetExceeded):
            run_scratch._list_directory(fd, run_scratch.Deadline(5, clock=Ticker()))

        self.assertEqual(len(run_scratch._list_directory(fd, None)), 30)

    def test_a_spent_budget_starts_no_further_run(self) -> None:
        first = self.make_run(size=self.FILE_SIZE)
        second = self.make_run(size=self.FILE_SIZE)
        bucket = self.scratch_path(first).parent

        # One file is the whole budget, and the first run's removal spends it.
        result = self.reclaim_ambiently(TreeClock(bucket, 2), budget=1.0)

        self.assertTrue(result["budgetExhausted"])
        self.assertEqual(len(result["planned"]), 1)
        self.assertEqual(len(result["reclaimed"]), 1)
        self.assertEqual(result["skipped"], [])
        left = [run_id for run_id in (first, second) if self.scratch_path(run_id).exists()]
        self.assertEqual(len(left), 1)
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(left[0]))

    def test_removal_stops_at_the_deadline_and_the_next_call_finishes(self) -> None:
        _run_id, scratch, total = self.big_run()
        clock = TreeClock(scratch, total)

        progress = run_scratch.remove_targets(
            [scratch], deadline=run_scratch.Deadline(self.BUDGET, clock=clock)
        )

        self.assertFalse(progress.complete)
        self.assertEqual(progress.freed_bytes, self.BUDGET * self.FILE_SIZE)
        self.assertEqual(self.count_files(scratch), total - self.BUDGET)

        finished = run_scratch.remove_targets([scratch])

        self.assertTrue(finished.complete)
        self.assertEqual(finished.freed_bytes, (total - self.BUDGET) * self.FILE_SIZE)
        self.assertFalse(scratch.exists())

    def test_the_ownership_scan_stops_at_the_deadline(self) -> None:
        _run_id, scratch, _total = self.big_run()
        clock = Ticker()

        # 67 entries to inspect against a budget of 10 reads.
        with self.assertRaises(run_scratch.ScratchBudgetExceeded):
            run_scratch._scan_for_foreign_owner(scratch, run_scratch.Deadline(10, clock=clock))

        self.assertLessEqual(clock.reads, 10 + 2)
        run_scratch._scan_for_foreign_owner(scratch, None)

    def test_a_tree_the_checks_alone_outlast_the_budget_is_left_untouched(self) -> None:
        _run_id, scratch, total = self.big_run()

        progress = run_scratch.remove_targets(
            [scratch], deadline=run_scratch.Deadline(10, clock=Ticker())
        )

        self.assertFalse(progress.complete)
        self.assertEqual(progress.freed_bytes, 0)
        self.assertEqual(self.count_files(scratch), total)

    def test_later_targets_wait_when_the_first_uses_up_the_budget(self) -> None:
        run_id, scratch, total = self.big_run(sidecar=True)
        sidecar = scratch.parent / f"{run_id}{run_scratch.SIDECAR_SEPARATOR}home"
        targets = run_scratch.owned_targets(self.registry_root, run_id)
        self.assertEqual(targets, [scratch, sidecar])
        clock = TreeClock(scratch, total)

        progress = run_scratch.remove_targets(
            targets, deadline=run_scratch.Deadline(self.BUDGET, clock=clock)
        )

        self.assertFalse(progress.complete)
        self.assertTrue((sidecar / "engine.cfg").exists())

    def test_a_directory_swapped_for_a_symlink_mid_removal_is_refused_not_followed(self) -> None:
        run_id = self.make_run()
        scratch = self.scratch_path(run_id)
        (scratch / "sub").mkdir()
        (scratch / "sub" / "inner.bin").write_bytes(b"i")
        outside = self.base / "outside-target"
        outside.mkdir()
        (outside / "precious.txt").write_bytes(b"p" * 50)
        real_list = run_scratch._list_directory
        swapped: list[bool] = []

        def swap_after_first_listing(fd, deadline):
            items = real_list(fd, deadline)
            if not swapped:
                swapped.append(True)
                (scratch / "sub").rename(scratch / "sub-moved")
                (scratch / "sub").symlink_to(outside, target_is_directory=True)
            return items

        with (
            mock.patch.object(run_scratch, "_list_directory", swap_after_first_listing),
            self.assertRaises(OSError),
        ):
            run_scratch.remove_targets([scratch])

        self.assertTrue(swapped)
        self.assertEqual((outside / "precious.txt").read_bytes(), b"p" * 50)
        self.assertTrue(scratch.exists())

    def test_a_cut_short_reclaim_leaves_the_run_unmarked_and_counts_what_it_freed(self) -> None:
        run_id, scratch, total = self.big_run(sidecar=True)
        sidecar = scratch.parent / f"{run_id}{run_scratch.SIDECAR_SEPARATOR}home"

        result = self.reclaim_ambiently(TreeClock(scratch, total))

        self.assertTrue(result["budgetExhausted"])
        self.assertEqual(result["reclaimed"], [])
        self.assertEqual(
            [(item["runId"], item["reason"]) for item in result["skipped"]],
            [(run_id, "budget_exhausted")],
        )
        self.assertEqual(result["skipped"][0]["freedBytes"], self.BUDGET * self.FILE_SIZE)
        self.assertEqual(result["totalBytes"], self.BUDGET * self.FILE_SIZE)
        self.assertEqual(self.count_files(scratch), total - self.BUDGET)
        self.assertTrue(sidecar.exists())
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

    def test_the_pass_after_a_cut_short_one_finishes_the_tree_and_marks_it(self) -> None:
        run_id, scratch, total = self.big_run(age_days=40, sidecar=True)
        sidecar = scratch.parent / f"{run_id}{run_scratch.SIDECAR_SEPARATOR}home"
        with mock.patch.object(retention, "_monotonic", TreeClock(scratch, total)):
            first = retention.run_retention_pass(self.registry_root, self.config(), now=self.now)

        self.assertEqual(first["scratchReclaimed"], 0)
        self.assertEqual(self.count_files(scratch), total - self.BUDGET)
        self.assertNotIn(retention.SCRATCH_RECLAIMED_AT_KEY, self.state_of(run_id))

        # The cut-short pass did not start the cadence window, so this runs now.
        second = retention.run_retention_pass(self.registry_root, self.config(), now=self.now)

        self.assertEqual(second["scratchReclaimed"], 1)
        self.assertFalse(scratch.exists())
        self.assertFalse(sidecar.exists())
        state = self.state_of(run_id)
        self.assertIn(retention.SCRATCH_RECLAIMED_AT_KEY, state)
        # The marker's byte count is what the finishing pass freed: the rest of
        # the scratch and the sidecar.
        self.assertEqual(
            state[retention.SCRATCH_RECLAIMED_BYTES_KEY],
            (total - self.BUDGET) * self.FILE_SIZE + self.FILE_SIZE,
        )

    def test_the_explicit_command_is_not_budgeted(self) -> None:
        _run_id, scratch, _total = self.big_run()

        with mock.patch.object(retention, "_monotonic", Ticker()):
            result = self.reclaim()

        self.assertFalse(result["budgetExhausted"])
        self.assertEqual(len(result["reclaimed"]), 1)
        self.assertFalse(scratch.exists())


if __name__ == "__main__":
    unittest.main()
