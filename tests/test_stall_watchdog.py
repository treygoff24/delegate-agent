import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import stall_watchdog  # noqa: E402


def omp_delta(delta: str, *, kind: str = "thinking_delta", seq: int = 0) -> str:
    """One omp/pi message_update line.

    The accumulating `partial` blob is included deliberately: it is what makes
    every real omp line unique even when the model repeats itself, so a test
    without it would not exercise the case the watchdog exists for.
    """
    return json.dumps(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": kind,
                "contentIndex": 0,
                "delta": delta,
                "partial": {"role": "assistant", "seq": seq},
            },
            "message": {"role": "assistant", "seq": seq},
        }
    )


def claude_assistant(*blocks: dict) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}
    )


def claude_tool_result(tool_use_id: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id}],
            },
        }
    )


class NormalizeDeltaTests(unittest.TestCase):
    def test_whitespace_only_deltas_normalize_to_nothing(self):
        for text in ("", "   ", "\n", "\t\n  "):
            self.assertEqual(stall_watchdog.normalize_delta(text), "")

    def test_internal_whitespace_is_collapsed(self):
        self.assertEqual(stall_watchdog.normalize_delta("  a\n\tb  "), "a b")

    def test_signature_is_bounded(self):
        long = "x" * (stall_watchdog.DELTA_SIGNATURE_LIMIT * 3)
        self.assertEqual(
            len(stall_watchdog.normalize_delta(long)),
            stall_watchdog.DELTA_SIGNATURE_LIMIT,
        )


