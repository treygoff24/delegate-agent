from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from delegate_agent import mail, run_registry
from delegate_agent.errors import DelegateError


class MailRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="delegate-mail-rules-", dir=str(Path(__file__).resolve().parents[3])
        )
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")

    def _run(self, *, group: str | None = None) -> tuple[str, str]:
        run_id, alias = run_registry.register_run(
            self.root,
            harness="cursor",
            metadata={"mode": "work", "cwd": str(self.workspace), "group": group},
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

    def _write_rules(self, rules: list[dict[str, object]]) -> None:
        run_registry.write_json_atomic(self.root / "mail" / "rules.json", {"rules": rules})

    def test_group_routes_apply_rules_to_each_expanded_alias(self) -> None:
        sender_id, sender_alias = self._run(group="reviewers")
        _blocked_id, blocked = self._run(group="reviewers")
        _allowed_id, allowed = self._run(group="reviewers")
        self._write_rules(
            [
                {
                    "action": "block",
                    "from": sender_alias,
                    "to": blocked,
                    "reason": "conflict of interest",
                }
            ]
        )

        message = mail.send(
            self.root,
            mail.MailCommand(action="send", group="reviewers", body="group update"),
            env={"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender_alias},
        )["message"]
        rows = {row["recipient"]: row for row in message["recipients"]}
        self.assertEqual(rows[blocked]["outcome"], "blocked")
        self.assertEqual(rows[blocked]["reason"], "conflict of interest")
        self.assertEqual(rows[allowed]["outcome"], "delivered")
        blocked_path = self.root / "mail" / "boxes" / blocked / "inbox" / f"{message['msgId']}.mail"
        allowed_path = (
            self.root
            / "mail"
            / "boxes"
            / rows[allowed]["box"]
            / "inbox"
            / f"{message['msgId']}.mail"
        )
        self.assertFalse(blocked_path.exists())
        self.assertTrue(allowed_path.exists())

    def test_direct_blocked_send_refuses_with_do_not_route_around_text(self) -> None:
        sender_id, sender_alias = self._run()
        _target_id, target = self._run()
        self._write_rules(
            [{"action": "deny", "from": sender_alias, "to": target, "reason": "policy boundary"}]
        )

        with self.assertRaises(DelegateError) as ctx:
            mail.send(
                self.root,
                mail.MailCommand(action="send", to=target, body="must not deliver"),
                env={"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender_alias},
            )
        self.assertEqual(ctx.exception.error, "blocked_recipient")
        self.assertIn("Do not route around this rule.", ctx.exception.message)
        self.assertEqual(list((self.root / "mail" / "sent").glob("*.json")), [])

    def test_blocked_row_is_in_ledger_even_when_another_group_member_delivers(self) -> None:
        sender_id, sender_alias = self._run(group="mixed")
        _blocked_id, blocked = self._run(group="mixed")
        _allowed_id, allowed = self._run(group="mixed")
        self._write_rules([{"blocked": True, "toAlias": blocked, "message": "not this member"}])

        payload = mail.send(
            self.root,
            mail.MailCommand(action="send", group="mixed", body="mixed"),
            env={"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender_alias},
        )
        rows = {row["recipient"]: row for row in payload["message"]["recipients"]}
        self.assertEqual(rows[blocked]["outcome"], "blocked")
        self.assertEqual(rows[blocked]["reason"], "not this member")
        self.assertEqual(rows[allowed]["outcome"], "delivered")


if __name__ == "__main__":
    unittest.main()
