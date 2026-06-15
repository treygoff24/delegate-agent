import importlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    return importlib.reload(importlib.import_module("delegate_agent.cli"))


def make_git_repo(*, with_commit: bool = False):
    temp = tempfile.TemporaryDirectory()
    git = ["git", "-C", temp.name]
    subprocess.run(
        [*git, "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if with_commit:
        subprocess.run(
            [
                *git,
                "-c",
                "user.name=Delegate Test",
                "-c",
                "user.email=delegate-test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return temp


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()
        # Hermetic config: main() must never read the developer's real
        # ~/.delegate/config.json (its codex.binary would bypass the PATH-prefixed
        # fake executables and shell out to the real CLI).
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        config_path = Path(config_dir.name) / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        self._config_env = {"DELEGATE_CONFIG": str(config_path)}

    def run_main(self, argv, *, path_prefix: Path | None = None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = dict(self._config_env)
        if path_prefix is not None:
            env["PATH"] = str(path_prefix) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, env, clear=False):
            code = self.delegate.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def build_git_request(
        self,
        engine: str,
        mode: str,
        model_alias: str | None,
        workspace: str,
        prompt: str,
        config: dict,
        dry_run: bool,
        **kwargs,
    ):
        return self.delegate.build_request(
            engine,
            mode,
            model_alias,
            self.delegate.ResolvedWorkspace(workspace, "git"),
            prompt,
            config,
            dry_run,
            **kwargs,
        )

    def write_fake_executable(
        self,
        name: str,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
    ) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s' {shlex.quote(stdout)}\n"
            f"printf '%s' {shlex.quote(stderr)} >&2\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return bin_dir

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
        self.assertTrue(argv[10].startswith(self.delegate.CURSOR_SAFE_REVIEW_PREFIX))
        self.assertTrue(argv[10].endswith("hello"))
        self.assertNotIn("--mode=plan", argv)
        self.assertNotIn("--mode=ask", argv)
        self.assertNotIn("--force", argv)
        self.assertNotIn("--approve-mcps", argv)

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
        self.assertTrue(argv[-1].startswith(self.delegate.DROID_SAFE_REVIEW_PREFIX))
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

    def test_codex_reasoning_effort_fails_without_model(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request(
                "codex",
                "safe",
                None,
                "/repo",
                "hello",
                self.delegate.DEFAULT_CONFIG,
                True,
                reasoning_effort="high",
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

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
        self.assertTrue(droid.prompt_file_text.startswith(self.delegate.DROID_SAFE_REVIEW_PREFIX))
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
        self.assertEqual(prompt.count(self.delegate.DROID_SAFE_REVIEW_PREFIX), 1)
        self.assertGreater(
            prompt.find(self.delegate.DROID_SAFE_REVIEW_PREFIX),
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

    def test_explicit_effort_still_fails_closed_without_model(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        self.assertIsNone(config["codex"]["defaultModel"])
        with self.assertRaises(self.delegate.DelegateError) as caught:
            self.build_git_request(
                "codex",
                "safe",
                None,
                "/repo",
                "hello",
                config,
                True,
                reasoning_effort="high",
            )
        self.assertEqual(caught.exception.error, "unsupported_reasoning_effort")

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

    def test_cursor_safe_dry_run_reports_isolation(self):
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertIn("isolation", payload)
        self.assertNotIn("--mode=plan", payload["argv"])
        self.assertNotIn("--approve-mcps", payload["argv"])

    def test_cursor_safe_cli_config_omits_mutating_shell(self):
        allow = self.delegate.CURSOR_SAFE_CLI_CONFIG["permissions"]["allow"]
        self.assertIn("Read(**)", allow)
        self.assertNotIn("Shell(git)", allow)
        self.assertNotIn("Shell(find)", allow)
        self.assertNotIn("Shell(ls)", allow)

    def test_cursor_safe_cli_config_is_permissions_only(self):
        self.assertEqual(set(self.delegate.CURSOR_SAFE_CLI_CONFIG), {"permissions"})

    def test_write_cursor_safe_project_config_serializes_permissions_only(self):
        with tempfile.TemporaryDirectory() as workspace:
            self.delegate.write_cursor_safe_project_config(Path(workspace))
            config = self.delegate.json.loads(
                (Path(workspace) / ".cursor" / "cli.json").read_text()
            )
        self.assertEqual(set(config), {"permissions"})
        self.assertIn("allow", config["permissions"])
        self.assertIn("deny", config["permissions"])

    def test_mirror_path_preserving_symlinks_keeps_link_target(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            link = Path(workspace) / "link.txt"
            link.symlink_to(secret)

            destination = Path(workspace) / "mirror" / "link.txt"
            self.delegate.mirror_path_preserving_symlinks(link, destination)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(os.readlink(destination), str(secret))

    def test_create_directory_safe_workspace_blocks_external_symlinks(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(workspace) / "link.txt").symlink_to(secret)

            copy_path, temp_base, warnings = self.delegate.create_directory_safe_workspace(
                workspace,
                include_warnings=True,
            )
            try:
                copied = Path(copy_path) / "link.txt"
                self.assertFalse(copied.is_symlink())
                self.assertEqual(
                    copied.read_text(encoding="utf-8"),
                    self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
                )
                self.assertNotIn(str(secret), copied.read_text(encoding="utf-8"))
                self.assertEqual(len(warnings), 1)
                self.assertIn(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX, warnings[0])
                self.assertIn("link.txt", warnings[0])
                self.assertNotIn(str(secret), warnings[0])
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    def test_create_directory_safe_workspace_preserves_internal_symlinks(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "target.txt").write_text("inside\n", encoding="utf-8")
            (root / "link.txt").symlink_to("target.txt")

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                copied = Path(copy_path) / "link.txt"
                self.assertTrue(copied.is_symlink())
                self.assertEqual(os.readlink(copied), "target.txt")
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    def test_create_directory_safe_workspace_omits_delegate_registry(self):
        with tempfile.TemporaryDirectory() as workspace:
            delegate_dir = Path(workspace) / ".delegate"
            delegate_dir.mkdir()
            (delegate_dir / "stdout.log").write_text("private run output\n", encoding="utf-8")

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                self.assertFalse((Path(copy_path) / ".delegate").exists())
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo unavailable")
    def test_create_directory_safe_workspace_skips_fifo_files(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "normal.txt").write_text("copy me\n", encoding="utf-8")
            os.mkfifo(root / "signal.fifo")

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                copied = Path(copy_path)
                self.assertEqual((copied / "normal.txt").read_text(encoding="utf-8"), "copy me\n")
                self.assertFalse((copied / "signal.fifo").exists())
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)

    def test_external_symlink_audit_warns_without_target_contents(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(workspace) / "link.txt").symlink_to(secret)

            warnings = self.delegate.external_symlink_warnings(workspace)
            self.assertEqual(len(warnings), 1)
            self.assertIn(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX, warnings[0])
            self.assertIn("link.txt", warnings[0])
            self.assertNotIn(str(secret), warnings[0])

    def test_git_safe_workspace_blocks_untracked_external_symlink(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            link = Path(repo.name) / "external-link.txt"
            link.symlink_to(secret)

            worktree_path, temp_base, warnings = self.delegate.create_git_safe_workspace(
                repo.name,
                include_warnings=True,
            )
            try:
                copied = Path(worktree_path) / "external-link.txt"
                self.assertFalse(copied.is_symlink())
                self.assertEqual(
                    copied.read_text(encoding="utf-8"),
                    self.delegate.SAFE_BLOCKED_SYMLINK_PLACEHOLDER,
                )
                self.assertNotIn(str(secret), copied.read_text(encoding="utf-8"))
                self.assertEqual(len(warnings), 1)
                self.assertIn(self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX, warnings[0])
                self.assertIn("external-link.txt", warnings[0])
                self.assertNotIn(str(secret), warnings[0])
            finally:
                self.delegate.cleanup_safe_isolated_workspace(
                    git_root=repo.name,
                    isolated_workspace=worktree_path,
                    temp_base=temp_base,
                )

    def test_create_git_safe_workspace_reports_worktree_timeout(self):
        timeout = subprocess.CompletedProcess(
            ["git"],
            124,
            "",
            "git command timed out after 30s",
        )
        with (
            mock.patch.object(self.delegate, "_run_git", return_value=timeout),
            self.assertRaises(self.delegate.DelegateError) as ctx,
        ):
            self.delegate.create_git_safe_workspace("/repo")

        self.assertEqual(ctx.exception.error, "safe_workspace_create_failed")
        self.assertIn("timed out", ctx.exception.message)

    def test_read_git_tracked_diff_reports_timeout_from_binary_runner(self):
        timeout = subprocess.CompletedProcess(
            ["git"],
            124,
            b"",
            b"git command timed out after 30s",
        )
        with (
            mock.patch.object(self.delegate, "_run_git_bytes", return_value=timeout),
            self.assertRaises(self.delegate.DelegateError) as ctx,
        ):
            self.delegate.read_git_tracked_diff("/repo")

        self.assertEqual(ctx.exception.error, "safe_workspace_sync_failed")
        self.assertIn("timed out", ctx.exception.message)

    def test_safe_isolation_surfaces_external_symlink_warning(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(repo.name) / "external-link.txt").symlink_to(secret)
            workspace = self.delegate.ResolvedWorkspace(repo.name, "git")
            iso_ctx = self.delegate.build_isolation_context(
                source_workspace=repo.name,
                resolved_isolation="auto",
                engine="cursor",
                mode="safe",
                source_git_root=repo.name,
            )
            request = self.delegate.build_request(
                "cursor",
                "safe",
                None,
                workspace,
                "review",
                self.delegate.DEFAULT_CONFIG,
                dry_run=False,
                isolation_context=iso_ctx,
            )

            with self.delegate.safe_isolated_request(request) as isolated:
                warnings = isolated.isolation_context.warnings
                self.assertEqual(isolated.isolation_context.safe_workspace_method, "git-worktree")
                self.assertTrue(
                    any(
                        self.delegate.SAFE_EXTERNAL_SYMLINK_WARNING_PREFIX in item
                        for item in warnings
                    )
                )

    def test_unborn_git_safe_isolation_falls_back_to_directory_copy(self):
        repo = make_git_repo(with_commit=False)
        self.addCleanup(repo.cleanup)
        delegate_dir = Path(repo.name) / ".delegate"
        delegate_dir.mkdir()
        (delegate_dir / "stdout.log").write_text("private run output\n", encoding="utf-8")
        workspace = self.delegate.ResolvedWorkspace(repo.name, "git")
        iso_ctx = self.delegate.build_isolation_context(
            source_workspace=repo.name,
            resolved_isolation="auto",
            engine="codex",
            mode="safe",
            source_git_root=repo.name,
        )
        request = self.delegate.build_request(
            "codex",
            "safe",
            None,
            workspace,
            "review",
            self.delegate.DEFAULT_CONFIG,
            dry_run=False,
            isolation_context=iso_ctx,
        )

        with self.delegate.safe_isolated_request(request) as isolated:
            self.assertEqual(isolated.workspace_kind, "directory")
            self.assertFalse((Path(isolated.workspace) / ".git").exists())
            self.assertFalse((Path(isolated.workspace) / ".delegate").exists())
            self.assertEqual(
                isolated.isolation_context.safe_workspace_method,
                "directory-copy",
            )
            self.assertIn(
                self.delegate.SAFE_UNBORN_GIT_WARNING,
                isolated.isolation_context.warnings,
            )
            self.assertIn("--skip-git-repo-check", isolated.argv)
            self.assertEqual(isolated.isolation_context.source_git_root, repo.name)

    def test_dry_run_codex_safe_does_not_create_delegate_dir(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        workspace = Path(repo.name)
        delegate_dir = workspace / ".delegate"
        self.assertFalse(delegate_dir.exists())
        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(workspace), "dry-run", "codex", "safe", "review"],
            stdout=stdout,
        )
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertFalse(delegate_dir.exists())

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


class KimiCommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        config_path = Path(config_dir.name) / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        self._config_env = {"DELEGATE_CONFIG": str(config_path)}

    def run_main(self, argv, *, path_prefix: Path | None = None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = dict(self._config_env)
        if path_prefix is not None:
            env["PATH"] = str(path_prefix) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, env, clear=False):
            code = self.delegate.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def build_git_request(
        self,
        engine: str,
        mode: str,
        model_alias: str | None,
        workspace: str,
        prompt: str,
        config: dict,
        dry_run: bool,
        **kwargs,
    ):
        return self.delegate.build_request(
            engine,
            mode,
            model_alias,
            self.delegate.ResolvedWorkspace(workspace, "git"),
            prompt,
            config,
            dry_run,
            **kwargs,
        )

    def test_kimi_safe_argv(self):
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        self.assertNotIn("--plan", request.argv)
        self.assertNotIn("--yolo", request.argv)
        self.assertNotIn("--auto", request.argv)
        self.assertIn("--model", request.argv)
        self.assertIn("--output-format", request.argv)
        self.assertIn("stream-json", request.argv)
        self.assertIn("--prompt", request.argv)
        prompt_arg = request.argv[request.argv.index("--prompt") + 1]
        self.assertTrue(prompt_arg.startswith(self.delegate.KIMI_SAFE_REVIEW_PREFIX))
        self.assertTrue(prompt_arg.endswith("hello"))

    def test_kimi_work_argv(self):
        request = self.build_git_request(
            "kimi",
            "work",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        self.assertNotIn("--yolo", request.argv)
        self.assertNotIn("--auto", request.argv)
        self.assertNotIn("--plan", request.argv)
        self.assertIn("--prompt", request.argv)
        prompt_arg = request.argv[request.argv.index("--prompt") + 1]
        self.assertFalse(prompt_arg.startswith(self.delegate.KIMI_SAFE_REVIEW_PREFIX))
        self.assertTrue(prompt_arg.endswith("hello"))

    def test_kimi_pass_through_argv(self):
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
            stream_capture=False,
        )
        self.assertNotIn("--output-format", request.argv)
        self.assertNotIn("stream-json", request.argv)
        self.assertNotIn("--plan", request.argv)
        self.assertNotIn("--yolo", request.argv)
        self.assertIn("--prompt", request.argv)

    def test_kimi_model_override_from_config(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["kimi"]["defaultModel"] = "kimi-code/custom-model"
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            dry_run=True,
        )
        self.assertEqual(request.model, "kimi-code/custom-model")
        self.assertIn("--model", request.argv)
        self.assertIn("kimi-code/custom-model", request.argv)

    def test_kimi_dry_run(self):
        code, out, err = self.run_main(["--json", "dry-run", "kimi", "safe", "hello"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["engine"], "kimi")
        self.assertEqual(payload["mode"], "safe")
        self.assertIsNotNone(payload["model"])
        self.assertNotIn("--plan", payload["argv"])
        self.assertNotIn("--yolo", payload["argv"])
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertEqual(payload["effectiveIsolation"], "worktree")

    def test_kimi_reasoning_effort_rejected(self):
        code, out, _err = self.run_main(
            ["--json", "kimi", "safe", "--reasoning-effort", "high", "hello"]
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unsupported_reasoning_effort")
        self.assertIn("kimi", payload["message"])

    def test_kimi_unconfigured_default_model(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["kimi"]["defaultModel"] = None
        request = self.build_git_request(
            "kimi",
            "work",
            None,
            "/repo",
            "hello",
            config,
            dry_run=True,
        )
        self.assertIsNone(request.model)
        self.assertNotIn("--model", request.argv)
