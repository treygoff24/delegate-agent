from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delegate_agent.workflows import commands, registry, runtime


class GateApprovalBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.wf_id = "wf_111111111111"
        self.root = registry.ensure_workflow_dir(self.workspace, self.wf_id)
        self.script = self.root / registry.SCRIPT_FILE
        self.script.write_text("return True\n", encoding="utf-8")
        (self.root / "child.py").write_text(
            'meta = {"name": "varying-child"}\nreturn {"ok": False}\n',
            encoding="utf-8",
        )
        registry.write_json(
            self.root / registry.STATUS_FILE,
            {
                "wfId": self.wf_id,
                "status": "created",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )

    def _resume(self, *, approve_gate: bool = False, detach_error: Exception | None = None) -> int:
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
            mock.patch.object(runtime, "detach_supervisor", side_effect=detach_error),
        ):
            return commands.emit_run(
                commands.WorkflowCommand("run", resume=self.wf_id),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                approve_gate=approve_gate,
            )

    def _state(self) -> runtime.WorkflowState:
        return runtime.WorkflowState(
            wf_id=self.wf_id,
            workspace=self.workspace,
            root=self.root,
            script_path=self.script,
            config={},
            cli_argv=["delegate"],
            args=None,
            budget=runtime.Budget(None),
        )

    def _run_gated_child(self, result: object) -> object:
        dsl = runtime.WorkflowDsl(self._state(), {})
        with mock.patch.object(runtime, "execute_workflow", return_value=result):
            return dsl.workflow(
                "child.py",
                args={"stable": "args"},
                gate="on-failure",
            )

    def _park(self, result: object) -> runtime.GateExit:
        with self.assertRaises(runtime.GateExit) as raised:
            self._run_gated_child(result)
        return raised.exception

    def _resume_auto_approve(self) -> None:
        self.assertEqual(self._resume(), 0)

    def test_approve_during_gate_drain_does_not_change_projection(self) -> None:
        self._park({"ok": False, "reason": "draining"})
        registry.write_json(
            self.root / registry.STATUS_FILE,
            {
                "wfId": self.wf_id,
                "status": "running",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0},
            },
        )
        before = (self.root / registry.STATUS_FILE).read_bytes()
        fd = registry.acquire_workflow_lock(self.root)
        try:
            with self.assertRaises(commands.DelegateError) as raised:
                self._resume(approve_gate=True)
            self.assertEqual(raised.exception.error, "workflow_locked")
            self.assertEqual((self.root / registry.STATUS_FILE).read_bytes(), before)
            self.assertFalse((self.root / registry.APPROVAL_FILE).exists())
        finally:
            os.close(fd)

    def test_approve_repairs_a_clobbered_gate_projection_on_disk(self) -> None:
        parked = self._park({"ok": False, "reason": "clobbered"})
        # status.json is a projection and can be lost or overwritten while a
        # supervisor drains a gate. The journal is authoritative.
        registry.write_json(
            self.root / registry.STATUS_FILE,
            {
                "wfId": self.wf_id,
                "status": "running",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0},
            },
        )
        with self.assertRaises(OSError):
            self._resume(approve_gate=True, detach_error=OSError("injected"))
        # The repair is durable: a resume that fails afterwards rolls back to
        # the recovered projection, not to the clobbered one.
        status = registry.read_json(self.root / registry.STATUS_FILE) or {}
        self.assertEqual(status.get("status"), "paused")
        self.assertIs(status.get("ok"), True)
        self.assertEqual(status.get("gateKey"), parked.gate_key)
        self.assertEqual(status.get("gateResultHash"), parked.result_hash)
        self.assertIsInstance(status.get("updatedAt"), str)

    def test_gate_without_result_identity_is_not_approvable(self) -> None:
        registry.append_jsonl(
            self.root / registry.JOURNAL_FILE,
            {
                "seq": 1,
                "type": "gate",
                "at": "2026-01-01T00:00:00Z",
                "key": "unbound-gate",
                "child": "child.py",
                "result": {"ok": False},
            },
        )
        self.assertIsNone(commands._latest_unapproved_gate_event(self.root))
        self.assertFalse(registry.approval_allows(self.root, "unbound-gate", None))
        with self.assertRaisesRegex(ValueError, "result hash"):
            registry.record_approval(self.root, "unbound-gate", None)

    def test_approve_reads_journal_and_approval_once(self) -> None:
        self._park({"ok": False, "reason": "single-fold"})
        with (
            mock.patch.object(registry, "iter_journal", wraps=registry.iter_journal) as journal,
            mock.patch.object(
                commands.private_io,
                "read_private_text_bounded",
                wraps=commands.private_io.read_private_text_bounded,
            ) as approval,
        ):
            self.assertEqual(self._resume(approve_gate=True), 0)
        self.assertEqual(journal.call_count, 1)
        approval_reads = [
            call
            for call in approval.call_args_list
            if call.args and call.args[0] == self.root / registry.APPROVAL_FILE
        ]
        self.assertEqual(len(approval_reads), 1)

    def test_checkpoint_key_stable_but_changed_evidence_needs_new_approval(self) -> None:
        first = self._park({"ok": False, "reason": "first"})
        repeated = self._park({"ok": False, "reason": "first"})
        self.assertEqual(first.gate_key, repeated.gate_key)
        self.assertEqual(first.result_hash, repeated.result_hash)
        registry.record_approval(self.root, first.gate_key, first.result_hash)
        changed = self._park({"ok": False, "reason": "changed"})
        self.assertEqual(first.gate_key, changed.gate_key)
        self.assertNotEqual(first.result_hash, changed.result_hash)
        self.assertFalse(registry.approval_allows(self.root, changed.gate_key, changed.result_hash))

    def test_same_red_replay_uses_its_result_bound_approval(self) -> None:
        red = {"ok": False, "reason": "same"}
        parked = self._park(red)
        self._resume_auto_approve()

        approval = registry.read_json(self.root / registry.APPROVAL_FILE) or {}
        self.assertIn(
            {"key": parked.gate_key, "resultHash": parked.result_hash},
            approval.get("approvedResults", []),
        )
        before = registry.iter_journal(self.root / registry.JOURNAL_FILE)
        self.assertEqual(self._run_gated_child(red), red)
        after = registry.iter_journal(self.root / registry.JOURNAL_FILE)
        self.assertEqual(after, before)

    def test_distinct_red_with_same_gate_key_reparks_and_reprojects(self) -> None:
        first = self._park({"ok": False, "reason": "first"})
        self._resume_auto_approve()

        second = self._park({"ok": False, "reason": "second"})
        self.assertEqual(second.gate_key, first.gate_key)
        self.assertNotEqual(second.result_hash, first.result_hash)
        events = [
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "gate"
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].get("gateResultHash"), second.result_hash)
        status = registry.read_json(self.root / registry.STATUS_FILE) or {}
        self.assertEqual(status.get("gateResult"), {"ok": False, "reason": "second"})
        self.assertEqual(status.get("gateResultHash"), second.result_hash)
        self.assertEqual(commands._latest_unapproved_gate_event(self.root), events[-1])

    def test_legacy_key_only_approval_does_not_authorize_a_result(self) -> None:
        approval_path = self.root / registry.APPROVAL_FILE
        original = b'{\n  "approved": true,\n  "approvedKeys": ["legacy"]\n}\n'
        approval_path.write_bytes(original)

        self.assertFalse(registry.approval_allows(self.root, "legacy", "any-result-hash"))
        self.assertEqual(approval_path.read_bytes(), original)

    def test_record_approval_merges_approved_results(self) -> None:
        registry.record_approval(self.root, "gate-one", "hash-one")
        payload = registry.record_approval(self.root, "gate-two", "hash-two")

        self.assertEqual(
            payload.get("approvedResults"),
            [
                {"key": "gate-one", "resultHash": "hash-one"},
                {"key": "gate-two", "resultHash": "hash-two"},
            ],
        )
        self.assertTrue(registry.approval_allows(self.root, "gate-one", "hash-one"))
        self.assertFalse(registry.approval_allows(self.root, "gate-one", "hash-two"))


if __name__ == "__main__":
    unittest.main()
