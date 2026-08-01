from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import mail, run_registry
from tests.delegate_commands_test_base import CommandTestBase


class MailContractTests(CommandTestBase):
    _MESSAGE_ID = "20260801-120000-a1b2c3"
    _SENT_AT = "2026-08-01T12:00:00Z"

    def setUp(self):
        super().setUp()
        self.workspace_temp = tempfile.TemporaryDirectory(prefix="delegate-mail-contract-")
        self.addCleanup(self.workspace_temp.cleanup)
        self.workspace = Path(self.workspace_temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def _run_json(self, *args: str) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_main(["--json", "--cwd", str(self.workspace), *args])
        return code, json.loads(stdout), stderr

    @staticmethod
    def _assert_sorted_json(test: unittest.TestCase, raw: str, payload: dict) -> None:
        test.assertEqual(raw, json.dumps(payload, sort_keys=True) + "\n")

    def test_send_inbox_read_status_have_frozen_v1_contracts(self):
        with (
            mock.patch.object(mail, "_next_message_id", return_value=self._MESSAGE_ID),
            mock.patch.object(run_registry, "utc_now_iso", return_value=self._SENT_AT),
        ):
            code, stdout, stderr = self.run_main(
                [
                    "--json",
                    "--cwd",
                    str(self.workspace),
                    "mail",
                    "send",
                    "--to",
                    "coordinator",
                    "--subject",
                    "contract",
                    "hello mailbox",
                ]
            )
        self.assertEqual(code, 0, stderr)
        send_payload = json.loads(stdout)
        self._assert_sorted_json(self, stdout, send_payload)
        self.assertEqual(set(send_payload), {"framing", "message", "ok", "schema"})
        self.assertEqual(send_payload["schema"], mail.MAIL_SEND_SCHEMA)
        self.assertTrue(send_payload["ok"])
        self.assertEqual(
            set(send_payload["message"]),
            {
                "from",
                "fromRunId",
                "group",
                "msgId",
                "recipients",
                "replyTo",
                "schema",
                "sent",
                "seq",
                "subject",
                "to",
            },
        )
        self.assertEqual(
            set(send_payload["message"]["recipients"][0]),
            {"box", "deliveredAt", "outcome", "recipient", "runId"},
        )
        self.assertEqual(send_payload["message"]["recipients"][0]["outcome"], "delivered")

        code, stdout, stderr = self.run_main(
            ["--json", "--cwd", str(self.workspace), "mail", "inbox"]
        )
        self.assertEqual(code, 0, stderr)
        inbox_payload = json.loads(stdout)
        self._assert_sorted_json(self, stdout, inbox_payload)
        self.assertEqual(set(inbox_payload), {"framing", "messages", "ok", "schema"})
        self.assertEqual(inbox_payload["schema"], mail.MAIL_INBOX_SCHEMA)
        self.assertEqual(len(inbox_payload["messages"]), 1)
        self.assertEqual(
            set(inbox_payload["messages"][0]),
            {
                "body",
                "from",
                "fromRunId",
                "group",
                "msgId",
                "replyTo",
                "schema",
                "sent",
                "seq",
                "subject",
                "to",
            },
        )
        self.assertEqual(inbox_payload["messages"][0]["body"], "hello mailbox")

        code, stdout, stderr = self.run_main(
            ["--json", "--cwd", str(self.workspace), "mail", "read", self._MESSAGE_ID]
        )
        self.assertEqual(code, 0, stderr)
        read_payload = json.loads(stdout)
        self._assert_sorted_json(self, stdout, read_payload)
        self.assertEqual(set(read_payload), {"framing", "message", "ok", "peek", "schema"})
        self.assertEqual(read_payload["schema"], mail.MAIL_READ_SCHEMA)
        self.assertFalse(read_payload["peek"])
        self.assertEqual(read_payload["message"]["body"], "hello mailbox")

        code, stdout, stderr = self.run_main(
            ["--json", "--cwd", str(self.workspace), "mail", "status", self._MESSAGE_ID]
        )
        self.assertEqual(code, 0, stderr)
        status_payload = json.loads(stdout)
        self._assert_sorted_json(self, stdout, status_payload)
        self.assertEqual(set(status_payload), {"framing", "message", "ok", "schema"})
        self.assertEqual(status_payload["schema"], mail.MAIL_STATUS_SCHEMA)
        status_row = status_payload["message"]["recipients"][0]
        self.assertEqual(status_row["outcome"], "delivered")
        self.assertEqual(status_row["pathState"], "read")

    def test_prune_has_own_schema_and_dry_run_shape(self):
        for dry_run in (True, False):
            code, payload, stderr = self._run_json(
                "mail",
                "prune",
                "--older-than",
                "30",
                *("--dry-run",) if dry_run else (),
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                set(payload),
                {
                    "dryRun",
                    "errors",
                    "olderThanDays",
                    "ok",
                    "planned",
                    "removed",
                    "schema",
                    "skipped",
                },
            )
            self.assertEqual(payload["schema"], mail.MAIL_PRUNE_SCHEMA)
            self.assertEqual(payload["dryRun"], dry_run)
            self.assertEqual(payload["removed"], [])

    def test_watch_emits_ndjson_mail_and_metadata_only(self):
        with (
            mock.patch.object(mail, "_next_message_id", return_value=self._MESSAGE_ID),
            mock.patch.object(run_registry, "utc_now_iso", return_value=self._SENT_AT),
        ):
            result = mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to="coordinator", body="secret body"),
            )
        self.assertTrue(result["ok"])
        output = io.StringIO()
        code = mail.watch(
            run_registry.registry_root(self.workspace),
            mail.MailCommand(action="watch", once=True),
            stdout=output,
        )
        self.assertEqual(code, 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(set(record), {"message", "schema", "type"})
        self.assertEqual(record["schema"], mail.MAIL_WATCH_SCHEMA)
        self.assertEqual(record["type"], "mail")
        self.assertEqual(
            set(record["message"]),
            {"from", "fromRunId", "group", "msgId", "replyTo", "sent", "seq", "subject", "to"},
        )
        self.assertNotIn("body", record["message"])

    def test_human_inbox_prints_framing_before_message_bodies(self):
        code, send_stdout, stderr = self.run_main(
            [
                "--cwd",
                str(self.workspace),
                "mail",
                "send",
                "--to",
                "coordinator",
                "--subject",
                "inbox summary",
                "hello from the inbox",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("sent ", send_stdout)
        self.assertIn(" seq=1", send_stdout)
        self.assertIn("  coordinator: delivered", send_stdout)
        code, stdout, stderr = self.run_main(["--cwd", str(self.workspace), "mail", "inbox"])
        self.assertEqual(code, 0, stderr)
        lines = stdout.splitlines()
        self.assertEqual(
            lines[0],
            "mail framing (coordinator, tier 1): " + mail.COORDINATOR_FRAMING["text"],
        )
        self.assertIn("from coordinator sent ", lines[1])
        self.assertIn("subject 'inbox summary': hello from the inbox", lines[1])
        self.assertNotIn('{"framing"', stdout)
        self.assertNotIn('{"body"', stdout)

    def test_human_read_and_status_render_prose(self):
        body = "first line\nsecond line"
        with mock.patch.object(mail, "_next_message_id", return_value=self._MESSAGE_ID):
            code, _stdout, stderr = self.run_main(
                [
                    "--cwd",
                    str(self.workspace),
                    "mail",
                    "send",
                    "--to",
                    "coordinator",
                    "--subject",
                    "human message",
                    body,
                ]
            )
        self.assertEqual(code, 0, stderr)

        code, stdout, stderr = self.run_main(
            ["--cwd", str(self.workspace), "mail", "read", self._MESSAGE_ID]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("mail framing (coordinator, tier 1):", stdout)
        self.assertIn(f"message: {self._MESSAGE_ID}", stdout)
        self.assertIn("from: coordinator", stdout)
        self.assertIn("subject: human message", stdout)
        self.assertIn("body:\nfirst line\nsecond line", stdout)
        self.assertNotIn('{"framing"', stdout)
        self.assertNotIn('{"message"', stdout)

        code, stdout, stderr = self.run_main(
            ["--cwd", str(self.workspace), "mail", "status", self._MESSAGE_ID]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"message {self._MESSAGE_ID} seq=1", stdout)
        self.assertIn("  coordinator: delivered (read)", stdout)
        self.assertNotIn('{"framing"', stdout)
        self.assertNotIn('{"message"', stdout)

    def test_watch_emits_an_unreadable_ndjson_record(self):
        malformed = (
            mail._ensure_box(self.registry_root, mail.COORDINATOR_BOX) / "inbox" / "bad.mail"
        )
        malformed.write_bytes(b"not a mail envelope")
        output = io.StringIO()
        code = mail.watch(
            self.registry_root,
            mail.MailCommand(action="watch", once=True),
            stdout=output,
        )
        self.assertEqual(code, 0)
        record = json.loads(output.getvalue())
        self.assertEqual(
            record,
            {
                "error": "mail_unreadable",
                "message": f"Mail message has no envelope separator: {malformed}",
                "schema": mail.MAIL_WATCH_SCHEMA,
                "type": "unreadable",
            },
        )

    def test_watch_timeout_defaults_to_600_seconds_and_exits_124(self):
        output = io.StringIO()
        with (
            mock.patch.object(mail.time, "monotonic", side_effect=[0.0, 0.0, 601.0]),
            mock.patch.object(mail.time, "sleep"),
        ):
            code = mail.watch(
                self.registry_root,
                mail.MailCommand(action="watch", once=True),
                stdout=output,
            )
        self.assertEqual(code, 124)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"schema": mail.MAIL_WATCH_SCHEMA, "timeout": 600, "type": "timeout"},
        )

    def test_watch_once_sleeps_only_until_its_deadline(self):
        output = io.StringIO()
        with (
            mock.patch.object(mail.time, "monotonic", side_effect=[0.0, 0.0, 0.25]),
            mock.patch.object(mail.time, "sleep") as sleep,
        ):
            code = mail.watch(
                self.registry_root,
                mail.MailCommand(action="watch", once=True, timeout=0.25, interval_ms=1000),
                stdout=output,
            )
        self.assertEqual(code, 124)
        sleep.assert_called_once_with(0.25)

    def test_standard_error_envelope_is_sorted_and_exact(self):
        code, stdout, stderr = self.run_main(
            ["--json", "--cwd", str(self.workspace), "mail", "send", "--to", "missing", "body"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self._assert_sorted_json(self, stdout, payload)
        self.assertEqual(set(payload), {"error", "exitCode", "message", "ok"})
        self.assertEqual(payload["error"], "unknown_recipient")
        self.assertEqual(payload["exitCode"], 2)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
