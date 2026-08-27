"""End-to-end followup coverage after a structured retry in a worktree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "delegate.py"


class FollowupAfterRetryE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-followup-retry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.bin_dir = self.root / "bin"
        self.home.mkdir()
        self.workspace.mkdir()
        self.bin_dir.mkdir()
        self._init_repo()
        self._write_fake_codex()
        self.config_path = self.home / "delegate-config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "codex": {
                        "binary": str(self.bin_dir / "codex"),
                        "defaultModel": "gpt-5",
                        "models": {"gpt-5": "gpt-5"},
                        "workSandbox": "workspace-write",
                        "ephemeral": True,
                    },
                    "workflows": {"structuredOutputRetries": 1, "itemThreads": 1},
                }
            ),
            encoding="utf-8",
        )
        self.workspace_log = self.root / "execution-cwds.log"
        self.attempt_file = self.root / "attempts.txt"

    def _init_repo(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", str(self.workspace)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.name", "Delegate Tests"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "config",
                "user.email",
                "delegate-tests@example.invalid",
            ],
            capture_output=True,
            check=True,
        )
        (self.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.workspace), "add", "tracked.txt"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-qm", "base"],
            capture_output=True,
            check=True,
        )

    def _write_fake_codex(self) -> None:
        path = self.bin_dir / "codex"
        path.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env python3
                import json
                import os
                import sys

                log = os.environ.get("FAKE_CODEX_WORKSPACE_LOG")
                if log:
                    with open(log, "a", encoding="utf-8") as handle:
                        handle.write(os.getcwd() + "\\n")
                print(json.dumps({"type": "thread.started", "thread_id": "thread-followup-retry"}))
                prompt = sys.stdin.read()
                if "--output-schema" in sys.argv:
                    attempts_path = os.environ["FAKE_CODEX_ATTEMPTS"]
                    try:
                        attempt = int(open(attempts_path, encoding="utf-8").read() or "0") + 1
                    except FileNotFoundError:
                        attempt = 1
                    with open(attempts_path, "w", encoding="utf-8") as handle:
                        handle.write(str(attempt))
                    text = '{"ok": "wrong"}' if attempt == 1 else json.dumps({"ok": True, "value": os.getcwd()})
                else:
                    text = "followup cwd: " + os.getcwd()
                print(json.dumps({"type": "message", "role": "assistant", "content": text}))
                print(json.dumps({"type": "completion", "finalText": text}))
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _run_delegate(
        self, args: list[str], *, env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "DELEGATE_CONFIG": str(self.config_path),
                "DELEGATE_WORKFLOW_NO_DAEMON": "1",
                "HOME": str(self.home),
                "FAKE_CODEX_WORKSPACE_LOG": str(self.workspace_log),
                "FAKE_CODEX_ATTEMPTS": str(self.attempt_file),
                **(env_extra or {}),
            }
        )
        return subprocess.run(
            [sys.executable, str(CLI), "--cwd", str(self.workspace), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=40,
            check=False,
        )

    def _write_workflow(self) -> Path:
        path = self.root / "workflow.py"
        path.write_text(
            textwrap.dedent(
                """
                meta = {"name": "followup-after-retry", "defaults": {"engine": "codex", "mode": "work"}}
                SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
                first = agent("inspect the repository", label="retry-child", schema=SCHEMA, retries=1, isolation="worktree", resumable=True)
                second = followup("retry-child", "continue after the structured result", label="followup-child")
                return {"first": first, "second": second}
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_followup_after_schema_retry_stays_on_persistent_worktree(self) -> None:
        workflow = self._write_workflow()
        launch = self._run_delegate(["--json", "workflow", "run", str(workflow)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self._run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "20"])
        self.assertEqual(waited.returncode, 0, waited.stderr)

        result = self._run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(result.returncode, 0, result.stderr)
        result_payload = json.loads(result.stdout)
        self.assertEqual(
            result_payload["result"]["first"]["value"],
            result_payload["result"]["second"].split(": ", 1)[1],
        )

        events_payload = json.loads(
            self._run_delegate(["--json", "workflow", "events", wf_id]).stdout
        )
        child_events = [
            event for event in events_payload["events"] if event.get("type") == "agent_child"
        ]
        self.assertGreaterEqual(len(child_events), 3)
        first_event, retry_event, followup_event = child_events[:3]
        self.assertEqual(first_event["label"], "retry-child")
        self.assertEqual(retry_event["label"], "retry-child")
        self.assertEqual(followup_event["label"], "followup-child")

        runs_payload = json.loads(self._run_delegate(["--json", "runs", "--group", wf_id]).stdout)
        manifests = {}
        for run in runs_payload["runs"]:
            run_id = run["runId"]
            manifest_path = self.workspace / ".delegate" / "runs" / run_id / "manifest.json"
            if manifest_path.is_file():
                manifests[run_id] = json.loads(manifest_path.read_text(encoding="utf-8"))
        first_manifest = manifests[first_event["runId"]]
        retry_manifest = manifests[retry_event["runId"]]
        followup_manifest = manifests[followup_event["runId"]]
        worktree_path = first_manifest["executionCwd"]
        self.assertNotEqual(worktree_path, str(self.workspace))
        self.assertEqual(first_manifest["isolationLifecycle"], "persistent")
        self.assertEqual(retry_manifest["isolationLifecycle"], "attached")
        self.assertEqual(retry_manifest["worktreeAttachment"]["path"], worktree_path)
        self.assertEqual(followup_manifest["isolationLifecycle"], "attached")
        self.assertEqual(followup_manifest["worktreeAttachment"]["path"], worktree_path)

        cwds = self.workspace_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(cwds), 3)
        self.assertEqual(cwds, [worktree_path] * 3)
        self.assertNotEqual(cwds[0], str(self.workspace))


if __name__ == "__main__":
    unittest.main()
