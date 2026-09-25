"""Stable caller-supplied step keys, gate actions, and lifetime-safe stale cancel."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delegate_agent import run_registry, runner, wait_cancel_commands
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


class ItemHandlerRefusalTests(_WorkflowFixture):
    """A key refusal reaches the script from every per-item handler."""

    def test_a_duplicate_live_key_in_parallel_reaches_the_script(self) -> None:
        script = (
            'meta = {"name": "dupes", "defaults": {"engine": "codex"}}\n'
            "refused = args['refused']\n"
            "def step(name):\n"
            "    try:\n"
            "        return agent(name, key='impl')\n"
            "    except Exception:\n"
            "        refused.set()\n"
            "        raise\n"
            "parallel([lambda: step('a'), lambda: step('b')])\n"
            "return True\n"
        )
        refused = threading.Event()

        def launch(_self: object, _engine: str, prompt: str, **_kw: object) -> str:
            # Hold the winner's key live until its peer has been refused.
            self.assertTrue(refused.wait(5), "the peer never refused the duplicate key")
            return f"done:{prompt}"

        with self.assertRaisesRegex(runtime.WorkflowKeyConflict, "held by a live"):
            self.run_script(script, {"refused": refused}, launch)
        self.assertEqual(self.events("thunk_failed"), [], "the refusal became a failed item slot")
        self.assertEqual(len(self.events("agent_started")), 1)

    def test_an_invalid_key_in_parallel_reaches_the_script(self) -> None:
        script = (
            'meta = {"name": "bad-key", "defaults": {"engine": "codex"}}\n'
            'parallel([lambda: agent("a", key=7)])\n'
            "return True\n"
        )
        with self.assertRaises(ValueError) as caught:
            self.run_script(script, {}, lambda *_a, **_k: self.fail("launched an invalid key"))
        self.assertIsInstance(caught.exception, runtime.WorkflowKeyError)
        self.assertIn("agent() key must be a non-empty printable string", str(caught.exception))
        self.assertEqual(self.events("thunk_failed"), [])

    def test_a_duplicate_live_key_in_pipeline_reaches_the_script(self) -> None:
        script = (
            'meta = {"name": "dupes", "defaults": {"engine": "codex"}}\n'
            "refused = args['refused']\n"
            "def stage(prev, item, index):\n"
            "    try:\n"
            "        return agent(item, key='impl')\n"
            "    except Exception:\n"
            "        refused.set()\n"
            "        raise\n"
            "pipeline(['a', 'b'], stage)\n"
            "return True\n"
        )
        refused = threading.Event()

        def launch(_self: object, _engine: str, prompt: str, **_kw: object) -> str:
            self.assertTrue(refused.wait(5), "the peer never refused the duplicate key")
            return f"done:{prompt}"

        with self.assertRaisesRegex(runtime.WorkflowKeyConflict, "held by a live"):
            self.run_script(script, {"refused": refused}, launch)
        self.assertEqual(self.events("stage_failed"), [], "the refusal dropped an item")
        self.assertEqual(len(self.events("agent_started")), 1)

    def test_a_duplicate_live_gate_key_in_soft_park_reaches_the_script(self) -> None:
        script = (
            'meta = {"name": "gates", "defaults": {"engine": "codex"}}\n'
            "refused = args['refused']\n"
            "def ask(name):\n"
            "    try:\n"
            "        return park_gate('review', {'item': name}, actions=['retry', 'accept'])\n"
            "    except Exception:\n"
            "        refused.set()\n"
            "        raise\n"
            "def worker():\n"
            "    return parallel([lambda: ask('a'), lambda: ask('b')])\n"
            "soft_park([('job', worker)])\n"
            "return True\n"
        )
        refused = threading.Event()

        def decide(_root: Path, _gate_key: str, _result_hash: str, **_: object) -> dict:
            # Hold the winner's gate key live until its peer has been refused.
            self.assertTrue(refused.wait(5), "the peer never refused the duplicate gate key")
            return {"action": "accept"}

        with (
            mock.patch.object(runtime.registry, "approval_decision", decide),
            self.assertRaisesRegex(runtime.WorkflowKeyConflict, "held by a live park_gate"),
        ):
            self.run_script(script, {"refused": refused}, lambda *_a, **_k: None)
        self.assertEqual(
            self.events("soft_park_item_failed"), [], "the refusal became a failed item slot"
        )
        self.assertEqual(self.events("thunk_failed"), [], "the refusal became a failed item slot")


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

    def test_an_answer_the_gate_no_longer_offers_is_asked_again(self) -> None:
        result = {"task": 8}
        self.assertIsInstance(self.park("task-8", result, ["retry", "accept"]), runtime.GateExit)
        choice = commands.GateChoice(
            gate="task-8", action="accept", note=None, data=None, has_data=False
        )
        self.assertEqual(self.resume(gate_choice=choice), 0)
        # The script stops offering "accept": the recorded answer lapses.
        reparked = self.park("task-8", result, ["retry", "skip"])
        self.assertIsInstance(reparked, runtime.GateExit, "an undeclared action was returned")
        gates = self.events("gate")
        self.assertEqual(gates[-1]["actions"], ["retry", "skip"])
        self.assertNotEqual(gates[-1]["gateResultHash"], gates[0]["gateResultHash"])
        self.assertEqual(self.events("gate_action_undeclared")[-1]["action"], "accept")
        skip = commands.GateChoice(
            gate="task-8", action="skip", note=None, data=None, has_data=False
        )
        self.assertEqual(self.resume(gate_choice=skip), 0)
        decision = self.park("task-8", result, ["retry", "skip"])
        self.assertEqual(decision["action"], "skip")

    def test_a_reask_question_is_not_answered_by_a_result_hash(self) -> None:
        """A result shaped like the old re-ask payload must not satisfy a re-ask."""
        result = {"task": 11}
        collision = {"reask": {"result": result, "actions": ["retry"]}}
        # Pass 1: the script parks this key on a result that is exactly what a
        # later re-ask would have hashed, and the operator answers it.
        self.assertIsInstance(
            self.park("task-11", collision, ["retry", "accept"]), runtime.GateExit
        )
        self.assertEqual(
            self.resume(
                gate_choice=commands.GateChoice(gate="task-11", action="accept", note="pass 1")
            ),
            0,
        )
        # Pass 2: the plain result, still offering "accept", and answered.
        self.assertIsInstance(self.park("task-11", result, ["retry", "accept"]), runtime.GateExit)
        self.assertEqual(
            self.resume(
                gate_choice=commands.GateChoice(gate="task-11", action="accept", note="pass 2")
            ),
            0,
        )
        # Pass 3: only "retry" is offered now, so the recorded "accept" lapses
        # and the question is asked again under a hash of its own.
        reparked = self.park("task-11", result, ["retry"])
        self.assertIsInstance(reparked, runtime.GateExit, "the re-ask consumed another answer")
        self.assertEqual(self.events("gate_action_undeclared")[-1]["action"], "accept")
        self.assertEqual(self.events("gate")[-1]["actions"], ["retry"])
        self.assertEqual(
            self.resume(
                gate_choice=commands.GateChoice(gate="task-11", action="retry", note="pass 3")
            ),
            0,
        )
        self.assertEqual(self.park("task-11", result, ["retry"])["action"], "retry")

    def test_a_second_live_park_gate_with_one_key_is_refused(self) -> None:
        dsl = runtime.WorkflowDsl(self.state(), {})
        nested: list[object] = []
        calls: list[str] = []

        def decide(root: Path, gate_key: str, result_hash: str, **_: object) -> dict:
            calls.append(gate_key)
            if len(calls) == 1:
                # While the first call is live, a sibling asks with the same key.
                try:
                    nested.append(dsl.park_gate("review", {"n": 2}))
                except runtime.WorkflowKeyConflict as exc:
                    nested.append(exc)
                nested.append(dsl.park_gate("other", {"n": 3}))
            return {"action": "approve"}

        with mock.patch.object(runtime.registry, "approval_decision", decide):
            first = dsl.park_gate("review", {"n": 1})
            self.assertEqual(first["action"], "approve")
            self.assertIsInstance(nested[0], runtime.WorkflowKeyConflict)
            self.assertIn("park_gate(key='review') is already held", str(nested[0]))
            self.assertIn("Make the key unique", str(nested[0]))
            self.assertEqual(nested[1]["action"], "approve")
            # A settled call releases its key: the same key may ask again.
            self.assertEqual(dsl.park_gate("review", {"n": 4})["action"], "approve")

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

    def test_adoption_waits_on_a_never_launched_run_this_lifetime_started(self) -> None:
        self.register_child(
            "key-s", {"status": "creating_isolation", "lastActivityAt": self.old(3600)}
        )
        state = self.state()
        state.lifetime_started_keys.add("key-s")
        seen: list[str] = []

        def wait(workspace: Path, waited_run_id: str, timeout: object) -> bool:
            seen.append(
                run_registry.load_run_state_or_none(
                    run_registry.registry_root(workspace), waited_run_id
                )["status"]
            )
            return True

        dsl = runtime.WorkflowDsl(state, {})
        with mock.patch.object(runtime, "_wait_for_workflow_agent_run", wait):
            dsl._adopt_existing_agent_run(
                "key-s",
                scope="root/seq#0",
                phase=None,
                schema=None,
                prefer_assistant=False,
                timeout=1,
            )
        self.assertEqual(self.events("agent_adopt_skipped"), [])
        self.assertEqual(seen, ["creating_isolation"], "the sibling was sealed, not waited on")

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


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # nosec B603
    child.wait()
    return child.pid


class UnlaunchedSealRaceTests(unittest.TestCase):
    """A sealed launch never gains a child, and the seal waits for its launcher."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        self.run_id, self.alias = run_registry.register_run(self.registry_root, harness="codex")
        self.run_path = run_registry.run_directory(self.registry_root, self.run_id)

    def write_unlaunched(self, *, launcher_pid: int | None, started_at: datetime) -> None:
        stale = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat()
        record = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": self.run_id,
            "alias": self.alias,
            "status": "creating_isolation",
            "lastActivityAt": stale,
        }
        if launcher_pid is not None:
            record["launcherPid"] = launcher_pid
        run_registry.write_run_state(self.run_path, record)
        manifest = run_registry.load_run_manifest_or_none(self.registry_root, self.run_id) or {}
        manifest["startedAt"] = started_at.isoformat()
        runner.write_manifest(self.run_path, manifest)

    def record(self) -> dict:
        state = run_registry.load_run_state_or_none(self.registry_root, self.run_id)
        assert isinstance(state, dict)
        return state

    def ctx(self) -> runner.RunContext:
        return runner.RunContext(
            registry_root=self.registry_root,
            run_id=self.run_id,
            alias=self.alias,
            harness="codex",
            engine="codex",
            mode="work",
            model=None,
            source_cwd=str(self.workspace),
            execution_cwd=str(self.workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=datetime.now(UTC).isoformat(),
        )

    def drive_launch(self, launch: object) -> None:
        stdout_log = self.run_path / runner.STDOUT_LOG
        stderr_log = self.run_path / runner.STDERR_LOG
        run_registry.write_private_bytes(stdout_log, b"")
        run_registry.write_private_bytes(stderr_log, b"")
        files = runner.TrackedRunFiles(
            run_path=self.run_path,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            scratch_dir=None,
        )

        def capture(*args: object, **kwargs: object) -> None:
            # Reaching capture means a child launched and its pid was
            # published; record that instead of supervising the child.
            self.published = self.record()
            raise runner.RunnerLaunchError("capture_reached", "a child was launched")

        self.published: dict | None = None
        with (
            mock.patch.object(runner, "_launch_tracked_process", launch),
            mock.patch.object(runner, "_capture_tracked_process", capture),
        ):
            runner._run_single_tracked_attempt(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                str(self.workspace),
                files,
                self.ctx(),
                started=time.monotonic(),
                deadline=time.monotonic() + 30,
                stdin_text=None,
                env_overrides=None,
                scratch_dir=None,
                progress=False,
                progress_stderr=None,
                progress_initial_delay_sec=60,
                progress_interval_sec=60,
            )

    def test_a_sealed_creating_isolation_run_never_starts_a_child(self) -> None:
        self.write_unlaunched(launcher_pid=_dead_pid(), started_at=datetime.now(UTC))
        self.assertTrue(wait_cancel_commands.seal_unlaunched_run(self.registry_root, self.run_id))
        started: list[subprocess.Popen[bytes]] = []
        real_launch = runner._launch_tracked_process

        def spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            process = real_launch(*args, **kwargs)
            started.append(process)
            return process

        def reap() -> None:
            for process in started:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, 9)
                process.wait()

        self.addCleanup(reap)
        with self.assertRaises(runner.RunnerLaunchError) as raised:
            self.drive_launch(spy)
        self.assertIsNone(self.published, "a pid was published over the sealed record")
        self.assertEqual(started, [], "the sealed launch started a child")
        self.assertEqual(raised.exception.error, "cancelled_by_user")
        state = self.record()
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["cancelRequested"])
        self.assertNotIn("pid", state)

    def test_publication_refuses_a_record_sealed_after_admission(self) -> None:
        self.write_unlaunched(launcher_pid=None, started_at=datetime.now(UTC))
        run_registry.write_run_state(
            self.run_path,
            {**self.record(), "status": "running", "lastActivityAt": run_registry.utc_now_iso()},
        )
        started: list[subprocess.Popen[bytes]] = []
        real_launch = runner._launch_tracked_process

        def seal_during_launch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            process = real_launch(*args, **kwargs)
            started.append(process)
            # A writer that bypassed the registry lock seals between the
            # admission read and the pid publication.
            run_registry.write_run_state(
                self.run_path,
                {**self.record(), "status": "cancelled", "cancelRequested": True},
            )
            return process

        def reap() -> None:
            for process in started:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, 9)
                process.wait()

        self.addCleanup(reap)
        with self.assertRaises(runner.RunnerLaunchError) as raised:
            self.drive_launch(seal_during_launch)
        self.assertIsNone(self.published, "a pid was published over the sealed record")
        self.assertEqual(raised.exception.error, "cancelled_by_user")
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll(), "the started child was left running")
        state = self.record()
        self.assertEqual(state["status"], "cancelled")
        self.assertNotIn("pid", state)

    def test_the_seal_waits_for_a_live_launcher(self) -> None:
        self.write_unlaunched(launcher_pid=os.getpid(), started_at=datetime.now(UTC))
        self.assertFalse(wait_cancel_commands.seal_unlaunched_run(self.registry_root, self.run_id))
        self.assertEqual(self.record()["status"], "creating_isolation")

    def test_the_seal_proceeds_when_the_launcher_pid_was_reused(self) -> None:
        # This process is alive but started long after the run's startedAt, so
        # it cannot be the launcher that wrote the record.
        self.write_unlaunched(launcher_pid=os.getpid(), started_at=datetime(2000, 1, 1, tzinfo=UTC))
        self.assertTrue(wait_cancel_commands.seal_unlaunched_run(self.registry_root, self.run_id))
        self.assertEqual(self.record()["status"], "cancelled")

    def test_the_seal_proceeds_when_the_launcher_is_dead(self) -> None:
        self.write_unlaunched(launcher_pid=_dead_pid(), started_at=datetime.now(UTC))
        self.assertTrue(wait_cancel_commands.seal_unlaunched_run(self.registry_root, self.run_id))


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

    def test_a_timed_out_dry_run_keeps_the_workspace_cwd_while_it_runs(self) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        release = Path(elsewhere.name) / "release"
        self.addCleanup(release.touch)
        self.script.write_text(
            "import os, time\n"
            "deadline = time.monotonic() + 20\n"
            "while not os.path.exists(args['release']) and time.monotonic() < deadline:\n"
            "    time.sleep(0.02)\n"
            "with open('marker.txt', 'w') as handle:\n"
            "    handle.write('late write')\n"
            "return None\n",
            encoding="utf-8",
        )
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(elsewhere.name)
        with self.assertRaises(DelegateError) as raised:
            commands.emit_dry_run(
                wf_id=self.wf_id,
                root=self.root,
                script_path=self.script,
                workspace=self.workspace,
                config={"workflows": {"dryRunTimeoutSeconds": 1}},
                args_value={"release": str(release)},
                budget_total=None,
                json_mode=True,
                warnings=[],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(raised.exception.error, "dry_run_timeout")
        release.touch()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
            (Path(folder) / "marker.txt").exists() for folder in (self.workspace, elsewhere.name)
        ):
            time.sleep(0.02)
        self.assertTrue(
            (self.workspace / "marker.txt").exists(),
            "the abandoned script's relative write left the workspace",
        )
        self.assertFalse((Path(elsewhere.name) / "marker.txt").exists())


if __name__ == "__main__":
    unittest.main()
