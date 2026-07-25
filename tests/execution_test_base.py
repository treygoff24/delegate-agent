import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
SCRIPT_PATH = ROOT / "bin" / "delegate.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    spec = importlib.util.spec_from_file_location("delegate_cli_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_git_repo():
    temp = tempfile.TemporaryDirectory()
    subprocess.run(
        ["git", "-C", temp.name, "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return temp


GIT_TEST_IDENTITY = ("-c", "user.name=Delegate Test", "-c", "user.email=delegate-test@example.com")


def safe_temp_dirs() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("delegate-safe-*"))


class ExecutionTestBase(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self._test_home = home.name

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
        with mock.patch.dict(os.environ, {"HOME": self._test_home}, clear=False):
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

    def parsed_launch(
        self,
        subcommand: str,
        *,
        cwd: str,
        engine: str,
        mode: str,
        prompt_parts: list[str],
        model_alias: str | None = None,
        dry_run: bool = False,
        isolation: str | None = None,
        json_mode: bool = True,
    ):
        return self.delegate.ParsedCommand(
            subcommand,
            global_options=self.delegate.GlobalOptions(
                json_mode=json_mode,
                cwd=cwd,
                isolation=isolation,
            ),
            launch=self.delegate.LaunchOptions(
                engine=engine,
                mode=mode,
                model_alias=model_alias,
                prompt_parts=prompt_parts,
                dry_run=dry_run,
            ),
        )

    def make_fake_bin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        for name in ("droid", "agent"):
            path = bin_dir / name
            path.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "${FAKE_ECHO_ARGS:-0}" = "1" ]; then\n'
                "  printf 'OUT:%s\\n' \"$*\"\n"
                "  printf 'ERR:%s\\n' \"$*\" >&2\n"
                "fi\n"
                'exit "${FAKE_EXIT:-0}"\n'
            )
            path.chmod(0o755)
        return bin_dir

    def make_cursor_safe_fake_agent(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "agent"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "touch mutated-by-agent.txt\n"
            'if [ "${FAKE_ECHO_ARGS:-0}" = "1" ]; then\n'
            "  printf 'OUT:%s\\n' \"$*\"\n"
            "fi\n"
            'exit "${FAKE_EXIT:-0}"\n'
        )
        path.chmod(0o755)
        return bin_dir

    def _make_git_repo_with_commit(self):
        """Create a temp git repo with one commit, return the path and common dir."""
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "config", "user.name", "Test"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "config", "user.email", "test@example.com"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "config", "maintenance.autoDetach", "false"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "config", "gc.autoDetach", "false"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        gd = subprocess.run(
            ["git", "-C", repo.name, "rev-parse", "--git-common-dir"],
            text=True,
            capture_output=True,
            check=True,
        )
        git_common_dir = gd.stdout.strip()
        if not git_common_dir.startswith("/"):
            git_common_dir = str(Path(repo.name) / git_common_dir)
        return repo, git_common_dir

    def _make_persistent_worktree_request(self, engine, mode, repo_dir, config, model_alias=None):
        """Build a Request with persistent worktree isolation context."""
        workspace = self.delegate.resolve_workspace(repo_dir)
        effective_isolation = self.delegate.delegate_config.resolve_isolation(
            cli_value="worktree",
            loaded_config=config,
            engine=engine,
            mode=mode,
        )
        git_root, git_common_dir, head_oid, head_ref, branch_name = (
            self.delegate.capture_git_metadata(repo_dir)
        )
        isolation_context = self.delegate.build_isolation_context(
            source_workspace=workspace.path,
            resolved_isolation=effective_isolation,
            engine=engine,
            mode=mode,
            model_alias=model_alias,
            config=config,
            run_short_id=None,
            source_git_root=git_root,
            source_git_common_dir=git_common_dir,
            source_head_oid=head_oid,
            source_head_ref=head_ref,
            source_branch=branch_name,
        )
        return self.delegate.build_request(
            engine,
            mode,
            model_alias,
            workspace,
            "hello",
            config,
            dry_run=False,
            isolation_context=isolation_context,
        )
