import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import run_registry, snapshot_view  # noqa: E402


class SnapshotViewTests(unittest.TestCase):
    def test_merge_snapshot_view_writes_schema_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, alias = run_registry.register_run(registry_root, harness="cursor")
            run_path = run_registry.run_directory(registry_root, run_id)
            run_registry.write_json_atomic(
                run_path / "state.json",
                {
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "status": "running",
                    "lastActivityAt": "2026-05-20T12:00:00Z",
                },
            )
            run_registry.write_json_atomic(
                run_path / "manifest.json",
                {
                    "schema": run_registry.MANIFEST_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "harness": "cursor",
                    "cwd": str(workspace),
                    "mode": "work",
                    "model": "composer-2.5",
                    "startedAt": "2026-05-20T12:00:00Z",
                },
            )
            view = snapshot_view.merge_snapshot_view(
                registry_root,
                run_id,
                {
                    "schema": run_registry.SNAPSHOT_SCHEMA,
                    "ok": True,
                    "runId": "stale-run-id",
                    "completionReport": {"path": "completion-report.md"},
                },
                redact=False,
            )

        self.assertEqual(view["runId"], run_id)
        self.assertEqual(view["alias"], alias)
        self.assertEqual(view["rawStatus"], "running")
        self.assertEqual(view["effectiveStatus"], "stale")
        self.assertEqual(view["status"], "stale")
        self.assertEqual(view["staleReason"], "missing_pid")
        self.assertEqual(
            view["snapshotCommand"],
            run_registry.snapshot_command(alias, cwd=str(workspace)),
        )
        self.assertIn(
            run_registry.run_output_command(alias, completion_report=True, cwd=str(workspace)),
            view["nextActions"],
        )
        self.assertEqual(
            view["completionReport"]["command"],
            run_registry.run_output_command(alias, completion_report=True, cwd=str(workspace)),
        )

    def test_merge_snapshot_view_emits_failure_and_metadata_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, alias = run_registry.register_run(registry_root, harness="codex")
            run_path = run_registry.run_directory(registry_root, run_id)
            cleanup = {
                "removeWorktree": "delegate worktree remove run-123",
                "keepWorktree": "delegate worktree remove run-123 --keep-branch",
            }
            run_registry.write_json_atomic(
                run_path / "state.json",
                {
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "status": "failed",
                    "lastActivityAt": "2026-05-20T12:01:00Z",
                    "current": "creating persistent worktree",
                    "error": "branch_collision",
                    "message": "Branch already exists.",
                    "plannedBranch": "delegate/codex-run-123",
                    "plannedExecutionCwd": str(workspace / "worktrees" / "run-123"),
                    "worktreeStatus": "missing",
                    "safeWorkspaceMethod": "git-worktree",
                },
            )
            run_registry.write_json_atomic(
                run_path / "manifest.json",
                {
                    "schema": run_registry.MANIFEST_SCHEMA,
                    "runId": run_id,
                    "alias": alias,
                    "harness": "codex",
                    "cwd": str(workspace),
                    "executionCwd": str(workspace / "worktrees" / "run-123"),
                    "mode": "work",
                    "model": "gpt-5.4",
                    "startedAt": "2026-05-20T12:00:00Z",
                    "isolationMode": "worktree",
                    "effectiveIsolation": "worktree",
                    "isolationLifecycle": "persistent",
                    "preservedWorkspace": True,
                    "sourceGitRoot": str(workspace),
                    "branch": "delegate/codex-run-123",
                    "creationContext": {"plannedBranch": "delegate/codex-run-123"},
                    "worktreeCleanupCommands": cleanup,
                    "requestedReasoningEffort": "high",
                    "resolvedReasoningEffort": "high",
                    "reasoningEffortSource": "cli",
                    "reasoningCapabilitySource": "cache",
                    "reasoningTransport": "native",
                },
            )

            view = snapshot_view.merge_snapshot_view(
                registry_root,
                run_id,
                None,
                redact=False,
            )

        self.assertEqual(view["runId"], run_id)
        self.assertEqual(view["alias"], alias)
        self.assertEqual(view["current"], "creating persistent worktree")
        self.assertEqual(view["error"], "branch_collision")
        self.assertEqual(view["message"], "Branch already exists.")
        self.assertEqual(view["plannedBranch"], "delegate/codex-run-123")
        self.assertEqual(
            view["plannedExecutionCwd"],
            str(workspace / "worktrees" / "run-123"),
        )
        self.assertEqual(view["isolationLifecycle"], "persistent")
        self.assertEqual(view["sourceGitRoot"], str(workspace))
        self.assertEqual(view["worktreeStatus"], "missing")
        self.assertEqual(view["safeWorkspaceMethod"], "git-worktree")
        self.assertEqual(view["resolvedReasoningEffort"], "high")
        self.assertEqual(view["reasoningTransport"], "native")
        self.assertEqual(view["worktreeCleanupCommands"], cleanup)


if __name__ == "__main__":
    unittest.main()
