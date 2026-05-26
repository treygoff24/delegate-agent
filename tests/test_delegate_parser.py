import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    spec = importlib.util.spec_from_file_location("delegate_cli_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_git_repo():
    temp = tempfile.TemporaryDirectory()
    subprocess.run(
        ["git", "-C", temp.name, "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return temp


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def test_documented_examples_parse(self):
        examples = [
            ["cursor", "safe", "analyze this"],
            ["cursor", "work", "fix this"],
            ["droid", "minimax", "safe", "analyze this"],
            ["droid", "minimax", "work", "fix this"],
            ["--json", "run", "--input-json", "task.json"],
            ["models"],
            ["--json", "models"],
            ["describe"],
            ["--json", "describe"],
            ["agent-help"],
            ["dry-run", "cursor", "work", "prompt"],
            ["--json", "dry-run", "droid", "minimax", "safe", "--prompt-file", "task.md"],
            ["snapshot", "cursor"],
            ["--json", "snapshot", "--latest", "cursor"],
            ["runs", "--active", "--limit", "5"],
            ["run-output", "cursor-2", "--completion-report"],
            ["run-output", "cursor", "--stdout", "--tail", "50"],
        ]
        for argv in examples:
            with self.subTest(argv=argv):
                parsed = self.delegate.parse_cli(argv)
                self.assertIsNotNone(parsed.subcommand)

    def test_global_flags_before_subcommand(self):
        parsed = self.delegate.parse_cli(["--json", "--cwd", "/tmp/repo", "models"])
        self.assertTrue(parsed.json_mode)
        self.assertEqual(parsed.cwd, "/tmp/repo")

    def test_trailing_json_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["dry-run", "droid", "minimax", "work", "hello", "--json"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_prompt_file_before_prompt_text(self):
        parsed = self.delegate.parse_cli(["cursor", "safe", "--prompt-file", "task.md"])
        self.assertEqual(parsed.prompt_file, "task.md")
        self.assertEqual(parsed.prompt_parts, [])

    def test_prompt_file_after_prompt_text_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["cursor", "safe", "hello", "--prompt-file", "task.md"])
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["cursor", "agent", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_mode")

    def test_agent_help_discourages_shell_tail_launches(self):
        stdout = io.StringIO()
        code = self.delegate.emit_agent_help(stdout)
        self.assertEqual(code, self.delegate.EXIT_OK)
        help_text = stdout.getvalue()
        self.assertIn("do not pipe delegate launches through tail", help_text)
        self.assertIn("delegate snapshot cursor", help_text)
        self.assertIn("set -o pipefail", help_text)

    def test_codex_direct_commands_parse(self):
        parsed = self.delegate.parse_cli(["codex", "work", "implement"])
        self.assertEqual(parsed.subcommand, "codex")
        self.assertEqual(parsed.engine, "codex")
        self.assertEqual(parsed.mode, "work")

    def test_dry_run_codex_parses(self):
        parsed = self.delegate.parse_cli(["dry-run", "codex", "safe", "review"])
        self.assertEqual(parsed.subcommand, "codex")
        self.assertTrue(parsed.dry_run)

    def test_json_describe_shape(self):
        payload = self.delegate.describe_payload(self.delegate.DEFAULT_CONFIG, "embedded-default")
        self.assertTrue(payload["ok"])
        self.assertIn("safe", payload["modes"])
        self.assertIn("work", payload["modes"])
        self.assertIn("cursor", payload["modeMapping"])
        self.assertIn("codex", payload["modeMapping"])
        self.assertIn("codex", payload["engines"])
        self.assertIn("policyProfiles", payload)
        self.assertIn("policyFieldSupport", payload)
        self.assertIn("effectivePolicy", payload)
        self.assertIn("codex", payload["effectivePolicy"])
        self.assertIn("passThrough", payload)

    def test_pass_through_parses_before_subcommand(self):
        parsed = self.delegate.parse_cli(["--pass-through", "cursor", "safe", "hello"])
        self.assertTrue(parsed.pass_through)
        self.assertEqual(parsed.subcommand, "cursor")

    def test_json_pass_through_is_invalid(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--json", "--pass-through", "cursor", "safe", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_completion_report_none_flag(self):
        parsed = self.delegate.parse_cli(["--completion-report", "none", "cursor", "safe", "hello"])
        self.assertEqual(parsed.completion_report, "none")

    def test_no_completion_report_alias(self):
        parsed = self.delegate.parse_cli(["--no-completion-report", "cursor", "safe", "hello"])
        self.assertEqual(parsed.completion_report, "none")

    def test_pass_through_skips_completion_report_injection(self):
        parsed = self.delegate.parse_cli(["--pass-through", "cursor", "safe", "hello"])
        mode = self.delegate.resolve_completion_report_mode(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(mode, "none")
        effective = self.delegate.effective_prompt("hello", completion_report_mode=mode)
        self.assertTrue(effective.startswith(self.delegate.delegate_runner.SKILL_REVIEW_PREFIX))
        self.assertNotIn("Delegate completion report requirement", effective)

    def test_effective_prompt_always_prepends_skill_review(self):
        effective = self.delegate.effective_prompt("hello", completion_report_mode="none")
        self.assertTrue(effective.startswith(self.delegate.delegate_runner.SKILL_REVIEW_PREFIX))
        self.assertTrue(effective.endswith("hello"))
        self.assertIn("mandatory for every Delegate Agent run", effective)

    def test_effective_prompt_does_not_duplicate_skill_review(self):
        original = self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"
        effective = self.delegate.effective_prompt(original, completion_report_mode="none")
        self.assertEqual(effective, original)

    def test_prompt_file_is_not_mutated_for_completion_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "task.md"
            prompt_path.write_text("original prompt\n")
            parsed = self.delegate.parse_cli(["cursor", "safe", "--prompt-file", str(prompt_path)])
            prompt = self.delegate.resolve_prompt(
                parsed.prompt_parts, parsed.prompt_file, io.StringIO()
            )
            effective = self.delegate.effective_prompt(
                prompt,
                completion_report_mode="markdown",
            )
            self.assertIn("Delegate sub-agent skill review requirement", effective)
            self.assertIn("Delegate completion report requirement", effective)
            self.assertEqual(prompt_path.read_text(), "original prompt\n")

    def test_snapshot_latest_and_handle_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["snapshot", "--latest", "cursor", "cursor"])
        self.assertEqual(ctx.exception.error, "ambiguous_snapshot_target")

    def test_run_output_requires_selector(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["run-output", "cursor"])
        self.assertEqual(ctx.exception.error, "missing_output_selector")

    def test_runs_limit_must_be_positive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["runs", "--limit", "0"])
        self.assertEqual(ctx.exception.error, "invalid_limit")

    def test_runs_active_and_recent_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["runs", "--active", "--recent"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_run_output_raw_and_tail_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["run-output", "cursor", "--stdout", "--raw", "--tail", "5"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_run_output_stdout_requires_tail_or_raw(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["run-output", "cursor", "--stdout"])
        self.assertEqual(ctx.exception.error, "missing_tail")
        self.assertIn("--tail", ctx.exception.message)
        self.assertIn("--raw", ctx.exception.message)

    def test_worktree_misplaced_global_option_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["worktree", "list", "--json"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_worktree_unknown_option_is_action_specific(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["worktree", "remove", "cursor-1", "--older-than", "7"])
        self.assertEqual(ctx.exception.error, "unknown_option")
        self.assertIn("worktree remove", ctx.exception.message)

    def test_worktree_show_latest_and_handle_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["worktree", "show", "--latest", "cursor", "cursor-1"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_worktree_remove_keep_branch_and_force_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["worktree", "remove", "cursor-1", "--keep-branch", "--force"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_worktree_prune_requires_filter_at_execution_time(self):
        parsed = self.delegate.parse_cli(["worktree", "prune"])
        self.assertEqual(parsed.worktree_action, "prune")
        self.assertFalse(parsed.worktree_merged)
        self.assertIsNone(parsed.worktree_older_than)

    def test_load_config_cli_overrides_win(self):
        config_path = ROOT / "src" / "delegate_agent" / "config.py"
        spec = importlib.util.spec_from_file_location("delegate_config_parser_test", config_path)
        config_mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(config_mod)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            local_delegate = workspace / ".delegate"
            local_delegate.mkdir()
            (local_delegate / "config.json").write_text(
                json.dumps({"cursor": {"defaultModel": "workspace-model"}})
            )
            loaded, source = config_mod.load_config(
                workspace=workspace,
                cli_overrides={"cursor": {"defaultModel": "cli-model"}},
            )
            self.assertEqual(loaded["cursor"]["defaultModel"], "cli-model")
            self.assertEqual(source, "cli-overrides")

    # -- Wave 1 isolation parser tests ------------------------------------------------

    def test_isolation_worktree_cursor_work_parses(self):
        parsed = self.delegate.parse_cli(
            ["--isolation", "worktree", "cursor", "work", "fix this"]
        )
        self.assertEqual(parsed.isolation, "worktree")
        self.assertEqual(parsed.engine, "cursor")
        self.assertEqual(parsed.mode, "work")

    def test_isolation_none_codex_work_parses(self):
        parsed = self.delegate.parse_cli(
            ["--isolation", "none", "codex", "work", "implement"]
        )
        self.assertEqual(parsed.isolation, "none")
        self.assertEqual(parsed.engine, "codex")
        self.assertEqual(parsed.mode, "work")

    def test_isolation_auto_droid_safe_parses(self):
        parsed = self.delegate.parse_cli(
            ["--isolation", "auto", "droid", "minimax", "safe", "review"]
        )
        self.assertEqual(parsed.isolation, "auto")
        self.assertEqual(parsed.engine, "droid")
        self.assertEqual(parsed.mode, "safe")

    def test_isolation_unknown_value_raises(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["--isolation", "bananas", "cursor", "work", "fix"]
            )
        self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_isolation_missing_value_raises(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--isolation"])
        self.assertEqual(ctx.exception.error, "missing_isolation_value")

    def test_isolation_after_subcommand_is_misplaced(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["cursor", "work", "--isolation", "worktree", "fix"]
            )
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_run_input_keys_contains_isolation(self):
        self.assertIn("isolation", self.delegate.RUN_INPUT_KEYS)

    def test_run_input_json_unknown_key_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(json.dumps({
                "engine": "droid",
                "mode": "safe",
                "model": "minimax",
                "cwd": tmp,
                "prompt": "hello",
                "isolation": "worktree",
                "bogus": "should-fail",
            }))
            parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "unknown_input_key")
            self.assertIn("bogus", ctx.exception.message)

    def test_resolve_isolation_cli_wins_over_json_and_config(self):
        result = self.delegate.delegate_config.resolve_isolation(
            cli_value="none",
            input_json_value="worktree",
            loaded_config={"isolation": {"work": "auto"}},
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "none")

    def test_resolve_isolation_json_wins_over_config(self):
        result = self.delegate.delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value="worktree",
            loaded_config={"isolation": {"work": "auto"}},
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "worktree")

    def test_resolve_isolation_config_wins_over_embedded_default(self):
        result = self.delegate.delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value=None,
            loaded_config={"isolation": {"work": "worktree"}},
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "worktree")

    def test_resolve_isolation_embedded_default_safe_is_auto(self):
        result = self.delegate.delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value=None,
            loaded_config=None,
            engine="cursor",
            mode="safe",
        )
        self.assertEqual(result, "auto")

    def test_resolve_isolation_embedded_default_work_is_none(self):
        result = self.delegate.delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value=None,
            loaded_config=None,
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "none")

    def test_resolve_isolation_cli_auto_bypasses_config_worktree(self):
        result = self.delegate.delegate_config.resolve_isolation(
            cli_value="auto",
            input_json_value=None,
            loaded_config={"isolation": {"work": "worktree"}},
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "auto")

    def test_run_input_json_isolation_invalid_value_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(json.dumps({
                "engine": "droid",
                "mode": "safe",
                "model": "minimax",
                "cwd": tmp,
                "prompt": "hello",
                "isolation": "bananas",
            }))
            parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_run_input_json_workspace_config_resolves_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            local_delegate = workspace / ".delegate"
            local_delegate.mkdir()
            (local_delegate / "config.json").write_text(json.dumps({
                "isolation": {"work": "worktree"},
                "cursor": {"argvPrefix": ["agent"], "defaultModel": "test"},
                "droid": {"binary": "droid", "models": {"minimax": "model-id"}},
            }))
            task = workspace / "task.json"
            task.write_text(json.dumps({
                "engine": "droid",
                "mode": "work",
                "model": "minimax",
                "cwd": tmp,
                "prompt": "hello",
            }))
            parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            # pre_read_run_json_for_config loads workspace-local config
            ws, cfg, src = self.delegate.pre_read_run_json_for_config(str(task), None)
            request = self.delegate.request_from_input_json(parsed, cfg)
            self.assertEqual(request.engine, "droid")
            self.assertEqual(request.mode, "work")

    def test_run_input_json_cwd_conflict_still_fails_with_isolation(self):
        repo1 = make_git_repo()
        repo2 = make_git_repo()
        self.addCleanup(repo1.cleanup)
        self.addCleanup(repo2.cleanup)
        task = Path(repo1.name) / "task.json"
        task.write_text(json.dumps({
            "engine": "droid",
            "mode": "safe",
            "model": "minimax",
            "cwd": repo1.name,
            "prompt": "hello",
            "isolation": "none",
        }))
        parsed = self.delegate.ParsedCommand(
            "run", json_mode=True, cwd=repo2.name, input_json=str(task)
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(ctx.exception.error, "ambiguous_cwd")

    # -- Missing coverage: misplaced_global_option for --isolation on codex, droid, dry-run --

    def test_isolation_after_subcommand_codex_work_is_misplaced(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "work", "--isolation", "worktree", "fix"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_isolation_after_subcommand_droid_work_is_misplaced(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["droid", "minimax", "work", "--isolation", "worktree", "fix"]
            )
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_isolation_after_subcommand_dry_run_cursor_is_misplaced(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["dry-run", "--isolation", "worktree", "cursor", "work", "fix"]
            )
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    # -- Missing coverage: isolation.work unknown value --

    def test_isolation_work_unknown_value_raises(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--isolation", "bananas", "codex", "work", "fix"])
        self.assertEqual(ctx.exception.error, "invalid_isolation")

    # -- Missing coverage: run-input-json isolation = null --

    def test_run_input_json_isolation_null_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(json.dumps({
                "engine": "droid",
                "mode": "safe",
                "model": "minimax",
                "cwd": tmp,
                "prompt": "hello",
                "isolation": None,
            }))
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.pre_read_run_json_for_config(str(task), None)
            self.assertEqual(ctx.exception.error, "invalid_isolation")

    # -- Missing coverage: run --input-json cwd with workspace-local config resolves isolation --

    def test_run_input_json_workspace_config_resolves_isolation_before_request(self):
        """Canonical test: JSON cwd pointing at a repo with .delegate/config.json
        containing isolation.work = worktree resolves isolation to worktree even when
        CLI cwd has different config."""
        with tempfile.TemporaryDirectory() as global_tmp:
            # Create a "global" config with isolation.work = none (the default)
            global_config = Path(global_tmp) / "global_config.json"
            global_config.write_text(json.dumps({
                "cursor": {"argvPrefix": ["agent"], "defaultModel": "global-model"},
                "droid": {"binary": "droid", "models": {"minimax": "model-id"}},
                "isolation": {"work": "none"},
            }))
            with tempfile.TemporaryDirectory() as workspace_tmp:
                # Create a workspace with local config that has isolation.work = worktree
                workspace = Path(workspace_tmp)
                local_delegate = workspace / ".delegate"
                local_delegate.mkdir()
                (local_delegate / "config.json").write_text(json.dumps({
                    "cursor": {"argvPrefix": ["agent"], "defaultModel": "ws-model"},
                    "droid": {"binary": "droid", "models": {"minimax": "model-id"}},
                    "isolation": {"work": "worktree"},
                }))
                # Create a task.json pointing at the workspace with local config
                task = workspace / "task.json"
                task.write_text(json.dumps({
                    "engine": "cursor",
                    "mode": "work",
                    "cwd": str(workspace),
                    "prompt": "hello",
                }))
                # The pre-read should load config from the workspace (with work = worktree)
                # and validate successfully.
                ws, cfg, src = self.delegate.pre_read_run_json_for_config(
                    str(task), None
                )
                self.assertEqual(
                    cfg["isolation"]["work"], "worktree",
                    "Config loaded from JSON-resolved workspace should have isolation.work = worktree",
                )

                # Now resolve isolation: no CLI flag, no JSON isolation, should use config default
                result = self.delegate.delegate_config.resolve_isolation(
                    cli_value=None,
                    input_json_value=None,
                    loaded_config=cfg,
                    engine="cursor",
                    mode="work",
                )
                self.assertEqual(result, "worktree")

                # CLI --isolation auto should bypass the config
                result = self.delegate.delegate_config.resolve_isolation(
                    cli_value="auto",
                    input_json_value=None,
                    loaded_config=cfg,
                    engine="cursor",
                    mode="work",
                )
                self.assertEqual(result, "auto")

                # JSON isolation overrides config
                result = self.delegate.delegate_config.resolve_isolation(
                    cli_value=None,
                    input_json_value="none",
                    loaded_config=cfg,
                    engine="cursor",
                    mode="work",
                )
                self.assertEqual(result, "none")

                # Build a full request from the JSON and verify it uses the resolved config
                parsed = self.delegate.ParsedCommand(
                    "run", json_mode=False, input_json=str(task)
                )
                request = self.delegate.request_from_input_json(parsed, cfg)
                self.assertEqual(request.engine, "cursor")
                self.assertEqual(request.mode, "work")

    # -- Finding #5: end-to-end main() integration test for run --input-json config discovery --

    def test_main_run_input_json_uses_workspace_config(self):
        """End-to-end main(): JSON cwd config discovery loads workspace-local config
        and resolve_isolation uses it."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            local_delegate = workspace / ".delegate"
            local_delegate.mkdir()
            (local_delegate / "config.json").write_text(json.dumps({
                "cursor": {"argvPrefix": ["agent"], "defaultModel": "ws-model"},
                "droid": {"binary": "/nonexistent/droid", "models": {"minimax": "model-id"}},
                "isolation": {"work": "worktree"},
            }))
            task = workspace / "task.json"
            task.write_text(json.dumps({
                "engine": "droid",
                "mode": "work",
                "model": "minimax",
                "cwd": str(workspace),
                "prompt": "hello",
            }))
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = self.delegate.main(
                ["run", "--input-json", str(task)],
                stdout=stdout,
                stderr=stderr,
            )
            # Should fail at persistent-worktree semantic validation before
            # attempting to launch the configured child binary. That confirms
            # config was loaded from the JSON-resolved workspace and isolation
            # resolved to "worktree" correctly.
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            err = stderr.getvalue()
            self.assertIn("worktree_requires_git", err)
            self.assertNotIn("invalid_isolation", err)
