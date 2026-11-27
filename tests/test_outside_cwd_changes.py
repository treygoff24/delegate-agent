"""--isolation none: files a child changed outside its cwd are recorded and warned."""

import io
import json
import os
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

    def run_child(
        self,
        cwd: Path,
        *,
        registry_at: Path | None = None,
        engine: str = "codex",
        child: str = CHILD,
    ) -> dict:
        registry_root = run_registry.ensure_registry(registry_at or cwd, workspace_kind="directory")
        run_id, alias = run_registry.register_run(registry_root, harness=engine)
        context = runner.RunContext(
            registry_root=registry_root,
            run_id=run_id,
            alias=alias,
            harness=engine,
            engine=engine,
            mode="work",
            model=None,
            source_cwd=str(cwd),
            execution_cwd=str(cwd),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at=run_registry.utc_now_iso(),
        )
        runner.execute_tracked(
            [sys.executable, "-c", child],
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

    def test_omp_write_receipt_in_sibling_checkout_not_other_activity(self):
        repo = self.repo()
        lane = repo.parent / "lane"
        git(repo, "worktree", "add", "-b", "lane", str(lane))
        reported = repo / "reported.txt"
        other = repo / "unrelated.txt"
        other.write_text("not from this lane")
        start = {
            "type": "tool_execution_start",
            "toolCallId": "write1",
            "toolName": "write",
            "args": {"path": str(reported)},
        }
        finish = {
            "type": "tool_execution_end",
            "toolCallId": "write1",
            "toolName": "write",
            "isError": False,
        }
        child = (
            f"print({json.dumps(start)!r}, flush=True)\n"
            f"from pathlib import Path; Path({str(reported)!r}).write_text('changed')\n"
            f"print({json.dumps(finish)!r}, flush=True)\n"
        )
        state = self.run_child(lane, engine="omp", child=child)
        self.assertEqual(reported.read_text(), "changed")
        self.assertEqual(state["reportedSiblingWorktreeWrites"], [str(reported)])
        self.assertNotIn("outsideCwdChanges", state)

    def test_nested_worktree_receipts_belong_to_the_deepest_checkout(self):
        repo = self.repo()
        nested = repo / "lane"
        git(repo, "worktree", "add", "-b", "lane", str(nested))
        for cwd, other in ((repo, nested), (nested / "sub", repo)):
            audit = outside_cwd_changes.SiblingWriteAudit(str(cwd))
            audit.observe(str(cwd.parent / "keep.txt") if cwd != repo else "keep.txt")
            audit.observe(str(other / "reported.txt"))
            self.assertEqual(audit.examples, [str(other / "reported.txt")])

    def test_a_repo_root_cwd_is_not_snapshotted(self):
        repo = self.repo()
        self.assertIsNone(outside_cwd_changes.capture(str(repo)))

    def test_a_non_git_cwd_is_not_snapshotted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(outside_cwd_changes.capture(tmp))

    def test_snapshots_never_rewrite_the_callers_index(self):
        # A plain `git status` refreshes stale stat data by taking index.lock and
        # rewriting .git/index. In the caller's real tree that races the user's
        # own git commands, so every probe must run without optional locks.
        repo = self.repo()
        stale = repo / "sub" / "keep.txt"
        later = stale.stat().st_mtime_ns + 5_000_000_000
        os.utime(stale, ns=(later, later))  # same content, stale stat entry
        index = repo / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        snapshot = outside_cwd_changes.capture(str(repo / "sub"))
        self.assertIsNotNone(snapshot)
        self.assertEqual(outside_cwd_changes.changed_outside(snapshot), ())
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
