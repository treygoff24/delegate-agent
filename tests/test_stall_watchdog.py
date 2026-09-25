import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import harness_events, stall_watchdog  # noqa: E402


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
    def test_opencode_running_tool_holds_until_matching_completion(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="opencode")
        watchdog.observe_line(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call_1",
                        "state": {"status": "running"},
                    },
                }
            ),
            now=0.0,
        )

        self.assertEqual(watchdog.tools_in_flight, 1)
        self.assertIsNone(watchdog.stalled_for(600.0))

        watchdog.observe_line(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call_1",
                        "state": {"status": "completed"},
                    },
                }
            ),
            now=600.0,
        )
        self.assertEqual(watchdog.tools_in_flight, 0)
        self.assertEqual(watchdog.stalled_for(1_200.0), 600.0)

    def test_grok_tool_call_holds_until_matching_update(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="grok")
        watchdog.observe_line(
            json.dumps(
                {
                    "type": "tool_call",
                    "toolCallId": "call_1",
                    "name": "read_file",
                    "rawInput": {"target_file": "PLAN.md"},
                }
            ),
            now=0.0,
        )

        self.assertEqual(watchdog.tools_in_flight, 1)
        self.assertIsNone(watchdog.stalled_for(600.0))

        watchdog.observe_line(
            json.dumps(
                {
                    "type": "tool_call_update",
                    "toolCallId": "call_1",
                    "status": "completed",
                }
            ),
            now=600.0,
        )
        self.assertEqual(watchdog.tools_in_flight, 0)
        self.assertEqual(watchdog.stalled_for(1_200.0), 600.0)

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


def codex_command(item_id: str, command: str, *, status: str | None = None) -> str:
    """One codex command_execution item line; ``status`` None means started."""
    item = {"id": item_id, "type": "command_execution", "command": command}
    if status is None:
        item["status"] = "in_progress"
        return json.dumps({"type": "item.started", "item": item})
    item["status"] = status
    item["exit_code"] = 0 if status == "completed" else 1
    return json.dumps({"type": "item.completed", "item": item})


class _FedWatchdog:
    """A watchdog fed the way the runner feeds it: raw line plus the tool
    events the stream accumulator derived from that same line."""

    def __init__(self, watchdog: stall_watchdog.StallWatchdog) -> None:
        self.watchdog = watchdog
        self.accumulator = harness_events.StreamAccumulator(harness=watchdog.harness or "codex")

    def feed(self, line: str, now: float) -> None:
        before = self.accumulator.events.total
        self.accumulator.ingest_line(line)
        events = self.accumulator.events.last(self.accumulator.events.total - before)
        self.watchdog.observe_line(
            line, now=now, tool_events=stall_watchdog.tool_events_from(events)
        )

    def command(self, index: int, command: str, *, status: str, start: float, end: float):
        self.feed(codex_command(f"item_{index}", command), start)
        self.feed(codex_command(f"item_{index}", command, status=status), end)


class RepeatedToolFailureTests(unittest.TestCase):
    def test_identical_failures_trip_at_the_limit(self):
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="codex"))
        limit = stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT
        for index in range(limit - 1):
            fed.command(index, "cat missing.txt", status="failed", start=index, end=index + 0.5)
        self.assertIsNone(fed.watchdog.stalled_for(limit + 1.0))
        fed.command(limit, "cat missing.txt", status="failed", start=limit, end=limit + 0.5)
        idle = fed.watchdog.stalled_for(limit + 1.0)
        self.assertIsNotNone(idle)
        detail = fed.watchdog.stall_detail(idle or 0.0)
        self.assertEqual(detail["stallReason"], "repeated_tool_failure")
        self.assertEqual(detail["tool"], "command_execution")
        self.assertEqual(detail["target"], "cat missing.txt")
        self.assertEqual(detail["failures"], limit)

    def test_a_success_in_between_resets_the_count(self):
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="codex"))
        limit = stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT
        now = 0.0
        for index in range(2 * (limit - 1) + 1):
            if index == limit - 1:
                fed.command(index, "ls", status="completed", start=now, end=now + 0.5)
            else:
                fed.command(index, "cat missing.txt", status="failed", start=now, end=now + 0.5)
            now += 1.0
        self.assertIsNone(fed.watchdog.stalled_for(now))

    def test_different_failing_targets_do_not_trip(self):
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="codex"))
        for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT * 2):
            fed.command(index, f"cat missing-{index}.txt", status="failed", start=index, end=index)
        self.assertIsNone(fed.watchdog.stalled_for(30.0))


FIX_LOOP_COMMAND = "pytest tests/test_x.py"


def _codex_fix_loop(index: int, edit: bool) -> list[str]:
    if edit:
        item = {"id": f"e{index}", "type": "file_change", "changes": [{"path": "a.py"}]}
        return [
            json.dumps({"type": "item.started", "item": {**item, "status": "in_progress"}}),
            json.dumps({"type": "item.completed", "item": {**item, "status": "completed"}}),
        ]
    return [
        codex_command(f"c{index}", FIX_LOOP_COMMAND),
        codex_command(f"c{index}", FIX_LOOP_COMMAND, status="failed"),
    ]


