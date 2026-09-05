"""Public discovery and advisory recovery contracts; no provider execution."""

import io
import json
from unittest import mock

from tests.test_delegate_help_cli import HelpCliTestBase


class DiscoveryContractsTests(HelpCliTestBase):
    def test_overview_is_a_compact_public_index(self):
        code, out, err = self.run_main(["--json", "describe", "--overview"])
        self.assertEqual(code, 0, err)
        overview = json.loads(out)
        self.assertTrue(overview["overview"])
        self.assertEqual(overview["modes"], ["safe", "work", "call"])
        self.assertEqual(len(overview["engines"]), 10)
        self.assertIn("version", overview)
        for excluded in ("configSource", "configResolution", "profiles", "launchOptions"):
            self.assertNotIn(excluded, overview)
        specs = self.delegate.command_help.COMMAND_SPECS
        self.assertEqual(
            {row["command"] for row in overview["commands"]},
            {name for name, spec in specs.items() if not spec.internal},
        )
        for row in overview["commands"]:
            self.assertEqual(set(row), {"command", "helpTopic"})
            self.assertEqual(row["helpTopic"], row["command"])
        self.assertNotIn("_supervise", out)
        self.assertIn("delegate --json help <command>", overview["recommendedDiscovery"])
        summary_code, summary, _ = self.run_main(["--json", "describe", "--summary"])
        self.assertEqual(summary_code, 0)
        self.assertLess(len(out.encode()), len(summary.encode()))

    def test_overview_text_and_flag_boundaries(self):
        code, out, err = self.run_main(["describe", "--overview"])
        self.assertEqual(code, 0, err)
        self.assertIn("workflow reject", out)
        self.assertIn("delegate help", out)
        for argv in (["describe", "--overview", "--summary"], ["models", "--overview"]):
            with self.subTest(argv=argv):
                code, _, _ = self.run_main(argv)
                self.assertEqual(code, 2)

    def test_overview_does_not_load_config(self):
        with mock.patch.object(
            self.delegate, "load_config", side_effect=AssertionError("config read")
        ):
            code, out, err = self.run_main(["--json", "describe", "--overview"])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["overview"])

    def test_reject_is_focused_and_discoverable(self):
        for argv in (["help", "workflow", "reject"], ["workflow", "reject", "--help"]):
            code, out, err = self.run_main(["--json", *argv])
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["command"], "workflow reject")
            self.assertIn("--reason", {option["flag"] for option in payload["options"]})
            self.assertIn("key-or-label", {arg["name"] for arg in payload["arguments"]})
        code, out, err = self.run_main(["help", "workflow"])
        self.assertEqual(code, 0, err)
        self.assertIn("reject", out)

    def test_resume_alias_preserves_existing_parse_contract(self):
        for extra in ([], ["--budget", "12"], ["--dry-run"], ["--notify", "channel:test"]):
            with self.subTest(extra=extra):
                original = self.delegate.parse_cli(
                    ["--json", "workflow", "run", "--resume", "wf_0123abcdef45", *extra]
                )
                alias = self.delegate.parse_cli(
                    ["--json", "workflow", "resume", "wf_0123abcdef45", *extra]
                )
                self.assertEqual(alias, original)
        for args in ([], ["wf_0123abcdef45", "another"], ["wf_0123abcdef45", "--args", "{}"]):
            with self.subTest(args=args), self.assertRaises(self.delegate.DelegateError):
                self.delegate.parse_cli(["workflow", "resume", *args])

    def test_nested_typo_is_advisory_with_safe_recovery(self):
        for parent, typo, suggestion in (
            ("workflow", "aproev", "approve"),
            ("worktree", "remvoe", "remove"),
            ("config", "inti", "init"),
            ("mail", "inbxo", "inbox"),
        ):
            with (
                self.subTest(parent=parent),
                mock.patch.object(self.delegate.workflow_commands, "emit") as execute,
            ):
                code, out, err = self.run_main(["--json", parent, typo])
                self.assertEqual(code, 2, err)
                payload = json.loads(out)
                self.assertEqual(payload["error"], f"unknown_{parent}_action")
                self.assertIn(suggestion, payload["message"])
                self.assertEqual(payload["schema"], "delegate.error.v1")
                self.assertEqual(payload["command"], parent)
                self.assertEqual(payload["helpTopic"], parent)
                self.assertIn(f"delegate help {parent} {suggestion}", payload["nextActions"])
                self.assertFalse(payload["ok"])
                execute.assert_not_called()

    def test_reject_usage_error_points_to_focused_help(self):
        code, out, err = self.run_main(["--json", "workflow", "reject", "wf_0123abcdef45"])
        self.assertEqual(code, 2, err)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "missing_workflow_reject_target")
        self.assertEqual(payload["helpTopic"], "workflow reject")
        self.assertIn("delegate help workflow reject", payload["nextActions"])

    def test_generic_error_preserves_existing_fields_and_actions(self):
        error = self.delegate.DelegateError(
            "example_error",
            "Example failure",
            3,
            diagnostics={"reason": "example"},
            next_actions=["Keep the existing action"],
        )
        stdout = io.StringIO()
        self.assertEqual(self.delegate.emit_error(error, True, stdout, io.StringIO()), 3)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema"], "delegate.error.v1")
        self.assertEqual(payload["error"], "example_error")
        self.assertEqual(payload["message"], "Example failure")
        self.assertEqual(payload["exitCode"], 3)
        self.assertEqual(payload["reason"], "example")
        self.assertEqual(payload["nextActions"], ["Keep the existing action"])
        self.assertIn("helpTopic", payload)
        self.assertIn("command", payload)

    def test_error_recovery_does_not_copy_unparsed_arguments(self):
        code, out, err = self.run_main(
            [
                "--json",
                "workflow",
                "aproev",
                "private-prompt-sentinel",
                "--token",
                "credential-sentinel",
            ]
        )
        self.assertEqual(code, 2, err)
        self.assertNotIn("private-prompt-sentinel", out)
        self.assertNotIn("credential-sentinel", out)
        self.assertNotIn("_supervise", out)

    def test_workflow_wait_help_names_attention_and_dry_run_boundaries(self):
        code, out, err = self.run_main(["help", "workflow", "wait"])
        self.assertEqual(code, 0, err)
        for phrase in ("paused", "stalled", "explicit", "dry-run", "excludes"):
            self.assertIn(phrase, out)

    def test_workflow_action_vocabulary_matches_public_focused_help(self):
        from delegate_agent import command_help

        actions = command_help.WORKFLOW_ACTION_KINDS
        public = {
            name.removeprefix("workflow ")
            for name, spec in command_help.COMMAND_SPECS.items()
            if name.startswith("workflow ") and not spec.internal
        }
        self.assertEqual(set(actions), public)
        self.assertNotIn("_supervise", actions)
        arguments = {
            "run": ["example.py"],
            "resume": ["wf_0123abcdef45"],
            "check": ["example.py"],
            "save": ["example.py", "--name", "example"],
            "status": ["wf_0123abcdef45"],
            "events": ["wf_0123abcdef45"],
            "watch": ["wf_0123abcdef45"],
            "result": [],
            "wait": [],
            "approve": ["wf_0123abcdef45"],
            "kill": ["wf_0123abcdef45"],
            "list": [],
            "reject": ["wf_0123abcdef45", "review", "--reason", "Needs evidence"],
        }
        self.assertEqual(set(arguments), set(actions))
        for action in actions:
            parsed = self.delegate.parse_cli(["workflow", action, *arguments[action]])
            self.assertEqual(
                parsed.workflow_command.action, "run" if action == "resume" else action
            )
            code, out, err = self.run_main(["--json", "workflow", action, "--help"])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["command"], f"workflow {action}")
