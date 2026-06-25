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
CODEX_AUTH_PATH = ROOT / "src" / "delegate_agent" / "codex_auth.py"
RUNNER_PATH = ROOT / "src" / "delegate_agent" / "runner.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import config as config_mod  # noqa: E402
from delegate_agent.errors import DelegateError  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
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
    subprocess.run(
        ["git", "-C", temp.name, "config", "user.email", "test@example.com"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", temp.name, "config", "user.name", "Test"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    readme = Path(temp.name) / "README.md"
    readme.write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", temp.name, "add", "README.md"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", temp.name, "commit", "-m", "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return temp


def write_codex_home(root: Path) -> str:
    home = root / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
    (home / "config.toml").write_text("[profile.delegate]\n", encoding="utf-8")
    return str(home)


class CodexAuthConfigTests(unittest.TestCase):
    def setUp(self):
        self.codex_auth = load_module(CODEX_AUTH_PATH, "codex_auth_under_test")

    def test_default_config_still_validates(self):
        config_mod.validate_config(config_mod.embedded_default_config())

    def test_valid_auth_profile_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            work = write_codex_home(Path(tmp) / "work")
            config = config_mod.deep_merge(
                config_mod.embedded_default_config(),
                {
                    "codex": {
                        "authProfile": "personal",
                        "fallbackAuthProfile": "work",
                        "authProfiles": {
                            "personal": {"codexHome": personal},
                            "work": {"codexHome": work},
                        },
                    }
                },
            )
            config_mod.validate_config(config)

    def test_bad_codex_home_rejects(self):
        config = config_mod.deep_merge(
            config_mod.embedded_default_config(),
            {"codex": {"authProfiles": {"personal": {"codexHome": "relative/path"}}}},
        )
        with self.assertRaises(config_mod.ConfigError):
            config_mod.validate_config(config)

    def test_unknown_selected_profile_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            config = config_mod.deep_merge(
                config_mod.embedded_default_config(),
                {
                    "codex": {
                        "authProfile": "missing",
                        "authProfiles": {"personal": {"codexHome": personal}},
                    }
                },
            )
            with self.assertRaises(config_mod.ConfigError):
                config_mod.validate_config(config)

    def test_same_selected_and_fallback_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            config = config_mod.deep_merge(
                config_mod.embedded_default_config(),
                {
                    "codex": {
                        "authProfile": "personal",
                        "fallbackAuthProfile": "personal",
                        "authProfiles": {"personal": {"codexHome": personal}},
                    }
                },
            )
            with self.assertRaises(config_mod.ConfigError):
                config_mod.validate_config(config)

    def test_preflight_missing_auth_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "empty"
            home.mkdir()
            config = {
                "codex": {
                    "profile": "delegate",
                    "authProfile": "personal",
                    "authProfiles": {"personal": {"codexHome": str(home)}},
                }
            }
            with self.assertRaises(Exception) as ctx:
                self.codex_auth.preflight_codex_auth(config)
            self.assertIn("auth.json", str(ctx.exception))

    def test_preflight_missing_fallback_auth_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            bad_fallback = Path(tmp) / "bad-fallback"
            bad_fallback.mkdir()
            config = {
                "codex": {
                    "authProfile": "personal",
                    "fallbackAuthProfile": "work",
                    "authProfiles": {
                        "personal": {"codexHome": personal},
                        "work": {"codexHome": str(bad_fallback)},
                    },
                }
            }
            with self.assertRaises(Exception) as ctx:
                self.codex_auth.preflight_codex_auth(config)
            self.assertIn("auth.json", str(ctx.exception))


class CodexAuthCommandTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(MODULE_PATH, "delegate_codex_auth_cli")

    def test_parse_codex_auth_commands(self):
        for argv, action, profile, fallback in [
            (["codex-auth", "show"], "show", None, None),
            (["codex-auth", "use", "personal"], "use", "personal", None),
            (["codex-auth", "use", "work", "--fallback", "personal"], "use", "work", "personal"),
            (["codex-auth", "swap"], "swap", None, None),
            (["codex-auth", "clear"], "clear", None, None),
        ]:
            with self.subTest(argv=argv):
                parsed = self.delegate.parse_cli(argv)
                self.assertEqual(parsed.subcommand, "codex-auth")
                command = parsed.codex_auth
                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(command.action, action)
                self.assertEqual(command.profile, profile)
                self.assertEqual(command.fallback, fallback)

    def test_fallback_rejected_for_non_use_actions(self):
        for argv in (
            ["codex-auth", "show", "--fallback", "work"],
            ["codex-auth", "swap", "--fallback", "work"],
            ["codex-auth", "clear", "--fallback", "work"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(DelegateError) as ctx:
                    self.delegate.parse_cli(argv)
                self.assertEqual(ctx.exception.error, "invalid_option")

    def test_use_edits_delegate_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            personal = write_codex_home(Path(tmp) / "personal")
            work = write_codex_home(Path(tmp) / "work")
            config_path.write_text(
                json.dumps(
                    {
                        "codex": {
                            "authProfiles": {
                                "personal": {"codexHome": personal},
                                "work": {"codexHome": work},
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            env = {**os.environ, "DELEGATE_CONFIG": str(config_path)}
            with mock.patch.dict(os.environ, env, clear=False):
                exit_code = self.delegate.main(
                    ["--json", "codex-auth", "use", "personal", "--fallback", "work"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 0)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["codex"]["authProfile"], "personal")
            self.assertEqual(saved["codex"]["fallbackAuthProfile"], "work")

    def test_swap_requires_both_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"codex": {"authProfile": "personal"}}) + "\n",
                encoding="utf-8",
            )
            env = {**os.environ, "DELEGATE_CONFIG": str(config_path)}
            with mock.patch.dict(os.environ, env, clear=False):
                exit_code = self.delegate.main(
                    ["codex-auth", "swap"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 2)

    def test_clear_nulls_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex": {
                            "authProfile": "personal",
                            "fallbackAuthProfile": "work",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            env = {**os.environ, "DELEGATE_CONFIG": str(config_path)}
            with mock.patch.dict(os.environ, env, clear=False):
                exit_code = self.delegate.main(
                    ["--json", "codex-auth", "clear"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 0)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("authProfile", saved["codex"])
            self.assertNotIn("fallbackAuthProfile", saved["codex"])

    def test_use_unknown_fallback_does_not_corrupt_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            personal = write_codex_home(Path(tmp) / "personal")
            original = {
                "codex": {
                    "authProfile": "personal",
                    "authProfiles": {"personal": {"codexHome": personal}},
                }
            }
            config_path.write_text(json.dumps(original) + "\n", encoding="utf-8")
            env = {**os.environ, "DELEGATE_CONFIG": str(config_path)}
            with mock.patch.dict(os.environ, env, clear=False):
                exit_code = self.delegate.main(
                    ["codex-auth", "use", "personal", "--fallback", "missing"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 2)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, original)

    def test_use_same_fallback_does_not_corrupt_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            personal = write_codex_home(Path(tmp) / "personal")
            work = write_codex_home(Path(tmp) / "work")
            original = {
                "codex": {
                    "authProfile": "personal",
                    "fallbackAuthProfile": "work",
                    "authProfiles": {
                        "personal": {"codexHome": personal},
                        "work": {"codexHome": work},
                    },
                }
            }
            config_path.write_text(json.dumps(original) + "\n", encoding="utf-8")
            env = {**os.environ, "DELEGATE_CONFIG": str(config_path)}
            with mock.patch.dict(os.environ, env, clear=False):
                exit_code = self.delegate.main(
                    ["codex-auth", "use", "personal", "--fallback", "personal"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 2)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, original)

    def test_use_copies_workspace_profiles_to_global_target(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with (
            tempfile.TemporaryDirectory() as home_tmp,
            tempfile.TemporaryDirectory() as profile_tmp,
        ):
            personal = write_codex_home(Path(profile_tmp) / "personal")
            work = write_codex_home(Path(profile_tmp) / "work")
            workspace_config_dir = Path(repo.name) / ".delegate"
            workspace_config_dir.mkdir()
            workspace_config_path = workspace_config_dir / "config.json"
            workspace_config_path.write_text(
                json.dumps(
                    {
                        "codex": {
                            "authProfiles": {
                                "personal": {"codexHome": personal},
                                "work": {"codexHome": work},
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            global_config_path = Path(home_tmp) / ".delegate" / "config.json"
            env = {key: value for key, value in os.environ.items() if key != "DELEGATE_CONFIG"}
            env["HOME"] = home_tmp
            with mock.patch.dict(os.environ, env, clear=True):
                exit_code = self.delegate.main(
                    [
                        "--cwd",
                        repo.name,
                        "--json",
                        "codex-auth",
                        "use",
                        "personal",
                        "--fallback",
                        "work",
                    ],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(global_config_path.exists())
            saved = json.loads(global_config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["codex"]["authProfile"], "personal")
            self.assertEqual(saved["codex"]["fallbackAuthProfile"], "work")
            self.assertEqual(
                saved["codex"]["authProfiles"]["personal"]["codexHome"],
                personal,
            )
            self.assertEqual(saved["codex"]["authProfiles"]["work"]["codexHome"], work)

    def test_use_refuses_tracked_repo_config_target(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            work = write_codex_home(Path(tmp) / "work")
            config_path = Path(repo.name) / "config.json"
            original = {
                "codex": {
                    "authProfiles": {
                        "personal": {"codexHome": personal},
                        "work": {"codexHome": work},
                    }
                }
            }
            config_path.write_text(json.dumps(original) + "\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", repo.name, "add", "config.json"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", repo.name, "commit", "-m", "add config"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = {**os.environ, "DELEGATE_CONFIG": str(config_path)}
            with mock.patch.dict(os.environ, env, clear=False):
                exit_code = self.delegate.main(
                    ["codex-auth", "use", "personal"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 2)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, original)


class CodexAuthEnvTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(MODULE_PATH, "delegate_codex_auth_env_cli")
        self.runner = load_module(RUNNER_PATH, "delegate_codex_auth_runner")
        self.registry = load_module(REGISTRY_PATH, "delegate_codex_auth_registry")
        self.codex_auth = load_module(CODEX_AUTH_PATH, "codex_auth_env_under_test")

    def test_fake_codex_sees_codex_home(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            env_file = Path(tmp) / "env.txt"
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "${{CODEX_HOME:-}}" > "{env_file}"\n'
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "exit 0\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            config = config_mod.embedded_default_config()
            config["codex"] = dict(config["codex"])
            config["codex"]["binary"] = str(fake_bin)
            config["codex"]["authProfile"] = "personal"
            config["codex"]["authProfiles"] = {"personal": {"codexHome": personal}}
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(repo.name, "git"),
                "review",
                config,
                dry_run=False,
            )
            self.assertEqual(request.env_overrides, {"CODEX_HOME": personal})
            registry_root = self.registry.ensure_registry(Path(repo.name), workspace_kind="git")
            run_id, alias = self.registry.register_run(registry_root, harness="codex")
            ctx = self.delegate.make_run_context(
                registry_root,
                request,
                run_id=run_id,
                alias=alias,
                source_workspace=self.delegate.ResolvedWorkspace(repo.name, "git"),
            )
            exit_code, _payload = self.runner.execute_tracked(
                request.argv,
                repo.name,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                stdin_text="review",
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(env_file.read_text(encoding="utf-8").strip(), personal)

    def test_config_selected_codex_home_beats_parent_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            other = write_codex_home(Path(tmp) / "other")
            config = config_mod.embedded_default_config()
            config["codex"] = dict(config["codex"])
            config["codex"]["authProfile"] = "personal"
            config["codex"]["authProfiles"] = {"personal": {"codexHome": personal}}
            overrides, profile, _fallback = self.codex_auth.resolve_codex_auth_for_request(config)
            self.assertEqual(profile, "personal")
            self.assertEqual(overrides["CODEX_HOME"], personal)
            with mock.patch.dict(os.environ, {"CODEX_HOME": other}, clear=False):
                env = self.codex_auth.child_environment(overrides=overrides)
                self.assertEqual(env["CODEX_HOME"], personal)

    def test_preflight_fails_when_fallback_lacks_auth_json_before_codex(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            bad_fallback = Path(tmp) / "bad-fallback"
            bad_fallback.mkdir()
            invoked = Path(tmp) / "invoked.txt"
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(
                f'#!/usr/bin/env bash\necho 1 > "{invoked}"\nexit 0\n',
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex": {
                            "binary": str(fake_bin),
                            "authProfile": "personal",
                            "fallbackAuthProfile": "work",
                            "authProfiles": {
                                "personal": {"codexHome": personal},
                                "work": {"codexHome": str(bad_fallback)},
                            },
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            env = {**os.environ, "DELEGATE_CONFIG": str(config_path)}
            with mock.patch.dict(os.environ, env, clear=False):
                exit_code = self.delegate.main(
                    ["--cwd", repo.name, "codex", "safe", "task"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 2)
            self.assertFalse(invoked.exists())


class CodexAuthFallbackTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(MODULE_PATH, "delegate_codex_auth_fallback_cli")
        self.runner = load_module(RUNNER_PATH, "delegate_codex_auth_fallback_runner")
        self.registry = load_module(REGISTRY_PATH, "delegate_codex_auth_fallback_registry")
        self.codex_auth = load_module(CODEX_AUTH_PATH, "codex_auth_fallback_under_test")

    def _run_with_fake_codex(
        self,
        *,
        script_body: str,
        mode: str = "safe",
        repo: tempfile.TemporaryDirectory | None = None,
        cwd: str | None = None,
    ) -> tuple[int, dict | None, Path, str, tempfile.TemporaryDirectory]:
        if repo is None:
            repo = make_git_repo()
        assert repo is not None
        execution_cwd = cwd or repo.name
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            work = write_codex_home(Path(tmp) / "work")
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(script_body.replace("__WORK_HOME__", work), encoding="utf-8")
            fake_bin.chmod(0o755)
            config = config_mod.embedded_default_config()
            config["codex"] = dict(config["codex"])
            config["codex"]["binary"] = str(fake_bin)
            config["codex"]["authProfile"] = "personal"
            config["codex"]["fallbackAuthProfile"] = "work"
            config["codex"]["authProfiles"] = {
                "personal": {"codexHome": personal},
                "work": {"codexHome": work},
            }
            request = self.delegate.build_request(
                "codex",
                mode,
                None,
                self.delegate.ResolvedWorkspace(repo.name, "git"),
                "task",
                config,
                dry_run=False,
            )
            registry_root = self.registry.ensure_registry(Path(repo.name), workspace_kind="git")
            run_id, alias = self.registry.register_run(registry_root, harness="codex")
            ctx = self.delegate.make_run_context(
                registry_root,
                request,
                run_id=run_id,
                alias=alias,
                source_workspace=self.delegate.ResolvedWorkspace(repo.name, "git"),
                fallback_env_overrides={"CODEX_HOME": work},
            )
            exit_code, payload = self.runner.execute_tracked(
                request.argv,
                execution_cwd,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                stdin_text="task",
            )
            return exit_code, payload, registry_root, run_id, repo

    def test_usage_limit_retries_with_fallback(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        exit_code, payload, registry_root, run_id, repo = self._run_with_fake_codex(
            script_body=(
                "#!/usr/bin/env bash\n"
                'if [ "${CODEX_HOME}" = "__WORK_HOME__" ]; then\n'
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "  exit 0\n"
                "fi\n"
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            ),
            repo=repo,
        )
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(payload)
        assert payload is not None
        fallback = payload.get("codexAuthFallback")
        self.assertIsInstance(fallback, dict)
        assert isinstance(fallback, dict)
        self.assertTrue(fallback.get("triggered"))
        snapshot = self.registry.load_run_snapshot(registry_root, run_id)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        snapshot_fallback = snapshot.get("codexAuthFallback")
        self.assertIsInstance(snapshot_fallback, dict)
        assert isinstance(snapshot_fallback, dict)
        self.assertTrue(snapshot_fallback.get("triggered"))

    def test_work_mode_dirty_baseline_quota_does_not_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        (Path(repo.name) / "dirty.txt").write_text("preexisting\n", encoding="utf-8")
        exit_code, payload, _registry_root, _run_id, _repo = self._run_with_fake_codex(
            script_body=(
                "#!/usr/bin/env bash\n"
                'if [ "${CODEX_HOME}" = "__WORK_HOME__" ]; then\n'
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "  exit 0\n"
                "fi\n"
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            ),
            mode="work",
            repo=repo,
        )
        self.assertEqual(exit_code, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertNotIn("codexAuthFallback", payload)

    def test_work_mode_mutation_before_quota_does_not_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        exit_code, payload, _registry_root, _run_id, repo = self._run_with_fake_codex(
            script_body=(
                "#!/usr/bin/env bash\n"
                'touch "$PWD/mutated.txt"\n'
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            ),
            mode="work",
            repo=repo,
        )
        self.assertEqual(exit_code, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertNotIn("codexAuthFallback", payload)

    def test_work_mode_commit_before_quota_does_not_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        exit_code, payload, _registry_root, _run_id, repo = self._run_with_fake_codex(
            script_body=(
                "#!/usr/bin/env bash\n"
                'if [ "${CODEX_HOME}" = "__WORK_HOME__" ]; then\n'
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "  exit 0\n"
                "fi\n"
                'echo "committed by primary" > committed.txt\n'
                "git add committed.txt\n"
                'git commit -m "primary codex commit"\n'
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            ),
            mode="work",
            repo=repo,
        )
        self.assertEqual(exit_code, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertNotIn("codexAuthFallback", payload)

    def test_generic_failure_does_not_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        exit_code, payload, _registry_root, _run_id, repo = self._run_with_fake_codex(
            script_body=('#!/usr/bin/env bash\necho "unexpected failure" >&2\nexit 1\n'),
            repo=repo,
        )
        self.assertEqual(exit_code, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertNotIn("codexAuthFallback", payload)

    def test_quota_after_tool_event_does_not_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        exit_code, payload, _registry_root, _run_id, repo = self._run_with_fake_codex(
            script_body=(
                "#!/usr/bin/env bash\n"
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"command_execution","command":"touch x"}}\'\n'
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            ),
            repo=repo,
        )
        self.assertEqual(exit_code, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertNotIn("codexAuthFallback", payload)

    def test_classifier_ignores_bare_429_without_context(self):
        self.assertFalse(self.codex_auth.classify_codex_usage_limit("HTTP 429 from upstream"))

    def test_dry_run_shows_auth_profile_not_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            config = config_mod.embedded_default_config()
            config["codex"] = dict(config["codex"])
            config["codex"]["authProfile"] = "personal"
            config["codex"]["authProfiles"] = {"personal": {"codexHome": personal}}
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(tmp, "directory"),
                "review",
                config,
                dry_run=True,
            )
            payload = self.delegate.dry_run_payload(request)
            self.assertEqual(payload.get("authProfile"), "personal")
            self.assertNotIn("codexHome", payload)


class CodexAuthPlumbingTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(MODULE_PATH, "delegate_codex_auth_plumbing_cli")
        self.runner = load_module(RUNNER_PATH, "delegate_codex_auth_plumbing_runner")

    def test_passthrough_codex_honors_codex_home_without_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            work = write_codex_home(Path(tmp) / "work")
            attempts_file = Path(tmp) / "attempts.txt"
            env_file = Path(tmp) / "env.txt"
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "${{CODEX_HOME:-}}" > "{env_file}"\n'
                f'echo 1 >> "{attempts_file}"\n'
                "exit 1\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            config = config_mod.embedded_default_config()
            config["codex"] = dict(config["codex"])
            config["codex"]["binary"] = str(fake_bin)
            config["codex"]["authProfile"] = "personal"
            config["codex"]["fallbackAuthProfile"] = "work"
            config["codex"]["authProfiles"] = {
                "personal": {"codexHome": personal},
                "work": {"codexHome": work},
            }
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(repo.name, "git"),
                "review",
                config,
                dry_run=False,
            )
            exit_code, _payload = self.delegate.execute_request(
                request,
                json_mode=False,
                config=config,
                pass_through=True,
                completion_report_mode="none",
                source_workspace=self.delegate.ResolvedWorkspace(repo.name, "git"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(env_file.read_text(encoding="utf-8").strip(), personal)
            self.assertEqual(attempts_file.read_text(encoding="utf-8").strip(), "1")

    def test_safe_isolation_codex_preserves_codex_home(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp) / "personal")
            env_file = Path(tmp) / "env.txt"
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "${{CODEX_HOME:-}}" > "{env_file}"\n'
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "exit 0\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            config = config_mod.embedded_default_config()
            config["codex"] = dict(config["codex"])
            config["codex"]["binary"] = str(fake_bin)
            config["codex"]["authProfile"] = "personal"
            config["codex"]["authProfiles"] = {"personal": {"codexHome": personal}}
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(repo.name, "git"),
                "review",
                config,
                dry_run=False,
            )
            exit_code, _payload = self.delegate.execute_request(
                request,
                json_mode=False,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=self.delegate.ResolvedWorkspace(repo.name, "git"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(env_file.read_text(encoding="utf-8").strip(), personal)


if __name__ == "__main__":
    unittest.main()
