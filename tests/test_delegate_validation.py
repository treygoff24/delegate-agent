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
        request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
        self.assertEqual(Path(request.workspace).resolve(), Path(repo.name).resolve())
        self.assertEqual(request.workspace_kind, "git")
        self.assertTrue(request.prompt.startswith(self.delegate.delegate_runner.SKILL_REVIEW_PREFIX))

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
            request = self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
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