def _claude_fix_loop(index: int, edit: bool) -> list[str]:
    name, tool_input = (
        ("Edit", {"file_path": "a.py"}) if edit else ("Bash", {"command": FIX_LOOP_COMMAND})
    )
    tool_id = f"t{index}{edit}"
    use = {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
    result = {"type": "tool_result", "tool_use_id": tool_id, "content": "x", "is_error": not edit}
    return [
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [use]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": [result]}}),
    ]


def _pi_fix_loop(index: int, edit: bool) -> list[str]:
    # The shape of tests/fixtures/pi/tool_read.jsonl: only the start carries
    # `args`; the end has the toolCallId, toolName, result, and isError.
    name, args = ("edit", {"path": "a.py"}) if edit else ("bash", {"command": FIX_LOOP_COMMAND})
    call_id = f"call_{index}{edit}|fc_{index}"
    result = {"content": [{"type": "text", "text": "ok" if edit else "1 failed"}]}
    return [
        json.dumps(
            {"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args}
        ),
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolCallId": call_id,
                "toolName": name,
                "result": result,
                "isError": not edit,
            }
        ),
    ]


def _cursor_fix_loop(index: int, edit: bool) -> list[str]:
    key, args = (
        ("editToolCall", {"path": "a.py"})
        if edit
        else ("shellToolCall", {"command": FIX_LOOP_COMMAND})
    )
    result = {"success": {}} if edit else {"error": {"message": "exit 1"}}
    call_id = f"u{index}{edit}"
    return [
        json.dumps(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": call_id,
                "tool_call": {key: {"args": args}},
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": call_id,
                "tool_call": {key: {"args": args, "result": result}},
            }
        ),
    ]


def _grok_fix_loop(index: int, edit: bool) -> list[str]:
    name, raw = (
        ("search_replace", {"target_file": "a.py"})
        if edit
        else ("run_terminal_command", {"command": FIX_LOOP_COMMAND})
    )
    call_id = f"g{index}{edit}"
    return [
        json.dumps(
            {
                "type": "tool_call",
                "toolCallId": call_id,
                "status": "pending",
                "toolName": name,
                "rawInput": raw,
            }
        ),
        json.dumps(
            {
                "type": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed" if edit else "failed",
            }
        ),
    ]


def _opencode_fix_loop(index: int, edit: bool) -> list[str]:
    tool, tool_input = (
        ("edit", {"filePath": "a.py"}) if edit else ("bash", {"command": FIX_LOOP_COMMAND})
    )
    state = {"status": "completed" if edit else "error", "input": tool_input}
    part = {"type": "tool", "tool": tool, "callID": f"o{index}{edit}", "state": state}
    return [json.dumps({"type": "tool_use", "part": part})]


FIX_LOOP_LINES = {
    "codex": _codex_fix_loop,
    "claude": _claude_fix_loop,
    "omp": _pi_fix_loop,
    "pi": _pi_fix_loop,
    "cursor": _cursor_fix_loop,
    "grok": _grok_fix_loop,
    "opencode": _opencode_fix_loop,
}


class FixLoopResetTests(unittest.TestCase):
    """A successful edit between identical failing test runs is a fix loop, not a loop."""

    def run_fix_loop(self, engine: str, *, with_edit: bool) -> str | None:
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness=engine))
        lines = FIX_LOOP_LINES[engine]
        now = 0.0
        for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT + 2):
            for line in lines(index, False) + (lines(index, True) if with_edit else []):
                now += 1.0
                fed.feed(line, now)
        return fed.watchdog.stall_detail(0.0).get("stallReason")

    def test_codex_patch_between_failing_runs_does_not_trip(self):
        self.assertEqual(self.run_fix_loop("codex", with_edit=True), "idle")

    def test_every_engine_resets_on_a_successful_edit_and_still_trips_without_one(self):
        for engine in FIX_LOOP_LINES:
            with self.subTest(engine=engine):
                self.assertEqual(self.run_fix_loop(engine, with_edit=True), "idle")
                self.assertEqual(
                    self.run_fix_loop(engine, with_edit=False), "repeated_tool_failure"
                )

    def test_codex_reasoning_between_failing_runs_is_not_a_reset(self):
        """Only a completed tool breaks the streak; the model thinking does not."""
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="codex"))
        now = 0.0
        for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT):
            reasoning = {"id": f"r{index}", "type": "reasoning", "text": f"try {index}"}
            for line in [
                json.dumps({"type": "item.completed", "item": reasoning}),
                *_codex_fix_loop(index, False),
            ]:
                now += 1.0
                fed.feed(line, now)
        self.assertEqual(fed.watchdog.stall_detail(0.0).get("stallReason"), "repeated_tool_failure")


