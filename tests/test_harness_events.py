import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
HARNESS_EVENTS_PATH = ROOT / "src" / "delegate_agent" / "harness_events.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_harness_events():
    spec = importlib.util.spec_from_file_location(
        "delegate_harness_events_under_test", HARNESS_EVENTS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HarnessEventsTests(unittest.TestCase):
    def setUp(self):
        self.events = load_harness_events()

    def ingest_opencode_fixture(self, name: str):
        fixture = ROOT / "tests" / "fixtures" / "opencode" / name
        acc = self.events.StreamAccumulator(harness="opencode")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)
        return acc

    def opencode_text_parts(self, name: str) -> list[str]:
        fixture = ROOT / "tests" / "fixtures" / "opencode" / name
        texts: list[str] = []
        for line in fixture.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            part = payload.get("part")
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return texts

    def test_assistant_message_text_is_captured(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps({"type": "message", "role": "assistant", "content": "Hello parent"})
        )
        self.assertIn("Hello parent", acc.assistant_text)

    def test_kimi_role_content_without_type_is_captured(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"role": "assistant", "content": "OK from Kimi"}))
        self.assertEqual(acc.assistant_text, "OK from Kimi")
        self.assertEqual(acc.recoverable_assistant_text, "OK from Kimi")

    def test_kimi_non_assistant_role_content_without_type_is_ignored(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"role": "user", "content": "prompt echo"}))
        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.recoverable_assistant_text)

    def test_assistant_text_is_cached_until_chunks_change(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps({"type": "message", "role": "assistant", "content": "Hello parent"})
        )
        first = acc.assistant_text
        second = acc.assistant_text
        self.assertIs(first, second)
        acc.ingest_line(
            json.dumps({"type": "message", "role": "assistant", "content": "More detail"})
        )
        third = acc.assistant_text
        self.assertIsNot(first, third)
        self.assertIn("More detail", third)

    def test_reasoning_events_are_ignored(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"type": "reasoning", "content": "hidden chain"}))
        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(acc.events, [])

    def test_tool_result_payloads_are_ignored(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_result",
                    "value": "SECRET OUTPUT FROM COMMAND",
                }
            )
        )
        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(acc.events, [])

    def test_tool_call_metadata_is_normalized(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"type": "tool_call", "tool": "read", "path": "README.md"}))
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "tool.started")
        self.assertEqual(acc.events[0].tool, "read")
        self.assertEqual(acc.events[0].path, "README.md")

    def test_completion_final_text_is_captured(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps({"type": "completion", "finalText": "Status: completed\n- did work"})
        )
        self.assertEqual(acc.completion_text, "Status: completed\n- did work")

    def test_codex_item_completed_agent_message_is_latest_completion(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I am checking the repo."},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "hidden reasoning"},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Status: completed\n- final answer",
                    },
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        self.assertIn("I am checking the repo.", acc.assistant_text)
        self.assertNotIn("hidden reasoning", acc.assistant_text)
        self.assertEqual(acc.completion_text, "Status: completed\n- final answer")

    def test_codex_command_execution_items_are_normalized(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I am checking the repo."},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "python3 -m unittest",
                        "status": "in_progress",
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "python3 -m unittest",
                        "status": "completed",
                    },
                }
            )
        )
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.events[0].kind, "tool.started")
        self.assertEqual(acc.events[0].status, "in_progress")
        self.assertEqual(acc.events[1].kind, "tool.completed")
        self.assertEqual(acc.events[1].status, "success")

    def test_codex_agent_message_started_does_not_record_text(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "agent_message", "text": "partial"},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "partial"},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        self.assertEqual(acc.assistant_text, "partial")
        self.assertEqual(acc.completion_text, "partial")

    def test_codex_completion_text_survives_turn_start_and_command_only_turn(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "answer one"},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        acc.ingest_line(json.dumps({"type": "turn.started"}))
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "ls",
                        "status": "in_progress",
                    },
                }
            )
        )
        self.assertEqual(acc.completion_text, "answer one")

    def test_codex_progress_message_before_command_is_not_promoted(self):
        # A turn that ends on a command (intro agent_message -> command ->
        # turn.completed) has no closing answer, so nothing is promoted. Promoting
        # the pre-command message would surface an intro line as the final report.
        acc = self.events.StreamAccumulator()
        for payload in [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "I'll start by checking the repo."},
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "git status",
                    "status": "completed",
                },
            },
            {"type": "turn.completed"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertIsNone(acc.completion_text)

    def test_codex_empty_completed_agent_message_clears_completion_candidate(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I'll start by reviewing."},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "   "},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        self.assertIsNone(acc.completion_text)

    def test_real_codex_stream_fixture_matches_parser_assumptions(self):
        # Guards against Codex changing its event schema: the other codex tests
        # use hand-authored JSON, so they would stay green even if the real
        # wire format drifted. This fixture is a sanitized capture of an actual
        # `codex` run (command output blanked, host paths scrubbed); if Codex
        # renames event types or fields, this test fails where synthetic ones
        # would not.
        fixture = ROOT / "tests" / "fixtures" / "codex_real_stream.jsonl"
        acc = self.events.StreamAccumulator()
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)

        # Final agent_message becomes the completion; the intro message and the
        # hidden reasoning item do not.
        self.assertIsNotNone(acc.completion_text)
        self.assertTrue(acc.completion_text.startswith("Verdict:"))
        self.assertIn("skill pass", acc.assistant_text.lower())
        self.assertNotIn("reasoning", acc.assistant_text.lower())

        # command_execution items normalize to tool events, and a real failed
        # command stays "failed" (only "completed" maps to "success").
        tool_events = [(e.kind, e.status) for e in acc.events if e.tool]
        self.assertEqual(
            tool_events,
            [
                ("tool.started", "in_progress"),
                ("tool.completed", "success"),
                ("tool.started", "in_progress"),
                ("tool.completed", "failed"),
            ],
        )

    def test_opencode_simple_text_fixture_sets_completion_from_stop_step(self):
        acc = self.ingest_opencode_fixture("simple_text.ndjson")
        expected = self.opencode_text_parts("simple_text.ndjson")[-1].strip()

        self.assertEqual(expected, "pong")
        self.assertEqual(acc.assistant_text, expected)
        self.assertEqual(acc.completion_text, expected)
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(
            acc.terminal_event,
            {"event": "opencode.step_finish", "status": "succeeded", "reason": "stop"},
        )
        self.assertEqual(acc.assistant_recovery_quality(), "explicit_completion")
        self.assertIn("opencode", self.events.ASSISTANT_RECOVERY_HARNESSES)

    def test_opencode_tool_run_fixture_recovers_final_text_not_tool_output(self):
        acc = self.ingest_opencode_fixture("tool_run.ndjson")
        expected = self.opencode_text_parts("tool_run.ndjson")[-1].strip()

        self.assertEqual(expected, "banana42")
        self.assertEqual(acc.assistant_text, expected)
        self.assertEqual(acc.completion_text, expected)
        self.assertNotIn("The secret word is", acc.assistant_text)
        tool_events = [event for event in acc.events if event.tool]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].kind, "tool.completed")
        self.assertEqual(tool_events[0].tool, "read")
        self.assertEqual(tool_events[0].status, "success")
        self.assertTrue((tool_events[0].target or "").endswith("note.txt"))

    def test_opencode_multi_step_text_keeps_only_final_stop_step(self):
        acc = self.events.StreamAccumulator(harness="opencode")
        for line in [
            json.dumps(
                {
                    "type": "step_start",
                    "part": {"type": "step-start"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "thinking about it..."},
                }
            ),
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "state": {"status": "completed", "input": {"filePath": "/tmp/x"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"type": "step-finish", "reason": "tool-calls"},
                }
            ),
            json.dumps(
                {
                    "type": "step_start",
                    "part": {"type": "step-start"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "FINAL ANSWER"},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"type": "step-finish", "reason": "stop"},
                }
            ),
        ]:
            acc.ingest_line(line)

        self.assertEqual(acc.assistant_text, "FINAL ANSWER")
        self.assertEqual(acc.completion_text, "FINAL ANSWER")
        bounded, _meta = acc.bounded_assistant_text()
        self.assertEqual(bounded, "FINAL ANSWER")
        self.assertNotIn("thinking about it", acc.assistant_text)
        self.assertEqual(acc.recoverable_assistant_text, "FINAL ANSWER")

    def test_opencode_truncated_after_second_step_start_recovers_latest_step_only(self):
        acc = self.events.StreamAccumulator(harness="opencode")
        for line in [
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "thinking about it..."},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"type": "step-finish", "reason": "tool-calls"},
                }
            ),
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "FINAL ANSWER"},
                }
            ),
        ]:
            acc.ingest_line(line)

        self.assertEqual(acc.assistant_text, "FINAL ANSWER")
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.recoverable_assistant_text, "FINAL ANSWER")
        self.assertNotIn("thinking about it", acc.assistant_text)
        self.assertNotIn("thinking about it", acc.recoverable_assistant_text or "")

    def test_opencode_error_fixture_records_terminal_failure_detail(self):
        acc = self.ingest_opencode_fixture("error_run.ndjson")

        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.terminal_status, "failed")
        self.assertEqual(
            acc.terminal_event,
            {
                "event": "opencode.error",
                "status": "failed",
                "reason": "UnknownError: Unexpected server error. Check server logs for details.",
            },
        )
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "failed")
        self.assertIn("UnknownError", completed[0].message or "")
        self.assertIn("Unexpected server error", completed[0].message or "")

    def test_opencode_unknown_and_malformed_lines_are_ignored(self):
        acc = self.events.StreamAccumulator(harness="opencode")

        for line in [
            '{"type":"surprise","part":{"type":"text","text":"nope"}}',
            '{"type":"text"}',
            '{"type":"text","part":"bad"}',
            '{"type":"tool_use","part":null}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":7}}',
            "not json at all",
        ]:
            acc.ingest_line(line)

        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(acc.events, [])
        self.assertIsNone(acc.completion_text)
        self.assertIsNone(acc.terminal_status)

    def test_invalid_json_falls_back_to_bounded_text_event(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line("not json at all")
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "text")
        self.assertIn("not json", acc.events[0].message or "")

        kimi = self.events.StreamAccumulator(harness="kimi")
        kimi.ingest_line("plain Kimi progress")
        kimi.ingest_line(json.dumps(["non-object Kimi output"]))
        self.assertEqual(kimi.events, [])

    def test_deeply_nested_json_line_falls_back_to_text_event(self):
        acc = self.events.StreamAccumulator()
        # Python 3.14's json scanner tolerates ~100k nesting levels before
        # aborting json.loads with RecursionError; older versions trip far
        # shallower, so 300k raises on every supported interpreter.
        acc.ingest_line("[" * 300_000 + "]" * 300_000)
        self.assertEqual(acc.structured_events_seen, 0)
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "text")

    def test_devin_json_shaped_line_is_preserved_as_plain_text(self):
        acc = self.events.StreamAccumulator(harness="devin")
        acc.ingest_line("Retrying the request with backoff config:")
        acc.ingest_line('{"retries": 3}')
        acc.ingest_line("Retry succeeded.")
        self.assertEqual(acc.structured_events_seen, 0)
        self.assertIn('{"retries": 3}', acc.assistant_text)
        self.assertIn("Retrying the request with backoff config:", acc.assistant_text)
        self.assertIn("Retry succeeded.", acc.assistant_text)

    def test_devin_multiline_output_preserves_single_newlines(self):
        acc = self.events.StreamAccumulator(harness="devin")
        acc.ingest_line("line one")
        acc.ingest_line("line two")
        acc.ingest_line("line three")
        self.assertEqual(acc.assistant_text, "line one\nline two\nline three")

    def test_claude_assistant_tool_use_blocks_emit_tool_started(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Running git status"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "git status"},
                            },
                        ]
                    },
                }
            )
        )
        tool_events = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].tool, "Bash")
        self.assertEqual(tool_events[0].target, "git status")
        self.assertEqual(acc.current, "Bash git status")
        self.assertIn("Running git status", acc.assistant_text)

    def test_claude_error_result_does_not_emit_success_completion(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "partial output",
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "failed")
        self.assertEqual(acc.terminal_status, "failed")
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.recoverable_assistant_text, "partial output")

    def test_claude_success_result_still_emits_success_completion(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "Status: completed\n- done",
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "succeeded")
        self.assertEqual(acc.completion_text, "Status: completed\n- done")

    def test_grok_cancelled_stop_reason_sets_terminal_cancelled(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "partial report"}))
        acc.ingest_line(json.dumps({"type": "end", "stopReason": "Cancelled"}))
        self.assertEqual(acc.terminal_status, "cancelled")
        self.assertEqual(
            acc.terminal_event, {"event": "grok.end", "status": "cancelled", "reason": "Cancelled"}
        )
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.recoverable_assistant_text, "partial report")

    def test_grok_maxtokens_stop_reason_stays_exit_code_derived(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "partial report"}))
        acc.ingest_line(json.dumps({"type": "end", "stopReason": "MaxTokens"}))
        self.assertIsNone(acc.terminal_status)
        self.assertEqual(acc.recoverable_assistant_text, "partial report")

    def test_codex_explicit_terminal_error_sets_failed(self):
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(json.dumps({"type": "turn.failed"}))
        self.assertEqual(acc.terminal_status, "failed")

    def test_claude_tool_result_emits_tool_completed_correlated_by_id(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_01abc",
                                "name": "Bash",
                                "input": {"command": "echo hello"},
                            }
                        ]
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01abc",
                                "content": "hello",
                                "is_error": False,
                            }
                        ]
                    },
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].tool, "Bash")
        self.assertEqual(completed[0].target, "echo hello")
        self.assertEqual(completed[0].status, "success")
        self.assertEqual(acc.current, "Bash echo hello")

    def test_claude_tool_result_error_sets_error_status(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_02def",
                                "name": "Bash",
                                "input": {"command": "false"},
                            }
                        ]
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_02def",
                                "content": "boom",
                                "is_error": True,
                            }
                        ]
                    },
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "error")

    def test_claude_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_03ghi",
                                "content": "SECRET COMMAND OUTPUT",
                                "is_error": False,
                            }
                        ]
                    },
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].tool, "tool")
        self.assertIsNone(completed[0].target)
        self.assertNotIn("SECRET COMMAND OUTPUT", acc.assistant_text)
        self.assertFalse(
            any("SECRET COMMAND OUTPUT" in (event.message or "") for event in acc.events)
        )

    def test_claude_parallel_tool_results_each_complete(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_a",
                                "name": "Read",
                                "input": {"file_path": "a.py"},
                            },
                            {
                                "type": "tool_use",
                                "id": "toolu_b",
                                "name": "Read",
                                "input": {"file_path": "b.py"},
                            },
                        ]
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_a",
                                "content": "...",
                                "is_error": False,
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_b",
                                "content": "...",
                                "is_error": False,
                            },
                        ]
                    },
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual({event.target for event in completed}, {"a.py", "b.py"})

    def test_kimi_tool_call_emits_started_and_completed_correlated_by_id(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_01abc",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"file_path": "README.md"}),
                            },
                        }
                    ],
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "role": "tool",
                    "tool_call_id": "call_01abc",
                    "content": "file contents",
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(started[0].tool, "Read")
        self.assertEqual(started[0].target, "README.md")
        self.assertEqual(completed[0].tool, "Read")
        self.assertEqual(completed[0].target, "README.md")
        # kimi 0.26.0 results carry no success/error signal; status stays unknown.
        self.assertIsNone(completed[0].status)
        self.assertNotIn("status", completed[0].to_dict())
        self.assertEqual(acc.current, "Read README.md")

    def test_kimi_parallel_tool_calls_correlate_by_id(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_a",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"file_path": "a.py"}),
                            },
                        },
                        {
                            "type": "function",
                            "id": "call_b",
                            "function": {
                                "name": "Bash",
                                "arguments": json.dumps({"command": "echo hi"}),
                            },
                        },
                    ],
                }
            )
        )
        # Results may arrive in any order; correlation is by tool_call_id.
        acc.ingest_line(json.dumps({"role": "tool", "tool_call_id": "call_b", "content": "hi"}))
        acc.ingest_line(json.dumps({"role": "tool", "tool_call_id": "call_a", "content": "..."}))
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 2)
        self.assertEqual(len(completed), 2)
        self.assertEqual((completed[0].tool, completed[0].target), ("Bash", "echo hi"))
        self.assertEqual((completed[1].tool, completed[1].target), ("Read", "a.py"))

    def test_kimi_tool_result_with_unmatched_id_is_graceful(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "tool",
                    "tool_call_id": "call_unknown",
                    "content": "orphaned output",
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].tool, "tool")
        self.assertIsNone(completed[0].target)
        self.assertIsNone(completed[0].status)

    def test_kimi_malformed_tool_calls_do_not_crash(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(json.dumps({"role": "assistant", "tool_calls": "not-a-list"}))
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        "not-a-dict",
                        {"type": "function", "id": "call_no_function"},
                        {
                            "type": "function",
                            "id": "call_bad_args",
                            "function": {"name": "Read", "arguments": "{not json"},
                        },
                        {
                            "type": "function",
                            "id": "call_no_name",
                            "function": {"arguments": json.dumps({"path": "x.py"})},
                        },
                    ],
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(started), 2)
        self.assertEqual((started[0].tool, started[0].target), ("Read", None))
        self.assertEqual((started[1].tool, started[1].target), ("tool", "x.py"))
        # Malformed-arguments entries still correlate their results by id.
        acc.ingest_line(
            json.dumps({"role": "tool", "tool_call_id": "call_bad_args", "content": "..."})
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0].tool, completed[0].target), ("Read", None))

    def test_kimi_deeply_nested_tool_arguments_do_not_crash(self):
        # json.loads raises RecursionError (not JSONDecodeError) on excessively
        # nested input; argument parsing must never let an exception escape
        # onto the runner's stdout drain thread.
        acc = self.events.StreamAccumulator(harness="kimi")
        nested_arguments = "[" * 5000 + "]" * 5000
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_nested",
                            "function": {"name": "Read", "arguments": nested_arguments},
                        }
                    ],
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(started), 1)
        self.assertEqual((started[0].tool, started[0].target), ("Read", None))
        # The unparseable-arguments entry still correlates its result by id.
        acc.ingest_line(
            json.dumps({"role": "tool", "tool_call_id": "call_nested", "content": "..."})
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0].tool, completed[0].target), ("Read", None))

    def test_kimi_combined_content_and_tool_calls_ends_current_on_tool(self):
        # A single assistant envelope carrying both prose and tool_calls must
        # leave `current` on the active tool, not on the stale assistant text.
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "content": "I'll read the file first.",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_read1",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"file_path": "src/main.py"}),
                            },
                        }
                    ],
                }
            )
        )
        self.assertEqual(acc.current, "Read src/main.py")
        self.assertIn("I'll read the file first.", acc.assistant_text)
        started = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(started), 1)
        self.assertEqual((started[0].tool, started[0].target), ("Read", "src/main.py"))

    def test_kimi_meta_session_resume_hint_is_dropped(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "meta",
                    "type": "session.resume_hint",
                    "session_id": "sess_123",
                    "command": "kimi --resume sess_123",
                    "content": "Resume this session with: kimi --resume sess_123",
                }
            )
        )
        self.assertEqual(acc.events, [])
        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.recoverable_assistant_text)

    def test_kimi_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_secret",
                            "function": {
                                "name": "Bash",
                                "arguments": json.dumps({"command": "cat secret.txt"}),
                            },
                        }
                    ],
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "role": "tool",
                    "tool_call_id": "call_secret",
                    "content": "SECRET COMMAND OUTPUT",
                }
            )
        )
        self.assertNotIn("SECRET COMMAND OUTPUT", acc.assistant_text)
        self.assertFalse(
            any("SECRET COMMAND OUTPUT" in (event.message or "") for event in acc.events)
        )
        self.assertFalse(
            any("SECRET COMMAND OUTPUT" in (event.target or "") for event in acc.events)
        )
        self.assertNotIn("SECRET COMMAND OUTPUT", acc.current or "")

    def test_kimi_deeply_nested_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        secret = "SECRET COMMAND OUTPUT"
        line = (
            '{"role":"tool","content":"'
            + secret
            + '","nested":'
            + "[" * 300_000
            + "0"
            + "]" * 300_000
            + "}"
        )

        acc.ingest_line(line)

        self.assertEqual(acc.events, [])
        self.assertIsNone(acc.current)
        self.assertNotIn(secret, acc.assistant_text)
        self.assertIsNone(acc.recoverable_assistant_text)
        self.assertIsNone(acc.completion_text)

    def test_kimi_truncated_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        secret = "SECRET COMMAND OUTPUT"

        acc.ingest_line('{"role":"tool","content":"' + secret)

        self.assertEqual(acc.events, [])
        self.assertIsNone(acc.current)
        self.assertNotIn(secret, acc.assistant_text)
        self.assertIsNone(acc.recoverable_assistant_text)
        self.assertIsNone(acc.completion_text)

    def test_kimi_0260_stream_vocabulary_regression(self):
        # Regression coverage of the exact line vocabulary live-captured from
        # kimi 0.26.0 `--output-format stream-json`: assistant content,
        # assistant tool_calls, role=tool results, and meta session.resume_hint.
        # A static fixture cannot detect future upstream drift; it pins the
        # shapes observed on 2026-07-16.
        lines = [
            {"role": "assistant", "content": "I'll read the file first."},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "call_read1",
                        "function": {
                            "name": "Read",
                            "arguments": json.dumps({"file_path": "src/main.py"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_read1",
                "content": "def main(): ...",
            },
            {
                "role": "meta",
                "type": "session.resume_hint",
                "session_id": "sess_abc",
                "command": "kimi --resume sess_abc",
                "content": "Resume this session with: kimi --resume sess_abc",
            },
            {"role": "assistant", "content": "Status: completed\n- read the file"},
        ]
        acc = self.events.StreamAccumulator(harness="kimi")
        for line in lines:
            acc.ingest_line(json.dumps(line))
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual((started[0].tool, started[0].target), ("Read", "src/main.py"))
        self.assertEqual((completed[0].tool, completed[0].target), ("Read", "src/main.py"))
        self.assertIsNone(completed[0].status)
        self.assertIn("I'll read the file first.", acc.assistant_text)
        self.assertIn("Status: completed", acc.assistant_text)
        self.assertNotIn("sess_abc", acc.assistant_text)
        self.assertFalse(any(event.kind == "text" for event in acc.events))

    def test_substantive_assistant_text_preferred_over_later_housekeeping(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Status: completed\n- fixed parser\n- added tests",
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Plan is up-to-date.",
                }
            )
        )
        self.assertIn("fixed parser", acc.recoverable_assistant_text or "")
        self.assertNotIn("Plan is up-to-date", acc.recoverable_assistant_text or "")
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")

    def test_substantive_assistant_text_preferred_over_longer_progress_message(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Status: completed\n- shipped the fix",
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": (
                        "I am still investigating the repository layout, reading files, "
                        "and running additional checks before I can finalize anything."
                    ),
                }
            )
        )
        self.assertIn("shipped the fix", acc.recoverable_assistant_text or "")
        self.assertNotIn("still investigating", acc.recoverable_assistant_text or "")
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")

    def test_housekeeping_only_assistant_text_is_still_recoverable(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "The final report was delivered in the previous message.",
                }
            )
        )
        self.assertEqual(
            acc.recoverable_assistant_text,
            "The final report was delivered in the previous message.",
        )
        self.assertEqual(acc.assistant_recovery_quality(), "housekeeping_fallback")

    def test_plaintext_fallback_current_advances_on_each_line(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line("first plaintext progress")
        acc.ingest_line("second plaintext progress")
        self.assertEqual(acc.current, "second plaintext progress")

    def test_explicit_completion_wins_over_assistant_recovery_quality(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Status: completed\n- interim report",
                }
            )
        )
        acc.ingest_line(
            json.dumps({"type": "completion", "finalText": "Status: completed\n- explicit"})
        )
        self.assertEqual(acc.completion_text, "Status: completed\n- explicit")
        self.assertEqual(acc.assistant_recovery_quality(), "explicit_completion")

    def test_classify_substantive_and_housekeeping_helpers(self):
        self.assertTrue(self.events.is_substantive_assistant_text("Status: completed\n- did work"))
        self.assertTrue(self.events.is_housekeeping_assistant_text("Plan is up-to-date."))
        self.assertTrue(self.events.is_housekeeping_assistant_text("I am still investigating."))
        self.assertTrue(
            self.events.is_housekeeping_assistant_text(
                "I'm still investigating.\n- checking files\n- running tests"
            )
        )
        self.assertFalse(
            self.events.is_substantive_assistant_text(
                "I'm still investigating.\n- checking files\n- running tests"
            )
        )
        self.assertTrue(
            self.events.is_housekeeping_assistant_text(
                "I\u2019m still investigating.\n- checking files\n- running tests"
            )
        )
        self.assertFalse(
            self.events.is_substantive_assistant_text(
                "The final report was delivered in the previous message."
            )
        )

    def test_structured_report_with_interior_progress_line_is_substantive(self):
        report = (
            "## Summary\n"
            "- Fixed the recovery classifier.\n"
            "- Added regression coverage.\n\n"
            "Let me check the failing case below.\n\n"
            "## Verification\n"
            "- python3 -m unittest tests.test_harness_events"
        )
        self.assertTrue(self.events.is_substantive_assistant_text(report))
        self.assertFalse(self.events.is_housekeeping_assistant_text(report))

        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"type": "message", "role": "assistant", "content": report}))
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")

    def test_grok_streaming_fixture_populates_assistant_and_completion(self):
        fixture = ROOT / "tests" / "fixtures" / "grok_streaming_json_smoke.jsonl"
        acc = self.events.StreamAccumulator(harness="grok")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)
        self.assertIn("delegate grok fixture ok", acc.assistant_text)
        self.assertIn("delegate grok fixture ok", acc.completion_text or "")
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "succeeded")

    def test_grok_unended_text_buffer_is_recoverable_substantive(self):
        parts = [
            "Status: completed\n",
            "- Restored streamed Grok recovery.\n",
            "- Added regression coverage before terminal end.\n",
            "\nVerification:\n",
            "- python3 -m unittest tests.test_harness_events\n",
        ]
        acc = self.events.StreamAccumulator(harness="grok")
        for part in parts:
            acc.ingest_line(json.dumps({"type": "text", "data": part}))

        self.assertEqual(acc.recoverable_assistant_text, "".join(parts).strip())
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")
        self.assertIsNone(acc.completion_text)

    def test_grok_current_advances_before_end(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "first line\nsecond"}))
        self.assertEqual(acc.current, "second")

        acc.ingest_line(json.dumps({"type": "text", "data": " line"}))
        self.assertEqual(acc.current, "second line")

        long_line = "x" * 121
        acc.ingest_line(json.dumps({"type": "text", "data": f"\n{long_line}"}))
        self.assertEqual(acc.current, "x" * 120 + "…")
        self.assertIsNone(acc.completion_text)

    def test_grok_live_text_chunks_ignore_thought_and_finalize_on_end(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "thought", "data": "hidden reasoning"}))
        acc.ingest_line(json.dumps({"type": "text", "data": "delegate"}))
        acc.ingest_line(json.dumps({"type": "text", "data": " grok"}))
        acc.ingest_line(
            json.dumps(
                {
                    "type": "end",
                    "stopReason": "EndTurn",
                    "sessionId": "sess",
                    "requestId": "req",
                }
            )
        )
        self.assertEqual(acc.assistant_text, "delegate grok")
        self.assertEqual(acc.completion_text, "delegate grok")
        self.assertNotIn("hidden reasoning", acc.assistant_text)

    def test_grok_maxtokens_end_keeps_text_recoverable_without_success_completion(self):
        fixture = ROOT / "tests" / "fixtures" / "grok_streaming_maxtokens.jsonl"
        acc = self.events.StreamAccumulator(harness="grok")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)
        self.assertIn("delegate grok fixture ok", acc.assistant_text)
        self.assertIn("delegate grok fixture ok", acc.recoverable_assistant_text or "")
        self.assertIsNone(acc.completion_text)
        self.assertFalse(
            any(
                event.kind == "run.completed" and event.status == "succeeded"
                for event in acc.events
            )
        )

    def test_grok_error_event_surfaces_message_without_success_completion(self):
        acc = self.events.StreamAccumulator(harness="grok")
        for payload in [
            {"type": "text", "data": "partial answer before failure"},
            {"type": "error", "message": "Couldn't start session: upstream 503"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertIsNone(acc.completion_text)
        recoverable = acc.recoverable_assistant_text or ""
        self.assertIn("Couldn't start session: upstream 503", recoverable + acc.assistant_text)
        self.assertFalse(
            any(
                event.kind == "run.completed" and event.status == "succeeded"
                for event in acc.events
            )
        )

    def test_grok_multiturn_tool_use_end_does_not_promote_preamble(self):
        acc = self.events.StreamAccumulator(harness="grok")
        for payload in [
            {"type": "text", "data": "I'll inspect the repo first."},
            {"type": "end", "stopReason": "ToolUse"},
            {"type": "tool_call", "tool": "Bash", "args": {"command": "git status"}},
            {"type": "text", "data": "Status: completed\n- final answer"},
            {"type": "end", "stopReason": "EndTurn"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertEqual(acc.completion_text, "Status: completed\n- final answer")
        self.assertNotEqual(acc.completion_text, "I'll inspect the repo first.")
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)

    def test_top_level_grok_shapes_are_ignored_for_non_grok_harnesses(self):
        acc = self.events.StreamAccumulator(harness="cursor")
        acc.ingest_line(json.dumps({"type": "text", "data": "x"}))
        acc.ingest_line(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        self.assertEqual(acc.assistant_text, "")
        self.assertFalse(any(event.kind == "run.completed" for event in acc.events))

        grok = self.events.StreamAccumulator(harness="grok")
        grok.ingest_line(json.dumps({"type": "text", "data": "x"}))
        grok.ingest_line(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        self.assertEqual(grok.assistant_text, "x")
        self.assertEqual(grok.completion_text, "x")
