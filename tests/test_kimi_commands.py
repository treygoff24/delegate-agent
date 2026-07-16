import json
import unittest

from tests.delegate_commands_test_base import CommandTestBase


class KimiCommandTests(CommandTestBase):
    def test_kimi_safe_argv(self):
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        self.assertNotIn("--plan", request.argv)
        self.assertNotIn("--yolo", request.argv)
        self.assertNotIn("--auto", request.argv)
        self.assertIn("--model", request.argv)
        self.assertIn("--output-format", request.argv)
        self.assertIn("stream-json", request.argv)
        self.assertIn("--prompt", request.argv)
        prompt_arg = request.argv[request.argv.index("--prompt") + 1]
        self.assertTrue(prompt_arg.startswith(self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE["kimi"]))
        self.assertTrue(prompt_arg.endswith("hello"))

    def test_kimi_work_argv(self):
        request = self.build_git_request(
            "kimi",
            "work",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        self.assertNotIn("--yolo", request.argv)
        self.assertNotIn("--auto", request.argv)
        self.assertNotIn("--plan", request.argv)
        self.assertIn("--prompt", request.argv)
        prompt_arg = request.argv[request.argv.index("--prompt") + 1]
        self.assertFalse(prompt_arg.startswith(self.delegate.SAFE_REVIEW_PREFIX_BY_ENGINE["kimi"]))
        self.assertTrue(prompt_arg.endswith("hello"))

    def test_kimi_pass_through_argv(self):
        request = self.build_git_request(
            "kimi",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
            stream_capture=False,
        )
        self.assertNotIn("--output-format", request.argv)
        self.assertNotIn("stream-json", request.argv)
        self.assertNotIn("--plan", request.argv)
        self.assertNotIn("--yolo", request.argv)
        self.assertIn("--prompt", request.argv)

    def test_kimi_model_override_from_config(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
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
        self.assertEqual(payload["model"], "kimi-code/k3")
        self.assertIn("kimi-code/k3", payload["argv"])
        self.assertNotIn("--plan", payload["argv"])
        self.assertNotIn("--yolo", payload["argv"])
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertEqual(payload["effectiveIsolation"], "worktree")

    def test_kimi_reasoning_effort_rejected(self):
        code, out, _err = self.run_main(
            ["--json", "kimi", "safe", "--reasoning-effort", "high", "hello"]
        )
        self.assertEqual(code, self.delegate.EXIT_USAGE)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unsupported_reasoning_effort")
        self.assertIn("kimi", payload["message"])

    def test_kimi_unconfigured_default_model(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
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
