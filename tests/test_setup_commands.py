from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import cli, harness_discovery, model_discovery, setup_commands  # noqa: E402
from delegate_agent.constants import KNOWN_ENGINES  # noqa: E402


def _record(
    *,
    installed: bool,
    selector: list[str] | None = None,
    status: str | None = None,
    version: str | None = None,
    models: dict[str, object] | None = None,
    default_model: str | None = None,
    harness_reasoning: dict[str, object] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    return {
        "installed": installed,
        "selector": selector or [],
        "version": version,
        "probeStatus": status or ("ok" if installed else "missing"),
        "modelScope": "account" if installed else "unknown",
        "defaultModel": default_model,
        "models": models or {},
        "harnessReasoning": harness_reasoning,
        "warnings": warnings or [],
    }


class SetupCommandTests(unittest.TestCase):
    def _binary(self, root: Path, name: str) -> Path:
        path = root / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    def _attempts(self, **installed: Path) -> dict[str, dict[str, object]]:
        attempts = {engine: _record(installed=False) for engine in KNOWN_ENGINES}
        for engine, binary in installed.items():
            attempts[engine] = _record(
                installed=True,
                selector=[str(binary)],
                version=f"{engine}-cli 1.0",
                models={f"{engine}-model": {}},
            )
        return attempts

    def _seed_cache(self, home: Path) -> tuple[Path, bytes]:
        snapshot = harness_discovery.empty_snapshot(profile="default")
        snapshot["harnesses"]["codex"] = _record(
            installed=True,
            selector=["/previous/codex"],
            version="codex-cli 0.9",
            models={"previous-model": {}},
        )
        path = harness_discovery.write_discovery_cache(None, snapshot, home=home)
        return path, path.read_bytes()

    def _run(
        self,
        argv: list[str],
        *,
        home: Path,
        attempts: dict[str, dict[str, object]],
        extra_env: dict[str, str] | None = None,
        probe_side_effect=None,
    ) -> tuple[int, str, str, mock.Mock]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
        env.update(extra_env or {})
        probe = mock.Mock(
            side_effect=probe_side_effect or (lambda _config, harness, **_kwargs: attempts[harness])
        )
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(harness_discovery, "probe_harness", probe),
            mock.patch.object(
                model_discovery,
                "_probe_devin_models",
                side_effect=AssertionError("setup must not run legacy prompt probes"),
            ),
        ):
            code = cli.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue(), probe

    def test_first_run_writes_only_absolute_selectors_and_private_cache(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cursor = self._binary(home, "cursor-agent")
            codex = self._binary(home, "codex-custom")
            attempts = self._attempts(cursor=cursor, codex=codex)

            code, stdout, stderr, probe = self._run(
                ["--json", "setup"], home=home, attempts=attempts
            )
            payload = json.loads(stdout)
            config_path = home / ".delegate" / "config.json"
            cache_path = home / ".delegate" / "cache" / "discovery" / "default.json"
            config_bytes = config_path.read_bytes()
            config = json.loads(config_bytes)

            self.assertEqual(code, 0, stderr)
            self.assertEqual(probe.call_count, len(KNOWN_ENGINES))
            self.assertEqual(
                config,
                {
                    "codex": {"binary": str(codex)},
                    "cursor": {"argvPrefix": [str(cursor)]},
                },
            )
            self.assertTrue(payload["discoveryReady"])
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["configState"], "created")
            self.assertEqual(payload["configPath"], str(config_path))
            self.assertEqual(payload["cachePath"], str(cache_path))
            self.assertEqual(payload["nextAction"], "Run delegate cursor safe.")
            self.assertEqual(set(payload["harnesses"]), set(KNOWN_ENGINES))
            self.assertEqual(
                set(payload["harnesses"]["codex"]),
                {
                    "installed",
                    "probeStatus",
                    "version",
                    "catalogCount",
                    "reasoningStatus",
                    "launchable",
                    "stale",
                    "nextAction",
                },
            )
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config_path.parent.stat().st_mode), 0o700)

            code, stdout, stderr, _ = self._run(["--json", "setup"], home=home, attempts=attempts)
            rerun = json.loads(stdout)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(rerun["configState"], "existing")
            self.assertEqual(config_path.read_bytes(), config_bytes)

    def test_missing_explicit_config_is_created_at_exact_path(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            target = home / "custom" / "delegate.json"

            code, stdout, stderr, _ = self._run(
                ["--json", "setup"],
                home=home,
                attempts=self._attempts(codex=codex),
                extra_env={"DELEGATE_CONFIG": str(target)},
            )
            payload = json.loads(stdout)

            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["configPath"], str(target))
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), {"codex": {"binary": str(codex)}}
            )
            self.assertFalse((home / ".delegate" / "config.json").exists())

    def test_missing_explicit_config_allows_symlinked_ancestor_directory(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            real_root = home / "real-config-root"
            real_root.mkdir()
            linked_root = home / "linked-config-root"
            linked_root.symlink_to(real_root, target_is_directory=True)
            target = linked_root / "nested" / "delegate.json"

            code, stdout, stderr, _ = self._run(
                ["--json", "setup"],
                home=home,
                attempts=self._attempts(codex=codex),
                extra_env={"DELEGATE_CONFIG": str(target)},
            )
            payload = json.loads(stdout)

            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["configPath"], str(target))
            self.assertEqual(
                json.loads((real_root / "nested" / "delegate.json").read_text()),
                {"codex": {"binary": str(codex)}},
            )

    def test_missing_explicit_config_still_rejects_final_component_symlink(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            target = home / "config-link.json"
            outside = home / "outside.json"
            target.symlink_to(outside)

            code, stdout, stderr, _ = self._run(
                ["--json", "setup"],
                home=home,
                attempts=self._attempts(codex=codex),
                extra_env={"DELEGATE_CONFIG": str(target)},
            )
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(payload["error"], "setup_config_write_failed")
            self.assertEqual(payload["configState"], "present-unverified")
            self.assertTrue(target.is_symlink())
            self.assertFalse(outside.exists())

    def test_existing_config_is_preserved_and_malformed_fails_before_probe(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            path = home / ".delegate" / "config.json"
            path.parent.mkdir()
            original = b'{"codex":{"binary":"' + str(codex).encode() + b'"}}\n'
            path.write_bytes(original)

            code, stdout, stderr, _ = self._run(
                ["--json", "setup"], home=home, attempts=self._attempts(codex=codex)
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["configState"], "existing")
            self.assertEqual(path.read_bytes(), original)

            path.write_text("{not-json", encoding="utf-8")
            code, stdout, stderr, probe = self._run(
                ["--json", "setup"], home=home, attempts=self._attempts(codex=codex)
            )
            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(json.loads(stdout)["error"], "invalid_config_json")
            probe.assert_not_called()

    def test_disappearing_existing_config_never_falls_back_to_embedded_defaults(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            path = home / ".delegate" / "config.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"codex": {"binary": str(codex)}}), encoding="utf-8")
            real_load = setup_commands.delegate_config.load_config

            def disappear(**kwargs):
                path.unlink()
                return real_load(**kwargs)

            with mock.patch.object(
                setup_commands.delegate_config,
                "load_config",
                side_effect=disappear,
            ):
                code, stdout, stderr, probe = self._run(
                    ["--json", "setup"], home=home, attempts=self._attempts(codex=codex)
                )

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(json.loads(stdout)["error"], "config_changed_during_setup")
            probe.assert_not_called()

    def test_no_harnesses_returns_exit_three_without_changing_config_or_cache(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cache_path, cache_before = self._seed_cache(home)
            code, stdout, stderr, _ = self._run(
                ["--json", "setup"], home=home, attempts=self._attempts()
            )
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_MISSING_BINARY, stderr)
            self.assertEqual(payload["error"], "no_harnesses_found")
            self.assertFalse(payload["discoveryReady"])
            self.assertFalse(payload["ready"])
            self.assertFalse((home / ".delegate" / "config.json").exists())
            self.assertEqual(payload["cacheState"], "unchanged")
            self.assertEqual(cache_path.read_bytes(), cache_before)

    def test_droid_without_configured_model_is_discovered_but_not_ready(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            droid = self._binary(home, "droid")
            attempts = self._attempts(droid=droid)
            attempts["droid"]["defaultModel"] = "native-default"

            code, stdout, stderr, _ = self._run(["--json", "setup"], home=home, attempts=attempts)
            payload = json.loads(stdout)

            self.assertEqual(code, 0, stderr)
            self.assertTrue(payload["discoveryReady"])
            self.assertFalse(payload["ready"])
            self.assertFalse(payload["harnesses"]["droid"]["launchable"])
            self.assertEqual(
                payload["harnesses"]["droid"]["nextAction"],
                "Pass --model or configure droid.defaultModel.",
            )
            self.assertNotIn(
                "defaultModel",
                json.loads((home / ".delegate" / "config.json").read_text())["droid"],
            )

    def test_divergent_race_winner_is_preserved_and_requires_fresh_setup(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cache_path, cache_before = self._seed_cache(home)
            codex = self._binary(home, "codex")
            winner_codex = self._binary(home, "winner-codex")
            winner = (
                json.dumps(
                    {
                        "codex": {"binary": str(winner_codex)},
                        "profiles": {
                            "default": "winner",
                            "definitions": {
                                "winner": {"env": {"CODEX_HOME": str(home / "winner-home")}}
                            },
                        },
                    }
                ).encode()
                + b"\n"
            )

            def win_race(path: Path, _payload: object) -> bool:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(winner)
                return False

            with (
                mock.patch.object(
                    setup_commands.private_io,
                    "write_json_atomic_if_absent",
                    side_effect=win_race,
                ),
                mock.patch.object(
                    setup_commands.profiles,
                    "resolve_active_profile",
                    wraps=setup_commands.profiles.resolve_active_profile,
                ) as resolve,
                mock.patch.object(
                    setup_commands,
                    "models_summary_payload",
                    side_effect=AssertionError("race winner must not mix with old attempts"),
                ),
            ):
                code, stdout, stderr, probe = self._run(
                    ["--json", "setup"], home=home, attempts=self._attempts(codex=codex)
                )
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(payload["error"], "config_changed_during_setup")
            self.assertEqual(payload["configState"], "race-winner")
            self.assertIn("Rerun delegate setup", payload["nextActions"][0])
            resolve.assert_called_once()
            self.assertEqual(probe.call_count, len(KNOWN_ENGINES))
            self.assertEqual((home / ".delegate" / "config.json").read_bytes(), winner)
            self.assertEqual(payload["cacheState"], "unchanged")
            self.assertEqual(cache_path.read_bytes(), cache_before)

    def test_existing_config_mutated_during_probe_is_preserved_and_requires_rerun(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cache_path, cache_before = self._seed_cache(home)
            codex = self._binary(home, "codex")
            changed_codex = self._binary(home, "changed-codex")
            path = home / ".delegate" / "config.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"codex": {"binary": str(codex)}}), encoding="utf-8")
            changed = json.dumps({"codex": {"binary": str(changed_codex)}}).encode() + b"\n"
            attempts = self._attempts(codex=codex)
            mutated = False

            def probe(_config, harness, **_kwargs):
                nonlocal mutated
                if not mutated:
                    path.write_bytes(changed)
                    mutated = True
                return attempts[harness]

            code, stdout, stderr, probe_mock = self._run(
                ["--json", "setup"],
                home=home,
                attempts=attempts,
                probe_side_effect=probe,
            )
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(payload["error"], "config_changed_during_setup")
            self.assertEqual(payload["configState"], "changed")
            self.assertEqual(payload["cacheState"], "unchanged")
            self.assertIn("Rerun delegate setup", payload["nextActions"][0])
            self.assertEqual(probe_mock.call_count, len(KNOWN_ENGINES))
            self.assertEqual(path.read_bytes(), changed)
            self.assertEqual(cache_path.read_bytes(), cache_before)

    def test_profile_environment_and_cache_are_selected_once(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            path = home / ".delegate" / "config.json"
            path.parent.mkdir()
            path.write_text(
                json.dumps(
                    {
                        "codex": {"binary": str(codex)},
                        "profiles": {
                            "definitions": {
                                "work": {"env": {"CODEX_HOME": str(home / "work-codex")}}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            seen: list[str | None] = []
            attempts = self._attempts(codex=codex)

            def probe(_config, harness, *, env, **_kwargs):
                seen.append(env.get("CODEX_HOME"))
                return attempts[harness]

            with mock.patch.object(
                setup_commands.profiles,
                "resolve_active_profile",
                wraps=setup_commands.profiles.resolve_active_profile,
            ) as resolve:
                code, stdout, stderr, _ = self._run(
                    ["--json", "--auth-profile", "work", "setup"],
                    home=home,
                    attempts=attempts,
                    probe_side_effect=probe,
                )
            payload = json.loads(stdout)

            self.assertEqual(code, 0, stderr)
            resolve.assert_called_once()
            self.assertEqual(payload["profile"]["name"], "work")
            self.assertEqual(seen, [str(home / "work-codex")] * len(KNOWN_ENGINES))
            self.assertIn("profile-", Path(payload["cachePath"]).name)

    def test_current_probe_error_reports_retained_cache_as_stale(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            config_path = home / ".delegate" / "config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps({"codex": {"binary": str(codex)}}), encoding="utf-8")
            attempts = self._attempts(codex=codex)
            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "PATH": os.environ.get("PATH", "")},
                clear=True,
            ):
                snapshot = harness_discovery.empty_snapshot(profile="default")
                snapshot["harnesses"]["codex"] = attempts["codex"]
                harness_discovery.write_discovery_cache(None, snapshot)
            attempts["codex"] = _record(
                installed=True,
                selector=[str(codex)],
                status="error",
                version="codex-cli 1.0",
                warnings=["provider api_key=do-not-print"],
            )

            code, stdout, stderr, _ = self._run(["--json", "setup"], home=home, attempts=attempts)
            payload = json.loads(stdout)

            self.assertEqual(code, 0, stderr)
            self.assertFalse(payload["discoveryReady"])
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["cacheState"], "unchanged")
            self.assertTrue(payload["harnesses"]["codex"]["stale"])
            self.assertEqual(payload["harnesses"]["codex"]["probeStatus"], "error")
            self.assertNotIn("do-not-print", stdout)

    def test_current_selector_mismatch_is_stale_with_drift_remediation(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            configured = self._binary(home, "configured-codex")
            attempted = self._binary(home, "attempted-codex")
            path = home / ".delegate" / "config.json"
            path.parent.mkdir()
            path.write_text(
                json.dumps({"codex": {"binary": str(configured)}}),
                encoding="utf-8",
            )
            attempts = self._attempts(codex=attempted)

            code, stdout, stderr, _ = self._run(["--json", "setup"], home=home, attempts=attempts)
            codex = json.loads(stdout)["harnesses"]["codex"]

            self.assertEqual(code, 0, stderr)
            self.assertTrue(codex["stale"])
            self.assertFalse(codex["launchable"])
            self.assertIn("Config selector drifted", codex["nextAction"])
            self.assertIn("rerun delegate setup", codex["nextAction"])

    def test_config_publication_error_leaves_prior_cache_unchanged(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cache_path, cache_before = self._seed_cache(home)
            codex = self._binary(home, "codex")
            with mock.patch.object(
                setup_commands.private_io,
                "write_json_atomic_if_absent",
                side_effect=OSError("simulated config failure with api_key=do-not-print"),
            ):
                code, stdout, stderr, _ = self._run(
                    ["--json", "setup"], home=home, attempts=self._attempts(codex=codex)
                )
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(payload["error"], "setup_config_write_failed")
            self.assertEqual(payload["configState"], "absent")
            self.assertEqual(payload["cacheState"], "unchanged")
            self.assertTrue(payload["discoveryReady"])
            self.assertEqual(cache_path.read_bytes(), cache_before)
            self.assertFalse(Path(payload["configPath"]).exists())
            self.assertNotIn("do-not-print", stdout)

    def test_post_publication_error_reports_present_unverified_config(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cache_path, cache_before = self._seed_cache(home)
            codex = self._binary(home, "codex")

            def publish_then_fail(path: Path, payload: object) -> bool:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")
                raise OSError("simulated directory fsync failure")

            with mock.patch.object(
                setup_commands.private_io,
                "write_json_atomic_if_absent",
                side_effect=publish_then_fail,
            ):
                code, stdout, stderr, _ = self._run(
                    ["--json", "setup"], home=home, attempts=self._attempts(codex=codex)
                )
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(payload["error"], "setup_config_write_failed")
            self.assertEqual(payload["configState"], "present-unverified")
            self.assertEqual(payload["cacheState"], "unchanged")
            self.assertTrue(Path(payload["configPath"]).exists())
            self.assertEqual(cache_path.read_bytes(), cache_before)

    def test_unsafe_relative_selector_is_never_written_to_config(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cache_path, cache_before = self._seed_cache(home)
            attempts = self._attempts()
            attempts["codex"] = _record(
                installed=True,
                selector=["relative-codex"],
                version="codex-cli 1.0",
            )

            code, stdout, stderr, _ = self._run(["--json", "setup"], home=home, attempts=attempts)
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(payload["error"], "setup_selector_unsafe")
            self.assertEqual(payload["cacheState"], "unchanged")
            self.assertFalse((home / ".delegate" / "config.json").exists())
            self.assertEqual(cache_path.read_bytes(), cache_before)

    def test_cache_publication_error_keeps_reconciled_config_and_prior_cache(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            cache_path, cache_before = self._seed_cache(home)
            codex = self._binary(home, "codex")

            with mock.patch.object(
                harness_discovery,
                "write_discovery_cache",
                side_effect=OSError("simulated cache failure with api_key=do-not-print"),
            ):
                code, stdout, stderr, _ = self._run(
                    ["--json", "setup"], home=home, attempts=self._attempts(codex=codex)
                )
            payload = json.loads(stdout)

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(payload["error"], "setup_cache_write_failed")
            self.assertEqual(payload["configState"], "created")
            self.assertEqual(payload["cacheState"], "error")
            self.assertTrue(Path(payload["configPath"]).exists())
            self.assertEqual(cache_path.read_bytes(), cache_before)
            self.assertNotIn("do-not-print", stdout)

    def test_missing_ai_profile_overlay_is_blocked_before_probing(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            code, stdout, stderr, probe = self._run(
                ["--json", "setup"],
                home=home,
                attempts=self._attempts(codex=codex),
                extra_env={"AI_PROFILE": "work"},
            )

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(json.loads(stdout)["error"], "profile_config_missing")
            probe.assert_not_called()
            self.assertFalse((home / ".delegate" / "config.json").exists())

    def test_auth_profile_requires_an_existing_definition(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            code, stdout, stderr, probe = self._run(
                ["--json", "--auth-profile", "work", "setup"],
                home=home,
                attempts=self._attempts(codex=codex),
            )

            self.assertEqual(code, cli.EXIT_USAGE, stderr)
            self.assertEqual(json.loads(stdout)["error"], "unknown_profile")
            probe.assert_not_called()
            self.assertFalse((home / ".delegate" / "config.json").exists())

    def test_human_output_is_bounded_and_does_not_print_missing_harnesses(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            code, stdout, stderr, _ = self._run(
                ["setup"], home=home, attempts=self._attempts(codex=codex)
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("discovery ready: true", stdout)
            self.assertIn("ready: true", stdout)
            self.assertIn("codex: ok, models=1, launchable=true", stdout)
            self.assertNotIn("droid:", stdout)

    def test_public_version_is_redacted_and_size_bounded(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            codex = self._binary(home, "codex")
            attempts = self._attempts(codex=codex)
            attempts["codex"]["version"] = "codex-cli 1.0 sk-abcdefgh " + ("x" * 1_000)

            code, stdout, stderr, _ = self._run(["--json", "setup"], home=home, attempts=attempts)
            version = json.loads(stdout)["harnesses"]["codex"]["version"]

            self.assertEqual(code, 0, stderr)
            self.assertLessEqual(len(version), setup_commands.PUBLIC_VERSION_LIMIT)
            self.assertNotIn("sk-abcdefgh", version)

    def test_cursor_path_collision_uses_fingerprinted_cursor_agent(self):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home).resolve()
            bin_dir = home / "bin"
            bin_dir.mkdir()
            wrong_agent = bin_dir / "agent"
            wrong_agent.write_text("#!/bin/sh\nprintf 'grok 1.0\\n'\n", encoding="utf-8")
            wrong_agent.chmod(0o700)
            cursor_agent = bin_dir / "cursor-agent"
            cursor_agent.write_text(
                "#!/bin/sh\n"
                'if [ "${1:-}" = "--version" ]; then\n'
                "  printf '2026.07.17-abcdef\\n'\n"
                "else\n"
                "  printf 'Available models\\n\\ncomposer-2.5 - Composer 2.5 (default)\\n'\n"
                "fi\n",
                encoding="utf-8",
            )
            cursor_agent.chmod(0o700)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "PATH": str(bin_dir)},
                clear=True,
            ):
                code = cli.main(["--json", "setup"], stdout=stdout, stderr=stderr)
            config = json.loads((home / ".delegate" / "config.json").read_text(encoding="utf-8"))

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(config, {"cursor": {"argvPrefix": [str(cursor_agent)]}})


if __name__ == "__main__":
    unittest.main()
