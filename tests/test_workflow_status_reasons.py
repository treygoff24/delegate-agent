"""Workflow operator messages: why paused, what to run next, who timed out."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delegate_agent import run_registry
from delegate_agent.workflows import commands, registry, runtime

WF_ID = "wf_777777777777"


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / ".delegate" / "workflows" / WF_ID
        run_registry.ensure_private_dir(self.root)
        self.script = self.root / registry.SCRIPT_FILE
        self.script.write_text("return True\n", encoding="utf-8")

    def write_status(self, **fields: object) -> None:
        registry.write_json(
            self.root / registry.STATUS_FILE,
            {
                "wfId": WF_ID,
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0, "remaining": None},
                **fields,
            },
        )

    def journal(self, *rows: dict) -> None:
        for seq, row in enumerate(rows, start=1):
            registry.append_jsonl(
                self.root / registry.JOURNAL_FILE,
                {"seq": seq, "at": "2026-09-28T00:00:00Z", **row},
            )

    def state(self) -> runtime.WorkflowState:
        return runtime.WorkflowState(
            wf_id=WF_ID,
            workspace=self.workspace,
            root=self.root,
            script_path=self.script,
            config={},
            cli_argv=["delegate"],
            args=None,
            budget=runtime.Budget(None),
        )

    def events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == event_type
        ]

    def status_json(self) -> dict:
        out = io.StringIO()
        commands.emit_status(
            commands.WorkflowCommand("status", wf_id=WF_ID, json_mode=True),
            workspace=self.workspace,
            stdout=out,
        )
        return json.loads(out.getvalue())

    def approve(self) -> commands.DelegateError:
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
            self.assertRaises(commands.DelegateError) as raised,
        ):
            commands.emit_run(
                commands.WorkflowCommand("run", resume=WF_ID),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                approve_gate=True,
            )
        return raised.exception


class PausedStatusTests(_Fixture):
    def test_paused_status_says_why_with_gate_failure_and_rejection(self) -> None:
        self.journal(
            {
                "type": "agent_timeout",
                "key": "k1",
                "label": "verify",
                "item": "t3",
                "engine": "gemini",
                "timeout": 600,
                "nextEngine": "codex",
            },
            {
                "type": "agent_rejected",
                "key": "k2",
                "label": "review",
                "reason": "answer ignored the schema",
                "rejectedBy": "coordinator",
            },
            {
                "type": "gate",
                "key": "gate-1",
                "gateName": "task-7",
                "gateResultHash": "h1",
                "actions": ["retry", "accept"],
                "result": {"title": "Task 7 close", "assignee": "atlas"},
            },
        )
        self.write_status(status="paused", gateKey="gate-1", gateResultHash="h1")
        # Pretend the supervisor is gone the way a parked run is.
        pause = self.status_json()["pause"]
        self.assertEqual(pause["gateKey"], "gate-1")
        self.assertEqual(pause["gateName"], "task-7")
        self.assertEqual(pause["title"], "Task 7 close")
        self.assertEqual(pause["assignee"], "atlas")
        self.assertEqual(pause["actions"], ["retry", "accept"])
        self.assertIn("verify (item t3): timed out after 600s on gemini", pause["failure"])
        self.assertEqual(pause["rejection"]["reason"], "answer ignored the schema")
        self.assertIn("answer ignored the schema", pause["summary"])

    def test_text_status_prints_the_pause_summary(self) -> None:
        self.journal(
            {"type": "gate", "key": "g", "gateName": "ship", "gateResultHash": "h", "result": {}},
        )
        self.write_status(status="paused", gateKey="g", gateResultHash="h")
        out = io.StringIO()
        commands.emit_status(
            commands.WorkflowCommand("status", wf_id=WF_ID), workspace=self.workspace, stdout=out
        )
        self.assertIn("paused: paused at gate ship; actions: approve", out.getvalue())
        self.assertIn("gate: g gateName=ship", out.getvalue())
        self.assertIn(f"workflow approve {WF_ID}", out.getvalue())

    def test_text_status_shows_the_question_title_and_assignee(self) -> None:
        self.journal(
            {
                "type": "gate",
                "key": "g",
                "gateName": "ship",
                "gateResultHash": "h",
                "result": {"title": "Ship it?", "assignee": "atlas"},
            },
        )
        self.write_status(status="paused", gateKey="g", gateResultHash="h")
        out = io.StringIO()
        commands.emit_status(
            commands.WorkflowCommand("status", wf_id=WF_ID), workspace=self.workspace, stdout=out
        )
        self.assertIn("gate: g gateName=ship title=Ship it? assignee=atlas", out.getvalue())

    def test_next_command_follows_the_gates_declared_actions(self) -> None:
        self.journal(
            {
                "type": "gate",
                "key": "g",
                "gateName": "task-7",
                "gateResultHash": "h",
                "actions": ["retry", "accept"],
                "result": {},
            },
        )
        self.write_status(status="paused", gateKey="g", gateResultHash="h")
        pause = self.status_json()["pause"]
        self.assertIn("--gate task-7 --action retry", pause["next"])
        self.assertNotEqual(pause["next"].split()[-1], WF_ID, "a bare approve is refused")
        self.assertTrue(any("--action accept" in c for c in pause["nextActions"]))
        self.assertFalse(any(c.endswith(f"approve {WF_ID}") for c in pause["nextActions"]))

    def test_non_paused_status_has_no_pause_block(self) -> None:
        self.journal({"type": "phase", "title": "x"})
        self.write_status(status="succeeded")
        self.assertNotIn("pause", self.status_json())

    def test_status_lists_recent_agent_timeouts(self) -> None:
        self.journal(
            {
                "type": "agent_timeout",
                "key": "k",
                "label": "verifier",
                "item": "7",
                "engine": "gemini",
                "timeout": 3600,
                "nextEngine": "codex",
            },
        )
        self.write_status(status="succeeded")
        (row,) = self.status_json()["timeouts"]
        self.assertEqual(
            (row["label"], row["item"], row["engine"], row["timeout"], row["nextEngine"]),
            ("verifier", "7", "gemini", 3600, "codex"),
        )


class DryRunResumeTests(_Fixture):
    def snapshot(self) -> dict[str, bytes]:
        base = self.workspace / ".delegate"
        return {
            str(path.relative_to(base)): path.read_bytes()
            for path in sorted(base.rglob("*"))
            if path.is_file() and path.name not in {registry.JOURNAL_FILE, registry.LOCK_FILE}
        }

    def test_dry_run_resume_of_a_paused_workflow_changes_only_the_journal(self) -> None:
        self.journal(
            {
                "type": "gate",
                "key": "g",
                "gateName": "ship",
                "gateResultHash": "h",
                "result": {},
            },
        )
        self.write_status(status="paused", gateKey="g", gateResultHash="h", lastSeq=1)
        status_before = (self.root / registry.STATUS_FILE).read_bytes()
        before = self.snapshot()
        pin = SimpleNamespace(
            cli_argv=["delegate"], environment={}, profile_identity={"current": True}
        )
        with (
            mock.patch.object(commands.workflow_pinning, "load_pin", return_value=pin),
            mock.patch.object(runtime, "detach_supervisor"),
        ):
            commands.emit_run(
                commands.WorkflowCommand("run", resume=WF_ID, dry_run=True, json_mode=True),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual((self.root / registry.STATUS_FILE).read_bytes(), status_before)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.status_json()["status"], "paused")

    def test_dry_run_resume_with_a_budget_leaves_status_alone(self) -> None:
        self.write_status(status="paused", gateKey="g", gateResultHash="h")
        status_before = (self.root / registry.STATUS_FILE).read_bytes()
        pin = SimpleNamespace(
            cli_argv=["delegate"], environment={}, profile_identity={"current": True}
        )
        with (
            mock.patch.object(commands.workflow_pinning, "load_pin", return_value=pin),
            mock.patch.object(runtime, "detach_supervisor"),
        ):
            commands.emit_run(
                commands.WorkflowCommand(
                    "run", resume=WF_ID, dry_run=True, budget=3, json_mode=True
                ),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual((self.root / registry.STATUS_FILE).read_bytes(), status_before)

    def test_stale_scope_would_cancel_row_leaves_status_alone(self) -> None:
        self.write_status(status="paused", gateKey="g", gateResultHash="h")
        status_before = (self.root / registry.STATUS_FILE).read_bytes()
        state = self.state()
        state.dry_run = True
        state.started_scopes["old"] = "root/seq#0"
        state.started_without_result.add("old")
        state.cancel_stale_scope_children("root/seq#0", "new")
        self.assertEqual(len(self.events("agent_stale_scope_would_cancel")), 1)
        self.assertEqual((self.root / registry.STATUS_FILE).read_bytes(), status_before)


class ApproveRefusalTests(_Fixture):
    def test_approve_on_a_failed_workflow_points_at_resume(self) -> None:
        self.journal(
            {"type": "agent_failed", "key": "k", "label": "build", "error": "exit 2"},
            {"type": "workflow_failed", "error": "boom"},
        )
        self.write_status(status="failed", ok=False)
        error = self.approve()
        self.assertEqual(error.error, "workflow_not_gated")
        self.assertIn(f"delegate workflow resume {WF_ID}", str(error))
        self.assertIn("is failed", str(error))

    def test_named_gate_on_a_failed_workflow_no_longer_says_only_none(self) -> None:
        self.write_status(status="failed", ok=False)
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
            self.assertRaises(commands.DelegateError) as raised,
        ):
            commands.emit_run(
                commands.WorkflowCommand("run", resume=WF_ID),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                approve_gate=True,
                gate_choice=commands.GateChoice(gate="task-1"),
            )
        self.assertEqual(raised.exception.error, "workflow_gate_not_found")
        self.assertIn("pending gates: none", str(raised.exception))
        self.assertIn(f"delegate workflow resume {WF_ID}", str(raised.exception))

    def test_approve_on_a_running_workflow_says_the_gate_is_not_open_yet(self) -> None:
        self.write_status(status="running")
        fd = registry.acquire_workflow_lock(self.root)
        try:
            error = self.approve()
        finally:
            os.close(fd)
        self.assertEqual(error.error, "workflow_locked")
        self.assertIn("still running", str(error))
        self.assertIn("gate is not open yet", str(error))
        self.assertIn(f"delegate workflow wait {WF_ID}", str(error))

    def test_a_locked_plain_resume_keeps_its_bare_message(self) -> None:
        self.write_status(status="running")
        fd = registry.acquire_workflow_lock(self.root)
        try:
            with self.assertRaises(commands.DelegateError) as raised:
                commands._acquire_workflow_lock(self.root, WF_ID)
        finally:
            os.close(fd)
        self.assertEqual(str(raised.exception), f"Workflow is already running: {WF_ID}")


class AgentRowIdentityTests(_Fixture):
    def test_finished_row_carries_label_and_item_from_the_started_row(self) -> None:
        state = self.state()
        scope = "root/soft-park/task-7/seq#0"
        state.append_event("agent_started", key="k", scope=scope, label="verify")
        state.append_event("agent_finished", key="k", scope=scope, result="ok")
        finished = self.events("agent_finished")[-1]
        self.assertEqual(finished["label"], "verify")
        self.assertEqual(finished["item"], "task-7")

    def test_unlabelled_row_carries_null_label_and_positional_item(self) -> None:
        state = self.state()
        state.append_event(
            "agent_attempt_failed", key="k", engine="codex", scope="root/seq#1/item#3/seq#0"
        )
        row = self.events("agent_attempt_failed")[-1]
        self.assertIn("label", row)
        self.assertIsNone(row["label"])
        self.assertEqual(row["item"], "3")

    def test_row_outside_any_item_has_null_item(self) -> None:
        state = self.state()
        state.append_event("agent_finished", key="k", scope="root/seq#0", result=1)
        row = self.events("agent_finished")[-1]
        self.assertIn("item", row)
        self.assertIsNone(row["item"])

    def test_timeout_row_carries_label_item_and_next_seat(self) -> None:
        dsl = runtime.WorkflowDsl(self.state(), {"defaults": {"engine": "gemini"}})
        dsl.state.thread_local.next_engine = "codex"
        with (
            mock.patch.object(
                runtime,
                "_run_child_command_for_state",
                side_effect=subprocess.TimeoutExpired(["delegate"], 1),
            ),
            mock.patch.object(runtime, "cancel_workflow_agent_child"),
            mock.patch.object(runtime, "_workflow_agent_run_result_metadata", return_value=None),
            dsl.state.scope("root/soft-park/t9"),
        ):
            dsl._run_delegate(
                "gemini",
                "p",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                isolation=None,
                passthrough=False,
                timeout=1,
                output_schema=None,
                prefer_assistant=False,
                workflow_agent_key="tk",
                label=None,
            )
        (row,) = self.events("agent_timeout")
        self.assertEqual((row["item"], row["nextEngine"], row["engine"]), ("t9", "codex", "gemini"))
        self.assertIn("label", row)

    def test_explicit_null_label_does_not_inherit_a_stale_label_for_the_key(self) -> None:
        state = self.state()
        # A replayed journal mapped an old label to this key.
        state.label_keys["old-label"] = "k"
        state.append_event("agent_started", key="k", scope="root/seq#0", label=None)
        self.assertIsNone(self.events("agent_started")[-1]["label"])

    def test_unlabelled_call_reusing_a_key_keeps_null_on_its_finished_row(self) -> None:
        dsl = runtime.WorkflowDsl(self.state(), {"defaults": {"engine": "codex"}})
        with mock.patch.object(
            runtime.WorkflowDsl, "_run_agent_attempts", lambda *_a, **_k: "answer"
        ):
            dsl.agent("p")
        key = self.events("agent_started")[-1]["key"]
        # A replayed journal had mapped an old label to this key.
        dsl.state.label_keys["stale"] = key
        dsl.state.append_event("agent_finished", key=key, scope="root/seq#9", result=1)
        self.assertIsNone(self.events("agent_finished")[-1]["label"])

    def test_started_row_records_the_timeout_and_deadline(self) -> None:
        dsl = runtime.WorkflowDsl(self.state(), {"defaults": {"engine": "codex"}})
        seen: list[str | None] = []

        def attempts(_self: object, engine: str, prompt: str, **_kw: object) -> str:
            seen.append(getattr(dsl.state.thread_local, "next_engine", "unset"))
            return "answer"

        with mock.patch.object(runtime.WorkflowDsl, "_run_agent_attempts", attempts):
            dsl.agent("p", engine=["codex", "claude"], timeout=45, label="lbl")
        started = self.events("agent_started")[-1]
        self.assertEqual(started["timeout"], 45)
        self.assertRegex(started["deadlineAt"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(started["label"], "lbl")
        self.assertIn("item", started)
        self.assertEqual(seen, ["claude"])
        finished = self.events("agent_finished")[-1]
        self.assertEqual(finished["label"], "lbl")


if __name__ == "__main__":
    unittest.main()
