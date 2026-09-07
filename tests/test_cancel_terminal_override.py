from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from delegate_agent import (
    harness_events,
    run_registry,
    runner,
    terminal_states,
    wait_cancel_commands,
)


def _provider_cancel_receipt(reason: str) -> dict[str, object]:
    return {
        "terminalState": terminal_states.PROVIDER_CANCELLED,
        "terminalRecord": {
            "state": terminal_states.PROVIDER_CANCELLED,
            "reason": reason,
            "reasonTruncated": True,
            "reasonChars": 999,
        },
        "failureReason": terminal_states.PROVIDER_CANCELLED,
        "error": terminal_states.PROVIDER_CANCELLED,
        "message": reason,
    }


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _snapshot(registry_root: Path, run_id: str) -> dict[str, object]:
    payload = run_registry.load_run_snapshot(registry_root, run_id)
    assert isinstance(payload, dict)
    return payload


def _assert_operator_cancel_receipt(payload: dict[str, object]) -> None:
    assert payload["status"] == run_registry.STATUS_CANCELLED
    assert payload["failureReason"] == "cancelled_by_user"
    assert payload["terminalState"] == terminal_states.FAILED
    terminal_record = payload["terminalRecord"]
    assert isinstance(terminal_record, dict)
    assert terminal_record["state"] == terminal_states.FAILED
    assert terminal_record["reason"] == "cancelled_by_user"
    assert "reasonTruncated" not in terminal_record
    assert "reasonChars" not in terminal_record
    assert "error" not in payload
    assert "message" not in payload


class CancelTerminalOverrideTests(unittest.TestCase):
    def test_wal_replay_operator_cancel_overrides_provider_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = run_registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, _alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            run_registry.write_json_atomic(
                run_path / run_registry.STATE_FILE,
                {"status": "running", "cancelRequested": True},
            )
            receipt = _provider_cancel_receipt("provider stopped the turn")
            run_registry.write_finalize_wal(
                root,
                run_id,
                status=run_registry.STATUS_CANCELLED,
                record={
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "status": run_registry.STATUS_CANCELLED,
                    "ok": False,
                    "exitCode": 1,
                    **receipt,
                },
            )

            with run_registry.registry_lock(root, timeout_seconds=1):
                run_registry.reconcile_finalize_wal_locked(root, run_id)

            _assert_operator_cancel_receipt(_read_json(run_path / run_registry.STATE_FILE))
            _assert_operator_cancel_receipt(_snapshot(root, run_id))

    def test_wal_replay_without_cancel_request_preserves_provider_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = run_registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, _alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            run_registry.write_json_atomic(
                run_path / run_registry.STATE_FILE, {"status": "running"}
            )
            receipt = _provider_cancel_receipt("provider stopped the turn")
            run_registry.write_finalize_wal(
                root,
                run_id,
                status=run_registry.STATUS_CANCELLED,
                record={
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "status": run_registry.STATUS_CANCELLED,
                    "ok": False,
                    "exitCode": 1,
                    **receipt,
                },
            )

            with run_registry.registry_lock(root, timeout_seconds=1):
                run_registry.reconcile_finalize_wal_locked(root, run_id)

            for persisted in (
                _read_json(run_path / run_registry.STATE_FILE),
                _snapshot(root, run_id),
            ):
                assert persisted["failureReason"] == terminal_states.PROVIDER_CANCELLED
                assert persisted["terminalState"] == terminal_states.PROVIDER_CANCELLED
                terminal_record = persisted["terminalRecord"]
                assert isinstance(terminal_record, dict)
                assert terminal_record["state"] == terminal_states.PROVIDER_CANCELLED
                assert terminal_record["reason"] == "provider stopped the turn"

    def test_post_grace_cancel_persist_overrides_existing_provider_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = run_registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            receipt = _provider_cancel_receipt("provider stopped during cancel grace")
            state = {
                "schema": run_registry.STATE_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "status": run_registry.STATUS_CANCELLED,
                "ok": False,
                "exitCode": 1,
                "cancelRequested": True,
                **receipt,
            }
            run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, state)

            target = run_registry.RunTarget(run_id=run_id, alias=alias)
            with run_registry.registry_lock(root):
                wait_cancel_commands._persist_cancelled_terminal_locked(root, target, state, [])

            _assert_operator_cancel_receipt(_read_json(run_path / run_registry.STATE_FILE))
            _assert_operator_cancel_receipt(_snapshot(root, run_id))

    def test_operator_cancel_report_omits_provider_harness_error(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="codex")
            run_path = run_registry.run_directory(root, run_id)
            run_registry.write_json_atomic(
                run_path / run_registry.STATE_FILE,
                {"status": "running", "cancelRequested": True},
            )
            ctx = runner.RunContext(
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
                started_at="2026-09-01T17:00:00Z",
            )
            accumulator = harness_events.StreamAccumulator(harness="codex")
            provider_reason = "provider cancelled after an upstream refusal"
            accumulator.terminal_event = {
                "event": "turn.failed",
                "status": "failed",
                "reason": provider_reason,
            }
            accumulator.terminal_status = run_registry.STATUS_FAILED
            accumulator.provider_terminal_state = terminal_states.PROVIDER_CANCELLED
            accumulator.provider_terminal_reason = provider_reason
            capture = runner.TrackedCaptureResult(
                accumulator=accumulator,
                exit_code=0,
                duration_ms=10,
                stdout_bytes=1,
                stderr_bytes=0,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=os.getpgid(0),
            )

            finalization = runner._finalize_tracked_run(
                runner.TrackedRunFiles(
                    run_path=run_path,
                    stdout_log=run_path / run_registry.STDOUT_LOG,
                    stderr_log=run_path / run_registry.STDERR_LOG,
                ),
                ctx,
                capture,
                completion_report_mode="off",
            )

            assert finalization.status == run_registry.STATUS_CANCELLED
            assert finalization.extra["failureReason"] == "cancelled_by_user"
            assert "terminalEvent" not in finalization.extra
            assert "terminalStatus" not in finalization.extra
            assert accumulator.terminal_event is None
            assert accumulator.terminal_status is None
            report = (run_path / run_registry.COMPLETION_REPORT_FILE).read_text(encoding="utf-8")
            assert "Failure reason: cancelled_by_user" in report
            assert "Harness error:" not in report
            assert provider_reason not in report
