from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, command_help, mail, profiles, run_registry
from delegate_agent.errors import DelegateError


class MailIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="delegate-mail-identity-", dir=str(Path(__file__).resolve().parents[3])
        )
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")

    def _run(
        self,
        *,
        mode: str = "work",
        status: str = "running",
        run_id: str | None = None,
        alias_harness: str = "cursor",
    ) -> tuple[str, str]:
        run_id, alias = run_registry.register_run(
            self.root,
            harness=alias_harness,
            run_id=run_id,
            metadata={"mode": mode, "cwd": str(self.workspace)},
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

    def _send(self, command: mail.MailCommand, env: dict[str, str | None]) -> dict:
        return mail.send(self.root, command, env=env)

    def test_unset_identity_is_coordinator(self) -> None:
        payload = self._send(
            mail.MailCommand(action="send", to="coordinator", body="operator steering"),
            {},
        )

        message = payload["message"]
        self.assertEqual(message["from"], "coordinator")
        self.assertIsNone(message["fromRunId"])
        self.assertEqual(message["recipients"][0]["recipient"], "coordinator")
        self.assertIsNone(message["recipients"][0]["runId"])

    def test_bound_work_identity_is_alias_in_envelope_and_ledger(self) -> None:
        run_id, alias = self._run()
        payload = self._send(
            mail.MailCommand(action="send", to="coordinator", body="lane report"),
            {"DELEGATE_RUN_ID": run_id, "DELEGATE_MAIL_SELF": alias},
        )

        message = payload["message"]
        self.assertEqual(message["from"], alias)
        self.assertEqual(message["fromRunId"], run_id)
        sent_path = self.root / "mail" / "sent" / f"{message['msgId']}.json"
        ledger = json.loads(sent_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["from"], alias)
        self.assertEqual(ledger["fromRunId"], run_id)
        inbox = mail.inbox(self.root, mail.MailCommand(action="inbox"), env={})
        self.assertEqual(inbox["messages"][0]["fromRunId"], run_id)

    def test_unknown_stale_and_malformed_identity_are_hard_errors(self) -> None:
        cases = {
            "unknown": "del_20260801T120000Z_abcdef",
            "stale": "del_20260731T120000Z_abcdef",
            "malformed": "not-a-run-id",
        }
        for label, run_id in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(DelegateError) as ctx:
                    self._send(
                        mail.MailCommand(action="send", to="coordinator", body="x"),
                        {"DELEGATE_RUN_ID": run_id, "DELEGATE_MAIL_SELF": "cursor-1"},
                    )
                self.assertEqual(ctx.exception.error, "unknown_sender")

    def test_mail_self_without_run_id_is_not_a_coordinator_fallback(self) -> None:
        with self.assertRaises(DelegateError) as ctx:
            self._send(
                mail.MailCommand(action="send", to="coordinator", body="x"),
                {"DELEGATE_MAIL_SELF": "coordinator"},
            )
        self.assertEqual(ctx.exception.error, "unknown_sender")

    def test_safe_run_cannot_send_as_a_lane(self) -> None:
        run_id, alias = self._run(mode="safe")
        with self.assertRaises(DelegateError) as ctx:
            self._send(
                mail.MailCommand(action="send", to="coordinator", body="x"),
                {"DELEGATE_RUN_ID": run_id, "DELEGATE_MAIL_SELF": alias},
            )
        self.assertEqual(ctx.exception.error, "reserved_sender")
        self.assertIn("Only work-mode runs", ctx.exception.message)

    def test_coordinator_is_reserved_sender_for_lane_impersonation(self) -> None:
        run_id, alias = self._run()
        with self.assertRaises(DelegateError) as ctx:
            self._send(
                mail.MailCommand(action="send", to="coordinator", body="x"),
                {
                    "DELEGATE_RUN_ID": run_id,
                    "DELEGATE_MAIL_SELF": "coordinator",
                },
            )
        self.assertEqual(ctx.exception.error, "unknown_sender")
        self.assertNotEqual(alias, "coordinator")

    def test_child_environment_pops_identity_from_ambient_and_profile_fallback_merge(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DELEGATE_RUN_ID": "del_parent", "DELEGATE_MAIL_SELF": "cursor-1"},
            clear=False,
        ):
            child = profiles.child_environment(base={"DELEGATE_UNRELATED": "base"})
        self.assertNotIn("DELEGATE_RUN_ID", child)
        self.assertNotIn("DELEGATE_MAIL_SELF", child)

        resolution = profiles.ProfileResolution(
            name="primary",
            source="flag",
            env={
                "CODEX_HOME": "/tmp/primary",
                "DELEGATE_RUN_ID": "del_parent",
                "DELEGATE_MAIL_SELF": "cursor-1",
            },
            codex_home="/tmp/primary",
            codex_fallback_home="/tmp/fallback",
        )
        fallback = profiles.codex_fallback_child_env_overrides(
            resolution,
            {"DELEGATE_RUN_ID": "del_parent", "DELEGATE_MAIL_SELF": "cursor-1"},
        )
        self.assertNotIn("DELEGATE_RUN_ID", fallback)
        self.assertNotIn("DELEGATE_MAIL_SELF", fallback)

    def test_no_launch_from_identity_flag_in_parser_or_registry_help(self) -> None:
        for engine in ("cursor", "codex", "claude", "grok", "devin", "opencode"):
            with self.subTest(engine=engine):
                with self.assertRaises(DelegateError) as ctx:
                    cli.parse_cli([engine, "work", "--from", "someone", "prompt"])
                self.assertEqual(ctx.exception.error, "unknown_option")

        for name, spec in command_help.COMMAND_SPECS.items():
            if name.startswith("mail"):
                continue
            option_flags = {option.flag for option in spec.options}
            self.assertNotIn("--from", option_flags, name)
            self.assertFalse(any("--from" in usage for usage in spec.usage), name)

    def test_bind_mail_identity_follows_register_on_cli_tracked_path(self) -> None:
        source = inspect.getsource(cli.execute_request)
        self.assertLess(
            source.index("run_registry.register_run("), source.index("mail.bind_mail_identity(")
        )

    def test_bind_mail_identity_follows_register_on_worktree_path(self) -> None:
        from delegate_agent import worktree_execution

        source = inspect.getsource(worktree_execution._register_persistent_worktree_run)
        self.assertLess(
            source.index("run_registry.register_run("), source.index("mail.bind_mail_identity(")
        )


if __name__ == "__main__":
    unittest.main()
