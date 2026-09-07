import unittest

from delegate_agent.cli_parser import parse_cli
from delegate_agent.errors import DelegateError


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

        self.assertIsNotNone(parsed.payload)
        self.assertEqual(parsed.payload.handle, "run-1")
        self.assertEqual(parsed.payload.timeout, 120)
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(
            parsed.payload.prompt_parts,
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
        self.assertIsNotNone(parsed.payload)
        self.assertEqual(parsed.payload.handle, "run-1")
        self.assertEqual(parsed.payload.prompt_file, "prompt.md")
        self.assertEqual(parsed.payload.prompt_parts, [])

    def test_followup_rejects_missing_prompt_file_arg(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--prompt-file"])
        self.assertEqual(caught.exception.error, "missing_prompt_file")

    def test_followup_rejects_duplicate_prompt_file(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--prompt-file", "a.txt", "--prompt-file", "b.txt", "run-1"])
        self.assertEqual(caught.exception.error, "ambiguous_prompt_source")

    def test_followup_is_allowed_for_auth_profiles_and_groups(self):
        parsed = parse_cli(["--auth-profile", "work", "--group", "batch", "followup", "run-1"])
        self.assertEqual(parsed.global_options.auth_profile, "work")
        self.assertEqual(parsed.global_options.group, "batch")

    def test_followup_is_allowed_for_notify(self):
        parsed = parse_cli(["--notify", "room:ops", "followup", "run-1"])
        self.assertEqual(parsed.global_options.notify, "room:ops")

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
        self.assertIsNotNone(parsed.payload)
        self.assertEqual(parsed.payload.handle, "codex-1")
        self.assertEqual(parsed.payload.timeout, 45)
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.prompt_parts, ["do more"])

    def test_followup_warns_about_an_option_after_the_prompt(self):
        """Nothing after the handle is an option, so the absorption is silent."""
        parsed = parse_cli(["followup", "codex-1", "do more", "--model", "opus"])

        self.assertEqual(parsed.payload.prompt_parts, ["do more", "--model", "opus"])
        warnings = tuple(parsed.payload.warnings)
        self.assertTrue(any("--model" in warning for warning in warnings), warnings)
        self.assertTrue(any("prompt text" in warning for warning in warnings), warnings)

    def test_followup_does_not_warn_about_prompt_prose_or_literals(self):
        after_separator = parse_cli(["followup", "codex-1", "do more", "--", "--model", "opus"])
        self.assertEqual(after_separator.payload.warnings, ())

        prose = parse_cli(["followup", "codex-1", "explain what --model does"])
        self.assertEqual(prose.payload.warnings, ())

        negative_number = parse_cli(["followup", "codex-1", "cool it by", "-5 degrees"])
        self.assertEqual(negative_number.payload.warnings, ())

    def test_resume_warns_about_an_option_after_the_prompt(self):
        parsed = parse_cli(["resume", "codex-1", "keep going", "--model", "opus"])

        self.assertEqual(parsed.payload.extra_parts, ["keep going", "--model", "opus"])
        warnings = tuple(parsed.payload.warnings)
        self.assertTrue(any("--model" in warning for warning in warnings), warnings)

    def test_resume_and_followup_with_no_trailing_text_carry_no_warnings(self):
        """The bare form never reaches the tail branch, so the field must be preset."""
        self.assertEqual(parse_cli(["resume", "codex-1"]).payload.warnings, ())
        self.assertEqual(parse_cli(["followup", "codex-1"]).payload.warnings, ())

    def test_resume_does_not_warn_about_tokens_after_a_separator(self):
        parsed = parse_cli(["resume", "codex-1", "keep going", "--", "--model", "opus"])
        self.assertEqual(parsed.payload.extra_parts, ["keep going", "--model", "opus"])
        self.assertEqual(parsed.payload.warnings, ())


if __name__ == "__main__":
    unittest.main()
