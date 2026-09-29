"""Mail delivered to a lane that never read it must not vanish at run end."""

from __future__ import annotations

import dataclasses
import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import mail, rendering, run_registry, runner, wait_cancel_commands
from delegate_agent.mail_core import MAIL_PROMPT_SUFFIX
from tests.tracked_capture_helpers import make_context, read_report


class MailPromptSuffixTests(unittest.TestCase):
    def test_suffix_demands_inbox_checks_before_verification_and_final_report(self):
        self.assertIn("before final verification or commit", MAIL_PROMPT_SUFFIX)
        self.assertIn("before your final report", MAIL_PROMPT_SUFFIX)
        self.assertIn("correct the task", MAIL_PROMPT_SUFFIX)


class UnreadMailBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-unread-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        base = make_context(self.workspace, "codex")
        self.root = base.registry_root
        run_id, alias = run_registry.register_run(
            self.root, harness="codex", metadata={"mode": "work"}
        )
        self.ctx = dataclasses.replace(base, run_id=run_id, alias=alias)
        self.lane_env = {"DELEGATE_RUN_ID": self.ctx.run_id, "DELEGATE_MAIL_SELF": self.ctx.alias}
        # Mail only delivers to a running work lane.
        run_registry.write_json_atomic(
            run_registry.run_directory(self.root, self.ctx.run_id) / run_registry.STATE_FILE,
            {"status": "running", "pid": os.getpid(), "lastActivityAt": "2026-08-01T12:00:00Z"},
        )

    def send(self, subject: str, body: str = "fix it") -> str:
        result = mail.send(
            self.root,
            mail.MailCommand(action="send", to=self.ctx.alias, subject=subject, body=body),
            env={},
        )
        return result["message"]["msgId"]

    def read(self, message_id: str) -> None:
        mail.read_message(
            self.root, mail.MailCommand(action="read", message_id=message_id), env=self.lane_env
        )

    def run_child(self):
        return runner.execute_tracked(
            [sys.executable, "-c", "print('done')"],
            str(self.workspace),
            self.ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            timeout=30,
        )


class UnreadMailSummaryTests(UnreadMailBase):
    def unread(self, *, mail_push: bool = False) -> dict:
        return mail.unread_mail_extra(
            self.root, self.ctx.run_id, self.ctx.alias, mail_push=mail_push
        )

    def test_delivered_unread_messages_are_counted_with_sender_and_subject(self):
        first = self.send("stop touching P4")
        extra = self.unread()
        self.assertEqual(extra["unreadMail"]["count"], 1)
        row = extra["unreadMail"]["messages"][0]
        self.assertEqual(
            (row["msgId"], row["from"], row["subject"]), (first, "coordinator", "stop touching P4")
        )
        warning = extra["warnings"][0]
        self.assertIn("1 mail message was delivered to this run and never read", warning)
        self.assertIn(f'delegate followup {self.ctx.alias} "<correction>"', warning)

    def test_read_messages_are_not_unread_and_none_means_no_fields(self):
        message_id = self.send("read me")
        self.read(message_id)
        self.assertEqual(self.unread(), {})

    def test_at_most_three_are_listed_but_all_are_counted(self):
        for n in range(5):
            self.send(f"note {n}")
        extra = self.unread()
        self.assertEqual(extra["unreadMail"]["count"], 5)
        self.assertEqual(len(extra["unreadMail"]["messages"]), 3)
        self.assertIn("and 2 more", extra["warnings"][0])

    def test_messages_a_push_hook_already_injected_count_as_seen(self):
        self.send("pushed already")
        with mock.patch("delegate_agent.mail_push.pushed_through_seq", return_value=10**6):
            self.assertEqual(self.unread(mail_push=True), {})
        # Without push the same message is still unread.
        self.assertEqual(self.unread()["unreadMail"]["count"], 1)


