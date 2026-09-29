"""Codex and Claude work Runs save their native session by default (dlg-3pl.4).

Covers the launch default and its opt-outs, the truthful followup errors, and the
resume/followup prompt-tail refusal. The workflow retry fallback lives in
``tests/test_workflow_commands.py`` beside the other structured-retry tests.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from delegate_agent import cli, request_build
from delegate_agent import config as delegate_config
from delegate_agent.cli_parser import parse_cli
from delegate_agent.errors import DelegateError
from tests.delegate_commands_test_base import CommandTestBase


class DefaultResumableRequestTests(CommandTestBase):
    """`build_request` resolves the tri-state once, for every launch frontend."""

    def build(self, engine, mode, config=None, **kwargs):
        cfg = (
            copy.deepcopy(config)
            if config is not None
            else delegate_config.embedded_default_config()
        )
        return self.build_git_request(engine, mode, None, "/repo", "hello", cfg, True, **kwargs)

    def test_codex_work_saves_its_session_by_default(self):
        request = self.build("codex", "work")

        self.assertTrue(request.resumable)
        self.assertNotIn("--ephemeral", request.argv)

    def test_claude_work_saves_its_session_by_default(self):
        request = self.build("claude", "work")

        self.assertTrue(request.resumable)
        self.assertNotIn("--no-session-persistence", request.argv)

    def test_user_ephemeral_config_does_not_beat_the_default(self):
        # The regression this guards: a config that already says ephemeral/
        # noSessionPersistence (the estate's does) must not switch the default off.
        config = delegate_config.embedded_default_config()
        config["codex"]["ephemeral"] = True
        config["claude"]["noSessionPersistence"] = True

        codex = self.build("codex", "work", config)
        claude = self.build("claude", "work", config)

        self.assertTrue(codex.resumable)
        self.assertNotIn("--ephemeral", codex.argv)
        self.assertTrue(claude.resumable)
        self.assertNotIn("--no-session-persistence", claude.argv)

    def test_explicit_opt_out_restores_the_ephemeral_flags(self):
        codex = self.build("codex", "work", resumable=False)
        claude = self.build("claude", "work", resumable=False)

        self.assertFalse(codex.resumable)
        self.assertIn("--ephemeral", codex.argv)
        self.assertFalse(claude.resumable)
        self.assertIn("--no-session-persistence", claude.argv)

    def test_engine_config_key_flips_the_default_off(self):
        config = delegate_config.embedded_default_config()
        config["codex"]["resumable"] = False
        config["claude"]["resumable"] = False

        codex = self.build("codex", "work", config)
        claude = self.build("claude", "work", config)

        self.assertFalse(codex.resumable)
        self.assertIn("--ephemeral", codex.argv)
        self.assertFalse(claude.resumable)
        self.assertIn("--no-session-persistence", claude.argv)

    def test_engine_config_key_only_flips_its_own_engine(self):
        config = delegate_config.embedded_default_config()
        config["codex"]["resumable"] = False

        self.assertFalse(self.build("codex", "work", config).resumable)
        self.assertTrue(self.build("claude", "work", config).resumable)

    def test_explicit_resumable_beats_the_config_key(self):
        config = delegate_config.embedded_default_config()
        config["codex"]["resumable"] = False

        request = self.build("codex", "work", config, resumable=True)

        self.assertTrue(request.resumable)
        self.assertNotIn("--ephemeral", request.argv)

    def test_safe_runs_stay_ephemeral_and_unresumable(self):
        codex = self.build("codex", "safe")
        claude = self.build("claude", "safe")

        self.assertFalse(codex.resumable)
        self.assertIn("--ephemeral", codex.argv)
        self.assertFalse(claude.resumable)
        self.assertIn("--no-session-persistence", claude.argv)

    def test_call_runs_and_other_engines_never_default_to_resumable(self):
        config = delegate_config.embedded_default_config()
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine, mode="call"):
                self.assertFalse(request_build.default_resumable(engine, "call", config))
        for engine in ("cursor", "droid", "grok", "devin", "opencode", "pi", "omp", "kimi"):
            with self.subTest(engine=engine):
                self.assertFalse(request_build.default_resumable(engine, "work", config))
        self.assertFalse(self.build("cursor", "work").resumable)

    def test_pass_through_runs_stay_untracked_and_ephemeral(self):
        request = self.build("codex", "work", pass_through=True)

        self.assertFalse(request.resumable)
        self.assertIn("--ephemeral", request.argv)

    def test_explicit_resumable_on_safe_is_still_refused(self):
        with self.assertRaises(DelegateError) as caught:
            self.build("codex", "safe", resumable=True)

        self.assertEqual(caught.exception.error, "invalid_option_combination")

    def test_non_boolean_resumable_is_rejected(self):
        with self.assertRaises(DelegateError) as caught:
            self.build("codex", "work", resumable="yes")

        self.assertEqual(caught.exception.error, "invalid_resumable")

    def test_config_validation_covers_the_new_keys(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                config = delegate_config.embedded_default_config()
                self.assertIs(config[engine]["resumable"], True)
                config[engine]["resumable"] = "no"
                with self.assertRaises(delegate_config.ConfigError) as caught:
                    delegate_config.validate_config(config)
                self.assertEqual(caught.exception.error, f"invalid_{engine}_config")


class ResumableFlagParserTests(unittest.TestCase):
    def test_launch_flag_is_tri_state(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                self.assertIsNone(parse_cli([engine, "work", "task"]).payload.resumable)
                self.assertIs(
                    parse_cli([engine, "work", "--resumable", "task"]).payload.resumable, True
                )
                self.assertIs(
                    parse_cli([engine, "work", "--no-resumable", "task"]).payload.resumable, False
                )

    def test_resumable_and_no_resumable_cannot_be_combined(self):
        for tokens in (["--resumable", "--no-resumable"], ["--no-resumable", "--resumable"]):
            with self.subTest(tokens=tokens):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(["codex", "work", *tokens, "task"])
                self.assertEqual(caught.exception.error, "invalid_option_combination")
                self.assertIn("cannot be combined", caught.exception.message)

    def test_duplicate_no_resumable_is_rejected(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["claude", "work", "--no-resumable", "--no-resumable", "task"])

        self.assertEqual(caught.exception.error, "invalid_option_combination")

    def test_no_resumable_is_a_quiet_no_op_where_nothing_is_saved(self):
        # Safe and call runs never save a session, so opting out of it is not an error.
        self.assertIs(
            parse_cli(["codex", "safe", "--no-resumable", "task"]).payload.resumable, False
        )

    def test_resume_accepts_no_resumable_before_the_handle(self):
        self.assertFalse(parse_cli(["resume", "run-1"]).payload.no_resumable)
        self.assertTrue(parse_cli(["resume", "--no-resumable", "run-1"]).payload.no_resumable)

    def test_resume_rejects_a_duplicate_no_resumable(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["resume", "--no-resumable", "--no-resumable", "run-1"])

        self.assertEqual(caught.exception.error, "invalid_option_combination")


class PromptTailRefusalParserTests(unittest.TestCase):
    """A real option of resume/followup typed after the prompt text is refused."""

    def assert_refused(self, argv, *names):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(argv)
        error = caught.exception
        self.assertEqual(error.error, "option_after_handle")
        for name in names:
            self.assertIn(name, error.message)
        command = argv[0]
        # The message names the escape hatch, in the form the caller can paste.
        self.assertIn(f"delegate {command} HANDLE -- <prompt text>", error.message)
        return error

    def test_followup_refuses_dry_run_in_the_tail_and_says_it_would_launch(self):
        error = self.assert_refused(["followup", "x", "fix", "it", "--dry-run"], "--dry-run")

        self.assertIn("starts a real run", error.message)

    def test_followup_refuses_each_of_its_options_in_the_tail(self):
        for tail in (
            ["--timeout", "5"],
            ["--timeout=5"],
            ["--prompt-file", "p.md"],
            ["--dry-run"],
        ):
            with self.subTest(tail=tail):
                self.assert_refused(["followup", "x", "fix it", *tail], tail[0].split("=")[0])

    def test_resume_refuses_its_options_in_the_tail(self):
        for tail in (["--model", "sol"], ["--dry-run"], ["--engine=claude"], ["--no-resumable"]):
            with self.subTest(tail=tail):
                self.assert_refused(["resume", "x", "go on", *tail], tail[0].split("=")[0])

    def test_several_swallowed_options_are_all_named(self):
        error = self.assert_refused(
            ["followup", "x", "fix it", "--dry-run", "--timeout", "5"], "--dry-run", "--timeout"
        )

        self.assertIn("appear after", error.message)

    def test_double_dash_makes_the_tail_literal_prompt_text(self):
        parsed = parse_cli(["followup", "x", "--", "fix it", "--dry-run"])

        self.assertFalse(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.prompt_parts, ["fix it", "--dry-run"])

        parsed = parse_cli(["resume", "x", "--", "--model", "sol"])

        self.assertEqual(parsed.payload.extra_parts, ["--model", "sol"])
        self.assertIsNone(parsed.payload.model)

    def test_double_dash_after_prompt_text_makes_the_rest_literal(self):
        parsed = parse_cli(["followup", "x", "fix it", "--", "--dry-run"])

        self.assertFalse(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.prompt_parts, ["fix it", "--dry-run"])

        parsed = parse_cli(["resume", "x", "go on", "--", "--model", "sol"])

        self.assertIsNone(parsed.payload.model)
        self.assertEqual(parsed.payload.extra_parts, ["go on", "--model", "sol"])

    def test_double_dash_after_prompt_text_still_refuses_options_before_it(self):
        self.assert_refused(
            ["followup", "x", "fix it", "--dry-run", "--", "--timeout"], "--dry-run"
        )

    def test_followup_keeps_options_on_either_side_of_the_handle(self):
        parsed = parse_cli(["followup", "--timeout", "9", "x", "--dry-run", "fix it"])

        self.assertEqual(parsed.payload.handle, "x")
        self.assertEqual(parsed.payload.timeout, 9)
        self.assertTrue(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.prompt_parts, ["fix it"])

    def test_prose_that_mentions_an_option_is_not_refused(self):
        parsed = parse_cli(["followup", "x", "please add a --dry-run mode"])

        self.assertFalse(parsed.payload.dry_run)
        self.assertEqual(parsed.payload.prompt_parts, ["please add a --dry-run mode"])

    def test_an_option_of_another_command_keeps_the_late_warning(self):
        # `--model` is a launch option, not a followup one: the parser cannot tell
        # this is a mistake, so the existing after-the-fact warning still applies.
        parsed = parse_cli(["followup", "x", "fix it", "--model", "opus"])

        self.assertEqual(parsed.payload.prompt_parts, ["fix it", "--model", "opus"])
        self.assertTrue(any("option after the prompt" in w for w in parsed.payload.warnings))


class ResumableDefaultE2ETests(unittest.TestCase):
    """Whole launches through `cli.main` with fake codex/claude binaries."""

    CODEX_SESSION = "th_default_resumable_1"
    CLAUDE_SESSION = "550e8400-e29b-41d4-a716-446655440000"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.bin_dir = root / "bin"
        for directory in (self.home, self.workspace, self.bin_dir):
            directory.mkdir()
        git = ["git", "-C", str(self.workspace)]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        (self.workspace / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run([*git, "add", "README.md"], check=True)
        subprocess.run(
            [
                *git,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.com",
                "commit",
                "-qm",
                "init",
            ],
            check=True,
        )
        self.argv_log = self.home / "argv.jsonl"
        self.write_binary("codex", self.codex_script())
        self.write_binary("claude", self.claude_script())
        self.config = {
            "version": 1,
            "codex": {
                "binary": str(self.bin_dir / "codex"),
                "defaultModel": "gpt-5",
                "models": {"gpt-5": "gpt-5"},
                "workSandbox": "workspace-write",
                # The estate's real config: ephemeral on. The default must still win.
                "ephemeral": True,
            },
            "claude": {
                "binary": str(self.bin_dir / "claude"),
                "defaultModel": "claude-3-7-sonnet",
                "models": {"claude-3-7-sonnet": "claude-3-7-sonnet"},
                "workPermissionMode": "auto",
                "noSessionPersistence": True,
            },
        }
        self.write_config()

    def write_config(self) -> None:
        self.config_path = self.home / "delegate_config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def write_binary(self, name: str, script: str) -> None:
        path = self.bin_dir / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    def codex_script(self) -> str:
        return (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['FAKE_ARGV_LOG'], 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(['codex'] + sys.argv[1:]) + '\\n')\n"
            f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{self.CODEX_SESSION}'}}))\n"
            "print(json.dumps({'type': 'turn.started'}))\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}))\n"
            "print(json.dumps({'type': 'turn.completed'}))\n"
        )

    def claude_script(self) -> str:
        return (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['FAKE_ARGV_LOG'], 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(['claude'] + sys.argv[1:]) + '\\n')\n"
            f"print(json.dumps({{'type': 'system', 'subtype': 'init', 'session_id': '{self.CLAUDE_SESSION}'}}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'claude done'}]}}))\n"
            "print(json.dumps({'type': 'result', 'status': 'success'}))\n"
        )

    def launches(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def delegate(self, args: list[str]) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {
            "HOME": str(self.home),
            "DELEGATE_CONFIG": str(self.config_path),
            "FAKE_ARGV_LOG": str(self.argv_log),
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        saved = os.environ.copy()
        try:
            os.environ.update(env)
            code = cli.main(
                ["--json", "--cwd", str(self.workspace), *args], stdout=stdout, stderr=stderr
            )
        finally:
            os.environ.clear()
            os.environ.update(saved)
        text = stdout.getvalue()
        return code, (json.loads(text) if text.strip() else {}), stderr.getvalue()

    def test_codex_followup_works_without_resumable_at_launch(self):
        code, launched, stderr = self.delegate(["codex", "work", "first task"])
        self.assertEqual(code, 0, (launched, stderr))
        self.assertNotIn("--ephemeral", self.launches()[0])

        code, followed, stderr = self.delegate(["followup", launched["alias"], "second task"])

        self.assertEqual(code, 0, (followed, stderr))
        self.assertEqual(followed["followupOf"], launched["runId"])
        argv = self.launches()[1]
        self.assertGreater(argv.index("resume"), argv.index("exec"))
        self.assertEqual(argv[-2:], [self.CODEX_SESSION, "-"])
        self.assertNotIn("--ephemeral", argv)

    def test_claude_followup_works_without_resumable_at_launch(self):
        code, launched, stderr = self.delegate(["claude", "work", "first task"])
        self.assertEqual(code, 0, (launched, stderr))
        self.assertNotIn("--no-session-persistence", self.launches()[0])

        code, followed, stderr = self.delegate(["followup", launched["alias"], "second task"])

        self.assertEqual(code, 0, (followed, stderr))
        argv = self.launches()[1]
        self.assertEqual(argv[argv.index("--resume") + 1], self.CLAUDE_SESSION)
        self.assertNotIn("--no-session-persistence", argv)

    def test_no_resumable_launch_keeps_the_ephemeral_flags_and_refuses_followup(self):
        for engine, flag in (("codex", "--ephemeral"), ("claude", "--no-session-persistence")):
            with self.subTest(engine=engine):
                code, launched, stderr = self.delegate([engine, "work", "--no-resumable", "task"])
                self.assertEqual(code, 0, (launched, stderr))
                self.assertIn(flag, self.launches()[-1])
                count = len(self.launches())

                code, refused, _stderr = self.delegate(["followup", launched["alias"], "again"])

                self.assertEqual(code, 2)
                self.assertEqual(refused["error"], "session-missing")
                message = refused["message"]
                # The old advice ("relaunch with --resumable") pointed at a flag that
                # is no longer the reason; the truthful next step is `delegate resume`.
                self.assertNotIn("--resumable", message)
                self.assertIn(f"delegate resume {launched['alias']}", message)
                self.assertEqual(len(self.launches()), count)

    def test_engine_config_key_restores_the_old_default(self):
        self.config["codex"]["resumable"] = False
        self.write_config()

        code, launched, stderr = self.delegate(["codex", "work", "task"])

        self.assertEqual(code, 0, (launched, stderr))
        self.assertIn("--ephemeral", self.launches()[0])
        code, refused, _stderr = self.delegate(["followup", launched["alias"], "again"])
        self.assertEqual(refused["error"], "session-missing")

    def test_explicit_resumable_beats_the_engine_config_key(self):
        self.config["codex"]["resumable"] = False
        self.write_config()

        code, launched, stderr = self.delegate(["codex", "work", "--resumable", "task"])

        self.assertEqual(code, 0, (launched, stderr))
        self.assertNotIn("--ephemeral", self.launches()[0])

    def test_safe_launch_stays_ephemeral(self):
        code, _launched, stderr = self.delegate(["codex", "safe", "review this"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("--ephemeral", self.launches()[0])

    def test_followup_dry_run_in_the_tail_launches_nothing(self):
        code, launched, stderr = self.delegate(["codex", "work", "first task"])
        self.assertEqual(code, 0, (launched, stderr))
        self.assertEqual(len(self.launches()), 1)
        runs_before = self.delegate(["runs"])[1]

        code, refused, _stderr = self.delegate(
            ["followup", launched["alias"], "fix", "it", "--dry-run"]
        )

        self.assertEqual(code, 2)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "option_after_handle")
        self.assertIn("--", refused["message"])
        self.assertEqual(len(self.launches()), 1, "the refused followup launched a child")
        self.assertEqual(self.delegate(["runs"])[1], runs_before, "the refusal registered a run")

    def test_resume_option_in_the_tail_launches_nothing(self):
        code, launched, stderr = self.delegate(["codex", "work", "first task"])
        self.assertEqual(code, 0, (launched, stderr))

        code, refused, _stderr = self.delegate(
            ["resume", launched["alias"], "go on", "--model", "gpt-5"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(refused["error"], "option_after_handle")
        self.assertEqual(len(self.launches()), 1)

    def test_double_dash_lets_the_prompt_carry_an_option_word(self):
        code, launched, stderr = self.delegate(["codex", "work", "first task"])
        self.assertEqual(code, 0, (launched, stderr))

        code, followed, stderr = self.delegate(
            ["followup", launched["alias"], "--", "explain what --dry-run does"]
        )

        self.assertEqual(code, 0, (followed, stderr))
        self.assertEqual(len(self.launches()), 2)

    def test_resume_of_a_plain_run_is_resumable_by_default_and_can_opt_out(self):
        code, launched, stderr = self.delegate(["codex", "work", "--no-resumable", "first task"])
        self.assertEqual(code, 0, (launched, stderr))

        code, resumed, stderr = self.delegate(["resume", "--dry-run", launched["alias"], "next"])
        self.assertEqual(code, 0, (resumed, stderr))
        self.assertTrue(resumed.get("resumable"), resumed)
        self.assertNotIn("--ephemeral", resumed["argv"])

        code, resumed, stderr = self.delegate(
            ["resume", "--dry-run", "--no-resumable", launched["alias"], "next"]
        )
        self.assertEqual(code, 0, (resumed, stderr))
        self.assertFalse(resumed.get("resumable", False), resumed)
        self.assertIn("--ephemeral", resumed["argv"])

    def expire_session(self, engine: str, text: str) -> None:
        self.write_binary(
            engine,
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({{'type': 'error', 'message': {text!r}}}))\n"
            "raise SystemExit(1)\n",
        )

    def test_claude_lookup_failure_names_the_account_not_a_missing_flag(self):
        code, launched, stderr = self.delegate(["claude", "work", "first task"])
        self.assertEqual(code, 0, (launched, stderr))
        self.expire_session(
            "claude", f"No conversation found with session ID: {self.CLAUDE_SESSION}"
        )

        code, failed, _stderr = self.delegate(["followup", launched["alias"], "continue"])

        self.assertEqual(code, 1)
        self.assertEqual(failed["error"], "session_expired")
        message = failed["message"]
        self.assertIn("different account", message)
        self.assertIn("launcher", message)
        self.assertIn(f"delegate resume {launched['alias']}", message)
        self.assertNotIn("--resumable", message)
        self.assertEqual(
            failed.get("nextActions"), [f'delegate resume {launched["alias"]} "<instructions>"']
        )

    def test_codex_lookup_failure_names_the_account_not_a_missing_flag(self):
        code, launched, stderr = self.delegate(["codex", "work", "first task"])
        self.assertEqual(code, 0, (launched, stderr))
        self.expire_session("codex", "no thread with id: th_default_resumable_1")

        code, failed, _stderr = self.delegate(["followup", launched["alias"], "continue"])

        self.assertEqual(code, 1)
        self.assertEqual(failed["error"], "session_expired")
        self.assertIn("different CODEX_HOME or account", failed["message"])
        self.assertIn(f"delegate resume {launched['alias']}", failed["message"])
        self.assertNotIn("--resumable", failed["message"])


if __name__ == "__main__":
    unittest.main()
