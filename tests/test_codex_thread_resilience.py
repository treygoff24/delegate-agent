import io
import json
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

from delegate_agent import run_registry, runner  # noqa: E402


class CodexThreadResilienceTests(unittest.TestCase):
    def _run(self, script_body: str):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        workspace = Path(temp.name)
        script = workspace / "codex"
        script.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
        script.chmod(0o755)
        root = run_registry.ensure_registry(workspace, workspace_kind="directory")
        run_id, alias = run_registry.register_run(root, harness="codex")
        ctx = runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode="safe",
            model="synthetic-model",
            source_cwd=str(workspace),
            execution_cwd=str(workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-07-21T12:00:00Z",
        )
        code, payload = runner.execute_tracked(
            [str(script), "--profile", "delegate", "exec", "--json", "--ephemeral", "-"],
            str(workspace),
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            stdin_text="task",
        )
        return workspace, root, run_id, code, payload

    def _run_work(
        self,
        script_body: str,
        *,
        mode: str = "work",
        isolated_workspace: bool = False,
        dirty_baseline: bool = False,
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root_dir = Path(temp.name)
        workspace = root_dir / "repo"
        workspace.mkdir()
        subprocess.run(
            ["git", "-C", str(workspace), "init", "-q"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.name", "Delegate Tests"],
            check=True,
        )
        (workspace / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "tracked.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-qm", "baseline"],
            check=True,
        )
        if dirty_baseline:
            (workspace / "uncommitted.txt").write_text("synced change\n", encoding="utf-8")
        script = root_dir / "codex"
        script.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
        script.chmod(0o755)
        attempts = root_dir / "attempts"
        registry = run_registry.ensure_registry(workspace, workspace_kind="git")
        run_id, alias = run_registry.register_run(registry, harness="codex")
        ctx = runner.RunContext(
            registry_root=registry,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode=mode,
            model="synthetic-model",
            source_cwd=str(workspace),
            execution_cwd=str(workspace),
            workspace_kind="git",
            isolated_workspace=isolated_workspace,
            started_at="2026-07-21T12:00:00Z",
            env_overrides={"ATTEMPTS_FILE": str(attempts)},
        )
        code, payload = runner.execute_tracked(
            [str(script), "--profile", "delegate", "exec", "--json", "-"],
            str(workspace),
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            stdin_text="task",
        )
        return workspace, attempts, code, payload

    def _run_call(self, *, read_only: bool):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        workspace = Path(temp.name)
        script = workspace / "codex"
        script.write_text(
            """#!/usr/bin/env python3
import json, pathlib
attempts = pathlib.Path(__file__).with_name("attempts")
count = int(attempts.read_text()) + 1 if attempts.exists() else 1
attempts.write_text(str(count))
if count == 1:
    print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
    raise SystemExit(1)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed"}))
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        result = runner.execute_call(
            [str(script)], str(workspace), harness="codex", read_only=read_only
        )
        return workspace / "attempts", result

    def test_thread_loss_retries_once_then_uses_ephemeral_fallback(self):
        workspace, _root, _run_id, code, payload = self._run(
            """import json, pathlib, sys
count_path = pathlib.Path(__file__).with_name("attempts")
count = int(count_path.read_text() or "0") + 1 if count_path.exists() else 1
count_path.write_text(str(count))
if count < 3:
    print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
    raise SystemExit(1)
assert "--ignore-user-config" in sys.argv
assert "--ephemeral" in sys.argv
assert "--profile" not in sys.argv
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Status: completed after ephemeral fallback."}}))
print(json.dumps({"type": "turn.completed"}))
"""
        )

        self.assertEqual(code, 0)
        self.assertEqual((workspace / "attempts").read_text(), "3")
        self.assertTrue(payload["codexThreadFallback"]["engaged"])
        self.assertTrue(payload["codexThreadFallback"]["resolved"])

    def test_repeated_thread_loss_surfaces_typed_failure(self):
        _workspace, root, run_id, code, payload = self._run(
            """import json
print(json.dumps({"type": "error", "message": "state database thread lookup failed"}))
raise SystemExit(1)
"""
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "codex_thread_lost")
        self.assertIn("thread", payload["message"].lower())
        state = run_registry.load_run_state(root, run_id)
        self.assertEqual(state["failureReason"], "codex_thread_lost")
        summary = run_registry.list_run_summaries(root, run_registry.load_index(root))[0]
        self.assertEqual(summary["effectiveStatus"], "failed")
        self.assertEqual(summary["error"], "codex_thread_lost")
        self.assertIn("thread", summary["message"].lower())
        report = (root / "runs" / run_id / "completion-report.md").read_text()
        self.assertIn("codex_thread_lost", report)

    def test_usage_limit_surfaces_reset_time_in_envelope_and_report(self):
        _workspace, root, run_id, code, payload = self._run(
            """import json
print(json.dumps({"type": "error", "message": "Usage limit reached; resets at 2026-07-22 01:00 UTC."}))
raise SystemExit(1)
"""
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "usage_limit")
        self.assertIn("2026-07-22 01:00 UTC", payload["message"])
        report = (root / "runs" / run_id / "completion-report.md").read_text()
        self.assertIn("usage_limit", report)
        self.assertIn("2026-07-22 01:00 UTC", report)

    def test_usage_limit_reset_window_is_redacted_before_it_reaches_disk(self):
        # The reset window is spliced into the failure message verbatim, and
        # codex reports quota walls as stdout error events, which are not
        # covered by the stderr-tail redaction.
        # The token is assembled at runtime so no secret-shaped literal lands
        # in the repository (release scans run over history).
        secret = "sk-" + "abcdef1234567890"
        _workspace, root, run_id, code, payload = self._run(
            'import json\nprint(json.dumps({"type": "error", "message": '
            '"Usage limit reached; resets at 2026-07-22 01:00 UTC for '
            'Authorization: Bearer sk-" + "abcdef1234567890"}))\n'
            "raise SystemExit(1)\n"
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "usage_limit")
        self.assertIn("2026-07-22 01:00 UTC", payload["message"])
        self.assertNotIn(secret, payload["message"])
        run_path = root / "runs" / run_id
        for name in ("state.json", "completion-report.md"):
            with self.subTest(artifact=name):
                self.assertNotIn(secret, (run_path / name).read_text(encoding="utf-8"))
        # recentEvents deliberately mirrors the raw event log, so the snapshot
        # is checked on the fields delegate itself composes.
        snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
        self.assertNotIn(secret, snapshot["message"])
        self.assertNotIn(secret, snapshot["current"])
        report = (run_path / "completion-report.md").read_text(encoding="utf-8")
        self.assertIn("usage_limit", report)
        self.assertIn("2026-07-22 01:00 UTC", report)

    def test_work_mode_thread_loss_after_tool_event_does_not_retry(self):
        _workspace, attempts, code, payload = self._run_work(
            """import json, os, pathlib
attempts = pathlib.Path(os.environ["ATTEMPTS_FILE"])
attempts.write_text(str(int(attempts.read_text()) + 1 if attempts.exists() else 1))
print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "touch output.txt"}}))
print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
raise SystemExit(1)
"""
        )

        self.assertEqual(code, 1)
        self.assertEqual(attempts.read_text(), "1")
        self.assertEqual(payload["error"], "codex_thread_lost")
        self.assertNotIn("codexThreadFallback", payload)

    def test_work_mode_thread_loss_after_workspace_change_does_not_retry(self):
        workspace, attempts, code, payload = self._run_work(
            """import json, os, pathlib
