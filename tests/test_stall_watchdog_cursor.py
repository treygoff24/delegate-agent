import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import stall_watchdog  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "cursor" / "tool_read.jsonl"
CALL_ID = "tool_ab15a5bd-c6ae-4efb-aa90-1a3b67c1ca5"


def cursor_tool_line(subtype: str, *, call_id: str = CALL_ID) -> str:
    """One Cursor tool event in the shape cursor-agent 2026.09.02 emits."""
    body = {"readToolCall": {"args": {"path": "/repo/README.md"}}}
    if subtype == "completed":
        body["readToolCall"]["result"] = {"success": {"content": "hi"}}
    return json.dumps(
        {
            "type": "tool_call",
            "subtype": subtype,
            "call_id": call_id,
            "tool_call": {**body, "toolCallId": call_id},
        }
    )


class CursorStallWatchdogTests(unittest.TestCase):
    def test_started_and_completed_pair_resolves_the_pending_tool(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480, harness="cursor")
        watchdog.observe_line(cursor_tool_line("started"), now=0.0)
        self.assertEqual(watchdog.tools_in_flight, 1)
        watchdog.observe_line(cursor_tool_line("completed"), now=1.0)
        self.assertEqual(watchdog.tools_in_flight, 0)
        self.assertEqual(watchdog.last_progress_label, "tool_call.completed")

    def test_a_tool_in_flight_suspends_the_stall_check(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480, harness="cursor")
        watchdog.observe_line(cursor_tool_line("started"), now=0.0)
        self.assertIsNone(watchdog.stalled_for(600.0))
        watchdog.observe_line(cursor_tool_line("completed"), now=1.0)
        self.assertAlmostEqual(watchdog.stalled_for(600.0) or 0.0, 599.0)

    def test_concurrent_tools_resolve_by_call_id(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480, harness="cursor")
        watchdog.observe_line(cursor_tool_line("started", call_id="a"), now=0.0)
        watchdog.observe_line(cursor_tool_line("started", call_id="b"), now=1.0)
        watchdog.observe_line(cursor_tool_line("completed", call_id="b"), now=2.0)
        self.assertEqual(watchdog.tools_in_flight, 1)
        watchdog.observe_line(cursor_tool_line("completed", call_id="a"), now=3.0)
        self.assertEqual(watchdog.tools_in_flight, 0)

    def test_the_real_capture_leaves_no_tool_in_flight(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480, harness="cursor")
        for index, line in enumerate(FIXTURE.read_text(encoding="utf-8").splitlines()):
            watchdog.observe_line(line, now=float(index))
        self.assertEqual(watchdog.tools_in_flight, 0)

    def test_cursor_still_gets_the_shared_stream_json_classification(self):
        """The cursor branch must not shadow assistant/user/result handling."""
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480, harness="cursor")
        watchdog.observe_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                }
            ),
            now=0.0,
        )
        self.assertEqual(watchdog.last_progress_label, "assistant")
        watchdog.observe_line(json.dumps({"type": "result", "subtype": "success"}), now=1.0)
        self.assertEqual(watchdog.last_progress_label, "result")

    def test_a_dotted_tool_call_type_is_still_classified(self):
        """Other stream-json engines may emit the dotted spelling."""
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480, harness="claude")
        watchdog.observe_line(
            json.dumps({"type": "tool_call.started", "tool_call": {"id": "t1"}}), now=0.0
        )
        self.assertEqual(watchdog.tools_in_flight, 1)
        watchdog.observe_line(
            json.dumps({"type": "tool_call.completed", "tool_call": {"id": "t1"}}), now=1.0
        )
        self.assertEqual(watchdog.tools_in_flight, 0)


if __name__ == "__main__":
    unittest.main()
