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
from unittest import mock

from delegate_agent import cli, run_registry, runner
from delegate_agent import config as config_api
from delegate_agent.errors import DelegateError
from delegate_agent.request_models import ResolvedWorkspace


def _start_lock_holder(
    lock_path: Path,
    ready_path: Path,
    *,
    seconds: float,
    release_path: Path | None = None,
) -> subprocess.Popen:
    # With a release path the holder keeps the flock until the caller has
    # observed the boundary it wants to contend; ``seconds`` stays as the
    # safety cap so a lost signal cannot wedge the test.
    script = (
        "import fcntl, os, pathlib, sys, time\n"
        "lock_path = pathlib.Path(sys.argv[1])\n"
        "ready_path = pathlib.Path(sys.argv[2])\n"
        "seconds = float(sys.argv[3])\n"
        "release_path = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else None\n"
        "fd = os.open(lock_path, os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "ready_path.touch()\n"
        "deadline = time.monotonic() + seconds\n"
        "while time.monotonic() < deadline:\n"
        "    if release_path is not None and release_path.exists():\n"
        "        break\n"
        "    time.sleep(0.005)\n"
    )
    argv = [sys.executable, "-c", script, str(lock_path), str(ready_path), str(seconds)]
    if release_path is not None:
        argv.append(str(release_path))
    return subprocess.Popen(argv, close_fds=True)


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
            # The holder owns the lock from before the child exits until the
            # runner's finalize WAL appears, pinning contention to the
            # finalization attempt instead of racing the startup publication.
            release = workspace / "release-holder"
            wal_path = run_registry.finalize_wal_path(root, run_id)
            holder = _start_lock_holder(
                run_registry.registry_lock_path(root),
                lock_ready,
                seconds=5,
                release_path=release,
            )
            try:
                while not lock_ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(lock_ready.exists(), "contention holder did not acquire lock")
                wal_deadline = time.monotonic() + 3
                while not wal_path.exists() and time.monotonic() < wal_deadline:
                    time.sleep(0.01)
                self.assertTrue(wal_path.exists(), "finalization did not publish a WAL")
            finally:
                release.touch()
                holder.wait(timeout=5)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "tracked finalization did not return")
            self.assertNotIn("error", result)
            code, payload = result["value"]
            self.assertEqual(code, 0)
            self.assertEqual(payload["assistantText"], "HELLO")
            run_path = run_registry.run_directory(root, run_id)
            self.assertIn("HELLO", (run_path / run_registry.STDOUT_LOG).read_text())
            self.assertTrue(wal_path.exists())

            with run_registry.registry_lock(root, timeout_seconds=1):
                run_registry.reconcile_finalize_wal_locked(root, run_id)
            run_path = run_registry.run_directory(root, run_id)
            state = json.loads((run_path / run_registry.STATE_FILE).read_text())
            snapshot = run_registry.load_run_snapshot(root, run_id)
            self.assertEqual(state["status"], run_registry.STATUS_SUCCEEDED)
            self.assertEqual(snapshot["status"], run_registry.STATUS_SUCCEEDED)
            self.assertFalse(run_registry.finalize_wal_path(root, run_id).exists())

    def test_running_refresh_after_launch_survives_lock_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="cursor")
            trigger = workspace / "child-finished"
            lock_ready = workspace / "lock-held"
            release = workspace / "release-holder"
            child = workspace / "child.py"
            child.write_text(
                "import json, pathlib, sys, time\n"
                "print(json.dumps({'type': 'message', 'role': 'assistant', 'content': 'HELLO'}), flush=True)\n"
                f"pathlib.Path({str(trigger)!r}).touch()\n"
                f"ready = pathlib.Path({str(lock_ready)!r})\n"
                "while not ready.exists(): time.sleep(0.005)\n",
                encoding="utf-8",
            )
            holder: subprocess.Popen | None = None
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
            real_capture = runner._capture_tracked_process

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

            def capture_under_held_lock(*args: object, **kwargs: object) -> object:
                # Barrier, not a delay: the child is launched and its
                # generation published before this entry, and the holder owns
                # the registry lock before the real capture body runs, so the
                # post-launch `running` refresh always meets contention. That
                # refresh used the caller's bounded admission budget before the
                # fix and raised out of the live run; it is now best-effort.
                nonlocal holder
                holder = _start_lock_holder(
                    run_registry.registry_lock_path(root),
                    lock_ready,
                    seconds=10,
                    release_path=release,
                )
                barrier_deadline = time.monotonic() + 5
                while not lock_ready.exists() and time.monotonic() < barrier_deadline:
                    time.sleep(0.005)
                return real_capture(*args, **kwargs)

            thread = threading.Thread(target=run)
            started = False
            try:
                with mock.patch.object(runner, "_capture_tracked_process", capture_under_held_lock):
                    thread.start()
                    started = True
                    thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "tracked run did not return")
                self.assertTrue(lock_ready.exists(), "contention holder did not acquire lock")
                self.assertNotIn("error", result)
                code, payload = result["value"]
                self.assertEqual(code, 0)
                self.assertEqual(payload["assistantText"], "HELLO")
                run_path = run_registry.run_directory(root, run_id)
                self.assertIn("HELLO", (run_path / run_registry.STDOUT_LOG).read_text())
                wal_path = run_registry.finalize_wal_path(root, run_id)
                self.assertTrue(wal_path.exists(), "contended finalization did not publish a WAL")
            finally:
                release.touch()
                lock_ready.touch()
                if holder is not None:
                    try:
                        holder.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        holder.kill()
                        holder.wait(timeout=5)
                if started:
                    thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "tracked run did not exit after lock release")

            with run_registry.registry_lock(root, timeout_seconds=1):
                run_registry.reconcile_finalize_wal_locked(root, run_id)
            run_path = run_registry.run_directory(root, run_id)
            state = json.loads((run_path / run_registry.STATE_FILE).read_text())
            self.assertEqual(state["status"], run_registry.STATUS_SUCCEEDED)
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
