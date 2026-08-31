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
from delegate_agent.workflows.runtime import WORKFLOW_HEARTBEAT_FILE

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "delegate.py"


class WorkflowWatchdogProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.launched_workflows: list[tuple[str, dict[str, str]]] = []
        self.addCleanup(self._cleanup_workflows)
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
        env.pop("DELEGATE_WORKFLOW_LOCK_FD", None)
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

    def _cleanup_workflows(self) -> None:
        for wf_id, env in reversed(self.launched_workflows):
            root = registry.workflow_dir(self.workspace, wf_id)
            if not registry.supervisor_alive(root):
                continue
            subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--cwd",
                    str(self.workspace),
                    "--json",
                    "workflow",
                    "kill",
                    wf_id,
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
                timeout=12,
            )

    def _launch(
        self,
        sleep: float,
        source: str | None = None,
        *,
        notify_target: str | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[str, Path]:
        script = self.workspace / f"workflow-{time.time_ns()}.py"
        script.write_text(
            source or "meta = {'name': 'watchdog'}\nreturn agent('long child')\n",
            encoding="utf-8",
        )
        argv = [
            sys.executable,
            str(CLI),
            "--cwd",
            str(self.workspace),
            "--json",
        ]
        if notify_target is not None:
            argv.extend(["--notify", notify_target])
        argv.extend(["workflow", "run", str(script)])
        env = self._env(sleep)
        if env_updates:
            env.update(env_updates)
        launched = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=10,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        wf_id = json.loads(launched.stdout)["wfId"]
        self.launched_workflows.append((wf_id, env))
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
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        self.assertEqual(status.get("watchdogReason"), "heartbeat_invalid")

    def test_healthy_long_child_keeps_heartbeat_and_is_not_killed(self) -> None:
        """Keep the child-wait regression while lease coverage moves to the timer."""
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

    def test_parked_script_survives_twice_stale_window_before_child_admission(self) -> None:
        _, root = self._launch(
            0,
            "import time\n"
            "meta = {'name': 'parked stretch'}\n"
            "time.sleep(3.5)\n"
            "return agent('after parked stretch')\n",
        )
        heartbeat = root / WORKFLOW_HEARTBEAT_FILE
        first = self._wait_for(lambda: registry.read_json(heartbeat))
        first_epoch = first.get("heartbeatEpoch") if isinstance(first, dict) else None
        self.assertIsInstance(first_epoch, (int, float))

        time.sleep(2.2)

        status = registry.read_json(root / registry.STATUS_FILE) or {}
        self.assertEqual(status.get("status"), "running")
        self.assertTrue(registry.supervisor_alive(root))
        self.assertEqual(registry.iter_journal(root / registry.JOURNAL_FILE), [])
        current = registry.read_json(heartbeat) or {}
        self.assertGreater(current.get("heartbeatEpoch", 0), float(first_epoch))
        self._wait_for(
            lambda: (
                (registry.read_json(root / registry.STATUS_FILE) or {}).get("status") == "succeeded"
            ),
            timeout=8,
        )

    def test_heartbeat_writer_recovers_after_one_unwritable_iteration(self) -> None:
        _, root = self._launch(
            0,
            "import time\nmeta = {'name': 'write recovery'}\ntime.sleep(4)\nreturn True\n",
        )
        heartbeat = root / WORKFLOW_HEARTBEAT_FILE
        first = self._wait_for(lambda: registry.read_json(heartbeat))
        first_epoch = first.get("heartbeatEpoch") if isinstance(first, dict) else None
        self.assertIsInstance(first_epoch, (int, float))

        original_mode = root.stat().st_mode & 0o777
        root.chmod(0o500)
        try:
            time.sleep(0.45)
        finally:
            root.chmod(original_mode)

        self._wait_for(
            lambda: (
                (registry.read_json(heartbeat) or {}).get("heartbeatEpoch", 0) > float(first_epoch)
            ),
            timeout=1.5,
        )
        self.assertTrue(registry.supervisor_alive(root))
        self._wait_for(
            lambda: (
                (registry.read_json(root / registry.STATUS_FILE) or {}).get("status") == "succeeded"
            ),
            timeout=8,
        )

    def test_one_refused_heartbeat_read_does_not_cancel_supervisor(self) -> None:
        _, root = self._launch(
            0,
            "import time\n"
            "from pathlib import Path\n"
            "from delegate_agent.workflows import registry as workflow_registry\n"
            "meta = {'name': 'heartbeat read retry'}\n"
            "real_read_json = workflow_registry.read_json\n"
            "refuse_once = [True]\n"
            "def read_json_with_one_refusal(path):\n"
            "    if Path(path).name == 'heartbeat.json' and refuse_once[0]:\n"
            "        refuse_once[0] = False\n"
            "        return None\n"
            "    return real_read_json(path)\n"
            "workflow_registry.read_json = read_json_with_one_refusal\n"
            "try:\n"
            "    time.sleep(0.7)\n"
            "finally:\n"
            "    workflow_registry.read_json = real_read_json\n"
            "return agent('after refused heartbeat read')\n",
        )

        self._wait_for(
            lambda: (
                (registry.read_json(root / registry.STATUS_FILE) or {}).get("status") == "succeeded"
            ),
            timeout=8,
        )
        events = registry.iter_journal(root / registry.JOURNAL_FILE)
        self.assertNotIn("workflow_watchdog", {event.get("type") for event in events})

    def test_slow_gate_and_soft_park_notifications_keep_lease_alive(self) -> None:
        post = self.bin_dir / "post"
        post.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "if ' paused' in ' '.join(sys.argv):\n"
            "    time.sleep(float(os.environ.get('FAKE_POST_SLEEP', '2.5')))\n",
            encoding="utf-8",
        )
        post.chmod(0o755)
        env_updates = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_POST_SLEEP": "2.5",
        }
        saved_workflows = self.home / ".delegate" / "workflows"
        saved_workflows.mkdir()
        child = saved_workflows / "gated-child.py"
        child.write_text("meta = {'name': 'child'}\nreturn True\n", encoding="utf-8")
        cases = {
            "gate": (
                "meta = {'name': 'slow gate notify'}\n"
                f"return workflow({str(child)!r}, gate=True)\n"
            ),
            "soft-park": (
                "meta = {'name': 'slow soft park notify'}\n"
                "def parked_item():\n"
                "    park_item('waiting')\n"
                "return soft_park({'waiting': parked_item})\n"
            ),
        }

        for name, source in cases.items():
            with self.subTest(name=name):
                _, root = self._launch(
                    0,
                    source,
                    notify_target=f"channel:{name}",
                    env_updates=env_updates,
                )
                self._wait_for(
                    lambda root=root: (
                        (registry.read_json(root / registry.STATUS_FILE) or {}).get("status")
                        == "paused"
                    )
                )
                heartbeat = root / WORKFLOW_HEARTBEAT_FILE
                first = registry.read_json(heartbeat) or {}
                first_epoch = first.get("heartbeatEpoch")
                self.assertIsInstance(first_epoch, (int, float))

                time.sleep(1.2)

                self.assertTrue(registry.supervisor_alive(root))
                current = registry.read_json(heartbeat) or {}
                self.assertGreater(current.get("heartbeatEpoch", 0), float(first_epoch))
                status = registry.read_json(root / registry.STATUS_FILE) or {}
                self._wait_process_gone(int(status["supervisorPid"]), timeout=8)
                self.assertEqual(
                    (registry.read_json(root / registry.STATUS_FILE) or {}).get("status"),
                    "paused",
                )


if __name__ == "__main__":
    unittest.main()
