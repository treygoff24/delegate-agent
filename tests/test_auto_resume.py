"""One automatic resume after a transient provider drop, and never a second.

These run the real CLI against a scripted fake `codex` (tests/provider_error_fakes.py).
The fake drops the stream ("websocket closed") on the steps the plan names.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from unittest import mock

from delegate_agent import auto_resume, cli, followup_command
from delegate_agent.errors import DelegateError
from tests.provider_error_fakes import FakeCodexCase

SESSION = "thr_fake_session"
CLAUDE_SESSION = "550e8400-e29b-41d4-a716-446655440000"


class AutoResumeTests(FakeCodexCase):
    def manifest(self, run_id: str) -> dict:
        path = self.workspace / ".delegate" / "runs" / run_id / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_one_drop_then_success_resumes_exactly_once(self):
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("codex", "work", "do the thing")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.invocation_count(), 2)
        first_argv, second_argv = self.invocations()
        self.assertNotIn("resume", first_argv)
        self.assertIn("resume", second_argv)
        self.assertIn(SESSION, second_argv)
        # The envelope is the continuation's, annotated as automatic and linked back.
        auto = payload["autoResume"]
        self.assertTrue(auto["automatic"])
        self.assertEqual(auto["attempt"], 1)
        self.assertEqual(auto["of"]["alias"], "codex-1")
        self.assertEqual(auto["trigger"]["signature"], "stream_disconnected")
        self.assertEqual(auto["trigger"]["class"], "transient")
        self.assertEqual(payload["alias"], "codex-2")
        self.assertEqual(payload["followupOf"], auto["of"]["runId"])

    def test_the_continuation_run_records_that_it_was_automatic(self):
        self.set_plan("ws_drop", "ok")

        _completed, payload = self.json_cli("codex", "work", "do the thing")

        manifest = self.manifest(payload["runId"])
        self.assertEqual(manifest["autoResume"], payload["autoResume"])
        self.assertEqual(manifest["followupOf"], payload["autoResume"]["of"]["runId"])
        first = self.manifest(payload["autoResume"]["of"]["runId"])
        self.assertNotIn("autoResume", first, "the dropped run is an ordinary run")

    def git_workspace(self) -> None:
        (self.workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
        for args in (
            ("init", "-q", "-b", "main"),
            ("add", "seed.txt"),
            ("commit", "-q", "-m", "seed"),
        ):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "-C",
                    str(self.workspace),
                    *args,
                ],
                check=True,
                capture_output=True,
            )

    def test_a_worktree_run_resumes_inside_its_own_worktree(self):
        self.git_workspace()
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("--isolation", "worktree", "codex", "work", "do it")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.invocation_count(), 2)
        self.assertEqual(payload["autoResume"]["of"]["alias"], "codex-1")
        first = self.manifest(payload["autoResume"]["of"]["runId"])
        self.assertEqual(first["isolationLifecycle"], "persistent")
        # The continuation attached to the first attempt's worktree and branch: the
        # files the first attempt touched are where the resumed session looks.
        self.assertEqual(payload["isolationLifecycle"], "attached")
        self.assertEqual(payload["executionCwd"], first["executionCwd"])
        self.assertEqual(payload["branch"], first["branch"])

    def test_a_continuation_that_cannot_be_built_leaves_the_first_result_annotated(self):
        self.set_plan("ws_drop", "ok")
        buffer = io.StringIO()
        refusal = DelegateError("followup_record_invalid", "the record is unreadable")

        with (
            mock.patch.dict(os.environ, self.env()),
            mock.patch.object(followup_command, "build_followup_plan", side_effect=refusal),
        ):
            exit_code = cli.main(
                ["--json", "--cwd", str(self.workspace), "codex", "work", "do it"],
                stdout=buffer,
            )

        payload = json.loads(buffer.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(self.invocation_count(), 1)
        self.assertEqual(payload["alias"], "codex-1")
        self.assertEqual(payload["providerError"]["signature"], "stream_disconnected")
        skipped = payload["autoResume"]
        self.assertFalse(skipped["attempted"])
        self.assertIn("followup_record_invalid", skipped["reason"])

    def test_a_second_drop_is_final_and_gets_no_second_resume(self):
        self.set_plan("ws_drop", "ws_drop", "ok")

        completed, payload = self.json_cli("codex", "work", "do the thing")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.invocation_count(), 2, "exactly one automatic resume")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["providerError"]["signature"], "stream_disconnected")
        self.assertEqual(payload["autoResume"]["attempt"], 1)
        self.assertEqual(payload["alias"], "codex-2")

    def test_claude_resumes_once_after_a_server_side_failure(self):
        self.set_claude_plan("overloaded", "ok")

        completed, payload = self.json_cli("claude", "work", "do the thing")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        first_argv, second_argv = self.claude_invocations()
        self.assertNotIn("--resume", first_argv)
        self.assertIn("--resume", second_argv)
        self.assertIn(CLAUDE_SESSION, second_argv)
        self.assertEqual(payload["autoResume"]["trigger"]["signature"], "provider_unavailable")
        self.assertEqual(payload["autoResume"]["of"]["alias"], "claude-1")

    def test_claude_gets_no_second_automatic_resume(self):
        self.set_claude_plan("overloaded", "overloaded", "ok")

        completed, payload = self.json_cli("claude", "work", "do the thing")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(len(self.claude_invocations()), 2)
        self.assertEqual(payload["providerError"]["signature"], "provider_unavailable")

    def test_the_opt_out_key_turns_it_off(self):
        self.config["providerErrors"] = {"autoResume": False}
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("codex", "work", "do the thing")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.invocation_count(), 1)
        self.assertNotIn("autoResume", payload)

    def test_a_run_without_a_saved_session_is_not_resumed(self):
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("codex", "work", "--no-resumable", "do the thing")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.invocation_count(), 1)
        self.assertNotIn("autoResume", payload)

    def test_safe_mode_is_never_resumed(self):
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("codex", "safe", "read only question")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.invocation_count(), 1)
        self.assertNotIn("autoResume", payload)

    def test_call_mode_is_never_resumed(self):
        self.set_plan("ws_drop", "ok")

        completed = self.bare_cli("--json", "codex", "call", "hello")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertNotIn("autoResume", json.loads(completed.stdout))
        self.assertFalse(
            any("resume" in argv for argv in self.invocations()),
            "a call never resumes a session",
        )

    def test_persistent_and_unclassified_failures_are_not_resumed(self):
        for step in ("auth401", "novel", "image_limit"):
            with self.subTest(step=step):
                self.state_dir_reset()
                self.set_plan(step, "ok")

                completed, payload = self.json_cli("--force-launch", "codex", "work", f"try {step}")

                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.invocation_count(), 1)
                self.assertNotIn("autoResume", payload)

    def test_the_text_output_says_it_resumed(self):
        self.set_plan("ws_drop", "ok")

        completed = self.cli("codex", "work", "do the thing")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.invocation_count(), 2)
        self.assertIn("resuming its saved session once automatically", completed.stderr)

    def state_dir_reset(self) -> None:
        counter = self.state_dir / "count"
        if counter.exists():
            counter.unlink()
        log = self.state_dir / "argv.log"
        if log.exists():
            log.unlink()


def _note(**overrides):
    fields = {
        "run_id": "del_x",
        "alias": "codex-1",
        "engine": "codex",
        "mode": "work",
        "status": "failed",
        "provider_error": {"signature": "stream_disconnected", "class": "transient"},
        "session_id": "thr_fake_session",
        "isolation_lifecycle": "none",
        "structured": False,
    }
    fields.update(overrides)
    return auto_resume.RunNote(**fields)


class SkipReasonTests(unittest.TestCase):
    def reason(self, note, *, enabled=True, already_automatic=False):
        return auto_resume.skip_reason(note, enabled=enabled, already_automatic=already_automatic)

    def test_the_canonical_drop_is_eligible(self):
        self.assertIsNone(self.reason(_note()))

    def test_each_guard_names_itself(self):
        cases = {
            "disabled": self.reason(_note(), enabled=False),
            "already_automatic": self.reason(_note(), already_automatic=True),
            "engine": self.reason(_note(engine="omp")),
            "mode": self.reason(_note(mode="safe")),
            "structured_output": self.reason(_note(structured=True)),
            "not_failed": self.reason(_note(status="succeeded")),
            "no_saved_session": self.reason(_note(session_id=None)),
            "workspace_not_carried": self.reason(_note(isolation_lifecycle="temporary")),
        }
        for expected, actual in cases.items():
            with self.subTest(expected):
                self.assertEqual(actual, expected)

    def test_only_transient_signatures_that_opt_in_are_eligible(self):
        for signature, klass in (
            ("auth_rejected", "persistent"),
            ("rate_limited", "transient"),
            ("model_at_capacity", "transient"),
            ("unclassified", "unknown"),
        ):
            with self.subTest(signature):
                note = _note(provider_error={"signature": signature, "class": klass})
                self.assertEqual(self.reason(note), "not_a_transient_drop")
        self.assertEqual(self.reason(_note(provider_error=None)), "not_a_transient_drop")
        provider = _note(provider_error={"signature": "provider_unavailable", "class": "transient"})
        self.assertIsNone(self.reason(provider))
