"""Proof that mail registries stay usable at shallow checkout depths."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.mail_test_helpers import mail_temporary_directory

ROOT = Path(__file__).resolve().parents[1]


class MailTempdirTests(unittest.TestCase):
    def test_valid_override_is_used(self) -> None:
        with tempfile.TemporaryDirectory(prefix="delegate-mail-override-") as base:
            with mock.patch.dict(os.environ, {"DELEGATE_TEST_TMPDIR": base}):
                temporary = mail_temporary_directory(prefix="delegate-mail-override-child-")
            try:
                self.assertEqual(Path(temporary.name).resolve().parent, Path(base).resolve())
            finally:
                temporary.cleanup()

    def test_invalid_override_falls_back_to_system_temp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="delegate-mail-invalid-") as base:
            invalid = Path(base) / "missing"
            with mock.patch.dict(os.environ, {"DELEGATE_TEST_TMPDIR": str(invalid)}):
                temporary = mail_temporary_directory(prefix="delegate-mail-fallback-")
            try:
                self.assertNotEqual(Path(temporary.name).resolve().parent, invalid.resolve())
            finally:
                temporary.cleanup()

    def test_mail_suite_runs_from_a_shallow_git_worktree(self) -> None:
        """A /tmp/<checkout> worktree must not derive its tempdir from parents[3]."""

        checkout = Path(tempfile.mkdtemp(prefix="delegate-mail-shallow-", dir="/tmp"))
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
        )
        for name in (
            "mail_test_helpers.py",
            "test_mail_eligibility.py",
            "test_mail_identity.py",
            "test_mail_rules.py",
            "test_mail_reply.py",
        ):
            shutil.copy2(ROOT / "tests" / name, checkout / "tests" / name)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_mail_eligibility"],
            cwd=checkout,
            capture_output=True,
            text=True,
            env={key: value for key, value in os.environ.items() if key != "DELEGATE_TEST_TMPDIR"},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
