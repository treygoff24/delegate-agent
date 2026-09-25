"""--isolation none: files a child changed outside its cwd are recorded and warned."""

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from delegate_agent import outside_cwd_changes, run_registry, runner

CHILD = (
    "from pathlib import Path\n"
    "Path('inside.txt').write_text('in')\n"
    "Path('../escaped.txt').write_text('out')\n"
    "Path('../already-dirty.txt').write_text('edited again, same dirty status')\n"
    "print('done', flush=True)\n"
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class OutsideCwdChangesTests(unittest.TestCase):
    def repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve() / "repo"
        (repo / "sub").mkdir(parents=True)
        git(repo, "init", "-q")
        (repo / "already-dirty.txt").write_text("base", encoding="utf-8")
        (repo / "sub" / "keep.txt").write_text("keep", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        (repo / "already-dirty.txt").write_text("dirty before launch", encoding="utf-8")
        return repo

    def run_child(self, cwd: Path, *, registry_at: Path | None = None) -> dict:
        registry_root = run_registry.ensure_registry(registry_at or cwd, workspace_kind="directory")
        run_id, alias = run_registry.register_run(registry_root, harness="codex")
        context = runner.RunContext(
            registry_root=registry_root,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode="work",
            model=None,
            source_cwd=str(cwd),
            execution_cwd=str(cwd),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
        )
        runner.execute_tracked(
            [sys.executable, "-c", CHILD],
            str(cwd),
            context,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        return run_registry.load_run_state(registry_root, run_id)

    def test_changes_outside_cwd_are_recorded_and_warned(self):
        repo = self.repo()
        state = self.run_child(repo / "sub")
        record = state["outsideCwdChanges"]
        self.assertEqual(record["repository"], str(repo))
        # The pre-dirty file keeps status " M" but its content changed: still caught.
        self.assertEqual(record["examples"], ["already-dirty.txt", "escaped.txt"])
        self.assertEqual(record["count"], 2)
        notes = [note for note in state.get("warnings", []) if "outside its working" in note]
        self.assertEqual(len(notes), 1)
        self.assertIn("escaped.txt", notes[0])
        self.assertNotIn("inside.txt", notes[0])

    def test_delegate_registry_writes_outside_cwd_are_not_blamed_on_the_child(self):
        repo = self.repo()
        # A directory-kind registry at the repo root is not git-ignored, so
        # its run files show up in status unless they are filtered as ours.
        state = self.run_child(repo / "sub", registry_at=repo)
        self.assertEqual(
            state["outsideCwdChanges"]["examples"], ["already-dirty.txt", "escaped.txt"]
        )

    def test_a_repo_root_cwd_is_not_snapshotted(self):
        repo = self.repo()
        self.assertIsNone(outside_cwd_changes.capture(str(repo)))

    def test_a_non_git_cwd_is_not_snapshotted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(outside_cwd_changes.capture(tmp))


if __name__ == "__main__":
    unittest.main()
