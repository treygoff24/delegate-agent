from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from tests.delegate_commands_test_base import CommandTestBase


class ResumeStaleTests(CommandTestBase):
    def setUp(self):
        super().setUp()
        self.workspace_temp = tempfile.TemporaryDirectory(prefix="delegate-resume-stale-")
        self.addCleanup(self.workspace_temp.cleanup)
        self.workspace = Path(self.workspace_temp.name)
        self.registry_root = self.delegate.run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        Path(self._config_env["DELEGATE_CONFIG"]).write_text("{}", encoding="utf-8")

    def seed_run(self, *, status: str, report: str, snapshot: dict) -> tuple[str, Path]:
        run_id, alias = self.delegate.run_registry.register_run(
            self.registry_root,
            harness="cursor",
            metadata={"engine": "cursor", "mode": "work", "cwd": str(self.workspace)},
        )
        run_path = self.delegate.run_registry.run_directory(self.registry_root, run_id)
        started = "2026-07-31T18:10:00Z"
        self.delegate.run_registry.write_json_atomic(
            run_path / "manifest.json",
            {
                "schema": self.delegate.run_registry.MANIFEST_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "harness": "cursor",
                "engine": "cursor",
                "mode": "work",
                "cwd": str(self.workspace),
                "isolationMode": "none",
                "startedAt": started,
            },
        )
        self.delegate.run_registry.write_json_atomic(
            run_path / "state.json",
            {
                "schema": self.delegate.run_registry.STATE_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "status": status,
                "lastActivityAt": started,
            },
        )
        self.delegate.run_registry.write_json_atomic(
            run_path / "snapshot.json",
            {
                "schema": self.delegate.run_registry.SNAPSHOT_SCHEMA,
                "ok": True,
                "runId": run_id,
                "alias": alias,
                **snapshot,
            },
        )
        self.delegate.run_registry.write_private_text(run_path / "prompt.txt", "original task")
        self.delegate.run_registry.write_private_text(run_path / "completion-report.md", report)
        return alias, run_path

    def build_plan(self, alias: str):
        parsed = self.delegate.parse_cli(
            ["--json", "--cwd", str(self.workspace), "resume", "--dry-run", alias, "continue"]
        )
        config, _source = self.delegate.load_config(workspace=self.workspace)
        return self.delegate.resume_command.build_resume_plan(
            parsed,
            self.delegate.ResolvedWorkspace(str(self.workspace), "directory"),
            config,
            stderr=io.StringIO(),
        )

    def test_terminal_status_uses_completion_report(self):
        alias, _run_path = self.seed_run(
            status="failed",
            report="REPORT IS THE TERMINAL RECORD",
            snapshot={
                "assistantText": "SNAPSHOT MUST NOT REPLACE REPORT",
                "assistantTextTruncated": False,
                "recentEvents": [],
            },
        )

        plan = self.build_plan(alias)
        continuation = plan.parsed.launch.prompt_parts[0]

        self.assertIn("REPORT IS THE TERMINAL RECORD", continuation)
        self.assertNotIn("SNAPSHOT MUST NOT REPLACE REPORT", continuation)
        self.assertIn("BEGIN PRIOR RUN REPORT", continuation)

    def test_blocked_truncate_then_write_finalizer_uses_snapshot_not_partial_report(self):
        alias, run_path = self.seed_run(
            status="running",
            report="PARTIAL REPORT FROM BLOCKED FINALIZER",
            snapshot={
                "assistantText": "SNAPSHOT ASSISTANT TEXT",
                "assistantTextTruncated": True,
                "recentEvents": [
                    {"kind": "tool.started", "tool": "shell", "command": "git status"}
                ],
            },
        )
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["pid"] = 999999
        self.delegate.run_registry.write_json_atomic(run_path / "state.json", state)
        opened = threading.Event()
        release = threading.Event()

        def blocked_finalizer() -> None:
            with (run_path / "completion-report.md").open("w", encoding="utf-8") as report:
                report.truncate(0)
                report.write("PARTIAL REPORT FROM BLOCKED FINALIZER")
                report.flush()
                opened.set()
                release.wait(timeout=5)

        finalizer = threading.Thread(target=blocked_finalizer)
        finalizer.start()
        self.assertTrue(opened.wait(timeout=5))
        try:
            plan = self.build_plan(alias)
        finally:
            release.set()
            finalizer.join(timeout=5)
        continuation = plan.parsed.launch.prompt_parts[0]

        self.assertNotIn("PARTIAL REPORT FROM BLOCKED FINALIZER", continuation)
        self.assertIn("SNAPSHOT ASSISTANT TEXT", continuation)
        self.assertIn("assistant output was truncated", continuation)
        self.assertIn('"kind": "tool.started"', continuation)
        self.assertIn("BEGIN PRIOR RUN DIGEST", continuation)
        self.assertLess(
            continuation.index("SNAPSHOT ASSISTANT TEXT"),
            continuation.index("Continuation instructions from the operator"),
        )


if __name__ == "__main__":
    unittest.main()
