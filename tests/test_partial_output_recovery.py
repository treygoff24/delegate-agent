"""A failed, timed-out, or capped run keeps the last substantive assistant text.

A cancelled Run already quoted its partial output in the completion report; a
failed one dropped it, and pi/omp cleared it at every new turn, so a long review
followed by a tool turn and a provider error left nothing to recover. A call that
timed out returned only its error and dropped the draft it had already read.
"""

import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, errors, harness_events, runner
from tests.delegate_commands_test_base import CommandTestBase
from tests.tracked_capture_helpers import jsonl, load_state, read_report, run_tracked

REVIEW = (
    "Verdict: two defects found.\n"
    "- The retry loop never resets its counter, so the third timeout aborts the batch.\n"
    "- The cache key omits the tenant, so one tenant can read another's rows."
)
NOT_A_REPORT = "This is not a completion report"


def pi_events(*, final_stop="error", final_error="provider exploded", first_turn_text=REVIEW):
    """A review in turn 1, a tool turn 2, and an empty failing turn 3."""
    assistant = {"role": "assistant", "content": [{"type": "text", "text": first_turn_text}]}
    return [
        {"type": "turn_start"},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": first_turn_text},
        },
        {"type": "message_end", "message": assistant},
        {"type": "turn_end", "message": {**assistant, "stopReason": "toolUse"}},
        {"type": "turn_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "t1",
            "toolName": "read",
            "args": {"path": "a.py"},
        },
        {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "read"},
        {"type": "message_end", "message": {"role": "assistant", "content": []}},
        {
            "type": "turn_end",
            "message": {"role": "assistant", "content": [], "stopReason": "toolUse"},
        },
        {"type": "turn_start"},
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": final_stop,
                "errorMessage": final_error,
            },
        },
    ]


def chatter_turn(text):
    message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    return [
        {"type": "turn_start"},
        {"type": "message_end", "message": message},
        {"type": "turn_end", "message": {**message, "stopReason": "toolUse"}},
    ]


OPENCODE_STEP_START = {"type": "step_start", "part": {"type": "step-start"}}
OPENCODE_ERROR = {
    "type": "error",
    "error": {"name": "APIError", "data": {"message": "provider exploded"}},
}


def opencode_text(text):
    return {"type": "text", "part": {"type": "text", "text": text}}