class RepeatedToolCallProgressTests(unittest.TestCase):
    def test_fast_identical_calls_do_not_keep_resetting_the_idle_clock(self):
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=300.0, harness="codex"))
        fed.command(0, "git status", status="completed", start=0.0, end=1.0)
        now = 1.0
        index = 1
        while now < 400.0:
            # Ten seconds of thought, one second of the same no-op call.
            fed.command(index, "git status", status="completed", start=now + 10, end=now + 11)
            now += 11.0
            index += 1
        idle = fed.watchdog.stalled_for(now)
        self.assertIsNotNone(idle)
        self.assertEqual(fed.watchdog.stall_detail(idle or 0.0)["stallReason"], "idle")

    def test_distinct_calls_are_still_progress(self):
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=300.0, harness="codex"))
        now = 0.0
        for index in range(40):
            fed.command(index, f"sed -n {index}p f", status="completed", start=now, end=now + 1)
            now += 11.0
        self.assertIsNone(fed.watchdog.stalled_for(now))

    def test_a_slow_identical_polling_loop_is_not_idle(self):
        """Time inside a repeated call is not idle time: waiting is legitimate."""
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=300.0, harness="codex"))
        now = 0.0
        for index in range(30):
            # A 60-second `delegate wait` returning the same thing, 2 s apart.
            fed.command(index, "delegate wait r1", status="completed", start=now, end=now + 60)
            now += 62.0
        self.assertIsNone(fed.watchdog.stalled_for(now))


class RunawayOutputTests(unittest.TestCase):
    def test_unique_output_without_tools_trips_past_the_budget(self):
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=480.0, harness="omp", runaway_output_chars=1000
        )
        for index in range(30):
            watchdog.observe_line(omp_delta(f"unique chunk {index} " + "x" * 40), now=index)
        idle = watchdog.stalled_for(31.0)
        self.assertIsNotNone(idle)
        detail = watchdog.stall_detail(idle or 0.0)
        self.assertEqual(detail["stallReason"], "runaway_output")
        self.assertEqual(detail["outputCharLimit"], 1000)
        self.assertGreater(detail["outputChars"], 1000)

    def test_tool_activity_resets_the_budget(self):
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=480.0, harness="omp", runaway_output_chars=1000
        )
        for index in range(60):
            watchdog.observe_line(omp_delta(f"unique chunk {index} " + "x" * 40), now=index)
            if index % 10 == 9:
                start = {"type": "tool_execution_start", "toolCallId": f"t{index}"}
                end = {"type": "tool_execution_end", "toolCallId": f"t{index}"}
                watchdog.observe_line(json.dumps(start), now=index)
                watchdog.observe_line(json.dumps(end), now=index)
        self.assertIsNone(watchdog.stalled_for(61.0))

    def test_the_budget_counts_payload_characters_not_prefixed_dedup_keys(self):
        """Token-sized omp deltas: 800 real characters stay under a 1000 budget
        even though each dedup key carries a 15-character event-type prefix."""
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=480.0, harness="omp", runaway_output_chars=1000
        )
        tokens = [f"t{index:03d}" for index in range(200)]  # 4 chars each, 800 total
        for index, token in enumerate(tokens):
            watchdog.observe_line(omp_delta(token, seq=index), now=float(index))
        # The *_end event repeats the whole block its deltas already streamed.
        end = {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_end", "content": "".join(tokens)},
        }
        watchdog.observe_line(json.dumps(end), now=201.0)
        self.assertIsNone(watchdog.stalled_for(202.0))
        for index in range(60):  # 240 more real characters: past the budget
            watchdog.observe_line(omp_delta(f"u{index:03d}", seq=300 + index), now=202.0)
        idle = watchdog.stalled_for(203.0)
        self.assertIsNotNone(idle)
        detail = watchdog.stall_detail(idle or 0.0)
        self.assertEqual(detail["stallReason"], "runaway_output")
        self.assertEqual(detail["outputChars"], 1004)

    def test_omp_tool_output_streamed_during_a_tool_is_not_model_output(self):
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=480.0, harness="omp", runaway_output_chars=1000
        )
        watchdog.observe_line(
            json.dumps({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash"}),
            now=0.0,
        )
        for index in range(50):
            update = {
                "type": "tool_execution_update",
                "toolCallId": "t1",
                "toolName": "bash",
                "partialResult": {"content": [{"type": "text", "text": f"line {index} " * 20}]},
            }
            watchdog.observe_line(json.dumps(update), now=float(index))
        watchdog.observe_line(
            json.dumps({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash"}),
            now=51.0,
        )
        self.assertIsNone(watchdog.stalled_for(52.0))
        self.assertNotIn("outputChars", watchdog.stall_detail(0.0))

    def test_a_devin_text_stream_past_the_budget_does_not_trip(self):
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=480.0, harness="devin", runaway_output_chars=1000
        )
        for index in range(100):
            watchdog.observe_line(f"build step {index}: " + "compiling " * 5, now=float(index))
        self.assertIsNone(watchdog.stalled_for(101.0))
        # The idle check still applies to devin.
        idle = watchdog.stalled_for(101.0 + 481.0)
        self.assertIsNotNone(idle)
        self.assertEqual(watchdog.stall_detail(idle or 0.0)["stallReason"], "idle")

    def test_unmodeled_whole_line_events_do_not_count(self):
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=480.0, harness="claude", runaway_output_chars=1000
        )
        for index in range(100):
            line = json.dumps({"type": "some_future_event", "n": index, "pad": "x" * 50})
            watchdog.observe_line(line, now=float(index))
        self.assertIsNone(watchdog.stalled_for(101.0))

    def test_default_budget_is_far_past_a_real_report(self):
        self.assertGreaterEqual(stall_watchdog.RUNAWAY_OUTPUT_CHARS_DEFAULT, 100_000)


class ProgressProbeTests(unittest.TestCase):
    def test_a_changed_probe_token_restarts_the_idle_clock(self):
        heads = iter(["a", "b", "b"])
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=60.0, harness="omp", progress_probe=lambda: next(heads)
        )
        watchdog.prime_probe()
        watchdog.observe_line(omp_delta("hello"), now=0.0)
        self.assertIsNone(watchdog.stalled_for(100.0))
        self.assertEqual(watchdog.last_progress_label, "worktree_commit")
        # Unchanged since the commit: the next window stalls.
        self.assertIsNotNone(watchdog.stalled_for(200.0))

    def test_an_unknown_probe_never_counts_as_progress(self):
        def broken() -> str | None:
            raise OSError("git missing")

        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=60.0, harness="omp", progress_probe=broken
        )
        watchdog.prime_probe()
        watchdog.observe_line(omp_delta("hello"), now=0.0)
        self.assertIsNotNone(watchdog.stalled_for(100.0))


