import importlib.util
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
RETENTION_PATH = ROOT / "src" / "delegate_agent" / "retention.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"
CLI_PATH = ROOT / "src" / "delegate_agent" / "cli.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.retention = load_module(RETENTION_PATH, "delegate_retention_test")
        self.registry = load_module(REGISTRY_PATH, "delegate_registry_retention_test")
        self.delegate = load_module(CLI_PATH, "delegate_cli_retention_test")
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.registry_root = self.registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        self.config = {
            "tracking": {
                "retention": {
                    "enabled": True,
                    "rawLogDays": 7,
                }
            }
        }

    def tearDown(self):
        self.temp.cleanup()

    def write_completed_run(
        self,
        *,
        status: str = "succeeded",
        finished_at: str | None = None,
        pid: int | None = None,
        with_logs: bool = True,
    ) -> tuple[str, str]:
        run_id, alias = self.registry.register_run(self.registry_root, harness="cursor")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        finished = finished_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {
            "schema": "delegate.state.v1",
            "runId": run_id,
            "alias": alias,
            "status": status,
            "finishedAt": finished,
            "lastActivityAt": finished,
        }
        if pid is not None:
            state["pid"] = pid
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.registry.write_json_atomic(
            run_path / "manifest.json",
            {
                "schema": "delegate.manifest.v1",
                "runId": run_id,
                "alias": alias,
                "harness": "cursor",
                "startedAt": finished,
            },
        )
        self.registry.write_json_atomic(
            run_path / "snapshot.json",
            {
                "schema": "delegate.snapshot.v1",
                "ok": True,
                "alias": alias,
                "runId": run_id,
                "status": status,
                "startedAt": finished,
                "assistantText": "done",
            },
        )
        if with_logs:
            (run_path / "stdout.log").write_text("stdout-data\n", encoding="utf-8")
            (run_path / "stderr.log").write_text("stderr-data\n", encoding="utf-8")
            (run_path / "events.jsonl").write_text('{"kind":"text"}\n', encoding="utf-8")
        return run_id, alias

    def test_running_run_is_not_archived(self):
        run_id, _alias = self.write_completed_run(status="running", pid=__import__("os").getpid())
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        result = self.retention.run_retention_pass(self.registry_root, self.config)
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_stale_run_is_not_archived(self):
        run_id, _alias = self.write_completed_run(status="running", pid=999999999)
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        result = self.retention.run_retention_pass(self.registry_root, self.config)
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_recent_completed_run_is_not_archived(self):
        run_id, _alias = self.write_completed_run()
        result = self.retention.run_retention_pass(self.registry_root, self.config)
        self.assertEqual(result["archived"], 0)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        self.assertTrue((run_path / "stdout.log").exists())
        self.assertFalse(self.retention.archive_path(self.registry_root, run_id).exists())

    def test_old_completed_run_archives_raw_logs(self):
        run_id, alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        state["lastActivityAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        zero_day_config = {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}}
        result = self.retention.run_retention_pass(self.registry_root, zero_day_config)
        self.assertEqual(result["archived"], 1)
        archive_file = self.retention.archive_path(self.registry_root, run_id)
        self.assertTrue(archive_file.exists())
        self.assertFalse((run_path / "stdout.log").exists())
        self.assertFalse((run_path / "stderr.log").exists())
        self.assertFalse((run_path / "events.jsonl").exists())
        self.assertTrue((run_path / "manifest.json").exists())
        self.assertTrue((run_path / "snapshot.json").exists())
        self.assertTrue((run_path / "state.json").exists())
        index = self.registry.load_index(self.registry_root)
        self.assertEqual(index["aliases"][alias], run_id)
        with tarfile.open(archive_file, "r:gz") as archive:
            names = {member.name for member in archive.getmembers()}
        self.assertEqual(names, {"stdout.log", "stderr.log", "events.jsonl"})

    def test_snapshot_works_after_archival(self):
        run_id, alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        import io

        stdout = io.StringIO()
        code = self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn(alias, output)
        self.assertIn("archived", output.lower())

    def test_run_output_reads_archived_stdout(self):
        run_id, alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        import io

        stdout = io.StringIO()
        code = self.delegate.main(
            ["--cwd", str(self.workspace), "run-output", alias, "--stdout", "--tail", "1"],
            stdout=stdout,
        )
        self.assertEqual(code, 0)
        self.assertIn("stdout-data", stdout.getvalue())

    def test_retention_disabled_skips_archival(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        disabled = {"tracking": {"retention": {"enabled": False, "rawLogDays": 0}}}
        result = self.retention.run_retention_pass(self.registry_root, disabled)
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_no_delete_commands_exist(self):
        help_text = self.delegate.HELP
        self.assertNotIn("prune", help_text.lower())
        self.assertNotIn("delete", help_text.lower())

    def test_unknown_status_is_not_archived(self):
        run_id, _alias = self.write_completed_run(status="running")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state.pop("status", None)
        state.pop("pid", None)
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        result = self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        self.assertEqual(result["archived"], 0)
        self.assertTrue((run_path / "stdout.log").exists())

    def test_crash_recovery_finishes_archival_when_archive_exists(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        archive_file = self.retention.archive_path(self.registry_root, run_id)
        self.retention.archive_dir(self.registry_root).mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_file, "w:gz") as archive:
            for name in self.retention.ARCHIVE_MEMBER_NAMES:
                archive.add(run_path / name, arcname=name)
        self.assertTrue((run_path / "stdout.log").exists())
        result = self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        self.assertEqual(result["archived"], 1)
        self.assertFalse((run_path / "stdout.log").exists())
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        self.assertIn("rawLogsArchivedAt", state)

    def test_effective_log_byte_sizes_after_archival(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        stdout_bytes, stderr_bytes = self.retention.effective_log_byte_sizes(
            self.registry_root,
            run_id,
        )
        self.assertEqual(stdout_bytes, len("stdout-data\n"))
        self.assertEqual(stderr_bytes, len("stderr-data\n"))

    def test_effective_log_byte_sizes_reads_state_without_opening_archive(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        with mock.patch.object(tarfile, "open", side_effect=AssertionError("tar must not open")):
            stdout_bytes, stderr_bytes = self.retention.effective_log_byte_sizes(
                self.registry_root,
                run_id,
            )
        self.assertEqual(stdout_bytes, len("stdout-data\n"))
        self.assertEqual(stderr_bytes, len("stderr-data\n"))

    def test_log_file_byte_size_after_archival(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        self.assertEqual(
            self.retention.log_file_byte_size(self.registry_root, run_id, "stdout.log"),
            len("stdout-data\n"),
        )
        self.assertEqual(
            self.retention.log_file_byte_size(self.registry_root, run_id, "stderr.log"),
            len("stderr-data\n"),
        )

    def test_read_archived_member_rejects_unknown_names(self):
        run_id, _alias = self.write_completed_run()
        archive_file = self.retention.archive_path(self.registry_root, run_id)
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        run_path = self.registry.run_directory(self.registry_root, run_id)
        with tarfile.open(archive_file, "w:gz") as archive:
            archive.add(run_path / "stdout.log", arcname="stdout.log")
        with self.assertRaises(ValueError):
            self.retention.read_archived_member(archive_file, "../evil")

    def test_run_output_tails_archived_stdout_without_loading_entire_file(self):
        run_id, _alias = self.write_completed_run()
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_path = self.registry.run_directory(self.registry_root, run_id)
        stdout_path = run_path / "stdout.log"
        stdout_path.write_text("".join(f"line-{index}\n" for index in range(200)), encoding="utf-8")
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["finishedAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)
        self.retention.run_retention_pass(
            self.registry_root,
            {"tracking": {"retention": {"enabled": True, "rawLogDays": 0}}},
        )
        output, _archived = self.retention.read_log_output(
            self.registry_root,
            run_id,
            "stdout.log",
            tail=2,
            raw=False,
        )
        self.assertEqual(output, "line-198\nline-199\n")

    def test_retention_preserves_persistent_worktree_dirs(self):
        """Raw-log retention preserves persistent worktree directories outside the registry runs dir."""
        with tempfile.TemporaryDirectory() as fake_home, mock.patch.dict(
            "os.environ", {"HOME": fake_home}
        ):
            # Create a persistent-worktree run with logs.
            run_id, _alias = self.write_completed_run()
            old = (datetime.now(UTC) - timedelta(days=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            run_path = self.registry.run_directory(self.registry_root, run_id)

            # Simulate a persistent worktree under the fake home, never the
            # operator's real ~/.delegate runtime.
            worktree_root = (
                Path(fake_home)
                / ".delegate"
                / "worktrees"
                / "abc123def456"
                / "cursor-test"
            )
            worktree_root.mkdir(parents=True, exist_ok=True)
            (worktree_root / "mutated-file.txt").write_text(
                "worktree content\n", encoding="utf-8"
            )

            # Set worktreeStatus in state to simulate a persistent worktree run.
            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            state["finishedAt"] = old
            state["lastActivityAt"] = old
            state["worktreeStatus"] = "present"
            self.registry.write_json_atomic(run_path / "state.json", state)

            # Run retention pass with zero-day retention to force archival.
            zero_day_config = {
                "tracking": {"retention": {"enabled": True, "rawLogDays": 0}}
            }
            result = self.retention.run_retention_pass(self.registry_root, zero_day_config)

            # Assert raw logs were archived.
            self.assertEqual(result["archived"], 1)

            # Assert the worktree directory under fake ~/.delegate/worktrees/ still exists.
            self.assertTrue(
                worktree_root.exists(),
                "Worktree directory should be preserved after retention pass",
            )
            self.assertEqual(
                (worktree_root / "mutated-file.txt").read_text(encoding="utf-8"),
                "worktree content\n",
            )

            # Assert the registry entry still has worktreeStatus: present.
            state_after = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state_after.get("worktreeStatus"), "present")


if __name__ == "__main__":
    unittest.main()
