import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import process_guard


def _failed_ps(*, returncode: int = 1, stdout: str = "") -> mock._patch:
    """Patch the scan's ``ps`` call: the listing it returns, or its failure."""
    return mock.patch.object(
        process_guard.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=["ps"], returncode=returncode, stdout=stdout, stderr="ps: cannot read\n"
        ),
    )


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

    def test_owns_processes_under_any_of_the_given_roots(self):
        """A caller's proof may name every root its own producers carry.

        A detached workflow child is launched by its supervisor as the pinned
        runtime entrypoint under the run's HOME, with the workspace only as its
        working directory (``_run_child_command_for_state``) and no workspace
        flag in its command line, so a proof that could name one root only would
        read that live producer as gone.
        """
        with (
            tempfile.TemporaryDirectory() as workspace,
            tempfile.TemporaryDirectory() as home,
        ):
            entrypoint = (
                Path(home)
                / ".delegate-workflow-pins"
                / "runtimes"
                / "abc123"
                / "bin"
                / "delegate.py"
            )
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("import time; time.sleep(60)\n", encoding="utf-8")
            child = subprocess.Popen(
                [sys.executable, str(entrypoint), "--json", "--group", "wf_test", "run"],
                cwd=workspace,
                start_new_session=True,
            )
            self.addCleanup(self._cleanup_process, child)

            self.assertEqual(
                process_guard.owned_delegate_processes(Path(workspace)),
                set(),
                "a child whose command line carries no workspace path is outside that root",
            )
            owned = process_guard.owned_delegate_processes(Path(workspace), Path(home))
            self.assertIn(child.pid, owned)

    @staticmethod
    def _cleanup_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


class OwnedProcessScanFailureTests(unittest.TestCase):
    """A scan that cannot be completed must never read as "nothing is running".

    Callers use this scan as the suite's producer boundary, so an unanswered
    scan is the one outcome that must be loud: reaping, and every containment
    proof built on it, raise instead of reporting a false absence.
    """

    def test_failed_scan_raises_instead_of_reporting_no_owned_process(self):
        with _failed_ps(returncode=1):
            with self.assertRaises(process_guard.OwnedProcessScanError):
                process_guard.owned_delegate_processes(Path("/nonexistent-owned-root"))
            with self.assertRaises(process_guard.OwnedProcessScanError):
                process_guard.reap_delegate_processes(Path("/nonexistent-owned-root"))

    def test_incomplete_scan_listing_raises(self):
        for stdout in (
            "",
            "  1234  1234\n",
            "  1  1 /usr/bin/ps\n",
        ):
            with (
                self.subTest(stdout=stdout),
                _failed_ps(returncode=0, stdout=stdout),
                self.assertRaises(process_guard.OwnedProcessScanError),
            ):
                process_guard.owned_delegate_processes(Path("/nonexistent-owned-root"))

    def test_unrunnable_scan_raises(self):
        with (
            mock.patch.object(process_guard.subprocess, "run", side_effect=OSError("no ps")),
            self.assertRaises(process_guard.OwnedProcessScanError),
        ):
            process_guard.reap_delegate_processes(Path("/nonexistent-owned-root"))

    def test_non_numeric_pid_or_pgid_raises_instead_of_dropping_the_row(self):
        """An unparseable matching row is an incomplete listing, not an absent process.

        The malformed row names a Delegate process under the requested root, so
        dropping it would let the scan certify a live producer as gone and
        ``poll()`` answer zero. A listing the scan cannot fully read is the same
        loud outcome as any other unanswered scan.
        """
        owned_row = "/opt/bin/delegate.py --cwd /x/owned"
        for pid_text, pgid_text in (("notpid", "123"), ("123", "notpgid")):
            stdout = (
                f"  {os.getpid()}  {os.getpgrp()} {sys.executable}\n"
                f"  {pid_text} {pgid_text} {owned_row}\n"
            )
            with (
                self.subTest(pid=pid_text, pgid=pgid_text),
                _failed_ps(returncode=0, stdout=stdout),
                self.assertRaises(process_guard.OwnedProcessScanError),
            ):
                process_guard.owned_delegate_processes(Path("/x/owned"))
