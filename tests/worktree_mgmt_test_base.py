from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.delegate_fixtures import seed_persistent_worktree_run, seed_plain_run

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

GIT_TEST_IDENTITY = (
    "-c",
    "user.name=Delegate Test",
    "-c",
    "user.email=delegate-test@example.com",
)


def load_delegate():
    return importlib.reload(importlib.import_module("delegate_agent.cli"))


def git(*args: str, cwd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *GIT_TEST_IDENTITY, *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


class WorktreeMgmtTestBase(unittest.TestCase):
    def setUp(self):
        self.delegate = load_delegate()

    def _make_repo(
        self, *, prefix: str = "delegate-wt-test-"
    ) -> tuple[tempfile.TemporaryDirectory, str]:
        repo = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(repo.cleanup)
        git("init", cwd=repo.name)
        readme = Path(repo.name) / "README.md"
        readme.write_text("hello\n", encoding="utf-8")
        git("add", "README.md", cwd=repo.name)
        git("commit", "-m", "init", cwd=repo.name)
        return repo, repo.name

    def _registry_root(self, repo_path: str) -> Path:
        return Path(repo_path) / ".delegate"

    def _run_cli(self, args: list[str], *, home: str) -> tuple[int, str, str]:
        self.assertNotEqual(Path(home).expanduser().resolve(), Path.home().resolve())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
            code = self.delegate.main(args, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def _seed_persistent_run(
        self,
        repo_path: str,
        *,
        alias: str = "cursor-4",
        harness: str = "cursor",
        branch: str | None = None,
        execution_cwd: str | None = None,
        worktree_status: str = "present",
        creation_oid: str | None = None,
        source_head_ref: str | object | None = object(),
        last_activity_at: str | None = None,
    ) -> tuple[str, str]:
        return seed_persistent_worktree_run(
            self.delegate,
            repo_path,
            git_runner=git,
            alias=alias,
            harness=harness,
            branch=branch,
            execution_cwd=execution_cwd,
            worktree_status=worktree_status,
            creation_oid=creation_oid,
            source_head_ref=source_head_ref,
            last_activity_at=last_activity_at,
        )

    def _seed_plain_run(
        self,
        repo_path: str,
        *,
        harness: str = "cursor",
        last_activity_at: str | None = None,
    ) -> tuple[str, str]:
        return seed_plain_run(
            self.delegate,
            repo_path,
            harness=harness,
            last_activity_at=last_activity_at,
        )

    def _tag_run_group(self, repo_path: str, run_id: str, group: str) -> None:
        registry_root = self._registry_root(repo_path)
        index = self.delegate.run_registry.load_index(registry_root)
        entry = index["runs"][run_id]
        if isinstance(entry, dict):
            entry["group"] = group
        self.delegate.run_registry.save_index(registry_root, index)
        run_path = self.delegate.run_registry.run_directory(registry_root, run_id)
        for filename in ("manifest.json", "state.json", "snapshot.json"):
            path = run_path / filename
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["group"] = group
                self.delegate.run_registry.write_json_atomic(path, payload)

    def _create_worktree_at(
        self,
        repo_path: str,
        branch: str,
        worktree_path: str,
        *,
        dirty_file: str | None = None,
    ) -> None:
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-B", branch, worktree_path, "HEAD", cwd=repo_path)
        if dirty_file:
            (Path(worktree_path) / dirty_file).write_text("dirty\n", encoding="utf-8")
