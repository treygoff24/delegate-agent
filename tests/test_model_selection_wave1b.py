"""Wave 1b: per-engine model_override consumption, droid/cursor rules, effort coupling."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from tests.delegate_commands_test_base import CommandTestBase, make_git_repo

# Frozen from Wave 1a / pre-1b shapes on embedded_default_config().
_MODELS_KEYS: dict[str, set[str]] = {
    "<root>": {
        "ok",
        "configSource",
        "configResolution",
        "runtime",
        "reasoningAliases",
        "cursor",
        "droid",
        "codex",
        "claude",
        "kimi",
        "grok",
        "devin",
    },
    "cursor": {
        "defaultModel",
        "argvPrefix",
        "defaultReasoningEffort",
        "reasoningEffortModels",
    },
    "droid": {"models", "defaultReasoningEffort"},
    "codex": {"binary", "defaultModel", "defaultReasoningEffort", "profile"},
    "claude": {
        "binary",
        "defaultModel",
        "defaultReasoningEffort",
        "workPermissionMode",
        "noSessionPersistence",
        "bare",
    },
    "kimi": {"binary", "defaultModel", "defaultReasoningEffort"},
    "grok": {
        "binary",
        "defaultModel",
        "defaultReasoningEffort",
        "workPermissionMode",
        "safePermissionMode",
        "safeSandbox",
        "workSandbox",
        "disableWebSearch",
        "noSubagents",
    },
    "devin": {"binary", "defaultModel", "defaultReasoningEffort"},
}

_MODELS_SUMMARY_KEYS: dict[str, set[str]] = {
    "<root>": {
        "ok",
        "summary",
        "configSource",
        "version",
        "aliases",
        "counts",
        "discovery",
    },
    "counts": {"aliases", "providers"},
    "discovery": {"fullModels", "safeSummary", "reasoningCapabilities"},
}

_CAPABILITIES_KEYS: dict[str, set[str]] = {
    "<root>": {"ok", "configSource", "cachePath", "reasoning"},
    "reasoning": {"harnesses", "aliases"},
}


def _assert_argv_has_model(test: unittest.TestCase, argv: list[str], model: str) -> None:
    test.assertIn("--model", argv)
    idx = argv.index("--model")
    test.assertLess(idx + 1, len(argv))
    test.assertEqual(argv[idx + 1], model)


def _nested_key_sets(value: object, *, prefix: str = "") -> dict[str, set[str]]:
    """Map dotted path -> immediate object key set for dict nodes."""
    out: dict[str, set[str]] = {}
    if not isinstance(value, dict):
        return out
    keys = {str(k) for k in value}
    out[prefix or "<root>"] = keys
    for key, child in value.items():
        child_prefix = f"{prefix}.{key}" if prefix else str(key)
        out.update(_nested_key_sets(child, prefix=child_prefix))
    return out


def _assert_payload_superset(
    test: unittest.TestCase,
    baseline: dict[str, set[str]],
    actual: object,
) -> None:
    actual_sets = _nested_key_sets(actual)
    for path, expected_keys in baseline.items():
        test.assertIn(path, actual_sets, f"missing payload node {path!r}")
        test.assertTrue(
            expected_keys <= actual_sets[path],
            f"{path}: missing keys {sorted(expected_keys - actual_sets[path])}",
        )
        node = actual
        if path != "<root>":
            for part in path.split("."):
                assert isinstance(node, dict)
                node = node[part]
        assert isinstance(node, dict)
        for key in expected_keys:
            # Values for pre-existing keys must remain present (identity checked
            # by callers that freeze literals where needed).
            test.assertIn(key, node)


class ModelOverrideDryRunTests(CommandTestBase):
    def test_model_flag_lands_in_request_and_argv_for_all_engines(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        config["droid"]["defaultModel"] = "droid-default-id"
        config["codex"]["defaultModel"] = "gpt-5.5"
        config["claude"]["defaultModel"] = "claude-sonnet-4-6"
        config["grok"]["defaultModel"] = "grok-4.5"
        config["kimi"]["defaultModel"] = "kimi-code/kimi-for-coding"
        config["devin"]["defaultModel"] = "swe-1.7"
        config["cursor"]["defaultModel"] = "composer-2.5"

        cases = (
            ("cursor", ["cursor", "safe", "--model", "pinned-cursor", "review"], "pinned-cursor"),
            ("codex", ["codex", "safe", "--model", "pinned-codex", "review"], "pinned-codex"),
            ("claude", ["claude", "safe", "--model", "pinned-claude", "review"], "pinned-claude"),
            ("grok", ["grok", "safe", "--model", "pinned-grok", "review"], "pinned-grok"),
            ("devin", ["devin", "safe", "--model", "pinned-devin", "review"], "pinned-devin"),
            ("kimi", ["kimi", "safe", "--model", "pinned-kimi", "review"], "pinned-kimi"),
            (
                "droid",
                ["droid", "safe", "--model", "pinned-droid", "review"],
                "pinned-droid",
            ),
        )
        for engine, argv, expected in cases:
            with self.subTest(engine=engine):
                parsed = self.delegate.parse_cli(["--cwd", repo.name, "dry-run", *argv])
                request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
                self.assertEqual(request.model, expected)
                _assert_argv_has_model(self, request.argv, expected)
                payload = self.delegate.dry_run_payload(request)
                self.assertEqual(payload["model"], expected)
                _assert_argv_has_model(self, payload["argv"], expected)

    def test_alias_resolution_and_verbatim_pass_through_per_engine(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        for engine in ("cursor", "codex", "claude", "grok", "devin", "kimi"):
            config[engine]["models"] = {"fast": f"{engine}-fast-id"}
            if config[engine].get("defaultModel") in (None, ""):
                config[engine]["defaultModel"] = f"{engine}-default"

        alias_cases = (
            ("codex", "fast", "codex-fast-id"),
            ("claude", "fast", "claude-fast-id"),
            ("grok", "fast", "grok-fast-id"),
            ("devin", "fast", "devin-fast-id"),
            ("kimi", "fast", "kimi-fast-id"),
            ("cursor", "fast", "cursor-fast-id"),
        )
        for engine, flag_value, expected in alias_cases:
            with self.subTest(engine=engine, kind="alias"):
                parsed = self.delegate.parse_cli(
                    ["--cwd", repo.name, "dry-run", engine, "safe", "--model", flag_value, "x"]
                )
                request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
                self.assertEqual(request.model, expected)
                _assert_argv_has_model(self, request.argv, expected)

        for engine in ("codex", "claude", "grok", "devin", "kimi", "cursor"):
            with self.subTest(engine=engine, kind="passthrough"):
                raw = f"raw-{engine}-id"
                parsed = self.delegate.parse_cli(
                    ["--cwd", repo.name, "dry-run", engine, "safe", "--model", raw, "x"]
                )
                request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
                self.assertEqual(request.model, raw)
                _assert_argv_has_model(self, request.argv, raw)


class DroidModelSelectionTests(CommandTestBase):
    def test_positional_and_model_flag_conflict(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        parsed = self.delegate.parse_cli(
            ["--cwd", repo.name, "droid", "reviewer", "safe", "--model", "other", "review"]
        )
        self.assertEqual(parsed.launch.model_alias, "reviewer")
        self.assertEqual(parsed.launch.model, "other")
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "model_conflict")
        self.assertIn("positional", ctx.exception.message.lower())
        self.assertIn("--model", ctx.exception.message)

    def test_optional_positional_with_model_flag(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        parsed = self.delegate.parse_cli(
            ["--cwd", repo.name, "dry-run", "droid", "safe", "--model", "raw-droid", "review"]
        )
        self.assertIsNone(parsed.launch.model_alias)
        self.assertEqual(parsed.launch.mode, "safe")
        self.assertEqual(parsed.launch.model, "raw-droid")
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(request.model, "raw-droid")
        _assert_argv_has_model(self, request.argv, "raw-droid")

    def test_plain_droid_safe_uses_default_model(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        config["droid"]["defaultModel"] = "factory/default-model"
        parsed = self.delegate.parse_cli(["--cwd", repo.name, "dry-run", "droid", "safe", "review"])
        self.assertIsNone(parsed.launch.model_alias)
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(request.model, "factory/default-model")
        _assert_argv_has_model(self, request.argv, "factory/default-model")

    def test_plain_droid_safe_without_default_requires_model(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        config["droid"].pop("defaultModel", None)
        parsed = self.delegate.parse_cli(["--cwd", repo.name, "dry-run", "droid", "safe", "review"])
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "missing_model")

    def test_strict_positional_vs_pass_through_flag_asymmetry(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}

        with self.assertRaises(self.delegate.DelegateError) as ctx:
            parsed = self.delegate.parse_cli(
                ["--cwd", repo.name, "dry-run", "droid", "unknown-alias", "safe", "review"]
            )
            self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "invalid_alias")

        parsed = self.delegate.parse_cli(
            [
                "--cwd",
                repo.name,
                "dry-run",
                "droid",
                "safe",
                "--model",
                "unknown-raw-id",
                "review",
            ]
        )
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(request.model, "unknown-raw-id")
        _assert_argv_has_model(self, request.argv, "unknown-raw-id")

    def test_droid_model_flag_alias_resolves(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5", "fast": "glm-5.1"}
        parsed = self.delegate.parse_cli(
            ["--cwd", repo.name, "dry-run", "droid", "safe", "--model", "fast", "review"]
        )
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(request.model, "glm-5.1")
        # A map-key hit via --model keeps alias metadata, matching the
        # positional and input-JSON alias paths.
        self.assertEqual(request.model_alias, "fast")

    def test_droid_model_flag_raw_id_has_no_alias_metadata(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "gpt-5.5"}
        parsed = self.delegate.parse_cli(
            ["--cwd", repo.name, "dry-run", "droid", "safe", "--model", "custom:raw", "review"]
        )
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(request.model, "custom:raw")
        self.assertIsNone(request.model_alias)

    def test_droid_input_json_omitted_model_uses_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps({"engine": "droid", "mode": "safe", "cwd": tmp, "prompt": "review"}),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            config["droid"]["defaultModel"] = "factory/json-default"
            request = self.delegate.request_from_input_json(parsed, config)
            self.assertEqual(request.model, "factory/json-default")

    def test_devin_effort_error_names_overridden_model(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        parsed = self.delegate.parse_cli(
            [
                "--cwd",
                repo.name,
                "dry-run",
                "devin",
                "safe",
                "--model",
                "swe-1.7-lightning",
                "--reasoning-effort",
                "high",
                "review",
            ]
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")
        self.assertIn("swe-1.7-lightning", ctx.exception.message)

    def test_kimi_effort_error_names_overridden_model(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        parsed = self.delegate.parse_cli(
            [
                "--cwd",
                repo.name,
                "dry-run",
                "kimi",
                "safe",
                "--model",
                "my-kimi-model",
                "--reasoning-effort",
                "high",
                "review",
            ]
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")
        self.assertIn("my-kimi-model", ctx.exception.message)


class CursorModelOverrideTests(CommandTestBase):
    def test_explicit_model_wins_over_effort_routing(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["cursor"]["reasoningEffortModels"] = {"high": "sonnet-4-thinking"}
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
            reasoning_effort="high",
            model_override="pinned-cursor-model",
        )
        self.assertEqual(request.model, "pinned-cursor-model")
        _assert_argv_has_model(self, request.argv, "pinned-cursor-model")
        self.assertNotIn("sonnet-4-thinking", request.argv)

    def test_explicit_model_with_effort_emits_bypass_warning(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["cursor"]["reasoningEffortModels"] = {"high": "sonnet-4-thinking"}
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            config,
            True,
            reasoning_effort="high",
            model_override="pinned-cursor-model",
        )
        self.assertTrue(
            any("bypass" in w.lower() or "pinned" in w.lower() for w in request.warnings)
        )
        self.assertTrue(
            any("effort" in w.lower() and "model" in w.lower() for w in request.warnings)
        )

    def test_input_json_model_override_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "safe",
                        "model": "input-json-cursor",
                        "cwd": tmp,
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            request = self.delegate.request_from_input_json(parsed, config)
            self.assertEqual(request.model, "input-json-cursor")
            _assert_argv_has_model(self, request.argv, "input-json-cursor")

    def test_input_json_model_no_longer_must_match_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "safe",
                        "model": "not-the-default",
                        "cwd": tmp,
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            # Must not raise invalid_model for mismatch with defaultModel.
            request = self.delegate.request_from_input_json(parsed, config)
            self.assertEqual(request.model, "not-the-default")


class InputJsonModelResolutionTests(CommandTestBase):
    def test_input_json_model_alias_resolves_for_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "safe",
                        "model": "fast",
                        "cwd": tmp,
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            config["codex"]["models"] = {"fast": "gpt-5.5"}
            request = self.delegate.request_from_input_json(parsed, config)
            self.assertEqual(request.model, "gpt-5.5")

    def test_droid_input_json_preserves_model_alias_and_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "minimax",
                        "cwd": tmp,
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            config["droid"]["models"] = {"minimax": "custom:minimax-e2e"}
            request = self.delegate.request_from_input_json(parsed, config)
            self.assertEqual(request.model_alias, "minimax")
            self.assertEqual(request.model, "custom:minimax-e2e")

    def test_droid_input_json_raw_id_passes_through(self):
        # Alias-or-id parity with --model: a non-alias value is passed through
        # verbatim (harness validates) with no modelAlias metadata.
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "model": "custom:raw-model-id",
                        "cwd": tmp,
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            config["droid"]["models"] = {"minimax": "custom:minimax-e2e"}
            request = self.delegate.request_from_input_json(parsed, config)
            self.assertIsNone(request.model_alias)
            self.assertEqual(request.model, "custom:raw-model-id")
            _assert_argv_has_model(self, request.argv, "custom:raw-model-id")

    def test_droid_input_json_still_requires_model_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "droid",
                        "mode": "safe",
                        "cwd": tmp,
                        "prompt": "review",
                    }
                ),
                encoding="utf-8",
            )
            parsed = self.delegate.ParsedCommand(
                "run",
                global_options=self.delegate.GlobalOptions(json_mode=True),
                run_json=self.delegate.RunJsonOptions(str(task)),
            )
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "missing_model")


class EffortCouplingRegressionTests(CommandTestBase):
    def test_codex_model_override_changes_effort_validation(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["codex"]["defaultModel"] = "gpt-5.5"
        # gpt-5.5 supports low/medium/high/xhigh — not "max".
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request(
                "codex",
                "safe",
                None,
                "/repo",
                "review",
                config,
                True,
                reasoning_effort="max",
                model_override="gpt-5.5",
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")
        self.assertIn("gpt-5.5", ctx.exception.message)

    def test_droid_model_override_changes_effort_validation(self):
        config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
        config["droid"]["models"] = {"reviewer": "claude-opus-4-8"}
        # glm-5.1 supports only off|high — not "max".
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.build_git_request(
                "droid",
                "safe",
                None,
                "/repo",
                "review",
                config,
                True,
                reasoning_effort="max",
                model_override="glm-5.1",
            )
        self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")
        self.assertIn("glm-5.1", ctx.exception.message)


class ContractSupersetTests(unittest.TestCase):
    """Plan §1.3a: models/capabilities payloads stay key-set supersets."""

    MODELS_KEYS: ClassVar[dict[str, set[str]]] = _MODELS_KEYS
    MODELS_SUMMARY_KEYS: ClassVar[dict[str, set[str]]] = _MODELS_SUMMARY_KEYS
    CAPABILITIES_KEYS: ClassVar[dict[str, set[str]]] = _CAPABILITIES_KEYS

    def setUp(self):
        from delegate_agent.capability_commands import capabilities_payload
        from delegate_agent.config import embedded_default_config
        from delegate_agent.describe_payload import models_payload, models_summary_payload

        self.embedded_default_config = embedded_default_config
        self.models_payload = models_payload
        self.models_summary_payload = models_summary_payload
        self.capabilities_payload = capabilities_payload

    def test_models_payload_is_key_set_superset(self):
        config = self.embedded_default_config()
        config["droid"]["models"] = {"glm": "glm-5.1"}
        with tempfile.TemporaryDirectory() as workspace:
            payload = self.models_payload(config, "fixture-config", Path(workspace))
            _assert_payload_superset(self, self.MODELS_KEYS, payload)
            # Pre-existing values unchanged for frozen keys.
            self.assertEqual(payload["ok"], True)
            self.assertEqual(payload["configSource"], "fixture-config")
            self.assertEqual(payload["cursor"]["defaultModel"], config["cursor"]["defaultModel"])
            self.assertEqual(payload["droid"]["models"]["glm"], "glm-5.1")

    def test_models_summary_payload_is_key_set_superset(self):
        config = self.embedded_default_config()
        config["droid"]["models"] = {"glm": "glm-5.1"}
        with tempfile.TemporaryDirectory() as workspace:
            payload = self.models_summary_payload(config, "fixture-config", Path(workspace))
            _assert_payload_superset(self, self.MODELS_SUMMARY_KEYS, payload)
            self.assertEqual(payload["ok"], True)
            self.assertEqual(payload["configSource"], "fixture-config")
            self.assertTrue(payload["summary"])
            self.assertIsInstance(payload["aliases"], list)
            self.assertIsInstance(payload["counts"], dict)
            self.assertIsInstance(payload["discovery"], dict)

    def test_capabilities_payload_is_key_set_superset(self):
        config = self.embedded_default_config()
        with tempfile.TemporaryDirectory() as workspace:
            payload = self.capabilities_payload(config, "fixture-config", workspace)
            _assert_payload_superset(self, self.CAPABILITIES_KEYS, payload)
            self.assertEqual(payload["ok"], True)
            self.assertEqual(payload["configSource"], "fixture-config")
            self.assertIn("codex", payload["reasoning"]["harnesses"])


if __name__ == "__main__":
    unittest.main()
