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

    def test_json_describe_shape(self):
        payload = self.delegate.describe_payload(self.delegate.DEFAULT_CONFIG, "embedded-default")
        self.assertTrue(payload["ok"])
        self.assertIn("safe", payload["modes"])
        self.assertIn("work", payload["modes"])
        self.assertIn("cursor", payload["modeMapping"])
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
        self.assertNotIn("Delegate completion report requirement", effective)

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
