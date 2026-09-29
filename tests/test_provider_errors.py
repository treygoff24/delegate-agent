import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import child_failures, outcome, provider_errors  # noqa: E402


def record(engine, message, *, status=None, code=None, fallback=""):
    raw = provider_errors.raw_error(message=message, status=status, code=code, source="test")
    return provider_errors.provider_error_record(engine=engine, raw=raw, fallback_text=fallback)


class StatusFirstTests(unittest.TestCase):
    def test_a_400_that_says_invalid_token_is_a_request_error_not_auth(self):
        # The audit's live misread: "invalid ... token" inside a 400 read as auth_failed.
        text = "400 Bad Request: invalid token in request body"
        self.assertEqual(child_failures.classify(text).code, "auth_failed")  # text alone
        result = record("omp", text, status=400)
        self.assertEqual(result["signature"], "request_rejected")
        self.assertEqual(result["scope"], provider_errors.SCOPE_REQUEST)
        self.assertEqual(result["class"], provider_errors.CLASS_PERSISTENT)
        self.assertEqual(child_failures.classify(text, status=400).code, "provider_error")

    def test_omp_image_limit_is_a_request_error_and_never_auth_failed(self):
        result = record("omp", "Too many images in request: 8 > 4", status=400)
        self.assertEqual(result["signature"], "request_image_limit")
        self.assertEqual(result["scope"], provider_errors.SCOPE_REQUEST)
        self.assertEqual(result["class"], provider_errors.CLASS_PERSISTENT)
        self.assertEqual(provider_errors.reason_for_record(result), "provider_error")
        self.assertIn("fewer or smaller images", result["hint"])

    def test_image_limit_text_keeps_its_signature_when_the_status_is_unknown(self):
        result = record("omp", "Too many images in request: 8 > 4")
        self.assertEqual(result["signature"], "request_image_limit")

    def test_one_status_carries_several_causes(self):
        self.assertEqual(
            record("codex", "quota exhausted", status=429)["signature"], "rate_limited"
        )
        self.assertEqual(
            record("codex", "You exceeded your current quota", status=429)["signature"],
            "usage_limit",
        )
        self.assertEqual(
            record("codex", "uid_unmapped: Broker returned HTTP 403", status=403)["signature"],
            "broker_uid_unmapped",
        )
        self.assertEqual(record("codex", "nope", status=403)["signature"], "forbidden")

    def test_status_classes(self):
        cases = {
            401: ("auth_rejected", "persistent"),
            402: ("payment_required", "persistent"),
            404: ("model_unavailable", "persistent"),
            413: ("request_too_large", "persistent"),
            429: ("rate_limited", "transient"),
            500: ("provider_unavailable", "transient"),
            503: ("provider_unavailable", "transient"),
        }
        for status, (signature, klass) in cases.items():
            with self.subTest(status=status):
                result = record("grok", "provider said no", status=status)
                self.assertEqual(result["signature"], signature)
                self.assertEqual(result["class"], klass)
                self.assertEqual(result["status"], status)

    def test_a_structured_provider_code_outranks_the_status(self):
        result = record("codex", "over quota", status=400, code="insufficient_quota")
        self.assertEqual(result["signature"], "usage_limit")
        self.assertEqual(result["providerCode"], "insufficient_quota")

    def test_a_status_stated_in_the_message_counts_when_none_was_structured(self):
        self.assertEqual(record("kimi", "402 Insufficient account funds")["status"], 402)
        self.assertEqual(
            record("kimi", "402 Insufficient account funds")["signature"], "payment_required"
        )
        self.assertEqual(record("codex", "unexpected status 401 Unauthorized")["status"], 401)

    def test_text_is_only_the_fallback(self):
        result = record(
            "cursor", "", fallback="Authentication required. Please run agent login first"
        )
        self.assertEqual(result["signature"], "cursor_auth_required")
        self.assertIn("estate-cursor login", result["hint"])
        self.assertIn("Authentication required", result["message"])

    def test_the_terminal_error_beats_an_incidental_earlier_line(self):
        # The audit's RC-3: an earlier "thread not found" line beat the real reason.
        result = record(
            "codex",
            "You've hit your usage limit.",
            fallback="thread abc not found\nYou've hit your usage limit.",
        )
        self.assertEqual(result["signature"], "usage_limit")

    def test_unknown_errors_are_unclassified_not_guessed(self):
        result = record("codex", "something novel happened")
        self.assertEqual(result["signature"], provider_errors.UNCLASSIFIED)
        self.assertEqual(result["class"], provider_errors.CLASS_UNKNOWN)
        self.assertEqual(result["message"], "something novel happened")

    def test_resolver_failures_stay_unclassified(self):
        # A DNS failure inside a safe-mode sandbox is not a provider fault; the
        # runner's sandbox-network hint depends on it staying an unexplained exit.
        for text in ("getaddrinfo ENOTFOUND api.example.com", "Could not resolve host: x"):
            with self.subTest(text=text):
                self.assertIsNone(provider_errors.match(text))
                self.assertIsNone(child_failures.classify(text))

    def test_no_error_and_no_text_means_no_record(self):
        self.assertIsNone(
            provider_errors.provider_error_record(engine="codex", raw=None, fallback_text="")
        )
        self.assertIsNone(
            provider_errors.provider_error_record(
                engine="codex", raw=None, fallback_text="fine, nothing wrong"
            )
        )


