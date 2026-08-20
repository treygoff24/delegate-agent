import unittest

from delegate_agent import cli_parser
from delegate_agent.cli import DelegateError, parse_cli


class FollowupParserTests(unittest.TestCase):
    def test_followup_flags_before_handle_and_prompt_after_handle_are_distinct(self):
        parsed = parse_cli(
            [
                "followup",
                "--timeout",
                "120",
                "--dry-run",
                "run-1",
                "--model",
                "literal prompt part",
                "--fast",
            ]
        )

        self.assertIsNotNone(parsed.followup)
        self.assertEqual(parsed.followup.handle, "run-1")
        self.assertEqual(parsed.followup.timeout, 120)
        self.assertTrue(parsed.followup.dry_run)
        self.assertEqual(
            parsed.followup.prompt_parts,
            ["--model", "literal prompt part", "--fast"],
        )

    def test_followup_accepts_prompt_file(self):
        parsed = parse_cli(
            [
                "followup",
                "--prompt-file",
                "prompt.md",
                "run-1",
            ]
        )
        self.assertIsNotNone(parsed.followup)
        self.assertEqual(parsed.followup.handle, "run-1")
        self.assertEqual(parsed.followup.prompt_file, "prompt.md")
        self.assertEqual(parsed.followup.prompt_parts, [])

    def test_followup_rejects_missing_prompt_file_arg(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--prompt-file"])
        self.assertEqual(caught.exception.error, "missing_prompt_file")

    def test_followup_rejects_duplicate_prompt_file(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--prompt-file", "a.txt", "--prompt-file", "b.txt", "run-1"])
        self.assertEqual(caught.exception.error, "ambiguous_prompt_source")

    def test_followup_is_allowed_for_auth_profiles_and_groups(self):
        self.assertIn("followup", cli_parser.AUTH_PROFILE_SUBCOMMANDS)
        self.assertIn("followup", cli_parser.GROUP_SUBCOMMANDS)
        parsed = parse_cli(["--auth-profile", "work", "--group", "batch", "followup", "run-1"])
        self.assertEqual(parsed.global_options.auth_profile, "work")
        self.assertEqual(parsed.global_options.group, "batch")

    def test_followup_rejects_isolation(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["--isolation", "none", "followup", "run-1"])
        self.assertEqual(caught.exception.error, "invalid_option_combination")

    def test_followup_rejects_model(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--model", "gpt-5", "run-1"])
        self.assertEqual(caught.exception.error, "unknown_option")

    def test_followup_rejects_engine(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--engine", "codex", "run-1"])
        self.assertEqual(caught.exception.error, "unknown_option")

    def test_followup_requires_handle(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--timeout", "30"])
        self.assertEqual(caught.exception.error, "missing_handle")

    def test_followup_accepts_timeout_and_dry_run(self):
        parsed = parse_cli(["followup", "--timeout", "45", "--dry-run", "codex-1", "do more"])
        self.assertIsNotNone(parsed.followup)
        self.assertEqual(parsed.followup.handle, "codex-1")
        self.assertEqual(parsed.followup.timeout, 45)
        self.assertTrue(parsed.followup.dry_run)
        self.assertEqual(parsed.followup.prompt_parts, ["do more"])


if __name__ == "__main__":
    unittest.main()
