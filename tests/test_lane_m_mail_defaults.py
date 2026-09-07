from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from delegate_agent import cli_parser, config, mail, profiles, run_registry
from tests.delegate_commands_test_base import CommandTestBase, make_git_repo


class MailDefaultTests(CommandTestBase):
    def test_missing_mail_section_and_key_are_enabled(self):
        for cfg in ({}, {"mail": {}}, config.embedded_default_config()):
            with self.subTest(config=cfg.get("mail")):
                self.assertTrue(config.mail_enabled(cfg))
        self.assertFalse(config.mail_enabled({"mail": {"enabled": False}}))

    def test_no_mail_global_parser_threads_launch_forms_and_preserves_literal(self):
        for command in (
            ["codex", "work", "x"],
            ["droid", "work", "x"],
            ["dry-run", "codex", "work", "x"],
            ["dry-run", "droid", "work", "x"],
            ["run", "--input-json", "input.json"],
            ["resume", "codex-1"],
            ["followup", "codex-1", "x"],
        ):
            with self.subTest(command=command):
                parsed = cli_parser.parse_cli(["--no-mail", *command])
                self.assertTrue(parsed.global_options.no_mail)
        parsed = cli_parser.parse_cli(["codex", "work", "--", "--no-mail"])
        self.assertEqual(
            (parsed.global_options.no_mail, parsed.payload.prompt_parts), (False, ["--no-mail"])
        )

    def test_dry_run_mail_default_opt_out_and_notify_are_launch_local(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        for label, cfg, flags, enabled in (
            ("default", {}, [], True),
            ("flag", {}, ["--no-mail"], False),
            ("config", {"mail": {"enabled": False}}, [], False),
            ("enabled flag", {"mail": {"enabled": True}}, ["--no-mail"], False),
        ):
            with self.subTest(case=label):
                config_path = Path(self._config_env["DELEGATE_CONFIG"])
                config_path.write_text(
                    json.dumps({**cfg, "isolation": {"work": "worktree"}}), encoding="utf-8"
                )
                before = config_path.read_bytes()
                code, stdout, stderr = self.run_main(
                    [
                        "--json",
                        "--cwd",
                        repo.name,
                        "--notify",
                        "room:coordinator",
                        *flags,
                        "dry-run",
                        "codex",
                        "work",
                        "x",
                    ]
                )
                payload = json.loads(stdout)
                self.assertEqual(
                    {
                        "code": code,
                        "suffix": payload.get("mailPromptSuffix"),
                        "grant": "sandbox_workspace_write.writable_roots"
                        in " ".join(payload.get("argv", [])),
                        "notify": payload.get("notify", {}).get("target"),
                        "push": payload.get("mailPush", False),
                        "configUnchanged": config_path.read_bytes() == before,
                        "storageCreated": (Path(repo.name) / ".delegate" / "mail").exists(),
                    },
                    {
                        "code": 0,
                        "suffix": mail.MAIL_PROMPT_SUFFIX if enabled else None,
                        "grant": enabled,
                        "notify": "room:coordinator",
                        "push": False,
                        "configUnchanged": True,
                        "storageCreated": False,
                    },
                    stderr,
                )

    def test_no_mail_rejects_explicit_push(self):
        code, stdout, stderr = self.run_main(
            ["--json", "--no-mail", "dry-run", "codex", "work", "--mail-push", "x"]
        )
        self.assertEqual((code, json.loads(stdout).get("error")), (2, "mail_push_disabled"), stderr)

    def test_safe_and_nonisolated_work_do_not_get_mail_grants_or_warnings(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        for mode, flags in (("safe", []), ("work", ["--isolation", "none"])):
            with self.subTest(mode=mode):
                code, stdout, stderr = self.run_main(
                    ["--json", "--cwd", repo.name, *flags, "dry-run", "codex", mode, "x"]
                )
                payload = json.loads(stdout)
                self.assertEqual(
                    (
                        code,
                        "sandbox_workspace_write.writable_roots" in " ".join(payload["argv"]),
                        "cannot reach .delegate/mail" in " ".join(payload.get("warnings", [])),
                    ),
                    (0, False, False),
                    stderr,
                )

    def test_unreachable_warning_is_manifest_only_and_deduplicated(self):
        stderr = io.StringIO()
        warnings = ()
        argv = ["codex", "exec", "--sandbox", "read-only", "-"]
        for _ in range(2):
            prepared = mail.prepare_work_mail_launch(
                enabled=True,
                mail_push=False,
                engine="codex",
                argv=argv,
                display_argv=argv,
                registry_root=Path("/unused/.delegate"),
                run_id="unused",
                env_overrides={},
                profile_resolution=profiles.empty_profile_resolution(),
                prompt="x",
                prompt_transport="stdin",
                stderr=stderr,
                isolated_workspace=True,
                warnings=warnings,
            )
            warnings = prepared.request_warnings
        self.assertEqual(
            (prepared.argv, prepared.display_argv, warnings, stderr.getvalue()),
            (
                argv,
                argv,
                (
                    "codex work launch sandbox policy read-only cannot reach "
                    ".delegate/mail from this isolated workspace.",
                ),
                "",
            ),
        )

    def test_storage_failure_disables_mail_for_direct_persistent_and_attached_launches(self):
        for isolation in ("none", "worktree"):
            with self.subTest(isolation=isolation):
                repo = make_git_repo(with_commit=True)
                self.addCleanup(repo.cleanup)
                workspace = Path(repo.name)
                # An external harness fixture, never a live-model claim.
                fake = Path(self._config_env["DELEGATE_CONFIG"]).parent / "fake-codex"
                capture = fake.with_suffix(".json")
                fake.write_text(
                    f"#!{sys.executable}\n"
                    "import json, sys\nfrom pathlib import Path\n"
                    f"Path({str(capture)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'prompt': sys.stdin.read()}}))\n"
                    "print(json.dumps({'type': 'thread.started', 'thread_id': 'mail-test-thread'}))\n"
                    "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'fixture'}}))\n"
                    "print(json.dumps({'type': 'turn.completed'}))\n",
                    encoding="utf-8",
                )
                fake.chmod(0o755)
                cfg = {
                    "codex": {"binary": str(fake)},
                    "isolation": {"work": isolation},
                    "worktrees": {"retireWorktreeOnCompletion": False},
                }
                Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                    json.dumps(cfg), encoding="utf-8"
                )
                root = run_registry.ensure_registry(workspace, workspace_kind="git")
                (root / "mail").write_text("occupied", encoding="utf-8")
                # Preserve a user-authored copy; remove only the generated suffix.
                user_prompt = "user content\n\n" + mail.MAIL_PROMPT_SUFFIX
                commands = [["codex", "work", "--mail-push", user_prompt]]
                if isolation == "worktree":
                    commands.append(["resume", "codex-1", "--mail-push"])
                for command in commands:
                    with self.subTest(command=command[0]):
                        code, stdout, stderr = self.run_main(
                            ["--json", "--cwd", repo.name, *command]
                        )
                        payload = json.loads(stdout)
                        if code:
                            self.fail(f"launch failed: {payload}; {stderr}")
                        observed = json.loads(capture.read_text(encoding="utf-8"))
                        manifest = run_registry.load_run_manifest(root, payload["runId"])
                        self.assertEqual(
                            {
                                "code": code,
                                "stderrWarnings": stderr.count("delegate mail: WARNING:"),
                                "warnings": sum(
                                    "mail_storage_unavailable" in w for w in payload["warnings"]
                                ),
                                "manifestWarnings": sum(
                                    "mail_storage_unavailable" in w for w in manifest["warnings"]
                                ),
                                "suffixCopies": observed["prompt"].count(mail.MAIL_PROMPT_SUFFIX),
                                "grant": "sandbox_workspace_write.writable_roots"
                                in " ".join(observed["argv"]),
                                "hook": "--dangerously-bypass-hook-trust" in observed["argv"],
                                "mailPush": manifest.get("mailPush", False),
                            },
                            {
                                "code": 0,
                                "stderrWarnings": 1,
                                "warnings": 1,
                                "manifestWarnings": 1,
                                "suffixCopies": 1,
                                "grant": False,
                                "hook": False,
                                "mailPush": False,
                            },
                        )
