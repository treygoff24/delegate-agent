import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import config as config_mod  # noqa: E402
from delegate_agent import (  # noqa: E402
    redaction,
    run_metadata,
)
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


def write_json_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config), encoding="utf-8")


def make_pointer_config(
    *,
    cursor_binary: Path | None = None,
    work_pointer: str = "work-pointer",
    personal_pointer: str = "personal-pointer",
    worktree_data_home: str | None = None,
) -> dict:
    config = config_mod.embedded_default_config()
    if cursor_binary is not None:
        config["cursor"]["argvPrefix"] = [str(cursor_binary)]
    config["tracking"]["completionReport"]["defaultMode"] = "none"
    config["profiles"] = {
        "detectFrom": ["DELEGATE_PROFILE", "AI_PROFILE"],
        "default": None,
        "definitions": {
            "work": {"env": {"DELEGATE_POINTER": work_pointer}},
            "personal": {"env": {"DELEGATE_POINTER": personal_pointer}},
        },
    }
    if worktree_data_home is not None:
        config["worktrees"]["dataHome"] = worktree_data_home
    return config


def make_env_probe_binary(root: Path, name: str = "agent") -> Path:
    path = root / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        'if [ -n "${DELEGATE_ENV_OUT:-}" ]; then\n'
        '  printf "%s\\n" "${DELEGATE_POINTER:-}" >> "${DELEGATE_ENV_OUT}"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def main_json(delegate, argv: list[str], *, env: dict[str, str]) -> tuple[int, dict, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False):
        code = delegate.main(argv, stdout=stdout, stderr=stderr)
    text = stdout.getvalue()
    payload = json.loads(text) if text.strip() else {}
    return code, payload, stderr.getvalue()


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
        # The message must be actionable: list defined profiles and point at the
        # read-only introspection command.
        self.assertIn("work", ctx.exception.message)
        self.assertIn("delegate profiles", ctx.exception.message)

    def test_unknown_ambient_warns_and_falls_to_default(self):
        config = base_config(work="/tmp/work")
        config["profiles"]["default"] = "work"
        resolved = self.profiles.resolve_active_profile(config, {"AI_PROFILE": "staging"})
        self.assertEqual(resolved.name, "work")
        self.assertEqual(resolved.source, "default")
        self.assertEqual(len(resolved.warnings), 1)
        self.assertIn("AI_PROFILE=staging", resolved.warnings[0])

    def test_undefined_secret_keyed_detect_var_value_is_redacted_in_warning(self):
        # A secret-shaped detectFrom var naming an undefined profile must not leak its
        # value through the warning (which surfaces via `delegate profiles` / dry-run).
        config = base_config(work="/tmp/work")
        config["profiles"]["detectFrom"] = ["GITHUB_TOKEN", "AI_PROFILE"]
        config["profiles"]["default"] = "work"
        resolved = self.profiles.resolve_active_profile(
            config, {"GITHUB_TOKEN": "ghp_super_secret_value", "AI_PROFILE": ""}
        )
        self.assertEqual(resolved.name, "work")
        joined = " ".join(resolved.warnings)
        self.assertNotIn("ghp_super_secret_value", joined)
        self.assertIn("GITHUB_TOKEN=***", joined)

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

    def test_codex_failover_identity_tracks_credentials_not_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = write_codex_home(Path(tmp), "shared")
            config = base_config()
            config["profiles"] = {
                "detectFrom": ["DELEGATE_PROFILE"],
                "default": None,
                "definitions": {
                    "primary": {"env": {"CODEX_HOME": home}},
                    "renamed": {"env": {"CODEX_HOME": home}},
                },
            }

            primary = self.profiles.resolve_active_profile(config, {"DELEGATE_PROFILE": "primary"})
            renamed = self.profiles.resolve_active_profile(config, {"DELEGATE_PROFILE": "renamed"})

            self.assertEqual(primary.codex_failover_identity, renamed.codex_failover_identity)

    def test_codex_failover_identity_separates_config_profile_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = write_codex_home(Path(tmp), "shared")
            config = base_config(work=home)
            config["profiles"]["default"] = "work"

            default_overlay = self.profiles.resolve_active_profile(config, {})
            config["codex"] = dict(config["codex"], profile="delegate")
            named_overlay = self.profiles.resolve_active_profile(config, {})

            self.assertNotEqual(
                default_overlay.codex_failover_identity,
                named_overlay.codex_failover_identity,
            )


