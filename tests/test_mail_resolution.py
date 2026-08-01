from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from delegate_agent import mail, private_io, request_build, run_registry
from delegate_agent.errors import DelegateError


class MailResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="delegate-mail-resolution-", dir=str(Path(__file__).resolve().parents[3])
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

    def test_outside_lane_explicit_cwd_beats_process_cwd(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        with mock.patch("os.getcwd", return_value=str(self.workspace)):
            resolved = mail.resolve_mail_workspace(str(other), env={})
        self.assertEqual(resolved, other.resolve())

    def test_validated_lane_uses_source_root_and_rejects_conflicting_cwd(self) -> None:
        run_id, _alias = self._run()
        source = self.workspace / "source"
        source.mkdir()
        # The registry root is authoritative for this test; use a plain directory
        # workspace so the expected source root is exact and deterministic.
        conflict = self.workspace / "other"
        conflict.mkdir()
        with self.assertRaises(DelegateError) as ctx:
            mail.resolve_mail_workspace(
                str(conflict),
                env={
                    "DELEGATE_RUN_ID": run_id,
                    "DELEGATE_SOURCE_ROOT": str(source),
                },
            )
        self.assertEqual(ctx.exception.error, "conflicting_cwd")

        self.assertEqual(
            mail.resolve_mail_workspace(
                None,
                env={
                    "DELEGATE_RUN_ID": run_id,
                    "DELEGATE_SOURCE_ROOT": str(self.workspace),
                },
            ),
            self.workspace.resolve(),
        )

    def test_validated_lane_accepts_hook_pinned_cwd_without_source_environment(self) -> None:
        run_id, _alias = self._run()

        self.assertEqual(
            mail.resolve_mail_workspace(str(self.workspace), env={"DELEGATE_RUN_ID": run_id}),
            self.workspace.resolve(),
        )

    def test_mail_resolution_does_not_change_global_workspace_resolution(self) -> None:
        explicit = self.workspace / "explicit"
        explicit.mkdir()
        with mock.patch.dict(
            os.environ,
            {"DELEGATE_RUN_ID": "del_unknown", "DELEGATE_SOURCE_ROOT": str(self.workspace)},
            clear=False,
        ):
            with self.assertRaises(DelegateError):
                mail.resolve_mail_workspace(None)
            resolved = request_build.resolve_workspace(str(explicit))
        self.assertEqual(resolved.path, str(explicit.resolve()))

    def test_send_holds_registry_lock_through_validation_and_publication(self) -> None:
        sender_id, sender = self._run()
        _recipient_id, recipient = self._run()
        events: list[str] = []
        real_lock = run_registry.registry_lock
        real_recipient = mail._recipient_for_alias
        real_publish = private_io.write_bytes_atomic_if_absent

        @contextmanager
        def tracked_lock(root: Path):
            events.append("lock-enter")
            with real_lock(root):
                yield
            events.append("lock-exit")

        def validate(*args, **kwargs):
            events.append("validate")
            return real_recipient(*args, **kwargs)

        def publish(*args, **kwargs):
            events.append("publish")
            return real_publish(*args, **kwargs)

        with (
            mock.patch.object(run_registry, "registry_lock", tracked_lock),
            mock.patch.object(mail, "_recipient_for_alias", side_effect=validate),
            mock.patch.object(private_io, "write_bytes_atomic_if_absent", side_effect=publish),
        ):
            mail.send(
                self.root,
                mail.MailCommand(action="send", to=recipient, body="locked"),
                env={"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender},
            )
        self.assertEqual(events[0], "lock-enter")
        self.assertLess(events.index("lock-enter"), events.index("validate"))
        self.assertLess(events.index("validate"), events.index("publish"))
        self.assertEqual(events[-1], "lock-exit")

    def test_dry_run_prune_uses_an_unlocked_best_effort_snapshot(self) -> None:
        sender_id, sender = self._run()
        _recipient_id, recipient = self._run()
        publication_started = threading.Event()
        release_publication = threading.Event()
        prune_finished = threading.Event()
        real_publish = private_io.write_bytes_atomic_if_absent

        def blocked_publish(*args, **kwargs):
            publication_started.set()
            self.assertTrue(release_publication.wait(timeout=5))
            return real_publish(*args, **kwargs)

        send_error: list[BaseException] = []

        def do_send() -> None:
            try:
                mail.send(
                    self.root,
                    mail.MailCommand(action="send", to=recipient, body="race"),
                    env={"DELEGATE_RUN_ID": sender_id, "DELEGATE_MAIL_SELF": sender},
                )
            except BaseException as exc:  # surfaced below, preserving thread failures
                send_error.append(exc)

        with mock.patch.object(
            private_io, "write_bytes_atomic_if_absent", side_effect=blocked_publish
        ):
            send_thread = threading.Thread(target=do_send)
            send_thread.start()
            self.assertTrue(publication_started.wait(timeout=5))

            def do_prune() -> None:
                mail.prune(self.root, mail.MailCommand(action="prune", dry_run=True))
                prune_finished.set()

            prune_thread = threading.Thread(target=do_prune)
            prune_thread.start()
            time.sleep(0.1)
            self.assertTrue(prune_finished.is_set())
            release_publication.set()
            send_thread.join(timeout=5)
            prune_thread.join(timeout=5)

        self.assertFalse(send_error)
        self.assertTrue(prune_finished.is_set())


if __name__ == "__main__":
    unittest.main()
