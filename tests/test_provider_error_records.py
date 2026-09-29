"""providerError as data: captured per engine, classified status-first, on every surface."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import harness_events, provider_errors, run_registry, runner  # noqa: E402


def accumulate(harness, *events):
    accumulator = harness_events.StreamAccumulator(harness=harness)
    for event in events:
        accumulator.ingest_line(json.dumps(event))
    accumulator.finish_stream()
    return accumulator


def omp_turn(reason, **fields):
    return {
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "partial"}],
            "stopReason": reason,
            **fields,
        },
    }


class AccumulatorCaptureTests(unittest.TestCase):
    def test_codex_error_event_keeps_status_and_message(self):
        acc = accumulate(
            "codex",
            {"type": "error", "message": "unexpected status 401 Unauthorized: token_expired"},
        )
        self.assertEqual(acc.provider_error["source"], "error")
        self.assertIn("401 Unauthorized", acc.provider_error["message"])

    def test_codex_turn_failed_nested_error_is_captured(self):
        acc = accumulate(
            "codex",
            {
                "type": "turn.failed",
                "error": {
                    "message": "stream disconnected before completion",
                    "type": "server_error",
                },
            },
        )
        self.assertEqual(acc.provider_error["providerCode"], "server_error")
        self.assertIn("stream disconnected", acc.provider_error["message"])
        self.assertEqual(acc.provider_error["source"], "turn.failed")

    def test_pi_and_omp_keep_the_structured_status(self):
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness):
                acc = accumulate(
                    harness, omp_turn("error", errorStatus=400, errorMessage="Too many images")
                )
                self.assertEqual(acc.provider_error["status"], 400)
                self.assertEqual(acc.provider_error["message"], "Too many images")

    def test_a_retry_epilogue_does_not_erase_the_status(self):
        acc = accumulate(
            "omp",
            omp_turn("error", errorStatus=429, errorMessage="slow down"),
            {"type": "auto_retry_start"},
            {"type": "auto_retry_end", "success": False, "finalError": "slow down"},
        )
        self.assertEqual(acc.provider_error["status"], 429)

    def test_a_successful_terminal_clears_the_error(self):
        acc = accumulate(
            "omp",
            omp_turn("error", errorStatus=429, errorMessage="slow down"),
            {"type": "auto_retry_start"},
            {"type": "turn_start"},
            omp_turn("stop"),
            {"type": "auto_retry_end", "success": True},
        )
        self.assertIsNone(acc.provider_error)

    def test_grok_error_event(self):
        acc = accumulate("grok", {"type": "error", "message": "Grok Build usage balance exhausted"})
        self.assertEqual(acc.provider_error["message"], "Grok Build usage balance exhausted")

    def test_opencode_error_carries_the_nested_status_code(self):
        acc = accumulate(
            "opencode",
            {
                "type": "error",
                "error": {"name": "APIError", "data": {"message": "no funds", "statusCode": 402}},
            },
        )
        self.assertEqual(acc.provider_error["status"], 402)
        self.assertEqual(acc.provider_error["message"], "APIError: no funds")

    def test_claude_and_cursor_result_errors(self):
        for harness in ("claude", "cursor"):
            with self.subTest(harness=harness):
                acc = accumulate(
                    harness,
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": True,
                        "api_error_status": 401,
                        "result": "Invalid API key - Please run /login",
                    },
                )
                self.assertEqual(acc.provider_error["status"], 401)
                self.assertIn("Please run /login", acc.provider_error["message"])

    def test_capture_is_redacted_and_bounded(self):
        secret = "sk-abcdef1234567890abcdef"
        acc = accumulate("codex", {"type": "error", "message": f"Bearer {secret} " + "y" * 9000})
        message = acc.provider_error["message"]
        self.assertNotIn(secret, message)
        self.assertLessEqual(len(message), provider_errors.MESSAGE_LIMIT)

    def test_a_recovered_error_leaves_no_provider_error(self):
        acc = accumulate(
            "codex",
            {"type": "error", "message": "Reconnecting... 2/5 (stream disconnected)"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        self.assertIsNone(acc.provider_error)


class TrackedRunProviderErrorTests(unittest.TestCase):
    def _run(
        self,
        harness,
        events,
        workspace,
        *,
        exit_code=0,
        stderr="",
        mode="work",
        auth_profile=None,
    ):
        root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
        run_id, alias = run_registry.register_run(root, harness=harness)
        ctx = runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness=harness,
            engine=harness,
            mode=mode,
            model=None,
            source_cwd=workspace,
            execution_cwd=workspace,
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
            auth_profile=auth_profile,
        )
        script = (
            "import json,sys\n"
            f"for event in {events!r}:\n"
            " print(json.dumps(event),flush=True)\n"
            f"sys.stderr.write({stderr!r})\n"
            f"sys.exit({exit_code})\n"
        )
        code, payload = runner.execute_tracked(
            [sys.executable, "-c", script],
            workspace,
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        return code, payload, run_registry.load_run_state(root, run_id), root, run_id

    def test_omp_400_that_says_invalid_token_is_a_request_error_not_auth(self):
        # The audit's live misread: the message wording beat the 400.
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, state, _root, _run_id = self._run(
                "omp",
                [omp_turn("error", errorStatus=400, errorMessage="Invalid image token in request")],
                workspace,
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["failureKind"], "provider_error")
        self.assertEqual(payload["error"], "provider_error")
        self.assertEqual(payload["providerError"]["signature"], "request_rejected")
        self.assertEqual(payload["providerError"]["status"], 400)
        self.assertEqual(payload["providerError"]["scope"], "request")
        self.assertEqual(state["providerError"]["signature"], "request_rejected")

    def test_omp_image_limit_names_its_signature_and_never_auth_failed(self):
        with tempfile.TemporaryDirectory() as workspace:
            _code, payload, _state, _root, _run_id = self._run(
                "omp",
                [
                    omp_turn(
                        "error", errorStatus=400, errorMessage="Too many images in request: 8 > 4"
                    )
                ],
                workspace,
            )
        self.assertEqual(payload["providerError"]["signature"], "request_image_limit")
        self.assertNotEqual(payload["failureKind"], "provider_auth")
        self.assertNotEqual(payload["error"], "auth_failed")
        self.assertIn("Too many images in request: 8 > 4", payload["message"])

    def test_cursor_auth_death_on_stderr_is_typed_with_the_fix(self):
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, _state, root, run_id = self._run(
                "cursor",
                [],
                workspace,
                exit_code=1,
                stderr="Authentication required. Please run agent login first\n",
            )
            report = (run_registry.run_directory(root, run_id) / "completion-report.md").read_text()
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "auth_failed")
        self.assertEqual(payload["failureKind"], "provider_auth")
        self.assertEqual(payload["providerError"]["signature"], "cursor_auth_required")
        self.assertEqual(payload["providerError"]["class"], "persistent")
        self.assertIn("estate-cursor login", payload["providerError"]["hint"])
        self.assertIn("estate-cursor login", payload["nextActions"][0])
        self.assertIn("Provider error: cursor_auth_required", report)
        self.assertIn("estate-cursor login", report)

    def test_cursor_auth_hint_names_the_resolved_run_profile_not_the_environment(self):
        # --auth-profile work while AI_PROFILE=personal: the run used the work realm.
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.dict(os.environ, {"AI_PROFILE": "personal"}),
        ):
            _code, payload, _state, _root, _run_id = self._run(
                "cursor",
                [],
                workspace,
                exit_code=1,
                stderr="Authentication required. Please run agent login first\n",
                auth_profile="work",
            )
        hint = payload["providerError"]["hint"]
        self.assertIn("for the work realm this run used", hint)
        self.assertIn("--auth-profile personal", hint)

    def test_codex_websocket_close_is_transient_and_carries_the_provider_text(self):
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, _state, _root, _run_id = self._run(
                "codex",
                [
                    {
                        "type": "error",
                        "message": "websocket closed by server before response.completed",
                    }
                ],
                workspace,
                exit_code=1,
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["providerError"]["signature"], "stream_disconnected")
        self.assertEqual(payload["providerError"]["class"], "transient")
        self.assertEqual(payload["failureKind"], "provider_error")
        self.assertNotEqual(payload["message"], "Child command failed.")
        self.assertIn("websocket closed", payload["message"])

    def test_kimi_402_reported_with_exit_zero_fails_as_provider_quota(self):
        # pc2_ba049c7e: a 402 shown as success.
        with tempfile.TemporaryDirectory() as workspace:
            code, payload, _state, _root, _run_id = self._run(
                "kimi",
                [{"type": "error", "message": "402 Insufficient account funds"}],
                workspace,
                exit_code=0,
            )
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failureKind"], "provider_quota")
        self.assertEqual(payload["providerError"]["signature"], "payment_required")
        self.assertEqual(payload["providerError"]["status"], 402)
        self.assertIn("Kimi credits", payload["providerError"]["hint"])

    def test_generic_engines_carry_the_provider_message_instead_of_child_failed(self):
        for harness in ("codex", "grok", "claude"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as workspace:
                events = (
                    [{"type": "error", "message": "flagged for possible cybersecurity risk"}]
                    if harness != "claude"
                    else [
                        {
                            "type": "result",
                            "subtype": "success",
                            "is_error": True,
                            "result": "flagged for possible cybersecurity risk",
                        }
                    ]
                )
                _code, payload, _state, _root, _run_id = self._run(
                    harness, events, workspace, exit_code=1
                )
                self.assertEqual(payload["providerError"]["signature"], "content_flagged")
                self.assertEqual(payload["failureKind"], "provider_refusal")
                self.assertIn("cybersecurity", payload["message"])
                self.assertNotEqual(payload["message"], "Child command failed.")

    def test_an_unrecognized_provider_error_still_reaches_the_message(self):
        with tempfile.TemporaryDirectory() as workspace:
            _code, payload, _state, _root, _run_id = self._run(
                "codex",
                [{"type": "error", "message": "the moon is in the wrong phase"}],
                workspace,
                exit_code=1,
            )
        self.assertEqual(payload["providerError"]["signature"], provider_errors.UNCLASSIFIED)
        self.assertEqual(payload["providerError"]["class"], "unknown")
        self.assertEqual(payload["error"], "provider_error")
        self.assertEqual(payload["failureKind"], "provider_error")
        self.assertIn("the moon is in the wrong phase", payload["message"])

    def test_a_successful_run_has_no_provider_error(self):
        with tempfile.TemporaryDirectory() as workspace:
            _code, payload, state, _root, _run_id = self._run(
                "codex",
                [
                    {"type": "error", "message": "Reconnecting... 2/5 (stream disconnected)"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
                ],
                workspace,
            )
        self.assertTrue(payload["ok"])
        self.assertNotIn("providerError", payload)
        self.assertNotIn("providerError", state)

    def test_the_provider_error_is_redacted_on_every_surface(self):
        secret = "sk-abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as workspace:
            _code, payload, state, root, run_id = self._run(
                "codex",
                [{"type": "error", "message": f"boom Authorization: Bearer {secret}"}],
                workspace,
                exit_code=1,
            )
            report = (run_registry.run_directory(root, run_id) / "completion-report.md").read_text()
        for surface in (json.dumps(payload), json.dumps(state), report):
            self.assertNotIn(secret, surface)


class CallModeProviderErrorTests(unittest.TestCase):
    def test_call_failure_carries_the_typed_provider_error(self):
        script = (
            "import json,sys\n"
            "print(json.dumps({'type':'error','message':'402 Insufficient account funds'}))\n"
            "sys.exit(1)\n"
        )
        with tempfile.TemporaryDirectory() as workspace:
            result = runner.execute_call([sys.executable, "-c", script], workspace, harness="kimi")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error, "usage_limit")
        self.assertEqual(result.provider_error["signature"], "payment_required")
        self.assertIn("Kimi credits", result.provider_error["hint"])

    def test_cursor_auth_hint_in_call_mode_names_the_resolved_profile(self):
        # execute_call must hand its resolved auth profile to the hint, or the
        # hint falls back to the environment's profile (personal here).
        script = (
            "import sys\n"
            "sys.stderr.write('Authentication required. Please run agent login first\\n')\n"
            "sys.exit(1)\n"
        )
        with (
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.dict(os.environ, {"AI_PROFILE": "personal"}),
        ):
            result = runner.execute_call(
                [sys.executable, "-c", script], workspace, harness="cursor", auth_profile="work"
            )
        self.assertEqual(result.provider_error["signature"], "cursor_auth_required")
        hint = result.provider_error["hint"]
        self.assertIn("for the work realm this run used", hint)
        self.assertIn("--auth-profile personal", hint)


if __name__ == "__main__":
    unittest.main()
