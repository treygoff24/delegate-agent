from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from delegate_agent import cli, run_registry, runner
from delegate_agent import config as config_api
from delegate_agent.errors import DelegateError
from delegate_agent.request_models import ResolvedWorkspace


def _start_lock_holder(lock_path: Path, ready_path: Path, *, seconds: float) -> subprocess.Popen:
    script = (
        "import fcntl, os, pathlib, sys, time\n"
        "lock_path = pathlib.Path(sys.argv[1])\n"
        "ready_path = pathlib.Path(sys.argv[2])\n"
        "seconds = float(sys.argv[3])\n"
        "fd = os.open(lock_path, os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "ready_path.touch()\n"
        "time.sleep(seconds)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path), str(ready_path), str(seconds)],
        close_fds=True,
    )


class RegistryContentionTests(unittest.TestCase):
    def test_completed_child_returns_success_and_wal_replays_after_lock_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="cursor")
            trigger = workspace / "child-finished"
            lock_ready = workspace / "lock-held"
            child = workspace / "child.py"
            child.write_text(
                "import json, pathlib, sys, time\n"
                "print(json.dumps({'type': 'message', 'role': 'assistant', 'content': 'HELLO'}), flush=True)\n"
                f"pathlib.Path({str(trigger)!r}).touch()\n"
                f"ready = pathlib.Path({str(lock_ready)!r})\n"
                "while not ready.exists(): time.sleep(0.005)\n",
                encoding="utf-8",
            )
            holder = None
            result: dict[str, object] = {}
            ctx = runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="work",
                model="model-id",
                source_cwd=str(workspace),
                execution_cwd=str(workspace),
                workspace_kind="directory",
                isolated_workspace=False,
                started_at=run_registry.utc_now_iso(),
                registry_lock_timeout_seconds=0.1,
            )

            def run() -> None:
                try:
                    result["value"] = runner.execute_tracked(
                        [sys.executable, str(child)],
                        str(workspace),
                        ctx,
                        json_mode=True,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                        completion_report_mode="none",
                    )
                except BaseException as exc:  # surfaced below without losing traceback
                    result["error"] = exc

            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 3
            while not trigger.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(trigger.exists(), "child did not reach completion trigger")
            holder = _start_lock_holder(
                run_registry.registry_lock_path(root), lock_ready, seconds=0.6
            )
            try:
                while not lock_ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(lock_ready.exists(), "contention holder did not acquire lock")
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "tracked finalization did not return")
                self.assertNotIn("error", result)
                code, payload = result["value"]
                self.assertEqual(code, 0)
                self.assertEqual(payload["assistantText"], "HELLO")
                run_path = run_registry.run_directory(root, run_id)
                self.assertIn("HELLO", (run_path / run_registry.STDOUT_LOG).read_text())
                self.assertTrue(run_registry.finalize_wal_path(root, run_id).exists())
            finally:
                holder.wait(timeout=5)

            with run_registry.registry_lock(root, timeout_seconds=1):
                run_registry.reconcile_finalize_wal_locked(root, run_id)
            run_path = run_registry.run_directory(root, run_id)
            state = json.loads((run_path / run_registry.STATE_FILE).read_text())
            snapshot = run_registry.load_run_snapshot(root, run_id)
            self.assertEqual(state["status"], run_registry.STATUS_SUCCEEDED)
            self.assertEqual(snapshot["status"], run_registry.STATUS_SUCCEEDED)
            self.assertFalse(run_registry.finalize_wal_path(root, run_id).exists())

    def test_launch_lock_contention_fails_before_alias_or_run_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            ready = workspace / "lock-held"
            holder = _start_lock_holder(run_registry.registry_lock_path(root), ready, seconds=0.6)
            try:
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "contention holder did not acquire lock")
                config = config_api.embedded_default_config().copy()
                config["tracking"] = {
                    **config["tracking"],
                    "registryLockTimeoutSec": 0.05,
                }
                with self.assertRaises(DelegateError) as caught:
                    cli._launch_registry(ResolvedWorkspace(str(workspace), "directory"), config)
                self.assertEqual(caught.exception.error, "registry_lock_timeout")
                self.assertEqual(list((root / "aliases").iterdir()), [])
                self.assertEqual(list((root / "runs").iterdir()), [])
            finally:
                holder.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
