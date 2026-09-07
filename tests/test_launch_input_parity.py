"""CLI/JSON normalization contracts; real builders, no provider execution."""

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from delegate_agent import cli_parser, config, request_build, run_registry
from delegate_agent.errors import DelegateError


class LaunchInputParityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (
            ("init",),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "fixture",
            ),
        ):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        # Discovery is a separate boundary. Neither frontend may launch a
        # provider while these tests compare their built requests.
        patch = mock.patch.object(
            request_build, "_runtime_discovery_for_engine", return_value=(None, ())
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.config = config.embedded_default_config()
        self.config["codex"]["defaultModel"] = "model-test"
        self.config["codex"]["models"] = {"alias": "model-test"}
        self.config["reasoning"]["capabilities"] = {
            "codex": {"model-test": {"supported": ["low", "high"], "default": "low"}}
        }
        self.schema = self.root / "schema.json"
        self.schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
            )
        )
        for base in (self.home, self.repo):
            directory = base / ".delegate" / "personas"
            directory.mkdir(parents=True)
            (
                directory / ("global-reviewer.md" if base == self.home else "repo-reviewer.md")
            ).write_text("Review carefully.\n")

    def pair(
        self,
        *,
        engine="codex",
        mode="work",
        options=(),
        values=None,
        globals=(),
        prompt="task",
        settings=None,
    ):
        cfg = copy.deepcopy(self.config if settings is None else settings)
        location = [] if mode == "call" else ["--cwd", str(self.repo)]
        cli = cli_parser.parse_cli([*location, *globals, engine, mode, *options, prompt])
        raw = {"engine": engine, "mode": mode, "prompt": prompt, **(values or {})}
        if mode != "call":
            raw["cwd"] = str(self.repo)
        path = self.root / "input.json"
        path.write_text(json.dumps(raw))
        json_command = cli_parser.parse_cli([*globals, "run", "--input-json", str(path)])
        left = request_build.request_from_parsed(cli, cfg, io.StringIO())
        if left.cleanup_workspace:
            self.addCleanup(shutil.rmtree, left.workspace, True)
        right = request_build.request_from_input_json(json_command, cfg)
        if right.cleanup_workspace:
            self.addCleanup(shutil.rmtree, right.workspace, True)
        return left, right

    def assert_equivalent(self, left, right):
        fields = (
            "engine",
            "mode",
            "model",
            "model_alias",
            "model_requested",
            "capability_model",
            "reasoning_effort",
            "requested_reasoning_effort",
            "fast",
            "progress",
            "progress_initial_delay_sec",
            "progress_interval_sec",
            "progress_requested",
            "timeout",
            "forbid_commit",
            "include_dirty",
            "group",
            "notify",
            "auth_profile",
            "persona_name",
            "persona_source",
            "persona_transport",
            "persona_digest",
            "persona_text",
            "allow_repo_persona",
            "output_schema",
            "output_schema_text",
            "output_schema_record_text",
            "call_read_only",
            "pure",
            "resumable",
            "resume_session_id",
            "mail_push",
            "continuity_mode",
            "prompt_instruction_mode",
            "source_prompt",
            "prompt",
            "prompt_transport",
            "completion_report_mode",
            "cleanup_workspace",
        )
        for field in fields:
            self.assertEqual(getattr(left, field), getattr(right, field), field)
        self.assertEqual(left.isolation_context, right.isolation_context)
        left_argv = (
            [value.replace(left.workspace, "<call>") for value in left.argv]
            if left.mode == "call"
            else left.argv
        )
        right_argv = (
            [value.replace(right.workspace, "<call>") for value in right.argv]
            if right.mode == "call"
            else right.argv
        )
        self.assertEqual(left_argv, right_argv)
        # Provenance names the input channel, not a semantic disagreement.
        if left.requested_reasoning_effort is not None:
            expected = (
                ("config", "config")
                if left.reasoning_effort_source == "config"
                else ("cli", "input-json")
            )
            self.assertEqual(
                (left.reasoning_effort_source, right.reasoning_effort_source), expected
            )

    def test_cross_cutting_option_matrix(self):
        cases = (
            ("defaults", (), {}, (), {}),
            ("model-alias", ("--model", "alias"), {"model": "alias"}, (), {"model": "model-test"}),
            (
                "reasoning",
                ("--reasoning-effort", "high"),
                {"reasoningEffort": "high"},
                (),
                {"reasoning_effort": "high"},
            ),
            ("fast", ("--fast",), {"fast": True}, (), {"fast": True}),
            ("not-fast", ("--no-fast",), {"fast": False}, (), {"fast": False}),
            ("progress", ("--progress",), {"progress": True}, (), {"progress": True}),
            ("no-progress", ("--no-progress",), {"progress": False}, (), {"progress": False}),
            ("timeout", ("--timeout", "73"), {"timeout": 73}, (), {"timeout": 73}),
            (
                "commit-policy",
                ("--forbid-commit",),
                {"forbidCommit": True},
                (),
                {"forbid_commit": True},
            ),
            (
                "dirty-policy",
                ("--include-dirty",),
                {"includeDirty": True},
                ("--isolation", "worktree"),
                {"include_dirty": True},
            ),
            (
                "completion",
                (),
                {},
                ("--completion-report", "none"),
                {"completion_report_mode": "none"},
            ),
            (
                "persona",
                ("--persona", "global-reviewer"),
                {"persona": "global-reviewer"},
                (),
                {"persona_name": "global-reviewer"},
            ),
            (
                "schema",
                ("--output-schema", str(self.schema)),
                {"outputSchema": str(self.schema)},
                (),
                {"output_schema": str(self.schema)},
            ),
            ("resumable", ("--resumable",), {"resumable": True}, (), {"resumable": True}),
            (
                "pinned",
                ("--continuity-mode", "pinned"),
                {"continuityMode": "pinned"},
                (),
                {"continuity_mode": "pinned"},
            ),
            (
                "panel",
                ("--continuity-mode", "panel"),
                {"continuityMode": "panel"},
                (),
                {"continuity_mode": "panel"},
            ),
            (
                "group-notify",
                (),
                {},
                ("--group", "fixture", "--notify", "channel:test"),
                {"group": "fixture", "notify": "channel:test"},
            ),
            ("mail", ("--mail-push",), {"mailPush": True}, (), {"mail_push": True}),
        )
        for name, options, values, globals, expected in cases:
            with self.subTest(name=name):
                settings = copy.deepcopy(self.config)
                if name == "mail":
                    settings["mail"]["enabled"] = True
                left, right = self.pair(
                    options=options, values=values, globals=globals, settings=settings
                )
                self.assert_equivalent(left, right)
                for field, value in expected.items():
                    self.assertEqual(getattr(left, field), value, field)

    def test_call_read_only_pure_schema_and_quiet_timing(self):
        for engine, options, values in (
            ("codex", ("--read-only", "--timeout", "41"), {"readOnly": True, "timeout": 41}),
            ("claude", ("--pure",), {"pure": True}),
            ("codex", ("--output-schema", str(self.schema)), {"outputSchema": str(self.schema)}),
        ):
            with self.subTest(engine=engine, options=options):
                left, right = self.pair(engine=engine, mode="call", options=options, values=values)
                self.assert_equivalent(left, right)
                self.assertFalse(left.progress)
                self.assertIsNone(left.isolation_context)

    def test_config_defaults_and_explicit_progress_override(self):
        cfg = copy.deepcopy(self.config)
        cfg["progress"] = {"enabled": True, "initialDelaySec": 7, "intervalSec": 13}
        cfg["codex"]["defaultReasoningEffort"] = "low"
        left, right = self.pair(
            settings=cfg, options=("--no-progress",), values={"progress": False}
        )
        self.assert_equivalent(left, right)
        self.assertEqual(
            (left.progress, left.progress_initial_delay_sec, left.progress_interval_sec),
            (False, 7, 13),
        )
        self.assertEqual(left.reasoning_effort, "low")

    def test_repo_persona_permission_and_tracked_slash(self):
        left, right = self.pair(
            mode="safe",
            options=("--persona", "repo-reviewer", "--allow-repo-persona"),
            values={"persona": "repo-reviewer", "allowRepoPersona": True},
        )
        self.assert_equivalent(left, right)
        self.assertTrue(left.allow_repo_persona)
        left, right = self.pair(prompt="/review this")
        self.assert_equivalent(left, right)
        self.assertEqual(left.prompt_instruction_mode, "slash-passthrough")

    def test_engine_model_and_agent_mapping(self):
        for engine in (
            "cursor",
            "droid",
            "codex",
            "claude",
            "grok",
            "devin",
            "opencode",
            "pi",
            "omp",
            "kimi",
        ):
            with self.subTest(engine=engine):
                cfg = copy.deepcopy(self.config)
                cfg[engine]["models"] = {
                    "alias": "provider/model-test" if engine == "opencode" else "model-test"
                }
                options = ["--model", "alias"]
                values = {"model": "alias"}
                if engine == "opencode":
                    options.extend(("--agent", "reviewer"))
                    values["agent"] = "reviewer"
                left, right = self.pair(engine=engine, options=options, values=values, settings=cfg)
                self.assert_equivalent(left, right)

    def test_droid_raw_model_plan_matches_its_resolved_alias_identity(self):
        left, right = self.pair(
            engine="droid",
            options=("--model", "model-external"),
            values={"model": "model-external"},
            globals=("--isolation", "worktree"),
        )
        self.assert_equivalent(left, right)
        self.assertIsNone(left.model_alias)
        self.assertEqual(left.model, "model-external")

    def test_documented_input_only_metadata_exceptions(self):
        left, right = self.pair(mode="call", prompt="/review this")
        self.assertEqual(left.prompt, right.prompt)
        self.assertEqual(
            (left.prompt_instruction_mode, right.prompt_instruction_mode),
            ("wrapped", "slash-passthrough"),
        )
        # JSON can explicitly suppress slash sniffing for interpolated content.
        left, right = self.pair(prompt="/review this", values={"promptInstructionMode": "wrapped"})
        self.assertEqual(left.prompt_instruction_mode, "slash-passthrough")
        self.assertEqual(right.prompt_instruction_mode, "wrapped")

    def test_invalid_json_instruction_shape_has_a_usage_error(self):
        with self.assertRaises(DelegateError) as error:
            self.pair(values={"promptInstructionMode": {"not": "a mode"}})
        self.assertEqual(error.exception.error, "invalid_prompt_instruction_mode")

    def test_auth_profile_and_null_defaults(self):
        cfg = copy.deepcopy(self.config)
        cfg["profiles"]["definitions"] = {
            "fixture": {
                "env": {
                    "DELEGATE_TEST_MARKER": "selected",
                    "CODEX_HOME": str(self.home / "codex-profile"),
                }
            }
        }
        left, right = self.pair(
            settings=cfg,
            globals=("--auth-profile", "fixture"),
            values={"fast": None, "model": None},
        )
        self.assert_equivalent(left, right)
        self.assertEqual(left.auth_profile, "fixture")
        self.assertEqual(left.env_overrides["DELEGATE_TEST_MARKER"], "selected")
        self.assertEqual(right.env_overrides["DELEGATE_TEST_MARKER"], "selected")

    def test_json_workflow_session_fields_keep_their_explicit_scope(self):
        left, right = self.pair(
            globals=("--group", "wf-fixture"),
            values={
                "workflowAgentKey": "agent-key",
                "structuredSession": True,
                "structuredRetryWorkspace": True,
            },
        )
        self.assertIsNone(left.workflow_agent_key)
        self.assertEqual(right.workflow_agent_key, "agent-key")
        self.assertFalse(left.structured_retry)
        self.assertTrue(right.structured_retry)
        self.assertIn("--ephemeral", left.argv)
        self.assertNotIn("--ephemeral", right.argv)

    def test_json_retry_reenters_only_the_verified_owner_and_session(self):
        target = self.root / "attached"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "-b",
                "delegate/fixture",
                str(target),
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
        root = run_registry.ensure_registry(self.repo, workspace_kind="git")
        run_id, _ = run_registry.register_run(root, harness="codex")
        path = run_registry.run_directory(root, run_id)
        run_registry.ensure_private_dir(path)
        manifest = {
            "engine": "codex",
            "group": "wf-fixture",
            "workflowAgentKey": "agent-key",
            "executionCwd": str(target),
            "sourceGitRoot": str(self.repo),
            "workspaceKind": "git",
            "isolationLifecycle": "persistent",
            "branch": "delegate/fixture",
        }
        run_registry.write_json_atomic(path / run_registry.MANIFEST_FILE, manifest)
        run_registry.write_json_atomic(
            path / run_registry.SNAPSHOT_FILE, {"sessionId": "session-fixture"}
        )
        values = {
            "workflowAgentKey": "agent-key",
            "structuredRetryRunId": run_id,
            "structuredRetrySessionId": "session-fixture",
            "structuredSession": True,
            "structuredRetryWorkspace": True,
        }
        _, retry = self.pair(globals=("--group", "wf-fixture"), values=values)
        self.assertEqual(retry.workspace, str(target))
        self.assertEqual(retry.isolation_context.isolation_lifecycle, "attached")
        self.assertEqual(retry.isolation_context.source_workspace, str(self.repo))
        self.assertEqual(retry.resume_session_id, "session-fixture")
        self.assertTrue(retry.structured_retry)
        self.assertIn("resume", retry.argv)
        self.assertEqual(retry.prompt, "task", "native retry must not frame the correction twice")
        for changed, expected in (
            ({"workflowAgentKey": "other"}, "structured_retry_run_invalid"),
            ({"structuredRetrySessionId": "other"}, "structured_retry_session_invalid"),
        ):
            with self.subTest(changed=changed), self.assertRaises(DelegateError) as error:
                self.pair(globals=("--group", "wf-fixture"), values={**values, **changed})
            self.assertEqual(error.exception.error, expected)

    def test_json_persona_digest_and_cli_inline_schema_remain_distinct_inputs(self):
        digest = hashlib.sha256(b"Review carefully.\n").hexdigest()
        left, right = self.pair(
            options=("--persona", "global-reviewer"),
            values={"persona": "global-reviewer", "expectedPersonaDigest": digest},
        )
        self.assert_equivalent(left, right)
        with self.assertRaises(DelegateError) as error:
            self.pair(
                options=("--persona", "global-reviewer"),
                values={"persona": "global-reviewer", "expectedPersonaDigest": "0" * 64},
            )
        self.assertEqual(error.exception.error, "workflow_persona_digest_mismatch")
        parsed = cli_parser.parse_cli(["--cwd", str(self.repo), "codex", "work", "task"])
        parsed.payload.output_schema_text = self.schema.read_text()
        inherited = request_build.request_from_parsed(parsed, self.config, io.StringIO())
        _, from_file = self.pair(values={"outputSchema": str(self.schema)})
        self.assertEqual(inherited.output_schema, request_build.INLINE_OUTPUT_SCHEMA_PLACEHOLDER)
        self.assertEqual(inherited.output_schema_record_text, from_file.output_schema_record_text)

    def test_negative_parity_control_detects_a_dropped_json_timeout(self):
        build = request_build._build_normalized_launch

        def broken(spec, config, **kwargs):
            if spec.origin == "input-json":
                spec = replace(spec, options=replace(spec.options, timeout=None))
            return build(spec, config, **kwargs)

        with mock.patch.object(request_build, "_build_normalized_launch", side_effect=broken):
            left, right = self.pair(options=("--timeout", "73"), values={"timeout": 73})
        with self.assertRaisesRegex(AssertionError, "timeout"):
            self.assert_equivalent(left, right)

    def test_json_call_empty_prompt_precedes_read_only_slash_refusal(self):
        path = self.root / "invalid-call.json"
        for prompt, expected in ((" ", "empty_prompt"), ("task", "slash_passthrough_unsupported")):
            path.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "call",
                        "prompt": prompt,
                        "readOnly": True,
                        "promptInstructionMode": "slash-passthrough",
                    }
                )
            )
            parsed = cli_parser.parse_cli(["run", "--input-json", str(path)])
            with (
                self.subTest(prompt=prompt),
                mock.patch.object(request_build, "_call_workspace") as allocate,
                self.assertRaises(DelegateError) as error,
            ):
                request_build.request_from_input_json(parsed, self.config)
            self.assertEqual(error.exception.error, expected)
            allocate.assert_not_called()

    def test_unsupported_combinations_keep_both_frontend_refusals(self):
        cases = (
            ("work", ("--read-only",), {"readOnly": True}, (), "invalid_option_combination"),
            ("call", ("--progress",), {"progress": True}, (), "invalid_option_combination"),
            ("safe", ("--resumable",), {"resumable": True}, (), "invalid_option_combination"),
            (
                "work",
                ("--forbid-commit",),
                {"forbidCommit": True},
                ("--isolation", "none"),
                "invalid_option_combination",
            ),
            (
                "work",
                ("--include-dirty",),
                {"includeDirty": True},
                ("--isolation", "none"),
                "invalid_option_combination",
            ),
        )
        for mode, options, values, globals, expected in cases:
            for origin in ("cli", "input-json"):
                with (
                    self.subTest(mode=mode, options=options, origin=origin),
                    self.assertRaises(DelegateError) as error,
                ):
                    if origin == "cli":
                        location = [] if mode == "call" else ["--cwd", str(self.repo)]
                        parsed = cli_parser.parse_cli(
                            [*location, *globals, "codex", mode, *options, "task"]
                        )
                        request_build.request_from_parsed(parsed, self.config, io.StringIO())
                    else:
                        path = self.root / "invalid.json"
                        raw = {"engine": "codex", "mode": mode, "prompt": "task", **values}
                        if mode != "call":
                            raw["cwd"] = str(self.repo)
                        path.write_text(json.dumps(raw))
                        parsed = cli_parser.parse_cli([*globals, "run", "--input-json", str(path)])
                        request_build.request_from_input_json(parsed, self.config)
                self.assertEqual(error.exception.error, expected)
