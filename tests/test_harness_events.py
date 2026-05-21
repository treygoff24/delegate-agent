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

    def test_invalid_json_falls_back_to_bounded_text_event(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line("not json at all")
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "text")
        self.assertIn("not json", acc.events[0].message or "")
