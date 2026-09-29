"""Wrong-Python guard: a clear message and exit 2, never a raw ImportError.

On Xcode's Python 3.9 the entrypoints used to die inside ``json_types`` with
``ImportError: cannot import name 'TypeAlias'``, a message that never names the
interpreter as the cause. ``bin/delegate.py`` and the package ``__init__`` now
check first; ``bin/delegate-profile-shim`` picks a 3.11+ interpreter itself.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "bin" / "delegate.py"
PACKAGE_INIT = ROOT / "src" / "delegate_agent" / "__init__.py"
SHIM = ROOT / "bin" / "delegate-profile-shim"
OLD_VERSION_INFO = "(3, 9, 6, 'final', 0)"


def _run(argv: list[str], *, env: dict[str, str] | None = None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False, env=env)


def _assert_clean_refusal(case: unittest.TestCase, result, found: str) -> None:
    case.assertEqual(result.returncode, 2, result.stderr)
    case.assertEqual(result.stdout, "")
    case.assertIn("Python 3.11 or newer is required", result.stderr)
    case.assertIn(found, result.stderr)
    case.assertNotIn("Traceback", result.stderr)
    case.assertNotIn("ImportError", result.stderr)


class GuardPlacementTests(unittest.TestCase):
    """The guard is only useful if nothing that can fail on 3.9 runs before it."""

    def _leading_statements(self, path: Path) -> list[ast.stmt]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = list(tree.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # module docstring
        return body

    def test_version_check_is_the_first_executable_code_in_both_entrypoints(self) -> None:
        for path in (ENTRY, PACKAGE_INIT):
            with self.subTest(path=path.name):
                body = self._leading_statements(path)
                first, guard = body[0], body[1]
                self.assertIsInstance(first, ast.Import)
                self.assertEqual([alias.name for alias in first.names], ["sys"])
                self.assertIsInstance(guard, ast.If)
                self.assertIn("version_info", ast.unparse(guard.test))
                self.assertTrue(
                    any(isinstance(node, ast.Raise) for node in ast.walk(guard)),
                    "the guard must stop the interpreter",
                )

    def test_no_future_import_can_precede_the_guard(self) -> None:
        # `from __future__ import annotations` is a SyntaxError before 3.7 and,
        # by the language rules, would have to sit above the guard.
        for path in (ENTRY, PACKAGE_INIT):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, "__future__")


class GuardBehaviourTests(unittest.TestCase):
    def test_entry_script_refuses_an_old_interpreter_cleanly(self) -> None:
        # sys.version_info is patched before the file runs, so this exercises
        # the guard on any interpreter, including CI's.
        code = (
            "import runpy, sys\n"
            f"sys.version_info = {OLD_VERSION_INFO}\n"
            f"runpy.run_path({str(ENTRY)!r}, run_name='__main__')\n"
        )
        _assert_clean_refusal(self, _run([sys.executable, "-c", code]), "3.9.6")

    def test_package_import_refuses_an_old_interpreter_cleanly(self) -> None:
        code = (
            "import sys\n"
            f"sys.version_info = {OLD_VERSION_INFO}\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "import delegate_agent\n"
        )
        _assert_clean_refusal(self, _run([sys.executable, "-c", code]), "3.9.6")

    def test_a_supported_interpreter_is_not_blocked(self) -> None:
        # The guard must not fire on the interpreter the suite runs on.
        result = _run([sys.executable, str(ENTRY), "--version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^\d+\.\d+\.\d+")

    def test_real_old_interpreter_gets_the_clean_message(self) -> None:
        old = _find_old_python()
        if old is None:
            self.skipTest("no Python older than 3.11 is installed")
        interpreter, version = old
        for argv, env in (
            ([interpreter, str(ENTRY), "--version"], None),
            (
                [interpreter, "-m", "delegate_agent.cli", "--version"],
                {**os.environ, "PYTHONPATH": str(ROOT / "src")},
            ),
        ):
            with self.subTest(argv=argv[1:3]):
                _assert_clean_refusal(self, _run(argv, env=env), version)


def _find_old_python() -> tuple[str, str] | None:
    candidates = ["/usr/bin/python3"]
    candidates += [
        found
        for name in ("python3.8", "python3.9", "python3.10")
        if (found := shutil.which(name)) is not None
    ]
    for candidate in candidates:
        if not Path(candidate).exists():
            continue
        try:
            probe = _run([candidate, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        except (OSError, subprocess.TimeoutExpired):
            continue
        version = probe.stdout.strip()
        if probe.returncode == 0 and version:
            major, minor, *_ = (int(part) for part in version.split("."))
            if (major, minor) < (3, 11):
                return candidate, version
    return None


class LauncherInterpreterPickerTests(unittest.TestCase):
    """``bin/delegate-profile-shim`` runs Delegate with a 3.11+ interpreter."""

    def setUp(self) -> None:
        self.bash = shutil.which("bash")
        if self.bash is None:
            self.skipTest("bash is not installed")
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-picker-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # The shim needs `cat`/`dirname` from PATH and nothing else. Linking
        # only those keeps the host's own pythons out of the search.
        sysbin = self.root / "sysbin"
        sysbin.mkdir()
        for tool in ("cat", "dirname"):
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} is not installed")
            (sysbin / tool).symlink_to(found)
        self.path = f"{self.bin}{os.pathsep}{sysbin}"

    def _fake_python(
        self,
        directory: Path,
        name: str,
        version: tuple[int, int, int],
        *,
        label: str | None = None,
    ) -> Path:
        """An executable that answers the shim's version probe and echoes real runs."""
        exit_code = 0 if version >= (3, 11) else 1
        dotted = ".".join(str(part) for part in version)
        lines = [
            "#!/bin/sh",
            'case "$1" in',
            f'  -c) case "$2" in *version_info*) exit {exit_code} ;; esac ;;',
            f'  -V) echo "Python {dotted}"; exit 0 ;;',
            "esac",
            f'echo "ran {label or name}: $*"',
        ]
        path = directory / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def _shim(self, *args: str, **env: str):
        assert self.bash is not None
        full_env = {"PATH": self.path, "HOME": str(self.root), **env}
        return _run([self.bash, str(SHIM), *args], env=full_env)

    def test_no_3_11_interpreter_is_a_clear_exit_2(self) -> None:
        self._fake_python(self.bin, "python3", (3, 9, 6))
        result = self._shim("--version")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("no Python 3.11+ interpreter found", result.stderr)
        self.assertIn("Python 3.9.6", result.stderr)
        self.assertIn("DELEGATE_PYTHON", result.stderr)

    def test_a_versioned_interpreter_is_chosen_over_an_old_python3(self) -> None:
        self._fake_python(self.bin, "python3", (3, 9, 6))
        self._fake_python(self.bin, "python3.12", (3, 12, 4))
        result = self._shim("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ran python3.12: -m delegate_agent.cli --version")

    def test_a_supported_python3_is_used_as_is(self) -> None:
        self._fake_python(self.bin, "python3", (3, 12, 4))
        self._fake_python(self.bin, "python3.13", (3, 13, 0))
        result = self._shim("--version")
        self.assertEqual(result.stdout.strip(), "ran python3: -m delegate_agent.cli --version")

    def test_shim_target_runs_under_the_chosen_interpreter(self) -> None:
        self._fake_python(self.bin, "python3.11", (3, 11, 9))
        result = self._shim("--version", DELEGATE_SHIM_PY="/x/delegate.py")
        self.assertEqual(result.stdout.strip(), "ran python3.11: /x/delegate.py --version")

    def test_homebrew_python_is_found_without_naming_its_prefix(self) -> None:
        self._fake_python(self.bin, "python3", (3, 9, 6))
        brew_bin = self.root / "brew" / "bin"
        brew_bin.mkdir(parents=True)
        self._fake_python(brew_bin, "python3", (3, 12, 4), label="brew-python3")
        result = self._shim("--version", HOMEBREW_PREFIX=str(self.root / "brew"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ran brew-python3: -m delegate_agent.cli --version")

    def test_explicit_override_is_used_when_it_is_supported(self) -> None:
        self._fake_python(self.bin, "python3", (3, 9, 6))
        chosen = self._fake_python(self.root, "chosen-python", (3, 12, 0))
        result = self._shim("--version", DELEGATE_PYTHON=str(chosen))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "ran chosen-python: -m delegate_agent.cli --version"
        )

    def test_explicit_override_that_is_too_old_is_refused_not_bypassed(self) -> None:
        self._fake_python(self.bin, "python3.12", (3, 12, 4))
        old = self._fake_python(self.root, "old-python", (3, 9, 6))
        result = self._shim("--version", DELEGATE_PYTHON=str(old))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(f"DELEGATE_PYTHON={old} is not Python 3.11+", result.stderr)
        self.assertIn("Python 3.9.6", result.stderr)
        self.assertNotIn("no Python 3.11+ interpreter found", result.stderr)


if __name__ == "__main__":
    unittest.main()
