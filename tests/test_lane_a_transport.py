"""Lane A: cursor and Oh My Pi read the prompt on stdin, never on child argv.

Both harnesses were verified to consume a piped prompt at their current
releases (cursor 2026.09.02-c22c1a3 live; omp 18.1.13 in the shipped
`src/main.ts`), so the argv carve-out — and the process-argv prompt exposure
that came with it — is gone.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from delegate_agent import argv_builders as argv_api
from delegate_agent import (
    config as delegate_config,
)
from delegate_agent import describe_payload as describe_api
from delegate_agent import mail_core
from delegate_agent import prompt_transport as transport_api
from tests.delegate_commands_test_base import CommandTestBase


class CursorStdinTransportTests(CommandTestBase):
    def test_cursor_safe_request_sends_the_prompt_on_stdin_not_argv(self):
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello world",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.prompt_transport, transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(request.stdin_text, request.prompt)
        self.assertIn("hello world", request.stdin_text)
        # The whole point of the move: nothing prompt-shaped reaches /proc/<pid>/cmdline.
        for token in request.argv:
            self.assertNotIn("hello world", token)
        # With no argv prompt there is nothing left to redact, so the parent-facing
        # argv is the real argv.
        self.assertEqual(request.display_argv, request.argv)
        self.assertEqual(request.argv[-1], "stream-json")

    def test_cursor_argv_builder_never_appends_the_prompt(self):
        argv = argv_api.build_cursor_argv(["cursor-agent"], "work", "/repo", "composer-2.5")
        self.assertEqual(
            argv,
            [
                "cursor-agent",
                "--workspace",
                "/repo",
                "-p",
                "--trust",
                "--approve-mcps",
                "--force",
                "--model",
                "composer-2.5",
                "--output-format",
                "stream-json",
            ],
        )

    def test_cursor_resume_keeps_stdin_transport(self):
        # The resume path is a separate argv branch; a prompt reintroduced there
        # would be just as exposed as the one A1 removed.
        argv = argv_api.build_cursor_argv(
            ["cursor-agent"],
            "safe",
            "/repo",
            "composer-2.5",
            resume_session_id="sess-1",
        )
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")
        self.assertEqual(argv[-1], "stream-json")


class OmpStdinTransportTests(CommandTestBase):
    def test_omp_safe_request_sends_the_prompt_on_stdin_not_argv(self):
        request = self.build_git_request(
            "omp",
            "safe",
            None,
            "/repo",
            "review task",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.prompt_transport, transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(request.stdin_text, request.prompt)
        self.assertIn("review task", request.stdin_text)
        for token in request.argv:
            self.assertNotIn("review task", token)
        self.assertEqual(request.display_argv, request.argv)
        # The read-only lockdown is untouched by the transport move.
        self.assertIn("--approval-mode", request.argv)
        self.assertIn("always-ask", request.argv)

    def test_omp_accepts_a_flag_like_prompt_now_that_it_never_reaches_argv(self):
        # The planted negative for the deleted guard: these prompts used to raise
        # pi_family_prompt_flag_like. On stdin they are inert text, and the run
        # must both succeed and keep them out of argv.
        for prompt in ("--auto-approve", "@/etc/hostname", "-x"):
            with self.subTest(prompt=prompt):
                request = self.build_git_request(
                    "omp",
                    "work",
                    None,
                    "/repo",
                    prompt,
                    delegate_config.embedded_default_config(),
                    dry_run=True,
                )
                self.assertEqual(request.stdin_text, prompt)
                self.assertNotIn(prompt, request.argv)

    def test_omp_argv_builder_takes_no_prompt(self):
        argv = argv_api.build_omp_argv(
            delegate_config.embedded_default_config()["omp"], "work", None, None
        )
        self.assertEqual(argv, ["omp", "-p", "--no-session", "--mode", "json"])


class SharedTransportSurfaceTests(unittest.TestCase):
    def test_only_kimi_still_rides_argv(self):
        self.assertEqual(transport_api.ARGV_PROMPT_TRANSPORT_ENGINES, ("kimi",))

    def test_cursor_and_omp_redaction_constants_are_gone(self):
        self.assertFalse(hasattr(transport_api, "CURSOR_PROMPT_REDACTION"))
        self.assertFalse(hasattr(transport_api, "OMP_PROMPT_REDACTION"))
        self.assertTrue(hasattr(transport_api, "KIMI_PROMPT_REDACTION"))

    def test_describe_payload_reports_the_new_transports(self):
        payload = describe_api.describe_payload(
            delegate_config.embedded_default_config(), "embedded default"
        )
        transports = payload["promptTransports"]
        self.assertEqual(transports["cursor"], transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(transports["omp"], transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(transports["kimi"], transport_api.PROMPT_TRANSPORT_ARGV)

    def test_mail_flags_are_appended_for_omp_without_an_argv_prompt_to_dodge(self):
        # The old branch spliced --add-dir before the trailing argv prompt. With
        # the prompt on stdin the flags simply append; a splice would now put
        # them before nothing and is no longer needed.
        with tempfile.TemporaryDirectory() as registry_root:
            argv = mail_core.wire_work_mail_argv(
                "omp",
                ["omp", "-p", "--mode", "json"],
                Path(registry_root),
                prompt_transport=transport_api.PROMPT_TRANSPORT_STDIN,
                isolated_workspace=True,
            )
        self.assertTrue(argv[-1].startswith("--add-dir="))
        self.assertEqual(argv[:4], ["omp", "-p", "--mode", "json"])


if __name__ == "__main__":
    unittest.main()


class CursorReadOnlyModeTests(CommandTestBase):
    """A2: the cursor read-only boundary moves from prompt text into the harness.

    Cursor documents `--mode ask` as "Q&A style for explanations and questions
    (read-only)" and `-p/--print` as having "access to all tools, including write
    and shell", so safe mode without a mode flag rested on the prompt prefix plus
    workspace isolation alone.
    """

    def test_cursor_safe_argv_carries_mode_ask(self):
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "review this",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.argv[request.argv.index("--mode") + 1], "ask")
        self.assertNotIn("--force", request.argv)
        self.assertNotIn("--approve-mcps", request.argv)

    def test_cursor_read_only_call_carries_mode_ask_and_plain_call_does_not(self):
        read_only = argv_api.build_cursor_argv(
            ["cursor-agent"], "call", "/ws", "model", call_read_only=True
        )
        self.assertEqual(read_only[read_only.index("--mode") + 1], "ask")
        self.assertNotIn("--force", read_only)

        # The planted negative: a write-capable call must not be silently
        # downgraded to a read-only harness mode.
        write_call = argv_api.build_cursor_argv(["cursor-agent"], "call", "/ws", "model")
        self.assertNotIn("--mode", write_call)
        self.assertIn("--force", write_call)

    def test_cursor_work_never_gets_a_read_only_mode(self):
        work = argv_api.build_cursor_argv(["cursor-agent"], "work", "/ws", "model")
        self.assertNotIn("--mode", work)
        self.assertIn("--force", work)

    def test_cursor_resume_and_text_output_paths_keep_mode_ask(self):
        # Both argv branches (stream-json and pass-through text) and the resume
        # branch must carry the flag; a mode flag on only one is not a boundary.
        for kwargs in ({}, {"stream_capture": False}, {"resume_session_id": "s1"}):
            with self.subTest(kwargs=sorted(kwargs)):
                argv = argv_api.build_cursor_argv(
                    ["cursor-agent"], "safe", "/ws", "model", **kwargs
                )
                self.assertEqual(argv[argv.index("--mode") + 1], "ask")

    def test_describe_mode_mapping_shows_mode_ask_for_cursor_safe(self):
        payload = describe_api.describe_payload(
            delegate_config.embedded_default_config(), "embedded default"
        )
        cursor_safe = payload["modeMapping"]["cursor"]["safe"]
        self.assertEqual(cursor_safe[cursor_safe.index("--mode") + 1], "ask")
        self.assertNotIn("--force", cursor_safe)
        self.assertNotIn("--mode", payload["modeMapping"]["cursor"]["work"])
