"""Token usage for pi, omp and opencode comes from the stream.

These harnesses report usage on every message or step, not once at the end, so
the accumulator sums it. The shapes are the real ones: `tests/fixtures/pi` and
`tests/fixtures/opencode` are captured streams, and the omp record mirrors a
`turn_end` from a local run (omp adds `reasoningTokens`, and `toolResults` beside
`message`).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from delegate_agent import harness_events, runner
from tests.tracked_capture_helpers import load_state, run_tracked

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_lines(*parts: str) -> list[str]:
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8").splitlines()


def ingest(harness: str, lines) -> harness_events.StreamAccumulator:
    acc = harness_events.StreamAccumulator(harness=harness)
    for line in lines:
        acc.ingest_line(line if isinstance(line, str) else json.dumps(line))
    return acc


def omp_message(*, timestamp, input_tokens, output_tokens, cache_read=0, cost=0, stop="toolUse"):
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": "step"}],
        "api": "openai-responses",
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read,
            "cacheWrite": 0,
            "totalTokens": input_tokens + output_tokens + cache_read,
            "reasoningTokens": 273,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost},
        },
        "stopReason": stop,
        "timestamp": timestamp,
    }


class PiFamilyUsageTests(unittest.TestCase):
    def test_pi_fixture_sums_both_turns_once_each(self):
        acc = ingest("pi", fixture_lines("pi", "tool_read.jsonl"))
        # Two assistant turns, each reported on message_end and again on turn_end.
        self.assertEqual(acc.usage["basis"], "reported")
        self.assertEqual(acc.usage["inputTokens"], 6846 + 226)
        self.assertEqual(acc.usage["outputTokens"], 18 + 12)
        self.assertEqual(acc.usage["cacheReadTokens"], 6656)
        self.assertEqual(acc.usage["cacheWriteTokens"], 0)
        self.assertAlmostEqual(acc.usage["costUsd"], 0.03477 + 0.004818)

    def test_omp_turn_end_alone_and_message_end_pair_count_the_same(self):
        message = omp_message(timestamp=1788473450430, input_tokens=53744, output_tokens=471)
        turn_only = ingest("omp", [{"type": "turn_end", "message": message, "toolResults": []}])
        pair = ingest(
            "omp",
            [
                {"type": "message_end", "message": message},
                {"type": "turn_end", "message": message, "toolResults": []},
            ],
        )
        for acc in (turn_only, pair):
            self.assertEqual(acc.usage["inputTokens"], 53744)
            self.assertEqual(acc.usage["outputTokens"], 471)
            self.assertNotIn("costUsd", acc.usage)  # provider reports a zero cost

    def test_omp_turns_with_identical_counters_still_add_up(self):
        first = omp_message(timestamp=1, input_tokens=1000, output_tokens=10, cache_read=500)
        second = omp_message(timestamp=2, input_tokens=1000, output_tokens=10, cache_read=500)
        acc = ingest(
            "omp",
            [
                {"type": "message_end", "message": first},
                {"type": "turn_end", "message": first},
                {"type": "message_end", "message": second},
                {"type": "turn_end", "message": second},
            ],
        )
        self.assertEqual(acc.usage["inputTokens"], 2000)
        self.assertEqual(acc.usage["outputTokens"], 20)
        self.assertEqual(acc.usage["cacheReadTokens"], 1000)

    def test_a_turn_end_for_a_different_message_is_not_mistaken_for_the_duplicate(self):
        first = omp_message(timestamp=1, input_tokens=400, output_tokens=5)
        second = omp_message(timestamp=2, input_tokens=400, output_tokens=5)
        acc = ingest(
            "omp",
            [
                # The first message's turn_end never arrives; the second turn_end
                # has the same counters but is a different message.
                {"type": "message_end", "message": first},
                {"type": "turn_end", "message": second},
            ],
        )
        self.assertEqual(acc.usage["inputTokens"], 800)

    def test_usage_is_available_while_the_run_is_still_going(self):
        message = omp_message(timestamp=1, input_tokens=300, output_tokens=7)
        acc = ingest("omp", [{"type": "message_end", "message": message}])
        self.assertEqual(acc.usage["inputTokens"], 300)

    def test_reported_cost_accumulates(self):
        first = omp_message(timestamp=1, input_tokens=10, output_tokens=1, cost=0.25)
        second = omp_message(timestamp=2, input_tokens=10, output_tokens=1, cost=0.5)
        acc = ingest(
            "omp",
            [
                {"type": "turn_end", "message": first},
                {"type": "turn_end", "message": second},
            ],
        )
        self.assertAlmostEqual(acc.usage["costUsd"], 0.75)

    def test_all_zero_or_missing_usage_is_not_reported(self):
        zero = omp_message(timestamp=1, input_tokens=0, output_tokens=0)
        missing = {**omp_message(timestamp=2, input_tokens=5, output_tokens=5)}
        del missing["usage"]
        # A block with a cost but no token counters is not a usage report either.
        cost_only = {**zero, "usage": {"cost": {"total": 0.5}}}
        for message in (zero, missing, {**zero, "usage": {}}, cost_only):
            with self.subTest(message=message.get("usage")):
                acc = ingest("omp", [{"type": "turn_end", "message": message}])
                self.assertIsNone(acc.usage)

    def test_a_failed_turn_still_reports_what_it_spent(self):
        spent = omp_message(timestamp=1, input_tokens=900, output_tokens=40, stop="error")
        acc = ingest("omp", [{"type": "turn_end", "message": {**spent, "errorMessage": "boom"}}])
        self.assertEqual(acc.terminal_status, "failed")
        self.assertEqual(acc.usage["inputTokens"], 900)


class OpencodeUsageTests(unittest.TestCase):
    def test_every_step_is_added_not_only_the_closing_stop_step(self):
        acc = ingest("opencode", fixture_lines("opencode", "tool_run.ndjson"))
        # Step 1 ended with reason tool-calls, step 2 with stop.
        self.assertEqual(acc.usage["basis"], "reported")
        self.assertEqual(acc.usage["inputTokens"], 17339 + 188)
        self.assertEqual(acc.usage["outputTokens"], 73 + 48)
        self.assertEqual(acc.usage["cacheReadTokens"], 17536)
        self.assertEqual(acc.usage["cacheWriteTokens"], 0)
        self.assertAlmostEqual(acc.usage["costUsd"], 0.00099855 + 0.00026988)
        self.assertEqual(acc.terminal_status, "succeeded")
        # The degenerate case of the same summing path: a single step.
        with self.subTest(fixture="simple_text.ndjson"):
            single = ingest("opencode", fixture_lines("opencode", "simple_text.ndjson"))
            self.assertEqual(single.usage["inputTokens"], 17324)
            self.assertEqual(single.usage["outputTokens"], 62)
            self.assertEqual(single.usage["cacheReadTokens"], 0)

    def test_step_finish_without_tokens_leaves_usage_unset(self):
        step = {
            "type": "step_finish",
            "part": {"type": "step-finish", "reason": "stop", "tokens": {"total": 0}},
        }
        self.assertIsNone(ingest("opencode", [step]).usage)


class TrackedRunUsageTests(unittest.TestCase):
    def test_pi_run_state_carries_stream_usage(self):
        path = FIXTURES / "pi" / "tool_read.jsonl"
        script = f"import sys\nsys.stdout.write(open({str(path)!r}, encoding='utf-8').read())\n"
        with tempfile.TemporaryDirectory() as temp:
            code, payload, _run, ctx = run_tracked(Path(temp), script, harness="pi")
            state = load_state(ctx)
        self.assertEqual(code, 0)
        self.assertEqual(state["usage"]["inputTokens"], 6846 + 226)
        self.assertEqual(payload["usage"]["outputTokens"], 30)

    def test_call_mode_returns_stream_usage_instead_of_unavailable(self):
        for harness, fixture, expected_input in (
            ("pi", FIXTURES / "pi" / "tool_read.jsonl", 6846 + 226),
            ("opencode", FIXTURES / "opencode" / "tool_run.ndjson", 17339 + 188),
        ):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as temp:
                script = f"import sys\nsys.stdout.write(open({str(fixture)!r}, encoding='utf-8').read())\n"
                result = runner.execute_call(
                    [sys.executable, "-c", script], temp, harness=harness, timeout=30
                )
                self.assertEqual(result.usage["basis"], "reported")
                self.assertEqual(result.usage["inputTokens"], expected_input)

    def test_opencode_run_state_carries_stream_usage(self):
        path = FIXTURES / "opencode" / "tool_run.ndjson"
        script = f"import sys\nsys.stdout.write(open({str(path)!r}, encoding='utf-8').read())\n"
        with tempfile.TemporaryDirectory() as temp:
            _code, _payload, _run, ctx = run_tracked(Path(temp), script, harness="opencode")
            state = load_state(ctx)
        self.assertEqual(state["usage"]["cacheReadTokens"], 17536)


if __name__ == "__main__":
    unittest.main()