class EngineKeyedTests(unittest.TestCase):
    def test_engine_keyed_rows_do_not_fire_for_other_engines(self):
        text = "Authentication required. Please run agent login first"
        self.assertEqual(
            provider_errors.match(text, engine="cursor").signature.id, "cursor_auth_required"
        )
        self.assertIsNone(provider_errors.match(text, engine="codex"))

    def test_hints_name_the_fix_per_engine(self):
        self.assertIn("estate-cursor login", record("cursor", "nope", status=401)["hint"])
        self.assertIn("Kimi credits", record("kimi", "nope", status=402)["hint"])
        missing = record("omp", "No API key found for opencode-go")
        self.assertEqual(missing["signature"], "api_key_missing")
        self.assertIn("opencode-go", missing["hint"])
        self.assertIn("pick another alias", missing["hint"])
        # omp's real message ends in a period (live 2026-09-28: "error: No API key
        # found for mistral."); the sentence's period is not part of the name.
        sentence = record("omp", "error: No API key found for mistral.")
        self.assertIn("provider mistral has no API key", sentence["hint"])

    def test_only_stream_drops_and_server_errors_are_auto_resume_candidates(self):
        resumable = {s.id for s in provider_errors.SIGNATURES if s.auto_resume}
        self.assertEqual(resumable, {"stream_disconnected", "provider_unavailable"})
        for signature in provider_errors.SIGNATURES:
            if signature.auto_resume:
                self.assertEqual(signature.klass, provider_errors.CLASS_TRANSIENT)


