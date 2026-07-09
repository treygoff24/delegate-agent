from __future__ import annotations

import errno
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import delegate_agent.archived_logs as archived_logs  # noqa: E402
import delegate_agent.argv_utils as argv_utils  # noqa: E402
import delegate_agent.cli as cli  # noqa: E402
import delegate_agent.json_types as json_types  # noqa: E402
import delegate_agent.log_output as log_output  # noqa: E402
import delegate_agent.private_io as private_io  # noqa: E402
import delegate_agent.prompt_instructions as prompt_instructions  # noqa: E402
import delegate_agent.prompt_transport as prompt_transport  # noqa: E402
import delegate_agent.reasoning as reasoning  # noqa: E402
import delegate_agent.redaction as redaction  # noqa: E402
import delegate_agent.run_metadata as run_metadata  # noqa: E402
import delegate_agent.run_output_commands as run_output_commands  # noqa: E402
import delegate_agent.run_registry as run_registry  # noqa: E402
import delegate_agent.runner as runner  # noqa: E402
import delegate_agent.worktree_commands as worktree_commands  # noqa: E402
import delegate_agent.worktree_execution as worktree_execution  # noqa: E402
import delegate_agent.worktree_mgmt as worktree_mgmt  # noqa: E402


class UtilityModuleTests(unittest.TestCase):
    def test_argv_utils_replace_workspace_arg_by_engine(self):
        cases = (
            (
                "cursor",
                ["cursor-agent", "--workspace", "/old", "prompt"],
                ["cursor-agent", "--workspace", "/new", "prompt"],
            ),
            (
                "codex",
                ["codex", "exec", "--cd", "/old", "prompt"],
                ["codex", "exec", "--cd", "/new", "prompt"],
            ),
            (
                "droid",
                ["droid", "exec", "--cwd", "/old", "prompt"],
                ["droid", "exec", "--cwd", "/new", "prompt"],
            ),
            (
                "opencode",
                ["opencode", "run", "--dir", "/old"],
                ["opencode", "run", "--dir", "/new"],
            ),
            ("kimi", ["kimi", "--prompt", "prompt"], ["kimi", "--prompt", "prompt"]),
            (
                "unknown",
                ["agent", "--workspace", "/old", "prompt"],
                ["agent", "--workspace", "/old", "prompt"],
            ),
        )
        for engine, argv, expected in cases:
            with self.subTest(engine=engine):
                self.assertEqual(
                    argv_utils.replace_workspace_arg_in_argv(engine, argv, "/new"),
                    expected,
                )
                self.assertIsNot(
                    argv_utils.replace_workspace_arg_in_argv(engine, argv, "/new"),
                    argv,
                )

    def test_json_types_non_negative_int_rejects_bool(self):
        self.assertTrue(json_types.is_non_negative_int(0))
        self.assertTrue(json_types.is_non_negative_int(3))
        self.assertFalse(json_types.is_non_negative_int(-1))
        self.assertFalse(json_types.is_non_negative_int(True))
        self.assertFalse(json_types.is_non_negative_int("3"))

    def test_json_types_first_string_returns_first_non_empty_string(self):
        self.assertEqual(json_types.first_string(None, "", "alpha", "beta"), "alpha")
        self.assertIsNone(json_types.first_string(None, "", 0, False))
        self.assertIsNone(json_types.first_string())

    def test_log_output_tail_and_raw_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stdout.log"
            path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

            tailed = log_output.tail_file_output(path, 2)
            self.assertEqual(tailed.content, "beta\ngamma\n")
            self.assertTrue(tailed.truncated)

            raw = log_output.read_log_output(path, tail=None, raw=True)
            self.assertEqual(raw.content, "alpha\nbeta\ngamma\n")
            self.assertFalse(raw.truncated)

    def test_log_output_missing_file_is_empty(self):
        missing = Path(tempfile.gettempdir()) / "delegate-missing-log-output-test.log"
        if missing.exists():
            missing.unlink()
        self.assertEqual(log_output.tail_file_output(missing, 5), log_output.LogOutput("", False))

    def test_prompt_transport_constants(self):
        self.assertEqual(prompt_transport.PROMPT_TRANSPORT_ARGV, "argv")
        self.assertEqual(prompt_transport.PROMPT_TRANSPORT_FILE, "file")
        self.assertEqual(prompt_transport.PROMPT_TRANSPORT_STDIN, "stdin")
        self.assertEqual(
            prompt_transport.DROID_PROMPT_FILE_ARG_PLACEHOLDER, "<delegate-prompt-file>"
        )
        self.assertEqual(prompt_transport.DROID_PROMPT_FILE_DISPLAY, "<prompt file>")
        self.assertEqual(
            prompt_transport.DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
            "<delegate-devin-agent-config>",
        )
        self.assertEqual(
            prompt_transport.DEVIN_AGENT_CONFIG_DISPLAY,
            "<devin agent config>",
        )

    def test_run_registry_commands_can_include_cwd(self):
        self.assertEqual(run_registry.snapshot_command("codex"), "delegate snapshot codex")
        self.assertEqual(
            run_registry.run_output_command("codex", completion_report=True),
            "delegate run-output codex --completion-report",
        )
        self.assertEqual(
            run_registry.snapshot_command("codex 1", cwd="/tmp/work tree"),
            "delegate --cwd '/tmp/work tree' snapshot 'codex 1'",
        )
        self.assertEqual(
            run_registry.run_output_command(
                "codex 1", completion_report=True, cwd="/tmp/work tree"
            ),
            "delegate --cwd '/tmp/work tree' run-output 'codex 1' --completion-report",
        )

    def test_archived_log_paths_and_byte_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_root = Path(tmp) / ".delegate"
            archive_file = archived_logs.archive_path(registry_root, "del_1")
            archive_file.parent.mkdir(parents=True)
            stdout = Path(tmp) / "stdout.log"
            stderr = Path(tmp) / "stderr.log"
            stdout.write_text("out\n", encoding="utf-8")
            stderr.write_text("error\n", encoding="utf-8")
            with tarfile.open(archive_file, "w:gz") as archive:
                archive.add(stdout, arcname="stdout.log")
                archive.add(stderr, arcname="stderr.log")

            self.assertEqual(archived_logs.archive_dir(registry_root), registry_root / "archive")
            self.assertEqual(
                archived_logs.archive_log_byte_sizes(
                    archive_file,
                    stdout_log="stdout.log",
                    stderr_log="stderr.log",
                ),
                (4, 6),
            )
            self.assertEqual(
                archived_logs.state_log_byte_sizes({"stdoutBytes": 4, "stderrBytes": 6}),
                (4, 6),
            )
            self.assertIsNone(archived_logs.state_log_byte_sizes({"stdoutBytes": True}))
            self.assertIsNone(
                archived_logs.state_log_byte_sizes({"stdoutBytes": 4, "stderrBytes": True})
            )
            self.assertIsNone(
                archived_logs.state_log_byte_sizes({"stdoutBytes": -1, "stderrBytes": 6})
            )
            self.assertIsNone(
                archived_logs.state_log_byte_sizes({"stdoutBytes": 4, "stderrBytes": -6})
            )

    def test_runner_launch_error_carries_code_and_message(self):
        error = runner.RunnerLaunchError("child_launch_failed", "nope")
        self.assertEqual(error.error, "child_launch_failed")
        self.assertEqual(error.message, "nope")

    def test_reasoning_effort_normalization_rejects_argv_hazards(self):
        self.assertEqual(reasoning.normalize_effort("xhigh"), "xhigh")
        for value in ("", "x high", 'x"high', r"x\high"):
            with (
                self.subTest(value=value),
                self.assertRaises(reasoning.ReasoningCapabilityError),
            ):
                reasoning.normalize_effort(value)

    def test_cli_parse_runs_json_mode(self):
        parsed = cli.parse_cli(["--json", "runs", "--limit", "1"])

        self.assertEqual(parsed.subcommand, "runs")
        self.assertTrue(parsed.global_options.json_mode)
        self.assertIsNotNone(parsed.runs)
        self.assertEqual(parsed.runs.limit, 1)

    def test_worktree_management_error_normalizes_payload(self):
        error = worktree_mgmt.WorktreeManagementError(
            {"code": "no_registry", "message": "No registry."}
        )

        self.assertEqual(error.code, "no_registry")
        self.assertEqual(error.message, "No registry.")
        self.assertFalse(error.payload["ok"])
        self.assertEqual(error.payload["error"], "no_registry")
        self.assertEqual(error.payload["exitCode"], worktree_mgmt.WORKTREE_ERROR_EXIT_CODE)

    def test_worktree_status_short_circuits_removed_registry_state(self):
        status, warnings = worktree_mgmt.detect_worktree_status(
            {
                "runId": "del_1",
                "registryWorktreeStatus": worktree_mgmt.STATUS_REMOVED,
            }
        )

        self.assertEqual(status, worktree_mgmt.STATUS_REMOVED)
        self.assertEqual(warnings, [])

    def test_prompt_instruction_helpers_are_idempotent(self):
        prompt = "Do the task."

        with_prefix = prompt_instructions.prepend_skill_review_instructions(prompt)
        self.assertTrue(with_prefix.startswith(prompt_instructions.SKILL_REVIEW_PREFIX))
        self.assertEqual(
            prompt_instructions.prepend_skill_review_instructions(with_prefix),
            with_prefix,
        )

        with_suffix = prompt_instructions.append_completion_report_instructions(prompt)
        self.assertTrue(
            with_suffix.rstrip().endswith(prompt_instructions.COMPLETION_REPORT_SUFFIX.strip())
        )
        self.assertEqual(
            prompt_instructions.append_completion_report_instructions(with_suffix),
            with_suffix,
        )

    def test_redact_value_recurses_nested_json_values(self):
        redacted = redaction.redact_value(
            {
                "headers": ["Authorization: Bearer secret-token-value"],
                "nested": {"env": "DB_PASSWORD=super-secret"},
                "safe": 7,
            }
        )

        self.assertEqual(redacted["headers"], ["Authorization: ***"])
        self.assertEqual(redacted["nested"], {"env": "DB_PASSWORD=***"})
        self.assertEqual(redacted["safe"], 7)

    def test_run_metadata_adds_optional_fields_and_warnings(self):
        payload = {"existing": True}
        carrier = SimpleNamespace(
            isolated_workspace=True,
            isolation_mode="safe",
            effective_isolation="copy",
            isolation_lifecycle="temporary",
            preserved_workspace=False,
            source_git_root="/repo",
            branch="delegate/test",
            creation_context={"plannedBranch": "delegate/test"},
            worktree_status="present",
            safe_workspace_method="copytree",
            warnings=("one", "two"),
        )

        run_metadata.add_run_metadata_payload_fields(payload, carrier)

        self.assertTrue(payload["existing"])
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertEqual(payload["effectiveIsolation"], "copy")
        self.assertEqual(payload["branch"], "delegate/test")
        self.assertEqual(payload["creationContext"], {"plannedBranch": "delegate/test"})
        self.assertEqual(payload["warnings"], ["one", "two"])

    def test_worktree_command_emit_requires_registry(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(worktree_mgmt.WorktreeManagementError) as caught,
        ):
            worktree_commands.emit(
                worktree_commands.WorktreeCommand("list"),
                workspace_path=tmp,
                config={},
                stdout=sys.stdout,
            )

        self.assertEqual(caught.exception.code, "no_registry")
        self.assertFalse(caught.exception.payload["retrySafe"])

    def test_persistent_worktree_error_carries_public_fields(self):
        error = worktree_execution.PersistentWorktreeError("worktree_requires_git", "no git")

        self.assertEqual(error.error, "worktree_requires_git")
        self.assertEqual(error.message, "no git")
        self.assertEqual(str(error), "no git")

    def test_run_output_emit_requires_registry(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(run_output_commands.RunOutputError) as caught,
        ):
            run_output_commands.emit(
                run_output_commands.RunOutputCommand("missing"),
                workspace_path=tmp,
                stdout=sys.stdout,
            )

        self.assertEqual(caught.exception.error, "unknown_handle")
        self.assertIn("Unknown run handle: missing", caught.exception.message)

    def test_run_output_error_carries_diagnostics_and_next_actions(self):
        error = run_output_commands.RunOutputError(
            "missing_completion_report",
            "missing",
            diagnostics={"status": "done"},
            next_actions=["delegate run-output del_1 --stdout"],
        )

        self.assertEqual(error.error, "missing_completion_report")
        self.assertEqual(error.diagnostics, {"status": "done"})
        self.assertEqual(error.next_actions, ["delegate run-output del_1 --stdout"])


class WriteJsonAtomicCleanupTests(unittest.TestCase):
    def test_write_json_atomic_unlinks_temp_on_failure(self):
        """A failed write_json_atomic must not leave .tmp files behind."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"

            def failing_replace(src: str, dst: str) -> None:
                raise OSError(errno.ENOSPC, "simulated disk full")

            with (
                mock.patch.object(private_io.os, "replace", failing_replace),
                self.assertRaises(OSError),
            ):
                private_io.write_json_atomic(target, {"ok": True})
            tmp_files = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(tmp_files, [], f"left-behind tmp files: {tmp_files}")


if __name__ == "__main__":
    unittest.main()
