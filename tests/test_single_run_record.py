from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import harness_events, run_registry, runner


class SingleRunRecordTests(unittest.TestCase):
    def _context(self, root: Path, run_id: str, alias: str) -> runner.RunContext:
        workspace = str(root.parent)
        return runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode="safe",
            model=None,
            source_cwd=workspace,
            execution_cwd=workspace,
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-09-07T00:00:00Z",
        )

    def test_progress_publishes_one_state_record_without_scanning_other_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = run_registry.ensure_registry(Path(temporary), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="codex")
            for number in range(20):
                other_id = f"del_20260901T000000Z_{number:06x}"
                run_registry.register_run(root, harness="codex", run_id=other_id)

            original_iterdir = Path.iterdir

            def reject_history_scan(path: Path):
                if path == run_registry.runs_dir(root):
                    raise AssertionError("progress must not enumerate run history")
                return original_iterdir(path)

            with mock.patch.object(Path, "iterdir", reject_history_scan):
                runner.persist_progress(
                    run_registry.run_directory(root, run_id),
                    self._context(root, run_id, alias),
                    harness_events.StreamAccumulator(harness="codex"),
                    status="running",
                    pid=os.getpid(),
                )

            state = run_registry.load_run_state(root, run_id)
            self.assertIsInstance(state, dict)
            self.assertEqual(state["status"], "running")
            self.assertFalse(
                (run_registry.run_directory(root, run_id) / run_registry.SNAPSHOT_FILE).exists()
            )

    def test_reader_projects_completed_pending_wal_while_mutation_lock_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = run_registry.ensure_registry(Path(temporary), workspace_kind="directory")
            run_id, _alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            run_registry.write_run_state(run_path, {"status": "running"})
            run_registry.write_finalize_wal(
                root,
                run_id,
                status="succeeded",
                record={
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "status": "succeeded",
                    "ok": True,
                    "exitCode": 0,
                    "assistantText": "completed through WAL",
                    "recentEvents": [],
                },
            )

            with run_registry.file_lock(run_registry.registry_lock_path(root), timeout_seconds=1):
                state = run_registry.load_run_state(root, run_id)
                snapshot = run_registry.load_run_snapshot(root, run_id)

            self.assertEqual(state["status"], "succeeded")
            self.assertEqual(snapshot["assistantText"], "completed through WAL")
            self.assertTrue((run_path / run_registry.FINALIZE_WAL_FILE).exists())

    def test_pending_success_wal_cannot_beat_a_fresh_cancel_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = run_registry.ensure_registry(Path(temporary), workspace_kind="directory")
            run_id, _alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            run_registry.write_run_state(
                run_path,
                {"status": "running", "cancelRequested": True, "cancelRequestedAt": "now"},
            )
            run_registry.write_finalize_wal(
                root,
                run_id,
                status="succeeded",
                record={
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "status": "succeeded",
                    "ok": True,
                    "exitCode": 0,
                },
            )

            observed = run_registry.load_run_state(root, run_id)
            self.assertEqual(observed["status"], "cancelled")
            self.assertEqual(observed["failureReason"], "cancelled_by_user")
            with run_registry.registry_lock(root):
                run_registry.reconcile_finalize_wal_locked(root, run_id)

            persisted = run_registry.load_run_state(root, run_id)
            self.assertEqual(persisted["status"], "cancelled")
            self.assertFalse((run_path / run_registry.FINALIZE_WAL_FILE).exists())

    def test_finalizer_never_replaces_an_already_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = run_registry.ensure_registry(Path(temporary), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            original = {
                "status": "succeeded",
                "exitCode": 0,
                "ok": True,
                "resultQuality": "ok",
            }
            run_registry.write_run_state(run_path, original)

            status, extra = runner._persist_final_progress(
                run_path,
                self._context(root, run_id, alias),
                harness_events.StreamAccumulator(harness="codex"),
                status="failed",
                exit_code=1,
                stdout_bytes=0,
                stderr_bytes=0,
                completion_report_written=False,
                extra={"failureReason": "child_failed"},
            )

            self.assertEqual(status, "succeeded")
            self.assertEqual(extra["exitCode"], 0)
            self.assertEqual(run_registry.load_run_state(root, run_id), original)

    def test_reader_ignores_and_locked_reconciler_quarantines_forged_inner_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = run_registry.ensure_registry(Path(temporary), workspace_kind="directory")
            run_id, _alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            run_registry.write_run_state(run_path, {"status": "running"})
            wal_path = run_registry.finalize_wal_path(root, run_id)
            run_registry.write_json_atomic(
                wal_path,
                {
                    "schema": run_registry.FINALIZE_WAL_SCHEMA,
                    "runId": run_id,
                    "status": "succeeded",
                    "record": {
                        "schema": run_registry.STATE_SCHEMA,
                        "runId": "del_20260901T000000Z_ffffff",
                        "status": "succeeded",
                    },
                },
            )

            self.assertEqual(run_registry.load_run_state(root, run_id)["status"], "running")
            with run_registry.registry_lock(root):
                run_registry.reconcile_finalize_wal_locked(root, run_id)

            self.assertFalse(wal_path.exists())
            self.assertTrue(list(run_path.glob(f"{wal_path.name}.corrupt.*")))

    def test_same_size_inner_status_tampering_is_not_a_terminal_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = run_registry.ensure_registry(Path(temporary), workspace_kind="directory")
            run_id, _alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            run_registry.write_run_state(run_path, {"status": "running"})
            wal_path = run_registry.finalize_wal_path(root, run_id)
            payload = {
                "schema": run_registry.FINALIZE_WAL_SCHEMA,
                "runId": run_id,
                "status": "failed",
                "record": {
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "status": "broken",
                },
            }
            self.assertEqual(len("failed"), len("broken"))
            run_registry.write_json_atomic(wal_path, payload)

            self.assertEqual(run_registry.load_run_state(root, run_id)["status"], "running")
            with run_registry.registry_lock(root):
                run_registry.reconcile_finalize_wal_locked(root, run_id)

            self.assertFalse(wal_path.exists())


def test_wal_and_state_share_canonical_size_limit(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(run_registry, "PRIVATE_RECORD_READ_MAX_BYTES", 65536)
    import json

    limit = 65536 - run_registry.FINALIZE_WAL_ENVELOPE_RESERVE_BYTES
    run_id = "del_20260907T000000Z_abcdef"
    record = {
        "schema": run_registry.STATE_SCHEMA,
        "runId": run_id,
        "status": "succeeded",
        "assistantText": "",
    }
    overhead = len((json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    run_path = run_registry.run_directory(tmp_path, run_id)
    run_path.mkdir(parents=True)
    for size in (limit - 1, limit):
        record["assistantText"] = "x" * (size - overhead)
        run_registry.write_run_state(run_path, record)
        run_registry.write_finalize_wal(tmp_path, run_id, status="succeeded", record=record)
        assert (run_path / run_registry.STATE_FILE).stat().st_size == size
    record["assistantText"] += "x"
    with pytest.raises(run_registry.RegistryJsonError, match="canonical record limit"):
        run_registry.write_run_state(run_path, record)
    with pytest.raises(run_registry.RegistryJsonError, match="canonical record limit"):
        run_registry.write_finalize_wal(tmp_path, run_id, status="succeeded", record=record)
