import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import cli, run_registry  # noqa: E402
from tests.delegate_fixtures import write_snapshot_run  # noqa: E402


class FollowupRefusalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def write_test_run(
        self,
        *,
        harness: str = "codex",
        mode: str = "work",
        status: str = "succeeded",
        harness_session_id: str | None = "th_test123456",
        resumable: bool = True,
        pid: int | None = None,
    ) -> tuple[str, str]:
        run_id, alias = write_snapshot_run(
            run_registry,
            self.registry_root,
            self.workspace,
            harness=harness,
            status=status,
            pid=pid,
        )
        run_path = run_registry.run_directory(self.registry_root, run_id)

        # Update manifest
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        manifest["engine"] = harness
        manifest["mode"] = mode
        manifest["model"] = "gpt-5"
        if resumable:
            manifest["resumable"] = True
        run_registry.write_json_atomic(run_path / "manifest.json", manifest)

        # Update state
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["status"] = status
        if pid is not None:
            state["pid"] = pid
        if resumable:
            state["resumable"] = True
        run_registry.write_json_atomic(run_path / "state.json", state)

        snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
        snapshot["status"] = status
        if harness_session_id is not None:
            snapshot["harnessSessionId"] = harness_session_id
        if resumable:
            snapshot["resumable"] = True
        run_registry.write_json_atomic(run_path / "snapshot.json", snapshot)

        return run_id, alias

    def run_followup_cli(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = cli.main(
            ["--cwd", str(self.workspace), *args],
            stdout=stdout,
            stderr=stderr,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_refusal_not_terminal(self):
        _run_id, alias = self.write_test_run(status="running", pid=os.getpid())
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "followup", alias, "next step"]
        )
        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "followup-not-terminal")
        self.assertIn("not terminal", payload["message"])

    def test_refusal_unsupported_mode_call(self):
        _run_id, alias = self.write_test_run(mode="call", status="succeeded")
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "followup", alias, "next step"]
        )
        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "followup-unsupported-mode")
        self.assertIn("call runs have no resumable workspace", payload["message"])

    def test_refusal_unsupported_mode_safe(self):
        _run_id, alias = self.write_test_run(mode="safe", status="succeeded")
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "followup", alias, "next step"]
        )
        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "followup-unsupported-mode")
        self.assertIn("safe temp workspace is gone; use delegate resume", payload["message"])

    def test_refusal_unsupported_engine(self):
        for engine in ("cursor", "droid", "grok", "devin", "opencode", "pi", "omp", "kimi"):
            with self.subTest(engine=engine):
                _run_id, alias = self.write_test_run(
                    harness=engine, mode="work", status="succeeded"
                )
                exit_code, stdout, _stderr = self.run_followup_cli(
                    ["--json", "followup", alias, "next step"]
                )
                self.assertNotEqual(exit_code, 0)
                payload = json.loads(stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["code"], "followup-unsupported")
                self.assertIn("codex and claude", payload["message"])

    def test_refusal_session_missing(self):
        _run_id, alias = self.write_test_run(harness_session_id=None, resumable=False)
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "followup", alias, "next step"]
        )
        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "session-missing")
        self.assertIn("relaunch with --resumable", payload["message"])

    def test_refusal_session_invalid_injection_flag(self):
        _run_id, alias = self.write_test_run(harness_session_id="--dangerously-bypass-approvals")
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "followup", alias, "next step"]
        )
        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "session-invalid")

    def test_refusal_session_invalid_whitespace_or_newline(self):
        for bad_id in ("th_test\nnewline", "th_test space", "\x00bad", "th\t123"):
            with self.subTest(bad_id=bad_id):
                _run_id, alias = self.write_test_run(harness_session_id=bad_id)
                exit_code, stdout, _stderr = self.run_followup_cli(
                    ["--json", "followup", alias, "next step"]
                )
                self.assertNotEqual(exit_code, 0)
                payload = json.loads(stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["code"], "session-invalid")

    def test_happy_path_codex_dry_run(self):
        _run_id, alias = self.write_test_run(
            harness="codex",
            mode="work",
            status="succeeded",
            harness_session_id="th_valid12345",
        )
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "followup", "--dry-run", alias, "continue working"]
        )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["engine"], "codex")
        self.assertEqual(payload["mode"], "work")
        self.assertEqual(payload["followupOf"], _run_id)
        self.assertTrue(payload["resumable"])
        argv = payload["argv"]
        self.assertIn("exec", argv)
        self.assertIn("resume", argv)
        exec_idx = argv.index("exec")
        self.assertEqual(argv[exec_idx + 1], "resume")
        self.assertIn("th_valid12345", argv)
        self.assertNotIn("--ephemeral", argv)

    def test_dry_run_preserves_notify_target(self):
        _run_id, alias = self.write_test_run(
            harness="codex",
            mode="work",
            status="succeeded",
            harness_session_id="th_valid12345",
        )
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "--notify", "room:ops", "followup", "--dry-run", alias, "continue"]
        )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["notify"]["target"], "room:ops")

    def test_happy_path_claude_dry_run(self):
        claude_session = "550e8400-e29b-41d4-a716-446655440000"
        _run_id, alias = self.write_test_run(
            harness="claude",
            mode="work",
            status="succeeded",
            harness_session_id=claude_session,
        )
        exit_code, stdout, _stderr = self.run_followup_cli(
            ["--json", "followup", "--dry-run", alias, "continue claude"]
        )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["engine"], "claude")
        self.assertEqual(payload["mode"], "work")
        self.assertEqual(payload["followupOf"], _run_id)
        self.assertTrue(payload["resumable"])
        argv = payload["argv"]
        self.assertIn("--resume", argv)
        resume_idx = argv.index("--resume")
        self.assertEqual(argv[resume_idx + 1], claude_session)
        self.assertNotIn("--no-session-persistence", argv)


if __name__ == "__main__":
    unittest.main()
