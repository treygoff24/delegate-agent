"""One automatic resume after a transient provider drop, and never a second.

These run the real CLI against a scripted fake `codex` (tests/provider_error_fakes.py).
The fake drops the stream ("websocket closed") on the steps the plan names.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from delegate_agent import auto_resume, cli, followup_command, provider_errors
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

    def test_safe_mode_is_never_session_resumed_but_gets_one_fresh_rerun(self):
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("codex", "safe", "read only question")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.invocation_count(), 2)
        self.assertFalse(
            any("resume" in argv for argv in self.invocations()),
            "a safe rerun is a fresh run, not a session resume",
        )
        auto = payload["autoResume"]
        self.assertEqual(auto["kind"], "rerun")
        self.assertEqual(auto["attempt"], 1)
        self.assertEqual(auto["of"]["alias"], "codex-1")
        self.assertEqual(auto["firstError"]["signature"], "stream_disconnected")
        self.assertEqual(payload["alias"], "codex-2")
        self.assertNotIn("followupOf", payload)

    def test_a_second_safe_drop_is_final(self):
        self.set_plan("ws_drop", "ws_drop", "ok")

        completed, payload = self.json_cli("codex", "safe", "read only question")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.invocation_count(), 2, "exactly one automatic rerun")
        self.assertEqual(payload["autoResume"]["kind"], "rerun")

    def test_the_opt_out_key_also_turns_off_the_safe_rerun(self):
        self.config["providerErrors"] = {"autoResume": False}
        self.set_plan("ws_drop", "ok")

        completed, payload = self.json_cli("codex", "safe", "read only question")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.invocation_count(), 1)
        self.assertNotIn("autoResume", payload)

    def in_process_json(self, *args: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env()),
            mock.patch.object(auto_resume, "sleep") as sleep,
        ):
            exit_code = cli.main(["--json", "--cwd", str(self.workspace), *args], stdout=buffer)
        self.slept = [call.args[0] for call in sleep.call_args_list]
        return exit_code, json.loads(buffer.getvalue())

    def test_a_broker_binding_refusal_is_retried_once_without_marking_the_lane(self):
        self.set_plan("broker403", "ok")

        exit_code, payload = self.in_process_json("codex", "safe", "review the thing")

        self.assertEqual(exit_code, 0, payload)
        self.assertEqual(self.invocation_count(), 2)
        self.assertEqual(payload["autoResume"]["kind"], "rerun")
        self.assertEqual(
            payload["autoResume"]["firstError"]["signature"], "broker_binding_inactive"
        )
        self.assertEqual(len(self.slept), 1)
        low, high = auto_resume.BROKER_RETRY_BACKOFF_SEC
        self.assertTrue(low <= self.slept[0] <= high)
        self.assertEqual(self.marker_files(), [])

    def test_a_broker_line_after_partial_work_is_not_retried_and_marks_as_before(self):
        self.set_plan("work_then_broker403", "ok")

        exit_code, payload = self.in_process_json("codex", "work", "do the thing")

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.invocation_count(), 1, "the child already produced output")
        self.assertNotIn("autoResume", payload)
        self.assertNotIn("laneMarkerDeferred", payload)
        self.assertEqual(self.slept, [])
        self.assertEqual(len(self.marker_files()), 1)

    def test_a_quiet_work_broker_refusal_is_not_rerun_and_says_how_to_relaunch(self):
        # A work child may have edited in place or acted externally before the
        # refusal line; a rerun could apply that twice, so work never reruns.
        self.set_plan("broker403", "ok")

        exit_code, payload = self.in_process_json("codex", "work", "do the thing")

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.invocation_count(), 1)
        self.assertNotIn("autoResume", payload)
        self.assertNotIn("laneMarkerDeferred", payload)
        self.assertEqual(self.slept, [])
        self.assertEqual(len(self.marker_files()), 1)
        self.assertIn("--force-launch", payload["providerError"]["hint"])

    def test_two_broker_refusals_fail_as_before_and_mark_the_lane(self):
        self.set_plan("broker403", "broker403", "ok")

        exit_code, payload = self.in_process_json("codex", "safe", "review the thing")

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.invocation_count(), 2, "exactly one retry")
        self.assertEqual(payload["providerError"]["signature"], "broker_binding_inactive")
        self.assertEqual(payload["autoResume"]["kind"], "rerun")
        self.assertEqual(len(self.marker_files()), 1)

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
        "no_child_output": False,
    }
    fields.update(overrides)
    return auto_resume.RunNote(**fields)


class SkipReasonTests(unittest.TestCase):
    def reason(self, note, *, enabled=True, already_automatic=False):
        return auto_resume.skip_reason(note, enabled=enabled, already_automatic=already_automatic)

    def test_each_guard_makes_a_run_ineligible(self):
        # Control: the canonical drop is eligible, so each single change below is
        # the only reason a run is refused.
        self.assertIsNone(self.reason(_note()))
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
        for guard, actual in cases.items():
            with self.subTest(guard):
                self.assertIsNotNone(actual)

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

    def test_a_persistent_signature_is_never_resumed_even_if_the_table_opts_it_in(self):
        opted_in = dataclasses.replace(
            provider_errors.SIGNATURES_BY_ID["auth_rejected"], auto_resume=True
        )
        note = _note(provider_error={"signature": "auth_rejected", "class": "persistent"})

        with mock.patch.object(provider_errors, "signature_for_record", return_value=opted_in):
            self.assertEqual(self.reason(note), "not_a_transient_drop")

    def test_only_the_two_drop_signatures_opt_in_and_both_are_transient(self):
        opted_in = {
            row.id: (row.klass, row.scope) for row in provider_errors.SIGNATURES if row.auto_resume
        }

        self.assertEqual(
            opted_in,
            {
                "stream_disconnected": ("transient", "lane"),
                "provider_unavailable": ("transient", "lane"),
            },
        )


class RerunSkipReasonTests(unittest.TestCase):
    def reason(self, note, *, enabled=True, already_automatic=False):
        return auto_resume.rerun_skip_reason(
            note, enabled=enabled, already_automatic=already_automatic
        )

    def test_each_guard_makes_a_run_ineligible(self):
        # Control: a safe stream drop is eligible with no saved session.
        self.assertIsNone(self.reason(_note(mode="safe", session_id=None)))
        cases = {
            "disabled": self.reason(_note(mode="safe"), enabled=False),
            "already_automatic": self.reason(_note(mode="safe"), already_automatic=True),
            "mode": self.reason(_note(mode="work")),
            "structured_output": self.reason(_note(mode="safe", structured=True)),
            "not_failed": self.reason(_note(mode="safe", status="succeeded")),
        }
        for guard, actual in cases.items():
            with self.subTest(guard):
                self.assertIsNotNone(actual)

    def test_a_safe_run_with_a_persistent_failure_is_not_rerun(self):
        note = _note(mode="safe", provider_error={"signature": "auth_rejected"})
        self.assertEqual(self.reason(note), "not_a_transient_drop")

    def test_a_quiet_safe_broker_binding_refusal_is_rerun(self):
        record = {"signature": "broker_binding_inactive", "class": "persistent"}
        self.assertIsNone(
            self.reason(_note(mode="safe", provider_error=record, no_child_output=True))
        )
        self.assertEqual(
            self.reason(_note(mode="safe", provider_error=record)),
            "child_produced_output",
            "no positive launch-time evidence fails closed",
        )
        self.assertGreater(auto_resume.broker_backoff_seconds(_note(provider_error=record)), 0)
        self.assertTrue(
            auto_resume.defers_lane_marker(
                record, enabled=True, already_automatic=False, no_child_output=True, mode="safe"
            )
        )
        self.assertFalse(
            auto_resume.defers_lane_marker(
                record, enabled=True, already_automatic=False, no_child_output=False, mode="safe"
            )
        )

    def test_a_work_broker_refusal_is_never_rerun_even_with_no_output(self):
        # Output counters are read after the child exits: a silent in-place edit or
        # external action before the refusal line would be applied twice by a rerun.
        record = {"signature": "broker_binding_inactive", "class": "persistent"}
        self.assertEqual(
            self.reason(_note(mode="work", provider_error=record, no_child_output=True)),
            "work_side_effects",
        )
        self.assertFalse(
            auto_resume.defers_lane_marker(
                record, enabled=True, already_automatic=False, no_child_output=True, mode="work"
            )
        )
        self.assertEqual(auto_resume.broker_backoff_seconds(_note(mode="safe")), 0.0)
