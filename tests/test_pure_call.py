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

    def test_opencode_pure_keeps_binary_pure_and_denies_every_tool(self):
        request = self.delegate.build_request(
            "opencode",
            "call",
            None,
            self.delegate.ResolvedWorkspace("/tmp/empty", "directory"),
            "hostile prompt",
            self.delegate.DEFAULT_CONFIG,
            True,
            pure=True,
        )
        self.assertIn("--pure", request.argv)
        self.assertNotIn("hostile prompt", request.argv)
        permissions = json.loads(request.env_overrides["OPENCODE_PERMISSION"])
        self.assertTrue(permissions)
        self.assertEqual(set(permissions.values()), {"deny"})
        for tool in ("read", "glob", "grep", "edit", "bash", "task", "webfetch"):
            self.assertEqual(permissions[tool], "deny")

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
        with mock.patch.object(self.delegate.delegate_runner, "execute_call", return_value=fake):
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
            contents = '{"type":"object","properties":{"answer":{"type":"string"}}}'
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
            self.delegate.build_codex_argv(
                self.delegate.DEFAULT_CONFIG["codex"],
                "call",
                "/tmp/call",
                None,
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

        no_usage = [{"type": "result", "result": "ok", "is_error": False}]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "claude_no_usage.py"
            script.write_text(f"print({json.dumps(json.dumps(no_usage))})\n", encoding="utf-8")
            result = self.delegate.delegate_runner.execute_call(
                [sys.executable, str(script)], tmp, harness="claude", pure=True
            )
        self.assertEqual(result.usage, {"basis": "unavailable"})

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
            with mock.patch.object(
                self.delegate.delegate_runner, "execute_call", return_value=fake
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
