from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from delegate_agent import mail, private_io, run_registry


class MailPruneTests(unittest.TestCase):
    NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    OLD_SENT = "2026-06-01T12:00:00Z"
    RECENT_SENT = "2026-07-15T12:00:00Z"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-prune-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def write_mail_file(self, box_key: str, folder: str, message_id: str, sent: str) -> Path:
        box = mail._ensure_box(self.registry_root, box_key)
        envelope = {
            "schema": mail.MAIL_MESSAGE_SCHEMA,
            "msgId": message_id,
            "seq": 1,
            "sent": sent,
            "from": "coordinator",
            "fromRunId": None,
            "to": box_key,
            "group": None,
            "subject": "",
            "replyTo": None,
        }
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        payload += mail.MESSAGE_SEPARATOR + b"mail body"
        path = box / folder / f"{message_id}.mail"
        private_io.write_bytes_atomic_if_absent(path, payload)
        return path

    def write_ledger(self, message_id: str, sent: str) -> Path:
        mail._ensure_mail_tree(self.registry_root)
        path = mail.sent_root(self.registry_root) / f"{message_id}.json"
        private_io.write_json_atomic(
            path,
            {
                "schema": mail.MAIL_MESSAGE_SCHEMA,
                "msgId": message_id,
                "seq": 1,
                "sent": sent,
                "from": "coordinator",
                "fromRunId": None,
                "recipients": [],
            },
        )
        return path

    def test_mail_prune_has_own_schema_and_does_not_change_runs_prune_schema(self):
        payload = mail.prune(
            self.registry_root,
            mail.MailCommand(action="prune", older_than_days=30),
            now=self.NOW,
        )
        self.assertEqual(payload["schema"], mail.MAIL_PRUNE_SCHEMA)
        self.assertEqual(
            set(payload),
            {"schema", "ok", "olderThanDays", "dryRun", "planned", "removed", "skipped", "errors"},
        )

    def test_empty_dry_run_does_not_create_a_mail_tree(self):
        self.assertFalse(mail.mail_root(self.registry_root).exists())

        payload = mail.prune(
            self.registry_root,
            mail.MailCommand(action="prune", older_than_days=30, dry_run=True),
            now=self.NOW,
        )

        self.assertEqual(payload["planned"], [])
        self.assertFalse(mail.mail_root(self.registry_root).exists())

        runs_payload = run_registry.prune_runs(
            self.registry_root, older_than_days=30, dry_run=True, now=self.NOW
        )
        self.assertEqual(runs_payload["schema"], "delegate.runs-prune.v1")
        self.assertEqual(
            set(runs_payload),
            {
                "schema",
                "ok",
                "olderThanDays",
                "dryRun",
                "planned",
                "removed",
                "skipped",
                "errors",
                "staleResumeSchemas",
            },
        )

    def test_age_basis_is_sent_and_covers_orphan_coordinator_and_ledger(self):
        orphan_id = "del_20260101T000000Z_abcdef"
        old_orphan = self.write_mail_file(orphan_id, "inbox", "old-orphan", self.OLD_SENT)
        old_coordinator = self.write_mail_file(
            mail.COORDINATOR_BOX, "inbox", "old-coordinator", self.OLD_SENT
        )
        old_read = self.write_mail_file(mail.COORDINATOR_BOX, "read", "old-read", self.OLD_SENT)
        old_ledger = self.write_ledger("old-ledger", self.OLD_SENT)
        recent = self.write_mail_file(
            mail.COORDINATOR_BOX, "inbox", "recent-coordinator", self.RECENT_SENT
        )
        recent_ledger = self.write_ledger("recent-ledger", self.RECENT_SENT)

        # A fresh mtime must not protect an old sent timestamp, and an old mtime
        # must not make a recent sent timestamp eligible.
        os.utime(old_orphan, (self.NOW.timestamp(), self.NOW.timestamp()))
        os.utime(recent, (0, 0))

        payload = mail.prune(
            self.registry_root,
            mail.MailCommand(action="prune", older_than_days=30),
            now=self.NOW,
        )
        planned_ids = {item["msgId"] for item in payload["planned"]}
        self.assertTrue({"old-orphan", "old-coordinator", "old-read", "old-ledger"} <= planned_ids)
        self.assertTrue(any(item["box"] == orphan_id for item in payload["skipped"]))
        for path in (old_orphan, old_coordinator, old_read, old_ledger):
            self.assertFalse(path.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(recent_ledger.exists())

    def test_dry_run_has_same_plan_as_mutating_run_and_changes_nothing(self):
        old_message = self.write_mail_file(
            mail.COORDINATOR_BOX, "inbox", "dry-message", self.OLD_SENT
        )
        old_ledger = self.write_ledger("dry-ledger", self.OLD_SENT)
        orphan_message = self.write_mail_file(
            "del_20260101T000000Z_abcdef", "read", "dry-orphan", self.OLD_SENT
        )
        before = {path: path.read_bytes() for path in (old_message, old_ledger, orphan_message)}

        dry = mail.prune(
            self.registry_root,
            mail.MailCommand(action="prune", older_than_days=30, dry_run=True),
            now=self.NOW,
        )
        self.assertEqual(dry["removed"], [])
        self.assertTrue(
            all(path.exists() and path.read_bytes() == data for path, data in before.items())
        )

        actual = mail.prune(
            self.registry_root,
            mail.MailCommand(action="prune", older_than_days=30),
            now=self.NOW,
        )
        self.assertEqual(actual["planned"], dry["planned"])
        self.assertEqual(actual["removed"], dry["planned"])
        self.assertFalse(any(path.exists() for path in before))

    def test_dry_run_reports_legacy_home_without_removing_it(self):
        legacy_home = (
            mail.boxes_root(self.registry_root)
            / "del_20260101T000000Z_abcdef"
            / mail.MAIL_PUSH_CODEX_HOME_NAME
        )
        legacy_home.mkdir(parents=True)
        canary = legacy_home / "auth.json"
        canary.write_text('{"token":"canary"}', encoding="utf-8")
        before = canary.read_bytes()

        dry = mail.prune(
            self.registry_root,
            mail.MailCommand(action="prune", older_than_days=30, dry_run=True),
            now=self.NOW,
        )

        self.assertTrue(legacy_home.is_dir())
        self.assertEqual(canary.read_bytes(), before)
        self.assertIn({"path": str(legacy_home), "kind": "legacy_codex_home"}, dry["planned"])

    def test_mail_prune_leaves_runs_prune_payload_bytes_untouched(self):
        run_id, _alias = run_registry.register_run(
            self.registry_root, harness="cursor", metadata={"mode": "work"}
        )
        run_registry.write_json_atomic(
            run_registry.run_directory(self.registry_root, run_id) / run_registry.STATE_FILE,
            {"status": "failed", "lastActivityAt": self.OLD_SENT},
        )
        before = json.dumps(
            run_registry.prune_runs(
                self.registry_root, older_than_days=30, dry_run=True, now=self.NOW
            ),
            sort_keys=True,
        )
        mail.prune(
            self.registry_root,
            mail.MailCommand(action="prune", older_than_days=30),
            now=self.NOW,
        )
        after = json.dumps(
            run_registry.prune_runs(
                self.registry_root, older_than_days=30, dry_run=True, now=self.NOW
            ),
            sort_keys=True,
        )
        self.assertEqual(before, after)
        self.assertEqual(json.loads(after)["schema"], run_registry.RUNS_PRUNE_SCHEMA)


if __name__ == "__main__":
    unittest.main()
