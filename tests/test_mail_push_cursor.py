from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import mail, run_registry, run_status


class _FailBeforeInjection(io.StringIO):
    def write(self, _text: str) -> int:
        raise OSError("simulated injection failure")


class _PartialWriter(io.StringIO):
    def write(self, text: str) -> int:
        super().write(text[:1])
        raise OSError("simulated short write")


class _FlushFailure(io.StringIO):
    def flush(self) -> None:
        raise OSError("simulated flush failure")


class MailPushCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-push-cursor-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        self.run_id, self.alias = run_registry.register_run(
            self.registry_root,
            harness="claude",
            run_id="del_20260801T120000Z_abcdef",
            metadata={"mode": "work", "cwd": str(self.workspace)},
        )
        run_registry.write_json_atomic(
            run_registry.run_directory(self.registry_root, self.run_id) / run_registry.STATE_FILE,
            {
                "schema": run_registry.STATE_SCHEMA,
                "runId": self.run_id,
                "alias": self.alias,
                "status": run_status.STATUS_RUNNING,
                "pid": os.getpid(),
            },
        )
        self.env = {
            "DELEGATE_RUN_ID": self.run_id,
            "DELEGATE_MAIL_SELF": self.alias,
            "DELEGATE_MAIL_HOOK_HARNESS": "claude",
        }
        mail.provision_mail_push(
            "claude",
            ["claude", "prompt"],
            None,
            self.registry_root,
            self.run_id,
            self.env,
        )

    @property
    def cursor_path(self) -> Path:
        return mail.boxes_root(self.registry_root) / self.run_id / mail.MAIL_PUSH_CURSOR_FILE_NAME

    def _cursor(self) -> int:
        return json.loads(self.cursor_path.read_text(encoding="utf-8"))["lastSeq"]

    def _send(self, body: str) -> dict:
        return mail.send(
            self.registry_root,
            mail.MailCommand(action="send", to=self.alias, body=body),
        )["message"]

    def _pump(self, stdout: io.StringIO | None = None) -> dict:
        output = stdout or io.StringIO()
        self.assertEqual(mail.hook_pump(self.registry_root, stdout=output, env=self.env), 0)
        return json.loads(output.getvalue())

    def _payload(self, response: dict) -> dict:
        field = response["reason"] if "reason" in response else response["additionalContext"]
        return json.loads(field)

    def test_two_boundaries_inject_each_batch_once_and_keep_mail_non_consuming(self) -> None:
        first = self._send("first")
        second = self._send("second")
        box = mail.boxes_root(self.registry_root) / self.run_id
        before = {
            "inbox": sorted(path.name for path in (box / "inbox").iterdir()),
            "read": sorted(path.name for path in (box / "read").iterdir()),
        }

        response = self._pump()
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(response)["messages"]],
            [first["msgId"], second["msgId"]],
        )
        self.assertEqual(self._cursor(), 0)
        self.assertEqual(
            {
                "inbox": sorted(path.name for path in (box / "inbox").iterdir()),
                "read": sorted(path.name for path in (box / "read").iterdir()),
            },
            before,
        )
        self.assertEqual(
            [
                message["msgId"]
                for message in mail.inbox(
                    self.registry_root, mail.MailCommand(action="inbox"), env=self.env
                )["messages"]
            ],
            [first["msgId"], second["msgId"]],
        )

        self.assertEqual(self._pump(), {})
        self.assertEqual(self._cursor(), second["seq"])

        third = self._send("third")
        response = self._pump()
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(response)["messages"]],
            [third["msgId"]],
        )
        self.assertEqual(self._cursor(), second["seq"])
        self.assertEqual(self._pump(), {})
        self.assertEqual(self._cursor(), third["seq"])
        self.assertEqual(self._pump(), {})

    def test_write_failure_leaves_cursor_unadvanced_without_post_response_output(self) -> None:
        message = self._send("retry before injection")
        self.assertEqual(
            mail.hook_pump(self.registry_root, stdout=_FailBeforeInjection(), env=self.env),
            1,
        )

        self.assertEqual(self._cursor(), 0)
        self.assertIsNone(mail.read_hook_failure_marker(self.registry_root, self.run_id))
        retry = self._pump()
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(retry)["messages"]],
            [message["msgId"]],
        )
        self.assertEqual(self._cursor(), 0)
        self.assertEqual(self._pump(), {})
        self.assertEqual(self._cursor(), message["seq"])

    def test_short_write_stays_silent_and_keeps_pending_unpromoted(self) -> None:
        self._send("short write")
        output = _PartialWriter()
        self.assertEqual(mail.hook_pump(self.registry_root, stdout=output, env=self.env), 1)
        self.assertEqual(output.getvalue(), "{")
        pending = mail._hook_pending(self.registry_root, self.run_id)
        self.assertIsNotNone(pending)
        self.assertFalse(pending["emitted"])
        self.assertIsNone(mail.read_hook_failure_marker(self.registry_root, self.run_id))

    def test_flush_failure_stays_silent_and_keeps_pending_unpromoted(self) -> None:
        self._send("flush failure")
        output = _FlushFailure()
        self.assertEqual(mail.hook_pump(self.registry_root, stdout=output, env=self.env), 1)
        self.assertNotEqual(output.getvalue(), "")
        pending = mail._hook_pending(self.registry_root, self.run_id)
        self.assertIsNotNone(pending)
        self.assertFalse(pending["emitted"])
        self.assertIsNone(mail.read_hook_failure_marker(self.registry_root, self.run_id))

    def test_crash_after_injection_before_cursor_write_allows_only_one_bounded_retry(self) -> None:
        message = self._send("retry after injection")
        first_output = io.StringIO()
        with mock.patch.object(
            mail, "_mark_hook_pending_emitted", side_effect=OSError("simulated kill after flush")
        ):
            self.assertEqual(
                mail.hook_pump(self.registry_root, stdout=first_output, env=self.env), 1
            )
        first = json.loads(first_output.getvalue())
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(first)["messages"]],
            [message["msgId"]],
        )
        self.assertEqual(self._cursor(), 0)

        retry_output = io.StringIO()
        self.assertEqual(mail.hook_pump(self.registry_root, stdout=retry_output, env=self.env), 0)
        retry = json.loads(retry_output.getvalue())
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(retry)["messages"]],
            [message["msgId"]],
        )
        self.assertEqual(self._cursor(), 0)

        self.assertEqual(self._pump(), {})
        self.assertEqual(self._cursor(), message["seq"])

    def test_consumed_mail_resolves_from_read_before_the_next_boundary(self) -> None:
        message = self._send("consume before boundary")
        first = self._pump()
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(first)["messages"]],
            [message["msgId"]],
        )
        mail.read_message(
            self.registry_root,
            mail.MailCommand(action="read", message_id=message["msgId"]),
            env=self.env,
        )
        self.assertEqual(self._pump(), {})
        self.assertEqual(self._cursor(), message["seq"])

    def test_grok_continuation_double_boundary_does_not_reinject_message(self) -> None:
        self.env["DELEGATE_MAIL_HOOK_HARNESS"] = "grok"
        message = self._send("grok continuation")

        first = self._pump()
        self.assertIn("additionalContext", first)
        self.assertNotIn("reason", first)
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(first)["messages"]],
            [message["msgId"]],
        )
        self.assertEqual(self._pump(), {})
        self.assertEqual(self._cursor(), message["seq"])


if __name__ == "__main__":
    unittest.main()