class CompletionReportTests(unittest.TestCase):
    def test_the_prompt_template_line_is_not_a_report(self):
        for line in (
            "Status: completed / blocked / failed",
            "- **Status:** completed / blocked / failed",
            "- Status: completed/blocked/failed",
        ):
            with self.subTest(line=line):
                self.assertIsNone(stall_watchdog.completion_report_status(f"intro\n{line}\n"))
        self.assertEqual(
            stall_watchdog.completion_report_status("- **Status:** blocked\n- need creds"),
            "blocked",
        )

    def test_prose_that_starts_with_a_status_word_is_not_a_report(self):
        for line in (
            "Status: completed the inventory",
            "Status: failed to open the lockfile",
            "- Status: completed | failed | blocked",
            "Status: completed (with caveats)",
        ):
            with self.subTest(line=line):
                self.assertIsNone(stall_watchdog.completion_report_status(f"work\n{line}\n"))

    def test_the_status_line_is_judged_on_its_own_line(self):
        # A \s in the old lookahead crossed into the next line and rejected this.
        text = "## Completion report\nStatus: completed\n/tmp/results.txt has the output\n"
        self.assertEqual(stall_watchdog.completion_report_status(text), "completed")

    def test_real_report_shapes_are_found(self):
        for line, status in (
            ("- Status: completed", "completed"),
            ("**Status:** failed", "failed"),
            ("Status: blocked.", "blocked"),
            ("- **Status:** completed  ", "completed"),
            ("> Status: `failed`!", "failed"),
        ):
            with self.subTest(line=line):
                text = f"report\n{line}\n- Files: none"
                self.assertEqual(stall_watchdog.completion_report_status(text), status)

    def test_trailing_report_status_is_found(self):
        text = "worked a while\n\n## Completion report\n- **Status:** completed\n- did it"
        self.assertEqual(stall_watchdog.completion_report_status(text), "completed")
        self.assertEqual(stall_watchdog.completion_report_status("Status: blocked"), "blocked")

    def test_no_report_or_a_distant_one_is_none(self):
        self.assertIsNone(stall_watchdog.completion_report_status("still working on it"))
        distant = "Status: completed\n" + "more work\n" * 2000
        self.assertIsNone(stall_watchdog.completion_report_status(distant))

    def test_only_text_stream_harnesses_use_it(self):
        self.assertTrue(stall_watchdog.reports_completion_by_text("devin"))
        self.assertFalse(stall_watchdog.reports_completion_by_text("claude"))


class ProcessGroupActivityTests(unittest.TestCase):
    def activity(self, first, second):
        samples = iter([first, second])
        return stall_watchdog.process_group_activity(
            4242, rows=lambda _pgid: next(samples), sleep=lambda _s: None
        )

    def test_cpu_growth_reads_as_active(self):
        result = self.activity([(10, "S", 1.0, "devin")], [(10, "S", 3.5, "devin")])
        self.assertEqual(result["childActivity"], "cpu_active")
        self.assertEqual(result["processes"][0]["cpuSeconds"], 3.5)

    def test_flat_cpu_reads_as_waiting(self):
        result = self.activity([(10, "S", 1.0, "devin")], [(10, "S", 1.0, "devin")])
        self.assertEqual(result["childActivity"], "waiting")

    def test_an_empty_group_reads_as_gone(self):
        self.assertEqual(self.activity([], [])["childActivity"], "no_processes")

    def test_ps_time_parsing(self):
        self.assertEqual(stall_watchdog._cpu_seconds("01:02.50"), 62.5)
        self.assertEqual(stall_watchdog._cpu_seconds("1-00:00:01"), 86401.0)
        self.assertEqual(stall_watchdog._cpu_seconds("garbage"), 0.0)


