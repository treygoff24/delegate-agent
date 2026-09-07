import json
from pathlib import Path

from tests.delegate_commands_test_base import CommandTestBase


class LaneRPublicSurfaceTests(CommandTestBase):
    def _dry_run(self, engine: str, effort: str) -> tuple[int, dict[str, object]]:
        code, stdout, stderr = self.run_main(
            ["--json", "dry-run", engine, "safe", "--reasoning-effort", effort, "x"]
        )
        self.assertEqual(stderr, "")
        return code, json.loads(stdout)

    def test_grok_xhigh_succeeds_and_max_is_rejected(self) -> None:
        code, payload = self._dry_run("grok", "xhigh")
        self.assertEqual(code, 0)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["resolvedReasoningEffort"], "xhigh")

        code, payload = self._dry_run("grok", "max")
        self.assertEqual(code, 2)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "unsupported_reasoning_effort")

    def test_pi_and_omp_expose_distinct_native_effort_vocabularies(self) -> None:
        code, payload = self._dry_run("pi", "off")
        self.assertEqual(code, 0)
        self.assertEqual(payload["resolvedReasoningEffort"], "off")

        code, payload = self._dry_run("omp", "auto")
        self.assertEqual(code, 0)
        self.assertEqual(payload["resolvedReasoningEffort"], "auto")

        code, payload = self._dry_run("pi", "auto")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "unsupported_reasoning_effort")

    def test_omp_alias_accepts_auto_while_pi_alias_rejects_it(self) -> None:
        config_path = Path(self._config_env["DELEGATE_CONFIG"])
        config_path.write_text(
            json.dumps(
                {
                    "omp": {
                        "models": {
                            "automatic": {
                                "model": "openrouter/example",
                                "thinking": "auto",
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        code, stdout, stderr = self.run_main(
            ["--json", "dry-run", "omp", "safe", "--model", "automatic", "x"]
        )
        self.assertEqual(stderr, "")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["resolvedReasoningEffort"], "auto")
        thinking_index = payload["argv"].index("--thinking")
        self.assertEqual(payload["argv"][thinking_index + 1], "auto")

        config_path.write_text(
            json.dumps(
                {
                    "pi": {
                        "models": {
                            "automatic": {
                                "model": "openrouter/example",
                                "thinking": "auto",
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        code, stdout, stderr = self.run_main(
            ["--json", "dry-run", "pi", "safe", "--model", "automatic", "x"]
        )
        self.assertEqual(stderr, "")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["error"], "invalid_pi_config")
