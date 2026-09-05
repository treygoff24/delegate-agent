"""PI/OMP terminal receipts, including exit-zero failures and harness retries.

Event shapes follow the installed PI/OMP AgentEvent/AgentSessionEvent types;
the subprocesses below are fixtures, not provider integration probes.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import harness_events, run_registry, runner


def turn(reason, text="partial result", **fields):
    return {
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stopReason": reason,
            **fields,
        },
    }


class ProviderOutcomeTests(unittest.TestCase):
    def test_explicit_turn_outcome_matrix_preserves_partial_text(self):
        for harness in ("pi", "omp"):
            for reason, expected in (
                ("stop", "succeeded"),
                ("error", "failed"),
                ("aborted", "cancelled"),
                ("length", "failed"),
                ("toolUse", None),
            ):
                with self.subTest(harness=harness, reason=reason):
                    acc = harness_events.StreamAccumulator(harness=harness)
                    acc.ingest_line(json.dumps(turn(reason, errorMessage="provider detail")))
                    self.assertEqual(acc.terminal_status, expected)
                    if expected:
                        self.assertEqual(acc.assistant_text, "partial result")
                        self.assertEqual(acc.completion_text is not None, expected == "succeeded")

    def test_http_failure_without_stop_reason_is_not_a_success(self):
        acc = harness_events.StreamAccumulator(harness="omp")
        acc.ingest_line(json.dumps(turn(None, errorStatus=403, errorMessage="account denied")))
        self.assertEqual(acc.terminal_status, "failed")
        self.assertIn("account denied", acc.terminal_event["reason"])

    def test_tool_message_cannot_forge_a_provider_outcome(self):
        acc = harness_events.StreamAccumulator(harness="omp")
        event = turn("error", errorStatus=403)
        event["message"]["role"] = "toolResult"
        acc.ingest_line(json.dumps(event))
        self.assertIsNone(acc.terminal_status)
        self.assertEqual(acc.assistant_text, "")

    def test_malformed_stop_reason_is_not_a_terminal_receipt(self):
        for reason in ({"error": True}, ["error"], 1):
            with self.subTest(reason=reason):
                acc = harness_events.StreamAccumulator(harness="omp")
                acc.ingest_line(json.dumps(turn(reason)))
                self.assertIsNone(acc.terminal_status)

    def test_retry_recovery_replaces_failure_not_partial_completion(self):
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness):
                acc = harness_events.StreamAccumulator(harness=harness)
                acc.ingest_line(json.dumps(turn("error", errorMessage="temporary error")))
                self.assertEqual(acc.terminal_status, "failed")
                acc.ingest_line(json.dumps({"type": "auto_retry_start"}))
                self.assertIsNone(acc.terminal_status)
                acc.ingest_line(json.dumps({"type": "turn_start"}))
                acc.ingest_line(json.dumps(turn("stop", "recovered result")))
                acc.ingest_line(json.dumps({"type": "auto_retry_end", "success": True}))
                self.assertEqual(acc.terminal_status, "succeeded")
                self.assertEqual(acc.assistant_text, "recovered result")

    def test_retry_exhaustion_records_last_error(self):
        acc = harness_events.StreamAccumulator(harness="omp")
        for event in (
            turn("error"),
            {"type": "auto_retry_start"},
            {"type": "auto_retry_end", "success": False, "finalError": "retries exhausted"},
        ):
            acc.ingest_line(json.dumps(event))
        self.assertEqual(acc.terminal_status, "failed")
        self.assertIn("retries exhausted", acc.terminal_event["reason"])

    def _tracked(self, harness, events, workspace, *, delay_after_retry=0):
        root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
        run_id, alias = run_registry.register_run(root, harness=harness)
        ctx = runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness=harness,
            engine=harness,
            mode="work",
            model=None,
            source_cwd=workspace,
            execution_cwd=workspace,
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
        )
        script = (
            "import json,time\n"
            f"for event in {events!r}:\n"
            " print(json.dumps(event),flush=True)\n"
            f" if event['type']=='auto_retry_start': time.sleep({delay_after_retry!r})\n"
        )
        code, payload = runner.execute_tracked(
            [sys.executable, "-c", script],
            workspace,
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        return code, payload, run_registry.load_run_state(root, run_id)

    def test_exit_zero_provider_error_fails_tracked_and_call(self):
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as workspace:
                events = [turn("error", errorMessage="provider rejected the request")]
                code, payload, state = self._tracked(harness, events, workspace)
                self.assertEqual(code, 1)
                self.assertFalse(payload["ok"])
                self.assertEqual(state["status"], "failed")
                self.assertIn("partial result", payload["assistantText"])
                self.assertEqual(payload["error"], "provider_error")
                self.assertIn("provider rejected the request", payload["message"])
                call = runner.execute_call(
                    [sys.executable, "-c", f"print({json.dumps(events[0])!r})"],
                    workspace,
                    harness=harness,
                )
                self.assertEqual(call.exit_code, 1)
                self.assertEqual(call.text, "partial result")
                self.assertEqual(call.error, "provider_error")
                self.assertIn("provider rejected the request", call.message)

    def test_previous_retry_error_does_not_classify_a_later_failure(self):
        acc = harness_events.StreamAccumulator(harness="omp")
        for event in (
            turn("error", errorMessage="unauthorized authentication failed"),
            {"type": "auto_retry_start"},
            {"type": "turn_start"},
            turn("error", errorMessage="invalid request payload"),
        ):
            acc.ingest_line(json.dumps(event))
        self.assertEqual(runner._accumulator_failure_signal_text(acc), "invalid request payload")

    def test_retry_delay_does_not_trigger_terminal_shutdown(self):
        events = [
            turn("error"),
            {"type": "auto_retry_start"},
            {"type": "turn_start"},
            turn("stop", "recovered result"),
            {"type": "auto_retry_end", "success": True},
        ]
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.object(runner, "TERMINAL_EXIT_GRACE_SEC", 0.03),
        ):
            code, payload, state = self._tracked("omp", events, workspace, delay_after_retry=0.15)
        self.assertEqual(code, 0)
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(payload["assistantText"], "recovered result")

    def test_exit_during_pending_retry_cannot_be_success(self):
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, state = self._tracked(
                "omp", [turn("error"), {"type": "auto_retry_start"}], workspace
            )
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(state["status"], "failed")


if __name__ == "__main__":
    unittest.main()