class EventBufferLastTests(unittest.TestCase):
    def test_last_spans_head_and_tail(self):
        buffer = harness_events.EventBuffer()
        total = harness_events.EVENT_HEAD + 5
        for index in range(total):
            buffer.append(harness_events.NormalizedEvent(kind="k", message=str(index)))
        self.assertEqual([e.message for e in buffer.last(2)], [str(total - 2), str(total - 1)])
        self.assertEqual(buffer.last(0), [])
        spanning = buffer.last(7)
        self.assertEqual([e.message for e in spanning], [str(i) for i in range(total - 7, total)])


FIXTURES = ROOT / "tests" / "fixtures"


def _fixture_line(relative: str, predicate) -> dict:
    for raw in (FIXTURES / relative).read_text(encoding="utf-8").splitlines():
        if raw.strip():
            payload = json.loads(raw)
            if predicate(payload):
                return payload
    raise AssertionError(f"no matching line in {relative}")


def _cursor_thinking(text: str, timestamp_ms: int) -> str:
    """A cursor thinking delta in the shape of tests/fixtures/cursor/tool_read.jsonl."""
    line = _fixture_line(
        "cursor/tool_read.jsonl",
        lambda p: p.get("type") == "thinking" and p.get("subtype") == "delta",
    )
    return json.dumps({**line, "text": text, "timestamp_ms": timestamp_ms})


def _pi_tool_start(call_id: str, command: str) -> str:
    """tool_execution_start in the shape of tests/fixtures/pi/tool_read.jsonl."""
    return json.dumps(
        {
            "type": "tool_execution_start",
            "toolCallId": call_id,
            "toolName": "bash",
            "args": {"command": command},
        }
    )


def _codex_file_change(index: int, *, status: str) -> list[str]:
    """A codex apply_patch, in the shape of real `codex exec --json` captures."""
    item = {
        "id": f"item_{index}",
        "type": "file_change",
        "changes": [{"path": "/repo/src/app.py", "kind": "update"}],
    }
    return [
        json.dumps({"type": "item.started", "item": {**item, "status": "in_progress"}}),
        json.dumps({"type": "item.completed", "item": {**item, "status": status}}),
    ]


def _codex_error_item(index: int, message: str = "patch rejected: context mismatch") -> str:
    """A codex error item: a bare item.completed carrying only a message."""
    item = {"id": f"item_{index}", "type": "error", "message": message}
    return json.dumps({"type": "item.completed", "item": item})


# The one error item seen across 95 real codex logs, at most twice per log.
HOOK_TRUST_NOTICE = (
    "`--dangerously-bypass-hook-trust` is enabled. "
    "Enabled hooks may run without review for this invocation."
)


def _codex_real_command(item_id: str, command: str, status: str) -> list[str]:
    """A command_execution start/end pair in the shape of
    tests/fixtures/codex_real_stream.jsonl."""
    started = _fixture_line(
        "codex_real_stream.jsonl",
        lambda p: (
            p.get("type") == "item.started" and p.get("item", {}).get("type") == "command_execution"
        ),
    )
    item = {**started["item"], "id": item_id, "command": command}
    ended = {**item, "exit_code": 0 if status == "completed" else 1, "status": status}
    return [
        json.dumps({**started, "item": item}),
        json.dumps({"type": "item.completed", "item": ended}),
    ]


def _long_command(index: int) -> str:
    """Commands that agree on their first 512+ characters (Astra's repro)."""
    return "printf " + "x" * 505 + "; pytest tests/test_" + str(index) + ".py"


def _opencode_tool_use(call_id: str, command: str, status: str, timestamp: int) -> str:
    """A tool_use in the shape of tests/fixtures/opencode/tool_run.ndjson."""
    line = _fixture_line("opencode/tool_run.ndjson", lambda p: p.get("type") == "tool_use")
    part = dict(line["part"])
    state = {**part["state"], "status": status, "input": {"command": command}}
    if status != "completed":
        state.pop("output", None)
    part.update(tool="bash", callID=call_id, state=state)
    return json.dumps({**line, "timestamp": timestamp, "part": part})


class BlockingProbeRecheckTests(unittest.TestCase):
    """A stall decided before a blocking step is re-derived after it (item A)."""

    def probe_watchdog(self, during_probe: str) -> stall_watchdog.StallWatchdog:
        calls = {"count": 0}
        watchdog: stall_watchdog.StallWatchdog

        def probe() -> str:
            calls["count"] += 1
            if calls["count"] == 2:
                # The stdout thread delivers a line while the HEAD probe runs.
                watchdog.observe_line(during_probe, now=61.5)
            return "same-head"

        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=60.0, harness="omp", progress_probe=probe
        )
        watchdog.prime_probe()
        watchdog.observe_line(json.dumps({"type": "turn_start"}), now=0.0)
        return watchdog

    def test_a_tool_that_starts_during_the_head_probe_cancels_the_stall(self):
        watchdog = self.probe_watchdog(_pi_tool_start("call_a|fc_a", "sleep 300"))
        self.assertIsNone(watchdog.stalled_for(61.0))
        self.assertEqual(watchdog.tools_in_flight, 1)

    def test_output_that_lands_during_the_head_probe_cancels_the_stall(self):
        watchdog = self.probe_watchdog(omp_delta("a new thought", kind="text_delta"))
        self.assertIsNone(watchdog.stalled_for(61.0))

    def test_confirm_stall_sees_progress_that_landed_after_the_decision(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="omp")
        watchdog.observe_line(json.dumps({"type": "turn_start"}), now=0.0)
        self.assertIsNotNone(watchdog.stalled_for(61.0))
        watchdog.observe_line(_pi_tool_start("call_b|fc_b", "sleep 300"), now=61.5)
        self.assertIsNone(watchdog.confirm_stall(62.0))


