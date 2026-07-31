from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from delegate_agent import run_registry, run_status, snapshot_view


class ResumeProjectionTests(unittest.TestCase):
    def seed_run(self, root: Path, *, resumed: bool) -> tuple[str, str, dict, dict]:
        run_id, alias = run_registry.register_run(root, harness="cursor")
        run_path = run_registry.run_directory(root, run_id)
        manifest = {
            "schema": run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": "cursor",
            "engine": "cursor",
            "mode": "work",
            "cwd": str(root.parent),
            "startedAt": "2026-07-31T18:20:00Z",
        }
        if resumed:
            manifest.update(
                {
                    "resumedFrom": {"runId": "del_source", "alias": "cursor-1"},
                    "worktreeAttachment": {
                        "sourceRunId": "del_source",
                        "path": "/private/worktrees/cursor-1",
                    },
                }
            )
        state = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": "succeeded",
            "lastActivityAt": "2026-07-31T18:21:00Z",
        }
        snapshot = {
            "schema": run_registry.SNAPSHOT_SCHEMA,
            "ok": True,
            "runId": run_id,
            "alias": alias,
            "status": "succeeded",
            "assistantText": "done",
            "recentEvents": [],
        }
        run_registry.write_json_atomic(run_path / "manifest.json", manifest)
        run_registry.write_json_atomic(run_path / "state.json", state)
        run_registry.write_json_atomic(run_path / "snapshot.json", snapshot)
        return run_id, alias, manifest, snapshot

    def test_resumed_metadata_is_added_to_runs_summary_and_snapshot_view(self):
        with tempfile.TemporaryDirectory(prefix="delegate-resume-projection-") as tmp:
            workspace = Path(tmp)
            root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, _alias, manifest, snapshot = self.seed_run(root, resumed=True)
            index = run_registry.load_index(root)

            summary = run_status.build_run_summary(root, run_id, index["runs"][run_id])
            view = snapshot_view.merge_snapshot_view(root, run_id, snapshot, redact=False)

        self.assertEqual(summary["resumedFrom"], manifest["resumedFrom"])
        self.assertEqual(summary["worktreeAttachment"], manifest["worktreeAttachment"])
        self.assertEqual(view["resumedFrom"], manifest["resumedFrom"])
        self.assertEqual(view["worktreeAttachment"], manifest["worktreeAttachment"])

    def test_ordinary_runs_do_not_gain_resume_projection_keys(self):
        with tempfile.TemporaryDirectory(prefix="delegate-ordinary-projection-") as tmp:
            workspace = Path(tmp)
            root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, _alias, _manifest, snapshot = self.seed_run(root, resumed=False)
            index = run_registry.load_index(root)

            summary = run_status.build_run_summary(root, run_id, index["runs"][run_id])
            view = snapshot_view.merge_snapshot_view(root, run_id, snapshot, redact=False)

        self.assertNotIn("resumedFrom", summary)
        self.assertNotIn("worktreeAttachment", summary)
        self.assertNotIn("resumedFrom", view)
        self.assertNotIn("worktreeAttachment", view)


if __name__ == "__main__":
    unittest.main()
