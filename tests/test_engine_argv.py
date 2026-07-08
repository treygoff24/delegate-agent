import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase, make_git_repo


class EngineArgvTests(CommandTestBase):
    def test_cursor_safe_argv_agent_prefix(self):
        argv = self.delegate.build_cursor_argv(["agent"], "safe", "/repo", "composer-2.5", "hello")
        self.assertEqual(argv[0], "agent")
        self.assertEqual(
            argv[1:10],
            [
                "--workspace",
                "/repo",
                "-p",
                "--trust",
                "--model",
                "composer-2.5",
                "--print",
                "--output-format",
                "stream-json",
            ],
        )
        self.assertTrue(argv[10].startswith(self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE["cursor"]))
        self.assertTrue(argv[10].endswith("hello"))
        self.assertNotIn("--mode=plan", argv)
        self.assertNotIn("--mode=ask", argv)
        self.assertNotIn("--force", argv)
        self.assertNotIn("--approve-mcps", argv)

    def test_safe_review_prefix_is_read_only_text_only(self):
        for engine, prefix in self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE.items():
            with self.subTest(engine=engine):
                lowered = prefix.lower()
                self.assertIn("read-only review/investigation", lowered)
                self.assertIn("propose patches or commands in text", lowered)
                self.assertIn("do not edit", lowered)
                self.assertIn("do not", lowered)
                self.assertNotIn("plan mode", lowered)
                self.assertNotIn("implement the change", lowered)

    def test_cursor_work_argv_cursor_agent_prefix(self):
        argv = self.delegate.build_cursor_argv(
            ["cursor", "agent"], "work", "/repo", "composer-2.5", "hello"
        )
        self.assertEqual(
            argv,
            [
                "cursor",
                "agent",
                "--workspace",
                "/repo",
                "-p",
                "--trust",
                "--approve-mcps",
                "--force",
                "--model",
                "composer-2.5",
                "--print",
                "--output-format",
                "stream-json",
                "hello",
            ],
        )
        self.assertNotIn("--mode=agent", argv)
        self.assertNotIn("--mode=plan", argv)
        self.assertNotIn("--mode=ask", argv)

    def test_droid_safe_argv(self):
        argv = self.delegate.build_droid_argv("droid", "safe", "/repo", "model-id", "hello")
        self.assertEqual(
            argv[:-1],
            [
                "droid",
                "exec",
                "--cwd",
                "/repo",
                "--model",
                "model-id",
                "--output-format",
                "stream-json",
            ],
        )
        self.assertTrue(argv[-1].startswith(self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE["droid"]))
        self.assertTrue(argv[-1].endswith("hello"))
        self.assertNotIn("--auto", argv)
        self.assertNotIn("--use-spec", argv)
        self.assertNotIn("--skip-permissions-unsafe", argv)

    def test_droid_work_argv(self):
        argv = self.delegate.build_droid_argv("droid", "work", "/repo", "model-id", "hello")
        self.assertEqual(
            argv,
            [
                "droid",
                "exec",
                "--cwd",
                "/repo",
                "--skip-permissions-unsafe",
                "--model",
                "model-id",
                "--output-format",
                "stream-json",
                "hello",
            ],
        )

    def test_pass_through_restores_text_argv(self):
        cursor = self.delegate.build_cursor_argv(
            ["agent"], "work", "/repo", "composer-2.5", "hello", stream_capture=False
        )
        self.assertIn("--output-format", cursor)
        self.assertIn("text", cursor)
        self.assertNotIn("--print", cursor)
        droid = self.delegate.build_droid_argv(
            "droid", "safe", "/repo", "model-id", "hello", stream_capture=False
        )
        self.assertNotIn("--output-format", droid)

    def test_invalid_alias_rejected_before_argv(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request(
                "droid", "safe", "nope", "/repo", "hello", self.delegate.DEFAULT_CONFIG, True
            )
        self.assertEqual(ctx.exception.error, "invalid_alias")

    def test_build_request_requires_resolved_workspace_boundary(self):
        with self.assertRaisesRegex(TypeError, "build_request requires a ResolvedWorkspace"):
            self.delegate.build_request(  # type: ignore[arg-type]
                "cursor",
                "safe",
                None,
                "/repo",
                "hello",
                self.delegate.DEFAULT_CONFIG,
                True,
            )

    def test_placeholder_droid_model_rejected_before_argv(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"my-model": "replace-with-your-droid-model-id"}
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request("droid", "safe", "my-model", "/repo", "hello", config, True)
        self.assertEqual(ctx.exception.error, "unconfigured_model")
        self.assertIn("placeholder", ctx.exception.message)

        config["droid"]["models"] = {"my-model": "your-droid-model-id"}
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request("droid", "safe", "my-model", "/repo", "hello", config, True)
        self.assertEqual(ctx.exception.error, "unconfigured_model")

    def test_codex_work_default_argv_uses_workspace_sandbox_with_network(self):
        policy = self.delegate.delegate_config.effective_policy(
            self.delegate.DEFAULT_CONFIG,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            self.delegate.DEFAULT_CONFIG["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        exec_index = argv.index("exec")
        self.assertIn("--ask-for-approval", argv[:exec_index])
        self.assertEqual(
            argv[argv.index("--ask-for-approval") + 1],
            "never",
        )
        self.assertIn("--sandbox", argv[exec_index:])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn("-c", argv[exec_index:])
        self.assertIn("sandbox_workspace_write.network_access=true", argv[exec_index:])
        self.assertIn("--json", argv[exec_index:])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_codex_work_trusted_hooks_argv_adds_hook_bypass_only(self):
        config = self.delegate.delegate_config.deep_merge(
            self.delegate.DEFAULT_CONFIG,
            {"policy": {"profile": "trusted-hooks"}},
        )
        policy = self.delegate.delegate_config.effective_policy(
            config,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            config["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        exec_index = argv.index("exec")
        self.assertIn("--dangerously-bypass-hook-trust", argv[exec_index:])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--sandbox", argv[exec_index:])
        self.assertIn("--ask-for-approval", argv[:exec_index])

    def test_codex_work_web_search_argv_when_enabled(self):
        config = self.delegate.delegate_config.deep_merge(
            self.delegate.DEFAULT_CONFIG,
            {"policy": {"work": {"webSearch": True}}},
        )
        policy = self.delegate.delegate_config.effective_policy(
            config,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            config["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        self.assertIn("--search", argv[: argv.index("exec")])

    def test_codex_default_model_null_omits_model_flag(self):
        policy = self.delegate.delegate_config.effective_policy(
            self.delegate.DEFAULT_CONFIG,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            self.delegate.DEFAULT_CONFIG["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        self.assertNotIn("--model", argv)

    def test_codex_output_schema_argv_after_exec(self):
        policy = self.delegate.delegate_config.effective_policy(
            self.delegate.DEFAULT_CONFIG,
            engine="codex",
            mode="safe",
        )
        argv = self.delegate.build_codex_argv(
            self.delegate.DEFAULT_CONFIG["codex"],
            "safe",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
            output_schema="/tmp/schema.json",
        )
        exec_index = argv.index("exec")
        schema_index = argv.index("--output-schema")
        self.assertGreater(schema_index, exec_index)
        self.assertEqual(argv[schema_index + 1], "/tmp/schema.json")

    def test_codex_dry_run_model_null_is_allowed(self):
        request = self.build_git_request(
            "codex",
            "work",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIsNone(payload["model"])
        self.assertNotIn("--model", payload["argv"])

    def test_codex_reasoning_effort_argv_uses_config_override(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultModel"] = "gpt-5.5"
        request = self.build_git_request(
            "codex",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
            reasoning_effort="high",
        )
        exec_index = request.argv.index("exec")
        self.assertIn("-c", request.argv[:exec_index])
        self.assertIn('model_reasoning_effort="high"', request.argv[:exec_index])
        self.assertEqual(request.reasoning_transport, "codex-config")

    def test_codex_reasoning_effort_without_model_uses_harness_default(self):
        request = self.build_git_request(
            "codex",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            True,
            reasoning_effort="high",
        )
        self.assertIsNone(request.model)
        self.assertNotIn("--model", request.argv)
        self.assertIn('model_reasoning_effort="high"', request.argv)
        self.assertEqual(request.reasoning_capability_source, "harness-default")

    def test_droid_reasoning_effort_argv_uses_flag(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        request = self.build_git_request(
            "droid",
            "safe",
            "reviewer",
            "/repo",
            "hello",
            config,
            True,
            reasoning_effort="xhigh",
        )
        self.assertIn("--reasoning-effort", request.argv)
        self.assertIn("xhigh", request.argv)
        self.assertNotIn("--skip-permissions-unsafe", request.argv)

    def test_codex_and_droid_requests_keep_prompt_out_of_argv(self):
        secret_prompt = "TOP-SECRET-PROMPT"
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultModel"] = "gpt-5.5"
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}

        codex = self.build_git_request(
            "codex",
            "work",
            None,
            "/repo",
            secret_prompt,
            config,
            True,
        )
        self.assertEqual(codex.prompt_transport, "stdin")
        self.assertIsNotNone(codex.stdin_text)
        self.assertIn(secret_prompt, codex.stdin_text)
        self.assertEqual(codex.argv[-1], "-")
        self.assertNotIn(secret_prompt, json.dumps(codex.argv))
        codex_payload = self.delegate.dry_run_payload(codex)
        self.assertEqual(codex_payload["promptTransport"], "stdin")
        self.assertNotIn(secret_prompt, json.dumps(codex_payload["argv"]))

        droid = self.build_git_request(
            "droid",
            "safe",
            "reviewer",
            "/repo",
            secret_prompt,
            config,
            True,
        )
        self.assertEqual(droid.prompt_transport, "file")
        self.assertIsNone(droid.stdin_text)
        self.assertIsNotNone(droid.prompt_file_text)
        self.assertTrue(
            droid.prompt_file_text.startswith(self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE["droid"])
        )
        self.assertIn(secret_prompt, droid.prompt_file_text)
        self.assertIn("--file", droid.argv)
        self.assertIn(self.delegate.DROID_PROMPT_FILE_ARG_PLACEHOLDER, droid.argv)
        self.assertNotIn(secret_prompt, json.dumps(droid.argv))
        droid_payload = self.delegate.dry_run_payload(droid)
        self.assertEqual(droid_payload["promptTransport"], "file")
        self.assertIn("--file", droid_payload["argv"])
        self.assertIn(self.delegate.DROID_PROMPT_FILE_DISPLAY, droid_payload["argv"])
        self.assertNotIn(secret_prompt, json.dumps(droid_payload["argv"]))

    def test_droid_safe_request_injects_safe_prefix_once_after_skill_prompt(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        parsed = self.delegate.ParsedCommand(
            "droid",
            global_options=self.delegate.GlobalOptions(cwd=repo.name),
            launch=self.delegate.LaunchOptions(
                "droid",
                "safe",
                model_alias="reviewer",
                prompt_parts=["review the diff"],
            ),
        )

        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))

        prompt = request.prompt_file_text
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertTrue(prompt.startswith(self.delegate.delegate_runner.SKILL_REVIEW_PREFIX))
        self.assertEqual(prompt.count(self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE["droid"]), 1)
        self.assertGreater(
            prompt.find(self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE["droid"]),
            prompt.find("Delegate sub-agent skill review"),
        )
        self.assertIn("review the diff", prompt)

    def test_cursor_dry_run_redacts_prompt_argv_tail(self):
        secret_prompt = "TOP-SECRET-CURSOR-PROMPT"
        request = self.build_git_request(
            "cursor",
            "work",
            None,
            "/repo",
            secret_prompt,
            self.delegate.DEFAULT_CONFIG,
            True,
        )
        self.assertEqual(request.prompt_transport, "argv")
        self.assertIn(secret_prompt, request.argv[-1])

        payload = self.delegate.dry_run_payload(request)

        self.assertEqual(payload["promptTransport"], "argv")
        self.assertEqual(payload["argv"][-1], self.delegate.CURSOR_PROMPT_REDACTION)
        self.assertNotIn(secret_prompt, json.dumps(payload["argv"]))

    def test_build_request_uses_cache_declared_custom_model_capability(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "custom:cached"}
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / ".delegate" / "capabilities" / "reasoning.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "harnesses": {
                            "droid": {
                                "models": {
                                    "custom:cached": {
                                        "supported": ["high"],
                                        "default": "high",
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            request = self.build_git_request(
                "droid",
                "safe",
                "reviewer",
                tmp,
                "hello",
                config,
                True,
                reasoning_effort="high",
            )
        self.assertIn("--reasoning-effort", request.argv)
        self.assertEqual(request.reasoning_capability_source, "cache")

    def test_cursor_reasoning_effort_requires_mapping(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request(
                "cursor",
                "safe",
                None,
                "/repo",
                "hello",
                self.delegate.DEFAULT_CONFIG,
                True,
                reasoning_effort="high",
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_cursor_reasoning_effort_uses_configured_model_mapping(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["cursor"]["reasoningEffortModels"] = {"high": "sonnet-4-thinking"}
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
            reasoning_effort="high",
        )
        self.assertEqual(request.model, "sonnet-4-thinking")
        self.assertIn("--model", request.argv)
        self.assertIn("sonnet-4-thinking", request.argv)
        self.assertEqual(request.reasoning_transport, "cursor-model-selection")

    def test_codex_default_reasoning_effort_is_used_when_request_omits_effort(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultModel"] = "gpt-5.5"
        config["codex"]["defaultReasoningEffort"] = "medium"
        request = self.build_git_request(
            "codex",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
        )
        self.assertEqual(request.reasoning_effort_source, "config")
        self.assertIn('model_reasoning_effort="medium"', request.argv)

    def test_codex_config_default_effort_degrades_to_warning_without_model(self):
        # A config defaultReasoningEffort must not brick the engine when no
        # model resolves; the run proceeds without effort and carries a warning.
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultReasoningEffort"] = "medium"
        self.assertIsNone(config["codex"]["defaultModel"])
        request = self.build_git_request(
            "codex",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
        )
        self.assertIsNone(request.reasoning_effort)
        self.assertNotIn("model_reasoning_effort", " ".join(request.argv))
        self.assertEqual(len(request.warnings), 1)
        self.assertIn("defaultReasoningEffort", request.warnings[0])
        payload = self.delegate.dry_run_payload(request)
        self.assertNotIn("requestedReasoningEffort", payload)
        self.assertIn("warnings", payload)

    def test_cursor_config_default_effort_degrades_to_warning_without_mapping(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["cursor"]["defaultReasoningEffort"] = "high"
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
        )
        self.assertIsNone(request.reasoning_effort)
        self.assertEqual(request.model, config["cursor"]["defaultModel"])
        self.assertEqual(len(request.warnings), 1)
        self.assertIn("defaultReasoningEffort", request.warnings[0])

    def test_explicit_effort_without_model_uses_harness_default(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        self.assertIsNone(config["codex"]["defaultModel"])
        request = self.build_git_request(
            "codex",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
            reasoning_effort="high",
        )
        self.assertIn('model_reasoning_effort="high"', request.argv)
        self.assertEqual(request.reasoning_capability_source, "harness-default")

    def test_corrupt_capability_cache_does_not_block_bundled_resolution(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultModel"] = "gpt-5.5"
        with tempfile.TemporaryDirectory() as workspace:
            cache_path = Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {"harnesses": {"codex": {"models": {"gpt-5.5": {"supported": "high"}}}}}
                ),
                encoding="utf-8",
            )
            request = self.build_git_request(
                "codex",
                "safe",
                None,
                workspace,
                "hello",
                config,
                True,
                reasoning_effort="high",
            )
        self.assertIn('model_reasoning_effort="high"', request.argv)
        self.assertEqual(request.reasoning_capability_source, "bundled")

    def test_capabilities_refresh_overwrites_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as workspace:
            cache_path = Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text('{"not": "a cache"}', encoding="utf-8")
            fake_bin = self.write_fake_executable(
                "codex",
                stdout=json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-refresh",
                                "default_reasoning_level": "medium",
                                "supported_reasoning_levels": [{"effort": "medium"}],
                            }
                        ]
                    }
                ),
            )
            code, _out, err = self.run_main(
                ["--cwd", workspace, "--json", "capabilities", "refresh"],
                path_prefix=fake_bin,
            )
            self.assertEqual(code, 0, err)
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("gpt-refresh", cache["harnesses"]["codex"]["models"])
            if os.name == "posix":
                self.assertEqual(cache_path.stat().st_mode & 0o777, 0o600)

    def test_request_from_parsed_threads_cli_reasoning_effort(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultModel"] = "gpt-5.5"
        with tempfile.TemporaryDirectory() as tmp:
            parsed = self.delegate.parse_cli(
                ["--cwd", tmp, "codex", "safe", "--reasoning-effort", "high", "review"]
            )
            request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
            payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["requestedReasoningEffort"], "high")
        self.assertEqual(payload["reasoningEffortSource"], "cli")
        self.assertIn('model_reasoning_effort="high"', payload["argv"])

    def test_request_from_input_json_threads_reasoning_effort(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "safe",
                        "model": "gpt-5.5",
                        "cwd": tmp,
                        "reasoningEffort": "high",
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            request = self.delegate.request_from_input_json(parsed, config)
        self.assertEqual(request.reasoning_effort_source, "input-json")
        self.assertIn('model_reasoning_effort="high"', request.argv)

    def test_request_from_input_json_threads_output_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "safe",
                        "cwd": tmp,
                        "outputSchema": str(schema),
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(request.output_schema, str(schema.resolve()))
        schema_index = request.argv.index("--output-schema")
        self.assertEqual(request.argv[schema_index + 1], str(schema.resolve()))
        self.assertNotIn(
            self.delegate.delegate_runner.COMPLETION_REPORT_SUFFIX.strip(), request.prompt
        )
        self.assertTrue(any("JSON-only final message" in warning for warning in request.warnings))

    def test_request_from_input_json_rejects_output_schema_for_non_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "safe",
                        "cwd": tmp,
                        "outputSchema": str(schema),
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(ctx.exception.error, "unsupported_output_schema")

    def test_input_json_effort_overrides_provider_default(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultReasoningEffort"] = "medium"
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "safe",
                        "model": "gpt-5.5",
                        "cwd": tmp,
                        "reasoningEffort": "high",
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            request = self.delegate.request_from_input_json(parsed, config)
        self.assertIn('model_reasoning_effort="high"', request.argv)
        self.assertNotIn('model_reasoning_effort="medium"', request.argv)

    def test_per_run_effort_overrides_provider_default(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultModel"] = "gpt-5.5"
        config["codex"]["defaultReasoningEffort"] = "medium"
        with tempfile.TemporaryDirectory() as tmp:
            parsed = self.delegate.parse_cli(
                ["--cwd", tmp, "codex", "safe", "--reasoning-effort", "high", "review"]
            )
            request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertIn('model_reasoning_effort="high"', request.argv)
        self.assertNotIn('model_reasoning_effort="medium"', request.argv)

    def test_capabilities_json_reports_reasoning_matrix(self):
        code, out, err = self.run_main(["--json", "capabilities"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertIn("reasoning", payload)
        self.assertIn("codex", payload["reasoning"]["harnesses"])

    def test_capabilities_json_reports_cache_source_when_cache_exists(self):
        with tempfile.TemporaryDirectory() as workspace:
            cache_path = Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "harnesses": {
                            "codex": {
                                "models": {
                                    "gpt-test": {
                                        "supported": ["low"],
                                        "default": "low",
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            code, out, err = self.run_main(["--cwd", workspace, "--json", "capabilities"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(
            payload["reasoning"]["harnesses"]["codex"]["models"]["gpt-test"]["source"],
            "cache",
        )

    def test_capabilities_refresh_writes_valid_cache_from_fake_codex(self):
        with tempfile.TemporaryDirectory() as workspace:
            fake_bin = self.write_fake_executable(
                "codex",
                stdout=json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-refresh",
                                "default_reasoning_level": "medium",
                                "supported_reasoning_levels": [
                                    {"effort": "low"},
                                    {"effort": "medium"},
                                ],
                            }
                        ]
                    }
                ),
            )
            code, _out, err = self.run_main(
                ["--cwd", workspace, "--json", "capabilities", "refresh"],
                path_prefix=fake_bin,
            )
            self.assertEqual(code, 0, err)
            cache = json.loads(
                (Path(workspace) / ".delegate" / "capabilities" / "reasoning.json").read_text()
            )
        self.assertIn("gpt-refresh", cache["harnesses"]["codex"]["models"])

    def test_capabilities_refresh_uses_auth_profile_env(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            empty_path = root / "empty-path"
            codex_home = root / "codex-home"
            marker = root / "codex-home-seen"
            bin_dir.mkdir()
            empty_path.mkdir()
            codex_home.mkdir()
            fake_codex = bin_dir / "codex"
            marker_literal = str(marker).replace("'", "'\"'\"'")
            fake_codex.write_text(
                "#!/bin/sh\n"
                f"printf '%s' \"${{CODEX_HOME:-}}\" > '{marker_literal}'\n"
                "printf '%s\\n' "
                '\'{"models":[{"slug":"gpt-profile","default_reasoning_level":"medium",'
                '"supported_reasoning_levels":[{"effort":"medium"}]}]}\'\n',
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            config_path = Path(self._config_env["DELEGATE_CONFIG"])
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "detectFrom": [],
                            "default": None,
                            "definitions": {
                                "work": {
                                    "env": {
                                        "CODEX_HOME": str(codex_home),
                                        "PATH": str(bin_dir),
                                    }
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(
                os.environ,
                {
                    "DELEGATE_CONFIG": str(config_path),
                    "PATH": str(empty_path),
                },
                clear=False,
            ):
                code = self.delegate.main(
                    [
                        "--cwd",
                        workspace,
                        "--auth-profile",
                        "work",
                        "--json",
                        "capabilities",
                        "refresh",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(marker.read_text(encoding="utf-8"), str(codex_home))
            payload = json.loads(stdout.getvalue())
            self.assertIn("gpt-profile", payload["reasoning"]["harnesses"]["codex"]["models"])

    def test_capabilities_refresh_preserves_non_codex_cache_entries(self):
        with tempfile.TemporaryDirectory() as workspace:
            cache_path = Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "harnesses": {
                            "droid": {
                                "models": {
                                    "custom:cached": {
                                        "supported": ["high"],
                                        "default": "high",
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            fake_bin = self.write_fake_executable(
                "codex",
                stdout=json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-refresh",
                                "default_reasoning_level": "medium",
                                "supported_reasoning_levels": [{"effort": "medium"}],
                            }
                        ]
                    }
                ),
            )
            code, _out, err = self.run_main(
                ["--cwd", workspace, "--json", "capabilities", "refresh"],
                path_prefix=fake_bin,
            )
            self.assertEqual(code, 0, err)
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertIn("gpt-refresh", cache["harnesses"]["codex"]["models"])
        self.assertIn("custom:cached", cache["harnesses"]["droid"]["models"])

    def test_capabilities_refresh_invalid_data_does_not_mutate_existing_cache(self):
        with tempfile.TemporaryDirectory() as workspace:
            cache_path = Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                '{"schema":1,"harnesses":{"codex":{"models":{"old":{"supported":["low"],"default":"low"}}}}}',
                encoding="utf-8",
            )
            fake_bin = self.write_fake_executable("codex", stdout='{"models":[{"slug":"bad"}]}')
            code, out, _err = self.run_main(
                ["--cwd", workspace, "--json", "capabilities", "refresh"],
                path_prefix=fake_bin,
            )
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["error"], "capability_refresh_failed")
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertIn("old", cache["harnesses"]["codex"]["models"])

    def test_capabilities_refresh_subprocess_failure_reports_error(self):
        with tempfile.TemporaryDirectory() as workspace:
            fake_bin = self.write_fake_executable("codex", stderr="boom", exit_code=1)
            code, out, _err = self.run_main(
                ["--cwd", workspace, "--json", "capabilities", "refresh"],
                path_prefix=fake_bin,
            )
        payload = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "capability_refresh_failed")
        self.assertIn("boom", payload["message"])

    def test_run_input_json_codex_allows_omitted_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "hello",
                    }
                )
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertIsNone(request.model)
            self.assertNotIn("--model", request.argv)

    def test_run_input_json_codex_rejects_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "hello",
                        "profile": "my-profile",
                    }
                )
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "invalid_input_key")
            self.assertIn("profile", ctx.exception.message)

    def test_describe_preserves_safe_read_only_modes(self):
        payload = self.delegate.describe_payload(self.delegate.DEFAULT_CONFIG, "embedded-default")
        self.assertIn("promptTransforms", payload)
        self.assertTrue(payload["engineCapabilities"]["codex"]["outputSchema"])
        self.assertEqual(payload["engineDefaults"]["devin"]["binary"], "devin")
        self.assertEqual(payload["engineDefaults"]["devin"]["defaultModel"], "swe-1.7")
        for engine in ("cursor", "droid", "kimi", "claude", "grok", "devin"):
            with self.subTest(engine=engine):
                self.assertFalse(payload["engineCapabilities"][engine]["outputSchema"])
        self.assertIn("skill review", payload["promptTransforms"][0])
        cursor_safe = payload["modeMapping"]["cursor"]["safe"]
        self.assertNotIn("--mode=plan", cursor_safe)
        self.assertNotIn("--mode=ask", cursor_safe)
        self.assertNotIn("--force", cursor_safe)
        self.assertNotIn("--approve-mcps", cursor_safe)
        self.assertIn("<isolated-workspace>", cursor_safe)
        self.assertIn("safeNotes", payload["modeMapping"]["cursor"])
        codex_safe = payload["modeMapping"]["codex"]["safe"]
        self.assertIn("--sandbox", codex_safe)
        self.assertIn("read-only", codex_safe)
        self.assertIn("safeNotes", payload["modeMapping"]["codex"])
        self.assertIn("isolated", payload["modeMapping"]["codex"]["safeNotes"][0].lower())
        self.assertEqual(payload["promptTransports"]["droid"], "file")
        self.assertIn("--file", payload["modeMapping"]["droid"]["safe"])
        self.assertIn(
            self.delegate.DROID_PROMPT_FILE_DISPLAY, payload["modeMapping"]["droid"]["safe"]
        )
        self.assertIn("<isolated-workspace>", payload["modeMapping"]["droid"]["safe"])
        self.assertFalse(payload["isolation"]["safeNoneAllowed"]["droid"])
        self.assertTrue(payload["isolation"]["safeNoneAllowed"]["codex"])
        self.assertIn("safeNotes", payload["modeMapping"]["droid"])
        self.assertIn("isolation none", payload["modeMapping"]["droid"]["safeNotes"][2])
        self.assertNotIn("--auto", payload["modeMapping"]["droid"]["safe"])
        self.assertNotIn("--use-spec", payload["modeMapping"]["droid"]["safe"])
        self.assertNotIn("--skip-permissions-unsafe", payload["modeMapping"]["droid"]["safe"])
        kimi_safe = payload["modeMapping"]["kimi"]["safe"]
        kimi_work = payload["modeMapping"]["kimi"]["work"]
        self.assertNotIn("--plan", kimi_safe)
        self.assertNotIn("--yolo", kimi_safe)
        self.assertNotIn("--auto", kimi_safe)
        self.assertNotIn("--yolo", kimi_work)
        self.assertIn("--prompt", kimi_work)
        self.assertNotIn("--auto", kimi_work)
        devin_safe = payload["modeMapping"]["devin"]["safe"]
        devin_work = payload["modeMapping"]["devin"]["work"]
        self.assertEqual(payload["promptTransports"]["devin"], "file")
        self.assertIn("--agent-config", devin_safe)
        self.assertIn(self.delegate.DEVIN_AGENT_CONFIG_DISPLAY, devin_safe)
        self.assertIn("--permission-mode auto", " ".join(devin_safe))
        self.assertIn("--prompt-file", devin_safe)
        self.assertIn(self.delegate.PROMPT_FILE_DISPLAY, devin_safe)
        self.assertFalse(payload["isolation"]["safeNoneAllowed"]["devin"])
        self.assertIn("--permission-mode dangerous", " ".join(devin_work))
        self.assertNotIn("--agent-config", devin_work)

    def test_grok_safe_argv_uses_prompt_file_and_read_only_controls(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        prompt = self.delegate.effective_prompt(
            "review task",
            engine="grok",
            mode="safe",
            completion_report_mode="none",
        )
        request = self.delegate.build_request(
            "grok",
            "safe",
            None,
            self.delegate.ResolvedWorkspace("/repo", "git"),
            prompt,
            config,
            dry_run=True,
        )
        self.assertEqual(request.prompt_transport, "file")
        self.assertIn("Delegate Grok safe mode", request.prompt_file_text or "")
        self.assertIn("--prompt-file", request.argv)
        self.assertIn("--output-format", request.argv)
        self.assertIn("streaming-json", request.argv)
        self.assertIn("--permission-mode", request.argv)
        self.assertIn("dontAsk", request.argv)
        self.assertIn("--sandbox", request.argv)
        self.assertIn("read-only", request.argv)
        self.assertIn("--disable-web-search", request.argv)
        self.assertIn(self.delegate.PROMPT_FILE_DISPLAY, request.display_argv or [])
        self.assertNotIn("review task", request.display_argv or [])

    def test_grok_reasoning_effort_reports_static_capability_source(self):
        request = self.build_git_request(
            "grok",
            "safe",
            None,
            "/repo",
            "review task",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
            reasoning_effort="high",
        )
        self.assertIn("--effort", request.argv)
        self.assertIn("high", request.argv)
        self.assertEqual(request.reasoning_capability_source, "static")
        payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["reasoningCapabilitySource"], "static")

    def test_grok_work_harness_bypass_requires_harness_scoped_policy(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["policy"]["profile"] = "external-sandbox"
        request = self.delegate.build_request(
            "grok",
            "work",
            None,
            self.delegate.ResolvedWorkspace("/repo", "git"),
            "implement",
            config,
            dry_run=True,
        )
        self.assertNotIn("bypassPermissions", request.argv)
        config["policy"]["harness"] = {"grok": {"work": {"bypassApprovalsAndSandbox": True}}}
        request = self.delegate.build_request(
            "grok",
            "work",
            None,
            self.delegate.ResolvedWorkspace("/repo", "git"),
            "implement",
            config,
            dry_run=True,
        )
        self.assertIn("bypassPermissions", request.argv)
        self.assertIn("--always-approve", request.argv)

    def test_grok_work_bypass_also_requires_effective_policy_true(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        argv = self.delegate.build_grok_argv(
            config["grok"],
            "work",
            "/repo",
            None,
            {},
            allow_bypass_permissions=True,
        )
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--always-approve", argv)

    def test_grok_output_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            parsed = self.delegate.parse_cli(
                ["--cwd", tmp, "grok", "work", "--output-schema", str(schema), "task"],
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_parsed(
                    parsed,
                    self.delegate.DEFAULT_CONFIG,
                    io.StringIO(""),
                )
        self.assertEqual(ctx.exception.error, "unsupported_output_schema")
        self.assertIn("grok", ctx.exception.message.lower())

    def test_devin_safe_argv_uses_prompt_file_and_agent_config(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        request = self.build_git_request(
            "devin",
            "safe",
            None,
            "/repo",
            "review task",
            config,
            dry_run=True,
        )
        self.assertEqual(request.model, "swe-1.7")
        self.assertEqual(request.prompt_transport, "file")
        self.assertEqual(request.prompt_file_text, "review task")
        self.assertEqual(
            json.loads(request.agent_config_text or "{}"),
            {
                "permissions": {
                    "allow": ["read", "grep", "glob", "Read(/**)"],
                    "deny": ["edit", "write", "exec", "Write(/**)", "mcp__*"],
                }
            },
        )
        self.assertIn("--agent-config", request.argv)
        self.assertIn(self.delegate.DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER, request.argv)
        self.assertIn("--permission-mode", request.argv)
        self.assertIn("auto", request.argv)
        self.assertIn("--prompt-file", request.argv)
        self.assertIn(self.delegate.PROMPT_FILE_ARG_PLACEHOLDER, request.argv)
        self.assertEqual(request.argv[-1], "-p")
        self.assertIn(self.delegate.DEVIN_AGENT_CONFIG_DISPLAY, request.display_argv or [])
        self.assertIn(self.delegate.PROMPT_FILE_DISPLAY, request.display_argv or [])

    def test_devin_work_uses_dangerous_permission_without_agent_config(self):
        request = self.build_git_request(
            "devin",
            "work",
            "gpt-5.4",
            "/repo",
            "implement",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        self.assertEqual(request.model, "gpt-5.4")
        self.assertIn("--model", request.argv)
        self.assertIn("gpt-5.4", request.argv)
        self.assertIn("--permission-mode", request.argv)
        self.assertIn("dangerous", request.argv)
        self.assertNotIn("--agent-config", request.argv)
        self.assertIsNone(request.agent_config_text)

    def test_devin_call_default_vs_read_only_permissions(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        default_req = self.delegate.request_from_parsed(
            self.delegate.parse_cli(["devin", "call", "answer"]),
            config,
            io.StringIO(""),
        )
        self.addCleanup(shutil.rmtree, default_req.workspace, ignore_errors=True)
        self.assertIn("dangerous", default_req.argv)
        self.assertIsNone(default_req.agent_config_text)

        ro_req = self.delegate.request_from_parsed(
            self.delegate.parse_cli(["devin", "call", "--read-only", "score"]),
            config,
            io.StringIO(""),
        )
        self.addCleanup(shutil.rmtree, ro_req.workspace, ignore_errors=True)
        self.assertIn("auto", ro_req.argv)
        self.assertIn("--agent-config", ro_req.argv)
        self.assertIsNotNone(ro_req.agent_config_text)
        self.assertTrue(ro_req.prompt_file_text.startswith("You are being called"))

    def test_devin_reasoning_effort_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request(
                "devin",
                "safe",
                None,
                "/repo",
                "review",
                self.delegate.DEFAULT_CONFIG,
                dry_run=True,
                reasoning_effort="high",
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

    def test_describe_and_models_include_runtime_and_config_provenance(self):
        workspace = Path("/tmp/delegate-provenance-test")
        describe = self.delegate.describe_payload(
            self.delegate.DEFAULT_CONFIG,
            "embedded-default",
            workspace,
        )
        models = self.delegate.models_payload(
            self.delegate.DEFAULT_CONFIG,
            "embedded-default",
            workspace,
        )
        for payload in (describe, models):
            self.assertIn("runtime", payload)
            self.assertEqual(payload["runtime"]["version"], self.delegate.VERSION)
            self.assertTrue(payload["runtime"]["modulePath"].endswith("cli.py"))
            self.assertIn("configResolution", payload)
            resolution = payload["configResolution"]
            self.assertEqual(resolution["source"], "embedded-default")
            self.assertEqual(resolution["workspace"], str(workspace))
            self.assertEqual(resolution["layers"][0]["name"], "embedded-default")
            self.assertTrue(any(layer.get("name") == "workspace" for layer in resolution["layers"]))

    def test_describe_codex_work_argv_matches_effective_network_policy(self):
        config = self.delegate.delegate_config.deep_merge(
            self.delegate.DEFAULT_CONFIG,
            {"policy": {"work": {"networkAccess": False}}},
        )
        payload = self.delegate.describe_payload(config, "test-config")
        codex_work = payload["modeMapping"]["codex"]["work"]
        self.assertEqual(payload["effectivePolicy"]["codex"]["work"]["networkAccess"], False)
        self.assertNotIn("sandbox_workspace_write.network_access=true", codex_work)

    def test_runs_without_registry_returns_empty_list(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        stdout = io.StringIO()
        code = self.delegate.main(["--cwd", str(repo.name), "runs"], stdout=stdout)
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertFalse((Path(repo.name) / ".delegate").exists())
        output = stdout.getvalue()
        self.assertIn("mode: recent", output)
        self.assertNotIn("cursor", output)


if __name__ == "__main__":
    unittest.main()