MODEL_OUTPUT_EVENTS = {
    "codex": (
        "codex_real_stream.jsonl",
        {"item.completed/agent_message", "item.completed/reasoning"},
    ),
    "droid": ("droid/simple_text.jsonl", {"message"}),
    "cursor": ("cursor/tool_read.jsonl", {"thinking/delta", "assistant"}),
    "claude": ("claude/structured_output.jsonl", {"assistant"}),
    "grok": ("grok/tool_read_multi_response.jsonl", {"thought", "text"}),
    "pi": (
        "pi/tool_read.jsonl",
        {"message_update/text_delta", "message_update/toolcall_delta"},
    ),
    "omp": (
        "pi/tool_read.jsonl",
        {"message_update/text_delta", "message_update/toolcall_delta"},
    ),
    "opencode": ("opencode/simple_text.ndjson", {"text"}),
}


class ModelOutputClassificationTests(unittest.TestCase):
    """Every model-output event in a checked-in fixture is a charged delta (item B)."""

    @staticmethod
    def carries_model_text(payload: dict) -> bool:
        """An assistant envelope holding only a tool_use block has no model text."""
        message = payload.get("message")
        if payload.get("type") != "assistant" or not isinstance(message, dict):
            return True
        content = message.get("content")
        return isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") in {"text", "thinking"}
            for block in content
        )

    @staticmethod
    def event_key(payload: dict) -> str:
        parts = [payload.get("type"), payload.get("subtype")]
        update = payload.get("assistantMessageEvent")
        if isinstance(update, dict):
            parts.append(update.get("type"))
        item = payload.get("item")
        if isinstance(item, dict):
            parts.append(item.get("type"))
        return "/".join(str(part) for part in parts if part is not None)

    def test_no_fixture_model_output_event_falls_back_to_the_whole_line(self):
        for harness, (relative, keys) in MODEL_OUTPUT_EVENTS.items():
            found: set[str] = set()
            for raw in (FIXTURES / relative).read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                payload = json.loads(line)
                key = self.event_key(payload)
                if key not in keys or not self.carries_model_text(payload):
                    continue
                found.add(key)
                with self.subTest(harness=harness, event=key):
                    signals = stall_watchdog.classify_line(line, harness=harness)
                    self.assertTrue(signals.deltas)
                    self.assertNotEqual(signals.deltas, (line,), "whole-line fallback")
                    self.assertNotEqual(signals.delta_chars, (0,) * len(signals.deltas))
            self.assertEqual(found, keys, f"{harness}: fixture lacks an expected event")

    def test_a_repeated_cursor_thinking_token_is_not_progress(self):
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="cursor")
        base = 1788760922776
        for second in range(10_000):
            watchdog.observe_line(
                _cursor_thinking("Reading marker.txt to", base + second * 1000), now=second
            )
        idle = watchdog.stalled_for(10_000.0)
        self.assertIsNotNone(idle)
        self.assertEqual(watchdog.stall_detail(idle or 0.0)["stallReason"], "idle")

    def test_cursor_thinking_charges_its_payload_toward_the_runaway_budget(self):
        watchdog = stall_watchdog.StallWatchdog(
            stall_seconds=480.0, harness="cursor", runaway_output_chars=1000
        )
        total = 0
        for index in range(60):
            text = f"thought {index} " + "y" * 20
            total += len(text)
            watchdog.observe_line(_cursor_thinking(text, 1788760922776 + index), now=index)
            if total > 1000:
                break
        idle = watchdog.stalled_for(float(index + 1))
        self.assertIsNotNone(idle)
        detail = watchdog.stall_detail(idle or 0.0)
        self.assertEqual(detail["stallReason"], "runaway_output")
        self.assertEqual(detail["outputChars"], total)

    def test_grok_usage_accounting_is_not_progress(self):
        usage = _fixture_line(
            "grok/tool_read_multi_response.jsonl", lambda p: p.get("type") == "usage"
        )
        watchdog = stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="grok")
        for second in range(0, 2000, 2):
            watchdog.observe_line(json.dumps({"type": "text", "data": "same"}), now=second)
            counters = {**usage["usage"], "output_tokens": second}
            watchdog.observe_line(json.dumps({**usage, "usage": counters}), now=second + 1)
        self.assertIsNotNone(watchdog.stalled_for(2000.0))


