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
            ["claude", "safe", "analyze this"],
            ["claude", "work", "fix this"],
            ["droid", "minimax", "safe", "analyze this"],
            ["droid", "minimax", "work", "fix this"],
            ["--json", "run", "--input-json", "task.json"],
            ["models"],
            ["--json", "models"],
            ["--json", "models", "--summary"],
            ["describe"],
            ["--json", "describe"],
            ["--json", "describe", "--summary"],
            ["config", "init"],
            ["--json", "config", "init", "--force"],
            ["agent-help"],
            ["dry-run", "cursor", "work", "prompt"],
            ["dry-run", "claude", "safe", "prompt"],
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
        self.assertTrue(parsed.global_options.json_mode)
        self.assertEqual(parsed.global_options.cwd, "/tmp/repo")

    def test_models_and_describe_reject_redacted_flag(self):
        for subcommand in ("models", "describe"):
            with self.subTest(subcommand=subcommand):
                with self.assertRaises(self.delegate.DelegateError) as ctx:
                    self.delegate.parse_cli([subcommand, "--redacted"])
                self.assertEqual(ctx.exception.error, "unexpected_argument")

    def test_models_and_describe_parse_summary_option(self):
        for subcommand in ("models", "describe"):
            with self.subTest(subcommand=subcommand):
                parsed = self.delegate.parse_cli([subcommand, "--summary"])
                self.assertTrue(parsed.inspection.summary)

    def test_models_unknown_option_fails_clearly(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["models", "--verbose"])
        self.assertEqual(ctx.exception.error, "unexpected_argument")

    def test_auth_profile_rejected_for_non_refresh_capabilities(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--auth-profile", "work", "capabilities"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("refresh", ctx.exception.message)

    def test_auth_profile_accepted_for_capabilities_refresh(self):
        parsed = self.delegate.parse_cli(["--auth-profile", "work", "capabilities", "refresh"])
        self.assertEqual(parsed.global_options.auth_profile, "work")
        self.assertTrue(parsed.capabilities.refresh)

    def test_infer_global_json_after_value_taking_globals(self):
        cases = [
            ["--isolation", "worktree", "--json", "cursor"],
            ["--completion-report", "markdown", "--json", "cursor"],
            ["--cwd", "/tmp/repo", "--completion-report", "none", "--json", "models"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertTrue(self.delegate.infer_global_json(argv))

    def test_infer_global_json_after_flag_globals(self):
        self.assertTrue(self.delegate.infer_global_json(["--pass-through", "--json", "cursor"]))
        self.assertTrue(
            self.delegate.infer_global_json(["--no-completion-report", "--json", "cursor"])
        )

    def test_trailing_json_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["dry-run", "droid", "minimax", "work", "hello", "--json"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_prompt_file_before_prompt_text(self):
        parsed = self.delegate.parse_cli(["cursor", "safe", "--prompt-file", "task.md"])
        self.assertEqual(parsed.launch.prompt_file, "task.md")
        self.assertEqual(parsed.launch.prompt_parts, [])

    def test_output_schema_before_prompt_text(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "--output-schema", "schema.json", "x"])
        self.assertEqual(parsed.launch.output_schema, "schema.json")
        self.assertEqual(parsed.launch.prompt_parts, ["x"])

    def test_output_schema_duplicate_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["codex", "safe", "--output-schema", "a.json", "--output-schema", "b.json", "x"]
            )
        self.assertEqual(ctx.exception.error, "invalid_output_schema")

    def test_output_schema_requires_value(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--output-schema"])
        self.assertEqual(ctx.exception.error, "missing_output_schema")

    def test_prompt_file_after_prompt_text_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["cursor", "safe", "hello", "--prompt-file", "task.md"])
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_codex_reasoning_effort_before_prompt(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "--reasoning-effort", "high", "review"])
        self.assertEqual(parsed.launch.reasoning_effort, "high")
        self.assertEqual(parsed.launch.prompt_parts, ["review"])

    def test_droid_reasoning_effort_after_alias_and_mode(self):
        parsed = self.delegate.parse_cli(
            ["droid", "reviewer", "safe", "--reasoning-effort", "high", "review"]
        )
        self.assertEqual(parsed.launch.engine, "droid")
        self.assertEqual(parsed.launch.model_alias, "reviewer")
        self.assertEqual(parsed.launch.reasoning_effort, "high")
        self.assertEqual(parsed.launch.prompt_parts, ["review"])

    def test_progress_launch_option_before_prompt(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "--progress", "review"])
        self.assertEqual(parsed.launch.progress_intent, "on")
        self.assertEqual(parsed.launch.prompt_parts, ["review"])

    def test_progress_after_prompt_is_prompt_text(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "review", "--progress"])
        self.assertIsNone(parsed.launch.progress_intent)
        self.assertEqual(parsed.launch.prompt_parts, ["review", "--progress"])

    def test_no_progress_launch_option_before_prompt(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "--no-progress", "review"])
        self.assertEqual(parsed.launch.progress_intent, "off")
        self.assertEqual(parsed.launch.prompt_parts, ["review"])

    def test_no_progress_after_prompt_is_prompt_text(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "review", "--no-progress"])
        self.assertIsNone(parsed.launch.progress_intent)
        self.assertEqual(parsed.launch.prompt_parts, ["review", "--no-progress"])

    def test_progress_and_no_progress_cannot_combine(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--progress", "--no-progress", "review"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_forbid_commit_launch_option_before_prompt(self):
        parsed = self.delegate.parse_cli(["cursor", "work", "--forbid-commit", "fix"])
        self.assertTrue(parsed.launch.forbid_commit)
        self.assertEqual(parsed.launch.prompt_parts, ["fix"])

    def test_forbid_commit_after_prompt_is_prompt_text(self):
        parsed = self.delegate.parse_cli(["cursor", "work", "fix", "--forbid-commit"])
        self.assertFalse(parsed.launch.forbid_commit)
        self.assertEqual(parsed.launch.prompt_parts, ["fix", "--forbid-commit"])

    def test_progress_with_pass_through_is_invalid(self):
        parsed = self.delegate.parse_cli(["--pass-through", "codex", "safe", "--progress", "x"])
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_parsed(
                parsed,
                self.delegate.DEFAULT_CONFIG,
                io.StringIO(""),
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_config_enabled_progress_conflicts_with_pass_through(self):
        parsed = self.delegate.parse_cli(["--pass-through", "codex", "safe", "x"])
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["progress"] = {"enabled": True, "initialDelaySec": 30, "intervalSec": 60}
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_config_enabled_progress_cleared_by_no_progress(self):
        parsed = self.delegate.parse_cli(
            ["codex", "safe", "--no-progress", "review"],
        )
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["progress"] = {"enabled": True, "initialDelaySec": 30, "intervalSec": 60}
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertFalse(request.progress)

    def test_config_enabled_progress_applies_when_intent_unset(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "review"])
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["progress"] = {"enabled": True, "initialDelaySec": 45, "intervalSec": 90}
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertTrue(request.progress)
        self.assertEqual(request.progress_initial_delay_sec, 45.0)
        self.assertEqual(request.progress_interval_sec, 90.0)

    def test_malformed_progress_config_hard_fails(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["progress"] = {"enabled": "yes"}
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_progress_config_rejects_boolean_timing_values(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["progress"] = {"initialDelaySec": True}
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_progress_config_rejects_nan_initial_delay(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["progress"] = {"initialDelaySec": float("nan")}
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_progress_config_rejects_infinite_interval(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["progress"] = {"intervalSec": float("inf")}
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_dry_run_droid_reasoning_effort(self):
        parsed = self.delegate.parse_cli(
            ["dry-run", "droid", "reviewer", "safe", "--reasoning-effort", "high", "review"]
        )
        self.assertTrue(parsed.launch.dry_run)
        self.assertEqual(parsed.launch.engine, "droid")
        self.assertEqual(parsed.launch.reasoning_effort, "high")

    def test_reasoning_effort_after_prompt_is_prompt_text(self):
        parsed = self.delegate.parse_cli(["codex", "safe", "review", "--reasoning-effort", "high"])
        self.assertIsNone(parsed.launch.reasoning_effort)
        self.assertEqual(parsed.launch.prompt_parts, ["review", "--reasoning-effort", "high"])

    def test_reasoning_effort_requires_value(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--reasoning-effort"])
        self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

    def test_reasoning_effort_rejects_option_looking_value(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["codex", "safe", "--reasoning-effort", "--prompt-file", "task.md"]
            )
        self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

    def test_reasoning_effort_rejects_help_token_as_value(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["codex", "safe", "--reasoning-effort", "--help"])
        self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

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
        self.assertEqual(parsed.launch.engine, "codex")
        self.assertEqual(parsed.launch.mode, "work")

    def test_claude_direct_commands_parse(self):
        parsed = self.delegate.parse_cli(["claude", "safe", "--reasoning-effort", "high", "review"])
        self.assertEqual(parsed.subcommand, "claude")
        self.assertEqual(parsed.launch.engine, "claude")
        self.assertEqual(parsed.launch.mode, "safe")
        self.assertEqual(parsed.launch.reasoning_effort, "high")
        self.assertEqual(parsed.launch.prompt_parts, ["review"])

    def test_grok_direct_commands_parse(self):
        parsed = self.delegate.parse_cli(["grok", "safe", "review"])
        self.assertEqual(parsed.subcommand, "grok")
        self.assertEqual(parsed.launch.engine, "grok")
        self.assertEqual(parsed.launch.mode, "safe")
        parsed = self.delegate.parse_cli(
            ["grok", "safe", "--prompt-file", "task.md"],
        )
        self.assertEqual(parsed.launch.prompt_file, "task.md")

    def test_dry_run_grok_parses(self):
        parsed = self.delegate.parse_cli(["dry-run", "grok", "work", "fix"])
        self.assertEqual(parsed.subcommand, "grok")
        self.assertTrue(parsed.launch.dry_run)
        parsed = self.delegate.parse_cli(
            ["dry-run", "grok", "safe", "--prompt-file", "task.md"],
        )
        self.assertEqual(parsed.launch.prompt_file, "task.md")

    def test_dry_run_codex_parses(self):
        parsed = self.delegate.parse_cli(["dry-run", "codex", "safe", "review"])
        self.assertEqual(parsed.subcommand, "codex")
        self.assertTrue(parsed.launch.dry_run)

    def test_dry_run_claude_parses(self):
        parsed = self.delegate.parse_cli(["dry-run", "claude", "work", "ship"])
        self.assertEqual(parsed.subcommand, "claude")
        self.assertTrue(parsed.launch.dry_run)

    def test_json_describe_shape(self):
        payload = self.delegate.describe_payload(self.delegate.DEFAULT_CONFIG, "embedded-default")
        self.assertTrue(payload["ok"])
        self.assertIn("safe", payload["modes"])
        self.assertIn("work", payload["modes"])
        self.assertIn("cursor", payload["modeMapping"])
        self.assertIn("claude", payload["modeMapping"])
        self.assertIn("codex", payload["modeMapping"])
        self.assertIn("claude", payload["engines"])
        self.assertIn("grok", payload["engines"])
        self.assertIn("codex", payload["engines"])
        self.assertIn("policyProfiles", payload)
        self.assertIn("policyFieldSupport", payload)
        self.assertIn("effectivePolicy", payload)
        self.assertIn("claude", payload["effectivePolicy"])
        self.assertIn("codex", payload["effectivePolicy"])
        self.assertIn("passThrough", payload)

    def test_describe_claude_effective_policy_masks_global_external_sandbox_bypass(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["policy"]["profile"] = "external-sandbox"
        payload = self.delegate.describe_payload(config, "test")
        self.assertFalse(payload["effectivePolicy"]["claude"]["work"]["bypassApprovalsAndSandbox"])
        self.assertNotIn("bypassPermissions", payload["modeMapping"]["claude"]["work"])

    def test_describe_claude_effective_policy_reports_harness_scoped_bypass(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["policy"]["harness"] = {"claude": {"work": {"bypassApprovalsAndSandbox": True}}}
        payload = self.delegate.describe_payload(config, "test")
        self.assertTrue(payload["effectivePolicy"]["claude"]["work"]["bypassApprovalsAndSandbox"])
        self.assertIn("bypassPermissions", payload["modeMapping"]["claude"]["work"])

    def test_pass_through_parses_before_subcommand(self):
        parsed = self.delegate.parse_cli(["--pass-through", "cursor", "safe", "hello"])
        self.assertTrue(parsed.global_options.pass_through)
        self.assertEqual(parsed.subcommand, "cursor")

    def test_json_pass_through_is_invalid(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--json", "--pass-through", "cursor", "safe", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_launch_global_options_after_mode_are_rejected(self):
        misplaced_options = [
            ["--pass-through"],
            ["--completion-report", "none"],
            ["--no-completion-report"],
        ]
        for option_tokens in misplaced_options:
            with self.subTest(option=option_tokens):
                with self.assertRaises(self.delegate.DelegateError) as ctx:
                    self.delegate.parse_cli(["codex", "safe", *option_tokens, "hello"])
                self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_dry_run_global_options_after_subcommand_are_rejected(self):
        misplaced_options = [
            ["--pass-through"],
            ["--completion-report", "none"],
            ["--no-completion-report"],
        ]
        for option_tokens in misplaced_options:
            with self.subTest(option=option_tokens):
                with self.assertRaises(self.delegate.DelegateError) as ctx:
                    self.delegate.parse_cli(["dry-run", *option_tokens, "codex", "safe", "hello"])
                self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_completion_report_none_flag(self):
        parsed = self.delegate.parse_cli(["--completion-report", "none", "cursor", "safe", "hello"])
        self.assertEqual(parsed.global_options.completion_report, "none")

    def test_no_completion_report_alias(self):
        parsed = self.delegate.parse_cli(["--no-completion-report", "cursor", "safe", "hello"])
        self.assertEqual(parsed.global_options.completion_report, "none")

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
                parsed.launch.prompt_parts, parsed.launch.prompt_file, io.StringIO()
            )
            effective = self.delegate.effective_prompt(
                prompt,
                completion_report_mode="markdown",
            )
            self.assertIn("Delegate sub-agent skill review requirement", effective)
            self.assertIn("Delegate completion report requirement", effective)
            self.assertEqual(prompt_path.read_text(), "original prompt\n")

    def test_nonblocking_stdin_select_failure_does_not_read(self):
        class BadSelectableStdin:
            def isatty(self):
                return False

            def fileno(self):
                raise OSError("not selectable")

            def read(self):
                raise AssertionError("read should not be called")

        stdin = BadSelectableStdin()
        self.assertIsNone(self.delegate.read_stdin_source(stdin, block=False))

    def test_snapshot_latest_and_handle_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["snapshot", "--latest", "cursor", "cursor"])
        self.assertEqual(ctx.exception.error, "ambiguous_snapshot_target")

    def test_run_output_without_selector_defaults_to_completion_report(self):
        parsed = self.delegate.parse_cli(["run-output", "cursor"])
        self.assertEqual(parsed.subcommand, "run-output")
        self.assertEqual(parsed.run_output.handle, "cursor")
        self.assertTrue(parsed.run_output.completion_report)
        self.assertTrue(parsed.run_output.default)

    def test_runs_limit_must_be_positive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["runs", "--limit", "0"])
        self.assertEqual(ctx.exception.error, "invalid_limit")

    def test_has_misplaced_global_option_detects_exact_tokens(self):
        self.assertFalse(self.delegate.has_misplaced_global_option([]))
        self.assertFalse(self.delegate.has_misplaced_global_option(["--jsonish"]))
        self.assertTrue(self.delegate.has_misplaced_global_option(["prompt", "--json"]))

    def test_parse_required_positive_int_option(self):
        parsed, next_index = self.delegate.parse_required_positive_int_option(
            ["--limit", "3"],
            0,
            option_label="runs --limit",
            missing_error="missing_limit",
            invalid_error="invalid_limit",
        )
        self.assertEqual(parsed, 3)
        self.assertEqual(next_index, 2)

    def test_parse_required_positive_int_option_errors(self):
        with self.assertRaises(self.delegate.DelegateError) as missing:
            self.delegate.parse_required_positive_int_option(
                ["--limit"],
                0,
                option_label="runs --limit",
                missing_error="missing_limit",
                invalid_error="invalid_limit",
            )
        self.assertEqual(missing.exception.error, "missing_limit")

        with self.assertRaises(self.delegate.DelegateError) as invalid:
            self.delegate.parse_required_positive_int_option(
                ["--limit", "nope"],
                0,
                option_label="runs --limit",
                missing_error="missing_limit",
                invalid_error="invalid_limit",
            )
        self.assertEqual(invalid.exception.error, "invalid_limit")

    def test_runs_active_and_recent_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["runs", "--active", "--recent"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_run_output_raw_and_tail_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["run-output", "cursor", "--stdout", "--raw", "--tail", "5"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_run_output_raw_and_max_chars_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["run-output", "cursor", "--stdout", "--raw", "--max-chars", "1000"]
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("--max-chars", ctx.exception.message)

    def test_run_output_max_chars_must_be_positive_integer(self):
        with self.assertRaises(self.delegate.DelegateError) as missing:
            self.delegate.parse_cli(["run-output", "cursor", "--stdout", "--max-chars"])
        self.assertEqual(missing.exception.error, "missing_max_chars")

        with self.assertRaises(self.delegate.DelegateError) as invalid:
            self.delegate.parse_cli(["run-output", "cursor", "--stdout", "--max-chars", "nope"])
        self.assertEqual(invalid.exception.error, "invalid_max_chars")

        with self.assertRaises(self.delegate.DelegateError) as zero:
            self.delegate.parse_cli(["run-output", "cursor", "--stdout", "--max-chars", "0"])
        self.assertEqual(zero.exception.error, "invalid_max_chars")

    def test_run_output_max_chars_is_parsed(self):
        parsed = self.delegate.parse_cli(
            ["run-output", "cursor", "--stdout", "--max-chars", "12000"]
        )
        self.assertEqual(parsed.run_output.max_chars, 12000)

    def test_run_output_stdout_without_tail_defaults_to_bounded_tail(self):
        parsed = self.delegate.parse_cli(["run-output", "cursor", "--stdout"])
        self.assertTrue(parsed.run_output.stdout)
        self.assertEqual(parsed.run_output.tail, self.delegate.RUN_OUTPUT_DEFAULT_TAIL_LINES)

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

    def test_worktree_option_value_rejects_next_option_token(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["worktree", "list", "--harness", "--status", "present"])
        self.assertEqual(ctx.exception.error, "missing_option_value")
        self.assertIn("--harness requires a value", ctx.exception.message)

    def test_parse_kimi_safe(self):
        parsed = self.delegate.parse_cli(["kimi", "safe", "review this"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertEqual(parsed.launch.engine, "kimi")
        self.assertEqual(parsed.launch.mode, "safe")
        self.assertEqual(parsed.launch.prompt_parts, ["review this"])

    def test_parse_kimi_work(self):
        parsed = self.delegate.parse_cli(["kimi", "work", "fix this"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertEqual(parsed.launch.engine, "kimi")
        self.assertEqual(parsed.launch.mode, "work")
        self.assertEqual(parsed.launch.prompt_parts, ["fix this"])

    def test_parse_kimi_dry_run(self):
        parsed = self.delegate.parse_cli(["dry-run", "kimi", "safe", "review"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertTrue(parsed.launch.dry_run)
        self.assertEqual(parsed.launch.engine, "kimi")
        self.assertEqual(parsed.launch.mode, "safe")

    def test_parse_kimi_help(self):
        parsed = self.delegate.parse_cli(["kimi", "--help"])
        self.assertEqual(parsed.subcommand, "help")
        self.assertEqual(parsed.help_topic, "kimi")

    def test_parse_kimi_prompt_file(self):
        parsed = self.delegate.parse_cli(["kimi", "safe", "--prompt-file", "task.md"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertEqual(parsed.launch.engine, "kimi")
        self.assertEqual(parsed.launch.mode, "safe")
        self.assertEqual(parsed.launch.prompt_file, "task.md")
        self.assertEqual(parsed.launch.prompt_parts, [])

    def test_parse_kimi_unknown_mode(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["kimi", "agent", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_mode")

    def test_worktree_remove_keep_branch_and_force_are_mutually_exclusive(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["worktree", "remove", "cursor-1", "--keep-branch", "--force"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_worktree_prune_requires_filter_at_execution_time(self):
        parsed = self.delegate.parse_cli(["worktree", "prune"])
        self.assertEqual(parsed.worktree.action, "prune")
        self.assertFalse(parsed.worktree.merged)
        self.assertIsNone(parsed.worktree.older_than_days)

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
        parsed = self.delegate.parse_cli(["--isolation", "worktree", "cursor", "work", "fix this"])
        self.assertEqual(parsed.global_options.isolation, "worktree")
        self.assertEqual(parsed.launch.engine, "cursor")
        self.assertEqual(parsed.launch.mode, "work")

    def test_isolation_none_codex_work_parses(self):
        parsed = self.delegate.parse_cli(["--isolation", "none", "codex", "work", "implement"])
        self.assertEqual(parsed.global_options.isolation, "none")
        self.assertEqual(parsed.launch.engine, "codex")
        self.assertEqual(parsed.launch.mode, "work")

    def test_isolation_auto_droid_safe_parses(self):
        parsed = self.delegate.parse_cli(
            ["--isolation", "auto", "droid", "minimax", "safe", "review"]
        )
        self.assertEqual(parsed.global_options.isolation, "auto")
        self.assertEqual(parsed.launch.engine, "droid")
        self.assertEqual(parsed.launch.mode, "safe")

    def test_isolation_unknown_value_raises(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--isolation", "bananas", "cursor", "work", "fix"])
        self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_isolation_missing_value_raises(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["--isolation"])
        self.assertEqual(ctx.exception.error, "missing_isolation_value")

    def test_isolation_after_subcommand_is_misplaced(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["cursor", "work", "--isolation", "worktree", "fix"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_run_input_keys_contains_isolation(self):
        self.assertIn("isolation", self.delegate.RUN_INPUT_KEYS)
        self.assertIn("progress", self.delegate.RUN_INPUT_KEYS)

    def test_run_input_json_unknown_key_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                        "isolation": "worktree",
                        "bogus": "should-fail",
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
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                        "isolation": "bananas",
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
            self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_run_input_json_claude_safe_uses_stdin_and_model_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "claude",
                        "mode": "safe",
                        "model": "claude-sonnet-4-6",
                        "cwd": tmp,
                        "prompt": "SECRET JSON CLAUDE PROMPT",
                        "reasoningEffort": "high",
                    }
                )
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(request.engine, "claude")
            self.assertEqual(request.mode, "safe")
            self.assertEqual(request.model, "claude-sonnet-4-6")
            self.assertEqual(request.prompt_transport, self.delegate.PROMPT_TRANSPORT_STDIN)
            self.assertEqual(request.stdin_text, request.prompt)
            self.assertNotIn("SECRET JSON CLAUDE PROMPT", request.argv)
            self.assertEqual(request.reasoning_transport, "claude-effort-flag")

    def test_run_input_json_threads_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                        "progress": True,
                    }
                )
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            cfg = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            cfg["droid"]["models"] = {"minimax": "model-id"}
            request = self.delegate.request_from_input_json(parsed, cfg)
            self.assertTrue(request.progress)

    def test_run_input_json_missing_progress_uses_config_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
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
            cfg = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            cfg["droid"]["models"] = {"minimax": "model-id"}
            cfg["progress"] = {"enabled": True, "initialDelaySec": 12, "intervalSec": 34}
            request = self.delegate.request_from_input_json(parsed, cfg)
            self.assertTrue(request.progress)
            self.assertEqual(request.progress_initial_delay_sec, 12.0)
            self.assertEqual(request.progress_interval_sec, 34.0)

    def test_run_input_json_progress_false_overrides_config_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                        "progress": False,
                    }
                )
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            cfg = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            cfg["droid"]["models"] = {"minimax": "model-id"}
            cfg["progress"] = {"enabled": True, "initialDelaySec": 30, "intervalSec": 60}
            request = self.delegate.request_from_input_json(parsed, cfg)
            self.assertFalse(request.progress)

    def test_run_input_json_threads_forbid_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["git", "-C", tmp, "init"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", tmp, "config", "user.email", "test@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", tmp, "config", "user.name", "Test User"],
                check=True,
                capture_output=True,
            )
            Path(tmp, "README.md").write_text("# test\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", tmp, "add", "README.md"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", tmp, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": tmp,
                        "isolation": "worktree",
                        "prompt": "hello",
                        "forbidCommit": True,
                    }
                )
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertTrue(request.forbid_commit)
            self.assertEqual(request.isolation_context.isolation_lifecycle, "persistent")

    def test_run_input_json_progress_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                        "progress": "yes",
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
            self.assertEqual(ctx.exception.error, "invalid_progress")

    def test_run_input_json_forbid_commit_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": tmp,
                        "isolation": "worktree",
                        "prompt": "hello",
                        "forbidCommit": "yes",
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
            self.assertEqual(ctx.exception.error, "invalid_forbid_commit")

    def test_run_input_json_claude_safe_rejects_isolation_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "claude",
                        "mode": "safe",
                        "cwd": tmp,
                        "isolation": "none",
                        "prompt": "hello",
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
            self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_run_input_json_claude_work_accepts_model_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "claude",
                        "mode": "work",
                        "model": "claude-opus-4-8",
                        "cwd": tmp,
                        "prompt": "ship",
                    }
                )
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(request.engine, "claude")
            self.assertEqual(request.mode, "work")
            self.assertEqual(request.model, "claude-opus-4-8")
            self.assertIn("claude-opus-4-8", request.argv)

    def test_run_input_json_workspace_config_resolves_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            local_delegate = workspace / ".delegate"
            local_delegate.mkdir()
            (local_delegate / "config.json").write_text(
                json.dumps(
                    {
                        "isolation": {"work": "worktree"},
                        "cursor": {"argvPrefix": ["agent"], "defaultModel": "test"},
                        "droid": {"binary": "droid", "models": {"minimax": "model-id"}},
                    }
                )
            )
            task = workspace / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "work",
                        "model": "minimax",
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
            # pre_read_run_json_for_config loads workspace-local config
            _ws, cfg, _src = self.delegate.pre_read_run_json_for_config(str(task), None)
            request = self.delegate.request_from_input_json(parsed, cfg)
            self.assertEqual(request.engine, "droid")
            self.assertEqual(request.mode, "work")

    def test_run_input_json_cwd_conflict_still_fails_with_isolation(self):
        repo1 = make_git_repo()
        repo2 = make_git_repo()
        self.addCleanup(repo1.cleanup)
        self.addCleanup(repo2.cleanup)
        task = Path(repo1.name) / "task.json"
        task.write_text(
            json.dumps(
                {
                    "engine": "droid",
                    "mode": "safe",
                    "model": "minimax",
                    "cwd": repo1.name,
                    "prompt": "hello",
                    "isolation": "auto",
                }
            )
        )
        parsed = self.delegate.ParsedCommand(
            "run",
            global_options=self.delegate.GlobalOptions(json_mode=True, cwd=repo2.name),
            run_json=self.delegate.RunJsonOptions(str(task)),
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
            self.delegate.parse_cli(["droid", "minimax", "work", "--isolation", "worktree", "fix"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")

    def test_isolation_after_subcommand_dry_run_cursor_is_misplaced(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["dry-run", "--isolation", "worktree", "cursor", "work", "fix"])
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
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                        "isolation": None,
                    }
                )
            )
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
            global_config.write_text(
                json.dumps(
                    {
                        "cursor": {"argvPrefix": ["agent"], "defaultModel": "global-model"},
                        "droid": {"binary": "droid", "models": {"minimax": "model-id"}},
                        "isolation": {"work": "none"},
                    }
                )
            )
            with tempfile.TemporaryDirectory() as workspace_tmp:
                # Create a workspace with local config that has isolation.work = worktree
                workspace = Path(workspace_tmp)
                local_delegate = workspace / ".delegate"
                local_delegate.mkdir()
                (local_delegate / "config.json").write_text(
                    json.dumps(
                        {
                            "cursor": {"argvPrefix": ["agent"], "defaultModel": "ws-model"},
                            "droid": {"binary": "droid", "models": {"minimax": "model-id"}},
                            "isolation": {"work": "worktree"},
                        }
                    )
                )
                # Create a task.json pointing at the workspace with local config
                task = workspace / "task.json"
                task.write_text(
                    json.dumps(
                        {
                            "engine": "cursor",
                            "mode": "work",
                            "cwd": str(workspace),
                            "prompt": "hello",
                        }
                    )
                )
                # The pre-read should load config from the workspace (with work = worktree)
                # and validate successfully.
                _ws, cfg, _src = self.delegate.pre_read_run_json_for_config(str(task), None)
                self.assertEqual(
                    cfg["isolation"]["work"],
                    "worktree",
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
                    "run",
                    global_options=self.delegate.GlobalOptions(json_mode=False),
                    run_json=self.delegate.RunJsonOptions(str(task)),
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
            (local_delegate / "config.json").write_text(
                json.dumps(
                    {
                        "cursor": {"argvPrefix": ["agent"], "defaultModel": "ws-model"},
                        "droid": {
                            "binary": "/nonexistent/droid",
                            "models": {"minimax": "model-id"},
                        },
                        "isolation": {"work": "worktree"},
                    }
                )
            )
            task = workspace / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "work",
                        "model": "minimax",
                        "cwd": str(workspace),
                        "prompt": "hello",
                    }
                )
            )
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
