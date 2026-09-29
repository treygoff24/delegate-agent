"""OMP ``tool_execution_update`` records are compacted, the way thinking deltas are.

OMP re-emits a tool's whole ``args`` on every update (for the sub-agent ``task``
tool that is the full shared context, about 70 KB a record) and a streaming
tool's whole accumulated ``partialResult``. Local run logs held records up to
71 KB. The shapes below mirror those logs; the ``task`` records are inferred from
them, not observed, because no local run happened to include one.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path

from delegate_agent import runner, stream_capture
from tests.tracked_capture_helpers import run_tracked

TASK_CONTEXT_UNIT = "shared context for every sub-agent "
TASK_CONTEXT = TASK_CONTEXT_UNIT * 2000  # about 70 KB


def update_line(*, args, partial, tool="task", call_id="call_1|fc_1") -> bytes:
    return (
        json.dumps(
            {
                "type": "tool_execution_update",
                "toolCallId": call_id,
                "toolName": tool,
                "args": args,
                "partialResult": partial,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def big_args() -> dict:
    return {"agent": "explore", "context": TASK_CONTEXT, "tasks": [{"id": "A", "assignment": "x"}]}


def small_partial() -> dict:
    return {"content": [{"type": "text", "text": "running"}]}


def capture(records, *, max_bytes=None, compact_omp=True, split=False):
    sink = io.BytesIO()
    cap = stream_capture.BoundedCapture(sink.write, max_bytes, compact_omp=compact_omp)
    wire = b"".join(records)
    if split:
        for byte in wire:
            cap.feed(bytes([byte]))
    else:
        cap.feed(wire)
    cap.finish()
    return sink.getvalue(), cap


class ToolUpdateCompactionTests(unittest.TestCase):
    def test_oversized_args_become_a_stub_and_the_rest_of_the_record_survives(self):
        record = update_line(args=big_args(), partial=small_partial())
        self.assertGreater(len(record), 60_000)
        out, cap = capture([record])
        line = json.loads(out)
        self.assertEqual(line["type"], "tool_execution_update")
        self.assertEqual(line["toolCallId"], "call_1|fc_1")
        self.assertEqual(line["toolName"], "task")
        self.assertEqual(line["partialResult"], small_partial())
        stub = line["args"]
        self.assertTrue(stub["delegateCompacted"])
        self.assertEqual(stub["originalBytes"], len(json.dumps(big_args(), ensure_ascii=False)))
        self.assertTrue(stub["head"].startswith('{"agent": "explore"'))
        self.assertTrue(stub["tail"].endswith('"assignment": "x"}]}'))
        self.assertLess(len(out), 2048)
        self.assertEqual(cap.stats.compacted_tool_update_records, 1)
        self.assertEqual(cap.stats.compacted_tool_update_bytes, len(record) - len(out))
        # The wire was still hashed and counted in full.
        self.assertEqual(cap.stats.transport_bytes, len(record))
        self.assertEqual(cap.stats.captured_bytes, len(out))

    def test_oversized_partial_result_is_compacted_and_small_args_stay(self):
        partial = {"content": [{"type": "text", "text": "line of streamed output\n" * 4000}]}
        record = update_line(args={"command": "make test"}, partial=partial, tool="bash")
        out, _cap = capture([record])
        line = json.loads(out)
        self.assertEqual(line["args"], {"command": "make test"})
        self.assertTrue(line["partialResult"]["delegateCompacted"])
        self.assertGreater(line["partialResult"]["originalBytes"], 60_000)
        self.assertLess(len(out), len(record) // 10)

    def test_small_updates_and_all_other_omp_records_are_byte_for_byte(self):
        start = (
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call_1|fc_1",
                    "toolName": "task",
                    "args": big_args(),
                }
            )
            + "\n"
        ).encode()
        end = (
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call_1|fc_1",
                    "toolName": "task",
                    "result": {"content": [{"type": "text", "text": "r" * 30_000}]},
                }
            )
            + "\n"
        ).encode()
        records = [
            update_line(args={"a": 1}, partial=small_partial()),
            start,
            end,
            b'{"type":"turn_end","message":{"role":"assistant"}}\n',
        ]
        out, cap = capture(records)
        self.assertEqual(out, b"".join(records))
        self.assertEqual(cap.stats.compacted_tool_update_records, 0)
        self.assertFalse(cap.stats.payload(None)["truncated"])

    def test_unrecognized_or_ambiguous_lines_are_never_rewritten(self):
        huge = "q" * 70_000
        records = [
            # Not JSON, though it mentions the event name.
            b"tool_execution_update but not json " + huge.encode() + b"\n",
            # A different record type that merely mentions the event name and
            # carries an oversized `args` of its own.
            (
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "task",
                        "resultOf": "tool_execution_update",
                        "args": {"x": huge},
                    }
                )
                + "\n"
            ).encode(),
            # Duplicate keys make the record ambiguous: a plain parser would read
            # the last `type` and rewrite it.
            b'{"type":"error","type":"tool_execution_update","args":{"x":"'
            + huge.encode()
            + b'"}}\n',
            # An update-shaped record that is not an object at the top level.
            b'["tool_execution_update","' + huge.encode() + b'"]\n',
            # Invalid UTF-8.
            b'{"type":"tool_execution_update","args":"\xff' + huge.encode() + b'"}\n',
        ]
        out, cap = capture(records)
        self.assertEqual(out, b"".join(records))
        self.assertEqual(cap.stats.compacted_tool_update_records, 0)

    def test_only_omp_capture_compacts(self):
        record = update_line(args=big_args(), partial=small_partial())
        out, cap = capture([record], compact_omp=False)
        self.assertEqual(out, record)
        self.assertEqual(cap.stats.compacted_tool_update_records, 0)

    def test_byte_at_a_time_delivery_compacts_the_same_way(self):
        record = update_line(args=big_args(), partial=small_partial())
        whole, _ = capture([record])
        split, cap = capture([record], split=True)
        self.assertEqual(split, whole)
        self.assertEqual(cap.stats.compacted_tool_update_records, 1)

    def test_compaction_is_reported_in_the_payload_and_a_warning(self):
        records = [update_line(args=big_args(), partial=small_partial()) for _ in range(3)]
        _out, cap = capture(records)
        payload = cap.stats.payload(None)
        self.assertEqual(payload["policy"], "omp-capture-v2")
        self.assertEqual(payload["compactedToolUpdateRecords"], 3)
        self.assertEqual(payload["compactedToolUpdateBytes"], cap.stats.compacted_tool_update_bytes)
        self.assertTrue(payload["truncated"])
        warning = stream_capture.capture_warning(payload)
        self.assertIn("tool_execution_update records were compacted", warning)
        self.assertIn("3 records", warning)
        self.assertNotIn("thinking", warning)

    def test_an_opted_in_cap_is_charged_the_compacted_size(self):
        record = update_line(args=big_args(), partial=small_partial())
        # Sixty of these are ~4 MB on the wire but ~100 KB retained.
        out, cap = capture([record] * 60, max_bytes=256 * 1024)
        self.assertLess(len(out), 256 * 1024)
        self.assertEqual(cap.stats.compacted_tool_update_records, 60)
        self.assertIsNone(cap.stats.limit_kind)

    def test_omp_flood_of_task_updates_keeps_stdout_log_small_and_the_run_succeeds(self):
        count = 1500
        # The child builds its ~70 KB records itself; a script that embedded them
        # would exceed the per-argument size limit on Linux.
        script = f"""