class PiCompletionTargetTests(unittest.TestCase):
    """Real pi/omp ends carry no args; the target comes from the start (item C)."""

    def test_twenty_identical_real_shape_failures_count(self):
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness):
                fed = _FedWatchdog(
                    stall_watchdog.StallWatchdog(stall_seconds=480.0, harness=harness)
                )
                now = 0.0
                for index in range(20):
                    for line in _pi_fix_loop(index, False):
                        now += 1.0
                        fed.feed(line, now)
                detail = fed.watchdog.stall_detail(0.0)
                self.assertEqual(detail["stallReason"], "repeated_tool_failure")
                self.assertEqual(detail["target"], FIX_LOOP_COMMAND)
                self.assertEqual(detail["failures"], stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT)

    def test_abandoned_starts_stay_bounded_and_a_later_start_keeps_its_target(self):
        accumulator = harness_events.StreamAccumulator(harness="pi")
        for index in range(10_000):
            accumulator.ingest_line(_pi_tool_start(f"call_{index}|fc_{index}", "sleep 1"))
        self.assertLessEqual(
            len(accumulator._pending_tool_uses), harness_events.PI_PENDING_TOOL_LIMIT
        )
        for line in _pi_fix_loop(99_999, False):
            accumulator.ingest_line(line)
        completed = accumulator.events.last(1)[0]
        self.assertEqual(completed.kind, "tool.completed")
        self.assertEqual(completed.target, FIX_LOOP_COMMAND)
        self.assertLessEqual(
            len(accumulator._pending_tool_uses), harness_events.PI_PENDING_TOOL_LIMIT
        )

    def test_the_start_target_is_forgotten_once_the_tool_completes(self):
        accumulator = harness_events.StreamAccumulator(harness="pi")
        for line in _pi_fix_loop(0, False):
            accumulator.ingest_line(line)
        self.assertEqual(accumulator._pending_tool_uses, {})


class FailedCodexFinishTests(unittest.TestCase):
    """An explicitly failed codex finish is neither a reset nor progress (item D)."""

    def run_lines(self, lines: list[str]) -> str | None:
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="codex"))
        for now, line in enumerate(lines, start=1):
            fed.feed(line, float(now))
        return fed.watchdog.stall_detail(0.0).get("stallReason")

    def test_repeated_failed_file_changes_alone_trip(self):
        lines = [
            line
            for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT)
            for line in _codex_file_change(index, status="failed")
        ]
        self.assertEqual(self.run_lines(lines), "repeated_tool_failure")

    def test_failed_file_changes_between_identical_failing_commands_trip(self):
        lines = [
            line
            for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT)
            for line in [
                *_codex_fix_loop(index, False),
                *_codex_file_change(100 + index, status="failed"),
            ]
        ]
        self.assertEqual(self.run_lines(lines), "repeated_tool_failure")

    def test_error_items_between_identical_failing_commands_trip(self):
        lines = [
            line
            for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT)
            for line in [*_codex_fix_loop(index, False), _codex_error_item(100 + index)]
        ]
        self.assertEqual(self.run_lines(lines), "repeated_tool_failure")

    def test_an_error_item_is_not_progress_for_idle(self):
        # A failed file_change's own start-to-finish time is tool time, which the
        # idle clock already excludes; an error item has no start, so this is
        # where "not progress" is observable.
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="codex"))
        fed.feed(json.dumps({"type": "turn.started"}), 0.0)
        fed.feed(_codex_error_item(1), 30.0)
        fed.feed(_codex_error_item(2), 50.0)
        idle = fed.watchdog.stalled_for(61.0)
        self.assertIsNotNone(idle)
        self.assertEqual(fed.watchdog.stall_detail(idle or 0.0)["stallReason"], "idle")

    def test_a_failed_file_change_whose_start_was_missed_is_not_progress(self):
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=60.0, harness="codex"))
        fed.feed(json.dumps({"type": "turn.started"}), 0.0)
        _started, completed = _codex_file_change(1, status="failed")
        fed.feed(completed, 50.0)
        self.assertIsNotNone(fed.watchdog.stalled_for(61.0))

    def test_a_successful_patch_still_resets(self):
        lines = [
            line
            for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT + 2)
            for line in [
                *_codex_fix_loop(index, False),
                *_codex_file_change(100 + index, status="completed"),
            ]
        ]
        self.assertEqual(self.run_lines(lines), "idle")


