import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import process_guard


class ProcessGuardTests(unittest.TestCase):
    def test_reaps_only_delegate_processes_owned_by_temp_root(self):
        with (
            tempfile.TemporaryDirectory() as owned,
            tempfile.TemporaryDirectory() as foreign,
            tempfile.TemporaryDirectory() as scripts,
        ):
            script = Path(scripts) / "delegate.py"
            script.write_text("import time; time.sleep(60)\n", encoding="utf-8")
            owned_process = subprocess.Popen(
                [sys.executable, str(script), "--cwd", owned, "omp", "work"],
                start_new_session=True,
            )
            foreign_process = subprocess.Popen(
                [sys.executable, str(script), "--cwd", foreign, "omp", "work"],
                start_new_session=True,
            )
            self.addCleanup(self._cleanup_process, owned_process)
            self.addCleanup(self._cleanup_process, foreign_process)

            with mock.patch.dict("os.environ", {"COLUMNS": "80"}):
                reaped = process_guard.reap_delegate_processes(Path(owned))

            self.assertIn(owned_process.pid, reaped)
            self.assertIsNotNone(owned_process.poll())
            self.assertIsNone(foreign_process.poll())

    @staticmethod
    def _cleanup_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
