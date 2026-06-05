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
