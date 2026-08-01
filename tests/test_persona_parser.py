import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, cli_parser, profile_guard, request_build
from delegate_agent.errors import DelegateError

ENGINES = ("cursor", "kimi", "codex", "claude", "grok", "devin", "opencode", "pi", "omp")


class PersonaParserTests(unittest.TestCase):
    def test_persona_options_parse_on_every_engine_and_dry_run(self):
        for engine in (*ENGINES, "droid"):
            argv = [engine, "work"] if engine != "droid" else ["droid", "work"]
            with self.subTest(engine=engine):
                parsed = cli_parser.parse_cli([*argv, "--persona", "editor", "prompt"])
                self.assertEqual(parsed.launch.persona, "editor")
                self.assertFalse(parsed.launch.no_persona)
                self.assertFalse(parsed.launch.allow_repo_persona)

                no_persona = cli_parser.parse_cli([*argv, "--no-persona", "prompt"])
                self.assertIsNone(no_persona.launch.persona)
                self.assertTrue(no_persona.launch.no_persona)

                allowed = cli_parser.parse_cli(
                    [*argv, "--allow-repo-persona", "--persona", "editor", "prompt"]
                )
                self.assertTrue(allowed.launch.allow_repo_persona)

        for engine in (*ENGINES, "droid"):
            argv = [engine, "work"] if engine != "droid" else ["droid", "work"]
            parsed = cli_parser.parse_cli(["dry-run", *argv, "--persona", "editor", "prompt"])
            with self.subTest(dry_run_engine=engine):
                self.assertEqual(parsed.launch.persona, "editor")

    def test_parse_prompt_tail_returns_all_persona_fields(self):
        parsed = cli_parser.parse_prompt_tail(
            ["--persona", "editor", "--allow-repo-persona", "review"],
            False,
            None,
            command_prefix=["cursor", "work"],
        )
        self.assertEqual(parsed[-4:], (None, "editor", False, True))

    def _input_json(self, root: Path, payload: dict) -> Path:
        path = root / "task.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _input_request(self, root: Path, engine: str, extra: dict):
        payload = {"engine": engine, "mode": "work", "cwd": str(root), "prompt": "review"}
        payload.update(extra)
        path = self._input_json(root, payload)
        parsed = cli_parser.parse_cli(["run", "--input-json", str(path)])
        sentinel = object()
        with mock.patch.object(request_build, "build_request", return_value=sentinel) as built:
            result = request_build.request_from_input_json(parsed, cli.DEFAULT_CONFIG)
        return result, built

    def test_input_json_persona_null_absent_and_nonempty_matrix_on_non_opencode(self):
        for engine in ("cursor", "opencode"):
            for extra, expected in (
                ({}, None),
                ({"persona": None}, None),
                ({"persona": "editor"}, "editor"),
            ):
                with self.subTest(engine=engine, extra=extra), tempfile.TemporaryDirectory() as tmp:
                    result, built = self._input_request(Path(tmp), engine, extra)
                    self.assertIs(result, built.return_value)
                    self.assertEqual(built.call_args.kwargs["persona"], expected)
                    self.assertFalse(built.call_args.kwargs["allow_repo_persona"])

    def test_input_json_allow_repo_persona_requires_a_boolean(self):
        for value in ("yes", 1, [], None):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(value=value):
                path = self._input_json(
                    Path(tmp),
                    {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "review",
                        "allowRepoPersona": value,
                    },
                )
                parsed = cli_parser.parse_cli(["run", "--input-json", str(path)])
                with self.assertRaises(DelegateError) as caught:
                    request_build.request_from_input_json(parsed, cli.DEFAULT_CONFIG)
                self.assertEqual(caught.exception.error, "invalid_allow_repo_persona")

    def test_input_json_empty_persona_is_an_error(self):
        for value in ("", "   "):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(value=value):
                path = self._input_json(
                    Path(tmp),
                    {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "review",
                        "persona": value,
                    },
                )
                parsed = cli_parser.parse_cli(["run", "--input-json", str(path)])
                with self.assertRaises(DelegateError) as caught:
                    request_build.request_from_input_json(parsed, cli.DEFAULT_CONFIG)
                self.assertEqual(caught.exception.error, "invalid_persona")

    def _assert_refusal(self, argv: list[str], error: str):
        parsed = cli_parser.parse_cli(argv)
        with self.assertRaises(DelegateError) as caught:
            request_build.request_from_parsed(
                parsed,
                cli.DEFAULT_CONFIG,
                io.StringIO(""),
                io.StringIO(),
            )
        self.assertEqual(caught.exception.error, error)

    def test_persona_call_refusal_surface(self):
        self._assert_refusal(
            ["cursor", "call", "--persona", "editor", "review"], "persona_call_refused"
        )

    def test_persona_read_only_call_refusal_surface(self):
        self._assert_refusal(
            ["cursor", "call", "--read-only", "--persona", "editor", "review"],
            "persona_read_only_call_refused",
        )

    def test_persona_pass_through_refusal_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_refusal(
                ["--pass-through", "--cwd", tmp, "cursor", "work", "--persona", "editor", "review"],
                "persona_pass_through_refused",
            )

    def test_persona_slash_passthrough_refusal_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_refusal(
                ["--cwd", tmp, "cursor", "work", "--persona", "editor", "/review"],
                "persona_slash_passthrough_refused",
            )

    def test_profile_guard_and_shell_shim_allowlist_both_include_personas(self):
        parsed = cli_parser.parse_cli(["personas"])
        self.assertTrue(profile_guard.is_read_only_command(parsed))
        shim = Path(__file__).parents[1] / "bin" / "delegate-profile-shim"
        source = shim.read_text(encoding="utf-8")
        self.assertIn("personas", profile_guard.READ_ONLY_SUBCOMMANDS)
        self.assertRegex(
            source, r"profiles\|runs\|ps\|run-output\|describe\|snapshot\|agent-help\|personas\|"
        )

        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "delegate.py"
            fake.write_text("#!/usr/bin/env python3\nprint('shim target')\n", encoding="utf-8")
            fake.chmod(0o755)
            env = dict(os.environ)
            env.update({"HOME": tmp, "AI_PROFILE": "work", "DELEGATE_SHIM_PY": str(fake)})
            env.pop("DELEGATE_CONFIG", None)
            result = subprocess.run(
                [str(shim), "personas"], capture_output=True, text=True, env=env, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("read-only", result.stderr)
            self.assertIn("shim target", result.stdout)


if __name__ == "__main__":
    unittest.main()
