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

    def test_invalid_json_falls_back_to_bounded_text_event(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line("not json at all")
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "text")
        self.assertIn("not json", acc.events[0].message or "")

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
        self.assertFalse(any(event.kind == "run.completed" for event in acc.events))
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
