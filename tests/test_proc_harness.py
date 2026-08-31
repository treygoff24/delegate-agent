from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import proc_harness


class ProcessHarnessTests(unittest.TestCase):
    def test_spawn_reaps_supervisor_shaped_tree(self) -> None:
        parent = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        child_pid: int | None = None
        parent_pid: int
        with proc_harness.spawn_process(
            [sys.executable, "-c", parent],
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            parent_pid = process.pid
            self.assertIsNotNone(process.stdout)
            line = process.stdout.readline() if process.stdout is not None else ""
            child_pid = int(line.strip())
            self.assertIsNotNone(child_pid)
            if process.stdout is not None:
                process.stdout.close()
        self._assert_process_gone(parent_pid)
        self.assertIsNotNone(child_pid)
        self._assert_process_gone(child_pid)

    def test_workflow_reap_tolerates_an_already_dead_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            root = workspace / ".delegate" / "workflows" / "wf_000000000001"
            root.mkdir(parents=True)
            (root / "status.json").write_text('{"supervisorPgid": 4242}', encoding="utf-8")
            with (
                mock.patch.object(
                    proc_harness.os,
                    "killpg",
                    side_effect=ProcessLookupError,
                ) as killpg,
                proc_harness.workflow_reap(workspace, "wf_000000000001"),
            ):
                pass
            self.assertEqual(
                killpg.call_args_list,
                [mock.call(4242, signal.SIGTERM), mock.call(4242, signal.SIGKILL)],
            )

    def test_suite_end_assertion_rejects_a_live_recorded_group(self) -> None:
        with (
            mock.patch.object(proc_harness, "_group_has_live_members", return_value=True),
            self.assertRaisesRegex(AssertionError, "4242"),
            mock.patch.object(proc_harness, "_RECORDED_PGIDS", {4242}),
        ):
            proc_harness.assert_no_live_process_groups()

    @staticmethod
    def _assert_process_gone(pid: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            else:
                state = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat="],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if not state or state.startswith("Z"):
                    return
            time.sleep(0.02)
        raise AssertionError(f"process {pid} is still live")


if __name__ == "__main__":
    unittest.main()
