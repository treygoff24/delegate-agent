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

from delegate_agent import config as config_mod  # noqa: E402
from delegate_agent import redaction  # noqa: E402
from delegate_agent.errors import DelegateError  # noqa: E402

MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
RUNNER_PATH = ROOT / "src" / "delegate_agent" / "runner.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"


def load_module(path: Path, name: str):
    import importlib.util

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


def write_codex_home(root: Path, name: str = "codex-home") -> str:
    home = root / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
    (home / "config.toml").write_text("[profile.delegate]\n", encoding="utf-8")
    return str(home)


def base_config(*, personal: str | None = None, work: str | None = None) -> dict:
    config = config_mod.embedded_default_config()
    definitions = {}
    if personal is not None:
        definitions["personal"] = {"env": {"CODEX_HOME": personal}}
    if work is not None:
        definitions["work"] = {"env": {"CODEX_HOME": work}}
    config["profiles"] = {
        "detectFrom": ["DELEGATE_PROFILE", "AI_PROFILE"],
        "default": None,
        "definitions": definitions,
    }
    return config


class ProfileResolutionTests(unittest.TestCase):
    def setUp(self):
        self.profiles = load_module(
            ROOT / "src" / "delegate_agent" / "profiles.py", "profiles_under_test"
        )

    def test_resolution_precedence_flag_detect_default_none(self):
        config = base_config(personal="/tmp/personal", work="/tmp/work")
        config["profiles"]["default"] = "work"

        resolved = self.profiles.resolve_active_profile(
            config, {"DELEGATE_PROFILE": "work"}, cli_override="personal"
        )
        self.assertEqual(resolved.name, "personal")
        self.assertEqual(resolved.source, "flag")

        resolved = self.profiles.resolve_active_profile(config, {"AI_PROFILE": "personal"})
        self.assertEqual(resolved.name, "personal")
        self.assertEqual(resolved.source, "AI_PROFILE")

        resolved = self.profiles.resolve_active_profile(config, {})
        self.assertEqual(resolved.name, "work")
        self.assertEqual(resolved.source, "default")

        config["profiles"]["default"] = None
        resolved = self.profiles.resolve_active_profile(config, {})
        self.assertIsNone(resolved.name)
        self.assertIsNone(resolved.source)
        self.assertEqual(resolved.env, {})

    def test_unknown_explicit_override_hard_errors(self):
        config = base_config(work="/tmp/work")
        with self.assertRaises(DelegateError) as ctx:
            self.profiles.resolve_active_profile(config, {}, cli_override="missing")
        self.assertEqual(ctx.exception.error, "unknown_profile")

    def test_unknown_ambient_warns_and_falls_to_default(self):
        config = base_config(work="/tmp/work")
        config["profiles"]["default"] = "work"
        resolved = self.profiles.resolve_active_profile(config, {"AI_PROFILE": "staging"})
        self.assertEqual(resolved.name, "work")
        self.assertEqual(resolved.source, "default")
        self.assertEqual(len(resolved.warnings), 1)
        self.assertIn("AI_PROFILE=staging", resolved.warnings[0])

    def test_detect_mismatch_warns_once_and_locks_first_defined_profile(self):
        config = base_config(personal="/tmp/personal", work="/tmp/work")
        resolved = self.profiles.resolve_active_profile(
            config, {"DELEGATE_PROFILE": "work", "AI_PROFILE": "personal"}
        )
        self.assertEqual(resolved.name, "work")
        self.assertEqual(resolved.source, "DELEGATE_PROFILE")
        self.assertEqual(len(resolved.warnings), 1)
        self.assertIn("using work", resolved.warnings[0])

    def test_profile_env_expands_paths_and_overrides_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = base_config()
            config["profiles"]["definitions"]["work"] = {
                "env": {"CODEX_HOME": "$PROFILE_ROOT/codex", "OTHER": "~/other"}
            }
            with mock.patch.dict(os.environ, {"PROFILE_ROOT": tmp, "HOME": tmp}, clear=False):
                env = self.profiles.profile_env(config, "work")
                self.assertEqual(env["CODEX_HOME"], str(Path(tmp) / "codex"))
                self.assertEqual(env["OTHER"], str(Path(tmp) / "other"))
                child = self.profiles.child_environment(
                    base={"CODEX_HOME": "inherited"}, overrides=env
                )
                self.assertEqual(child["CODEX_HOME"], str(Path(tmp) / "codex"))


