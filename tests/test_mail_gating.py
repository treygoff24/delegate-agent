from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from delegate_agent import config as delegate_config
from delegate_agent import mail, request_build, run_registry, runner
from delegate_agent.constants import (
    KNOWN_ENGINES,
    PROMPT_INSTRUCTION_MODE_SLASH,
    PROMPT_INSTRUCTION_MODE_WRAPPED,
)
from delegate_agent.request_models import ResolvedWorkspace
from tests.delegate_commands_test_base import CommandTestBase


class MailGatingTests(CommandTestBase):
    def _config(self, enabled: bool | None = None) -> dict:
        config = delegate_config.embedded_default_config()
        if enabled is not None:
            config["mail"] = {"enabled": enabled}
        return config

    def test_mail_config_defaults_false_and_unknown_keys_are_rejected(self):
        config = self._config()
        self.assertFalse(delegate_config.mail_enabled(config))
        delegate_config.validate_config(config)

        config["mail"] = {"enabled": False, "unexpected": True}
        with self.assertRaises(delegate_config.ConfigError) as caught:
            delegate_config.validate_config(config)
        self.assertEqual(caught.exception.error, "invalid_mail_config")

        for invalid in ("yes", 1, []):
            with self.subTest(invalid=invalid):
                config = self._config()
                config["mail"] = invalid
                with self.assertRaises(delegate_config.ConfigError) as caught:
                    delegate_config.validate_config(config)
                self.assertEqual(caught.exception.error, "invalid_mail_config")

    def test_mail_commands_work_when_injection_flag_is_disabled(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-command-") as tmp:
            workspace = Path(tmp)
            config_path = Path(self._config_env["DELEGATE_CONFIG"])
            config_path.write_text(json.dumps({"mail": {"enabled": False}}), encoding="utf-8")
            code, stdout, stderr = self.run_main(
                [
                    "--json",
                    "--cwd",
                    str(workspace),
                    "mail",
                    "send",
                    "--to",
                    "coordinator",
                    "body",
                ]
            )
            self.assertEqual(code, 0, stderr)
            self.assertTrue(json.loads(stdout)["ok"])

    def test_suffix_gate_matches_wrapping_predicate(self):
        cases = (
            ("work", PROMPT_INSTRUCTION_MODE_WRAPPED, False, True),
            ("work", PROMPT_INSTRUCTION_MODE_SLASH, False, False),
            ("work", PROMPT_INSTRUCTION_MODE_WRAPPED, True, False),
            ("safe", PROMPT_INSTRUCTION_MODE_WRAPPED, False, False),
            ("call", PROMPT_INSTRUCTION_MODE_WRAPPED, False, False),
        )
        for mode, instruction_mode, pass_through, expected in cases:
            with self.subTest(
                mode=mode, instruction_mode=instruction_mode, pass_through=pass_through
            ):
                enabled = self.build_git_request(
                    "codex",
                    mode,
                    None,
                    "/repo",
                    "prompt",
                    self._config(True),
                    False,
                    pass_through=pass_through,
                    prompt_instruction_mode=instruction_mode,
                    frame_prompt=True,
                )
                disabled = self.build_git_request(
                    "codex",
                    mode,
                    None,
                    "/repo",
                    "prompt",
                    self._config(False),
                    False,
                    pass_through=pass_through,
                    prompt_instruction_mode=instruction_mode,
                    frame_prompt=True,
                )
                self.assertEqual(mail.MAIL_PROMPT_SUFFIX in enabled.prompt, expected)
                self.assertNotIn(mail.MAIL_PROMPT_SUFFIX, disabled.prompt)

    def test_mail_push_is_plumbed_through_input_request_dry_run_context_and_manifest(self):
        parsed = self.delegate.parse_cli(["codex", "work", "--mail-push", "prompt"])
        self.assertTrue(parsed.launch.mail_push)
        self.assertIn("mailPush", request_build.RUN_INPUT_KEYS)

        request = self.build_git_request(
            "codex",
            "work",
            None,
            "/repo",
            "prompt",
            self._config(True),
            True,
            mail_push=True,
            frame_prompt=True,
        )
        self.assertTrue(request.mail_push)
        self.assertTrue(self.delegate.dry_run_payload(request)["mailPush"])

        with tempfile.TemporaryDirectory(prefix="delegate-mail-manifest-") as tmp:
            workspace = Path(tmp)
            registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, alias = run_registry.register_run(
                registry_root,
                harness="codex",
                metadata={"mode": "work", "cwd": str(workspace)},
            )
            context = self.delegate.make_run_context(
                registry_root,
                request,
                run_id=run_id,
                alias=alias,
                source_workspace=ResolvedWorkspace("/repo", "directory"),
            )
            self.assertTrue(context.mail_push)
            manifest = runner.build_manifest(context, request.display_argv or request.argv)
            self.assertTrue(manifest["mailPush"])

    def test_input_json_mail_push_is_accepted_and_recorded(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-input-") as tmp:
            workspace = Path(tmp)
            input_path = workspace / "input.json"
            input_path.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "work",
                        "prompt": "input prompt",
                        "mailPush": True,
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.parse_cli(
                ["--cwd", str(workspace), "run", "--input-json", str(input_path)]
            )
            request = request_build.request_from_input_json(parsed, self._config(True))
            self.assertTrue(request.mail_push)

    def test_degraded_harness_launch_warns_and_does_not_claim_scoping(self):
        stderr = io.StringIO()
        argv = ["grok", "work", "prompt"]
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            result = mail.wire_work_mail_argv("grok", argv, Path(tmp), stderr=stderr)
            self.assertEqual(result, argv)
            self.assertTrue((Path(tmp) / "mail").is_dir())
        self.assertEqual(
            stderr.getvalue(),
            "delegate mail: WARNING: grok work launch is workspace-writable; "
            "it is not filesystem-scoped to .delegate/mail.\n",
        )

    def test_mail_sandbox_table_lists_every_known_engine(self):
        self.assertEqual(set(mail.MAIL_SANDBOX_ROWS), set(KNOWN_ENGINES))

    def test_scoped_harnesses_add_only_their_declared_mail_grant(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            root = Path(tmp)
            mail_root = str((root / "mail").resolve())
            cases = {
                "codex": ["-c", f'sandbox_workspace_write.writable_roots=["{mail_root}"]'],
                "cursor": ["--sandbox", "enabled", "--add-dir", mail_root],
                "kimi": ["--add-dir", mail_root],
                "claude": ["--add-dir", mail_root],
                "omp": [f"--add-dir={mail_root}"],
            }
            for engine, expected in cases.items():
                with self.subTest(engine=engine):
                    argv = mail.wire_work_mail_argv(
                        engine,
                        [engine, "work", "prompt"],
                        root,
                        isolated_workspace=engine == "codex",
                    )
                    for flag in expected:
                        self.assertIn(flag, argv)
                    self.assertTrue((root / "mail").is_dir())

    def test_workspace_writable_harnesses_warn(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            for engine in ("droid", "grok", "devin", "opencode", "pi"):
                with self.subTest(engine=engine):
                    stderr = io.StringIO()
                    self.assertEqual(
                        mail.wire_work_mail_argv(
                            engine, [engine, "work"], Path(tmp), stderr=stderr
                        ),
                        [engine, "work"],
                    )
                    self.assertIn("workspace-writable", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
