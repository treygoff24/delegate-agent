"""The stall watchdog as the tracked runner drives it, against a real child."""

import io
import os
import sys
import tempfile
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