class FailureIdentityTests(unittest.TestCase):
    """Streaks compare whole targets, and a command counts in one streak only."""

    def feed_all(self, lines: list[str]) -> stall_watchdog.StallWatchdog:
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="codex"))
        for now, line in enumerate(lines, start=1):
            fed.feed(line, float(now))
        return fed.watchdog

    def test_distinct_long_failing_commands_do_not_trip(self):
        lines = [
            line
            for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT)
            for line in _codex_real_command(f"item_{index}", _long_command(index), "failed")
        ]
        detail = self.feed_all(lines).stall_detail(0.0)
        self.assertNotEqual(detail["stallReason"], "repeated_tool_failure")

    def test_identical_long_failing_commands_trip_once_through_the_command_streak(self):
        limit = stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT
        command = _long_command(0)
        lines = [
            line
            for index in range(limit)
            for line in _codex_real_command(f"item_{index}", command, "failed")
        ]
        watchdog = self.feed_all(lines)
        detail = watchdog.stall_detail(0.0)
        self.assertEqual(detail["stallReason"], "repeated_tool_failure")
        self.assertEqual(detail["tool"], "command_execution")
        self.assertEqual(detail["target"], command[: stall_watchdog.DELTA_SIGNATURE_LIMIT])
        self.assertEqual(detail["failures"], limit)
        self.assertEqual(watchdog._failure_count, limit)
        # The same five failures were not counted a second time elsewhere.
        self.assertEqual(watchdog._unmatched_failure_count, 0)

    def test_error_items_that_differ_past_the_limit_do_not_trip(self):
        lines = [
            _codex_error_item(index, "x" * 600 + f" failure {index}")
            for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT)
        ]
        detail = self.feed_all(lines).stall_detail(0.0)
        self.assertNotEqual(detail["stallReason"], "repeated_tool_failure")

    def test_failed_patches_to_long_distinct_paths_do_not_trip(self):
        lines = []
        for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT):
            path = "/repo/" + "d" * 520 + f"/file_{index}.py"
            item = {"id": f"item_{index}", "type": "file_change", "changes": [{"path": path}]}
            lines.append(
                json.dumps({"type": "item.started", "item": {**item, "status": "in_progress"}})
            )
            lines.append(
                json.dumps({"type": "item.completed", "item": {**item, "status": "failed"}})
            )
        detail = self.feed_all(lines).stall_detail(0.0)
        self.assertNotEqual(detail["stallReason"], "repeated_tool_failure")

    def test_failed_mcp_calls_with_long_distinct_arguments_do_not_trip(self):
        lines = []
        for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT):
            item = {
                "id": f"item_{index}",
                "type": "mcp_tool_call",
                "server": "docs",
                "tool": "search",
                "arguments": {"query": "q" * 520 + f" {index}"},
            }
            lines.append(
                json.dumps({"type": "item.started", "item": {**item, "status": "in_progress"}})
            )
            ended = {**item, "result": None, "error": {"message": "timeout"}, "status": "failed"}
            lines.append(json.dumps({"type": "item.completed", "item": ended}))
        detail = self.feed_all(lines).stall_detail(0.0)
        self.assertNotEqual(detail["stallReason"], "repeated_tool_failure")


class ErrorItemStreakTests(unittest.TestCase):
    """Error items count on their own, and the real hook-trust notice does not trip."""

    def feed_all(self, lines: list[str]) -> dict:
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="codex"))
        for now, line in enumerate(lines, start=1):
            fed.feed(line, float(now))
        return fed.watchdog.stall_detail(0.0)

    def test_identical_error_items_alone_trip(self):
        limit = stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT
        detail = self.feed_all([_codex_error_item(index) for index in range(limit)])
        self.assertEqual(detail["stallReason"], "repeated_tool_failure")
        self.assertEqual(detail["tool"], "error")
        self.assertEqual(detail["failures"], limit)

    def test_identical_error_items_between_distinct_failing_commands_trip(self):
        lines = [
            line
            for index in range(stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT)
            for line in [
                *_codex_real_command(f"c{index}", f"pytest tests/test_{index}.py", "failed"),
                _codex_error_item(100 + index),
            ]
        ]
        detail = self.feed_all(lines)
        self.assertEqual(detail["stallReason"], "repeated_tool_failure")
        self.assertEqual(detail["tool"], "error")

    def test_the_hook_trust_notice_twice_then_normal_work_does_not_trip(self):
        lines = [
            json.dumps({"type": "turn.started"}),
            _codex_error_item(0, HOOK_TRUST_NOTICE),
            _codex_error_item(1, HOOK_TRUST_NOTICE),
        ]
        for index in range(8):
            lines.extend(
                _codex_real_command(f"c{index}", f"sed -n '1,40p' f{index}.py", "completed")
            )
        lines.extend(_codex_real_command("c_fail", "pytest tests/test_x.py", "failed"))
        detail = self.feed_all(lines)
        self.assertNotIn("failures", detail)
        self.assertNotEqual(detail["stallReason"], "repeated_tool_failure")


class OpencodeRunningStatusTests(unittest.TestCase):
    """A tool that is still running has not completed (item G)."""

    def test_running_then_error_pairs_trip_on_the_fifth_error(self):
        fed = _FedWatchdog(stall_watchdog.StallWatchdog(stall_seconds=480.0, harness="opencode"))
        stamp = 1783614108246
        limit = stall_watchdog.REPEATED_TOOL_FAILURE_LIMIT
        for index in range(limit):
            self.assertIsNone(fed.watchdog.stall_detail(0.0).get("failures"))
            call_id = f"call_{index}"
            fed.feed(_opencode_tool_use(call_id, FIX_LOOP_COMMAND, "running", stamp), index * 2.0)
            fed.feed(
                _opencode_tool_use(call_id, FIX_LOOP_COMMAND, "error", stamp + 1), index * 2.0 + 1
            )
            stamp += 2
        detail = fed.watchdog.stall_detail(0.0)
        self.assertEqual(detail["stallReason"], "repeated_tool_failure")
        self.assertEqual(detail["failures"], limit)


if __name__ == "__main__":
    unittest.main()
