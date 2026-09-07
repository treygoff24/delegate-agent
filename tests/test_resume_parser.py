import unittest

from delegate_agent.cli_parser import parse_cli
from delegate_agent.errors import DelegateError


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

        self.assertEqual(parsed.payload.handle, "run-1")
        self.assertEqual(parsed.payload.engine, "cursor")
        self.assertEqual(parsed.payload.model, "model-a")
        self.assertEqual(parsed.payload.reasoning_effort, "high")
        self.assertEqual(
            parsed.payload.extra_parts,
            ["--model", "literal prompt part", "--fast"],
        )

    def test_resume_is_allowed_for_auth_profiles_and_groups(self):
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

    def test_resumable_accepted_for_codex_and_claude(self):
        parsed_codex = parse_cli(["codex", "work", "--resumable", "implement feature"])
        self.assertTrue(parsed_codex.payload.resumable)

        parsed_claude = parse_cli(["claude", "work", "--resumable", "review code"])
        self.assertTrue(parsed_claude.payload.resumable)

    def test_resumable_rejected_on_safe_mode(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli([engine, "safe", "--resumable", "review code"])
                self.assertEqual(caught.exception.error, "invalid_option_combination")
                self.assertIn("safe workspaces are temporary", caught.exception.message)
                self.assertIn("no re-entry path", caught.exception.message)

    def test_resumable_rejected_on_call_mode(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli([engine, "call", "--resumable", "summarize"])
                self.assertEqual(caught.exception.error, "invalid_option_combination")

    def test_resumable_rejected_for_unsupported_engines(self):
        for engine in ("cursor", "droid", "grok", "devin", "opencode", "pi", "omp", "kimi"):
            with self.subTest(engine=engine):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli([engine, "work", "--resumable", "prompt"])
                self.assertEqual(caught.exception.error, "followup-unsupported")
                self.assertIn("codex and claude", caught.exception.message)
                self.assertEqual(caught.exception.diagnostics.get("code"), "followup-unsupported")

    def test_resumable_rejected_on_duplicate_flag(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["codex", "work", "--resumable", "--resumable", "prompt"])
        self.assertEqual(caught.exception.error, "invalid_option_combination")