class InitiatorRootResolutionTests(unittest.TestCase):
    def test_initiator_root_resolution_prefers_existing_root_and_single_native_ids(self):
        self.assertEqual(
            run_metadata.resolve_initiator_root({"CODEX_THREAD_ID": "abc"}),
            "codex:abc",
        )
        self.assertEqual(
            run_metadata.resolve_initiator_root({"CLAUDE_CODE_SESSION_ID": "def"}),
            "claude:def",
        )
        self.assertEqual(
            run_metadata.resolve_initiator_root(
                {"DELEGATE_INITIATOR_ROOT": "codex:root", "CODEX_THREAD_ID": "child"}
            ),
            "codex:root",
        )
        self.assertIsNone(
            run_metadata.resolve_initiator_root(
                {"CODEX_THREAD_ID": "abc", "CLAUDE_CODE_SESSION_ID": "def"}
            )
        )

    def test_initiator_root_resolution_rejects_ambiguous_values(self):
        for env in (
            {"CODEX_THREAD_ID": ""},
            {"CODEX_THREAD_ID": "   "},
            {"CODEX_THREAD_ID": " padded "},
            {"CODEX_THREAD_ID": "x" * 257},
            {"CODEX_THREAD_ID": "has:colon"},
            {"CLAUDE_CODE_SESSION_ID": "has\nnewline"},
            {"DELEGATE_INITIATOR_ROOT": "codex:"},
            {"DELEGATE_INITIATOR_ROOT": "native:abc"},
        ):
            with self.subTest(env=env):
                self.assertIsNone(run_metadata.resolve_initiator_root(env))


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
        for key in (
            "CURSOR_API_KEY",
            "APIKEY",
            "ACCESS_TOKEN",
            "DB_PASSWORD",
            "DB_PASSWD",
            "PRIVATE_KEY",
        ):
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
        # Build the secret-shaped sample at runtime so no literal token lands in
        # the tree; keeps release-surface gitleaks scans clean (see .gitleaksignore).
        # Masking here is key-driven, so the value content is otherwise immaterial.
        openai_key = "sk-proj-" + "0123456789ab"
        redacted = redaction.redact_env_map(
            {
                "OPENAI_API_KEY": openai_key,
                "APIKEY": "plain-secret",
                "DB_PASSWD": "secret",
                "CODEX_HOME": "/tmp/codex",
            }
        )
        self.assertEqual(redacted["OPENAI_API_KEY"], "***")
        self.assertEqual(redacted["APIKEY"], "***")
        self.assertEqual(redacted["DB_PASSWD"], "***")
        self.assertEqual(redacted["CODEX_HOME"], "/tmp/codex")

    def test_merge_config_layer_replaces_profile_definitions_atomically(self):
        # Profile definitions replace atomically per name so a higher layer cannot
        # inherit stale env keys (for example credential routing pointers) from
        # lower layers.
        base = {
            "profiles": {
                "definitions": {
                    "work": {
                        "env": {
                            "CODEX_HOME": "/lower/codex",
                            "STALE_POINTER": "/should-not-survive",
                        }
                    }
                }
            }
        }
        override = {
            "profiles": {
                "definitions": {
                    "work": {
                        "env": {
                            "CODEX_HOME": "/higher/codex",
                        }
                    }
                }
            }
        }
        merged = config_mod.merge_config_layer(base, override)
        work_def = merged["profiles"]["definitions"]["work"]
        # The entire profile definition is replaced, not deep-merged env-by-env.
        self.assertEqual(work_def, override["profiles"]["definitions"]["work"])
        self.assertEqual(work_def["env"], {"CODEX_HOME": "/higher/codex"})
        self.assertNotIn("STALE_POINTER", work_def["env"])

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

    def _env_probe_binary(self, root: Path, log_path: Path) -> Path:
        fake = root / "agent"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "${{DELEGATE_INITIATOR_ROOT:-}}" > "{log_path}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    def _cursor_config(self, fake: Path) -> dict:
        config = dict(self.delegate.DEFAULT_CONFIG)
        config["cursor"] = {**config["cursor"], "argvPrefix": [str(fake)]}
        config["tracking"] = {
            **config["tracking"],
            "completionReport": {"defaultMode": "none"},
        }
        return config

    def test_tracked_work_persists_initiator_root_and_passes_child_env(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "initiator.txt"
            config = self._cursor_config(self._env_probe_binary(Path(tmp), log_path))
            workspace = self.delegate.ResolvedWorkspace(repo.name, "git")
            request = self.delegate.build_request(
                "cursor", "work", None, workspace, "task", config, dry_run=False
            )

            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1"}, clear=False):
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            registry_root = Path(repo.name) / ".delegate"
            index = self.registry.load_index(registry_root)
            run_id = next(iter(index["runs"]))
            self.assertEqual(index["runs"][run_id]["initiatorRoot"], "codex:thread-1")
            self.assertEqual(log_path.read_text(encoding="utf-8").strip(), "codex:thread-1")

    def test_grouped_call_persists_existing_initiator_root(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "initiator.txt"
            config = self._cursor_config(self._env_probe_binary(Path(tmp), log_path))
            workspace = self.delegate.ResolvedWorkspace(repo.name, "git")
            request = self.delegate.build_request(
                "cursor",
                "call",
                None,
                workspace,
                "task",
                config,
                dry_run=False,
                group="swarm",
            )

            with mock.patch.dict(
                os.environ,
                {"DELEGATE_INITIATOR_ROOT": "claude:root-session", "CODEX_THREAD_ID": "child"},
                clear=False,
            ):
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            registry_root = Path(repo.name) / ".delegate"
            index = self.registry.load_index(registry_root)
            run_id = next(iter(index["runs"]))
            self.assertEqual(index["runs"][run_id]["initiatorRoot"], "claude:root-session")
            self.assertEqual(log_path.read_text(encoding="utf-8").strip(), "claude:root-session")

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

    def test_relative_codex_home_is_rejected_before_launch(self):
        # A relative CODEX_HOME resolves against different cwds for preflight vs the
        # spawned child, so it must fail closed before launch rather than risk the
        # wrong account.
        with tempfile.TemporaryDirectory() as tmp:
            config = base_config(work="relative/codex/home")
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
            self.assertEqual(ctx.exception.error, "codex_home_not_absolute")

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
                "  printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
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

    def test_usage_limit_on_stdout_events_retries_with_fallback_profile(self):
        # Regression (2026-07-11): the real codex --json harness reports quota
        # exhaustion as stdout events with a clean stderr; the failover
        # classifier must see the stdout-borne message too.
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
                "  printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
                "  exit 0\n"
                "fi\n"
                "printf '%s\\n' '{\"type\":\"turn.started\"}'\n"
                "printf '%s\\n' "
                '\'{"type":"error","message":"You have hit your usage limit. Try again at 3:48 PM."}\'\n'
                "printf '%s\\n' "
                '\'{"type":"turn.failed","error":{"message":"You have hit your usage limit. Try again at 3:48 PM."}}\'\n'
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
            self.delegate._set_child_root_env(
                request,
                self.delegate.ResolvedWorkspace(repo.name, "git"),
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
            self.assertIn("DELEGATE_SOURCE_ROOT", ctx.env_overrides)
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

    def test_fallback_env_overrides_preserve_non_codex_home_pointers(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp), "personal")
            work = write_codex_home(Path(tmp), "work")
            config = base_config(personal=personal, work=work)
            config["codex"] = dict(config["codex"], fallbackProfile="work")
            config["profiles"]["definitions"]["personal"]["env"]["CODEX_PROXY_URL"] = (
                "http://work-proxy"
            )
            config["profiles"]["default"] = "personal"
            config["profiles"]["detectFrom"] = []
            resolution = self.profiles.resolve_active_profile(config, {})
            overrides = self.profiles.codex_fallback_env_overrides(resolution)
            self.assertIsNotNone(overrides)
            assert overrides is not None
            self.assertEqual(overrides["CODEX_HOME"], work)
            self.assertEqual(overrides["CODEX_PROXY_URL"], "http://work-proxy")

    def test_child_environment_drops_ambient_delegate_roots(self):
        with mock.patch.dict(
            os.environ,
            {
                "DELEGATE_SOURCE_ROOT": "/outer/source",
                "DELEGATE_EXECUTION_ROOT": "/outer/run",
                "WORKSPACE_ROOT": "/outer/workspace",
            },
            clear=False,
        ):
            env = self.profiles.child_environment()

        self.assertNotIn("DELEGATE_SOURCE_ROOT", env)
        self.assertNotIn("DELEGATE_EXECUTION_ROOT", env)
        self.assertNotIn("WORKSPACE_ROOT", env)

    def test_fallback_environment_keeps_authoritative_delegate_roots(self):
        resolution = self.profiles.ProfileResolution(
            name="personal",
            source="default",
            env={
                "CODEX_HOME": "/personal",
                "DELEGATE_SOURCE_ROOT": "/profile/source",
                "DELEGATE_EXECUTION_ROOT": "/profile/run",
                "WORKSPACE_ROOT": "/profile/workspace",
            },
            codex_home="/personal",
            codex_fallback_home="/work",
        )
        with mock.patch.object(self.profiles, "codex_homes_same_account", return_value=False):
            overrides = self.profiles.codex_fallback_child_env_overrides(
                resolution,
                {
                    "DELEGATE_SOURCE_ROOT": "/actual/source",
                    "DELEGATE_EXECUTION_ROOT": "/actual/run",
                    "WORKSPACE_ROOT": "/actual/workspace",
                },
            )

        self.assertEqual(overrides["CODEX_HOME"], "/work")
        self.assertEqual(overrides["DELEGATE_SOURCE_ROOT"], "/actual/source")
        self.assertEqual(overrides["DELEGATE_EXECUTION_ROOT"], "/actual/run")
        self.assertEqual(overrides["WORKSPACE_ROOT"], "/actual/workspace")

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

    def _run_codex_fallback_scenario(
        self,
        *,
        mode: str,
        fake_script: str,
        tmp: Path,
        repo,
        config: dict,
        env: dict[str, str] | None = None,
    ) -> tuple[int, dict | None]:
        fake_bin = tmp / "codex"
        fake_bin.write_text(fake_script, encoding="utf-8")
        fake_bin.chmod(0o755)
        config = dict(config)
        config["codex"] = dict(config["codex"], binary=str(fake_bin))
        registry_root = self.registry.ensure_registry(Path(repo.name), workspace_kind="git")
        build_env = dict(env or {})
        with mock.patch.dict(os.environ, build_env, clear=False):
            request = self.delegate.build_request(
                "codex",
                mode,
                None,
                self.delegate.ResolvedWorkspace(repo.name, "git"),
                "task",
                config,
                dry_run=False,
            )
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
        return exit_code, payload

    def test_directory_baseline_detects_workspace_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = self.profiles.capture_workspace_baseline(tmp)

            self.assertIsNotNone(baseline)
            self.assertTrue(self.profiles.work_mode_safe_for_codex_fallback(tmp, baseline))
            Path(tmp, "changed.txt").write_text("changed\n", encoding="utf-8")
            self.assertFalse(self.profiles.work_mode_safe_for_codex_fallback(tmp, baseline))

    def test_directory_baseline_stops_scandir_before_exhausting_a_large_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            consumed = 0
            limit = self.profiles.DIRECTORY_BASELINE_MAX_ENTRIES

            class Entry:
                def __init__(self, index: int) -> None:
                    self.name = f"entry-{index}"
                    self.path = str(root / self.name)

                def stat(self, *, follow_symlinks: bool = False):
                    return root.stat()

                def is_dir(self, *, follow_symlinks: bool = False) -> bool:
                    return False

            class Directory:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def __iter__(self):
                    nonlocal consumed
                    for index in range(limit + 100):
                        consumed += 1
                        yield Entry(index)

            with mock.patch.object(self.profiles.os, "scandir", return_value=Directory()):
                self.assertIsNone(self.profiles._capture_directory_baseline(tmp))
            self.assertEqual(consumed, limit + 1)

    def test_unborn_head_git_workspace_fails_closed_instead_of_directory_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True, text=True)
            Path(tmp, "staged.txt").write_text("dirty\n", encoding="utf-8")
            # git repo, no commits: porcelain works, HEAD is unborn. The weaker
            # directory baseline must not be used — no baseline, no retry.
            self.assertIsNone(self.profiles.capture_workspace_baseline(tmp))
            self.assertFalse(self.profiles.work_mode_safe_for_codex_fallback(tmp, None))

    def test_work_mode_clean_unchanged_baseline_retries_with_fallback(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp), "personal")
            work = write_codex_home(Path(tmp), "work")
            attempts = Path(tmp) / "attempts.txt"
            fake_script = (
                "#!/usr/bin/env bash\n"
                f'echo "${{CODEX_HOME:-}}" >> "{attempts}"\n'
                f'if [ "${{CODEX_HOME}}" = "{work}" ]; then\n'
                '  printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "  exit 0\n"
                "fi\n"
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            )
            config = base_config(personal=personal, work=work)
            config["codex"] = dict(config["codex"], fallbackProfile="work")
            config["profiles"]["default"] = "personal"
            config["profiles"]["detectFrom"] = []
            exit_code, payload = self._run_codex_fallback_scenario(
                mode="work",
                fake_script=fake_script,
                tmp=Path(tmp),
                repo=repo,
                config=config,
            )
            self.assertEqual(exit_code, 0)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertIn("codexAuthFallback", payload)
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), [personal, work])

    def test_tracked_work_fallback_reuses_registered_mail_identity(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            personal = write_codex_home(root, "personal")
            work = write_codex_home(root, "work")
            observations = root / "child-identities.tsv"
            fake_bin = root / "codex"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                'printf "%s\\t%s\\t%s\\n" "${CODEX_HOME:-}" "${DELEGATE_RUN_ID:-}" '
                f'"${{DELEGATE_MAIL_SELF:-}}" >> "{observations}"\n'
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
            config["profiles"]["definitions"]["personal"]["env"].update(
                {
                    "DELEGATE_RUN_ID": "del_20260801T120000Z_abcdef",
                    "DELEGATE_MAIL_SELF": "codex-99",
                }
            )
            workspace = self.delegate.ResolvedWorkspace(repo.name, "git")
            request = self.delegate.build_request(
                "codex", "work", None, workspace, "task", config, dry_run=False
            )

            exit_code, payload = self.delegate.execute_request(
                request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertIn("codexAuthFallback", payload or {})
            manifests = list((Path(repo.name) / ".delegate" / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            observed = [
                line.split("\t") for line in observations.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row[0] for row in observed], [personal, work])
            self.assertEqual([row[1] for row in observed], [manifest["runId"], manifest["runId"]])
            self.assertEqual([row[2] for row in observed], [manifest["alias"], manifest["alias"]])
            self.assertNotEqual(manifest["runId"], "del_20260801T120000Z_abcdef")
            self.assertNotEqual(manifest["alias"], "codex-99")

    def test_work_mode_dirty_baseline_skips_fallback_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp), "personal")
            work = write_codex_home(Path(tmp), "work")
            attempts = Path(tmp) / "attempts.txt"
            dirty_marker = Path(repo.name) / "dirty.txt"
            fake_script = (
                "#!/usr/bin/env bash\n"
                f'echo "${{CODEX_HOME:-}}" >> "{attempts}"\n'
                f'echo dirty > "{dirty_marker}"\n'
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            )
            config = base_config(personal=personal, work=work)
            config["codex"] = dict(config["codex"], fallbackProfile="work")
            config["profiles"]["default"] = "personal"
            config["profiles"]["detectFrom"] = []
            exit_code, payload = self._run_codex_fallback_scenario(
                mode="work",
                fake_script=fake_script,
                tmp=Path(tmp),
                repo=repo,
                config=config,
            )
            self.assertEqual(exit_code, 1)
            self.assertNotIn("codexAuthFallback", payload or {})
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), [personal])

    def test_tool_events_suppress_codex_fallback_retry(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            personal = write_codex_home(Path(tmp), "personal")
            work = write_codex_home(Path(tmp), "work")
            attempts = Path(tmp) / "attempts.txt"
            fake_script = (
                "#!/usr/bin/env bash\n"
                f'echo "${{CODEX_HOME:-}}" >> "{attempts}"\n'
                'printf \'%s\\n\' \'{"type":"item.started","item":{"type":"command_execution","command":"echo hi"}}\'\n'
                'echo "You exceeded your current quota usage limit" >&2\n'
                "exit 1\n"
            )
            config = base_config(personal=personal, work=work)
            config["codex"] = dict(config["codex"], fallbackProfile="work")
            config["profiles"]["default"] = "personal"
            config["profiles"]["detectFrom"] = []
            exit_code, payload = self._run_codex_fallback_scenario(
                mode="safe",
                fake_script=fake_script,
                tmp=Path(tmp),
                repo=repo,
                config=config,
            )
            self.assertEqual(exit_code, 1)
            self.assertNotIn("codexAuthFallback", payload or {})
            self.assertEqual(attempts.read_text(encoding="utf-8").splitlines(), [personal])

    def test_dry_run_includes_redacted_profile_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_codex_home(Path(tmp), "work")
            config = base_config(work=work)
            config["profiles"]["default"] = "work"
            config["profiles"]["detectFrom"] = []
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(tmp, "directory"),
                "review",
                config,
                dry_run=True,
            )
            request = replace(
                request,
                profile_resolution=replace(
                    request.profile_resolution,
                    env={
                        "CODEX_HOME": work,
                        "OPENAI_API_KEY": "sk-test-secret",
                    },
                ),
            )
            payload = self.delegate.dry_run_payload(request)
            self.assertIn("profileEnv", payload)
            self.assertEqual(payload["profileEnv"]["CODEX_HOME"], work)
            self.assertEqual(payload["profileEnv"]["OPENAI_API_KEY"], "***")

    def test_no_active_profile_omits_fallback_profile_from_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_codex_home(Path(tmp), "work")
            config = base_config(work=work)
            config["codex"] = dict(config["codex"], fallbackProfile="work")
            config["profiles"]["default"] = None
            config["profiles"]["detectFrom"] = []
            request = self.delegate.build_request(
                "codex",
                "safe",
                None,
                self.delegate.ResolvedWorkspace(tmp, "directory"),
                "review",
                config,
                dry_run=True,
            )
            self.assertIsNone(request.auth_profile)
            self.assertIsNone(request.fallback_auth_profile)
            payload = self.delegate.dry_run_payload(request)
            self.assertNotIn("fallbackProfile", payload)


