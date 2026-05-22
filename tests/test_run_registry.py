import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"


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
            self.assertEqual(self.registry.lookup_alias(index, "cursor"), first_id)
            self.assertEqual(self.registry.lookup_alias(index, "cursor-2"), second_id)
            self.assertIsNone(self.registry.lookup_alias(index, "cursor-3"))
            self.assertNotEqual(self.registry.lookup_alias(index, "cursor"), second_id)

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