def opencode_events(*second_step, first_step_text=REVIEW):
    """Step 1 writes the review and hands off to a tool; step 2 is whatever follows."""
    return [
        OPENCODE_STEP_START,
        opencode_text(first_step_text),
        {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls"}},
        OPENCODE_STEP_START,
        *second_step,
    ]


class AccumulatorRetentionTests(unittest.TestCase):
    def feed(self, harness, events):
        acc = harness_events.StreamAccumulator(harness=harness)
        for event in events:
            acc.ingest_line(json.dumps(event))
        return acc

    def test_pi_and_omp_keep_the_last_substantive_text_across_later_turns(self):
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness):
                events = pi_events()
                acc = self.feed(harness, events[:4])
                self.assertEqual(acc.recoverable_assistant_text, REVIEW)
                # Turn 2 starts: the published text resets, the recoverable text stays.
                acc = self.feed(harness, events)
                self.assertEqual(acc.terminal_status, "failed")
                self.assertEqual(acc.recoverable_assistant_text, REVIEW)
                self.assertEqual(acc.completion_text, None)

    def test_a_short_progress_note_survives_later_turns_too(self):
        note = "Looking into it."
        self.assertFalse(harness_events.is_substantive_assistant_text(note))
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness):
                acc = self.feed(harness, pi_events(first_turn_text=note))
                self.assertEqual(acc.terminal_status, "failed")
                self.assertEqual(acc.recoverable_assistant_text, note)

    def test_later_chatter_does_not_displace_the_substantive_review(self):
        chatter = "Running the tests now."
        self.assertFalse(harness_events.is_substantive_assistant_text(chatter))
        events = [*pi_events()[:4], *chatter_turn(chatter)]
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness):
                acc = self.feed(harness, events)
                # The current turn's text is the chatter; the recoverable text is
                # still the review.
                self.assertEqual(acc.assistant_text, chatter)
                self.assertEqual(acc.recoverable_assistant_text, REVIEW)

    def test_a_later_substantive_turn_replaces_the_earlier_one(self):
        later = "Verdict: revised.\n- one\n- two"
        events = [
            *pi_events()[:-2],
            {"type": "turn_start"},
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": later}],
                },
            },
        ]
        acc = self.feed("omp", events)
        self.assertEqual(acc.recoverable_assistant_text, later)
        self.assertEqual(acc.completion_text, later)

    def test_opencode_keeps_the_step_one_review_when_step_two_only_errors(self):
        acc = self.feed("opencode", opencode_events(OPENCODE_ERROR))
        self.assertEqual(acc.terminal_status, "failed")
        # Step 2 published nothing, and step 1's prose is not the current answer...
        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.completion_text)
        # ...but it is still the last substantive thing the child said.
        self.assertEqual(acc.recoverable_assistant_text, REVIEW)
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")

    def test_opencode_keeps_the_step_one_review_when_step_two_is_only_chatter(self):
        chatter = "Running the tests now."
        self.assertFalse(harness_events.is_substantive_assistant_text(chatter))
        acc = self.feed("opencode", opencode_events(opencode_text(chatter)))
        self.assertEqual(acc.assistant_text, chatter)
        self.assertEqual(acc.recoverable_assistant_text, REVIEW)

    def test_opencode_a_short_progress_note_survives_later_steps_too(self):
        note = "Looking into it."
        self.assertFalse(harness_events.is_substantive_assistant_text(note))
        acc = self.feed("opencode", opencode_events(OPENCODE_ERROR, first_step_text=note))
        self.assertEqual(acc.terminal_status, "failed")
        self.assertEqual(acc.recoverable_assistant_text, note)

    def test_opencode_a_later_substantive_step_replaces_the_earlier_review(self):
        later = "Verdict: revised.\n- one\n- two"
        acc = self.feed("opencode", opencode_events(opencode_text(later), OPENCODE_ERROR))
        self.assertEqual(acc.recoverable_assistant_text, later)
        self.assertNotIn("retry loop", acc.recoverable_assistant_text)

    def test_bound_assistant_text_keeps_head_and_tail_and_names_the_cut(self):
        short = "short"
        self.assertEqual(harness_events.bound_assistant_text(short), short)
        long_text = "H" * 30_000 + "M" * 50_000 + "T" * 30_000
        bounded = harness_events.bound_assistant_text(long_text)
        self.assertLess(len(bounded), 31_000)
        self.assertTrue(bounded.startswith("H" * harness_events.ASSISTANT_TEXT_HEAD))
        self.assertTrue(bounded.endswith("T" * harness_events.ASSISTANT_TEXT_TAIL))
        self.assertIn(
            f"[{len(long_text) - harness_events.ASSISTANT_TEXT_HEAD - harness_events.ASSISTANT_TEXT_TAIL}"
            " chars omitted]",
            bounded,
        )


