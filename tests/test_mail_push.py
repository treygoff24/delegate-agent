from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import mail, run_registry, run_status, runner


class _FailingWriter(io.StringIO):
    def write(self, _text: str) -> int:
        raise OSError("simulated hook output failure")


class MailPushTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-push-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        self.run_id, self.alias = run_registry.register_run(
            self.registry_root,
            harness="claude",
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

    def _send_from_coordinator(self, body: str) -> dict:
        return mail.send(
            self.registry_root,
            mail.MailCommand(action="send", to=self.alias, body=body),
        )["message"]

    def test_hook_is_non_consuming_framed_and_cursor_suppresses_duplicates(self):
        first = self._send_from_coordinator("first")
        provision = mail.provision_mail_push(
            "claude", ["claude", "prompt"], None, self.registry_root, self.run_id, self.env
        )
        self.assertIsNone(provision.warning)

        output = io.StringIO()
        self.assertEqual(mail.hook_pump(self.registry_root, stdout=output, env=self.env), 0)
        response = json.loads(output.getvalue())
        self.assertEqual(response["decision"], "block")
        payload = json.loads(response["reason"])
        self.assertEqual(payload["messages"][0]["message"]["msgId"], first["msgId"])
        self.assertEqual(payload["messages"][0]["framing"], mail.LANE_FRAMING)
        self.assertEqual(payload["framing"], mail.LANE_FRAMING)

        inbox = mail.inbox(self.registry_root, mail.MailCommand(action="inbox"), env=self.env)
        self.assertEqual(len(inbox["messages"]), 1)
        cursor = json.loads(
            (
                mail.boxes_root(self.registry_root) / self.run_id / mail.MAIL_PUSH_CURSOR_FILE_NAME
            ).read_text()
        )
        self.assertEqual(cursor["lastSeq"], 0)

        replay = io.StringIO()
        self.assertEqual(mail.hook_pump(self.registry_root, stdout=replay, env=self.env), 0)
        self.assertEqual(json.loads(replay.getvalue()), {})
        self.assertEqual(
            json.loads(
                (
                    mail.boxes_root(self.registry_root)
                    / self.run_id
                    / mail.MAIL_PUSH_CURSOR_FILE_NAME
                ).read_text()
            )["lastSeq"],
            first["seq"],
        )

        duplicate = io.StringIO()
        self.assertEqual(mail.hook_pump(self.registry_root, stdout=duplicate, env=self.env), 0)
        self.assertEqual(json.loads(duplicate.getvalue()), {})

    def test_hook_output_failure_does_not_advance_cursor_and_records_marker(self):
        self._send_from_coordinator("retry me")
        mail.provision_mail_push(
            "claude", ["claude", "prompt"], None, self.registry_root, self.run_id, self.env
        )
        self.assertEqual(
            mail.hook_pump(self.registry_root, stdout=_FailingWriter(), env=self.env), 1
        )
        cursor_path = (
            mail.boxes_root(self.registry_root) / self.run_id / mail.MAIL_PUSH_CURSOR_FILE_NAME
        )
        self.assertEqual(json.loads(cursor_path.read_text())["lastSeq"], 0)
        marker = mail.read_hook_failure_marker(self.registry_root, self.run_id)
        self.assertIsNotNone(marker)

    def test_provisioning_is_audited_and_run_scoped(self):
        claude_env: dict[str, str] = {}
        claude = mail.provision_mail_push(
            "claude",
            ["claude", "prompt"],
            ["claude", "prompt"],
            self.registry_root,
            self.run_id,
            claude_env,
        )
        self.assertIsNone(claude.warning)
        self.assertIn("--settings", claude.argv)
        self.assertEqual(claude_env["DELEGATE_MAIL_HOOK_HARNESS"], "claude")
        self.assertTrue(
            (
                mail.boxes_root(self.registry_root)
                / self.run_id
                / mail.MAIL_PUSH_SETTINGS_FILE_NAME
            ).is_file()
        )

        codex_source = self.workspace / "source-codex-home"
        codex_source.mkdir()
        (codex_source / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
        (codex_source / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
        codex_env = {"CODEX_HOME": str(codex_source)}
        codex = mail.provision_mail_push(
            "codex",
            ["codex", "exec", "prompt"],
            ["codex", "exec", "prompt"],
            self.registry_root,
            self.run_id,
            codex_env,
        )
        self.assertIsNone(codex.warning)
        self.assertIn("hooks=true", codex.argv)
        self.assertIn("--dangerously-bypass-hook-trust", codex.argv)
        self.assertEqual(codex_env["DELEGATE_MAIL_HOOK_HARNESS"], "codex")
        self.assertEqual(codex_env["CODEX_HOME"], codex.codex_home)
        self.assertTrue(Path(codex.codex_home or "").joinpath("auth.json").is_file())
        self.assertTrue(
            Path(codex.codex_home or "").is_relative_to(
                run_registry.run_directory(self.registry_root, self.run_id)
            )
        )
        self.assertFalse(
            (mail.boxes_root(self.registry_root) / self.run_id / "codex-home").exists()
        )
        self.assertEqual((codex_source / "auth.json").read_text(), '{"token":"test"}')

        before = dict(codex_env)
        unverified = mail.provision_mail_push(
            "cursor", ["cursor", "prompt"], None, self.registry_root, self.run_id, codex_env
        )
        self.assertIsNotNone(unverified.warning)
        self.assertEqual(codex_env, before)

    def test_codex_credentials_never_enter_mail_and_legacy_homes_are_swept(self):
        source = self.workspace / "source-codex-home"
        source.mkdir()
        (source / "auth.json").write_text('{"token":"mail-secret-canary"}', encoding="utf-8")
        (source / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
        legacy = mail.boxes_root(self.registry_root) / self.run_id / "codex-home"
        legacy.mkdir(parents=True)
        (legacy / "auth.json").write_text('{"token":"mail-secret-canary"}', encoding="utf-8")

        provision = mail.provision_mail_push(
            "codex",
            ["codex", "exec", "prompt"],
            None,
            self.registry_root,
            self.run_id,
            {"CODEX_HOME": str(source)},
        )

        self.assertIsNone(provision.warning)
        self.assertFalse(legacy.exists())
        self.assertTrue(Path(provision.codex_home or "").joinpath("auth.json").is_file())
        for path in mail.mail_root(self.registry_root).rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn("mail-secret-canary", path.read_text(encoding="utf-8"))

        mail.cleanup_mail_push_private_homes(self.registry_root, self.run_id)
        self.assertFalse(Path(provision.codex_home or "").exists())

    def test_codex_fallback_uses_a_second_private_home_only_when_present(self):
        primary = self.workspace / "primary-codex-home"
        fallback = self.workspace / "fallback-codex-home"
        for home, token in ((primary, "primary-token"), (fallback, "fallback-token")):
            home.mkdir()
            (home / "auth.json").write_text(f'{{"token":"{token}"}}', encoding="utf-8")
        provision = mail.provision_mail_push(
            "codex",
            ["codex", "exec", "prompt"],
            None,
            self.registry_root,
            self.run_id,
            {"CODEX_HOME": str(primary)},
        )

        self.assertEqual(
            mail.mail_push_fallback_env_overrides(provision, {}, self.registry_root, self.run_id),
            {},
        )
        fallback_env = mail.mail_push_fallback_env_overrides(
            provision,
            {"CODEX_HOME": str(fallback)},
            self.registry_root,
            self.run_id,
        )
        fallback_home = Path(fallback_env["CODEX_HOME"])
        self.assertNotEqual(fallback_home, Path(provision.codex_home or ""))
        self.assertEqual(
            fallback_home.joinpath("auth.json").read_text(encoding="utf-8"),
            '{"token":"fallback-token"}',
        )
        self.assertTrue(
            fallback_home.is_relative_to(
                run_registry.run_directory(self.registry_root, self.run_id)
            )
        )

    def test_fallback_home_failure_reverts_to_pull_without_aborting_launch(self):
        source = self.workspace / "source-codex-home"
        source.mkdir()
        (source / "auth.json").write_text('{"token":"primary"}', encoding="utf-8")
        env = {"CODEX_HOME": str(source)}
        original_argv = ["codex", "exec", "prompt"]
        provision = mail.provision_mail_push(
            "codex", original_argv, None, self.registry_root, self.run_id, env
        )

        with mock.patch.object(
            mail,
            "_codex_home_for_mail_push",
            side_effect=OSError("fallback home unavailable"),
        ):
            fallback = mail.mail_push_fallback_env_overrides(
                provision,
                {"CODEX_HOME": str(source)},
                self.registry_root,
                self.run_id,
            )

        self.assertEqual(fallback, {"CODEX_HOME": str(source)})
        self.assertEqual(provision.argv, original_argv)
        self.assertEqual(env, {"CODEX_HOME": str(source)})
        self.assertIsNotNone(provision.warning)
        self.assertFalse(
            (run_registry.run_directory(self.registry_root, self.run_id) / "codex-home").exists()
        )

    def test_unrecordable_hook_failure_emits_parent_observable_sentinel(self):
        mail.provision_mail_push(
            "claude", ["claude", "prompt"], None, self.registry_root, self.run_id, self.env
        )
        stderr = io.StringIO()
        with mock.patch.object(
            mail.private_io, "write_json_atomic_if_absent", side_effect=OSError("box unavailable")
        ):
            self.assertEqual(
                mail.hook_pump(
                    self.registry_root,
                    stdout=_FailingWriter(),
                    stderr=stderr,
                    env=self.env,
                ),
                1,
            )
        reason = mail.hook_failure_reason_from_stderr(
            stderr.getvalue(), nonce=self.env["DELEGATE_MAIL_HOOK_NONCE"]
        )
        self.assertEqual(reason, "simulated hook output failure")
        context = runner.RunContext(
            registry_root=self.registry_root,
            run_id=self.run_id,
            alias=self.alias,
            harness="claude",
            engine="claude",
            mode="work",
            model=None,
            source_cwd=str(self.workspace),
            execution_cwd=str(self.workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
            mail_push=True,
        )
        self.assertEqual(runner._mail_push_failure_marker(context, reason), reason)
        run_path = run_registry.run_directory(self.registry_root, self.run_id)
        runner.write_snapshot(run_path, {"recentEvents": [], "eventsTotal": 0, "warnings": []})
        runner.record_mail_push_degradation(
            self.registry_root, self.run_id, engine="claude", reason=reason or "missing"
        )
        runner.record_mail_push_degradation(
            self.registry_root, self.run_id, engine="claude", reason=reason or "missing"
        )
        snapshot = run_registry.load_run_snapshot(self.registry_root, self.run_id)
        self.assertEqual(snapshot["eventsTotal"], 1)

    def test_sentinel_requires_the_run_nonce_and_survives_later_stderr(self):
        mail.provision_mail_push(
            "claude", ["claude", "prompt"], None, self.registry_root, self.run_id, self.env
        )
        nonce = self.env["DELEGATE_MAIL_HOOK_NONCE"]
        forged = f"{mail.MAIL_PUSH_FAILURE_SENTINEL}wrong:forged\n"
        real = mail.hook_failure_sentinel(nonce, "recording unavailable")

        self.assertIsNone(mail.hook_failure_reason_from_stderr(forged, nonce=nonce))
        self.assertEqual(
            mail.hook_failure_reason_from_stderr(real + "\n" + ("x" * (100 * 1024)), nonce=nonce),
            "recording unavailable",
        )

    def test_degradation_is_recorded_once_in_state_events_and_snapshot(self):
        run_path = run_registry.run_directory(self.registry_root, self.run_id)
        runner.write_snapshot(
            run_path,
            {"recentEvents": [], "eventsTotal": 0, "warnings": []},
        )
        first = runner.record_mail_push_degradation(
            self.registry_root,
            self.run_id,
            engine="claude",
            reason="hook returned malformed output",
        )
        second = runner.record_mail_push_degradation(
            self.registry_root,
            self.run_id,
            engine="claude",
            reason="a different later failure",
        )
        self.assertEqual(first, second)
        state = run_registry.load_run_state(self.registry_root, self.run_id)
        snapshot = run_registry.load_run_snapshot(self.registry_root, self.run_id)
        self.assertTrue(state["mailPushDegraded"])
        self.assertEqual(state["mailPushWarning"], first)
        self.assertIn(first, snapshot["warnings"])
        self.assertEqual(snapshot["eventsTotal"], 1)
        self.assertEqual(
            [
                event
                for event in snapshot["recentEvents"]
                if event["kind"] == runner.MAIL_PUSH_EVENT_KIND
            ],
            [{"kind": runner.MAIL_PUSH_EVENT_KIND, "message": first}],
        )


if __name__ == "__main__":
    unittest.main()
