import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
CLI_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"
RENDERING_PATH = ROOT / "src" / "delegate_agent" / "rendering.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SnapshotCommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(CLI_PATH, "delegate_cli_snapshot_test")
        self.registry = load_module(REGISTRY_PATH, "delegate_registry_snapshot_test")
        self.rendering = load_module(RENDERING_PATH, "delegate_rendering_snapshot_test")
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.registry_root = self.registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_run(
        self,
        *,
        harness: str = "cursor",
        status: str = "running",
        assistant_text: str = "planning the change",
        stdout_bytes: int = 0,
        stderr_bytes: int = 0,
        pid: int | None = os.getpid(),
        started_at: str | None = None,
    ) -> tuple[str, str]:
        run_id, alias = self.registry.register_run(self.registry_root, harness=harness)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        started = started_at or datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        snapshot = {
            "schema": "delegate.snapshot.v1",
            "ok": True,
            "alias": alias,
            "runId": run_id,
            "harness": harness,
            "status": status,
            "startedAt": started,
            "assistantText": assistant_text,
            "assistantTextChars": len(assistant_text),
            "assistantTextTruncated": False,
            "recentEvents": [{"kind": "tool.started", "tool": "read", "path": "README.md"}],
            "warnings": [],
        }
        state = {
            "schema": "delegate.state.v1",
            "runId": run_id,
            "alias": alias,
            "status": status,
            "stdoutBytes": stdout_bytes,
            "stderrBytes": stderr_bytes,
            "lastActivityAt": started,
            "current": "editing cli.py",
        }
        if pid is not None:
            state["pid"] = pid
        self.registry.write_json_atomic(run_path / "snapshot.json", snapshot)
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.registry.write_json_atomic(
            run_path / "manifest.json",
            {
                "schema": "delegate.manifest.v1",
                "runId": run_id,
                "alias": alias,
                "harness": harness,
                "startedAt": started,
                "cwd": str(self.workspace),
                "mode": "work",
                "model": "composer-2.5",
            },
        )
        if stdout_bytes:
            (run_path / "stdout.log").write_bytes(b"x" * stdout_bytes)
        if stderr_bytes:
            (run_path / "stderr.log").write_bytes(b"y" * stderr_bytes)
        return run_id, alias

    def test_snapshot_codex_null_model_round_trips(self):
        run_id, alias = self.write_run(
            harness="codex",
            assistant_text="codex planning",
        )
        run_path = self.registry.run_directory(self.registry_root, run_id)
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        manifest["model"] = None
        self.registry.write_json_atomic(run_path / "manifest.json", manifest)
        snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
        snapshot["model"] = None
        self.registry.write_json_atomic(run_path / "snapshot.json", snapshot)
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "snapshot", alias], stdout=stdout
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIsNone(payload.get("model"))

    def test_snapshot_exact_handle_text(self):
        _, alias = self.write_run()
        stdout = io.StringIO()
        code = self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn(alias, output)
        self.assertIn("running", output)
        self.assertIn("assistant text:", output)
        self.assertIn("planning the change", output)
        self.assertNotIn("stdout.log", output)

    def test_snapshot_json_includes_bounded_fields(self):
        _, alias = self.write_run()
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "snapshot", alias], stdout=stdout
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema"], "delegate.snapshot.v1")
        self.assertIn("assistantText", payload)
        self.assertIn("recentEvents", payload)

    def test_snapshot_latest_harness(self):
        self.write_run(
            harness="cursor",
            assistant_text="first",
            started_at="2026-05-20T12:00:00Z",
        )
        _, latest_alias = self.write_run(
            harness="cursor",
            assistant_text="second",
            started_at="2026-05-20T12:05:00Z",
        )
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "snapshot", "--latest", "cursor"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        self.assertIn(latest_alias, stdout.getvalue())

    def test_snapshot_unknown_handle_returns_suggestions(self):
        _, first_alias = self.write_run()
        self.write_run()
        stderr = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "snapshot", "cursor-9"],
            stdout=sys.stdout,
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("Suggestions", stderr.getvalue())
        self.assertIn(first_alias, stderr.getvalue())

    def test_redact_string_preserves_separator_format(self):
        self.assertIn(":", self.rendering.redact_string("API_KEY: secret-value"))
        self.assertIn("=", self.rendering.redact_string("token=secret-value"))

    def test_redact_string_covers_common_credential_shapes(self):
        jwt_like = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        cases = [
            ("Authorization: Bearer bearer-token-12345", "bearer-token-12345"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("Authorization: ApiKey api-key-token-12345", "api-key-token-12345"),
            ("Authorization: opaque-authorization-token-12345", "opaque-authorization-token-12345"),
            ("Bearer whitespace-token-12345", "whitespace-token-12345"),
            ("token=secret-token-value", "secret-token-value"),
            ("api_key=secret-api-key", "secret-api-key"),
            ("password: super-secret", "super-secret"),
            (f"jwt {jwt_like}", jwt_like),
            # JSON-quoted keys (the value sits after a closing quote on the key).
            ('{"secret": "topsecretvalue123456"}', "topsecretvalue123456"),
            ('"password": "hunter2hunter2"', "hunter2hunter2"),
            ('{"Authorization": "Bearer abcdef123456"}', "abcdef123456"),
            # Provider token shapes.
            ("sk-proj-aBcD1234efGH5678ijKL", "aBcD1234efGH5678ijKL"),
            ("sk-ant-api03-abcdefgh12345678", "abcdefgh12345678"),
            ("ghs_1234567890abcdefghijklmnopqrstuvwx", "1234567890abcdefghijklmnopqrstuvwx"),
            (
                "github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz",
                "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz",
            ),
            ("AKIAIOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
            ("AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe", "SyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe"),
            # Connection-string passwords (with and without a username).
            ("postgres://admin:supersecret@db.example.com/x", "supersecret"),
            ("redis://:redacted-pass-9999@localhost:6379", "redacted-pass-9999"),
        ]
        for raw, secret in cases:
            with self.subTest(raw=raw):
                redacted = self.rendering.redact_string(raw)
                self.assertIn("***", redacted)
                self.assertNotIn(secret, redacted)

    def test_redact_string_preserves_ordinary_dotted_identifiers(self):
        # The JWT matcher is anchored on the eyJ header so it must not shred
        # tracebacks, module paths, or dotted filenames the parent agent reads.
        survivors = [
            "No module named delegate_agent.run_registry.parser_helper",
            "see file_one_long.module_two_long.module_three_x",
            "2026-06-02.error_log_file.backup_archive_v2",
            "the secret to success is hard work",
            "git@github.com:user/repo.git",
        ]
        for text in survivors:
            with self.subTest(text=text):
                self.assertEqual(self.rendering.redact_string(text), text)

    def test_redact_string_is_linear_on_pathological_input(self):
        # Regression guard: a long dotted string previously triggered quadratic
        # backtracking in the connection-string scheme matcher (~12s). Linear now.
        payload = ("a" * 10 + ".") * 20000
        start = time.perf_counter()
        self.rendering.redact_string(payload)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0, f"redaction took {elapsed:.2f}s on pathological input")

    def test_snapshot_redacts_secrets_by_default(self):
        _, alias = self.write_run(  # pragma: allowlist secret
            assistant_text="export API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
        )
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("***", output)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", output)  # pragma: allowlist secret

    def test_snapshot_no_redact_preserves_secret_like_text(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz"  # pragma: allowlist secret
        _, alias = self.write_run(assistant_text=f"token {secret}")
        stdout = io.StringIO()
        self.delegate.main(
            ["--cwd", str(self.workspace), "snapshot", "--no-redact", alias],
            stdout=stdout,
        )
        self.assertIn(secret, stdout.getvalue())

    def test_snapshot_warns_on_large_logs(self):
        large = self.registry.LARGE_LOG_WARN_BYTES + 1
        _, alias = self.write_run(stdout_bytes=large)
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        self.assertIn("stdout.log > 50 MB", stdout.getvalue())

    def test_runs_lists_recent_rows(self):
        self.write_run(status="succeeded", pid=None)
        self.write_run()
        stdout = io.StringIO()
        code = self.delegate.main(["--cwd", str(self.workspace), "runs"], stdout=stdout)
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("alias", output)
        self.assertIn("cursor", output)

    def test_runs_active_filter(self):
        self.write_run(status="succeeded", pid=None)
        _, active_alias = self.write_run()
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "runs", "--active"], stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("mode: active", output)
        self.assertIn(active_alias, output)
        lines = [
            line for line in output.splitlines() if line and not line.startswith(("alias", "mode:"))
        ]
        self.assertEqual(len(lines), 1)
        self.assertIn(active_alias, lines[0])

    def test_runs_json_shape(self):
        self.write_run()
        stdout = io.StringIO()
        self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "runs", "--limit", "1"], stdout=stdout
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema"], "delegate.runs.v1")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(len(payload["runs"]), 1)
        self.assertIn("snapshotCommand", payload["runs"][0])

    def test_run_output_completion_report(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "completion-report.md").write_text("# done\n", encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        self.assertIn("# done", stdout.getvalue())

    def test_run_output_completion_report_falls_back_to_codex_stdout(self):
        run_id, alias = self.write_run(status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "I am checking the repo.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "reasoning",
                                "text": "hidden reasoning",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "Status: completed\n- final from stdout",
                            },
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("final from stdout", output)
        self.assertNotIn("I am checking the repo.", output)
        self.assertNotIn("hidden reasoning", output)

    def test_run_output_json_marks_synthetic_completion_report(self):
        run_id, alias = self.write_run(status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "Status: completed\n- json fallback",
                            },
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        report = payload["sections"]["completionReport"]
        self.assertTrue(report["synthetic"])
        self.assertEqual(report["source"], "stdout.log")
        self.assertIn("json fallback", report["content"])

    def test_run_output_recovers_completion_from_real_codex_fixture(self):
        # End-to-end recovery against a sanitized capture of a real `codex` run
        # (see tests/fixtures/codex_real_stream.jsonl). Synthetic fixtures could
        # drift from Codex's actual wire format without this catching it.
        fixture = ROOT / "tests" / "fixtures" / "codex_real_stream.jsonl"
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())["sections"]["completionReport"]
        self.assertTrue(report["synthetic"])
        self.assertEqual(report["source"], "stdout.log")
        self.assertTrue(report["content"].startswith("Verdict:"))
        # Intermediate progress and hidden reasoning must not leak into the report.
        self.assertNotIn("skill pass", report["content"].lower())
        self.assertNotIn("reasoning", report["content"].lower())

    def test_run_output_recovers_completion_from_long_stdout(self):
        # Recovery reads a bounded tail, not the whole log. A completion at the end
        # of a stream longer than the tail window must still be recovered.
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        filler = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "noise", "status": "completed"},
            }
        )
        final = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Status: completed\n- final answer"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
        lines = [filler] * (self.delegate.RECOVERY_STDOUT_TAIL_LINES + 500) + final
        (run_path / "stdout.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--completion-report"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        self.assertIn("final answer", stdout.getvalue())

    def test_run_output_completion_report_recovery_skipped_for_running_run(self):
        run_id, alias = self.write_run(status="running", pid=os.getpid())
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "Status: completed\n- final from stdout",
                            },
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        code = self.delegate.main(
            [
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("missing_completion_report", stderr.getvalue())

    def test_run_output_completion_report_recovery_requires_final_text(self):
        run_id, alias = self.write_run(status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "I am still investigating.",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        code = self.delegate.main(
            [
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("missing_completion_report", stderr.getvalue())

    def test_run_output_completion_report_ignores_superseded_codex_progress(self):
        run_id, alias = self.write_run(status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "I am still investigating.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "type": "command_execution",
                                "command": "python3 -m unittest",
                                "status": "in_progress",
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        code = self.delegate.main(
            [
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("missing_completion_report", stderr.getvalue())

    def test_run_output_completion_report_requires_codex_turn_completion(self):
        run_id, alias = self.write_run(status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "I am still investigating.",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        code = self.delegate.main(
            [
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("missing_completion_report", stderr.getvalue())

    def test_run_output_stdout_tail_only(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
        stdout = io.StringIO()
        self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--stdout", "--tail", "2"],
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("line2", output)
        self.assertIn("line3", output)
        self.assertNotIn("line1", output)

    def test_run_output_requires_output_selector(self):
        _, alias = self.write_run()
        stderr = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias],
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("missing_output_selector", stderr.getvalue())

    def test_stale_status_when_pid_dead(self):
        _, alias = self.write_run(pid=999999999)
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        self.assertIn("stale", stdout.getvalue())

    def test_stale_status_when_pid_missing(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state.pop("pid", None)
        self.registry.write_json_atomic(run_path / "state.json", state)
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        self.assertIn("stale", stdout.getvalue())

    def test_runs_active_includes_stale_runs(self):
        self.write_run(status="succeeded", pid=None)
        _, stale_alias = self.write_run(pid=999999999)
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "runs", "--active"], stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("mode: active", output)
        self.assertIn(stale_alias, output)
        lines = [
            line for line in output.splitlines() if line and not line.startswith(("mode:", "alias"))
        ]
        self.assertEqual(len(lines), 1)

    def test_runs_recent_mode_visible_in_json(self):
        self.write_run()
        stdout = io.StringIO()
        self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "runs", "--recent"], stdout=stdout
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "recent")

    def test_runs_json_default_mode_is_recent(self):
        self.write_run()
        stdout = io.StringIO()
        self.delegate.main(["--json", "--cwd", str(self.workspace), "runs"], stdout=stdout)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "recent")

    def test_run_output_json_nests_content_with_metadata(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text("line1\n", encoding="utf-8")
        stdout = io.StringIO()
        self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--stdout",
                "--tail",
                "1",
            ],
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        stdout_section = payload["sections"]["stdout"]
        self.assertIn("bytes", stdout_section)
        self.assertTrue(stdout_section["truncated"])
        self.assertEqual(stdout_section["content"], "line1\n")

    def test_run_output_json_raw_stdout_not_truncated(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text("line1\nline2\n", encoding="utf-8")
        stdout = io.StringIO()
        self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "run-output", alias, "--raw"],
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        stdout_section = payload["sections"]["stdout"]
        self.assertFalse(stdout_section["truncated"])
        self.assertEqual(stdout_section["content"], "line1\nline2\n")

    def test_run_output_redacts_secrets_by_default(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        secret = "sk-abcdefghijklmnopqrstuvwxyz"  # pragma: allowlist secret
        (run_path / "stdout.log").write_text(f"API_KEY={secret}\n", encoding="utf-8")
        stdout = io.StringIO()
        self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--stdout", "--tail", "1"],
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("***", output)
        self.assertNotIn(secret, output)

    def test_run_output_raw_redacts_secrets_by_default(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        secret = "sk-abcdefghijklmnopqrstuvwxyz"  # pragma: allowlist secret
        (run_path / "stdout.log").write_text(f"token: {secret}\n", encoding="utf-8")
        stdout = io.StringIO()
        self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--raw"],
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("***", output)
        self.assertNotIn(secret, output)

    def test_run_output_redacts_authorization_header_by_default(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        secret = "bearer-output-token-12345"
        (run_path / "stdout.log").write_text(
            f"Authorization: Bearer {secret}\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--stdout", "--tail", "1"],
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("Authorization: ***", output)
        self.assertNotIn("Bearer", output)
        self.assertNotIn(secret, output)

    def test_run_output_no_redact_preserves_completion_report_secrets(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
        (run_path / "completion-report.md").write_text(secret, encoding="utf-8")
        stdout = io.StringIO()
        self.delegate.main(
            [
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
                "--no-redact",
            ],
            stdout=stdout,
        )
        self.assertIn(secret, stdout.getvalue())

    def test_run_output_stdout_without_tail_or_raw_errors_clearly(self):
        _, alias = self.write_run()
        stderr = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--stdout"],
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        message = stderr.getvalue()
        self.assertIn("missing_tail", message)
        self.assertIn("--tail", message)
        self.assertIn("--raw", message)

    def test_render_worktree_cleanup_commands_formats_all_lines(self):
        cleanup = {
            "safe": "delegate worktree remove demo",
            "forceBranch": "delegate worktree remove demo --force-branch",
            "discardUncommitted": "delegate worktree remove demo --discard-uncommitted",
            "force": "delegate worktree remove demo --force",
            "rawGit": "git -C /repo worktree remove /wt && git -C /repo branch -d demo",
        }
        stdout = io.StringIO()
        self.rendering.render_worktree_cleanup_commands(cleanup, stdout)
        output = stdout.getvalue()
        self.assertEqual(
            output,
            (
                "cleanup (refuses dirty / unmerged):       delegate worktree remove demo\n"
                "cleanup (allow unmerged branch deletion): delegate worktree remove demo --force-branch\n"
                "cleanup (DISCARD uncommitted edits):      delegate worktree remove demo --discard-uncommitted\n"
                "cleanup (DISCARD edits + delete branch):  delegate worktree remove demo --force\n"
                "raw git equivalent:                       git -C /repo worktree remove /wt && git -C /repo branch -d demo\n"
            ),
        )

    def test_render_snapshot_text_uses_shared_cleanup_renderer(self):
        stdout = io.StringIO()
        self.rendering.render_snapshot_text(
            {
                "alias": "demo",
                "status": "running",
                "worktreeCleanupCommands": {
                    "safe": "delegate worktree remove demo",
                    "forceBranch": "delegate worktree remove demo --force-branch",
                    "discardUncommitted": "delegate worktree remove demo --discard-uncommitted",
                    "force": "delegate worktree remove demo --force",
                    "rawGit": "git -C /repo worktree remove /wt",
                },
            },
            stdout,
        )
        output = stdout.getvalue()
        self.assertIn(
            "cleanup (refuses dirty / unmerged):       delegate worktree remove demo", output
        )
        self.assertIn(
            "cleanup (DISCARD edits + delete branch):  delegate worktree remove demo --force",
            output,
        )
        self.assertIn(
            "raw git equivalent:                       git -C /repo worktree remove /wt", output
        )

    def test_render_worktree_list_text_includes_auto_prune_footer(self):
        stdout = io.StringIO()
        self.rendering.render_worktree_list_text(
            {
                "entries": [],
                "autoPrune": {
                    "ok": True,
                    "removed": [{"alias": "cursor-1"}],
                    "skipped": [],
                    "errors": [],
                },
            },
            stdout,
        )
        self.assertIn("auto-prune: removed 1, skipped 0, errors 0", stdout.getvalue())

    def test_render_worktree_list_text_includes_auto_prune_skip_and_failure(self):
        skipped = io.StringIO()
        self.rendering.render_worktree_list_text(
            {
                "entries": [],
                "autoPrune": {"ok": False, "skipped": True, "reason": "lock_contended"},
            },
            skipped,
        )
        self.assertIn("auto-prune: skipped (lock_contended)", skipped.getvalue())

        failed = io.StringIO()
        self.rendering.render_worktree_list_text(
            {
                "entries": [],
                "autoPrune": {
                    "ok": False,
                    "code": "branch_remove_failed",
                    "errors": [{"code": "branch_remove_failed"}],
                },
            },
            failed,
        )
        self.assertIn("auto-prune: failed (branch_remove_failed, errors=1)", failed.getvalue())

    def test_render_worktree_remove_text_includes_branch_error(self):
        stdout = io.StringIO()
        self.rendering.render_worktree_remove_text(
            {
                "alias": "cursor-1",
                "ok": False,
                "removed": True,
                "pathRemoved": True,
                "branchRemoved": False,
                "branchRemovalError": "fatal: cannot delete branch",
                "nextActions": ["delete branch manually"],
            },
            stdout,
        )
        output = stdout.getvalue()
        self.assertIn("branchRemoved=False", output)
        self.assertIn("error: fatal: cannot delete branch", output)
        self.assertIn("delete branch manually", output)

    def test_render_worktree_gc_text_includes_warnings(self):
        stdout = io.StringIO()
        self.rendering.render_worktree_gc_text(
            {
                "reconciled": 0,
                "prunedSourceRoots": 0,
                "orphans": [],
                "warnings": [{"sourceGitRoot": "/repo", "message": "fatal: worktree list failed"}],
            },
            stdout,
        )
        output = stdout.getvalue()
        self.assertIn("warnings:", output)
        self.assertIn("/repo: fatal: worktree list failed", output)


if __name__ == "__main__":
    unittest.main()
