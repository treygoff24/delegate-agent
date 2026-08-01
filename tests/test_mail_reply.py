from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from delegate_agent import mail, run_registry
from delegate_agent.errors import DelegateError


class MailReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="delegate-mail-reply-", dir=str(Path(__file__).resolve().parents[3])
        )
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")

    def _run(self) -> tuple[str, str]:
        run_id, alias = run_registry.register_run(
            self.root,
            harness="cursor",
            metadata={"mode": "work", "cwd": str(self.workspace)},
        )
        run_registry.write_json_atomic(
            run_registry.run_directory(self.root, run_id) / run_registry.STATE_FILE,
            {
                "schema": run_registry.STATE_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "status": "running",
                "pid": os.getpid(),
                "lastActivityAt": "2026-08-01T12:00:00Z",
            },
        )
        return run_id, alias

    def _send(self, command: mail.MailCommand, env: dict[str, str | None] | None = None) -> dict:
        return mail.send(self.root, command, env=env)["message"]

    def test_watch_reply_to_allows_only_original_delivered_recipients(self) -> None:
        replier_id, replier = self._run()
        third_id, third = self._run()
        original = self._send(mail.MailCommand(action="send", to=replier, body="question"))
        self._send(
            mail.MailCommand(
                action="send", to="coordinator", reply_to=original["msgId"], body="answer"
            ),
            {"DELEGATE_RUN_ID": replier_id, "DELEGATE_MAIL_SELF": replier},
        )
        self._send(
            mail.MailCommand(action="send", to="coordinator", body="unrelated"),
            {"DELEGATE_RUN_ID": third_id, "DELEGATE_MAIL_SELF": third},
        )

        stdout = io.StringIO()
        code = mail.watch(
            self.root,
            mail.MailCommand(action="watch", once=True, reply_to=original["msgId"], timeout=1),
            stdout=stdout,
            env={},
        )
        self.assertEqual(code, 0)
        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["message"]["fromRunId"], replier_id)

    def test_watch_from_narrows_reply_replier_set(self) -> None:
        replier_id, replier = self._run()
        third_id, third = self._run()
        original = self._send(mail.MailCommand(action="send", to=replier, body="question"))
        self._send(
            mail.MailCommand(
                action="send", to="coordinator", reply_to=original["msgId"], body="answer"
            ),
            {"DELEGATE_RUN_ID": replier_id, "DELEGATE_MAIL_SELF": replier},
        )
        self._send(
            mail.MailCommand(action="send", to="coordinator", body="third-party"),
            {"DELEGATE_RUN_ID": third_id, "DELEGATE_MAIL_SELF": third},
        )
        stdout = io.StringIO()
        code = mail.watch(
            self.root,
            mail.MailCommand(
                action="watch",
                once=True,
                reply_to=original["msgId"],
                from_sender=third,
                timeout=1,
            ),
            stdout=stdout,
            env={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["type"], "timeout")

    def test_send_reply_to_requires_existing_exchange_and_participation(self) -> None:
        replier_id, replier = self._run()
        third_id, third = self._run()
        original = self._send(mail.MailCommand(action="send", to=replier, body="question"))

        with self.assertRaises(DelegateError) as missing:
            self._send(
                mail.MailCommand(
                    action="send", to="coordinator", reply_to="does-not-exist", body="x"
                ),
                {"DELEGATE_RUN_ID": replier_id, "DELEGATE_MAIL_SELF": replier},
            )
        self.assertEqual(missing.exception.error, "unknown_message")

        with self.assertRaises(DelegateError) as third_party:
            self._send(
                mail.MailCommand(
                    action="send", to="coordinator", reply_to=original["msgId"], body="x"
                ),
                {"DELEGATE_RUN_ID": third_id, "DELEGATE_MAIL_SELF": third},
            )
        self.assertEqual(third_party.exception.error, "reply_not_participant")
        self.assertIn("cannot be routed around", third_party.exception.message)

    def test_reply_envelope_carries_original_reply_to_id(self) -> None:
        replier_id, replier = self._run()
        original = self._send(mail.MailCommand(action="send", to=replier, body="question"))
        reply = self._send(
            mail.MailCommand(
                action="send", to="coordinator", reply_to=original["msgId"], body="answer"
            ),
            {"DELEGATE_RUN_ID": replier_id, "DELEGATE_MAIL_SELF": replier},
        )
        self.assertEqual(reply["replyTo"], original["msgId"])
        self.assertEqual(reply["fromRunId"], replier_id)


if __name__ == "__main__":
    unittest.main()
