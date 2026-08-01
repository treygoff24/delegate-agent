from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import request_build
from tests.delegate_commands_test_base import CommandTestBase


class ResumeFixture(CommandTestBase):
    def setUp(self):
        super().setUp()
        self.workspace_temp = tempfile.TemporaryDirectory(prefix="delegate-resume-")
        self.addCleanup(self.workspace_temp.cleanup)
        self.workspace = Path(self.workspace_temp.name)
        self.registry_root = self.delegate.run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def write_config(self, config: dict | None = None) -> None:
        Path(self._config_env["DELEGATE_CONFIG"]).write_text(
            json.dumps(config or {}), encoding="utf-8"
        )

    def seed_run(
        self,
        *,
        engine: str = "codex",
        mode: str = "work",
        status: str = "succeeded",
        manifest: dict | None = None,
        snapshot: dict | None = None,
        prompt: str = "original prompt bytes\nsecond line",
    ) -> tuple[str, str, Path]:
        run_id, alias = self.delegate.run_registry.register_run(
            self.registry_root,
            harness=engine,
            metadata={"engine": engine, "mode": mode, "cwd": str(self.workspace)},
        )
        run_path = self.delegate.run_registry.run_directory(self.registry_root, run_id)
        started = "2026-07-31T18:00:00Z"
        source_manifest = {
            "schema": self.delegate.run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": engine,
            "engine": engine,
            "mode": mode,
            "cwd": str(self.workspace),
            "startedAt": started,
        }
        source_manifest.update(manifest or {})
        source_state = {
            "schema": self.delegate.run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "lastActivityAt": started,
        }
        source_snapshot = {
            "schema": self.delegate.run_registry.SNAPSHOT_SCHEMA,
            "ok": True,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "assistantText": "snapshot assistant text",
            "assistantTextTruncated": False,
            "recentEvents": [],
        }
        if snapshot:
            source_snapshot.update(snapshot)
        self.delegate.run_registry.write_json_atomic(run_path / "manifest.json", source_manifest)
        self.delegate.run_registry.write_json_atomic(run_path / "state.json", source_state)
        self.delegate.run_registry.write_json_atomic(run_path / "snapshot.json", source_snapshot)
        self.delegate.run_registry.write_private_text(run_path / "prompt.txt", prompt)
        self.delegate.run_registry.write_private_text(
            run_path / "completion-report.md", "terminal completion report"
        )
        return run_id, alias, run_path

    def run_resume(self, args: list[str]) -> tuple[dict, str]:
        code, stdout, stderr = self.run_main(
            [
                "--json",
                "--cwd",
                str(self.workspace),
                "resume",
                *args,
            ]
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"], payload)
        return payload, stderr

    def loaded_config(self) -> dict:
        with mock.patch.dict(os.environ, self._config_env, clear=False):
            config, _source = self.delegate.load_config(workspace=self.workspace)
        return config


class ResumeInheritanceTests(ResumeFixture):
    def test_codex_dry_run_inherits_table_and_honors_overrides(self):
        codex_home = Path(self._config_env["HOME"]) / "codex-source"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text("{}", encoding="utf-8")
        self.write_config(
            {
                "codex": {"defaultModel": "gpt-5.4"},
                "profiles": {
                    "definitions": {"source-profile": {"env": {"CODEX_HOME": "$HOME/codex-source"}}}
                },
                "progress": {"enabled": True},
            }
        )
        _run_id, alias, _run_path = self.seed_run(
            manifest={
                "modelAlias": "gpt-5.4",
                "modelRequested": "source-requested",
                "modelResolved": "source-resolved",
                "requestedReasoningEffort": "high",
                "resolvedReasoningEffort": "high",
                "reasoningEffortSource": "cli",
                "requestedFast": True,
                "progressRequested": "on",
                "timeoutSeconds": 73,
                "group": "source-group",
                "authProfile": "source-profile",
                "commitPolicy": {"forbidCommit": True},
                "includeDirty": True,
                "isolationMode": "none",
            }
        )

        inherited, stderr = self.run_resume(["--dry-run", alias, "continue it"])

        self.assertEqual(inherited["engine"], "codex")
        self.assertEqual(inherited["mode"], "work")
        self.assertEqual(inherited["modelRequested"], "gpt-5.4")
        self.assertEqual(inherited["resolvedReasoningEffort"], "high")
        self.assertEqual(inherited["reasoningEffortSource"], "cli")
        self.assertTrue(inherited["requestedFast"])
        self.assertTrue(inherited["progressRequested"])
        self.assertEqual(inherited["timeoutSeconds"], 73)
        self.assertEqual(inherited["group"], "source-group")
        self.assertEqual(inherited["authProfile"], "source-profile")
        self.assertEqual(inherited["commitPolicy"], {"forbidCommit": True})
        self.assertEqual(inherited["resumedFrom"]["alias"], alias)
        self.assertNotIn("includeDirty", inherited)
        self.assertIn("includeDirty is creation-only", stderr)

        overridden, _stderr = self.run_resume(
            [
                "--model",
                "gpt-5.4-mini",
                "--reasoning-effort",
                "low",
                "--no-fast",
                "--no-progress",
                "--timeout",
                "9",
                "--dry-run",
                alias,
                "continue with explicit settings",
            ]
        )
        self.assertEqual(overridden["modelRequested"], "gpt-5.4-mini")
        self.assertEqual(overridden["resolvedReasoningEffort"], "low")
        self.assertEqual(overridden["requestedFast"], False)
        self.assertNotIn("progressRequested", overridden)
        self.assertEqual(overridden["timeoutSeconds"], 9)

        fast_override, _stderr = self.run_resume(
            ["--fast", "--dry-run", alias, "continue with fast service"]
        )
        self.assertTrue(fast_override["requestedFast"])

    def test_reasoning_from_config_is_reresolved_against_target(self):
        self.write_config(
            {"codex": {"defaultModel": "gpt-5.4", "defaultReasoningEffort": "medium"}}
        )
        _run_id, alias, _run_path = self.seed_run(
            manifest={
                "requestedReasoningEffort": "high",
                "resolvedReasoningEffort": "high",
                "reasoningEffortSource": "config",
                "isolationMode": "none",
            }
        )

        payload, _stderr = self.run_resume(["--dry-run", alias, "re-resolve effort"])

        self.assertEqual(payload["resolvedReasoningEffort"], "medium")
        self.assertEqual(payload["reasoningEffortSource"], "config")

    def test_reasoning_from_input_json_is_inherited_as_an_explicit_request(self):
        self.write_config({})
        _run_id, alias, _run_path = self.seed_run(
            manifest={
                "requestedReasoningEffort": "high",
                "resolvedReasoningEffort": "high",
                "reasoningEffortSource": "input-json",
                "isolationMode": "none",
            }
        )

        payload, _stderr = self.run_resume(["--dry-run", alias, "keep explicit effort"])

        self.assertEqual(payload["resolvedReasoningEffort"], "high")
        self.assertEqual(payload["reasoningEffortSource"], "cli")

    def test_model_resolved_is_used_when_alias_and_requested_model_are_absent(self):
        self.write_config({})
        _run_id, alias, _run_path = self.seed_run(
            manifest={"modelResolved": "gpt-5.4", "isolationMode": "none"}
        )

        payload, _stderr = self.run_resume(["--dry-run", alias, "use resolved model"])

        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["modelResolved"], "gpt-5.4")

    def test_progress_omitted_uses_target_default_and_emits_note(self):
        self.write_config({"progress": {"enabled": True}})
        _run_id, alias, _run_path = self.seed_run(manifest={"isolationMode": "none"})

        payload, stderr = self.run_resume(["--dry-run", alias, "use target default"])

        self.assertTrue(payload["progressRequested"])
        self.assertIn("resume note:", stderr)
        self.assertIn("target progress configuration", stderr)

        _run_id, off_alias, _run_path = self.seed_run(
            manifest={"progressRequested": "off", "isolationMode": "none"}
        )
        off_payload, off_stderr = self.run_resume(["--dry-run", off_alias, "keep progress off"])
        self.assertNotIn("progressRequested", off_payload)
        self.assertNotIn("progress intent absent", off_stderr)

    def test_mode_is_not_overridable_and_cross_engine_drops_source_fields(self):
        self.write_config({"cursor": {"defaultModel": "cursor-default"}})
        _run_id, alias, _run_path = self.seed_run(
            engine="codex",
            mode="safe",
            manifest={
                "modelAlias": "codex-source",
                "requestedReasoningEffort": "high",
                "reasoningEffortSource": "cli",
                "requestedFast": True,
                "progressRequested": "on",
                "outputSchema": '{"type":"object","properties":{"ok":{"type":"boolean"}}}',
                "agent": "source-agent",
                "isolationMode": "none",
            },
        )

        payload, stderr = self.run_resume(
            ["--engine", "cursor", "--dry-run", alias, "cross engine"]
        )

        self.assertEqual(payload["engine"], "cursor")
        self.assertEqual(payload["mode"], "safe")
        self.assertNotIn("modelAlias", payload)
        self.assertNotIn("requestedFast", payload)
        self.assertNotIn("agent", payload)
        self.assertNotIn("--output-schema", payload["argv"])
        self.assertIn("resume note:", stderr)
        self.assertIn("model selection", stderr)
        self.assertIn("reasoning effort dropped", stderr)
        self.assertIn("fast-tier selection dropped", stderr)
        self.assertIn("output schema dropped", stderr)

    def test_schema_text_stays_in_memory_until_normal_launch_materialization(self):
        schema_text = (
            '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}\n'
        )
        self.write_config({})
        _run_id, alias, _run_path = self.seed_run(
            manifest={"outputSchema": schema_text, "isolationMode": "none"}
        )
        parsed = self.delegate.parse_cli(
            ["--json", "--cwd", str(self.workspace), "resume", "--dry-run", alias, "next"]
        )

        with mock.patch.dict(os.environ, self._config_env, clear=False):
            plan = self.delegate.resume_command.build_resume_plan(
                parsed,
                self.delegate.ResolvedWorkspace(str(self.workspace), "directory"),
                self.loaded_config(),
                stderr=io.StringIO(),
            )
        self.assertIsNone(plan.parsed.launch.output_schema)
        self.assertEqual(plan.parsed.launch.output_schema_text, schema_text)
        self.assertFalse(list((self.workspace / ".delegate" / "tmp").glob("resume-schema-*")))
        with mock.patch.object(request_build, "resolve_output_schema") as resolve:
            request = self.delegate.request_from_parsed(
                plan.parsed, self.loaded_config(), io.StringIO()
            )
        resolve.assert_not_called()
        self.assertIn(request_build.INLINE_OUTPUT_SCHEMA_PLACEHOLDER, request.argv)
        self.assertIsNotNone(request.output_schema_text)

    def test_inherited_timeout_accepts_integral_float_and_refuses_invalid_values(self):
        self.write_config({})
        _run_id, float_alias, _run_path = self.seed_run(
            manifest={"timeoutSeconds": 12.0, "isolationMode": "none"}
        )
        payload, _stderr = self.run_resume(["--dry-run", float_alias, "continue"])
        self.assertEqual(payload["timeoutSeconds"], 12)

        for timeout in (0, -1, 1.5, True, "12"):
            with self.subTest(timeout=timeout):
                _run_id, alias, _run_path = self.seed_run(
                    manifest={"timeoutSeconds": timeout, "isolationMode": "none"}
                )
                code, stdout, _stderr = self.run_main(
                    [
                        "--json",
                        "--cwd",
                        str(self.workspace),
                        "resume",
                        "--dry-run",
                        alias,
                        "continue",
                    ]
                )
                self.assertEqual(code, self.delegate.EXIT_USAGE)
                self.assertEqual(json.loads(stdout)["error"], "resume_record_invalid")

    def test_runs_prune_removes_legacy_crash_orphaned_resume_schema(self):
        self.write_config({})
        tmp = self.registry_root / "tmp"
        tmp.mkdir()
        orphan = tmp / "resume-schema-crashed.json"
        orphan.write_text('{"type":"object"}', encoding="utf-8")

        payload = self.delegate.run_registry.prune_runs(self.registry_root, older_than_days=30)

        self.assertFalse(orphan.exists())
        self.assertEqual(payload["staleResumeSchemas"], [orphan.name])

    def test_opencode_agent_is_threaded_through_dry_run(self):
        self.write_config({"opencode": {"defaultModel": "open-model"}})
        _run_id, alias, _run_path = self.seed_run(
            engine="opencode",
            manifest={"agent": "reviewer", "isolationMode": "none"},
        )

        payload, _stderr = self.run_resume(["--dry-run", alias, "continue"])

        self.assertEqual(payload["engine"], "opencode")
        self.assertEqual(payload["agent"], "reviewer")


if __name__ == "__main__":
    unittest.main()
