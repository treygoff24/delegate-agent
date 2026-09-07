import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import config, profiles, workflow_attempts, workflow_pinning
from delegate_agent.workflows import commands, registry


class WorkflowProfileIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "DELEGATE_PROFILE": "",
                "AI_PROFILE": "",
                "TEAM_PROFILE": "",
                "AUTH_ROOT": str(self.root / "accounts"),
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.base = config.embedded_default_config()
        self.base["profiles"] = {
            "detectFrom": ["TEAM_PROFILE", "DELEGATE_PROFILE"],
            "default": None,
            "definitions": {
                "A": {"env": {"CODEX_HOME": "$AUTH_ROOT/A"}},
                "B": {"env": {"CODEX_HOME": "$AUTH_ROOT/B"}},
            },
        }
        self.base["codex"]["fallbackProfile"] = "B"

    def make(self, selected):
        with mock.patch.dict(os.environ, {"DELEGATE_PROFILE": selected}):
            pin = workflow_pinning.create_pin(
                "wf_abcd01234567", workspace=self.workspace, config=self.base
            )
            attempt = workflow_attempts.create(
                pin, workflow_attempts.prepare(pin, self.base, "fixture")
            )
        return pin, attempt

    def child(self, pin, attempt, **env):
        code = """import json,os,sys
from delegate_agent import config,profiles
try:
    cfg,_=config.load_config()
    selected=profiles.resolve_active_profile(cfg,os.environ,expand_env=os.environ)
    print(json.dumps({'profile':selected.name,'home':selected.codex_home,'fallback':selected.codex_fallback_home,'attempt':os.environ.get('DELEGATE_WORKFLOW_ATTEMPT')}))
except config.ConfigError as exc:
    print(json.dumps({'error':exc.error})); sys.exit(2)
"""
        return subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, **pin.environment, **attempt.environment, **env},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_changed_selector_cannot_change_same_attempt_account(self):
        pin, attempt = self.make("A")
        first = self.child(pin, attempt, DELEGATE_PROFILE="A")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["profile"], "A")
        self.assertEqual(json.loads(first.stdout)["attempt"], str(attempt.path))
        overridden_ambient = self.child(
            pin, attempt, DELEGATE_PROFILE="A", CODEX_HOME=str(self.root / "irrelevant")
        )
        self.assertEqual(overridden_ambient.returncode, 0, overridden_ambient.stderr)
        self.assertEqual(
            json.loads(overridden_ambient.stdout)["home"], json.loads(first.stdout)["home"]
        )
        changed = self.child(pin, attempt, DELEGATE_PROFILE="B")
        self.assertEqual(changed.returncode, 2, changed.stdout + changed.stderr)
        self.assertEqual(json.loads(changed.stdout)["error"], "workflow_profile_drift")

    def test_absent_selector_cannot_start_selecting_a_profile(self):
        pin, attempt = self.make("")
        changed = self.child(pin, attempt, DELEGATE_PROFILE="B")
        self.assertEqual(changed.returncode, 2, changed.stdout + changed.stderr)

    def test_selector_drift_is_refused_before_approval_mutation(self):
        pin, _attempt = self.make("A")
        root = registry.ensure_workflow_dir(self.workspace, pin.workflow_id)
        (root / registry.SCRIPT_FILE).write_text("return True\n")
        registry.write_json(
            root / registry.STATUS_FILE,
            {
                "status": "paused",
                "workflowKeyVersion": 2,
                "gateKey": "gate",
                "gateResultHash": "evidence",
            },
        )
        before = (root / registry.STATUS_FILE).read_bytes()
        with (
            mock.patch.dict(os.environ, {"DELEGATE_PROFILE": "B"}),
            self.assertRaises(commands.DelegateError) as raised,
        ):
            commands.emit_approve(
                commands.WorkflowCommand("approve", wf_id=pin.workflow_id),
                workspace=self.workspace,
                config=self.base,
                stdout=io.StringIO(),
            )
        self.assertEqual(raised.exception.error, "workflow_profile_drift")
        self.assertEqual((root / registry.STATUS_FILE).read_bytes(), before)
        self.assertFalse((root / registry.APPROVAL_FILE).exists())

    def test_default_selection_and_token_rotation_keep_the_same_namespace(self):
        self.base["profiles"]["default"] = "A"
        pin, attempt = self.make("")
        auth = self.root / "accounts" / "A" / "auth.json"
        auth.parent.mkdir(parents=True)
        for token in ("FIRST_FIXTURE_CREDENTIAL", "ROTATED_FIXTURE_CREDENTIAL"):
            auth.write_text(json.dumps({"token": token}))
            child = self.child(pin, attempt)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout)["profile"], "A")
            self.assertNotIn(
                token, pin.path.read_text() + pin.config_path.read_text() + attempt.path.read_text()
            )
        changed = self.child(pin, attempt, TEAM_PROFILE="B")
        self.assertEqual(changed.returncode, 2, changed.stdout + changed.stderr)

    def test_changed_home_cannot_hide_a_missing_current_pin(self):
        pin, attempt = self.make("A")
        root = registry.ensure_workflow_dir(self.workspace, pin.workflow_id)
        (root / registry.SCRIPT_FILE).write_text("return True\n")
        registry.write_json(
            root / registry.STATUS_FILE,
            {"status": "paused", "workflowKeyVersion": 2},
        )
        registry.append_jsonl(
            root / registry.JOURNAL_FILE, {"seq": 1, "type": "attempt_config", **attempt.metadata}
        )
        other_home = self.root / "other-home"
        other_home.mkdir()
        with (
            mock.patch.dict(os.environ, {"HOME": str(other_home), "DELEGATE_PROFILE": "A"}),
            self.assertRaises(commands.DelegateError) as raised,
        ):
            commands.emit_run(
                commands.WorkflowCommand("run", resume=pin.workflow_id),
                workspace=self.workspace,
                config=self.base,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(raised.exception.error, "invalid_pin")
        self.assertFalse((root / registry.APPROVAL_FILE).exists())

    def test_external_harness_keeps_private_home_but_not_attempt_validator(self):
        pin, attempt = self.make("A")
        private = str(self.root / "private-codex")
        with mock.patch.dict(
            os.environ, {**pin.environment, **attempt.environment, "DELEGATE_PROFILE": "A"}
        ):
            for pure in (False, True):
                external = profiles.child_environment(
                    pure=pure,
                    overrides={"CODEX_HOME": private, "DELEGATE_CONFIG": str(attempt.config_path)},
                )
                self.assertNotIn("DELEGATE_WORKFLOW_PIN", external)
                self.assertNotIn("DELEGATE_WORKFLOW_ATTEMPT", external)
                self.assertEqual(external["CODEX_HOME"], private)
                self.assertEqual(external["DELEGATE_CONFIG"], str(attempt.config_path))

    def test_home_alias_retarget_cannot_redirect_the_pinned_namespace(self):
        accounts = self.root / "accounts"
        for name in ("A", "B"):
            (accounts / name).mkdir(parents=True)
        alias = self.root / "account-alias"
        alias.symlink_to(accounts / "A", target_is_directory=True)
        self.base["profiles"]["definitions"]["A"]["env"]["CODEX_HOME"] = str(alias)
        pin, attempt = self.make("A")
        alias.unlink()
        alias.symlink_to(accounts / "B", target_is_directory=True)
        same = self.child(pin, attempt, DELEGATE_PROFILE="A")
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertEqual(json.loads(same.stdout)["home"], str(accounts / "A"))
        (accounts / "A").rename(accounts / "A-original")
        (accounts / "A").symlink_to(accounts / "B", target_is_directory=True)
        redirected = self.child(pin, attempt, DELEGATE_PROFILE="A")
        self.assertEqual(redirected.returncode, 2, redirected.stdout + redirected.stderr)

    def test_pin_without_profile_identity_is_rejected(self):
        pin, _attempt = self.make("A")
        payload = json.loads(pin.path.read_text())
        payload.pop("profileIdentity")
        payload.pop("profileIdentityDigest")
        pin.path.chmod(0o600)
        pin.path.write_text(json.dumps(payload))
        pin.path.chmod(0o400)
        with self.assertRaises(workflow_pinning.WorkflowPinError) as raised:
            workflow_pinning.load_pin(pin.workflow_id)
        self.assertEqual(raised.exception.error, "invalid_pin")

    def test_custom_selector_and_namespace_expansion_remain_bound(self):
        with mock.patch.dict(os.environ, {"TEAM_PROFILE": "A"}):
            pin, attempt = self.make("")
        first_home = str(self.root / "accounts" / "A")
        child = self.child(
            pin, attempt, TEAM_PROFILE="A", AUTH_ROOT=str(self.root / "other-accounts")
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)["home"], first_home)
        self.assertEqual(json.loads(child.stdout)["fallback"], str(self.root / "accounts" / "B"))
        changed = self.child(pin, attempt, TEAM_PROFILE="B")
        self.assertEqual(changed.returncode, 2, changed.stdout + changed.stderr)


if __name__ == "__main__":
    unittest.main()
