"""`current` and failure hints tell the truth about what a live run is doing."""

import json
import unittest

from delegate_agent import harness_events


def _feed(acc, *events):
    for event in events:
        acc.ingest_line(json.dumps(event))


def _turn_error(message="429 rate limited", status=429):
    return {
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "content": [],
            "stopReason": "error",
            "errorMessage": message,
            "errorStatus": status,
        },
    }


class StaleErrorCurrentTests(unittest.TestCase):
    def test_omp_retry_turn_and_thinking_replace_the_429_current(self):
        for harness in ("omp", "pi"):
            with self.subTest(harness=harness):
                acc = harness_events.StreamAccumulator(harness=harness)
                _feed(acc, _turn_error())
                self.assertIn("429", acc.current)
                _feed(acc, {"type": "auto_retry_start"})
                self.assertEqual(acc.current, "retrying after provider error (429)")
                _feed(acc, {"type": "turn_start"})
                self.assertEqual(acc.current, "thinking")
                _feed(
                    acc,
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "thinking_delta", "delta": "hm"},
                    },
                )
                self.assertEqual(acc.current, "thinking")

    def test_thinking_delta_alone_clears_error_current(self):
        acc = harness_events.StreamAccumulator(harness="omp")
        _feed(
            acc,
            _turn_error(),
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "hm"},
            },
        )
        self.assertEqual(acc.current, "thinking")

    def test_later_tool_current_is_not_overwritten_by_thinking(self):
        acc = harness_events.StreamAccumulator(harness="omp")
        _feed(acc, _turn_error(), {"type": "auto_retry_start"})
        acc.current = "read src/x.py"
        _feed(acc, {"type": "turn_start"})
        self.assertEqual(acc.current, "read src/x.py")

    def test_codex_turn_started_after_error_clears_error_current(self):
        acc = harness_events.StreamAccumulator(harness="codex")
        _feed(acc, {"type": "error", "message": "Reconnecting... 2/5"}, {"type": "turn.started"})
        self.assertEqual(acc.current, "thinking")

    def test_opencode_step_start_after_error_clears_error_current(self):
        acc = harness_events.StreamAccumulator(harness="opencode")
        _feed(
            acc,
            {"type": "error", "error": {"name": "APIError", "data": {"message": "boom"}}},
        )
        self.assertIn("boom", acc.current)
        _feed(acc, {"type": "step_start", "part": {"type": "step-start"}})
        self.assertEqual(acc.current, "thinking")


if __name__ == "__main__":
    unittest.main()
