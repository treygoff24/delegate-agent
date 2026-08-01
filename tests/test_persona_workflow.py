from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from delegate_agent import config, describe_payload, run_registry, runner
from delegate_agent.cli import parse_cli, request_from_parsed
from delegate_agent.errors import DelegateError
from delegate_agent.workflows import registry as workflow_registry
from delegate_agent.workflows import runtime as workflow_runtime


class PersonaWorkflowTests(unittest.TestCase):
    old_text = "Old workflow persona\n"
    new_text = "Edited workflow persona\n"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        persona_dir = self.workspace / ".delegate" / "personas"
        persona_dir.mkdir(parents=True)
        (persona_dir / "editor.md").write_text(self.old_text, encoding="utf-8")

    def _state(
        self,
        *,
        dry_run: bool = False,
        wf_id: str = "wf_bbbbbbbbbbbb",
    ) -> workflow_runtime.WorkflowState:
        root = self.workspace / ".delegate" / "workflows" / wf_id
        run_registry.ensure_private_dir(root)
        return workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=self.workspace / "workflow.py",
            config=config.embedded_default_config(),
            cli_argv=["delegate"],
            args=None,
            budget=workflow_runtime.Budget(None),
            dry_run=dry_run,
        )

    def _dsl(self, state: workflow_runtime.WorkflowState) -> workflow_runtime.WorkflowDsl:
        return workflow_runtime.WorkflowDsl(
            state,
            {"defaults": {"engine": "cursor", "mode": "safe"}},
        )

    def _child_result(self, calls: list[dict[str, object]]):
        def run_child(argv, *, cwd, timeout):
            input_path = Path(argv[argv.index("--input-json") + 1])
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            calls.append(payload)
            digest = payload.get("expectedPersonaDigest")
            context = runner.RunContext(
                registry_root=self.workspace,
                run_id="del_persona_child",
                alias="persona-child",
                harness=str(payload["engine"]),
                engine=str(payload["engine"]),
                mode=str(payload["mode"]),
                model=None,
                source_cwd=str(self.workspace),
                execution_cwd=str(self.workspace),
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-08-01T00:00:00Z",
                persona_name=str(payload.get("persona") or "editor"),
                persona_source="workspace",
                persona_transport="prepend",
                persona_digest=str(digest) if digest is not None else None,
            )
            envelope = runner.completion_json_payload(
                context,
                ok=True,
                status="completed",
                exit_code=0,
                duration_ms=1,
                stdout_bytes=0,
                stderr_bytes=0,
            )
            envelope["text"] = "child result"
            return CompletedProcess(
                argv,
                0,
                json.dumps(envelope).encode(),
                b"",
            )

        return run_child

    def test_agent_opts_payload_dry_run_journal_and_describe_signature_chain(self) -> None:
        state = self._state()
        dsl = self._dsl(state)
        payloads: list[dict[str, object]] = []
        with mock.patch.object(
            workflow_runtime, "_run_child_command", side_effect=self._child_result(payloads)
        ):
            result = dsl.agent(
                "Review this workflow",
                engine="cursor",
                mode="safe",
                model="composer-2.5",
                effort="high",
                isolation="auto",
                persona="editor",
                allow_repo_persona=True,
            )
        self.assertEqual(result, "child result")
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(
            set(payload),
            {
                "engine",
                "mode",
                "prompt",
                "cwd",
                "model",
                "reasoningEffort",
                "isolation",
                "persona",
                "allowRepoPersona",
                "expectedPersonaDigest",
                "workflowAgentKey",
                "promptInstructionMode",
            },
        )
        self.assertEqual(payload["persona"], "editor")
        self.assertTrue(payload["allowRepoPersona"])
        self.assertEqual(payload["promptInstructionMode"], "wrapped")
        self.assertIsInstance(payload["workflowAgentKey"], str)
        self.assertEqual(
            payload["expectedPersonaDigest"], hashlib.sha256(self.old_text.encode()).hexdigest()
        )
        self.assertNotIn(self.old_text, json.dumps(payload))

    def test_real_completion_envelope_reports_matching_persona_digest(self) -> None:
        state = self._state()
        calls: list[dict[str, object]] = []
        with mock.patch.object(
            workflow_runtime, "_run_child_command", side_effect=self._child_result(calls)
        ):
            self.assertEqual(
                self._dsl(state).agent(
                    "Review this workflow",
                    engine="cursor",
                    mode="safe",
                    persona="editor",
                    allow_repo_persona=True,
                ),
                "child result",
            )
        self.assertEqual(len(calls), 1)
        payload = calls[0]

        live_events = workflow_registry.iter_journal(state.journal_path)
        self.assertEqual(
            [event["type"] for event in live_events],
            ["budget", "agent_started", "agent_child", "agent_finished"],
        )
        started = live_events[1]
        persona_digest = hashlib.sha256(self.old_text.encode("utf-8")).hexdigest()
        self.assertEqual(started["personaDigest"], persona_digest)
        self.assertEqual(started["personaSource"], "workspace")
        self.assertEqual(started["workflowAgentKey"], payload["workflowAgentKey"])

        dry_state = self._state(dry_run=True, wf_id="wf_cccccccccccc")
        self._dsl(dry_state).agent(
            "Review this workflow",
            engine="cursor",
            mode="safe",
            persona="editor",
            allow_repo_persona=True,
        )
        dry_entry = dry_state.dry_runs[0]
        self.assertEqual(
            set(dry_entry),
            {
                "scope",
                "engine",
                "mode",
                "model",
                "effort",
                "fast",
                "isolation",
                "promptBytes",
                "phase",
                "label",
                "schema",
                "persona",
                "personaSource",
                "personaDigest",
                "personaBytes",
            },
        )
        self.assertEqual(dry_entry["persona"], "editor")
        self.assertEqual(dry_entry["personaDigest"], persona_digest)
        self.assertEqual(dry_entry["personaBytes"], len(self.old_text.encode("utf-8")))
        dry_events = workflow_registry.iter_journal(dry_state.journal_path)
        self.assertEqual(
            [event["type"] for event in dry_events],
            ["budget", "agent_started", "agent_finished"],
        )
        self.assertEqual(dry_events[1]["personaDigest"], persona_digest)
        self.assertEqual(dry_events[1]["personaSource"], "workspace")

        described = describe_payload.describe_payload(
            config.embedded_default_config(), "embedded-default", self.workspace
        )
        signature = described["workflows"]["dsl"]["agent"]["signature"]
        self.assertEqual(
            signature,
            "agent(prompt, engine=None, mode=None, model=None, effort=None, "
            "schema=None, label=None, phase=None, isolation=None, passthrough=False, "
            "timeout=None, retries=None, persona=None, allow_repo_persona=False, fast=None)",
        )
        self.assertIn("persona=None", signature)
        self.assertIn("allow_repo_persona=False", signature)

    def test_persona_digest_is_structural_key_and_edited_file_is_cache_miss(self) -> None:
        state = self._state()
        first_calls: list[dict[str, object]] = []
        with mock.patch.object(
            workflow_runtime, "_run_child_command", side_effect=self._child_result(first_calls)
        ):
            first_result = self._dsl(state).agent(
                "same prompt",
                engine="cursor",
                mode="safe",
                persona="editor",
                allow_repo_persona=True,
            )
        self.assertEqual(first_result, "child result")
        first_events = workflow_registry.iter_journal(state.journal_path)
        first_started = next(event for event in first_events if event["type"] == "agent_started")
        first_key = first_started["key"]
        first_digest = first_started["personaDigest"]

        (self.workspace / ".delegate" / "personas" / "editor.md").write_text(
            self.new_text, encoding="utf-8"
        )
        resumed_state = self._state()
        second_calls: list[dict[str, object]] = []
        with mock.patch.object(
            workflow_runtime,
            "_run_child_command",
            side_effect=self._child_result(second_calls),
        ):
            second_result = self._dsl(resumed_state).agent(
                "same prompt",
                engine="cursor",
                mode="safe",
                persona="editor",
                allow_repo_persona=True,
            )
        self.assertEqual(second_result, "child result")
        self.assertEqual(len(second_calls), 1, "edited persona must not replay the old result")
        all_events = workflow_registry.iter_journal(resumed_state.journal_path)
        started = [event for event in all_events if event["type"] == "agent_started"]
        self.assertEqual(len(started), 2)
        self.assertNotEqual(started[0]["key"], started[1]["key"])
        self.assertEqual(started[0]["key"], first_key)
        self.assertEqual(started[0]["personaDigest"], first_digest)
        self.assertEqual(
            started[1]["personaDigest"],
            hashlib.sha256(self.new_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(second_calls[0]["persona"], "editor")
        self.assertEqual(second_calls[0]["allowRepoPersona"], True)

    def test_child_digest_mismatch_after_persona_mutation_fails_without_cache_entry(self) -> None:
        state = self._state()
        calls: list[dict[str, object]] = []

        def mutate_before_child_resolution(argv, *, cwd, timeout):
            input_path = Path(argv[argv.index("--input-json") + 1])
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            calls.append(payload)
            (self.workspace / ".delegate" / "personas" / "editor.md").write_text(
                self.new_text, encoding="utf-8"
            )
            with self.assertRaises(DelegateError) as caught:
                request_from_parsed(
                    parse_cli(["run", "--input-json", str(input_path)]),
                    config.embedded_default_config(),
                    mock.MagicMock(),
                )
            self.assertEqual(caught.exception.error, "workflow_persona_digest_mismatch")
            return CompletedProcess(
                argv,
                2,
                json.dumps(
                    {
                        "ok": False,
                        "error": "workflow_persona_digest_mismatch",
                    }
                ).encode(),
                b"",
            )

        with (
            self.assertRaises(workflow_runtime.PersonaDigestMismatch),
            mock.patch.object(
                workflow_runtime, "_run_child_command", side_effect=mutate_before_child_resolution
            ),
        ):
            self._dsl(state).agent(
                "same prompt",
                engine="cursor",
                mode="safe",
                persona="editor",
                allow_repo_persona=True,
            )
        key = calls[0]["workflowAgentKey"]
        self.assertNotIn(key, state.replay)
        self.assertNotIn(key, state.replay_keys)

    def test_post_pin_persona_resolution_error_does_not_cache_none(self) -> None:
        state = self._state()
        calls: list[dict[str, object]] = []

        def delete_before_child_resolution(argv, *, cwd, timeout):
            input_path = Path(argv[argv.index("--input-json") + 1])
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            calls.append(payload)
            (self.workspace / ".delegate" / "personas" / "editor.md").unlink()
            return CompletedProcess(
                argv,
                2,
                json.dumps({"ok": False, "error": "persona_not_found"}).encode(),
                b"",
            )

        with (
            self.assertRaises(workflow_runtime.PersonaDigestMismatch),
            mock.patch.object(
                workflow_runtime, "_run_child_command", side_effect=delete_before_child_resolution
            ),
        ):
            self._dsl(state).agent(
                "same prompt",
                engine="cursor",
                mode="safe",
                persona="editor",
                allow_repo_persona=True,
            )
        key = calls[0]["workflowAgentKey"]
        self.assertNotIn(key, state.replay)
        self.assertNotIn(key, state.replay_keys)

        (self.workspace / ".delegate" / "personas" / "editor.md").write_text(
            self.old_text, encoding="utf-8"
        )
        restored_calls: list[dict[str, object]] = []
        with mock.patch.object(
            workflow_runtime, "_run_child_command", side_effect=self._child_result(restored_calls)
        ):
            self.assertEqual(
                self._dsl(state).agent(
                    "same prompt",
                    engine="cursor",
                    mode="safe",
                    persona="editor",
                    allow_repo_persona=True,
                ),
                "child result",
            )
        self.assertEqual(len(restored_calls), 1)

    def test_post_pin_workspace_refusal_does_not_cache_none(self) -> None:
        workspace_persona = self.workspace / ".delegate" / "personas" / "editor.md"
        workspace_persona.unlink()
        fake_home = self.workspace / "home"
        global_persona = fake_home / ".delegate" / "personas" / "editor.md"
        global_persona.parent.mkdir(parents=True)
        global_persona.write_text(self.old_text, encoding="utf-8")
        state = self._state()
        calls: list[dict[str, object]] = []

        def create_workspace_shadow(argv, *, cwd, timeout):
            input_path = Path(argv[argv.index("--input-json") + 1])
            calls.append(json.loads(input_path.read_text(encoding="utf-8")))
            workspace_persona.write_text("shadow", encoding="utf-8")
            return CompletedProcess(
                argv,
                2,
                json.dumps({"ok": False, "error": "workspace_persona_refused"}).encode(),
                b"",
            )

        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            with (
                self.assertRaises(workflow_runtime.PersonaDigestMismatch),
                mock.patch.object(
                    workflow_runtime, "_run_child_command", side_effect=create_workspace_shadow
                ),
            ):
                self._dsl(state).agent(
                    "same prompt", engine="cursor", mode="safe", persona="editor"
                )
            key = calls[0]["workflowAgentKey"]
            self.assertNotIn(key, state.replay)
            self.assertNotIn(key, state.replay_keys)

            workspace_persona.unlink()
            restored_calls: list[dict[str, object]] = []
            with mock.patch.object(
                workflow_runtime,
                "_run_child_command",
                side_effect=self._child_result(restored_calls),
            ):
                self.assertEqual(
                    self._dsl(state).agent(
                        "same prompt", engine="cursor", mode="safe", persona="editor"
                    ),
                    "child result",
                )
        self.assertEqual(len(restored_calls), 1)


if __name__ == "__main__":
    unittest.main()