class ProfileConfigAndRedactionTests(unittest.TestCase):
    def test_default_config_still_validates(self):
        config_mod.validate_config(config_mod.embedded_default_config())

    def test_profiles_validator_is_wired(self):
        config = config_mod.deep_merge(
            config_mod.embedded_default_config(), {"profiles": {"detectFrom": "AI_PROFILE"}}
        )
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_profiles_config")

    def test_secret_keys_in_profile_env_are_rejected(self):
        for key in ("CURSOR_API_KEY", "ACCESS_TOKEN", "DB_PASSWORD", "PRIVATE_KEY"):
            with self.subTest(key=key):
                config = config_mod.deep_merge(
                    config_mod.embedded_default_config(),
                    {
                        "profiles": {
                            "detectFrom": [],
                            "default": None,
                            "definitions": {"work": {"env": {key: "secret"}}},
                        }
                    },
                )
                with self.assertRaises(config_mod.ConfigError) as ctx:
                    config_mod.validate_config(config)
                self.assertEqual(ctx.exception.error, "secret_in_profile_env")

    def test_key_aware_redaction_masks_secret_keyed_values(self):
        redacted = redaction.redact_env_map(
            {"OPENAI_API_KEY": "sk-proj-123456789", "CODEX_HOME": "/tmp/codex"}
        )
        self.assertEqual(redacted["OPENAI_API_KEY"], "***")
        self.assertEqual(redacted["CODEX_HOME"], "/tmp/codex")

    def test_fallback_profile_without_codex_home_is_config_error(self):
        config = config_mod.deep_merge(
            config_mod.embedded_default_config(),
            {
                "codex": {"fallbackProfile": "work"},
                "profiles": {
                    "detectFrom": [],
                    "default": None,
                    "definitions": {"work": {"env": {"OTHER": "/tmp"}}},
                },
            },
        )
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.validate_config(config)
        self.assertEqual(ctx.exception.error, "profile_missing_codex_home")


class CodexProfileExecutionTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(MODULE_PATH, "delegate_profiles_cli")
        self.runner = load_module(RUNNER_PATH, "delegate_profiles_runner")
        self.registry = load_module(REGISTRY_PATH, "delegate_profiles_registry")
        self.profiles = load_module(
            ROOT / "src" / "delegate_agent" / "profiles.py", "profiles_execution_under_test"
        )

    def test_codex_active_profile_requires_codex_home_before_launch(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            invoked = Path(tmp) / "invoked.txt"
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(f'#!/usr/bin/env bash\necho yes > "{invoked}"\n', encoding="utf-8")
            fake_bin.chmod(0o755)
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex": {"binary": str(fake_bin)},
                        "profiles": {
                            "detectFrom": ["AI_PROFILE"],
                            "default": None,
                            "definitions": {"work": {"env": {"OTHER": "/tmp"}}},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"DELEGATE_CONFIG": str(config_path), "AI_PROFILE": "work"}, clear=False
            ):
                exit_code = self.delegate.main(
                    ["--cwd", repo.name, "codex", "safe", "task"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 2)
            self.assertFalse(invoked.exists())

    def test_codex_preflight_fails_on_unreadable_resolved_auth_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "empty"
            home.mkdir()
            config = base_config(work=str(home))
            config["profiles"]["default"] = "work"
            config["profiles"]["detectFrom"] = []
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(tmp, "directory"),
                "review",
                config,
                dry_run=False,
            )
            with self.assertRaises(DelegateError) as ctx:
                self.profiles.preflight_codex_request(request, config["codex"])
            self.assertEqual(ctx.exception.error, "codex_auth_unavailable")

    def test_fake_codex_sees_profile_codex_home(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            work = write_codex_home(Path(tmp), "work")
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
            config = base_config(work=work)
            config["codex"] = dict(config["codex"], binary=str(fake_bin))
            config["profiles"]["default"] = "work"
            config["profiles"]["detectFrom"] = []
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(repo.name, "git"),
                "review",
                config,
                dry_run=False,
            )
            self.assertEqual(request.env_overrides["CODEX_HOME"], work)
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
            self.assertEqual(env_file.read_text(encoding="utf-8").strip(), work)

    def test_usage_limit_retries_with_distinct_fallback_profile(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp), "personal")
            work = write_codex_home(Path(tmp), "work")
            attempts = Path(tmp) / "attempts.txt"
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "${{CODEX_HOME:-}}" >> "{attempts}"\n'
                f'if [ "${{CODEX_HOME}}" = "{work}" ]; then\n'
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "  exit 0\n"
                "fi\n"
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            config = base_config(personal=personal, work=work)
            config["codex"] = dict(config["codex"], binary=str(fake_bin), fallbackProfile="work")
            config["profiles"]["default"] = "personal"
            config["profiles"]["detectFrom"] = []
            request = self.delegate.build_request(
                "codex",
                "safe",
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
            )
            exit_code, payload = self.runner.execute_tracked(
                request.argv,
                repo.name,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                stdin_text="task",
            )
            self.assertEqual(exit_code, 0)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertIn("codexAuthFallback", payload)
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), [personal, work])

    def test_runtime_fallback_same_profile_is_noop(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            work = write_codex_home(Path(tmp), "work")
            attempts = Path(tmp) / "attempts.txt"
            fake_bin = Path(tmp) / "codex"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                f'echo attempt >> "{attempts}"\n'
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            config = base_config(work=work)
            config["codex"] = dict(config["codex"], binary=str(fake_bin), fallbackProfile="work")
            config["profiles"]["detectFrom"] = ["AI_PROFILE"]
            with mock.patch.dict(os.environ, {"AI_PROFILE": "work"}, clear=False):
                request = self.delegate.build_request(
                    "codex",
                    "safe",
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
            )
            self.assertEqual(ctx.fallback_env_overrides, {})
            exit_code, payload = self.runner.execute_tracked(
                request.argv,
                repo.name,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                stdin_text="task",
            )
            self.assertEqual(exit_code, 1)
            self.assertNotIn("codexAuthFallback", payload or {})
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), ["attempt"])

    def test_fallback_same_account_normalization_handles_home_vars_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = Path(write_codex_home(root / "accounts", "work"))
            link = root / "link-home"
            link.symlink_to(real, target_is_directory=True)
            cases = [
                (str(real), "~/accounts/work"),
                ("$PROFILE_ROOT/accounts/work", str(link)),
            ]
            for primary, fallback in cases:
                with self.subTest(primary=primary, fallback=fallback):
                    config = base_config()
                    config["codex"] = dict(config["codex"], fallbackProfile="personal")
                    config["profiles"] = {
                        "detectFrom": ["AI_PROFILE"],
                        "default": None,
                        "definitions": {
                            "work": {"env": {"CODEX_HOME": primary}},
                            "personal": {"env": {"CODEX_HOME": fallback}},
                        },
                    }
                    with mock.patch.dict(
                        os.environ,
                        {"AI_PROFILE": "work", "HOME": str(root), "PROFILE_ROOT": str(root)},
                        clear=False,
                    ):
                        resolution = self.profiles.resolve_active_profile(config, os.environ)
                    self.assertIsNone(self.profiles.codex_fallback_env_overrides(resolution))

    def test_profile_warning_is_carried_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_codex_home(Path(tmp), "work")
            personal = write_codex_home(Path(tmp), "personal")
            config = base_config(personal=personal, work=work)
            with mock.patch.dict(
                os.environ, {"DELEGATE_PROFILE": "work", "AI_PROFILE": "personal"}, clear=False
            ):
                request = self.delegate.build_request(
                    "codex",
                    "safe",
                    None,
                    self.delegate.ResolvedWorkspace(tmp, "directory"),
                    "review",
                    config,
                    dry_run=True,
                )
            warnings = [w for w in request.warnings if "profile mismatch" in w]
            self.assertEqual(len(warnings), 1)
            payload = self.delegate.dry_run_payload(request)
            payload_warnings = [w for w in payload.get("warnings", []) if "profile mismatch" in w]
            self.assertEqual(len(payload_warnings), 1)


if __name__ == "__main__":
    unittest.main()
