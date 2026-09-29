"""Workflow agent() typed provider outcomes and the per-stage lane guard."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from delegate_agent import config as delegate_config
from delegate_agent import run_registry
from delegate_agent.workflows import registry as workflow_registry
from delegate_agent.workflows import runtime as workflow_runtime
from delegate_agent.workflows import stage_guard

AUTH = {
    "signature": "auth_rejected",
    "class": "persistent",
    "scope": "lane",
    "status": 401,
    "engine": "codex",
    "hint": "Re-authenticate the lane.",
}
DROP = {"signature": "stream_disconnected", "class": "transient", "scope": "lane"}
IMAGES = {"signature": "request_image_limit", "class": "persistent", "scope": "request"}


def _failed(provider_error: dict, index: int) -> CompletedProcess:
    envelope = {
        "ok": False,
        "status": "failed",
        "exitCode": 1,
        "runId": f"del_20260928T000000Z_{index:06d}",
        "failureKind": "provider_auth" if provider_error is AUTH else "provider_error",
        "failureReason": "auth_failed" if provider_error is AUTH else "provider_error",
        "error": "auth_failed" if provider_error is AUTH else "provider_error",
        "message": "provider said no",
        "providerError": provider_error,
    }
    return CompletedProcess(["delegate"], 1, json.dumps(envelope).encode(), b"")


def _ok(index: int) -> CompletedProcess:
    envelope = {
        "ok": True,
        "status": "succeeded",
        "exitCode": 0,
        "runId": f"del_20260928T000000Z_{index:06d}",
        "failureKind": None,
        "text": "done",
    }
    return CompletedProcess(["delegate"], 0, json.dumps(envelope).encode(), b"")


def _refused(index: int) -> CompletedProcess:
    envelope = {
        "ok": False,
        "schema": "delegate.error.v1",
        "error": "lane_known_bad",
        "message": "Refusing to launch: lane codex is marked known-bad.",
        "exitCode": 4,
        "failureKind": "lane_known_bad",
        "laneKnownBad": True,
        "signature": "auth_rejected",
        "class": "persistent",
        "hint": "Re-authenticate the lane.",
    }
    return CompletedProcess(["delegate"], 4, json.dumps(envelope).encode(), b"")


class GuardUnitTests(unittest.TestCase):
    def test_trips_when_the_first_n_results_share_one_persistent_signature(self):
        guard = stage_guard.StageLaneGuard(3)
        self.assertIsNone(guard.record("s", "codex", signature="auth_rejected"))
        self.assertIsNone(guard.record("s", "codex", signature="auth_rejected"))
        self.assertIsNone(guard.tripped("s", "codex"))

        trip = guard.record("s", "codex", signature="auth_rejected")

        self.assertEqual(
            trip, {"stage": "s", "lane": "codex", "signature": "auth_rejected", "count": 3}
        )
        self.assertEqual(guard.tripped("s", "codex"), trip)

    def test_a_success_or_a_different_result_among_the_first_n_means_it_never_trips(self):
        for first, second in ((None, "auth_rejected"), ("usage_limit", "auth_rejected")):
            with self.subTest(first=first):
                guard = stage_guard.StageLaneGuard(3)
                guard.record("s", "codex", signature=first)
                guard.record("s", "codex", signature=second)
                guard.record("s", "codex", signature="auth_rejected")
                guard.record("s", "codex", signature="auth_rejected")
                self.assertIsNone(guard.tripped("s", "codex"))

    def test_only_the_first_n_results_decide(self):
        guard = stage_guard.StageLaneGuard(2)
        guard.record("s", "codex", signature=None)
        guard.record("s", "codex", signature=None)
        for _ in range(5):
            guard.record("s", "codex", signature="auth_rejected")
        self.assertIsNone(guard.tripped("s", "codex"))

    def test_stages_and_lanes_are_independent(self):
        guard = stage_guard.StageLaneGuard(1)
        guard.record("review", "codex", signature="auth_rejected")
        self.assertIsNotNone(guard.tripped("review", "codex"))
        self.assertIsNone(guard.tripped("review", "claude"))
        self.assertIsNone(guard.tripped("build", "codex"))
        self.assertIsNone(guard.tripped("review", "codex:other-model"))

    def test_zero_disables_the_guard(self):
        guard = stage_guard.StageLaneGuard(0)
        for _ in range(10):
            guard.record("s", "codex", signature="auth_rejected")
        self.assertIsNone(guard.tripped("s", "codex"))
        self.assertFalse(guard.enabled)

    def test_only_lane_scoped_persistent_errors_count(self):
        cases = (
            (AUTH, "auth_rejected"),
            (DROP, None),
            (IMAGES, None),
            ({"signature": "unclassified", "class": "unknown", "scope": "lane"}, None),
            (None, None),
        )
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(stage_guard.persistent_signature(record), expected)


class StageGuardWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.calls: list[str] = []
        self.plan = []
        self.build_state({})

    def build_state(self, provider_errors_config: dict) -> None:
        # A fresh workflow id per state: the same id would replay cached cells from its journal.
        self.builds = getattr(self, "builds", 0) + 1
        wf_id = f"wf_5c05c05c{self.builds:04d}"
        root = self.workspace / ".delegate" / "workflows" / wf_id
        run_registry.ensure_private_dir(root)
        config = delegate_config.embedded_default_config()
        if provider_errors_config:
            config = {**config, "providerErrors": provider_errors_config}
        self.state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=self.workspace / "workflow.py",
            config=config,
            cli_argv=["delegate"],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.dsl = workflow_runtime.WorkflowDsl(
            self.state, {"defaults": {"engine": "codex", "mode": "safe"}}
        )

    def child(self, argv, *, cwd, timeout, environment=None, cancel_event=None):
        payload = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        self.calls.append(payload["engine"])
        index = len(self.calls)
        behavior = self.plan[min(index - 1, len(self.plan) - 1)]
        return behavior(index) if callable(behavior) else behavior[0](behavior[1], index)

    def run_calls(self, count: int, **kwargs):
        results = []
        with (
            mock.patch.object(workflow_runtime, "_run_child_command", side_effect=self.child),
            mock.patch.object(self.dsl, "_release_structured_retry_worktree"),
        ):
            for number in range(count):
                results.append(
                    self.dsl.agent(
                        f"cell {number}", label=f"cell-{number}", on_failure="typed", **kwargs
                    )
                )
        return results

    def events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in workflow_registry.iter_journal(self.state.journal_path)
            if event.get("type") == event_type
        ]

    def persistent_failure(self, index):
        return _failed(AUTH, index)

    def test_the_stage_stops_launching_after_three_same_signature_failures(self):
        self.plan = [self.persistent_failure]
        self.dsl.phase("review")

        results = self.run_calls(6)

        self.assertEqual(len(self.calls), 3, "cells 4-6 must not launch")
        first, skipped = results[:3], results[3:]
        for failure in first:
            self.assertEqual(failure.failure_kind, "provider_auth")
            self.assertEqual(failure.provider_error["signature"], "auth_rejected")
        for failure in skipped:
            self.assertIsInstance(failure, workflow_runtime.AgentFailure)
            self.assertEqual(failure.failure_kind, "provider_exhausted")
            self.assertEqual(failure.failure_reason, "provider_exhausted")
            self.assertEqual(failure.provider_error["signature"], "auth_rejected")
            self.assertEqual(failure.provider_error["stage"], "review")
            self.assertEqual(failure.provider_error["lane"], "codex")
            self.assertEqual(failure.provider_error["count"], 3)
            self.assertIsNone(failure.run_id)
            self.assertFalse(failure)

    def test_the_journal_says_why_the_stage_stopped(self):
        self.plan = [self.persistent_failure]
        self.dsl.phase("review")

        self.run_calls(5)

        (stopped,) = self.events("stage_lane_stopped")
        self.assertEqual(stopped["stage"], "review")
        self.assertEqual(stopped["lane"], "codex")
        self.assertEqual(stopped["signature"], "auth_rejected")
        self.assertEqual(stopped["count"], 3)
        self.assertIn("provider_exhausted", stopped["message"])
        skips = self.events("agent_lane_skipped")
        self.assertEqual(len(skips), 2)
        self.assertEqual({event["signature"] for event in skips}, {"auth_rejected"})
        self.assertEqual({event["reason"] for event in skips}, {"stage_stopped_on_lane"})
        self.assertEqual([event["label"] for event in skips], ["cell-3", "cell-4"])

    def test_a_success_among_the_first_three_keeps_the_stage_launching(self):
        self.plan = [self.persistent_failure, _ok, self.persistent_failure]
        self.dsl.phase("review")

        results = self.run_calls(6)

        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.events("stage_lane_stopped"), [])
        self.assertEqual(results[1], "done")

    def test_another_stage_is_unaffected(self):
        self.plan = [self.persistent_failure]
        self.dsl.phase("review")
        self.run_calls(4)
        self.assertEqual(len(self.calls), 3)

        self.dsl.phase("build")
        self.run_calls(1)

        self.assertEqual(len(self.calls), 4, "a new stage gets its own tally")

    def test_transient_and_request_scoped_failures_never_stop_a_stage(self):
        for label, record in (("transient", DROP), ("request", IMAGES)):
            with self.subTest(label):
                self.build_state({})
                self.calls.clear()
                self.plan = [lambda index, record=record: _failed(record, index)]
                self.dsl.phase(label)

                results = self.run_calls(6)

                self.assertEqual(len(self.calls), 6)
                self.assertEqual(self.events("stage_lane_stopped"), [])
                self.assertNotEqual(results[-1].failure_kind, "provider_exhausted")

    def test_the_config_key_sets_the_threshold_and_zero_turns_it_off(self):
        self.build_state({"stageStopAfter": 2})
        self.plan = [self.persistent_failure]
        self.dsl.phase("review")
        self.run_calls(5)
        self.assertEqual(len(self.calls), 2)

        self.build_state({"stageStopAfter": 0})
        self.calls.clear()
        self.dsl.phase("review")
        self.run_calls(5)
        self.assertEqual(len(self.calls), 5)

    def test_a_known_bad_refusal_is_typed_and_counts_toward_the_stop(self):
        self.plan = [_refused]
        self.dsl.phase("review")

        results = self.run_calls(5)

        self.assertEqual(len(self.calls), 3)
        for refusal in results[:3]:
            self.assertEqual(refusal.failure_kind, "lane_known_bad")
            self.assertEqual(refusal.provider_error["signature"], "auth_rejected")
        for skipped in results[3:]:
            self.assertEqual(skipped.failure_kind, "provider_exhausted")

    def test_a_fallback_engine_still_runs_when_the_first_lane_is_stopped(self):
        def by_engine(index):
            return _failed(AUTH, index) if self.calls[-1] == "codex" else _ok(index)

        self.plan = [by_engine]
        self.dsl.phase("review")

        results = self.run_calls(5, engine=["codex", "claude"])

        # Cells 1-3 try codex (fail) then claude (ok); after the stop, only claude runs.
        self.assertEqual(self.calls, ["codex", "claude"] * 3 + ["claude", "claude"])
        self.assertEqual(results, ["done"] * 5)
        (stopped,) = self.events("stage_lane_stopped")
        self.assertEqual(stopped["lane"], "codex")
        self.assertEqual(len(self.events("agent_lane_skipped")), 2)

    def test_a_cell_queued_behind_the_agent_cap_does_not_launch_after_the_stop(self):
        self.state.agent_semaphore = threading.Semaphore(1)
        self.plan = [self.persistent_failure]
        self.dsl.phase("review")
        barrier = threading.Barrier(6)
        results: list = []
        lock = threading.Lock()

        def cell(number: int) -> None:
            barrier.wait()
            outcome = self.dsl.agent(f"cell {number}", label=f"cell-{number}", on_failure="typed")
            with lock:
                results.append(outcome)

        with (
            mock.patch.object(workflow_runtime, "_run_child_command", side_effect=self.child),
            mock.patch.object(self.dsl, "_release_structured_retry_worktree"),
        ):
            threads = [threading.Thread(target=cell, args=(number,)) for number in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        self.assertEqual(len(results), 6)
        self.assertEqual(len(self.calls), 3, "queued cells must see the stop once they get a slot")
        kinds = sorted(failure.failure_kind for failure in results)
        self.assertEqual(kinds, ["provider_auth"] * 3 + ["provider_exhausted"] * 3)