import json, os
def event(**fields):
    return (json.dumps(fields, separators=(",", ":")) + "\\n").encode()
args = {{"agent": "explore", "context": {TASK_CONTEXT_UNIT!r} * 2000, "tasks": []}}
partial = {{"content": [{{"type": "text", "text": "running"}}]}}
os.write(1, event(type="tool_execution_start", toolCallId="c|f", toolName="task", args=args))
update = event(
    type="tool_execution_update", toolCallId="c|f", toolName="task", args=args, partialResult=partial
)
for _ in range({count}):
    os.write(1, update)
os.write(1, event(
    type="tool_execution_end", toolCallId="c|f", toolName="task",
    result={{"content": [{{"type": "text", "text": "sub-agent report: all findings"}}]}},
))
os.write(1, event(
    type="turn_end",
    message={{"role": "assistant", "stopReason": "stop",
             "content": [{{"type": "text", "text": "review complete"}}]}},
))
"""
        with tempfile.TemporaryDirectory() as temp:
            code, payload, run_path, _ctx = run_tracked(
                Path(temp), script, harness="omp", timeout=60
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["assistantText"], "review complete")
            capture_info = payload["stdoutCapture"]
            self.assertEqual(capture_info["compactedToolUpdateRecords"], count)
            self.assertGreater(capture_info["transportBytes"], count * 60_000)
            raw = (run_path / runner.STDOUT_LOG).read_bytes()
            self.assertEqual(len(raw), capture_info["capturedBytes"])
            # 1500 records of ~70 KB would be ~105 MB; the compacted log is a few hundred KB.
            self.assertLess(len(raw), 1_500_000)
            # Start and end still carry the complete arguments and result.
            self.assertIn(TASK_CONTEXT.encode(), raw)
            self.assertIn(b"sub-agent report: all findings", raw)
            self.assertTrue(
                any(
                    "tool_execution_update records were compacted" in warning
                    for warning in payload["warnings"]
                )
            )


if __name__ == "__main__":
    unittest.main()