class CodexUsageLimitClassifierTests(unittest.TestCase):
    def setUp(self):
        self.profiles = load_module(
            ROOT / "src" / "delegate_agent" / "profiles.py", "profiles_classifier_under_test"
        )

    def test_classify_codex_usage_limit_negative_cases(self):
        self.assertFalse(self.profiles.classify_codex_usage_limit(""))
        self.assertFalse(self.profiles.classify_codex_usage_limit("   "))
        self.assertFalse(self.profiles.classify_codex_usage_limit("command failed: not found"))
        self.assertFalse(self.profiles.classify_codex_usage_limit("rate limit exceeded, try again"))
        self.assertFalse(
            self.profiles.classify_codex_usage_limit(
                "Server is temporarily limiting requests (not your usage limit)"
            )
        )

    def test_real_limit_survives_transient_guard_on_another_line(self):
        self.assertTrue(
            self.profiles.classify_codex_usage_limit(
                "warning (not your usage limit)\nYou've hit your usage limit"
            )
        )

    def test_classify_codex_usage_limit_positive_cases(self):
        self.assertTrue(
            self.profiles.classify_codex_usage_limit("You exceeded your current quota usage limit")
        )
        self.assertTrue(
            self.profiles.classify_codex_usage_limit("rate limit exceeded for your account")
        )
        self.assertTrue(self.profiles.classify_codex_usage_limit("rate limit exceeded for quota"))

    def test_read_bounded_stderr_tail_does_not_read_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr_log = Path(tmp) / "stderr.log"
            stderr_log.write_bytes(b"prefix-" + (b"x" * 32) + b"tail-end")

            with mock.patch.object(
                Path, "read_bytes", side_effect=AssertionError("unbounded read")
            ):
                self.assertEqual(
                    self.profiles.read_bounded_stderr_tail(stderr_log, limit=8),
                    "tail-end",
                )


