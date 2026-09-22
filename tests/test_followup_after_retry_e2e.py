"""End-to-end followup coverage after a structured retry in a worktree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tests import assert_compact_temps_contained, proc_harness

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "delegate.py"


class FollowupAfterRetryE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-followup-retry-")
        self.addCleanup(self.temp.cleanup)
        # Runs launched here are real subprocesses, so each tracked child
        # resolved the production compact child temp root. Registered after the
        # temp cleanup, so it runs first: the recorded paths can still be read
        # from the run manifests while they exist.
        self.addCleanup(self._contain_compact_temps)
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

    def _owned_producer_roots(self) -> tuple[Path, Path]:
        """The two roots every Delegate process this test owns carries.

        This test's HOME is a sibling of its workspace (`<root>/home` and
        `<root>/workspace`), so there is no one root that covers its producers:

        * the CLI calls in `_run_delegate` are `bin/delegate.py --cwd
          <workspace> ...` and carry the workspace;
        * the run's real producer is the child the detached supervisor launches
          as `[python, <HOME>/.delegate-workflow-pins/.../bin/delegate.py,
          --json, --group <wfId>, run, --input-json <TMPDIR file>]` -- the
          pinned runtime entrypoint under this test's HOME, with the workspace
          only as that child's working directory (`_run_child_command_for_state`),
          so the workspace string appears nowhere in its command line.

        A scan rooted at the workspace alone therefore cannot see a live child
        producer, and would certify its run's temp as safe to delete while that
        child could still launch another attempt into it.
        """
        return (self.workspace, self.home)

    def _contain_compact_temps(self) -> None:
        # The Delegate process that drives each tracked run here is a child of
        # this test's detached workflow supervisor, so it owns no handle: the
        # proofs are the supervisor's own lock (that no further child can be
        # launched) and the owned-process scan over both roots its producers
        # carry (`_owned_producer_roots`). Both are re-checked at the deletion
        # boundary, because a run's compact temp is shared by all attempts and a
        # child already launched can retry until it exits.
        producers = [
            *proc_harness.workflow_producer_proofs(self.workspace),
            proc_harness.reaped_owned_producers(*self._owned_producer_roots()),
        ]
        assert_compact_temps_contained(self.workspace / ".delegate", producers=producers)

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
                import time

                sleep = os.environ.get("FAKE_CODEX_SLEEP")
                if sleep:
                    time.sleep(float(sleep))
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
        self.assertEqual(self.attempt_file.read_text(encoding="utf-8"), "2")

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
        self.assertEqual(retry_manifest["worktreeAttachment"]["sourceRunId"], first_event["runId"])
        self.assertEqual(retry_manifest["worktreeAttachment"]["path"], worktree_path)
        self.assertEqual(followup_manifest["isolationLifecycle"], "attached")
        self.assertEqual(followup_manifest["worktreeAttachment"]["path"], worktree_path)
        retry_events = [
            event
            for event in events_payload["events"]
            if event.get("type") == "agent_structured_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["strategy"], "resume")

        cwds = self.workspace_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(cwds), 3)
        self.assertEqual(cwds, [worktree_path] * 3)
        self.assertNotEqual(cwds[0], str(self.workspace))

    def _wait_for_live_run_child(self, wf_id: str, *, timeout: float = 30.0) -> int:
        """The pid of the workflow child Delegate process running this workflow's run.

        Found by the ``--group`` flag its supervisor launches it with
        (`_run_child_command_for_state`), which is independent of the proof under
        test: the proof is what the assertions ask about.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            listing = subprocess.run(
                ["ps", "-ww", "-axo", "pid=,pgid=,command="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            for line in listing.splitlines():
                if "delegate.py" in line and f"--group {wf_id}" in line:
                    return int(line.split()[0])
            time.sleep(0.1)
        self.fail(f"no live workflow child producer appeared for {wf_id}")

    def _wait_for_recorded_run_temp(self, *, timeout: float = 30.0) -> Path:
        """The production compact temp the run's own manifest records."""
        from tests import recorded_compact_temps

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            recorded = [
                path for _run_id, path in recorded_compact_temps(self.workspace / ".delegate")
            ]
            if recorded:
                return recorded[0]
            time.sleep(0.05)
        self.fail("no run manifest recorded a compact child temp")

    def test_owned_producer_proof_covers_the_child_that_drives_a_run(self) -> None:
        """A live child producer must never be certified gone by this caller's proof.

        The producer of a run here is the child the detached supervisor launches
        from the pinned runtime entrypoint under this test's HOME; it is launched
        with the workspace as its working directory and names the workspace
        nowhere in its command line. A proof rooted at the workspace alone
        therefore answers "gone" while that child is mid-run, and containment
        acting on that answer may delete the run's compact temp -- the shared
        TMPDIR of every attempt the child could still launch. The caller's proof
        covers both roots its producers carry, and its answer tracks the child:
        alive while the child runs, gone once the child has exited.
        """
        script = self.root / "producer-route-workflow.py"
        script.write_text(
            textwrap.dedent(
                """
                meta = {"name": "producer-route", "defaults": {"engine": "codex", "mode": "work"}}
                return agent("hold the run open", isolation="worktree")
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        launch = self._run_delegate(
            ["--json", "workflow", "run", str(script)], env_extra={"FAKE_CODEX_SLEEP": "10"}
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        child_pid = self._wait_for_live_run_child(wf_id)
        temp_path = self._wait_for_recorded_run_temp()

        from tests.process_guard import owned_delegate_processes

        self.assertNotIn(
            child_pid,
            owned_delegate_processes(self.workspace),
            "the workspace-only root is exactly the gap this caller's roots close",
        )
        roots = self._owned_producer_roots()
        self.assertIn(child_pid, owned_delegate_processes(*roots))
        proof = proc_harness.owned_producer_proof(*roots)
        self.assertIsNone(
            proof.poll(), f"the proof did not see the live child producer {child_pid}"
        )

        from tests import reap_recorded_compact_temps

        removed, surviving = reap_recorded_compact_temps(
            self.workspace / ".delegate", producers=[proof]
        )
        self.assertEqual(removed, [], "containment removed a live producer's run temp")
        self.assertEqual(surviving, [temp_path])
        self.assertTrue(temp_path.is_dir(), temp_path)

        waited = self._run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "40"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        deadline = time.monotonic() + 20
        while proof.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNotNone(proof.poll(), "the proof never reported the exited child gone")


if __name__ == "__main__":
    unittest.main()
