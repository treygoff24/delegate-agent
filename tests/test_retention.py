import importlib.util
import json
import os
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
RETENTION_PATH = ROOT / "src" / "delegate_agent" / "retention.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"
CLI_PATH = ROOT / "src" / "delegate_agent" / "cli.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import run_scratch  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.retention = load_module(RETENTION_PATH, "delegate_retention_test")
        self.registry = load_module(REGISTRY_PATH, "delegate_registry_retention_test")
        self.delegate = load_module(CLI_PATH, "delegate_cli_retention_test")
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.registry_root = self.registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        self.config = {
            "tracking": {
                "retention": {
                    "enabled": True,
                    "rawLogDays": 7,
                }
            }
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_raw_log_retention_days_rejects_bool(self):
        config = {"tracking": {"retention": {"rawLogDays": True}}}
        self.assertEqual(
            self.retention.raw_log_retention_days(config),
            self.retention.DEFAULT_RAW_LOG_RETENTION_DAYS,
        )

    def test_retention_enabled_ignores_non_bool_values(self):
        config = {"tracking": {"retention": {"enabled": "false"}}}
        self.assertTrue(self.retention.retention_enabled(config))

    def test_retention_archiving_does_not_block_registry_or_concurrent_pass(self):
        self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
        archive_started = threading.Event()
        release_archive = threading.Event()
        outcome = {}

        def blocking_archive(*_args, **_kwargs):
            archive_started.set()
            release_archive.wait(timeout=2)
            return False

        def run_retention():
            outcome["result"] = self.retention.run_retention_pass(
                self.registry_root,
                self.config,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

        with mock.patch.object(
            self.retention,
            "archive_run_raw_logs",
            side_effect=blocking_archive,
        ):
            thread = threading.Thread(target=run_retention)
            thread.start()
            self.assertTrue(archive_started.wait(timeout=1))
            started = time.monotonic()
            try:
                with self.registry.registry_lock(self.registry_root, timeout_seconds=0.05):
                    pass
                concurrent = self.retention.run_retention_pass(
                    self.registry_root,
                    self.config,
                )
            finally:
                release_archive.set()
                thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["result"]["scanned"], 1)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(concurrent, {"scanned": 0, "archived": 0, "skipped": 0})

    def test_run_prune_waits_for_active_retention(self):
        run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        outcome = {}

        def prune():
            outcome["result"] = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

        with self.registry.file_lock(self.registry.retention_lock_path(self.registry_root)):
            thread = threading.Thread(target=prune)
            thread.start()
            time.sleep(0.05)
            self.assertTrue(thread.is_alive())
            self.assertTrue(run_path.exists())
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(outcome["result"]["ok"])
        self.assertFalse(run_path.exists())

    def test_run_prune_refuses_tampered_recorded_scratch_and_preserves_pointer(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
            scratch = run_scratch.allocate(self.registry_root, run_id)
            (scratch / "artifact.txt").write_text("remove\n", encoding="utf-8")
            outside = self.workspace / "outside-canary"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
            run_path = self.registry.run_directory(self.registry_root, run_id)
            self.registry.write_json_atomic(
                run_path / "manifest.json",
                {
                    "runId": run_id,
                    "scratchPath": str(outside),
                    "scratchPermissions": {"writableRoots": [str(outside)]},
                },
            )

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"][0]["code"], "record_remove_failed")
            self.assertTrue(scratch.exists())
            self.assertTrue(run_path.exists())
            self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep\n")

    def test_run_prune_removes_exact_recorded_owned_scratch(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
            scratch = run_scratch.allocate(self.registry_root, run_id)
            (scratch / "artifact.txt").write_text("remove\n", encoding="utf-8")
            run_path = self.registry.run_directory(self.registry_root, run_id)
            self.registry.write_json_atomic(
                run_path / "manifest.json", {"runId": run_id, "scratchPath": str(scratch)}
            )

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            self.assertFalse(scratch.exists())
            self.assertFalse(run_path.exists())

    def test_run_prune_refuses_corrupt_manifest_with_external_scratch(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
            scratch = run_scratch.allocate(self.registry_root, run_id)
            (scratch / "forensic.txt").write_text("retain\n", encoding="utf-8")
            run_path = self.registry.run_directory(self.registry_root, run_id)
            manifest_path = run_path / "manifest.json"
            corrupt = b"{not-json\n"
            manifest_path.write_bytes(corrupt)

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertFalse(result["ok"])
            self.assertIn("unreadable manifest", result["errors"][0]["message"])
            self.assertEqual(manifest_path.read_bytes(), corrupt)
            self.assertEqual((scratch / "forensic.txt").read_text(), "retain\n")

    def test_run_prune_cleans_owned_external_scratch_when_legacy_manifest_is_absent(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
            scratch = run_scratch.allocate(self.registry_root, run_id)
            (scratch / "orphan.txt").write_text("remove\n", encoding="utf-8")
            run_path = self.registry.run_directory(self.registry_root, run_id)
            (run_path / "manifest.json").unlink()
            self.assertFalse((run_path / "manifest.json").exists())

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            self.assertFalse(scratch.exists())
            self.assertFalse(run_path.exists())

    def test_moved_registry_with_corrupt_manifest_preserves_forensic_record(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
            scratch = run_scratch.allocate(self.registry_root, run_id)
            run_path = self.registry.run_directory(self.registry_root, run_id)
            manifest_path = run_path / "manifest.json"
            corrupt = b"{forensic-corruption\n"
            manifest_path.write_bytes(corrupt)
            moved_registry = self.workspace / "moved-registry"
            self.registry_root.rename(moved_registry)
            self.registry_root = moved_registry
            moved_run_path = self.registry.run_directory(self.registry_root, run_id)

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertFalse(result["ok"])
            self.assertEqual((moved_run_path / "manifest.json").read_bytes(), corrupt)
            self.assertTrue(scratch.exists())

    def test_changed_home_with_corrupt_manifest_preserves_forensic_record(self):
        with (
            tempfile.TemporaryDirectory() as first_home,
            tempfile.TemporaryDirectory() as second_home,
        ):
            with mock.patch.dict(os.environ, {"HOME": first_home}):
                run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
                scratch = run_scratch.allocate(self.registry_root, run_id)
                run_path = self.registry.run_directory(self.registry_root, run_id)
                corrupt = b"{forensic-corruption\n"
                (run_path / "manifest.json").write_bytes(corrupt)

            with mock.patch.dict(os.environ, {"HOME": second_home}):
                result = self.registry.prune_runs(
                    self.registry_root,
                    older_than_days=0,
                    now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
                )

            self.assertFalse(result["ok"])
            self.assertEqual((run_path / "manifest.json").read_bytes(), corrupt)
            self.assertTrue(scratch.exists())

    def test_run_prune_refuses_when_registry_move_changes_derived_scratch(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
            scratch = run_scratch.allocate(self.registry_root, run_id)
            run_path = self.registry.run_directory(self.registry_root, run_id)
            self.registry.write_json_atomic(
                run_path / "manifest.json", {"runId": run_id, "scratchPath": str(scratch)}
            )
            moved_workspace = self.workspace / "moved"
            self.registry_root.rename(moved_workspace)
            self.registry_root = moved_workspace

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertFalse(result["ok"])
            self.assertIn("does not match", result["errors"][0]["message"])
            self.assertTrue(scratch.exists())
            self.assertTrue(self.registry.run_directory(self.registry_root, run_id).exists())

    def test_run_prune_refuses_when_home_change_changes_derived_scratch(self):
        with (
            tempfile.TemporaryDirectory() as first_home,
            tempfile.TemporaryDirectory() as second_home,
        ):
            with mock.patch.dict(os.environ, {"HOME": first_home}):
                run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
                scratch = run_scratch.allocate(self.registry_root, run_id)
                run_path = self.registry.run_directory(self.registry_root, run_id)
                self.registry.write_json_atomic(
                    run_path / "manifest.json",
                    {"runId": run_id, "scratchPath": str(scratch)},
                )

            with mock.patch.dict(os.environ, {"HOME": second_home}):
                result = self.registry.prune_runs(
                    self.registry_root,
                    older_than_days=0,
                    now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
                )

            self.assertFalse(result["ok"])
            self.assertIn("does not match", result["errors"][0]["message"])
            self.assertTrue(scratch.exists())
            self.assertTrue(run_path.exists())

    def test_run_prune_preserves_legacy_run_local_scratch(self):
        run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        legacy_scratch = run_path / "scratch"
        legacy_scratch.mkdir()
        (legacy_scratch / "old.txt").write_text("legacy\n", encoding="utf-8")

        result = self.registry.prune_runs(
            self.registry_root,
            older_than_days=0,
            now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        )

        self.assertTrue(result["ok"])
        self.assertFalse(run_path.exists())

    def test_run_prune_keeps_ordinary_legacy_no_manifest_no_external_behavior(self):
        run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "manifest.json").unlink()
        self.assertFalse(run_scratch.expected_path(self.registry_root, run_id).exists())

        result = self.registry.prune_runs(
            self.registry_root,
            older_than_days=0,
            now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        )

        self.assertTrue(result["ok"])
        self.assertFalse(run_path.exists())

    def test_run_prune_refuses_symlinked_derived_scratch_and_keeps_record(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
            scratch = run_scratch.allocate(self.registry_root, run_id)
            scratch.rmdir()
            outside = self.workspace / "outside-canary"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
            scratch.symlink_to(outside, target_is_directory=True)
            run_path = self.registry.run_directory(self.registry_root, run_id)
            self.registry.write_json_atomic(
                run_path / "manifest.json", {"runId": run_id, "scratchPath": str(scratch)}
            )

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"][0]["code"], "record_remove_failed")
            self.assertTrue(self.registry.run_directory(self.registry_root, run_id).exists())
            self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep\n")

    def test_run_prune_keeps_active_run_scratch(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run(
                status="running", finished_at="2000-01-01T00:00:00Z", pid=os.getpid()
            )
            scratch = run_scratch.allocate(self.registry_root, run_id)
            (scratch / "active.txt").write_text("keep\n", encoding="utf-8")

            result = self.registry.prune_runs(
                self.registry_root,
                older_than_days=0,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

            self.assertEqual(result["skipped"][0]["reason"], "running")
            self.assertEqual((scratch / "active.txt").read_text(encoding="utf-8"), "keep\n")

    def write_completed_run(
        self,
        *,
        status: str = "succeeded",
        finished_at: str | None = None,
        pid: int | None = None,
        with_logs: bool = True,
    ) -> tuple[str, str]:
        run_id, alias = self.registry.register_run(self.registry_root, harness="cursor")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        finished = finished_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {
            "schema": "delegate.state.v1",
            "runId": run_id,
            "alias": alias,
            "status": status,
            "finishedAt": finished,
            "lastActivityAt": finished,
            "ok": status == "succeeded",
            "assistantText": "done",
            "recentEvents": [],
        }
        if pid is not None:
            state["pid"] = pid
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.registry.write_json_atomic(
            run_path / "manifest.json",
            {
                "schema": "delegate.manifest.v1",
                "runId": run_id,
                "alias": alias,
                "harness": "cursor",
                "startedAt": finished,
            },
        )
        if with_logs:
            (run_path / "stdout.log").write_text("stdout-data\n", encoding="utf-8")
            (run_path / "stderr.log").write_text("stderr-data\n", encoding="utf-8")
            (run_path / "events.jsonl").write_text('{"kind":"text"}\n', encoding="utf-8")
        return run_id, alias

    def test_running_run_is_not_archived(self):
        run_id, _alias = self.write_completed_run(status="running", pid=__import__("os").getpid())
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        result = self.retention.run_retention_pass(self.registry_root, self.config)
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_stale_run_is_not_archived(self):
        run_id, _alias = self.write_completed_run(status="running", pid=999999999)
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        result = self.retention.run_retention_pass(self.registry_root, self.config)
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_recent_completed_run_is_not_archived(self):
        run_id, _alias = self.write_completed_run()
        result = self.retention.run_retention_pass(self.registry_root, self.config)
        self.assertEqual(result["archived"], 0)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        self.assertTrue((run_path / "stdout.log").exists())
        self.assertFalse(self.retention.archive_path(self.registry_root, run_id).exists())

    def test_completed_generation_skips_an_immediate_repeat_pass(self):
        run_id, alias = self.registry.register_run(self.registry_root, harness="cursor")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        record = {
            "schema": self.registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": "succeeded",
            "finishedAt": "2000-01-01T00:00:00Z",
            "lastActivityAt": "2000-01-01T00:00:00Z",
        }
        self.registry.write_json_atomic(
            run_path / self.registry.MANIFEST_FILE,
            {"schema": self.registry.MANIFEST_SCHEMA, "runId": run_id, "alias": alias},
        )
        for name in self.retention.ARCHIVE_MEMBER_NAMES:
            (run_path / name).write_text("old output\n", encoding="utf-8")
        with self.registry.registry_lock(self.registry_root):
            self.registry.publish_terminal_record_locked(self.registry_root, run_id, record)

        config = {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}}
        first = self.retention.run_retention_pass(self.registry_root, config)
        second = self.retention.run_retention_pass(self.registry_root, config)

        self.assertEqual(first["scanned"], 1)
        self.assertEqual(second, {"scanned": 0, "archived": 0, "skipped": 0})

    def test_retention_retries_a_young_terminal_after_the_time_cadence(self):
        run_id, alias = self.registry.register_run(self.registry_root, harness="cursor")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        finished = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
        record = {
            "schema": self.registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": "succeeded",
            "finishedAt": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lastActivityAt": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.registry.write_json_atomic(
            run_path / self.registry.MANIFEST_FILE,
            {"schema": self.registry.MANIFEST_SCHEMA, "runId": run_id, "alias": alias},
        )
        for name in self.retention.ARCHIVE_MEMBER_NAMES:
            (run_path / name).write_text("old output\n", encoding="utf-8")
        with self.registry.registry_lock(self.registry_root):
            self.registry.publish_terminal_record_locked(self.registry_root, run_id, record)

        config = {"tracking": {"retention": {"enabled": True, "rawLogDays": 1}}}
        first = self.retention.run_retention_pass(self.registry_root, config, now=finished)
        immediate = self.retention.run_retention_pass(
            self.registry_root,
            config,
            now=finished + timedelta(seconds=1),
        )
        later = self.retention.run_retention_pass(
            self.registry_root,
            config,
            now=finished + timedelta(days=2),
        )

        self.assertEqual(first["archived"], 0)
        self.assertEqual(immediate, {"scanned": 0, "archived": 0, "skipped": 0})
        self.assertEqual(later["archived"], 1)

    def test_old_completed_run_archives_raw_logs(self):
        run_id, alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        state["lastActivityAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        zero_day_config = {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}}
        with mock.patch.object(
            self.retention,
            "_verify_archive_members",
            wraps=self.retention._verify_archive_members,
        ) as verify_archive:
            result = self.retention.run_retention_pass(self.registry_root, zero_day_config)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(verify_archive.call_count, 1)
        archive_file = self.retention.archive_path(self.registry_root, run_id)
        self.assertTrue(archive_file.exists())
        self.assertFalse((run_path / "stdout.log").exists())
        self.assertFalse((run_path / "stderr.log").exists())
        self.assertFalse((run_path / "events.jsonl").exists())
        self.assertTrue((run_path / "manifest.json").exists())
        self.assertFalse((run_path / "snapshot.json").exists())
        self.assertTrue((run_path / "state.json").exists())
        index = self.registry.load_index(self.registry_root)
        self.assertEqual(index["aliases"][alias], run_id)
        with tarfile.open(archive_file, "r:gz") as archive:
            names = {member.name for member in archive.getmembers()}
        self.assertEqual(names, {"stdout.log", "stderr.log", "events.jsonl"})

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_archived_raw_logs_are_owner_only(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        state["lastActivityAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)

        result = self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )

        self.assertEqual(result["archived"], 1)
        archive_root = self.retention.archive_dir(self.registry_root)
        archive_file = self.retention.archive_path(self.registry_root, run_id)
        self.assertEqual(stat.S_IMODE(archive_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(archive_file.stat().st_mode), 0o600)

    def test_snapshot_works_after_archival(self):
        run_id, alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        import io

        stdout = io.StringIO()
        code = self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn(alias, output)
        self.assertIn("archived", output.lower())

    def test_run_output_reads_archived_stdout(self):
        run_id, alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        import io

        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--stdout", "--tail", "1"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        self.assertIn("stdout-data", stdout.getvalue())

    def test_retention_disabled_skips_archival(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        disabled = {"tracking": {"retention": {"enabled": False, "rawLogDays": 0}}}
        result = self.retention.run_retention_pass(self.registry_root, disabled)
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_no_delete_commands_exist(self):
        help_text = self.delegate.HELP
        self.assertIn("worktree prune", help_text.lower())
        self.assertNotIn("retention prune", help_text.lower())
        self.assertNotIn("delete", help_text.lower())

    def test_unknown_status_is_not_archived(self):
        run_id, _alias = self.write_completed_run(status="running")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state.pop("status", None)
        state.pop("pid", None)
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        result = self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_retention_pass_skips_corrupt_per_run_json(self):
        state_corrupt_id, _ = self.write_completed_run()
        manifest_corrupt_id, _ = self.write_completed_run()
        state_corrupt_path = self.registry.run_directory(self.registry_root, state_corrupt_id)
        manifest_corrupt_path = self.registry.run_directory(self.registry_root, manifest_corrupt_id)
        (state_corrupt_path / "state.json").write_text("{garbage", encoding="utf-8")
        self.registry.write_json_atomic(
            manifest_corrupt_path / "state.json",
            {"schema": "delegate.state.v1", "status": "succeeded"},
        )
        (manifest_corrupt_path / "manifest.json").write_text("{garbage", encoding="utf-8")

        result = self.retention.run_retention_pass(
            self.registry_root,
            self.config,
            now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        )

        self.assertEqual(result, {"scanned": 2, "archived": 0, "skipped": 2})
        self.assertTrue((state_corrupt_path / "stdout.log").exists())
        self.assertTrue((manifest_corrupt_path / "stdout.log").exists())

    def test_retention_pass_skips_per_run_io_and_lock_timeouts(self):
        for _ in range(3):
            self.write_completed_run(finished_at="2000-01-01T00:00:00Z")

        with mock.patch.object(
            self.retention,
            "archive_run_raw_logs",
            side_effect=[True, FileNotFoundError("removed by older runtime"), TimeoutError("busy")],
        ):
            result = self.retention.run_retention_pass(
                self.registry_root,
                self.config,
                now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            )

        self.assertEqual(result, {"scanned": 3, "archived": 1, "skipped": 2})

    def test_archive_refuses_changed_source_identity(self):
        run_id, _alias = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        members = list(self.retention.ARCHIVE_MEMBER_NAMES)
        original = self.retention._member_identities(run_path, members)
        changed = dict(original)
        first = members[0]
        identity = list(changed[first])
        identity[3] += 1
        changed[first] = tuple(identity)

        with mock.patch.object(
            self.retention,
            "_member_identities",
            side_effect=[original, changed],
        ):
            archived = self.retention.archive_run_raw_logs(self.registry_root, run_id)

        self.assertFalse(archived)
        self.assertTrue((run_path / first).exists())
        self.assertFalse(self.retention.archive_path(self.registry_root, run_id).exists())

    def test_retention_skips_hardlinked_and_oversized_unrelated_records(self):
        for kind in ("hardlinked", "oversized"):
            with self.subTest(kind=kind):
                good_id, _ = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
                bad_id, _ = self.write_completed_run(finished_at="2000-01-01T00:00:00Z")
                bad_state = self.registry.run_directory(self.registry_root, bad_id) / "state.json"
                if kind == "hardlinked":
                    outside = self.workspace / f"outside-{bad_id}.json"
                    outside.write_text("{}", encoding="utf-8")
                    bad_state.unlink()
                    os.link(outside, bad_state)
                else:
                    bad_state.write_bytes(
                        b"x" * (self.registry.private_io.PRIVATE_RECORD_READ_MAX_BYTES + 1)
                    )

                result = self.retention.run_retention_pass(
                    self.registry_root,
                    self.config,
                    now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
                )

                self.assertGreaterEqual(result["archived"], 1)
                self.assertTrue(
                    self.retention.archive_path(self.registry_root, good_id).exists(),
                    "a tampered unrelated run must not interrupt the retention sweep",
                )
                self.assertTrue(
                    (
                        self.registry.run_directory(self.registry_root, bad_id) / "stdout.log"
                    ).exists()
                )

    def test_mark_raw_logs_archived_treats_corrupt_state_as_empty(self):
        run_id, _alias = self.write_completed_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "state.json").write_text("{garbage", encoding="utf-8")

        self.retention._mark_raw_logs_archived(run_path, stdout_bytes=12, stderr_bytes=34)

        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        self.assertIn("rawLogsArchivedAt", state)
        self.assertEqual(state["stdoutBytes"], 12)
        self.assertEqual(state["stderrBytes"], 34)

    def test_crash_recovery_finishes_archival_when_archive_exists(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        archive_file = self.retention.archive_path(self.registry_root, run_id)
        self.retention.archive_dir(self.registry_root).mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_file, "w:gz") as archive:
            for name in self.retention.ARCHIVE_MEMBER_NAMES:
                archive.add(run_path / name, arcname=name)
        self.assertTrue((run_path / "stdout.log").exists())
        result = self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        self.assertEqual(result["archived"], 1)
        self.assertFalse((run_path / "stdout.log").exists())
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        self.assertIn("rawLogsArchivedAt", state)

    def test_effective_log_byte_sizes_after_archival(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        stdout_bytes, stderr_bytes = self.retention.effective_log_byte_sizes(
            self.registry_root,
            run_id,
        )
        self.assertEqual(stdout_bytes, len("stdout-data\n"))
        self.assertEqual(stderr_bytes, len("stderr-data\n"))

    def test_effective_log_byte_sizes_reads_state_without_opening_archive(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        with mock.patch.object(tarfile, "open", side_effect=AssertionError("tar must not open")):
            stdout_bytes, stderr_bytes = self.retention.effective_log_byte_sizes(
                self.registry_root,
                run_id,
            )
        self.assertEqual(stdout_bytes, len("stdout-data\n"))
        self.assertEqual(stderr_bytes, len("stderr-data\n"))

    def test_log_file_byte_size_after_archival(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        self.assertEqual(
            self.retention.log_file_byte_size(self.registry_root, run_id, "stdout.log"),
            len("stdout-data\n"),
        )
        self.assertEqual(
            self.retention.log_file_byte_size(self.registry_root, run_id, "stderr.log"),
            len("stderr-data\n"),
        )

    def test_read_archived_member_rejects_unknown_names(self):
        run_id, _alias = self.write_completed_run()
        archive_file = self.retention.archive_path(self.registry_root, run_id)
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        with tarfile.open(archive_file, "w:gz") as archive:
            archive.add(run_path / "stdout.log", arcname="stdout.log")
        with self.assertRaises(ValueError):
            self.retention.read_archived_member(archive_file, "../evil")

    def test_run_output_tails_archived_stdout_without_loading_entire_file(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        stdout_path = run_path / "stdout.log"
        stdout_path.write_text("".join(f"line-{index}\n" for index in range(200)), encoding="utf-8")
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        output = self.retention.read_log_output(
            self.registry_root,
            run_id,
            "stdout.log",
            tail=2,
            raw=False,
        )
        self.assertEqual(output.content, "line-198\nline-199\n")
        self.assertTrue(output.truncated)

    def test_run_output_archived_tail_not_truncated_when_within_limit(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        stdout_path = run_path / "stdout.log"
        stdout_path.write_text("line-1\nline-2\n", encoding="utf-8")
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        output = self.retention.read_log_output(
            self.registry_root,
            run_id,
            "stdout.log",
            tail=5,
            raw=False,
        )
        self.assertEqual(output.content, "line-1\nline-2\n")
        self.assertFalse(output.truncated)

    def test_retention_preserves_persistent_worktree_dirs(self):
        """Raw-log retention preserves persistent worktree directories outside the registry runs dir."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict("os.environ", {"HOME": fake_home}),
        ):
            run_id, _alias = self.write_completed_run()
            old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            run_path = self.registry.run_directory(self.registry_root, run_id)

            # Simulate a persistent worktree under the fake home, never the
            # operator's real ~/.delegate runtime.
            worktree_root = (
                Path(fake_home) / ".delegate" / "worktrees" / "abc123def456" / "cursor-test"
            )
            worktree_root.mkdir(parents=True, exist_ok=True)
            (worktree_root / "mutated-file.txt").write_text("worktree content\n", encoding="utf-8")

            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            state["finishedAt"] = old
            state["lastActivityAt"] = old
            state["worktreeStatus"] = "present"
            self.registry.write_json_atomic(run_path / "state.json", state)

            zero_day_config = {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}}
            result = self.retention.run_retention_pass(self.registry_root, zero_day_config)

            self.assertEqual(result["archived"], 1)

            self.assertTrue(
                worktree_root.exists(),
                "Worktree directory should be preserved after retention pass",
            )
            self.assertEqual(
                (worktree_root / "mutated-file.txt").read_text(encoding="utf-8"),
                "worktree content\n",
            )

            state_after = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state_after.get("worktreeStatus"), "present")


if __name__ == "__main__":
    unittest.main()
