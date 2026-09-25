"""The stall watchdog as the tracked runner drives it, against a real child."""

import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry, runner, stall_watchdog


class StallWatchdogRunnerTests(unittest.TestCase):
    def context(
        self, workspace: Path, *, harness: str, stall_seconds: float, pinned: bool = False
    ) -> runner.RunContext:
        registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
        run_id, alias = run_registry.register_run(registry_root, harness=harness)
        return runner.RunContext(
            registry_root=registry_root,
            run_id=run_id,
            alias=alias,
            harness=harness,
            engine=harness,
            mode="work",
            model=None,
            source_cwd=str(workspace),
            execution_cwd=str(workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
            stall_seconds=stall_seconds,
            stall_seconds_pinned=pinned,
        )

    def run_child(self, context: runner.RunContext, workspace: Path, script: str):
        with mock.patch.object(stall_watchdog, "PROCESS_SAMPLE_INTERVAL_SEC", 0.05):
            return runner.execute_tracked(
                [sys.executable, "-c", script],
                str(workspace),
                context,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

    def test_devin_report_then_silence_finishes_instead_of_stalling(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            context = self.context(workspace, harness="devin", stall_seconds=0.5)
            script = (
                "import time\n"
                "print('Did the work.', flush=True)\n"
                "print('- Status: completed', flush=True)\n"
                "print('- Files changed: none', flush=True)\n"
                "time.sleep(30)\n"
            )
            started = time.monotonic()
            code, payload = self.run_child(context, workspace, script)
            self.assertLess(time.monotonic() - started, 15)
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertEqual(payload["status"], "succeeded")
            self.assertTrue(payload["stoppedAfterCompletion"])
            self.assertNotIn("stall", payload)

    def test_a_devin_report_of_failed_or_blocked_then_silence_records_a_failure(self):
        for status in ("failed", "blocked"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                context = self.context(workspace, harness="devin", stall_seconds=0.5)
                script = (
                    "import time\n"
                    "print('Could not finish.', flush=True)\n"
                    f"print('- Status: {status}', flush=True)\n"
                    "time.sleep(30)\n"
                )
                started = time.monotonic()
                code, _payload = self.run_child(context, workspace, script)
                self.assertLess(time.monotonic() - started, 15)
                self.assertNotEqual(code, 0)
                state = run_registry.load_run_state(context.registry_root, context.run_id)
                self.assertEqual(state["status"], "failed")
                self.assertTrue(state["stoppedAfterCompletion"])
                self.assertNotIn("stall", state)

    def test_an_echoed_report_template_is_not_a_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            context = self.context(workspace, harness="devin", stall_seconds=0.5)
            script = (
                "import time\n"
                "print('- Status: completed / blocked / failed', flush=True)\n"
                "time.sleep(30)\n"
            )
            with self.assertRaises(runner.RunnerLaunchError) as raised:
                self.run_child(context, workspace, script)
            self.assertEqual(raised.exception.error, "stalled")

    def test_devin_silence_without_a_report_stalls_with_child_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            context = self.context(workspace, harness="devin", stall_seconds=0.5)
            script = "import time\nprint('starting', flush=True)\ntime.sleep(30)\n"
            with self.assertRaises(runner.RunnerLaunchError) as raised:
                self.run_child(context, workspace, script)
            self.assertEqual(raised.exception.error, "stalled")
            self.assertIn("waiting on its provider", raised.exception.message)
            state = run_registry.load_run_state(context.registry_root, context.run_id)
            self.assertEqual(state["failureReason"], "stalled")
            stall = state["stall"]
            self.assertEqual(stall["stallReason"], "idle")
            # A sleeping child is alive with flat CPU: waiting, not dead.
            self.assertEqual(stall["childActivity"], "waiting")

    def test_a_tool_that_starts_while_the_group_is_sampled_cancels_the_kill(self):
        """Idle crosses the threshold, sampling starts, and a tool starts meanwhile.

        The child waits for the sampler to open a gate, then prints a real
        tool_execution_start (tests/fixtures/pi/tool_read.jsonl shape). The
        sampler returns only after the watchdog has seen that line, so the
        interleaving is fixed by barriers, not timing. The kill must be
        cancelled and the run finish normally once the tool ends.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            gate = workspace / "gate"
            context = self.context(workspace, harness="omp", stall_seconds=0.5, pinned=True)
            script = (
                "import json, pathlib, time\n"
                f"gate = pathlib.Path({str(gate)!r})\n"
                "def emit(p):\n"
                "    print(json.dumps(p), flush=True)\n"
                "emit({'type': 'agent_start'})\n"
                "emit({'type': 'turn_start'})\n"
                "while not gate.exists():\n"
                "    time.sleep(0.01)\n"
                "call = 'call_gate|fc_gate'\n"
                "emit({'type': 'tool_execution_start', 'toolCallId': call,"
                " 'toolName': 'bash', 'args': {'command': 'make test'}})\n"
                "time.sleep(1.0)  # the tool running, not synchronization\n"
                "emit({'type': 'tool_execution_end', 'toolCallId': call, 'toolName': 'bash',"
                " 'result': {'content': [{'type': 'text', 'text': 'ok'}]}, 'isError': False})\n"
                "emit({'type': 'turn_end', 'message': {'role': 'assistant', 'content':"
                " [{'type': 'text', 'text': 'done'}], 'stopReason': 'stop'}, 'toolResults': []})\n"
                "emit({'type': 'agent_end', 'messages': [], 'willRetry': False})\n"
            )
            saw_tool = threading.Event()
            observe = stall_watchdog.StallWatchdog.observe_line

            def observe_and_signal(watchdog, line, **kwargs):
                observe(watchdog, line, **kwargs)
                if '"tool_execution_start"' in line:
                    saw_tool.set()

            sampled = []

            def sample_while_a_tool_starts(pgid, **_kwargs):
                sampled.append(pgid)
                gate.touch()
                self.assertTrue(saw_tool.wait(10), "child never started its tool")
                return {"childActivity": "waiting"}

            with (
                mock.patch.object(stall_watchdog.StallWatchdog, "observe_line", observe_and_signal),
                mock.patch.object(
                    stall_watchdog, "process_group_activity", sample_while_a_tool_starts
                ),
            ):
                code, _payload = self.run_child(context, workspace, script)
            self.assertEqual(len(sampled), 1)
            self.assertEqual(code, 0)
            state = run_registry.load_run_state(context.registry_root, context.run_id)
            self.assertEqual(state["status"], "succeeded")
            self.assertNotIn("stall", state)

    def test_pinned_threshold_is_not_overridden_by_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            context = self.context(workspace, harness="devin", stall_seconds=0.5, pinned=True)
            script = "import time\nprint('starting', flush=True)\ntime.sleep(30)\n"
            started = time.monotonic()
            with (
                mock.patch.dict(os.environ, {runner.STALL_MINUTES_ENV: "999"}),
                self.assertRaises(runner.RunnerLaunchError) as raised,
            ):
                self.run_child(context, workspace, script)
            self.assertLess(time.monotonic() - started, 15)
            self.assertEqual(raised.exception.error, "stalled")


if __name__ == "__main__":
    unittest.main()
