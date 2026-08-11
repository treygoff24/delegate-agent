import contextlib
import datetime
import gc
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
CLI_PATH = ROOT / "bin" / "delegate.py"
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
RUNNER_PATH = ROOT / "src" / "delegate_agent" / "runner.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"
RUN_OUTPUT_PATH = ROOT / "src" / "delegate_agent" / "run_output_commands.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import mail  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_streaming_fake_bin():
    temp = tempfile.TemporaryDirectory()
    script = Path(temp.name) / "droid"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' \'{"type":"message","role":"assistant","content":"HELLO"}\'\n'
        'printf \'%s\\n\' \'{"type":"completion","finalText":"DONE"}\' >&1\n'
        "printf 'ERR:noise\\n' >&2\n"
        'exit "${FAKE_EXIT:-0}"\n'
    )
    script.chmod(0o755)
    return temp, script.parent


@contextlib.contextmanager
def open_pipe_as_process_stdin():
    saved_stdin = os.dup(0)
    read_fd, write_fd = os.pipe()
    try:
        os.dup2(read_fd, 0)
        os.close(read_fd)
        yield
    finally:
        os.dup2(saved_stdin, 0)
        for fd in (saved_stdin, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def write_stdin_probe_script(path: Path) -> None:
    path.write_text(
        "import select\n"
        "import sys\n"
        "from pathlib import Path\n"
        "ready, _, _ = select.select([sys.stdin], [], [], 0)\n"
        "if ready:\n"
        "    data = sys.stdin.read(1)\n"
        "    status = 'stdin:eof' if data == '' else 'stdin:data'\n"
        "else:\n"
        "    status = 'stdin:blocked'\n"
        "if len(sys.argv) > 1:\n"
        "    Path(sys.argv[1]).write_text(status, encoding='utf-8')\n"
        "else:\n"
        "    print(status)\n",
        encoding="utf-8",
    )


class RunnerCaptureTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module(RUNNER_PATH, "delegate_runner_under_test")
        self.registry = load_module(REGISTRY_PATH, "delegate_registry_runner_test")
        self.run_output = load_module(RUN_OUTPUT_PATH, "delegate_run_output_under_test")

    def test_execute_call_bounds_plain_stdout_fallback_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "plain_stdout.py"
            raw_len = self.runner.harness_events.ASSISTANT_TEXT_LIMIT + 1000
            script.write_text(
                f"import sys\nsys.stdout.write('x' * {raw_len})\n",
                encoding="utf-8",
            )
            result = self.runner.execute_call(
                [sys.executable, str(script)],
                tmp,
                harness="cursor",
            )
        self.assertEqual(result.exit_code, 0)
        self.assertLess(len(result.text), raw_len)
        self.assertIn("chars omitted", result.text)

    def test_execute_call_suppresses_structured_noise_without_assistant_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "events_only.py"
            script.write_text(
                'print(\'{"type":"thought","data":"internal"}\')\n',
                encoding="utf-8",
            )
            result = self.runner.execute_call(
                [sys.executable, str(script)],
                tmp,
                harness="grok",
            )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.text, "")
        self.assertEqual(result.text_chars, 0)
        self.assertTrue(any("no assistant text" in warning for warning in result.warnings))

    def test_execute_call_captures_redacted_stderr_tail_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fail.py"
            script.write_text(
                "import sys\n"
                "sys.stderr.write('Authorization: Bearer abcdefghijklmnop\\n')\n"
                "raise SystemExit(9)\n",
                encoding="utf-8",
            )
            result = self.runner.execute_call(
                [sys.executable, str(script)],
                tmp,
                harness="codex",
            )
        self.assertEqual(result.exit_code, 9)
        self.assertIn("Authorization: ***", result.stderr_tail)
        self.assertNotIn("abcdefghijklmnop", result.stderr_tail)

    def test_write_stdin_records_delivery_failure(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        pipe = os.fdopen(write_fd, "wb")
        failures: list[str] = []
        self.runner._write_stdin(pipe, "x" * 65536, failures)
        self.assertEqual(len(failures), 1)
        self.assertIn("stdin prompt delivery", failures[0])

    def test_tracked_run_surfaces_stdin_delivery_failure(self):
        # Child closes stdin without reading the prompt: the run must warn
        # instead of silently proceeding as if delivery succeeded.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "closer"
        script.write_text(
            "#!/usr/bin/env bash\nexec 0<&-\nsleep 0.2\nexit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            stderr = io.StringIO()
            code, _payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=stderr,
                stdin_text="x" * (1 << 20),
            )
            self.assertEqual(code, 0)
            self.assertIn("stdin prompt delivery", stderr.getvalue())
            snapshot = json.loads(
                (root / "runs" / run_id / "snapshot.json").read_text(encoding="utf-8")
            )
            warnings = snapshot.get("warnings", [])
            self.assertTrue(any("stdin prompt delivery" in w for w in warnings))

    def test_tracked_launch_error_records_failed_state(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            missing = str(Path(workspace) / "missing-agent")

            with self.assertRaises(self.runner.RunnerLaunchError) as caught:
                self.runner.execute_tracked(
                    [missing],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(caught.exception.error, "child_launch_failed")
            run_path = root / "runs" / run_id
            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error"], "child_launch_failed")
            self.assertIn("missing-agent", state["message"])
            self.assertFalse(snapshot["ok"])
            self.assertEqual(snapshot["status"], "failed")
            self.assertEqual(snapshot["error"], "child_launch_failed")

    def test_terminal_launch_failure_cleans_mail_push_private_home(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            source_home = Path(workspace) / "source-codex-home"
            source_home.mkdir()
            (source_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
            env = {"CODEX_HOME": str(source_home)}
            provision = mail.provision_mail_push(
                "codex", ["codex", "exec", "prompt"], None, root, run_id, env
            )
            private_home = Path(provision.codex_home or "")
            self.assertTrue(private_home.is_dir())
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="work",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                env_overrides=env,
                mail_push=True,
            )

            with self.assertRaises(self.runner.RunnerLaunchError):
                self.runner.execute_tracked(
                    [str(Path(workspace) / "missing-agent")],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertFalse(private_home.exists())

    def test_prepare_failure_cleans_mail_push_private_home_and_records_cleanup_warning(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            source_home = Path(workspace) / "source-codex-home"
            source_home.mkdir()
            (source_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
            env = {"CODEX_HOME": str(source_home)}
            provision = mail.provision_mail_push(
                "codex", ["codex", "exec", "prompt"], None, root, run_id, env
            )
            private_home = Path(provision.codex_home or "")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="work",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                env_overrides=env,
                mail_push=True,
            )
            original_cleanup = mail.cleanup_mail_push_private_homes

            def cleanup_then_fail(registry_root: Path, cleanup_run_id: str) -> None:
                original_cleanup(registry_root, cleanup_run_id)
                raise OSError("injected cleanup failure")

            with (
                mock.patch.object(
                    self.runner, "_prepare_tracked_run", side_effect=OSError("injected")
                ),
                mock.patch.object(
                    mail, "cleanup_mail_push_private_homes", side_effect=cleanup_then_fail
                ),
                self.assertRaises(OSError),
            ):
                self.runner.execute_tracked(
                    ["codex", "exec", "prompt"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertFalse(private_home.exists())
            state = json.loads((root / "runs" / run_id / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(
                any(
                    self.runner.MAIL_PUSH_CLEANUP_WARNING in warning
                    for warning in state["warnings"]
                )
            )

    def test_passthrough_launch_error_uses_runner_error(self):
        with tempfile.TemporaryDirectory() as workspace:
            missing = str(Path(workspace) / "missing-agent")

            with self.assertRaises(self.runner.RunnerLaunchError) as caught:
                self.runner.execute_passthrough([missing], workspace)

        self.assertEqual(caught.exception.error, "child_launch_failed")
        self.assertIn("missing-agent", caught.exception.message)

    def test_tracked_run_writes_logs_and_bounded_json(self):
        temp, bin_dir = make_streaming_fake_bin()
        self.addCleanup(temp.cleanup)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            argv = [str(bin_dir / "droid"), "exec", "hello"]
            env_path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": env_path}):
                code, payload = self.runner.execute_tracked(
                    argv,
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["alias"], alias)
            self.assertEqual(payload["runId"], run_id)
            self.assertEqual(
                payload["snapshotCommand"],
                f"delegate --cwd {workspace} snapshot {alias}",
            )
            self.assertEqual(
                payload["completionReportCommand"],
                f"delegate --cwd {workspace} run-output {alias} --completion-report",
            )
            self.assertEqual(payload["exitCode"], 0)
            self.assertNotIn("stdout", payload)
            self.assertNotIn("stderr", payload)
            run_path = self.registry.run_directory(root, run_id)
            self.assertTrue((run_path / "stdout.log").exists())
            self.assertTrue((run_path / "stderr.log").exists())
            stdout_text = (run_path / "stdout.log").read_text()
            self.assertIn("HELLO", stdout_text)
            self.assertNotIn("OUT:", stdout_text)
            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            self.assertIsInstance(state.get("pid"), int)
            self.assertEqual(state.get("pgid"), state.get("pid"))

    def test_tracked_events_jsonl_truncation_metadata_and_final_line(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "child.py"
        limit = self.runner.harness_events.EVENT_TEXT_LIMIT
        long_body = "L" * (limit + 42)
        secret = "sk-" + "abcdef1234567890"
        script.write_text(
            "import sys\n"
            f"print('short-ok')\n"
            f"print({long_body!r})\n"
            f"print('token {secret} visible')\n"
            "sys.stdout.write('unterminated-final')\n"
            "sys.stdout.flush()\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [sys.executable, str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0, payload)
            run_path = self.registry.run_directory(root, run_id)
            events = [
                json.loads(line)
                for line in (run_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            stream_lines = [event for event in events if event.get("kind") == "stream.line"]
            texts = [event.get("text") for event in stream_lines]
            self.assertIn("short-ok", texts)
            self.assertIn("unterminated-final", texts)
            short_event = next(event for event in stream_lines if event.get("text") == "short-ok")
            self.assertNotIn("truncated", short_event)
            self.assertNotIn("textChars", short_event)
            long_event = next(
                event
                for event in stream_lines
                if isinstance(event.get("text"), str) and event["text"].startswith("L")
            )
            self.assertTrue(long_event["truncated"])
            self.assertEqual(long_event["textChars"], len(long_body))
            self.assertEqual(long_event["text"], long_body[: limit - 1] + "…")
            secret_event = next(event for event in stream_lines if secret in str(event.get("text")))
            self.assertIn(secret, secret_event["text"])

            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            recent = snapshot.get("recentEvents") or []
            short_recent = next(
                event
                for event in recent
                if event.get("kind") == "text" and event.get("message") == "short-ok"
            )
            self.assertNotIn("truncated", short_recent)
            self.assertNotIn("textChars", short_recent)
            long_recent = next(
                event
                for event in recent
                if event.get("kind") == "text"
                and isinstance(event.get("message"), str)
                and event["message"].startswith("L")
            )
            self.assertTrue(long_recent["truncated"])
            self.assertEqual(long_recent["textChars"], len(long_body))
            self.assertEqual(long_recent["message"], long_body[: limit - 1] + "…")
            secret_recent = next(
                event
                for event in recent
                if event.get("kind") == "text" and secret in str(event.get("message"))
            )
            self.assertIn(secret, secret_recent["message"])
            self.assertTrue(any(event.get("message") == "unterminated-final" for event in recent))

    def test_housekeeping_completion_report_is_classified_from_disk(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "droid"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' \'{"type":"message","role":"assistant","content":"Plan is up-to-date."}\'\n'
            'printf \'%s\\n\' \'{"type":"completion","finalText":"Plan is up-to-date."}\'\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )

            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(code, 0)
            assert payload is not None
            self.assertEqual(payload["resultQuality"], "housekeeping_noop")
            self.assertTrue(payload["completionReportWritten"])
            self.assertEqual(payload["completionReportSource"], "child")
            self.assertTrue(any("Droid no-op" in warning for warning in payload["warnings"]))
            snapshot = json.loads((root / "runs" / run_id / "snapshot.json").read_text())
            self.assertEqual(snapshot["resultQuality"], "housekeeping_noop")

            out = io.StringIO()
            command = self.run_output.RunOutputCommand(
                alias,
                json_mode=True,
                completion_report=True,
            )
            self.assertEqual(
                self.run_output.emit(command, workspace_path=workspace, stdout=out),
                0,
            )
            run_output_payload = json.loads(out.getvalue())
            section = run_output_payload["sections"]["completionReport"]
            self.assertEqual(section["resultQuality"], "housekeeping_noop")
            self.assertEqual(section["completionReportSource"], "child")

    def test_failed_run_synthesizes_completion_report_and_auth_reason(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'Error 401: token_expired; refresh token was revoked; sk-secret123456\\n' >&2\n"
            "exit 7\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )

            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(code, 7)
            assert payload is not None
            self.assertEqual(payload["failureReason"], "auth_failed")
            self.assertEqual(payload["error"], "auth_failed")
            self.assertIn("token", payload["message"].lower())
            self.assertTrue(payload["completionReportWritten"])
            self.assertEqual(payload["completionReportSource"], "delegate_synthesized")
            report = (root / "runs" / run_id / "completion-report.md").read_text(encoding="utf-8")
            self.assertIn("Synthesized by delegate", report)
            self.assertIn("Failure reason: auth_failed", report)
            self.assertIn("delegate profiles", report)
            self.assertIn("codex login", report)
            self.assertNotIn("sk-secret123456", report)

            out = io.StringIO()
            command = self.run_output.RunOutputCommand(
                alias,
                json_mode=True,
                completion_report=True,
            )
            self.assertEqual(
                self.run_output.emit(command, workspace_path=workspace, stdout=out),
                0,
            )
            section = json.loads(out.getvalue())["sections"]["completionReport"]
            self.assertEqual(section["completionReportSource"], "delegate_synthesized")
            self.assertIn("Synthesized by delegate", section["content"])

    def test_grok_cancelled_terminal_event_overrides_exit_zero(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "grok"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' \'{"type":"text","data":"partial"}\'\n'
            'printf \'%s\\n\' \'{"type":"end","stopReason":"Cancelled"}\'\n'
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="grok")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="grok",
                engine="grok",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 1)
            assert payload is not None
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "cancelled")
            self.assertEqual(payload["terminalStatus"], "cancelled")
            self.assertEqual(payload["failureReason"], "harness_cancelled")
            state = json.loads((root / "runs" / run_id / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "cancelled")
            self.assertEqual(state["terminalStatus"], "cancelled")

    def test_cancelled_run_synthesizes_completion_report_readable_via_run_output(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "grok"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' \'{"type":"text","data":"partial"}\'\n'
            'printf \'%s\\n\' \'{"type":"end","stopReason":"Cancelled"}\'\n'
            "printf 'cancel-noise-stderr\\n' >&2\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="grok")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="grok",
                engine="grok",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 1)
            assert payload is not None
            self.assertEqual(payload["status"], "cancelled")
            self.assertTrue(payload["completionReportWritten"])
            self.assertEqual(payload["completionReportSource"], "delegate_synthesized")
            report = (root / "runs" / run_id / "completion-report.md").read_text(encoding="utf-8")
            self.assertIn("Synthesized by delegate", report)
            self.assertIn("Status: cancelled", report)
            self.assertIn("Failure reason: harness_cancelled", report)
            self.assertIn("run-output", report)
            self.assertIn("partial output", report)

            out = io.StringIO()
            command = self.run_output.RunOutputCommand(
                alias,
                json_mode=True,
                completion_report=True,
            )
            self.assertEqual(
                self.run_output.emit(command, workspace_path=workspace, stdout=out),
                0,
            )
            section = json.loads(out.getvalue())["sections"]["completionReport"]
            self.assertEqual(section["completionReportSource"], "delegate_synthesized")
            self.assertIn("Status: cancelled", section["content"])

    def test_progress_persist_preserves_cancel_marker(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            run_path = self.registry.run_directory(root, run_id)
            requested_at = self.registry.utc_now_iso()
            self.registry.write_json_atomic(
                run_path / self.registry.STATE_FILE,
                {
                    "status": "running",
                    "cancelRequested": True,
                    "cancelRequestedAt": requested_at,
                },
            )
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at=requested_at,
            )
            accumulator = self.runner.harness_events.StreamAccumulator(harness="codex")

            self.runner.persist_progress(
                run_path,
                ctx,
                accumulator,
                status="running",
                pid=os.getpid(),
                stdout_bytes=7,
            )

            state = json.loads((run_path / self.registry.STATE_FILE).read_text(encoding="utf-8"))
            snapshot = json.loads(
                (run_path / self.registry.SNAPSHOT_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "running")
            self.assertTrue(state["cancelRequested"])
            self.assertEqual(state["cancelRequestedAt"], requested_at)
            self.assertEqual(snapshot["status"], "running")
            self.assertTrue(snapshot["cancelRequested"])
            self.assertEqual(snapshot["cancelRequestedAt"], requested_at)

    def test_progress_persist_never_downgrades_terminal_state_or_snapshot(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            run_path = self.registry.run_directory(root, run_id)
            terminal_state = {
                "status": "cancelled",
                "exitCode": 1,
                "failureReason": "cancelled_by_user",
            }
            terminal_snapshot = {
                "status": "cancelled",
                "exitCode": 1,
                "failureReason": "cancelled_by_user",
                "ok": False,
            }
            self.registry.write_json_atomic(run_path / self.registry.STATE_FILE, terminal_state)
            self.registry.write_json_atomic(
                run_path / self.registry.SNAPSHOT_FILE, terminal_snapshot
            )
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at=self.registry.utc_now_iso(),
            )

            self.runner.persist_progress(
                run_path,
                ctx,
                self.runner.harness_events.StreamAccumulator(harness="codex"),
                status="running",
                pid=os.getpid(),
                stdout_bytes=99,
            )

            self.assertEqual(
                json.loads((run_path / self.registry.STATE_FILE).read_text(encoding="utf-8")),
                terminal_state,
            )
            self.assertEqual(
                json.loads((run_path / self.registry.SNAPSHOT_FILE).read_text(encoding="utf-8")),
                terminal_snapshot,
            )

    def test_finalize_first_marker_race_envelope_and_state_both_cancelled(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            run_path = self.registry.run_directory(root, run_id)
            state = {
                "schema": self.registry.STATE_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "status": "running",
                "lastActivityAt": self.registry.utc_now_iso(),
                "pid": os.getpid(),
                "pgid": os.getpgid(0),
                "cancelRequested": True,
                "cancelRequestedAt": self.registry.utc_now_iso(),
            }
            self.registry.write_json_atomic(run_path / self.registry.STATE_FILE, state)
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            accumulator = self.runner.harness_events.StreamAccumulator(harness="codex")
            files = self.runner.TrackedRunFiles(
                run_path=run_path,
                stdout_log=run_path / self.registry.STDOUT_LOG,
                stderr_log=run_path / self.registry.STDERR_LOG,
            )
            # Child exited 0, but cancelRequested is stamped.
            capture = self.runner.TrackedCaptureResult(
                accumulator=accumulator,
                exit_code=0,
                duration_ms=100,
                stdout_bytes=10,
                stderr_bytes=5,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=os.getpgid(0),
            )
            finalization = self.runner._finalize_tracked_run(
                files,
                ctx,
                capture,
                completion_report_mode="off",
            )
            # The LIVE envelope must agree with the persisted state.
            self.assertEqual(finalization.status, "cancelled")
            self.assertEqual(finalization.exit_code, 1)
            self.assertEqual(finalization.extra.get("failureReason"), "cancelled_by_user")
            ok = finalization.exit_code == 0
            self.assertFalse(ok, "finalize-first with cancelRequested and exit 0 must be ok=False")
            # A synthesized cancelled report must have been written.
            self.assertTrue(finalization.report_written)
            self.assertEqual(
                finalization.extra.get("completionReportSource"), "delegate_synthesized"
            )
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            self.assertIn("Status: cancelled", report)
            self.assertIn("cancelled_by_user", report)
            # Persisted state must be cancelled/exitCode 1.
            persisted = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "cancelled")
            self.assertEqual(persisted["failureReason"], "cancelled_by_user")
            self.assertEqual(persisted.get("exitCode"), 1)

    def test_timeout_with_cancel_marker_returns_cancelled_without_timeout_error(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            run_path = self.registry.run_directory(root, run_id)
            self.registry.write_json_atomic(
                run_path / self.registry.STATE_FILE,
                {
                    "schema": self.registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": "running",
                    "cancelRequested": True,
                },
            )
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            capture = self.runner.TrackedCaptureResult(
                accumulator=self.runner.harness_events.StreamAccumulator(harness="codex"),
                exit_code=1,
                duration_ms=1000,
                stdout_bytes=0,
                stderr_bytes=0,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=os.getpgid(0),
                error="call_timeout",
                message="Child command exceeded the configured timeout.",
            )

            with mock.patch.object(
                self.runner,
                "_run_single_tracked_attempt",
                return_value=capture,
            ):
                code, payload = self.runner.execute_tracked(
                    [sys.executable, "-c", "pass"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    timeout=1,
                )

            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "cancelled")
            self.assertEqual(payload["failureReason"], "cancelled_by_user")
            self.assertEqual(payload["error"], "cancelled_by_user")
            self.assertEqual(payload["message"], "Run was cancelled.")
            for name in ("state.json", "snapshot.json"):
                persisted = json.loads((run_path / name).read_text(encoding="utf-8"))
                self.assertEqual(persisted["status"], "cancelled")
                self.assertEqual(persisted["failureReason"], "cancelled_by_user")
                self.assertNotIn("error", persisted)
                self.assertNotIn("message", persisted)
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            self.assertIn("Status: cancelled", report)
            self.assertIn("Failure reason: cancelled_by_user", report)
            self.assertNotIn("Status: failed", report)
            self.assertNotIn("call_timeout", report)

    def test_timeout_late_cancel_reconciliation_clears_timeout_error(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            run_path = self.registry.run_directory(root, run_id)
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            capture = self.runner.TrackedCaptureResult(
                accumulator=self.runner.harness_events.StreamAccumulator(harness="codex"),
                exit_code=1,
                duration_ms=1000,
                stdout_bytes=0,
                stderr_bytes=0,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=os.getpgid(0),
                error="call_timeout",
                message="Child command exceeded the configured timeout.",
            )
            running = {"status": "running"}
            cancel_requested = {"status": "running", "cancelRequested": True}

            with mock.patch.object(
                self.runner.run_registry,
                "load_run_state_or_none",
                side_effect=(running, cancel_requested, cancel_requested),
            ):
                finalization = self.runner._finalize_tracked_run(
                    self.runner.TrackedRunFiles(
                        run_path=run_path,
                        stdout_log=run_path / self.registry.STDOUT_LOG,
                        stderr_log=run_path / self.registry.STDERR_LOG,
                    ),
                    ctx,
                    capture,
                    completion_report_mode="off",
                    extra={
                        "error": capture.error,
                        "message": capture.message,
                        "failureReason": capture.error,
                    },
                )

            self.assertEqual(finalization.status, "cancelled")
            self.assertEqual(finalization.exit_code, 1)
            self.assertEqual(finalization.extra["failureReason"], "cancelled_by_user")
            self.assertNotIn("error", finalization.extra)
            self.assertNotIn("message", finalization.extra)
            for name in ("state.json", "snapshot.json"):
                persisted = json.loads((run_path / name).read_text(encoding="utf-8"))
                self.assertEqual(persisted["status"], "cancelled")
                self.assertEqual(persisted["failureReason"], "cancelled_by_user")
                self.assertNotIn("error", persisted)
                self.assertNotIn("message", persisted)
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            self.assertIn("Status: cancelled", report)
            self.assertIn("Failure reason: cancelled_by_user", report)
            self.assertNotIn("Status: failed", report)
            self.assertNotIn("call_timeout", report)

    def test_tracked_run_gives_child_eof_stdin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_probe.py"
        write_stdin_probe_script(script)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            with open_pipe_as_process_stdin():
                code, _payload = self.runner.execute_tracked(
                    [sys.executable, str(script)],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
            self.assertIn("stdin:eof", stdout_text)
            self.assertNotIn("stdin:blocked", stdout_text)

    def test_tracked_run_can_deliver_prompt_via_stdin_without_manifest_leak(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_echo.py"
        script.write_text(
            "import sys\ndata = sys.stdin.read()\nprint('STDIN:' + data)\n",
            encoding="utf-8",
        )
        secret_prompt = "TOP-SECRET-STDIN-PROMPT"
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                prompt_transport="stdin",
            )
            code, _payload = self.runner.execute_tracked(
                [sys.executable, str(script), "-"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                stdin_text=secret_prompt,
                manifest_argv=[sys.executable, str(script), "<prompt via stdin>"],
            )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
            self.assertIn(secret_prompt, stdout_text)
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["promptTransport"], "stdin")
            self.assertNotIn(secret_prompt, json.dumps(manifest["argv"]))

    def test_tracked_run_can_deliver_prompt_via_private_file_without_manifest_leak(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "prompt_file_reader.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "prompt_path = Path(sys.argv[sys.argv.index('--file') + 1])\n"
            "print('PROMPT_FILE:' + str(prompt_path))\n"
            "print('PROMPT:' + prompt_path.read_text(encoding='utf-8'))\n",
            encoding="utf-8",
        )
        secret_prompt = "TOP-SECRET-FILE-PROMPT"
        placeholder = "<delegate-prompt-file>"
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                prompt_transport="file",
            )
            code, _payload = self.runner.execute_tracked(
                [sys.executable, str(script), "--file", placeholder],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                prompt_file_text=secret_prompt,
                prompt_file_placeholder=placeholder,
                manifest_argv=[sys.executable, str(script), "--file", "<prompt file>"],
            )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
            self.assertIn(secret_prompt, stdout_text)
            prompt_file_line = next(
                line.removeprefix("PROMPT_FILE:")
                for line in stdout_text.splitlines()
                if line.startswith("PROMPT_FILE:")
            )
            self.assertFalse(Path(prompt_file_line).exists())
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["promptTransport"], "file")
            self.assertNotIn(secret_prompt, json.dumps(manifest["argv"]))

    def test_tracked_devin_materializes_agent_config_without_manifest_leak(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "devin_reader.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "prompt_path = Path(sys.argv[sys.argv.index('--prompt-file') + 1])\n"
            "agent_config_path = Path(sys.argv[sys.argv.index('--agent-config') + 1])\n"
            "print('PROMPT_FILE:' + str(prompt_path))\n"
            "print('AGENT_CONFIG_FILE:' + str(agent_config_path))\n"
            "print('PROMPT:' + prompt_path.read_text(encoding='utf-8'))\n"
            "print('AGENT_CONFIG:' + agent_config_path.read_text(encoding='utf-8'))\n",
            encoding="utf-8",
        )
        secret_prompt = "TOP-SECRET-DEVIN-PROMPT"
        agent_config_text = '{"permissions":{"allow":["read"],"deny":["edit"]}}'
        prompt_placeholder = "<delegate-prompt-file>"
        agent_config_placeholder = "<delegate-devin-agent-config>"
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="devin")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="devin",
                engine="devin",
                mode="safe",
                model="swe-1.7",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                prompt_transport="file",
            )
            code, _payload = self.runner.execute_tracked(
                [
                    sys.executable,
                    str(script),
                    "--agent-config",
                    agent_config_placeholder,
                    "--prompt-file",
                    prompt_placeholder,
                    "-p",
                ],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                prompt_file_text=secret_prompt,
                prompt_file_placeholder=prompt_placeholder,
                agent_config_text=agent_config_text,
                agent_config_placeholder=agent_config_placeholder,
                manifest_argv=[
                    sys.executable,
                    str(script),
                    "--agent-config",
                    "<devin agent config>",
                    "--prompt-file",
                    "<prompt file>",
                    "-p",
                ],
            )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
            self.assertIn(secret_prompt, stdout_text)
            self.assertIn(agent_config_text, stdout_text)
            prompt_file_line = next(
                line.removeprefix("PROMPT_FILE:")
                for line in stdout_text.splitlines()
                if line.startswith("PROMPT_FILE:")
            )
            agent_config_file_line = next(
                line.removeprefix("AGENT_CONFIG_FILE:")
                for line in stdout_text.splitlines()
                if line.startswith("AGENT_CONFIG_FILE:")
            )
            self.assertFalse(Path(prompt_file_line).exists())
            self.assertTrue(Path(agent_config_file_line).exists())
            self.assertEqual(Path(agent_config_file_line).parent, run_path)
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["promptTransport"], "file")
            self.assertNotIn(secret_prompt, json.dumps(manifest["argv"]))
            self.assertNotIn(agent_config_text, json.dumps(manifest["argv"]))

    def test_tracked_codex_item_completed_writes_completion_report(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"I am working"}}\'\n'
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"reasoning","text":"hidden reasoning"}}\'\n'
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"Status: completed\\\\n- final from codex"}}\'\n'
            "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, _payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertIn("final from codex", report)
            self.assertNotIn("I am working", report)
            self.assertIn("I am working", snapshot["assistantText"])
            self.assertNotIn("hidden reasoning", snapshot["assistantText"])

    def test_tracked_kimi_role_content_stream_writes_snapshot_and_completion_report(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "kimi"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf \'%s\\n\' \'{"role":"assistant","content":"Status: completed\\\\n- final from kimi"}\'\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="kimi")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="kimi",
                engine="kimi",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertIn("completionReportCommand", payload)
            run_path = self.registry.run_directory(root, run_id)
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertIn("final from kimi", report)
            self.assertIn("final from kimi", snapshot["assistantText"])

    def test_tracked_opencode_fixtures_write_reports_without_no_assistant_text(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "opencode_fixture.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.stdout.write(Path(sys.argv[1]).read_text(encoding='utf-8'))\n",
            encoding="utf-8",
        )
        fixture_dir = ROOT / "tests" / "fixtures" / "opencode"
        for fixture_name, expected in (
            ("simple_text.ndjson", "pong"),
            ("tool_run.ndjson", "banana42"),
        ):
            with self.subTest(fixture=fixture_name), tempfile.TemporaryDirectory() as workspace:
                root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
                run_id, alias = self.registry.register_run(root, harness="opencode")
                ctx = self.runner.RunContext(
                    registry_root=root,
                    run_id=run_id,
                    alias=alias,
                    harness="opencode",
                    engine="opencode",
                    mode="safe",
                    model=None,
                    source_cwd=workspace,
                    execution_cwd=workspace,
                    workspace_kind="directory",
                    isolated_workspace=False,
                    started_at="2026-05-20T21:42:33Z",
                )
                code, payload = self.runner.execute_tracked(
                    [sys.executable, str(script), str(fixture_dir / fixture_name)],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
                self.assertEqual(code, 0)
                assert payload is not None
                self.assertEqual(payload["completionReportSource"], "child")
                self.assertNotEqual(payload["resultQuality"], "no_assistant_text")
                run_path = self.registry.run_directory(root, run_id)
                report = (run_path / "completion-report.md").read_text(encoding="utf-8")
                snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
                self.assertIn(expected, report)
                self.assertIn(expected, snapshot["assistantText"])

    def test_tracked_pi_fixture_populates_json_envelope_assistant_text(self):
        fixture = ROOT / "tests" / "fixtures" / "pi" / "simple_text.jsonl"
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            for mode in ("safe", "work"):
                with self.subTest(mode=mode):
                    run_id, alias = self.registry.register_run(root, harness="pi")
                    ctx = self.runner.RunContext(
                        registry_root=root,
                        run_id=run_id,
                        alias=alias,
                        harness="pi",
                        engine="pi",
                        mode=mode,
                        model=None,
                        source_cwd=workspace,
                        execution_cwd=workspace,
                        workspace_kind="directory",
                        isolated_workspace=False,
                        started_at="2026-05-20T21:42:33Z",
                    )
                    code, payload = self.runner.execute_tracked(
                        [
                            sys.executable,
                            "-c",
                            "import sys; print(open(sys.argv[1]).read(), end='')",
                            str(fixture),
                        ],
                        workspace,
                        ctx,
                        json_mode=True,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

                    self.assertEqual(code, 0)
                    assert payload is not None
                    self.assertIn("PI_OK", payload["assistantText"])
                    self.assertNotEqual(payload["resultQuality"], "no_assistant_text")

    def test_pi_call_fixture_returns_assistant_text(self):
        fixture = ROOT / "tests" / "fixtures" / "pi" / "simple_text.jsonl"
        with tempfile.TemporaryDirectory() as workspace:
            result = self.runner.execute_call(
                [
                    sys.executable,
                    "-c",
                    "import sys; print(open(sys.argv[1]).read(), end='')",
                    str(fixture),
                ],
                workspace,
                harness="pi",
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("PI_OK", result.text)
        self.assertNotEqual(result.result_quality, "no_assistant_text")

    def test_omp_synthetic_argv_stream_populates_assistant_text_in_all_modes(self):
        stream = "\n".join(
            (
                json.dumps({"type": "turn_start"}),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "OMP_OK"}],
                            "stopReason": "stop",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "OMP_OK"}],
                            "stopReason": "stop",
                        },
                    }
                ),
            )
        )
        command = [sys.executable, "-c", f"print({stream!r})"]
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            for mode in ("safe", "work"):
                with self.subTest(mode=mode):
                    run_id, alias = self.registry.register_run(root, harness="omp")
                    ctx = self.runner.RunContext(
                        registry_root=root,
                        run_id=run_id,
                        alias=alias,
                        harness="omp",
                        engine="omp",
                        mode=mode,
                        model=None,
                        source_cwd=workspace,
                        execution_cwd=workspace,
                        workspace_kind="directory",
                        isolated_workspace=False,
                        started_at="2026-07-18T21:00:00Z",
                    )
                    code, payload = self.runner.execute_tracked(
                        command,
                        workspace,
                        ctx,
                        json_mode=True,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )
                    self.assertEqual(code, 0)
                    assert payload is not None
                    self.assertEqual(payload["assistantText"], "OMP_OK")

            result = self.runner.execute_call(command, workspace, harness="omp")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.text, "OMP_OK")

    def test_tracked_codex_progress_message_before_command_does_not_write_report(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\n' '{\"type\":\"turn.started\"}'\n"
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"I will inspect the repo first"}}\'\n'
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"command_execution","command":"pwd","status":"completed"}}\'\n'
            "printf '%s\n' '{\"type\":\"turn.completed\"}'\n"
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertNotIn("completionReportCommand", payload)
            self.assertNotIn("completionReportPath", payload)
            run_path = self.registry.run_directory(root, run_id)
            self.assertFalse((run_path / "completion-report.md").exists())
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertNotIn("completionReport", snapshot)
            self.assertIn("I will inspect the repo first", snapshot["assistantText"])

    def test_tracked_text_output_omits_raw_streams(self):
        temp, bin_dir = make_streaming_fake_bin()
        self.addCleanup(temp.cleanup)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            [str(bin_dir / "droid"), "exec", "hello"]
            env_path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": env_path}):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(CLI_PATH),
                        "--cwd",
                        workspace,
                        "droid",
                        "minimax",
                        "safe",
                        "hello",
                    ],
                    text=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "PATH": env_path,
                        "DELEGATE_CONFIG": str(self._config_path(workspace)),
                    },
                    check=False,
                )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("alias:", completed.stdout)
            self.assertNotIn("OUT:", completed.stdout)
            self.assertNotIn("ERR:", completed.stdout)
            self.assertEqual(completed.stderr, "")

    def _config_path(self, workspace: str) -> str:
        path = Path(workspace) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "droid": {
                        "binary": "droid",
                        "models": {"minimax": "model-id"},
                    }
                }
            )
        )
        return str(path)

    def test_pass_through_cli_preserves_raw_output(self):
        repo_temp = tempfile.TemporaryDirectory()
        subprocess.run(
            ["git", "-C", repo_temp.name, "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(repo_temp.cleanup)
        temp, bin_dir = make_streaming_fake_bin()
        self.addCleanup(temp.cleanup)
        fake = bin_dir / "droid"
        fake.write_text(
            "#!/usr/bin/env bash\nprintf 'OUT:raw\\n'\nprintf 'ERR:raw\\n' >&2\nexit 0\n"
        )
        fake.chmod(0o755)
        config = Path(repo_temp.name) / "config.json"
        config.write_text(
            json.dumps(
                {
                    "droid": {
                        "binary": "droid",
                        "models": {"minimax": "model-id"},
                    }
                }
            )
        )
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--pass-through",
                "--cwd",
                repo_temp.name,
                "droid",
                "minimax",
                "safe",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("OUT:raw", completed.stdout)
        self.assertIn("ERR:raw", completed.stderr)

    def test_passthrough_run_gives_child_eof_stdin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_probe.py"
        write_stdin_probe_script(script)
        with tempfile.TemporaryDirectory() as workspace:
            marker = Path(workspace) / "stdin-state.txt"
            with open_pipe_as_process_stdin():
                code = self.runner.execute_passthrough(
                    [sys.executable, str(script), str(marker)],
                    workspace,
                )
            self.assertEqual(code, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "stdin:eof")

    def test_passthrough_run_can_deliver_prompt_via_stdin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_writer.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as workspace:
            marker = Path(workspace) / "stdin-prompt.txt"
            code = self.runner.execute_passthrough(
                [sys.executable, str(script), str(marker)],
                workspace,
                stdin_text="PROMPT-VIA-STDIN",
            )
            self.assertEqual(code, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "PROMPT-VIA-STDIN")

    def test_completion_json_payload_always_includes_exit_code(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="alias-1",
            harness="droid",
            engine="droid",
            mode="safe",
            model="model-id",
            source_cwd="/tmp",
            execution_cwd="/tmp",
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-05-20T21:42:33Z",
        )
        payload = self.runner.completion_json_payload(
            ctx,
            ok=True,
            status="succeeded",
            exit_code=0,
            duration_ms=100,
            stdout_bytes=10,
            stderr_bytes=0,
        )
        self.assertEqual(payload["exitCode"], 0)

    def test_snapshot_prefers_effective_resolved_model_metadata(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="alias-1",
            harness="codex",
            engine="codex",
            mode="safe",
            model="requested-alias",
            model_resolved="effective-model",
            source_cwd="/tmp",
            execution_cwd="/tmp",
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-05-20T21:42:33Z",
        )
        snapshot = self.runner.build_snapshot(
            ctx,
            accumulator=self.runner.harness_events.StreamAccumulator(),
        )
        self.assertEqual(snapshot["modelResolved"], "effective-model")

    def test_persistent_worktree_completion_payload_includes_force_cleanup_command(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="cursor-1",
            harness="cursor",
            engine="cursor",
            mode="work",
            model="composer-2.5",
            source_cwd="/repo",
            execution_cwd="/wt",
            workspace_kind="git",
            isolated_workspace=True,
            started_at="2026-05-20T21:42:33Z",
            source_git_root="/repo",
            isolation_mode="worktree",
            effective_isolation="worktree",
            isolation_lifecycle="persistent",
            preserved_workspace=True,
            branch="delegate/cursor-1",
            worktree_status="present",
        )
        payload = self.runner.completion_json_payload(
            ctx,
            ok=True,
            status="succeeded",
            exit_code=0,
            duration_ms=100,
            stdout_bytes=0,
            stderr_bytes=0,
        )
        cleanup = payload["worktreeCleanupCommands"]
        self.assertEqual(cleanup["force"], "delegate worktree remove cursor-1 --force")

    def test_persistent_worktree_payload_and_snapshot_merge_ctx_and_extra_warnings(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="cursor-1",
            harness="cursor",
            engine="cursor",
            mode="work",
            model="composer-2.5",
            source_cwd="/repo",
            execution_cwd="/wt",
            workspace_kind="git",
            isolated_workspace=True,
            started_at="2026-05-20T21:42:33Z",
            source_git_root="/repo",
            isolation_lifecycle="persistent",
            branch="delegate/cursor-1",
            warnings=("ctx warning", "duplicate warning"),
        )
        extra = {"warnings": ["extra warning", "ctx warning"]}

        payload = self.runner.completion_json_payload(
            ctx,
            ok=True,
            status="succeeded",
            exit_code=0,
            duration_ms=100,
            stdout_bytes=0,
            stderr_bytes=0,
            extra=extra,
        )
        snapshot = self.runner.build_snapshot(
            ctx,
            accumulator=self.runner.harness_events.StreamAccumulator(),
            exit_code=0,
            extra=extra,
        )

        expected = ["ctx warning", "duplicate warning", "extra warning"]
        self.assertEqual(payload["warnings"], expected)
        self.assertEqual(snapshot["warnings"], expected)

    def test_work_summary_no_changes_becomes_top_level_warning(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="cursor-1",
            harness="cursor",
            engine="cursor",
            mode="work",
            model="composer-2.5",
            source_cwd="/repo",
            execution_cwd="/wt",
            workspace_kind="git",
            isolated_workspace=True,
            started_at="2026-05-20T21:42:33Z",
            source_git_root="/repo",
            isolation_lifecycle="persistent",
            branch="delegate/cursor-1",
        )
        with mock.patch.object(
            self.runner,
            "_persistent_work_summary",
            return_value={"noChanges": True, "commitsCreatedCount": 0},
        ):
            exit_code, extra = self.runner._final_extra(ctx, 0)

        self.assertEqual(exit_code, 0)
        self.assertIn("Work-mode run completed with no file changes", extra["warnings"][0])

    def test_forbid_commit_unverified_does_not_mask_child_failure(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="cursor-1",
            harness="cursor",
            engine="cursor",
            mode="work",
            model="composer-2.5",
            source_cwd="/repo",
            execution_cwd="/wt",
            workspace_kind="git",
            isolated_workspace=True,
            started_at="2026-05-20T21:42:33Z",
            source_git_root="/repo",
            isolation_lifecycle="persistent",
            branch="delegate/cursor-1",
            forbid_commit=True,
        )
        with mock.patch.object(
            self.runner,
            "_persistent_work_summary",
            return_value={"commitsCreatedCount": None, "commitInspectionStatus": "unverified"},
        ):
            exit_code, extra = self.runner._final_extra(ctx, 7)

        self.assertEqual(exit_code, 7)
        self.assertTrue(extra["commitPolicyUnverified"])
        self.assertFalse(extra["commitPolicy"]["verified"])
        self.assertIsNone(extra["commitPolicy"]["commitsCreatedCount"])
        self.assertEqual(extra["childExitCode"], 7)
        self.assertNotIn("commitPolicyCausedFailure", extra)
        self.assertNotIn("error", extra)
        payload = self.runner.completion_json_payload(
            ctx,
            ok=False,
            status="failed",
            exit_code=exit_code,
            duration_ms=100,
            stdout_bytes=0,
            stderr_bytes=0,
            extra=extra,
        )
        self.assertEqual(payload["error"], "child_failed")
        self.assertTrue(payload["commitPolicyUnverified"])
        self.assertIn("commitPolicy", payload)

    def test_forbid_commit_violation_after_child_success_causes_failure(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="cursor-1",
            harness="cursor",
            engine="cursor",
            mode="work",
            model="composer-2.5",
            source_cwd="/repo",
            execution_cwd="/wt",
            workspace_kind="git",
            isolated_workspace=True,
            started_at="2026-05-20T21:42:33Z",
            source_git_root="/repo",
            isolation_lifecycle="persistent",
            branch="delegate/cursor-1",
            forbid_commit=True,
        )
        with mock.patch.object(
            self.runner,
            "_persistent_work_summary",
            return_value={"commitsCreatedCount": 1, "commitInspectionStatus": "verified"},
        ):
            exit_code, extra = self.runner._final_extra(ctx, 0)

        self.assertEqual(exit_code, 1)
        self.assertTrue(extra["commitPolicyViolated"])
        self.assertTrue(extra["commitPolicyCausedFailure"])
        self.assertEqual(extra["error"], "commit_policy_violated")
        payload = self.runner.completion_json_payload(
            ctx,
            ok=False,
            status="failed",
            exit_code=exit_code,
            duration_ms=100,
            stdout_bytes=0,
            stderr_bytes=0,
            extra=extra,
        )
        self.assertEqual(payload["error"], "commit_policy_violated")
        self.assertTrue(payload["commitPolicyCausedFailure"])

    def test_forbid_commit_violation_does_not_mask_child_failure(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="cursor-1",
            harness="cursor",
            engine="cursor",
            mode="work",
            model="composer-2.5",
            source_cwd="/repo",
            execution_cwd="/wt",
            workspace_kind="git",
            isolated_workspace=True,
            started_at="2026-05-20T21:42:33Z",
            source_git_root="/repo",
            isolation_lifecycle="persistent",
            branch="delegate/cursor-1",
            forbid_commit=True,
        )
        with mock.patch.object(
            self.runner,
            "_persistent_work_summary",
            return_value={"commitsCreatedCount": 1, "commitInspectionStatus": "verified"},
        ):
            exit_code, extra = self.runner._final_extra(ctx, 7)

        self.assertEqual(exit_code, 7)
        self.assertTrue(extra["commitPolicyViolated"])
        self.assertNotIn("commitPolicyCausedFailure", extra)
        payload = self.runner.completion_json_payload(
            ctx,
            ok=False,
            status="failed",
            exit_code=exit_code,
            duration_ms=100,
            stdout_bytes=0,
            stderr_bytes=0,
            extra=extra,
        )
        self.assertEqual(payload["error"], "child_failed")

    def test_join_drain_thread_completes_after_pipe_drained(self):
        reader, writer = os.pipe()
        os.write(writer, b"line\n")
        os.close(writer)
        read_fd = os.fdopen(reader, "rb", closefd=True)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "stdout.log"
            byte_counter = self.runner.ByteCounter()
            thread = threading.Thread(
                target=self.runner._drain_stream,
                args=(read_fd, log_path, byte_counter),
                kwargs={
                    "on_line": None,
                    "max_bytes": self.runner.TRACKED_STREAM_MAX_BYTES,
                    "limit_signal": self.runner.StreamLimitSignal(),
                    "stream": "stdout",
                },
                daemon=True,
            )
            thread.start()
            self.runner._join_drain_thread(thread, read_fd)
            read_fd.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(byte_counter.total, len(b"line\n"))

    def test_drain_stream_preserves_utf8_across_read_chunks(self):
        payload = b"x" * (self.runner.STREAM_READ_CHUNK_BYTES - 1) + "😀\n".encode()
        decoded: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "stdout.log"
            counter = self.runner.ByteCounter()
            self.runner._drain_stream(
                io.BytesIO(payload),
                log_path,
                counter,
                on_line=decoded.append,
                max_bytes=self.runner.TRACKED_STREAM_MAX_BYTES,
                limit_signal=self.runner.StreamLimitSignal(),
                stream="stdout",
            )

            self.assertEqual(log_path.read_bytes(), payload)
        self.assertEqual("".join(decoded), payload.decode())
        self.assertNotIn("�", "".join(decoded))

    def test_tracked_run_batches_progress_persistence(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "droid"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "for i in $(seq 0 24); do\n"
            '  printf \'%s\\n\' "{\\"type\\":\\"message\\",\\"role\\":\\"assistant\\",\\"content\\":\\"line${i}\\"}"\n'
            "done\n"
            "exit 0\n"
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            with mock.patch.object(self.runner, "persist_progress") as persist_mock:
                self.runner.execute_tracked(
                    [str(script)],
                    workspace,
                    ctx,
                    json_mode=False,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            running_persists = [
                call
                for call in persist_mock.call_args_list
                if call.kwargs.get("status") == "running"
            ]
            self.assertGreater(len(running_persists), 0)
            self.assertLess(len(running_persists), 25)

    def test_running_persist_skips_when_no_new_lines_between_ticks(self):
        progress_dirty = True
        outcomes: list[str] = []

        def maybe_persist_running() -> None:
            nonlocal progress_dirty
            if not progress_dirty:
                outcomes.append("skipped")
                return
            progress_dirty = False
            outcomes.append("persisted")

        maybe_persist_running()
        maybe_persist_running()
        self.assertEqual(outcomes, ["persisted", "skipped"])

    def test_progress_current_label_redacts_secret_like_tool_targets(self):
        accumulator = self.runner.harness_events.StreamAccumulator()
        bearer_token = "bearer-output-" + "token-12345"
        accumulator.current = (
            f'running curl -H "Authorization: Bearer {bearer_token}" https://example.test'
        )

        label = self.runner._progress_current_label(accumulator)

        self.assertIn("Authorization: ***", label)
        self.assertNotIn(bearer_token, label)

    def test_progress_current_label_redacts_absolute_paths(self):
        accumulator = self.runner.harness_events.StreamAccumulator()
        accumulator.current = (
            "Bash cat /Users/alice/Code/client/.env /root/.ssh/id_rsa "
            "/workspace/client/.env /mnt/c/Users/dev/.env /app/x /nix/store/x"
        )

        label = self.runner._progress_current_label(accumulator)

        self.assertIn("<redacted-path>", label)
        self.assertNotIn("/Users/alice/Code/client/.env", label)
        self.assertNotIn("/root/.ssh/id_rsa", label)
        self.assertNotIn("/workspace/client/.env", label)
        self.assertNotIn("/mnt/c/Users/dev/.env", label)
        self.assertNotIn("/app/x", label)
        self.assertNotIn("/nix/store/x", label)

    def test_progress_current_label_preserves_urls_and_non_paths(self):
        from delegate_agent import redaction as redaction_mod

        for unchanged in (
            "Read src/delegate_agent/runner.py",
            "Edit tests/test_foo.py",
            "reviewing a/b and c/d",
        ):
            self.assertEqual(
                redaction_mod.redact_progress_label(unchanged),
                unchanged,
            )

        tilde_masked = redaction_mod.redact_progress_label("~/Code/foo")
        self.assertIn("<redacted-path>", tilde_masked)
        self.assertNotIn("~/Code", tilde_masked)
        self.assertNotIn("~<redacted-path>", tilde_masked)

        accumulator = self.runner.harness_events.StreamAccumulator()
        accumulator.current = (
            "fetch https://example.test and https://example.test/app/file ratio 1/2 /"
        )

        label = self.runner._progress_current_label(accumulator)

        self.assertIn("https://example.test", label)
        self.assertIn("https://example.test/app/file", label)
        self.assertIn("ratio 1/2", label)
        self.assertIn(" /", label)
        self.assertNotIn("<redacted-path>", label)

    def test_progress_interval_from_env_overrides_config_default(self):
        env_name = self.runner.PROGRESS_INTERVAL_ENV
        config_default = 60.0
        with mock.patch.dict(os.environ, {env_name: "12.5"}, clear=False):
            self.assertEqual(
                self.runner._progress_interval_from_env(env_name, config_default),
                12.5,
            )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(env_name, None)
            self.assertEqual(
                self.runner._progress_interval_from_env(env_name, config_default),
                config_default,
            )
        with mock.patch.dict(os.environ, {env_name: "not-a-number"}, clear=False):
            self.assertEqual(
                self.runner._progress_interval_from_env(env_name, config_default),
                config_default,
            )

    def test_progress_interval_from_env_rejects_non_finite_and_non_positive(self):
        env_name = self.runner.PROGRESS_INTERVAL_ENV
        config_default = 60.0
        for raw in ("nan", "inf", "-inf", "0", "-5"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {env_name: raw}, clear=False):
                self.assertEqual(
                    self.runner._progress_interval_from_env(env_name, config_default),
                    config_default,
                )
        with mock.patch.dict(os.environ, {env_name: "12.5"}, clear=False):
            self.assertEqual(
                self.runner._progress_interval_from_env(env_name, config_default),
                12.5,
            )

    def test_progress_heartbeat_broken_pipe_does_not_abort_tracked_run(self):
        class BrokenHeartbeatStderr(io.StringIO):
            def write(self, text):
                if "delegate: still running" in text:
                    raise BrokenPipeError("consumer closed")
                return super().write(text)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "slow_child.py"
        script.write_text(
            "import time\n"
            'print(\'{"type":"message","role":"assistant","content":"starting"}\', flush=True)\n'
            "time.sleep(0.15)\n"
            'print(\'{"type":"completion","finalText":"done"}\', flush=True)\n',
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [sys.executable, str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=BrokenHeartbeatStderr(),
                progress=True,
                progress_initial_delay_sec=0.01,
                progress_interval_sec=0.01,
            )

            self.assertEqual(code, 0)
            self.assertIsNotNone(payload)
            self.assertTrue(payload["ok"])
            state = json.loads((root / "runs" / run_id / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "succeeded")

    def test_progress_defaults_are_sourced_from_config(self):
        from delegate_agent import config as delegate_config

        self.assertEqual(
            self.runner.PROGRESS_INITIAL_DELAY_SEC,
            delegate_config.default_progress_initial_delay_sec(),
        )
        self.assertEqual(
            self.runner.PROGRESS_HEARTBEAT_INTERVAL_SEC,
            delegate_config.default_progress_interval_sec(),
        )

    def test_emit_bounded_text_summary_uses_shared_cleanup_renderer(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="git")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="work",
                model="composer-2.5",
                source_cwd=workspace,
                execution_cwd=str(Path(workspace) / "wt"),
                workspace_kind="git",
                isolated_workspace=True,
                started_at="2026-05-20T21:42:33Z",
                source_git_root=workspace,
                isolation_lifecycle="persistent",
                branch="delegate/cursor-demo",
            )
            stdout = io.StringIO()
            self.runner.emit_bounded_text_summary(
                ctx,
                status="completed",
                duration_ms=1200,
                stdout=stdout,
            )
            output = stdout.getvalue()
            self.assertIn(
                f"cleanup (refuses dirty / unmerged):       delegate worktree remove {alias}",
                output,
            )
            self.assertIn(
                f"cleanup (DISCARD uncommitted edits):      delegate worktree remove {alias} --discard-uncommitted",
                output,
            )
            self.assertIn("raw git equivalent:", output)
            self.assertIn(f"git -C {workspace} worktree remove {Path(workspace) / 'wt'}", output)
            self.assertIn("git -C", output)
            self.assertIn("branch -d delegate/cursor-demo", output)
            self.assertNotIn("worktree remove --force", output)
            self.assertNotIn("branch -D", output)

    def test_tracked_claude_result_stream_writes_completion_report(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "claude"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'prompt="$(cat)"\n'
            'printf \'{"type":"assistant","message":{"content":[{"type":"text","text":"read:%s"}]}}\\n\' "$prompt"\n'
            'printf \'%s\\n\' \'{"type":"result","subtype":"success","result":"Status: completed\\\\n- final from claude"}\'\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="claude")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="claude",
                engine="claude",
                mode="safe",
                model="claude-sonnet-4-6",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                prompt_transport="stdin",
            )
            code, payload = self.runner.execute_tracked(
                [str(script), "-p"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                stdin_text="CLAUDE STDIN PROMPT",
                manifest_argv=[str(script), "-p", "<prompt via stdin>"],
            )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertIn("completionReportCommand", payload)
            run_path = self.registry.run_directory(root, run_id)
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("final from claude", report)
            self.assertIn("read:CLAUDE STDIN PROMPT", snapshot["assistantText"])
            self.assertEqual(manifest["promptTransport"], "stdin")
            self.assertNotIn("CLAUDE STDIN PROMPT", json.dumps(manifest["argv"]))

    def test_substantive_short_child_report_is_not_suspect_short(self):
        # F1: a terse but substantive "Verdict:/Status:" report must NOT be flagged
        # suspect_short even though it is under 200 chars in safe mode.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"Verdict: pass"}}\'\n'
            "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertEqual(payload["completionReportSource"], "child")
            self.assertEqual(payload["resultQuality"], "ok")
            self.assertFalse(
                any("suspect_short" in w for w in payload.get("warnings", [])),
                f"expected no suspect_short warning, got {payload.get('warnings')}",
            )

    def test_preamble_only_short_child_report_is_suspect_short(self):
        # F1: a preamble-only fragment like "Performing an adversarial review..."
        # that is short and NOT substantive must still flag suspect_short.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"Performing an adversarial review now."}}\'\n'
            "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertEqual(payload["completionReportSource"], "child")
            self.assertEqual(payload["resultQuality"], "suspect_short")
            self.assertTrue(any("suspect_short" in w for w in payload.get("warnings", [])))

    def test_synthesized_report_is_never_suspect_short(self):
        # F2: a safe-mode failed run with empty stderr tail produces a synthesized
        # report; resultQuality must NOT be suspect_short (delegate-synthesized
        # reports are never suspect).
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\nexit 7\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 7)
            assert payload is not None
            self.assertEqual(payload["completionReportSource"], "delegate_synthesized")
            self.assertNotEqual(payload["resultQuality"], "suspect_short")
            self.assertFalse(
                any("suspect_short" in w for w in payload.get("warnings", [])),
            )

    def test_child_report_discussing_401_is_not_auth_failed(self):
        # Model text is untrusted classifier input even when the child exits nonzero.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"Fixture says: 401 Unauthorized: invalid token."}}\'\n'
            "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
            "printf 'runtime error: something broke\\n' >&2\n"
            "exit 9\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 9)
            assert payload is not None
            self.assertEqual(payload.get("failureReason"), "child_failed")
            self.assertEqual(payload.get("error"), "child_failed")

    def test_auth_401_with_unauthorized_context_in_stderr_is_auth_failed(self):
        # F3: a 401 in stderr with auth context (unauthorized nearby) counts as
        # auth_failed.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\nprintf 'Error 401: unauthorized request\\n' >&2\nexit 7\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 7)
            assert payload is not None
            self.assertEqual(payload["failureReason"], "auth_failed")

    def test_auth_remediation_is_generic_for_non_codex_harness(self):
        # F6: for a non-codex harness, auth remediation must mention re-authenticating
        # the <harness> CLI, not codex commands.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "cursor"
        script.write_text(
            "#!/usr/bin/env bash\nprintf 'token_expired: please re-authenticate\\n' >&2\nexit 7\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="safe",
                model="composer-2.5",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 7)
            assert payload is not None
            self.assertEqual(payload["failureReason"], "auth_failed")
            next_actions = payload.get("nextActions", [])
            self.assertTrue(
                any("re-authenticate the cursor CLI" in a for a in next_actions),
                f"expected generic re-authenticate action, got {next_actions}",
            )
            self.assertFalse(any("codex login" in a for a in next_actions))
            report = (self.registry.run_directory(root, run_id) / "completion-report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("re-authenticate the cursor CLI", report)
            self.assertNotIn("codex login", report)

    def test_auth_remediation_mentions_codex_commands_for_codex_harness(self):
        # F6: for codex, auth remediation mentions codex login and delegate profiles.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\nprintf 'token_expired: please re-authenticate\\n' >&2\nexit 7\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 7)
            assert payload is not None
            self.assertEqual(payload["failureReason"], "auth_failed")
            next_actions = payload.get("nextActions", [])
            self.assertIn("delegate profiles", next_actions)
            self.assertIn("codex login", next_actions)

    def test_launch_failure_state_and_snapshot_omit_result_quality(self):
        # F8: a launch failure never ran the child, so both state and snapshot must
        # omit resultQuality (never ran, no result to classify).
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            missing = str(Path(workspace) / "missing-agent")

            with self.assertRaises(self.runner.RunnerLaunchError):
                self.runner.execute_tracked(
                    [missing],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            run_path = root / "runs" / run_id
            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertNotIn(
                "resultQuality",
                state,
                f"state must omit resultQuality for launch failure, got {state}",
            )
            self.assertNotIn(
                "resultQuality",
                snapshot,
                f"snapshot must omit resultQuality for launch failure, got {snapshot}",
            )

    def test_second_attempt_launch_failure_preserves_primary_capture(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            script = Path(workspace) / "codex"
            script.write_text(
                '#!/usr/bin/env bash\nprintf "usage limit\\n" >&2\nchmod 000 "$0"\nexit 1\n',
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                codex_failover_identity=f"auth={workspace}/primary/auth.json\0profile=",
                codex_fallback_failover_identity=f"auth={workspace}/fallback/auth.json\0profile=",
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )

            with (
                mock.patch.dict(os.environ, {"HOME": home}, clear=False),
                self.assertRaises(self.runner.RunnerLaunchError) as caught,
            ):
                self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(caught.exception.error, "child_launch_failed")
            run_path = root / "runs" / run_id
            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(state["stderrBytes"], len(b"usage limit\n"))
            self.assertEqual(state["exitCode"], 1)
            self.assertIn("finishedAt", state)
            self.assertEqual(snapshot["exitCode"], 1)

    def test_cancel_marker_suppresses_auth_fallback(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                codex_failover_identity=f"auth={workspace}/primary/auth.json\0profile=",
                codex_fallback_failover_identity=f"auth={workspace}/fallback/auth.json\0profile=",
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )
            accumulator = self.runner.harness_events.StreamAccumulator(harness="codex")
            accumulator.ingest_line('{"type":"error","message":"usage limit"}')
            primary = self.runner.TrackedCaptureResult(
                accumulator=accumulator,
                exit_code=1,
                duration_ms=10,
                stdout_bytes=1,
                stderr_bytes=0,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=None,
            )

            def primary_then_cancel(*_args, **_kwargs):
                state = {
                    "schema": self.registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": "running",
                    "cancelRequested": True,
                    "cancelRequestedAt": self.registry.utc_now_iso(),
                }
                self.registry.write_json_atomic(
                    self.registry.run_directory(root, run_id) / self.registry.STATE_FILE,
                    state,
                )
                return primary

            with (
                mock.patch.object(
                    self.runner,
                    "_run_single_tracked_attempt",
                    side_effect=primary_then_cancel,
                ) as run_attempt,
                mock.patch.object(self.runner, "_should_retry_profiles") as should_retry,
            ):
                code, payload = self.runner.execute_tracked(
                    ["codex", "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(run_attempt.call_count, 1)
            should_retry.assert_not_called()
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "cancelled")
            self.assertEqual(payload["exitCode"], 1)

    def test_cancel_marker_suppresses_empty_result_retry(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            primary = self.runner.TrackedCaptureResult(
                accumulator=self.runner.harness_events.StreamAccumulator(harness="codex"),
                exit_code=0,
                duration_ms=10,
                stdout_bytes=0,
                stderr_bytes=0,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=None,
            )

            def primary_then_cancel(*_args, **_kwargs):
                state = {
                    "schema": self.registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": "running",
                    "cancelRequested": True,
                    "cancelRequestedAt": self.registry.utc_now_iso(),
                }
                self.registry.write_json_atomic(
                    self.registry.run_directory(root, run_id) / self.registry.STATE_FILE,
                    state,
                )
                return primary

            with (
                mock.patch.object(
                    self.runner,
                    "_run_single_tracked_attempt",
                    side_effect=primary_then_cancel,
                ) as run_attempt,
                mock.patch.object(self.runner, "_tracked_capture_quality") as capture_quality,
            ):
                code, payload = self.runner.execute_tracked(
                    ["codex", "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    manifest_argv=["codex", "<prompt>"],
                )

            self.assertEqual(run_attempt.call_count, 1)
            capture_quality.assert_not_called()
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "cancelled")
            self.assertNotIn("emptyRetry", payload)

    def test_retry_launch_gate_rechecks_cancel_marker_under_lock(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            run_path = self.registry.run_directory(root, run_id)
            self.registry.write_json_atomic(
                run_path / self.registry.STATE_FILE,
                {
                    "schema": self.registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": "running",
                    "cancelRequested": True,
                    "cancelRequestedAt": self.registry.utc_now_iso(),
                },
            )
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            files = self.runner.TrackedRunFiles(
                run_path=run_path,
                stdout_log=run_path / self.registry.STDOUT_LOG,
                stderr_log=run_path / self.registry.STDERR_LOG,
            )
            prior = self.runner.TrackedCaptureResult(
                accumulator=self.runner.harness_events.StreamAccumulator(harness="codex"),
                exit_code=1,
                duration_ms=10,
                stdout_bytes=0,
                stderr_bytes=0,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=None,
            )

            with (
                mock.patch.object(self.runner, "_launch_tracked_process") as launch,
                self.assertRaises(self.runner.RunnerLaunchError) as caught,
            ):
                self.runner._run_single_tracked_attempt(
                    ["codex", "task"],
                    workspace,
                    files,
                    ctx,
                    started=time.monotonic(),
                    deadline=None,
                    stdin_text=None,
                    env_overrides=None,
                    scratch_dir=None,
                    progress=False,
                    progress_stderr=None,
                    progress_initial_delay_sec=1,
                    progress_interval_sec=1,
                    attempt_label="fallback",
                    prior_capture=prior,
                )

            self.assertEqual(caught.exception.error, "cancelled_by_user")
            launch.assert_not_called()
            self.assertFalse((run_path / self.registry.STDERR_LOG).exists())

    def test_fallback_launch_failure_after_cancel_preserves_cancelled_outcome(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                codex_failover_identity=f"auth={workspace}/primary/auth.json\0profile=",
                codex_fallback_failover_identity=f"auth={workspace}/fallback/auth.json\0profile=",
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )
            accumulator = self.runner.harness_events.StreamAccumulator(harness="codex")
            accumulator.ingest_line('{"type":"error","message":"usage limit"}')
            primary = self.runner.TrackedCaptureResult(
                accumulator=accumulator,
                exit_code=1,
                duration_ms=10,
                stdout_bytes=1,
                stderr_bytes=0,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=None,
            )
            attempts = 0

            def cancel_during_fallback_launch(_argv, _cwd, files, attempt_ctx, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    return primary
                state = {
                    "schema": self.registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": "running",
                    "cancelRequested": True,
                    "cancelRequestedAt": self.registry.utc_now_iso(),
                }
                self.registry.write_json_atomic(files.run_path / self.registry.STATE_FILE, state)
                error = self.runner.RunnerLaunchError(
                    "child_launch_failed",
                    "fallback launch failed",
                )
                self.runner._record_tracked_launch_failure(
                    files,
                    attempt_ctx,
                    error,
                    prior_capture=kwargs.get("prior_capture"),
                )
                raise error

            with (
                mock.patch.object(
                    self.runner,
                    "_run_single_tracked_attempt",
                    side_effect=cancel_during_fallback_launch,
                ),
                mock.patch.object(self.runner, "_should_retry_profiles", return_value=True),
                mock.patch.dict(os.environ, {"HOME": home}, clear=False),
            ):
                code, payload = self.runner.execute_tracked(
                    ["codex", "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(attempts, 2)
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "cancelled")
            self.assertEqual(payload["exitCode"], 1)
            self.assertEqual(payload["failureReason"], "cancelled_by_user")
            self.assertEqual(payload["error"], "cancelled_by_user")
            run_path = self.registry.run_directory(root, run_id)
            for name in (self.registry.STATE_FILE, self.registry.SNAPSHOT_FILE):
                persisted = json.loads((run_path / name).read_text(encoding="utf-8"))
                self.assertEqual(persisted["status"], "cancelled")
                self.assertEqual(persisted["exitCode"], 1)
                self.assertNotIn("error", persisted)
                self.assertNotIn("message", persisted)
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            self.assertIn("Status: cancelled", report)
            self.assertIn("Failure reason: cancelled_by_user", report)
            self.assertNotIn("child_launch_failed", report)

    def test_safe_empty_success_retries_once_and_resolves(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'attempt:%s\\n' \"$*\" >&2\n"
                "if [[ \"$*\" == *'Delegate retry instruction'* ]]; then\n"
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"Status: completed. The requested review finished successfully and this plain-text final report contains the findings, verification result, changed-file summary, and remaining-risk statement needed by the parent operator. No further action is required."}}\'\n'
                "  printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
                "fi\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script), "original prompt"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manifest_argv=[str(script), "<prompt>"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["emptyRetry"], {"attempted": True, "resolved": True})
            self.assertEqual(payload["resultQuality"], "ok")
            stderr_log = (root / "runs" / run_id / "stderr.log").read_text(encoding="utf-8")
            self.assertEqual(
                sum(line.startswith("attempt:") for line in stderr_log.splitlines()), 2
            )
            self.assertIn("empty-success-retry", stderr_log)
            self.assertIn("--- delegate empty-retry attempt:", stderr_log)
            stdout_log = (root / "runs" / run_id / "stdout.log").read_bytes()
            self.assertEqual(payload["stdoutBytes"], len(stdout_log))
            primary_stderr, marker, retry_stderr = stderr_log.encode("utf-8").partition(
                b"\n--- delegate empty-retry attempt: empty-success-retry ---\n"
            )
            self.assertTrue(marker)
            _primary_marker, primary_child = primary_stderr.split(
                b"\n--- delegate attempt: primary ---\n", 1
            )
            self.assertEqual(payload["stderrBytes"], len(primary_child) + len(retry_stderr))

    def test_safe_resume_empty_retry_refuses_argv_overflow_before_spawning(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "cursor"
            script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-31T12:00:00Z",
                resumed_from={"runId": "del_source", "alias": "cursor-1"},
            )
            prompt = "x" * (self.runner.resume_command.ARGV_PROMPT_GUARD_BYTES - 1)

            with self.assertRaises(self.runner.RunnerLaunchError) as caught:
                self.runner.execute_tracked(
                    [str(script), prompt],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    manifest_argv=[str(script), "<prompt>"],
                )

            self.assertEqual(caught.exception.error, "resume_prompt_too_large")
            state = json.loads(
                (self.registry.run_directory(root, run_id) / self.registry.STATE_FILE).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["status"], "failed")

    def test_non_resume_empty_retry_does_not_raise_resume_prompt_overflow(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "cursor"
            script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-31T12:00:00Z",
            )
            prompt = "x" * (self.runner.resume_command.ARGV_PROMPT_GUARD_BYTES - 1)

            code, payload = self.runner.execute_tracked(
                [str(script), prompt],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manifest_argv=[str(script), "<prompt>"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["emptyRetry"], {"attempted": True, "resolved": False})
            self.assertNotEqual(payload.get("error"), "resume_prompt_too_large")

    def test_safe_empty_retry_preserves_both_attempts_and_stays_empty(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "for _ in $(seq 1 600); do printf 'noise\\n'; done\n"
                "printf 'empty-attempt:%s\\n' \"$*\" >&2\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script), "original prompt"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manifest_argv=[str(script), "<prompt>"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["emptyRetry"], {"attempted": True, "resolved": False})
            self.assertEqual(payload["resultQuality"], "empty")
            self.assertIn(self.runner.EMPTY_RETRY_WARNING, payload["warnings"])
            stderr_log = (root / "runs" / run_id / "stderr.log").read_text(encoding="utf-8")
            self.assertEqual(stderr_log.count("empty-attempt:"), 2)
            events = [
                json.loads(line)
                for line in (root / "runs" / run_id / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                sum(event.get("kind") == "stream.line" for event in events),
                self.runner.harness_events.EVENT_LIMIT,
            )
            self.assertEqual(
                sum(event.get("kind") == "stream.lines_truncated" for event in events), 1
            )

    def test_empty_retry_uses_retry_duration_as_total(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            accumulator = self.runner.harness_events.StreamAccumulator(harness="codex")
            primary = self.runner.TrackedCaptureResult(
                accumulator=accumulator,
                exit_code=0,
                duration_ms=40,
                stdout_bytes=2,
                stderr_bytes=3,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=None,
            )
            retry = self.runner.TrackedCaptureResult(
                accumulator=accumulator,
                exit_code=0,
                duration_ms=70,
                stdout_bytes=5,
                stderr_bytes=7,
                stdin_failures=(),
                pid=os.getpid(),
                pgid=None,
            )
            with (
                mock.patch.object(
                    self.runner, "_run_single_tracked_attempt", side_effect=[primary, retry]
                ),
                mock.patch.object(
                    self.runner,
                    "_tracked_capture_quality",
                    side_effect=[self.runner.RESULT_QUALITY_EMPTY, self.runner.RESULT_QUALITY_OK],
                ),
            ):
                _code, payload = self.runner.execute_tracked(
                    ["codex", "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    manifest_argv=["codex", "<prompt>"],
                )

            self.assertEqual(payload["durationMs"], 70)
            self.assertEqual(payload["stdoutBytes"], 7)
            self.assertEqual(payload["stderrBytes"], 10)

    def test_work_empty_success_does_not_retry(self):
        with tempfile.TemporaryDirectory() as workspace:
            counter = Path(workspace) / "attempts"
            script = Path(workspace) / "codex"
            script.write_text(
                f"#!/usr/bin/env bash\nprintf x >> {counter!s}\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="work",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script), "original prompt"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manifest_argv=[str(script), "<prompt>"],
            )
            self.assertEqual(code, 0)
            self.assertEqual(counter.read_text(encoding="utf-8"), "x")
            self.assertNotIn("emptyRetry", payload)

    def test_read_only_call_empty_success_retries_once(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "agent"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'attempt stderr\\n' >&2\n"
                "if [[ \"$*\" == *'Delegate retry instruction'* ]]; then\n"
                "  printf 'Final answer.\\n'\n"
                "fi\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            result = self.runner.execute_call(
                [str(script), "original prompt"],
                workspace,
                harness="cursor",
                read_only=True,
            )
            self.assertEqual(result.text, "Final answer.")
            self.assertTrue(result.empty_retry_attempted)
            self.assertTrue(result.empty_retry_resolved)
            self.assertEqual(result.result_quality, "ok")
            self.assertEqual(result.stderr_tail.count("attempt stderr"), 2)

    def test_write_capable_call_empty_success_does_not_retry(self):
        with tempfile.TemporaryDirectory() as workspace:
            counter = Path(workspace) / "attempts"
            script = Path(workspace) / "agent"
            script.write_text(
                f"#!/usr/bin/env bash\nprintf x >> {counter!s}\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            result = self.runner.execute_call(
                [str(script), "side-effectful prompt"],
                workspace,
                harness="cursor",
            )

            self.assertEqual(counter.read_text(encoding="utf-8"), "x")
            self.assertEqual(result.result_quality, "empty")
            self.assertFalse(result.empty_retry_attempted)
            self.assertIn(
                self.runner.EMPTY_RETRY_SKIPPED_WRITE_CAPABLE_WARNING,
                result.warnings,
            )

    def test_empty_call_retry_preserves_primary_truncation_flag(self):
        primary = self.runner.CallResult(
            text="",
            exit_code=0,
            duration_ms=10,
            stdout_bytes=1,
            stderr_bytes=2,
            text_chars=30_001,
            text_truncated=True,
            result_quality=self.runner.RESULT_QUALITY_EMPTY,
        )
        retry = self.runner.CallResult(
            text="final answer",
            exit_code=0,
            duration_ms=20,
            stdout_bytes=3,
            stderr_bytes=4,
            text_chars=12,
            text_truncated=False,
            result_quality=self.runner.RESULT_QUALITY_OK,
        )
        with mock.patch.object(self.runner, "_execute_call_once", side_effect=[primary, retry]):
            result = self.runner.execute_call(
                ["agent", "task"],
                "/tmp",
                harness="cursor",
                read_only=True,
            )

            self.assertTrue(result.text_truncated)

    def test_empty_call_retry_aggregates_exact_usage(self):
        primary = self.runner.CallResult(
            text="",
            exit_code=0,
            duration_ms=1,
            stdout_bytes=0,
            stderr_bytes=0,
            text_chars=0,
            text_truncated=False,
            result_quality="empty",
            usage={"inputTokens": 3, "outputTokens": 2, "basis": "exact"},
        )
        retry = self.runner.CallResult(
            text="ok",
            exit_code=0,
            duration_ms=1,
            stdout_bytes=0,
            stderr_bytes=0,
            text_chars=2,
            text_truncated=False,
            usage={"inputTokens": 5, "outputTokens": 7, "basis": "exact"},
        )
        with mock.patch.object(self.runner, "_execute_call_once", side_effect=(primary, retry)):
            result = self.runner.execute_call(
                ["agent", "task"], "/tmp", harness="claude", read_only=True
            )

        self.assertEqual(result.usage, {"inputTokens": 8, "outputTokens": 9, "basis": "exact"})

    def test_attempt_delimiters_keep_auth_fallback_marker_distinct_from_empty_retry(self):
        with tempfile.TemporaryDirectory() as workspace:
            stderr_log = Path(workspace) / "stderr.log"
            self.runner._append_attempt_delimiter(stderr_log, label="fallback")
            self.runner._append_attempt_delimiter(stderr_log, label="empty-success-retry")

            markers = stderr_log.read_text(encoding="utf-8")
            self.assertIn("--- delegate codex auth attempt: fallback ---", markers)
            self.assertIn("--- delegate empty-retry attempt: empty-success-retry ---", markers)

    def test_fallback_configured_single_attempt_keeps_stderr_unprefixed(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'single attempt\\n' >&2\n"
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                fallback_env_overrides={"CODEX_HOME": "/fallback"},
            )
            self.runner.execute_tracked(
                [str(script), "task"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            stderr_log = (root / "runs" / run_id / "stderr.log").read_text(encoding="utf-8")
            self.assertEqual(stderr_log, "single attempt\n")

    def test_fallback_retry_writes_both_attempt_delimiters(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "${ATTEMPT}" = primary ]; then\n'
                "  printf 'You exceeded your current quota usage limit\\n' >&2\n"
                "  exit 1\n"
                "fi\n"
                "printf 'fallback attempt\\n' >&2\n"
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                env_overrides={"ATTEMPT": "primary"},
                codex_failover_identity=f"auth={workspace}/primary/auth.json\0profile=",
                codex_fallback_failover_identity=f"auth={workspace}/fallback/auth.json\0profile=",
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )
            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            stderr_log = (root / "runs" / run_id / "stderr.log").read_text(encoding="utf-8")
            self.assertIn("--- delegate attempt: primary ---", stderr_log)
            self.assertIn("--- delegate codex auth attempt: fallback ---", stderr_log)

    def test_successful_auth_fallback_aggregates_primary_capture(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            script = Path(workspace) / "codex"
            primary_stdout = b'{"type":"error","message":"usage limit"}\n'
            fallback_lines = (
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"Status: completed with a substantive fallback result."}}',
                '{"type":"turn.completed"}',
            )
            fallback_stdout = "\n".join(fallback_lines).encode("utf-8") + b"\n"
            script.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "${ATTEMPT}" = primary ]; then\n'
                "  printf '%s\\n' '" + primary_stdout.decode("utf-8").strip() + "'\n"
                "  printf 'primary stderr\\n' >&2\n"
                "  exit 1\n"
                "fi\n"
                + "".join(f"printf '%s\\n' '{line}'\n" for line in fallback_lines)
                + "printf 'fallback stderr\\n' >&2\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                env_overrides={"ATTEMPT": "primary"},
                codex_failover_identity=f"auth={workspace}/primary/auth.json\0profile=",
                codex_fallback_failover_identity=f"auth={workspace}/fallback/auth.json\0profile=",
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )

            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                code, payload = self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            assert payload is not None
            self.assertEqual(payload["stdoutBytes"], len(primary_stdout) + len(fallback_stdout))
            self.assertEqual(
                payload["stderrBytes"], len(b"primary stderr\n") + len(b"fallback stderr\n")
            )
            snapshot = json.loads(
                (root / "runs" / run_id / "snapshot.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(event.get("message") == "usage limit" for event in snapshot["recentEvents"])
            )

    def test_auth_fallback_persists_stdout_event_reset_time(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            target = (datetime.datetime.now() + datetime.timedelta(hours=3)).replace(
                second=0, microsecond=0
            )
            hour = target.hour % 12 or 12
            reset_text = f"{hour}:{target.minute:02d} {'AM' if target.hour < 12 else 'PM'}"
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "${ATTEMPT}" = primary ]; then\n'
                "  printf '%s\\n' "
                f'\'{{"type":"error","message":"You have hit your usage limit. Try again at {reset_text}."}}\'\n'
                "  exit 1\n"
                "fi\n"
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            primary_identity = "auth=/primary/auth.json\0profile="
            fallback_identity = "auth=/fallback/auth.json\0profile="
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                auth_profile="primary",
                fallback_auth_profile="fallback",
                codex_failover_identity=primary_identity,
                codex_fallback_failover_identity=fallback_identity,
                env_overrides={"ATTEMPT": "primary"},
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )

            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                code, _payload = self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
                blocked, blocked_until = self.runner.failover_state.check_blocked(
                    "codex", primary_identity
                )
                fallback_blocked, _ = self.runner.failover_state.check_blocked(
                    "codex", fallback_identity
                )

            self.assertEqual(code, 0)
            self.assertTrue(blocked)
            self.assertEqual(blocked_until, int(target.timestamp()))
            self.assertFalse(fallback_blocked)

    def test_preflight_fallback_failure_records_fallback_block(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            target = (datetime.datetime.now() + datetime.timedelta(hours=4)).replace(
                second=0, microsecond=0
            )
            hour = target.hour % 12 or 12
            reset_text = f"{hour}:{target.minute:02d} {'AM' if target.hour < 12 else 'PM'}"
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' "
                f'\'{{"type":"error","message":"You have hit your usage limit. Try again at {reset_text}."}}\'\n'
                "exit 1\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            primary_identity = "auth=/primary/auth.json\0profile="
            fallback_identity = "auth=/fallback/auth.json\0profile="
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                auth_profile="primary",
                fallback_auth_profile="fallback",
                codex_failover_identity=primary_identity,
                codex_fallback_failover_identity=fallback_identity,
                env_overrides={"ATTEMPT": "primary"},
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )

            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                self.runner.failover_state.write_block(
                    "codex", primary_identity, int(time.time()) + 60
                )
                code, payload = self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
                blocked, blocked_until = self.runner.failover_state.check_blocked(
                    "codex", fallback_identity
                )

            self.assertEqual(code, 1)
            self.assertEqual(payload["codexAuthFallback"]["reason"], "usage_limit_preflight")
            self.assertTrue(blocked)
            self.assertEqual(blocked_until, int(target.timestamp()))

    def test_preflight_fallback_honors_legacy_profile_block(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            attempts = Path(workspace) / "attempts.txt"
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "${{ATTEMPT:-}}" >> "{attempts}"\n'
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            work_identity = (
                f"auth={(Path(home) / '.codex/auth.json').resolve(strict=False)}\0profile="
            )
            personal_identity = (
                "auth="
                f"{(Path(home) / '.ai-profiles/runtime/codex/personal/auth.json').resolve(strict=False)}"
                "\0profile="
            )
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                auth_profile="work",
                fallback_auth_profile="personal",
                codex_failover_identity=work_identity,
                codex_fallback_failover_identity=personal_identity,
                env_overrides={"ATTEMPT": "primary"},
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )

            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                state_dir = Path(home) / ".ai-profiles/runtime/failover"
                state_dir.mkdir(parents=True)
                (state_dir / "codex-work.blocked-until").write_text(
                    f"{int(time.time()) + 60}\n", encoding="utf-8"
                )
                code, payload = self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            self.assertTrue(payload["codexAuthFallback"]["failoverPreflight"])
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), ["fallback"])

    def test_preflight_fallback_thread_ephemeral_stays_on_fallback_env(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            attempts = Path(workspace) / "attempts.txt"
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                f"attempts = pathlib.Path({str(attempts)!r})\n"
                "prior = attempts.read_text() if attempts.exists() else ''\n"
                "count = len(prior.splitlines()) + 1 if prior else 1\n"
                "attempts.write_text(prior + os.environ.get('ATTEMPT', '') + '\\n')\n"
                "if count < 3:\n"
                "    print(json.dumps({'type': 'error', 'message': 'no thread with id: synthetic-thread'}))\n"
                "    raise SystemExit(1)\n"
                "assert os.environ.get('ATTEMPT') == 'fallback'\n"
                "assert '--ignore-user-config' in sys.argv\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n"
                "print(json.dumps({'type':'turn.completed'}))\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            primary_identity = "auth=/primary/auth.json\0profile="
            fallback_identity = "auth=/fallback/auth.json\0profile="
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                auth_profile="primary",
                fallback_auth_profile="fallback",
                codex_failover_identity=primary_identity,
                codex_fallback_failover_identity=fallback_identity,
                env_overrides={"ATTEMPT": "primary"},
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )

            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                self.runner.failover_state.write_block(
                    "codex", primary_identity, int(time.time()) + 60
                )
                code, payload = self.runner.execute_tracked(
                    [str(script), "exec", "--json", "-"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    stdin_text="task",
                )

            self.assertEqual(code, 0)
            self.assertTrue(payload["codexThreadFallback"]["engaged"])
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), ["fallback"] * 3)

    def test_auth_fallback_skips_when_fallback_identity_is_blocked(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            attempts = Path(workspace) / "attempts.txt"
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "${{ATTEMPT:-}}" >> "{attempts}"\n'
                'if [ "${ATTEMPT}" = primary ]; then\n'
                "  printf 'You exceeded your current quota usage limit\\n' >&2\n"
                "  exit 1\n"
                "fi\n"
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"should not run"}}\'\n',
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            primary_identity = "auth=/primary/auth.json\0profile="
            fallback_identity = "auth=/fallback/auth.json\0profile="
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                auth_profile="primary",
                fallback_auth_profile="fallback",
                codex_failover_identity=primary_identity,
                codex_fallback_failover_identity=fallback_identity,
                env_overrides={"ATTEMPT": "primary"},
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )

            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                self.runner.failover_state.write_block(
                    "codex", fallback_identity, int(time.time()) + 60
                )
                code, payload = self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 1)
            self.assertNotIn("codexAuthFallback", payload)
            self.assertIn("fallback profile is also blocked", " ".join(payload["warnings"]))
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), ["primary"])

    def test_auth_fallback_empty_retry_keeps_all_byte_counts(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "${ATTEMPT}" = primary ]; then printf "usage limit\\n" >&2; exit 1; fi\n'
                'if [[ "$*" == *"Delegate retry instruction"* ]]; then\n'
                '  printf "retry\\n" >&2\n'
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "else\n"
                '  printf "fallback\\n" >&2\n'
                "fi\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="call",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                group="workflow",
                call_read_only=True,
                env_overrides={"ATTEMPT": "primary"},
                codex_failover_identity=f"auth={workspace}/primary/auth.json\0profile=",
                codex_fallback_failover_identity=f"auth={workspace}/fallback/auth.json\0profile=",
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )
            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                code, payload = self.runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    manifest_argv=[str(script), "<prompt>"],
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                payload["stderrBytes"], len(b"usage limit\n") + len(b"fallback\n") + len(b"retry\n")
            )

    def test_grouped_read_only_call_empty_success_retries_once(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "agent"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *'Delegate retry instruction'* ]]; then\n"
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"Status: completed. The requested review finished successfully and this plain-text final report contains the findings, verification result, changed-file summary, and remaining-risk statement needed by the parent operator. No further action is required."}}\'\n'
                "  printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
                "fi\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="call",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                group="workflow",
                call_read_only=True,
            )

            code, payload = self.runner.execute_tracked(
                [str(script), "original prompt"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manifest_argv=[str(script), "<prompt>"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["emptyRetry"], {"attempted": True, "resolved": True})

    def test_grouped_call_timeout_terminates_the_child(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "agent"
            script.write_text("#!/usr/bin/env bash\nsleep 60\n", encoding="utf-8")
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="call",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                group="workflow",
                call_read_only=True,
            )

            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always", ResourceWarning)
                with self.assertRaises(self.runner.RunnerLaunchError) as caught:
                    self.runner.execute_tracked(
                        [str(script), "task"],
                        workspace,
                        ctx,
                        json_mode=True,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                        manifest_argv=[str(script), "<prompt>"],
                        timeout=1,
                    )
                gc.collect()

            self.assertEqual(caught.exception.error, "call_timeout")
            self.assertFalse(
                [
                    warning
                    for warning in caught_warnings
                    if issubclass(warning.category, ResourceWarning)
                ]
            )
            state = json.loads((root / "runs" / run_id / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error"], "call_timeout")

    def test_grouped_call_timeout_kills_descendants_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "agent.py"
            child_pid_path = Path(workspace) / "child.pid"
            child_ready_path = Path(workspace) / "child.ready"
            child_code = (
                "import signal, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"Path({str(child_ready_path)!r}).write_text('ready', encoding='utf-8')\n"
                "time.sleep(60)\n"
            )
            script.write_text(
                "import os, signal, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, lambda *_: os._exit(0))\n"
                f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
                "stderr=subprocess.DEVNULL)\n"
                f"Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8')\n"
                f"ready = Path({str(child_ready_path)!r})\n"
                "while not ready.exists():\n"
                "    time.sleep(0.01)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="call",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                group="workflow",
                call_read_only=True,
            )

            child_pid = None
            started = time.monotonic()
            try:
                with self.assertRaises(self.runner.RunnerLaunchError) as caught:
                    self.runner.execute_tracked(
                        [sys.executable, str(script)],
                        workspace,
                        ctx,
                        json_mode=True,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                        timeout=1,
                    )
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                self.assertEqual(caught.exception.error, "call_timeout")
                self.assertLess(time.monotonic() - started, 6)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if child_pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)

    def test_empty_call_does_not_mutate_verbatim_prompts(self):
        empty = self.runner.CallResult(
            text="",
            exit_code=0,
            duration_ms=1,
            stdout_bytes=0,
            stderr_bytes=0,
            text_chars=0,
            text_truncated=False,
            result_quality="empty",
        )
        for kwargs in (
            {"pure": True},
            {"prompt_instruction_mode": "slash-passthrough"},
        ):
            with (
                self.subTest(kwargs=kwargs),
                mock.patch.object(self.runner, "_execute_call_once", return_value=empty) as attempt,
            ):
                result = self.runner.execute_call(
                    ["agent", "/review"], "/tmp", harness="codex", read_only=True, **kwargs
                )

            self.assertEqual(attempt.call_count, 1)
            self.assertIn("verbatim prompt boundary", result.warnings[0])

    def test_grouped_empty_retry_failure_is_not_resolved(self):
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace) / "agent"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *'Delegate retry instruction'* ]]; then exit 1; fi\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="call",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                group="workflow",
                call_read_only=True,
            )
            code, payload = self.runner.execute_tracked(
                [str(script), "task"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manifest_argv=[str(script), "<prompt>"],
            )

            self.assertEqual(code, 1)
            self.assertEqual(payload["emptyRetry"], {"attempted": True, "resolved": False})

    def test_grouped_write_capable_call_empty_success_does_not_retry(self):
        with tempfile.TemporaryDirectory() as workspace:
            counter = Path(workspace) / "attempts"
            script = Path(workspace) / "agent"
            script.write_text(
                f"#!/usr/bin/env bash\nprintf x >> {counter!s}\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="call",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-16T12:00:00Z",
                group="workflow",
            )

            code, payload = self.runner.execute_tracked(
                [str(script), "side-effectful prompt"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manifest_argv=[str(script), "<prompt>"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(counter.read_text(encoding="utf-8"), "x")
            self.assertEqual(payload["resultQuality"], "empty")
            self.assertNotIn("emptyRetry", payload)
            self.assertIn(
                self.runner.EMPTY_RETRY_SKIPPED_WRITE_CAPABLE_WARNING, payload["warnings"]
            )
