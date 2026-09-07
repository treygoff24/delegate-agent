import ctypes
import errno
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delegate_agent import private_io


class DirectoryPublicationTests(unittest.TestCase):
    def test_native_success_and_existing_destinations(self):
        for kind in ("absent", "empty", "nonempty", "file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, target = root / "staged", root / "published"
                source.mkdir()
                (source / "ready").write_bytes(b"verified")
                if kind in {"empty", "nonempty"}:
                    target.mkdir()
                    if kind == "nonempty":
                        (target / "foreign").write_bytes(b"retain")
                elif kind == "file":
                    target.write_bytes(b"retain")
                elif kind == "symlink":
                    target.symlink_to(source, target_is_directory=True)
                if kind == "absent":
                    private_io.rename_directory_noreplace(source, target)
                    self.assertFalse(source.exists())
                    self.assertEqual((target / "ready").read_bytes(), b"verified")
                else:
                    inode = target.lstat().st_ino
                    with self.assertRaises(FileExistsError):
                        private_io.rename_directory_noreplace(source, target)
                    self.assertEqual(target.lstat().st_ino, inode)
                    self.assertEqual((source / "ready").read_bytes(), b"verified")

    def test_missing_native_primitive_and_kernel_errors_never_fallback(self):
        with (
            mock.patch.object(ctypes, "CDLL", return_value=SimpleNamespace()),
            self.assertRaises(OSError) as error,
        ):
            private_io.rename_directory_noreplace(Path("source"), Path("target"))
        self.assertEqual(error.exception.errno, errno.ENOTSUP)
        function = mock.Mock(return_value=-1)
        library = SimpleNamespace(renameat2=function, renamex_np=function)
        with (
            mock.patch.object(ctypes, "CDLL", return_value=library),
            mock.patch.object(ctypes, "get_errno", return_value=errno.ENOSYS),
            self.assertRaises(OSError) as error,
        ):
            private_io.rename_directory_noreplace(Path("source"), Path("target"))
        self.assertEqual(error.exception.errno, errno.ENOSYS)
        self.assertEqual(function.call_count, 1)

    def test_platform_flags_follow_linux_and_apple_declarations(self):
        for platform, symbol, expected in (
            ("linux", "renameat2", (-100, b"source", -100, b"target", 1)),
            ("darwin", "renamex_np", (b"source", b"target", 4)),
        ):
            function = mock.Mock(return_value=0)
            with (
                mock.patch.object(sys, "platform", platform),
                mock.patch.object(
                    ctypes, "CDLL", return_value=SimpleNamespace(**{symbol: function})
                ),
            ):
                private_io.rename_directory_noreplace(Path("source"), Path("target"))
            function.assert_called_once_with(*expected)


if __name__ == "__main__":
    unittest.main()
