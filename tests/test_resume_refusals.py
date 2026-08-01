import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from delegate_agent import cli, run_registry


class ResumeRefusalTests(unittest.TestCase):
    def _seed(
        self,
        workspace: Path,
        *,
        status: str = "failed",
        mode: str = "work",
        include_prompt: bool = True,
        persistent: bool = False,
    ) -> tuple[str, Path]:
        root = run_registry.ensure_registry(workspace, workspace_kind="directory")
        run_id, alias = run_registry.register_run(
            root,
            harness="cursor",
            metadata={"mode": mode, "cwd": str(workspace)},
        )
        run_path = run_registry.run_directory(root, run_id)
        manifest = {
            "schema": run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": "cursor",
            "engine": "cursor",
            "mode": mode,
            "model": "composer-2.5",
            "cwd": str(workspace),
            "startedAt": "2026-07-31T12:00:00Z",
        }
        last_activity = (
            run_registry.utc_now_iso() if status == "running" else "2026-07-31T12:00:00Z"
        )
        state = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "lastActivityAt": last_activity,
        }
        if status == "running":
            state["pid"] = os.getpid()
        snapshot = {
            "schema": run_registry.SNAPSHOT_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "assistantText": "previous output",
            "recentEvents": [],
        }
        if persistent:
            missing_path = workspace / "missing-worktree"
            manifest.update(
                {
                    "isolationLifecycle": "persistent",
                    "preservedWorkspace": True,
                    "isolationMode": "worktree",
                    "executionCwd": str(missing_path),
                    "sourceGitRoot": str(workspace),
                    "branch": "delegate/cursor-missing",
                    "worktreeStatus": "missing",
                    "creationContext": {"branch": "delegate/cursor-missing"},
                }
            )
            state["worktreeStatus"] = "missing"
        for name, payload in (
            (run_registry.MANIFEST_FILE, manifest),
            (run_registry.STATE_FILE, state),
            (run_registry.SNAPSHOT_FILE, snapshot),
        ):
            run_registry.write_json_atomic(run_path / name, payload)
        if include_prompt:
            run_registry.write_private_text(run_path / run_registry.PROMPT_TXT_FILE, "prompt\n")
        return alias, run_path

    def _assert_refusal(self, workspace: Path, handle: str, expected: str, *extra: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = cli.main(
            ["--json", "--cwd", str(workspace), "resume", handle, *extra],
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 2, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(set(payload), {"ok", "error", "message", "exitCode"})
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], expected)
        self.assertEqual(payload["exitCode"], 2)

    def test_legacy_record_without_prompt_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            alias, _run_path = self._seed(workspace, include_prompt=False)
            self._assert_refusal(workspace, alias, "resume_prompt_unavailable")

    def test_record_invalid_refusal_is_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            alias, run_path = self._seed(workspace)
            run_registry.write_private_text(run_path / run_registry.MANIFEST_FILE, "not json")
            self._assert_refusal(workspace, alias, "resume_record_invalid")

    def test_running_source_refuses_with_running_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            alias, _run_path = self._seed(workspace, status="running")
            self._assert_refusal(workspace, alias, "resume_source_running")

    def test_succeeded_source_requires_extra_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            alias, _run_path = self._seed(workspace, status="succeeded")
            self._assert_refusal(workspace, alias, "resume_requires_instructions")

    def test_call_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            alias, _run_path = self._seed(workspace, mode="call")
            self._assert_refusal(workspace, alias, "resume_call_source")

    def test_unknown_handle_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed(workspace)
            self._assert_refusal(workspace, "does-not-exist", "unknown_handle")

    def test_missing_persistent_worktree_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            alias, _run_path = self._seed(workspace, persistent=True)
            self._assert_refusal(workspace, alias, "worktree_missing")
