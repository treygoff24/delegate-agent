"""Devin's running answer block stays bounded in memory however long the run is.

Devin writes its answer as plain lines with no message boundaries, so the
accumulator merges every line into one block. That block used to be rebuilt as a
new string on every line: quadratic copying, and with the output cap off, the
whole run held in memory. The block now keeps the head and tail that
`bound_assistant_text` keeps; `stdout.log` still has the full stream.
"""

import string
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import harness_events, stall_watchdog
from tests.tracked_capture_helpers import load_state, read_report, run_tracked

LIMIT = harness_events.ASSISTANT_TEXT_LIMIT
HEAD = harness_events.ASSISTANT_TEXT_HEAD
TAIL = harness_events.ASSISTANT_TEXT_TAIL
# The marker that `bound_assistant_text` inserts is a few dozen characters.
MARKER_ROOM = 64


def text_of_length(length, *, width=100):
    """`length` characters in lines of at most `width`, no empty lines, no edge spaces.

    Characters cycle through the alphabet by index, so a slice that is off by one
    at the head/tail seam changes the text and does not go unnoticed.
    """
    letters = string.ascii_letters
    chars = [letters[i % len(letters)] for i in range(length)]
    for index in range(width - 1, length - 1, width):
        chars[index] = "\n"
    return "".join(chars)


def feed(text, *, harness="devin"):
    acc = harness_events.StreamAccumulator(harness=harness)
    for line in text.split("\n"):
        acc.ingest_line(line)
    return acc


