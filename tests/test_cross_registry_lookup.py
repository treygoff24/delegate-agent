"""dlg-3pl.7: handles resolve across known registries, or say where the run lives."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import cli, run_registry  # noqa: E402
from delegate_agent.workflows import registry as workflow_registry  # noqa: E402
from tests.delegate_fixtures import write_snapshot_run  # noqa: E402


class CrossRegistryLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        home = self.base / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = self.make_workspace("owner")
        self.elsewhere = self.make_workspace("elsewhere")

    def make_workspace(self, name: str) -> Path:
        workspace = self.base / name
        workspace.mkdir()
        run_registry.ensure_registry(workspace, workspace_kind="directory")
        return workspace

    def write_run(self, workspace: Path, *, status: str = "succeeded") -> tuple[str, str]:
        root = run_registry.registry_root(workspace)
        return write_snapshot_run(run_registry, root, workspace, harness="codex", status=status)

    def run_cli(self, cwd: Path, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli.main(["--cwd", str(cwd), "--json", *argv], stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_snapshot_reads_a_unique_run_id_from_the_registry_that_holds_it(self) -> None:
        run_id, alias = self.write_run(self.owner)

        code, out, err = self.run_cli(self.elsewhere, "snapshot", run_id)

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["runId"], run_id)
        self.assertEqual(payload["alias"], alias)
        self.assertEqual(payload["resolutionKind"], "cross_registry")
        self.assertEqual(payload["resolvedWorkspace"], str(self.owner))
        self.assertTrue(any(w.startswith("cross_registry:") for w in payload["warnings"]))

    def test_run_output_reads_a_unique_run_id_from_the_registry_that_holds_it(self) -> None:
        run_id, _alias = self.write_run(self.owner)
        stdout_log = run_registry.run_directory(run_registry.registry_root(self.owner), run_id)
        (stdout_log / "stdout.log").write_text("owner-side output\n", encoding="utf-8")

        code, out, err = self.run_cli(
            self.elsewhere, "run-output", run_id, "--stdout", "--tail", "5"
        )

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["runId"], run_id)
        self.assertIn("owner-side output", json.dumps(payload))
        self.assertEqual(payload["resolutionKind"], "cross_registry")
        self.assertEqual(payload["resolvedWorkspace"], str(self.owner))

    def test_a_workspace_with_no_registry_still_finds_the_run(self) -> None:
        run_id, _alias = self.write_run(self.owner)
        bare = self.base / "bare"
        bare.mkdir()

        code, out, err = self.run_cli(bare, "snapshot", run_id)

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["resolvedWorkspace"], str(self.owner))

    def test_an_alias_is_never_resolved_across_registries(self) -> None:
        _run_id, alias = self.write_run(self.owner)

        code, out, _err = self.run_cli(self.elsewhere, "snapshot", alias)

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        self.assertIn(str(self.owner), payload["message"])
        self.assertIn("not unique", payload["message"])
        self.assertNotIn("runId", payload)

    def test_an_alias_held_by_several_registries_lists_all_of_them(self) -> None:
        _first_id, alias = self.write_run(self.owner)
        second = self.make_workspace("second")
        _second_id, second_alias = self.write_run(second)
        self.assertEqual(alias, second_alias)

        code, out, _err = self.run_cli(self.elsewhere, "snapshot", alias)

        self.assertNotEqual(code, 0)
        message = json.loads(out)["message"]
        self.assertIn(str(self.owner), message)
        self.assertIn(str(second), message)

    def test_wait_names_the_workspace_and_the_exact_cwd_command(self) -> None:
        run_id, _alias = self.write_run(self.owner)

        code, out, _err = self.run_cli(self.elsewhere, "wait", run_id, "--timeout", "2")

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        expected = f"delegate --cwd {self.owner} wait {run_id}"
        self.assertIn(expected, payload["message"])
        self.assertEqual(payload["nextActions"][0], expected)

    def test_cancel_never_follows_a_run_id_into_another_registry(self) -> None:
        run_id, _alias = self.write_run(self.owner)

        code, out, _err = self.run_cli(self.elsewhere, "cancel", run_id)

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        self.assertIn(f"delegate --cwd {self.owner} cancel {run_id}", payload["message"])

    def test_a_run_id_recorded_in_two_registries_is_not_guessed(self) -> None:
        run_id, _alias = self.write_run(self.owner)
        copy = self.make_workspace("copy")
        shutil.copytree(
            run_registry.registry_root(self.owner),
            run_registry.registry_root(copy),
            dirs_exist_ok=True,
        )

        code, out, _err = self.run_cli(self.elsewhere, "snapshot", run_id)

        self.assertNotEqual(code, 0)
        message = json.loads(out)["message"]
        self.assertIn(str(self.owner), message)
        self.assertIn(str(copy), message)

    def test_an_unknown_run_id_keeps_the_plain_error(self) -> None:
        self.write_run(self.owner)

        code, out, _err = self.run_cli(self.elsewhere, "snapshot", "del_20200101T000000Z_abcdef")

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        self.assertNotIn("recorded in workspace", payload["message"])
        self.assertFalse(any("--cwd" in action for action in payload.get("nextActions", [])))

    def test_a_deleted_registry_is_not_offered(self) -> None:
        run_id, _alias = self.write_run(self.owner)
        shutil.rmtree(run_registry.registry_root(self.owner))

        code, out, _err = self.run_cli(self.elsewhere, "snapshot", run_id)

        self.assertNotEqual(code, 0)
        self.assertNotIn(str(self.owner), json.loads(out)["message"])

    def test_followup_from_the_wrong_workspace_names_the_owner(self) -> None:
        run_id, _alias = self.write_run(self.owner)

        code, out, _err = self.run_cli(self.elsewhere, "followup", run_id, "keep going")

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        self.assertIn(f"delegate --cwd {self.owner} followup {run_id}", payload["message"])

    def test_resume_from_the_wrong_workspace_names_the_owner(self) -> None:
        run_id, _alias = self.write_run(self.owner)

        code, out, _err = self.run_cli(self.elsewhere, "resume", run_id, "keep going")

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        self.assertIn(f"delegate --cwd {self.owner} resume {run_id}", payload["message"])

    def test_followup_from_a_workspace_without_a_registry_names_the_owner(self) -> None:
        run_id, _alias = self.write_run(self.owner)
        bare = self.base / "bare"
        bare.mkdir()

        code, out, _err = self.run_cli(bare, "followup", run_id, "keep going")

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        self.assertIn(f"delegate --cwd {self.owner} followup {run_id}", payload["message"])

    def test_resume_from_a_workspace_without_a_registry_names_the_owner(self) -> None:
        run_id, _alias = self.write_run(self.owner)
        bare = self.base / "bare"
        bare.mkdir()

        code, out, _err = self.run_cli(bare, "resume", run_id, "keep going")

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_handle")
        self.assertIn(f"delegate --cwd {self.owner} resume {run_id}", payload["message"])

    def test_worktree_show_from_the_wrong_workspace_names_the_owner(self) -> None:
        run_id, _alias = self.write_run(self.owner)

        code, out, _err = self.run_cli(self.elsewhere, "worktree", "show", run_id)

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["code"], "unknown_handle")
        self.assertIn(f"delegate --cwd {self.owner} worktree show {run_id}", payload["message"])


class CrossRegistryWorkflowTests(unittest.TestCase):
    WF_ID = "wf_0123456789ab"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        home = self.base / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = self.base / "owner"
        self.owner.mkdir()
        self.elsewhere = self.base / "elsewhere"
        self.elsewhere.mkdir()
        root = workflow_registry.ensure_workflow_dir(self.owner, self.WF_ID)
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {"wfId": self.WF_ID, "status": "succeeded", "budget": {"total": None, "spent": 0}},
        )
        workflow_registry.write_json(
            root / workflow_registry.RESULT_FILE,
            {"ok": True, "wfId": self.WF_ID, "result": {"summary": "done"}},
        )

    def run_cli(self, cwd: Path, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli.main(["--cwd", str(cwd), "--json", *argv], stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_reads_a_workflow_stored_in_another_known_workspace(self) -> None:
        code, out, err = self.run_cli(self.elsewhere, "workflow", "status", self.WF_ID)

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["wfId"], self.WF_ID)
        self.assertEqual(payload["resolvedWorkspace"], str(self.owner))

    def test_result_and_wait_read_a_workflow_stored_in_another_known_workspace(self) -> None:
        code, out, err = self.run_cli(self.elsewhere, "workflow", "result", self.WF_ID)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["result"], {"summary": "done"})
        self.assertEqual(result["resolvedWorkspace"], str(self.owner))

        code, out, err = self.run_cli(self.elsewhere, "workflow", "wait", self.WF_ID)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["resolvedWorkspace"], str(self.owner))

    def test_mutating_actions_name_the_workspace_and_the_exact_command(self) -> None:
        code, out, _err = self.run_cli(self.elsewhere, "workflow", "kill", self.WF_ID)

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "workflow_not_found")
        expected = f"delegate --cwd {self.owner} workflow kill {self.WF_ID}"
        self.assertIn(expected, payload["message"])
        self.assertEqual(payload["nextActions"], [expected])

    def test_resume_names_the_workspace_and_the_exact_command(self) -> None:
        code, out, _err = self.run_cli(self.elsewhere, "workflow", "run", "--resume", self.WF_ID)

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "workflow_not_found")
        self.assertIn(str(self.owner), payload["message"])

    def test_an_unknown_workflow_id_keeps_the_plain_error(self) -> None:
        code, out, _err = self.run_cli(self.elsewhere, "workflow", "status", "wf_ffffffffffff")

        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "workflow_not_found")
        self.assertNotIn("recorded in workspace", payload["message"])


if __name__ == "__main__":
    unittest.main()
