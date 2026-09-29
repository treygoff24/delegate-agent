from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry
from delegate_agent.workflows import registry, runtime
from delegate_agent.workflows import schema as workflow_schema

_CHANGED_SUMMARY = {"changedFilesCount": 2, "commitsCreatedCount": 1, "noChanges": False}


class ChildAttemptOutcomeTests(unittest.TestCase):
    def test_provider_terminal_reasons_keep_their_names(self) -> None:
        expected = {
            "provider_cancelled": "provider_cancelled",
            "provider_refusal": "provider_refusal",
            "provider_max_turns": "provider_max_turns",
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

    def _dsl(self, cli_argv: list[str] | None = None) -> runtime.WorkflowDsl:
        state = runtime.WorkflowState(
            wf_id="wf_666666666666",
            workspace=self.workspace,
            root=self.root,
            script_path=self.script,
            config={},
            cli_argv=cli_argv or ["delegate"],
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

    def test_followup_timeout_event_names_the_step(self) -> None:
        """A follow-up that times out is attributed to its step, like an agent timeout.

        The row carried the engine and the timeout only, so a journal with
        several follow-ups could not say which one died.
        """
        dsl = self._dsl()
        with (
            mock.patch.object(
                runtime,
                "_run_child_command_for_state",
                side_effect=subprocess.TimeoutExpired(["delegate"], 1),
            ),
            mock.patch.object(runtime, "cancel_workflow_agent_child"),
        ):
            result = dsl._run_delegate_followup(
                "del_20260920T000000Z_abc126",
                "prompt text",
                engine="codex",
                timeout=1,
                prefer_assistant=False,
                workflow_agent_key="followup-timeout-key",
                label="followup-timeout-label",
            )
        assert result.outcome is not None
        self.assertEqual(result.outcome.failure_reason, "timeout")
        (event,) = [
            event
            for event in runtime.registry.iter_journal(dsl.state.journal_path)
            if event.get("type") == "agent_timeout"
        ]
        self.assertEqual(event["engine"], "codex")
        self.assertEqual(event["timeout"], 1)
        self.assertEqual(event["key"], "followup-timeout-key")
        self.assertEqual(event["label"], "followup-timeout-label")
        self.assertEqual(event["scope"], "root")

    def test_resume_retry_prompt_re_renders_the_schema(self) -> None:
        """A resume correction without the schema rebuilds the shape from memory.

        Two GLM review retries echoed a root-array schema's outer keyword as an
        object wrapper and failed validation again; the retry prompt was 152
        bytes of prose with no schema in it.
        """
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        first = runtime._DelegateChildResult(
            text=None,
            run_id="del_20260827T040000Z_resume1",
            execution_cwd="/tmp/resume-worktree",
            session_id="session-1",
            outcome=runtime.ChildAttemptOutcome(
                run_id="del_20260827T040000Z_resume1",
                failure_reason="timeout",
                session_id="session-1",
            ),
        )
        dsl = self._dsl()
        calls: list[mock._Call] = []

        def run(*args: object, **kwargs: object) -> runtime._DelegateChildResult:
            calls.append(mock.call(*args, **kwargs))
            if len(calls) == 1:
                return first
            return runtime._DelegateChildResult(
                text=json.dumps({"ok": True}),
                run_id="del_20260827T040000Z_resume2",
                execution_cwd="/tmp/resume-worktree",
                session_id="session-1",
            )

        with (
            mock.patch.object(dsl, "_run_delegate", side_effect=run),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            value = dsl._run_structured_or_text(
                "omp",
                "retry",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation="worktree",
                passthrough=False,
                timeout=1,
                retries=1,
                key="workflow-key",
            )

        self.assertEqual(value, {"ok": True})
        self.assertEqual(calls[1].kwargs["resume_session_id"], "session-1")
        retry_prompt = calls[1].args[1]
        self.assertIn(json.dumps(schema, sort_keys=True), retry_prompt)
        self.assertIn("failed validation", retry_prompt)

    def _work_retry(
        self, first: runtime._DelegateChildResult
    ) -> tuple[object, list[mock._Call], runtime.WorkflowDsl]:
        dsl = self._dsl()
        calls: list[mock._Call] = []

        def run(*args: object, **kwargs: object) -> runtime._DelegateChildResult:
            calls.append(mock.call(*args, **kwargs))
            if len(calls) == 1:
                return first
            return runtime._DelegateChildResult(
                text=json.dumps({"ok": True}),
                run_id="del_20260925T040000Z_work2",
                execution_cwd="/tmp/work-worktree",
                session_id=first.session_id,
            )

        with (
            mock.patch.object(dsl, "_run_delegate", side_effect=run),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
            mock.patch.object(dsl, "_wait_retry_backoff"),
        ):
            value = dsl._run_structured_or_text(
                "codex",
                "perform the implementation",
                mode="work",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                isolation="worktree",
                passthrough=False,
                timeout=1,
                retries=1,
                key="work-key",
            )
        return value, calls, dsl

    def _quiet_work_children(
        self, *, session_id: str | None, work_summary: dict[str, object]
    ) -> dict[str, runtime._DelegateChildResult]:
        """The two shapes a work child that landed changes but no answer takes."""
        run_id = "del_20260925T040000Z_work1"
        return {
            # Exit 0, no text, changes: succeeded with a warning (outcome None).
            "succeeded_without_text": runtime._DelegateChildResult(
                text=None,
                run_id=run_id,
                execution_cwd="/tmp/work-worktree",
                session_id=session_id,
                work_summary=work_summary,
            ),
            "empty_result": runtime._DelegateChildResult(
                text=None,
                run_id=run_id,
                execution_cwd="/tmp/work-worktree",
                session_id=session_id,
                work_summary=work_summary,
                outcome=runtime.ChildAttemptOutcome(
                    run_id=run_id, failure_reason="empty_result", session_id=session_id
                ),
            ),
        }

    def test_work_child_with_changes_is_not_relaunched_fresh_into_its_tree(self) -> None:
        children = self._quiet_work_children(session_id=None, work_summary=_CHANGED_SUMMARY)
        for shape, first in children.items():
            with self.subTest(shape):
                self.setUp()
                value, calls, dsl = self._work_retry(first)
                self.assertIsNone(value)
                self.assertEqual(len(calls), 1, "a fresh child was launched into a changed tree")
                events = list(runtime.registry.iter_journal(dsl.state.journal_path))
                self.assertFalse(any(e.get("type") == "agent_structured_retry" for e in events))
                refused = next(
                    e for e in events if e.get("type") == "agent_structured_retry_refused"
                )
                self.assertEqual(refused["workSummary"], _CHANGED_SUMMARY)
                exhausted = next(e for e in events if e.get("type") == "agent_structured_exhausted")
                self.assertEqual(exhausted["failureKind"], "structured_invalid")
                self.assertEqual(exhausted["workSummary"], _CHANGED_SUMMARY)
                self.assertEqual(exhausted["runId"], first.run_id)

    def test_work_child_with_changes_resumes_its_session_for_the_structured_result(
        self,
    ) -> None:
        children = self._quiet_work_children(
            session_id="session-work", work_summary=_CHANGED_SUMMARY
        )
        for shape, first in children.items():
            with self.subTest(shape):
                self.setUp()
                value, calls, _dsl = self._work_retry(first)
                self.assertEqual(value, {"ok": True})
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1].kwargs["resume_session_id"], "session-work")
                retry_prompt = calls[1].args[1]
                self.assertIn("Re-emit the StructuredOutput now.", retry_prompt)
                self.assertNotIn("perform the implementation", retry_prompt)

    def test_work_child_without_changes_keeps_the_fresh_relaunch(self) -> None:
        unchanged = {"changedFilesCount": 0, "commitsCreatedCount": 0, "noChanges": True}
        children = self._quiet_work_children(session_id=None, work_summary=unchanged)
        for shape, first in children.items():
            with self.subTest(shape):
                self.setUp()
                value, calls, _dsl = self._work_retry(first)
                self.assertEqual(value, {"ok": True})
                self.assertEqual(len(calls), 2)
                self.assertIsNone(calls[1].kwargs["resume_session_id"])
                self.assertIn("perform the implementation", calls[1].args[1])

    def test_child_result_reads_the_envelope_work_summary(self) -> None:
        child = runtime._child_result_from_payload(
            {"ok": True, "runId": "del_x", "workSummary": dict(_CHANGED_SUMMARY)},
            text=None,
        )
        self.assertTrue(child.work_changed)
        failed = runtime._failed_child_result(child, reason="empty_result")
        self.assertEqual(failed.work_summary, _CHANGED_SUMMARY)
        self.assertFalse(runtime._child_result_from_payload({"ok": True}, text=None).work_changed)

    def test_a_failed_child_launch_records_its_stderr_and_exit_code(self) -> None:
        """A child that dies before publishing JSON must still say why.

        The journal recorded only 'child attempt nonzero_exit: Expecting value:
        line 1 column 1 (char 0)' — the decode error of empty stdout — and the
        child's real stderr was discarded.
        """
        fake = self.workspace / "fake-delegate"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'quota exceeded for workspace; retry after reset\\n' >&2\n"
            "exit 3\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        dsl = self._dsl(cli_argv=[str(fake)])

        result = dsl._run_delegate_followup(
            "del_20260920T000000Z_abc123",
            "prompt text",
            engine="codex",
            timeout=5,
            prefer_assistant=False,
            workflow_agent_key="capture-key",
            label="capture",
        )

        self.assertIsNotNone(result.outcome)
        assert result.outcome is not None
        payload = result.outcome.as_json()
        self.assertEqual(payload["failureReason"], "nonzero_exit")
        self.assertEqual(payload["exitCode"], 3)
        self.assertIn("quota exceeded", payload["stderrTail"])
        event = next(
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "child_stdout_unparsed"
        )
        self.assertEqual(event["exitCode"], 3)
        self.assertEqual(event["stdoutChars"], 0)
        self.assertIn("quota exceeded", event["stderrTail"])

    def test_an_initial_child_launch_records_its_stderr_and_exit_code(self) -> None:
        """The first `agent()` attempt must carry the same evidence as a followup.

        A child that dies before publishing JSON (quota, auth, launch refusal)
        leaves the decode error of empty stdout as the journal's only diagnosis,
        and the initial launch is the common case: followups already carried
        the exit code and stderr tail, initial launches did not.
        """
        fake = self.workspace / "fake-initial-delegate"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'quota exceeded for workspace; retry after reset\n' >&2\n"
            "exit 3\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        dsl = self._dsl(cli_argv=[str(fake)])

        result = dsl._run_delegate(
            "codex",
            "prompt text",
            mode="safe",
            model=None,
            effort=None,
            fast=None,
            isolation="none",
            passthrough=False,
            timeout=5,
            output_schema=None,
            prefer_assistant=False,
            workflow_agent_key="initial-capture-key",
            label="initial-capture",
            return_metadata=True,
        )

        assert isinstance(result, runtime._DelegateChildResult)
        assert result.outcome is not None
        payload = result.outcome.as_json()
        self.assertEqual(payload["failureReason"], "nonzero_exit")
        self.assertEqual(payload["exitCode"], 3)
        self.assertIn("quota exceeded", payload["stderrTail"])
        event = next(
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "child_stdout_unparsed"
        )
        self.assertEqual(event["exitCode"], 3)
        self.assertEqual(event["stdoutChars"], 0)
        self.assertIn("quota exceeded", event["stderrTail"])

    def test_an_initial_child_launch_redacts_its_stderr(self) -> None:
        secret = "sk-initialsecret1234567890"
        fake = self.workspace / "fake-initial-delegate-secret"
        fake.write_text(
            f"#!/usr/bin/env bash\nprintf 'debug %s\n' {secret} >&2\nexit 1\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        dsl = self._dsl(cli_argv=[str(fake)])

        result = dsl._run_delegate(
            "codex",
            "prompt text",
            mode="safe",
            model=None,
            effort=None,
            fast=None,
            isolation="none",
            passthrough=False,
            timeout=5,
            output_schema=None,
            prefer_assistant=False,
            workflow_agent_key="initial-secret-key",
            return_metadata=True,
        )

        assert isinstance(result, runtime._DelegateChildResult)
        assert result.outcome is not None
        tail = result.outcome.as_json()["stderrTail"]
        self.assertIn("debug", tail)
        self.assertNotIn(secret, tail)

    def test_exit_zero_invalid_envelope_is_not_reported_as_a_nonzero_exit(self) -> None:
        """Exit zero with an unusable envelope is a contract break, not a crash.

        `nonzero_exit` was the default reason for every child that published no
        usable `ok` envelope, which is false for a child that exited 0.
        """
        fake = self.workspace / "fake-initial-delegate-empty"
        fake.write_text(
            "#!/usr/bin/env bash\nprintf '{\"ok\": false}\n'\nexit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        dsl = self._dsl(cli_argv=[str(fake)])

        result = dsl._run_delegate(
            "codex",
            "prompt text",
            mode="safe",
            model=None,
            effort=None,
            fast=None,
            isolation="none",
            passthrough=False,
            timeout=5,
            output_schema=None,
            prefer_assistant=False,
            workflow_agent_key="initial-invalid-envelope",
            return_metadata=True,
        )

        assert isinstance(result, runtime._DelegateChildResult)
        assert result.outcome is not None
        payload = result.outcome.as_json()
        self.assertEqual(payload["failureReason"], "invalid_envelope")
        self.assertEqual(payload["exitCode"], 0)

    def _write_fake_delegate(
        self,
        name: str,
        *,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> Path:
        """A fake child whose stdout and stderr are exactly the given bytes."""
        stdout_path = self.workspace / f"{name}.stdout"
        stderr_path = self.workspace / f"{name}.stderr"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        fake = self.workspace / name
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f"cat {shlex.quote(str(stdout_path))}\n"
            f"cat {shlex.quote(str(stderr_path))} >&2\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    def _unparsed_stdout_event(self, key: str) -> dict:
        events = [
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "child_stdout_unparsed" and event.get("key") == key
        ]
        self.assertEqual(len(events), 1, events)
        return events[0]

    def test_an_initial_exit_zero_child_with_unparsed_stdout_reports_the_contract(self) -> None:
        """Exit zero with no JSON at all is a broken envelope, not a crash.

        The initial path returned reason 'structured' with no exit code, no
        stderr tail, and no `child_stdout_unparsed` event, so a child that exits
        0 after writing empty or non-JSON stdout kept the decode error of its
        own stdout as the journal's only diagnosis.
        """
        secret = "sk-exitzeroinitial1234567890"
        stderr_text = f"cannot parse child stdout; debug {secret}\n"
        for label, stdout_text in (("empty", ""), ("non-json", "not json at all\n")):
            with self.subTest(stdout=label):
                key = f"initial-exit-zero-{label}"
                fake = self._write_fake_delegate(
                    f"fake-initial-exit-zero-{label}",
                    stdout=stdout_text,
                    stderr=stderr_text,
                    exit_code=0,
                )
                dsl = self._dsl(cli_argv=[str(fake)])

                result = dsl._run_delegate(
                    "codex",
                    "prompt text",
                    mode="safe",
                    model=None,
                    effort=None,
                    fast=None,
                    isolation="none",
                    passthrough=False,
                    timeout=5,
                    output_schema=None,
                    prefer_assistant=False,
                    workflow_agent_key=key,
                    label="initial-exit-zero",
                    return_metadata=True,
                )

                assert isinstance(result, runtime._DelegateChildResult)
                assert result.outcome is not None
                payload = result.outcome.as_json()
                self.assertEqual(payload["failureReason"], "invalid_envelope")
                self.assertEqual(payload["exitCode"], 0)
                self.assertIn("cannot parse child stdout", payload["stderrTail"])
                self.assertNotIn(secret, payload["stderrTail"])

                event = self._unparsed_stdout_event(key)
                self.assertEqual(event["exitCode"], 0)
                self.assertEqual(event["stdoutChars"], len(stdout_text))
                self.assertIn("cannot parse child stdout", event["stderrTail"])
                self.assertNotIn(secret, event["stderrTail"])

    def test_a_followup_exit_zero_child_with_unparsed_stdout_reports_the_contract(self) -> None:
        """The followup path called this same condition a nonzero exit.

        `nonzero_exit` was the fallback reason whenever the payload did not
        parse, even for a child that exited 0, so a retry or an operator read a
        process failure that never happened on the followup lane.
        """
        secret = "sk-exitzerofollowup1234567890"
        stderr_text = f"diagnostic for followup; token {secret}\n"
        for label, stdout_text in (("empty", ""), ("non-json", "not json at all\n")):
            with self.subTest(stdout=label):
                key = f"followup-exit-zero-{label}"
                fake = self._write_fake_delegate(
                    f"fake-followup-exit-zero-{label}",
                    stdout=stdout_text,
                    stderr=stderr_text,
                    exit_code=0,
                )
                dsl = self._dsl(cli_argv=[str(fake)])

                result = dsl._run_delegate_followup(
                    "del_20260920T000000Z_abc125",
                    "prompt text",
                    engine="codex",
                    timeout=5,
                    prefer_assistant=False,
                    workflow_agent_key=key,
                    label="followup-exit-zero",
                )

                assert result.outcome is not None
                payload = result.outcome.as_json()
                self.assertEqual(payload["failureReason"], "invalid_envelope")
                self.assertEqual(payload["exitCode"], 0)
                self.assertIn("diagnostic for followup", payload["stderrTail"])
                self.assertNotIn(secret, payload["stderrTail"])

                event = self._unparsed_stdout_event(key)
                self.assertEqual(event["exitCode"], 0)
                self.assertEqual(event["stdoutChars"], len(stdout_text))
                self.assertIn("diagnostic for followup", event["stderrTail"])
                self.assertNotIn(secret, event["stderrTail"])

    def test_a_failed_child_tail_is_bounded_and_redacted(self) -> None:
        secret = "sk-livesecret1234567890"
        fake = self.workspace / "fake-delegate-secret"
        fake.write_text(
            f"#!/usr/bin/env bash\nprintf 'debug %s\\n' {secret} >&2\nexit 1\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        dsl = self._dsl(cli_argv=[str(fake)])

        result = dsl._run_delegate_followup(
            "del_20260920T000000Z_abc124",
            "prompt text",
            engine="codex",
            timeout=5,
            prefer_assistant=False,
            workflow_agent_key="capture-secret-key",
        )

        assert result.outcome is not None
        tail = result.outcome.as_json()["stderrTail"]
        self.assertIn("debug", tail)
        self.assertNotIn(secret, tail)
        self.assertLessEqual(len(tail), runtime.CHILD_STDERR_TAIL_CHARS)

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
                        "workflowKeyVersion": 2,
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

    def test_output_cap_outcome_stops_and_preserves_failed_run_identity(self) -> None:
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
        self.assertIsNone(value)
        self.assertEqual(len(calls), 1)
        event = next(
            event
            for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)
            if event.get("type") == "agent_attempt_failed"
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
        with self.assertRaises(ValueError):
            resumed_dsl.state.resolve_agent_key("parse-key")
        runtime.registry.append_jsonl(
            dsl.state.journal_path,
            {"type": "agent_finished", "key": "parse-key", "result": "stub", "simulated": True},
        )
        self.assertEqual(
            resumed_dsl.structured_attempt("parse-key"),
            {
                "lastParsedCandidate": None,
                "validationError": event["validationError"],
            },
        )

    def test_schema_allowed_null_stays_private_until_agent_settlement(self) -> None:
        dsl = self._dsl()
        child = runtime._DelegateChildResult(
            text="null",
            run_id="del_20260827T040000Z_null1",
            execution_cwd="/tmp/null-worktree",
            session_id=None,
        )
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "codex",
                "null",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "null"},
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="null-key",
            )
        self.assertIs(result, runtime._STRUCTURED_NULL)
        self.assertIsNone(runtime._unwrap_structured_null(result))
        self.assertNotIn(
            "agent_structured_exhausted",
            {event["type"] for event in registry.iter_journal(self.root / registry.JOURNAL_FILE)},
        )

    def test_completion_report_null_uses_the_same_success_sentinel(self) -> None:
        report_path = self.workspace / "completion-report.md"
        report_path.write_text("Status: completed.\n\n```json\nnull\n```\n", encoding="utf-8")
        child = runtime._DelegateChildResult(
            text="assistant prose, not JSON",
            run_id="del_20260827T040000Z_null-report",
            execution_cwd="/tmp/null-report-worktree",
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
                "null fallback",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "null"},
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="null-report-key",
            )
        self.assertIs(result, runtime._STRUCTURED_NULL)
        self.assertIsNone(runtime._unwrap_structured_null(result))

    def test_provider_refusal_never_becomes_successful_null(self) -> None:
        dsl = self._dsl()
        refused = runtime._DelegateChildResult(
            text=None,
            run_id="del_20260827T040000Z_refused",
            execution_cwd="/tmp/refused-worktree",
            session_id=None,
            outcome=runtime.ChildAttemptOutcome(
                run_id="del_20260827T040000Z_refused",
                failure_reason="nonzero_exit",
            ),
        )
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=refused),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "codex",
                "refused",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema={"type": "null"},
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="refused-key",
            )
        self.assertIsNone(result)
        self.assertNotIsInstance(result, runtime._StructuredNullType)

    def test_replay_only_registers_non_exhausted_explicit_result_children(self) -> None:
        rows = [
            {
                "seq": 1,
                "type": "agent_child",
                "key": "exhausted-key",
                "workflowAgentKey": "exhausted-key",
                "runId": "del_exhausted",
                "engine": "codex",
                "label": "exhausted",
                "resumable": True,
            },
            {
                "seq": 2,
                "type": "agent_finished",
                "key": "exhausted-key",
                "result": None,
                "exhausted": True,
            },
            {
                "seq": 3,
                "type": "agent_child",
                "key": "missing-result-key",
                "workflowAgentKey": "missing-result-key",
                "runId": "del_missing",
                "engine": "codex",
                "label": "missing-result",
                "resumable": True,
            },
            {
                "seq": 4,
                "type": "agent_finished",
                "key": "missing-result-key",
            },
        ]
        for row in rows:
            registry.append_jsonl(self.root / registry.JOURNAL_FILE, row)
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
        self.assertEqual(state.completed_labels, {})
        self.assertIn("exhausted-key", state.exhausted_keys)

    def test_claude_workflow_schema_goes_native_through_output_schema(self) -> None:
        dsl = self._dsl()
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
            "required": ["ok"],
        }
        # Optional field: Codex would take the prompt-only path; Claude must not.
        self.assertIsNone(runtime._codex_native_schema(schema))
        seen: dict[str, object] = {}

        def fake_run_delegate(engine, prompt, **kwargs):
            seen["engine"] = engine
            seen["prompt"] = prompt
            seen["output_schema"] = kwargs["output_schema"]
            seen["schema_bytes"] = Path(kwargs["output_schema"]).read_text(encoding="utf-8")
            return runtime._DelegateChildResult(
                text='{"ok": true}',
                run_id="del_20260901T000000Z_claude1",
                execution_cwd="/tmp/claude-worktree",
                session_id=None,
            )

        with (
            mock.patch.object(dsl, "_run_delegate", side_effect=fake_run_delegate),
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "judge",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation="none",
                passthrough=False,
                timeout=None,
                retries=0,
                key="claude-key",
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["engine"], "claude")
        self.assertIsNotNone(seen["output_schema"])
        self.assertEqual(json.loads(seen["schema_bytes"]), schema)
        # Native enforcement means no prompt-embedded schema contract.
        self.assertEqual(seen["prompt"], "judge")

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

    def test_negative_retries_still_launch_one_structured_attempt(self) -> None:
        child = runtime._DelegateChildResult(
            text=json.dumps({"ok": True}),
            run_id="del_20260827T040000Z_negret1",
            execution_cwd="/tmp/negative-retries",
            session_id=None,
        )
        dsl = self._dsl()
        with (
            mock.patch.object(dsl, "_run_delegate", return_value=child) as launch,
            mock.patch.object(dsl, "_release_structured_retry_worktree"),
        ):
            result = dsl._run_structured_or_text(
                "claude",
                "one-attempt",
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
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result, {"ok": True})

    def test_negative_retries_still_launch_one_followup_attempt(self) -> None:
        child = runtime._DelegateChildResult(
            text="followup done",
            run_id="del_20260827T040000Z_negret2",
            execution_cwd=None,
            session_id=None,
        )
        dsl = self._dsl()
        with mock.patch.object(dsl, "_run_delegate_followup", return_value=child) as launch:
            result = dsl._run_followup_structured_or_text(
                runtime.CompletedChild("prior", "claude", True),
                "follow up",
                schema=None,
                timeout=None,
                retries=-1,
                key="negative-followup-key",
            )
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result, "followup done")

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
                "candidatePresent": True,
                "validationError": event["validationError"],
                # dlg-5kl: the typed exhaustion record also names attempts and kind.
                "attempts": 1,
                "failureKind": "structured_invalid",
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
