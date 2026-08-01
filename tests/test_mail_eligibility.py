from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from delegate_agent import mail, run_registry


class MailEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="delegate-mail-eligibility-", dir=str(Path(__file__).resolve().parents[3])
        )
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")

    def _run(self, *, mode: str = "work", status: str = "running", group: str | None = None):
        run_id, alias = run_registry.register_run(
            self.root,
            harness="cursor",
            metadata={"mode": mode, "cwd": str(self.workspace), "group": group},
        )
        state = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "lastActivityAt": "2026-08-01T12:00:00Z",
        }
        if status == "running":
            state["pid"] = os.getpid()
        run_registry.write_json_atomic(
            run_registry.run_directory(self.root, run_id) / run_registry.STATE_FILE,
            state,
        )
        return run_id, alias

    def _send(
        self, *, sender: dict[str, str] | None, to: str | None = None, group: str | None = None
    ):
        return mail.send(
            self.root,
            mail.MailCommand(action="send", to=to, group=group, body="eligibility"),
            env=sender,
        )["message"]

    def test_direct_to_only_publishes_to_effectively_running_work_run(self) -> None:
        sender_id, sender_alias = self._run(group="g")
        _eligible_id, eligible = self._run(group="g")
        _safe_id, safe = self._run(mode="safe", group="g")
        _call_id, call = self._run(mode="call", group="g")
        _terminal_id, terminal = self._run(status="succeeded", group="g")
        _stale_id, stale = self._run(status="running", group="g")
        stale_path = run_registry.run_directory(self.root, _stale_id) / run_registry.STATE_FILE
        state = run_registry.read_json_object(stale_path)
        assert state is not None
        state["pid"] = 99999999
        run_registry.write_json_atomic(stale_path, state)

        sender = {"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender_alias}
        for alias in (eligible, safe, call, terminal, stale):
            with self.subTest(alias=alias):
                message = self._send(sender=sender, to=alias)
                row = message["recipients"][0]
                if alias == eligible:
                    self.assertEqual(row["outcome"], "delivered")
                    self.assertTrue(
                        (
                            self.root
                            / "mail"
                            / "boxes"
                            / row["box"]
                            / "inbox"
                            / f"{message['msgId']}.mail"
                        ).is_file()
                    )
                else:
                    self.assertEqual(row["outcome"], "skipped_ineligible")
                    self.assertFalse(
                        (
                            self.root
                            / "mail"
                            / "boxes"
                            / row["box"]
                            / "inbox"
                            / f"{message['msgId']}.mail"
                        ).exists()
                    )

    def test_group_uses_same_eligibility_predicate_as_direct_delivery(self) -> None:
        sender_id, sender_alias = self._run(group="reviewers")
        _eligible_id, eligible = self._run(group="reviewers")
        _safe_id, safe = self._run(mode="safe", group="reviewers")
        _call_id, call = self._run(mode="call", group="reviewers")
        _terminal_id, terminal = self._run(status="cancelled", group="reviewers")
        stale_id, stale = self._run(group="reviewers")
        stale_path = run_registry.run_directory(self.root, stale_id) / run_registry.STATE_FILE
        state = run_registry.read_json_object(stale_path)
        assert state is not None
        state["pid"] = 99999999
        run_registry.write_json_atomic(stale_path, state)

        message = self._send(
            sender={"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender_alias},
            group="reviewers",
        )
        outcomes = {row["recipient"]: row["outcome"] for row in message["recipients"]}
        self.assertEqual(outcomes[eligible], "delivered")
        for alias in (safe, call, terminal, stale):
            self.assertEqual(outcomes[alias], "skipped_ineligible")

    def test_sender_is_excluded_from_its_own_broadcast(self) -> None:
        sender_id, sender_alias = self._run(group="self-broadcast")
        _other_id, other = self._run(group="self-broadcast")
        message = self._send(
            sender={"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender_alias},
            group="self-broadcast",
        )

        recipients = {row["recipient"]: row for row in message["recipients"]}
        self.assertNotIn(sender_alias, recipients)
        self.assertEqual(recipients[other]["outcome"], "delivered")

    def test_coordinator_is_an_explicit_reserved_destination(self) -> None:
        run_id, alias = self._run()
        message = self._send(
            sender={"DELEGATE_RUN_ID": run_id, "DELEGATE_MAIL_SELF": alias},
            to="coordinator",
        )
        self.assertEqual(
            message["recipients"],
            [
                {
                    "recipient": "coordinator",
                    "runId": None,
                    "box": "coordinator",
                    "outcome": "delivered",
                    "deliveredAt": message["recipients"][0]["deliveredAt"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
