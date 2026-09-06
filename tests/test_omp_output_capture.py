"""Finite local event fixtures, not live-provider evidence."""

import hashlib
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


def thinking_line(text="thinking" * 512):
    return (
        json.dumps(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_delta",
                    "contentIndex": 0,
                    "delta": text,
                },
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


def final_line():
    return (
        json.dumps(
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "completed after bounded telemetry"}],
                },
            }
        )
        + "\n"
    ).encode()


class OmpOutputCaptureTests(unittest.TestCase):
    def noisy_script(self, ending=None):
        line = thinking_line()
        count = runner.TRACKED_STREAM_MAX_BYTES // len(line) + 100
        return (
            f"import os\nline={line!r}\n"
            f"for _ in range({count}): os.write(1,line)\n"
            f"os.write(1,{(final_line() if ending is None else ending)!r})\n"
        )

    def tracked(self, workspace, script):
        root = run_registry.ensure_registry(workspace, workspace_kind="directory")
        run_id, alias = run_registry.register_run(root, harness="omp")
        ctx = runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness="omp",
            engine="omp",
            mode="work",
            model=None,
            source_cwd=str(workspace),
            execution_cwd=str(workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
        )
        code, payload = runner.execute_tracked(
            [sys.executable, "-c", script],
            str(workspace),
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            timeout=10,
        )
        return code, payload, run_registry.run_directory(root, run_id)

    def test_tracked_completion_survives_more_than_old_cap_of_thinking(self):
        with tempfile.TemporaryDirectory() as temp:
            code, payload, run_path = self.tracked(Path(temp), self.noisy_script())
            self.assertEqual(code, 0)
            self.assertEqual(payload["assistantText"], "completed after bounded telemetry")
            capture = payload["stdoutCapture"]
            self.assertGreater(capture["transportBytes"], runner.TRACKED_STREAM_MAX_BYTES)
            self.assertGreater(capture["omittedThinkingRecords"], 0)
            self.assertTrue(capture["truncated"])
            raw = (run_path / runner.STDOUT_LOG).read_bytes()
            self.assertEqual(len(raw), capture["capturedBytes"])
            self.assertLess(len(raw), 128 * 1024)
            self.assertIn(b"delegate.capture", raw)
            self.assertIn(final_line(), raw)
            self.assertTrue(any("thinking" in warning for warning in payload["warnings"]))

    def test_call_completion_survives_more_than_old_cap_of_thinking(self):
        with tempfile.TemporaryDirectory() as temp:
            result = runner.execute_call(
                [sys.executable, "-c", self.noisy_script()], temp, harness="omp", timeout=10
            )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.text, "completed after bounded telemetry")
        self.assertGreater(result.stdout_capture["transportBytes"], runner.CALL_STDOUT_MAX_BYTES)
        self.assertLess(result.stdout_bytes, 128 * 1024)
        self.assertTrue(any("thinking" in warning for warning in result.warnings))

    def test_provider_failure_after_compacted_thinking_remains_a_failure(self):
        ending = (
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "provider rejected the request",
                        "content": [{"type": "text", "text": "partial answer"}],
                    },
                }
            )
            + "\n"
        ).encode()
        with tempfile.TemporaryDirectory() as temp:
            script = self.noisy_script(ending)
            code, payload, _ = self.tracked(Path(temp), script)
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"], "provider_error")
            self.assertEqual(payload["assistantText"], "partial answer")
            self.assertGreater(payload["stdoutCapture"]["omittedThinkingRecords"], 0)
            call = runner.execute_call(
                [sys.executable, "-c", script], temp, harness="omp", timeout=10
            )
            self.assertEqual(call.exit_code, 1)
            self.assertEqual(call.error, "provider_error")
            self.assertEqual(call.text, "partial answer")

    def test_infinite_thinking_hits_transport_limit_and_reaps_child(self):
        script = f"import os\nwhile True: os.write(1,{thinking_line()!r})\n"
        started = time.monotonic()
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(stream_capture, "OMP_TRANSPORT_MAX_BYTES", 16384),
        ):
            with self.assertRaises(runner.RunnerLaunchError) as error:
                self.tracked(Path(temp), script)
            self.assertEqual(error.exception.error, "output_limit_exceeded")
            states = list((Path(temp) / ".delegate" / "runs").glob("*/state.json"))
            self.assertEqual(len(states), 1)
            state = json.loads(states[0].read_text())
            self.assertEqual(state["stdoutCapture"]["limitKind"], "transport")
            self.assertEqual(state["outputLimit"]["bytes"], 16384)
            self.assertLessEqual(
                state["stdoutCapture"]["transportBytes"], 16384 + runner.STREAM_READ_CHUNK_BYTES
            )
            with self.assertRaises(runner.RunnerLaunchError) as call_error:
                runner.execute_call([sys.executable, "-c", script], temp, harness="omp", timeout=10)
            self.assertEqual(call_error.exception.error, "call_stdout_overflow")
        self.assertLess(time.monotonic() - started, 5)

    def test_useful_and_malformed_floods_keep_retained_limit(self):
        useful = (
            json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "useful output" * 100},
                }
            )
            + "\n"
        ).encode()
        for line in (useful, b"not JSON\n"):
            with self.subTest(line=line[:30]), tempfile.TemporaryDirectory() as temp:
                script = f"import os\nfor _ in range(2000): os.write(1,{line!r})\n"
                with (
                    mock.patch.object(runner, "TRACKED_STREAM_MAX_BYTES", 4096),
                    self.assertRaises(runner.RunnerLaunchError) as error,
                ):
                    self.tracked(Path(temp), script)
                self.assertEqual(error.exception.error, "output_limit_exceeded")
                with (
                    mock.patch.object(runner, "CALL_STDOUT_MAX_BYTES", 4096),
                    self.assertRaises(runner.RunnerLaunchError) as error,
                ):
                    runner.execute_call(
                        [sys.executable, "-c", script], temp, harness="omp", timeout=10
                    )
                self.assertEqual(error.exception.error, "call_stdout_overflow")

    def test_filter_preserves_metadata_and_unknown_shapes_byte_for_byte(self):
        base = json.loads(thinking_line("example"))
        records = [b"not JSON\n", b"{}\n", b'{"type":"error","message":"failed"}\n', final_line()]
        for key in ("model", "usage", "error", "tool", "message"):
            records.append((json.dumps({**base, key: "must survive"}) + "\n").encode())
            records.append(
                (
                    json.dumps(
                        {
                            **base,
                            "assistantMessageEvent": {
                                **base["assistantMessageEvent"],
                                key: "must survive",
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
        records.append(
            b'{"type":"error","type":"message_update","assistantMessageEvent":{"type":"thinking_delta","contentIndex":0,"delta":"duplicate keys"}}\n'
        )
        records.append(json.dumps(base).encode("utf-16") + b"\n")
        output = io.BytesIO()
        with mock.patch.object(stream_capture, "OMP_THINKING_SAMPLE_BYTES", 0):
            capture = stream_capture.BoundedCapture(output.write, 16384, compact_omp=True)
            for record in records:
                capture.feed(record)
            capture.finish()
        self.assertEqual(output.getvalue(), b"".join(records))
        self.assertEqual(capture.stats.omitted_thinking_records, 0)

    def test_utf8_splits_hashes_and_unterminated_final_record(self):
        telemetry = thinking_line("reasoning \u2603")
        final = final_line().rstrip(b"\n")
        wire = telemetry + final
        output = io.BytesIO()
        observed = []
        with mock.patch.object(stream_capture, "OMP_THINKING_SAMPLE_BYTES", 0):
            capture = stream_capture.BoundedCapture(
                output.write, 16384, compact_omp=True, on_omitted=observed.append
            )
            for byte in wire:
                capture.feed(bytes([byte]))
            capture.finish()
        self.assertEqual(output.getvalue(), stream_capture.OMISSION_MARKER + final)
        self.assertEqual(observed, [telemetry.decode()])
        self.assertEqual(capture.payload()["transportSha256"], hashlib.sha256(wire).hexdigest())
        self.assertEqual(capture.stats.omitted_thinking_bytes, len(telemetry))

    def test_record_boundary_is_finite_and_not_a_discount_for_huge_records(self):
        record = thinking_line("large telemetry")
        with mock.patch.object(stream_capture, "OMP_RECORD_MAX_BYTES", len(record)):
            output = io.BytesIO()
            accepted = stream_capture.BoundedCapture(output.write, 16384, compact_omp=True)
            accepted.feed(record)
            accepted.finish()
            self.assertEqual(output.getvalue(), record)
            oversized = stream_capture.BoundedCapture(io.BytesIO().write, 16384, compact_omp=True)
            with self.assertRaises(stream_capture.CaptureLimit) as error:
                oversized.feed(record[:-1] + b" \n")
            self.assertEqual(error.exception.kind, "record")

    def test_non_omp_capture_never_discounts_thinking(self):
        output = io.BytesIO()
        record = thinking_line("ordinary raw bytes")
        capture = stream_capture.BoundedCapture(output.write, len(record) - 1)
        with self.assertRaises(stream_capture.CaptureLimit):
            capture.feed(record)
        self.assertEqual(output.getvalue(), record[:-1])
        self.assertEqual(capture.stats.omitted_thinking_records, 0)

    def test_call_checks_overflow_discovered_after_leader_exit(self):
        released = threading.Event()

        class DelayedPipe(io.BytesIO):
            def read(self, size=-1):
                released.wait(timeout=2)
                return super().read(size)

        process = mock.Mock(
            stdout=DelayedPipe(b"x" * 32),
            stderr=io.BytesIO(),
            stdin=None,
        )
        process.wait.return_value = 0
        process.poll.return_value = 0
        with (
            mock.patch.object(
                runner, "_terminate_call_process", side_effect=lambda *_: released.set()
            ),
            self.assertRaises(runner.RunnerLaunchError) as error,
        ):
            runner._bounded_call_communicate(process, None, 5, 16, 16)
        self.assertEqual(error.exception.error, "call_stdout_overflow")
