import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
HARNESS_EVENTS_PATH = ROOT / "src" / "delegate_agent" / "harness_events.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_harness_events():
    spec = importlib.util.spec_from_file_location(
        "delegate_harness_events_under_test", HARNESS_EVENTS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HarnessEventsTests(unittest.TestCase):
    def test_provider_refusal_is_runtime_observed_but_assistant_text_cannot_forge_it(self):
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "provider_refusal is just prose here"}]
                    },
                }
            )
        )
        self.assertIsNone(acc.provider_terminal_state)

        acc.ingest_line(
            json.dumps(
                {
                    "type": "error",
                    "code": "provider_refusal",
                    "message": "Provider refusal: policy",
                }
            )
        )

        self.assertEqual(acc.provider_terminal_state, "provider_refusal")
        self.assertEqual(acc.terminal_status, "failed")

    def test_free_text_error_words_do_not_forge_provider_terminal_state(self):
        for message in (
            "stream cancelled while reconnecting",
            "worker refused a retry request",
            "usage limit",
        ):
            with self.subTest(message=message):
                acc = self.events.StreamAccumulator(harness="codex")
                acc.ingest_line(json.dumps({"type": "error", "message": message}))
                # The run is failed because an error event arrived, but the
                # typed provider state stays unset: error prose cannot forge it.
                self.assertIsNone(acc.provider_terminal_state)
                self.assertEqual(acc.terminal_status, "failed")

    def test_generic_error_event_records_failed_terminal(self):
        """shared B1: an error event is a terminal signal on every harness."""
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Partial answer"},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "error", "message": "429 rate limit"}))
        acc.ingest_line(json.dumps({"type": "turn.completed"}))

        self.assertEqual(acc.terminal_status, "failed")
        self.assertEqual(
            acc.terminal_event,
            {"event": "error", "status": "failed", "reason": "429 rate limit"},
        )
        self.assertEqual(acc._last_error_message, "429 rate limit")
        # The pre-error preamble must not be promoted over the failure.
        self.assertIsNone(acc.completion_text)

    def test_codex_error_followed_by_a_fresh_sealed_message_recovers(self):
        """An error the stream recovers from must not fail an exit-zero run."""
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(
            json.dumps({"type": "error", "message": "stream cancelled while reconnecting"})
        )
        self.assertEqual(acc.terminal_status, "failed")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Status: completed after reconnect."},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))

        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(acc.completion_text, "Status: completed after reconnect.")

    def test_a_terminal_reason_is_redacted_before_it_is_persisted(self):
        """terminalEvent reaches the run record; recentEvents is the raw mirror."""
        secret = "Authorization: Bearer sk-abcdef1234567890"
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(json.dumps({"type": "error", "message": f"Usage limit for {secret}"}))

        self.assertNotIn(secret, json.dumps(acc.terminal_event))
        self.assertIn("Usage limit for", acc.terminal_event["reason"])

    def test_no_sink_of_an_error_message_keeps_the_bearer_token(self):
        """The error text reaches terminalEvent, recentEvents and `current`; all redact."""
        secret = "Bearer sk-ant-api03-SECRETVALUE123"
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(json.dumps({"type": "error", "message": f"401 from api: {secret}"}))

        self.assertNotIn(secret, json.dumps(acc.terminal_event))
        recent = acc.bounded_recent_events()[0]
        self.assertNotIn(secret, json.dumps(recent))
        kinds = {event["kind"] for event in recent}
        self.assertIn("error", kinds)
        self.assertIn("run.completed", kinds)
        self.assertNotIn(secret, json.dumps(acc.current))
        self.assertNotIn(secret, json.dumps(acc._last_error_message))
        self.assertIn("401 from api:", acc.terminal_event["reason"])

    def test_a_terminal_reason_from_outside_the_error_path_is_redacted_in_both_sinks(self):
        """opencode builds its reason from the payload without touching _ingest_error_event."""
        secret = "Bearer sk-ant-api03-SECRETVALUE123"
        acc = self.events.StreamAccumulator(harness="opencode")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "error",
                    "error": {"name": "AuthError", "data": {"message": f"sent {secret}"}},
                }
            )
        )

        self.assertEqual(acc.terminal_status, "failed")
        self.assertNotIn(secret, json.dumps(acc.terminal_event))
        self.assertIn("AuthError", acc.terminal_event["reason"])
        self.assertNotIn(secret, json.dumps(acc.bounded_recent_events()[0]))

    def test_error_event_reads_nested_error_message(self):
        """shared B2: Anthropic/OpenAI-shaped errors nest the text one level down."""
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(
            json.dumps({"type": "error", "error": {"message": "429 rate_limit_exceeded"}})
        )

        self.assertEqual(acc._last_error_message, "429 rate_limit_exceeded")
        error_events = [event for event in acc.events if event.kind == "error"]
        self.assertEqual([event.message for event in error_events], ["429 rate_limit_exceeded"])
        self.assertEqual(acc.terminal_status, "failed")
        self.assertEqual(
            acc.terminal_event,
            {"event": "error", "status": "failed", "reason": "429 rate_limit_exceeded"},
        )

    def test_typed_provider_error_records_exactly_one_terminal(self):
        """A provider-typed error must not publish a second run.completed."""
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(
            json.dumps(
                {"type": "error", "code": "provider_refusal", "message": "Provider refusal: policy"}
            )
        )

        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(acc.provider_terminal_state, "provider_refusal")
        self.assertEqual(acc.terminal_status, "failed")

    def test_pi_error_event_still_records_exactly_one_terminal(self):
        """pi/omp own their error branch; the generic path must not double up."""
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness):
                acc = self.events.StreamAccumulator(harness=harness)
                acc.ingest_line(json.dumps({"type": "error", "message": "provider exploded"}))
                completed = [event for event in acc.events if event.kind == "run.completed"]
                self.assertEqual(len(completed), 1)
                self.assertEqual(acc.terminal_event["event"], f"{harness}.error")

    def test_cursor_effort_labels_match_harness_discovery(self):
        """The duplicated label table must not drift from its source."""
        import delegate_agent.harness_discovery as harness_discovery

        self.assertEqual(
            self.events._CURSOR_EFFORT_LABELS,
            harness_discovery._CURSOR_EFFORT_LABELS,
        )

    def test_pinned_cursor_accepts_the_served_display_name(self):
        """cursor B1: cursor reports a display name, never the requested id."""
        acc = self.events.StreamAccumulator(
            harness="cursor",
            requested_model="composer-2.5",
            continuity_mode="pinned",
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "29e13e2f-2ddd-49c7-a5d5-8b1d6b672db5",
                    "model": "Composer 2.5",
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "result", "subtype": "success", "result": "Done."}))

        self.assertIsNone(acc.continuity_violation)
        self.assertEqual(acc.served_model, "Composer 2.5")
        self.assertEqual(acc.completion_text, "Done.")
        self.assertEqual(acc.session_id, "29e13e2f-2ddd-49c7-a5d5-8b1d6b672db5")

    def test_pinned_cursor_accepts_the_effort_suffixed_catalog_label(self):
        acc = self.events.StreamAccumulator(
            harness="cursor",
            requested_model="cursor-grok-4.6-xhigh",
            continuity_mode="pinned",
        )
        acc.ingest_line(
            json.dumps({"type": "system", "subtype": "init", "model": "Cursor Grok 4.6 Extra High"})
        )
        self.assertIsNone(acc.continuity_violation)

    def test_pinned_cursor_still_rejects_a_different_model(self):
        for served in ("Composer 2.6", "Cursor Grok 4.6 Extra High"):
            with self.subTest(served=served):
                acc = self.events.StreamAccumulator(
                    harness="cursor",
                    requested_model="composer-2.5",
                    continuity_mode="pinned",
                )
                acc.ingest_line(json.dumps({"type": "system", "subtype": "init", "model": served}))
                self.assertIsNotNone(acc.continuity_violation)
                self.assertEqual(acc.terminal_status, "failed")

    def test_pinned_cursor_uses_a_supplied_catalog_display_name(self):
        acc = self.events.StreamAccumulator(
            harness="cursor",
            requested_model="cursor-mystery-1",
            requested_model_display_name="Mystery One",
            continuity_mode="pinned",
        )
        acc.ingest_line(json.dumps({"type": "system", "subtype": "init", "model": "Mystery One"}))
        self.assertIsNone(acc.continuity_violation)

    def test_pinned_claude_accepts_the_dated_served_id_for_an_alias(self):
        """claude L4: every documented alias trips a pinned run today."""
        for requested, served in (
            ("haiku", "claude-haiku-4-5-20251001"),
            ("opus", "claude-opus-5-20260101"),
            ("fable", "claude-fable-5-1-20260601"),
            ("sonnet[1m]", "claude-sonnet-5-20260101"),
            ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
            ("claude-opus-5[1m]", "claude-opus-5"),
        ):
            with self.subTest(requested=requested, served=served):
                acc = self.events.StreamAccumulator(
                    harness="claude",
                    requested_model=requested,
                    continuity_mode="pinned",
                )
                acc.ingest_line(
                    json.dumps(
                        {"type": "system", "subtype": "init", "session_id": "s1", "model": served}
                    )
                )
                self.assertIsNone(acc.continuity_violation)
                self.assertIsNone(acc.terminal_status)

    def test_pinned_claude_rejects_a_different_family_or_an_unmapped_alias(self):
        for requested, served in (
            ("haiku", "claude-opus-5-20260101"),
            ("claude-haiku-4-5", "claude-haiku-4-5-turbo"),
            ("claude-haiku-4-5", "claude-haiku-4-5-2025100"),
            ("best", "claude-opus-5-20260101"),
            ("opusplan", "claude-opus-5-20260101"),
        ):
            with self.subTest(requested=requested, served=served):
                acc = self.events.StreamAccumulator(
                    harness="claude",
                    requested_model=requested,
                    continuity_mode="pinned",
                )
                acc.ingest_line(json.dumps({"type": "system", "subtype": "init", "model": served}))
                self.assertIsNotNone(acc.continuity_violation)
                self.assertEqual(acc.terminal_status, "failed")

    def test_pinned_equivalence_is_not_generic_containment(self):
        """A served id that merely embeds the requested one is a different model."""
        acc = self.events.StreamAccumulator(
            harness="codex",
            requested_model="gpt-5.6",
            continuity_mode="pinned",
        )
        acc.ingest_line(json.dumps({"type": "turn.started", "model": "gpt-5.6-sol-preview"}))
        self.assertIsNotNone(acc.continuity_violation)

    def test_mid_run_model_switch_check_is_unchanged_for_cursor(self):
        acc = self.events.StreamAccumulator(
            harness="cursor",
            requested_model="composer-2.5",
            continuity_mode="pinned",
        )
        acc.ingest_line(json.dumps({"type": "system", "subtype": "init", "model": "Composer 2.5"}))
        self.assertIsNone(acc.continuity_violation)
        acc.ingest_line(json.dumps({"type": "assistant", "model": "Composer 2.6"}))
        self.assertEqual((acc.continuity_violation or {}).get("reason"), "mid_session_model_switch")

    def test_provider_max_turns_is_typed_from_result_metadata(self):
        acc = self.events.StreamAccumulator(harness="claude")

        acc.ingest_line(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "result": "partial",
                }
            )
        )

        self.assertEqual(acc.provider_terminal_state, "provider_max_turns")
        self.assertEqual(acc.terminal_status, "failed")

    def test_model_provenance_records_switch_and_sticky_turn(self):
        acc = self.events.StreamAccumulator(
            harness="codex",
            requested_model="model-a",
            continuity_mode="fungible",
        )

        acc.ingest_line(json.dumps({"type": "turn.started", "model": "model-a"}))
        acc.ingest_line(json.dumps({"type": "turn.started", "model": "model-b"}))

        self.assertEqual(acc.served_model, "model-b")
        self.assertEqual(acc.sticky_model_turn, 2)
        self.assertEqual(
            acc.model_fallback_hops,
            [
                {
                    "fromModel": "model-a",
                    "toModel": "model-b",
                    "reason": "harness_reported_model_switch",
                    "turn": 2,
                    "stickyFromTurn": 2,
                    "observedAt": acc.model_fallback_hops[0]["observedAt"],
                }
            ],
        )
        self.assertIsNone(acc.continuity_violation)

    def test_pinned_model_switch_pauses_while_panel_records_diversity(self):
        pinned = self.events.StreamAccumulator(
            harness="codex",
            requested_model="model-a",
            continuity_mode="pinned",
        )
        panel = self.events.StreamAccumulator(
            harness="codex",
            requested_model="model-a",
            continuity_mode="panel",
        )
        for acc in (pinned, panel):
            acc.ingest_line(json.dumps({"type": "turn.started", "model": "model-a"}))
            acc.ingest_line(json.dumps({"type": "turn.started", "model": "model-b"}))

        self.assertEqual(pinned.terminal_status, "failed")
        self.assertEqual(pinned.continuity_violation["reason"], "mid_session_model_switch")
        self.assertEqual(pinned.continuity_violation["turn"], 2)
        self.assertIsNone(panel.continuity_violation)
        self.assertEqual(panel.served_model, "model-b")

    def test_model_provenance_events_are_bounded_under_oscillation(self):
        acc = self.events.StreamAccumulator(harness="codex", continuity_mode="fungible")
        for turn in range(100):
            acc.ingest_line(
                json.dumps(
                    {
                        "type": "turn.started",
                        "model": "model-a" if turn % 2 == 0 else "model-b",
                    }
                )
            )

        self.assertEqual(len(acc.model_observations), self.events.MODEL_PROVENANCE_EVENT_LIMIT)
        self.assertEqual(len(acc.model_fallback_hops), self.events.MODEL_PROVENANCE_EVENT_LIMIT)
        self.assertEqual(acc.model_observations_total, 100)
        self.assertEqual(acc.model_fallback_hops_total, 99)

    def setUp(self):
        self.events = load_harness_events()

    def ingest_opencode_fixture(self, name: str):
        fixture = ROOT / "tests" / "fixtures" / "opencode" / name
        acc = self.events.StreamAccumulator(harness="opencode")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)
        return acc

    def opencode_text_parts(self, name: str) -> list[str]:
        fixture = ROOT / "tests" / "fixtures" / "opencode" / name
        texts: list[str] = []
        for line in fixture.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            part = payload.get("part")
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return texts

    def test_assistant_message_text_is_captured(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps({"type": "message", "role": "assistant", "content": "Hello parent"})
        )
        self.assertIn("Hello parent", acc.assistant_text)

    def test_kimi_role_content_without_type_is_captured(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"role": "assistant", "content": "OK from Kimi"}))
        self.assertEqual(acc.assistant_text, "OK from Kimi")
        self.assertEqual(acc.recoverable_assistant_text, "OK from Kimi")

    def test_kimi_non_assistant_role_content_without_type_is_ignored(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"role": "user", "content": "prompt echo"}))
        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.recoverable_assistant_text)

    def test_assistant_text_is_cached_until_chunks_change(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps({"type": "message", "role": "assistant", "content": "Hello parent"})
        )
        first = acc.assistant_text
        second = acc.assistant_text
        self.assertIs(first, second)
        acc.ingest_line(
            json.dumps({"type": "message", "role": "assistant", "content": "More detail"})
        )
        third = acc.assistant_text
        self.assertIsNot(first, third)
        self.assertIn("More detail", third)

    def test_reasoning_events_are_ignored(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"type": "reasoning", "content": "hidden chain"}))
        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(acc.events, [])

    def test_tool_result_payloads_are_ignored(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_result",
                    "value": "SECRET OUTPUT FROM COMMAND",
                }
            )
        )
        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(acc.events, [])

    def test_tool_call_metadata_is_normalized(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"type": "tool_call", "tool": "read", "path": "README.md"}))
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "tool.started")
        self.assertEqual(acc.events[0].tool, "read")
        self.assertEqual(acc.events[0].path, "README.md")

    def test_completion_final_text_is_captured(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps({"type": "completion", "finalText": "Status: completed\n- did work"})
        )
        self.assertEqual(acc.completion_text, "Status: completed\n- did work")

    def test_resume_capable_harnesses_capture_native_session_id(self):
        cases = (
            ("codex", {"type": "thread.started", "thread_id": "thread-123"}, "thread-123"),
            (
                "claude",
                {"type": "system", "subtype": "init", "session_id": "claude-123"},
                "claude-123",
            ),
            (
                "cursor",
                {"type": "system", "subtype": "init", "session_id": "cursor-123"},
                "cursor-123",
            ),
            ("omp", {"type": "session", "version": 3, "id": "omp-123"}, "omp-123"),
        )
        for harness, event, expected in cases:
            with self.subTest(harness=harness):
                acc = self.events.StreamAccumulator(harness=harness)
                acc.ingest_line(json.dumps(event))
                self.assertEqual(acc.session_id, expected)

        untrusted = self.events.StreamAccumulator(harness="codex")
        untrusted.ingest_line(
            json.dumps({"type": "thread.started", "thread_id": "--dangerous-flag"})
        )
        self.assertIsNone(untrusted.session_id)

    def test_codex_item_completed_agent_message_is_latest_completion(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I am checking the repo."},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "hidden reasoning"},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Status: completed\n- final answer",
                    },
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        self.assertIn("I am checking the repo.", acc.assistant_text)
        self.assertNotIn("hidden reasoning", acc.assistant_text)
        self.assertEqual(acc.completion_text, "Status: completed\n- final answer")

    def test_codex_command_execution_items_are_normalized(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I am checking the repo."},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "python3 -m unittest",
                        "status": "in_progress",
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "python3 -m unittest",
                        "status": "completed",
                    },
                }
            )
        )
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.events[0].kind, "tool.started")
        self.assertEqual(acc.events[0].status, "in_progress")
        self.assertEqual(acc.events[1].kind, "tool.completed")
        self.assertEqual(acc.events[1].status, "success")

    def test_codex_agent_message_started_does_not_record_text(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "agent_message", "text": "partial"},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "partial"},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        self.assertEqual(acc.assistant_text, "partial")
        self.assertEqual(acc.completion_text, "partial")

    def test_codex_completion_text_survives_turn_start_and_command_only_turn(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "answer one"},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        acc.ingest_line(json.dumps({"type": "turn.started"}))
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "ls",
                        "status": "in_progress",
                    },
                }
            )
        )
        self.assertEqual(acc.completion_text, "answer one")

    def test_codex_progress_message_before_command_is_not_promoted(self):
        # A turn that ends on a command (intro agent_message -> command ->
        # turn.completed) has no closing answer, so nothing is promoted. Promoting
        # the pre-command message would surface an intro line as the final report.
        acc = self.events.StreamAccumulator()
        for payload in [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "I'll start by checking the repo."},
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "git status",
                    "status": "completed",
                },
            },
            {"type": "turn.completed"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertIsNone(acc.completion_text)

    def test_codex_empty_completed_agent_message_clears_completion_candidate(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I'll start by reviewing."},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "   "},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        self.assertIsNone(acc.completion_text)

    def test_real_codex_stream_fixture_matches_parser_assumptions(self):
        # Guards against Codex changing its event schema: the other codex tests
        # use hand-authored JSON, so they would stay green even if the real
        # wire format drifted. This fixture is a sanitized capture of an actual
        # `codex` run (command output blanked, host paths scrubbed); if Codex
        # renames event types or fields, this test fails where synthetic ones
        # would not.
        fixture = ROOT / "tests" / "fixtures" / "codex_real_stream.jsonl"
        acc = self.events.StreamAccumulator()
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)

        # Final agent_message becomes the completion; the intro message and the
        # hidden reasoning item do not.
        self.assertIsNotNone(acc.completion_text)
        self.assertTrue(acc.completion_text.startswith("Verdict:"))
        self.assertIn("skill pass", acc.assistant_text.lower())
        self.assertNotIn("reasoning", acc.assistant_text.lower())

        # command_execution items normalize to tool events, and a real failed
        # command stays "failed" (only "completed" maps to "success").
        tool_events = [(e.kind, e.status) for e in acc.events if e.tool]
        self.assertEqual(
            tool_events,
            [
                ("tool.started", "in_progress"),
                ("tool.completed", "success"),
                ("tool.started", "in_progress"),
                ("tool.completed", "failed"),
            ],
        )

        # turn.completed carries the run's token accounting.
        self.assertEqual(
            acc.usage,
            {
                "basis": "reported",
                "inputTokens": 1597930,
                "outputTokens": 9862,
                "cacheReadTokens": 1404544,
                "cacheWriteTokens": None,
            },
        )

    def test_codex_preamble_is_cleared_by_every_non_message_item(self):
        """codex L3: only command_execution cleared the candidate."""
        for item_type in ("file_change", "mcp_tool_call", "web_search", "todo_list", "reasoning"):
            with self.subTest(item_type=item_type):
                acc = self.events.StreamAccumulator(harness="codex")
                acc.ingest_line(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "I'll start by reading."},
                        }
                    )
                )
                acc.ingest_line(json.dumps({"type": "item.completed", "item": {"type": item_type}}))
                acc.ingest_line(json.dumps({"type": "turn.completed"}))
                self.assertIsNone(acc.completion_text)
                self.assertIn("I'll start by reading.", acc.assistant_text)

    def test_codex_message_sealed_after_the_last_item_is_still_promoted(self):
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(
            json.dumps({"type": "item.completed", "item": {"type": "file_change", "id": "f1"}})
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Status: completed"},
                }
            )
        )
        acc.ingest_line(json.dumps({"type": "turn.completed"}))
        self.assertEqual(acc.completion_text, "Status: completed")

    def test_opencode_simple_text_fixture_sets_completion_from_stop_step(self):
        acc = self.ingest_opencode_fixture("simple_text.ndjson")
        expected = self.opencode_text_parts("simple_text.ndjson")[-1].strip()

        self.assertEqual(expected, "pong")
        self.assertEqual(acc.assistant_text, expected)
        self.assertEqual(acc.completion_text, expected)
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(
            acc.terminal_event,
            {"event": "opencode.step_finish", "status": "succeeded", "reason": "stop"},
        )
        self.assertEqual(acc.assistant_recovery_quality(), "explicit_completion")
        self.assertIn("opencode", self.events.ASSISTANT_RECOVERY_HARNESSES)

    def test_pi_json_fixture_populates_assistant_text_and_completion(self):
        fixture = ROOT / "tests" / "fixtures" / "pi" / "simple_text.jsonl"
        acc = self.events.StreamAccumulator(harness="pi")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)

        self.assertEqual(acc.assistant_text, "PI_OK")
        self.assertEqual(acc.completion_text, "PI_OK")
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(
            acc.terminal_event,
            {"event": "pi.turn_end", "status": "succeeded"},
        )
        self.assertIn("pi", self.events.ASSISTANT_RECOVERY_HARNESSES)

    def test_pi_empty_stop_turn_is_terminal_success(self):
        acc = self.events.StreamAccumulator(harness="pi")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {"role": "assistant", "content": [], "stopReason": "stop"},
                }
            )
        )

        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(
            acc.terminal_event,
            {"event": "pi.turn_end", "status": "succeeded"},
        )

    def test_reasoning_typed_content_is_not_assistant_text(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "hidden chain"},
                        {"type": "text", "text": "visible answer"},
                    ],
                }
            )
        )

        self.assertEqual(acc.assistant_text, "visible answer")

    def test_real_pi_tool_fixture_parses_tool_without_exposing_result(self):
        fixture = ROOT / "tests" / "fixtures" / "pi" / "tool_read.jsonl"
        acc = self.events.StreamAccumulator(harness="pi")
        result_text = None
        for line in fixture.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            if payload.get("type") == "tool_execution_end":
                result = payload.get("result")
                if isinstance(result, dict):
                    content = result.get("content")
                    if isinstance(content, list) and content:
                        result_text = content[0].get("text")
            acc.ingest_line(line)

        self.assertEqual(
            [(event.kind, event.tool, event.target) for event in acc.events if event.tool],
            [
                ("tool.started", "read", "notes.txt"),
                ("tool.completed", "read", None),
            ],
        )
        self.assertIsInstance(result_text, str)
        self.assertNotIn(result_text, acc.assistant_text)
        self.assertNotIn(result_text, json.dumps([event.to_dict() for event in acc.events]))
        self.assertTrue(acc.assistant_text)
        self.assertEqual(acc.completion_text, acc.assistant_text)
        completions = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(
            len(completions), 1, "toolUse turn_end must not record a premature completion"
        )

    def test_pi_non_stop_turn_end_is_not_terminal_success(self):
        for stop_reason, terminal_status in (
            ("error", "failed"),
            ("aborted", "cancelled"),
            ("length", "failed"),
            ("toolUse", None),
        ):
            acc = self.events.StreamAccumulator(harness="pi")
            acc.ingest_line(
                json.dumps(
                    {
                        "type": "turn_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "partial"}],
                            "stopReason": stop_reason,
                        },
                    }
                )
            )
            self.assertEqual(
                acc.terminal_status,
                terminal_status,
                f"stopReason={stop_reason} must not record a succeeded terminal",
            )

    def test_real_omp_fixture_records_observed_stdin_transport_divergence(self):
        fixture = ROOT / "tests" / "fixtures" / "omp" / "simple_text.jsonl"
        payloads = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([payload["type"] for payload in payloads], ["session"])
        self.assertEqual(payloads[0]["id"], "sanitized")
        self.assertEqual(payloads[0]["cwd"], "/tmp/delegate-omp")

        acc = self.events.StreamAccumulator(harness="omp")
        for payload in payloads:
            acc.ingest_line(json.dumps(payload))
        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.completion_text)
        self.assertIsNone(acc.terminal_status)

    def test_omp_stop_turn_populates_assistant_text_and_completes_once(self):
        acc = self.events.StreamAccumulator(harness="omp")
        acc.ingest_line(json.dumps({"type": "turn_start"}))
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "OMP_OK"},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "OMP_OK"}],
                        "stopReason": "stop",
                    },
                }
            )
        )

        self.assertEqual(acc.assistant_text, "OMP_OK")
        self.assertEqual(acc.completion_text, "OMP_OK")
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(acc.terminal_event, {"event": "omp.turn_end", "status": "succeeded"})
        self.assertEqual(len([event for event in acc.events if event.kind == "run.completed"]), 1)
        self.assertIn("omp", self.events.ASSISTANT_RECOVERY_HARNESSES)

    def test_omp_non_stop_turn_end_matrix_is_not_terminal_success(self):
        for stop_reason, terminal_status in (
            ("error", "failed"),
            ("aborted", "cancelled"),
            ("length", "failed"),
            ("toolUse", None),
        ):
            acc = self.events.StreamAccumulator(harness="omp")
            acc.ingest_line(
                json.dumps(
                    {
                        "type": "turn_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "partial"}],
                            "stopReason": stop_reason,
                        },
                    }
                )
            )
            self.assertEqual(acc.terminal_status, terminal_status, stop_reason)
            self.assertEqual(
                len([event for event in acc.events if event.kind == "run.completed"]),
                int(terminal_status is not None),
            )

    def test_pi_tool_result_content_is_not_exposed(self):
        acc = self.events.StreamAccumulator(harness="pi")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "toolName": "read",
                    "args": {"path": "note.txt"},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolName": "read",
                    "args": {"path": "note.txt"},
                    "result": {"content": [{"type": "text", "text": "SECRET"}]},
                    "isError": False,
                }
            )
        )
        self.assertEqual(acc.assistant_text, "")
        self.assertEqual(
            [(event.kind, event.tool, event.path, event.status) for event in acc.events],
            [
                ("tool.started", "read", "note.txt", None),
                ("tool.completed", "read", "note.txt", "success"),
            ],
        )

    def test_opencode_tool_run_fixture_recovers_final_text_not_tool_output(self):
        acc = self.ingest_opencode_fixture("tool_run.ndjson")
        expected = self.opencode_text_parts("tool_run.ndjson")[-1].strip()

        self.assertEqual(expected, "banana42")
        self.assertEqual(acc.assistant_text, expected)
        self.assertEqual(acc.completion_text, expected)
        self.assertNotIn("The secret word is", acc.assistant_text)
        tool_events = [event for event in acc.events if event.tool]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].kind, "tool.completed")
        self.assertEqual(tool_events[0].tool, "read")
        self.assertEqual(tool_events[0].status, "success")
        self.assertTrue((tool_events[0].target or "").endswith("note.txt"))

    def test_opencode_multi_step_text_keeps_only_final_stop_step(self):
        acc = self.events.StreamAccumulator(harness="opencode")
        for line in [
            json.dumps(
                {
                    "type": "step_start",
                    "part": {"type": "step-start"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "thinking about it..."},
                }
            ),
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "state": {"status": "completed", "input": {"filePath": "/tmp/x"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"type": "step-finish", "reason": "tool-calls"},
                }
            ),
            json.dumps(
                {
                    "type": "step_start",
                    "part": {"type": "step-start"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "FINAL ANSWER"},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"type": "step-finish", "reason": "stop"},
                }
            ),
        ]:
            acc.ingest_line(line)

        self.assertEqual(acc.assistant_text, "FINAL ANSWER")
        self.assertEqual(acc.completion_text, "FINAL ANSWER")
        bounded, _meta = acc.bounded_assistant_text()
        self.assertEqual(bounded, "FINAL ANSWER")
        self.assertNotIn("thinking about it", acc.assistant_text)
        self.assertEqual(acc.recoverable_assistant_text, "FINAL ANSWER")

    def test_opencode_truncated_after_second_step_start_recovers_latest_step_only(self):
        acc = self.events.StreamAccumulator(harness="opencode")
        for line in [
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "thinking about it..."},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"type": "step-finish", "reason": "tool-calls"},
                }
            ),
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "FINAL ANSWER"},
                }
            ),
        ]:
            acc.ingest_line(line)

        self.assertEqual(acc.assistant_text, "FINAL ANSWER")
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.recoverable_assistant_text, "FINAL ANSWER")
        self.assertNotIn("thinking about it", acc.assistant_text)
        self.assertNotIn("thinking about it", acc.recoverable_assistant_text or "")

    def test_opencode_error_fixture_records_terminal_failure_detail(self):
        acc = self.ingest_opencode_fixture("error_run.ndjson")

        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.terminal_status, "failed")
        self.assertEqual(
            acc.terminal_event,
            {
                "event": "opencode.error",
                "status": "failed",
                "reason": "UnknownError: Unexpected server error. Check server logs for details.",
            },
        )
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "failed")
        self.assertIn("UnknownError", completed[0].message or "")
        self.assertIn("Unexpected server error", completed[0].message or "")

    def test_opencode_unknown_and_malformed_lines_are_ignored(self):
        acc = self.events.StreamAccumulator(harness="opencode")

        for line in [
            '{"type":"surprise","part":{"type":"text","text":"nope"}}',
            '{"type":"text"}',
            '{"type":"text","part":"bad"}',
            '{"type":"tool_use","part":null}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":7}}',
            "not json at all",
        ]:
            acc.ingest_line(line)

        self.assertEqual(acc.assistant_text, "")
        # Only the last line is malformed; the five structured-but-unmodelled
        # ones are still dropped without an event.
        self.assertEqual([event.kind for event in acc.events], ["stream.malformed"])
        self.assertEqual(acc.malformed_lines, 1)
        self.assertIsNone(acc.completion_text)
        self.assertIsNone(acc.terminal_status)

    def test_invalid_json_falls_back_to_bounded_text_event(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line("not json at all")
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "text")
        self.assertIn("not json", acc.events[0].message or "")

        kimi = self.events.StreamAccumulator(harness="kimi")
        kimi.ingest_line("plain Kimi progress")
        kimi.ingest_line(json.dumps(["non-object Kimi output"]))
        # Recorded as a diagnostic, never as text the run can deliver.
        self.assertEqual([event.kind for event in kimi.events], ["stream.malformed"] * 2)
        self.assertEqual(kimi.malformed_lines, 2)
        self.assertEqual(kimi.assistant_text, "")
        self.assertIsNone(kimi.recoverable_assistant_text)
        self.assertIsNone(kimi.current)

    def test_malformed_lines_are_bounded_counted_and_redacted(self):
        """shared B4: these lines used to vanish with no event and no counter."""
        acc = self.events.StreamAccumulator(harness="opencode")
        for index in range(5):
            acc.ingest_line(f"Error {index}: 401 authentication_error from provider anthropic")

        self.assertEqual(acc.malformed_lines, 5)
        malformed = [event for event in acc.events if event.kind == "stream.malformed"]
        self.assertEqual(len(malformed), self.events.MALFORMED_SAMPLE_LIMIT)
        self.assertEqual(
            [event.message for event in malformed],
            [
                f"Error {index}: 401 authentication_error from provider anthropic"
                for index in range(3)
            ],
        )

    def test_a_malformed_sample_is_bounded_and_redacted(self):
        acc = self.events.StreamAccumulator(harness="pi")
        acc.ingest_line("token sk-ant-api03-" + "A" * 400)
        sample = acc.malformed_samples[0]
        self.assertLessEqual(len(sample), self.events.MALFORMED_SAMPLE_CHARS)
        self.assertNotIn("sk-ant-api03-AAAA", sample)

    def test_a_malformed_line_is_counted_apart_from_parsed_events(self):
        """structured_events_seen decides whether the parser owned stdout; a plain
        line did not come from the parser, so it must not inflate that count."""
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line("kimi: fatal: no credentials configured")
        self.assertEqual(acc.structured_events_seen, 0)
        self.assertEqual(acc.malformed_lines, 1)
        self.assertEqual(acc.assistant_text, "")

    def test_a_parsed_event_beside_a_malformed_line_still_counts_as_structured(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line("kimi: warning: retrying")
        acc.ingest_line(json.dumps({"type": "tool_call", "toolName": "read"}))
        self.assertEqual(acc.structured_events_seen, 1)
        self.assertEqual(acc.malformed_lines, 1)
        self.assertEqual(acc.assistant_text, "")

    def test_a_later_valid_assistant_message_clears_the_textless_state(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line("kimi: warning: retrying")
        acc.ingest_line(
            json.dumps({"role": "assistant", "content": "Status: completed\n- recovered"})
        )
        self.assertEqual(acc.malformed_lines, 1)
        self.assertEqual(acc.assistant_text, "Status: completed\n- recovered")
        self.assertEqual(acc.recoverable_assistant_text, "Status: completed\n- recovered")

    def test_omp_keeps_the_plain_text_fallback(self):
        """omp shares pi's parser but its stdout carries no raw passthrough."""
        acc = self.events.StreamAccumulator(harness="omp")
        acc.ingest_line("omp banner line")
        self.assertEqual([event.kind for event in acc.events], ["text"])
        self.assertEqual(acc.malformed_lines, 0)

    def test_unhandled_event_types_are_counted(self):
        """shared L9: an event that matches no branch shipped in silence."""
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(json.dumps({"type": "stream_error", "message": "gone"}))
        acc.ingest_line(json.dumps({"type": "stream_error", "message": "gone again"}))
        acc.ingest_line(json.dumps({"type": "rate_limit_event"}))

        self.assertEqual(acc.unhandled_event_types, {"stream_error": 2, "rate_limit_event": 1})
        self.assertFalse(acc.unhandled_event_types_truncated)

    def test_handled_and_deliberately_dropped_types_are_not_counted(self):
        acc = self.events.StreamAccumulator(harness="claude")
        for payload in (
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "reasoning", "text": "hidden"},
            {"type": "thought", "data": "hidden"},
            {"type": "tool_result"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "result", "subtype": "success", "result": "done"},
        ):
            acc.ingest_line(json.dumps(payload))
        self.assertEqual(acc.unhandled_event_types, {})

    def test_unhandled_event_types_are_bounded_to_32_distinct(self):
        acc = self.events.StreamAccumulator(harness="claude")
        for index in range(40):
            acc.ingest_line(json.dumps({"type": f"vendor_event_{index}"}))
        self.assertEqual(len(acc.unhandled_event_types), self.events.UNHANDLED_EVENT_TYPE_LIMIT)
        self.assertTrue(acc.unhandled_event_types_truncated)

    def test_an_unhandled_event_type_name_is_bounded_and_control_safe(self):
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(json.dumps({"type": "V" * 200}))
        acc.ingest_line(json.dumps({"type": "bad\u0000type"}))
        self.assertEqual(
            list(acc.unhandled_event_types), ["V" * self.events.UNHANDLED_EVENT_TYPE_CHARS]
        )

    def test_unhandled_event_types_are_counted_for_pi_and_opencode(self):
        pi = self.events.StreamAccumulator(harness="pi")
        pi.ingest_line(json.dumps({"type": "queue_update", "size": 2}))
        pi.ingest_line(json.dumps({"type": "notice", "level": "info", "message": "ok"}))
        self.assertEqual(pi.unhandled_event_types, {"queue_update": 1})

        opencode = self.events.StreamAccumulator(harness="opencode")
        opencode.ingest_line(json.dumps({"type": "session.idle"}))
        opencode.ingest_line(json.dumps({"type": "text", "part": {"type": "reasoning"}}))
        self.assertEqual(opencode.unhandled_event_types, {"session.idle": 1, "text": 1})

    def test_the_real_captures_leave_a_readable_unhandled_tally(self):
        claude = self.events.StreamAccumulator(harness="claude")
        fixture = ROOT / "tests" / "fixtures" / "claude" / "structured_output.jsonl"
        for line in fixture.read_text(encoding="utf-8").splitlines():
            claude.ingest_line(line)
        self.assertEqual(claude.unhandled_event_types, {"rate_limit_event": 1})
        self.assertEqual(claude.completion_text, '{"ok": true}')

        grok = self.events.StreamAccumulator(harness="grok")
        fixture = ROOT / "tests" / "fixtures" / "grok" / "tool_read_multi_response.jsonl"
        for line in fixture.read_text(encoding="utf-8").splitlines():
            grok.ingest_line(line)
        self.assertEqual(grok.unhandled_event_types, {"available_commands": 4})

    def test_deeply_nested_json_line_falls_back_to_text_event(self):
        acc = self.events.StreamAccumulator()
        # Python 3.14's json scanner tolerates ~100k nesting levels before
        # aborting json.loads with RecursionError; older versions trip far
        # shallower, so 300k raises on every supported interpreter.
        acc.ingest_line("[" * 300_000 + "]" * 300_000)
        self.assertEqual(acc.structured_events_seen, 0)
        self.assertEqual(len(acc.events), 1)
        self.assertEqual(acc.events[0].kind, "text")

    def test_devin_json_shaped_line_is_preserved_as_plain_text(self):
        acc = self.events.StreamAccumulator(harness="devin")
        acc.ingest_line("Retrying the request with backoff config:")
        acc.ingest_line('{"retries": 3}')
        acc.ingest_line("Retry succeeded.")
        self.assertEqual(acc.structured_events_seen, 0)
        self.assertIn('{"retries": 3}', acc.assistant_text)
        self.assertIn("Retrying the request with backoff config:", acc.assistant_text)
        self.assertIn("Retry succeeded.", acc.assistant_text)

    def test_devin_multiline_output_preserves_single_newlines(self):
        acc = self.events.StreamAccumulator(harness="devin")
        acc.ingest_line("line one")
        acc.ingest_line("line two")
        acc.ingest_line("line three")
        self.assertEqual(acc.assistant_text, "line one\nline two\nline three")

    def test_claude_assistant_tool_use_blocks_emit_tool_started(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Running git status"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "git status"},
                            },
                        ]
                    },
                }
            )
        )
        tool_events = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].tool, "Bash")
        self.assertEqual(tool_events[0].target, "git status")
        self.assertEqual(acc.current, "Bash git status")
        self.assertIn("Running git status", acc.assistant_text)

    def test_claude_error_result_does_not_emit_success_completion(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "partial output",
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "failed")
        self.assertEqual(acc.terminal_status, "failed")
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.recoverable_assistant_text, "partial output")

    def test_error_result_without_result_text_records_failed_terminal(self):
        """shared B3: a bodiless error result was dropped entirely."""
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "num_turns": 3,
                }
            )
        )

        self.assertEqual(acc.terminal_status, "failed")
        self.assertEqual(
            acc.terminal_event,
            {"event": "result", "status": "failed", "reason": "error_during_execution"},
        )
        self.assertIsNone(acc.completion_text)

    def test_error_max_turns_result_records_exactly_one_terminal(self):
        """The provider-terminal table already owns this subtype; do not double up."""
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(
            json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})
        )

        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(acc.provider_terminal_state, "provider_max_turns")
        self.assertEqual(acc.terminal_status, "failed")

    def test_bodiless_success_result_records_no_terminal(self):
        """A result with neither text nor is_error stays as silent as it was."""
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(json.dumps({"type": "result", "subtype": "success", "is_error": False}))

        self.assertIsNone(acc.terminal_status)
        self.assertEqual([event.kind for event in acc.events], [])

    def test_claude_result_text_prefers_the_documented_structured_field(self):
        """claude S1/L5: `structured_output` is the only documented surface."""
        self.assertEqual(
            self.events.claude_result_text(
                {"result": "stale echo", "structured_output": {"ok": True, "n": 2}}
            ),
            '{"ok": true, "n": 2}',
        )
        self.assertEqual(self.events.claude_result_text({"result": "plain answer"}), "plain answer")
        self.assertEqual(self.events.claude_result_text({"structured_output": [1, 2]}), "[1, 2]")
        self.assertIsNone(self.events.claude_result_text({"result": "   "}))
        # A PRESENT structured_output wins even when null: on a --json-schema
        # run the field is the answer, and null is a representable one.
        self.assertEqual(self.events.claude_result_text({"structured_output": None}), "null")
        self.assertEqual(self.events.claude_result_text({"structured_output": float("nan")}), None)
        self.assertIsNone(self.events.claude_result_text({"is_error": True, "num_turns": 3}))

    def test_claude_result_text_does_not_judge_is_error(self):
        """Extraction and failure detection are separate questions."""
        self.assertEqual(
            self.events.claude_result_text({"is_error": True, "structured_output": {"ok": False}}),
            '{"ok": false}',
        )

    def test_claude_result_event_reads_structured_output_when_result_is_absent(self):
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(
            json.dumps({"type": "result", "subtype": "success", "structured_output": {"ok": True}})
        )
        self.assertEqual(acc.completion_text, '{"ok": true}')
        self.assertEqual(acc.terminal_status, "succeeded")

    def test_claude_real_capture_delivers_the_structured_answer(self):
        fixture = ROOT / "tests" / "fixtures" / "claude" / "structured_output.jsonl"
        acc = self.events.StreamAccumulator(harness="claude")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)

        # The vendor echoes `{"ok":true}` into `result`; delegate delivers the
        # documented `structured_output` field, so the spacing differs and the
        # parsed value does not.
        self.assertEqual(acc.completion_text, '{"ok": true}')
        self.assertEqual(json.loads(acc.completion_text), {"ok": True})
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(acc.session_id, "226e1bf3-7bd8-43c7-8958-89966432300c")
        self.assertEqual(acc.harness_session_id, "226e1bf3-7bd8-43c7-8958-89966432300c")

    def test_claude_success_result_still_emits_success_completion(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "Status: completed\n- done",
                    "usage": {"input_tokens": 21, "output_tokens": 8},
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "succeeded")
        self.assertEqual(acc.completion_text, "Status: completed\n- done")
        self.assertIsNone(acc.usage)

    def test_result_usage_accepts_snake_case_and_keeps_missing_fields_null(self):
        acc = self.events.StreamAccumulator(harness="cursor")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "result",
                    "result": "done",
                    "usage": {"input_tokens": 21, "output_tokens": 8},
                }
            )
        )

        self.assertEqual(
            acc.usage,
            {
                "basis": "reported",
                "inputTokens": 21,
                "outputTokens": 8,
                "cacheReadTokens": None,
                "cacheWriteTokens": None,
            },
        )

    def test_grok_cancelled_stop_reason_sets_terminal_cancelled(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "partial report"}))
        acc.ingest_line(json.dumps({"type": "end", "stopReason": "Cancelled"}))
        self.assertEqual(acc.terminal_status, "cancelled")
        self.assertEqual(
            acc.terminal_event, {"event": "grok.end", "status": "cancelled", "reason": "Cancelled"}
        )
        self.assertIsNone(acc.completion_text)
        self.assertEqual(acc.recoverable_assistant_text, "partial report")

    def test_grok_truncation_stop_reasons_are_incomplete_terminals(self):
        """grok L1: max_tokens and max_turn_requests were silent successes."""
        for stop_reason in ("max_tokens", "MaxTokens", "max_turn_requests"):
            with self.subTest(stop_reason=stop_reason):
                acc = self.events.StreamAccumulator(harness="grok")
                acc.ingest_line(json.dumps({"type": "text", "data": "partial report"}))
                acc.ingest_line(json.dumps({"type": "end", "stopReason": stop_reason}))
                self.assertEqual(acc.terminal_status, "failed")
                self.assertIsNone(acc.completion_text)
                self.assertEqual(acc.recoverable_assistant_text, "partial report")
                completed = [event for event in acc.events if event.kind == "run.completed"]
                self.assertEqual(len(completed), 1)

    def test_grok_max_turn_requests_is_typed_as_provider_max_turns(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "end", "stopReason": "max_turn_requests"}))
        self.assertEqual(acc.provider_terminal_state, "provider_max_turns")

    def test_grok_max_tokens_is_typed_like_the_other_truncations(self):
        """Without a provider state the run record carries no failureReason."""
        for stop_reason in ("max_tokens", "MaxTokens"):
            with self.subTest(stop_reason=stop_reason):
                acc = self.events.StreamAccumulator(harness="grok")
                acc.ingest_line(json.dumps({"type": "text", "data": "partial report"}))
                acc.ingest_line(json.dumps({"type": "end", "stopReason": stop_reason}))
                self.assertEqual(acc.provider_terminal_state, "provider_max_turns")
                self.assertEqual(acc.terminal_status, "failed")
                self.assertIsNone(acc.completion_text)
                self.assertEqual(acc.recoverable_assistant_text, "partial report")

    def test_grok_end_turn_is_not_typed_as_a_truncation(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "the answer"}))
        acc.ingest_line(json.dumps({"type": "end", "stopReason": "end_turn"}))
        self.assertIsNone(acc.provider_terminal_state)
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(acc.completion_text, "the answer")

    def test_grok_max_turns_reached_event_is_a_terminal(self):
        """grok L4: the second, independent truncation signal was discarded."""
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "partial report"}))
        acc.ingest_line(json.dumps({"type": "max_turns_reached", "limit": 20}))
        self.assertEqual(acc.provider_terminal_state, "provider_max_turns")
        self.assertEqual(acc.terminal_status, "failed")

    def test_codex_explicit_terminal_error_sets_failed(self):
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(json.dumps({"type": "turn.failed"}))
        self.assertEqual(acc.terminal_status, "failed")

    def test_claude_tool_result_emits_tool_completed_correlated_by_id(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_01abc",
                                "name": "Bash",
                                "input": {"command": "echo hello"},
                            }
                        ]
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01abc",
                                "content": "hello",
                                "is_error": False,
                            }
                        ]
                    },
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].tool, "Bash")
        self.assertEqual(completed[0].target, "echo hello")
        self.assertEqual(completed[0].status, "success")
        self.assertEqual(acc.current, "Bash echo hello")

    def test_claude_tool_result_error_sets_error_status(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_02def",
                                "name": "Bash",
                                "input": {"command": "false"},
                            }
                        ]
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_02def",
                                "content": "boom",
                                "is_error": True,
                            }
                        ]
                    },
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "error")

    def test_claude_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_03ghi",
                                "content": "SECRET COMMAND OUTPUT",
                                "is_error": False,
                            }
                        ]
                    },
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].tool, "tool")
        self.assertIsNone(completed[0].target)
        self.assertNotIn("SECRET COMMAND OUTPUT", acc.assistant_text)
        self.assertFalse(
            any("SECRET COMMAND OUTPUT" in (event.message or "") for event in acc.events)
        )

    def test_claude_parallel_tool_results_each_complete(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_a",
                                "name": "Read",
                                "input": {"file_path": "a.py"},
                            },
                            {
                                "type": "tool_use",
                                "id": "toolu_b",
                                "name": "Read",
                                "input": {"file_path": "b.py"},
                            },
                        ]
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_a",
                                "content": "...",
                                "is_error": False,
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_b",
                                "content": "...",
                                "is_error": False,
                            },
                        ]
                    },
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual({event.target for event in completed}, {"a.py", "b.py"})

    def test_kimi_tool_call_emits_started_and_completed_correlated_by_id(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_01abc",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"file_path": "README.md"}),
                            },
                        }
                    ],
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "role": "tool",
                    "tool_call_id": "call_01abc",
                    "content": "file contents",
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(started[0].tool, "Read")
        self.assertEqual(started[0].target, "README.md")
        self.assertEqual(completed[0].tool, "Read")
        self.assertEqual(completed[0].target, "README.md")
        # kimi 0.26.0 results carry no success/error signal; status stays unknown.
        self.assertIsNone(completed[0].status)
        self.assertNotIn("status", completed[0].to_dict())
        self.assertEqual(acc.current, "Read README.md")

    def test_kimi_parallel_tool_calls_correlate_by_id(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_a",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"file_path": "a.py"}),
                            },
                        },
                        {
                            "type": "function",
                            "id": "call_b",
                            "function": {
                                "name": "Bash",
                                "arguments": json.dumps({"command": "echo hi"}),
                            },
                        },
                    ],
                }
            )
        )
        # Results may arrive in any order; correlation is by tool_call_id.
        acc.ingest_line(json.dumps({"role": "tool", "tool_call_id": "call_b", "content": "hi"}))
        acc.ingest_line(json.dumps({"role": "tool", "tool_call_id": "call_a", "content": "..."}))
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 2)
        self.assertEqual(len(completed), 2)
        self.assertEqual((completed[0].tool, completed[0].target), ("Bash", "echo hi"))
        self.assertEqual((completed[1].tool, completed[1].target), ("Read", "a.py"))

    def test_kimi_tool_result_with_unmatched_id_is_graceful(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "tool",
                    "tool_call_id": "call_unknown",
                    "content": "orphaned output",
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].tool, "tool")
        self.assertIsNone(completed[0].target)
        self.assertIsNone(completed[0].status)

    def test_kimi_malformed_tool_calls_do_not_crash(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(json.dumps({"role": "assistant", "tool_calls": "not-a-list"}))
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        "not-a-dict",
                        {"type": "function", "id": "call_no_function"},
                        {
                            "type": "function",
                            "id": "call_bad_args",
                            "function": {"name": "Read", "arguments": "{not json"},
                        },
                        {
                            "type": "function",
                            "id": "call_no_name",
                            "function": {"arguments": json.dumps({"path": "x.py"})},
                        },
                    ],
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(started), 2)
        self.assertEqual((started[0].tool, started[0].target), ("Read", None))
        self.assertEqual((started[1].tool, started[1].target), ("tool", "x.py"))
        # Malformed-arguments entries still correlate their results by id.
        acc.ingest_line(
            json.dumps({"role": "tool", "tool_call_id": "call_bad_args", "content": "..."})
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0].tool, completed[0].target), ("Read", None))

    def test_kimi_deeply_nested_tool_arguments_do_not_crash(self):
        # json.loads raises RecursionError (not JSONDecodeError) on excessively
        # nested input; argument parsing must never let an exception escape
        # onto the runner's stdout drain thread.
        acc = self.events.StreamAccumulator(harness="kimi")
        nested_arguments = "[" * 5000 + "]" * 5000
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_nested",
                            "function": {"name": "Read", "arguments": nested_arguments},
                        }
                    ],
                }
            )
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(started), 1)
        self.assertEqual((started[0].tool, started[0].target), ("Read", None))
        # The unparseable-arguments entry still correlates its result by id.
        acc.ingest_line(
            json.dumps({"role": "tool", "tool_call_id": "call_nested", "content": "..."})
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0].tool, completed[0].target), ("Read", None))

    def test_kimi_combined_content_and_tool_calls_ends_current_on_tool(self):
        # A single assistant envelope carrying both prose and tool_calls must
        # leave `current` on the active tool, not on the stale assistant text.
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "content": "I'll read the file first.",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_read1",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"file_path": "src/main.py"}),
                            },
                        }
                    ],
                }
            )
        )
        self.assertEqual(acc.current, "Read src/main.py")
        self.assertIn("I'll read the file first.", acc.assistant_text)
        started = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual(len(started), 1)
        self.assertEqual((started[0].tool, started[0].target), ("Read", "src/main.py"))

    def test_kimi_meta_session_resume_hint_is_dropped(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "meta",
                    "type": "session.resume_hint",
                    "session_id": "sess_123",
                    "command": "kimi --resume sess_123",
                    "content": "Resume this session with: kimi --resume sess_123",
                }
            )
        )
        self.assertEqual(acc.events, [])
        self.assertEqual(acc.assistant_text, "")
        self.assertIsNone(acc.recoverable_assistant_text)

    def test_kimi_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "call_secret",
                            "function": {
                                "name": "Bash",
                                "arguments": json.dumps({"command": "cat secret.txt"}),
                            },
                        }
                    ],
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "role": "tool",
                    "tool_call_id": "call_secret",
                    "content": "SECRET COMMAND OUTPUT",
                }
            )
        )
        self.assertNotIn("SECRET COMMAND OUTPUT", acc.assistant_text)
        self.assertFalse(
            any("SECRET COMMAND OUTPUT" in (event.message or "") for event in acc.events)
        )
        self.assertFalse(
            any("SECRET COMMAND OUTPUT" in (event.target or "") for event in acc.events)
        )
        self.assertNotIn("SECRET COMMAND OUTPUT", acc.current or "")

    def test_kimi_deeply_nested_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        secret = "SECRET COMMAND OUTPUT"
        line = (
            '{"role":"tool","content":"'
            + secret
            + '","nested":'
            + "[" * 300_000
            + "0"
            + "]" * 300_000
            + "}"
        )

        acc.ingest_line(line)

        self.assertEqual([event.kind for event in acc.events], ["stream.malformed"])
        self.assertIsNone(acc.current)
        self.assertNotIn(secret, acc.assistant_text)
        self.assertIsNone(acc.recoverable_assistant_text)
        self.assertIsNone(acc.completion_text)

    def test_kimi_truncated_tool_result_content_does_not_leak(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        secret = "SECRET COMMAND OUTPUT"

        acc.ingest_line('{"role":"tool","content":"' + secret)

        self.assertEqual([event.kind for event in acc.events], ["stream.malformed"])
        self.assertIsNone(acc.current)
        self.assertNotIn(secret, acc.assistant_text)
        self.assertIsNone(acc.recoverable_assistant_text)
        self.assertIsNone(acc.completion_text)

    def test_kimi_0260_stream_vocabulary_regression(self):
        # Regression coverage of the exact line vocabulary live-captured from
        # kimi 0.26.0 `--output-format stream-json`: assistant content,
        # assistant tool_calls, role=tool results, and meta session.resume_hint.
        # A static fixture cannot detect future upstream drift; it pins the
        # shapes observed on 2026-07-16.
        lines = [
            {"role": "assistant", "content": "I'll read the file first."},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "call_read1",
                        "function": {
                            "name": "Read",
                            "arguments": json.dumps({"file_path": "src/main.py"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_read1",
                "content": "def main(): ...",
            },
            {
                "role": "meta",
                "type": "session.resume_hint",
                "session_id": "sess_abc",
                "command": "kimi --resume sess_abc",
                "content": "Resume this session with: kimi --resume sess_abc",
            },
            {"role": "assistant", "content": "Status: completed\n- read the file"},
        ]
        acc = self.events.StreamAccumulator(harness="kimi")
        for line in lines:
            acc.ingest_line(json.dumps(line))
        started = [event for event in acc.events if event.kind == "tool.started"]
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual((started[0].tool, started[0].target), ("Read", "src/main.py"))
        self.assertEqual((completed[0].tool, completed[0].target), ("Read", "src/main.py"))
        self.assertIsNone(completed[0].status)
        self.assertIn("I'll read the file first.", acc.assistant_text)
        self.assertIn("Status: completed", acc.assistant_text)
        self.assertNotIn("sess_abc", acc.assistant_text)
        self.assertFalse(any(event.kind == "text" for event in acc.events))

    def test_substantive_assistant_text_preferred_over_later_housekeeping(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Status: completed\n- fixed parser\n- added tests",
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Plan is up-to-date.",
                }
            )
        )
        self.assertIn("fixed parser", acc.recoverable_assistant_text or "")
        self.assertNotIn("Plan is up-to-date", acc.recoverable_assistant_text or "")
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")

    def test_substantive_assistant_text_preferred_over_longer_progress_message(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Status: completed\n- shipped the fix",
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": (
                        "I am still investigating the repository layout, reading files, "
                        "and running additional checks before I can finalize anything."
                    ),
                }
            )
        )
        self.assertIn("shipped the fix", acc.recoverable_assistant_text or "")
        self.assertNotIn("still investigating", acc.recoverable_assistant_text or "")
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")

    def test_housekeeping_only_assistant_text_is_still_recoverable(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "The final report was delivered in the previous message.",
                }
            )
        )
        self.assertEqual(
            acc.recoverable_assistant_text,
            "The final report was delivered in the previous message.",
        )
        self.assertEqual(acc.assistant_recovery_quality(), "housekeeping_fallback")

    def test_plaintext_fallback_current_advances_on_each_line(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line("first plaintext progress")
        acc.ingest_line("second plaintext progress")
        self.assertEqual(acc.current, "second plaintext progress")

    def test_bounded_event_text_helper_and_fallback_metadata(self):
        short = "short event text"
        bounded, truncated, original = self.events.bounded_event_text(short)
        self.assertEqual(bounded, short)
        self.assertFalse(truncated)
        self.assertEqual(original, len(short))

        long = "x" * (self.events.EVENT_TEXT_LIMIT + 40)
        bounded, truncated, original = self.events.bounded_event_text(long)
        self.assertTrue(truncated)
        self.assertEqual(original, len(long))
        self.assertEqual(bounded, long[: self.events.EVENT_TEXT_LIMIT - 1] + "…")
        self.assertEqual(len(bounded), self.events.EVENT_TEXT_LIMIT)

        acc = self.events.StreamAccumulator()
        acc.ingest_line(short)
        short_event = acc.events[-1].to_dict()
        self.assertEqual(short_event["kind"], "text")
        self.assertEqual(short_event["message"], short)
        self.assertNotIn("truncated", short_event)
        self.assertNotIn("textChars", short_event)

        acc.ingest_line(long)
        long_event = acc.events[-1].to_dict()
        self.assertEqual(long_event["kind"], "text")
        self.assertTrue(long_event["truncated"])
        self.assertEqual(long_event["textChars"], len(long))
        self.assertEqual(long_event["message"], long[: self.events.EVENT_TEXT_LIMIT - 1] + "…")
        recent, _meta = acc.bounded_recent_events()
        self.assertEqual(recent[-1], long_event)

    def test_error_and_terminal_messages_bound_only_at_serialization(self):
        long_error = "E" * 5000
        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"type": "error", "message": long_error}))
        self.assertEqual(acc._last_error_message, long_error)
        error_event = next(event for event in acc.events if event.kind == "error")
        self.assertEqual(error_event.message, long_error)
        error_payload = error_event.to_dict()
        self.assertTrue(error_payload["truncated"])
        self.assertEqual(error_payload["textChars"], 5000)
        self.assertTrue(error_payload["message"].endswith("…"))
        self.assertEqual(len(error_payload["message"]), self.events.EVENT_TEXT_LIMIT)
        recent, _meta = acc.bounded_recent_events()
        recent_error = next(event for event in recent if event["kind"] == "error")
        self.assertEqual(recent_error["message"], error_payload["message"])
        self.assertTrue(recent_error["truncated"])

        short = "short error"
        short_acc = self.events.StreamAccumulator()
        short_acc.ingest_line(json.dumps({"type": "error", "message": short}))
        short_payload = next(event for event in short_acc.events if event.kind == "error").to_dict()
        self.assertEqual(short_payload["message"], short)
        self.assertNotIn("truncated", short_payload)
        self.assertNotIn("textChars", short_payload)

        long_reason = "R" * 5000
        terminal_acc = self.events.StreamAccumulator()
        terminal_acc._record_terminal_event(
            event="turn.failed", status="failed", reason=long_reason
        )
        completed = [e for e in terminal_acc.events if e.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].message, long_reason)
        self.assertEqual(
            terminal_acc.terminal_event["reason"],
            long_reason[: self.events.EVENT_TEXT_LIMIT - 1] + "…",
        )
        self.assertEqual(terminal_acc.terminal_event["reasonChars"], len(long_reason))
        completed_payload = completed[0].to_dict()
        self.assertTrue(completed_payload["truncated"])
        self.assertEqual(completed_payload["textChars"], 5000)
        self.assertTrue(completed_payload["message"].endswith("…"))
        self.assertEqual(len(completed_payload["message"]), self.events.EVENT_TEXT_LIMIT)

    def test_event_buffer_retains_head_and_tail_with_exact_total(self):
        acc = self.events.StreamAccumulator()
        total = self.events.EVENT_LIMIT + 25
        for index in range(total):
            acc.events.append(self.events.NormalizedEvent(kind=f"event-{index}"))

        recent, meta = acc.bounded_recent_events()

        self.assertEqual(len(acc.events), self.events.EVENT_LIMIT)
        self.assertEqual(meta["eventsTotal"], total)
        self.assertTrue(meta["eventsTruncated"])
        self.assertEqual(meta["eventsOmittedMiddle"], 25)
        self.assertEqual(recent[0]["kind"], "event-0")
        self.assertEqual(recent[self.events.EVENT_HEAD - 1]["kind"], "event-99")
        self.assertEqual(recent[self.events.EVENT_HEAD]["kind"], "event-125")
        self.assertEqual(recent[-1]["kind"], f"event-{total - 1}")

        acc.events.append(self.events.NormalizedEvent(kind="tool.started", tool="shell"))
        for index in range(self.events.EVENT_TAIL + 1):
            acc.events.append(self.events.NormalizedEvent(kind=f"tail-{index}"))
        self.assertIn("tool.started", acc.events.last_by_kind)
        self.assertFalse(any(event.kind == "tool.started" for event in acc.events))

    def test_normalized_event_bounds_tool_target_and_path(self):
        target = "x" * 1500
        payload = self.events.NormalizedEvent(
            kind="tool.started", tool=target, target=target, path=target
        ).to_dict()

        for key in ("tool", "target", "path"):
            self.assertEqual(len(payload[key]), self.events.EVENT_TEXT_LIMIT)
            self.assertTrue(payload[key].endswith("…"))
            self.assertEqual(payload[f"{key}Chars"], len(target))
        self.assertTrue(payload["truncated"])

    def test_explicit_completion_wins_over_assistant_recovery_quality(self):
        acc = self.events.StreamAccumulator()
        acc.ingest_line(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Status: completed\n- interim report",
                }
            )
        )
        acc.ingest_line(
            json.dumps({"type": "completion", "finalText": "Status: completed\n- explicit"})
        )
        self.assertEqual(acc.completion_text, "Status: completed\n- explicit")
        self.assertEqual(acc.assistant_recovery_quality(), "explicit_completion")

    def test_classify_substantive_and_housekeeping_helpers(self):
        self.assertTrue(self.events.is_substantive_assistant_text("Status: completed\n- did work"))
        self.assertTrue(self.events.is_housekeeping_assistant_text("Plan is up-to-date."))
        self.assertTrue(self.events.is_housekeeping_assistant_text("I am still investigating."))
        self.assertTrue(
            self.events.is_housekeeping_assistant_text(
                "I'm still investigating.\n- checking files\n- running tests"
            )
        )
        self.assertFalse(
            self.events.is_substantive_assistant_text(
                "I'm still investigating.\n- checking files\n- running tests"
            )
        )
        self.assertTrue(
            self.events.is_housekeeping_assistant_text(
                "I\u2019m still investigating.\n- checking files\n- running tests"
            )
        )
        self.assertFalse(
            self.events.is_substantive_assistant_text(
                "The final report was delivered in the previous message."
            )
        )

    def test_structured_report_with_interior_progress_line_is_substantive(self):
        report = (
            "## Summary\n"
            "- Fixed the recovery classifier.\n"
            "- Added regression coverage.\n\n"
            "Let me check the failing case below.\n\n"
            "## Verification\n"
            "- python3 -m unittest tests.test_harness_events"
        )
        self.assertTrue(self.events.is_substantive_assistant_text(report))
        self.assertFalse(self.events.is_housekeeping_assistant_text(report))

        acc = self.events.StreamAccumulator()
        acc.ingest_line(json.dumps({"type": "message", "role": "assistant", "content": report}))
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")

    def test_grok_streaming_fixture_populates_assistant_and_completion(self):
        fixture = ROOT / "tests" / "fixtures" / "grok_streaming_json_smoke.jsonl"
        acc = self.events.StreamAccumulator(harness="grok")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)
        self.assertIn("delegate grok fixture ok", acc.assistant_text)
        self.assertIn("delegate grok fixture ok", acc.completion_text or "")
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "succeeded")

    def test_grok_unended_text_buffer_is_recoverable_substantive(self):
        parts = [
            "Status: completed\n",
            "- Restored streamed Grok recovery.\n",
            "- Added regression coverage before terminal end.\n",
            "\nVerification:\n",
            "- python3 -m unittest tests.test_harness_events\n",
        ]
        acc = self.events.StreamAccumulator(harness="grok")
        for part in parts:
            acc.ingest_line(json.dumps({"type": "text", "data": part}))

        self.assertEqual(acc.recoverable_assistant_text, "".join(parts).strip())
        self.assertEqual(acc.assistant_recovery_quality(), "substantive_assistant_fallback")
        self.assertIsNone(acc.completion_text)

    def test_grok_current_advances_before_end(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "first line\nsecond"}))
        self.assertEqual(acc.current, "second")

        acc.ingest_line(json.dumps({"type": "text", "data": " line"}))
        self.assertEqual(acc.current, "second line")

        long_line = "x" * 121
        acc.ingest_line(json.dumps({"type": "text", "data": f"\n{long_line}"}))
        self.assertEqual(acc.current, "x" * 120 + "…")
        self.assertIsNone(acc.completion_text)

    def test_grok_live_text_chunks_ignore_thought_and_finalize_on_end(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "thought", "data": "hidden reasoning"}))
        acc.ingest_line(json.dumps({"type": "text", "data": "delegate"}))
        acc.ingest_line(json.dumps({"type": "text", "data": " grok"}))
        acc.ingest_line(
            json.dumps(
                {
                    "type": "end",
                    "stopReason": "EndTurn",
                    "sessionId": "sess",
                    "requestId": "req",
                }
            )
        )
        self.assertEqual(acc.assistant_text, "delegate grok")
        self.assertEqual(acc.completion_text, "delegate grok")
        self.assertNotIn("hidden reasoning", acc.assistant_text)

    def test_grok_maxtokens_end_keeps_text_recoverable_without_success_completion(self):
        fixture = ROOT / "tests" / "fixtures" / "grok_streaming_maxtokens.jsonl"
        acc = self.events.StreamAccumulator(harness="grok")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)
        self.assertIn("delegate grok fixture ok", acc.assistant_text)
        self.assertIn("delegate grok fixture ok", acc.recoverable_assistant_text or "")
        self.assertIsNone(acc.completion_text)
        self.assertFalse(
            any(
                event.kind == "run.completed" and event.status == "succeeded"
                for event in acc.events
            )
        )

    def test_grok_error_event_surfaces_message_without_success_completion(self):
        acc = self.events.StreamAccumulator(harness="grok")
        for payload in [
            {"type": "text", "data": "partial answer before failure"},
            {"type": "error", "message": "Couldn't start session: upstream 503"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertIsNone(acc.completion_text)
        recoverable = acc.recoverable_assistant_text or ""
        self.assertIn("Couldn't start session: upstream 503", recoverable + acc.assistant_text)
        self.assertFalse(
            any(
                event.kind == "run.completed" and event.status == "succeeded"
                for event in acc.events
            )
        )

    def test_grok_usage_boundary_keeps_the_preamble_out_of_the_answer(self):
        """grok B3: `usage`, not `end`, is the per-response boundary in 1.0.13."""
        acc = self.events.StreamAccumulator(harness="grok")
        for payload in [
            {"type": "text", "data": "I'll inspect the repo first."},
            {"type": "usage", "usage": {"input_tokens": 12, "output_tokens": 8}},
            {
                "type": "tool_call",
                "toolCallId": "call-1",
                "toolName": "read_file",
                "rawInput": {"target_file": "README.md"},
            },
            {"type": "tool_call_update", "toolCallId": "call-1", "status": "completed"},
            {"type": "text", "data": "Status: completed\n- final answer"},
            {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 9}},
            {"type": "end", "stopReason": "end_turn"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertEqual(acc.completion_text, "Status: completed\n- final answer")
        self.assertEqual(acc.assistant_text, "Status: completed\n- final answer")
        completed = [event for event in acc.events if event.kind == "run.completed"]
        self.assertEqual(len(completed), 1)

    def test_grok_single_response_usage_before_end_still_delivers_text(self):
        """The seal must not swallow a stream whose only `usage` precedes `end`."""
        acc = self.events.StreamAccumulator(harness="grok")
        for payload in [
            {"type": "text", "data": "the only answer"},
            {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 2}},
            {"type": "end", "stopReason": "end_turn"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertEqual(acc.completion_text, "the only answer")

    def test_grok_sealed_response_is_visible_before_end(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(json.dumps({"type": "text", "data": "sealed answer"}))
        acc.ingest_line(json.dumps({"type": "usage", "usage": {"input_tokens": 1}}))
        self.assertEqual(acc.assistant_text, "sealed answer")
        self.assertEqual(acc.recoverable_assistant_text, "sealed answer")

    def test_grok_usage_with_no_new_text_keeps_the_previous_response(self):
        acc = self.events.StreamAccumulator(harness="grok")
        for payload in [
            {"type": "text", "data": "the answer"},
            {"type": "usage", "usage": {"input_tokens": 1}},
            {"type": "usage", "usage": {"input_tokens": 2}},
            {"type": "end", "stopReason": "end_turn"},
        ]:
            acc.ingest_line(json.dumps(payload))
        self.assertEqual(acc.completion_text, "the answer")

    def test_grok_real_capture_delivers_only_the_final_response(self):
        fixture = ROOT / "tests" / "fixtures" / "grok" / "tool_read_multi_response.jsonl"
        acc = self.events.StreamAccumulator(harness="grok")
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)

        self.assertEqual(acc.completion_text, "ZQ-1147")
        self.assertNotIn("I'll read", acc.assistant_text)
        self.assertEqual(acc.terminal_status, "succeeded")
        self.assertEqual(acc.session_id, "01a07a75-f34b-7e70-9f82-1ca6f7161365")
        self.assertEqual(
            acc.usage,
            {
                "basis": "reported",
                "inputTokens": 26387,
                "outputTokens": 172,
                "cacheReadTokens": 37632,
                "cacheWriteTokens": 0,
                "costUsd": 0.01234574,
            },
        )
        tool_events = [event for event in acc.events if event.kind.startswith("tool.")]
        self.assertEqual(
            [(event.kind, event.tool, event.target, event.status) for event in tool_events],
            [
                ("tool.started", "read_file", "marker.txt", None),
                ("tool.completed", "read_file", "marker.txt", "success"),
            ],
        )

    def test_grok_tool_update_without_a_status_does_not_complete_the_tool(self):
        """The first update carries only `locations`; the tool is still running."""
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_call",
                    "toolCallId": "call-1",
                    "toolName": "read_file",
                    "rawInput": {"target_file": "marker.txt"},
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_call_update",
                    "toolCallId": "call-1",
                    "status": None,
                    "locations": [{"path": "marker.txt"}],
                }
            )
        )
        self.assertEqual([event.kind for event in acc.events], ["tool.started"])

    def test_grok_failed_tool_update_is_not_reported_as_success(self):
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(
            json.dumps({"type": "tool_call", "toolCallId": "call-1", "toolName": "read_file"})
        )
        acc.ingest_line(
            json.dumps({"type": "tool_call_update", "toolCallId": "call-1", "status": "failed"})
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual([event.status for event in completed], ["failed"])

    def test_cursor_tool_events_are_named_and_targeted_from_the_real_shape(self):
        """cursor B2: the type is dotless and the tool is named by its key."""
        acc = self.events.StreamAccumulator(harness="cursor")
        fixture = ROOT / "tests" / "fixtures" / "cursor" / "tool_read.jsonl"
        for line in fixture.read_text(encoding="utf-8").splitlines():
            acc.ingest_line(line)

        tool_events = [event for event in acc.events if event.kind.startswith("tool.")]
        self.assertEqual(
            [(event.kind, event.tool, event.target, event.status) for event in tool_events],
            [
                ("tool.started", "read", "/var/tmp/lane-E-cap/marker.txt", None),
                ("tool.completed", "read", "/var/tmp/lane-E-cap/marker.txt", "success"),
            ],
        )
        self.assertEqual(acc.completion_text, "ZQ-1147")
        self.assertEqual(
            acc.usage,
            {
                "basis": "reported",
                "inputTokens": 9854,
                "outputTokens": 114,
                "cacheReadTokens": 25047,
                "cacheWriteTokens": 0,
            },
        )

    def test_cursor_tool_completion_does_not_invent_a_success(self):
        acc = self.events.StreamAccumulator(harness="cursor")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "c1",
                    "tool_call": {
                        "shellToolCall": {"args": {"command": "pytest"}},
                        "toolCallId": "c1",
                    },
                }
            )
        )
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "c1",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": "pytest"},
                            "result": {"error": {"message": "exit 1"}},
                        },
                        "toolCallId": "c1",
                    },
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual(
            [(e.tool, e.target, e.status) for e in completed], [("shell", "pytest", "error")]
        )

    def test_cursor_tool_completion_with_an_unreadable_result_has_no_status(self):
        acc = self.events.StreamAccumulator(harness="cursor")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "c1",
                    "tool_call": {"readToolCall": {"args": {"path": "a.txt"}}, "toolCallId": "c1"},
                }
            )
        )
        completed = [event for event in acc.events if event.kind == "tool.completed"]
        self.assertEqual([e.status for e in completed], [None])

    def test_cursor_tool_call_without_a_subtype_is_a_start(self):
        acc = self.events.StreamAccumulator(harness="cursor")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "tool_call",
                    "call_id": "c1",
                    "tool_call": {"readToolCall": {"args": {"path": "a.txt"}}},
                }
            )
        )
        self.assertEqual([event.kind for event in acc.events], ["tool.started"])

    def test_non_cursor_tool_call_keeps_the_flat_shape(self):
        """The generic flat `tool_call` used by grok and droid is untouched."""
        acc = self.events.StreamAccumulator(harness="grok")
        acc.ingest_line(
            json.dumps({"type": "tool_call", "tool": "Bash", "args": {"command": "git status"}})
        )
        started = [event for event in acc.events if event.kind == "tool.started"]
        self.assertEqual([(e.tool, e.target) for e in started], [("Bash", "git status")])

    def test_top_level_grok_shapes_are_ignored_for_non_grok_harnesses(self):
        acc = self.events.StreamAccumulator(harness="cursor")
        acc.ingest_line(json.dumps({"type": "text", "data": "x"}))
        acc.ingest_line(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        self.assertEqual(acc.assistant_text, "")
        self.assertFalse(any(event.kind == "run.completed" for event in acc.events))

        grok = self.events.StreamAccumulator(harness="grok")
        grok.ingest_line(json.dumps({"type": "text", "data": "x"}))
        grok.ingest_line(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        self.assertEqual(grok.assistant_text, "x")
        self.assertEqual(grok.completion_text, "x")

    def test_kimi_goal_summary_is_preserved_as_a_terminal_reason(self):
        """kimi L2: a paused goal reported a bare failure with no reason."""
        for status, expected in (
            ("complete", "succeeded"),
            ("blocked", "failed"),
            ("paused", "failed"),
        ):
            with self.subTest(status=status):
                acc = self.events.StreamAccumulator(harness="kimi")
                acc.ingest_line(
                    json.dumps(
                        {
                            "type": "goal.summary",
                            "goalId": "g-1",
                            "status": status,
                            "reason": "waiting on a human decision",
                            "turnsUsed": 7,
                            "tokensUsed": 41234,
                            "wallClockMs": 90210,
                        }
                    )
                )
                self.assertEqual(acc.terminal_status, expected)
                self.assertEqual(
                    acc.terminal_event["reason"],
                    f"status={status} reason=waiting on a human decision turns=7 tokens=41234",
                )

    def test_kimi_goal_summary_reason_is_bounded(self):
        acc = self.events.StreamAccumulator(harness="kimi")
        acc.ingest_line(
            json.dumps({"type": "goal.summary", "status": "blocked", "reason": "R" * 5000})
        )
        completed = next(event for event in acc.events if event.kind == "run.completed")
        payload = completed.to_dict()
        self.assertEqual(len(payload["message"]), self.events.EVENT_TEXT_LIMIT)
        self.assertTrue(payload["truncated"])

    def test_goal_summary_is_ignored_for_other_harnesses(self):
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(json.dumps({"type": "goal.summary", "status": "paused"}))
        self.assertIsNone(acc.terminal_status)

    def test_codex_thread_started_captures_harness_session_id(self):
        acc = self.events.StreamAccumulator(harness="codex")
        acc.ingest_line(
            json.dumps(
                {"type": "thread.started", "thread_id": "019e88bc-8615-7232-b6cf-f22315386ee8"}
            )
        )
        self.assertEqual(acc.harness_session_id, "019e88bc-8615-7232-b6cf-f22315386ee8")

    def test_claude_system_init_captures_harness_session_id(self):
        acc = self.events.StreamAccumulator(harness="claude")
        acc.ingest_line(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "ses_0b850d7d2ffeowrlqEvxrNru7d",
                    "cwd": "/repo",
                }
            )
        )
        self.assertEqual(acc.harness_session_id, "ses_0b850d7d2ffeowrlqEvxrNru7d")

    def test_invalid_harness_session_id_discarded_with_event(self):
        overlong_id = "a" * 257
        acc_overlong = self.events.StreamAccumulator(harness="codex")
        acc_overlong.ingest_line(json.dumps({"type": "thread.started", "thread_id": overlong_id}))
        self.assertIsNone(acc_overlong.harness_session_id)
        self.assertTrue(any(e.kind == "session.invalid" for e in acc_overlong.events))

        control_id = "ses_abc\x00def"
        acc_control = self.events.StreamAccumulator(harness="claude")
        acc_control.ingest_line(
            json.dumps({"type": "system", "subtype": "init", "session_id": control_id})
        )
        self.assertIsNone(acc_control.harness_session_id)
        self.assertTrue(any(e.kind == "session.invalid" for e in acc_control.events))
