"""The tracked output cap is off by default (Trey, 2026-09-28) and opt-in.

Each test here fails against the previous behavior: a default 16 MiB cap (64 MiB
for pi and omp) that killed finished lanes, a raw log that lagged the child, and
an ``events.jsonl`` marker that read as the child stalling.
"""

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry, runner, stream_capture
from tests.tracked_capture_helpers import (
    jsonl,
    load_state,
    make_context,
    read_report,
    run_tracked,
)

MIB = 1024 * 1024
JUNK_LINE = b"x" * 4095 + b"\n"
CODEX_FINISH = jsonl(
    {"type": "item.completed", "item": {"type": "agent_message", "text": "done after the flood"}},
    {"type": "turn.completed"},
)


def flood_script(total_bytes: int, tail: str = CODEX_FINISH) -> str:
    return (
        "import os\n"
        f"line = {JUNK_LINE!r}\n"
        f"for _ in range({total_bytes // len(JUNK_LINE) + 1}): os.write(1, line)\n"
        f"{tail}"
    )


class UncappedCaptureTests(unittest.TestCase):
    """``BoundedCapture(max_bytes=None)``: nothing the child prints can fail it."""

    def capture(self, max_bytes, *, compact_omp=True):
        sink = io.BytesIO()
        return sink, stream_capture.BoundedCapture(sink.write, max_bytes, compact_omp=compact_omp)

    def test_no_cap_retains_every_byte_past_a_small_limit(self):
        sink, capture = self.capture(None, compact_omp=False)
        data = JUNK_LINE * 64
        capture.feed(data)
        capture.finish()
        self.assertEqual(sink.getvalue(), data)
        self.assertIsNone(capture.stats.limit_kind)

    def test_no_cap_lifts_the_omp_transport_and_record_ceilings(self):
        record = b"y" * 2048 + b"\n"
        with (
            mock.patch.object(stream_capture, "OMP_TRANSPORT_MAX_BYTES", 4096),
            mock.patch.object(stream_capture, "OMP_RECORD_MAX_BYTES", 1024),
        ):
            sink, capture = self.capture(None)
            for _ in range(8):
                capture.feed(record)
            capture.finish()
            payload = capture.stats.payload(None)
        self.assertEqual(sink.getvalue(), record * 8)
        self.assertIsNone(capture.stats.limit_kind)
        self.assertIsNone(payload["retainedLimitBytes"])
        self.assertIsNone(payload["transportLimitBytes"])
        self.assertIsNone(payload["recordLimitBytes"])

    def test_an_unterminated_overlong_fragment_is_kept_not_fatal(self):
        with mock.patch.object(stream_capture, "OMP_RECORD_MAX_BYTES", 1024):
            sink, capture = self.capture(None)
            data = b"z" * 5000
            capture.feed(data)
            capture.finish()
        self.assertEqual(sink.getvalue(), data)

    def test_a_configured_cap_still_fails_each_way(self):
        with mock.patch.object(stream_capture, "OMP_TRANSPORT_MAX_BYTES", 4096):
            _sink, capture = self.capture(1 << 30)
            with self.assertRaises(stream_capture.CaptureLimit) as error:
                for _ in range(8):
                    capture.feed(b"y" * 1023 + b"\n")
            self.assertEqual(error.exception.kind, "transport")
        with mock.patch.object(stream_capture, "OMP_RECORD_MAX_BYTES", 1024):
            _sink, capture = self.capture(1 << 30)
            with self.assertRaises(stream_capture.CaptureLimit) as error:
                capture.feed(b"y" * 2048 + b"\n")
            self.assertEqual(error.exception.kind, "record")
        _sink, capture = self.capture(2048, compact_omp=False)
        with self.assertRaises(stream_capture.CaptureLimit) as error:
            capture.feed(JUNK_LINE)
        self.assertEqual(error.exception.kind, "retained")

    def test_payload_names_a_configured_limit_and_null_for_no_cap(self):
        capped = stream_capture.CaptureStats().payload(4096)
        self.assertEqual(capped["retainedLimitBytes"], 4096)
        self.assertEqual(capped["transportLimitBytes"], stream_capture.OMP_TRANSPORT_MAX_BYTES)
        self.assertEqual(capped["recordLimitBytes"], stream_capture.OMP_RECORD_MAX_BYTES)
        uncapped = stream_capture.CaptureStats().payload(None)
        self.assertIsNone(uncapped["retainedLimitBytes"])
        self.assertIsNone(uncapped["transportLimitBytes"])
        self.assertIsNone(uncapped["recordLimitBytes"])


