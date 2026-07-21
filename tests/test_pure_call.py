import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase


class PureCallTests(CommandTestBase):
    def test_claude_pure_argv_is_exact_boundary(self):
        argv = self.delegate.build_claude_argv(
            self.delegate.DEFAULT_CONFIG["claude"],
            "call",
            "requested-model",
            {},
            reasoning_effort="high",
            pure=True,
        )
        self.assertEqual(
            argv,
            [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--input-format",
                "text",
                "--safe-mode",
                "--tools",
                "",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--model",
                "requested-model",
                "--effort",
                "high",
            ],
        )
        self.assertNotIn("--permission-mode", argv)
        self.assertNotIn("--bare", argv)

    def test_opencode_pure_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.build_request(
                "opencode",
                "call",
                None,
                self.delegate.ResolvedWorkspace("/tmp/empty", "directory"),
                "hostile prompt",
                self.delegate.DEFAULT_CONFIG,
                True,
                pure=True,
            )
        self.assertEqual(ctx.exception.error, "unsupported_pure_call")

    def test_call_pure_prompt_uses_stdin_not_argv(self):
        prompt = "HOSTILE_PROMPT_SENTINEL"
        parsed = self.delegate.parse_cli(["claude", "call", "--pure", prompt])
        request = self.delegate.request_from_parsed(
            parsed, self.delegate.DEFAULT_CONFIG, io.StringIO("")
        )
        self.addCleanup(lambda: Path(request.workspace).exists() and os.rmdir(request.workspace))
        self.assertEqual(request.stdin_text, prompt)
        self.assertNotIn(prompt, request.argv)
        self.assertEqual(request.prompt, prompt)

    def test_call_pure_uses_empty_temporary_cwd_and_cleans_it(self):
        parsed = self.delegate.parse_cli(["claude", "call", "--pure", "answer"])
        request = self.delegate.request_from_parsed(
            parsed, self.delegate.DEFAULT_CONFIG, io.StringIO("")
        )
        workspace = Path(request.workspace)
        self.assertTrue(workspace.is_dir())
        self.assertEqual(list(workspace.iterdir()), [])
        fake = self.delegate.delegate_runner.CallResult(
            text="ok",
            exit_code=0,
            duration_ms=1,
            stdout_bytes=2,
            stderr_bytes=0,
            text_chars=2,
            text_truncated=False,
        )
        with (
            mock.patch.object(self.delegate, "ensure_binary"),
            mock.patch.object(self.delegate.delegate_runner, "execute_call", return_value=fake),
        ):
            code, _ = self.delegate.execute_request(
                request,
                json_mode=True,
                config=self.delegate.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=self.delegate.ResolvedWorkspace("<call>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        self.assertFalse(workspace.exists())

    def test_call_preserves_child_created_files_in_result_envelope(self):
        with tempfile.TemporaryDirectory() as source:
            parsed = self.delegate.parse_cli(["cursor", "call", "write memo"])
            request = self.delegate.request_from_parsed(
                parsed, self.delegate.DEFAULT_CONFIG, io.StringIO("")
            )
            workspace = Path(request.workspace)
            fake = self.delegate.delegate_runner.CallResult(
                text="done",
                exit_code=0,
                duration_ms=1,
                stdout_bytes=4,
                stderr_bytes=0,
                text_chars=4,
                text_truncated=False,
            )

            def create_deliverable(*_args, **_kwargs):
                deliverable = workspace / "research" / "memo.md"
                deliverable.parent.mkdir()
                deliverable.write_text("completed memo\n", encoding="utf-8")
                return fake

            with (
                mock.patch.object(self.delegate, "ensure_binary"),
                mock.patch.object(
                    self.delegate.delegate_runner,
                    "execute_call",
                    side_effect=create_deliverable,
                ),
            ):
                code, payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=self.delegate.ResolvedWorkspace(source, "directory"),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            self.assertFalse(workspace.exists())
            self.assertIsNotNone(payload)
            assert payload is not None
            preserved = [Path(item) for item in payload["preservedArtifacts"]]
            self.assertEqual(len(preserved), 1)
            self.assertEqual(preserved[0].name, "memo.md")
            self.assertEqual(preserved[0].read_text(encoding="utf-8"), "completed memo\n")
            self.assertEqual(preserved[0].parents[2].name, "artifacts")

    def test_call_pure_env_is_allowlisted_no_ambient_secrets(self):
        with mock.patch.dict(
            os.environ,
            {"PATH": "/bin", "HOME": "/home/test", "DELEGATE_TEST_SECRET": "secret"},
            clear=True,
        ):
            env = self.delegate.profiles.child_environment(
                base={"CODEX_HOME": "/trusted/codex"},
                overrides={"PROFILE_VALUE": "trusted"},
                pure=True,
            )
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["HOME"], "/home/test")
        self.assertNotIn("DELEGATE_TEST_SECRET", env)
        self.assertEqual(env["CODEX_HOME"], "/trusted/codex")
        self.assertEqual(env["PROFILE_VALUE"], "trusted")

    def test_call_output_schema_maps_to_codex_path_and_claude_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            contents = (
                '{"type":"object","properties":{"answer":{"type":"string"}},'
                '"required":["answer"],"additionalProperties":false}'
            )
            schema.write_text(contents, encoding="utf-8")
            codex = self.delegate.build_request(
                "codex",
                "call",
                None,
                self.delegate.ResolvedWorkspace(tmp, "directory"),
                "answer",
                self.delegate.DEFAULT_CONFIG,
                True,
                output_schema=str(schema),
            )
            claude = self.delegate.build_request(
                "claude",
                "call",
                None,
                self.delegate.ResolvedWorkspace(tmp, "directory"),
                "answer",
                self.delegate.DEFAULT_CONFIG,
                True,
                output_schema=str(schema),
                pure=True,
            )
            self.assertEqual(
                codex.argv[codex.argv.index("--output-schema") + 1], str(schema.resolve())
            )
            self.assertEqual(claude.argv[claude.argv.index("--json-schema") + 1], contents)
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.build_request(
                    "grok",
                    "call",
                    None,
                    self.delegate.ResolvedWorkspace(tmp, "directory"),
                    "answer",
                    self.delegate.DEFAULT_CONFIG,
                    True,
                    output_schema=str(schema),
                )
            self.assertEqual(ctx.exception.error, "unsupported_output_schema")

    def test_pure_rejects_engine_without_required_capabilities(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.build_cursor_argv(
                ["agent"],
                "call",
                "/tmp/call",
                "requested-model",
                "answer",
                pure=True,
            )
        self.assertEqual(ctx.exception.error, "unsupported_pure_call")

    def test_codex_pure_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.build_codex_argv(
                self.delegate.DEFAULT_CONFIG["codex"],
                "call",
                "/tmp/call",
                "requested-model",
                "answer",
                {},
                workspace_kind="directory",
                pure=True,
            )
        self.assertEqual(ctx.exception.error, "unsupported_pure_call")

    def test_call_json_reports_resolved_model_and_usage_basis(self):
        event = [
            {
                "type": "result",
                "result": '{"answer":"yes"}',
                "structured_output": {"answer": "yes"},
                "is_error": False,
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "modelUsage": {
                    "small-model": {"outputTokens": 2},
                    "substantive-model": {"outputTokens": 7},
                },
                "permission_denials": [{"secret": "must not surface"}],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "claude_result.py"
            script.write_text(f"print({json.dumps(json.dumps(event))})\n", encoding="utf-8")
            result = self.delegate.delegate_runner.execute_call(
                [sys.executable, str(script)], tmp, harness="claude", pure=True
            )
        self.assertEqual(result.text, '{"answer":"yes"}')
        self.assertEqual(result.model_resolved, "substantive-model")
        self.assertEqual(result.usage, {"inputTokens": 11, "outputTokens": 7, "basis": "exact"})
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error, "pure_boundary_violation")
        self.assertEqual(result.message, "Pure boundary violation: 1 permission denial(s).")
        self.assertEqual(result.warnings, ())

        no_usage = [
            {
                "type": "result",
                "result": "ok",
                "is_error": False,
                "permission_denials": [],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "claude_no_usage.py"
            script.write_text(f"print({json.dumps(json.dumps(no_usage))})\n", encoding="utf-8")
            result = self.delegate.delegate_runner.execute_call(
                [sys.executable, str(script)], tmp, harness="claude", pure=True
            )
        self.assertEqual(result.usage, {"basis": "unavailable"})
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.error)

    def test_pure_tripwire_requires_permission_denials_list(self):
        cases = (
            (
                [{"type": "result", "result": "ok", "is_error": False}],
                "pure_boundary_unverified",
            ),
            (
                [
                    {
                        "type": "result",
                        "result": "ok",
                        "is_error": False,
                        "permission_denials": None,
                    }
                ],
                "pure_boundary_unverified",
            ),
            (
                [
                    {
                        "type": "result",
                        "result": "ok",
                        "is_error": False,
                        "permission_denials": {"tool": "Bash"},
                    }
                ],
                "pure_boundary_unverified",
            ),
        )
        for events, error in cases:
            with self.subTest(events=events), tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "claude_tripwire.py"
                script.write_text(f"print({json.dumps(json.dumps(events))})\n", encoding="utf-8")
                result = self.delegate.delegate_runner.execute_call(
                    [sys.executable, str(script)], tmp, harness="claude", pure=True
                )
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.error, error)
                self.assertIn("permission_denials", result.message or "")

    def test_call_envelope_reports_pure_schema_model_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            parsed = self.delegate.parse_cli(
                [
                    "claude",
                    "call",
                    "--pure",
                    "--model",
                    "requested-alias",
                    "--output-schema",
                    str(schema),
                    "answer",
                ]
            )
            request = self.delegate.request_from_parsed(
                parsed, self.delegate.DEFAULT_CONFIG, io.StringIO("")
            )
            fake = self.delegate.delegate_runner.CallResult(
                text='{"answer":"yes"}',
                exit_code=0,
                duration_ms=3,
                stdout_bytes=100,
                stderr_bytes=0,
                text_chars=16,
                text_truncated=False,
                model_resolved="resolved-model",
                usage={"inputTokens": 5, "outputTokens": 2, "basis": "exact"},
            )
            with (
                mock.patch.object(self.delegate, "ensure_binary"),
                mock.patch.object(self.delegate.delegate_runner, "execute_call", return_value=fake),
            ):
                code, payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=self.delegate.ResolvedWorkspace("<call>", "directory"),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
        self.assertEqual(code, 0)
        self.assertTrue(payload["pure"])
        self.assertTrue(payload["structuredOutput"])
        self.assertEqual(payload["modelRequested"], "requested-alias")
        self.assertEqual(payload["modelResolved"], "resolved-model")
        self.assertEqual(payload["usage"]["basis"], "exact")

    def test_malformed_claude_json_is_call_output_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "bad_claude.py"
            script.write_text("print('not json')\n", encoding="utf-8")
            result = self.delegate.delegate_runner.execute_call(
                [sys.executable, str(script)], tmp, harness="claude", pure=True
            )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error, "call_output_invalid")
        self.assertEqual(result.text, "")

    def test_claude_is_error_result_keeps_typed_usage_limit(self):
        events = [
            {
                "type": "result",
                "result": "Usage limit reached; resets at 2026-07-22 01:00 UTC.",
                "is_error": True,
                "permission_denials": [],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "claude_usage.py"
            script.write_text(f"print({json.dumps(json.dumps(events))})\n", encoding="utf-8")
            result = self.delegate.delegate_runner.execute_call(
                [sys.executable, str(script)], tmp, harness="claude", pure=True
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error, "usage_limit")
        self.assertIn("2026-07-22 01:00 UTC", result.message or "")

    def test_pure_claude_schema_dry_run_proves_temp_cwd_and_boundary_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            contents = '{"type":"object"}'
            schema.write_text(contents, encoding="utf-8")
            parsed = self.delegate.parse_cli(
                ["dry-run", "claude", "call", "--pure", "--output-schema", str(schema), "x"]
            )
            request = self.delegate.request_from_parsed(
                parsed, self.delegate.DEFAULT_CONFIG, io.StringIO("")
            )
        self.assertEqual(request.workspace, self.delegate.CALL_TEMP_CWD_PLACEHOLDER)
        self.assertIn("--safe-mode", request.argv)
        self.assertEqual(request.argv[request.argv.index("--json-schema") + 1], contents)

    def test_call_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            child_pid = Path(tmp) / "child.pid"
            script = Path(tmp) / "sleep_group.py"
            script.write_text(
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(child_pid)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            with self.assertRaises(self.delegate.delegate_runner.RunnerLaunchError) as ctx:
                self.delegate.delegate_runner.execute_call(
                    [sys.executable, str(script)], tmp, harness="codex", timeout=1
                )
            self.assertEqual(ctx.exception.error, "call_timeout")
            pid = int(child_pid.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"process-group child {pid} survived timeout")

    def test_call_timeout_covers_blocked_stdin_write(self):
        import subprocess
        import threading

        runner = self.delegate.delegate_runner

        class BlockingStdin:
            def __init__(self) -> None:
                self.closed = False
                self.write_started = threading.Event()

            def write(self, data: bytes) -> int:
                self.write_started.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if self.closed:
                        raise BrokenPipeError()
                    time.sleep(0.05)
                return len(data)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        class EmptyPipe:
            def read(self, _size: int = -1) -> bytes:
                time.sleep(0.05)
                return b""

            def close(self) -> None:
                return None

        stdin = BlockingStdin()
        process = mock.Mock()
        process.stdin = stdin
        process.stdout = EmptyPipe()
        process.stderr = EmptyPipe()
        process.poll = mock.Mock(return_value=None)
        process.wait = mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.05))

        started = time.monotonic()
        with (
            mock.patch.object(runner, "_terminate_call_process") as terminate,
            self.assertRaises(runner.RunnerLaunchError) as ctx,
        ):
            runner._bounded_call_communicate(
                process,
                b"x" * 1024,
                timeout=1,
                max_stdout=1024,
                max_stderr=1024,
            )
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.error, "call_timeout")
        self.assertTrue(stdin.write_started.wait(timeout=1))
        self.assertTrue(stdin.closed)
        terminate.assert_called_once_with(process)
        self.assertLess(elapsed, 3.0)

    def test_call_timeout_covers_real_full_pipe(self):
        # Regression for the close-before-terminate deadlock: a real child that
        # never reads stdin, fed a payload larger than the pipe buffer. The
        # cooperative BlockingStdin fake above cannot catch this because a real
        # buffered writer's close() contends with the blocked write() lock.
        import subprocess

        runner = self.delegate.delegate_runner
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # so termination hits the child's group, not ours
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        started = time.monotonic()
        with self.assertRaises(runner.RunnerLaunchError) as ctx:
            runner._bounded_call_communicate(
                process,
                b"x" * (1024 * 1024),
                timeout=1,
                max_stdout=1024,
                max_stderr=1024,
            )
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.error, "call_timeout")
        # Without the fix this hangs on the full pipe far past the deadline.
        self.assertLess(elapsed, 8.0)
        # The child was terminated as part of the timeout path.
        self.assertIsNotNone(process.poll())

    def test_copy_auth_is_private_copy_not_hardlink(self):
        runner = self.delegate.delegate_runner
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "auth.json"
            dst = Path(tmp) / "ephemeral" / "auth.json"
            dst.parent.mkdir()
            src.write_text('{"token":"secret"}', encoding="utf-8")
            runner._copy_auth(str(src), str(dst))
            self.assertNotEqual(src.stat().st_ino, dst.stat().st_ino)
            dst.write_text('{"token":"mutated"}', encoding="utf-8")
            self.assertEqual(src.read_text(encoding="utf-8"), '{"token":"secret"}')

    def test_call_stderr_overflow_uses_distinct_error(self):
        import subprocess

        runner = self.delegate.delegate_runner

        class GrowingStderr:
            def __init__(self) -> None:
                self._sent = False

            def read(self, _size: int = -1) -> bytes:
                if self._sent:
                    return b""
                self._sent = True
                return b"e" * 64

            def close(self) -> None:
                return None

        class EmptyStdout:
            def read(self, _size: int = -1) -> bytes:
                return b""

            def close(self) -> None:
                return None

        process = mock.Mock()
        process.stdin = None
        process.stdout = EmptyStdout()
        process.stderr = GrowingStderr()
        process.poll = mock.Mock(return_value=None)
        process.wait = mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.05))

        with (
            mock.patch.object(runner, "_terminate_call_process"),
            self.assertRaises(runner.RunnerLaunchError) as ctx,
        ):
            runner._bounded_call_communicate(
                process,
                None,
                timeout=2,
                max_stdout=1024,
                max_stderr=16,
            )
        self.assertEqual(ctx.exception.error, "call_stderr_overflow")
        self.assertIn("stderr", ctx.exception.message)

    def test_call_timeout_main_returns_stable_json_error(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        binary = Path(temp.name) / "claude"
        binary.write_text("#!/usr/bin/env bash\nsleep 60\n", encoding="utf-8")
        binary.chmod(0o755)
        code, stdout, _stderr = self.run_main(
            ["--json", "claude", "call", "--pure", "--timeout", "1", "answer"],
            path_prefix=Path(temp.name),
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "call_timeout")
        self.assertEqual(payload["exitCode"], 1)

    def test_call_failure_stderr_is_bounded_and_redacted(self):
        prompt = "PROMPT_SECRET_SENTINEL"
        schema = "SCHEMA_SECRET_SENTINEL"
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fail.py"
            script.write_text(
                "import sys\n"
                f"sys.stderr.write({('x' * 9000 + prompt + schema)!r})\n"
                "raise SystemExit(9)\n",
                encoding="utf-8",
            )
            result = self.delegate.delegate_runner.execute_call(
                [sys.executable, str(script)],
                tmp,
                harness="codex",
                sensitive_texts=(prompt, schema),
            )
        self.assertLessEqual(len(result.stderr_tail), self.delegate.profiles.STDERR_TAIL_LIMIT)
        self.assertNotIn(prompt, result.stderr_tail)
        self.assertNotIn(schema, result.stderr_tail)


if __name__ == "__main__":
    import unittest

    unittest.main()
