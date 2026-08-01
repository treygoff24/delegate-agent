from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import profile_guard
from delegate_agent.cli import DelegateError, parse_cli


class MailParserTests(unittest.TestCase):
    def test_global_group_stays_launch_only_and_local_group_uses_mail_send(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["--group", "launch-group", "mail", "send", "--to", "coordinator", "body"])
        self.assertEqual(caught.exception.error, "invalid_option_combination")

        parsed = parse_cli(["mail", "send", "--group", "reviewers", "--subject", "subject", "body"])
        self.assertEqual(parsed.global_options.group, None)
        self.assertIsNotNone(parsed.mail_command)
        self.assertEqual(parsed.mail_command.group, "reviewers")
        self.assertEqual(parsed.mail_command.body, "body")
        self.assertEqual(parsed.mail_command.subject, "subject")

        launch = parse_cli(["--group", "launch-group", "codex", "work", "prompt"])
        self.assertEqual(launch.global_options.group, "launch-group")

    def test_all_six_mail_subcommands_parse_into_typed_commands(self):
        cases = {
            "send": (["--to", "coordinator", "body"], {"to": "coordinator", "body": "body"}),
            "inbox": (["--from", "sender"], {"from_sender": "sender"}),
            "read": (["id-prefix", "--peek"], {"message_id": "id-prefix", "peek": True}),
            "status": (["message-id"], {"message_id": "message-id"}),
            "watch": (
                ["--once", "--from", "sender", "--reply-to", "original"],
                {
                    "once": True,
                    "from_sender": "sender",
                    "reply_to": "original",
                },
            ),
            "prune": (["--older-than", "7", "--dry-run"], {"older_than_days": 7, "dry_run": True}),
        }
        for action, (args, expected) in cases.items():
            with self.subTest(action=action):
                parsed = parse_cli(["mail", action, *args])
                self.assertEqual(parsed.subcommand, "mail")
                command = parsed.mail_command
                self.assertIsNotNone(command)
                self.assertEqual(command.action, action)
                for field, value in expected.items():
                    self.assertEqual(getattr(command, field), value)

    def test_watch_interval_rejects_values_outside_documented_bounds(self):
        for value in ("1", "60001"):
            with self.subTest(value=value), self.assertRaises(DelegateError) as caught:
                parse_cli(["mail", "watch", "--interval-ms", value])
            self.assertEqual(caught.exception.error, "invalid_interval")
        self.assertEqual(
            parse_cli(["mail", "watch", "--interval-ms", "100"]).mail_command.interval_ms, 100
        )
        self.assertEqual(
            parse_cli(["mail", "watch", "--interval-ms", "60000"]).mail_command.interval_ms,
            60000,
        )
        self.assertEqual(
            parse_cli(["mail", "watch"]).mail_command.interval_ms,
            1000,
        )

    def test_python_profile_guard_matrix(self):
        matrix = {
            ("inbox",): True,
            ("status", "id"): True,
            ("watch",): True,
            ("read", "id", "--peek"): True,
            ("read", "id"): False,
            ("send", "--to", "coordinator", "body"): False,
            ("prune",): False,
        }
        with tempfile.TemporaryDirectory(prefix="delegate-mail-guard-") as tmp:
            home = Path(tmp)
            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "AI_PROFILE": "work"},
                clear=False,
            ):
                for args, expected in matrix.items():
                    with self.subTest(args=args):
                        parsed = parse_cli(["mail", *args])
                        self.assertEqual(profile_guard.is_read_only_command(parsed), expected)

    def test_shell_shim_and_python_profile_guard_have_mail_parity(self):
        shim = Path(__file__).parents[1] / "bin" / "delegate-profile-shim"
        matrix = {
            ("mail", "inbox"): True,
            ("mail", "status", "id"): True,
            ("mail", "watch"): True,
            ("mail", "read", "id", "--peek"): True,
            ("mail", "read", "id"): False,
            ("mail", "send", "--to", "coordinator", "body"): False,
            ("mail", "prune"): False,
        }
        with tempfile.TemporaryDirectory(prefix="delegate-mail-shim-") as tmp:
            root = Path(tmp)
            fake = root / "shim-target.py"
            fake.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
            home = root / "home"
            home.mkdir()
            for args, expected_read_only in matrix.items():
                with self.subTest(args=args):
                    env = dict(os.environ)
                    env.update(
                        {
                            "HOME": str(home),
                            "AI_PROFILE": "work",
                            "DELEGATE_SHIM_PY": str(fake),
                        }
                    )
                    env.pop("DELEGATE_CONFIG", None)
                    result = subprocess.run(
                        [str(shim), *args],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0 if expected_read_only else 1)
                    if expected_read_only:
                        self.assertIn("continuing because 'mail' is read-only", result.stderr)
                    else:
                        self.assertIn("refusing to run a launch or mutation command", result.stderr)


if __name__ == "__main__":
    unittest.main()
