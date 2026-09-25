"""The one outcome contract (dlg-5kl).

Every surface -- the CLI envelope's ok/status/exitCode, the process exit code,
the persisted record, and the workflow agent() return path -- reads the Outcome
that ``outcome.compute_outcome`` derives once at finalization.
"""

from __future__ import annotations

import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from typing import ClassVar
from unittest import mock

from delegate_agent import (
    cli,
    cli_parser,
    outcome,
    request_build,
    request_models,
    run_registry,
    runner,
)
from delegate_agent import config as delegate_config
from delegate_agent.workflows import registry as workflow_registry
from delegate_agent.workflows import runtime as workflow_runtime
from delegate_agent.workflows import schema as workflow_schema

REPORT = (
    "Status: completed. The review finished and this final report lists the findings, "
    "the verification that ran, the changed files, and the remaining risks for the parent."
)


def _agent_message(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


class TrackedOutcomeTests(unittest.TestCase):
    def _run(
        self,
        workspace: str,
        body: str,
        *,
        mode: str = "work",
        expect_files: tuple[str, ...] = (),
    ) -> tuple[int, dict[str, object], dict[str, object]]:
        root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
        run_id, alias = run_registry.register_run(root, harness="codex")
        script = Path(workspace) / "codex"
        script.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
        script.chmod(0o755)
        ctx = runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode=mode,
            model=None,
            source_cwd=workspace,
            execution_cwd=workspace,
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-09-24T00:00:00Z",
            **({"expect_files": expect_files} if expect_files else {}),
        )
        code, payload = runner.execute_tracked(
            [str(script)],
            workspace,
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert payload is not None
        state = run_registry.load_run_state(root, run_id)
        return code, payload, state

    def test_exit_zero_without_assistant_text_fails_on_every_surface(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            turn = shlex.quote(json.dumps({"type": "turn.completed"}))
            code, payload, state = self._run(workspace, f"printf '%s\\n' {turn}\nexit 0\n")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exitCode"], 1)
        self.assertEqual(payload["failureKind"], "no_assistant_text")
        self.assertFalse(state["ok"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exitCode"], 1)
        self.assertEqual(state["failureKind"], "no_assistant_text")

    def test_exit_zero_provider_quota_event_fails_as_provider_quota(self) -> None:
        # The child wrote a report and then its provider refused with a quota
        # error it never recovered from, and the harness still exited 0.
        error = json.dumps(
            {"type": "error", "message": "429 RESOURCE_EXHAUSTED: quota exceeded for model"}
        )
        body = (
            f"printf '%s\\n' {shlex.quote(_agent_message(REPORT))}\n"
            f"printf '%s\\n' {shlex.quote(error)}\n"
            "exit 0\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, state = self._run(workspace, body)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failureKind"], "provider_quota")
        self.assertEqual(payload["failureReason"], "usage_limit")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failureKind"], "provider_quota")

    def test_exit_zero_empty_run_with_quota_diagnostics_fails_as_provider_quota(self) -> None:
        # The provider refused on quota, the harness said so only on stderr,
        # printed no assistant text, and still exited 0.
        turn = shlex.quote(json.dumps({"type": "turn.completed"}))
        body = (
            "printf 'ERROR: 429 Too Many Requests: You have hit your usage limit.\\n' >&2\n"
            f"printf '%s\\n' {turn}\n"
            "exit 0\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, state = self._run(workspace, body)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failureKind"], "provider_quota")
        self.assertEqual(state["failureKind"], "provider_quota")

    def test_expect_file_missing_fails_as_deliverable_missing(self) -> None:
        body = (
            f"printf '%s\\n' {shlex.quote(_agent_message(REPORT))}\n"
            f"printf '%s\\n' {shlex.quote(json.dumps({'type': 'turn.completed'}))}\n"
            "exit 0\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, state = self._run(
                workspace, body, expect_files=("out/report.md", "present.txt")
            )
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failureKind"], "deliverable_missing")
        self.assertEqual(payload["error"], "deliverable_missing")
        self.assertEqual(
            payload["expectedFiles"],
            {
                "expected": ["out/report.md", "present.txt"],
                "missing": ["out/report.md", "present.txt"],
            },
        )
        self.assertEqual(state["failureKind"], "deliverable_missing")

    def test_expect_file_present_keeps_the_run_succeeded(self) -> None:
        body = (
            "mkdir -p out && printf 'done\\n' > out/report.md\n"
            f"printf '%s\\n' {shlex.quote(_agent_message(REPORT))}\n"
            f"printf '%s\\n' {shlex.quote(json.dumps({'type': 'turn.completed'}))}\n"
            "exit 0\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, _state = self._run(workspace, body, expect_files=("out/report.md",))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["failureKind"])
        self.assertNotIn("expectedFiles", payload)

    def test_surviving_process_group_member_records_orphaned_warning(self) -> None:
        body = (
            # Detached from stdout/stderr so capture reaches EOF when the
            # harness exits, leaving only the background process alive.
            "sleep 30 </dev/null >/dev/null 2>&1 &\n"
            f"printf '%s\\n' {shlex.quote(_agent_message(REPORT))}\n"
            f"printf '%s\\n' {shlex.quote(json.dumps({'type': 'turn.completed'}))}\n"
            "exit 0\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, state = self._run(workspace, body)
        self.assertEqual(code, 0)
        self.assertTrue(payload["orphanedProcesses"])
        self.assertTrue(
            any(str(w).startswith("orphanedProcesses:") for w in payload.get("warnings") or [])
        )
        self.assertTrue(state["orphanedProcesses"])

    def test_clean_exit_records_no_orphaned_warning(self) -> None:
        body = (
            f"printf '%s\\n' {shlex.quote(_agent_message(REPORT))}\n"
            f"printf '%s\\n' {shlex.quote(json.dumps({'type': 'turn.completed'}))}\n"
            "exit 0\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, _state = self._run(workspace, body)
        self.assertEqual(code, 0)
        self.assertNotIn("orphanedProcesses", payload)


class CallOutcomeTests(unittest.TestCase):
    def _execute(self, result: runner.CallResult) -> tuple[int, dict[str, object]]:
        parsed = cli_parser.parse_cli(["codex", "call", "hello"])
        request = request_build.request_from_parsed(
            parsed, delegate_config.embedded_default_config(), io.StringIO("")
        )
        with (
            mock.patch.object(cli, "ensure_binary"),
            mock.patch.object(runner, "execute_call", return_value=result),
        ):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=delegate_config.embedded_default_config(),
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        assert payload is not None
        return code, payload

    def test_call_exit_zero_unrecovered_quota_error_fails(self) -> None:
        code, payload = self._execute(
            runner.CallResult(
                text=REPORT,
                exit_code=0,
                duration_ms=10,
                stdout_bytes=10,
                stderr_bytes=0,
                text_chars=len(REPORT),
                text_truncated=False,
                unrecovered_error="429 RESOURCE_EXHAUSTED: quota exceeded",
            )
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failureKind"], "provider_quota")
        self.assertEqual(payload["error"], "usage_limit")

    def test_call_json_uses_assistant_text_and_keeps_text_alias(self) -> None:
        code, payload = self._execute(
            runner.CallResult(
                text=REPORT,
                exit_code=0,
                duration_ms=10,
                stdout_bytes=10,
                stderr_bytes=0,
                text_chars=len(REPORT),
                text_truncated=False,
            )
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["failureKind"])
        self.assertEqual(payload["assistantText"], REPORT)
        self.assertEqual(payload["assistantTextChars"], len(REPORT))
        self.assertFalse(payload["assistantTextTruncated"])
        self.assertEqual(payload["text"], payload["assistantText"])

    def test_call_retry_classifies_only_the_final_attempt_stderr(self) -> None:
        # The first attempt hit a usage limit; the retried attempt exited 0,
        # empty, with a clean stderr. The merged stderr keeps both for
        # diagnosis, but only the final attempt's may name the failure.
        first = runner.CallResult(
            text="",
            exit_code=0,
            duration_ms=5,
            stdout_bytes=0,
            stderr_bytes=40,
            text_chars=0,
            text_truncated=False,
            stderr_tail="ERROR: 429 Too Many Requests: You have hit your usage limit.",
            result_quality="empty",
        )
        last = runner.CallResult(
            text="",
            exit_code=0,
            duration_ms=5,
            stdout_bytes=0,
            stderr_bytes=0,
            text_chars=0,
            text_truncated=False,
            stderr_tail="",
            result_quality="empty",
        )
        merged = runner._merge_call_attempts(first, last, "delegate empty-retry attempt")
        self.assertIn("usage limit", merged.stderr_tail)
        self.assertEqual(merged.final_attempt_stderr_tail, "")
        code, payload = self._execute(merged)
        self.assertEqual(code, 1)
        self.assertEqual(payload["failureKind"], "no_assistant_text")


class ComputeOutcomeTests(unittest.TestCase):
    def test_precedence_and_failure_kinds(self) -> None:
        cases = [
            (dict(child_exit_code=0, cancelled=True, result_quality="empty"), "cancelled", 1),
            (dict(child_exit_code=124, failure_reason="call_timeout"), "timeout", 124),
            (
                dict(child_exit_code=0, failure_reason="commit_policy_violated"),
                "policy_violation",
                1,
            ),
            (dict(child_exit_code=2, signal_text="insufficient_quota"), "provider_quota", 2),
            (dict(child_exit_code=2, signal_text="segfault"), "exit_nonzero", 2),
            (
                dict(child_exit_code=0, provider_terminal_state="provider_refusal"),
                "provider_refusal",
                1,
            ),
            (dict(child_exit_code=0, result_quality="no_assistant_text"), "no_assistant_text", 1),
            (dict(child_exit_code=0, missing_deliverables=("a.md",)), "deliverable_missing", 1),
            (dict(child_exit_code=0, result_quality="ok"), None, 0),
        ]
        for kwargs, kind, exit_code in cases:
            with self.subTest(kwargs=kwargs):
                result = outcome.compute_outcome(**kwargs)
                self.assertEqual(result.failure_kind, kind)
                self.assertEqual(result.exit_code, exit_code)
                self.assertEqual(result.ok, kind is None)
                self.assertTrue(kind is None or kind in outcome.FAILURE_KINDS)

    def test_prior_attempt_quota_does_not_name_an_empty_final_attempt(self) -> None:
        result = outcome.compute_outcome(
            child_exit_code=0,
            result_quality="empty",
            signal_text="usage limit reached",
            final_attempt_signal_text="",
        )
        self.assertEqual(result.failure_kind, "no_assistant_text")


class StructuredOutputOutcomeTests(unittest.TestCase):
    def test_raw_control_characters_inside_json_strings_parse(self) -> None:
        text = '{"verdict": "pass\twith a literal tab\x0cand form feed", "n": 1}'
        self.assertEqual(
            workflow_schema.parse_json_tolerant(text),
            {"verdict": "pass\twith a literal tab\x0cand form feed", "n": 1},
        )

    def test_stray_control_characters_outside_strings_are_stripped(self) -> None:
        self.assertEqual(workflow_schema.parse_json_tolerant('{"a":\x01 1}'), {"a": 1})


class WorkflowAgentFailureTests(unittest.TestCase):
    SCHEMA: ClassVar[dict[str, object]] = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        root = self.workspace / ".delegate" / "workflows" / "wf_5c05c05c05c0"
        run_registry.ensure_private_dir(root)
        self.state = workflow_runtime.WorkflowState(
            wf_id="wf_5c05c05c05c0",
            workspace=self.workspace,
            root=root,
            script_path=self.workspace / "workflow.py",
            config=delegate_config.embedded_default_config(),
            cli_argv=["delegate"],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.dsl = workflow_runtime.WorkflowDsl(
            self.state, {"defaults": {"engine": "claude", "mode": "safe"}}
        )

    @staticmethod
    def _child(argv, *, cwd, timeout, environment=None, cancel_event=None):
        envelope = {
            "ok": True,
            "status": "succeeded",
            "exitCode": 0,
            "runId": "del_20260924T000000Z_invalid",
            "failureKind": None,
            "text": json.dumps({"ok": "not-a-boolean"}),
            "modelProvenance": {"servedModel": "model-x", "servedProvider": "provider-y"},
        }
        return CompletedProcess(argv, 0, json.dumps(envelope).encode(), b"")

    def _agent(self, **kwargs: object) -> object:
        with (
            mock.patch.object(workflow_runtime, "_run_child_command", side_effect=self._child),
            mock.patch.object(self.dsl, "_release_structured_retry_worktree"),
        ):
            return self.dsl.agent("review", schema=self.SCHEMA, retries=0, label="rev", **kwargs)

    def test_structured_exhaustion_returns_typed_failure_with_candidate(self) -> None:
        result = self._agent(on_failure="typed")
        self.assertIsInstance(result, workflow_runtime.AgentFailure)
        assert isinstance(result, workflow_runtime.AgentFailure)
        self.assertFalse(result)
        self.assertEqual(result.failure_kind, "structured_invalid")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.last_parsed_candidate, {"ok": "not-a-boolean"})
        self.assertTrue(result.candidate_present)
        self.assertEqual(result.run_id, "del_20260924T000000Z_invalid")
        self.assertEqual(result.served_model, "model-x")
        self.assertEqual(result.served_provider, "provider-y")
        meta = self.dsl.agent_meta("rev")
        assert meta is not None
        self.assertEqual(meta["servedModel"], "model-x")
        self.assertEqual(meta["servedProvider"], "provider-y")
        self.assertTrue(meta["ok"])

    def test_default_on_failure_keeps_returning_none_for_compiled_scripts(self) -> None:
        self.assertIsNone(self._agent())
        exhausted = [
            event
            for event in workflow_registry.iter_journal(self.state.journal_path)
            if event.get("type") == "agent_structured_exhausted"
        ]
        self.assertEqual(exhausted[-1]["attempts"], 1)
        self.assertEqual(exhausted[-1]["failureKind"], "structured_invalid")
        self.assertEqual(exhausted[-1]["lastParsedCandidate"], {"ok": "not-a-boolean"})

    def test_capabilities_global_advertises_the_contract(self) -> None:
        script = self.workspace / "caps.py"
        script.write_text(
            'return {"caps": capabilities, "typed": AgentFailure.__name__}\n', encoding="utf-8"
        )
        frame = workflow_runtime._WorkflowInvocation(script, None, "root", 0)
        self.assertEqual(
            workflow_runtime.execute_workflow(self.state, frame),
            {
                "caps": {
                    "agentFailure": 1,
                    "agentMeta": 1,
                    "failureKind": 1,
                    "agentKey": 1,
                    "scopeKey": 1,
                    "gateActions": 1,
                },
                "typed": "AgentFailure",
            },
        )


class ExpectFileParsingTests(unittest.TestCase):
    def test_expect_file_is_repeatable_and_rejected_for_call_mode(self) -> None:
        parsed = cli_parser.parse_cli(
            ["codex", "work", "--expect-file", "a.md", "--expect-file", "b/c.md", "do it"]
        )
        self.assertEqual(parsed.payload.expect_files, ("a.md", "b/c.md"))
        with self.assertRaises(Exception) as caught:
            request_build.request_from_parsed(
                cli_parser.parse_cli(["codex", "call", "--expect-file", "a.md", "hi"]),
                delegate_config.embedded_default_config(),
                io.StringIO(""),
            )
        self.assertIn("--expect-file", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


def _pi_line(payload: dict[str, object]) -> str:
    return f"printf '%s\\n' {shlex.quote(json.dumps(payload))}\n"


_PI_QUOTA_NOTICE = {
    "type": "notice",
    "level": "error",
    "message": "429 RESOURCE_EXHAUSTED: quota exceeded for this model",
}
_PI_TEXT = {
    "type": "message_end",
    "message": {"role": "assistant", "content": [{"type": "text", "text": REPORT}]},
}


class FinalAttemptClassificationTests(unittest.TestCase):
    """Provider signals classify a run only from its final attempt (dlg-5kl)."""

    def _run_pi(self, workspace: str, attempts: list[str]) -> tuple[int, dict[str, object]]:
        # Each invocation runs the next attempt body; the counter file keeps
        # the fake's state across the runner's retry.
        counter = Path(workspace) / "attempt-count"
        cases = "".join(f"  {index}) {body.strip()} ;;\n" for index, body in enumerate(attempts, 1))
        script = Path(workspace) / "pi"
        script.write_text(
            "#!/usr/bin/env bash\n"
            f"n=$(( $(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0) + 1 ))\n"
            f"echo $n > {shlex.quote(str(counter))}\n"
            'case "$n" in\n' + cases + "esac\nexit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
        run_id, alias = run_registry.register_run(root, harness="pi")
        ctx = runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness="pi",
            engine="pi",
            mode="safe",
            model=None,
            source_cwd=workspace,
            execution_cwd=workspace,
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-09-24T00:00:00Z",
        )
        code, payload = runner.execute_tracked(
            [str(script), "original prompt"],
            workspace,
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            # A manifest argv makes the empty-result retry available.
            manifest_argv=[str(script), "<prompt>"],
        )
        assert payload is not None
        return code, payload

    def test_unrecovered_quota_notice_fails_a_run_that_produced_text(self) -> None:
        # pi/omp report a session-layer quota refusal as an error notice; with
        # no later successful turn_end it is the provider's last word.
        with tempfile.TemporaryDirectory() as workspace:
            code, payload = self._run_pi(
                workspace, [_pi_line(_PI_QUOTA_NOTICE) + _pi_line(_PI_TEXT)]
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["failureKind"], "provider_quota")
        self.assertEqual(payload["resultQuality"], "ok")

    def test_final_attempt_unrecovered_quota_survives_the_retry_merge(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            code, payload = self._run_pi(
                workspace,
                [":", _pi_line(_PI_QUOTA_NOTICE) + _pi_line(_PI_TEXT)],
            )
        self.assertEqual(payload["emptyRetry"]["attempted"], True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["failureKind"], "provider_quota")

    def test_earlier_attempt_quota_does_not_label_a_clean_empty_final_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            code, payload = self._run_pi(
                workspace,
                # An attempt with no stdout at all is what the empty-result retry
                # retries, so the first attempt's quota refusal is on stderr.
                [
                    "printf 'ERROR: 429 Too Many Requests: You have hit your usage limit.\\n' >&2",
                    ":",
                ],
            )
        self.assertEqual(payload["emptyRetry"]["attempted"], True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["failureKind"], "no_assistant_text")
        self.assertNotEqual(payload.get("failureReason"), "usage_limit")

    def test_failed_over_attempt_quota_event_does_not_label_a_clean_empty_final_attempt(
        self,
    ) -> None:
        # The primary attempt's structured 429 drives an auth failover; the
        # fallback exits 0 with no text and a clean stderr. The primary's error event stays in the record but must
        # not make the outcome a quota failure (which would also skip the
        # workflow's structured correction retry).
        error = shlex.quote(
            json.dumps({"type": "error", "message": "429 RESOURCE_EXHAUSTED: usage limit reached"})
        )
        turn = shlex.quote(json.dumps({"type": "turn.completed"}))
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            script = Path(workspace) / "codex"
            script.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "${ATTEMPT}" = primary ]; then\n'
                f"  printf '%s\\n' {error}\n"
                '  printf "usage limit\\n" >&2\n'
                "  exit 1\n"
                "fi\n"
                'if [[ "$*" == *"Delegate retry instruction"* ]]; then\n'
                f"  printf '%s\\n' {turn}\n"
                "fi\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="codex")
            ctx = runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="call",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-09-24T00:00:00Z",
                group="workflow",
                call_read_only=True,
                env_overrides={"ATTEMPT": "primary"},
                codex_failover_identity=f"auth={workspace}/primary/auth.json\0profile=",
                codex_fallback_failover_identity=f"auth={workspace}/fallback/auth.json\0profile=",
                fallback_env_overrides={"ATTEMPT": "fallback"},
            )
            with mock.patch.dict("os.environ", {"HOME": home}, clear=False):
                code, payload = runner.execute_tracked(
                    [str(script), "task"],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    manifest_argv=[str(script), "<prompt>"],
                )
        assert payload is not None
        self.assertTrue(payload["codexAuthFallback"]["triggered"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["failureKind"], "no_assistant_text")
        self.assertNotEqual(payload.get("failureReason"), "usage_limit")