class RecordShapeTests(unittest.TestCase):
    def test_record_carries_the_documented_fields(self):
        result = record("codex", "websocket closed by server before response.completed")
        self.assertEqual(
            set(result),
            {"status", "providerCode", "message", "engine", "signature", "class", "hint", "scope"},
        )
        self.assertEqual(result["signature"], "stream_disconnected")
        self.assertEqual(result["class"], "transient")
        self.assertEqual(result["engine"], "codex")

    def test_message_is_redacted_and_bounded(self):
        secret = "sk-abcdef1234567890abcdef"
        message = f"Authorization: Bearer {secret} " + "x" * 5000
        raw = provider_errors.raw_error(message=message, code="c" * 500)
        self.assertLessEqual(len(raw["message"]), provider_errors.MESSAGE_LIMIT)
        self.assertLessEqual(len(raw["providerCode"]), provider_errors.CODE_LIMIT)
        self.assertNotIn(secret, raw["message"])

    def test_email_addresses_are_masked_in_the_message_and_the_code(self):
        raw = provider_errors.raw_error(
            message="401 Unauthorized: the token for alice@example.com was rejected",
            code="account_alice.b+tag@mail.example.co.uk_suspended",
            status=401,
        )
        blob = json.dumps(raw)
        self.assertNotIn("alice", blob)
        self.assertNotIn("example.com", blob)
        self.assertNotIn("example.co.uk", blob)
        self.assertIn(
            "was rejected", raw["message"], "only the address is masked, not the sentence"
        )

    def test_masking_an_address_never_changes_the_classification(self):
        text = "unexpected status 401 Unauthorized: the token for alice@example.com was rejected"
        masked = record("codex", text)
        plain = record("codex", "unexpected status 401 Unauthorized: the token was rejected")
        self.assertEqual(masked["signature"], plain["signature"])
        self.assertEqual(masked["status"], 401)
        self.assertNotIn("alice@example.com", masked["message"])

    def test_a_signature_line_lifted_from_trusted_text_is_masked_too(self):
        # No terminal error message: the record's message falls back to the matched line.
        fallback = (
            "stream disconnected before completion: websocket closed by server for bob@corp.example"
        )
        result = record("codex", None, fallback=fallback)
        self.assertEqual(result["signature"], "stream_disconnected")
        self.assertNotIn("bob@corp.example", json.dumps(result))
        self.assertIn("websocket closed by server", result["message"], "the line is still surfaced")

    def test_text_that_only_looks_like_an_address_survives(self):
        for text in ("model@latest is retired", "rate limit @ 100 rpm", "see user@ host"):
            with self.subTest(text):
                self.assertEqual(provider_errors.raw_error(message=text)["message"], text)

    def test_payload_extraction_reads_the_shapes_engines_emit(self):
        self.assertEqual(
            provider_errors.status_from_payload({"message": {"errorStatus": 400}}), None
        )
        self.assertEqual(provider_errors.status_from_payload({"errorStatus": 400}), 400)
        self.assertEqual(provider_errors.status_from_payload({"error": {"status": "429"}}), 429)
        self.assertEqual(
            provider_errors.status_from_payload({"error": {"data": {"statusCode": 503}}}), 503
        )
        self.assertEqual(provider_errors.status_from_payload({"api_error_status": 401}), 401)
        self.assertIsNone(provider_errors.status_from_payload({"status": "completed"}))
        self.assertIsNone(provider_errors.status_from_payload({"status": 200}))
        self.assertEqual(
            provider_errors.code_from_payload(
                {"type": "error", "error": {"type": "overloaded_error"}}
            ),
            "overloaded_error",
        )
        self.assertIsNone(provider_errors.code_from_payload({"type": "error"}))


class CatalogIntegrityTests(unittest.TestCase):
    def test_every_signature_reason_maps_into_the_closed_failure_kinds(self):
        for signature in provider_errors.SIGNATURES:
            with self.subTest(signature=signature.id):
                kind = outcome.failure_kind_for_reason(signature.reason, exit_code=1)
                self.assertIn(kind, outcome.FAILURE_KINDS)

    def test_classes_and_scopes_are_from_the_closed_sets(self):
        for signature in provider_errors.SIGNATURES:
            self.assertIn(signature.klass, provider_errors.PROVIDER_ERROR_CLASSES)
            self.assertIn(
                signature.scope, {provider_errors.SCOPE_LANE, provider_errors.SCOPE_REQUEST}
            )
            self.assertTrue(signature.hint)

    def test_transient_rows_never_poison_a_lane_scope_check_is_by_class(self):
        # Marker writers key on persistent + lane; a transient row must never be persistent.
        for signature in provider_errors.SIGNATURES:
            if signature.klass == provider_errors.CLASS_TRANSIENT:
                self.assertNotEqual(signature.reason, "usage_limit")

    def test_request_scoped_rows_are_persistent_for_the_prompt(self):
        scoped = {
            s.id for s in provider_errors.SIGNATURES if s.scope == provider_errors.SCOPE_REQUEST
        }
        self.assertIn("request_image_limit", scoped)
        self.assertIn("request_rejected", scoped)
        self.assertIn("thread_lost", scoped)
        self.assertNotIn("cursor_auth_required", scoped)

    def test_quota_rows_still_count_as_usage_limits_for_exit_zero_runs(self):
        for text in ("402 Insufficient account funds", "Grok Build usage balance exhausted"):
            with self.subTest(text=text):
                self.assertTrue(child_failures.is_usage_limit(text))
        self.assertFalse(child_failures.is_usage_limit("websocket closed by server"))


if __name__ == "__main__":
    unittest.main()
