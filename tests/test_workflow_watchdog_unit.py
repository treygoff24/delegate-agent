from __future__ import annotations

import errno
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent.workflows import registry
from delegate_agent.workflows import runtime as workflow_runtime
from delegate_agent.workflows.runtime import (
    Budget,
    WorkflowState,
    _SupervisorWatchdog,
)
from tests import proc_harness


class _WatchdogState:
    def __init__(self) -> None:
        self.wf_id = "wf_000000000001"
        self.cancel_event = threading.Event()
        self.fired: list[str] = []

    def _record_watchdog_fire(self, reason: str) -> bool:
        self.fired.append(reason)
        return True


class WorkflowWatchdogUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status_path = self.root / registry.STATUS_FILE
        registry.write_status(
            self.root,
            {"wfId": "wf_000000000001", "status": "running", "ok": True},
        )

    def _check(self) -> str | None:
        state = type("State", (), {"root": self.root, "wf_id": "wf_000000000001"})()
        watchdog = _SupervisorWatchdog(state, interval_seconds=0.01)
        return watchdog._check()

    def _run_watchdog_unwind(
        self,
        *,
        event_result: object,
        approved_result: object | None = None,
        event_flags: dict[str, object] | None = None,
        wf_id: str | None = None,
        remove_status_before_fire: bool = False,
    ) -> tuple[int, dict[str, object], list[tuple[str, object]]]:
        wf_id = wf_id or f"wf_{time.time_ns() & ((1 << 48) - 1):012x}"
        root = registry.ensure_workflow_dir(self.root, wf_id)
        script = root / registry.SCRIPT_FILE
        script.write_text("return True\n", encoding="utf-8")
        gate_key = "checkpoint"
        result_hash = workflow_runtime._gate_result_hash(event_result)
        registry.write_status(
            root,
            {"wfId": wf_id, "status": "running", "budget": {"total": None, "spent": 0}},
        )
        registry.append_jsonl(
            root / registry.JOURNAL_FILE,
            {
                "seq": 1,
                "type": "gate",
                "at": "2026-09-06T00:00:00Z",
                "key": gate_key,
                "result": event_result,
                "gateResultHash": result_hash,
                **(event_flags or {}),
            },
        )
        if approved_result is not None:
            registry.record_approval(
                root,
                gate_key,
                workflow_runtime._gate_result_hash(approved_result),
            )
        notices: list[tuple[str, object]] = []

        def watchdog_unwind(state: WorkflowState) -> None:
            if remove_status_before_fire:
                state.status_path.unlink()
            self.assertTrue(state._record_watchdog_fire("state_missing"))
            state.cancel_event.set()
            raise workflow_runtime.SupervisorWatchdogExit("state_missing")

        def capture_notice(_state: WorkflowState, event: str, *, detail: object = None) -> None:
            notices.append((event, detail))

        with (
            mock.patch.object(workflow_runtime, "execute_workflow", side_effect=watchdog_unwind),
            mock.patch.object(workflow_runtime._SupervisorWatchdog, "start", autospec=True),
            mock.patch.object(workflow_runtime._SupervisorWatchdog, "stop", autospec=True),
            mock.patch.object(workflow_runtime.WorkflowState, "notify_event", new=capture_notice),
        ):
            return_code = workflow_runtime.run_supervisor(
                workspace=self.root,
                wf_id=wf_id,
                cli_argv=[],
                config={},
            )
        return return_code, registry.read_json(root / registry.STATUS_FILE) or {}, notices

    def test_active_statuses_are_healthy(self) -> None:
        for status in (
            {"status": "running"},
            {"status": "paused", "gateKey": "gate-1"},
            {"status": "paused", "parkedItems": ["item-1"]},
        ):
            registry.write_status(self.root, {"wfId": "wf_000000000001", **status})
            self.assertIsNone(self._check())

    def test_terminal_status_reports_terminal(self) -> None:
        for status in ("succeeded", "failed", "killed"):
            registry.write_status(self.root, {"wfId": "wf_000000000001", "status": status})
            self.assertEqual(self._check(), "terminal")

    def test_deleted_state_reports_state_missing(self) -> None:
        self.status_path.unlink()
        self.assertEqual(self._check(), "state_missing")

    def test_unreadable_status_is_no_information(self) -> None:
        for exc in (
            OSError(errno.EMFILE, "too many open files"),
            PermissionError(errno.EACCES, "denied"),
        ):
            with (
                self.subTest(exc=exc),
                mock.patch("delegate_agent.workflows.runtime.os.stat", side_effect=exc),
            ):
                self.assertIsNone(self._check())

    def test_malformed_status_is_no_information(self) -> None:
        self.status_path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self._check())

    def test_one_bad_sample_is_tolerated_and_valid_sample_resets(self) -> None:
        state = _WatchdogState()
        watchdog = _SupervisorWatchdog(state, interval_seconds=0.005)
        samples = iter(("state_missing", None))
        watchdog._check = lambda: next(samples, None)  # type: ignore[method-assign]
        watchdog.start()
        time.sleep(0.03)
        watchdog.stop()
        self.assertFalse(state.cancel_event.is_set())
        self.assertEqual(state.fired, [])

    def test_second_bad_sample_requests_cancel_with_reason(self) -> None:
        state = _WatchdogState()
        watchdog = _SupervisorWatchdog(state, interval_seconds=0.005)
        watchdog._check = lambda: "state_missing"  # type: ignore[method-assign]
        watchdog.start()
        deadline = time.monotonic() + 1.0
        while not state.cancel_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        watchdog.stop()
        self.assertTrue(state.cancel_event.is_set())
        self.assertEqual(state.fired, ["state_missing"])
        self.assertEqual(watchdog.reason, "state_missing")

    def test_fire_event_precedes_unwind_and_markers_survive_status_writes(self) -> None:
        wf_id = "wf_000000000001"
        script = self.root / "script.py"
        script.write_text("return True\n", encoding="utf-8")
        state = WorkflowState(
            wf_id=wf_id,
            workspace=self.root,
            root=self.root,
            script_path=script,
            config={},
            cli_argv=[],
            args={},
            budget=Budget(None),
        )
        state.write_status("running")
        self.assertTrue(state._record_watchdog_fire("state_missing"))
        state.append_event("workflow_watchdog", reason="state_missing")
        state.write_status("failed", error="watchdog")

        events = registry.iter_journal(self.root / registry.JOURNAL_FILE)
        self.assertEqual(
            [event["type"] for event in events],
            ["workflow_watchdog_fired", "workflow_watchdog"],
        )
        status = registry.read_json(self.status_path) or {}
        self.assertEqual(status.get("watchdogReason"), "state_missing")
        self.assertTrue(status.get("watchdogCancelRequested"))
        self.assertEqual(status.get("watchdogFiredAt"), events[0].get("at"))

    def test_watchdog_stops_without_post_terminal_write(self) -> None:
        wf_id = "wf_000000000001"
        script = self.root / "script.py"
        script.write_text("return True\n", encoding="utf-8")
        state = WorkflowState(
            wf_id=wf_id,
            workspace=self.root,
            root=self.root,
            script_path=script,
            config={},
            cli_argv=[],
            args={},
            budget=Budget(None),
        )
        state.write_status("succeeded")
        watchdog = _SupervisorWatchdog(state, interval_seconds=0.005)
        watchdog.start()
        time.sleep(0.03)
        watchdog.stop()
        status = registry.read_json(self.status_path) or {}
        self.assertEqual(status.get("status"), "succeeded")
        self.assertNotIn("watchdogFiredAt", status)
        self.assertNotIn(
            "workflow_watchdog_fired",
            {
                event.get("type")
                for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            },
        )

    def test_watchdog_unwind_does_not_restore_approved_gate(self) -> None:
        result = {"ok": False, "reason": "review"}
        return_code, status, notices = self._run_watchdog_unwind(
            event_result=result,
            approved_result=result,
        )

        self.assertEqual(return_code, 1)
        self.assertEqual(status.get("status"), "failed")
        self.assertEqual(status.get("watchdogReason"), "state_missing")
        self.assertNotIn("gateKey", status)
        self.assertEqual(
            notices,
            [("failed", "workflow supervisor watchdog: state_missing")],
        )

    def test_watchdog_unwind_preserves_unapproved_gate(self) -> None:
        return_code, status, notices = self._run_watchdog_unwind(
            event_result={"ok": False, "reason": "review"},
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(status.get("status"), "paused")
        self.assertEqual(status.get("gateKey"), "checkpoint")
        self.assertEqual(notices, [("paused", "awaiting approval at checkpoint")])

    def test_watchdog_unwind_keeps_hash_mismatched_gate_pending(self) -> None:
        return_code, status, _notices = self._run_watchdog_unwind(
            event_result={"ok": False, "version": 2},
            approved_result={"ok": False, "version": 1},
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(status.get("status"), "paused")
        self.assertEqual(status.get("gateKey"), "checkpoint")

    def test_watchdog_unwind_ignores_simulated_gate_records(self) -> None:
        for flag in ("simulated", "dryRun"):
            with self.subTest(flag=flag):
                return_code, status, notices = self._run_watchdog_unwind(
                    event_result={"ok": False},
                    event_flags={flag: True},
                )

                self.assertEqual(return_code, 1)
                self.assertEqual(status.get("status"), "failed")
                self.assertNotIn("gateKey", status)
                self.assertEqual(notices[0][0], "failed")

    def test_pending_gate_selection_keeps_approved_history_and_independent_keys_distinct(
        self,
    ) -> None:
        wf_id = "wf_a11a11a11a11"
        root = registry.ensure_workflow_dir(self.root, wf_id)
        script = root / registry.SCRIPT_FILE
        script.write_text("return True\n", encoding="utf-8")
        state = WorkflowState(
            wf_id=wf_id,
            workspace=self.root,
            root=root,
            script_path=script,
            config={},
            cli_argv=[],
            args={},
            budget=Budget(None),
        )
        result_hashes: list[str] = []
        for seq, result in enumerate(({"version": 1}, {"version": 2}), start=1):
            result_hash = workflow_runtime._gate_result_hash(result)
            result_hashes.append(result_hash)
            registry.append_jsonl(
                root / registry.JOURNAL_FILE,
                {
                    "seq": seq,
                    "type": "gate",
                    "at": f"2026-09-06T00:00:0{seq}Z",
                    "key": "same-key",
                    "result": result,
                    "gateResultHash": result_hash,
                },
            )
        registry.record_approval(root, "same-key", result_hashes[1])

        older_pending = state.latest_gate_event(unapproved_only=True)
        self.assertEqual(older_pending.get("result") if older_pending else None, {"version": 1})

        registry.record_approval(root, "same-key", result_hashes[0])
        self.assertIsNone(state.latest_gate_event(unapproved_only=True))

        pending_result = {"version": 1}
        registry.append_jsonl(
            root / registry.JOURNAL_FILE,
            {
                "seq": 3,
                "type": "gate",
                "at": "2026-09-06T00:00:03Z",
                "key": "independent-pending",
                "result": pending_result,
                "gateResultHash": workflow_runtime._gate_result_hash(pending_result),
            },
        )
        pending = state.latest_gate_event(unapproved_only=True)
        self.assertEqual(pending.get("key") if pending else None, "independent-pending")

    def test_watchdog_child_cancel_continues_after_terminal_race_and_records_failure(
        self,
    ) -> None:
        wf_id = "wf_cancelerrors1"
        registry_root = workflow_runtime.run_registry.ensure_registry(
            self.root, workspace_kind="directory"
        )
        run_ids: list[str] = []
        for _ in range(4):
            run_id, alias = workflow_runtime.run_registry.register_run(
                registry_root,
                harness="codex",
                metadata={"group": wf_id},
            )
            run_ids.append(run_id)
            workflow_runtime.run_registry.write_json_atomic(
                workflow_runtime.run_registry.run_directory(registry_root, run_id)
                / workflow_runtime.run_registry.STATE_FILE,
                {
                    "schema": workflow_runtime.run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": workflow_runtime.run_registry.STATUS_RUNNING,
                    "pid": 99_999_999,
                },
            )
        attempted: list[str] = []
        terminal_race_run: list[str] = []
        signal_refused_run: list[str] = []
        failed_run: list[str] = []
        cancelled_run: list[str] = []

        def fake_emit_cancel(command, *, workspace_path, stdout):  # type: ignore[no-untyped-def]
            del workspace_path
            run_id = command.handles[0]
            attempted.append(run_id)
            run_path = workflow_runtime.run_registry.run_directory(registry_root, run_id)
            state = workflow_runtime.run_registry.load_run_state(registry_root, run_id)
            if len(attempted) == 1:
                terminal_race_run.append(run_id)
                state["status"] = workflow_runtime.run_registry.STATUS_SUCCEEDED
                workflow_runtime.run_registry.write_json_atomic(
                    run_path / workflow_runtime.run_registry.STATE_FILE, state
                )
                raise workflow_runtime.wait_cancel_commands.WaitCancelError(
                    "run_already_terminal", "settled during cancellation"
                )
            if len(attempted) == 2:
                signal_refused_run.append(run_id)
                state["status"] = workflow_runtime.run_registry.STATUS_CANCELLED
                workflow_runtime.run_registry.write_json_atomic(
                    run_path / workflow_runtime.run_registry.STATE_FILE, state
                )
                stdout.write(
                    json.dumps(
                        {
                            "ok": True,
                            "runs": [
                                {
                                    "runId": run_id,
                                    "status": "cancelled",
                                    "signalRefusal": {
                                        "signal": "SIGKILL",
                                        "reason": "permission_denied",
                                    },
                                }
                            ],
                        }
                    )
                )
                return
            if len(attempted) == 3:
                failed_run.append(run_id)
                raise workflow_runtime.wait_cancel_commands.WaitCancelError(
                    "cancel_target_changed", "generation kept changing"
                )
            cancelled_run.append(run_id)
            state["status"] = workflow_runtime.run_registry.STATUS_CANCELLED
            workflow_runtime.run_registry.write_json_atomic(
                run_path / workflow_runtime.run_registry.STATE_FILE, state
            )
            stdout.write(
                json.dumps(
                    {
                        "ok": True,
                        "runs": [{"runId": run_id, "status": "cancelled"}],
                    }
                )
            )

        with (
            mock.patch.object(
                workflow_runtime.wait_cancel_commands,
                "emit_cancel",
                side_effect=fake_emit_cancel,
            ),
            self.assertRaises(workflow_runtime.WorkflowChildCancellationError) as caught,
        ):
            workflow_runtime.cancel_workflow_children(self.root, wf_id)

        self.assertCountEqual(attempted, run_ids)
        self.assertEqual(
            workflow_runtime.run_registry.raw_status(
                workflow_runtime.run_registry.load_run_state(registry_root, terminal_race_run[0])
            ),
            workflow_runtime.run_registry.STATUS_SUCCEEDED,
        )
        self.assertEqual(
            workflow_runtime.run_registry.raw_status(
                workflow_runtime.run_registry.load_run_state(registry_root, cancelled_run[0])
            ),
            workflow_runtime.run_registry.STATUS_CANCELLED,
        )
        failure = caught.exception
        self.assertEqual(failure.failure_count, 2)
        failures_by_error = {item.get("error"): item for item in failure.failures}
        self.assertEqual(failures_by_error["signal_refused"].get("runId"), signal_refused_run[0])
        self.assertEqual(failures_by_error["cancel_target_changed"].get("runId"), failed_run[0])

    def test_watchdog_cancel_failure_is_durable_and_does_not_claim_pending_gate(self) -> None:
        failure = workflow_runtime.WorkflowChildCancellationError(
            [
                {
                    "runId": "child-2",
                    "error": "cancel_target_changed",
                    "detail": "generation kept changing",
                }
            ]
        )
        with mock.patch.object(
            workflow_runtime,
            "cancel_workflow_children",
            side_effect=failure,
        ):
            return_code, status, notices = self._run_watchdog_unwind(
                event_result={"ok": False, "reason": "review"},
            )

        self.assertEqual(return_code, 1)
        self.assertEqual(status.get("status"), "failed")
        self.assertEqual(status.get("watchdogReason"), "state_missing")
        self.assertEqual(status.get("watchdogChildCancellationFailureCount"), 1)
        self.assertEqual(status.get("watchdogChildCancellationFailures"), failure.failures)
        self.assertNotIn("gateKey", status)
        self.assertEqual(
            notices,
            [
                (
                    "failed",
                    "workflow supervisor watchdog: state_missing; child cancellation incomplete",
                )
            ],
        )

        root = registry.workflow_dir(self.root, status["wfId"])
        result = registry.read_json(root / registry.RESULT_FILE) or {}
        self.assertEqual(result.get("childCancellationFailureCount"), 1)
        self.assertEqual(result.get("childCancellationFailures"), failure.failures)
        events = registry.iter_journal(root / registry.JOURNAL_FILE)
        watchdog_event = next(event for event in events if event.get("type") == "workflow_watchdog")
        self.assertEqual(watchdog_event.get("childCancellationFailureCount"), 1)
        self.assertEqual(watchdog_event.get("childCancellationFailures"), failure.failures)
        pending = WorkflowState(
            wf_id=status["wfId"],
            workspace=self.root,
            root=root,
            script_path=root / registry.SCRIPT_FILE,
            config={},
            cli_argv=[],
            args={},
            budget=Budget(None),
        ).latest_gate_event(unapproved_only=True)
        self.assertEqual(pending.get("key") if pending else None, "checkpoint")

    def test_state_missing_cancel_failure_does_not_resurrect_status(self) -> None:
        wf_id = "wf_c33c33c33c33"
        failure = workflow_runtime.WorkflowChildCancellationError(
            [
                {
                    "runId": "child-2",
                    "error": "cancel_target_changed",
                    "detail": "generation kept changing",
                }
            ]
        )
        with mock.patch.object(
            workflow_runtime,
            "cancel_workflow_children",
            side_effect=failure,
        ):
            return_code, status, notices = self._run_watchdog_unwind(
                event_result={"ok": False, "reason": "review"},
                wf_id=wf_id,
                remove_status_before_fire=True,
            )

        root = registry.workflow_dir(self.root, wf_id)
        self.assertEqual(return_code, 1)
        self.assertEqual(status, {})
        self.assertFalse((root / registry.STATUS_FILE).exists())
        self.assertEqual(notices[0][0], "failed")
        result = registry.read_json(root / registry.RESULT_FILE) or {}
        self.assertEqual(result.get("childCancellationFailureCount"), 1)
        events = registry.iter_journal(root / registry.JOURNAL_FILE)
        self.assertTrue(
            any(
                event.get("type") == "workflow_watchdog"
                and event.get("childCancellationFailureCount") == 1
                for event in events
            )
        )
        self.assertFalse(any(event.get("type") == "workflow_finished" for event in events))

    def test_sigkill_refusal_makes_watchdog_fail_instead_of_restore_gate(self) -> None:
        wf_id = "wf_d44d44d44d44"
        registry_root = workflow_runtime.run_registry.ensure_registry(
            self.root, workspace_kind="directory"
        )
        with proc_harness.spawn_process(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        ) as proc:
            pgid = os.getpgid(proc.pid)
            run_id, alias = workflow_runtime.run_registry.register_run(
                registry_root,
                harness="codex",
                metadata={"group": wf_id, "mode": "work", "cwd": str(self.root)},
            )
            run_dir = workflow_runtime.run_registry.run_directory(registry_root, run_id)
            started_at = workflow_runtime.run_registry.utc_now_iso()
            workflow_runtime.run_registry.write_json_atomic(
                run_dir / workflow_runtime.run_registry.MANIFEST_FILE,
                {
                    "schema": workflow_runtime.run_registry.MANIFEST_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "harness": "codex",
                    "mode": "work",
                    "cwd": str(self.root),
                    "startedAt": started_at,
                },
            )
            workflow_runtime.run_registry.write_json_atomic(
                run_dir / workflow_runtime.run_registry.STATE_FILE,
                {
                    "schema": workflow_runtime.run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": workflow_runtime.run_registry.STATUS_RUNNING,
                    "pid": proc.pid,
                    "pgid": pgid,
                    "lastActivityAt": started_at,
                },
            )

            def refuse_sigkill(value, sig, *, process_group):  # type: ignore[no-untyped-def]
                self.assertEqual(value, pgid)
                self.assertTrue(process_group)
                if sig == workflow_runtime.signal.SIGKILL:
                    raise PermissionError("fixture denies SIGKILL")

            with (
                mock.patch.object(
                    workflow_runtime.wait_cancel_commands,
                    "_send_signal",
                    side_effect=refuse_sigkill,
                ),
                mock.patch.object(
                    workflow_runtime.wait_cancel_commands,
                    "_signal_target_alive",
                    return_value=True,
                ),
                mock.patch.object(
                    workflow_runtime.wait_cancel_commands,
                    "CANCEL_GRACE_SECONDS",
                    0,
                ),
            ):
                return_code, status, notices = self._run_watchdog_unwind(
                    event_result={"ok": False, "reason": "review"},
                    wf_id=wf_id,
                )

            self.assertEqual(return_code, 1)
            self.assertEqual(status.get("status"), "failed")
            self.assertNotIn("gateKey", status)
            self.assertEqual(status.get("watchdogChildCancellationFailureCount"), 1)
            failures = status.get("watchdogChildCancellationFailures")
            self.assertEqual(failures[0].get("error"), "signal_refused")
            self.assertEqual(notices[0][0], "failed")
            self.assertIsNone(proc.poll())

    def test_deleted_root_notify_failure_does_not_recreate_workflow(self) -> None:
        wf_id = "wf_e55e55e55e55"
        root = registry.ensure_workflow_dir(self.root, wf_id)
        script = root / registry.SCRIPT_FILE
        script.write_text("return True\n", encoding="utf-8")
        registry.write_status(
            root,
            {
                "wfId": wf_id,
                "status": "running",
                "budget": {"total": None, "spent": 0},
                "notify": "channel:fixture",
            },
        )
        cancellation_failure = workflow_runtime.WorkflowChildCancellationError(
            [{"runId": "child", "error": "cancel_target_changed", "detail": "changed"}]
        )

        def remove_root_and_unwind(state: WorkflowState) -> None:
            shutil.rmtree(root)
            state.cancel_event.set()
            raise workflow_runtime.SupervisorWatchdogExit("state_missing")

        with (
            mock.patch.object(
                workflow_runtime,
                "execute_workflow",
                side_effect=remove_root_and_unwind,
            ),
            mock.patch.object(workflow_runtime._SupervisorWatchdog, "start", autospec=True),
            mock.patch.object(workflow_runtime._SupervisorWatchdog, "stop", autospec=True),
            mock.patch.object(
                workflow_runtime,
                "cancel_workflow_children",
                side_effect=cancellation_failure,
            ),
            mock.patch.object(
                workflow_runtime.notify,
                "send_notification",
                side_effect=RuntimeError("fixture send failure"),
            ) as send_notification,
        ):
            return_code = workflow_runtime.run_supervisor(
                workspace=self.root,
                wf_id=wf_id,
                cli_argv=[],
                config={},
            )

        self.assertEqual(return_code, 1)
        send_notification.assert_called_once()
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