class DevinBlockBoundTests(unittest.TestCase):
    def test_a_million_characters_of_lines_stay_within_the_budget(self):
        lines = [f"line {i:05d} " + "x" * 40 for i in range(20_000)]
        full = "\n".join(lines)
        self.assertGreater(len(full), 1_000_000)
        acc = harness_events.StreamAccumulator(harness="devin")
        for index, line in enumerate(lines):
            acc.ingest_line(line)
            if index % 5_000 == 4_999:
                self.assertEqual(len(acc.assistant_chunks), 1)
                self.assertLess(len(acc.assistant_chunks[0]), LIMIT + MARKER_ROOM)
        # Nothing the accumulator keeps grows with the run.
        self.assertEqual(len(acc.assistant_chunks), 1)
        self.assertLess(len(acc.assistant_chunks[0]), LIMIT + MARKER_ROOM)
        self.assertLessEqual(len(acc._devin_block.head), HEAD)
        self.assertLessEqual(len(acc._devin_block.tail), 2 * TAIL + 100)
        self.assertLess(len(acc.recoverable_assistant_text), LIMIT + MARKER_ROOM)
        # What is kept is right: the same head and tail bound_assistant_text keeps.
        expected = harness_events.bound_assistant_text(full)
        self.assertEqual(acc.assistant_text, expected)
        self.assertEqual(acc.recoverable_assistant_text, expected)
        self.assertTrue(acc.assistant_text.startswith("line 00000 "))
        self.assertTrue(acc.assistant_text.endswith(lines[-1]))
        bounded, meta = acc.bounded_assistant_text()
        self.assertEqual(bounded, expected)
        self.assertEqual(meta["assistantTextChars"], len(full))
        self.assertTrue(meta["assistantTextTruncated"])
        self.assertEqual(meta["assistantTextOmittedMiddleChars"], len(full) - HEAD - TAIL)
        self.assertIn(f"[{len(full) - HEAD - TAIL} chars omitted]", bounded)

    def test_the_bounded_block_matches_bound_assistant_text_around_the_limit(self):
        for length in (1, 99, 100, 101, HEAD, LIMIT - 1, LIMIT, LIMIT + 1, 40_000, 40_001, 50_000):
            for width in (7, 100, 9_999):
                with self.subTest(length=length, width=width):
                    full = text_of_length(length, width=width)
                    acc = feed(full)
                    expected = harness_events.bound_assistant_text(full)
                    self.assertEqual(acc.assistant_text, expected)
                    self.assertEqual(acc.recoverable_assistant_text, expected)
                    bounded, meta = acc.bounded_assistant_text()
                    self.assertEqual(bounded, expected)
                    self.assertEqual(meta["assistantTextChars"], length)
                    self.assertEqual(meta["assistantTextTruncated"], length > LIMIT)

    def test_one_line_longer_than_the_whole_budget_is_bounded(self):
        line = "".join(string.ascii_letters[i % 52] for i in range(45_000))
        acc = feed(line)
        self.assertEqual(acc.assistant_text, harness_events.bound_assistant_text(line))
        self.assertLess(len(acc.assistant_chunks[0]), LIMIT + MARKER_ROOM)

    def test_short_output_is_unchanged(self):
        acc = feed("line one\nline two\nline three")
        self.assertEqual(acc.assistant_text, "line one\nline two\nline three")
        bounded, meta = acc.bounded_assistant_text()
        self.assertEqual(bounded, "line one\nline two\nline three")
        self.assertFalse(meta["assistantTextTruncated"])
        self.assertEqual(meta["assistantTextOmittedMiddleChars"], 0)

    def test_a_long_run_of_chatter_is_still_recovered_as_substantive(self):
        acc = feed("\n".join(f"step {i}" for i in range(8_000)))
        self.assertGreater(acc._devin_block.total, LIMIT)
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")
        self.assertTrue(
            harness_events.is_substantive_assistant_text(acc.recoverable_assistant_text)
        )
        self.assertTrue(acc.recoverable_assistant_text.endswith("step 7999"))

    def test_lines_past_the_budget_do_not_rescan_the_block(self):
        calls = []
        real = harness_events.is_substantive_assistant_text

        def counting(text):
            calls.append(len(text))
            return real(text)

        acc = harness_events.StreamAccumulator(harness="devin")
        with mock.patch.object(harness_events, "is_substantive_assistant_text", counting):
            index = 0
            while acc._devin_block is None or acc._devin_block.total <= LIMIT:
                acc.ingest_line(f"warm up {index}")
                index += 1
            seen = len(calls)
            for extra in range(2_000):
                acc.ingest_line(f"past the budget {extra}")
        self.assertGreater(seen, 0)
        self.assertEqual(len(calls), seen)

    def test_a_short_progress_note_is_still_housekeeping(self):
        acc = feed("Running the tests now.")
        self.assertEqual(acc.assistant_recovery_quality(), "housekeeping_fallback")

    def test_the_watchdog_still_reads_the_completion_status_from_the_tail(self):
        report = "Status: completed\nSummary: fixed both."
        acc = feed("\n".join([*(f"working on item {i}" for i in range(15_000)), report]))
        self.assertGreater(acc._devin_block.total, LIMIT)
        self.assertEqual(stall_watchdog.completion_report_status(acc.assistant_text), "completed")

    def test_text_appended_by_another_path_is_not_folded_into_a_stale_block(self):
        acc = harness_events.StreamAccumulator(harness="devin")
        acc.ingest_line("first line")
        acc.assistant_chunks.append("a chunk some other path added")
        acc.ingest_line("second line")
        # Lines keep merging into whatever chunk is last, as they always did.
        self.assertEqual(
            acc.assistant_chunks, ["first line", "a chunk some other path added\nsecond line"]
        )
        self.assertEqual(
            acc.assistant_text, "first line\n\na chunk some other path added\nsecond line"
        )


class DevinTrackedRunTests(unittest.TestCase):
    def test_a_tracked_devin_run_keeps_the_full_stream_and_a_bounded_report(self):
        script = (
            "import os, sys\n"
            "for i in range(4000):\n"
            "    os.write(1, ('progress line %05d ' % i + 'x' * 30 + '\\n').encode())\n"
            "os.write(1, b'Status: completed\\nSummary: all done.\\n')\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            code, _payload, run_dir, ctx = run_tracked(Path(temp), script, harness="devin")
            self.assertEqual(code, 0)
            self.assertEqual(load_state(ctx)["status"], "succeeded")
            report = read_report(ctx)
            log = (run_dir / "stdout.log").read_text(encoding="utf-8")
        # The cap is off: the full stream is on disk, first line to last.
        self.assertIn("progress line 00000", log)
        self.assertIn("progress line 02000", log)
        self.assertIn("progress line 03999", log)
        self.assertIn("Status: completed", log)
        # The report is the bounded excerpt: head, a marker, the tail with the status.
        self.assertIn("chars omitted]", report)
        self.assertIn("progress line 00000", report)
        self.assertNotIn("progress line 02000", report)
        self.assertTrue(report.rstrip().endswith("Summary: all done."))
        self.assertLess(len(report), LIMIT + 5_000)


if __name__ == "__main__":
    unittest.main()
