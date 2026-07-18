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

    def test_first_cursor_alias_is_numbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            self.assertEqual(alias, "cursor-1")
            self.assertRegex(run_id, self.registry.RUN_ID_RE)

    def test_pi_is_a_tracked_harness(self):
        self.assertIn("pi", self.registry.HARNESS_NAMES)

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

    @unittest.skipUnless(os.name == "posix", "POSIX symlink protections only")
    def test_registry_rejects_delegate_directory_symlink(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = Path(tmp)
            outside_path = Path(outside)
            outside_path.chmod(0o755)
            (workspace / ".delegate").symlink_to(outside_path, target_is_directory=True)

            with self.assertRaises(OSError):
                self.registry.ensure_registry(workspace, workspace_kind="directory")

            self.assertEqual(list(outside_path.iterdir()), [])
            self.assertEqual(stat.S_IMODE(outside_path.stat().st_mode), 0o755)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink protections only")
    def test_registry_rejects_nested_directory_symlink(self):
        for component in ("aliases", "runs"):
            with (
                self.subTest(component=component),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as outside,
            ):
                workspace = Path(tmp)
                root = workspace / ".delegate"
                root.mkdir()
                outside_path = Path(outside)
                outside_path.chmod(0o755)
                (root / component).symlink_to(outside_path, target_is_directory=True)

                with self.assertRaises(OSError):
                    self.registry.ensure_registry(workspace, workspace_kind="directory")

                self.assertEqual(list(outside_path.iterdir()), [])
                self.assertEqual(stat.S_IMODE(outside_path.stat().st_mode), 0o755)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink protections only")
    def test_private_file_helpers_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp) / ".delegate"
            root.mkdir()
            external = Path(outside) / "external.txt"
            external.write_text("keep", encoding="utf-8")
            external.chmod(0o644)
            link = root / "claim"
            link.symlink_to(external)

            with self.assertRaises(OSError):
                self.registry.write_private_text(link, "overwrite\n")

            self.assertEqual(external.read_text(encoding="utf-8"), "keep")
            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o644)

            external_json = Path(outside) / "external.json"
            external_json.write_text('{"keep": true}\n', encoding="utf-8")
            json_link = root / "state.json"
            json_link.symlink_to(external_json)

            with self.assertRaises(OSError):
                self.registry.write_json_atomic(json_link, {"keep": False})
            with self.assertRaises(self.registry.RegistryJsonError):
                self.registry.read_json_object(json_link)

            self.assertEqual(external_json.read_text(encoding="utf-8"), '{"keep": true}\n')

    def test_second_cursor_alias_is_cursor_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            _, alias1 = self.registry.register_run(root, harness="cursor")
            _, alias2 = self.registry.register_run(root, harness="cursor")
            self.assertEqual(alias1, "cursor-1")
            self.assertEqual(alias2, "cursor-2")

    def test_droid_aliases_increment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            _, alias1 = self.registry.register_run(root, harness="droid")
            _, alias2 = self.registry.register_run(root, harness="droid")
            self.assertEqual(alias1, "droid-1")
            self.assertEqual(alias2, "droid-2")

    def test_exact_alias_lookup_does_not_guess_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            _first_id, first_alias = self.registry.register_run(root, harness="cursor")
            second_id, second_alias = self.registry.register_run(root, harness="cursor")
            index = self.registry.load_index(root)
            self.assertEqual(first_alias, "cursor-1")
            self.assertEqual(second_alias, "cursor-2")
            self.assertIsNone(self.registry.lookup_run_id(index, "cursor"))
            self.assertEqual(self.registry.lookup_run_id(index, "cursor-2"), second_id)
            self.assertIsNone(self.registry.lookup_run_id(index, "cursor-3"))
            resolved = self.registry.resolve_handle(index, "cursor", registry_root=root)
            self.assertEqual(resolved.run_id, second_id)
            self.assertEqual(resolved.resolution_kind, "latest")
            self.assertEqual(resolved.resolved_handle, "cursor-2")

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
            self.assertEqual(alias_a, "cursor-1")
            self.assertEqual(alias_b, "cursor-2")
            self.assertTrue((self.registry.aliases_dir(root) / "cursor-1").exists())
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
            self.assertEqual(aliases, {"cursor-1", "cursor-2"})

    def test_parallel_first_registry_init_preserves_all_index_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            barrier = threading.Barrier(4)
            results: list[tuple[str, str]] = []
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    barrier.wait(timeout=5)
                    root = self.registry.ensure_registry(workspace, workspace_kind="directory")
                    results.append(self.registry.register_run(root, harness="codex"))
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 4)
            root = self.registry.registry_root(workspace)
            index = self.registry.load_index(root)
            self.assertEqual(len(index["runs"]), 4)
            self.assertEqual(set(index["aliases"]), {"codex-1", "codex-2", "codex-3", "codex-4"})

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

    def test_latest_run_id_for_harness_model_uses_model_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            old_glm, _ = self.registry.register_run(
                root, harness="droid", metadata={"modelAlias": "glm", "modelResolved": "glm-id"}
            )
            kimi, _ = self.registry.register_run(
                root, harness="droid", metadata={"modelAlias": "kimi", "modelResolved": "kimi-id"}
            )
            new_glm, _ = self.registry.register_run(
                root, harness="droid", metadata={"modelAlias": "glm", "modelResolved": "glm-id"}
            )
            for run_id, ts in (
                (old_glm, "2026-05-20T10:00:00Z"),
                (kimi, "2026-05-20T10:05:00Z"),
                (new_glm, "2026-05-20T10:10:00Z"),
            ):
                self.registry.write_json_atomic(
                    self.registry.run_directory(root, run_id) / "state.json",
                    {"status": "succeeded", "lastActivityAt": ts},
                )
            index = self.registry.load_index(root)
            self.assertEqual(
                self.registry.latest_run_id_for_harness_model(root, index, "droid", "glm"),
                new_glm,
            )
            resolved = self.registry.resolve_handle(index, "droid:glm", registry_root=root)
            self.assertEqual(resolved.run_id, new_glm)
            self.assertEqual(resolved.resolution_kind, "latest_model")

    def test_opencode_handle_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            first, _ = self.registry.register_run(
                root, harness="opencode", metadata={"modelAlias": "fast"}
            )
            second, _ = self.registry.register_run(
                root, harness="opencode", metadata={"modelAlias": "fast"}
            )
            for run_id, ts in (
                (first, "2026-05-20T10:00:00Z"),
                (second, "2026-05-20T10:10:00Z"),
            ):
                self.registry.write_json_atomic(
                    self.registry.run_directory(root, run_id) / "state.json",
                    {"status": "succeeded", "lastActivityAt": ts},
                )
            index = self.registry.load_index(root)
            latest = self.registry.resolve_handle(index, "opencode", registry_root=root)
            self.assertEqual(latest.run_id, second)
            self.assertEqual(latest.resolution_kind, "latest")
            aliased = self.registry.resolve_handle(index, "opencode:fast", registry_root=root)
            self.assertEqual(aliased.run_id, second)
            self.assertEqual(aliased.resolution_kind, "latest_model")

    def test_latest_run_id_for_harness_model_falls_back_to_manifest_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            run_id, _ = self.registry.register_run(root, harness="droid")
            run_path = self.registry.run_directory(root, run_id)
            self.registry.write_json_atomic(
                run_path / "manifest.json",
                {
                    "schema": self.registry.MANIFEST_SCHEMA,
                    "runId": run_id,
                    "harness": "droid",
                    "modelAlias": "glm",
                    "modelResolved": "glm-id",
                    "startedAt": "2026-05-20T10:00:00Z",
                },
            )
            index = self.registry.load_index(root)

            self.assertEqual(
                self.registry.latest_run_id_for_harness_model(root, index, "droid", "glm"),
                run_id,
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
        self.assertIn("50 MiB", warnings[0])

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
                "aliases": {"cursor-1": older_id, "cursor-2": newer_id},
                "runs": {
                    older_id: {"alias": "cursor-1", "harness": "cursor"},
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
                "aliases": {"cursor-1": first_id, "cursor-2": second_id},
                "runs": {
                    first_id: {"alias": "cursor-1", "harness": "cursor"},
                    second_id: {"alias": "cursor-2", "harness": "cursor"},
                },
            }
            self.registry.write_json_atomic(self.registry.index_path(root), index)
            loaded = self.registry.load_index(root)
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, loaded, "cursor"),
                second_id,
            )

    def test_latest_run_id_ties_same_timestamp_and_sequence_by_insertion_order(self):
        # A pre-v0.10 literal alias ``codex`` (sequence 1) and a v0.10
        # ``codex-1`` (also sequence 1) with identical activity timestamps and
        # equal alias sequences form the exact tie the tertiary key resolves.
        # The later-registered run must win, NOT by run-id string luck: run A is
        # registered first but gets the lexicographically GREATER random suffix,
        # while run B is registered second with a suffix that sorts BELOW A's.
        # If the tiebreaker were lexicographic run_id, A would win. These
        # hand-built legacy entries carry no ``registrationOrdinal``, so the
        # insertion-index fallback applies; the runs mapping is enumerated in
        # dict order, so a higher insertion index is newer here.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            first_id = "del_20260520T100000Z_ffffff"  # registered first, alias codex
            second_id = "del_20260520T100000Z_000000"  # registered second, alias codex-1
            # Guard: the later-registered run's id sorts lexicographically below
            # the earlier run's id, so a string-based tiebreaker would pick the
            # wrong (older) run.
            self.assertLess(second_id, first_id)
            for run_id in (first_id, second_id):
                run_path = self.registry.run_directory(root, run_id)
                run_path.mkdir(parents=True)
                self.registry.write_json_atomic(
                    run_path / "state.json",
                    {"status": "succeeded", "startedAt": "2026-05-20T10:00:00Z"},
                )
            # Build the index in registration order (A first, B second). The
            # runs mapping is enumerated in dict order, so insertion index is
            # true chronology here for the legacy (no-ordinal) fallback. Passing
            # the in-memory index (not a round-tripped load_index) preserves
            # that insertion order, which is what the fallback relies on.
            index = {
                "version": self.registry.INDEX_VERSION,
                "aliases": {"codex": first_id, "codex-1": second_id},
                "runs": {
                    first_id: {"alias": "codex", "harness": "codex"},
                    second_id: {"alias": "codex-1", "harness": "codex"},
                },
            }
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, index, "codex"),
                second_id,
            )

    def test_latest_run_id_tie_survives_save_index_reload_round_trip(self):
        # The insertion-index tiebreaker degrades to random-suffix order after a
        # save_index/load_index round trip, because save_index persists with
        # sort_keys=True and reorders the ``runs`` mapping lexicographically by
        # run_id. The explicit ``registrationOrdinal`` stamped by register_run
        # is the chronology source that survives reload. Register two runs with
        # identical activity timestamps and equal alias sequences (both rank as
        # alias sequence 1, via the legacy literal ``codex`` and ``codex-1``),
        # adversarial run_id suffixes (the later-registered run gets the
        # lexicographically SMALLER suffix, so a string tiebreaker would pick
        # the older run), then reload via the real save_index/load_index cycle
        # and assert the later-registered run still wins.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            # Adversarial suffixes: first-registered gets a LARGE suffix,
            # second-registered gets a SMALL suffix. After save_index reloads
            # the runs mapping lexicographically by run_id, the first-registered
            # run enumerates BEFORE the second-registered run. The ordinal makes
            # the result independent of suffix luck.
            first_id = "del_20260520T100000Z_ffffff"
            second_id = "del_20260520T100000Z_000000"
            self.assertLess(second_id, first_id)  # later-registered sorts below
            # Register through the real register_run path so ordinals are
            # stamped and persisted. Both runs share the same activity second.
            self.registry.register_run(root, harness="codex", run_id=first_id)
            self.registry.register_run(root, harness="codex", run_id=second_id)
            # register_run allocates incrementing aliases (codex-1, codex-2);
            # override the index entries' aliases to the adversarial
            # equal-sequence pair (legacy literal ``codex`` and ``codex-1``,
            # both alias sequence 1) so the tertiary ordinal key is the only
            # thing that breaks the tie.
            index = self.registry.load_index(root)
            index["runs"][first_id]["alias"] = "codex"
            index["runs"][second_id]["alias"] = "codex-1"
            # Sanity: ordinals were stamped and are strictly increasing.
            self.assertEqual(index["runs"][first_id]["registrationOrdinal"], 1)
            self.assertEqual(index["runs"][second_id]["registrationOrdinal"], 2)
            self.registry.save_index(root, index)
            for run_id in (first_id, second_id):
                self.registry.write_json_atomic(
                    self.registry.run_directory(root, run_id) / "state.json",
                    {"status": "succeeded", "startedAt": "2026-05-20T10:00:00Z"},
                )
            # Reload from disk: sort_keys=True reorders runs lexicographically.
            reloaded = self.registry.load_index(root)
            # Guard: after reload, the LATER-registered run (second_id, smaller
            # suffix) enumerates FIRST (insertion index 0), and the
            # earlier-registered run (first_id, larger suffix) enumerates
            # SECOND (insertion index 1). This proves the round trip does not
            # preserve in-memory insertion order: a pure insertion-index
            # fallback would now rank the EARLIER-registered run higher
            # (insertion index 1 > 0), picking the wrong (older) run. Only the
            # explicit ordinal rescues the tie.
            reloaded_order = list(reloaded["runs"].keys())
            self.assertEqual(reloaded_order[0], second_id)
            self.assertEqual(reloaded_order[1], first_id)
            # The later-registered run must still win after reload, by ordinal
            # (2 > 1), NOT by insertion index (which would pick first_id).
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, reloaded, "codex"),
                second_id,
            )

    def test_latest_run_id_legacy_index_without_ordinal_resolves_via_insertion_fallback(self):
        # Legacy index entries that predate ``registrationOrdinal`` must still
        # resolve deterministically via the insertion-index fallback. Build a
        # persisted index with two same-timestamp, same-sequence legacy entries
        # (no registrationOrdinal field), adversarial suffixes, reload it, and
        # assert the tie resolves to the run that enumerates later after the
        # lexicographic reload -- i.e. the lexicographically GREATER run_id,
        # which is the deterministic legacy behavior.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.registry.ensure_registry(Path(tmp), workspace_kind="directory")
            smaller_id = "del_20260520T100000Z_000000"
            larger_id = "del_20260520T100000Z_ffffff"
            self.assertLess(smaller_id, larger_id)
            for run_id in (smaller_id, larger_id):
                run_path = self.registry.run_directory(root, run_id)
                run_path.mkdir(parents=True)
                self.registry.write_json_atomic(
                    run_path / "state.json",
                    {"status": "succeeded", "startedAt": "2026-05-20T10:00:00Z"},
                )
            legacy_index = {
                "version": self.registry.INDEX_VERSION,
                "aliases": {"codex": smaller_id, "codex-1": larger_id},
                "runs": {
                    smaller_id: {"alias": "codex", "harness": "codex"},
                    larger_id: {"alias": "codex-1", "harness": "codex"},
                },
            }
            # No registrationOrdinal on either entry -- legacy.
            self.assertNotIn("registrationOrdinal", legacy_index["runs"][smaller_id])
            self.assertNotIn("registrationOrdinal", legacy_index["runs"][larger_id])
            self.registry.save_index(root, legacy_index)
            reloaded = self.registry.load_index(root)
            # After lexicographic reload, larger_id enumerates after smaller_id,
            # so its insertion fallback index is greater and it wins the tie.
            reloaded_order = list(reloaded["runs"].keys())
            self.assertEqual(reloaded_order[0], smaller_id)
            self.assertEqual(reloaded_order[1], larger_id)
            self.assertEqual(
                self.registry.latest_run_id_for_harness(root, reloaded, "codex"),
                larger_id,
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

            updated = self.registry.set_worktree_status(root, run_id, "present")
            self.assertEqual(updated["worktreeStatus"], "present")

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

    def test_naive_timestamp_is_normalized_to_utc_for_age_calculation(self):
        parsed = self.registry.parse_utc_timestamp("2026-07-16T12:00:00")

        self.assertIsNotNone(parsed)
        self.assertEqual(self.registry._format_age("2026-07-16T12:00:00", now=parsed), "0s ago")
