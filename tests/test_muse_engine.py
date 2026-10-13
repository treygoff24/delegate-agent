"""Muse (Meta `muse` CLI) engine: argv, config, stream, watchdog.

Stream fixtures are raw bytes captured from Muse 1.4.6; see
tests/fixtures/muse/README.md. Design record:
docs/plans/2026-10-12-muse-engine.md.
"""

import io
import json
import unittest
from pathlib import Path

from delegate_agent import argv_builders as argv_api
from delegate_agent import cli_parser as parser_api
from delegate_agent import config as config_api
from delegate_agent import harness_discovery, reasoning, structured_output
from delegate_agent import request_build as request_api
from delegate_agent import stall_watchdog as watchdog_api
from delegate_agent.errors import DelegateError
from delegate_agent.harness_events import StreamAccumulator
from delegate_agent.prompt_transport import PROMPT_FILE_ARG_PLACEHOLDER

FIXTURES = Path(__file__).parent / "fixtures" / "muse"
SECTION = {"binary": "muse"}


def feed_fixture(name: str) -> StreamAccumulator:
    acc = StreamAccumulator(harness="muse")
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        acc.ingest_line(line)
    return acc


class MuseArgvTests(unittest.TestCase):
    def test_safe_argv_is_exact_lockdown(self):
        argv = argv_api.build_muse_argv(SECTION, "safe", "/ws", "muse-spark-1.3")
        self.assertEqual(
            argv,
            [
                "muse",
                "exec",
                "--json",
                "--workspace",
                "/ws",
                "--disable-write",
                "--disable-shell",
                "--disable-web-tools",
                "--disable-approval",
                "--no-session-log",
                "--model",
                "muse-spark-1.3",
                "--prompt-file",
                PROMPT_FILE_ARG_PLACEHOLDER,
            ],
        )
        # The prompt never rides argv, and safe never trusts or unsandboxes.
        self.assertNotIn("--trust-workspace", argv)
        self.assertNotIn("--disable-sandbox", argv)
        self.assertNotIn("--yolo", argv)

    def test_work_argv_trusts_workspace_and_keeps_sandbox(self):
        argv = argv_api.build_muse_argv(
            SECTION, "work", "/ws", None, reasoning_effort="high", persist_session=True
        )
        self.assertIn("--trust-workspace", argv)
        self.assertNotIn("--disable-sandbox", argv)
        self.assertNotIn("--disable-write", argv)
        self.assertNotIn("--no-session-log", argv)
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")

    def test_work_bypass_policy_disables_sandbox(self):
        argv = argv_api.build_muse_argv(SECTION, "work", "/ws", None, allow_bypass_permissions=True)
        self.assertIn("--disable-sandbox", argv)

    def test_call_read_only_matches_safe_lockdown(self):
        argv = argv_api.build_muse_argv(SECTION, "call", "/ws", None, call_read_only=True)
        for flag in ("--disable-write", "--disable-shell", "--disable-web-tools"):
            self.assertIn(flag, argv)
        self.assertNotIn("--trust-workspace", argv)

    def test_plain_call_is_unlocked_but_sessionless(self):
        argv = argv_api.build_muse_argv(SECTION, "call", "/ws", None)
        self.assertNotIn("--disable-write", argv)
        self.assertIn("--no-session-log", argv)

    def test_resume_session_id_and_schema_pass_through(self):
        argv = argv_api.build_muse_argv(
            SECTION,
            "work",
            "/ws",
            None,
            output_schema="/tmp/schema.json",
            resume_session_id="123e4567-e89b-12d3-a456-426614174000",
        )
        self.assertEqual(
            argv[argv.index("--session-id") + 1], "123e4567-e89b-12d3-a456-426614174000"
        )
        self.assertEqual(argv[argv.index("--output-schema") + 1], "/tmp/schema.json")
        self.assertNotIn("--no-session-log", argv)

    def test_pure_is_rejected(self):
        with self.assertRaises(DelegateError):
            argv_api.build_muse_argv(SECTION, "call", "/ws", None, pure=True)


class MuseConfigTests(unittest.TestCase):
    def _config(self, muse_section):
        config = json.loads(json.dumps(config_api.DEFAULT_CONFIG))
        config["muse"] = muse_section
        return config

    def test_leading_dash_model_rejected(self):
        config = self._config({"binary": "muse", "defaultModel": "--yolo", "models": {}})
        with self.assertRaises(config_api.ConfigError):
            config_api._validate_muse_section(config["muse"])

    def test_bogus_default_effort_rejected(self):
        config = self._config(
            {"binary": "muse", "defaultReasoningEffort": "ludicrous", "models": {}}
        )
        with self.assertRaises(config_api.ConfigError):
            config_api._validate_muse_section(config["muse"])

    def test_valid_section_accepted(self):
        config = self._config(
            {
                "binary": "muse",
                "defaultModel": "muse-spark-1.3",
                "defaultReasoningEffort": "xhigh",
                "resumable": True,
                "models": {"fast": "muse-spark-1.2"},
            }
        )
        config_api._validate_muse_section(config["muse"])


