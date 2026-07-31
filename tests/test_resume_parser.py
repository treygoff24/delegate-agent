import unittest

from delegate_agent import cli_parser
from delegate_agent.cli import DelegateError, parse_cli


class ResumeParserTests(unittest.TestCase):
    def test_resume_flags_before_handle_and_extra_parts_after_handle_are_distinct(self):
        parsed = parse_cli(
            [
                "resume",
                "--engine",
                "cursor",
                "--model",
                "model-a",
                "--reasoning-effort",
                "high",
                "run-1",
                "--model",
                "literal prompt part",
                "--fast",
            ]
        )

        self.assertEqual(parsed.resume.handle, "run-1")
        self.assertEqual(parsed.resume.engine, "cursor")
        self.assertEqual(parsed.resume.model, "model-a")
        self.assertEqual(parsed.resume.reasoning_effort, "high")
        self.assertEqual(
            parsed.resume.extra_parts,
            ["--model", "literal prompt part", "--fast"],
        )

    def test_resume_is_allowed_for_auth_profiles_and_groups(self):
        self.assertIn("resume", cli_parser.AUTH_PROFILE_SUBCOMMANDS)
        self.assertIn("resume", cli_parser.GROUP_SUBCOMMANDS)
        parsed = parse_cli(["--auth-profile", "work", "--group", "batch", "resume", "run-1"])
        self.assertEqual(parsed.global_options.auth_profile, "work")
        self.assertEqual(parsed.global_options.group, "batch")

    def test_resume_rejects_isolation(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["--isolation", "none", "resume", "run-1"])
        self.assertEqual(caught.exception.error, "invalid_option_combination")

    def test_resume_rejects_conflicting_output_schema_flags(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["resume", "--output-schema", "schema.json", "--no-output-schema", "run-1"])
        self.assertEqual(caught.exception.error, "invalid_option_combination")

    def test_resume_requires_handle(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["resume", "--engine", "cursor"])
        self.assertEqual(caught.exception.error, "missing_handle")
