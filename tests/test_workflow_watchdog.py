from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from delegate_agent.workflows import registry

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "delegate.py"


class WorkflowWatchdogProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.home = self.workspace / "home"
        self.home.mkdir()
        (self.home / ".delegate").mkdir()
        self.bin_dir = self.workspace / "bin"
        self.bin_dir.mkdir()
        self.codex = self.bin_dir / "codex"
        self.codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "time.sleep(float(os.environ.get('FAKE_CODEX_SLEEP', '0')))\n"
            "print(json.dumps({'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}))\n"
            "print(json.dumps({'type':'completion','finalText':'done'}))\n",
            encoding="utf-8",
        )
        self.codex.chmod(0o755)
        self.config = self.workspace / ".delegate" / "config.json"
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            json.dumps(
                {
                    "codex": {"binary": str(self.codex)},
                    "workflows": {"watchdogTimeoutSeconds": 1.0},
                }
            ),
            encoding="utf-8",
        )

    def _env(self, sleep: float) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "DELEGATE_CONFIG": str(self.config),
                "DELEGATE_WORKFLOW_NO_DAEMON": "1",
                "DELEGATE_WORKFLOW_WATCHDOG_TIMEOUT_SECONDS": "1",
                "FAKE_CODEX_SLEEP": str(sleep),
            }
        )
        return env

    def _launch(self, sleep: float) -> tuple[str, Path]:
        script = self.workspace / f"workflow-{time.time_ns()}.py"
        script.write_text(
            "meta = {'name': 'watchdog'}\nreturn agent('long child')\n", encoding="utf-8"
        )
        launched = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--cwd",
                str(self.workspace),
                "--json",
                "workflow",
                "run",
                str(script),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=self._env(sleep),
            timeout=10,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        wf_id = json.loads(launched.stdout)["wfId"]
        root = registry.workflow_dir(self.workspace, wf_id)
        self._wait_for(
            lambda: (registry.read_json(root / registry.STATUS_FILE) or {}).get("supervisorPid")
        )
        return wf_id, root

    def _wait_for(self, predicate, timeout: float = 6.0) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("watchdog process condition timed out")

    def _wait_process_gone(self, pid: int, timeout: float = 6.0) -> None:
        # ps rather than /proc: the supervisor is not our child (no waitpid),
        # and /proc does not exist on macOS. A zombie counts as gone.
        def gone() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            state = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip()
            return not state or state.startswith("Z")

        self._wait_for(gone, timeout)

    def test_state_file_deletion_cancels_real_supervisor_and_releases_lock(self) -> None:
        _, root = self._launch(10)
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        pid = int(status["supervisorPid"])
        (root / registry.STATUS_FILE).unlink()
        self._wait_process_gone(pid)
        self.assertFalse(registry.supervisor_alive(root))
        self.assertFalse((root / registry.STATUS_FILE).exists())

    def test_registry_entry_deletion_cancels_real_supervisor(self) -> None:
        _, root = self._launch(10)
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        pid = int(status["supervisorPid"])
        shutil.rmtree(root)
        self._wait_process_gone(pid)

    def test_frozen_heartbeat_cancels_real_supervisor_and_releases_lock(self) -> None:
        _, root = self._launch(10)
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        pid = int(status["supervisorPid"])
        heartbeat = root / "heartbeat.json"
        self._wait_for(lambda: heartbeat.exists())
        heartbeat.unlink()
        heartbeat.mkdir()
        self._wait_process_gone(pid)
        self.assertFalse(registry.supervisor_alive(root))

    def test_healthy_long_child_keeps_heartbeat_and_is_not_killed(self) -> None:
        _, root = self._launch(2)
        heartbeat = root / "heartbeat.json"
        first = self._wait_for(lambda: registry.read_json(heartbeat))
        first_epoch = first.get("heartbeatEpoch") if isinstance(first, dict) else None
        self.assertIsInstance(first_epoch, (int, float))
        self._wait_for(
            lambda: (
                (registry.read_json(heartbeat) or {}).get("heartbeatEpoch", 0) > float(first_epoch)
            )
        )
        self._wait_for(
            lambda: (
                (registry.read_json(root / registry.STATUS_FILE) or {}).get("status") == "succeeded"
            ),
            timeout=8,
        )
        events = registry.iter_journal(root / registry.JOURNAL_FILE)
        self.assertNotIn("workflow_watchdog", {event.get("type") for event in events})
        self.assertFalse(registry.supervisor_alive(root))


if __name__ == "__main__":
    unittest.main()
