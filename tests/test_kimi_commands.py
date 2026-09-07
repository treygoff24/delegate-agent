import json
import unittest

from delegate_agent import argv_builders as argv_builders_api
from delegate_agent import config as config_api
from delegate_agent import errors as errors_api
from delegate_agent import prompt_instructions
from tests.delegate_commands_test_base import CommandTestBase


class KimiCommandTests(CommandTestBase):
    def test_kimi_safe_argv(self):
        config = config_api.embedded_default_config()
        config["tracking"]["skillReviewPreamble"] = {"enabled": True}
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            dry_run=True,
            frame_prompt=True,
        )
        self.assertNotIn("--plan", request.argv)
        self.assertNotIn("--yolo", request.argv)
        self.assertNotIn("--auto", request.argv)
        self.assertNotIn("--model", request.argv)
        self.assertIn("--output-format", request.argv)
        self.assertIn("stream-json", request.argv)
        self.assertIn("--prompt", request.argv)
        prompt_arg = request.argv[request.argv.index("--prompt") + 1]
        self.assertTrue(prompt_arg.startswith(prompt_instructions.SKILL_REVIEW_PREFIX))
        self.assertIn(argv_builders_api.SAFE_REVIEW_PREFIX_BY_ENGINE["kimi"], prompt_arg)
        self.assertIn("hello", prompt_arg)

    def test_kimi_work_argv(self):
        request = self.build_git_request(
            "kimi",
            "work",
            None,
            "/repo",
            "hello",
            config_api.embedded_default_config(),
            dry_run=True,
        )
        self.assertNotIn("--yolo", request.argv)
        self.assertNotIn("--auto", request.argv)
        self.assertNotIn("--plan", request.argv)
        self.assertIn("--prompt", request.argv)
        prompt_arg = request.argv[request.argv.index("--prompt") + 1]
        self.assertFalse(
            prompt_arg.startswith(argv_builders_api.SAFE_REVIEW_PREFIX_BY_ENGINE["kimi"])
        )
        self.assertTrue(prompt_arg.endswith("hello"))

    def test_kimi_pass_through_argv_pins_text_output(self):
        # Kimi resolves --output-format from the flag, then KIMI_MODEL_OUTPUT_FORMAT,
        # then "text", and it honours that env var in prompt mode — exactly the
        # pass-through case. Omitting the flag let an ambient export flip a
        # pass-through run to stream-json and break its captured output.
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            config_api.embedded_default_config(),
            dry_run=True,
            stream_capture=False,
        )
        self.assertEqual(request.argv[request.argv.index("--output-format") + 1], "text")
        self.assertNotIn("stream-json", request.argv)
        self.assertNotIn("--plan", request.argv)
        self.assertNotIn("--yolo", request.argv)
        self.assertIn("--prompt", request.argv)

    def test_kimi_tracked_argv_still_pins_stream_json(self):
        # The planted negative: pinning text must not leak into tracked runs,
        # whose snapshots need the structured stream.
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            config_api.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.argv[request.argv.index("--output-format") + 1], "stream-json")
        self.assertNotIn("text", request.argv)

    def test_kimi_model_override_from_config(self):
        config = config_api.embedded_default_config()
        config["kimi"]["defaultModel"] = "kimi-code/custom-model"
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            dry_run=True,
        )
        self.assertEqual(request.model, "kimi-code/custom-model")
        self.assertIn("--model", request.argv)
        self.assertIn("kimi-code/custom-model", request.argv)

    def test_kimi_dry_run(self):
        code, out, err = self.run_main(["--json", "dry-run", "kimi", "safe", "hello"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["engine"], "kimi")
        self.assertEqual(payload["mode"], "safe")
        self.assertIsNone(payload["model"])
        self.assertNotIn("--model", payload["argv"])
        self.assertNotIn("--plan", payload["argv"])
        self.assertNotIn("--yolo", payload["argv"])
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertEqual(payload["effectiveIsolation"], "worktree")

    def test_kimi_reasoning_effort_rejected(self):
        code, out, _err = self.run_main(
            ["--json", "kimi", "safe", "--reasoning-effort", "high", "hello"]
        )
        self.assertEqual(code, errors_api.EXIT_USAGE)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unsupported_reasoning_effort")
        self.assertIn("kimi", payload["message"])

    def test_kimi_unconfigured_default_model(self):
        config = config_api.embedded_default_config()
        config["kimi"]["defaultModel"] = None
        request = self.build_git_request(
            "kimi",
            "work",
            None,
            "/repo",
            "hello",
            config,
            dry_run=True,
        )
        self.assertIsNone(request.model)
        self.assertNotIn("--model", request.argv)


if __name__ == "__main__":
    unittest.main()
