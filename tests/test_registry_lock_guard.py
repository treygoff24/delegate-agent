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


@unittest.skipUnless(
    sys.platform == "linux",
    "the lock-guard watcher reads the kernel flock table via /proc (Linux-only by design)",
)
class RegistryLockGuardAttributionTests(unittest.TestCase):
    """The guard owns its suite's holders, not every holder of the file."""

    def _holder(self, target: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, pathlib, time, sys, os; p=pathlib.Path(sys.argv[1]); "
                    "fd=os.open(p, os.O_CREAT|os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX); "
                    "time.sleep(30)"
                ),
                str(target),
            ],
            close_fds=True,
        )

    def test_scan_ignores_holders_outside_the_suite_process_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".delegate" / ".registry.lock"
            target.parent.mkdir()
            # Stands in for a concurrent launcher from another session: alive,
            # but no ancestor of the holder below.
            foreign_root = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                close_fds=True,
            )
            holder = self._holder(target)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not scan_lock(target):
                    time.sleep(0.02)
                self.assertTrue(scan_lock(target), "the holder never took the lock")

                self.assertEqual(scan_lock(target, suite_pid=foreign_root.pid), ())
                attributed = scan_lock(target, suite_pid=os.getpid())
                self.assertEqual([violation.owner_pid for violation in attributed], [holder.pid])
                self.assertEqual(attributed[0].attribution, "suite-descendant")
                self.assertEqual(scan_lock(target)[0].attribution, "unverified")
            finally:
                holder.terminate()
                holder.wait(timeout=5)
                foreign_root.terminate()
                foreign_root.wait(timeout=5)

    def test_unrelated_suite_pids_attribute_nothing_without_a_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".delegate" / ".registry.lock"
            target.parent.mkdir()
            # No holder at all: an unrelated or unreadable ancestry must not
            # invent an escape, and pid 1 is never a descendant of this suite.
            self.assertEqual(scan_lock(target, suite_pid=1), ())
            self.assertEqual(scan_lock(target, suite_pid=os.getpid()), ())
            self.assertEqual(scan_lock(target, suite_pid=os.getpid() + 0x7FFFFFFF), ())


@unittest.skipUnless(
    sys.platform == "linux",
    "the lock-guard watcher reads the kernel flock table via /proc (Linux-only by design)",
)
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
