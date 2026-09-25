"""CLI ergonomics and errors that carry their rule (dlg-qd1).

Each case is a command an agent actually typed. It either got a bare
"does not support option" or no compact view existed. The tests pin the
new behavior: a compact count view, and errors that name the rule and the
command to use instead.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from delegate_agent import run_registry
from delegate_agent.cli_parser import parse_cli
from delegate_agent.errors import DelegateError
from tests.delegate_commands_test_base import CommandTestBase


class RunsSummaryTests(CommandTestBase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="delegate-runs-summary-")
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def _run(self, harness: str, status: str, group: str | None = None) -> None:
        metadata = {"mode": "work"}
        if group is not None:
            metadata["group"] = group
        run_id, _alias = run_registry.register_run(
            self.registry_root, harness=harness, metadata=metadata
        )
        state = {"status": status, "lastActivityAt": "2026-09-24T12:00:00Z"}
        if status == "running":
            state["pid"] = os.getpid()
        run_registry.write_json_atomic(
            run_registry.run_directory(self.registry_root, run_id) / run_registry.STATE_FILE,
            state,
        )

    def test_summary_counts_every_match_by_status_harness_and_group(self):
        self._run("codex", "running", group="wave")
        self._run("codex", "succeeded", group="wave")
        self._run("cursor", "failed", group="wave")
        self._run("cursor", "succeeded")

        code, stdout, stderr = self.run_main(
            ["--json", "--cwd", str(self.workspace), "runs", "--summary"]
        )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema"], "delegate.runs.summary.v1")
        self.assertNotIn("runs", payload)
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["byStatus"], {"succeeded": 2, "failed": 1, "running": 1})
        self.assertEqual(payload["byHarness"], {"codex": 2, "cursor": 2})
        self.assertEqual(payload["byGroup"], {"wave": 3, "(none)": 1})

        code, stdout, _stderr = self.run_main(
            ["--cwd", str(self.workspace), "runs", "--group", "wave", "--summary"]
        )
        self.assertEqual(code, 0)
        self.assertIn("total: 3", stdout)
        self.assertIn("harness: codex 2, cursor 1", stdout)

    def test_summary_refuses_row_shaping_options(self):
        for option in (["--limit", "3"], ["--structural"]):
            with self.subTest(option=option[0]):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(["runs", "--summary", *option])
                self.assertEqual(caught.exception.error, "invalid_option_combination")
                self.assertIn("counts with no rows", caught.exception.message)


class OneRunRequestHintTests(CommandTestBase):
    def test_runs_and_ps_point_handle_options_at_snapshot_and_run_output(self):
        for argv in (
            ["ps", "--id", "codex-3"],
            ["runs", "--alias=codex-3"],
            ["runs", "codex-3"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(argv)
                self.assertEqual(caught.exception.error, "unknown_option")
                self.assertIn("delegate snapshot HANDLE", caught.exception.message)
                self.assertIn("delegate run-output HANDLE", caught.exception.message)

    def test_other_unknown_runs_options_keep_the_plain_message(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["runs", "--verbose"])
        self.assertEqual(caught.exception.message, "runs does not support option: --verbose")

    def test_show_and_status_subcommands_suggest_the_per_run_commands(self):
        for word in ("show", "status", "logs"):
            with self.subTest(word=word):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli([word, "codex-3"])
                self.assertEqual(caught.exception.error, "unknown_subcommand")
                self.assertIn("delegate run-output HANDLE", caught.exception.message)


class MailBodyOptionTests(CommandTestBase):
    def test_body_option_names_the_positional_rule(self):
        for option in ("--body", "--message=hi", "-m"):
            with self.subTest(option=option):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(["mail", "send", "--to", "coordinator", option, "hi"])
                message = caught.exception.message
                self.assertEqual(caught.exception.error, "unknown_option")
                self.assertIn("the body is the positional BODY, --file FILE, or '-'", message)
                self.assertIn("delegate mail send --to coordinator", message)

    def test_dash_reads_stdin_as_the_help_and_error_promise(self):
        parsed = parse_cli(["mail", "send", "--to", "coordinator", "-"])
        self.assertEqual(parsed.payload.body, "-")


class FollowupInheritedRouteTests(CommandTestBase):
    def test_route_options_say_the_route_is_inherited_and_point_at_resume(self):
        for argv in (
            ["followup", "--model", "gpt-5.5", "codex-1", "go on"],
            ["followup", "--progress", "codex-1", "go on"],
            ["followup", "--reasoning-effort=high", "codex-1", "go on"],
        ):
            with self.subTest(option=argv[1]):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(argv)
                message = caught.exception.message
                self.assertEqual(caught.exception.error, "unknown_option")
                self.assertIn("inherits the source run's route", message)
                option = argv[1].split("=", 1)[0]
                self.assertIn(f"delegate resume {option} ... HANDLE", message)

    def test_non_route_unknown_option_keeps_the_plain_message(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--bogus", "codex-1", "go on"])
        self.assertNotIn("inherits", caught.exception.message)
