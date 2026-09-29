from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import private_io


def _lose_the_race(leaf: str, *, plant_symlink_to: str | None = None):
    """An os.lstat that lets a rival create `leaf` right after the first miss."""
    real_lstat = os.lstat
    state = {"raced": False}

    def lstat(path, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        if path == leaf and dir_fd is not None and not state["raced"]:
            state["raced"] = True
            if plant_symlink_to is None:
                os.mkdir(leaf, 0o700, dir_fd=dir_fd)
            else:
                os.symlink(plant_symlink_to, leaf, dir_fd=dir_fd)
            raise FileNotFoundError(leaf)
        return real_lstat(path, *args, **kwargs)

    return lstat


class EnsureOwnedDirRaceTests(unittest.TestCase):
    def test_directory_created_by_a_rival_between_lstat_and_mkdir_is_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run-scratch"
            with mock.patch.object(os, "lstat", _lose_the_race("run-scratch")):
                private_io.ensure_private_owned_dir(target)
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertTrue(target.is_dir())
            self.assertEqual(mode, private_io.PRIVATE_DIR_MODE)

    def test_a_symlink_planted_by_the_rival_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run-scratch"
            with (
                mock.patch.object(os, "lstat", _lose_the_race("run-scratch", plant_symlink_to=tmp)),
                self.assertRaises(OSError),
            ):
                private_io.ensure_private_owned_dir(target)


if __name__ == "__main__":
    unittest.main()
