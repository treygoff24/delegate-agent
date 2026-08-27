from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.process_guard import reap_delegate_processes
from tests.registry_lock_guard import source_lock_for_linked_worktree


@pytest.fixture(autouse=True)
def isolate_and_reap_delegate_processes(tmp_path, monkeypatch):
    temp_root = tmp_path / "delegate-temp"
    temp_root.mkdir()
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(temp_root)
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(temp_root))
    try:
        yield
    finally:
        try:
            reap_delegate_processes(temp_root)
        finally:
            tempfile.tempdir = previous_tempdir


@pytest.fixture(scope="session", autouse=True)
def linked_worktree_registry_lock_guard():
    """Fail the suite if any process flocks the source checkout's registry lock."""
    worktree = Path(__file__).resolve().parents[1]
    target = source_lock_for_linked_worktree(worktree)
    if target is None:
        yield
        return

    guard_dir = Path(tempfile.mkdtemp(prefix="delegate-lock-guard-"))
    report = guard_dir / "violations.jsonl"
    stop = guard_dir / "stop"
    command = [
        sys.executable,
        "-m",
        "tests.registry_lock_guard",
        "--target",
        str(target),
        "--report",
        str(report),
        "--stop",
        str(stop),
    ]
    watcher = subprocess.Popen(command, close_fds=True)
    try:
        yield
    finally:
        stop.touch()
        try:
            watcher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watcher.kill()
            watcher.wait(timeout=5)
        if report.exists():
            details = report.read_text(encoding="utf-8").strip()
            if details:
                pytest.fail(
                    "linked-worktree suite flocks source registry lock; offending caller(s): "
                    + details
                )
