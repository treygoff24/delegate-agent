from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from delegate_agent import mail, private_io, run_registry
from delegate_agent.errors import DelegateError


class MailBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-bounds-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def assert_mail_error(self, callable_obj, expected_code: str):
        with self.assertRaises(DelegateError) as context:
            callable_obj()
        self.assertEqual(context.exception.error, expected_code)

    def write_inbox_messages(self, count: int):
        mail._ensure_mail_tree(self.registry_root)
        box = mail._ensure_box(self.registry_root, mail.COORDINATOR_BOX)
        for number in range(count):
            message_id = f"20260801-120000-{number:06x}"
            envelope = {
                "schema": mail.MAIL_MESSAGE_SCHEMA,
                "msgId": message_id,
                "seq": number + 1,
                "sent": "2026-08-01T12:00:00Z",
                "from": "coordinator",
                "fromRunId": None,
                "to": mail.COORDINATOR_BOX,
                "group": None,
                "subject": "",
                "replyTo": None,
            }
            body = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
            body += mail.MESSAGE_SEPARATOR + b"body"
            private_io.write_bytes_atomic_if_absent(box / "inbox" / f"{message_id}.mail", body)

    def test_body_boundary_accepts_256_kib_and_rejects_one_more_byte(self):
        exact = "x" * mail.MAIL_MAX_BODY_BYTES
        result = mail.send(
            self.registry_root,
            mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body=exact),
        )
        self.assertEqual(len(result["message"]["recipients"]), 1)
        self.assert_mail_error(
            lambda: mail.send(
                self.registry_root,
                mail.MailCommand(
                    action="send",
                    to=mail.COORDINATOR_BOX,
                    body="x" * (mail.MAIL_MAX_BODY_BYTES + 1),
                ),
            ),
            "message_too_large",
        )

    def test_subject_boundary_accepts_200_chars_and_rejects_one_more(self):
        result = mail.send(
            self.registry_root,
            mail.MailCommand(
                action="send",
                to=mail.COORDINATOR_BOX,
                subject="s" * mail.MAIL_MAX_SUBJECT_CHARS,
                body="subject boundary",
            ),
        )
        self.assertEqual(result["message"]["subject"], "s" * mail.MAIL_MAX_SUBJECT_CHARS)
        self.assert_mail_error(
            lambda: mail.send(
                self.registry_root,
                mail.MailCommand(
                    action="send",
                    to=mail.COORDINATOR_BOX,
                    subject="s" * (mail.MAIL_MAX_SUBJECT_CHARS + 1),
                    body="too long",
                ),
            ),
            "message_too_large",
        )

    def write_rules(self, count: int, *, exact_bytes: int | None = None):
        mail._ensure_mail_tree(self.registry_root)
        raw = json.dumps(
            {"rules": [{"action": "allow"} for _ in range(count)]},
            separators=(",", ":"),
        )
        if exact_bytes is not None:
            self.assertLessEqual(len(raw.encode()), exact_bytes)
            raw += " " * (exact_bytes - len(raw.encode()))
        private_io.write_private_bytes(
            mail.mail_root(self.registry_root) / mail.RULES_FILE_NAME,
            raw.encode(),
        )

    def test_rules_boundary_accepts_64_kib_and_500_rules_but_rejects_each_overage(self):
        self.write_rules(mail.MAIL_MAX_RULES, exact_bytes=mail.MAIL_MAX_RULES_BYTES)
        accepted = mail.send(
            self.registry_root,
            mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="rules boundary"),
        )
        self.assertTrue(accepted["ok"])

        self.write_rules(mail.MAIL_MAX_RULES, exact_bytes=mail.MAIL_MAX_RULES_BYTES + 1)
        self.assert_mail_error(
            lambda: mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="too many bytes"),
            ),
            "rules_too_large",
        )

        self.write_rules(mail.MAIL_MAX_RULES + 1)
        self.assert_mail_error(
            lambda: mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="too many rules"),
            ),
            "rules_too_large",
        )

    def test_inbox_listing_cap_is_exactly_1000_items(self):
        for count in (mail.MAIL_MAX_INBOX_ITEMS, mail.MAIL_MAX_INBOX_ITEMS + 1):
            with self.subTest(count=count):
                box = mail.boxes_root(self.registry_root) / mail.COORDINATOR_BOX
                if box.exists():
                    shutil.rmtree(box)
                self.write_inbox_messages(count)
                result = mail.inbox(self.registry_root, mail.MailCommand(action="inbox"))
                self.assertEqual(len(result["messages"]), mail.MAIL_MAX_INBOX_ITEMS)

    def test_watch_batch_cap_is_exactly_1000_items(self):
        for count in (mail.MAIL_MAX_WATCH_ITEMS, mail.MAIL_MAX_WATCH_ITEMS + 1):
            with self.subTest(count=count):
                box = mail.boxes_root(self.registry_root) / mail.COORDINATOR_BOX
                if box.exists():
                    shutil.rmtree(box)
                self.write_inbox_messages(count)
                output = io.StringIO()
                exit_code = mail.watch(
                    self.registry_root,
                    mail.MailCommand(action="watch", once=True, timeout=1),
                    stdout=output,
                )
                self.assertEqual(exit_code, 0)
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), mail.MAIL_MAX_WATCH_ITEMS)
                self.assertTrue(all(json.loads(line)["type"] == "mail" for line in lines))


if __name__ == "__main__":
    unittest.main()