class UnreadMailDamageTests(UnreadMailBase):
    def test_a_malformed_file_does_not_hide_a_valid_unread_message(self):
        good = self.send("the real correction")
        inbox = self.root / "mail" / "boxes" / self.ctx.run_id / "inbox"
        (inbox / "20260101-000000-abcdef.mail").write_bytes(b"not an envelope")
        extra = mail.unread_mail_extra(self.root, self.ctx.run_id, self.ctx.alias, mail_push=False)
        record = extra["unreadMail"]
        self.assertEqual(record["count"], 1)
        self.assertEqual(record["messages"][0]["msgId"], good)
        self.assertEqual(record["unreadable"], 1)
        self.assertIn("damaged or unreadable", extra["warnings"][0])
        self.assertIn("never read by it", extra["warnings"][0])

    def test_only_a_malformed_file_is_still_visible(self):
        self.send("placeholder")
        inbox = self.root / "mail" / "boxes" / self.ctx.run_id / "inbox"
        for path in inbox.glob("*.mail"):
            path.write_bytes(b"junk")
        extra = mail.unread_mail_extra(self.root, self.ctx.run_id, self.ctx.alias, mail_push=False)
        self.assertEqual(extra["unreadMail"]["count"], 0)
        self.assertEqual(extra["unreadMail"]["unreadable"], 1)
        self.assertIn("damaged or unreadable", extra["warnings"][0])


class UnreadMailFinalizationTests(UnreadMailBase):
    def test_run_that_never_read_its_mail_records_it_on_state_envelope_and_report(self):
        self.send("the P4 restore always refuses")
        _code, payload = self.run_child()
        state = run_registry.load_run_state(self.root, self.ctx.run_id)
        for record in (state, payload):
            self.assertEqual(record["unreadMail"]["count"], 1)
            self.assertTrue(
                any("never read by it" in w for w in record["warnings"]), record["warnings"]
            )
        self.assertIn("never read by it", read_report(self.ctx))
        self.assertIn("the P4 restore always refuses", read_report(self.ctx))

    def test_run_with_no_unread_mail_has_no_unread_field(self):
        _code, payload = self.run_child()
        self.assertNotIn("unreadMail", payload)
        self.assertNotIn("unreadMail", run_registry.load_run_state(self.root, self.ctx.run_id))


class UnreadMailSurfaceTests(unittest.TestCase):
    def test_wait_table_prints_unread_mail_line(self):
        out = io.StringIO()
        wait_cancel_commands._print_wait_table(
            [
                {
                    "alias": "codex-1",
                    "status": "succeeded",
                    "unreadMail": {"count": 2, "messages": []},
                }
            ],
            out,
        )
        self.assertIn("unread mail: 2", out.getvalue())

    def test_wait_structural_keys_carry_unread_mail(self):
        self.assertIn("unreadMail", wait_cancel_commands.WAIT_STRUCTURAL_KEYS)

    def test_snapshot_text_prints_the_warning(self):
        out = io.StringIO()
        rendering.render_snapshot_text(
            {"alias": "codex-1", "runId": "r", "status": "succeeded", "warnings": ["2 mail x"]},
            out,
        )
        self.assertIn("2 mail x", out.getvalue())


class InboxLocationTests(UnreadMailBase):
    def test_manifest_and_completion_payload_name_the_inbox_host_and_paths(self):
        self.ctx = dataclasses.replace(
            self.ctx, mail_inbox=mail.inbox_location(self.root, self.ctx.run_id)
        )
        manifest = runner.build_manifest(self.ctx, ["x"])
        location = manifest["mailInbox"]
        self.assertEqual(location["host"], socket.gethostname())
        self.assertEqual(location["root"], str(self.root / "mail"))
        self.assertTrue(location["coordinatorInbox"].endswith("boxes/coordinator/inbox"))
        self.assertTrue(location["laneInbox"].endswith(f"boxes/{self.ctx.run_id}/inbox"))
        self.assertIs(location["crossHostDelivery"], False)
        _code, payload = self.run_child()
        self.assertEqual(payload["mailInbox"], location)

    def test_run_without_mail_has_no_inbox_block(self):
        self.assertNotIn("mailInbox", runner.build_manifest(self.ctx, ["x"]))


if __name__ == "__main__":
    unittest.main()
