import io
import json
import os
import sys
import unittest

from tests.snapshot_commands_test_base import ROOT, SnapshotCommandTestBase


class SnapshotRunOutputTests(SnapshotCommandTestBase):
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

    def test_runs_running_and_stale_filters_split_effective_status(self):
        self.write_run(status="succeeded", pid=None)
        _, running_alias = self.write_run()
        _, stale_alias = self.write_run(pid=999999999)

        running_stdout = io.StringIO()
        self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "runs", "--running"],
            stdout=running_stdout,
        )
        running_payload = json.loads(running_stdout.getvalue())
        self.assertEqual(running_payload["mode"], "running")
        self.assertEqual([run["alias"] for run in running_payload["runs"]], [running_alias])
        self.assertEqual(running_payload["runs"][0]["rawStatus"], "running")
        self.assertEqual(running_payload["runs"][0]["effectiveStatus"], "running")

        stale_stdout = io.StringIO()
        self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "runs", "--stale"],
            stdout=stale_stdout,
        )
        stale_payload = json.loads(stale_stdout.getvalue())
        self.assertEqual(stale_payload["mode"], "stale")
        self.assertEqual([run["alias"] for run in stale_payload["runs"]], [stale_alias])
        stale_run = stale_payload["runs"][0]
        self.assertEqual(stale_run["rawStatus"], "running")
        self.assertEqual(stale_run["effectiveStatus"], "stale")
        self.assertEqual(stale_run["staleReason"], "dead_pid")
        self.assertIn(
            self.delegate.run_registry.snapshot_command(stale_alias, cwd=str(self.workspace)),
            stale_run["nextActions"],
        )

    def test_runs_json_shape(self):
        _, alias = self.write_run()
        stdout = io.StringIO()
        self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "runs", "--limit", "1"], stdout=stdout
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema"], "delegate.runs.v1")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual(
            payload["runs"][0]["snapshotCommand"],
            self.delegate.run_registry.snapshot_command(alias, cwd=str(self.workspace)),
        )

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

    def test_run_output_bare_harness_resolves_latest_with_resolution_fields(self):
        self.write_run(
            harness="codex",
            assistant_text="first",
            started_at="2026-05-20T12:00:00Z",
        )
        latest_run_id, latest_alias = self.write_run(
            harness="codex",
            assistant_text="second",
            started_at="2026-05-20T12:05:00Z",
        )
        run_path = self.registry.run_directory(self.registry_root, latest_run_id)
        (run_path / "completion-report.md").write_text("# latest done\n", encoding="utf-8")
        stdout = io.StringIO()

        code = self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                "codex",
                "--completion-report",
            ],
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["runId"], latest_run_id)
        self.assertEqual(payload["alias"], latest_alias)
        self.assertEqual(payload["requestedHandle"], "codex")
        self.assertEqual(payload["resolvedHandle"], latest_alias)
        self.assertEqual(payload["resolutionKind"], "latest")
        self.assertEqual(payload["sections"]["completionReport"]["content"], "# latest done\n")

    def test_run_output_defaults_to_completion_report(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "completion-report.md").write_text("# default done\n", encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        self.assertIn("# default done", stdout.getvalue())

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

    def test_run_output_default_recovers_codex_final_agent_message(self):
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
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
                                "type": "command_execution",
                                "command": "rg run-output",
                                "status": "completed",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "Status: completed\n- recovered bare default",
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
            ["--cwd", str(self.workspace), "run-output", alias],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("recovered bare default", output)
        self.assertNotIn("I am checking the repo.", output)

    def test_run_output_completion_report_recovers_cursor_assistant_message(self):
        run_id, alias = self.write_run(harness="cursor", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Working..."}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Status: completed\n- cursor final",
                                    }
                                ],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--completion-report"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("cursor final", output)
        self.assertNotIn("Working...", output)

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
        self.assertEqual(report["recoveryQuality"], "explicit_completion")
        self.assertIn("json fallback", report["content"])

    def test_synthetic_completion_report_redacts_by_default_and_can_be_disabled(self):
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        api_secret = "sk-abcdefghijklmnopqrstuvwxyz"  # pragma: allowlist secret
        private_secret = "base64privatekeypayload123456"
        bracket_secret = "openai-secret-from-python"
        final_text = (
            "Status: completed\n"
            f"- env OPENAI_API_KEY={api_secret}\n"
            f"- private PRIVATE_KEY={private_secret}\n"
            f'- bracket os.environ["OPENAI_API_KEY"] = "{bracket_secret}"'
        )
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": final_text},
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        redacted_stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stdout=redacted_stdout,
        )
        self.assertEqual(code, 0)
        redacted_report = json.loads(redacted_stdout.getvalue())["sections"]["completionReport"]
        self.assertTrue(redacted_report["synthetic"])
        self.assertIn("***", redacted_report["content"])
        self.assertNotIn(api_secret, redacted_report["content"])
        self.assertNotIn(private_secret, redacted_report["content"])
        self.assertNotIn(bracket_secret, redacted_report["content"])

        unredacted_stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
                "--no-redact",
            ],
            stdout=unredacted_stdout,
        )
        self.assertEqual(code, 0)
        unredacted_report = json.loads(unredacted_stdout.getvalue())["sections"]["completionReport"]
        self.assertIn(api_secret, unredacted_report["content"])
        self.assertIn(private_secret, unredacted_report["content"])
        self.assertIn(bracket_secret, unredacted_report["content"])

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

    def test_run_output_completion_report_recovery_is_byte_bounded(self):
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        stale = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Status: completed\n- stale"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
        oversized = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "noise",
                    "status": "completed",
                    "aggregated_output": "x" * (self.delegate.RECOVERY_STDOUT_TAIL_BYTES + 1000),
                },
            }
        )
        final = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Status: completed\n- final"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
        (run_path / "stdout.log").write_text(
            "\n".join([*stale, oversized, *final]) + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "run-output", alias, "--completion-report"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())["sections"]["completionReport"]
        self.assertTrue(report["truncated"])
        self.assertEqual(report["tailBytes"], self.delegate.RECOVERY_STDOUT_TAIL_BYTES)
        self.assertIn("final", report["content"])
        self.assertNotIn("stale", report["content"])

    def test_run_output_completion_report_recovery_fails_when_final_outside_byte_bound(self):
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        stale = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Status: completed\n- stale"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
        oversized = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "noise",
                    "status": "completed",
                    "aggregated_output": "x" * (self.delegate.RECOVERY_STDOUT_TAIL_BYTES + 1000),
                },
            }
        )
        (run_path / "stdout.log").write_text(
            "\n".join([*stale, oversized]) + "\n", encoding="utf-8"
        )
        stderr = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--completion-report"],
            stderr=stderr,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("missing_completion_report", stderr.getvalue())

    def test_run_output_completion_report_json_failure_includes_diagnostics(self):
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text("partial stdout\n", encoding="utf-8")
        (run_path / "stderr.log").write_text("warning\n", encoding="utf-8")
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
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "missing_completion_report")
        self.assertEqual(payload["diagnostics"]["status"], "succeeded")
        self.assertTrue(payload["diagnostics"]["stdout"]["present"])
        self.assertGreater(payload["diagnostics"]["stdout"]["bytes"], 0)
        self.assertTrue(payload["diagnostics"]["stderr"]["present"])
        self.assertIn(
            f"--stdout --tail {self.delegate.RUN_OUTPUT_DEFAULT_TAIL_LINES}",
            payload["nextActions"][0],
        )
        self.assertIn("--stderr", payload["nextActions"][1])

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

    def test_run_output_completion_report_rejects_droid_interim_message(self):
        run_id, alias = self.write_run(harness="droid", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "I'm still investigating.\n- checking files\n- running tests",
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

        json_stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            stdout=json_stdout,
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        payload = json.loads(json_stdout.getvalue())
        self.assertEqual(payload["error"], "missing_completion_report")
        self.assertEqual(payload["diagnostics"]["recovery"]["quality"], "housekeeping_fallback")

    def test_run_output_recovers_substantive_report_over_housekeeping_for_droid(self):
        run_id, alias = self.write_run(harness="droid", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": "Status: completed\n- fixed recovery\n- added tests",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": "Plan is up-to-date.",
                        }
                    ),
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
        report = json.loads(stdout.getvalue())["sections"]["completionReport"]
        self.assertEqual(report["recoveryQuality"], "substantive_assistant_fallback")
        self.assertIn("fixed recovery", report["content"])
        self.assertNotIn("Plan is up-to-date", report["content"])

    def test_run_output_recovers_substantive_report_over_long_progress_message(self):
        run_id, alias = self.write_run(harness="droid", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": "Status: completed\n- delivered the fix",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "I am still investigating the repository layout, reading files, "
                                "and running additional checks before I can finalize anything."
                            ),
                        }
                    ),
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
        report = json.loads(stdout.getvalue())["sections"]["completionReport"]
        self.assertEqual(report["recoveryQuality"], "substantive_assistant_fallback")
        self.assertIn("delivered the fix", report["content"])
        self.assertNotIn("still investigating", report["content"])

    def test_run_output_recovers_structured_report_with_interior_progress_line(self):
        run_id, alias = self.write_run(harness="droid", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        report_text = (
            "## Summary\n"
            "- Fixed the recovery classifier.\n"
            "- Added regression coverage.\n\n"
            "Let me check the failing case below.\n\n"
            "## Verification\n"
            "- python3 -m unittest tests.test_snapshot_run_output"
        )
        (run_path / "stdout.log").write_text(
            json.dumps({"type": "message", "role": "assistant", "content": report_text}) + "\n",
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
        report = json.loads(stdout.getvalue())["sections"]["completionReport"]
        self.assertEqual(report["recoveryQuality"], "substantive_assistant_fallback")
        self.assertIn("Fixed the recovery classifier", report["content"])
        self.assertIn("Let me check the failing case below", report["content"])

    def test_run_output_default_marks_housekeeping_only_recovery_quality_in_diagnostics(self):
        run_id, alias = self.write_run(harness="droid", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "The final report was delivered in the previous message.",
                }
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
            ],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        sections = json.loads(stdout.getvalue())["sections"]
        self.assertNotIn("completionReport", sections)
        self.assertIn(
            "recovery quality: housekeeping_fallback",
            sections["diagnostics"]["content"],
        )

    def test_run_output_completion_report_codex_never_recovers_interim_message(self):
        run_id, alias = self.write_run(harness="codex", status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I'll start by reading files."},
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

    def test_run_output_default_falls_back_to_bounded_diagnostics(self):
        run_id, alias = self.write_run(status="succeeded", pid=None)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        lines = [f"line{i}" for i in range(self.delegate.RUN_OUTPUT_DEFAULT_TAIL_LINES + 5)]
        (run_path / "stdout.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("=== stdout (last 80 lines;", output)
        self.assertNotIn("line0", output)
        self.assertIn("line84", output)
        self.assertIn("=== diagnostics ===", output)
        self.assertIn("--stdout --tail 80", output)

    def test_stale_status_when_pid_dead(self):
        _, alias = self.write_run(pid=999999999)
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("stale", output)
        self.assertIn("status detail: raw=running effective=stale", output)
        self.assertIn("stale reason: dead_pid", output)
        self.assertIn(
            self.delegate.run_registry.snapshot_command(alias, cwd=str(self.workspace)),
            output,
        )

    def test_snapshot_json_includes_stale_diagnostics(self):
        _, alias = self.write_run(pid=999999999)
        stdout = io.StringIO()
        self.delegate.main(
            ["--cwd", str(self.workspace), "snapshot", alias, "--json"], stdout=stdout
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "stale")
        self.assertEqual(payload["rawStatus"], "running")
        self.assertEqual(payload["effectiveStatus"], "stale")
        self.assertEqual(payload["staleReason"], "dead_pid")
        self.assertIn(
            self.delegate.run_registry.run_output_command(
                alias,
                completion_report=True,
                cwd=str(self.workspace),
            ),
            payload["nextActions"],
        )

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
        self.assertFalse(stdout_section["truncated"])
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
        self.assertEqual(stdout_section["rawOutputBytes"], len("line1\nline2\n"))
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

    def test_run_output_stdout_without_tail_or_raw_defaults_to_bounded_tail(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        lines = [f"line{i}" for i in range(self.delegate.RUN_OUTPUT_DEFAULT_TAIL_LINES + 1)]
        (run_path / "stdout.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--stdout"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertNotIn("line0", output)
        self.assertIn("line80", output)

    def test_run_output_huge_single_line_tail_is_char_capped_at_end(self):
        from delegate_agent.log_output import RUN_OUTPUT_DEFAULT_MAX_CHARS

        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        prefix = "START-"
        suffix = "-END"
        filler_len = RUN_OUTPUT_DEFAULT_MAX_CHARS + 5000
        (run_path / "stdout.log").write_text(
            prefix + ("x" * filler_len) + suffix + "\n",
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
                "--stdout",
                "--tail",
                "1",
            ],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        section = json.loads(stdout.getvalue())["sections"]["stdout"]
        self.assertTrue(section["charTruncated"])
        self.assertEqual(section["maxChars"], RUN_OUTPUT_DEFAULT_MAX_CHARS)
        self.assertEqual(section["returnedChars"], RUN_OUTPUT_DEFAULT_MAX_CHARS)
        self.assertGreater(section["omittedChars"], 0)
        self.assertNotIn("START-", section["content"])
        self.assertIn(suffix, section["content"])

    def test_run_output_max_chars_override(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(("y" * 500) + "\n", encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--stdout",
                "--tail",
                "1",
                "--max-chars",
                "100",
            ],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        section = json.loads(stdout.getvalue())["sections"]["stdout"]
        self.assertTrue(section["charTruncated"])
        self.assertEqual(section["maxChars"], 100)
        self.assertEqual(section["returnedChars"], 100)
        self.assertEqual(section["omittedChars"], 401)
        self.assertEqual(len(section["content"]), 100)

    def test_run_output_raw_stdout_has_no_char_cap_metadata(self):
        run_id, alias = self.write_run()
        run_path = self.registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text(("z" * 100_000) + "\n", encoding="utf-8")
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--json", "--cwd", str(self.workspace), "run-output", alias, "--raw"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        section = json.loads(stdout.getvalue())["sections"]["stdout"]
        self.assertNotIn("maxChars", section)
        self.assertNotIn("charTruncated", section)
        self.assertEqual(section["rawOutputBytes"], 100_001)
        self.assertEqual(len(section["content"]), 100_001)


if __name__ == "__main__":
    unittest.main()
