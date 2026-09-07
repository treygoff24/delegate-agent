from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry
from delegate_agent.workflows import registry, runtime


class StructuredSchemaFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / ".delegate" / "workflows" / "wf_lane_s"
        run_registry.ensure_private_dir(self.root)
        script = self.root / registry.SCRIPT_FILE
        script.write_text("return True\n", encoding="utf-8")
        registry.write_json(
            self.root / registry.STATUS_FILE,
            {
                "wfId": "wf_lane_s",
                "status": "created",
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        state = runtime.WorkflowState(
            wf_id="wf_lane_s",
            workspace=self.workspace,
            root=self.root,
            script_path=script,
            config={},
            cli_argv=["delegate"],
            args=None,
            budget=runtime.Budget(None),
        )
        self.dsl = runtime.WorkflowDsl(state, {"defaults": {}})

    def _journal(self) -> list[dict[str, object]]:
        return list(registry.iter_journal(self.root / registry.JOURNAL_FILE))

    def test_ineligible_claude_schema_uses_prompt_path_and_journals_reason(self) -> None:
        schema = {"type": "array", "items": {"type": "string"}}
        child = runtime._DelegateChildResult(
            text='["ok"]', run_id="child-1", execution_cwd=None, session_id=None
        )
        with mock.patch.object(self.dsl, "_run_delegate", return_value=child) as run:
            result = self.dsl._run_structured_or_text(
                "claude",
                "return a list",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation="none",
                passthrough=False,
                timeout=None,
                retries=0,
                key="array-root",
            )

        self.assertEqual(result, ["ok"])
        self.assertIsNone(run.call_args.kwargs["output_schema"])
        self.assertIn(json.dumps(schema, sort_keys=True), run.call_args.args[1])
        event = next(row for row in self._journal() if row["type"] == "agent_schema_prompt_path")
        self.assertEqual(event["key"], "array-root")
        self.assertIn("object", str(event["reason"]))

    def test_failed_native_attempt_demotes_and_embeds_schema_on_resume(self) -> None:
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        failed = runtime._DelegateChildResult(
            text=None,
            run_id="child-1",
            execution_cwd=None,
            session_id="session-1",
            outcome=runtime.ChildAttemptOutcome(
                run_id="child-1",
                failure_reason="nonzero_exit",
                session_id="session-1",
            ),
        )
        recovered = runtime._DelegateChildResult(
            text='{"ok": true}',
            run_id="child-2",
            execution_cwd=None,
            session_id="session-1",
        )
        with mock.patch.object(self.dsl, "_run_delegate", side_effect=[failed, recovered]) as run:
            result = self.dsl._run_structured_or_text(
                "claude",
                "return an object",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation="none",
                passthrough=False,
                timeout=None,
                retries=1,
                key="native-failure",
            )

        self.assertEqual(result, {"ok": True})
        self.assertIsNotNone(run.call_args_list[0].kwargs["output_schema"])
        self.assertIsNone(run.call_args_list[1].kwargs["output_schema"])
        retry_prompt = run.call_args_list[1].args[1]
        self.assertIn("Re-emit the StructuredOutput now", retry_prompt)
        self.assertIn(json.dumps(schema, sort_keys=True), retry_prompt)
        demotion = next(row for row in self._journal() if row["type"] == "agent_schema_demoted")
        self.assertEqual(demotion["key"], "native-failure")
        self.assertEqual(demotion["reason"], "nonzero_exit")
