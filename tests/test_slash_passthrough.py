import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent.constants import (  # noqa: E402
    PROMPT_INSTRUCTION_MODE_SLASH,
    PROMPT_INSTRUCTION_MODE_WRAPPED,
)
from delegate_agent.errors import DelegateError  # noqa: E402
from delegate_agent.prompt_instructions import (  # noqa: E402
    COMPLETION_REPORT_SUFFIX,
    SKILL_REVIEW_PREFIX,
    detect_slash_command,
)
from tests.execution_test_base import ExecutionTestBase, make_git_repo  # noqa: E402


class SlashDetectionTests(ExecutionTestBase):
    def test_detection_table(self):
        accepted = [
            "/goal fix the failing tests",
            "/goal",
            "/review-code src/main.py",
            "/g quick",
            "/compact_now please",
            "/goal\nmultiline body",
        ]
        rejected = [
            "/tmp/foo.py is broken",
            "/goal/sub does not count",
            "fix /goal mentions mid-prompt",
            "/2fast starts with a digit",
            "//double slash",
            "/ goal space after slash",
            "",
            "plain prompt",
        ]
        for prompt in accepted:
            self.assertTrue(detect_slash_command(prompt), prompt)
        for prompt in rejected:
            self.assertFalse(detect_slash_command(prompt), prompt)


