from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock as unittest_mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import cli, run_registry, wait_cancel_commands  # noqa: E402


class WaitCancelCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def write_run(
        self,
        *,
        status: str,
        pid: int | None = None,
        pgid: int | None = None,
        started_at: str | None = None,
        group: str | None = None,
        execution_cwd: str | None = None,
        isolated_workspace: bool | None = None,
    ):
        metadata = {"mode": "work", "cwd": str(self.workspace)}
        if group is not None:
            metadata["group"] = group
        run_id, alias = run_registry.register_run(
            self.registry_root,
            harness="codex",
            metadata=metadata,
        )
        run_path = run_registry.run_directory(self.registry_root, run_id)
        state = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "lastActivityAt": run_registry.utc_now_iso(),
        }
        if group is not None:
            state["group"] = group
        if pid is not None:
            state["pid"] = pid
        if pgid is not None:
            state["pgid"] = pgid
        run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, state)
        run_registry.write_json_atomic(
            run_path / run_registry.MANIFEST_FILE,
            {
                "schema": run_registry.MANIFEST_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "harness": "codex",
                "engine": "codex",
                "mode": "work",
                "cwd": str(self.workspace),
                **({"executionCwd": execution_cwd} if execution_cwd is not None else {}),
                **(
                    {"isolatedWorkspace": isolated_workspace}
                    if isolated_workspace is not None
                    else {}
                ),
                "startedAt": started_at or run_registry.utc_now_iso(),
            },
        )
        run_registry.write_json_atomic(
            run_path / run_registry.SNAPSHOT_FILE,
            {
                "schema": run_registry.SNAPSHOT_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "harness": "codex",
                "status": status,
                **({"group": group} if group is not None else {}),
                **({"executionCwd": execution_cwd} if execution_cwd is not None else {}),
                **(
                    {"isolatedWorkspace": isolated_workspace}
                    if isolated_workspace is not None
                    else {}
                ),
            },
        )
        return run_id, alias

    def run_cli(self, argv: list[str]):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = cli.main(["--cwd", str(self.workspace), *argv], stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def add_process_cleanup(self, proc: subprocess.Popen) -> None:
        self.addCleanup(self.cleanup_process, proc)

    @staticmethod
    def cleanup_process(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def test_wait_succeeded_returns_zero_json(self):
        _run_id, alias = self.write_run(status="succeeded")
        code, out, err = self.run_cli(["--json", "wait", alias, "--interval", "1"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["timedOut"])
        self.assertEqual(payload["runs"][0]["status"], "succeeded")

    def test_wait_dead_pid_is_terminal_failure_not_timeout(self):
        _run_id, alias = self.write_run(status="running", pid=999999999)
        code, out, err = self.run_cli(
            ["--json", "wait", alias, "--timeout", "10", "--interval", "1"]
        )
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        self.assertFalse(payload["timedOut"])
        self.assertEqual(payload["runs"][0]["status"], "failed")
        self.assertEqual(payload["runs"][0]["staleReason"], "dead_pid")

    def test_wait_timeout_returns_124(self):
        _run_id, alias = self.write_run(status="running", pid=os.getpid())
        code, out, err = self.run_cli(
            ["--json", "wait", alias, "--timeout", "1", "--interval", "1"]
        )
        self.assertEqual(code, 124, err)
        self.assertTrue(json.loads(out)["timedOut"])

    def test_wait_latest_selector(self):
        self.write_run(status="succeeded")
        _run_id, latest_alias = self.write_run(status="succeeded")
        code, out, err = self.run_cli(["--json", "wait", "--latest", "codex", "--interval", "1"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["runs"][0]["alias"], latest_alias)
        self.assertEqual(payload["runs"][0]["resolutionKind"], "latest")

    def test_wait_bare_harness_reports_resolution_workspace_and_stale_warning(self):
        run_id, alias = self.write_run(status="succeeded")
        run_path = run_registry.run_directory(self.registry_root, run_id)
        state = run_registry.load_run_state(self.registry_root, run_id)
        state["lastActivityAt"] = "2026-05-20T12:00:00Z"
        run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, state)

        code, out, err = self.run_cli(["--json", "wait", "codex", "--interval", "1"])
        self.assertEqual(code, 0, err)
        resolved = json.loads(out)["runs"][0]
        self.assertEqual(resolved["resolvedRunId"], run_id)
        self.assertEqual(resolved["resolvedAlias"], alias)
        self.assertEqual(resolved["resolvedWorkspace"], str(self.workspace))
        self.assertGreater(resolved["resolvedAgeSeconds"], 24 * 60 * 60)
        self.assertTrue(any("bare_handle_stale" in warning for warning in resolved["warnings"]))

    def test_wait_group_selector_waits_all_matching_runs(self):
        _first_id, first_alias = self.write_run(status="succeeded", group="wave4")
        _other_id, _other_alias = self.write_run(status="failed", group="other")
        _second_id, second_alias = self.write_run(status="succeeded", group="wave4")

        code, out, err = self.run_cli(["--json", "wait", "--group", "wave4", "--interval", "1"])

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([run["alias"] for run in payload["runs"]], [first_alias, second_alias])

    def test_wait_latest_and_group_emit_overlapping_run_once(self):
        _first_id, first_alias = self.write_run(status="succeeded", group="wave4")
        _latest_id, latest_alias = self.write_run(status="succeeded", group="wave4")

        code, out, err = self.run_cli(
            ["--json", "wait", "--latest", "codex", "--group", "wave4", "--interval", "1"]
        )

        self.assertEqual(code, 0, err)
        runs = json.loads(out)["runs"]
        self.assertEqual([run["alias"] for run in runs], [latest_alias, first_alias])
        self.assertEqual(runs[0]["resolutionKind"], "latest")
        self.assertEqual(runs[0]["resolvedHandle"], latest_alias)

    def test_wait_group_selector_reports_no_matches(self):
        self.write_run(status="succeeded", group="wave4")

        code, out, err = self.run_cli(["--json", "wait", "--group", "missing"])

        self.assertEqual(code, cli.EXIT_USAGE)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "no_matching_runs")
        self.assertIn("No runs found for group: missing", payload["message"])
        self.assertEqual(err, "")

    def test_wait_group_warns_when_work_runs_share_nonisolated_workspace_json(self):
        for _ in range(2):
            self.write_run(
                status="succeeded",
                group="wave4",
                execution_cwd=f"{self.workspace}/.",
                isolated_workspace=False,
            )

        code, out, err = self.run_cli(["--json", "wait", "--group", "wave4"])

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("share the same non-isolated execution workspace", payload["warnings"][0])

    def test_wait_group_warns_after_table_in_text_mode(self):
        for _ in range(2):
            self.write_run(
                status="succeeded",
                group="wave4",
                execution_cwd=str(self.workspace),
                isolated_workspace=False,
            )

        code, out, err = self.run_cli(["wait", "--group", "wave4"])

        self.assertEqual(code, 0, err)
        self.assertGreater(out.index("warning:"), out.index("alias        status"))
        self.assertIn("share the same non-isolated execution workspace", out)

    def test_wait_group_does_not_warn_for_one_run(self):
        self.write_run(
            status="succeeded",
            group="wave4",
            execution_cwd=str(self.workspace),
            isolated_workspace=False,
        )

        code, out, err = self.run_cli(["--json", "wait", "--group", "wave4"])

        self.assertEqual(code, 0, err)
        self.assertNotIn("warnings", json.loads(out))

    def test_wait_handles_do_not_warn_without_group_selector(self):
        aliases = [
            self.write_run(
                status="succeeded",
                group="wave4",
                execution_cwd=str(self.workspace),
                isolated_workspace=False,
            )[1]
            for _ in range(2)
        ]

        code, out, err = self.run_cli(["--json", "wait", *aliases])

        self.assertEqual(code, 0, err)
        self.assertNotIn("warnings", json.loads(out))

    def test_wait_group_does_not_warn_for_distinct_isolated_worktrees(self):
        for name in ("one", "two"):
            self.write_run(
                status="succeeded",
                group="wave4",
                execution_cwd=str(self.workspace / name),
                isolated_workspace=True,
            )

        code, out, err = self.run_cli(["--json", "wait", "--group", "wave4"])

        self.assertEqual(code, 0, err)
        self.assertNotIn("warnings", json.loads(out))

    def test_cancel_refuses_terminal_run(self):
        _run_id, alias = self.write_run(status="succeeded")
        code, out, err = self.run_cli(["cancel", alias])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("run_already_terminal", err or out)

    def test_cancel_process_group_marks_cancelled(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.add_process_cleanup(proc)
        pgid = os.getpgid(proc.pid)
        run_id, alias = self.write_run(status="running", pid=proc.pid, pgid=pgid)
        code, out, err = self.run_cli(["--json", "cancel", alias])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["runs"][0]["status"], "cancelled")
        proc.wait(timeout=5)
        state = json.loads(
            (
                run_registry.run_directory(self.registry_root, run_id) / run_registry.STATE_FILE
            ).read_text()
        )
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["failureReason"], "cancelled_by_user")

    def test_race_runner_finalizes_first_cancel_wins(self):
        """Runner finalizer writes terminal state during the cancel grace window,
        then cancel reconciles to cancelled (cancel wins)."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.add_process_cleanup(proc)
        pgid = os.getpgid(proc.pid)
        run_id, alias = self.write_run(status="running", pid=proc.pid, pgid=pgid)
        run_path = run_registry.run_directory(self.registry_root, run_id)
        # The initial state is 'running' so the top-of-_cancel_target check
        # passes. Simulate the runner finalizer writing 'failed' during the
        # grace window. The marker protocol adds a pre-signal locked re-read
        # (call #2) that must still see 'running' so the marker is stamped; the
        # runner's terminal write is observed by the final locked re-read
        # (call #3), which reconciles to cancelled.
        original_load = run_registry.load_run_state_or_none
        runner_terminal_state = dict(json.loads((run_path / run_registry.STATE_FILE).read_text()))
        runner_terminal_state.update(
            {
                "status": "failed",
                "failureReason": "harness_error",
                "exitCode": 1,
                "finishedAt": run_registry.utc_now_iso(),
            }
        )

        call_count = {"n": 0}

        def fake_load(root, rid):
            call_count["n"] += 1
            if call_count["n"] == 3:
                # Final locked re-read sees the runner's terminal write.
                return dict(runner_terminal_state)
            return original_load(root, rid)

        target = run_registry.RunTarget(run_id=run_id, alias=alias)
        with unittest_mock.patch.object(
            run_registry, "load_run_state_or_none", side_effect=fake_load
        ):
            payload = wait_cancel_commands._cancel_target(self.registry_root, target)
        self.assertEqual(payload["status"], "cancelled")
        state = json.loads((run_path / run_registry.STATE_FILE).read_text())
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["failureReason"], "cancelled_by_user")
        proc.kill()

    def test_cancel_stamps_cancel_requested_marker_before_signal(self):
        """Cancel stamps cancelRequested:true + cancelRequestedAt on a live run
        under the registry lock BEFORE sending any signal. The marker survives
        the cancel flow and the final state is cancelled."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.add_process_cleanup(proc)
        pgid = os.getpgid(proc.pid)
        run_id, alias = self.write_run(status="running", pid=proc.pid, pgid=pgid)
        run_path = run_registry.run_directory(self.registry_root, run_id)
        target = run_registry.RunTarget(run_id=run_id, alias=alias)
        # Capture the state written by the pre-signal marker stamp by hooking
        # write_json_atomic: record every state.json write.
        state_writes: list[dict] = []
        original_write = run_registry.write_json_atomic

        def capturing_write(path, data):
            if str(path).endswith(run_registry.STATE_FILE):
                state_writes.append(dict(data) if isinstance(data, dict) else data)
            return original_write(path, data)

        with unittest_mock.patch.object(
            run_registry, "write_json_atomic", side_effect=capturing_write
        ):
            payload = wait_cancel_commands._cancel_target(self.registry_root, target)
        self.assertEqual(payload["status"], "cancelled")
        # The first state write must be the marker stamp (cancelRequested true).
        marker_writes = [
            w for w in state_writes if isinstance(w, dict) and w.get("cancelRequested") is True
        ]
        self.assertTrue(marker_writes, "cancel must stamp cancelRequested:true before signaling")
        first_marker = marker_writes[0]
        self.assertIn("cancelRequestedAt", first_marker)
        self.assertIsInstance(first_marker["cancelRequestedAt"], str)
        self.assertTrue(first_marker["cancelRequestedAt"])
        # The marker stamp must NOT itself be terminal: it preserves the running
        # status so the runner can still observe the marker if it finalizes first.
        self.assertNotEqual(first_marker.get("status"), "cancelled")
        # Final state is cancelled.
        final_state = json.loads((run_path / run_registry.STATE_FILE).read_text())
        self.assertEqual(final_state["status"], "cancelled")
        proc.wait(timeout=5)

    def test_cancel_does_not_stamp_marker_on_already_terminal_run(self):
        """cancelRequested is never stamped on an already-terminal run.

        A terminal run is refused before the marker stamp.
        """
        _run_id, alias = self.write_run(status="succeeded")
        target = run_registry.RunTarget(run_id=_run_id, alias=alias)
        original_write = run_registry.write_json_atomic
        state_writes: list[dict] = []

        def capturing_write(path, data):
            if str(path).endswith(run_registry.STATE_FILE):
                state_writes.append(dict(data) if isinstance(data, dict) else data)
            return original_write(path, data)

        with (
            unittest_mock.patch.object(
                run_registry, "write_json_atomic", side_effect=capturing_write
            ),
            self.assertRaises(wait_cancel_commands.WaitCancelError) as ctx,
        ):
            wait_cancel_commands._cancel_target(self.registry_root, target)
        self.assertEqual(ctx.exception.error, "run_already_terminal")
        # No state write happened at all (refusal precedes the marker stamp).
        self.assertEqual(state_writes, [])

    def test_race_cancel_finalizes_first_runner_preserves_cancelled(self):
        """Cancel writes cancelled first, then the runner finalizer preserves
        cancelled instead of downgrading to failed/succeeded."""
        run_id, alias = self.write_run(status="running", pid=os.getpid(), pgid=os.getpgid(0))
        run_path = run_registry.run_directory(self.registry_root, run_id)
        cancel_state = json.loads((run_path / run_registry.STATE_FILE).read_text())
        cancel_state.update(
            {
                "status": "cancelled",
                "failureReason": "cancelled_by_user",
                "exitCode": 1,
                "finishedAt": run_registry.utc_now_iso(),
            }
        )
        run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, cancel_state)
        # Now the runner finalizer runs via _persist_final_progress: it should
        # re-read state, see cancelled, and preserve it while recording metadata.
        from delegate_agent import harness_events, runner

        ctx = runner.RunContext(
            registry_root=self.registry_root,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode="work",
            model=None,
            source_cwd=str(self.workspace),
            execution_cwd=str(self.workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
        )
        accumulator = harness_events.StreamAccumulator(harness="codex")
        files = runner.TrackedRunFiles(
            run_path=run_path,
            stdout_log=run_path / run_registry.STDOUT_LOG,
            stderr_log=run_path / run_registry.STDERR_LOG,
        )
        capture = runner.TrackedCaptureResult(
            accumulator=accumulator,
            exit_code=0,
            duration_ms=100,
            stdout_bytes=10,
            stderr_bytes=5,
            stdin_failures=(),
            pid=os.getpid(),
            pgid=os.getpgid(0),
        )
        runner._finalize_tracked_run(
            files,
            ctx,
            capture,
            completion_report_mode="off",
        )
        state = json.loads((run_path / run_registry.STATE_FILE).read_text())
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["failureReason"], "cancelled_by_user")
        # The runner still recorded its work summary/output metadata.
        self.assertEqual(state.get("exitCode"), 1)
        self.assertIn("stdoutBytes", state)

    def test_preserved_cancel_envelope_matches_state_child_exit_zero(self):
        """When the finalizer preserves a concurrent cancel and the child
        exited 0, the LIVE result (TrackedFinalization) must agree with the
        persisted state: ok=False, status cancelled, exitCode 1, and
        failureReason cancelled_by_user. The process exit code computed from
        the finalization must also be 1."""
        run_id, alias = self.write_run(status="running", pid=os.getpid(), pgid=os.getpgid(0))
        run_path = run_registry.run_directory(self.registry_root, run_id)
        # Simulate cancel writing 'cancelled' first (cancel-wins race).
        cancel_state = json.loads((run_path / run_registry.STATE_FILE).read_text())
        cancel_state.update(
            {
                "status": "cancelled",
                "failureReason": "cancelled_by_user",
                "exitCode": 1,
                "finishedAt": run_registry.utc_now_iso(),
            }
        )
        run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, cancel_state)
        from delegate_agent import harness_events, runner

        ctx = runner.RunContext(
            registry_root=self.registry_root,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode="work",
            model=None,
            source_cwd=str(self.workspace),
            execution_cwd=str(self.workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
        )
        accumulator = harness_events.StreamAccumulator(harness="codex")
        files = runner.TrackedRunFiles(
            run_path=run_path,
            stdout_log=run_path / run_registry.STDOUT_LOG,
            stderr_log=run_path / run_registry.STDERR_LOG,
        )
        # Child exited 0, but cancel already won the race.
        capture = runner.TrackedCaptureResult(
            accumulator=accumulator,
            exit_code=0,
            duration_ms=100,
            stdout_bytes=10,
            stderr_bytes=5,
            stdin_failures=(),
            pid=os.getpid(),
            pgid=os.getpgid(0),
        )
        finalization = runner._finalize_tracked_run(
            files,
            ctx,
            capture,
            completion_report_mode="off",
        )
        # The LIVE result must match the persisted state.
        self.assertEqual(finalization.status, "cancelled")
        self.assertEqual(finalization.exit_code, 1)
        self.assertEqual(finalization.extra.get("failureReason"), "cancelled_by_user")
        # _tracked_result derives ok and the process exit from the finalization.
        ok = finalization.exit_code == 0
        self.assertFalse(ok, "preserved cancel with child exit 0 must be ok=False")
        state = json.loads((run_path / run_registry.STATE_FILE).read_text())
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["failureReason"], "cancelled_by_user")
        self.assertEqual(state.get("exitCode"), 1)

    def test_pid_identity_mismatch_refuses_stale_pid(self):
        """A pid that predates the run beyond skew is refused.

        Deterministic: ``_process_start_datetime`` is monkeypatched to a fixed
        datetime so the mismatch does not depend on real wall-clock process
        start times. The process start is stamped 5 minutes before the run's
        manifest startedAt, far beyond the 60s skew window.
        """
        from datetime import UTC, datetime

        proc_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        started_at = "2026-01-01T12:05:00Z"  # 5 min after proc_start -> mismatch
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.add_process_cleanup(proc)
        pgid = os.getpgid(proc.pid)
        run_id, alias = self.write_run(
            status="running", pid=proc.pid, pgid=pgid, started_at=started_at
        )
        target = run_registry.RunTarget(run_id=run_id, alias=alias)
        with (
            unittest_mock.patch.object(
                wait_cancel_commands, "_process_start_datetime", return_value=proc_start
            ),
            self.assertRaises(wait_cancel_commands.WaitCancelError) as ctx,
        ):
            wait_cancel_commands._cancel_target(self.registry_root, target)
        self.assertEqual(ctx.exception.error, "pid_identity_mismatch")

    def test_pid_identity_within_skew_allows_cancel(self):
        """A pid that started within the skew window is allowed and cancelled.

        Deterministic: ``_process_start_datetime`` is monkeypatched to a fixed
        datetime 30s before the run's startedAt, which is within the 60s skew
        window, so the identity check passes and cancel proceeds.
        """
        from datetime import UTC, datetime

        proc_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        started_at = "2026-01-01T12:00:30Z"  # proc 30s before run -> within skew
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.add_process_cleanup(proc)
        pgid = os.getpgid(proc.pid)
        run_id, alias = self.write_run(
            status="running", pid=proc.pid, pgid=pgid, started_at=started_at
        )
        target = run_registry.RunTarget(run_id=run_id, alias=alias)
        with unittest_mock.patch.object(
            wait_cancel_commands, "_process_start_datetime", return_value=proc_start
        ):
            payload = wait_cancel_commands._cancel_target(self.registry_root, target)
        self.assertEqual(payload["status"], "cancelled")
        proc.wait(timeout=5)

    def test_process_start_datetime_sets_lc_all_c(self):
        """The ps invocation env forces LC_ALL=C for locale-stable parsing."""
        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="")

        with unittest_mock.patch.object(
            wait_cancel_commands.subprocess, "run", side_effect=fake_run
        ):
            result = wait_cancel_commands._process_start_datetime(pid=999999999)
        self.assertIsNone(result, "empty ps stdout should soft-degrade to None")
        self.assertEqual(captured["env"].get("LC_ALL"), "C")
        self.assertEqual(captured["args"], ["ps", "-o", "lstart=", "-p", "999999999"])

    def test_pid_identity_soft_degrades_when_ps_unparseable(self):
        """When ps output is unparseable, cancel proceeds with a warning."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.add_process_cleanup(proc)
        pgid = os.getpgid(proc.pid)
        run_id, alias = self.write_run(status="running", pid=proc.pid, pgid=pgid)
        target = run_registry.RunTarget(run_id=run_id, alias=alias)
        with unittest_mock.patch.object(
            wait_cancel_commands, "_process_start_datetime", return_value=None
        ):
            payload = wait_cancel_commands._cancel_target(self.registry_root, target)
        self.assertEqual(payload["status"], "cancelled")
        self.assertTrue(any("identity" in w or "ps" in w for w in payload.get("warnings") or []))
        proc.wait(timeout=5)

    def test_group_liveness_escalates_from_sigterm_to_sigkill(self):
        """A process group still live after SIGTERM receives SIGKILL."""
        pid = os.getpid()
        pgid = 4243
        run_id, alias = self.write_run(status="running", pid=pid, pgid=pgid)
        target = run_registry.RunTarget(run_id=run_id, alias=alias)

        with (
            unittest_mock.patch.object(
                wait_cancel_commands, "_check_pid_identity", return_value=[]
            ),
            unittest_mock.patch.object(
                wait_cancel_commands, "_signal_target_alive", return_value=True
            ),
            unittest_mock.patch.object(wait_cancel_commands, "_send_signal") as send_signal,
            unittest_mock.patch.object(wait_cancel_commands, "CANCEL_GRACE_SECONDS", 0),
        ):
            payload = wait_cancel_commands._cancel_target(self.registry_root, target)

        self.assertEqual(payload["status"], "cancelled")
        self.assertEqual(
            send_signal.call_args_list,
            [
                unittest_mock.call(pgid, wait_cancel_commands.signal.SIGTERM, process_group=True),
                unittest_mock.call(pgid, wait_cancel_commands.signal.SIGKILL, process_group=True),
            ],
        )

    def test_cancel_refuses_stale_dead_pid_run(self):
        """A running run with a dead pid is stale and cancel refuses it."""
        _run_id, alias = self.write_run(status="running", pid=999999999)
        code, out, err = self.run_cli(["cancel", alias])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("run_already_terminal", err or out)

    def test_cancel_refuses_stale_missing_pid_run(self):
        """A running run with no pid is stale (missing_pid) and cancel refuses."""
        _run_id, alias = self.write_run(status="running")
        code, out, err = self.run_cli(["cancel", alias])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("run_already_terminal", err or out)

    def test_cancel_refuses_pid_le_one(self):
        _run_id, alias = self.write_run(status="running", pid=1, pgid=1)
        code, out, err = self.run_cli(["cancel", alias])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("unsafe_signal_target", err or out)

    def test_cancel_legacy_pid_only_emits_warning(self):
        """A run with pid but no pgid falls back to pid signal with a warning."""
        # Use a short-lived child so the identity check passes (started ~now).
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.add_process_cleanup(proc)
        _run_id, alias = self.write_run(status="running", pid=proc.pid)
        code, out, err = self.run_cli(["--json", "cancel", alias])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["runs"][0]["status"], "cancelled")
        self.assertTrue(
            any(
                "legacy" in w or "pgid missing" in w
                for w in payload["runs"][0].get("warnings") or []
            )
        )
        proc.wait(timeout=5)

    def test_wait_multi_handle_one_failed_returns_one(self):
        _run_id1, alias1 = self.write_run(status="succeeded")
        _run_id2, alias2 = self.write_run(status="failed")
        code, _out, err = self.run_cli(["--json", "wait", alias1, alias2, "--interval", "1"])
        self.assertEqual(code, 1, err)

    def test_wait_multi_handle_mixed_timeout_and_failure_returns_one(self):
        """Any failed/cancelled run -> exit 1 even if others timed out."""
        _run_id1, alias1 = self.write_run(status="failed")
        _run_id2, alias2 = self.write_run(status="running", pid=os.getpid())
        code, out, err = self.run_cli(
            ["--json", "wait", alias1, alias2, "--timeout", "1", "--interval", "1"]
        )
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        # The deadline was hit (the running run never terminated), but the
        # failed run's failure takes exit-code precedence over the timeout.
        self.assertTrue(payload["timedOut"])

    def test_wait_multi_handle_only_timeouts_returns_124(self):
        _run_id1, alias1 = self.write_run(status="running", pid=os.getpid())
        _run_id2, alias2 = self.write_run(status="running", pid=os.getpid())
        code, out, err = self.run_cli(
            ["--json", "wait", alias1, alias2, "--timeout", "1", "--interval", "1"]
        )
        self.assertEqual(code, 124, err)
        self.assertTrue(json.loads(out)["timedOut"])

    def test_wait_multi_handle_mixed_cancelled_and_timeout_returns_one(self):
        _run_id1, alias1 = self.write_run(status="cancelled")
        _run_id2, alias2 = self.write_run(status="running", pid=os.getpid())
        code, _out, err = self.run_cli(
            ["--json", "wait", alias1, alias2, "--timeout", "1", "--interval", "1"]
        )
        self.assertEqual(code, 1, err)

    def test_wait_multi_handle_all_succeeded_returns_zero(self):
        _run_id1, alias1 = self.write_run(status="succeeded")
        _run_id2, alias2 = self.write_run(status="succeeded")
        code, _out, err = self.run_cli(["--json", "wait", alias1, alias2, "--interval", "1"])
        self.assertEqual(code, 0, err)
