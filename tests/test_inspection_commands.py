import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.delegate_fixtures import write_snapshot_run

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import (  # noqa: E402
    command_errors,
    inspection_commands,
    run_registry,
)


class InspectionCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def write_run(
        self,
        *,
        harness: str = "cursor",
        status: str = "running",
        assistant_text: str = "planning the change",
        pid: int | None = os.getpid(),
        started_at: str | None = None,
    ) -> tuple[str, str]:
        return write_snapshot_run(
            run_registry,
            self.registry_root,
            self.workspace,
            harness=harness,
            status=status,
            assistant_text=assistant_text,
            pid=pid,
            started_at=started_at,
        )

    def test_resolve_latest_harness_returns_latest_alias(self):
        self.write_run(
            harness="codex",
            assistant_text="first",
            started_at="2026-05-20T12:00:00Z",
        )
        latest_run_id, latest_alias = self.write_run(
            harness="codex",
            assistant_text="second",
            started_at="2026-05-20T12:05:00Z",
        )

        run_id, alias = command_errors.resolve_run_target(
            self.registry_root,
            handle=None,
            latest_harness="codex",
            error_cls=inspection_commands.InspectionError,
        )

        self.assertEqual(run_id, latest_run_id)
        self.assertEqual(alias, latest_alias)

    def test_emit_snapshot_json_uses_registry_view(self):
        run_id, alias = self.write_run(assistant_text="adapter output")
        stdout = io.StringIO()

        code = inspection_commands.emit_snapshot(
            inspection_commands.SnapshotCommand(handle=alias, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], run_registry.SNAPSHOT_SCHEMA)
        self.assertEqual(payload["runId"], run_id)
        self.assertEqual(payload["alias"], alias)
        self.assertEqual(payload["assistantText"], "adapter output")

    def test_emit_runs_json_filters_and_redacts_summaries(self):
        _, alias = self.write_run(assistant_text="adapter output")
        index = run_registry.load_index(self.registry_root)
        run_id = index["aliases"][alias]
        run_path = run_registry.run_directory(self.registry_root, run_id)
        state = run_registry.load_run_state(self.registry_root, run_id)
        state["current"] = "checking token=super-secret-value"
        run_registry.write_json_atomic(run_path / "state.json", state)
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(running=True, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "running")
        self.assertEqual(payload["runs"][0]["alias"], alias)
        self.assertIn("token=", payload["runs"][0]["current"])
        self.assertNotIn("super-secret-value", payload["runs"][0]["current"])

    def test_missing_registry_latest_snapshot_raises_inspection_error(self):
        isolated_dir = tempfile.TemporaryDirectory()
        self.addCleanup(isolated_dir.cleanup)
        isolated = Path(isolated_dir.name)

        with self.assertRaises(inspection_commands.InspectionError) as caught:
            inspection_commands.emit_snapshot(
                inspection_commands.SnapshotCommand(handle=None, latest_harness="droid"),
                workspace_path=str(isolated),
                stdout=io.StringIO(),
            )

        self.assertEqual(caught.exception.error, "no_matching_runs")
        self.assertEqual(caught.exception.message, "No runs found for harness: droid")

    def test_missing_registry_snapshot_unknown_handle_message_comes_from_resolver(self):
        isolated_dir = tempfile.TemporaryDirectory()
        self.addCleanup(isolated_dir.cleanup)
        isolated = Path(isolated_dir.name)

        with self.assertRaises(inspection_commands.InspectionError) as caught:
            inspection_commands.emit_snapshot(
                inspection_commands.SnapshotCommand(handle="missing"),
                workspace_path=str(isolated),
                stdout=io.StringIO(),
            )

        self.assertEqual(caught.exception.error, "unknown_handle")
        self.assertEqual(
            caught.exception.message,
            "Unknown run handle: missing. Suggestions: (none)",
        )

    def test_missing_registry_snapshot_missing_handle_keeps_snapshot_message(self):
        isolated_dir = tempfile.TemporaryDirectory()
        self.addCleanup(isolated_dir.cleanup)
        isolated = Path(isolated_dir.name)

        with self.assertRaises(inspection_commands.InspectionError) as caught:
            inspection_commands.emit_snapshot(
                inspection_commands.SnapshotCommand(handle=None),
                workspace_path=str(isolated),
                stdout=io.StringIO(),
            )

        self.assertEqual(caught.exception.error, "missing_handle")
        self.assertEqual(
            caught.exception.message,
            "snapshot requires a run handle or --latest.",
        )


if __name__ == "__main__":
    unittest.main()
