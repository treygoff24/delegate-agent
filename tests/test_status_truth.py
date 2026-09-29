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


def _pi_tool_start(call_id="c1", name="mcp__node_repl_js_add_node_module_dir", **args):
    return json.dumps(
        {"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args}
    )


def _pi_tool_end(call_id="c1", name="mcp__node_repl_js_add_node_module_dir"):
    return json.dumps(
        {"type": "tool_execution_end", "toolCallId": call_id, "toolName": name, "result": {}}
    )


class PendingToolTests(unittest.TestCase):
    def _watch(self):
        from delegate_agent import stall_watchdog

        return stall_watchdog.StallWatchdog(stall_seconds=480, harness="omp"), stall_watchdog

    def _observe(self, watchdog, stall_watchdog, line, now):
        acc = harness_events.StreamAccumulator(harness="omp")
        acc.ingest_line(line)
        watchdog.observe_line(
            line, now=now, tool_events=stall_watchdog.tool_events_from(acc.events.last(5))
        )

    def test_oldest_pending_tool_names_the_call_and_its_age(self):
        watchdog, sw = self._watch()
        self.assertIsNone(watchdog.oldest_pending_tool(0.0))
        self._observe(watchdog, sw, _pi_tool_start(), 10.0)
        self._observe(watchdog, sw, _pi_tool_start("c2", "read"), 20.0)
        detail = watchdog.oldest_pending_tool(790.0)
        self.assertEqual(detail["name"], "mcp__node_repl_js_add_node_module_dir")
        self.assertEqual(detail["seconds"], 780)
        self._observe(watchdog, sw, _pi_tool_end(), 800.0)
        self.assertEqual(watchdog.oldest_pending_tool(801.0)["name"], "read")
        self._observe(watchdog, sw, _pi_tool_end("c2", "read"), 802.0)
        self.assertIsNone(watchdog.oldest_pending_tool(803.0))

    def test_pending_tool_is_tracked_with_stall_detection_disabled(self):
        from delegate_agent import stall_watchdog as sw

        watchdog = sw.StallWatchdog(stall_seconds=0, harness="omp")
        self._observe(watchdog, sw, _pi_tool_start(), 5.0)
        self.assertEqual(watchdog.oldest_pending_tool(65.0)["seconds"], 60)
        self.assertIsNone(watchdog.stalled_for(100000.0))
        self.assertIsNone(watchdog.confirm_stall(100000.0))
        self._observe(watchdog, sw, _pi_tool_end(), 70.0)
        self.assertIsNone(watchdog.oldest_pending_tool(71.0))

    def test_pending_tool_bookkeeping_is_bounded(self):
        watchdog, sw = self._watch()
        for index in range(sw.PENDING_TOOL_LIMIT + 40):
            self._observe(watchdog, sw, _pi_tool_start(f"c{index}", f"t{index}"), float(index))
        self.assertEqual(watchdog.tools_in_flight, sw.PENDING_TOOL_LIMIT)
        self.assertEqual(watchdog.oldest_pending_tool(1000.0)["name"], "t40")

    def test_pending_generation_moves_on_start_and_finish(self):
        watchdog, sw = self._watch()
        first = watchdog.pending_generation
        self._observe(watchdog, sw, _pi_tool_start(), 1.0)
        second = watchdog.pending_generation
        self._observe(watchdog, sw, _pi_tool_end(), 2.0)
        self.assertLess(first, second)
        self.assertLess(second, watchdog.pending_generation)

    def test_a_tool_that_finishes_quietly_stops_being_shown_as_pending(self):
        import io
        import sys
        import tempfile
        import threading
        import time
        from pathlib import Path
        from unittest import mock

        from delegate_agent import pending_tool, run_registry, runner

        script = (
            "import json,time\n"
            "print(json.dumps({'type':'tool_execution_start','toolCallId':'c1',"
            "'toolName':'mcp__brief','args':{}}),flush=True)\n"
            "time.sleep(0.3)\n"
            "print(json.dumps({'type':'tool_execution_end','toolCallId':'c1',"
            "'toolName':'mcp__brief','result':{}}),flush=True)\n"
            "time.sleep(2.5)\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="omp")
            ctx = _ctx(root=root, run_id=run_id, alias=alias, workspace=workspace)
            seen = {"pending": False, "cleared": False}

            def poll():
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not seen["cleared"]:
                    state = run_registry.load_run_state(root, run_id) or {}
                    if state.get("status") == "running":
                        if state.get("pendingTool"):
                            seen["pending"] = True
                        elif seen["pending"]:
                            seen["cleared"] = True
                    time.sleep(0.05)

            thread = threading.Thread(target=poll)
            thread.start()
            with (
                mock.patch.object(pending_tool, "NOTICE_SECONDS", 0),
                mock.patch.object(pending_tool, "REFRESH_SECONDS", 1000),
            ):
                runner.execute_tracked(
                    [sys.executable, "-c", script],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            thread.join()
        self.assertTrue(seen["pending"])
        self.assertTrue(seen["cleared"], "pendingTool stayed on the running record")

    def test_a_pending_tool_still_never_stalls_the_run(self):
        watchdog, sw = self._watch()
        self._observe(watchdog, sw, _pi_tool_start(), 0.0)
        self.assertIsNone(watchdog.stalled_for(100000.0))

    def test_record_carries_pending_tool_and_waiting_current(self):
        from delegate_agent import pending_tool, runner

        acc = harness_events.StreamAccumulator(harness="omp")
        acc.current = "mcp__x"
        acc.pending_tool = {
            "name": "mcp__x",
            "seconds": 780,
            "target": "token=sk-abcdefghijklmnop1234",
        }
        ctx = _ctx()
        record = runner.build_run_record(ctx, status="running", accumulator=acc)
        self.assertEqual(record["current"], "waiting on tool mcp__x for 13m")
        self.assertEqual(record["pendingTool"]["name"], "mcp__x")
        self.assertEqual(record["pendingTool"]["seconds"], 780)
        self.assertIn("startedAt", record["pendingTool"])
        self.assertNotIn("sk-abcdefghijklmnop1234", json.dumps(record))
        acc.pending_tool = {"name": "mcp__x", "seconds": 5}
        record = runner.build_run_record(ctx, status="running", accumulator=acc)
        self.assertEqual(record["current"], "mcp__x")
        self.assertEqual(record["pendingTool"]["seconds"], 5)
        acc.pending_tool = {"name": "mcp__x", "seconds": 900}
        record = runner.build_run_record(ctx, status="failed", accumulator=acc)
        self.assertNotIn("pendingTool", record)
        self.assertEqual(pending_tool.waiting_text("t", 3900), "waiting on tool t for 1h05m")

    def test_live_run_publishes_pending_tool_to_state_and_snapshot(self):
        import io
        import sys
        import tempfile
        import threading
        import time
        from pathlib import Path
        from unittest import mock

        from delegate_agent import pending_tool, run_registry, runner, snapshot_view

        script = (
            "import json,sys,time\n"
            "print(json.dumps({'type':'tool_execution_start','toolCallId':'c1',"
            "'toolName':'mcp__hung','args':{}}),flush=True)\n"
            "time.sleep(2.0)\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="omp")
            ctx = _ctx(root=root, run_id=run_id, alias=alias, workspace=workspace)
            seen: dict = {}

            def poll():
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and "state" not in seen:
                    state = run_registry.load_run_state(root, run_id) or {}
                    if state.get("pendingTool") and "waiting on tool" in state.get("current", ""):
                        seen["state"] = state
                        seen["view"] = snapshot_view.merge_snapshot_view(
                            root, run_id, None, redact=True
                        )
                    time.sleep(0.05)

            thread = threading.Thread(target=poll)
            thread.start()
            with (
                mock.patch.object(pending_tool, "NOTICE_SECONDS", 0),
                mock.patch.object(pending_tool, "REFRESH_SECONDS", 0),
            ):
                runner.execute_tracked(
                    [sys.executable, "-c", script],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            thread.join()
        self.assertEqual(seen["state"]["pendingTool"]["name"], "mcp__hung")
        self.assertEqual(seen["view"]["pendingTool"]["name"], "mcp__hung")
        self.assertIn("mcp__hung", seen["view"]["current"])


def _ctx(root=None, run_id="run-1", alias="a1", workspace="/tmp"):
    from pathlib import Path

    from delegate_agent import run_registry, runner

    return runner.RunContext(
        registry_root=root if root is not None else Path("/tmp/none"),
        run_id=run_id,
        alias=alias,
        harness="omp",
        engine="omp",
        mode="work",
        model=None,
        source_cwd=workspace,
        execution_cwd=workspace,
        workspace_kind="directory",
        isolated_workspace=False,
        started_at=run_registry.utc_now_iso(),
    )


if __name__ == "__main__":
    unittest.main()
