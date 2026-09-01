from __future__ import annotations

import errno
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent.workflows import registry
from delegate_agent.workflows.runtime import (
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
        registry.write_status(
            self.root,
            {"wfId": "wf_000000000001", "status": "running", "ok": True},
        )

    def _check(self) -> str | None:
        state = type("State", (), {"root": self.root, "wf_id": "wf_000000000001"})()
        watchdog = _SupervisorWatchdog(state, interval_seconds=0.01)
        return watchdog._check()

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


if __name__ == "__main__":
    unittest.main()
