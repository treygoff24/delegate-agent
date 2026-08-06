import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry, runner


class TrackedOutputBoundsTests(unittest.TestCase):
    def context(self, workspace: Path, *, harness: str) -> runner.RunContext:
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
        )

    def test_tracked_stream_limit_stops_child_and_caps_raw_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            context = self.context(workspace, harness="pi")
            script = (
                "import os\nchunk = b'x' * 4095 + b'\\n'\n"
                "for _ in range(4):\n    os.write(1, chunk)\n"
            )

            with (
                mock.patch.object(runner, "TRACKED_STREAM_MAX_BYTES", 8192),
                self.assertRaises(runner.RunnerLaunchError) as caught,
            ):
                runner.execute_tracked(
                    [sys.executable, "-c", script],
                    str(workspace),
                    context,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(caught.exception.error, "output_limit_exceeded")
            run_path = run_registry.run_directory(context.registry_root, context.run_id)
            self.assertLessEqual((run_path / run_registry.STDOUT_LOG).stat().st_size, 8192)
            state = run_registry.load_run_state(context.registry_root, context.run_id)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failureReason"], "output_limit_exceeded")
            self.assertEqual(state["outputLimit"]["stream"], "stdout")
            self.assertEqual(state["outputLimit"]["bytes"], 8192)

    def test_explicit_terminal_success_stops_lingering_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            context = self.context(workspace, harness="pi")
            terminal = json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Status: completed\n- done"}],
                        "stopReason": "stop",
                    },
                }
            )
            script = f"import time\nprint({terminal!r}, flush=True)\ntime.sleep(30)\n"

            started = time.monotonic()
            with mock.patch.object(runner, "TERMINAL_EXIT_GRACE_SEC", 0.1):
                code, payload = runner.execute_tracked(
                    [sys.executable, "-c", script],
                    str(workspace),
                    context,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["status"], "succeeded")
            self.assertTrue(payload["stoppedAfterCompletion"])
            self.assertLess(time.monotonic() - started, 5)
            state = run_registry.load_run_state(context.registry_root, context.run_id)
            self.assertEqual(state["status"], "succeeded")
            self.assertTrue(state["stoppedAfterCompletion"])


if __name__ == "__main__":
    unittest.main()
