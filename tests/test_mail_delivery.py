from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from delegate_agent import mail, private_io, run_registry


class MailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def lane(
        self,
        *,
        harness: str = "cursor",
        mode: str = "work",
        status: str = "running",
        group: str | None = None,
    ) -> tuple[str, str, dict[str, str]]:
        metadata = {"mode": mode}
        if group is not None:
            metadata["group"] = group
        run_id, alias = run_registry.register_run(
            self.registry_root, harness=harness, metadata=metadata
        )
        state = {"status": status, "lastActivityAt": "2026-08-01T12:00:00Z"}
        if status == "running":
            state["pid"] = os.getpid()
        run_registry.write_json_atomic(
            run_registry.run_directory(self.registry_root, run_id) / run_registry.STATE_FILE,
            state,
        )
        return run_id, alias, {"DELEGATE_RUN_ID": run_id, "DELEGATE_MAIL_SELF": alias}

    def test_ledger_records_all_outcomes_and_status_probes_pruned_delivery(self):
        _sender_id, _sender, sender_env = self.lane(group="other")
        _delivered_id, delivered, _delivered_env = self.lane(group="crew")
        _safe_id, safe, _safe_env = self.lane(mode="safe", group="crew")
        _failed_id, failed, _failed_env = self.lane(status="failed", group="crew")
        _blocked_id, blocked, _blocked_env = self.lane(group="crew")

        mail._ensure_mail_tree(self.registry_root)
        private_io.write_json_atomic(
            mail.mail_root(self.registry_root) / mail.RULES_FILE_NAME,
            {"rules": [{"action": "block", "to": blocked, "reason": "no route"}]},
        )
        result = mail.send(
            self.registry_root,
            mail.MailCommand(action="send", group="crew", body="payload"),
            env=sender_env,
        )
        rows = {row["recipient"]: row for row in result["message"]["recipients"]}
        self.assertEqual(
            {row["outcome"] for row in rows.values()},
            {"delivered", "skipped_ineligible", "blocked"},
        )
        self.assertEqual(rows[delivered]["outcome"], "delivered")
        self.assertEqual(rows[safe]["outcome"], "skipped_ineligible")
        self.assertEqual(rows[failed]["outcome"], "skipped_ineligible")
        self.assertEqual(rows[blocked]["outcome"], "blocked")
        message_id = result["message"]["msgId"]
        delivered_path = (
            mail.boxes_root(self.registry_root) / _delivered_id / "inbox" / f"{message_id}.mail"
        )
        self.assertTrue(delivered_path.exists())
        self.assertFalse(
            (
                mail.boxes_root(self.registry_root) / _safe_id / "inbox" / f"{message_id}.mail"
            ).exists()
        )
        self.assertFalse(
            (
                mail.boxes_root(self.registry_root) / _failed_id / "inbox" / f"{message_id}.mail"
            ).exists()
        )
        self.assertFalse(
            (
                mail.boxes_root(self.registry_root) / _blocked_id / "inbox" / f"{message_id}.mail"
            ).exists()
        )

        delivered_path.unlink()
        status = mail.status(
            self.registry_root,
            mail.MailCommand(action="status", message_id=message_id),
            env=sender_env,
        )
        status_rows = {row["recipient"]: row for row in status["message"]["recipients"]}
        self.assertEqual(status_rows[delivered]["outcome"], "pruned")
        self.assertEqual(status_rows[safe]["outcome"], "skipped_ineligible")
        self.assertEqual(status_rows[failed]["outcome"], "skipped_ineligible")
        self.assertEqual(status_rows[blocked]["outcome"], "blocked")

    def test_crash_after_ledger_claim_leaves_failed_row_without_inbox_file(self):
        _sender_id, _sender, sender_env = self.lane()
        _recipient_id, recipient, _recipient_env = self.lane()

        with (
            mock.patch.object(
                mail.private_io,
                "write_bytes_atomic_if_absent",
                side_effect=KeyboardInterrupt,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=recipient, body="crash window"),
                env=sender_env,
            )

        sent_files = list(mail.sent_root(self.registry_root).glob("*.json"))
        self.assertEqual(len(sent_files), 1)
        ledger = json.loads(sent_files[0].read_text(encoding="utf-8"))
        self.assertEqual(ledger["recipients"][0]["outcome"], "failed")
        self.assertFalse(
            (
                mail.boxes_root(self.registry_root)
                / _recipient_id
                / "inbox"
                / f"{ledger['msgId']}.mail"
            ).exists()
        )

    def test_publication_link_failure_records_failed_and_does_not_claim_inbox(self):
        _sender_id, _sender, sender_env = self.lane()
        _recipient_id, recipient, _recipient_env = self.lane()
        real_link = private_io.os.link
        calls = 0

        def link_once_then_fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "simulated publication link failure")
            return real_link(*args, **kwargs)

        with mock.patch.object(private_io.os, "link", side_effect=link_once_then_fail):
            result = mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=recipient, body="link failure"),
                env=sender_env,
            )

        self.assertEqual(calls, 2)
        row = result["message"]["recipients"][0]
        self.assertEqual(row["outcome"], "failed")
        self.assertTrue(row["reason"])
        self.assertFalse(
            any(mail.boxes_root(self.registry_root).rglob(f"{result['message']['msgId']}.mail"))
        )

    def test_disjoint_recipient_collision_still_claims_two_global_ids(self):
        _sender_a_id, _sender_a, env_a = self.lane(harness="cursor")
        _sender_b_id, _sender_b, env_b = self.lane(harness="codex")
        _recipient_a_id, recipient_a, _recipient_a_env = self.lane(harness="cursor")
        _recipient_b_id, recipient_b, _recipient_b_env = self.lane(harness="codex")
        with mock.patch.object(
            mail,
            "_next_message_id",
            side_effect=["20260801-120000-fixed", "20260801-120000-fixed", "20260801-120000-next"],
        ):
            first = mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=recipient_a, body="a"),
                env=env_a,
            )
            second = mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=recipient_b, body="b"),
                env=env_b,
            )

        self.assertNotEqual(first["message"]["msgId"], second["message"]["msgId"])
        self.assertEqual(len(list(mail.sent_root(self.registry_root).glob("*.json"))), 2)
        self.assertEqual(first["message"]["recipients"][0]["outcome"], "delivered")
        self.assertEqual(second["message"]["recipients"][0]["outcome"], "delivered")

    def test_sent_seq_is_mailbox_order_and_meta_sequence_is_atomic_under_lock(self):
        active = False
        meta_lock_observations: list[bool] = []
        real_lock = run_registry.registry_lock
        real_write = private_io.write_json_atomic

        @contextmanager
        def tracked_lock(registry_root, **kwargs):
            nonlocal active
            with real_lock(registry_root, **kwargs):
                active = True
                try:
                    yield
                finally:
                    active = False

        def observed_write(path, payload, **kwargs):
            if path == mail.mail_root(self.registry_root) / mail.META_FILE_NAME:
                meta_lock_observations.append(active)
            return real_write(path, payload, **kwargs)

        with (
            mock.patch.object(mail.run_registry, "registry_lock", tracked_lock),
            mock.patch.object(mail.private_io, "write_json_atomic", side_effect=observed_write),
        ):
            first = mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="one"),
            )
            second = mail.send(
                self.registry_root,
                mail.MailCommand(action="send", to=mail.COORDINATOR_BOX, body="two"),
            )

        self.assertEqual(meta_lock_observations, [True, True, True])
        self.assertEqual([first["message"]["seq"], second["message"]["seq"]], [1, 2])
        inbox = mail.inbox(self.registry_root, mail.MailCommand(action="inbox"))
        self.assertEqual([message["seq"] for message in inbox["messages"]], [1, 2])
        meta = json.loads(
            (mail.mail_root(self.registry_root) / mail.META_FILE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(meta["nextSeq"], 3)


if __name__ == "__main__":
    unittest.main()
