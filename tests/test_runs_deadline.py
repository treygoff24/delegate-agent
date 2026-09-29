"""`delegate runs` shows how long a running Run has left before its timeout."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delegate_agent import cli, rendering, run_registry


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunsDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def _write_run(self, *, status: str, timeout: int | None, started_ago: int) -> str:
        run_id, alias = run_registry.register_run(
            self.registry_root,
            harness="codex",
            metadata={"mode": "work", "cwd": str(self.workspace)},
        )
        run_path = run_registry.run_directory(self.registry_root, run_id)
        started = _iso(datetime.now(UTC) - timedelta(seconds=started_ago))
        state = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
            "pid": os.getpid(),
            "startedAt": started,
            "lastActivityAt": started,
        }
        manifest = {
            "schema": run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": "codex",
            "engine": "codex",
            "mode": "work",
            "cwd": str(self.workspace),
            "startedAt": started,
        }
        if timeout is not None:
            manifest["timeoutSeconds"] = timeout
        run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, state)
        run_registry.write_json_atomic(run_path / run_registry.MANIFEST_FILE, manifest)
        return alias

    def _runs(self, *extra: str) -> tuple[int, str]:
        stdout = io.StringIO()
        code = cli.main(
            ["--cwd", str(self.workspace), *extra, "runs"], stdout=stdout, stderr=io.StringIO()
        )
        return code, stdout.getvalue()

    def _rows(self) -> dict[str, dict]:
        code, out = self._runs("--json")
        self.assertEqual(code, 0, out)
        return {row["alias"]: row for row in json.loads(out)["runs"]}

    def test_running_run_with_a_timeout_carries_its_deadline_and_remaining_time(self):
        alias = self._write_run(status="running", timeout=1800, started_ago=600)
        row = self._rows()[alias]
        self.assertEqual(row["status"], "running")
        self.assertLessEqual(abs(row["remainingSeconds"] - 1200), 5)
        deadline = datetime.strptime(row["deadlineAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        started = datetime.now(UTC) - timedelta(seconds=600)
        self.assertLessEqual(abs((deadline - started).total_seconds() - 1800), 5)

    def test_overdue_run_floors_remaining_at_zero(self):
        alias = self._write_run(status="running", timeout=60, started_ago=600)
        self.assertEqual(self._rows()[alias]["remainingSeconds"], 0)

    def test_running_run_without_a_timeout_and_terminal_runs_carry_neither_field(self):
        no_timeout = self._write_run(status="running", timeout=None, started_ago=10)
        done = self._write_run(status="completed", timeout=1800, started_ago=10)
        rows = self._rows()
        for alias in (no_timeout, done):
            self.assertNotIn("deadlineAt", rows[alias])
            self.assertNotIn("remainingSeconds", rows[alias])

    def test_text_rendering_shows_the_time_left(self):
        alias = self._write_run(status="running", timeout=1800, started_ago=600)
        code, out = self._runs()
        self.assertEqual(code, 0)
        line = next(line for line in out.splitlines() if line.startswith(alias))
        self.assertRegex(line, r"\b(19|20)m left")

    def _set_state(self, alias: str, **fields) -> None:
        run_id = next(
            rid
            for rid, entry in run_registry.load_index(self.registry_root)["runs"].items()
            if entry.get("alias") == alias
        )
        path = run_registry.run_directory(self.registry_root, run_id) / run_registry.STATE_FILE
        state = json.loads(path.read_text())
        state.update(fields)
        run_registry.write_json_atomic(path, state)

    def test_recorded_deadline_wins_over_the_started_at_computation(self):
        # A long --setup delays the runner's timeout clock past startedAt.
        alias = self._write_run(status="running", timeout=60, started_ago=600)
        later = datetime.now(UTC) + timedelta(seconds=900)
        self._set_state(alias, deadlineAt=_iso(later))
        row = self._rows()[alias]
        self.assertEqual(row["deadlineAt"], _iso(later))
        self.assertLessEqual(abs(row["remainingSeconds"] - 900), 5)

    def test_runner_records_the_deadline_while_running_and_drops_it_when_terminal(self):
        import sys
        import threading
        import time

        from delegate_agent import runner
        from tests.test_status_truth import _ctx

        script = "import time; time.sleep(2)"
        run_id, alias = run_registry.register_run(self.registry_root, harness="omp")
        ctx = _ctx(
            root=self.registry_root, run_id=run_id, alias=alias, workspace=str(self.workspace)
        )
        seen: list[str] = []

        def poll():
            end = time.monotonic() + 10
            while time.monotonic() < end and not seen:
                state = run_registry.load_run_state(self.registry_root, run_id) or {}
                if state.get("status") == "running" and state.get("deadlineAt"):
                    seen.append(state["deadlineAt"])
                time.sleep(0.05)

        thread = threading.Thread(target=poll)
        thread.start()
        runner.execute_tracked(
            [sys.executable, "-c", script],
            str(self.workspace),
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            timeout=600,
        )
        thread.join()
        self.assertTrue(seen, "no running record carried deadlineAt")
        recorded = datetime.strptime(seen[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertLessEqual(abs((recorded - datetime.now(UTC)).total_seconds() - 600), 10)
        final = run_registry.load_run_state(self.registry_root, run_id) or {}
        self.assertNotIn("deadlineAt", final)

    def test_snapshot_does_not_expose_the_recorded_deadline(self):
        alias = self._write_run(status="running", timeout=60, started_ago=5)
        self._set_state(alias, deadlineAt=_iso(datetime.now(UTC) + timedelta(seconds=50)))
        stdout = io.StringIO()
        code = cli.main(
            ["--cwd", str(self.workspace), "--json", "snapshot", alias],
            stdout=stdout,
            stderr=io.StringIO(),
        )
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertNotIn("deadlineAt", json.loads(stdout.getvalue()))

    def test_a_cancellation_merge_does_not_keep_the_live_deadline(self):
        merged = run_registry.merge_terminal_record(
            {"status": "cancelled", "cancelRequested": True, "deadlineAt": "2030-01-01T00:00:00Z"},
            {"status": "failed"},
        )
        self.assertEqual(merged["status"], "cancelled")
        self.assertNotIn("deadlineAt", merged)

    def test_remaining_time_format_is_compact(self):
        self.assertEqual(rendering.format_remaining(45), "45s left")
        self.assertEqual(rendering.format_remaining(720), "12m left")
        self.assertEqual(rendering.format_remaining(3900), "1h5m left")
        self.assertEqual(rendering.format_remaining(-3), "0s left")


if __name__ == "__main__":
    unittest.main()
