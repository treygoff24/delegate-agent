from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry
from delegate_agent.workflows import registry, runtime


class ChildAttemptOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / ".delegate" / "workflows" / "wf_666666666666"
        run_registry.ensure_private_dir(self.root)
        self.script = self.root / registry.SCRIPT_FILE
        self.script.write_text("return True\n", encoding="utf-8")
        registry.write_json(
            self.root / registry.STATUS_FILE,
            {
                "wfId": "wf_666666666666",
                "status": "created",
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )

    def _dsl(self) -> runtime.WorkflowDsl:
        state = runtime.WorkflowState(
            wf_id="wf_666666666666",
            workspace=self.workspace,
            root=self.root,
            script_path=self.script,
            config={},
            cli_argv=["delegate"],
            args=None,
            budget=runtime.Budget(None),
        )
        return runtime.WorkflowDsl(state, {"defaults": {"engine": "codex"}})

    def _retry_value(
        self,
        first: runtime._DelegateChildResult,
    ) -> tuple[object, list[mock._Call]]:
        second = runtime._DelegateChildResult(
            text=json.dumps({"ok": True}),
            run_id="del_20260827T040000Z_retry2",
            execution_cwd="/tmp/retry-worktree",
            session_id=None,
        )
        dsl = self._dsl()
        calls: list[mock._Call] = []

        def run(*args: object, **kwargs: object) -> runtime._DelegateChildResult:
            calls.append(mock.call(*args, **kwargs))
            return first if len(calls) == 1 else second

        with (
            mock.patch.object(dsl, "_run_delegate", side_effect=run),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            value = dsl._run_structured_or_text(
                "codex",
                "retry",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                isolation="worktree",
                passthrough=False,
                timeout=1,
                retries=1,
                key="workflow-key",
            )
        return value, calls

    def test_timeout_outcome_links_retry_to_failed_run(self) -> None:
        first = runtime._DelegateChildResult(
            text=None,
            run_id="del_20260827T040000Z_timeout1",
            execution_cwd="/tmp/failed-worktree",
            session_id=None,
            outcome=runtime.ChildAttemptOutcome(
                run_id="del_20260827T040000Z_timeout1",
                failure_reason="timeout",
                branch="delegate/codex-timeout",
                worktree="/tmp/failed-worktree",
            ),
        )
        value, calls = self._retry_value(first)
        self.assertEqual(value, {"ok": True})
        self.assertEqual(calls[1].kwargs["structured_retry_run_id"], first.run_id)
        event = next(
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "agent_structured_retry"
        )
        outcome = event["childAttemptOutcome"]
        self.assertEqual(outcome["failureReason"], "timeout")
        self.assertEqual(outcome["runId"], first.run_id)
        self.assertEqual(outcome["branch"], "delegate/codex-timeout")

    def test_output_cap_outcome_links_retry_to_failed_run(self) -> None:
        first = runtime._DelegateChildResult(
            text=None,
            run_id="del_20260827T040000Z_output1",
            execution_cwd="/tmp/failed-worktree",
            session_id=None,
            outcome=runtime.ChildAttemptOutcome(
                run_id="del_20260827T040000Z_output1",
                failure_reason="output_cap",
                branch="delegate/codex-output",
                worktree="/tmp/failed-worktree",
                cleanup_ownership={"isolatedWorkspace": "/tmp/failed-worktree"},
            ),
        )
        value, calls = self._retry_value(first)
        self.assertEqual(value, {"ok": True})
        self.assertEqual(calls[1].kwargs["structured_retry_run_id"], first.run_id)
        event = next(
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "agent_structured_retry"
        )
        self.assertEqual(event["childAttemptOutcome"]["failureReason"], "output_cap")
        self.assertEqual(
            event["childAttemptOutcome"]["cleanupOwnership"],
            {"isolatedWorkspace": "/tmp/failed-worktree"},
        )

    def test_text_retry_rejects_changed_workspace_cleanup_metadata(self) -> None:
        first_cleanup = {
            "gitRoot": None,
            "isolatedWorkspace": "/tmp/first-retry-worktree",
            "tempBase": "/tmp",
            "sourceRoot": str(self.workspace),
        }
        second_cleanup = {
            "gitRoot": None,
            "isolatedWorkspace": "/tmp/second-retry-worktree",
            "tempBase": "/tmp",
            "sourceRoot": str(self.workspace),
        }
        first = runtime._DelegateChildResult(
            text=None,
            run_id="del_20260827T040000Z_text1",
            execution_cwd="/tmp/first-retry-worktree",
            session_id=None,
            workspace_cleanup=first_cleanup,
            outcome=runtime.ChildAttemptOutcome(
                run_id="del_20260827T040000Z_text1",
                failure_reason="timeout",
                cleanup_ownership=first_cleanup,
            ),
        )
        second = runtime._DelegateChildResult(
            text=None,
            run_id="del_20260827T040000Z_text2",
            execution_cwd="/tmp/second-retry-worktree",
            session_id=None,
            workspace_cleanup=second_cleanup,
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", side_effect=[first, second]),
            mock.patch.object(dsl, "_release_structured_retry_worktree") as release,
            mock.patch.object(runtime, "_cleanup_structured_retry_workspace") as cleanup,
            self.assertRaisesRegex(RuntimeError, "workspace cleanup metadata changed"),
        ):
            dsl._run_structured_or_text(
                "codex",
                "retry",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=None,
                isolation="worktree",
                passthrough=False,
                timeout=1,
                retries=1,
                key="workflow-key",
            )

        cleanup.assert_has_calls([mock.call(second_cleanup), mock.call(first_cleanup)])
        release.assert_called_once_with(first.run_id)


if __name__ == "__main__":
    unittest.main()
