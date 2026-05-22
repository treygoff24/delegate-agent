import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    spec = importlib.util.spec_from_file_location("delegate_cli_under_test", MODULE_PATH)
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


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def test_cursor_safe_argv_agent_prefix(self):
        argv = self.delegate.build_cursor_argv(["agent"], "safe", "/repo", "composer-2.5", "hello")
        self.assertEqual(argv[0], "agent")
        self.assertEqual(
            argv[1:10],
            [
                "--workspace",
                "/repo",
                "-p",
                "--trust",
                "--model",
                "composer-2.5",
                "--print",
                "--output-format",
                "stream-json",
            ],
        )
        self.assertTrue(argv[10].startswith(self.delegate.CURSOR_SAFE_REVIEW_PREFIX))
        self.assertTrue(argv[10].endswith("hello"))
        self.assertNotIn("--mode=plan", argv)
        self.assertNotIn("--mode=ask", argv)
        self.assertNotIn("--force", argv)
        self.assertNotIn("--approve-mcps", argv)

    def test_cursor_work_argv_cursor_agent_prefix(self):
        argv = self.delegate.build_cursor_argv(
            ["cursor", "agent"], "work", "/repo", "composer-2.5", "hello"
        )
        self.assertEqual(
            argv,
            [
                "cursor",
                "agent",
                "--workspace",
                "/repo",
                "-p",
                "--trust",
                "--approve-mcps",
                "--force",
                "--model",
                "composer-2.5",
                "--print",
                "--output-format",
                "stream-json",
                "hello",
            ],
        )
        self.assertNotIn("--mode=agent", argv)
        self.assertNotIn("--mode=plan", argv)
        self.assertNotIn("--mode=ask", argv)

    def test_droid_safe_argv(self):
        argv = self.delegate.build_droid_argv("droid", "safe", "/repo", "model-id", "hello")
        self.assertEqual(
            argv,
            [
                "droid",
                "exec",
                "--cwd",
                "/repo",
                "--model",
                "model-id",
                "--output-format",
                "stream-json",
                "hello",
            ],
        )
        self.assertNotIn("--auto", argv)
        self.assertNotIn("--use-spec", argv)
        self.assertNotIn("--skip-permissions-unsafe", argv)

    def test_droid_work_argv(self):
        argv = self.delegate.build_droid_argv("droid", "work", "/repo", "model-id", "hello")
        self.assertEqual(
            argv,
            [
                "droid",
                "exec",
                "--cwd",
                "/repo",
                "--skip-permissions-unsafe",
                "--model",
                "model-id",
                "--output-format",
                "stream-json",
                "hello",
            ],
        )

    def test_pass_through_restores_text_argv(self):
        cursor = self.delegate.build_cursor_argv(
            ["agent"], "work", "/repo", "composer-2.5", "hello", stream_capture=False
        )
        self.assertIn("--output-format", cursor)
        self.assertIn("text", cursor)
        self.assertNotIn("--print", cursor)
        droid = self.delegate.build_droid_argv(
            "droid", "safe", "/repo", "model-id", "hello", stream_capture=False
        )
        self.assertNotIn("--output-format", droid)

    def test_invalid_alias_rejected_before_argv(self):
        with self.assertRaises(self.delegate.DelegateError) as ctx:
            self.delegate.build_request(
                "droid", "safe", "nope", "/repo", "hello", self.delegate.DEFAULT_CONFIG, True
            )
        self.assertEqual(ctx.exception.error, "invalid_alias")

    def test_codex_work_default_argv_uses_workspace_sandbox_with_network(self):
        policy = self.delegate.delegate_config.effective_policy(
            self.delegate.DEFAULT_CONFIG,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            self.delegate.DEFAULT_CONFIG["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        exec_index = argv.index("exec")
        self.assertIn("--ask-for-approval", argv[:exec_index])
        self.assertEqual(
            argv[argv.index("--ask-for-approval") + 1],
            "never",
        )
        self.assertIn("--sandbox", argv[exec_index:])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn("-c", argv[exec_index:])
        self.assertIn("sandbox_workspace_write.network_access=true", argv[exec_index:])
        self.assertIn("--json", argv[exec_index:])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_codex_work_trusted_hooks_argv_adds_hook_bypass_only(self):
        config = self.delegate.delegate_config.deep_merge(
            self.delegate.DEFAULT_CONFIG,
            {"policy": {"profile": "trusted-hooks"}},
        )
        policy = self.delegate.delegate_config.effective_policy(
            config,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            config["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        exec_index = argv.index("exec")
        self.assertIn("--dangerously-bypass-hook-trust", argv[exec_index:])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--sandbox", argv[exec_index:])
        self.assertIn("--ask-for-approval", argv[:exec_index])

    def test_codex_work_web_search_argv_when_enabled(self):
        config = self.delegate.delegate_config.deep_merge(
            self.delegate.DEFAULT_CONFIG,
            {"policy": {"work": {"webSearch": True}}},
        )
        policy = self.delegate.delegate_config.effective_policy(
            config,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            config["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        self.assertIn("--search", argv[: argv.index("exec")])

    def test_codex_default_model_null_omits_model_flag(self):
        policy = self.delegate.delegate_config.effective_policy(
            self.delegate.DEFAULT_CONFIG,
            engine="codex",
            mode="work",
        )
        argv = self.delegate.build_codex_argv(
            self.delegate.DEFAULT_CONFIG["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
        )
        self.assertNotIn("--model", argv)

    def test_codex_dry_run_model_null_is_allowed(self):
        request = self.delegate.build_request(
            "codex",
            "work",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertIsNone(payload["model"])
        self.assertNotIn("--model", payload["argv"])

    def test_run_input_json_codex_allows_omitted_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "hello",
                    }
                )
            )
            parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            request = self.delegate.request_from_input_json(
                parsed, self.delegate.DEFAULT_CONFIG
            )
            self.assertIsNone(request.model)
            self.assertNotIn("--model", request.argv)

    def test_run_input_json_codex_rejects_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "engine": "codex",
                        "mode": "work",
                        "cwd": tmp,
                        "prompt": "hello",
                        "profile": "my-profile",
                    }
                )
            )
            parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
            with self.assertRaises(self.delegate.DelegateError) as ctx:
                self.delegate.request_from_input_json(parsed, self.delegate.DEFAULT_CONFIG)
            self.assertEqual(ctx.exception.error, "invalid_input_key")
            self.assertIn("profile", ctx.exception.message)

    def test_describe_preserves_safe_read_only_modes(self):
        payload = self.delegate.describe_payload(self.delegate.DEFAULT_CONFIG, "embedded-default")
        self.assertIn("promptTransforms", payload)
        self.assertIn("skill review", payload["promptTransforms"][0])
        cursor_safe = payload["modeMapping"]["cursor"]["safe"]
        self.assertNotIn("--mode=plan", cursor_safe)
        self.assertNotIn("--mode=ask", cursor_safe)
        self.assertNotIn("--force", cursor_safe)
        self.assertNotIn("--approve-mcps", cursor_safe)
        self.assertIn("<isolated-workspace>", cursor_safe)
        self.assertIn("safeNotes", payload["modeMapping"]["cursor"])
        codex_safe = payload["modeMapping"]["codex"]["safe"]
        self.assertIn("--sandbox", codex_safe)
        self.assertIn("read-only", codex_safe)
        self.assertIn("safeNotes", payload["modeMapping"]["codex"])
        self.assertIn("isolated", payload["modeMapping"]["codex"]["safeNotes"][0].lower())
        self.assertNotIn("--auto", payload["modeMapping"]["droid"]["safe"])
        self.assertNotIn("--use-spec", payload["modeMapping"]["droid"]["safe"])
        self.assertNotIn("--skip-permissions-unsafe", payload["modeMapping"]["droid"]["safe"])

    def test_cursor_safe_dry_run_reports_isolation(self):
        request = self.delegate.build_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello",
            self.delegate.DEFAULT_CONFIG,
            dry_run=True,
        )
        payload = self.delegate.dry_run_payload(request)
        self.assertTrue(payload["isolatedWorkspace"])
        self.assertIn("isolation", payload)
        self.assertNotIn("--mode=plan", payload["argv"])
        self.assertNotIn("--approve-mcps", payload["argv"])

    def test_cursor_safe_cli_config_omits_mutating_shell(self):
        allow = self.delegate.CURSOR_SAFE_CLI_CONFIG["permissions"]["allow"]
        self.assertIn("Read(**)", allow)
        self.assertNotIn("Shell(git)", allow)
        self.assertNotIn("Shell(find)", allow)
        self.assertNotIn("Shell(ls)", allow)

    def test_cursor_safe_cli_config_is_permissions_only(self):
        self.assertEqual(set(self.delegate.CURSOR_SAFE_CLI_CONFIG), {"permissions"})

    def test_write_cursor_safe_project_config_serializes_permissions_only(self):
        with tempfile.TemporaryDirectory() as workspace:
            self.delegate.write_cursor_safe_project_config(Path(workspace))
            config = self.delegate.json.loads((Path(workspace) / ".cursor" / "cli.json").read_text())
        self.assertEqual(set(config), {"permissions"})
        self.assertIn("allow", config["permissions"])
        self.assertIn("deny", config["permissions"])

    def test_mirror_path_preserving_symlinks_keeps_link_target(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            link = Path(workspace) / "link.txt"
            link.symlink_to(secret)

            destination = Path(workspace) / "mirror" / "link.txt"
            self.delegate.mirror_path_preserving_symlinks(link, destination)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(os.readlink(destination), str(secret))

    def test_create_directory_safe_workspace_preserves_symlinks(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside-secret\n")
            (Path(workspace) / "link.txt").symlink_to(secret)

            copy_path, temp_base = self.delegate.create_directory_safe_workspace(workspace)
            try:
                copied = Path(copy_path) / "link.txt"
                self.assertTrue(copied.is_symlink())
                self.assertEqual(os.readlink(copied), str(secret))
            finally:
                shutil.rmtree(temp_base, ignore_errors=True)
