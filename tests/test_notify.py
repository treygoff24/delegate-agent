"""--notify: completion ping over the post CLI, probe-and-degrade."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import notify
from delegate_agent.errors import DelegateError


class NotifyTargetTests(unittest.TestCase):
    def test_room_and_channel_targets_parse(self) -> None:
        self.assertEqual(notify.parse_notify_target("room:devbox").spec, "room:devbox")
        channel = notify.parse_notify_target("channel:machineroom-devbox")
        self.assertEqual((channel.kind, channel.name), ("channel", "machineroom-devbox"))

    def test_invalid_targets_fail_closed(self) -> None:
        for bad in ("devbox", "#devbox", "room:", "room:has space", "channel:a;b", "mail:x"):
            with self.subTest(bad=bad), self.assertRaises(DelegateError) as caught:
                notify.parse_notify_target(bad)
            self.assertEqual(caught.exception.error, "invalid_notify_target")


class NotifyArgvTests(unittest.TestCase):
    def test_room_uses_post_send_and_channel_uses_chat_send_anyway(self) -> None:
        room = notify.notify_argv(notify.parse_notify_target("room:r"), "m")
        self.assertEqual(room[:4], ["post", "send", "--to", "r"])
        self.assertIn("--body", room)
        channel = notify.notify_argv(notify.parse_notify_target("channel:c"), "m")
        self.assertEqual(channel[:5], ["post", "chat", "c", "--send", "--anyway"])
        self.assertEqual(channel[-2:], ["--body", "m"])

    def test_message_is_metadata_only(self) -> None:
        text = notify.notify_message(
            run_id="del_x",
            status="succeeded",
            engine="omp",
            model="ox",
            elapsed_sec=12.4,
            workspace="/home/u/Code/proj",
        )
        self.assertEqual(text, "delegate del_x succeeded omp/ox 12s — proj")


def _fake_post(directory: Path, *, exit_code: int, stdout: str = "", sleep: float = 0.0) -> str:
    """Install a fake `post` and return a PATH that finds it (plus /bin for sh builtins)."""
    script = directory / "post"
    script.write_text(
        "#!/bin/sh\n"
        f"/bin/sleep {sleep}\n"
        f"printf '%s\\n' \"{stdout}\"\n"
        f'echo "$@" > "{directory}/argv.txt"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{directory}:/usr/bin:/bin"


class SendNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.target = notify.parse_notify_target("channel:c")

    def test_missing_post_degrades(self) -> None:
        env = {"PATH": str(self.dir)}
        outcome = notify.send_notification(self.target, "m", cwd=self.temp.name, env=env)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "post_not_found")
        self.assertEqual(
            outcome.payload(), {"target": "channel:c", "ok": False, "reason": "post_not_found"}
        )

    def test_success_captures_message_id_and_runs_from_cwd(self) -> None:
        path = _fake_post(
            self.dir, exit_code=0, stdout="post: sent #c 20260822-010000-000001-abcdef from r"
        )
        env = {"PATH": path}
        outcome = notify.send_notification(self.target, "hello", cwd=self.temp.name, env=env)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message_id, "20260822-010000-000001-abcdef")
        argv = (self.dir / "argv.txt").read_text()
        self.assertIn("chat c --send --anyway --body hello", argv)

    def test_nonzero_exit_degrades_with_first_line(self) -> None:
        path = _fake_post(self.dir, exit_code=65, stdout="post: not_a_member")
        outcome = notify.send_notification(self.target, "m", cwd=self.temp.name, env={"PATH": path})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "post: not_a_member")

    def test_timeout_degrades(self) -> None:
        path = _fake_post(self.dir, exit_code=0, sleep=2)
        outcome = notify.send_notification(
            self.target, "m", cwd=self.temp.name, env={"PATH": path}, timeout=0.2
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "post_timeout")

    def test_never_sets_post_from(self) -> None:
        path = _fake_post(self.dir, exit_code=0)
        with mock.patch.object(notify.subprocess, "run", wraps=notify.subprocess.run) as run:
            notify.send_notification(self.target, "m", cwd=self.temp.name, env={"PATH": path})
        env = run.call_args.kwargs["env"]
        self.assertNotIn("POST_FROM", env)
        self.assertEqual(run.call_args.kwargs["cwd"], self.temp.name)


class RunnerHookTests(unittest.TestCase):
    def test_hook_writes_manifest_and_fires_once(self) -> None:
        from delegate_agent import runner

        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "registry"
            run_path = registry / "runs" / "del_test"
            run_path.mkdir(parents=True)
            (run_path / "manifest.json").write_text(json.dumps({"runId": "del_test"}))
            ctx = mock.Mock()
            ctx.notify = "room:r"
            ctx.run_id = "del_test"
            ctx.registry_root = registry
            ctx.engine = "omp"
            ctx.model = "ox"
            ctx.model_resolved = None
            ctx.started_at = "2026-08-22T00:00:00Z"
            ctx.source_cwd = temp
            files = mock.Mock()
            files.run_path = run_path
            outcome = notify.NotifyOutcome(ok=True, target="room:r", message_id="id")
            with (
                mock.patch.object(notify, "send_notification", return_value=outcome) as send,
                mock.patch.object(
                    runner.run_registry,
                    "load_run_manifest_or_none",
                    return_value={"runId": "del_test"},
                ),
            ):
                runner._send_completion_notification(files, ctx, "failed")
            self.assertEqual(send.call_count, 1)
            message = send.call_args.args[1]
            self.assertTrue(message.startswith("delegate del_test failed omp/ox "))
            manifest = json.loads((run_path / "manifest.json").read_text())
            self.assertEqual(
                manifest["notify"], {"target": "room:r", "ok": True, "messageId": "id"}
            )

    def test_hook_is_a_no_op_without_notify(self) -> None:
        from delegate_agent import runner

        ctx = mock.Mock()
        ctx.notify = None
        with mock.patch.object(notify, "send_notification") as send:
            runner._send_completion_notification(mock.Mock(), ctx, "succeeded")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ParserAndDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.execution_test_base import load_delegate

        self.delegate = load_delegate()

    def test_notify_threads_into_global_options_for_launches(self) -> None:
        parsed = self.delegate.parse_cli(
            ["--json", "--notify", "channel:machineroom", "codex", "safe", "review"]
        )
        self.assertEqual(parsed.global_options.notify, "channel:machineroom")

    def test_notify_rejected_for_non_launch_subcommands_and_call_mode(self) -> None:
        with self.assertRaises(DelegateError) as caught:
            self.delegate.parse_cli(["--notify", "room:r", "runs"])
        self.assertEqual(caught.exception.error, "invalid_option_combination")
        with self.assertRaises(DelegateError) as caught:
            self.delegate.parse_cli(["--notify", "nope", "codex", "safe", "x"])
        self.assertEqual(caught.exception.error, "invalid_notify_target")

    def test_dry_run_payload_reports_target_and_post_argv(self) -> None:
        import dataclasses

        from tests.execution_test_base import ExecutionTestBase

        base = ExecutionTestBase()
        base.setUp()
        try:
            request = base.build_git_request(
                "codex", "safe", None, "/repo", "review", self.delegate.DEFAULT_CONFIG, dry_run=True
            )
        except AttributeError:
            self.skipTest("execution test base lacks build_git_request")
        request = dataclasses.replace(request, notify="room:devbox")
        payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["notify"]["target"], "room:devbox")
        self.assertEqual(payload["notify"]["argv"][:4], ["post", "send", "--to", "devbox"])
