from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_nonisolated_work_launch_is_already_workspace_writable_without_a_warning(self):
        stderr = io.StringIO()
        argv = ["grok", "work", "prompt"]
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            result = mail.wire_work_mail_argv("grok", argv, Path(tmp), stderr=stderr)
            self.assertEqual(result, argv)
            self.assertFalse((Path(tmp) / "mail").exists())
        self.assertEqual(stderr.getvalue(), "")

    def test_mail_sandbox_table_lists_every_known_engine(self):
        self.assertEqual(set(mail.MAIL_SANDBOX_ROWS), set(KNOWN_ENGINES))

    def test_scoped_harnesses_add_only_their_declared_mail_grant(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            root = Path(tmp)
            mail_root = str((root / "mail").resolve())
            cases = {
                "codex": ["-c", f'sandbox_workspace_write.writable_roots=["{mail_root}"]'],
                "kimi": ["--add-dir", mail_root],
                "claude": ["--add-dir", mail_root],
                "omp": [f"--add-dir={mail_root}"],
            }
            for engine, expected in cases.items():
                with self.subTest(engine=engine):
                    argv = [engine, "work", "prompt"]
                    if engine == "codex":
                        argv = [engine, "exec", "--sandbox", "workspace-write", "prompt"]
                    argv = mail.wire_work_mail_argv(
                        engine,
                        argv,
                        root,
                        isolated_workspace=True,
                    )
                    for flag in expected:
                        self.assertIn(flag, argv)
                    self.assertFalse((root / "mail").exists())

    def test_cursor_default_work_argv_is_not_changed_to_enable_a_sandbox(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            argv = mail.wire_work_mail_argv(
                "cursor", ["cursor", "work", "prompt"], Path(tmp), isolated_workspace=True
            )
        self.assertEqual(argv, ["cursor", "work", "prompt"])
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--add-dir", argv)

    def test_effective_sandbox_policy_controls_mail_grants_and_warnings(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            root = Path(tmp)
            mail_root = str((root / "mail").resolve())
            cases = {
                "codex workspace-write": (
                    "codex",
                    ["codex", "exec", "--sandbox", "workspace-write", "prompt"],
                    True,
                    "",
                ),
                "codex read-only": (
                    "codex",
                    ["codex", "exec", "--sandbox", "read-only", "prompt"],
                    False,
                    "delegate mail: WARNING: codex work launch sandbox policy read-only cannot reach .delegate/mail from this isolated workspace.\n",
                ),
                "codex danger-full-access": (
                    "codex",
                    ["codex", "exec", "--sandbox", "danger-full-access", "prompt"],
                    False,
                    "",
                ),
                "codex bypass": (
                    "codex",
                    ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "prompt"],
                    False,
                    "",
                ),
                "codex unknown": (
                    "codex",
                    ["codex", "exec", "--sandbox", "future-policy", "prompt"],
                    False,
                    "delegate mail: WARNING: codex work launch sandbox policy future-policy cannot reach .delegate/mail from this isolated workspace.\n",
                ),
                "grok absent": ("grok", ["grok", "--cwd", "/tmp", "prompt"], False, ""),
                "grok none": (
                    "grok",
                    ["grok", "--sandbox", "none", "prompt"],
                    False,
                    "",
                ),
                "grok workspace": (
                    "grok",
                    ["grok", "--sandbox", "workspace", "prompt"],
                    False,
                    "",
                ),
                "grok devbox": (
                    "grok",
                    ["grok", "--sandbox", "devbox", "prompt"],
                    False,
                    "delegate mail: WARNING: grok work launch sandbox policy devbox cannot reach .delegate/mail from this isolated workspace.\n",
                ),
                "grok read-only": (
                    "grok",
                    ["grok", "--sandbox", "read-only", "prompt"],
                    False,
                    "delegate mail: WARNING: grok work launch sandbox policy read-only cannot reach .delegate/mail from this isolated workspace.\n",
                ),
                "grok strict": (
                    "grok",
                    ["grok", "--sandbox", "strict", "prompt"],
                    False,
                    "delegate mail: WARNING: grok work launch sandbox policy strict cannot reach .delegate/mail from this isolated workspace.\n",
                ),
                "grok unknown": (
                    "grok",
                    ["grok", "--sandbox", "future-policy", "prompt"],
                    False,
                    "delegate mail: WARNING: grok work launch sandbox policy future-policy cannot reach .delegate/mail from this isolated workspace.\n",
                ),
            }
            for name, (engine, argv, granted, expected_stderr) in cases.items():
                with self.subTest(policy=name):
                    stderr = io.StringIO()
                    result = mail.wire_work_mail_argv(
                        engine, argv, root, stderr=stderr, isolated_workspace=True
                    )
                    grant = f'sandbox_workspace_write.writable_roots=["{mail_root}"]'
                    self.assertEqual(grant in result, granted)
                    self.assertEqual(stderr.getvalue(), expected_stderr)

    def test_nonisolated_codex_has_no_writable_root_grant(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-sandbox-") as tmp:
            result = mail.wire_work_mail_argv(
                "codex",
                ["codex", "exec", "--sandbox", "workspace-write", "prompt"],
                Path(tmp),
                isolated_workspace=False,
            )
        self.assertNotIn("sandbox_workspace_write.writable_roots", " ".join(result))

    def test_mail_storage_failure_refuses_work_launch_before_run_or_alias_claim(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-storage-") as tmp:
            workspace = Path(tmp)
            # Cursor's default argvPrefix is ["agent"], so the fake must be
            # named exactly that — any other name makes this test depend on the
            # host's real cursor install (green locally, exit 3 on CI).
            fake_bin = self.write_fake_executable("agent")
            with mock.patch.object(
                mail,
                "prepare_mail_storage",
                side_effect=mail._error("mail_storage_unavailable", "blocked"),
            ):
                code, _stdout, stderr = self.run_main(
                    ["--cwd", str(workspace), "cursor", "work", "prompt"], path_prefix=fake_bin
                )
            self.assertEqual(code, 2)
            self.assertIn("mail_storage_unavailable", stderr)
            registry_root = workspace / ".delegate"
            index = run_registry.load_index(registry_root)
            self.assertEqual(index["runs"], {})
            self.assertEqual(list((registry_root / "aliases").iterdir()), [])

    def test_work_pass_through_strips_profile_supplied_mail_identity(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-pass-through-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            capture = root / "child-env"
            fake = root / "cursor"
            fake.write_text(
                '#!/bin/sh\nenv | grep "^DELEGATE_" > "$FAKE_ENV_LOG" || true\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = self._config()
            config["cursor"] = {**config["cursor"], "argvPrefix": [str(fake)]}
            config["profiles"] = {
                **config["profiles"],
                "default": "identity-profile",
                "definitions": {
                    "identity-profile": {
                        "env": {
                            "DELEGATE_RUN_ID": "del_20260801T120000Z_abcdef",
                            "DELEGATE_MAIL_SELF": "cursor-99",
                        }
                    }
                },
            }
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(config), encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"FAKE_ENV_LOG": str(capture)}, clear=False):
                code, _stdout, stderr = self.run_main(
                    ["--pass-through", "--cwd", str(workspace), "cursor", "work", "prompt"]
                )
            self.assertEqual(code, 0, stderr)
            observed = capture.read_text(encoding="utf-8")
            self.assertNotIn("DELEGATE_RUN_ID=", observed)
            self.assertNotIn("DELEGATE_MAIL_SELF=", observed)

    def test_real_child_launches_bind_identity_only_for_tracked_work(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-launch-matrix-") as tmp:
            root = Path(tmp)
            args_path = root / "args"
            env_path = root / "env"
            fake = root / "fake-agent"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['FAKE_ARGS_LOG'], 'w', encoding='utf-8') as handle:\n"
                "    json.dump(sys.argv[1:], handle)\n"
                "with open(os.environ['FAKE_ENV_LOG'], 'w', encoding='utf-8') as handle:\n"
                "    json.dump({key: value for key, value in os.environ.items() if key.startswith('DELEGATE_')}, handle)\n"
                "print(json.dumps([{'type': 'result', 'result': 'ok', 'permission_denials': []}]))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = self._config()
            config["cursor"] = {**config["cursor"], "argvPrefix": [str(fake)]}
            config["claude"] = {**config["claude"], "binary": str(fake)}
            config["profiles"] = {
                **config["profiles"],
                "default": "identity-profile",
                "definitions": {
                    "identity-profile": {
                        "env": {
                            "DELEGATE_RUN_ID": "del_20260801T120000Z_abcdef",
                            "DELEGATE_MAIL_SELF": "cursor-99",
                            "FAKE_ARGS_LOG": str(args_path),
                            "FAKE_ENV_LOG": str(env_path),
                        }
                    }
                },
            }
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(config), encoding="utf-8"
            )

            cases = (
                ("tracked work", ["cursor", "work", "prompt"], True),
                ("safe", ["cursor", "safe", "prompt"], True),
                ("grouped call", ["--group", "batch", "cursor", "call", "prompt"], True),
                ("untracked call", ["cursor", "call", "prompt"], False),
                ("read-only call", ["cursor", "call", "--read-only", "prompt"], False),
                ("pure call", ["claude", "call", "--pure", "prompt"], False),
            )
            for name, command, tracked in cases:
                with self.subTest(name=name):
                    workspace = root / name.replace(" ", "-")
                    workspace.mkdir()
                    args_path.unlink(missing_ok=True)
                    env_path.unlink(missing_ok=True)
                    if "call" in command:
                        original_cwd = os.getcwd()
                        try:
                            os.chdir(workspace)
                            code, _stdout, stderr = self.run_main(command)
                        finally:
                            os.chdir(original_cwd)
                    else:
                        code, _stdout, stderr = self.run_main(["--cwd", str(workspace), *command])
                    self.assertEqual(code, 0, stderr)
                    observed_argv = json.loads(args_path.read_text(encoding="utf-8"))
                    observed_env = json.loads(env_path.read_text(encoding="utf-8"))
                    if name == "tracked work":
                        # The prefixes alone would also match the profile-planted
                        # spoof values, so require the FRESH bind: equality with
                        # this run's manifest and inequality with the plant.
                        run_manifests = list(
                            (workspace / ".delegate" / "runs").glob("*/manifest.json")
                        )
                        self.assertEqual(len(run_manifests), 1)
                        run_manifest = json.loads(run_manifests[0].read_text(encoding="utf-8"))
                        self.assertEqual(observed_env["DELEGATE_RUN_ID"], run_manifest["runId"])
                        self.assertEqual(observed_env["DELEGATE_MAIL_SELF"], run_manifest["alias"])
                        self.assertNotEqual(
                            observed_env["DELEGATE_RUN_ID"], "del_20260801T120000Z_abcdef"
                        )
                        self.assertNotEqual(observed_env["DELEGATE_MAIL_SELF"], "cursor-99")
                    else:
                        self.assertNotIn("DELEGATE_RUN_ID", observed_env)
                        self.assertNotIn("DELEGATE_MAIL_SELF", observed_env)
                    manifests = list((workspace / ".delegate" / "runs").glob("*/manifest.json"))
                    self.assertEqual(bool(manifests), tracked)
                    if manifests:
                        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
                        self.assertEqual(manifest["argv"][1:-1], observed_argv[:-1])
                        self.assertIn("<prompt redacted:", manifest["argv"][-1])


if __name__ == "__main__":
    unittest.main()