attempts = pathlib.Path(os.environ["ATTEMPTS_FILE"])
attempts.write_text(str(int(attempts.read_text()) + 1 if attempts.exists() else 1))
pathlib.Path("changed.txt").write_text("changed")
print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
raise SystemExit(1)
"""
        )

        self.assertEqual(code, 1)
        self.assertEqual(attempts.read_text(), "1")
        self.assertTrue((workspace / "changed.txt").exists())
        self.assertEqual(payload["error"], "codex_thread_lost")
        self.assertNotIn("codexThreadFallback", payload)

    def test_safe_isolated_thread_loss_after_workspace_change_does_not_retry(self):
        workspace, attempts, code, payload = self._run_work(
            """import json, os, pathlib
attempts = pathlib.Path(os.environ["ATTEMPTS_FILE"])
attempts.write_text(str(int(attempts.read_text()) + 1 if attempts.exists() else 1))
pathlib.Path("changed.txt").write_text("changed")
print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
raise SystemExit(1)
""",
            mode="safe",
            isolated_workspace=True,
        )

        self.assertEqual(code, 1)
        self.assertEqual(attempts.read_text(), "1")
        self.assertTrue((workspace / "changed.txt").exists())
        self.assertEqual(payload["error"], "codex_thread_lost")
        self.assertNotIn("codexThreadFallback", payload)

    def test_safe_isolated_unchanged_dirty_baseline_retries_once(self):
        _workspace, attempts, code, payload = self._run_work(
            """import json, os, pathlib
