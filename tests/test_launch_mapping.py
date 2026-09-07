"""Request policy propagation and CLI/JSON planning parity, without providers."""

import io
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from delegate_agent import (
    cli,
    cli_parser,
    errors,
    harness_events,
    request_build,
    run_metadata,
    runner,
    worktree_execution,
)
from delegate_agent import config as delegate_config
from delegate_agent.isolation import IsolationContext
from delegate_agent.request_models import Request, ResolvedWorkspace
from tests.execution_test_base import ExecutionTestBase


class LaunchMappingTests(ExecutionTestBase):
    def test_policy_sentinels_survive_each_context_path(self):
        # This checks actual constructors, not provider execution. Each location
        # has distinct cleanup/environment ownership even though policy is shared.
        values = {
            "model_alias": "alias-a",
            "model_requested": "requested-model",
            "capability_model": "capability-model",
            "capability_model_source": "config",
            "continuity_mode": "pinned",
            "reasoning_effort": "high",
            "requested_reasoning_effort": "high",
            "reasoning_effort_source": "cli",
            "reasoning_capability_source": "fixture",
            "reasoning_capability_evidence": "exact",
            "reasoning_transport": "fixture-transport",
            "fast": False,
            "prompt_transport": "stdin",
            "forbid_commit": True,
            "progress_initial_delay_sec": 4.5,
            "progress_interval_sec": 8.5,
            "stall_seconds": 91.0,
            "process_group_termination_grace_sec": 17.0,
            "registry_lock_timeout_seconds": 23.0,
            "auth_profile": "primary",
            "fallback_auth_profile": "fallback",
            "codex_failover_identity": "primary-id",
            "codex_fallback_failover_identity": "fallback-id",
            "mail_push": True,
            "resumable": True,
            "followup_of": "prior-run",
            "resume_session_id": "session-a",
            "structured_retry": True,
            "group": "group-a",
            "notify": "channel:test",
            "workflow_agent_key": "key-a",
            "pure": True,
            "prompt_instruction_mode": "wrapped",
            "source_prompt": "source prompt",
            "progress_requested": "off",
            "agent": "agent-a",
            "resumed_from": {"runId": "prior-run"},
            "persona_name": "reviewer",
            "persona_source": "user",
            "persona_transport": "native-file",
            "persona_digest": "a" * 64,
            "persona_file": "persona.txt",
            "persona_text": "Review carefully",
            "account_binding_command": ("identity",),
        }
        with tempfile.TemporaryDirectory() as source:
            root = Path(source) / ".delegate"
            workspace = ResolvedWorkspace(source, "directory")
            request = Request(
                "codex",
                "work",
                source,
                "prompt",
                [],
                "resolved-model",
                **values,
                output_schema="schema.json",
                output_schema_record_text='{"type":"object"}',
                timeout=67,
                env_overrides={"EXAMPLE": "retained"},
                temporary_workspace_cleanup={"sourceWorkspace": source},
            )
            for lane in ("direct", "temporary", "persistent", "attached", "grouped-call"):
                with self.subTest(lane=lane):
                    isolated = lane in {"temporary", "persistent", "attached"}
                    context = IsolationContext(
                        source_workspace=source,
                        effective_isolation="worktree" if isolated else "none",
                        isolation_mode="worktree" if isolated else "none",
                        isolation_lifecycle=lane if isolated else "none",
                        preserved_workspace=lane == "persistent",
                    )
                    current = replace(
                        request,
                        isolation_context=context,
                        mode="call" if lane == "grouped-call" else "work",
                        launch_cwd=str(Path(source) / lane),
                    )
                    if lane == "persistent":
                        execution = worktree_execution.PersistentWorktreeExecution(
                            current,
                            True,
                            delegate_config.embedded_default_config(),
                            False,
                            "none",
                            workspace,
                            io.StringIO(),
                            io.StringIO(),
                            lambda *_: None,
                        )
                        preflight = worktree_execution.PersistentWorktreePreflight(
                            context,
                            source,
                            "head",
                            None,
                            "head",
                            None,
                            None,
                            root,
                            0,
                            0,
                            (),
                            None,
                        )
                        ctx = worktree_execution._build_persistent_worktree_run_context(
                            execution,
                            preflight,
                            run_id="run-a",
                            alias="alias-a",
                            branch="branch-a",
                            worktree_path=current.launch_cwd,
                            creation_context={"includeDirty": True, "syncedFiles": 2},
                        )
                        self.assertIsNone(ctx.temporary_workspace_cleanup)
                        self.assertTrue(ctx.include_dirty)
                        self.assertEqual(ctx.synced_files, 2)
                        self.assertEqual(ctx.env_overrides["WORKSPACE_ROOT"], current.launch_cwd)
                    else:
                        ctx = cli.make_run_context(
                            root,
                            current,
                            run_id="run-a",
                            alias="alias-a",
                            source_workspace=workspace,
                        )
                        self.assertEqual(
                            ctx.temporary_workspace_cleanup, request.temporary_workspace_cleanup
                        )
                        self.assertFalse(ctx.include_dirty)
                    for name, expected in values.items():
                        self.assertEqual(getattr(ctx, name), expected, name)
                    self.assertTrue(ctx.call_read_only)
                    self.assertEqual(ctx.timeout_seconds, 67)
                    self.assertEqual(ctx.model_resolved, "resolved-model")
                    self.assertTrue(ctx.structured_output)
                    self.assertEqual(ctx.output_schema_text, request.output_schema_record_text)
                    self.assertEqual(ctx.execution_cwd, current.launch_cwd)
                    self.assertEqual(ctx.isolated_workspace, isolated)
                    self.assertEqual(ctx.env_overrides["EXAMPLE"], "retained")
                    ctx.env_overrides["EXAMPLE"] = "changed"
                    self.assertEqual(request.env_overrides["EXAMPLE"], "retained")

    def test_cli_json_share_isolation_and_launch_planning(self):
        repo, _ = self._make_git_repo_with_commit()
        for mode, isolation, extra, json_extra in (
            ("work", "none", [], {}),
            ("safe", "auto", [], {}),
            (
                "work",
                "worktree",
                ["--forbid-commit", "--include-dirty"],
                {"forbidCommit": True, "includeDirty": True},
            ),
            ("work", None, ["--forbid-commit"], {"forbidCommit": True}),
        ):
            with (
                self.subTest(mode=mode, isolation=isolation),
                tempfile.TemporaryDirectory() as temp,
            ):
                common = ["--cwd", repo.name]
                raw = {
                    "engine": "codex",
                    "mode": mode,
                    "cwd": repo.name,
                    "prompt": "task",
                    "continuityMode": "panel",
                    **json_extra,
                }
                if isolation is not None:
                    common.extend(["--isolation", isolation])
                    raw["isolation"] = isolation
                cli = cli_parser.parse_cli(
                    [*common, "codex", mode, "--continuity-mode", "panel", *extra, "task"]
                )
                path = Path(temp) / "request.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                json_command = cli_parser.parse_cli(["run", "--input-json", str(path)])
                with mock.patch.object(request_build, "build_request") as build:
                    request_build.request_from_parsed(
                        cli, delegate_config.embedded_default_config(), io.StringIO()
                    )
                    cli_call = build.call_args
                    request_build.request_from_input_json(
                        json_command, delegate_config.embedded_default_config()
                    )
                    json_call = build.call_args
                # Both inputs reach the same final launch builder and resolved
                # isolation object; JSON-only fields stay in their own parser.
                self.assertEqual(cli_call.args[:6], json_call.args[:6])
                expected_lifecycle = (
                    "temporary"
                    if mode == "safe"
                    else "none"
                    if isolation == "none"
                    else "persistent"
                )
                self.assertEqual(
                    cli_call.kwargs["isolation_context"].isolation_lifecycle,
                    expected_lifecycle,
                )
                for key in (
                    "isolation_context",
                    "warnings",
                    "progress",
                    "progress_initial_delay_sec",
                    "progress_interval_sec",
                    "forbid_commit",
                    "include_dirty",
                    "continuity_mode",
                    "source_prompt",
                    "prompt_instruction_mode",
                    "completion_report_mode",
                ):
                    self.assertEqual(cli_call.kwargs[key], json_call.kwargs[key], key)

    def test_json_absence_null_and_cli_isolation_precedence_remain_distinct(self):
        repo, _ = self._make_git_repo_with_commit()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "request.json"
            raw = {"engine": "codex", "mode": "work", "cwd": repo.name, "prompt": "task"}
            for isolation in (None, "worktree"):
                with self.subTest(isolation=isolation):
                    path.write_text(json.dumps({**raw, "isolation": isolation}), encoding="utf-8")
                    parsed = cli_parser.parse_cli(
                        ["--isolation", "none", "run", "--input-json", str(path)]
                    )
                    with mock.patch.object(request_build, "build_request") as build:
                        if isolation is None:
                            with self.assertRaises(errors.DelegateError) as error:
                                request_build.request_from_input_json(
                                    parsed, delegate_config.embedded_default_config()
                                )
                            self.assertEqual(error.exception.error, "invalid_isolation")
                            build.assert_not_called()
                        else:
                            request_build.request_from_input_json(
                                parsed, delegate_config.embedded_default_config()
                            )
                            self.assertEqual(
                                build.call_args.kwargs["isolation_context"].effective_isolation,
                                "none",
                            )

    def test_unsupported_combinations_do_not_reach_launch_builder(self):
        repo, _ = self._make_git_repo_with_commit()
        cases = (
            (["codex", "call", "--include-dirty", "task"], "invalid_option_combination"),
            (
                ["--isolation", "none", "codex", "work", "--forbid-commit", "task"],
                "invalid_option_combination",
            ),
        )
        for args, expected in cases:
            with (
                self.subTest(args=args),
                mock.patch.object(request_build, "build_request") as build,
            ):
                with self.assertRaises(errors.DelegateError) as error:
                    parsed = cli_parser.parse_cli(["--cwd", repo.name, *args])
                    request_build.request_from_parsed(
                        parsed, delegate_config.embedded_default_config(), io.StringIO()
                    )
                self.assertEqual(error.exception.error, expected)
                build.assert_not_called()

    def test_persona_metadata_keeps_omission_null_and_filename_fallback(self):
        request = Request("codex", "work", "/example", "prompt", [], None)
        for persona, filename in (
            (None, None),
            ("reviewer", None),
            ("reviewer", ""),
            ("reviewer", "record.md"),
        ):
            with self.subTest(persona=persona, filename=filename):
                request.persona_name, request.persona_file = persona, filename
                dry = cli.dry_run_payload(request)
                tracked = {}
                run_metadata.add_persona_payload_fields(
                    tracked, request, default_file=runner.PERSONA_TXT_FILE
                )
                if persona is None:
                    self.assertNotIn("personaName", dry)
                    self.assertEqual(tracked, {})
                else:
                    self.assertEqual(dry["personaFile"], filename)
                    self.assertEqual(tracked["personaFile"], filename or runner.PERSONA_TXT_FILE)
                    self.assertIsNone(dry["personaSource"])

    def test_selection_projection_matches_dry_run_manifest_and_snapshot(self):
        with tempfile.TemporaryDirectory() as source:
            for model, effort, fast in ((None, None, None), ("model-a", "high", False)):
                with self.subTest(model=model, effort=effort, fast=fast):
                    request = Request(
                        "codex",
                        "work",
                        source,
                        "prompt",
                        [],
                        model,
                        model_requested=model,
                        reasoning_effort=effort,
                        requested_reasoning_effort=effort,
                        fast=fast,
                    )
                    ctx = cli.make_run_context(
                        Path(source),
                        request,
                        run_id="run-a",
                        alias="alias-a",
                        source_workspace=ResolvedWorkspace(source, "directory"),
                    )
                    projections = (
                        cli.dry_run_payload(request),
                        runner.build_manifest(ctx, []),
                        runner.build_run_record(
                            ctx,
                            accumulator=harness_events.StreamAccumulator(harness="codex"),
                            status="running",
                        ),
                    )
                    keys = (
                        *run_metadata.MODEL_METADATA_KEYS,
                        *run_metadata.REASONING_METADATA_KEYS,
                        *run_metadata.SPEED_METADATA_KEYS,
                    )
                    selected = [
                        {key: payload[key] for key in keys if key in payload}
                        for payload in projections
                    ]
                    self.assertEqual(selected[0], selected[1])
                    record = projections[2]
                    provenance = record["modelProvenance"]
                    self.assertEqual(provenance["requestedModel"], model)
                    self.assertEqual(provenance["resolvedModel"], model)
                    self.assertEqual(record["status"], "running")
                    self.assertEqual(selected[0]["modelRequested"], model)
                    if fast is None:
                        self.assertNotIn("requestedFast", selected[0])
                    else:
                        self.assertIs(selected[0]["requestedFast"], False)
