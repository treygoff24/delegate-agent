from __future__ import annotations

import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import config as delegate_config
from delegate_agent import mail, runner
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_SLASH
from delegate_agent.errors import DelegateError
from tests.delegate_commands_test_base import CommandTestBase


class MailPushGatingTests(CommandTestBase):
    def _config(self, enabled: bool) -> dict:
        config = delegate_config.embedded_default_config()
        config["mail"] = {"enabled": enabled}
        return config

    def _write_fake_harness(self, root: Path) -> tuple[Path, Path, Path]:
        args_path = root / "args.json"
        env_path = root / "env.json"
        fake = root / "fake-harness"
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
        return fake, args_path, env_path

    def _launch_config(self, engine: str, fake: Path, enabled: bool) -> dict:
        config = self._config(enabled)
        if engine == "cursor":
            config[engine] = {**config[engine], "argvPrefix": [str(fake)]}
        else:
            config[engine] = {**config[engine], "binary": str(fake)}
        return config

    @staticmethod
    def _run_path(workspace: Path) -> Path:
        manifests = list((workspace / ".delegate" / "runs").glob("*/manifest.json"))
        if len(manifests) != 1:
            raise AssertionError(f"expected one run manifest, found {manifests}")
        return manifests[0].parent

    def test_mail_push_refuses_slash_and_pass_through_with_typed_errors(self):
        cases = (
            {
                "prompt_instruction_mode": PROMPT_INSTRUCTION_MODE_SLASH,
                "error": "mail_push_unsupported",
            },
            {"pass_through": True, "error": "mail_push_unsupported"},
        )
        for case in cases:
            with self.subTest(case=case):
                kwargs = {
                    "mail_push": True,
                    "frame_prompt": True,
                    **{key: value for key, value in case.items() if key != "error"},
                }
                with self.assertRaises(DelegateError) as caught:
                    self.build_git_request(
                        "cursor",
                        "work",
                        None,
                        "/repo",
                        "prompt",
                        self._config(True),
                        False,
                        **kwargs,
                    )
                self.assertEqual(caught.exception.error, case["error"])

    def test_mail_push_requires_mail_enabled(self):
        with self.assertRaises(DelegateError) as caught:
            self.build_git_request(
                "cursor",
                "work",
                None,
                "/repo",
                "prompt",
                self._config(False),
                False,
                mail_push=True,
                frame_prompt=True,
            )
        self.assertEqual(caught.exception.error, "mail_push_disabled")

    def test_mail_enabled_does_not_auto_enable_push_without_the_flag(self):
        request = self.build_git_request(
            "cursor",
            "work",
            None,
            "/repo",
            "prompt",
            self._config(True),
            False,
            frame_prompt=True,
        )
        self.assertFalse(request.mail_push)
        self.assertIn(mail.MAIL_PROMPT_SUFFIX, request.prompt)

    def test_mail_enabled_alone_does_not_install_a_stop_hook_at_launch(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-pull-only-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            fake, args_path, env_path = self._write_fake_harness(root)
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(self._launch_config("claude", fake, enabled=True)),
                encoding="utf-8",
            )
            with self._fake_log_environment(args_path, env_path):
                code, _stdout, stderr = self.run_main(
                    ["--cwd", str(workspace), "claude", "work", "prompt"]
                )

            self.assertEqual(code, 0, stderr)
            run_path = self._run_path(workspace)
            manifest = json.loads((run_path / runner.MANIFEST_FILE).read_text())
            box = mail.boxes_root(workspace / ".delegate") / manifest["runId"]
            self.assertFalse((box / mail.MAIL_PUSH_SETTINGS_FILE_NAME).exists())
            self.assertFalse((box / mail.MAIL_PUSH_CURSOR_FILE_NAME).exists())
            child_env = json.loads(env_path.read_text(encoding="utf-8"))
            self.assertNotIn("DELEGATE_MAIL_HOOK_HARNESS", child_env)

    def test_unverified_launch_degrades_to_pull_once_in_events_and_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-push-gating-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            fake, args_path, env_path = self._write_fake_harness(root)
            config = self._launch_config("cursor", fake, enabled=True)
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(config), encoding="utf-8"
            )
            with self._fake_log_environment(args_path, env_path):
                code, _stdout, stderr = self.run_main(
                    [
                        "--cwd",
                        str(workspace),
                        "cursor",
                        "work",
                        "--mail-push",
                        "prompt",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            warning = mail.mail_push_warning("cursor")
            self.assertEqual(stderr.count(warning), 1)
            run_path = self._run_path(workspace)
            run_id = json.loads((run_path / runner.MANIFEST_FILE).read_text())["runId"]
            box = mail.boxes_root(workspace / ".delegate") / run_id
            self.assertFalse((box / mail.MAIL_PUSH_SETTINGS_FILE_NAME).exists())
            self.assertFalse((box / mail.MAIL_PUSH_CURSOR_FILE_NAME).exists())

            events = [
                json.loads(line)
                for line in (run_path / runner.EVENTS_JSONL).read_text().splitlines()
                if line
            ]
            warning_events = [
                event
                for event in events
                if event.get("kind") == runner.MAIL_PUSH_EVENT_KIND
                and event.get("message") == warning
            ]
            self.assertEqual(
                warning_events, [{"kind": runner.MAIL_PUSH_EVENT_KIND, "message": warning}]
            )
            snapshot = json.loads((run_path / runner.SNAPSHOT_FILE).read_text())
            self.assertEqual(
                [
                    event
                    for event in snapshot["recentEvents"]
                    if event.get("kind") == runner.MAIL_PUSH_EVENT_KIND
                ],
                [{"kind": runner.MAIL_PUSH_EVENT_KIND, "message": warning}],
            )
            self.assertIn(warning, snapshot["warnings"])

    def test_verified_launch_provisions_only_inside_the_run_mailbox(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-push-launch-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            fake, args_path, env_path = self._write_fake_harness(root)
            config = self._launch_config("claude", fake, enabled=True)
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(config), encoding="utf-8"
            )
            global_settings = Path(self._config_env["HOME"]) / ".claude" / "settings.json"
            global_settings.parent.mkdir(parents=True)
            global_settings.write_text('{"canary":"untouched"}\n', encoding="utf-8")

            with self._fake_log_environment(args_path, env_path):
                code, _stdout, stderr = self.run_main(
                    [
                        "--cwd",
                        str(workspace),
                        "claude",
                        "work",
                        "--mail-push",
                        "prompt",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            run_path = self._run_path(workspace)
            manifest = json.loads((run_path / runner.MANIFEST_FILE).read_text())
            box = mail.boxes_root(workspace / ".delegate") / manifest["runId"]
            args = json.loads(args_path.read_text(encoding="utf-8"))
            settings_path = Path(args[args.index("--settings") + 1]).resolve()
            self.assertEqual(settings_path, (box / mail.MAIL_PUSH_SETTINGS_FILE_NAME).resolve())
            env = json.loads(env_path.read_text(encoding="utf-8"))
            self.assertEqual(env["DELEGATE_MAIL_HOOK_HARNESS"], "claude")
            command = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["Stop"][0][
                "hooks"
            ][0]["command"]
            self.assertNotIn("delegate mail hook-pump", command)
            self.assertIn(
                f"--cwd {shlex.quote(str(workspace.resolve()))} mail hook-pump",
                command,
            )
            self.assertIn(f"{env['DELEGATE_MAIL_HOOK_NONCE']}:hook_pump_unreachable", command)
            self.assertEqual(
                global_settings.read_text(encoding="utf-8"), '{"canary":"untouched"}\n'
            )

    def _fake_log_environment(self, args_path: Path, env_path: Path):
        return mock.patch.dict(
            os.environ,
            {"FAKE_ARGS_LOG": str(args_path), "FAKE_ENV_LOG": str(env_path)},
            clear=False,
        )


if __name__ == "__main__":
    unittest.main()