class FailedReportKeepsPartialOutputTests(unittest.TestCase):
    def assert_partial_in_report(self, report, expected=REVIEW):
        self.assertIn("Partial output recovered", report)
        self.assertIn(NOT_A_REPORT, report)
        self.assertIn(expected, report)
        # The quoted text sits in a fenced block, after the failure explanation.
        self.assertLess(report.index("Partial output recovered"), report.index(expected))

    def test_omp_provider_error_after_a_tool_turn_keeps_the_earlier_review(self):
        script = "import os\n" + jsonl(*pi_events())
        with tempfile.TemporaryDirectory() as temp:
            code, payload, _run, ctx = run_tracked(Path(temp), script, harness="omp")
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"], "provider_error")
            self.assertEqual(load_state(ctx)["status"], "failed")
            self.assert_partial_in_report(read_report(ctx))

    def test_timed_out_omp_run_keeps_what_it_had_said(self):
        events = pi_events()[:4]
        script = "import os, time\n" + jsonl(*events) + "time.sleep(60)\n"
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                run_tracked(Path(temp), script, harness="omp", timeout=2)
            self.assertIn("timeout", caught.exception.message)
            run_dir = next((Path(temp) / ".delegate" / "runs").iterdir())
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assert_partial_in_report((run_dir / "completion-report.md").read_text("utf-8"))

    def test_opencode_error_in_a_later_step_keeps_the_step_one_review(self):
        script = "import os, sys\n" + jsonl(*opencode_events(OPENCODE_ERROR)) + "sys.exit(1)\n"
        with tempfile.TemporaryDirectory() as temp:
            code, _payload, _run, ctx = run_tracked(Path(temp), script, harness="opencode")
            self.assertEqual(code, 1)
            self.assertEqual(load_state(ctx)["status"], "failed")
            self.assert_partial_in_report(read_report(ctx))

    def test_timed_out_opencode_run_keeps_the_step_one_review(self):
        script = "import os, time\n" + jsonl(*opencode_events()) + "time.sleep(60)\n"
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                run_tracked(Path(temp), script, harness="opencode", timeout=2)
            self.assertIn("timeout", caught.exception.message)
            run_dir = next((Path(temp) / ".delegate" / "runs").iterdir())
            self.assert_partial_in_report((run_dir / "completion-report.md").read_text("utf-8"))

    def test_claude_error_result_after_assistant_text_keeps_the_draft(self):
        script = (
            "import os, sys\n"
            + jsonl(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": REVIEW}],
                    },
                },
                {"type": "result", "subtype": "error_max_turns", "is_error": True},
            )
            + "sys.exit(1)\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            code, _payload, _run, ctx = run_tracked(Path(temp), script, harness="claude")
            self.assertEqual(code, 1)
            self.assertEqual(load_state(ctx)["status"], "failed")
            self.assert_partial_in_report(read_report(ctx))

    def test_an_opted_in_cap_keeps_the_draft_written_before_the_flood(self):
        script = (
            "import os\n"
            + jsonl(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": REVIEW},
                }
            )
            + "line = b'x' * 4095 + b'\\n'\n"
            + "for _ in range(64): os.write(1, line)\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                run_tracked(Path(temp), script, harness="codex", tracked_stream_max_bytes=16 * 1024)
            self.assertEqual(caught.exception.error, "output_limit_exceeded")
            report = next(
                path.read_text(encoding="utf-8")
                for path in (Path(temp) / ".delegate" / "runs").glob("*/completion-report.md")
            )
            self.assert_partial_in_report(report)

    def test_an_enormous_draft_is_bounded_in_the_report(self):
        huge = "Verdict: long.\n" + "- finding " * 20_000
        assistant = {"role": "assistant", "content": [{"type": "text", "text": huge}]}
        events = [
            {"type": "turn_start"},
            {"type": "message_end", "message": assistant},
            {
                "type": "turn_end",
                "message": {**assistant, "stopReason": "error", "errorMessage": "boom"},
            },
        ]
        script = "import os\n" + jsonl(*events)
        with tempfile.TemporaryDirectory() as temp:
            code, _payload, _run, ctx = run_tracked(Path(temp), script, harness="omp")
            self.assertEqual(code, 1)
            report = read_report(ctx)
        self.assertIn("Partial output recovered", report)
        self.assertIn("chars omitted]", report)
        self.assertLess(len(report), 40_000)

    def test_a_failure_with_no_assistant_text_adds_no_empty_quote_block(self):
        events = [{"type": "turn_start"}, pi_events()[-1]]
        script = "import os\n" + jsonl(*events)
        with tempfile.TemporaryDirectory() as temp:
            code, _payload, _run, ctx = run_tracked(Path(temp), script, harness="omp")
            self.assertEqual(code, 1)
            self.assertNotIn("Partial output recovered", read_report(ctx))


