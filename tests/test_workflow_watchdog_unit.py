from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from delegate_agent.workflows import registry
from delegate_agent.workflows.runtime import (
    WORKFLOW_HEARTBEAT_FILE,
    Budget,
    WorkflowState,
    _SupervisorWatchdog,
)


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
        self.heartbeat_path = self.root / WORKFLOW_HEARTBEAT_FILE
        registry.write_status(
            self.root,
            {"wfId": "wf_000000000001", "status": "running", "ok": True},
        )

    def _write_heartbeat(self, value: object) -> None:
        self.heartbeat_path.write_text(
            json.dumps(
                {
                    "schema": "delegate.workflow-heartbeat.v1",
                    "heartbeatEpoch": value,
                }
            ),
            encoding="utf-8",
        )

    def _check(self) -> str | None:
        state = type("State", (), {"root": self.root, "wf_id": "wf_000000000001"})()
        watchdog = _SupervisorWatchdog(
            state,
            interval_seconds=0.01,
            stale_seconds=5.0,
        )
        return watchdog._check()

    def test_pause_exemptions_and_terminal_status(self) -> None:
        self._write_heartbeat(float("nan"))
        for status in (
            {"status": "paused", "gateKey": "gate-1"},
            {"status": "paused", "parkedItems": ["item-1"]},
        ):
            registry.write_status(self.root, {"wfId": "wf_000000000001", **status})
            self.assertIsNone(self._check())

        registry.write_status(
            self.root,
            {
                "wfId": "wf_000000000001",
                "status": "succeeded",
                "watchdogFiredAt": "already-recorded",
            },
        )
        self.assertEqual(self._check(), "terminal")

    def test_invalid_samples_include_nan_and_infinity(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                registry.write_status(
                    self.root,
                    {"wfId": "wf_000000000001", "status": "running"},
                )
                self._write_heartbeat(value)
                self.assertEqual(self._check(), "heartbeat_invalid")

    def test_valid_old_timestamp_is_stale(self) -> None:
        self._write_heartbeat(time.time() - 10.0)
        self.assertEqual(self._check(), "heartbeat_stale")

    def test_one_bad_sample_is_tolerated_and_valid_sample_resets(self) -> None:
        state = _WatchdogState()
        watchdog = _SupervisorWatchdog(
            state,
            interval_seconds=0.005,
            stale_seconds=1.0,
        )
        samples = iter(("heartbeat_invalid", None))
        watchdog._check = lambda: next(samples, None)  # type: ignore[method-assign]
        watchdog.start()
        time.sleep(0.03)
        watchdog.stop()
        self.assertFalse(state.cancel_event.is_set())
        self.assertEqual(state.fired, [])

    def test_second_bad_sample_requests_cancel_with_reason(self) -> None:
        state = _WatchdogState()
        watchdog = _SupervisorWatchdog(
            state,
            interval_seconds=0.005,
            stale_seconds=1.0,
        )
        watchdog._check = lambda: "heartbeat_invalid"  # type: ignore[method-assign]
        watchdog.start()
        deadline = time.monotonic() + 1.0
        while not state.cancel_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        watchdog.stop()
        self.assertTrue(state.cancel_event.is_set())
        self.assertEqual(state.fired, ["heartbeat_invalid"])
        self.assertEqual(watchdog.reason, "heartbeat_invalid")

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
        self.assertTrue(state._record_watchdog_fire("heartbeat_stale"))
        state.append_event("workflow_watchdog", reason="heartbeat_stale")
        state.write_status("failed", error="watchdog")

        events = registry.iter_journal(self.root / registry.JOURNAL_FILE)
        self.assertEqual(
            [event["type"] for event in events],
            ["workflow_watchdog_fired", "workflow_watchdog"],
        )
        status = registry.read_json(self.status_path) or {}
        self.assertEqual(status.get("watchdogReason"), "heartbeat_stale")
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
        watchdog = _SupervisorWatchdog(
            state,
            interval_seconds=0.005,
            stale_seconds=0.001,
        )
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


if __name__ == "__main__":
    unittest.main()