class ProfilePhase2CliTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(MODULE_PATH, "delegate_profiles_phase2_cli")

    def test_auth_profile_override_reaches_tracked_cursor_child(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_out = root / "env.txt"
            config_path = root / "config.json"
            config = make_pointer_config(cursor_binary=make_env_probe_binary(root))
            write_json_config(config_path, config)
            code, payload, _stderr = main_json(
                self.delegate,
                [
                    "--json",
                    "--auth-profile",
                    "work",
                    "--cwd",
                    repo.name,
                    "cursor",
                    "work",
                    "task",
                ],
                env={
                    "DELEGATE_CONFIG": str(config_path),
                    "DELEGATE_ENV_OUT": str(env_out),
                    "DELEGATE_PROFILE": "",
                    "AI_PROFILE": "personal",
                },
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["authProfile"], "work")
            self.assertEqual(env_out.read_text(encoding="utf-8").splitlines(), ["work-pointer"])

    def test_auth_profile_override_reaches_passthrough_cursor_child(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_out = root / "env.txt"
            config_path = root / "config.json"
            config = make_pointer_config(cursor_binary=make_env_probe_binary(root))
            write_json_config(config_path, config)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "DELEGATE_CONFIG": str(config_path),
                    "DELEGATE_ENV_OUT": str(env_out),
                    "DELEGATE_PROFILE": "",
                    "AI_PROFILE": "personal",
                },
                clear=False,
            ):
                code = self.delegate.main(
                    [
                        "--auth-profile",
                        "work",
                        "--pass-through",
                        "--cwd",
                        repo.name,
                        "cursor",
                        "work",
                        "task",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )
            self.assertEqual(code, 0)
            self.assertEqual(env_out.read_text(encoding="utf-8").splitlines(), ["work-pointer"])

    def test_auth_profile_override_reaches_safe_isolated_cursor_child(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_out = root / "env.txt"
            config_path = root / "config.json"
            config = make_pointer_config(cursor_binary=make_env_probe_binary(root))
            write_json_config(config_path, config)
            code, payload, _stderr = main_json(
                self.delegate,
                [
                    "--json",
                    "--auth-profile",
                    "work",
                    "--cwd",
                    repo.name,
                    "cursor",
                    "safe",
                    "review",
                ],
                env={
                    "DELEGATE_CONFIG": str(config_path),
                    "DELEGATE_ENV_OUT": str(env_out),
                    "DELEGATE_PROFILE": "",
                    "AI_PROFILE": "personal",
                },
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["authProfile"], "work")
            self.assertTrue(payload["isolatedWorkspace"])
            self.assertEqual(
                env_out.read_text(encoding="utf-8").splitlines(),
                ["work-pointer", "work-pointer"],
            )
            self.assertEqual(payload["emptyRetry"], {"attempted": True, "resolved": False})

    def test_auth_profile_override_reaches_persistent_worktree_cursor_child(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_out = root / "env.txt"
            config_path = root / "config.json"
            config = make_pointer_config(
                cursor_binary=make_env_probe_binary(root),
                worktree_data_home=str(root / "worktrees"),
            )
            write_json_config(config_path, config)
            code, payload, _stderr = main_json(
                self.delegate,
                [
                    "--json",
                    "--auth-profile",
                    "work",
                    "--isolation",
                    "worktree",
                    "--cwd",
                    repo.name,
                    "cursor",
                    "work",
                    "task",
                ],
                env={
                    "DELEGATE_CONFIG": str(config_path),
                    "DELEGATE_ENV_OUT": str(env_out),
                    "DELEGATE_PROFILE": "",
                    "AI_PROFILE": "personal",
                },
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["authProfile"], "work")
            self.assertEqual(payload["isolationLifecycle"], "persistent")
            self.assertEqual(env_out.read_text(encoding="utf-8").splitlines(), ["work-pointer"])

    def test_auth_profile_override_applies_to_dry_run_and_run_input_json(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_out = root / "env.txt"
            config_path = root / "config.json"
            config = make_pointer_config(cursor_binary=make_env_probe_binary(root))
            write_json_config(config_path, config)
            env = {
                "DELEGATE_CONFIG": str(config_path),
                "DELEGATE_ENV_OUT": str(env_out),
                "DELEGATE_PROFILE": "",
                "AI_PROFILE": "personal",
            }
            dry_code, dry_payload, _stderr = main_json(
                self.delegate,
                [
                    "--json",
                    "--auth-profile",
                    "work",
                    "--cwd",
                    repo.name,
                    "dry-run",
                    "cursor",
                    "work",
                    "task",
                ],
                env=env,
            )
            self.assertEqual(dry_code, 0)
            self.assertEqual(dry_payload["authProfile"], "work")

            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(
                    {
                        "engine": "cursor",
                        "mode": "work",
                        "cwd": repo.name,
                        "prompt": "task",
                    }
                ),
                encoding="utf-8",
            )
            run_code, run_payload, _stderr = main_json(
                self.delegate,
                ["--json", "--auth-profile", "work", "run", "--input-json", str(input_path)],
                env=env,
            )
            self.assertEqual(run_code, 0)
            self.assertEqual(run_payload["authProfile"], "work")
            self.assertEqual(env_out.read_text(encoding="utf-8").splitlines(), ["work-pointer"])

    def test_auth_profile_rejected_for_inspection_and_worktree_commands(self):
        for argv in (
            ["--json", "--auth-profile", "work", "snapshot", "cursor"],
            ["--json", "--auth-profile", "work", "runs"],
            ["--json", "--auth-profile", "work", "run-output", "cursor"],
            ["--json", "--auth-profile", "work", "worktree", "list"],
        ):
            with self.subTest(argv=argv):
                code, payload, _stderr = main_json(self.delegate, argv, env={})
                self.assertEqual(code, 2)
                self.assertEqual(payload["error"], "invalid_option_combination")
                self.assertIn("--auth-profile", payload["message"])

    def test_profiles_command_json_shape_and_secret_failure_redacts_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config = make_pointer_config()
            write_json_config(config_path, config)
            code, payload, _stderr = main_json(
                self.delegate,
                ["--json", "--auth-profile", "work", "profiles"],
                env={"DELEGATE_CONFIG": str(config_path), "AI_PROFILE": "personal"},
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                set(payload),
                {"ok", "profile", "source", "envKeys", "env", "warnings", "configSource"},
            )
            self.assertEqual(payload["profile"], "work")
            self.assertEqual(payload["source"], "flag")
            self.assertEqual(payload["envKeys"], ["DELEGATE_POINTER"])
            self.assertEqual(payload["env"], {"DELEGATE_POINTER": "work-pointer"})

            secret_config = make_pointer_config()
            secret_config["profiles"]["definitions"]["work"]["env"] = {
                "OPENAI_API_KEY": "sk-test-secret"
            }
            write_json_config(config_path, secret_config)
            code, payload, stderr = main_json(
                self.delegate,
                ["--json", "profiles"],
                env={"DELEGATE_CONFIG": str(config_path), "AI_PROFILE": "work"},
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "secret_in_profile_env")
            self.assertNotIn("sk-test-secret", json.dumps(payload))
            self.assertNotIn("sk-test-secret", stderr)

    def test_describe_includes_profiles_and_auth_profile_global_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config = make_pointer_config()
            config["profiles"]["default"] = "personal"
            write_json_config(config_path, config)
            code, payload, _stderr = main_json(
                self.delegate,
                ["--json", "describe"],
                env={"DELEGATE_CONFIG": str(config_path)},
            )
            self.assertEqual(code, 0)
            self.assertIn("--auth-profile", payload["globalOptions"])
            self.assertEqual(payload["profiles"]["detectFrom"], ["DELEGATE_PROFILE", "AI_PROFILE"])
            self.assertEqual(payload["profiles"]["default"], "personal")
            self.assertEqual(payload["profiles"]["definedProfiles"], ["personal", "work"])
            commands = {entry["command"] for entry in payload["commands"]}
            self.assertIn("profiles", commands)
            self.assertNotIn("codex-auth", commands)

    def test_profile_warning_appears_once_end_to_end_with_codex_preflight(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = write_codex_home(root, "work")
            personal = write_codex_home(root, "personal")
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                "exit 0\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            config = base_config(personal=personal, work=work)
            config["codex"] = dict(config["codex"], binary=str(fake_codex))
            config_path = root / "config.json"
            write_json_config(config_path, config)
            code, payload, _stderr = main_json(
                self.delegate,
                ["--json", "--cwd", repo.name, "codex", "safe", "task"],
                env={
                    "DELEGATE_CONFIG": str(config_path),
                    "DELEGATE_PROFILE": "work",
                    "AI_PROFILE": "personal",
                },
            )
            self.assertEqual(code, 0)
            warnings = [w for w in payload.get("warnings", []) if "profile mismatch" in w]
            self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
