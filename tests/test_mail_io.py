from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from delegate_agent import mail, private_io, run_registry


class MailIoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-io-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def test_write_bytes_atomic_if_absent_is_private_exclusive_and_non_overwriting(self):
        destination = self.registry_root / "mail" / "bytes.bin"
        private_io.ensure_private_dir(destination.parent)

        self.assertTrue(private_io.write_bytes_atomic_if_absent(destination, b"first"))
        self.assertFalse(private_io.write_bytes_atomic_if_absent(destination, b"second"))
        self.assertEqual(destination.read_bytes(), b"first")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_send_persists_json_envelope_separator_and_exact_body(self):
        payload = mail.send(
            self.registry_root,
            mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="hello"),
        )
        message = payload["message"]
        message_id = message["msgId"]
        path = (
            mail.boxes_root(self.registry_root)
            / mail.COORDINATOR_BOX
            / "inbox"
            / f"{message_id}.mail"
        )

        raw = path.read_bytes()
        envelope_bytes, body = raw.split(mail.MESSAGE_SEPARATOR, 1)
        envelope = json.loads(envelope_bytes)
        self.assertEqual(envelope["schema"], mail.MAIL_MESSAGE_SCHEMA)
        self.assertEqual(envelope["msgId"], message_id)
        self.assertEqual(envelope["seq"], message["seq"])
        self.assertEqual(body, b"hello")
        self.assertEqual(raw.count(mail.MESSAGE_SEPARATOR), 1)

    def test_read_moves_inbox_message_to_read_and_peek_does_not(self):
        first = mail.send(
            self.registry_root,
            mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="first"),
        )["message"]
        second = mail.send(
            self.registry_root,
            mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="second"),
        )["message"]
        box = mail.boxes_root(self.registry_root) / mail.COORDINATOR_BOX

        listed = mail.inbox(self.registry_root, mail.MailCommand(action="inbox"))
        self.assertEqual(
            [item["msgId"] for item in listed["messages"]], [first["msgId"], second["msgId"]]
        )
        self.assertTrue((box / "inbox" / f"{first['msgId']}.mail").exists())

        peeked = mail.read_message(
            self.registry_root,
            mail.MailCommand(action="read", message_id=second["msgId"], peek=True),
        )
        self.assertTrue(peeked["peek"])
        self.assertTrue((box / "inbox" / f"{second['msgId']}.mail").exists())
        self.assertFalse((box / "read" / f"{second['msgId']}.mail").exists())

        consumed = mail.read_message(
            self.registry_root,
            mail.MailCommand(action="read", message_id=first["msgId"]),
        )
        self.assertFalse(consumed["peek"])
        self.assertFalse((box / "inbox" / f"{first['msgId']}.mail").exists())
        self.assertTrue((box / "read" / f"{first['msgId']}.mail").exists())

        read_again = mail.read_message(
            self.registry_root,
            mail.MailCommand(action="read", message_id=first["msgId"]),
        )
        self.assertEqual(read_again["message"]["body"], "first")


if __name__ == "__main__":
    unittest.main()
