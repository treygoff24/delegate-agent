from __future__ import annotations

import tempfile

import pytest

from tests import assert_linked_worktree_lock_clean
from tests.process_guard import reap_delegate_processes


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
    yield
    try:
        assert_linked_worktree_lock_clean()
    except AssertionError as exc:
        pytest.fail(str(exc))
