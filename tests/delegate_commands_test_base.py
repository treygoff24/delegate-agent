import importlib
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    return importlib.reload(importlib.import_module("delegate_agent.cli"))


def make_git_repo(*, with_commit: bool = False):
    temp = tempfile.TemporaryDirectory()
    git = ["git", "-C", temp.name]
    subprocess.run(
        [*git, "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if with_commit:
        subprocess.run(
            [
                *git,
                "-c",
                "user.name=Delegate Test",
                "-c",
                "user.email=delegate-test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return temp


class CommandTestBase(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()
        # Hermetic config: main() must never read the developer's real
        # ~/.delegate/config.json (its codex.binary would bypass the PATH-prefixed
        # fake executables and shell out to the real CLI).
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        config_path = Path(config_dir.name) / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        self._config_env = {
            "DELEGATE_CONFIG": str(config_path),
            "HOME": config_dir.name,
        }

    def run_main(self, argv, *, path_prefix: Path | None = None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = dict(self._config_env)
        if path_prefix is not None:
            env["PATH"] = str(path_prefix) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, env, clear=False):
            code = self.delegate.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def build_git_request(
        self,
        engine: str,
        mode: str,
        model_alias: str | None,
        workspace: str,
        prompt: str,
        config: dict,
        dry_run: bool,
        **kwargs,
    ):
        return self.delegate.build_request(
            engine,
            mode,
            model_alias,
            self.delegate.ResolvedWorkspace(workspace, "git"),
            prompt,
            config,
            dry_run,
            **kwargs,
        )

    def write_fake_executable(
        self,
        name: str,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
    ) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s' {shlex.quote(stdout)}\n"
            f"printf '%s' {shlex.quote(stderr)} >&2\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return bin_dir
