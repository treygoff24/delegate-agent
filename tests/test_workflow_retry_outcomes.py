from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry
from delegate_agent.workflows import registry, runtime
from delegate_agent.workflows import schema as workflow_schema


class ChildAttemptOutcomeTests(unittest.TestCase):
    def test_provider_terminal_reasons_keep_explicit_workflow_retry_aliases(self) -> None:
        expected = {
            "provider_cancelled": "stall",
            "provider_refusal": "nonzero_exit",
            "provider_max_turns": "nonzero_exit",
        }
        for reason, normalized in expected.items():
            with self.subTest(reason=reason):
                self.assertEqual(
                    runtime._normalize_child_failure_reason(reason, default="structured"),
                    normalized,
                )

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

    def test_timeout_event_keeps_child_identity_fields(self) -> None:
        dsl = self._dsl()
        with (
            mock.patch.object(
                runtime,
                "_run_child_command_for_state",
                side_effect=subprocess.TimeoutExpired(["delegate"], 1),
            ),
            mock.patch.object(runtime, "cancel_workflow_agent_child"),
            mock.patch.object(runtime, "_workflow_agent_run_result_metadata", return_value=None),
        ):
            result = dsl._run_delegate(
                "codex",
                "timeout",
                mode="safe",
                model="model-id",
                effort=None,
                fast=None,
                isolation=None,
                passthrough=False,
                timeout=1,
                output_schema=None,
                prefer_assistant=False,
                workflow_agent_key="timeout-key",
                label="timeout-label",
            )
        self.assertIsNone(result)
        event = next(
            event
            for event in runtime.registry.iter_journal(dsl.state.journal_path)
            if event.get("type") == "agent_timeout"
        )
        self.assertEqual(
            {event["key"], event["label"], event["model"]},
            {"timeout-key", "timeout-label", "model-id"},
        )
        self.assertEqual(event["scope"], "root")

    def test_workflow_notify_events_cover_paused_failed_and_succeeded_states(self) -> None:
        scenarios = {
            "succeeded": "meta = {'name': 'notify-success'}\nreturn True\n",
            "failed": "meta = {'name': 'notify-failure'}\nraise RuntimeError('boom')\n",
            "paused": (
                "meta = {'name': 'notify-paused'}\nreturn workflow('child.py', gate=True)\n"
            ),
        }
        observed: list[str] = []
        with mock.patch.object(
            runtime.WorkflowState,
            "notify_event",
            side_effect=lambda event, **_kwargs: observed.append(event),
        ):
            for index, (name, source) in enumerate(scenarios.items(), start=1):
                wf_id = f"wf_7777777777{index:02x}"
                root = self.workspace / ".delegate" / "workflows" / wf_id
                root.mkdir(parents=True)
                (root / runtime.registry.SCRIPT_FILE).write_text(source, encoding="utf-8")
                if name == "paused":
                    (root / "child.py").write_text(
                        "meta = {'name': 'notify-child'}\nreturn True\n", encoding="utf-8"
                    )
                runtime.registry.write_json(root / runtime.registry.ARGS_FILE, {"args": None})
                runtime.registry.write_status(
                    root,
                    {
                        "wfId": wf_id,
                        "status": "created",
                        "workspace": str(self.workspace),
                        "budget": {"total": None, "spent": 0, "remaining": None},
                    },
                )
                self.assertEqual(
                    runtime.run_supervisor(
                        workspace=self.workspace,
                        wf_id=wf_id,
                        cli_argv=["delegate"],
                        config={},
                    ),
                    0 if name in {"paused", "succeeded"} else 1,
                )
        self.assertEqual({"paused", "failed", "succeeded"}, set(observed))

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

    def test_structured_parse_failure_exhaustion_carries_no_candidate(self) -> None:
        dsl = self._dsl()
        child = runtime._DelegateChildResult(
            text="not json",
            run_id="del_20260827T040000Z_parse1",
            execution_cwd="/tmp/parse-worktree",
            session_id=None,
        )
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "codex",
                "parse",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "object"},
                isolation="worktree",
                passthrough=False,
                timeout=None,
                retries=0,
                key="parse-key",
            )
        self.assertIsNone(result)
        event = next(
            event
            for event in runtime.registry.iter_journal(dsl.state.journal_path)
            if event.get("type") == "agent_structured_exhausted"
        )
        self.assertIsNone(event["lastParsedCandidate"])
        self.assertIn("Expecting value", event["validationError"])
        self.assertIsNone(dsl.structured_attempt("parse-key")["lastParsedCandidate"])
        resumed_dsl = self._dsl()
        self.assertEqual(
            resumed_dsl.structured_attempt("parse-key"),
            {
                "lastParsedCandidate": None,
                "validationError": event["validationError"],
            },
        )

    def test_structured_exhaustion_falls_back_to_child_completion_report(self) -> None:
        report_path = self.workspace / "completion-report.md"
        report_path.write_text(
            'Status: completed.\n\n```json\n{"ok": true}\n```\n', encoding="utf-8"
        )
        child = runtime._DelegateChildResult(
            text="assistant prose, not JSON",
            run_id="del_20260827T040000Z_fallback1",
            execution_cwd="/tmp/fallback-worktree",
            session_id=None,
            completion_report_source="child",
            completion_report_path=str(report_path),
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "fallback",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "object", "required": ["ok"]},
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="fallback-key",
            )
        self.assertEqual(result, {"ok": True})
        self.assertFalse(
            any(
                event.get("type") == "agent_structured_exhausted"
                for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            )
        )

    def test_structured_fallback_rejects_synthesized_report(self) -> None:
        report_path = self.workspace / "completion-report.md"
        report_path.write_text('```json\n{"ok": true}\n```\n', encoding="utf-8")
        child = runtime._DelegateChildResult(
            text="not JSON",
            run_id="del_20260827T040000Z_synth1",
            execution_cwd="/tmp/synth-worktree",
            session_id=None,
            completion_report_source="delegate_synthesized",
            completion_report_path=str(report_path),
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "synthesized",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "object", "required": ["ok"]},
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="synth-key",
            )
        self.assertIsNone(result)

    def test_structured_fallback_rejects_missing_report(self) -> None:
        child = runtime._DelegateChildResult(
            text="not JSON",
            run_id="del_20260827T040000Z_missing1",
            execution_cwd="/tmp/missing-worktree",
            session_id=None,
            completion_report_source="child",
            completion_report_path=str(self.workspace / "missing-report.md"),
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "missing",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "object", "required": ["ok"]},
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="missing-key",
            )
        self.assertIsNone(result)

    def test_structured_fallback_uses_last_fenced_block_only(self) -> None:
        report_path = self.workspace / "completion-report.md"
        report_path.write_text(
            '```json\n{"ok": true}\n```\n'
            "The final block is invalid.\n"
            '```json\n{"ok": "stale"}\n```\n',
            encoding="utf-8",
        )
        child = runtime._DelegateChildResult(
            text="not JSON",
            run_id="del_20260827T040000Z_stale1",
            execution_cwd="/tmp/stale-worktree",
            session_id=None,
            completion_report_source="child",
            completion_report_path=str(report_path),
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "stale",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                },
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="stale-key",
            )
        self.assertIsNone(result)
        event = next(
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "agent_structured_exhausted"
        )
        self.assertIn("Expecting value", event["validationError"])

    def test_structured_fallback_ignores_mixed_fence_examples(self) -> None:
        report_path = self.workspace / "completion-report.md"
        report_path.write_text(
            "```sh\npytest -q\n```\n"
            '{"ok": false, "note": "STALE EXAMPLE"}\n'
            '```json\n{"ok": true, "note": "real"}\n```\n',
            encoding="utf-8",
        )
        child = runtime._DelegateChildResult(
            text="assistant prose, not JSON",
            run_id="del_20260827T040000Z_mixed1",
            execution_cwd="/tmp/mixed-worktree",
            session_id=None,
            completion_report_source="child",
            completion_report_path=str(report_path),
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "mixed",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={
                    "type": "object",
                    "required": ["ok", "note"],
                    "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
                    "additionalProperties": False,
                },
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="mixed-key",
            )
        self.assertEqual(result, {"ok": True, "note": "real"})

    def test_structured_fallback_settles_after_non_json_fenced_report(self) -> None:
        report_path = self.workspace / "completion-report.md"
        report_path.write_text(
            "Ran the gate:\n\n"
            "```sh\npytest -q\n```\n\n"
            "Final:\n\n"
            '```json\n{"ok": true, "note": "real"}\n```\n',
            encoding="utf-8",
        )
        child = runtime._DelegateChildResult(
            text="assistant prose, not JSON",
            run_id="del_20260827T040000Z_mixed2",
            execution_cwd="/tmp/mixed-worktree",
            session_id=None,
            completion_report_source="child",
            completion_report_path=str(report_path),
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "mixed",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "object", "required": ["ok"]},
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="mixed-key-2",
            )
        self.assertEqual(result, {"ok": True, "note": "real"})

    def test_structured_negative_retries_returns_none(self) -> None:
        dsl = self._dsl()
        result = dsl._run_structured_or_text(
            "claude",
            "no-attempts",
            mode="safe",
            model=None,
            effort=None,
            fast=None,
            schema={"type": "object"},
            isolation=None,
            passthrough=False,
            timeout=None,
            retries=-1,
            key="negative-retries-key",
        )
        self.assertIsNone(result)

    def test_structured_agent_accepts_json_string_payload_for_object_schema(self) -> None:
        dsl = self._dsl()
        child = runtime._DelegateChildResult(
            text=json.dumps(json.dumps({"ok": True})),
            run_id="del_20260827T040000Z_string1",
            execution_cwd="/tmp/string-worktree",
            session_id=None,
        )
        schema = {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        }
        with mock.patch.object(dsl, "_run_delegate", return_value=child):
            result = dsl._run_structured_or_text(
                "codex",
                "string payload",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="string-key",
            )
        self.assertEqual(result, {"ok": True})

    def test_structured_invalid_candidate_exhaustion_carries_candidate_and_error(self) -> None:
        dsl = self._dsl()
        child = runtime._DelegateChildResult(
            text=json.dumps({"ok": "wrong"}),
            run_id="del_20260827T040000Z_invalid1",
            execution_cwd="/tmp/invalid-worktree",
            session_id=None,
        )
        schema = {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        }
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "codex",
                "invalid",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation="worktree",
                passthrough=False,
                timeout=None,
                retries=0,
                key="invalid-key",
            )
        self.assertIsNone(result)
        event = next(
            event
            for event in runtime.registry.iter_journal(dsl.state.journal_path)
            if event.get("type") == "agent_structured_exhausted"
        )
        self.assertEqual(event["lastParsedCandidate"], {"ok": "wrong"})
        self.assertIn("value.ok must be 'boolean'", event["validationError"])
        self.assertEqual(
            dsl.structured_attempt("invalid-key"),
            {
                "lastParsedCandidate": {"ok": "wrong"},
                "validationError": event["validationError"],
            },
        )

    def test_structured_json_string_invalid_candidate_carries_decoded_object(self) -> None:
        dsl = self._dsl()
        child = runtime._DelegateChildResult(
            text=json.dumps(json.dumps({"ok": "wrong"})),
            run_id="del_20260827T040000Z_stringinvalid1",
            execution_cwd="/tmp/string-invalid-worktree",
            session_id=None,
        )
        schema = {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        }
        with mock.patch.object(dsl, "_run_delegate", return_value=child):
            result = dsl._run_structured_or_text(
                "codex",
                "invalid",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="string-invalid-key",
            )
        self.assertIsNone(result)
        event = next(
            event
            for event in runtime.registry.iter_journal(dsl.state.journal_path)
            if event.get("type") == "agent_structured_exhausted"
        )
        self.assertEqual(event["lastParsedCandidate"], {"ok": "wrong"})
        self.assertIn("value.ok must be 'boolean'", event["validationError"])

    def test_schema_string_union_preserves_literal_json_string(self) -> None:
        wrapped = json.dumps({"a": 1})
        schema = {"type": ["string", "object"]}
        self.assertEqual(workflow_schema.parse_json_tolerant(json.dumps(wrapped), schema), wrapped)

    def test_typeless_object_schema_unwraps_json_string_in_prose(self) -> None:
        schema = {"required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
        self.assertEqual(
            workflow_schema.parse_json_tolerant(
                f"report: {json.dumps(json.dumps({'ok': True}))}", schema
            ),
            {"ok": True},
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
