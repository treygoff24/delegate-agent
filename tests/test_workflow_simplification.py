from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent.workflows import registry, runtime


class WorkflowSimplificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.sequence = 0

    def state(self, script_body: str = "return True\n") -> runtime.WorkflowState:
        self.sequence += 1
        wf_id = f"wf_{self.sequence:012x}"
        root = registry.ensure_workflow_dir(self.workspace, wf_id)
        script = root / registry.SCRIPT_FILE
        script.write_text(script_body, encoding="utf-8")
        registry.write_status(
            root,
            {
                "wfId": wf_id,
                "status": "created",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        return runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script,
            config={},
            cli_argv=["delegate"],
            args=None,
            budget=runtime.Budget(None),
        )

    def test_scope_restores_exact_parent_state_after_exception(self) -> None:
        state = self.state()
        counters: dict[str, int] = {}
        state.thread_local.scope = "root/parallel@0/thunk#0"
        state.thread_local.counters = counters
        with self.assertRaisesRegex(RuntimeError, "stop"), state.scope("child"):
            state.next_agent_path()
            raise RuntimeError("stop")
        self.assertEqual(state.current_scope(), "root/parallel@0/thunk#0")
        self.assertIs(state.thread_local.counters, counters)
        self.assertEqual(counters, {})

    def test_dry_run_rejection_updates_the_simulated_tombstone(self) -> None:
        state = self.state()
        state.dry_run = True
        state.append_event("agent_started", key="simulated-key", scope="root/seq#0", label="draft")
        self.assertEqual(state.reject_agent("draft", "retry the preview"), "simulated-key")
        self.assertIn("simulated-key", state.tombstoned_keys)

    def test_nested_workflow_reuses_state_without_replaying_journal(self) -> None:
        state = self.state()
        child = state.script_path.parent / "child.py"
        child.write_text(
            'meta = {"name": "child"}\nreturn agent(args["prompt"])\n',
            encoding="utf-8",
        )
        dsl = runtime.WorkflowDsl(state, {})
        with (
            state.scope("root/parallel@0/thunk#0"),
            mock.patch.object(registry, "iter_journal", side_effect=AssertionError("replayed")),
            mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts", return_value="done"),
        ):
            self.assertEqual(dsl.workflow("child.py", {"prompt": "nested"}), "done")
            self.assertEqual(state.current_scope(), "root/parallel@0/thunk#0")
        started = next(
            event
            for event in registry.iter_journal(state.journal_path)
            if event.get("type") == "agent_started"
        )
        self.assertEqual(
            started["scope"],
            "root/parallel@0/thunk#0/wf:child@0/seq#0",
        )

    def test_followup_reuses_exhausted_agent_adoption_lifecycle(self) -> None:
        state = self.state()
        state.record_completed_child("prior", "run-prior", "codex", True)
        key = runtime._followup_key("root/seq#0", "prior", "again", {})
        state.replay_keys.add(key)
        state.replay[key] = None
        state.exhausted_keys.add(key)
        dsl = runtime.WorkflowDsl(state, {})
        with (
            mock.patch.object(dsl, "_adopt_existing_agent_run", return_value="recovered") as adopt,
            mock.patch.object(dsl, "_run_followup_attempts") as launch,
        ):
            self.assertEqual(dsl.followup("prior", "again"), "recovered")
        adopt.assert_called_once()
        launch.assert_not_called()
        self.assertNotIn(key, state.exhausted_keys)

    def test_started_after_tombstone_is_adopted_instead_of_relaunched(self) -> None:
        opts = {
            "engine": "codex",
            "mode": "safe",
            "model": None,
            "effort": None,
            "fast": None,
            "schema": None,
            "isolation": None,
            "personaDigest": None,
        }
        base_key = runtime._agent_key("root/seq#0", "race", opts)
        state = self.state()
        registry.append_jsonl(
            state.journal_path,
            {"seq": 1, "type": "agent_rejected", "key": base_key, "reason": "retry"},
        )
        state = runtime.WorkflowState(
            wf_id=state.wf_id,
            workspace=state.workspace,
            root=state.root,
            script_path=state.script_path,
            config={},
            cli_argv=["delegate"],
            args=None,
            budget=runtime.Budget(None),
            replay_attempt=1,
        )
        dsl = runtime.WorkflowDsl(state, {"defaults": {"engine": "codex"}})
        decision_made = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def pause_before_start(_scope: str, _key: str) -> None:
            decision_made.set()
            release.wait(2)

        def run() -> None:
            try:
                dsl.agent("race", label="draft")
            except BaseException as exc:
                errors.append(exc)

        with (
            mock.patch.object(state, "cancel_stale_scope_children", side_effect=pause_before_start),
            mock.patch.object(
                dsl,
                "_run_agent_attempts",
                side_effect=runtime.SupervisorWatchdogExit("crash"),
            ),
        ):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(decision_made.wait(2))
            retry_key = state.label_keys["draft"]
            state.reject_agent("draft", "raced before start")
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], runtime.SupervisorWatchdogExit)

        replayed = runtime.WorkflowState(
            wf_id=state.wf_id,
            workspace=state.workspace,
            root=state.root,
            script_path=state.script_path,
            config={},
            cli_argv=["delegate"],
            args=None,
            budget=runtime.Budget(None),
            replay_attempt=1,
        )
        self.assertIn(retry_key, replayed.started_after_tombstone)
        resumed = runtime.WorkflowDsl(replayed, {"defaults": {"engine": "codex"}})
        with (
            mock.patch.object(
                resumed, "_adopt_existing_agent_run", return_value="adopted"
            ) as adopt,
            mock.patch.object(resumed, "_run_agent_attempts") as launch,
        ):
            self.assertEqual(resumed.agent("race", label="draft"), "adopted")
        adopt.assert_called_once()
        launch.assert_not_called()

    def test_gate_operation_reads_journal_and_approval_once(self) -> None:
        state = self.state()
        first = state.park_gate("first", child=None, result={"ok": False, "n": 1})
        registry.record_approval(state.root, "first", first.result_hash)
        state.gate_state["stop_admitting"] = False
        second_hash = runtime._gate_result_hash({"ok": False, "n": 2})
        registry.append_jsonl(
            state.journal_path,
            {
                "seq": state.sequence + 1,
                "type": "gate",
                "key": "second",
                "result": {"ok": False, "n": 2},
                "gateResultHash": second_hash,
            },
        )
        with (
            mock.patch.object(registry, "iter_journal", wraps=registry.iter_journal) as journal,
            mock.patch.object(registry, "read_json", wraps=registry.read_json) as read_json,
        ):
            pending = state.latest_gate_event(unapproved_only=True)
        self.assertEqual(pending["key"] if pending else None, "second")
        self.assertEqual(journal.call_count, 1)
        approval_reads = [
            call
            for call in read_json.call_args_list
            if call.args and call.args[0] == state.root / registry.APPROVAL_FILE
        ]
        self.assertEqual(len(approval_reads), 1)


if __name__ == "__main__":
    unittest.main()