class SlashPassthroughRequestTests(ExecutionTestBase):
    def setUp(self):
        super().setUp()
        self.repo = make_git_repo()
        self.addCleanup(self.repo.cleanup)
        self.config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        self.config["droid"]["models"] = {"reviewer": "model-id"}

    def build(self, argv):
        parsed = self.delegate.parse_cli(["--cwd", self.repo.name, *argv])
        return self.delegate.request_from_parsed(parsed, self.config, io.StringIO(""))

    def assert_verbatim(self, request, prompt):
        self.assertEqual(request.prompt_instruction_mode, PROMPT_INSTRUCTION_MODE_SLASH)
        self.assertEqual(request.prompt, prompt)
        self.assertNotIn(SKILL_REVIEW_PREFIX, request.prompt)
        self.assertNotIn(COMPLETION_REPORT_SUFFIX.strip(), request.prompt)

    def test_codex_work_slash_prompt_is_verbatim(self):
        prompt = "/goal fix the failing tests"
        request = self.build(["codex", "work", prompt])
        self.assert_verbatim(request, prompt)

    def test_codex_safe_slash_prompt_allowed_and_verbatim(self):
        prompt = "/goal review this repo"
        request = self.build(["codex", "safe", prompt])
        self.assert_verbatim(request, prompt)

    def test_claude_and_grok_safe_slash_allowed(self):
        for engine in ("claude", "grok"):
            prompt = "/goal audit"
            request = self.build([engine, "safe", prompt])
            self.assert_verbatim(request, prompt)

    def test_devin_safe_slash_fails_as_unsupported_mode(self):
        with self.assertRaises(DelegateError) as caught:
            self.build(["devin", "safe", "/goal audit"])
        self.assertEqual(caught.exception.error, "unsupported_mode")

    def test_prompt_enforced_safe_engines_reject_slash(self):
        for argv in (
            ["cursor", "safe", "/goal x"],
            ["kimi", "safe", "/goal x"],
            ["droid", "reviewer", "safe", "/goal x"],
        ):
            with self.assertRaises(DelegateError) as caught:
                self.build(argv)
            self.assertEqual(caught.exception.error, "slash_passthrough_unsupported")

    def test_cursor_work_slash_prompt_verbatim_in_argv(self):
        prompt = "/goal implement the thing"
        request = self.build(["cursor", "work", prompt])
        self.assertEqual(request.prompt_instruction_mode, PROMPT_INSTRUCTION_MODE_SLASH)
        self.assertEqual(request.argv[-1], prompt)

    def test_kimi_work_slash_prompt_verbatim_in_argv(self):
        prompt = "/goal implement the thing"
        request = self.build(["kimi", "work", prompt])
        self.assertEqual(request.prompt_instruction_mode, PROMPT_INSTRUCTION_MODE_SLASH)
        self.assertEqual(request.argv[request.argv.index("--prompt") + 1], prompt)

    def test_droid_work_slash_prompt_verbatim_in_prompt_file(self):
        prompt = "/goal implement the thing"
        request = self.build(["droid", "reviewer", "work", prompt])
        self.assertEqual(request.prompt_instruction_mode, PROMPT_INSTRUCTION_MODE_SLASH)
        self.assertEqual(request.prompt_file_text, prompt)

    def build_no_cwd(self, argv):
        parsed = self.delegate.parse_cli(argv)
        return self.delegate.request_from_parsed(parsed, self.config, io.StringIO(""))

    def test_call_read_only_rejects_slash(self):
        with self.assertRaises(DelegateError) as caught:
            self.build_no_cwd(["codex", "call", "--read-only", "/goal x"])
        self.assertEqual(caught.exception.error, "slash_passthrough_unsupported")

    def test_plain_call_slash_prompt_stays_verbatim(self):
        prompt = "/goal answer directly"
        request = self.build_no_cwd(["codex", "call", prompt])
        self.assertEqual(request.prompt, prompt)
        Path(request.workspace).rmdir()

    def test_wrapped_prompt_still_gets_instructions(self):
        request = self.build(["codex", "work", "fix the tests"])
        self.assertEqual(request.prompt_instruction_mode, PROMPT_INSTRUCTION_MODE_WRAPPED)
        self.assertTrue(request.prompt.startswith(SKILL_REVIEW_PREFIX))

    def test_path_like_prompt_is_not_detected(self):
        request = self.build(["codex", "work", "/tmp/foo.py is broken, fix it"])
        self.assertEqual(request.prompt_instruction_mode, PROMPT_INSTRUCTION_MODE_WRAPPED)
        self.assertTrue(request.prompt.startswith(SKILL_REVIEW_PREFIX))

    def test_pass_through_without_slash_keeps_safe_prefix_drops_preamble(self):
        request = self.build(["--pass-through", "droid", "reviewer", "safe", "review this"])
        self.assertEqual(request.prompt_instruction_mode, PROMPT_INSTRUCTION_MODE_WRAPPED)
        self.assertNotIn(SKILL_REVIEW_PREFIX, request.prompt)
        self.assertIn("Delegate Droid safe mode", request.prompt)
        self.assertNotIn(COMPLETION_REPORT_SUFFIX.strip(), request.prompt)

    def test_pass_through_with_slash_is_fully_raw(self):
        prompt = "/goal go build"
        request = self.build(["--pass-through", "codex", "work", prompt])
        self.assert_verbatim(request, prompt)

    def test_dry_run_payload_reports_instruction_mode(self):
        request = self.build(["dry-run", "codex", "work", "/goal fix tests"])
        payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["promptInstructionMode"], PROMPT_INSTRUCTION_MODE_SLASH)
        request = self.build(["dry-run", "codex", "work", "fix tests"])
        payload = self.delegate.dry_run_payload(request)
        self.assertEqual(payload["promptInstructionMode"], PROMPT_INSTRUCTION_MODE_WRAPPED)

    def test_input_json_slash_prompt_is_verbatim(self):
        prompt = "/goal fix everything"
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=self.repo.name
        ) as handle:
            json.dump(
                {"engine": "codex", "mode": "work", "prompt": prompt, "cwd": self.repo.name},
                handle,
            )
            input_path = handle.name
        self.addCleanup(lambda: Path(input_path).unlink(missing_ok=True))
        parsed = self.delegate.parse_cli(["run", "--input-json", input_path])
        request = self.delegate.request_from_parsed(parsed, self.config, io.StringIO(""))
        self.assert_verbatim(request, prompt)

    def test_input_json_slash_prompt_safe_cursor_rejected(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=self.repo.name
        ) as handle:
            json.dump(
                {"engine": "cursor", "mode": "safe", "prompt": "/goal x", "cwd": self.repo.name},
                handle,
            )
            input_path = handle.name
        self.addCleanup(lambda: Path(input_path).unlink(missing_ok=True))
        parsed = self.delegate.parse_cli(["run", "--input-json", input_path])
        with self.assertRaises(DelegateError) as caught:
            self.delegate.request_from_parsed(parsed, self.config, io.StringIO(""))
        self.assertEqual(caught.exception.error, "slash_passthrough_unsupported")


class SlashPassthroughDescribeTests(ExecutionTestBase):
    def test_describe_capability_table(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        payload = self.delegate.describe_payload(config, "defaults")
        modes = payload["promptInstructionModes"]
        self.assertEqual(
            modes["modes"],
            [PROMPT_INSTRUCTION_MODE_WRAPPED, PROMPT_INSTRUCTION_MODE_SLASH],
        )
        self.assertEqual(
            modes["safeModeAllowed"],
            {
                "cursor": False,
                "droid": False,
                "codex": True,
                "kimi": False,
                "claude": True,
                "grok": True,
                "devin": False,
                "opencode": True,
                "pi": True,
            },
        )
        self.assertFalse(modes["callReadOnlyAllowed"])
