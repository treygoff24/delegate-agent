from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import mail, private_io, run_registry, run_status


class _FailBeforeInjection(io.StringIO):
    def write(self, _text: str) -> int:
        raise OSError("simulated injection failure")


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

    def test_pump_is_non_consuming_and_only_injects_beyond_cursor(self) -> None:
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
        self.assertEqual(self._cursor(), second["seq"])
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

        third = self._send("third")
        response = self._pump()
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(response)["messages"]],
            [third["msgId"]],
        )
        self.assertEqual(self._cursor(), third["seq"])
        self.assertEqual(self._pump(), {})

    def test_crash_before_injection_leaves_cursor_unadvanced_and_retries_without_loss(self) -> None:
        message = self._send("retry before injection")
        self.assertEqual(
            mail.hook_pump(self.registry_root, stdout=_FailBeforeInjection(), env=self.env),
            0,
        )

        self.assertEqual(self._cursor(), 0)
        self.assertEqual(
            mail.read_hook_failure_marker(self.registry_root, self.run_id),
            "hook_runtime_failed: simulated injection failure",
        )
        retry = self._pump()
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(retry)["messages"]],
            [message["msgId"]],
        )
        self.assertEqual(self._cursor(), message["seq"])

    def test_crash_after_injection_before_cursor_write_allows_only_one_bounded_retry(self) -> None:
        message = self._send("retry after injection")
        original_write = private_io.write_json_atomic

        def crash_before_cursor_write(path: Path, payload: dict) -> None:
            if path == self.cursor_path:
                raise RuntimeError("simulated crash before cursor write")
            original_write(path, payload)

        first_output = io.StringIO()
        with (
            mock.patch.object(
                private_io, "write_json_atomic", side_effect=crash_before_cursor_write
            ),
            self.assertRaisesRegex(RuntimeError, "before cursor write"),
        ):
            mail.hook_pump(self.registry_root, stdout=first_output, env=self.env)
        first = json.loads(first_output.getvalue())
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(first)["messages"]],
            [message["msgId"]],
        )
        self.assertEqual(self._cursor(), 0)

        retry = self._pump()
        self.assertEqual(
            [row["message"]["msgId"] for row in self._payload(retry)["messages"]],
            [message["msgId"]],
        )
        self.assertEqual(self._cursor(), message["seq"])
        self.assertEqual(self._pump(), {})

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
