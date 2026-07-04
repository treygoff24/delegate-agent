from __future__ import annotations

import io
import json
import os
import shutil
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

from tests.execution_test_base import ExecutionTestBase  # noqa: E402

GIT_TEST_IDENTITY = (
    "-c",
    "user.name=Delegate Test",
    "-c",
    "user.email=delegate-test@example.com",
)


def git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *GIT_TEST_IDENTITY, *args],
        text=True,
        capture_output=True,
        check=True,
    )


class Wave4LaunchFeatureTests(ExecutionTestBase):
    def write_executable(self, name: str, body: str) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / name
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def write_config(self, config: dict) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def config_with_cursor(self, agent: Path, *, data_home: str | None = None) -> dict:
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["cursor"]["argvPrefix"] = [str(agent)]
        if data_home is not None:
            config["worktrees"]["dataHome"] = data_home
        return config

    def test_include_dirty_worktree_syncs_tracked_untracked_ignored_and_external_symlink(self):
        from delegate_agent import safe_workspace

        with tempfile.TemporaryDirectory() as fake_home:
            repo, _git_cd = self._make_git_repo_with_commit()
            repo_path = Path(repo.name)
            (repo_path / "tracked.txt").write_text("clean\n", encoding="utf-8")
            (repo_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            git(repo.name, "add", "tracked.txt", ".gitignore")
            git(repo.name, "commit", "-m", "base")

            (repo_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            (repo_path / "untracked.txt").write_text("new\n", encoding="utf-8")
            (repo_path / "ignored.txt").write_text("secret\n", encoding="utf-8")
            outside = Path(fake_home) / "outside.txt"
            outside.write_text("host secret\n", encoding="utf-8")
            os.symlink(outside, repo_path / "external-link")

            agent = self.write_executable("agent", "exit 0\n")
            config_path = self.write_config(
                self.config_with_cursor(agent, data_home=str(Path(fake_home) / "worktrees"))
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"HOME": fake_home, "DELEGATE_CONFIG": str(config_path)},
                clear=False,
            ):
                code = self.delegate.main(
                    [
                        "--cwd",
                        repo.name,
                        "--json",
                        "--isolation",
                        "worktree",
                        "cursor",
                        "work",
                        "--include-dirty",
                        "hello",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )
            self.assertEqual(code, 0, stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["includeDirty"])
            self.assertEqual(payload["syncedFiles"], 3)

            execution_cwd = Path(payload["executionCwd"])
            self.assertEqual((execution_cwd / "tracked.txt").read_text(encoding="utf-8"), "dirty\n")
            self.assertEqual(
                (execution_cwd / "untracked.txt").read_text(encoding="utf-8"),
                "new\n",
            )
            self.assertFalse((execution_cwd / "ignored.txt").exists())
            blocked_link = execution_cwd / "external-link"
            self.assertFalse(blocked_link.is_symlink())
            self.assertEqual(
                blocked_link.read_text(encoding="utf-8"),
                safe_workspace.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
            )

    def test_scratch_env_is_applied_after_profile_env_for_isolated_run(self):
        repo, _git_cd = self._make_git_repo_with_commit()
        agent = self.write_executable(
            "agent",
            'printf "TMPDIR=%s\\nTMP=%s\\nTEMP=%s\\n" "$TMPDIR" "$TMP" "$TEMP"\n',
        )
        config = self.config_with_cursor(agent)
        config["profiles"]["default"] = "profiled"
        config["profiles"]["definitions"] = {
            "profiled": {"env": {"TMPDIR": "/profile/tmpdir", "TMP": "/profile/tmp"}}
        }
        config_path = self.write_config(config)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"DELEGATE_CONFIG": str(config_path)}, clear=False):
            code = self.delegate.main(
                ["--cwd", repo.name, "--json", "cursor", "safe", "hello"],
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(code, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        run_path = Path(repo.name) / ".delegate" / "runs" / payload["runId"]
        scratch = run_path / "scratch"
        expected_scratch = scratch.resolve()
        stdout_log = (run_path / "stdout.log").read_text(encoding="utf-8")
        self.assertIn(f"TMPDIR={expected_scratch}", stdout_log)
        self.assertIn(f"TMP={expected_scratch}", stdout_log)
        self.assertIn(f"TEMP={expected_scratch}", stdout_log)
        self.assertNotIn("/profile/tmp", stdout_log)

    def test_codex_safe_run_manifest_adds_scratch_dir_to_read_only_sandbox(self):
        repo, _git_cd = self._make_git_repo_with_commit()
        codex = self.write_executable("codex", "exit 0\n")
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["binary"] = str(codex)
        config["codex"]["defaultModel"] = "gpt-test"
        config_path = self.write_config(config)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"DELEGATE_CONFIG": str(config_path)}, clear=False):
            code = self.delegate.main(
                ["--cwd", repo.name, "--json", "codex", "safe", "hello"],
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(code, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        run_path = Path(repo.name) / ".delegate" / "runs" / payload["runId"]
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        argv = manifest["argv"]
        self.assertIn("--sandbox", argv)
        self.assertIn("read-only", argv)
        add_dir_index = argv.index("--add-dir")
        self.assertEqual(Path(argv[add_dir_index + 1]).resolve(), (run_path / "scratch").resolve())

    def test_droid_call_invalid_alias_does_not_leave_call_temp_dir(self):
        before = set(Path(tempfile.gettempdir()).glob("delegate-call-*"))
        config_path = self.write_config(self.delegate.DEFAULT_CONFIG)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"DELEGATE_CONFIG": str(config_path)}, clear=False):
            code = self.delegate.main(
                ["--json", "droid", "invalid_alias", "call", "hello"],
                stdout=stdout,
                stderr=stderr,
            )
        self.assertNotEqual(code, 0)
        after = set(Path(tempfile.gettempdir()).glob("delegate-call-*"))
        self.assertEqual(after - before, set())

    def test_describe_full_is_strict_superset_of_summary_and_command_options_populated(self):
        from delegate_agent import describe_payload as describe_module

        config = self.delegate.DEFAULT_CONFIG
        workspace = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        full = describe_module.describe_payload(config, "test-config", workspace)
        summary = describe_module.describe_summary_payload(config, "test-config", workspace)
        for key in summary:
            self.assertIn(key, full)
        self.assertEqual(summary["commands"], full["commands"])
        for command in full["commands"]:
            self.assertIn("name", command)
            self.assertIsInstance(command.get("options"), list)


class Wave4ParserFeatureTests(ExecutionTestBase):
    def test_group_rejects_invalid_launch_name(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--group", "bad/name", "cursor", "work", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_group")

    def test_run_input_json_include_dirty_matches_cli_semantics(self):
        repo, _git_cd = self._make_git_repo_with_commit()
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "run.json"
            input_path.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": repo.name,
                        "prompt": "hello",
                        "isolation": "worktree",
                        "includeDirty": True,
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.parse_cli(["run", "--input-json", str(input_path)])
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertTrue(request.include_dirty)


if __name__ == "__main__":
    unittest.main()