class CallModeTimeoutKeepsPartialOutputTests(CommandTestBase):
    def omp_stream_script(self, *, sleep=60, events=None):
        lines = "".join(json.dumps(event) + "\n" for event in events or pi_events()[:4])
        return (
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            f"sys.stdout.write({lines!r})\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('warming up\\n')\n"
            "sys.stderr.flush()\n"
            f"time.sleep({sleep})\n"
        )

    def test_runner_call_timeout_returns_the_draft_it_read(self):
        # A later chatter turn follows the review: the review stays the partial text.
        events = [*pi_events()[:4], *chatter_turn("Running the tests now.")]
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "child.py"
            script.write_text(self.omp_stream_script(events=events), encoding="utf-8")
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                runner.execute_call([sys.executable, str(script)], temp, harness="omp", timeout=2)
        self.assertEqual(caught.exception.error, "call_timeout")
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["partialText"], REVIEW)
        self.assertEqual(diagnostics["partialTextChars"], len(REVIEW))
        self.assertIn("turn_end", diagnostics["stdoutTail"])
        self.assertIn("warming up", diagnostics["stderrTail"])

    def test_partial_output_is_redacted_and_bounded(self):
        secret = "sk-ant-api03-" + "A" * 40
        body = "Verdict: leak check\n- key " + secret + "\n- " + "w" * 60_000
        event = {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": body}]},
        }
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "child.py"
            script.write_text(
                "import sys, time\n"
                f"sys.stdout.write({json.dumps(event) + chr(10)!r})\n"
                "sys.stdout.flush()\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                runner.execute_call(
                    [sys.executable, str(script)],
                    temp,
                    harness="omp",
                    timeout=2,
                    sensitive_texts=("w" * 10,),
                )
        diagnostics = caught.exception.diagnostics
        self.assertNotIn(secret, json.dumps(diagnostics))
        # A caller-supplied sensitive text (the prompt, say) is scrubbed too.
        self.assertNotIn("w" * 10, json.dumps(diagnostics))
        self.assertIn("[REDACTED]", diagnostics["partialText"])
        self.assertLess(len(diagnostics["partialText"]), 31_000)
        self.assertLessEqual(len(diagnostics["stdoutTail"]), runner.CALL_PARTIAL_STDOUT_TAIL_CHARS)

    def test_unstructured_stdout_is_returned_as_the_partial_text(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "child.py"
            script.write_text(
                "import sys, time\n"
                "sys.stdout.write('half an answer\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                runner.execute_call([sys.executable, str(script)], temp, harness="codex", timeout=2)
        self.assertEqual(caught.exception.diagnostics["partialText"], "half an answer")

    def test_a_call_that_never_spoke_carries_no_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "child.py"
            script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                runner.execute_call([sys.executable, str(script)], temp, harness="omp", timeout=1)
        self.assertEqual(caught.exception.error, "call_timeout")
        self.assertEqual(caught.exception.diagnostics, {})

    def test_cli_json_error_carries_the_partial_text(self):
        binary_dir = self.write_fake_python_omp()
        code, stdout, _stderr = self.run_main(
            ["--json", "omp", "call", "--timeout", "2", "review it"], path_prefix=binary_dir
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "call_timeout")
        self.assertEqual(payload["diagnostics"]["partialText"], REVIEW)
        self.assertEqual(payload["partialText"], REVIEW)

    def test_cli_text_error_prints_the_partial_text(self):
        binary_dir = self.write_fake_python_omp()
        code, stdout, stderr = self.run_main(
            ["omp", "call", "--timeout", "2", "review it"], path_prefix=binary_dir
        )
        self.assertEqual(code, 1)
        self.assertNotIn(REVIEW, stdout)
        self.assertIn("call_timeout", stderr)
        self.assertIn("Partial output recovered before the call stopped:", stderr)
        self.assertIn(REVIEW, stderr)

    def write_fake_python_omp(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "omp"
        path.write_text(self.omp_stream_script(), encoding="utf-8")
        path.chmod(0o755)
        return Path(temp.name)

    def test_emit_error_without_diagnostics_is_unchanged(self):
        err = io.StringIO()
        code = cli.emit_error(
            errors.DelegateError("call_timeout", "too slow", 1), False, io.StringIO(), err
        )
        self.assertEqual(code, 1)
        self.assertNotIn("Partial output", err.getvalue())


class CallModeFastOverflowKeepsPartialOutputTests(unittest.TestCase):
    """The child finishes before the drains read its output, so overflow is found late.

    The leader's exit and the drains' first read race in production. Holding the
    stdout pipe until the runner has seen the leader exit makes the late branch
    the one that runs, with a real child writing real bytes.
    """

    CAP = 2048

    def flood_script(self):
        lines = "".join(json.dumps(event) + "\n" for event in pi_events()[:4])
        return (
            "import sys\n"
            f"sys.stdout.write({lines!r})\n"
            "sys.stdout.write(('x' * 99 + '\\n') * 40)\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('warming up\\n')\n"
            "sys.stderr.flush()\n"
        )

    def hold_stdout_until_leader_exit(self):
        released = threading.Event()
        real_popen = subprocess.Popen
        real_terminate = runner._terminate_call_process

        class HeldPipe:
            def __init__(self, pipe):
                self._pipe = pipe

            def read(self, size=-1):
                released.wait(timeout=10)
                return self._pipe.read(size)

            def close(self):
                self._pipe.close()

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            process.stdout = HeldPipe(process.stdout)
            return process

        def terminate(*args, **kwargs):
            released.set()
            return real_terminate(*args, **kwargs)

        return (
            mock.patch.object(runner.subprocess, "Popen", side_effect=popen),
            mock.patch.object(runner, "_terminate_call_process", side_effect=terminate),
            mock.patch.object(runner, "CALL_STDOUT_MAX_BYTES", self.CAP),
        )

    def run_call(self, temp, **kwargs):
        script = Path(temp) / "child.py"
        script.write_text(self.flood_script(), encoding="utf-8")
        popen, terminate, cap = self.hold_stdout_until_leader_exit()
        with popen, terminate, cap:
            return runner.execute_call(
                [sys.executable, str(script)], temp, harness="omp", timeout=30, **kwargs
            )

    def test_a_fast_child_that_overflows_still_returns_its_buffers(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "child.py"
            script.write_text(self.flood_script(), encoding="utf-8")
            popen, terminate, cap = self.hold_stdout_until_leader_exit()
            with popen, terminate, cap:
                process = subprocess.Popen(
                    [sys.executable, str(script)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                with self.assertRaises(runner.RunnerLaunchError) as caught:
                    runner._bounded_call_communicate(process, None, 30, self.CAP, 4096)
        self.assertEqual(caught.exception.error, "call_stdout_overflow")
        self.assertEqual(len(caught.exception.partial_stdout), self.CAP)
        self.assertIn(REVIEW.split("\n")[0].encode(), caught.exception.partial_stdout)
        self.assertEqual(caught.exception.partial_stderr, b"warming up\n")

    def test_execute_call_reports_the_draft_after_a_fast_overflow(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaises(runner.RunnerLaunchError) as caught,
        ):
            self.run_call(temp)
        self.assertEqual(caught.exception.error, "call_stdout_overflow")
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["partialText"], REVIEW)
        self.assertIn("warming up", diagnostics["stderrTail"])
        self.assertLessEqual(len(diagnostics["stdoutTail"]), runner.CALL_PARTIAL_STDOUT_TAIL_CHARS)

    def test_the_fast_overflow_diagnostics_are_redacted(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaises(runner.RunnerLaunchError) as caught,
        ):
            self.run_call(temp, sensitive_texts=("retry loop",))
        rendered = json.dumps(caught.exception.diagnostics)
        self.assertNotIn("retry loop", rendered)
        self.assertIn("[REDACTED]", caught.exception.diagnostics["partialText"])


if __name__ == "__main__":
    unittest.main()
