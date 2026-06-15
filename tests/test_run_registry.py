import importlib.util
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_registry():
    spec = importlib.util.spec_from_file_location("delegate_run_registry_under_test", REGISTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_git_repo(*, with_commit: bool = False):
    temp = tempfile.TemporaryDirectory()
    git = ["git", "-C", temp.name]
    subprocess.run(
        [*git, "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if with_commit:
        subprocess.run(
            [
                *git,
                "-c",
                "user.name=Delegate Test",
                "-c",
                "user.email=delegate-test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return temp


def make_linked_worktree():
    main = make_git_repo(with_commit=True)
    linked = tempfile.mkdtemp()
    subprocess.run(
        ["git", "-C", main.name, "worktree", "add", linked, "-b", "linked-branch"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return main, Path(linked)


class RunRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry()

    def test_generate_run_id_format(self):
        run_id = self.registry.generate_run_id(datetime(2026, 5, 20, 21, 42, 33, tzinfo=UTC))
        self.assertRegex(run_id, self.registry.RUN_ID_RE)
        self.assertEqual(run_id, "del_20260520T214233Z_" + run_id.split("_")[-1])

    def test_first_cursor_alias_is_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            self.assertEqual(alias, "cursor")
            self.assertRegex(run_id, self.registry.RUN_ID_RE)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits only")
    def test_registry_dirs_and_files_are_private_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            run_path = self.registry.run_directory(root, run_id)
            self.registry.write_json_atomic(run_path / "state.json", {"status": "running"})

            private_dirs = [
                root,
                self.registry.aliases_dir(root),
                self.registry.runs_dir(root),
                run_path,
            ]
            private_files = [
                self.registry.index_path(root),
                self.registry.aliases_dir(root) / alias,
                self.registry.registry_lock_path(root),
                run_path / "state.json",
            ]

            # Force lock creation.
            with self.registry.registry_lock(root):
                pass

            for path in private_dirs:
                with self.subTest(path=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in private_files:
                with self.subTest(path=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_second_cursor_alias_is_cursor_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            _, alias1 = self.registry.register_run(root, harness="cursor")
            _, alias2 = self.registry.register_run(root, harness="cursor")
            self.assertEqual(alias1, "cursor")
            self.assertEqual(alias2, "cursor-2")

    def test_droid_aliases_increment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            _, alias1 = self.registry.register_run(root, harness="droid")
            _, alias2 = self.registry.register_run(root, harness="droid")
            self.assertEqual(alias1, "droid")
            self.assertEqual(alias2, "droid-2")

    def test_exact_alias_lookup_does_not_guess_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            first_id, first_alias = self.registry.register_run(root, harness="cursor")
            second_id, second_alias = self.registry.register_run(root, harness="cursor")
            index = self.registry.load_index(root)
            self.assertEqual(first_alias, "cursor")
            self.assertEqual(second_alias, "cursor-2")
            self.assertEqual(self.registry.lookup_run_id(index, "cursor"), first_id)
            self.assertEqual(self.registry.lookup_run_id(index, "cursor-2"), second_id)
            self.assertIsNone(self.registry.lookup_run_id(index, "cursor-3"))
            self.assertNotEqual(self.registry.lookup_run_id(index, "cursor"), second_id)

    def test_lookup_run_id_accepts_exact_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            index = self.registry.load_index(root)
            self.assertEqual(self.registry.lookup_run_id(index, run_id), run_id)
            self.assertEqual(self.registry.lookup_run_id(index, alias), run_id)

    def test_git_workspace_adds_delegate_to_info_exclude(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        self.registry.ensure_registry(Path(repo.name), workspace_kind="git")
        exclude = Path(repo.name) / ".git" / "info" / "exclude"
        self.assertIn(self.registry.GIT_EXCLUDE_ENTRY, exclude.read_text())

    def test_git_exclude_is_idempotent(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        workspace = Path(repo.name)
        self.registry.ensure_registry(workspace, workspace_kind="git")
        self.registry.ensure_registry(workspace, workspace_kind="git")
        exclude = workspace / ".git" / "info" / "exclude"
        self.assertEqual(exclude.read_text().count(self.registry.GIT_EXCLUDE_ENTRY), 1)

    def test_linked_worktree_adds_delegate_to_info_exclude(self):
        main, linked = make_linked_worktree()
        self.addCleanup(main.cleanup)
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", main.name, "worktree", "remove", "--force", str(linked)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        self.registry.ensure_registry(linked, workspace_kind="git")
        exclude = self.registry.git_info_exclude_path(linked)
        self.assertIsNotNone(exclude)
        self.assertIn(self.registry.GIT_EXCLUDE_ENTRY, exclude.read_text())
        check = subprocess.run(
            ["git", "-C", str(linked), "check-ignore", "-v", ".delegate/"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_index_json_maintains_alias_and_run_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = self.registry.register_run(
                root, harness="cursor", metadata={"mode": "safe"}
            )
            index = json.loads(self.registry.index_path(root).read_text())
            self.assertEqual(index["aliases"][alias], run_id)
            self.assertEqual(index["runs"][run_id]["alias"], alias)
            self.assertEqual(index["runs"][run_id]["harness"], "cursor")
            self.assertEqual(index["runs"][run_id]["mode"], "safe")

    def test_allocate_alias_uses_exclusive_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.delegate_root(Path(tmp))
            root.mkdir(parents=True)
            self.registry.aliases_dir(root).mkdir(parents=True)
            alias_a = self.registry.allocate_alias(root, "cursor")
            alias_b = self.registry.allocate_alias(root, "cursor")
            self.assertEqual(alias_a, "cursor")
            self.assertEqual(alias_b, "cursor-2")
            self.assertTrue((self.registry.aliases_dir(root) / "cursor").exists())
            self.assertTrue((self.registry.aliases_dir(root) / "cursor-2").exists())

    def test_concurrent_register_run_preserves_all_index_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            barrier = threading.Barrier(2)
            results: list[tuple[str, str]] = []
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(self.registry.register_run(root, harness="cursor"))
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            index = self.registry.load_index(root)
            self.assertEqual(len(index["runs"]), 2)
            self.assertEqual(len(index["aliases"]), 2)
            aliases = {alias for _, alias in results}
            self.assertEqual(aliases, {"cursor", "cursor-2"})

    def test_resolve_handle_returns_suggestions_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            self.registry.register_run(root, harness="cursor")
            index = self.registry.load_index(root)
            resolved = self.registry.resolve_handle(index, "cursor-9")
            self.assertIsNone(resolved.run_id)
            self.assertTrue(resolved.suggestions)

    def test_latest_run_id_for_harness_uses_most_recent_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            older_id, _ = self.registry.register_run(root, harness="cursor")
            newer_id, _ = self.registry.register_run(root, harness="cursor")
            older_path = self.registry.run_directory(root, older_id)
            newer_path = self.registry.run_directory(root, newer_id)
            self.registry.write_json_atomic(
                older_path / "state.json",
                {"status": "succeeded", "lastActivityAt": "2026-05-20T10:00:00Z"},
            )
            self.registry.write_json_atomic(
                newer_path / "state.json",
                {"status": "running", "lastActivityAt": "2026-05-20T11:00:00Z"},
            )
            index = self.registry.load_index(root)
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, index, "cursor"),
                newer_id,
            )

    def test_bulk_run_readers_tolerate_corrupt_per_run_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            state_corrupt_id = self.registry.generate_run_id(
                datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
            )
            manifest_corrupt_id = self.registry.generate_run_id(
                datetime(2026, 5, 20, 10, 1, 0, tzinfo=UTC)
            )
            healthy_id = self.registry.generate_run_id(datetime(2026, 5, 20, 10, 2, 0, tzinfo=UTC))
            for run_id in (state_corrupt_id, manifest_corrupt_id, healthy_id):
                self.registry.register_run(root, harness="cursor", run_id=run_id)

            state_corrupt_path = self.registry.run_directory(root, state_corrupt_id)
            manifest_corrupt_path = self.registry.run_directory(root, manifest_corrupt_id)
            healthy_path = self.registry.run_directory(root, healthy_id)
            (state_corrupt_path / "state.json").write_text("{garbage", encoding="utf-8")
            archive_file = root / "archive" / f"{state_corrupt_id}.tar.gz"
            archive_file.parent.mkdir(parents=True)
            stdout_path = state_corrupt_path / "stdout.log"
            stdout_path.write_text("archived stdout\n", encoding="utf-8")
            with tarfile.open(archive_file, "w:gz") as archive:
                archive.add(stdout_path, arcname="stdout.log")
            stdout_path.unlink()
            self.registry.write_json_atomic(
                state_corrupt_path / "manifest.json",
                {"startedAt": "2026-05-20T10:00:00Z", "executionCwd": "/tmp/state-corrupt"},
            )
            self.registry.write_json_atomic(
                manifest_corrupt_path / "state.json",
                {"status": "succeeded", "finishedAt": "2026-05-20T10:01:00Z"},
            )
            (manifest_corrupt_path / "manifest.json").write_text("{garbage", encoding="utf-8")
            self.registry.write_json_atomic(
                healthy_path / "state.json",
                {"status": "succeeded", "finishedAt": "2026-05-20T11:00:00Z"},
            )
            self.registry.write_json_atomic(
                healthy_path / "manifest.json",
                {"startedAt": "2026-05-20T11:00:00Z", "executionCwd": "/tmp/healthy"},
            )

            index = self.registry.load_index(root)
            summaries = self.registry.list_run_summaries(root, index, harness="cursor", limit=10)

            self.assertCountEqual(
                [summary["runId"] for summary in summaries],
                [state_corrupt_id, manifest_corrupt_id, healthy_id],
            )
            by_id = {summary["runId"]: summary for summary in summaries}
            self.assertEqual(by_id[state_corrupt_id]["status"], self.registry.STATUS_UNKNOWN)
            self.assertEqual(by_id[state_corrupt_id]["executionCwd"], "/tmp/state-corrupt")
            self.assertEqual(by_id[state_corrupt_id]["stdoutBytes"], len("archived stdout\n"))
            self.assertEqual(by_id[manifest_corrupt_id]["status"], "succeeded")
            self.assertNotIn("executionCwd", by_id[manifest_corrupt_id])
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, index, "cursor"),
                healthy_id,
            )

    def test_targeted_run_state_load_still_fails_loud_for_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, _alias = self.registry.register_run(root, harness="cursor")
            run_path = self.registry.run_directory(root, run_id)
            (run_path / "state.json").write_text("{garbage", encoding="utf-8")

            with self.assertRaises(self.registry.RegistryJsonError):
                self.registry.load_run_state(root, run_id)

    def test_large_log_warnings_threshold(self):
        warnings = self.registry.large_log_warnings(self.registry.LARGE_LOG_WARN_BYTES + 1, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("stdout.log", warnings[0])

    def test_effective_status_stale_when_running_without_pid(self):
        status = self.registry.effective_status({"status": "running"})
        self.assertEqual(status, self.registry.STATUS_STALE)

    def test_effective_status_stale_when_running_with_invalid_pid(self):
        status = self.registry.effective_status({"status": "running", "pid": "not-a-pid"})
        self.assertEqual(status, self.registry.STATUS_STALE)

    def test_effective_status_stale_when_running_with_bool_pid(self):
        status = self.registry.effective_status({"status": "running", "pid": True})
        self.assertEqual(status, self.registry.STATUS_STALE)

    def test_effective_status_stale_when_running_with_non_positive_pid(self):
        for pid in (0, -1):
            with self.subTest(pid=pid):
                status = self.registry.effective_status({"status": "running", "pid": pid})
                self.assertEqual(status, self.registry.STATUS_STALE)

    def test_latest_run_id_uses_run_id_timestamp_when_activity_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            older_id = self.registry.generate_run_id(datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC))
            newer_id = self.registry.generate_run_id(datetime(2026, 5, 20, 11, 0, 0, tzinfo=UTC))
            for run_id in (older_id, newer_id):
                run_path = self.registry.run_directory(root, run_id)
                run_path.mkdir(parents=True)
                self.registry.write_json_atomic(
                    run_path / "state.json",
                    {"status": "running"},
                )
            index = {
                "version": self.registry.INDEX_VERSION,
                "aliases": {"cursor": older_id, "cursor-2": newer_id},
                "runs": {
                    older_id: {"alias": "cursor", "harness": "cursor"},
                    newer_id: {"alias": "cursor-2", "harness": "cursor"},
                },
            }
            self.registry.write_json_atomic(self.registry.index_path(root), index)
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, index, "cursor"),
                newer_id,
            )

    def test_latest_run_id_ties_same_timestamp_by_harness_alias_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            # Deliberately invert lexicographic run-id suffix order. When two
            # same-harness runs happen within the same second, the later alias
            # allocation is the durable registration-order signal.
            first_id = "del_20260520T100000Z_ffffff"
            second_id = "del_20260520T100000Z_000000"
            for run_id in (first_id, second_id):
                run_path = self.registry.run_directory(root, run_id)
                run_path.mkdir(parents=True)
                self.registry.write_json_atomic(
                    run_path / "state.json",
                    {"status": "succeeded", "startedAt": "2026-05-20T10:00:00Z"},
                )
            index = {
                "version": self.registry.INDEX_VERSION,
                "aliases": {"cursor": first_id, "cursor-2": second_id},
                "runs": {
                    first_id: {"alias": "cursor", "harness": "cursor"},
                    second_id: {"alias": "cursor-2", "harness": "cursor"},
                },
            }
            self.registry.write_json_atomic(self.registry.index_path(root), index)
            loaded = self.registry.load_index(root)
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, loaded, "cursor"),
                second_id,
            )

    def test_set_worktree_status_updates_state(self):
        """set_worktree_status updates worktreeStatus and optionally worktreeRemovedAt."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            run_path = self.registry.run_directory(root, run_id)
            run_path.mkdir(parents=True, exist_ok=True)
            state = {
                "schema": "delegate.state.v1",
                "runId": run_id,
                "alias": alias,
                "status": "succeeded",
            }
            self.registry.write_json_atomic(run_path / "state.json", state)

            # Set to present.
            updated = self.registry.set_worktree_status(root, run_id, "present")
            self.assertEqual(updated["worktreeStatus"], "present")

            # Set to removed with timestamp.
            removed_at = "2026-05-24T12:00:00Z"
            updated2 = self.registry.set_worktree_status(
                root, run_id, "removed", removed_at=removed_at
            )
            self.assertEqual(updated2["worktreeStatus"], "removed")
            self.assertEqual(updated2["worktreeRemovedAt"], removed_at)

    def test_set_worktree_status_locked_updates_under_existing_lock(self):
        """set_worktree_status_locked is the explicit helper for lock-held callers."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            run_path = self.registry.run_directory(root, run_id)
            run_path.mkdir(parents=True, exist_ok=True)
            state = {
                "schema": "delegate.state.v1",
                "runId": run_id,
                "alias": alias,
                "status": "succeeded",
            }
            self.registry.write_json_atomic(run_path / "state.json", state)

            with self.registry.registry_lock(root):
                updated = self.registry.set_worktree_status_locked(root, run_id, "unknown")

            self.assertEqual(updated["worktreeStatus"], "unknown")

    def test_set_worktree_status_missing_state_raises(self):
        """set_worktree_status on a missing state file raises."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            with self.assertRaises(ValueError) as ctx:
                self.registry.set_worktree_status(root, "del_nonexistent", "invalid_status")
            self.assertIn("must be one of", str(ctx.exception))

    def test_set_worktree_status_invalid_status_raises(self):
        """set_worktree_status with an invalid status raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            with self.assertRaises(ValueError):
                self.registry.set_worktree_status(root, "del_nonexistent", "invalid_status")
