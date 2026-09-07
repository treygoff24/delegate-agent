import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli_parser as parser_api
from delegate_agent import config as delegate_config
from delegate_agent import describe_payload as describe_api
from delegate_agent import errors as error_types
from delegate_agent import prompt_transport as transport_api
from delegate_agent import request_build as request_api
from delegate_agent import request_models as request_types
from delegate_agent import run_output_commands, runner

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"

DEFAULT_CONFIG = delegate_config.embedded_default_config()

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
            ["droid", "safe", "--model", "minimax", "analyze this"],
            ["droid", "work", "--model", "minimax", "fix this"],
            ["--json", "run", "--input-json", "task.json"],
            ["models"],
            ["--json", "models"],
            ["--json", "models", "--summary"],
            ["describe"],
            ["--json", "describe"],
            ["--json", "describe", "--summary"],
            ["config", "init"],
            ["--json", "config", "init", "--force"],
            ["setup"],
            ["--json", "setup"],
            ["--auth-profile", "work", "setup"],
            ["doctor"],
            ["--json", "doctor"],
            ["promote", "--actor", "hq", "--source", "main cba3446"],
            ["--json", "promote", "--actor", "hq", "--source", "v1", "--runtime-digest", "a" * 64],
            ["agent-help"],
            ["dry-run", "cursor", "work", "prompt"],
            ["dry-run", "claude", "safe", "prompt"],
            [
                "--json",
                "dry-run",
                "droid",
                "safe",
                "--model",
                "minimax",
                "--prompt-file",
                "task.md",
            ],
            ["snapshot", "cursor"],
            ["--json", "snapshot", "--latest", "cursor"],
            ["runs", "--active", "--limit", "5"],
            ["run-output", "cursor-2", "--completion-report"],
            ["run-output", "cursor", "--stdout", "--tail", "50"],
        ]
        for argv in examples:
            with self.subTest(argv=argv):
                parsed = parser_api.parse_cli(argv)
                self.assertIsNotNone(parsed.subcommand)

    def test_global_flags_before_subcommand(self):
        parsed = parser_api.parse_cli(["--json", "--cwd", "/tmp/repo", "models"])
        self.assertTrue(parsed.global_options.json_mode)
        self.assertEqual(parsed.global_options.cwd, "/tmp/repo")

    def test_all_global_options_are_normalized_before_dispatch(self):
        cases = (
            (["dry-run", "--json", "codex", "safe", "hello"], "json_mode", True),
            (["dry-run", "codex", "safe", "hello", "--cwd", "/tmp/repo"], "cwd", "/tmp/repo"),
            (
                ["dry-run", "--isolation", "none", "codex", "safe", "hello"],
                "isolation",
                "none",
            ),
            (
                ["dry-run", "codex", "safe", "hello", "--pass-through"],
                "pass_through",
                True,
            ),
            (
                ["dry-run", "--completion-report", "markdown", "codex", "safe", "hello"],
                "completion_report",
                "markdown",
            ),
            (
                ["dry-run", "codex", "safe", "hello", "--no-completion-report"],
                "completion_report",
                "none",
            ),
            (
                ["dry-run", "--auth-profile", "work", "codex", "safe", "hello"],
                "auth_profile",
                "work",
            ),
            (
                ["dry-run", "codex", "safe", "hello", "--group", "launch-group"],
                "group",
                "launch-group",
            ),
            (
                ["dry-run", "--notify", "room:ops", "codex", "safe", "hello"],
                "notify",
                "room:ops",
            ),
        )
        for argv, attribute, expected in cases:
            with self.subTest(option=attribute, argv=argv):
                parsed = parser_api.parse_cli(argv)
                self.assertEqual(getattr(parsed.global_options, attribute), expected)
                self.assertEqual(parsed.payload.prompt_parts, ["hello"])

    def test_group_stays_local_for_commands_that_own_it(self):
        cases = (
            (["runs", "--group", "local"], lambda parsed: parsed.payload.group),
            (["ps", "--group", "local"], lambda parsed: parsed.payload.group),
            (["wait", "--group", "local"], lambda parsed: parsed.payload.group),
            (
                ["mail", "send", "--group", "local", "body"],
                lambda parsed: parsed.payload.group,
            ),
            (
                ["worktree", "list", "--group", "local"],
                lambda parsed: parsed.payload.group,
            ),
            (
                ["worktree", "remove", "--group", "local"],
                lambda parsed: parsed.payload.group,
            ),
            (
                ["worktree", "prune", "--group", "local"],
                lambda parsed: parsed.payload.group,
            ),
        )
        for argv, local_group in cases:
            with self.subTest(argv=argv):
                parsed = parser_api.parse_cli(argv)
                self.assertIsNone(parsed.global_options.group)
                self.assertEqual(local_group(parsed), "local")

    def test_group_is_global_for_a_launch(self):
        parsed = parser_api.parse_cli(["codex", "safe", "hello", "--group", "launch-group"])
        self.assertEqual(parsed.global_options.group, "launch-group")
        self.assertEqual(parsed.payload.prompt_parts, ["hello"])

    def test_completion_report_stays_local_for_commands_that_own_it(self):
        run_output = parser_api.parse_cli(["run-output", "run-1", "--completion-report"])
        self.assertIsNone(run_output.global_options.completion_report)
        self.assertTrue(run_output.payload.completion_report)

        wait = parser_api.parse_cli(["wait", "run-1", "--completion-report"])
        self.assertIsNone(wait.global_options.completion_report)
        self.assertTrue(wait.payload.completion_report)

    def test_option_terminator_keeps_global_tokens_literal(self):
        parsed = parser_api.parse_cli(
            ["dry-run", "codex", "safe", "hello", "--", "--json", "--group", "literal"]
        )
        self.assertFalse(parsed.global_options.json_mode)
        self.assertIsNone(parsed.global_options.group)
        self.assertEqual(parsed.payload.prompt_parts, ["hello", "--json", "--group", "literal"])

    def test_setup_accepts_only_json_and_auth_profile(self):
        parsed = parser_api.parse_cli(["--json", "--auth-profile", "work", "setup"])
        self.assertEqual(parsed.subcommand, "setup")
        self.assertTrue(parsed.global_options.json_mode)
        self.assertEqual(parsed.global_options.auth_profile, "work")

        trailing_json = parser_api.parse_cli(["setup", "--json"])
        self.assertTrue(trailing_json.global_options.json_mode)

    def test_parsed_command_and_promote_options_keep_dataclass_contracts(self):
        import dataclasses

        fields = {field.name for field in dataclasses.fields(request_types.ParsedCommand)}
        self.assertEqual(fields, {"subcommand", "global_options", "help_topic", "payload"})
        options = parser_api.parse_cli(["promote", "--actor", "a", "--source", "b"]).payload
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.actor = "c"

    def test_doctor_and_promote_parse_and_refuse_bad_input(self):
        parsed = parser_api.parse_cli(["--json", "doctor"])
        self.assertEqual(parsed.subcommand, "doctor")
        self.assertTrue(parsed.global_options.json_mode)
        parsed = parser_api.parse_cli(
            ["promote", "--source", "main", "--actor", "hq", "--runtime-digest", "b" * 64]
        )
        self.assertEqual(parsed.subcommand, "promote")
        self.assertEqual(parsed.payload.actor, "hq")
        self.assertEqual(parsed.payload.source, "main")
        self.assertEqual(parsed.payload.runtime_digest, "b" * 64)
        self.assertEqual(parser_api.parse_cli(["promote", "--help"]).subcommand, "help")
        failures = [
            (["doctor", "extra"], "unexpected_argument"),
            (["--cwd", "/tmp", "doctor"], "invalid_option_combination"),
            (
                ["--auth-profile", "work", "promote", "--actor", "a", "--source", "b"],
                "invalid_option_combination",
            ),
            (["promote", "--source", "main"], "missing_actor"),
            (["promote", "--actor", "hq"], "missing_source"),
            (["promote", "--actor", "--source", "main"], "missing_option_value"),
            (
                ["promote", "--actor", "hq", "--source", "main", "--runtime-digest", "xyz"],
                "invalid_runtime_digest",
            ),
            (["promote", "--actor", "hq", "--source", "main", "stray"], "unexpected_argument"),
        ]
        for argv, error in failures:
            with self.subTest(argv=argv):
                with self.assertRaises(error_types.DelegateError) as ctx:
                    parser_api.parse_cli(argv)
                self.assertEqual(ctx.exception.error, error)

    def test_setup_rejects_trailing_arguments(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["setup", "unexpected"])
        self.assertEqual(ctx.exception.error, "unexpected_argument")

    def test_setup_rejects_irrelevant_global_options(self):
        cases = (
            ["--cwd", "/tmp", "setup"],
            ["--isolation", "none", "setup"],
            ["--pass-through", "setup"],
            ["--completion-report", "none", "setup"],
            ["--no-completion-report", "setup"],
            ["--group", "batch", "setup"],
        )
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli(argv)
            self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_setup_help_does_not_bypass_irrelevant_global_rejection(self):
        cases = (
            ["--cwd", "/tmp", "setup", "--help"],
            ["--isolation", "none", "setup", "--help"],
            ["--pass-through", "setup", "--help"],
            ["--completion-report", "none", "setup", "--help"],
            ["--no-completion-report", "setup", "--help"],
            ["--group", "batch", "setup", "--help"],
        )
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli(argv)
            self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_codex_fast_flags_parse_as_tri_state(self):
        fast = parser_api.parse_cli(["codex", "safe", "--fast", "review"])
        standard = parser_api.parse_cli(["codex", "safe", "--no-fast", "review"])
        inherited = parser_api.parse_cli(["codex", "safe", "review"])
        self.assertIs(fast.payload.fast, True)
        self.assertIs(standard.payload.fast, False)
        self.assertIsNone(inherited.payload.fast)

    def test_codex_fast_flags_are_mutually_exclusive_and_codex_only(self):
        cases = (
            (["codex", "safe", "--fast", "--no-fast", "review"], "invalid_option_combination"),
            (["codex", "safe", "--fast", "--fast", "review"], "invalid_option_combination"),
            (["claude", "safe", "--fast", "review"], "unsupported_fast"),
            (["droid", "safe", "--no-fast", "review"], "unsupported_fast"),
        )
        for argv, error in cases:
            with self.subTest(argv=argv), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli(argv)
            self.assertEqual(ctx.exception.error, error)

    def test_run_input_json_fast_accepts_boolean_or_null_for_codex(self):
        for value, expected in ((True, True), (False, False), (None, None)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "task.json"
                path.write_text(
                    json.dumps(
                        {
                            "engine": "codex",
                            "mode": "call",
                            "prompt": "summarize",
                            "readOnly": True,
                            "fast": value,
                        }
                    ),
                    encoding="utf-8",
                )
                parsed = parser_api.parse_cli(["run", "--input-json", str(path)])
                request = request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
                self.assertIs(request.fast, expected)

    def test_run_input_json_fast_rejects_invalid_type_and_non_codex(self):
        cases = (
            ("codex", "yes", "invalid_fast"),
            ("claude", True, "unsupported_fast"),
            ("claude", None, "unsupported_fast"),
            ("opencode", None, "unsupported_fast"),
        )
        for engine, value, error in cases:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "task.json"
                path.write_text(
                    json.dumps(
                        {
                            "engine": engine,
                            "mode": "call",
                            "prompt": "summarize",
                            "readOnly": True,
                            "fast": value,
                        }
                    ),
                    encoding="utf-8",
                )
                parsed = parser_api.parse_cli(["run", "--input-json", str(path)])
                with self.assertRaises(error_types.DelegateError) as ctx:
                    request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
                self.assertEqual(ctx.exception.error, error)

    def test_run_input_json_agent_rejected_by_presence_for_non_opencode(self):
        cases = (
            ("codex", None),
            ("claude", "reviewer"),
            ("cursor", 1),
            ("droid", False),
            ("grok", []),
            ("devin", {}),
            ("kimi", ""),
        )
        for engine, value in cases:
            with self.subTest(engine=engine, value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "task.json"
                payload = {
                    "engine": engine,
                    "mode": "call",
                    "prompt": "summarize",
                    "agent": value,
                }
                if engine == "droid":
                    payload["model"] = "minimax"
                path.write_text(json.dumps(payload), encoding="utf-8")
                parsed = parser_api.parse_cli(["run", "--input-json", str(path)])
                with self.assertRaises(error_types.DelegateError) as ctx:
                    request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
                self.assertEqual(ctx.exception.error, "unsupported_agent")

    def test_run_input_json_opencode_null_agent_matches_omission(self):
        requests = []
        with tempfile.TemporaryDirectory() as tmp:
            for suffix, agent in (("omitted", ...), ("null", None)):
                payload = {"engine": "opencode", "mode": "call", "prompt": "summarize"}
                if agent is not ...:
                    payload["agent"] = agent
                path = Path(tmp) / f"task-{suffix}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                parsed = parser_api.parse_cli(["run", "--input-json", str(path)])
                requests.append(request_api.request_from_input_json(parsed, DEFAULT_CONFIG))

        normalized_argv = []
        for request in requests:
            argv = list(request.argv)
            argv[argv.index("--dir") + 1] = "<call-cwd>"
            normalized_argv.append(argv)
        self.assertEqual(normalized_argv[0], normalized_argv[1])
        self.assertEqual(requests[0].env_overrides, requests[1].env_overrides)

    def test_models_and_describe_reject_redacted_flag(self):
        for subcommand in ("models", "describe"):
            with self.subTest(subcommand=subcommand):
                with self.assertRaises(error_types.DelegateError) as ctx:
                    parser_api.parse_cli([subcommand, "--redacted"])
                self.assertEqual(ctx.exception.error, "unexpected_argument")

    def test_models_and_describe_parse_summary_option(self):
        for subcommand in ("models", "describe"):
            with self.subTest(subcommand=subcommand):
                parsed = parser_api.parse_cli([subcommand, "--summary"])
                self.assertTrue(parsed.payload.summary)

    def test_models_unknown_option_fails_clearly(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["models", "--verbose"])
        self.assertEqual(ctx.exception.error, "unexpected_argument")

    def test_auth_profile_accepted_for_models_and_capabilities_reads(self):
        for argv in (
            ["--auth-profile", "work", "models"],
            ["--auth-profile", "work", "models", "codex", "--live"],
            ["--auth-profile", "work", "capabilities"],
        ):
            with self.subTest(argv=argv):
                parsed = parser_api.parse_cli(argv)
                self.assertEqual(parsed.global_options.auth_profile, "work")

    def test_auth_profile_accepted_for_capabilities_refresh(self):
        parsed = parser_api.parse_cli(["--auth-profile", "work", "capabilities", "refresh"])
        self.assertEqual(parsed.global_options.auth_profile, "work")
        self.assertTrue(parsed.payload.refresh)
        self.assertIsNone(parsed.payload.engines)

    def test_capabilities_refresh_accepts_engine_subset(self):
        parsed = parser_api.parse_cli(["capabilities", "refresh", "codex", "claude", "codex"])
        self.assertTrue(parsed.payload.refresh)
        self.assertEqual(parsed.payload.engines, ("codex", "claude"))

    def test_capabilities_refresh_rejects_unknown_engine(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["capabilities", "refresh", "not-a-harness"])
        self.assertEqual(ctx.exception.error, "invalid_engine")

    def test_capabilities_refresh_accepts_trailing_auth_profile(self):
        parsed = parser_api.parse_cli(["capabilities", "refresh", "--auth-profile", "work"])
        self.assertEqual(parsed.global_options.auth_profile, "work")
        self.assertTrue(parsed.payload.refresh)

    def test_ps_rejects_conflicting_runs_filters_with_ps_specific_message(self):
        for flag in ("--running", "--stale", "--recent"):
            with self.subTest(flag=flag):
                with self.assertRaises(error_types.DelegateError) as ctx:
                    parser_api.parse_cli(["ps", flag])
                self.assertEqual(ctx.exception.error, "invalid_option_combination")
                self.assertIn("delegate ps always shows active runs", ctx.exception.message)
                self.assertNotIn("mutually exclusive", ctx.exception.message)

    def test_ps_tolerates_explicit_active_flag(self):
        parsed = parser_api.parse_cli(["ps", "--active"])
        self.assertEqual(parsed.subcommand, "ps")
        self.assertTrue(parsed.payload.active)

    def test_ps_errors_name_ps_not_the_delegated_runs_command(self):
        cases = (
            (["ps", "bogus"], "unknown_option", "ps does not support option: bogus"),
            (["ps", "--limit", "0"], "invalid_limit", "ps --limit must be at least 1."),
            (["ps", "--limit"], "missing_limit", "ps --limit requires a positive integer."),
            (["ps", "--harness"], "missing_harness", "ps --harness requires a harness name."),
            (["ps", "--harness", "nope"], "invalid_harness", "ps --harness must be one of"),
            (["ps", "--group"], "missing_group", "ps --group requires a group name."),
        )
        for argv, error, message in cases:
            with self.subTest(argv=argv):
                with self.assertRaises(error_types.DelegateError) as ctx:
                    parser_api.parse_cli(argv)
                self.assertEqual(ctx.exception.error, error)
                self.assertIn(message, ctx.exception.message)
                self.assertNotIn("runs", ctx.exception.message)

    def test_runs_errors_still_name_runs(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["runs", "bogus"])
        self.assertEqual(ctx.exception.message, "runs does not support option: bogus")
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["runs", "--limit", "0"])
        self.assertEqual(ctx.exception.message, "runs --limit must be at least 1.")

    def test_capabilities_read_rejects_engine_arguments(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["capabilities", "codex"])
        self.assertEqual(ctx.exception.error, "unexpected_argument")

    def test_auth_profile_remains_rejected_for_describe(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["--auth-profile", "work", "describe"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_auth_profile_accepted_for_grok_launch(self):
        parsed = parser_api.parse_cli(["--auth-profile", "work", "grok", "safe", "x"])
        self.assertEqual(parsed.global_options.auth_profile, "work")
        self.assertEqual(parsed.payload.engine, "grok")

    def test_infer_global_json_after_value_taking_globals(self):
        cases = [
            ["--isolation", "worktree", "--json", "cursor"],
            ["--completion-report", "markdown", "--json", "cursor"],
            ["--cwd", "/tmp/repo", "--completion-report", "none", "--json", "models"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertTrue(parser_api.infer_global_json(argv))

    def test_infer_global_json_after_flag_globals(self):
        self.assertTrue(parser_api.infer_global_json(["--pass-through", "--json", "cursor"]))
        self.assertTrue(
            parser_api.infer_global_json(["--no-completion-report", "--json", "cursor"])
        )

    def test_json_after_inline_prompt_text_is_global(self):
        parsed = parser_api.parse_cli(
            ["dry-run", "droid", "work", "--model", "minimax", "hello", "--json"]
        )
        self.assertTrue(parsed.global_options.json_mode)
        self.assertEqual(parsed.payload.prompt_parts, ["hello"])

    def test_json_in_launch_tail_before_prompt_text_is_accepted(self):
        # Agents reflexively append --json; before inline prompt text it is unambiguous.
        for argv in (
            ["codex", "work", "--prompt-file", "task.md", "--json"],
            ["codex", "work", "--json", "--prompt-file", "task.md"],
            ["droid", "safe", "--model", "minimax", "--json"],
            ["dry-run", "codex", "work", "--prompt-file", "task.md", "--json"],
        ):
            with self.subTest(argv=argv):
                parsed = parser_api.parse_cli(argv)
                self.assertTrue(parsed.global_options.json_mode)
                self.assertEqual(parsed.payload.prompt_parts, [])

    def test_json_before_prompt_text_keeps_prompt_intact(self):
        parsed = parser_api.parse_cli(["cursor", "safe", "--json", "review", "the", "diff"])
        self.assertTrue(parsed.global_options.json_mode)
        self.assertEqual(parsed.payload.prompt_parts, ["review", "the", "diff"])

    def test_prompt_file_before_prompt_text(self):
        parsed = parser_api.parse_cli(["cursor", "safe", "--prompt-file", "task.md"])
        self.assertEqual(parsed.payload.prompt_file, "task.md")
        self.assertEqual(parsed.payload.prompt_parts, [])

    def test_output_schema_before_prompt_text(self):
        parsed = parser_api.parse_cli(["codex", "safe", "--output-schema", "schema.json", "x"])
        self.assertEqual(parsed.payload.output_schema, "schema.json")
        self.assertEqual(parsed.payload.prompt_parts, ["x"])

    def test_output_schema_duplicate_rejected(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(
                ["codex", "safe", "--output-schema", "a.json", "--output-schema", "b.json", "x"]
            )
        self.assertEqual(ctx.exception.error, "invalid_output_schema")

    def test_output_schema_requires_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["codex", "safe", "--output-schema"])
        self.assertEqual(ctx.exception.error, "missing_output_schema")

    def test_pure_and_timeout_parse_for_supported_call_engines(self):
        parsed = parser_api.parse_cli(
            ["claude", "call", "--pure", "--timeout", "12", "answer this"]
        )
        self.assertTrue(parsed.payload.pure)
        self.assertEqual(parsed.payload.timeout, 12)

    def test_pure_rejects_non_call_conflict_and_unsupported_engine(self):
        cases = (
            (["claude", "safe", "--pure", "x"], "unsupported_pure_call"),
            (
                ["claude", "call", "--pure", "--read-only", "x"],
                "pure_conflicts_read_only",
            ),
            (
                ["--group", "g", "claude", "call", "--pure", "x"],
                "pure_conflicts_group",
            ),
            (["cursor", "call", "--pure", "x"], "unsupported_pure_call"),
            (["opencode", "call", "--pure", "x"], "unsupported_pure_call"),
            (["pi", "call", "--pure", "x"], "unsupported_pure_call"),
            (["omp", "call", "--pure", "x"], "unsupported_pure_call"),
            (["codex", "call", "--pure", "x"], "unsupported_pure_call"),
        )
        for argv, error in cases:
            with self.subTest(argv=argv), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli(argv)
            self.assertEqual(ctx.exception.error, error)
        self.assertTrue(ctx.exception.next_actions)
        self.assertIn("claude", ctx.exception.next_actions[0])
        self.assertNotIn("opencode", ctx.exception.next_actions[0])
        self.assertNotIn("pi", ctx.exception.next_actions[0])
        self.assertNotIn("codex", ctx.exception.next_actions[0])

    def test_timeout_is_positive_integer_and_rejected_with_pass_through(self):
        invalid = (
            (["claude", "call", "--timeout", "0", "x"], "invalid_timeout"),
            (["claude", "call", "--timeout", "-1", "x"], "invalid_timeout"),
            (["claude", "call", "--timeout", "1.5", "x"], "invalid_timeout"),
            (["claude", "call", "--timeout"], "missing_timeout"),
            (
                ["--pass-through", "claude", "safe", "--timeout", "1", "x"],
                "invalid_option_combination",
            ),
            (
                ["--pass-through", "droid", "work", "--timeout", "1", "x"],
                "invalid_option_combination",
            ),
        )
        for argv, error in invalid:
            with self.subTest(argv=argv), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli(argv)
            self.assertEqual(ctx.exception.error, error)

    def test_timeout_parses_for_safe_and_work(self):
        for argv in (
            ["claude", "safe", "--timeout", "12", "x"],
            ["cursor", "work", "--timeout", "12", "x"],
            ["droid", "work", "--timeout", "12", "x"],
        ):
            with self.subTest(argv=argv):
                parsed = parser_api.parse_cli(argv)
                self.assertEqual(parsed.payload.timeout, 12)

    def test_prompt_file_after_prompt_text_is_rejected(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["cursor", "safe", "hello", "--prompt-file", "task.md"])
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_codex_reasoning_effort_before_prompt(self):
        parsed = parser_api.parse_cli(["codex", "safe", "--reasoning-effort", "high", "review"])
        self.assertEqual(parsed.payload.reasoning_effort, "high")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

    def test_droid_reasoning_effort_after_alias_and_mode(self):
        parsed = parser_api.parse_cli(
            ["droid", "safe", "--model", "reviewer", "--reasoning-effort", "high", "review"]
        )
        self.assertEqual(parsed.payload.engine, "droid")
        self.assertEqual(parsed.payload.model, "reviewer")
        self.assertEqual(parsed.payload.reasoning_effort, "high")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

    def test_progress_launch_option_before_prompt(self):
        parsed = parser_api.parse_cli(["codex", "safe", "--progress", "review"])
        self.assertEqual(parsed.payload.progress_intent, "on")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

    def test_continuity_mode_is_parsed_before_prompt(self):
        for continuity_mode in ("pinned", "fungible", "panel"):
            with self.subTest(continuity_mode=continuity_mode):
                parsed = parser_api.parse_cli(
                    [
                        "codex",
                        "work",
                        "--continuity-mode",
                        continuity_mode,
                        "implement",
                    ]
                )
                self.assertEqual(parsed.payload.continuity_mode, continuity_mode)
                self.assertEqual(parsed.payload.prompt_parts, ["implement"])

    def test_continuity_mode_rejects_missing_and_unknown_values(self):
        for args in (
            ["codex", "work", "--continuity-mode"],
            ["codex", "work", "--continuity-mode", "elastic", "implement"],
        ):
            with self.subTest(args=args), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli(args)
            self.assertEqual(ctx.exception.error, "invalid_continuity_mode")

    def test_progress_after_prompt_is_prompt_text(self):
        parsed = parser_api.parse_cli(["codex", "safe", "review", "--progress"])
        self.assertIsNone(parsed.payload.progress_intent)
        self.assertEqual(parsed.payload.prompt_parts, ["review", "--progress"])

    def test_no_progress_launch_option_before_prompt(self):
        parsed = parser_api.parse_cli(["codex", "safe", "--no-progress", "review"])
        self.assertEqual(parsed.payload.progress_intent, "off")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

    def test_no_progress_after_prompt_is_prompt_text(self):
        parsed = parser_api.parse_cli(["codex", "safe", "review", "--no-progress"])
        self.assertIsNone(parsed.payload.progress_intent)
        self.assertEqual(parsed.payload.prompt_parts, ["review", "--no-progress"])

    def test_progress_and_no_progress_cannot_combine(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["codex", "safe", "--progress", "--no-progress", "review"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_forbid_commit_launch_option_before_prompt(self):
        parsed = parser_api.parse_cli(["cursor", "work", "--forbid-commit", "fix"])
        self.assertTrue(parsed.payload.forbid_commit)
        self.assertEqual(parsed.payload.prompt_parts, ["fix"])

    def test_forbid_commit_after_prompt_is_prompt_text(self):
        parsed = parser_api.parse_cli(["cursor", "work", "fix", "--forbid-commit"])
        self.assertFalse(parsed.payload.forbid_commit)
        self.assertEqual(parsed.payload.prompt_parts, ["fix", "--forbid-commit"])

    def test_progress_with_pass_through_is_invalid(self):
        parsed = parser_api.parse_cli(["--pass-through", "codex", "safe", "--progress", "x"])
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.request_from_parsed(
                parsed,
                DEFAULT_CONFIG,
                io.StringIO(""),
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_config_enabled_progress_conflicts_with_pass_through(self):
        parsed = parser_api.parse_cli(["--pass-through", "codex", "safe", "x"])
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["progress"] = {"enabled": True, "initialDelaySec": 30, "intervalSec": 60}
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_config_enabled_progress_cleared_by_no_progress(self):
        parsed = parser_api.parse_cli(
            ["codex", "safe", "--no-progress", "review"],
        )
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["progress"] = {"enabled": True, "initialDelaySec": 30, "intervalSec": 60}
        request = request_api.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertFalse(request.progress)

    def test_config_enabled_progress_applies_when_intent_unset(self):
        parsed = parser_api.parse_cli(["codex", "safe", "review"])
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["progress"] = {"enabled": True, "initialDelaySec": 45, "intervalSec": 90}
        request = request_api.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertTrue(request.progress)
        self.assertEqual(request.progress_initial_delay_sec, 45.0)
        self.assertEqual(request.progress_interval_sec, 90.0)

    def test_malformed_progress_config_hard_fails(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["progress"] = {"enabled": "yes"}
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_progress_config_rejects_boolean_timing_values(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["progress"] = {"initialDelaySec": True}
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_progress_config_rejects_nan_initial_delay(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["progress"] = {"initialDelaySec": float("nan")}
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_progress_config_rejects_infinite_interval(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["progress"] = {"intervalSec": float("inf")}
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_progress_config")

    def test_dry_run_droid_reasoning_effort(self):
        parsed = parser_api.parse_cli(
            [
                "dry-run",
                "droid",
                "safe",
                "--model",
                "reviewer",
                "--reasoning-effort",
                "high",
                "review",
            ]
        )
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.engine, "droid")
        self.assertEqual(parsed.payload.reasoning_effort, "high")

    def test_reasoning_effort_after_prompt_is_prompt_text(self):
        parsed = parser_api.parse_cli(["codex", "safe", "review", "--reasoning-effort", "high"])
        self.assertIsNone(parsed.payload.reasoning_effort)
        self.assertEqual(parsed.payload.prompt_parts, ["review", "--reasoning-effort", "high"])

    def test_reasoning_effort_requires_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["codex", "safe", "--reasoning-effort"])
        self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

    def test_reasoning_effort_rejects_option_looking_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(
                ["codex", "safe", "--reasoning-effort", "--prompt-file", "task.md"]
            )
        self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

    def test_reasoning_effort_rejects_help_token_as_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["codex", "safe", "--reasoning-effort", "--help"])
        self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["cursor", "agent", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_mode")

    def test_agent_help_discourages_shell_tail_launches(self):
        stdout = io.StringIO()
        code = describe_api.emit_agent_help(stdout)
        self.assertEqual(code, error_types.EXIT_OK)
        help_text = stdout.getvalue()
        self.assertIn("do not pipe delegate launches through tail", help_text)
        self.assertIn("delegate snapshot cursor-1", help_text)
        self.assertIn("set -o pipefail", help_text)

    def test_codex_direct_commands_parse(self):
        parsed = parser_api.parse_cli(["codex", "work", "implement"])
        self.assertEqual(parsed.subcommand, "codex")
        self.assertEqual(parsed.payload.engine, "codex")
        self.assertEqual(parsed.payload.mode, "work")

    def test_modeless_call_mode_parses(self):
        for engine in (
            "cursor",
            "codex",
            "kimi",
            "claude",
            "grok",
            "devin",
            "opencode",
            "pi",
            "omp",
        ):
            with self.subTest(engine=engine):
                parsed = parser_api.parse_cli([engine, "call", "summarize"])
                self.assertEqual(parsed.subcommand, engine)
                self.assertEqual(parsed.payload.engine, engine)
                self.assertEqual(parsed.payload.mode, "call")
                self.assertEqual(parsed.payload.prompt_parts, ["summarize"])

    def test_droid_call_mode_parses_with_model_option(self):
        parsed = parser_api.parse_cli(["droid", "call", "--model", "reviewer", "summarize"])
        self.assertEqual(parsed.payload.engine, "droid")
        self.assertEqual(parsed.payload.model, "reviewer")
        self.assertEqual(parsed.payload.mode, "call")

    def test_dry_run_call_mode_parses(self):
        parsed = parser_api.parse_cli(["dry-run", "codex", "call", "summarize"])
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.mode, "call")

    def test_run_input_json_call_rejects_global_isolation_in_pre_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "call",
                        "prompt": "hello",
                    }
                )
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                self.delegate.pre_read_run_json_for_config(str(task), None, "none")
            self.assertEqual(ctx.exception.error, "invalid_option_combination")
            self.assertIn("call mode does not use --isolation", ctx.exception.message)

    def test_call_read_only_flag_parses_in_tail(self):
        parsed = parser_api.parse_cli(["codex", "call", "--read-only", "score"])
        self.assertEqual(parsed.payload.mode, "call")
        self.assertTrue(parsed.payload.read_only)
        default = parser_api.parse_cli(["codex", "call", "score"])
        self.assertFalse(default.payload.read_only)

    def test_call_run_input_json_read_only_accepted_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:

            def parsed_for(raw):
                task = Path(tmp) / "task.json"
                task.write_text(json.dumps(raw), encoding="utf-8")
                return request_types.ParsedCommand(
                    "run",
                    global_options=request_types.GlobalOptions(json_mode=True),
                    payload=request_types.RunJsonOptions(str(task)),
                )

            ok = request_api.request_from_input_json(
                parsed_for({"engine": "codex", "mode": "call", "prompt": "s", "readOnly": True}),
                DEFAULT_CONFIG,
            )
            self.addCleanup(shutil.rmtree, ok.workspace, ignore_errors=True)
            self.assertTrue(ok.stdin_text.startswith("You are being called"))

            with self.assertRaises(error_types.DelegateError) as bad_type:
                request_api.request_from_input_json(
                    parsed_for(
                        {"engine": "codex", "mode": "call", "prompt": "s", "readOnly": "yes"}
                    ),
                    DEFAULT_CONFIG,
                )
            self.assertEqual(bad_type.exception.error, "invalid_read_only")

            with self.assertRaises(error_types.DelegateError) as bad_mode:
                request_api.request_from_input_json(
                    parsed_for(
                        {"engine": "codex", "mode": "work", "prompt": "s", "readOnly": True}
                    ),
                    DEFAULT_CONFIG,
                )
            self.assertEqual(bad_mode.exception.error, "invalid_option_combination")

    def test_call_cli_rejects_incompatible_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = (
                (["--cwd", tmp, "codex", "call", "hello"], "--cwd"),
                (["codex", "call", "--isolation", "none", "hello"], "--isolation"),
                (["--pass-through", "codex", "call", "hello"], "--pass-through"),
                (
                    ["--completion-report", "markdown", "codex", "call", "hello"],
                    "--completion-report",
                ),
                (["codex", "call", "--progress", "hello"], "--progress"),
                (["codex", "call", "--forbid-commit", "hello"], "--forbid-commit"),
                (["codex", "call", "--include-dirty", "hello"], "--include-dirty"),
            )
            for argv, message in cases:
                with self.subTest(argv=argv):
                    try:
                        parsed = parser_api.parse_cli(argv)
                    except error_types.DelegateError as ctx:
                        self.assertIn(message, ctx.message)
                        continue
                    with self.assertRaises(error_types.DelegateError) as ctx:
                        request_api.request_from_parsed(parsed, DEFAULT_CONFIG, io.StringIO(""))
                    self.assertIn(message, ctx.exception.message)

    def test_call_run_input_json_rejects_incompatible_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {"engine": "codex", "mode": "call", "prompt": "hello"}

            def parsed_for(raw, global_options=None):
                task = Path(tmp) / "task.json"
                task.write_text(json.dumps(raw), encoding="utf-8")
                return request_types.ParsedCommand(
                    "run",
                    global_options=global_options or request_types.GlobalOptions(json_mode=True),
                    payload=request_types.RunJsonOptions(str(task)),
                )

            cases = (
                (base, request_types.GlobalOptions(json_mode=True, cwd=tmp), "--cwd"),
                (
                    base,
                    request_types.GlobalOptions(json_mode=True, isolation="none"),
                    "--isolation",
                ),
                (
                    base,
                    request_types.GlobalOptions(json_mode=True, pass_through=True),
                    "--pass-through",
                ),
                (
                    base,
                    request_types.GlobalOptions(
                        json_mode=True,
                        completion_report=delegate_config.COMPLETION_REPORT_MODE_MARKDOWN,
                    ),
                    "--completion-report",
                ),
                ({**base, "cwd": tmp}, None, "must not include cwd"),
                ({**base, "isolation": "none"}, None, "must not include isolation"),
                ({**base, "progress": True}, None, "progress is not supported"),
                ({**base, "forbidCommit": True}, None, "forbidCommit requires work mode"),
                ({**base, "includeDirty": True}, None, "includeDirty requires work mode"),
            )
            for raw, global_options, message in cases:
                with self.subTest(raw=raw, global_options=global_options):
                    with self.assertRaises(error_types.DelegateError) as ctx:
                        request_api.request_from_input_json(
                            parsed_for(raw, global_options),
                            DEFAULT_CONFIG,
                        )
                    self.assertEqual(ctx.exception.error, "invalid_option_combination")
                    self.assertIn(message, ctx.exception.message)

    def test_cli_safe_isolation_none_warns_after_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parser_api.parse_cli(
                ["--cwd", tmp, "cursor", "safe", "--isolation", "none", "hello"]
            )
            request = request_api.request_from_parsed(
                parsed,
                DEFAULT_CONFIG,
                io.StringIO(""),
            )
            self.assertEqual(request.isolation_context.effective_isolation, "worktree")
            self.assertTrue(request.warnings)
            self.assertIn("isolation none", request.warnings[0])
            self.assertNotIn("--isolation", request.warnings[0])

    def test_claude_direct_commands_parse(self):
        parsed = parser_api.parse_cli(["claude", "safe", "--reasoning-effort", "high", "review"])
        self.assertEqual(parsed.subcommand, "claude")
        self.assertEqual(parsed.payload.engine, "claude")
        self.assertEqual(parsed.payload.mode, "safe")
        self.assertEqual(parsed.payload.reasoning_effort, "high")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

    def test_grok_direct_commands_parse(self):
        parsed = parser_api.parse_cli(["grok", "safe", "review"])
        self.assertEqual(parsed.subcommand, "grok")
        self.assertEqual(parsed.payload.engine, "grok")
        self.assertEqual(parsed.payload.mode, "safe")
        parsed = parser_api.parse_cli(
            ["grok", "safe", "--prompt-file", "task.md"],
        )
        self.assertEqual(parsed.payload.prompt_file, "task.md")

    def test_dry_run_grok_parses(self):
        parsed = parser_api.parse_cli(["dry-run", "grok", "work", "fix"])
        self.assertEqual(parsed.subcommand, "grok")
        self.assertTrue(parsed.payload.dry_run)
        parsed = parser_api.parse_cli(
            ["dry-run", "grok", "safe", "--prompt-file", "task.md"],
        )
        self.assertEqual(parsed.payload.prompt_file, "task.md")

    def test_devin_direct_commands_parse(self):
        parsed = parser_api.parse_cli(["devin", "safe", "review"])
        self.assertEqual(parsed.subcommand, "devin")
        self.assertEqual(parsed.payload.engine, "devin")
        self.assertEqual(parsed.payload.mode, "safe")
        parsed = parser_api.parse_cli(
            ["devin", "safe", "--prompt-file", "task.md"],
        )
        self.assertEqual(parsed.payload.prompt_file, "task.md")

    def test_missing_mode_lists_each_engine_supported_modes(self):
        for engine, expected in (
            ("devin", "devin requires mode: work, or call."),
            ("codex", "codex requires mode: safe, work, or call."),
        ):
            with self.subTest(engine=engine), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli([engine])
            self.assertEqual(ctx.exception.error, "missing_mode")
            self.assertEqual(ctx.exception.message, expected)

    def test_dry_run_devin_parses(self):
        parsed = parser_api.parse_cli(["dry-run", "devin", "work", "fix"])
        self.assertEqual(parsed.subcommand, "devin")
        self.assertTrue(parsed.payload.dry_run)
        parsed = parser_api.parse_cli(
            ["dry-run", "devin", "safe", "--prompt-file", "task.md"],
        )
        self.assertEqual(parsed.payload.prompt_file, "task.md")

    def test_opencode_direct_commands_parse(self):
        parsed = parser_api.parse_cli(
            ["opencode", "safe", "--model", "openai/gpt-5.5", "--agent", "reviewer", "review"]
        )
        self.assertEqual(parsed.subcommand, "opencode")
        self.assertEqual(parsed.payload.engine, "opencode")
        self.assertEqual(parsed.payload.mode, "safe")
        self.assertEqual(parsed.payload.model, "openai/gpt-5.5")
        self.assertEqual(parsed.payload.agent, "reviewer")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

        parsed = parser_api.parse_cli(["opencode", "work", "--agent", "builder", "fix"])
        self.assertEqual(parsed.payload.mode, "work")
        self.assertEqual(parsed.payload.agent, "builder")

        parsed = parser_api.parse_cli(["opencode", "call", "--agent", "judge", "score"])
        self.assertEqual(parsed.payload.mode, "call")
        self.assertEqual(parsed.payload.agent, "judge")

    def test_dry_run_opencode_parses(self):
        parsed = parser_api.parse_cli(["dry-run", "opencode", "work", "--agent", "builder", "fix"])
        self.assertEqual(parsed.subcommand, "opencode")
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.agent, "builder")
        parsed = parser_api.parse_cli(
            ["dry-run", "opencode", "safe", "--prompt-file", "task.md"],
        )
        self.assertEqual(parsed.payload.prompt_file, "task.md")

    def test_pi_direct_and_prompt_file_commands_parse(self):
        parsed = parser_api.parse_cli(
            ["pi", "safe", "--model", "reviewer", "--reasoning-effort", "high", "review"]
        )
        self.assertEqual(parsed.subcommand, "pi")
        self.assertEqual(parsed.payload.engine, "pi")
        self.assertEqual(parsed.payload.mode, "safe")
        self.assertEqual(parsed.payload.model, "reviewer")
        self.assertEqual(parsed.payload.reasoning_effort, "high")
        self.assertEqual(parsed.payload.prompt_parts, ["review"])

        parsed = parser_api.parse_cli(["dry-run", "pi", "work", "--prompt-file", "task.md"])
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.prompt_file, "task.md")

    def test_omp_direct_and_prompt_file_commands_parse(self):
        parsed = parser_api.parse_cli(
            ["omp", "safe", "--model", "reviewer", "--reasoning-effort", "high", "review"]
        )
        self.assertEqual(parsed.subcommand, "omp")
        self.assertEqual(parsed.payload.engine, "omp")
        self.assertEqual(parsed.payload.model, "reviewer")
        self.assertEqual(parsed.payload.reasoning_effort, "high")

        parsed = parser_api.parse_cli(["dry-run", "omp", "work", "--prompt-file", "task.md"])
        self.assertEqual(parsed.payload.engine, "omp")
        self.assertEqual(parsed.payload.prompt_file, "task.md")

    def test_agent_flag_rejected_for_non_opencode_engines(self):
        for engine in ("cursor", "codex", "kimi", "claude", "grok", "devin"):
            with self.subTest(engine=engine):
                with self.assertRaises(error_types.DelegateError) as ctx:
                    parser_api.parse_cli([engine, "safe", "--agent", "reviewer", "review"])
                self.assertEqual(ctx.exception.error, "unsupported_agent")

        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["droid", "safe", "--agent", "reviewer", "review"])
        self.assertEqual(ctx.exception.error, "unsupported_agent")

    def test_dry_run_codex_parses(self):
        parsed = parser_api.parse_cli(["dry-run", "codex", "safe", "review"])
        self.assertEqual(parsed.subcommand, "codex")
        self.assertTrue(parsed.payload.dry_run)

    def test_dry_run_claude_parses(self):
        parsed = parser_api.parse_cli(["dry-run", "claude", "work", "ship"])
        self.assertEqual(parsed.subcommand, "claude")
        self.assertTrue(parsed.payload.dry_run)

    def test_json_describe_shape(self):
        payload = describe_api.describe_payload(DEFAULT_CONFIG, "embedded-default")
        self.assertTrue(payload["ok"])
        self.assertIn("safe", payload["modes"])
        self.assertIn("work", payload["modes"])
        self.assertIn("cursor", payload["modeMapping"])
        self.assertIn("claude", payload["modeMapping"])
        self.assertIn("codex", payload["modeMapping"])
        self.assertIn("devin", payload["modeMapping"])
        self.assertIn("opencode", payload["modeMapping"])
        self.assertIn("claude", payload["engines"])
        self.assertIn("grok", payload["engines"])
        self.assertIn("devin", payload["engines"])
        self.assertIn("codex", payload["engines"])
        self.assertIn("policyProfiles", payload)
        self.assertIn("policyFieldSupport", payload)
        self.assertIn("effectivePolicy", payload)
        self.assertIn("claude", payload["effectivePolicy"])
        self.assertIn("codex", payload["effectivePolicy"])
        self.assertIn("passThrough", payload)

    def test_describe_claude_effective_policy_masks_global_external_sandbox_bypass(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["policy"]["profile"] = "external-sandbox"
        payload = describe_api.describe_payload(config, "test")
        self.assertFalse(payload["effectivePolicy"]["claude"]["work"]["bypassApprovalsAndSandbox"])
        self.assertNotIn("bypassPermissions", payload["modeMapping"]["claude"]["work"])

    def test_describe_claude_effective_policy_reports_harness_scoped_bypass(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["policy"]["harness"] = {"claude": {"work": {"bypassApprovalsAndSandbox": True}}}
        payload = describe_api.describe_payload(config, "test")
        self.assertTrue(payload["effectivePolicy"]["claude"]["work"]["bypassApprovalsAndSandbox"])
        self.assertIn("bypassPermissions", payload["modeMapping"]["claude"]["work"])

    def test_pass_through_parses_before_subcommand(self):
        parsed = parser_api.parse_cli(["--pass-through", "cursor", "safe", "hello"])
        self.assertTrue(parsed.global_options.pass_through)
        self.assertEqual(parsed.subcommand, "cursor")

    def test_json_pass_through_is_invalid(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["--json", "--pass-through", "cursor", "safe", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_launch_global_options_after_mode_are_accepted(self):
        cases = (
            (["--pass-through"], "pass_through", True),
            (["--completion-report", "none"], "completion_report", "none"),
            (["--no-completion-report"], "completion_report", "none"),
        )
        for option_tokens, attribute, expected in cases:
            with self.subTest(option=option_tokens):
                parsed = parser_api.parse_cli(["codex", "safe", *option_tokens, "hello"])
                self.assertEqual(getattr(parsed.global_options, attribute), expected)

    def test_dry_run_global_options_after_subcommand_are_accepted(self):
        cases = (
            (["--pass-through"], "pass_through", True),
            (["--completion-report", "none"], "completion_report", "none"),
            (["--no-completion-report"], "completion_report", "none"),
        )
        for option_tokens, attribute, expected in cases:
            with self.subTest(option=option_tokens):
                parsed = parser_api.parse_cli(["dry-run", *option_tokens, "codex", "safe", "hello"])
                self.assertEqual(getattr(parsed.global_options, attribute), expected)

    def test_completion_report_none_flag(self):
        parsed = parser_api.parse_cli(["--completion-report", "none", "cursor", "safe", "hello"])
        self.assertEqual(parsed.global_options.completion_report, "none")

    def test_no_completion_report_alias(self):
        parsed = parser_api.parse_cli(["--no-completion-report", "cursor", "safe", "hello"])
        self.assertEqual(parsed.global_options.completion_report, "none")

    def test_pass_through_skips_completion_report_injection(self):
        parsed = parser_api.parse_cli(["--pass-through", "cursor", "safe", "hello"])
        mode = request_api.resolve_completion_report_mode(parsed, DEFAULT_CONFIG)
        self.assertEqual(mode, "none")
        effective = request_api.effective_prompt("hello", completion_report_mode=mode)
        self.assertTrue(effective.startswith(runner.SKILL_REVIEW_PREFIX))
        self.assertNotIn("Delegate completion report requirement", effective)

    def test_effective_prompt_always_prepends_skill_review(self):
        effective = request_api.effective_prompt("hello", completion_report_mode="none")
        self.assertTrue(effective.startswith(runner.SKILL_REVIEW_PREFIX))
        self.assertTrue(effective.endswith("hello"))
        self.assertIn("mandatory for every Delegate Agent run", effective)

    def test_effective_prompt_does_not_duplicate_skill_review(self):
        original = runner.SKILL_REVIEW_PREFIX + "hello"
        effective = request_api.effective_prompt(original, completion_report_mode="none")
        self.assertEqual(effective, original)

    def test_prompt_file_is_not_mutated_for_completion_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "task.md"
            prompt_path.write_text("original prompt\n")
            parsed = parser_api.parse_cli(["cursor", "safe", "--prompt-file", str(prompt_path)])
            prompt = request_api.resolve_prompt(
                parsed.payload.prompt_parts, parsed.payload.prompt_file, io.StringIO()
            )
            effective = request_api.effective_prompt(
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
        self.assertIsNone(request_api.read_stdin_source(stdin, block=False))

    def test_snapshot_latest_and_handle_are_mutually_exclusive(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["snapshot", "--latest", "cursor", "cursor"])
        self.assertEqual(ctx.exception.error, "ambiguous_snapshot_target")

    def test_run_output_without_selector_defaults_to_completion_report(self):
        parsed = parser_api.parse_cli(["run-output", "cursor"])
        self.assertEqual(parsed.subcommand, "run-output")
        self.assertEqual(parsed.payload.handle, "cursor")
        self.assertTrue(parsed.payload.completion_report)
        self.assertTrue(parsed.payload.default)

    def test_runs_limit_must_be_positive(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["runs", "--limit", "0"])
        self.assertEqual(ctx.exception.error, "invalid_limit")

    def test_runs_prune_parses_age_override_and_dry_run(self):
        parsed = parser_api.parse_cli(
            [
                "--json",
                "--cwd",
                "/tmp/repo",
                "runs",
                "prune",
                "--older-than",
                "14",
                "--dry-run",
            ]
        )
        self.assertEqual(parsed.subcommand, "runs")
        self.assertEqual(parsed.payload.action, "prune")
        self.assertEqual(parsed.payload.older_than_days, 14)
        self.assertTrue(parsed.payload.dry_run)
        self.assertTrue(parsed.payload.json_mode)

    def test_runs_prune_rejects_negative_age(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["runs", "prune", "--older-than", "-1"])
        self.assertEqual(ctx.exception.error, "missing_option_value")

    def test_parse_required_positive_int_option(self):
        parsed, next_index = parser_api.parse_required_positive_int_option(
            ["--limit", "3"],
            0,
            option_label="runs --limit",
            missing_error="missing_limit",
            invalid_error="invalid_limit",
        )
        self.assertEqual(parsed, 3)
        self.assertEqual(next_index, 2)

    def test_parse_required_positive_int_option_errors(self):
        with self.assertRaises(error_types.DelegateError) as missing:
            parser_api.parse_required_positive_int_option(
                ["--limit"],
                0,
                option_label="runs --limit",
                missing_error="missing_limit",
                invalid_error="invalid_limit",
            )
        self.assertEqual(missing.exception.error, "missing_limit")

        with self.assertRaises(error_types.DelegateError) as invalid:
            parser_api.parse_required_positive_int_option(
                ["--limit", "nope"],
                0,
                option_label="runs --limit",
                missing_error="missing_limit",
                invalid_error="invalid_limit",
            )
        self.assertEqual(invalid.exception.error, "invalid_limit")

    def test_runs_active_and_recent_are_mutually_exclusive(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["runs", "--active", "--recent"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_run_output_raw_and_tail_are_mutually_exclusive(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["run-output", "cursor", "--stdout", "--raw", "--tail", "5"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_run_output_raw_and_max_chars_are_mutually_exclusive(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(
                ["run-output", "cursor", "--stdout", "--raw", "--max-chars", "1000"]
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("--max-chars", ctx.exception.message)

    def test_run_output_max_chars_must_be_positive_integer(self):
        with self.assertRaises(error_types.DelegateError) as missing:
            parser_api.parse_cli(["run-output", "cursor", "--stdout", "--max-chars"])
        self.assertEqual(missing.exception.error, "missing_max_chars")

        with self.assertRaises(error_types.DelegateError) as invalid:
            parser_api.parse_cli(["run-output", "cursor", "--stdout", "--max-chars", "nope"])
        self.assertEqual(invalid.exception.error, "invalid_max_chars")

        with self.assertRaises(error_types.DelegateError) as zero:
            parser_api.parse_cli(["run-output", "cursor", "--stdout", "--max-chars", "0"])
        self.assertEqual(zero.exception.error, "invalid_max_chars")

    def test_run_output_max_chars_is_parsed(self):
        parsed = parser_api.parse_cli(["run-output", "cursor", "--stdout", "--max-chars", "12000"])
        self.assertEqual(parsed.payload.max_chars, 12000)

    def test_run_output_tail_and_max_chars_require_stream_selector(self):
        for flag in ("--tail", "--max-chars"):
            with self.subTest(flag=flag), self.assertRaises(error_types.DelegateError) as ctx:
                parser_api.parse_cli(["run-output", "cursor", "--completion-report", flag, "5"])
            self.assertEqual(ctx.exception.error, "invalid_option_combination")
            self.assertIn("--stdout/--stderr", ctx.exception.message)

    def test_run_output_stdout_without_tail_defaults_to_bounded_tail(self):
        parsed = parser_api.parse_cli(["run-output", "cursor", "--stdout"])
        self.assertTrue(parsed.payload.stdout)
        self.assertEqual(parsed.payload.tail, run_output_commands.RUN_OUTPUT_DEFAULT_TAIL_LINES)

    def test_worktree_trailing_json_is_global(self):
        parsed = parser_api.parse_cli(["worktree", "list", "--json"])
        self.assertTrue(parsed.global_options.json_mode)

    def test_worktree_unknown_option_is_action_specific(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["worktree", "remove", "cursor-1", "--older-than", "7"])
        self.assertEqual(ctx.exception.error, "unknown_option")
        self.assertIn("worktree remove", ctx.exception.message)

    def test_worktree_show_latest_and_handle_are_mutually_exclusive(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["worktree", "show", "--latest", "cursor", "cursor-1"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_worktree_option_value_rejects_next_option_token(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["worktree", "list", "--harness", "--status", "present"])
        self.assertEqual(ctx.exception.error, "missing_option_value")
        self.assertIn("--harness requires a value", ctx.exception.message)

    def test_parse_kimi_safe(self):
        parsed = parser_api.parse_cli(["kimi", "safe", "review this"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertEqual(parsed.payload.engine, "kimi")
        self.assertEqual(parsed.payload.mode, "safe")
        self.assertEqual(parsed.payload.prompt_parts, ["review this"])

    def test_parse_kimi_work(self):
        parsed = parser_api.parse_cli(["kimi", "work", "fix this"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertEqual(parsed.payload.engine, "kimi")
        self.assertEqual(parsed.payload.mode, "work")
        self.assertEqual(parsed.payload.prompt_parts, ["fix this"])

    def test_parse_kimi_dry_run(self):
        parsed = parser_api.parse_cli(["dry-run", "kimi", "safe", "review"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.engine, "kimi")
        self.assertEqual(parsed.payload.mode, "safe")

    def test_parse_kimi_help(self):
        parsed = parser_api.parse_cli(["kimi", "--help"])
        self.assertEqual(parsed.subcommand, "help")
        self.assertEqual(parsed.help_topic, "kimi")

    def test_parse_kimi_prompt_file(self):
        parsed = parser_api.parse_cli(["kimi", "safe", "--prompt-file", "task.md"])
        self.assertEqual(parsed.subcommand, "kimi")
        self.assertEqual(parsed.payload.engine, "kimi")
        self.assertEqual(parsed.payload.mode, "safe")
        self.assertEqual(parsed.payload.prompt_file, "task.md")
        self.assertEqual(parsed.payload.prompt_parts, [])

    def test_parse_kimi_unknown_mode(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["kimi", "agent", "hello"])
        self.assertEqual(ctx.exception.error, "invalid_mode")

    def test_worktree_remove_keep_branch_and_force_are_mutually_exclusive(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["worktree", "remove", "cursor-1", "--keep-branch", "--force"])
        self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_worktree_prune_requires_filter_at_execution_time(self):
        parsed = parser_api.parse_cli(["worktree", "prune"])
        self.assertEqual(parsed.payload.action, "prune")
        self.assertFalse(parsed.payload.merged)
        self.assertIsNone(parsed.payload.older_than_days)

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

    def test_isolation_worktree_cursor_work_parses(self):
        parsed = parser_api.parse_cli(["--isolation", "worktree", "cursor", "work", "fix this"])
        self.assertEqual(parsed.global_options.isolation, "worktree")
        self.assertEqual(parsed.payload.engine, "cursor")
        self.assertEqual(parsed.payload.mode, "work")

    def test_isolation_none_codex_work_parses(self):
        parsed = parser_api.parse_cli(["--isolation", "none", "codex", "work", "implement"])
        self.assertEqual(parsed.global_options.isolation, "none")
        self.assertEqual(parsed.payload.engine, "codex")
        self.assertEqual(parsed.payload.mode, "work")

    def test_isolation_auto_droid_safe_parses(self):
        parsed = parser_api.parse_cli(
            ["--isolation", "auto", "droid", "safe", "--model", "minimax", "review"]
        )
        self.assertEqual(parsed.global_options.isolation, "auto")
        self.assertEqual(parsed.payload.engine, "droid")
        self.assertEqual(parsed.payload.mode, "safe")

    def test_isolation_unknown_value_raises(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["--isolation", "bananas", "cursor", "work", "fix"])
        self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_isolation_missing_value_raises(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["--isolation"])
        self.assertEqual(ctx.exception.error, "missing_isolation_value")

    def test_isolation_in_launch_tail_before_prompt_text_is_accepted(self):
        parsed = parser_api.parse_cli(["cursor", "work", "--isolation", "worktree", "fix"])
        self.assertEqual(parsed.global_options.isolation, "worktree")
        self.assertEqual(parsed.payload.prompt_parts, ["fix"])

    def test_isolation_after_inline_prompt_text_is_global(self):
        parsed = parser_api.parse_cli(["cursor", "work", "fix", "--isolation", "worktree"])
        self.assertEqual(parsed.global_options.isolation, "worktree")
        self.assertEqual(parsed.payload.prompt_parts, ["fix"])

    def test_isolation_launch_tail_rejects_unknown_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["codex", "work", "--isolation", "bananas", "fix"])
        self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_isolation_launch_tail_requires_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["codex", "work", "--isolation"])
        self.assertEqual(ctx.exception.error, "missing_isolation_value")

    def test_isolation_launch_tail_wins_over_global_value(self):
        parsed = parser_api.parse_cli(
            ["--isolation", "none", "codex", "work", "--isolation", "worktree", "fix"]
        )
        self.assertEqual(parsed.global_options.isolation, "worktree")

    def test_run_input_keys_contains_isolation(self):
        self.assertIn("isolation", request_api.RUN_INPUT_KEYS)
        self.assertIn("progress", request_api.RUN_INPUT_KEYS)
        self.assertIn("pure", request_api.RUN_INPUT_KEYS)
        self.assertIn("timeout", request_api.RUN_INPUT_KEYS)
        self.assertIn("continuityMode", request_api.RUN_INPUT_KEYS)

    def test_run_input_json_accepts_continuity_mode_and_defaults_to_fungible(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value, expected in (
                ("pinned", "pinned"),
                ("fungible", "fungible"),
                ("panel", "panel"),
                (None, "fungible"),
            ):
                with self.subTest(value=value):
                    task = Path(tmp) / f"task-{value or 'default'}.json"
                    payload = {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "hello",
                    }
                    if value is not None:
                        payload["continuityMode"] = value
                    task.write_text(json.dumps(payload))
                    parsed = request_types.ParsedCommand(
                        "run",
                        global_options=request_types.GlobalOptions(json_mode=True),
                        payload=request_types.RunJsonOptions(str(task)),
                    )
                    request = request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
                    self.assertEqual(request.continuity_mode, expected)

    def test_run_input_json_rejects_invalid_continuity_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "hello",
                        "continuityMode": None,
                    }
                )
            )
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "invalid_continuity_mode")

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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "unknown_input_key")
            self.assertIn("bogus", ctx.exception.message)

    def test_resolve_isolation_cli_wins_over_json_and_config(self):
        result = delegate_config.resolve_isolation(
            cli_value="none",
            input_json_value="worktree",
            loaded_config={"isolation": {"work": "auto"}},
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "none")

    def test_resolve_isolation_json_wins_over_config(self):
        result = delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value="worktree",
            loaded_config={"isolation": {"work": "auto"}},
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "worktree")

    def test_resolve_isolation_config_wins_over_embedded_default(self):
        result = delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value=None,
            loaded_config={"isolation": {"work": "worktree"}},
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "worktree")

    def test_resolve_isolation_embedded_default_safe_is_auto(self):
        result = delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value=None,
            loaded_config=None,
            engine="cursor",
            mode="safe",
        )
        self.assertEqual(result, "auto")

    def test_resolve_isolation_embedded_default_work_is_none(self):
        result = delegate_config.resolve_isolation(
            cli_value=None,
            input_json_value=None,
            loaded_config=None,
            engine="cursor",
            mode="work",
        )
        self.assertEqual(result, "none")

    def test_resolve_isolation_cli_auto_bypasses_config_worktree(self):
        result = delegate_config.resolve_isolation(
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            request = request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertEqual(request.engine, "claude")
            self.assertEqual(request.mode, "safe")
            self.assertEqual(request.model, "claude-sonnet-4-6")
            self.assertEqual(request.prompt_transport, transport_api.PROMPT_TRANSPORT_STDIN)
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["droid"]["models"] = {"minimax": "model-id"}
            request = request_api.request_from_input_json(parsed, cfg)
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["droid"]["models"] = {"minimax": "model-id"}
            cfg["progress"] = {"enabled": True, "initialDelaySec": 12, "intervalSec": 34}
            request = request_api.request_from_input_json(parsed, cfg)
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["droid"]["models"] = {"minimax": "model-id"}
            cfg["progress"] = {"enabled": True, "initialDelaySec": 30, "intervalSec": 60}
            request = request_api.request_from_input_json(parsed, cfg)
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            request = request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "invalid_forbid_commit")

    def test_run_input_json_claude_safe_none_normalizes_to_temporary_isolation(self):
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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            request = request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertEqual(request.isolation_context.effective_isolation, "worktree")
            self.assertIn("isolation none", request.warnings[0])
            self.assertNotIn("--isolation", request.warnings[0])

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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            request = request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertEqual(request.engine, "claude")
            self.assertEqual(request.mode, "work")
            self.assertEqual(request.model, "claude-opus-4-8")
            self.assertIn("claude-opus-4-8", request.argv)

    def test_call_run_input_json_pure_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "claude",
                        "mode": "call",
                        "prompt": "hello",
                        "pure": True,
                        "timeout": 12,
                    }
                )
            )
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            request = request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertTrue(request.pure)
            self.assertEqual(request.timeout, 12)

    def test_call_run_input_json_pure_rejects_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "claude",
                        "mode": "call",
                        "prompt": "hello",
                        "pure": True,
                    }
                )
            )
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(
                    json_mode=True,
                    group="g",
                ),
                payload=request_types.RunJsonOptions(str(task)),
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "pure_conflicts_group")

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
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            # Workspace config is deliberately ignored during config discovery.
            _ws, cfg, _src = self.delegate.pre_read_run_json_for_config(str(task), None)
            request = request_api.request_from_input_json(parsed, cfg)
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
        parsed = request_types.ParsedCommand(
            "run",
            global_options=request_types.GlobalOptions(json_mode=True, cwd=repo2.name),
            payload=request_types.RunJsonOptions(str(task)),
        )
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
        self.assertEqual(ctx.exception.error, "ambiguous_cwd")

    def test_isolation_after_subcommand_codex_work_is_accepted(self):
        parsed = parser_api.parse_cli(["codex", "work", "--isolation", "worktree", "fix"])
        self.assertEqual(parsed.global_options.isolation, "worktree")

    def test_isolation_after_subcommand_droid_work_is_accepted(self):
        parsed = parser_api.parse_cli(
            ["droid", "work", "--model", "minimax", "--isolation", "worktree", "fix"]
        )
        self.assertEqual(parsed.global_options.isolation, "worktree")

    def test_isolation_after_subcommand_dry_run_cursor_is_accepted(self):
        parsed = parser_api.parse_cli(
            ["dry-run", "--isolation", "worktree", "cursor", "work", "fix"]
        )
        self.assertEqual(parsed.global_options.isolation, "worktree")

    def test_isolation_after_dry_run_engine_mode_is_accepted(self):
        parsed = parser_api.parse_cli(
            ["dry-run", "codex", "work", "--isolation", "worktree", "fix"]
        )
        self.assertEqual(parsed.global_options.isolation, "worktree")

    def test_isolation_work_unknown_value_raises(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["--isolation", "bananas", "codex", "work", "fix"])
        self.assertEqual(ctx.exception.error, "invalid_isolation")

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
            with self.assertRaises(error_types.DelegateError) as ctx:
                self.delegate.pre_read_run_json_for_config(str(task), None)
            self.assertEqual(ctx.exception.error, "invalid_isolation")

    def test_run_input_json_ignores_workspace_config_before_request(self):
        """JSON cwd cannot select isolation through repository config."""
        with tempfile.TemporaryDirectory() as global_tmp:
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
                with mock.patch.dict(
                    os.environ,
                    {"DELEGATE_CONFIG": str(global_config)},
                    clear=False,
                ):
                    _ws, cfg, _src = self.delegate.pre_read_run_json_for_config(str(task), None)
                self.assertEqual(
                    cfg["isolation"]["work"],
                    "none",
                    "Repository config must not override explicitly trusted config",
                )

                result = delegate_config.resolve_isolation(
                    cli_value=None,
                    input_json_value=None,
                    loaded_config=cfg,
                    engine="cursor",
                    mode="work",
                )
                self.assertEqual(result, "none")

                result = delegate_config.resolve_isolation(
                    cli_value="auto",
                    input_json_value=None,
                    loaded_config=cfg,
                    engine="cursor",
                    mode="work",
                )
                self.assertEqual(result, "auto")

                result = delegate_config.resolve_isolation(
                    cli_value=None,
                    input_json_value="none",
                    loaded_config=cfg,
                    engine="cursor",
                    mode="work",
                )
                self.assertEqual(result, "none")

                parsed = request_types.ParsedCommand(
                    "run",
                    global_options=request_types.GlobalOptions(json_mode=False),
                    payload=request_types.RunJsonOptions(str(task)),
                )
                request = request_api.request_from_input_json(parsed, cfg)
                self.assertEqual(request.engine, "cursor")
                self.assertEqual(request.mode, "work")

    def test_main_run_input_json_ignores_workspace_config(self):
        """End-to-end main(): repository config cannot select executable or isolation."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
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
            with mock.patch.dict(os.environ, {"HOME": home, "PATH": ""}, clear=True):
                code = self.delegate.main(
                    ["run", "--input-json", str(task)],
                    stdout=stdout,
                    stderr=stderr,
                )
            self.assertEqual(code, error_types.EXIT_MISSING_BINARY)
            err = stderr.getvalue()
            self.assertIn("missing_binary", err)
            self.assertNotIn("worktree_requires_git", err)
