"""Stable caller-supplied step keys, gate actions, and lifetime-safe stale cancel."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delegate_agent import run_registry
from delegate_agent.cli_parser import parse_cli
from delegate_agent.errors import DelegateError
from delegate_agent.workflows import commands, registry, runtime


class _WorkflowFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.wf_id = "wf_222222222222"
        self.root = registry.ensure_workflow_dir(self.workspace, self.wf_id)
        self.script = self.root / registry.SCRIPT_FILE
        self.script.write_text("return True\n", encoding="utf-8")
        registry.write_status(
            self.root,
            {
                "wfId": self.wf_id,
                "status": "created",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )

    def state(self, *, args: object = None) -> runtime.WorkflowState:
        """A fresh runtime lifetime over the same journal (a resume)."""
        return runtime.WorkflowState(
            wf_id=self.wf_id,
            workspace=self.workspace,
            root=self.root,
            script_path=self.script,
            config={},
            cli_argv=["delegate"],
            args=args,
            budget=runtime.Budget(None),
        )

    def events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == event_type
        ]

    def run_script(self, body: str, args: object, launch: object) -> object:
        script = self.root / "keyed.py"
        script.write_text(body, encoding="utf-8")
        state = self.state(args=args)
        frame = runtime._WorkflowInvocation(script, args, "root", 0)
        # A plain function patched onto the class binds self like the method.
        with mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts", launch):
            return runtime.execute_workflow(state, frame)


class KeyedReplayTests(_WorkflowFixture):
    SCRIPT = (
        'meta = {"name": "keyed", "defaults": {"engine": "codex"}}\n'
        'if not args["skip"]:\n'
        '    parallel([lambda: agent("early step")])\n'
        'keyed = parallel([lambda: agent("keyed step head=" + args["head"], key="impl")])\n'
        'unkeyed = parallel([lambda: agent("unkeyed step")])\n'
        'return {"keyed": keyed[0], "unkeyed": unkeyed[0]}\n'
    )

    def test_keyed_call_replays_when_an_earlier_parallel_is_skipped(self) -> None:
        first_calls: list[str] = []

        def first(_self: object, _engine: str, prompt: str, **_kw: object) -> str:
            first_calls.append(prompt)
            return f"run1:{prompt}"

        first_result = self.run_script(self.SCRIPT, {"skip": False, "head": "abc"}, first)
        self.assertEqual(first_result["keyed"], "run1:keyed step head=abc")
        self.assertEqual(len(first_calls), 3)

        second_calls: list[str] = []

        def second(_self: object, _engine: str, prompt: str, **_kw: object) -> str:
            second_calls.append(prompt)
            return f"run2:{prompt}"

        second_result = self.run_script(self.SCRIPT, {"skip": True, "head": "abc"}, second)
        # The keyed step hits the cache although its parallel moved from
        # position 1 to position 0.
        self.assertEqual(second_result["keyed"], "run1:keyed step head=abc")
        # Control: the unkeyed step's positional key shifted, so it misses and
        # relaunches, which is today's behavior for unkeyed calls.
        self.assertEqual(second_result["unkeyed"], "run2:unkeyed step")
        self.assertEqual(second_calls, ["unkeyed step"])

    def test_prompt_change_under_the_same_key_adopts_and_warns(self) -> None:
        self.run_script(
            self.SCRIPT,
            {"skip": True, "head": "abc"},
            lambda _self, _engine, prompt, **_kw: f"run1:{prompt}",
        )
        launches: list[str] = []

        def second(_self: object, _engine: str, prompt: str, **_kw: object) -> str:
            launches.append(prompt)
            return f"run2:{prompt}"

        result = self.run_script(self.SCRIPT, {"skip": True, "head": "def"}, second)
        self.assertEqual(result["keyed"], "run1:keyed step head=abc")
        self.assertNotIn("keyed step head=def", launches)
        warnings = self.events("key_prompt_mismatch")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["callerKey"], "impl")
        self.assertTrue(warnings[0]["promptChanged"])
        self.assertFalse(warnings[0]["optsChanged"])
        self.assertNotEqual(warnings[0]["recordedPromptDigest"], warnings[0]["promptDigest"])

    def test_keyed_scopes_make_positional_children_stable(self) -> None:
        script = (
            'meta = {"name": "scoped", "defaults": {"engine": "codex"}}\n'
            'if not args["skip"]:\n'
            '    parallel([lambda: agent("early step")])\n'
            'inner = parallel([lambda: agent("inside keyed parallel")], key="wave-1")\n'
            'items = pipeline(["a"], lambda prev, item, i: agent("stage " + item), key="p")\n'
            "return inner + items\n"
        )
        self.run_script(script, {"skip": False}, lambda _s, _e, prompt, **_k: f"run1:{prompt}")
        launches: list[str] = []

        def second(_self: object, _engine: str, prompt: str, **_kw: object) -> str:
            launches.append(prompt)
            return f"run2:{prompt}"

        result = self.run_script(script, {"skip": True}, second)
        self.assertEqual(result, ["run1:inside keyed parallel", "run1:stage a"])
        self.assertEqual(launches, [])

    def test_reject_moves_a_keyed_step_to_a_fresh_identity(self) -> None:
        state = self.state()
        dsl = runtime.WorkflowDsl(state, {"defaults": {"engine": "codex"}})
        answers = iter(["first", "second"])
        with mock.patch.object(
            runtime.WorkflowDsl, "_run_agent_attempts", side_effect=lambda *a, **k: next(answers)
        ):
            self.assertEqual(dsl.agent("judge", key="verdict", label="verdict"), "first")
            dsl.reject("verdict", "wrong answer")
            self.assertEqual(dsl.agent("judge", key="verdict", label="verdict"), "second")
        # A resume replays the retried identity, not the rejected one.
        resumed = runtime.WorkflowDsl(self.state(), {"defaults": {"engine": "codex"}})
        with mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts") as launch:
            self.assertEqual(resumed.agent("judge", key="verdict"), "second")
        launch.assert_not_called()

    def test_reusing_a_settled_key_in_one_lifetime_is_refused(self) -> None:
        dsl = runtime.WorkflowDsl(self.state(), {"defaults": {"engine": "codex"}})
        with mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts", return_value="one"):
            dsl.agent("item a", key="impl")
            with self.assertRaisesRegex(runtime.WorkflowKeyConflict, "'impl'.*earlier"):
                dsl.agent("item b", key="impl")

    def test_invalid_keys_are_refused(self) -> None:
        dsl = runtime.WorkflowDsl(self.state(), {})
        for bad in ("", "   ", "a\nb", "x" * (runtime.CALLER_KEY_MAX_CHARS + 1), 7):
            with self.subTest(key=bad), self.assertRaises(ValueError):
                dsl.agent("p", key=bad)


class LifetimeStaleCancelTests(_WorkflowFixture):
    def test_plain_threads_sharing_a_scope_both_complete(self) -> None:
        """Two agent() calls on plain threads mint the same positional scope."""
        state = self.state()
        dsl = runtime.WorkflowDsl(state, {"defaults": {"engine": "codex"}})
        cancelled: set[str] = set()
        both_launched = threading.Barrier(2, timeout=5)

        def cancel(_workspace: Path, _wf_id: str, key: str) -> list:
            cancelled.add(key)
            return []

        def launch(_self: object, _engine: str, prompt: str, *, key: str, **_kw: object):
            both_launched.wait()
            time.sleep(0.05)
            return None if key in cancelled else f"done:{prompt}"

        results: dict[str, object] = {}

        def call(name: str) -> None:
            results[name] = dsl.agent(f"adjudicate {name}")

        with (
            mock.patch.object(runtime, "cancel_workflow_agent_child", side_effect=cancel),
            mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts", launch),
        ):
            threads = [threading.Thread(target=call, args=(name,)) for name in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        started = self.events("agent_started")
        self.assertEqual({event["scope"] for event in started}, {"root/seq#0"})
        self.assertEqual(cancelled, set())
        self.assertEqual(results, {"a": "done:adjudicate a", "b": "done:adjudicate b"})
        self.assertEqual({event.get("incarnation") for event in started}, {state.supervisor_token})

    def test_duplicate_live_caller_key_raises_without_cancelling_the_holder(self) -> None:
        dsl = runtime.WorkflowDsl(self.state(), {"defaults": {"engine": "codex"}})
        holder_running = threading.Event()
        release = threading.Event()
        results: list[object] = []

        def launch(_self: object, _engine: str, prompt: str, **_kw: object) -> str:
            if prompt == "holder":
                holder_running.set()
                release.wait(5)
            return f"done:{prompt}"

        with (
            mock.patch.object(runtime, "cancel_workflow_agent_child") as cancel,
            mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts", launch),
        ):
            holder = threading.Thread(
                target=lambda: results.append(dsl.agent("holder", key="adjudicate"))
            )
            holder.start()
            self.assertTrue(holder_running.wait(5))
            with self.assertRaisesRegex(
                runtime.WorkflowKeyConflict, "'adjudicate'.*held by a live"
            ):
                dsl.agent("intruder", key="adjudicate")
            release.set()
            holder.join(5)
        cancel.assert_not_called()
        self.assertEqual(results, ["done:holder"])

    def test_prior_lifetime_child_in_the_scope_is_still_cancelled(self) -> None:
        registry.append_jsonl(
            self.root / registry.JOURNAL_FILE,
            {
                "seq": 1,
                "type": "agent_started",
                "key": "old-lifetime-key",
                "scope": "root/seq#0",
                "incarnation": "earlier",
            },
        )
        dsl = runtime.WorkflowDsl(self.state(), {"defaults": {"engine": "codex"}})
        with (
            mock.patch.object(runtime, "cancel_workflow_agent_child") as cancel,
            mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts", return_value="new"),
        ):
            self.assertEqual(dsl.agent("changed prompt"), "new")
        cancel.assert_called_once_with(self.workspace, self.wf_id, "old-lifetime-key")


class GateActionTests(_WorkflowFixture):
    def resume(self, *, gate_choice: commands.GateChoice | None = None) -> int:
        pin = SimpleNamespace(
            cli_argv=["delegate"], environment={}, profile_identity={"current": True}
        )
        attempt = SimpleNamespace(
            config={}, metadata={}, environment={}, config_path=self.root / "attempt.json"
        )
        with (
            mock.patch.object(commands.workflow_pinning, "load_pin", return_value=pin),
            mock.patch.object(commands.workflow_attempts, "prepare", return_value={}),
            mock.patch.object(commands.workflow_attempts, "create", return_value=attempt),
            mock.patch.object(commands.workflow_pinning, "register_active_supervisor"),
            mock.patch.object(
                commands.workflow_pinning, "temporarily_apply_environment", return_value={}
            ),
            mock.patch.object(runtime, "detach_supervisor"),
        ):
            return commands.emit_run(
                commands.WorkflowCommand("run", resume=self.wf_id),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                approve_gate=True,
                gate_choice=gate_choice,
            )

    def park(self, key: str, result: object, actions: list[str] | None) -> object:
        dsl = runtime.WorkflowDsl(self.state(), {})
        try:
            return dsl.park_gate(key, result, actions=actions)
        except runtime.GateExit as exc:
            return exc

    def test_action_round_trips_into_the_script(self) -> None:
        result = {"task": 7, "failure": "fixture drift"}
        parked = self.park("task-7", result, ["retry", "accept"])
        self.assertIsInstance(parked, runtime.GateExit)
        gate = self.events("gate")[-1]
        self.assertEqual(gate["gateName"], "task-7")
        self.assertEqual(gate["actions"], ["retry", "accept"])

        self.assertEqual(
            self.resume(
                gate_choice=commands.GateChoice(
                    gate="task-7",
                    action="accept",
                    note="known flake",
                    data={"waive": True},
                    has_data=True,
                )
            ),
            0,
        )
        decision = self.park("task-7", result, ["retry", "accept"])
        self.assertEqual(
            decision,
            {
                "gate": "task-7",
                "action": "accept",
                "note": "known flake",
                "data": {"waive": True},
            },
        )
        self.assertEqual(self.events("gate_decided")[-1]["action"], "accept")

    def test_invalid_action_is_refused_naming_the_allowed_ones(self) -> None:
        self.park("task-7", {"n": 1}, ["retry", "accept"])
        with self.assertRaises(DelegateError) as caught:
            self.resume(gate_choice=commands.GateChoice(gate="task-7", action="merge"))
        self.assertEqual(caught.exception.error, "invalid_gate_action")
        self.assertIn("allowed actions: retry, accept", str(caught.exception))
        approval = registry.read_json(self.root / registry.APPROVAL_FILE)
        self.assertIsNone(approval)

    def test_bare_approve_on_an_action_gate_without_approve_is_refused(self) -> None:
        self.park("task-7", {"n": 1}, ["retry", "accept"])
        with self.assertRaisesRegex(DelegateError, "requires --action"):
            self.resume()

    def test_unknown_gate_is_refused_listing_pending_gates(self) -> None:
        self.park("task-7", {"n": 1}, ["retry"])
        with self.assertRaises(DelegateError) as caught:
            self.resume(gate_choice=commands.GateChoice(gate="task-9", action="retry"))
        self.assertEqual(caught.exception.error, "workflow_gate_not_found")
        self.assertIn("pending gates: task-7", str(caught.exception))

    def test_bare_approve_is_unchanged(self) -> None:
        self.park("plain", {"n": 1}, None)
        gate = self.events("gate")[-1]
        self.assertEqual(self.resume(), 0)
        approval = registry.read_json(self.root / registry.APPROVAL_FILE) or {}
        self.assertEqual(
            approval["approvedResults"],
            [{"key": gate["key"], "resultHash": gate["gateResultHash"]}],
        )
        self.assertEqual(
            self.park("plain", {"n": 1}, None),
            {"gate": "plain", "action": "approve", "note": None, "data": None},
        )

    def test_cli_parses_approve_gate_options(self) -> None:
        parsed = parse_cli(
            [
                "workflow",
                "approve",
                "wf_222222222222",
                "--gate",
                "task-7",
                "--action",
                "retry",
                "--note",
                "go",
                "--data",
                '{"n": 1}',
            ]
        )
        payload = parsed.payload
        self.assertEqual(
            (payload.gate, payload.gate_action, payload.gate_note, payload.gate_data_json),
            ("task-7", "retry", "go", '{"n": 1}'),
        )


class AdoptionAndUnlaunchedRunTests(_WorkflowFixture):
    def register_child(self, key: str, state: dict) -> str:
        registry_root = run_registry.registry_root(self.workspace)
        run_id, alias = run_registry.register_run(
            registry_root,
            harness="codex",
            metadata={"group": self.wf_id, "workflowAgentKey": key},
        )
        run_registry.write_run_state(
            run_registry.run_directory(registry_root, run_id),
            {"schema": run_registry.STATE_SCHEMA, "runId": run_id, "alias": alias, **state},
        )
        return run_id

    def old(self, seconds: float) -> str:
        return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()

    def test_adoption_relaunches_over_a_run_that_never_launched(self) -> None:
        run_id = self.register_child(
            "key-a", {"status": "creating_isolation", "lastActivityAt": self.old(3600)}
        )
        dsl = runtime.WorkflowDsl(self.state(), {})
        adopted = dsl._adopt_existing_agent_run(
            "key-a", scope="root/seq#0", phase=None, schema=None, prefer_assistant=False, timeout=1
        )
        self.assertIs(adopted, runtime._MISSING)
        skipped = self.events("agent_adopt_skipped")
        self.assertEqual(skipped[-1]["reason"], "never_launched")
        self.assertEqual(skipped[-1]["runId"], run_id)
        sealed = run_registry.load_run_state_or_none(
            run_registry.registry_root(self.workspace), run_id
        )
        self.assertEqual(sealed["status"], "cancelled")

    def test_adoption_skips_a_cancelled_run(self) -> None:
        self.register_child("key-b", {"status": "cancelled"})
        dsl = runtime.WorkflowDsl(self.state(), {})
        adopted = dsl._adopt_existing_agent_run(
            "key-b", scope="root/seq#0", phase=None, schema=None, prefer_assistant=False, timeout=1
        )
        self.assertIs(adopted, runtime._MISSING)
        self.assertEqual(self.events("agent_adopt_skipped")[-1]["reason"], "cancelled")

    def test_resume_seals_missing_pid_children_past_the_grace_window(self) -> None:
        run_id = self.register_child(
            "key-c", {"status": "creating_isolation", "lastActivityAt": self.old(3600)}
        )
        cancelled = runtime.cancel_workflow_children(self.workspace, self.wf_id)
        self.assertEqual([item["runId"] for item in cancelled], [run_id])
        self.assertTrue(cancelled[0]["sealed"])
        state = run_registry.load_run_state_or_none(
            run_registry.registry_root(self.workspace), run_id
        )
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["staleReason"], "missing_pid")

    def test_resume_still_refuses_a_missing_pid_child_inside_the_grace_window(self) -> None:
        self.register_child("key-d", {"status": "running", "lastActivityAt": self.old(5)})
        with self.assertRaises(runtime.WorkflowChildCancellationError):
            runtime.cancel_workflow_children(self.workspace, self.wf_id)


class DryRunCwdTests(_WorkflowFixture):
    def test_dry_run_executes_in_the_cwd_workspace(self) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        self.script.write_text("import os\nreturn os.getcwd()\n", encoding="utf-8")
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(elsewhere.name)
        out = io.StringIO()
        commands.emit_dry_run(
            wf_id=self.wf_id,
            root=self.root,
            script_path=self.script,
            workspace=self.workspace,
            config={},
            args_value=None,
            budget_total=None,
            json_mode=True,
            warnings=[],
            stdout=out,
            stderr=io.StringIO(),
        )
        payload = json.loads(out.getvalue())
        self.assertEqual(os.path.realpath(payload["result"]), os.path.realpath(self.workspace))
        self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(elsewhere.name))


if __name__ == "__main__":
    unittest.main()
