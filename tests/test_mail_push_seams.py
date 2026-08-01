from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import config as delegate_config
from delegate_agent import mail, runner
from tests.delegate_commands_test_base import CommandTestBase


class MailPushSeamTests(CommandTestBase):
    """Production-launch coverage for mail push across the three work seams."""

    credential_canary = b"mail-push-credential-canary"

    def setUp(self) -> None:
        super().setUp()
        self._config_env["HOME"] = str(Path(self._config_env["HOME"]).resolve())
        self._launch_baselines: dict[Path, dict[Path, bytes | str]] = {}

    def _write_fake_harness(self, root: Path, *, fail_primary: bool = False) -> Path:
        fake = root / "fake-harness"
        failure = (
            "if os.environ.get('FAKE_FAIL_PRIMARY') == '1' and "
            "'codex-home-fallback' not in os.environ.get('CODEX_HOME', ''):\n"
            "    print('You exceeded your current quota usage limit', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            if fail_primary
            else ""
        )
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "observation = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'env': {key: value for key, value in os.environ.items()\n"
            "            if key.startswith('DELEGATE_') or key == 'CODEX_HOME'},\n"
            "}\n"
            "with open(os.environ['FAKE_OBSERVATIONS'], 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(observation) + '\\n')\n"
            + failure
            + "print(json.dumps([{'type': 'result', 'result': 'ok', 'permission_denials': []}]))\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    def _write_codex_home(self, root: Path, name: str, token: bytes) -> Path:
        home = root / name
        home.mkdir()
        (home / "auth.json").write_bytes(token)
        (home / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
        return home

    def _config(
        self,
        engine: str,
        fake: Path,
        observations: Path,
        *,
        primary_home: Path | None = None,
        fallback_home: Path | None = None,
    ) -> dict:
        config = delegate_config.embedded_default_config()
        config["mail"] = {"enabled": True}
        config[engine] = {**config[engine], "binary": str(fake)}
        definitions: dict[str, dict[str, dict[str, str]]] = {
            "primary": {"env": {"FAKE_OBSERVATIONS": str(observations)}}
        }
        if primary_home is not None:
            definitions["primary"]["env"]["CODEX_HOME"] = str(primary_home)
        if fallback_home is not None:
            definitions["fallback"] = {"env": {"CODEX_HOME": str(fallback_home)}}
            config["codex"] = {**config["codex"], "fallbackProfile": "fallback"}
        config["profiles"] = {"default": "primary", "detectFrom": [], "definitions": definitions}
        return config

    def _make_git_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
        (workspace / "README.md").write_text("mail seams\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Delegate Test",
                "-c",
                "user.email=delegate-test@example.com",
                "commit",
                "-m",
                "init",
            ],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        return workspace

    def _run_launch(
        self, argv: list[str], *, observations: Path, fail_primary: bool = False
    ) -> tuple[int, str, str]:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_OBSERVATIONS": str(observations),
                "FAKE_FAIL_PRIMARY": "1" if fail_primary else "",
            },
            clear=False,
        ):
            return self.run_main(argv)

    @staticmethod
    def _only_run(workspace: Path) -> tuple[Path, dict]:
        manifests = list((workspace / ".delegate" / "runs").glob("*/manifest.json"))
        if len(manifests) != 1:
            raise AssertionError(f"expected one run manifest, found {manifests}")
        return manifests[0].parent, json.loads(manifests[0].read_text(encoding="utf-8"))

    @staticmethod
    def _run(workspace: Path, run_id: str) -> tuple[Path, dict]:
        run_path = workspace / ".delegate" / "runs" / run_id
        return run_path, json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))

    @staticmethod
    def _observations(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    @staticmethod
    def _file_snapshot(root: Path) -> dict[Path, bytes | str]:
        snapshot: dict[Path, bytes | str] = {}
        for path in root.rglob("*"):
            if path.is_symlink():
                snapshot[path] = os.readlink(path)
            elif path.is_file():
                snapshot[path] = path.read_bytes()
        return snapshot

    def _assert_provisioning_delta_is_run_scoped(
        self, workspace: Path, run_path: Path, box: Path
    ) -> None:
        baseline = self._launch_baselines[workspace]
        current = self._file_snapshot(workspace.parent)
        artifact_names = {
            mail.MAIL_PUSH_CURSOR_FILE_NAME,
            mail.MAIL_PUSH_PENDING_FILE_NAME,
            mail.MAIL_PUSH_FAILURE_FILE_NAME,
            mail.MAIL_PUSH_NONCE_FILE_NAME,
            mail.MAIL_PUSH_SETTINGS_FILE_NAME,
            "hooks.json",
            mail.MAIL_PUSH_CODEX_HOME_NAME,
            mail.MAIL_PUSH_FALLBACK_CODEX_HOME_NAME,
        }
        changed = {
            path
            for path, contents in current.items()
            if baseline.get(path) != contents and path.name in artifact_names
        }
        self.assertTrue(changed)
        self.assertTrue(
            all(path.is_relative_to(box) or path.is_relative_to(run_path) for path in changed),
            changed,
        )

    def _assert_verified_launch(
        self,
        *,
        engine: str,
        workspace: Path,
        observations: Path,
        credential_canary: bytes | None = None,
        run_id: str | None = None,
    ) -> tuple[Path, dict, dict]:
        run_path, manifest = self._run(workspace, run_id) if run_id else self._only_run(workspace)
        observed = self._observations(observations)
        self.assertEqual(len(observed), 1)
        child = observed[0]
        self.assertEqual(manifest["argv"][1:], child["argv"])
        box = mail.boxes_root(workspace / ".delegate") / manifest["runId"]
        self._assert_provisioning_delta_is_run_scoped(workspace, run_path, box)
        self.assertEqual(
            json.loads((box / mail.MAIL_PUSH_CURSOR_FILE_NAME).read_text(encoding="utf-8")),
            {"schema": mail.MAIL_PUSH_SCHEMA, "lastSeq": 0},
        )
        self.assertTrue((box / mail.MAIL_PUSH_SETTINGS_FILE_NAME).is_file())
        for path in mail.mail_root(workspace / ".delegate").rglob("*"):
            if path.is_file() and not path.is_symlink() and credential_canary is not None:
                self.assertNotIn(credential_canary, path.read_bytes(), path)

        self.assertEqual(child["env"]["DELEGATE_MAIL_HOOK_HARNESS"], engine)
        self.assertIn("DELEGATE_MAIL_HOOK_NONCE", child["env"])
        if engine == "claude":
            self.assertIn("--settings", child["argv"])
            self.assertEqual(
                Path(child["argv"][child["argv"].index("--settings") + 1]).resolve(),
                (box / mail.MAIL_PUSH_SETTINGS_FILE_NAME).resolve(),
            )
        else:
            private_home = Path(child["env"]["CODEX_HOME"]).resolve()
            self.assertTrue(private_home.is_relative_to(run_path.resolve()))
            self.assertFalse(private_home.is_relative_to(mail.mail_root(workspace / ".delegate")))
            self.assertTrue((private_home / "hooks.json").is_file())
            self.assertIn("-c", child["argv"])
            self.assertIn("hooks=true", child["argv"])
            self.assertIn("--dangerously-bypass-hook-trust", child["argv"])
        return run_path, manifest, child

    def _run_verified_tracked(self, engine: str) -> tuple[Path, Path, Path, Path]:
        temp = tempfile.TemporaryDirectory(prefix=f"delegate-mail-push-{engine}-tracked-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        observations = root / "observations.jsonl"
        primary = self._write_codex_home(root, "primary-codex", self.credential_canary)
        fake = self._write_fake_harness(root)
        Path(self._config_env["DELEGATE_CONFIG"]).write_text(
            json.dumps(self._config(engine, fake, observations, primary_home=primary)),
            encoding="utf-8",
        )
        self._launch_baselines[workspace] = self._file_snapshot(root)
        code, _stdout, stderr = self._run_launch(
            ["--cwd", str(workspace), engine, "work", "--mail-push", "launch"],
            observations=observations,
        )
        self.assertEqual(code, 0, f"stdout={_stdout}\nstderr={stderr}")
        return root, workspace, observations, primary

    def _run_verified_persistent(self, engine: str) -> tuple[Path, Path, Path, Path]:
        temp = tempfile.TemporaryDirectory(prefix=f"delegate-mail-push-{engine}-persistent-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = self._make_git_workspace(root)
        observations = root / "observations.jsonl"
        primary = self._write_codex_home(root, "primary-codex", self.credential_canary)
        fake = self._write_fake_harness(root)
        Path(self._config_env["DELEGATE_CONFIG"]).write_text(
            json.dumps(self._config(engine, fake, observations, primary_home=primary)),
            encoding="utf-8",
        )
        self._launch_baselines[workspace] = self._file_snapshot(root)
        code, _stdout, stderr = self._run_launch(
            [
                "--cwd",
                str(workspace),
                "--isolation",
                "worktree",
                engine,
                "work",
                "--mail-push",
                "launch",
            ],
            observations=observations,
        )
        self.assertEqual(code, 0, f"stdout={_stdout}\nstderr={stderr}")
        return root, workspace, observations, primary

    def _run_verified_attached(self, engine: str) -> tuple[Path, Path, Path, Path, str]:
        root, workspace, observations, primary = self._run_verified_persistent(engine)
        _run_path, manifest = self._only_run(workspace)
        observations.unlink()
        self._launch_baselines[workspace] = self._file_snapshot(root)
        code, _stdout, stderr = self._run_launch(
            [
                "--json",
                "--cwd",
                str(workspace),
                "resume",
                "--mail-push",
                manifest["alias"],
                "continue",
            ],
            observations=observations,
        )
        self.assertEqual(code, 0, f"stdout={_stdout}\nstderr={stderr}")
        manifests = list((workspace / ".delegate" / "runs").glob("*/manifest.json"))
        resumed = [path for path in manifests if path.parent.name != manifest["runId"]]
        self.assertEqual(len(resumed), 1)
        return root, workspace, observations, primary, resumed[0].parent.name

    def test_claude_mail_push_tracked_work_launch(self):
        _root, workspace, observations, _primary = self._run_verified_tracked("claude")
        self._assert_verified_launch(
            engine="claude",
            workspace=workspace,
            observations=observations,
            credential_canary=self.credential_canary,
        )

    def test_codex_mail_push_tracked_work_launch(self):
        _root, workspace, observations, _primary = self._run_verified_tracked("codex")
        self._assert_verified_launch(
            engine="codex",
            workspace=workspace,
            observations=observations,
            credential_canary=self.credential_canary,
        )

    def test_claude_mail_push_persistent_worktree_launch(self):
        _root, workspace, observations, _primary = self._run_verified_persistent("claude")
        self._assert_verified_launch(
            engine="claude",
            workspace=workspace,
            observations=observations,
            credential_canary=self.credential_canary,
        )

    def test_codex_mail_push_persistent_worktree_launch(self):
        _root, workspace, observations, _primary = self._run_verified_persistent("codex")
        self._assert_verified_launch(
            engine="codex",
            workspace=workspace,
            observations=observations,
            credential_canary=self.credential_canary,
        )

    def test_claude_mail_push_attached_resume_launch(self):
        _root, workspace, observations, _primary, run_id = self._run_verified_attached("claude")
        run_path, manifest, _child = self._assert_verified_launch(
            engine="claude",
            workspace=workspace,
            observations=observations,
            credential_canary=self.credential_canary,
            run_id=run_id,
        )
        self.assertEqual(manifest["isolationLifecycle"], "attached")
        self.assertTrue(run_path.is_dir())

    def test_codex_mail_push_attached_resume_launch(self):
        _root, workspace, observations, _primary, run_id = self._run_verified_attached("codex")
        _run_path, manifest, _child = self._assert_verified_launch(
            engine="codex",
            workspace=workspace,
            observations=observations,
            credential_canary=self.credential_canary,
            run_id=run_id,
        )
        self.assertEqual(manifest["isolationLifecycle"], "attached")

    def test_codex_mail_push_fallback_uses_second_private_home_and_fallback_auth(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-push-fallback-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            observations = root / "observations.jsonl"
            primary = self._write_codex_home(root, "primary-codex", b"primary-auth")
            fallback = self._write_codex_home(root, "fallback-codex", b"fallback-auth")
            fake = self._write_fake_harness(root, fail_primary=True)
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(
                    self._config(
                        "codex", fake, observations, primary_home=primary, fallback_home=fallback
                    )
                ),
                encoding="utf-8",
            )
            code, _stdout, stderr = self._run_launch(
                ["--json", "--cwd", str(workspace), "codex", "work", "--mail-push", "launch"],
                observations=observations,
                fail_primary=True,
            )
            self.assertEqual(code, 0, f"stdout={_stdout}\nstderr={stderr}")
            run_path, manifest = self._only_run(workspace)
            self.assertTrue(observations.exists(), (run_path / "stderr.log").read_text())
            observed = self._observations(observations)
            self.assertEqual(len(observed), 2)
            primary_home = Path(observed[0]["env"]["CODEX_HOME"])
            fallback_home = Path(observed[1]["env"]["CODEX_HOME"])
            self.assertEqual(primary_home.name, mail.MAIL_PUSH_CODEX_HOME_NAME)
            self.assertEqual(fallback_home.name, mail.MAIL_PUSH_FALLBACK_CODEX_HOME_NAME)
            self.assertNotEqual(primary_home, fallback_home)
            self.assertTrue(fallback_home.is_relative_to(run_path))
            self.assertTrue((fallback_home / "hooks.json").is_file())
            self.assertEqual((fallback_home / "auth.json").read_bytes(), b"fallback-auth")
            self.assertEqual(manifest["argv"][1:], observed[1]["argv"])

    def test_codex_mail_push_without_fallback_creates_only_one_private_home(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-push-no-fallback-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            observations = root / "observations.jsonl"
            primary = self._write_codex_home(root, "primary-codex", b"primary-auth")
            fake = self._write_fake_harness(root, fail_primary=True)
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(self._config("codex", fake, observations, primary_home=primary)),
                encoding="utf-8",
            )
            code, _stdout, _stderr = self._run_launch(
                ["--json", "--cwd", str(workspace), "codex", "work", "--mail-push", "launch"],
                observations=observations,
                fail_primary=True,
            )
            self.assertEqual(code, 1)
            run_path, manifest = self._only_run(workspace)
            self.assertTrue(observations.exists(), (run_path / "stderr.log").read_text())
            observed = self._observations(observations)
            self.assertEqual(len(observed), 1)
            self.assertNotIn("fallbackProfile", manifest)
            self.assertEqual(
                Path(observed[0]["env"]["CODEX_HOME"]).name,
                mail.MAIL_PUSH_CODEX_HOME_NAME,
            )
            self.assertFalse((run_path / mail.MAIL_PUSH_FALLBACK_CODEX_HOME_NAME).exists())

    def test_grok_mail_push_tracked_work_degrades_to_pull_once_without_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-push-grok-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            observations = root / "observations.jsonl"
            fake = self._write_fake_harness(root)
            Path(self._config_env["DELEGATE_CONFIG"]).write_text(
                json.dumps(self._config("grok", fake, observations)), encoding="utf-8"
            )
            code, _stdout, stderr = self._run_launch(
                ["--cwd", str(workspace), "grok", "work", "--mail-push", "launch"],
                observations=observations,
            )
            self.assertEqual(code, 0, f"stdout={_stdout}\nstderr={stderr}")
            run_path, manifest = self._only_run(workspace)
            warning = mail.mail_push_warning("grok")
            self.assertEqual(stderr.count(warning), 1)
            box = mail.boxes_root(workspace / ".delegate") / manifest["runId"]
            self.assertFalse((box / mail.MAIL_PUSH_CURSOR_FILE_NAME).exists())
            self.assertFalse((box / mail.MAIL_PUSH_SETTINGS_FILE_NAME).exists())
            snapshot = json.loads((run_path / runner.SNAPSHOT_FILE).read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (run_path / runner.EVENTS_JSONL)
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(
                [event for event in events if event.get("kind") == runner.MAIL_PUSH_EVENT_KIND],
                [{"kind": runner.MAIL_PUSH_EVENT_KIND, "message": warning}],
            )
            self.assertIn(warning, snapshot["warnings"])


if __name__ == "__main__":
    unittest.main()
