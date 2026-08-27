from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from delegate_agent import workflow_pinning  # noqa: E402
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