class OmpStreamTests(unittest.TestCase):
    def _run(self, lines, *, stall_seconds=60.0, step=10.0):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=stall_seconds, harness="omp")
        now = 0.0
        for line in lines:
            now += step
            watchdog.observe_line(line, now=now)
        return watchdog, now

    def test_repeated_identical_thinking_deltas_stall(self):
        lines = [omp_delta("```", seq=index) for index in range(40)]
        watchdog, now = self._run(lines)
        idle = watchdog.stalled_for(now)
        self.assertIsNotNone(idle)
        self.assertGreaterEqual(idle, 60.0)

    def test_fence_cycle_stalls_even_though_consecutive_deltas_differ(self):
        # The observed failure alternated an opening and a closing fence. A
        # compare-with-the-previous-delta rule would call every event progress.
        lines = [omp_delta("```python" if index % 2 else "```", seq=index) for index in range(40)]
        watchdog, now = self._run(lines)
        self.assertIsNotNone(watchdog.stalled_for(now))

    def test_fence_alternating_with_newlines_stalls(self):
        lines = [omp_delta("\n" if index % 2 else "```", seq=index) for index in range(40)]
        watchdog, now = self._run(lines)
        self.assertIsNotNone(watchdog.stalled_for(now))

    def test_distinct_thinking_deltas_do_not_stall(self):
        lines = [omp_delta(f"step {index}", seq=index) for index in range(40)]
        watchdog, now = self._run(lines)
        self.assertIsNone(watchdog.stalled_for(now))

    def test_distinct_text_deltas_do_not_stall(self):
        lines = [omp_delta(f"word{index} ", kind="text_delta", seq=index) for index in range(40)]
        watchdog, now = self._run(lines)
        self.assertIsNone(watchdog.stalled_for(now))

    def test_tool_execution_in_flight_never_stalls(self):
        start = json.dumps(
            {
                "type": "tool_execution_start",
                "toolCallId": "call_1",
                "toolName": "bash",
                "args": {"command": "pytest"},
            }
        )
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        watchdog.observe_line(start, now=1.0)
        self.assertEqual(watchdog.tools_in_flight, 1)
        self.assertIsNone(watchdog.stalled_for(100_000.0))

    def test_tool_execution_end_releases_the_hold(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        watchdog.observe_line(
            json.dumps({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash"}),
            now=1.0,
        )
        watchdog.observe_line(
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "c1",
                    "toolName": "bash",
                    "isError": False,
                }
            ),
            now=2.0,
        )
        self.assertEqual(watchdog.tools_in_flight, 0)
        self.assertIsNotNone(watchdog.stalled_for(300.0))

    def test_turn_boundaries_count_as_progress(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        watchdog.observe_line(json.dumps({"type": "turn_start"}), now=1.0)
        watchdog.observe_line(json.dumps({"type": "turn_start"}), now=100.0)
        self.assertIsNone(watchdog.stalled_for(120.0))

    def test_repeated_deltas_after_a_tool_call_start_a_fresh_window(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        watchdog.observe_line(omp_delta("```", seq=0), now=1.0)
        watchdog.observe_line(omp_delta("```", seq=1), now=2.0)
        watchdog.observe_line(
            json.dumps({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read"}),
            now=3.0,
        )
        watchdog.observe_line(
            json.dumps({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "read"}),
            now=4.0,
        )
        # The same fence after a tool call is new output for this phase.
        watchdog.observe_line(omp_delta("```", seq=2), now=5.0)
        self.assertIsNone(watchdog.stalled_for(60.0))
        self.assertIsNotNone(watchdog.stalled_for(200.0))


class ClaudeStreamTests(unittest.TestCase):
    def test_long_running_command_is_never_stalled(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="claude")
        watchdog.observe_line(
            claude_assistant(
                {"type": "text", "text": "Running the suite."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "pytest"},
                },
            ),
            now=1.0,
        )
        # 90 minutes inside one Bash call: the engine's tool timeout owns this.
        self.assertIsNone(watchdog.stalled_for(5_400.0))
        watchdog.observe_line(claude_tool_result("toolu_1"), now=5_401.0)
        self.assertIsNone(watchdog.stalled_for(5_402.0))
        self.assertIsNotNone(watchdog.stalled_for(5_600.0))

    def test_repeated_thinking_blocks_stall(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="claude")
        now = 0.0
        for _ in range(40):
            now += 10.0
            watchdog.observe_line(
                claude_assistant({"type": "thinking", "thinking": "```"}), now=now
            )
        self.assertIsNotNone(watchdog.stalled_for(now))

    def test_distinct_thinking_blocks_do_not_stall(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="claude")
        now = 0.0
        for index in range(40):
            now += 10.0
            watchdog.observe_line(
                claude_assistant({"type": "thinking", "thinking": f"considering {index}"}),
                now=now,
            )
        self.assertIsNone(watchdog.stalled_for(now))

    def test_system_events_are_not_progress(self):
        # Thinking-token accounting keeps climbing while a model repeats itself,
        # so counting a system event as progress would hide every stall.
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="claude")
        watchdog.observe_line(
            json.dumps({"type": "system", "subtype": "init", "session_id": "s"}), now=1.0
        )
        for index in range(20):
            watchdog.observe_line(
                json.dumps(
                    {"type": "system", "subtype": "thinking_tokens", "thinking_tokens": index * 100}
                ),
                now=10.0 * (index + 1),
            )
        self.assertIsNotNone(watchdog.stalled_for(300.0))

    def test_result_event_is_progress(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="claude")
        watchdog.observe_line(claude_assistant({"type": "text", "text": "hi"}), now=1.0)
        watchdog.observe_line(json.dumps({"type": "result", "result": "done"}), now=300.0)
        self.assertIsNone(watchdog.stalled_for(310.0))


class CodexStreamTests(unittest.TestCase):
    def test_command_execution_holds_the_watchdog_open(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="codex")
        watchdog.observe_line(
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
            now=1.0,
        )
        self.assertIsNone(watchdog.stalled_for(9_000.0))
        watchdog.observe_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "python3 -m unittest",
                        "status": "completed",
                    },
                }
            ),
            now=9_001.0,
        )
        self.assertEqual(watchdog.tools_in_flight, 0)
        self.assertIsNotNone(watchdog.stalled_for(9_200.0))

    def test_repeated_agent_messages_stall(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="codex")
        now = 0.0
        for _ in range(40):
            now += 10.0
            watchdog.observe_line(
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "```"}}
                ),
                now=now,
            )
        self.assertIsNotNone(watchdog.stalled_for(now))

    def test_turn_completed_is_progress(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="codex")
        watchdog.observe_line(json.dumps({"type": "turn.started"}), now=1.0)
        watchdog.observe_line(json.dumps({"type": "turn.completed"}), now=300.0)
        self.assertIsNone(watchdog.stalled_for(310.0))


