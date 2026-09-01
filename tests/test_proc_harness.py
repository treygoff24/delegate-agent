from __future__ import annotations

import contextlib
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
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "start_new_session=True)\n"
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

    def test_spawn_reaps_group_members_after_leader_already_exited(self) -> None:
        # Regression PG-REG-001: a leader waited on inside the context leaves
        # identity-gated tree discovery nothing to walk; the recorded
        # launch-time group must still be reaped or same-group children leak.
        parent = (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(child.pid, flush=True)\n"
        )
        child_pid: int | None = None
        with proc_harness.spawn_process(
            [sys.executable, "-c", parent],
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            self.assertIsNotNone(process.stdout)
            line = process.stdout.readline() if process.stdout is not None else ""
            child_pid = int(line.strip())
            if process.stdout is not None:
                process.stdout.close()
            process.wait(timeout=10)
        self.assertIsNotNone(child_pid)
        self._assert_process_gone(child_pid)

    def test_matching_reap_requires_the_marker(self) -> None:
        # Regression PG-REG-002: captured pgids must not authorize a raw
        # killpg once their supervisor is gone; only a group whose member
        # command line carries the caller's marker may be signalled.
        sentinel = f"proc-harness-marker-{os.getpid()}-{time.time_ns()}"
        script = f"import time\n# {sentinel}\ntime.sleep(30)\n"
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            start_new_session=True,
        )
        try:
            pgid = os.getpgid(process.pid)
            proc_harness.reap_recorded_group_matching(pgid, "no-such-marker-anywhere")
            self.assertIsNone(process.poll(), "reap fired without a marker match")
            proc_harness.reap_recorded_group_matching(pgid, sentinel)
            self._assert_process_gone(process.pid)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)

    def test_workflow_reap_tolerates_an_already_dead_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            root = workspace / ".delegate" / "workflows" / "wf_000000000001"
            root.mkdir(parents=True)
            (root / "status.json").write_text(
                '{"supervisorPid": 4241, "supervisorPgid": 4242}', encoding="utf-8"
            )
            with (
                mock.patch.object(proc_harness.os, "getpgid", return_value=4242),
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
                [mock.call(4242, signal.SIGTERM), mock.call(4242, 0)],
            )

    def test_workflow_reap_refuses_a_reused_supervisor_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            root = workspace / ".delegate" / "workflows" / "wf_000000000001"
            root.mkdir(parents=True)
            (root / "status.json").write_text(
                '{"supervisorPid": 4241, "supervisorPgid": 4242}', encoding="utf-8"
            )
            with (
                mock.patch.object(proc_harness.os, "getpgid", return_value=4243),
                mock.patch.object(proc_harness.os, "killpg") as killpg,
                mock.patch.object(proc_harness, "_RECORDED_PGIDS", set()) as recorded,
            ):
                proc_harness.reap_workflow_now(workspace, "wf_000000000001")
            killpg.assert_not_called()
            self.assertEqual(recorded, set())

    def test_live_group_detector_sees_a_real_live_group(self) -> None:
        with proc_harness.spawn_process(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        ) as process:
            pgid = os.getpgid(process.pid)
            self.assertTrue(proc_harness._group_has_live_members(pgid))

    def test_subprocess_suite_end_hook_rejects_a_live_recorded_group(self) -> None:
        code = (
            "import os\n"
            "from tests import proc_harness\n"
            "proc_harness.register_process_tree(os.getpid(), os.getpgrp())\n"
        )
        env = os.environ.copy()
        env.pop("DELEGATE_WORKFLOW_PIN", None)
        env.pop("PYTHONPATH", None)
        env["TMPDIR"] = "/tmp"
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        pgid = process.pid
        try:
            process.wait(timeout=10)
            self.assertNotEqual(process.returncode, 0)
        finally:
            proc_harness.reap_process_group(pgid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)

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