class TrackedRunWithoutCapTests(unittest.TestCase):
    def test_codex_run_past_the_old_16_mib_default_completes_with_its_report(self):
        with tempfile.TemporaryDirectory() as temp:
            code, payload, run_path, ctx = run_tracked(
                Path(temp), flood_script(18 * MIB), harness="codex"
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["assistantText"], "done after the flood")
            state = load_state(ctx)
            self.assertEqual(state["status"], "succeeded")
            self.assertNotIn("outputLimit", state)
            self.assertNotEqual(state.get("failureReason"), "output_limit_exceeded")
            raw = (run_path / runner.STDOUT_LOG).read_bytes()
            # Nothing was truncated: the log holds the whole flood and the report line.
            self.assertGreater(len(raw), 18 * MIB)
            self.assertEqual(state["stdoutBytes"], len(raw))
            self.assertIn(b"done after the flood", raw[-4096:])
            self.assertIn("done after the flood", read_report(ctx))

    def test_omp_run_past_the_old_64_mib_default_completes(self):
        final = jsonl(
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "omp finished after the flood"}],
                },
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            code, payload, run_path, ctx = run_tracked(
                Path(temp), flood_script(66 * MIB, final), harness="omp", timeout=120
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["assistantText"], "omp finished after the flood")
            state = load_state(ctx)
            self.assertEqual(state["status"], "succeeded")
            self.assertNotIn("outputLimit", state)
            self.assertGreater((run_path / runner.STDOUT_LOG).stat().st_size, 66 * MIB)
            self.assertIsNone(payload["stdoutCapture"]["retainedLimitBytes"])
            self.assertIsNone(payload["stdoutCapture"]["limitKind"])

    def test_default_resolution_reaches_the_drain_as_no_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            ctx = make_context(Path(temp), "codex")
            self.assertIsNone(ctx.tracked_stream_max_bytes)
            self.assertEqual(runner._tracked_stream_max_bytes(ctx), 0)
            manifest = runner.build_manifest(ctx, ["codex"])
            self.assertNotIn("trackedStreamMaxBytes", manifest)
            ctx = make_context(Path(temp), "codex", tracked_stream_max_bytes=0)
            self.assertIsNone(runner.build_manifest(ctx, ["codex"])["trackedStreamMaxBytes"])
            ctx = make_context(Path(temp), "codex", tracked_stream_max_bytes=8192)
            self.assertEqual(runner.build_manifest(ctx, ["codex"])["trackedStreamMaxBytes"], 8192)

    def test_an_opted_in_cap_still_stops_the_child_and_names_the_key(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(runner.RunnerLaunchError) as caught:
                run_tracked(
                    Path(temp),
                    flood_script(64 * 1024),
                    harness="codex",
                    tracked_stream_max_bytes=16 * 1024,
                )
            self.assertEqual(caught.exception.error, "output_limit_exceeded")
            self.assertIn(
                "configured tracked stream limit of 16384 bytes", caught.exception.message
            )
            self.assertIn("codex.trackedStreamMaxBytes", caught.exception.message)
            self.assertIn("null for no cap", caught.exception.message)
            state = next(
                json.loads(path.read_text())
                for path in (Path(temp) / ".delegate" / "runs").glob("*/state.json")
            )
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failureReason"], "output_limit_exceeded")
            self.assertEqual(state["outputLimit"], {"stream": "stdout", "bytes": 16384})


class StdoutLogFlushTests(unittest.TestCase):
    def test_log_size_tracks_the_child_while_it_is_still_running(self):
        # A child that prints one short line and then works quietly. A buffered
        # log handle held that line back until 8 KiB accumulated, so the log
        # read as empty and the lane read as hung.
        script = (
            "import os, time\n"
            "os.write(1, b'first short line\\n')\n"
            "time.sleep(2.5)\n"
            "os.write(1, b'last line\\n')\n"
        )
        seen: list[bytes] = []
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            ctx = make_context(workspace, "codex")
            log = run_registry.run_directory(ctx.registry_root, ctx.run_id) / runner.STDOUT_LOG
            done = threading.Event()

            def watch():
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not done.is_set():
                    if log.exists() and log.read_bytes():
                        seen.append(log.read_bytes())
                        return
                    time.sleep(0.05)

            watcher = threading.Thread(target=watch)
            watcher.start()
            try:
                runner.execute_tracked(
                    [sys.executable, "-c", script],
                    str(workspace),
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    timeout=30,
                )
            finally:
                done.set()
                watcher.join(timeout=5)
        self.assertEqual(seen, [b"first short line\n"])


if __name__ == "__main__":
    unittest.main()