class MuseReasoningTests(unittest.TestCase):
    def test_effort_enum_matches_binary(self):
        self.assertEqual(
            reasoning.MUSE_NATIVE_EFFORTS,
            ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        )
        cap = reasoning.resolve_harness_enum_capability(
            harness="muse", model="muse-spark-1.3", requested_effort="ultra", discovery=None
        )
        self.assertEqual(cap.effort, "ultra")

    def test_discovery_parser_reads_help_enum(self):
        fragment = harness_discovery.parse_muse_efforts(
            "Meta reasoning effort: none|minimal|low|medium|high|xhigh|max|ultra (default: high)"
        )
        self.assertEqual(
            fragment["harnessReasoning"]["supported"], list(reasoning.MUSE_NATIVE_EFFORTS)
        )


class MuseStreamTests(unittest.TestCase):
    def test_echo_simple_fixture(self):
        acc = feed_fixture("echo-simple.jsonl")
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(acc.completion_text, "echo: Say hello")
        self.assertTrue(acc.session_id)

    def test_tool_read_fixture_seals_terminal_text(self):
        acc = feed_fixture("real-tool-read.jsonl")
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertIn("alpha", acc.assistant_text)
        self.assertTrue(acc.session_id)
        tools = [e for e in acc.events if e.kind == "tool.completed"]
        self.assertTrue(any(e.tool == "read_file" for e in tools))

    def test_structured_fixture_text_is_json(self):
        acc = feed_fixture("real-structured.jsonl")
        self.assertEqual(json.loads(acc.completion_text), {"word": "hello"})

    def test_failed_terminal_is_failure_with_reason(self):
        acc = feed_fixture("real-terminal-failed-schema.jsonl")
        self.assertEqual(acc.terminal_status, "failed")
        self.assertFalse(acc.completion_text)
        self.assertIn("additionalProperties", acc._last_error_message or "")

    def test_configured_model_is_never_served_model(self):
        # run.model.configured only echoes the request; the muse branch must
        # not let it (or any stray model field) become a served-model claim.
        acc = feed_fixture("real-tool-read.jsonl")
        self.assertIsNone(acc.served_model)
        self.assertIsNone(acc.continuity_violation)

    def test_malformed_lines_never_become_text(self):
        acc = StreamAccumulator(harness="muse")
        acc.ingest_line("not json at all")
        acc.ingest_line('{"payload_type": 5}')
        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(acc.malformed_lines, 1)


class MuseWatchdogTests(unittest.TestCase):
    def test_tool_start_and_finish_pair_up(self):
        started = finished = 0
        for line in (FIXTURES / "real-tool-read.jsonl").read_text().splitlines():
            signals = watchdog_api._classify_payload(json.loads(line), None, harness="muse")
            if signals:
                started += len(signals.tools_started)
                finished += len(signals.tools_finished)
        self.assertEqual((started, finished), (1, 1))

    def test_lifecycle_noise_signals_nothing(self):
        record = {
            "payload_type": "task.lifecycle.accepted",
            "payload": {"kind": "task_lifecycle"},
            "recorded_at": 1780531400000001,
        }
        signals = watchdog_api._classify_payload(record, None, harness="muse")
        self.assertIsNotNone(signals)
        self.assertFalse(signals.progress)
        self.assertEqual(signals.deltas, ())


class MuseStructuredOutputTests(unittest.TestCase):
    def test_root_must_close_additional_properties(self):
        open_schema = {"type": "object", "properties": {"w": {"type": "string"}}}
        self.assertIsNotNone(structured_output.native_schema_eligible("muse", open_schema))
        closed = {**open_schema, "additionalProperties": False}
        self.assertIsNone(structured_output.native_schema_eligible("muse", closed))


class MuseRequestTests(unittest.TestCase):
    """Request-level wiring the argv unit tests cannot see."""

    def _argv(self, *args):
        parsed = parser_api.parse_cli(["--json", "dry-run", *args])
        request = request_api.request_from_parsed(
            parsed, config_api.embedded_default_config(), io.StringIO("")
        )
        return request.argv

    def test_work_run_keeps_session_log_by_default(self):
        # Regression: resumable work runs launched with --no-session-log, so
        # followup re-entered an empty session (live smoke, 2026-10-12).
        self.assertNotIn("--no-session-log", self._argv("muse", "work", "do it"))

    def test_safe_and_call_runs_drop_session_log(self):
        self.assertIn("--no-session-log", self._argv("muse", "safe", "review"))
        self.assertIn("--no-session-log", self._argv("muse", "call", "answer"))


if __name__ == "__main__":
    unittest.main()
