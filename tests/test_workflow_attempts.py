from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from delegate_agent import config, workflow_attempts, workflow_pinning
from delegate_agent.workflows import commands, registry, runtime

ROOT = Path(__file__).resolve().parents[1]


class WorkflowAttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.base = config.embedded_default_config()
        self.base["cursor"]["defaultModel"] = "pinned-model"
        self.pin = workflow_pinning.create_pin(
            "wf_123456abcdef", workspace=self.workspace, config=self.base, home=self.home
        )

    def attempt(self, live=None):
        return workflow_attempts.create(
            self.pin, workflow_attempts.prepare(self.pin, live or self.base, "test-command-config")
        )

    def test_only_ops_change_and_exact_config_load_ignores_global_overlay(self):
        live = copy.deepcopy(self.base)
        live["cursor"]["defaultModel"] = "live-model"
        live["policy"]["work"]["networkAccess"] = not self.base["policy"]["work"]["networkAccess"]
        live["worktrees"]["retirementIgnoreGlobs"] = ["*"]
        live["worktrees"]["autoPrune"]["enabled"] = True
        live["workflows"]["itemThreads"] = 3
        attempt = self.attempt(live)
        self.assertEqual(attempt.config["cursor"], self.pin.config["cursor"])
        self.assertEqual(attempt.config["policy"], self.pin.config["policy"])
        self.assertEqual(attempt.config["worktrees"], self.pin.config["worktrees"])
        self.assertEqual(attempt.config["workflows"]["itemThreads"], 3)
        global_path = self.home / ".delegate" / "config.json"
        global_path.parent.mkdir()
        global_path.write_text(json.dumps({"cursor": {"models": {"injected": "model"}}}))
        with mock.patch.dict(os.environ, {**self.pin.environment, **attempt.environment}):
            loaded, source = config.load_config()
        self.assertEqual(loaded, attempt.config)
        self.assertEqual(source, str(attempt.config_path))
        self.assertEqual(workflow_pinning.load_pin(self.pin.workflow_id).config, self.pin.config)

    def test_environment_is_captured_once_then_cannot_override_attempt(self):
        with mock.patch.dict(os.environ, {"DELEGATE_STALL_MINUTES": "7.5"}):
            attempt = self.attempt()
        self.assertEqual(attempt.metadata["opsValues"]["workflows.stallMinutes"], 7.5)
        with mock.patch.dict(os.environ, {**self.pin.environment, **attempt.environment}):
            self.assertEqual(config.load_config()[0]["workflows"]["stallMinutes"], 7.5)
            os.environ["DELEGATE_STALL_MINUTES"] = "8"
            with self.assertRaises(config.ConfigError):
                config.load_config()

    def test_legacy_registry_timeout_alias_survives_default_merge(self):
        values = workflow_attempts.operational_values(
            {"tracking": {"registryLockTimeoutSeconds": 7}}
        )
        self.assertEqual(values["tracking.registryLockTimeoutSec"], 7)
        values = workflow_attempts.operational_values(
            {"tracking": {"registryLockTimeoutSec": 9, "registryLockTimeoutSeconds": 7}}
        )
        self.assertEqual(values["tracking.registryLockTimeoutSec"], 9)
        loaded = config.merge_config_layer(
            self.base, {"tracking": {"registryLockTimeoutSeconds": 7}}
        )
        self.assertEqual(
            workflow_attempts.operational_values(loaded)["tracking.registryLockTimeoutSec"], 7
        )
        self.assertEqual(self.base["tracking"]["registryLockTimeoutSec"], 120)

    def test_failed_attempt_write_does_not_poison_identical_retry(self):
        metadata = workflow_attempts.prepare(self.pin, self.base, "test")
        writer = workflow_attempts.run_registry.write_json_atomic

        def fail_manifest(path, value):
            if path.name == "attempt.json":
                raise OSError("injected manifest write failure")
            return writer(path, value)

        with (
            mock.patch.object(
                workflow_attempts.run_registry, "write_json_atomic", side_effect=fail_manifest
            ),
            self.assertRaises(OSError),
        ):
            workflow_attempts.create(self.pin, metadata)
        destination = (
            self.pin.path.parent.parent
            / "attempts"
            / self.pin.workflow_id
            / workflow_pinning._json_digest(metadata)
        )
        self.assertFalse(destination.exists())
        result = workflow_attempts.create(self.pin, metadata)
        self.assertEqual(result.metadata, metadata)
        self.assertEqual(workflow_attempts.load(result.path, pin=self.pin).config, result.config)

    def test_duplicate_attempt_publication_is_validated_and_partial_collision_retained(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _index: self.attempt(), range(2)))
        self.assertEqual(first.path, second.path)
        metadata = workflow_attempts.prepare(self.pin, self.base, "different-source")
        target = first.path.parent.parent / workflow_pinning._json_digest(metadata)
        target.mkdir()
        (target / "foreign.txt").write_bytes(b"retain this partial artifact")
        with self.assertRaises(workflow_pinning.WorkflowPinError):
            workflow_attempts.create(self.pin, metadata)
        self.assertEqual((target / "foreign.txt").read_bytes(), b"retain this partial artifact")
        self.assertEqual(list(target.iterdir()), [target / "foreign.txt"])

    def test_empty_collision_during_staging_is_not_replaced(self):
        metadata = workflow_attempts.prepare(self.pin, self.base, "collision-test")
        destination = (
            self.pin.path.parent.parent
            / "attempts"
            / self.pin.workflow_id
            / workflow_pinning._json_digest(metadata)
        )
        writer = workflow_attempts.run_registry.write_json_atomic
        collision_inode = []

        def publish_collision(path, value):
            writer(path, value)
            if path.name == "attempt.json":
                destination.mkdir()
                collision_inode.append(destination.stat().st_ino)

        with (
            mock.patch.object(
                workflow_attempts.run_registry, "write_json_atomic", side_effect=publish_collision
            ),
            self.assertRaises(workflow_pinning.WorkflowPinError),
        ):
            workflow_attempts.create(self.pin, metadata)
        self.assertEqual(destination.stat().st_ino, collision_inode[0])
        self.assertEqual(list(destination.iterdir()), [])

    def test_failed_resume_restores_exact_prior_approval(self):
        root = registry.ensure_workflow_dir(self.workspace, self.pin.workflow_id)
        (root / registry.SCRIPT_FILE).write_text("return True\n")
        approval_path = root / registry.APPROVAL_FILE
        for prior in (None, b'{ "approvedResults": [{"key":"earlier","resultHash":"old"}] }\n'):
            for seam in ("journal", "detach"):
                with self.subTest(prior=prior, seam=seam):
                    registry.write_json(
                        root / registry.STATUS_FILE,
                        {"status": "paused", "gateKey": "gate", "gateResultHash": "result"},
                    )
                    if prior is None:
                        approval_path.unlink(missing_ok=True)
                    else:
                        approval_path.write_bytes(prior)
                    target = commands if seam == "journal" else runtime
                    name = "_append_command_event" if seam == "journal" else "detach_supervisor"
                    with (
                        mock.patch.object(
                            target, name, side_effect=OSError("injected launch failure")
                        ),
                        self.assertRaises(OSError),
                    ):
                        commands.emit_run(
                            commands.WorkflowCommand("run", resume=self.pin.workflow_id),
                            workspace=self.workspace,
                            config=self.base,
                            stdout=io.StringIO(),
                            stderr=io.StringIO(),
                        )
                    self.assertEqual(
                        approval_path.read_bytes() if approval_path.exists() else None, prior
                    )

    def test_corrupt_and_injected_attempts_fail_closed(self):
        attempt = self.attempt()
        foreign = self.root / "attempt.json"
        foreign.write_bytes(attempt.path.read_bytes())
        with self.assertRaises(workflow_pinning.WorkflowPinError):
            workflow_attempts.load(foreign, pin=self.pin)
        attempt.config_path.chmod(0o600)
        tampered = copy.deepcopy(attempt.config)
        tampered["cursor"]["defaultModel"] = "tampered"
        attempt.config_path.write_text(json.dumps(tampered))
        with self.assertRaises(workflow_pinning.WorkflowPinError):
            workflow_attempts.load(attempt.path, pin=self.pin)
        forged = copy.deepcopy(attempt.metadata)
        forged["opsValues"]["cursor.defaultModel"] = "tampered"
        forged["opsChangedKeys"].append("cursor.defaultModel")
        forged["opsChangedKeys"].sort()
        target = attempt.path.parent.parent / workflow_pinning._json_digest(forged)
        target.mkdir()
        (target / "attempt.json").write_text(json.dumps(forged))
        (target / "config.json").write_text(json.dumps(tampered))
        with self.assertRaises(workflow_pinning.WorkflowPinError):
            workflow_attempts.load(target / "attempt.json", pin=self.pin)

    def test_symlinked_attempt_storage_is_refused_before_writing(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.pin.path.parent.parent / "attempts").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(workflow_pinning.WorkflowPinError):
            self.attempt()
        self.assertEqual(list(outside.iterdir()), [])

    def test_distinct_attempts_are_immutable_and_isolated(self):
        live = copy.deepcopy(self.base)
        live["workflows"]["itemThreads"] = 2
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(self.attempt, [self.base, live]))
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(workflow_attempts.load(first.path, pin=self.pin).config, first.config)
        self.assertEqual(workflow_attempts.load(second.path, pin=self.pin).config, second.config)
        self.assertEqual(first.config_path.stat().st_mode & 0o777, 0o400)

    def test_supported_supervisor_cannot_silently_fall_back_to_base(self):
        with self.assertRaises(commands.DelegateError) as raised:
            commands.emit(
                commands.WorkflowCommand("_supervise", wf_id=self.pin.workflow_id),
                workspace_path=str(self.workspace),
                config=self.base,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(raised.exception.error, "invalid_workflow_attempt")

    def test_delegate_child_uses_state_snapshot_not_mutated_ambient_environment(self):
        attempt = self.attempt()
        state = runtime.WorkflowState(
            wf_id=self.pin.workflow_id,
            workspace=self.workspace,
            root=registry.ensure_workflow_dir(self.workspace, self.pin.workflow_id),
            script_path=self.workspace / "script.py",
            config=attempt.config,
            cli_argv=self.pin.cli_argv,
            args=None,
            budget=runtime.Budget(None),
            attempt_environment={**self.pin.environment, **attempt.environment},
        )
        with mock.patch.dict(
            os.environ, {"DELEGATE_CONFIG": "missing.json", "DELEGATE_STALL_MINUTES": "999"}
        ):
            child = runtime._run_child_command_for_state(
                [*self.pin.cli_argv, "--json", "models"],
                state=state,
                timeout=10,
            )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)["configSource"], str(attempt.config_path))

    def test_missing_explicit_config_fails_before_workflow_creation(self):
        with (
            mock.patch.dict(os.environ, {"DELEGATE_CONFIG": str(self.root / "missing.json")}),
            self.assertRaises(config.ConfigError),
        ):
            config.load_config()
        self.assertFalse(registry.workflow_root(self.workspace).exists())

    def test_invalid_ops_are_rejected_before_approval_or_status_mutation(self):
        root = registry.ensure_workflow_dir(self.workspace, self.pin.workflow_id)
        (root / registry.SCRIPT_FILE).write_text("return True\n")
        registry.write_json(root / registry.STATUS_FILE, {"status": "paused", "gateKey": "gate"})
        before = (root / registry.STATUS_FILE).read_bytes()
        live = copy.deepcopy(self.base)
        live["workflows"]["itemThreads"] = -1
        with self.assertRaises(commands.DelegateError) as raised:
            commands.emit_approve(
                commands.WorkflowCommand("approve", wf_id=self.pin.workflow_id),
                workspace=self.workspace,
                config=live,
                stdout=io.StringIO(),
            )
        self.assertEqual(raised.exception.error, "invalid_workflows_config")
        self.assertEqual((root / registry.STATUS_FILE).read_bytes(), before)
        self.assertFalse((root / registry.APPROVAL_FILE).exists())

    def test_empty_config_uses_defaults_without_user_config_file(self):
        attempt = workflow_attempts.create(
            self.pin, workflow_attempts.prepare(self.pin, {}, "embedded-default")
        )
        with mock.patch.dict(os.environ, {**self.pin.environment, **attempt.environment}):
            loaded, _ = config.load_config()
        self.assertEqual(loaded["workflows"]["itemThreads"], 64)
        self.assertIsNone(loaded["codex"]["defaultModel"])
        self.assertFalse((self.home / ".delegate" / "config.json").exists())

    def test_new_empty_and_partial_pins_freeze_complete_defaults_once(self):
        for number, raw in enumerate(
            (
                {},
                {"codex": {"defaultModel": None}, "workflows": {"itemThreads": 3}},
                {"workflows": None, "tracking": None},
            )
        ):
            with self.subTest(raw=raw):
                pin = workflow_pinning.create_pin(
                    f"wf_123456abcd{number:02x}",
                    workspace=self.workspace,
                    config=raw,
                )
                before = pin.config_path.read_bytes()
                initial = workflow_attempts.create(
                    pin, workflow_attempts.prepare(pin, {}, "defaults")
                )
                self.assertEqual(pin.attempt_config_version, 1)
                self.assertEqual(initial.config["cursor"], pin.config["cursor"])
                changed_defaults = config.embedded_default_config()
                changed_defaults["cursor"]["defaultModel"] = "later-default"
                changed_defaults["codex"]["defaultModel"] = "later-codex"
                changed_defaults["policy"]["work"]["networkAccess"] = False
                changed_defaults["workflows"]["itemThreads"] = 8
                with mock.patch.object(
                    config, "embedded_default_config", return_value=changed_defaults
                ):
                    later = workflow_attempts.create(
                        pin, workflow_attempts.prepare(pin, {}, "later-defaults")
                    )
                    verified = workflow_pinning.create_pin(
                        pin.workflow_id, workspace=self.workspace, config=raw
                    )
                self.assertEqual(later.config["cursor"], pin.config["cursor"])
                self.assertEqual(later.config["policy"], pin.config["policy"])
                self.assertIsNone(later.config["codex"]["defaultModel"])
                self.assertEqual(later.config["workflows"]["itemThreads"], 8)
                self.assertEqual(pin.config_path.read_bytes(), before)
                self.assertEqual(verified, pin)

    def test_legacy_partial_pin_is_loaded_without_refilling_or_recreating_it(self):
        pin = self.pin
        payload = json.loads(pin.path.read_text())
        payload["runtime"].pop("attemptConfigVersion")
        payload["config"] = {}
        payload["configDigest"] = workflow_pinning._json_digest({})
        for path, value in ((pin.path, payload), (pin.config_path, {})):
            path.chmod(0o600)
            path.write_text(json.dumps(value))
            path.chmod(0o400)
        before = (
            pin.path.read_bytes(),
            pin.config_path.read_bytes(),
            pin.path.parent.stat().st_mode,
        )
        loaded = workflow_pinning.load_pin(pin.workflow_id)
        self.assertEqual(loaded.config, {})
        self.assertEqual(loaded.attempt_config_version, 0)
        with self.assertRaises(workflow_pinning.WorkflowPinError) as raised:
            workflow_pinning.create_pin(pin.workflow_id, workspace=self.workspace, config={})
        self.assertEqual(raised.exception.error, "pin_collision")
        self.assertEqual(
            (pin.path.read_bytes(), pin.config_path.read_bytes(), pin.path.parent.stat().st_mode),
            before,
        )

    def test_real_supervisor_and_fake_child_use_new_ops_after_approve(self):
        live_path = self.root / "live.json"
        live = copy.deepcopy(self.base)
        live["workflows"]["itemThreads"] = 3
        live_path.write_text(json.dumps(live))
        child = self.home / ".delegate" / "workflows" / "child.py"
        child.parent.mkdir(parents=True)
        # The subprocess is a fake work child that imports Delegate's real
        # config loader through the inherited pinned runtime environment.
        child.write_text("""import json, os, subprocess, sys
from delegate_agent.config import load_config
cfg, path = load_config()
code = 'import json; from delegate_agent.config import load_config; c,p=load_config(); print(json.dumps({"threads":c["workflows"]["itemThreads"],"model":c["cursor"]["defaultModel"],"path":p}))'
result = json.loads(subprocess.check_output([sys.executable, '-c', code], text=True))
cli = json.loads(subprocess.check_output([sys.executable, '-m', 'delegate_agent.cli', '--json', 'models'], text=True))
return {"supervisorThreads":cfg["workflows"]["itemThreads"],"supervisorPath":path,"child":result,"cliPath":cli["configSource"],"cliModel":cli["cursor"]["defaultModel"]}
""")
        script = self.workspace / "workflow.py"
        script.write_text(f"return workflow({str(child)!r}, gate=True)\n")
        env = {
            **os.environ,
            "HOME": str(self.home),
            "DELEGATE_CONFIG": str(live_path),
            "DELEGATE_WORKFLOW_NO_DAEMON": "1",
        }

        def call(*args):
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bin" / "delegate.py"),
                    "--cwd",
                    str(self.workspace),
                    "--json",
                    "workflow",
                    *args,
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)

        launch = call("run", str(script))
        wf_id = launch["wfId"]
        first = call("wait", wf_id, "--timeout", "10")["workflow"]
        first_result = first["gateResult"]
        self.assertEqual(first_result["child"]["threads"], 3)
        live["workflows"]["itemThreads"] = 7
        live["cursor"]["defaultModel"] = "must-not-apply"
        live_path.write_text(json.dumps(live))
        call("approve", wf_id)
        second = call("wait", wf_id, "--timeout", "10")["workflow"]
        self.assertEqual(second["attemptConfig"]["opsValues"]["workflows.itemThreads"], 7)
        # Changed gate evidence correctly requires another approval.
        observed = second["gateResult"]
        self.assertEqual(observed["supervisorThreads"], 7)
        self.assertEqual(observed["child"]["threads"], 7)
        self.assertEqual(observed["child"]["model"], "pinned-model")
        self.assertEqual(observed["supervisorPath"], observed["child"]["path"])
        self.assertEqual(observed["cliPath"], observed["supervisorPath"])
        self.assertEqual(observed["cliModel"], "pinned-model")
        self.assertNotEqual(first_result["supervisorPath"], observed["supervisorPath"])
        live["workflows"]["itemThreads"] = 9
        live_path.write_text(json.dumps(live))
        call("run", "--resume", wf_id)
        resumed = call("wait", wf_id, "--timeout", "10")["workflow"]
        self.assertEqual(resumed["gateResult"]["child"]["threads"], 9)
        self.assertEqual(resumed["gateResult"]["cliModel"], "pinned-model")
        self.assertEqual(resumed["attemptConfig"]["opsValues"]["workflows.itemThreads"], 9)


if __name__ == "__main__":
    unittest.main()
