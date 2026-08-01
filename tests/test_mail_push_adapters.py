from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from delegate_agent import cli, mail, profile_guard, run_registry
from tests.delegate_commands_test_base import CommandTestBase

# The plan's five-harness list is the planning-time CANDIDATE set; the wave-2
# audit (adjudicated 2026-08-01, recorded in the plan) verified only claude and
# codex — grok/cursor/droid lack evidenced Stop-hook injection and degrade to
# pull. Promoting a row requires new audit evidence, not just editing this set.
WAVE_2_VERIFIED = {"claude", "codex"}
WAVE_2_CANDIDATES_DEGRADED = {"grok", "cursor", "droid"}


class MailPushAdapterTests(CommandTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-mail-push-adapters-")
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
                "status": run_registry.STATUS_RUNNING,
                "pid": os.getpid(),
            },
        )
        self.user_home = Path(self._config_env["HOME"])
        self.canaries = {
            self.user_home / ".claude" / "settings.json": b'{"canary":"claude"}\n',
            self.user_home / ".codex" / "config.toml": b"canary = 'codex'\n",
            self.user_home / ".cursor" / "hooks.json": b'{"canary":"cursor"}\n',
            self.user_home / ".grok" / "config.toml": b"canary = 'grok'\n",
            self.user_home / ".factory" / "settings.json": b'{"canary":"droid"}\n',
        }
        for path, contents in self.canaries.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)

    def _env(self) -> dict[str, str]:
        return {
            "DELEGATE_RUN_ID": self.run_id,
            "DELEGATE_MAIL_SELF": self.alias,
            "HOME": str(self.user_home),
            "CODEX_HOME": str(self.user_home / ".codex"),
        }

    def test_wave_2_adapter_rows_are_verified_for_the_named_harnesses(self):
        self.assertEqual(
            {
                engine
                for engine, status in mail.MAIL_PUSH_ADAPTER_ROWS.items()
                if status == "verified"
            },
            WAVE_2_VERIFIED,
        )
        self.assertEqual(set(mail.MAIL_PUSH_ADAPTER_ROWS), set(mail.MAIL_SANDBOX_ROWS))
        for engine in WAVE_2_CANDIDATES_DEGRADED:
            self.assertNotEqual(mail.MAIL_PUSH_ADAPTER_ROWS[engine], "verified", engine)

    def test_verified_adapters_use_run_scoped_files_and_their_audited_channel(self):
        argv_by_engine = {
            "grok": ["grok", "prompt"],
            "claude": ["claude", "prompt"],
            "codex": ["codex", "exec", "prompt"],
            "cursor": ["cursor", "prompt"],
            "droid": ["droid", "model", "prompt"],
        }
        for engine in sorted(WAVE_2_VERIFIED):
            with self.subTest(engine=engine):
                env = self._env()
                argv = argv_by_engine[engine]
                before_files = {
                    path.resolve() for path in self.workspace.rglob("*") if path.is_file()
                }
                provision = mail.provision_mail_push(
                    engine,
                    argv,
                    list(argv),
                    self.registry_root,
                    self.run_id,
                    env,
                )
                self.assertIsNone(provision.warning)
                box = mail.boxes_root(self.registry_root) / self.run_id
                settings_path = box / mail.MAIL_PUSH_SETTINGS_FILE_NAME
                cursor_path = box / mail.MAIL_PUSH_CURSOR_FILE_NAME
                self.assertTrue(settings_path.is_file())
                self.assertTrue(cursor_path.is_file())
                self.assertEqual(env["DELEGATE_MAIL_HOOK_HARNESS"], engine)
                self.assertEqual(
                    json.loads(settings_path.read_text(encoding="utf-8")),
                    mail._hook_settings(),
                )
                self.assertEqual(json.loads(cursor_path.read_text(encoding="utf-8"))["lastSeq"], 0)
                if engine == "claude":
                    self.assertIn("--settings", provision.argv)
                    settings_arg = Path(
                        provision.argv[provision.argv.index("--settings") + 1]
                    ).resolve()
                    self.assertEqual(settings_arg, settings_path.resolve())
                elif engine == "codex":
                    self.assertIn("hooks=true", provision.argv)
                    self.assertIn("--dangerously-bypass-hook-trust", provision.argv)
                    self.assertTrue(
                        Path(env["CODEX_HOME"])
                        .resolve()
                        .is_relative_to(run_registry.run_directory(self.registry_root, self.run_id))
                    )
                    self.assertTrue(Path(env["CODEX_HOME"]).joinpath("hooks.json").is_file())
                    self.assertFalse((box / "codex-home").exists())
                else:
                    self.assertNotEqual(provision.argv, argv)

                for path, contents in self.canaries.items():
                    self.assertEqual(path.read_bytes(), contents, path)
                created_files = {
                    path.resolve()
                    for path in self.workspace.rglob("*")
                    if path.is_file() and path.resolve() not in before_files
                }
                self.assertTrue(created_files)
                self.assertTrue(
                    all(
                        path.is_relative_to(box.resolve())
                        or path.is_relative_to(
                            run_registry.run_directory(self.registry_root, self.run_id)
                        )
                        for path in created_files
                    ),
                    created_files,
                )

    def test_unverified_rows_never_provision_or_mutate_environment(self):
        for engine in sorted(set(mail.MAIL_PUSH_ADAPTER_ROWS) - WAVE_2_VERIFIED):
            with self.subTest(engine=engine):
                env = self._env()
                before = dict(env)
                argv = [engine, "prompt"]
                provision = mail.provision_mail_push(
                    engine,
                    argv,
                    None,
                    self.registry_root,
                    self.run_id,
                    env,
                )
                self.assertIsNotNone(provision.warning)
                self.assertEqual(provision.argv, argv)
                self.assertEqual(env, before)
                self.assertFalse((mail.boxes_root(self.registry_root) / self.run_id).exists())

    def test_provisioning_is_absent_without_mail_push_flag(self):
        parsed = cli.parse_cli(["claude", "work", "prompt"])
        self.assertFalse(parsed.launch.mail_push)
        self.assertFalse((mail.boxes_root(self.registry_root) / self.run_id).exists())

    def test_hook_pump_is_classified_as_a_mutation_by_both_python_and_shell_guards(self):
        parsed = cli.parse_cli(["mail", "hook-pump"])
        self.assertFalse(profile_guard.is_read_only_command(parsed))
        shim = Path(__file__).resolve().parents[1] / "bin" / "delegate-profile-shim"
        shim_text = shim.read_text(encoding="utf-8")
        self.assertIn("inbox|status|watch)", shim_text)


if __name__ == "__main__":
    unittest.main()
