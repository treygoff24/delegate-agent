import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
CONFIG_PATH = ROOT / "src" / "delegate_agent" / "config.py"

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
    config = json.loads(json.dumps(delegate.DEFAULT_CONFIG))
    config["droid"]["models"] = {"minimax": "model-id"}
    return config


class TtyStdin(io.StringIO):
    def isatty(self):
        return True


class NonTtyStdin(io.StringIO):
    def isatty(self):
        return False


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def test_non_git_temp_directory_resolves_as_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.delegate.resolve_workspace(tmp)
            self.assertEqual(Path(workspace.path).resolve(), Path(tmp).resolve())
            self.assertEqual(workspace.kind, "directory")

    def test_git_repo_resolves_from_nested_directory(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        nested = Path(repo.name) / "a" / "b"
        nested.mkdir(parents=True)
        workspace = self.delegate.resolve_workspace(str(nested))
        self.assertEqual(Path(workspace.path).resolve(), Path(repo.name).resolve())
        self.assertEqual(workspace.kind, "git")

    def test_prompt_file_works(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write("from file")
            path = f.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        self.assertEqual(self.delegate.resolve_prompt([], path, TtyStdin()), "from file")

    def test_stdin_works(self):
        self.assertEqual(
            self.delegate.resolve_prompt([], None, NonTtyStdin("from stdin")), "from stdin"
        )

    def test_direct_plus_prompt_file_fails(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt(["direct"], "/tmp/task.md", TtyStdin())
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_direct_plus_stdin_fails(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt(["direct"], None, NonTtyStdin("from stdin"))
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_prompt_file_plus_stdin_fails(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write("from file")
            path = f.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt([], path, NonTtyStdin("from stdin"))
        self.assertEqual(ctx.exception.error, "ambiguous_prompt_source")

    def test_no_prompt_with_tty_fails_without_blocking(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.resolve_prompt([], None, TtyStdin())
        self.assertEqual(ctx.exception.error, "missing_prompt")

    def test_control_characters_fail(self):
        for bad in ["hello\x00", "hello\x01"]:
            with self.subTest(bad=repr(bad)), self.assertRaises(self.delegate.DelegateError):
                self.delegate.validate_prompt(bad)

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
        parsed = self.delegate.ParsedCommand(
            "run", json_mode=True, cwd=str(nested), input_json=str(task)
        )
        request = self.delegate.request_from_input_json(parsed, droid_test_config(self.delegate))
        self.assertEqual(Path(request.workspace).resolve(), Path(repo.name).resolve())
        self.assertEqual(request.workspace_kind, "git")
        self.assertTrue(
            request.prompt.startswith(self.delegate.delegate_runner.SKILL_REVIEW_PREFIX)
        )

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
            parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            request = self.delegate.request_from_input_json(
                parsed, droid_test_config(self.delegate)
            )
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
        parsed = self.delegate.ParsedCommand(
            "run", json_mode=True, cwd=repo2.name, input_json=str(task)
        )
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(ctx.exception.error, "ambiguous_cwd")

    def test_workspace_local_config_overrides_global(self):
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
                json.dumps({"cursor": {"defaultModel": "workspace-model"}})
            )
            with mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg):
                loaded, source = config_mod.load_config(workspace=workspace)
            self.assertEqual(loaded["cursor"]["defaultModel"], "workspace-model")
            self.assertEqual(source, str(workspace / ".delegate" / "config.json"))

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

    def test_no_workspace_local_preserves_global_only_behavior(self):
        config_mod = load_config_module()
        with tempfile.TemporaryDirectory() as tmp:
            global_cfg = Path(tmp) / "config.json"
            global_cfg.write_text(json.dumps({"cursor": {"defaultModel": "global-model"}}))
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            with mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH", global_cfg):
                loaded, source = config_mod.load_config(workspace=workspace)
            self.assertEqual(loaded["cursor"]["defaultModel"], "global-model")
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

    def test_cli_load_config_uses_workspace_when_cwd_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            delegate_dir = Path(tmp) / ".delegate"
            delegate_dir.mkdir()
            (delegate_dir / "config.json").write_text(
                json.dumps({"cursor": {"defaultModel": "from-workspace"}})
            )
            parsed = self.delegate.ParsedCommand("models", cwd=tmp)
            config, _source = self.delegate.load_config(
                workspace=self.delegate.workspace_path_for_config(parsed.cwd)
            )
            self.assertEqual(config["cursor"]["defaultModel"], "from-workspace")

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

    # -- Wave 1 isolation / worktrees config validation --------------------------------

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

    # -- Missing coverage: explicit null rejection + string types --

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

    def test_worktrees_data_home_explicit_null_accepted(self):
        """dataHome: null is the valid explicit default sentinel."""
        config_mod = load_config_module()
        # Should not raise
        config_mod.validate_config(
            config_mod.deep_merge(
                config_mod.DEFAULT_CONFIG,
                {"worktrees": {"dataHome": None}},
            )
        )

    # -- Finding #4: resolve_isolation loaded-config validation (defense in depth) -----

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

    # -- Finding #3: request_from_input_json explicit null isolation -------------------

    def test_request_from_input_json_explicit_null_isolation_raises(self):
        """Direct call to request_from_input_json with isolation: null raises."""
        delegate = load_delegate()
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps({
                    "engine": "droid",
                    "mode": "safe",
                    "model": "minimax",
                    "cwd": tmp,
                    "prompt": "hello",
                    "isolation": None,
                })
            )
            parsed = delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            with self.assertRaises(delegate.DelegateError) as ctx:
                delegate.request_from_input_json(parsed, droid_test_config(delegate))
            self.assertEqual(ctx.exception.error, "invalid_isolation")
            self.assertIn("null", ctx.exception.message.lower())
