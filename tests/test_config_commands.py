import importlib
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

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_delegate():
    return importlib.reload(importlib.import_module("delegate_agent.cli"))


class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def run_main(self, argv, *, env):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            code = self.delegate.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_config_init_writes_editable_starter_config(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init"],
                env={"HOME": home, "PATH": os.environ.get("PATH", "")},
            )
            payload = json.loads(stdout)
            path = Path(home) / ".delegate" / "config.json"
            work_path = Path(home) / ".delegate" / "config.work.json"
            personal_path = Path(home) / ".delegate" / "config.personal.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            work_config = json.loads(work_path.read_text(encoding="utf-8"))
            personal_config = json.loads(personal_path.read_text(encoding="utf-8"))

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        self.assertEqual(payload["path"], str(path))
        self.assertEqual(config["droid"]["models"]["reviewer"], "replace-with-read-only-model-id")
        self.assertEqual(
            config["profiles"]["definitions"]["work"]["env"]["CODEX_HOME"],
            "~/replace-with-work-codex-home",
        )
        self.assertEqual(work_config["profiles"]["default"], "work")
        self.assertEqual(personal_config["profiles"]["default"], "personal")
        self.assertIn(str(work_path), payload["profileConfigs"]["created"])
        self.assertIn(str(personal_path), payload["profileConfigs"]["created"])
        self.assertEqual(
            payload["nextAction"],
            "Run delegate setup for automatic harness discovery.",
        )

    def test_config_sync_profiles_materializes_missing_overlays_only(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": os.environ.get("PATH", "")}
            code, _, stderr = self.run_main(["--json", "config", "init"], env=env)
            self.assertEqual(code, self.delegate.EXIT_OK, stderr)
            root = Path(home) / ".delegate"
            work_path = root / "config.work.json"
            personal_path = root / "config.personal.json"
            personal_payload = {"claude": {"binary": "/private/personal/claude"}}
            personal_path.write_text(json.dumps(personal_payload), encoding="utf-8")
            work_path.unlink()

            code, stdout, stderr = self.run_main(["--json", "config", "sync-profiles"], env=env)
            payload = json.loads(stdout)
            work_config = json.loads(work_path.read_text(encoding="utf-8"))
            personal_config = json.loads(personal_path.read_text(encoding="utf-8"))

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        self.assertEqual(payload["action"], "sync-profiles")
        self.assertIn(str(work_path), payload["profileConfigs"]["created"])
        self.assertIn(str(personal_path), payload["profileConfigs"]["existing"])
        self.assertEqual(work_config["profiles"]["default"], "work")
        self.assertEqual(personal_config, personal_payload)

    def test_config_sync_profiles_requires_base_config(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["--json", "config", "sync-profiles"],
                env={"HOME": home, "PATH": os.environ.get("PATH", "")},
            )
            payload = json.loads(stdout)

        self.assertEqual(code, self.delegate.EXIT_USAGE, stderr)
        self.assertEqual(payload["error"], "config_not_found")

    def test_config_sync_profiles_rejects_invalid_profile_env_before_writing(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / ".delegate"
            root.mkdir()
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "definitions": {
                                "work": {"env": {"API_KEY": "do-not-copy"}},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            code, stdout, stderr = self.run_main(
                ["--json", "config", "sync-profiles"],
                env={"HOME": home, "PATH": os.environ.get("PATH", "")},
            )
            payload = json.loads(stdout)
            work_exists = (root / "config.work.json").exists()

        self.assertEqual(code, self.delegate.EXIT_USAGE, stderr)
        self.assertEqual(payload["error"], "secret_in_profile_env")
        self.assertFalse(work_exists)

    def test_config_init_refuses_existing_without_force(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".delegate" / "config.json"
            path.parent.mkdir()
            path.write_text('{"old": true}\n', encoding="utf-8")
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init"],
                env={"HOME": home, "PATH": os.environ.get("PATH", "")},
            )
            payload = json.loads(stdout)

        self.assertEqual(code, self.delegate.EXIT_USAGE, stderr)
        self.assertEqual(payload["error"], "config_exists")

    def test_config_init_text_recommends_setup(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["config", "init"],
                env={"HOME": home, "PATH": os.environ.get("PATH", "")},
            )

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        self.assertIn("run delegate setup for automatic harness discovery", stdout)

    def test_config_init_honors_delegate_config_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init", "--force"],
                env={
                    "HOME": tmp,
                    "DELEGATE_CONFIG": str(path),
                    "PATH": os.environ.get("PATH", ""),
                },
            )
            payload = json.loads(stdout)
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        self.assertEqual(payload["path"], str(path))
        self.assertIn("codex", config)

    def test_config_init_rejects_windows_delegate_config_in_wsl(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["--json", "config", "init"],
                env={
                    "HOME": home,
                    "PATH": os.environ.get("PATH", ""),
                    "WSL_DISTRO_NAME": "Ubuntu",
                    "DELEGATE_CONFIG": r"C:\Users\trey\.delegate\config.json",
                },
            )
            payload = json.loads(stdout)

        self.assertEqual(code, self.delegate.EXIT_USAGE, stderr)
        self.assertEqual(payload["error"], "windows_path")
        self.assertIn("wslpath", payload["message"])


class LauncherShimTests(unittest.TestCase):
    def run_shim(self, argv, *, env):
        return subprocess.run(
            [str(ROOT / "bin" / "delegate-profile-shim"), *argv],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def write_probe(self, root: Path) -> Path:
        probe = root / "probe.py"
        probe.write_text(
            "import json, os, sys\n"
            "json.dump({\n"
            "  'argv': sys.argv[1:],\n"
            "  'delegateConfig': os.environ.get('DELEGATE_CONFIG'),\n"
            "  'aiProfile': os.environ.get('AI_PROFILE'),\n"
            "}, sys.stdout)\n",
            encoding="utf-8",
        )
        return probe

    def shim_env(self, home: str, probe: Path, profile: str) -> dict[str, str]:
        return {
            "HOME": home,
            "PATH": os.environ.get("PATH", ""),
            "AI_PROFILE": profile,
            "DELEGATE_SHIM_PY": str(probe),
        }

    def test_missing_work_profile_launch_and_mutation_commands_are_blocked(self):
        with tempfile.TemporaryDirectory() as home:
            probe = self.write_probe(Path(home))
            env = self.shim_env(home, probe, "work")
            for argv in (
                ["codex", "safe", "hello"],
                ["cancel", "codex-1"],
                ["worktree", "remove", "cursor-1"],
                ["worktree", "prune"],
                ["capabilities", "refresh"],
                ["models", "codex", "--live"],
                ["models", "--live", "codex"],
                # A refresh positional after a flag must still be caught, not just $1.
                ["capabilities", "--verbose", "refresh"],
                # Bare `list` is not a documented top-level subcommand (the Python
                # parser silently aliases it to `runs`); the shim no longer special-
                # cases it, so it falls through to the conservative default.
                ["list"],
            ):
                with self.subTest(argv=argv):
                    result = self.run_shim(argv, env=env)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("AI_PROFILE=work", result.stderr)
                    self.assertIn("refusing to run a launch or mutation command", result.stderr)
                    self.assertIn("config sync-profiles", result.stderr)
                    self.assertIn("env -u AI_PROFILE", result.stderr)
                    self.assertIn("DELEGATE_CONFIG=/path/to/config.json", result.stderr)

    def test_missing_profile_read_only_commands_pass_with_warning(self):
        with tempfile.TemporaryDirectory() as home:
            probe = self.write_probe(Path(home))
            env = self.shim_env(home, probe, "work")
            for argv in (
                ["profiles"],
                ["runs"],
                ["ps"],
                ["run-output", "codex-1"],
                ["describe"],
                ["models"],
                ["models", "codex"],
                ["models", "codex", "--live", "--help"],
                ["models", "codex", "--help", "--live"],
                ["snapshot", "codex-1"],
                ["capabilities"],
                ["capabilities", "--json"],
                ["capabilities", "refresh", "--help"],
                ["capabilities", "--help", "refresh"],
                ["worktree", "show", "cursor-1"],
                ["worktree", "list"],
                ["cursor", "--help"],
                ["cursor", "safe", "--help"],
                ["droid", "opus", "safe", "-h"],
                ["dry-run", "codex", "safe", "--help"],
            ):
                with self.subTest(argv=argv):
                    result = self.run_shim(argv, env=env)
                    payload = json.loads(result.stdout)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(payload["argv"], argv)
                    self.assertEqual(payload["aiProfile"], "work")
                    self.assertIsNone(payload["delegateConfig"])
                    self.assertIn("continuing because", result.stderr)
                    self.assertIn("read-only", result.stderr)

    def test_help_inside_launch_prompt_cannot_bypass_missing_profile_guard(self):
        with tempfile.TemporaryDirectory() as home:
            probe = self.write_probe(Path(home))
            env = self.shim_env(home, probe, "work")
            for argv in (
                ["cursor", "safe", "inspect", "--help"],
                ["codex", "work", "please inspect -h"],
                ["droid", "opus", "safe", "review", "-h"],
                ["dry-run", "kimi", "safe", "explain", "--help"],
            ):
                with self.subTest(argv=argv):
                    result = self.run_shim(argv, env=env)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("refusing to run a launch or mutation command", result.stderr)

    def test_missing_personal_profile_launch_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as home:
            probe = self.write_probe(Path(home))
            result = self.run_shim(
                ["codex", "safe", "hello"],
                env=self.shim_env(home, probe, "personal"),
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("AI_PROFILE=personal", result.stderr)
        self.assertIn("refusing to run a launch or mutation command", result.stderr)

    def test_unrecognized_ai_profile_warns_but_does_not_block(self):
        with tempfile.TemporaryDirectory() as home:
            probe = self.write_probe(Path(home))
            # Miscased/typo'd AI_PROFILE is not "work" or "personal", so the
            # fail-closed gate never engages: no config.<profile>.json lookup,
            # no block, just a warning that the base account is in use.
            result = self.run_shim(
                ["codex", "safe", "hello"],
                env=self.shim_env(home, probe, "Work"),
            )
            payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["argv"], ["codex", "safe", "hello"])
        self.assertIsNone(payload["delegateConfig"])
        self.assertIn(
            "AI_PROFILE='Work' is not a recognized profile (work|personal)", result.stderr
        )
        self.assertIn("running on the base account", result.stderr)

    def test_existing_profile_config_is_exported_before_exec(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / ".delegate"
            root.mkdir()
            config_path = root / "config.work.json"
            config_path.write_text("{}\n", encoding="utf-8")
            probe = self.write_probe(Path(home))
            result = self.run_shim(
                ["profiles"],
                env=self.shim_env(home, probe, "work"),
            )
            payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["delegateConfig"], str(config_path))
        self.assertEqual(result.stderr, "")


class ProfileGuardCliTests(unittest.TestCase):
    """Python-CLI-layer mirror of LauncherShimTests: the same fail-closed
    guarantee, enforced in delegate_agent.cli.main via profile_guard, so it
    holds for the pip console entry point, `python -m delegate_agent.cli`, and
    bin/delegate.py -- not only when bin/delegate-profile-shim is in front."""

    def setUp(self):
        self.delegate = load_delegate()

    def run_main(self, argv, *, env):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            code = self.delegate.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def base_env(self, home: str, *, profile: str) -> dict[str, str]:
        return {"HOME": home, "PATH": os.environ.get("PATH", ""), "AI_PROFILE": profile}

    def test_guard_blocks_launch_command_when_profile_config_missing(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["codex", "safe", "hello"], env=self.base_env(home, profile="work")
            )

        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertEqual(stdout, "")
        self.assertIn("AI_PROFILE=work", stderr)
        self.assertIn("refusing to run a launch or mutation command", stderr)
        self.assertIn("config sync-profiles", stderr)
        self.assertIn("env -u AI_PROFILE", stderr)

    def test_guard_blocks_worktree_remove_for_personal_profile(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, stderr = self.run_main(
                ["worktree", "remove", "cursor-1"],
                env=self.base_env(home, profile="personal"),
            )

        self.assertEqual(code, self.delegate.EXIT_USAGE)
        self.assertEqual(stdout, "")
        self.assertIn("AI_PROFILE=personal", stderr)
        self.assertIn("refusing to run a launch or mutation command", stderr)

    def test_guard_blocks_capabilities_refresh_but_allows_capabilities(self):
        with tempfile.TemporaryDirectory() as home:
            env = self.base_env(home, profile="work")
            block_code, block_stdout, block_stderr = self.run_main(
                ["capabilities", "refresh"], env=env
            )
            allow_code, allow_stdout, allow_stderr = self.run_main(
                ["--json", "--cwd", home, "capabilities"], env=env
            )

        self.assertEqual(block_code, self.delegate.EXIT_USAGE)
        self.assertEqual(block_stdout, "")
        self.assertIn("refusing to run a launch or mutation command", block_stderr)

        self.assertEqual(allow_code, self.delegate.EXIT_OK, allow_stderr)
        json.loads(allow_stdout)  # capabilities still produced its normal payload
        self.assertIn("continuing because 'capabilities' is read-only", allow_stderr)

    def test_guard_blocks_live_models_but_allows_cached_models(self):
        with tempfile.TemporaryDirectory() as home:
            env = self.base_env(home, profile="work")
            block_code, block_stdout, block_stderr = self.run_main(
                ["models", "codex", "--live"], env=env
            )
            allow_code, allow_stdout, allow_stderr = self.run_main(
                ["--json", "models", "codex"], env=env
            )

        self.assertEqual(block_code, self.delegate.EXIT_USAGE)
        self.assertEqual(block_stdout, "")
        self.assertIn("refusing to run a launch or mutation command", block_stderr)
        self.assertEqual(allow_code, self.delegate.EXIT_OK, allow_stderr)
        self.assertEqual(json.loads(allow_stdout)["engine"], "codex")
        self.assertIn("continuing because 'models' is read-only", allow_stderr)

    def test_guard_allows_read_only_commands_with_warning_when_profile_config_missing(self):
        with tempfile.TemporaryDirectory() as home:
            env = self.base_env(home, profile="work")
            code, stdout, stderr = self.run_main(["--json", "--cwd", home, "profiles"], env=env)

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        json.loads(stdout)
        self.assertIn("AI_PROFILE=work", stderr)
        self.assertIn("continuing because 'profiles' is read-only", stderr)
        self.assertIn(
            "Launch and mutation commands remain blocked until the profile config exists",
            stderr,
        )

    def test_guard_allows_ps_alias_with_warning(self):
        with tempfile.TemporaryDirectory() as home:
            env = self.base_env(home, profile="work")
            code, stdout, stderr = self.run_main(["--json", "--cwd", home, "ps"], env=env)

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        json.loads(stdout)
        self.assertNotIn("refusing to run a launch or mutation command", stderr)
        self.assertIn("continuing because 'ps' is read-only", stderr)

    def test_guard_allows_worktree_list_with_warning(self):
        # An empty tmp cwd has no run registry at all, so worktree list still
        # fails with worktree_commands' own no_registry error -- that failure
        # is downstream of the guard, not from it. The guard's job is only to
        # not fail-closed here, which is what the absence of its message and
        # the presence of its read-only warning both confirm.
        with tempfile.TemporaryDirectory() as home:
            env = self.base_env(home, profile="work")
            _code, _stdout, stderr = self.run_main(
                ["--json", "--cwd", home, "worktree", "list"], env=env
            )

        self.assertNotIn("refusing to run a launch or mutation command", stderr)
        self.assertIn("continuing because 'worktree' is read-only", stderr)

    def test_guard_blocks_worktree_remove_but_allows_worktree_show(self):
        with tempfile.TemporaryDirectory() as home:
            env = self.base_env(home, profile="work")
            block_code, _, block_stderr = self.run_main(
                ["--cwd", home, "worktree", "remove", "cursor-1"], env=env
            )
            _show_code, _, show_stderr = self.run_main(
                ["--json", "--cwd", home, "worktree", "show", "cursor-1"], env=env
            )

        self.assertEqual(block_code, self.delegate.EXIT_USAGE)
        self.assertIn("refusing to run a launch or mutation command", block_stderr)
        # worktree show on a handle that was never registered fails downstream
        # (not_found), but that failure must come from worktree_commands, not
        # the profile guard -- so it must not carry the guard's fix message.
        self.assertNotIn("refusing to run a launch or mutation command", show_stderr)
        self.assertIn("continuing because 'worktree' is read-only", show_stderr)

    def test_guard_does_not_refire_when_delegate_config_already_set(self):
        # Mirrors shim precedence: once DELEGATE_CONFIG is exported (by the
        # shim, or set directly), the Python-layer guard must not re-fire --
        # it only cares that some config was explicitly selected, matching
        # "the shim path must keep working unchanged" from the design brief.
        with tempfile.TemporaryDirectory() as home:
            config_path = Path(home) / "custom.json"
            config_path.write_text("{}\n", encoding="utf-8")
            env = {
                "HOME": home,
                "PATH": os.environ.get("PATH", ""),
                "AI_PROFILE": "work",
                "DELEGATE_CONFIG": str(config_path),
            }
            _code, stdout, stderr = self.run_main(["dry-run", "codex", "safe", "hello"], env=env)

        # No profile_config_missing failure and no guard warning: DELEGATE_CONFIG
        # being set short-circuits the guard before it inspects AI_PROFILE at all.
        self.assertNotIn("profile_config_missing", stdout)
        self.assertNotIn("AI_PROFILE", stderr)
        self.assertNotIn("is not a recognized profile", stderr)

    def test_guard_warns_on_unrecognized_profile_without_blocking(self):
        with tempfile.TemporaryDirectory() as home:
            env = self.base_env(home, profile="Work")
            code, stdout, stderr = self.run_main(["--json", "--cwd", home, "describe"], env=env)

        self.assertEqual(code, self.delegate.EXIT_OK, stderr)
        json.loads(stdout)
        self.assertIn("AI_PROFILE='Work' is not a recognized profile (work|personal)", stderr)
        self.assertIn("running on the base account", stderr)
        # Unrecognized names never reach the missing-overlay check at all.
        self.assertNotIn("config sync-profiles", stderr)


if __name__ == "__main__":
    unittest.main()
