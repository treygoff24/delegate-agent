"""End-to-end CLI behavior tests for per-subcommand ``--help``.

These tests drive the CLI through ``main()`` (capturing stdout/stderr and the
exit code) and through ``parse_cli`` (for routing/boundary assertions). They are
distinct from ``tests/test_command_help.py``, which unit-tests the pure
``command_help`` registry/renderers. The focus here is the wiring in ``cli.py``:
help detection at each parser's decision points, the ``help`` positional
subcommand, JSON help envelopes, destructive-safety, and prompt-boundary
correctness.
"""

import importlib
import io
import json
import os
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


# Top-level commands that must support `<cmd> --help`.
TOP_LEVEL_COMMANDS = (
    "cursor",
    "claude",
    "devin",
    "opencode",
    "codex",
    "droid",
    "dry-run",
    "profiles",
    "run",
    "snapshot",
    "runs",
    "run-output",
    "wait",
    "cancel",
    "worktree",
    "workflow",
    "models",
    "describe",
    "agent-help",
    "help",
)


class HelpCliTestBase(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        config_path = Path(config_dir.name) / "config.json"
        config_path.write_text(json.dumps(self.delegate.DEFAULT_CONFIG), encoding="utf-8")
        self._config_env = {"DELEGATE_CONFIG": str(config_path)}

    def run_main(self, argv):
        """Drive main() and return (exit_code, stdout_text, stderr_text)."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self._config_env, clear=False):
            code = self.delegate.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()


class TopLevelHelpTests(HelpCliTestBase):
    """`<cmd> --help` exits 0 and prints non-empty help naming the command."""

    def test_every_top_level_command_help(self):
        for command in TOP_LEVEL_COMMANDS:
            with self.subTest(command=command):
                code, out, _err = self.run_main([command, "--help"])
                self.assertEqual(code, self.delegate.EXIT_OK)
                self.assertTrue(out.strip(), f"{command} --help printed nothing")
                self.assertIn(command, out, f"{command} --help missing command name")


class MultiLevelHelpTests(HelpCliTestBase):
    """Multi-level help paths exit 0 with focused, non-empty help."""

    CASES = (
        (["cursor", "safe", "--help"], "cursor"),
        (["claude", "safe", "--help"], "claude"),
        (["droid", "x", "--help"], "droid"),
        (["droid", "x", "safe", "--help"], "droid"),
        (["dry-run", "cursor", "--help"], "dry-run"),
        (["dry-run", "droid", "--help"], "dry-run"),
        (["worktree", "remove", "--help"], "worktree remove"),
        (["worktree", "prune", "--help"], "worktree prune"),
        (["worktree", "list", "--help"], "worktree list"),
        (["worktree", "show", "--help"], "worktree show"),
        (["worktree", "gc", "--help"], "worktree gc"),
        (["workflow", "wait", "--help"], "workflow wait"),
        (["workflow", "result", "--help"], "workflow result"),
    )

    def test_multi_level_help_paths(self):
        for argv, topic in self.CASES:
            with self.subTest(argv=argv):
                code, out, _err = self.run_main(argv)
                self.assertEqual(code, self.delegate.EXIT_OK)
                self.assertTrue(out.strip(), f"{argv} printed nothing")
                self.assertIn(topic, out, f"{argv} help missing topic {topic!r}")

    def test_workflow_wait_and_result_focused_contracts(self):
        code, out, err = self.run_main(["workflow", "wait", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK, err)
        self.assertIn("workflow wait [<wfId>]", out)
        self.assertIn("resolutionKind", out)

        code, out, err = self.run_main(["workflow", "result", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK, err)
        self.assertIn("workflow result [<wfId>] [--field KEY]", out)
        self.assertIn("--field KEY", out)


class FocusedCallHelpTests(HelpCliTestBase):
    """Call help reflects call's narrower option contract after real CLI routing."""

    CASES = (
        (["cursor", "call", "--help"], "cursor call"),
        (["codex", "call", "--help"], "codex call"),
        (["opencode", "call", "--help"], "opencode call"),
        (["droid", "call", "--help"], "droid call"),
        (["droid", "reviewer", "call", "--help"], "droid call"),
        (["dry-run", "cursor", "call", "--help"], "dry-run call"),
        (["dry-run", "codex", "call", "--help"], "dry-run call"),
        (["dry-run", "droid", "reviewer", "call", "--help"], "dry-run call"),
    )

    def test_call_help_routes_to_focused_mode_spec(self):
        for argv, topic in self.CASES:
            with self.subTest(argv=argv):
                parsed = self.delegate.parse_cli(argv)
                self.assertEqual(parsed.help_topic, topic)
                code, out, err = self.run_main(argv)
                self.assertEqual(code, self.delegate.EXIT_OK, err)
                self.assertIn(f"delegate {topic} --", out)
                for option in (
                    "--isolation",
                    "--progress",
                    "--no-progress",
                    "--forbid-commit",
                    "--include-dirty",
                    "--pass-through",
                    "--completion-report",
                    "--no-completion-report",
                ):
                    self.assertNotIn(option, out)
                self.assertIn("--cwd PATH", out)
                self.assertIn("only with --group", out)
                self.assertIn("never changes the child's throwaway execution cwd", out)

    def test_json_call_help_exposes_same_filtered_contract(self):
        code, out, err = self.run_main(["--json", "codex", "call", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK, err)
        payload = json.loads(out)
        self.assertEqual(payload["command"], "codex call")
        self.assertEqual(
            {option["flag"] for option in payload["options"]}
            & {"--progress", "--no-progress", "--forbid-commit", "--include-dirty"},
            set(),
        )
        self.assertIn("--cwd", {option["flag"] for option in payload["globalOptions"]})


class DashHAliasTests(HelpCliTestBase):
    """`-h` is an alias for `--help` everywhere."""

    CASES = (
        (["cursor", "-h"], "cursor"),
        (["claude", "-h"], "claude"),
        (["worktree", "remove", "-h"], "worktree remove"),
        (["droid", "-h"], "droid"),
        (["dry-run", "-h"], "dry-run"),
    )

    def test_dash_h_alias(self):
        for argv, topic in self.CASES:
            with self.subTest(argv=argv):
                code, out, _err = self.run_main(argv)
                self.assertEqual(code, self.delegate.EXIT_OK)
                self.assertIn(topic, out, f"{argv} help missing topic {topic!r}")


class JsonCommandHelpTests(HelpCliTestBase):
    """`--json <cmd> --help` returns valid JSON with ok=true and command==topic."""

    CASES = (
        (["--json", "cursor", "--help"], "cursor"),
        (["--json", "claude", "--help"], "claude"),
        (["--json", "opencode", "--help"], "opencode"),
        (["--json", "codex", "--help"], "codex"),
        (["--json", "droid", "--help"], "droid"),
        (["--json", "dry-run", "--help"], "dry-run"),
        (["--json", "run", "--help"], "run"),
        (["--json", "snapshot", "--help"], "snapshot"),
        (["--json", "runs", "--help"], "runs"),
        (["--json", "run-output", "--help"], "run-output"),
        (["--json", "wait", "--help"], "wait"),
        (["--json", "cancel", "--help"], "cancel"),
        (["--json", "worktree", "--help"], "worktree"),
        (["--json", "models", "--help"], "models"),
        (["--json", "describe", "--help"], "describe"),
        (["--json", "agent-help", "--help"], "agent-help"),
        (["--json", "help", "--help"], "help"),
        (["--json", "worktree", "remove", "--help"], "worktree remove"),
        (["--json", "cursor", "safe", "--help"], "cursor"),
        (["--json", "claude", "safe", "--help"], "claude"),
        (["--json", "opencode", "safe", "--help"], "opencode"),
    )

    def test_json_command_help(self):
        for argv, topic in self.CASES:
            with self.subTest(argv=argv):
                code, out, _err = self.run_main(argv)
                self.assertEqual(code, self.delegate.EXIT_OK)
                payload = json.loads(out)
                self.assertIs(payload["ok"], True)
                self.assertEqual(payload["command"], topic)


class HelpSubcommandTests(HelpCliTestBase):
    """The `help` positional subcommand: overview, focused, and JSON index."""

    def test_help_overview(self):
        code, out, _err = self.run_main(["help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertTrue(out.strip())
        # Overview enumerates the worktree actions on their own lines (I1).
        self.assertIn("worktree prune", out)

    def test_help_focused_topic(self):
        code, out, _err = self.run_main(["help", "worktree", "remove"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("worktree remove", out)

    def test_json_help_index(self):
        code, out, _err = self.run_main(["--json", "help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        payload = json.loads(out)
        self.assertIn("ok", payload)
        self.assertIs(payload["ok"], True)
        self.assertIn("commands", payload)
        self.assertIsInstance(payload["commands"], list)
        self.assertTrue(payload["commands"])

    def test_models_and_describe_help_include_summary_discovery_flags(self):
        for command in ("models", "describe"):
            with self.subTest(command=command):
                code, out, _err = self.run_main([command, "--help"])
                self.assertEqual(code, self.delegate.EXIT_OK)
                self.assertIn("--summary", out)
                self.assertNotIn("--redacted", out)
                self.assertIn(f"delegate --json {command} --summary", out)

    def test_engine_help_includes_progress_and_commit_policy_launch_options(self):
        for command in (
            "cursor",
            "claude",
            "devin",
            "opencode",
            "codex",
            "droid",
            "kimi",
            "dry-run",
        ):
            with self.subTest(command=command):
                code, out, _err = self.run_main([command, "--help"])
                self.assertEqual(code, self.delegate.EXIT_OK)
                self.assertIn("--progress", out)
                self.assertIn("stderr", out)
                self.assertIn("--forbid-commit", out)
                self.assertIn("persistent worktree", out)

    def test_describe_summary_lists_launch_options(self):
        code, out, _err = self.run_main(["--json", "describe", "--summary"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        payload = json.loads(out)
        self.assertIn("--progress", payload["launchOptions"])
        self.assertIn("--no-progress", payload["launchOptions"])
        self.assertIn("--forbid-commit", payload["launchOptions"])

    def test_describe_summary_text_renders_without_full_payload_keys(self):
        code, out, _err = self.run_main(["describe", "--summary"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("delegate", out)
        self.assertIn("launch options:", out)
        self.assertIn("recommended discovery:", out)

    def test_agent_help_includes_canonical_review_before_commit_example(self):
        code, out, err = self.run_main(["agent-help"])
        self.assertEqual(code, self.delegate.EXIT_OK, err)
        self.assertIn(
            "delegate codex work --isolation worktree --forbid-commit --prompt-file task.md",
            out,
        )


class JsonPositionIndependenceTests(HelpCliTestBase):
    """`--json` anywhere in a help invocation yields the same focused help (D3)."""

    def test_json_position_independent_for_worktree(self):
        variants = (
            ["help", "--json", "worktree"],
            ["help", "worktree", "--json"],
            ["--json", "help", "worktree"],
        )
        payloads = []
        for argv in variants:
            with self.subTest(argv=argv):
                code, out, _err = self.run_main(argv)
                self.assertEqual(code, self.delegate.EXIT_OK)
                payload = json.loads(out)
                self.assertIs(payload["ok"], True)
                self.assertEqual(payload["command"], "worktree")
                payloads.append(payload)
        # All three must be byte-for-byte identical help for "worktree".
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[1], payloads[2])


class HelpShortCircuitsValidationTests(HelpCliTestBase):
    """Help wins before required-arg validation (no alias/mode/--input-json)."""

    def test_droid_help_without_alias(self):
        code, out, _err = self.run_main(["droid", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("droid", out)

    def test_cursor_help_without_mode(self):
        code, out, _err = self.run_main(["cursor", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("cursor", out)

    def test_run_help_without_input_json(self):
        code, out, _err = self.run_main(["run", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("run", out)


class DestructiveSafetyTests(HelpCliTestBase):
    """A help token in a worktree action prints help and removes nothing (M2)."""

    def test_worktree_remove_with_help_builds_no_removal(self):
        parsed = self.delegate.parse_cli(["worktree", "remove", "cursor", "--help"])
        self.assertEqual(parsed.subcommand, "help")
        self.assertEqual(parsed.help_topic, "worktree remove")
        # No removal path is constructed: the worktree action is never set, so
        # emit_worktree's removal branch is unreachable.
        self.assertIsNone(parsed.worktree)

    def test_worktree_remove_with_help_main_prints_help(self):
        code, out, _err = self.run_main(["worktree", "remove", "cursor", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("worktree remove", out)


class PromptBoundaryTests(HelpCliTestBase):
    """`cursor work explain --help` is a RUN; --help is captured prompt text (M4)."""

    def test_help_after_prompt_positional_is_prompt_text(self):
        parsed = self.delegate.parse_cli(["cursor", "work", "explain", "--help"])
        # This is a run, NOT help. A naive "grep argv for --help" impl fails here.
        self.assertEqual(parsed.subcommand, "cursor")
        self.assertIsNone(parsed.help_topic)
        self.assertEqual(parsed.launch.engine, "cursor")
        self.assertEqual(parsed.launch.mode, "work")
        self.assertEqual(parsed.launch.prompt_parts, ["explain", "--help"])


class RegressionGuardTests(HelpCliTestBase):
    """Help wiring must not disturb existing parse outcomes (I4/I5)."""

    def test_prompt_file_still_parses_as_run(self):
        parsed = self.delegate.parse_cli(["cursor", "safe", "--prompt-file", "task.md"])
        self.assertEqual(parsed.subcommand, "cursor")
        self.assertIsNone(parsed.help_topic)
        self.assertEqual(parsed.launch.prompt_file, "task.md")
        self.assertEqual(parsed.launch.prompt_parts, [])

    def test_trailing_json_after_prompt_is_rejected(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["dry-run", "droid", "minimax", "work", "hello", "--json"])
        self.assertEqual(ctx.exception.error, "misplaced_global_option")
        self.assertIn("delegate --json", ctx.exception.message)

    def test_trailing_json_is_accepted_for_inspection_commands(self):
        cases = (
            ["describe", "--json"],
            ["models", "--json"],
            ["capabilities", "--json"],
            ["snapshot", "cursor", "--json"],
            ["runs", "--stale", "--json"],
            ["run-output", "cursor", "--completion-report", "--json"],
            ["wait", "cursor", "--json"],
            ["cancel", "cursor", "--json"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                parsed = self.delegate.parse_cli(argv)
                self.assertTrue(parsed.global_options.json_mode)


class RunOutputHelpTests(HelpCliTestBase):
    """run-output help documents bounded output and raw incompatibilities."""

    def test_run_output_help_mentions_max_chars_and_raw_limits(self):
        code, out, _err = self.run_main(["run-output", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("--max-chars", out)
        self.assertIn("60000", out)
        self.assertIn("--raw", out)
        self.assertIn("--tail", out)
        self.assertIn("incompatible", out.lower())
        self.assertIn("implies --stdout", out)

    def test_run_output_json_help_documents_max_chars(self):
        code, out, _err = self.run_main(["--json", "run-output", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        payload = json.loads(out)
        flags = {opt["flag"] for opt in payload["options"]}
        self.assertIn("--max-chars", flags)
        raw_option = next(opt for opt in payload["options"] if opt["flag"] == "--raw")
        self.assertIn("--max-chars", raw_option["description"])


class UnknownTopicTests(HelpCliTestBase):
    """Unknown help topics error cleanly with exit 2 (m3)."""

    def test_unknown_topic_text(self):
        code, _out, err = self.run_main(["help", "bogus"])
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertIn("unknown_help_topic", err)

    def test_unknown_topic_json_envelope(self):
        code, out, _err = self.run_main(["--json", "help", "bogus"])
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        payload = json.loads(out)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "unknown_help_topic")


class SparseArgsNoIndexErrorTests(HelpCliTestBase):
    """Sparse args raise a clean DelegateError, never a stray exception."""

    CASES = (
        ["droid"],
        ["droid", "grok"],
        ["dry-run", "droid"],
        ["worktree"],
        ["run-output"],
        ["wait"],
        ["cancel"],
    )

    def test_sparse_args_raise_delegate_error(self):
        for argv in self.CASES:
            with self.subTest(argv=argv), self.assertRaises(self.delegate.DelegateError):
                self.delegate.parse_cli(argv)


class ErgonomicsParserTests(HelpCliTestBase):
    def test_list_alias_maps_to_runs(self):
        parsed = self.delegate.parse_cli(["list", "--recent"])
        self.assertEqual(parsed.subcommand, "runs")
        self.assertIsNotNone(parsed.runs)

    def test_unknown_subcommand_suggests_known_forms(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["kill", "codex-1"])
        self.assertEqual(ctx.exception.error, "unknown_subcommand")
        self.assertIn("delegate cancel", ctx.exception.message)

        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["droid-glm", "safe", "review"])
        self.assertIn("delegate droid glm", ctx.exception.message)

    def test_unknown_run_output_option_suggests_raw_for_full(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["run-output", "codex-1", "--full"])
        self.assertEqual(ctx.exception.error, "unknown_option")
        self.assertIn("--raw", ctx.exception.message)

    def test_run_output_latest_parser_path(self):
        parsed = self.delegate.parse_cli(["run-output", "--latest", "droid:glm"])
        self.assertEqual(parsed.subcommand, "run-output")
        self.assertIsNone(parsed.run_output.handle)
        self.assertEqual(parsed.run_output.latest_harness, "droid:glm")

    def test_wait_latest_parser_path(self):
        parsed = self.delegate.parse_cli(["wait", "codex-1", "--latest", "droid:glm"])
        self.assertEqual(parsed.wait_command.handles, ("codex-1",))
        self.assertEqual(parsed.wait_command.latest_harness, "droid:glm")

    def test_ambiguous_prompt_source_includes_corrected_command(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(["cursor", "work", "prompt text", "--prompt-file", "task.md"])
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")
        self.assertIn("Corrected command:", ctx.exception.message)
        self.assertIn("--prompt-file task.md", ctx.exception.message)

    def test_forbid_commit_implies_worktree_when_isolation_omitted(self):
        parsed = self.delegate.parse_cli(["cursor", "work", "--forbid-commit", "do it"])
        self.assertEqual(parsed.global_options.isolation, "worktree")
        self.assertTrue(parsed.launch.forbid_commit_implied_isolation)

    def test_forbid_commit_with_explicit_none_errors_with_corrected_command(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.parse_cli(
                ["--isolation", "none", "cursor", "work", "--forbid-commit", "do it"]
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("Corrected command:", ctx.exception.message)
        self.assertIn("--isolation worktree", ctx.exception.message)


class KimiHelpTests(HelpCliTestBase):
    """Kimi harness surfaces in discovery commands."""

    def test_kimi_help_matches_no_yolo_argv_policy(self):
        code, out, _err = self.run_main(["kimi", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertNotIn("--yolo by default", out)
        self.assertIn("does not emit --yolo", out)

    def test_kimi_in_describe_engines(self):
        code, out, _err = self.run_main(["--json", "describe"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        payload = json.loads(out)
        self.assertIn("kimi", payload["engines"])

    def test_kimi_in_describe_mode_mapping(self):
        code, out, _err = self.run_main(["--json", "describe"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        payload = json.loads(out)
        self.assertIn("kimi", payload["modeMapping"])
        self.assertIn("safe", payload["modeMapping"]["kimi"])
        self.assertIn("work", payload["modeMapping"]["kimi"])

    def test_kimi_in_agent_help(self):
        code, out, _err = self.run_main(["agent-help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("kimi", out)

    def test_kimi_in_models(self):
        code, out, _err = self.run_main(["--json", "models"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        payload = json.loads(out)
        self.assertIn("kimi", payload)
        self.assertEqual(payload["kimi"]["binary"], self.delegate.DEFAULT_CONFIG["kimi"]["binary"])

    def test_safe_workspace_sync_note_is_shared_across_help_surfaces(self):
        note = self.delegate.command_help.SAFE_WORKSPACE_SYNC_NOTE
        for command in ("cursor", "kimi", "codex", "claude", "opencode", "droid"):
            with self.subTest(command=command):
                code, out, _err = self.run_main([command, "--help"])
                self.assertEqual(code, self.delegate.EXIT_OK)
                self.assertIn(note, out)
                if command == "droid":
                    self.assertIn(
                        "Positional MODEL_ALIAS is alias-only (strict); --model is alias-or-id",
                        out,
                    )

        code, out, _err = self.run_main(["--json", "describe"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        payload = json.loads(out)
        for command in ("cursor", "kimi", "codex", "claude", "opencode", "droid"):
            with self.subTest(describe=command):
                self.assertIn(note, payload["modeMapping"][command]["safeNotes"])

        code, out, _err = self.run_main(["devin", "--help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn("Devin safe mode is rejected during preflight", out)
        self.assertNotIn(note, out)

        code, out, _err = self.run_main(["agent-help"])
        self.assertEqual(code, self.delegate.EXIT_OK)
        self.assertIn(note, out)
        self.assertIn(
            "Positional MODEL_ALIAS is alias-only (strict); --model is alias-or-id",
            out,
        )


if __name__ == "__main__":
    unittest.main()