attempts = pathlib.Path(os.environ["ATTEMPTS_FILE"])
count = int(attempts.read_text()) + 1 if attempts.exists() else 1
attempts.write_text(str(count))
if count == 1:
    print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
    raise SystemExit(1)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed"}))
""",
            mode="safe",
            isolated_workspace=True,
            dirty_baseline=True,
        )

        self.assertEqual(code, 0)
        self.assertEqual(attempts.read_text(), "2")
        self.assertTrue(payload["codexThreadFallback"]["retryAttempted"])
        self.assertTrue(payload["codexThreadFallback"]["resolved"])

    def test_baseline_probe_error_does_not_fail_run_and_disables_retry(self):
        success = """import json, os, pathlib
attempts = pathlib.Path(os.environ["ATTEMPTS_FILE"])
attempts.write_text("1")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed"}))
"""
        thread_loss = """import json, os, pathlib
attempts = pathlib.Path(os.environ["ATTEMPTS_FILE"])
attempts.write_text(str(int(attempts.read_text()) + 1 if attempts.exists() else 1))
print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
raise SystemExit(1)
"""

        with mock.patch.object(
            runner.profiles,
            "capture_workspace_porcelain",
            side_effect=PermissionError("denied"),
        ):
            _workspace, attempts, code, payload = self._run_work(
                success,
                mode="safe",
                isolated_workspace=True,
            )
        self.assertEqual(code, 0)
        self.assertEqual(attempts.read_text(), "1")
        self.assertNotIn("codexThreadFallback", payload)

        with mock.patch.object(
            runner.profiles,
            "capture_workspace_porcelain",
            side_effect=PermissionError("denied"),
        ):
            _workspace, attempts, code, payload = self._run_work(
                thread_loss,
                mode="safe",
                isolated_workspace=True,
            )
        self.assertEqual(code, 1)
        self.assertEqual(attempts.read_text(), "1")
        self.assertEqual(payload["error"], "codex_thread_lost")
        self.assertNotIn("codexThreadFallback", payload)

    def test_clean_work_mode_prelaunch_thread_loss_retries_once(self):
        _workspace, attempts, code, payload = self._run_work(
            """import json, os, pathlib
attempts = pathlib.Path(os.environ["ATTEMPTS_FILE"])
count = int(attempts.read_text()) + 1 if attempts.exists() else 1
attempts.write_text(str(count))
if count == 1:
    print(json.dumps({"type": "error", "message": "no thread with id: synthetic-thread"}))
    raise SystemExit(1)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed"}))
"""
        )

        self.assertEqual(code, 0)
        self.assertEqual(attempts.read_text(), "2")
        self.assertTrue(payload["codexThreadFallback"]["retryAttempted"])
        self.assertFalse(payload["codexThreadFallback"]["engaged"])
        self.assertTrue(payload["codexThreadFallback"]["resolved"])

    def test_successful_call_quoting_thread_loss_stays_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "codex"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'type': 'item.completed', 'item': "
                "{'type': 'agent_message', 'text': 'Quoted: no thread with id'}}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            result = runner.execute_call([str(script)], tmp, harness="codex")

        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.error)
        self.assertIsNone(result.codex_thread_fallback)
        self.assertEqual(result.text, "Quoted: no thread with id")

    def test_write_capable_call_thread_loss_does_not_retry(self):
        attempts, result = self._run_call(read_only=False)

        self.assertEqual(attempts.read_text(), "1")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error, "codex_thread_lost")
        self.assertIsNone(result.codex_thread_fallback)

    def test_read_only_call_thread_loss_retries_once(self):
        attempts, result = self._run_call(read_only=True)

        self.assertEqual(attempts.read_text(), "2")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.text, "ok")
        self.assertFalse(result.codex_thread_fallback["engaged"])
        self.assertTrue(result.codex_thread_fallback["resolved"])


if __name__ == "__main__":
    unittest.main()
