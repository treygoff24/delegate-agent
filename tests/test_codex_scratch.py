"""Pure argv contracts; the separate native probe exercises the actual ACL."""

import io
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from delegate_agent import run_registry, runner


class CodexScratchTests(unittest.TestCase):
    def test_profile_is_read_only_plus_one_root_and_keeps_launch_context(self):
        original = [
            "codex",
            "--ask-for-approval",
            "never",
            "--model",
            "model-a",
            "exec",
            "--cd",
            "/review",
            "--sandbox",
            "read-only",
            "--json",
            "-",
        ]
        with tempfile.TemporaryDirectory() as temp:
            updated = runner._codex_argv_with_scratch(original, Path(temp))
            self.assertNotIn("--sandbox", updated)
            self.assertIn("--strict-config", updated)
            self.assertEqual(updated[updated.index("--cd") + 1], "/review")
            self.assertEqual(updated[updated.index("--model") + 1], "model-a")
            self.assertEqual(updated[-1], "-")
            settings = [
                updated[index + 1] for index, value in enumerate(updated[:-1]) if value == "-c"
            ]
            config = tomllib.loads("\n".join(settings))
            name = config["default_permissions"]
            self.assertEqual(
                config["permissions"][name],
                {"extends": ":read-only", "filesystem": {str(Path(temp).resolve()): "write"}},
            )
            second = runner._codex_argv_with_scratch(original, Path(temp))
            self.assertNotEqual(updated, second)

    def test_other_sandbox_modes_and_absent_scratch_are_unchanged(self):
        for argv in (
            ["codex", "exec", "--sandbox", "workspace-write", "task"],
            ["codex", "exec", "task"],
        ):
            self.assertEqual(runner._codex_argv_with_scratch(argv, Path("/unused")), argv)
        argv = ["codex", "exec", "--sandbox", "read-only", "task"]
        self.assertEqual(runner._codex_argv_with_scratch(argv, None), argv)

    def test_symlink_scratch_is_refused_before_changing_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source").mkdir()
            (root / "scratch").symlink_to(root / "source", target_is_directory=True)
            with self.assertRaises(runner.RunnerLaunchError):
                runner._codex_argv_with_scratch(
                    ["codex", "exec", "--sandbox", "read-only", "task"], root / "scratch"
                )

    def test_old_cli_refusal_is_actionable_without_permissive_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = run_registry.ensure_registry(Path(temp), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="codex")
            ctx = runner.RunContext(
                root,
                run_id,
                alias,
                "codex",
                "codex",
                "safe",
                None,
                temp,
                temp,
                "directory",
                False,
                run_registry.utc_now_iso(),
            )
            # A local old-CLI fixture rejects the enforcement flag before a turn.
            executable = Path(temp) / "old-codex"
            executable.write_text(
                f"#!{sys.executable}\nimport sys\nprint(\"error: unexpected argument '--strict-config' found\",file=sys.stderr)\nsys.exit(2)\n"
            )
            executable.chmod(0o700)
            code, payload = runner.execute_tracked(
                [str(executable), "exec", "--sandbox", "read-only", "task"],
                temp,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "codex_scratch_permissions_unavailable")
            self.assertIn("no permissive fallback", payload["message"])
            self.assertEqual(payload["scratchPermissions"]["base"], ":read-only")
