from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.registry_lock_guard import scan_lock


class RegistryLockGuardTests(unittest.TestCase):
    def test_scan_reports_real_flock_with_offending_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".delegate" / ".registry.lock"
            target.parent.mkdir()
            fd = os.open(target, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                violations = scan_lock(target)
                self.assertTrue(violations)
                self.assertEqual(violations[0].owner_pid, os.getpid())
                self.assertIn("python", violations[0].owner_command)
                self.assertEqual(violations[0].target, str(target))
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_independent_watcher_catches_subprocess_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / ".delegate" / ".registry.lock"
            target.parent.mkdir()
            report = root / "report.jsonl"
            stop = root / "stop"
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "from tests.registry_lock_guard import _watch; import sys; "
                        "from pathlib import Path; "
                        "raise SystemExit(_watch(Path(sys.argv[1]), Path(sys.argv[2]), "
                        "Path(sys.argv[3]), interval=0.01))"
                    ),
                    str(target),
                    str(report),
                    str(stop),
                ],
                close_fds=True,
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl, pathlib, time, sys, os; p=pathlib.Path(sys.argv[1]); "
                        "fd=os.open(p, os.O_CREAT|os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX); "
                        "time.sleep(2)"
                    ),
                    str(target),
                ],
                close_fds=True,
            )
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not report.exists():
                    time.sleep(0.02)
                self.assertTrue(report.exists())
                record = json.loads(report.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(record["target"], str(target))
                self.assertEqual(record["owner_pid"], child.pid)
            finally:
                child.terminate()
                child.wait(timeout=5)
                stop.touch()
                watcher.wait(timeout=5)
