from __future__ import annotations

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
sys.path.insert(0, str(ROOT / "src"))

from delegate_agent import profiles, safe_workspace, workflow_pinning  # noqa: E402
from delegate_agent.workflows import registry as workflow_registry  # noqa: E402


class WorkflowPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".delegate" / "personas").mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

    def _restore_home(self) -> None:
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    def test_create_pin_is_content_addressed_and_excludes_secret_config(self) -> None:
        persona = self.home / ".delegate" / "personas" / "reviewer.md"
        persona.write_text("Review only.\n", encoding="utf-8")
        config = {
            "codex": {"binary": "/bin/codex", "defaultModel": "safe"},
            "credentials": {"apiKey": "must-not-be-copied"},
            "policy": {"profile": "safe"},
        }
        pool = self.home / ".delegate" / "worktrees"
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config=config,
            data_home=pool,
            home=self.home,
        )

        self.assertTrue(pin.path.is_file())
        self.assertFalse(pin.path.is_relative_to(pool))
        self.assertTrue(pin.runtime_root.is_dir())
        self.assertTrue(pin.import_root.is_dir())
        self.assertEqual(pin.personas["reviewer"]["text"], "Review only.\n")
        payload = json.loads(pin.path.read_text(encoding="utf-8"))
        self.assertNotIn("credentials", payload["config"])
        self.assertEqual(payload["config"]["codex"]["binary"], "/bin/codex")
        self.assertEqual(pin.path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(pin.config_path.stat().st_mode & 0o777, 0o400)

    def test_applying_pin_environment_twice_does_not_duplicate_pythonpath(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        user_pythonpath = str(self.root / "user-pythonpath")

        with mock.patch.dict(os.environ, {"PYTHONPATH": user_pythonpath}, clear=False):
            workflow_pinning.temporarily_apply_environment(pin)
            applied_once = os.environ["PYTHONPATH"]
            workflow_pinning.temporarily_apply_environment(pin)
            applied_twice = os.environ["PYTHONPATH"]

        self.assertEqual(applied_once, os.pathsep.join((str(pin.import_root), user_pythonpath)))
        self.assertEqual(applied_twice, applied_once)

    def test_engine_child_environment_scrubs_pin_without_poisoning_python(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        user_pythonpath = self.root / "user-pythonpath"
        similar_prefix = self.home / f"{workflow_pinning.PIN_ROOT_DIRNAME}-user"
        user_pythonpath.mkdir()
        similar_prefix.mkdir()
        child_pythonpath = os.pathsep.join(
            (
                str(pin.import_root),
                str(pin.runtime_root),
                str(user_pythonpath),
                str(similar_prefix),
            )
        )

        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "DELEGATE_CONFIG": str(pin.config_path),
            },
            clear=True,
        ):
            child_env = profiles.child_environment(
                overrides={
                    "DELEGATE_WORKFLOW_PIN": str(pin.path),
                    "DELEGATE_WORKFLOW_LOCK_FD": "123",
                    "PYTHONPATH": child_pythonpath,
                }
            )

        self.assertNotIn("DELEGATE_WORKFLOW_PIN", child_env)
        self.assertNotIn("DELEGATE_WORKFLOW_LOCK_FD", child_env)
        self.assertEqual(child_env["DELEGATE_CONFIG"], str(pin.config_path))
        self.assertEqual(
            child_env["PYTHONPATH"],
            os.pathsep.join((str(user_pythonpath), str(similar_prefix))),
        )

        probe = subprocess.run(
            [sys.executable, "-c", "import sys; print('delegate_agent' in sys.modules)"],
            cwd=self.workspace,
            env=child_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "False")

    def test_pinned_delegate_cli_child_passes_persona_resolver_guard(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        probe = subprocess.run(
            [*pin.cli_argv, "--json", "describe"],
            cwd=self.workspace,
            env={**os.environ, **pin.environment},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertNotIn("workflow_persona_pin_unavailable", probe.stderr)

    def test_safe_workspace_cleanup_preserves_pin_store_modes(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        pinned_paths = (pin.runtime_root, pin.import_root, pin.entrypoint)
        source_modes = {path: path.stat().st_mode & 0o777 for path in pinned_paths}

        copy_path, temp_base = safe_workspace.create_directory_safe_workspace(str(pin.runtime_root))
        self.assertEqual(Path(copy_path).stat().st_mode & 0o777, 0o500)
        safe_workspace.cleanup_safe_isolated_workspace(
            git_root=None,
            isolated_workspace=copy_path,
            temp_base=temp_base,
            source_root=str(pin.runtime_root),
        )

        self.assertFalse(Path(temp_base).exists())
        self.assertEqual(
            {path: path.stat().st_mode & 0o777 for path in pinned_paths},
            source_modes,
        )

    def test_pinned_runtime_and_persona_are_used_by_child_imports(self) -> None:
        persona = self.home / ".delegate" / "personas" / "reviewer.md"
        persona.write_text("Pinned bytes\n", encoding="utf-8")
        pin = workflow_pinning.create_pin(
            "wf_abcdefabcdef",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        persona.write_text("Live bytes\n", encoding="utf-8")
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from delegate_agent.personas import resolve_persona; print(resolve_persona('.', 'reviewer').text, end='')",
            ],
            env={
                **os.environ,
                "DELEGATE_WORKFLOW_PIN": str(pin.path),
                "PYTHONPATH": str(pin.import_root),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout, "Pinned bytes\n")

    def test_safe_workspace_persona_never_falls_through_to_live_home(self) -> None:
        workspace_persona = self.workspace / ".delegate" / "personas" / "reviewer.md"
        workspace_persona.parent.mkdir(parents=True)
        workspace_persona.write_text("Pinned workspace bytes\n", encoding="utf-8")
        live_persona = self.home / ".delegate" / "personas" / "reviewer.md"
        live_persona.write_text("LIVE HOME BYTES\n", encoding="utf-8")
        pin = workflow_pinning.create_pin(
            "wf_123456789abc",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        workspace_persona.unlink()
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from delegate_agent.errors import DelegateError; "
                "from delegate_agent.personas import resolve_persona; "
                "\ntry:\n"
                " print(resolve_persona('.', 'reviewer', mode='safe').text, end='')\n"
                "except DelegateError as exc:\n"
                " print(exc.error)",
            ],
            cwd=self.workspace,
            env={
                **os.environ,
                "DELEGATE_WORKFLOW_PIN": str(pin.path),
                "PYTHONPATH": str(pin.import_root),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "workspace_persona_refused")
        self.assertNotIn("LIVE HOME BYTES", probe.stdout)

    def test_cli_hard_exits_when_pinned_resolver_installation_is_missing(self) -> None:
        live_persona = self.home / ".delegate" / "personas" / "reviewer.md"
        live_persona.write_text("LIVE HOME BYTES\n", encoding="utf-8")
        pin = workflow_pinning.create_pin(
            "wf_456789abcdef",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        sitecustomize = pin.import_root / "sitecustomize.py"
        sitecustomize.chmod(0o600)
        sitecustomize.write_text('"""Deliberately leaves the pin resolver uninstalled."""\n')

        probe_code = (
            "from delegate_agent.cli import main; "
            "from delegate_agent.personas import resolve_persona; "
            "print(resolve_persona('.', 'reviewer').text, end='')"
        )
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PYTHONPATH": str(pin.import_root),
        }
        pinned = subprocess.run(
            [sys.executable, "-c", probe_code],
            cwd=self.workspace,
            env={**env, "DELEGATE_WORKFLOW_PIN": str(pin.path)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(pinned.returncode, 0)
        self.assertEqual(pinned.stdout, "")
        self.assertNotIn("LIVE HOME BYTES", pinned.stderr)

        pinless = subprocess.run(
            [sys.executable, "-c", probe_code],
            cwd=self.workspace,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(pinless.returncode, 0, pinless.stderr)
        self.assertEqual(pinless.stdout, "LIVE HOME BYTES\n")

    def test_partial_runtime_temporary_directory_is_rebuilt_before_publish(self) -> None:
        files = workflow_pinning._runtime_source_files()
        digest = workflow_pinning._runtime_digest(files)
        partial = self.home / workflow_pinning.PIN_ROOT_DIRNAME / workflow_pinning.RUNTIME_DIR
        partial = partial / f"{digest}.tmp"
        (partial / "src").mkdir(parents=True)
        (partial / "src" / "partial.py").write_text("partial", encoding="utf-8")

        pin = workflow_pinning.create_pin(
            "wf_23456789abcd",
            workspace=self.workspace,
            config={},
            home=self.home,
        )

        self.assertTrue(pin.entrypoint.is_file())
        self.assertFalse(partial.exists())

    def test_active_supervisor_index_reconciles_stale_entries(self) -> None:
        workspace_root = self.workspace / ".delegate" / "workflows" / "wf_0123456789ab"
        workspace_root.mkdir(parents=True)
        lock_fd = workflow_registry.acquire_workflow_lock(workspace_root)
        try:
            pin = workflow_pinning.create_pin(
                "wf_0123456789ab",
                workspace=self.workspace,
                config={},
                home=self.home,
            )
            workflow_pinning.register_active_supervisor(
                "wf_0123456789ab",
                workflow_root=workspace_root,
                workspace=self.workspace,
                pin=pin,
                home=self.home,
            )
            live = workflow_pinning.reconcile_active_supervisors(home=self.home)
            self.assertIn("wf_0123456789ab", live["supervisors"])
        finally:
            os.close(lock_fd)
        stale = workflow_pinning.reconcile_active_supervisors(home=self.home)
        self.assertNotIn("wf_0123456789ab", stale["supervisors"])

    def test_concurrent_supervisor_registration_preserves_both_entries(self) -> None:
        writer = """
import os
import sys
import time
from pathlib import Path
from delegate_agent import workflow_pinning
from delegate_agent.workflows import registry as workflow_registry

home, workspace, workflow_id = map(Path, sys.argv[1:])
root = workspace / '.delegate' / 'workflows' / str(workflow_id)
root.mkdir(parents=True)
lock_fd = workflow_registry.acquire_workflow_lock(root)
try:
    pin = workflow_pinning.create_pin(str(workflow_id), workspace=workspace, config={}, home=home)
    workflow_pinning.register_active_supervisor(
        str(workflow_id), workflow_root=root, workspace=workspace, pin=pin, home=home
    )
    print('ready', flush=True)
    time.sleep(2)
finally:
    os.close(lock_fd)
"""
        processes = []
        for workflow_id in ("wf_0123456789ab", "wf_abcdefabcdef"):
            workspace = self.root / workflow_id
            workspace.mkdir()
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", writer, str(self.home), str(workspace), workflow_id],
                    env={**os.environ, "PYTHONPATH": SRC},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        try:
            self.assertEqual(processes[0].stdout.readline().strip(), "ready")
            self.assertEqual(processes[1].stdout.readline().strip(), "ready")
            index = workflow_pinning.reconcile_active_supervisors(home=self.home)
            self.assertEqual(set(index["supervisors"]), {"wf_0123456789ab", "wf_abcdefabcdef"})
        finally:
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")

    def test_doctor_and_promote_share_machine_local_surface(self) -> None:
        stamp = workflow_pinning.promote(
            actor="test",
            runtime_digest="a" * 64,
            source="unit-test",
            home=self.home,
        )
        report = workflow_pinning.doctor(home=self.home)
        self.assertEqual(report["promotion"], stamp)
        self.assertEqual(report["schema"], workflow_pinning.ACTIVE_INDEX_SCHEMA)


if __name__ == "__main__":
    unittest.main()
