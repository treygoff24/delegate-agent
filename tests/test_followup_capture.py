import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import cli  # noqa: E402


class FollowupCaptureE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.workspace = Path(self.temp.name) / "workspace"
        self.bin_dir = Path(self.temp.name) / "bin"
        self.home.mkdir()
        self.workspace.mkdir()
        self.bin_dir.mkdir()

        # Initialize git repo in workspace
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=self.workspace, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=self.workspace, check=True
        )
        (self.workspace / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=self.workspace, check=True, capture_output=True
        )

        self._write_fake_codex()
        self._write_fake_claude()

        self.config_path = self.home / "delegate_config.json"
        config = {
            "version": 1,
            "codex": {
                "binary": str(self.bin_dir / "codex"),
                "defaultModel": "gpt-5",
                "models": {"gpt-5": "gpt-5"},
                "workSandbox": "workspace-write",
                "ephemeral": True,
            },
            "claude": {
                "binary": str(self.bin_dir / "claude"),
                "defaultModel": "claude-3-7-sonnet",
                "models": {"claude-3-7-sonnet": "claude-3-7-sonnet"},
                "workPermissionMode": "auto",
                "noSessionPersistence": True,
            },
        }
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

        self.argv_log = self.home / "argv.jsonl"

    def _write_fake_codex(self) -> None:
        path = self.bin_dir / "codex"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "argv_log = os.environ.get('FAKE_CODEX_ARGV_LOG')\n"
            "if argv_log:\n"
            "    with open(argv_log, 'a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'th_e2e_fixed_12345'}))\n"
            "print(json.dumps({'type': 'turn.started'}))\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}))\n"
            "print(json.dumps({'type': 'turn.completed'}))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_fake_claude(self) -> None:
        path = self.bin_dir / "claude"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "argv_log = os.environ.get('FAKE_CLAUDE_ARGV_LOG')\n"
            "if argv_log:\n"
            "    with open(argv_log, 'a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': '550e8400-e29b-41d4-a716-446655440000'}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'claude done'}]}}))\n"
            "print(json.dumps({'type': 'result', 'status': 'success'}))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def run_delegate(
        self, args: list[str], env_extra: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = {
            "HOME": str(self.home),
            "DELEGATE_CONFIG": str(self.config_path),
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            **(env_extra or {}),
        }
        old_env = os.environ.copy()
        try:
            os.environ.update(env)
            exit_code = cli.main(
                ["--cwd", str(self.workspace), *args],
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_codex_resumable_capture_and_followup_e2e(self):
        env_extra = {"FAKE_CODEX_ARGV_LOG": str(self.argv_log)}
        # 1. Launch with --resumable
        exit_code, stdout, _stderr = self.run_delegate(
            ["--json", "codex", "work", "--resumable", "initial task"],
            env_extra=env_extra,
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r}, stderr={_stderr!r}")
        launch_payload = json.loads(stdout)
        alias = launch_payload.get("alias")
        self.assertIsNotNone(alias)

        # 2. Run followup on that alias
        exit_code, stdout, _stderr = self.run_delegate(
            ["--json", "followup", alias, "second followup task"],
            env_extra=env_extra,
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r}, stderr={_stderr!r}")
        followup_payload = json.loads(stdout)
        self.assertTrue(followup_payload.get("ok"))
        self.assertEqual(followup_payload.get("followupOf"), launch_payload.get("runId"))

        # 3. Verify logged argv
        lines = [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").strip().splitlines()
        ]
        self.assertEqual(len(lines), 2)
        initial_argv, followup_argv = lines[0], lines[1]

        # Initial launch had --resumable, so no --ephemeral
        self.assertNotIn("--ephemeral", initial_argv)
        self.assertIn("exec", initial_argv)

        # Followup launch has exec resume <thread_id> and no --ephemeral
        self.assertNotIn("--ephemeral", followup_argv)
        self.assertIn("exec", followup_argv)
        exec_idx = followup_argv.index("exec")
        self.assertEqual(followup_argv[exec_idx + 1], "resume")
        self.assertIn("th_e2e_fixed_12345", followup_argv)
        session_idx = followup_argv.index("th_e2e_fixed_12345")
        self.assertEqual(session_idx, len(followup_argv) - 2)
        self.assertEqual(followup_argv[-1], "-")

    def test_claude_resumable_capture_and_followup_e2e(self):
        claude_argv_log = self.home / "claude_argv.jsonl"
        env_extra = {"FAKE_CLAUDE_ARGV_LOG": str(claude_argv_log)}
        # 1. Launch with --resumable
        exit_code, stdout, _stderr = self.run_delegate(
            ["--json", "claude", "work", "--resumable", "initial task"],
            env_extra=env_extra,
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r}, stderr={_stderr!r}")
        launch_payload = json.loads(stdout)
        alias = launch_payload.get("alias")
        self.assertIsNotNone(alias)

        # 2. Run followup on that alias
        exit_code, stdout, _stderr = self.run_delegate(
            ["--json", "followup", alias, "second followup task"],
            env_extra=env_extra,
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r}, stderr={_stderr!r}")
        followup_payload = json.loads(stdout)
        self.assertTrue(followup_payload.get("ok"))
        self.assertEqual(followup_payload.get("followupOf"), launch_payload.get("runId"))

        # 3. Verify logged argv
        lines = [
            json.loads(line)
            for line in claude_argv_log.read_text(encoding="utf-8").strip().splitlines()
        ]
        self.assertEqual(len(lines), 2)
        initial_argv, followup_argv = lines[0], lines[1]

        # Initial launch had --resumable, so no --no-session-persistence
        self.assertNotIn("--no-session-persistence", initial_argv)

        # Followup launch has --resume <session_id> and no --no-session-persistence
        self.assertNotIn("--no-session-persistence", followup_argv)
        self.assertIn("--resume", followup_argv)
        resume_idx = followup_argv.index("--resume")
        self.assertEqual(followup_argv[resume_idx + 1], "550e8400-e29b-41d4-a716-446655440000")


if __name__ == "__main__":
    unittest.main()
