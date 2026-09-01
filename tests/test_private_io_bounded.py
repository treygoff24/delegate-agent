from __future__ import annotations

import errno
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import private_io
from delegate_agent.private_io import BoundedReadError, read_private_text_bounded


class BoundedPrivateReaderTests(unittest.TestCase):
    def test_hard_link_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            alias = Path(tmp) / "record-alias"
            path.write_text("trusted", encoding="utf-8")
            os.link(path, alias)

            with self.assertRaises(BoundedReadError) as caught:
                read_private_text_bounded(path, max_bytes=1024)

            self.assertEqual(caught.exception.reason, "multiple_links")

    def test_replaced_inode_is_reopened_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            replacement = Path(tmp) / "replacement"
            path.write_text("old", encoding="utf-8")
            replacement.write_text("new", encoding="utf-8")
            original_open = private_io.open_private_file
            calls = 0

            def open_with_replacement(open_path, flags, mode=private_io.PRIVATE_FILE_MODE):
                nonlocal calls
                fd = original_open(open_path, flags, mode)
                if calls == 0:
                    os.unlink(open_path)
                    os.replace(replacement, open_path)
                calls += 1
                return fd

            with mock.patch.object(
                private_io, "open_private_file", side_effect=open_with_replacement
            ):
                self.assertEqual(read_private_text_bounded(path, max_bytes=1024), "new")
            self.assertEqual(calls, 2)

    def test_reopened_hard_link_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            replacement = Path(tmp) / "replacement"
            path.write_text("old", encoding="utf-8")
            replacement.write_text("new", encoding="utf-8")
            original_open = private_io.open_private_file
            calls = 0

            def open_with_hard_link(open_path, flags, mode=private_io.PRIVATE_FILE_MODE):
                nonlocal calls
                fd = original_open(open_path, flags, mode)
                if calls == 0:
                    os.unlink(open_path)
                    os.link(replacement, open_path)
                calls += 1
                return fd

            with (
                mock.patch.object(private_io, "open_private_file", side_effect=open_with_hard_link),
                self.assertRaises(BoundedReadError) as caught,
            ):
                read_private_text_bounded(path, max_bytes=1024)

            self.assertEqual(caught.exception.reason, "multiple_links")
            self.assertEqual(calls, 2)

    def test_persistent_zero_link_file_exhausts_reopen_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            calls = 0

            def open_unlinked(_path, _flags, _mode=private_io.PRIVATE_FILE_MODE):
                nonlocal calls
                candidate = Path(tmp) / f"candidate-{calls}"
                candidate.write_text("orphan", encoding="utf-8")
                fd = os.open(candidate, os.O_RDONLY)
                candidate.unlink()
                calls += 1
                return fd

            with (
                mock.patch.object(private_io, "open_private_file", side_effect=open_unlinked),
                self.assertRaises(BoundedReadError) as caught,
            ):
                read_private_text_bounded(path, max_bytes=1024)

            self.assertEqual(caught.exception.reason, "replaced")
            self.assertEqual(calls, private_io._PRIVATE_READ_MAX_OPEN_ATTEMPTS)

    def test_reopen_not_found_maps_to_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            path.write_text("old", encoding="utf-8")
            original_open = private_io.open_private_file
            calls = 0

            def open_then_missing(open_path, flags, mode=private_io.PRIVATE_FILE_MODE):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise FileNotFoundError(errno.ENOENT, "missing", str(open_path))
                fd = original_open(open_path, flags, mode)
                os.unlink(open_path)
                return fd

            with (
                mock.patch.object(private_io, "open_private_file", side_effect=open_then_missing),
                self.assertRaises(BoundedReadError) as caught,
            ):
                read_private_text_bounded(path, max_bytes=1024)

            self.assertEqual(caught.exception.reason, "not_found")
            self.assertEqual(calls, 2)

    def test_atomic_replace_churn_has_no_bounded_read_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            path.write_text('{"value": 0}\n', encoding="utf-8")
            stop = threading.Event()
            started = threading.Event()
            replaced = threading.Event()
            writer_errors: list[BaseException] = []
            writes = 0

            def writer() -> None:
                nonlocal writes
                try:
                    started.set()
                    while not stop.is_set():
                        candidate = Path(tmp) / f".replacement-{writes}"
                        candidate.write_text(f'{{"value": {writes % 2}}}\n', encoding="utf-8")
                        os.replace(candidate, path)
                        writes += 1
                        replaced.set()
                        time.sleep(0.000001)
                except BaseException as exc:
                    writer_errors.append(exc)

            thread = threading.Thread(target=writer)
            thread.start()
            reads = 0
            try:
                self.assertTrue(started.wait(timeout=2), "replace writer did not start")
                self.assertTrue(replaced.wait(timeout=2), "replace writer did not replace the file")
                for _ in range(20_000):
                    text = read_private_text_bounded(path, max_bytes=1024)
                    self.assertIn('"value":', text)
                    reads += 1
            finally:
                stop.set()
                thread.join(timeout=5)

            self.assertFalse(thread.is_alive(), "replace writer did not stop")
            self.assertEqual(writer_errors, [])
            self.assertEqual(reads, 20_000)
            statvfs = os.statvfs(path)
            filesystem = getattr(statvfs, "f_fsid", "unknown")
            print(
                f"replace_churn_filesystem={filesystem} st_dev={path.stat().st_dev} "
                f"writes={writes} reads={reads}"
            )


if __name__ == "__main__":
    unittest.main()