class OtherHarnessTests(unittest.TestCase):
    def test_kimi_role_envelopes_pair_tool_calls(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="kimi")
        watchdog.observe_line(
            json.dumps(
                {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "bash", "arguments": "{}"}}
                    ],
                }
            ),
            now=1.0,
        )
        self.assertIsNone(watchdog.stalled_for(9_000.0))
        watchdog.observe_line(
            json.dumps({"role": "tool", "tool_call_id": "call_1", "content": "output"}), now=9_001.0
        )
        self.assertIsNotNone(watchdog.stalled_for(9_200.0))

    def test_devin_plain_text_lines_are_deduplicated(self):
        repeated = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="devin")
        varied = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="devin")
        now = 0.0
        for index in range(40):
            now += 10.0
            repeated.observe_line("still working...", now=now)
            varied.observe_line(f"checked file {index}", now=now)
        self.assertIsNotNone(repeated.stalled_for(now))
        self.assertIsNone(varied.stalled_for(now))

    def test_unmodeled_structured_events_fall_back_to_line_deduplication(self):
        repeated = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="brand-new-engine")
        varied = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="brand-new-engine")
        now = 0.0
        for index in range(40):
            now += 10.0
            repeated.observe_line(json.dumps({"type": "heartbeat"}), now=now)
            varied.observe_line(json.dumps({"type": "heartbeat", "n": index}), now=now)
        self.assertIsNotNone(repeated.stalled_for(now))
        self.assertIsNone(varied.stalled_for(now))

    def test_unmatched_tool_finish_does_not_leak_an_in_flight_hold(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        watchdog.observe_line(
            json.dumps({"type": "tool_execution_start", "toolCallId": "a", "toolName": "read"}),
            now=1.0,
        )
        watchdog.observe_line(
            json.dumps({"type": "tool_execution_end", "toolCallId": "b", "toolName": "read"}),
            now=2.0,
        )
        self.assertEqual(watchdog.tools_in_flight, 0)
        self.assertIsNotNone(watchdog.stalled_for(300.0))


class WatchdogLifecycleTests(unittest.TestCase):
    def test_disabled_watchdog_never_reports_a_stall(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=0.0, harness="omp")
        for index in range(40):
            watchdog.observe_line(omp_delta("```", seq=index), now=float(index))
        self.assertFalse(watchdog.enabled)
        self.assertIsNone(watchdog.stalled_for(100_000.0))

    def test_a_silent_child_is_left_to_the_stage_timeout(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        self.assertIsNone(watchdog.stalled_for(100_000.0))

    def test_blank_lines_do_not_arm_the_watchdog(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        watchdog.observe_line("   ", now=1.0)
        self.assertIsNone(watchdog.stalled_for(100_000.0))

    def test_stall_detail_reports_threshold_and_last_progress(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        now = 0.0
        for index in range(20):
            now += 10.0
            watchdog.observe_line(omp_delta("```", seq=index), now=now)
        idle = watchdog.stalled_for(now)
        assert idle is not None
        detail = watchdog.stall_detail(idle)
        self.assertEqual(detail["thresholdSeconds"], 60.0)
        self.assertEqual(detail["lastProgress"], "thinking_delta")
        self.assertEqual(detail["linesSeen"], 20)
        self.assertGreaterEqual(detail["idleSeconds"], 60.0)

    def test_stall_seconds_from_minutes(self):
        self.assertEqual(stall_watchdog.stall_seconds_from_minutes(8), 480.0)
        self.assertEqual(stall_watchdog.stall_seconds_from_minutes(0), 0.0)
        self.assertEqual(stall_watchdog.stall_seconds_from_minutes(-3), 0.0)


if __name__ == "__main__":
    unittest.main()
