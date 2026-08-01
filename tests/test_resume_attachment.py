from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import worktree_records
from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git


class ResumeAttachmentTests(WorktreeMgmtTestBase):
    def _fake_agent(self, *, commit: bool = False) -> tempfile.TemporaryDirectory:
        temp = tempfile.TemporaryDirectory(prefix="delegate-resume-agent-")
        agent = Path(temp.name) / "agent"
        body = ["#!/bin/sh", "set -eu"]
        if commit:
            body.extend(
                [
                    "printf '%s\\n' child > resumed-child.txt",
                    "git add resumed-child.txt",
                    "git -c user.name='Delegate Test' -c user.email=delegate-test@example.com commit -m resumed-child >/dev/null",
                ]
            )
        body.append("exit 0")
        agent.write_text("\n".join(body) + "\n", encoding="utf-8")
        agent.chmod(0o755)
        return temp

    def _source_prompt(self, repo_path: str, run_id: str, text: str = "original task") -> None:
        root = self._registry_root(repo_path)
        run_path = self.delegate.run_registry.run_directory(root, run_id)
        self.delegate.run_registry.write_private_text(run_path / "prompt.txt", text)
        manifest = self.delegate.run_registry.load_run_manifest(root, run_id)
        manifest["promptFile"] = "prompt.txt"
        self.delegate.run_registry.write_json_atomic(run_path / "manifest.json", manifest)

    def _owner(
        self,
        repo_path: str,
        fake_home: str,
        *,
        include_dirty: bool = False,
        forbid_commit: bool = False,
        alias: str = "cursor-owner",
        execution_cwd: str | None = None,
    ) -> tuple[str, str, str, str]:
        branch = "delegate/resume-owner"
        worktree_path = execution_cwd or str(Path(fake_home).resolve() / "worktree" / "owner")
        run_id, owner_alias = self._seed_persistent_run(
            repo_path,
            alias=alias,
            branch=branch,
            execution_cwd=worktree_path,
        )
        self._create_worktree_at(repo_path, branch, worktree_path)
        self._source_prompt(repo_path, run_id)
        root = self._registry_root(repo_path)
        manifest = self.delegate.run_registry.load_run_manifest(root, run_id)
        if include_dirty:
            manifest["includeDirty"] = True
        if forbid_commit:
            manifest["commitPolicy"] = {"forbidCommit": True}
        self.delegate.run_registry.write_json_atomic(
            self.delegate.run_registry.run_directory(root, run_id) / "manifest.json", manifest
        )
        return run_id, owner_alias, worktree_path, branch

    def _resume(
        self,
        repo_path: str,
        fake_home: str,
        owner_alias: str,
        *extra: str,
        include_dirty: bool = False,
    ) -> tuple[int, dict, str]:
        args = ["--cwd", repo_path, "--json", "resume"]
        if include_dirty:
            args.append("--include-dirty")
        args.extend([owner_alias, *extra])
        code, stdout, stderr = self._run_cli(args, home=fake_home)
        return code, json.loads(stdout), stderr

    def _child_manifest(self, repo_path: str, owner_id: str) -> tuple[str, dict, dict]:
        root = self._registry_root(repo_path)
        index = self.delegate.run_registry.load_index(root)
        child_id = next(
            run_id
            for run_id, entry in index["runs"].items()
            if run_id != owner_id and isinstance(entry, dict)
        )
        run_path = self.delegate.run_registry.run_directory(root, child_id)
        return (
            child_id,
            json.loads((run_path / "manifest.json").read_text(encoding="utf-8")),
            json.loads((run_path / "state.json").read_text(encoding="utf-8")),
        )

    def _write_live_attachment(self, repo_path: str, worktree_path: str) -> tuple[str, str]:
        root = self._registry_root(repo_path)
        run_id, alias = self.delegate.run_registry.register_run(
            root,
            harness="cursor",
            metadata={
                "mode": "work",
                "cwd": repo_path,
                "worktreeAttachment": {
                    "sourceRunId": "del_owner",
                    "sourceAlias": "cursor-owner",
                    "path": worktree_path,
                    "startHeadOid": git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip(),
                },
            },
        )
        run_path = self.delegate.run_registry.run_directory(root, run_id)
        self.delegate.run_registry.write_json_atomic(
            run_path / "manifest.json",
            {
                "schema": self.delegate.run_registry.MANIFEST_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "harness": "cursor",
                "engine": "cursor",
                "mode": "work",
                "cwd": repo_path,
                "executionCwd": worktree_path,
                "isolationLifecycle": "attached",
                "worktreeAttachment": {
                    "sourceRunId": "del_owner",
                    "sourceAlias": "cursor-owner",
                    "path": worktree_path,
                    "startHeadOid": git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip(),
                },
                "startedAt": "2000-01-01T00:00:00Z",
            },
        )
        self.delegate.run_registry.write_json_atomic(
            run_path / "state.json",
            {
                "schema": self.delegate.run_registry.STATE_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "status": "running",
                "pid": os.getpid(),
                "lastActivityAt": "2000-01-01T00:00:00Z",
            },
        )
        return run_id, alias

    def test_resume_persistent_worktree_attaches_without_adding_worktree(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home, self._fake_agent() as fake_agent:
            owner_id, owner_alias, worktree_path, _branch = self._owner(repo_path, fake_home)
            before = git("worktree", "list", "--porcelain", cwd=repo_path).stdout
            with mock.patch.dict(
                os.environ,
                {"PATH": fake_agent + os.pathsep + os.environ.get("PATH", "")},
            ):
                code, payload, stderr = self._resume(repo_path, fake_home, owner_alias, "continue")
            self.assertEqual(code, 0, stderr)
            self.assertTrue(payload.get("ok", True))
            after = git("worktree", "list", "--porcelain", cwd=repo_path).stdout
            self.assertEqual(after, before, "resume attachment must not run git worktree add")

            child_id, manifest, _state = self._child_manifest(repo_path, owner_id)
            self.assertNotEqual(child_id, owner_id)
            self.assertEqual(manifest["isolationLifecycle"], "attached")
            self.assertEqual(
                manifest["worktreeAttachment"],
                {
                    "sourceRunId": owner_id,
                    "sourceAlias": owner_alias,
                    "path": worktree_path,
                    "startHeadOid": git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip(),
                },
            )
            records = worktree_records.load_persistent_records(self._registry_root(repo_path))
            self.assertEqual([record["runId"] for record in records], [owner_id])

    def test_attached_runs_never_create_a_second_derived_worktree_record(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            owner_id, _owner_alias, worktree_path, _branch = self._owner(repo_path, fake_home)
            root = self._registry_root(repo_path)
            for _ in range(3):
                self._write_live_attachment(repo_path, worktree_path)
            records = worktree_records.load_persistent_records(root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["runId"], owner_id)

    def test_resume_of_attached_alias_reuses_the_original_owner_worktree(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home, self._fake_agent() as fake_agent:
            owner_id, owner_alias, worktree_path, _branch = self._owner(repo_path, fake_home)
            before = git("worktree", "list", "--porcelain", cwd=repo_path).stdout
            with mock.patch.dict(
                os.environ, {"PATH": fake_agent + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _payload, stderr = self._resume(repo_path, fake_home, owner_alias, "first")
                self.assertEqual(code, 0, stderr)
                first_id, first_manifest, _state = self._child_manifest(repo_path, owner_id)
                code, _payload, stderr = self._resume(
                    repo_path, fake_home, first_manifest["alias"], "second"
                )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(git("worktree", "list", "--porcelain", cwd=repo_path).stdout, before)

            root = self._registry_root(repo_path)
            index = self.delegate.run_registry.load_index(root)
            child_ids = [run_id for run_id in index["runs"] if run_id != owner_id]
            self.assertEqual(len(child_ids), 2)
            second_id = next(run_id for run_id in child_ids if run_id != first_id)
            second_manifest = self.delegate.run_registry.load_run_manifest(root, second_id)
            self.assertEqual(second_manifest["isolationLifecycle"], "attached")
            self.assertEqual(second_manifest["worktreeAttachment"]["sourceRunId"], owner_id)
            self.assertEqual(second_manifest["worktreeAttachment"]["path"], worktree_path)
            records = worktree_records.load_persistent_records(root)
            self.assertEqual([record["runId"] for record in records], [owner_id])

    def test_live_attachment_guards_remove_gc_and_runs_prune(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            owner_id, owner_alias, worktree_path, _branch = self._owner(repo_path, fake_home)
            attached_id, attached_alias = self._write_live_attachment(repo_path, worktree_path)
            root = self._registry_root(repo_path)

            code, stdout, _stderr = self._run_cli(
                ["--cwd", repo_path, "--json", "worktree", "remove", owner_alias, "--force"],
                home=fake_home,
            )
            payload = json.loads(stdout)
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(payload["error"], "worktree_attached")
            self.assertIn(attached_alias, payload["message"])
            self.assertTrue(Path(worktree_path).exists())

            gc_payload = self.delegate.worktree_mgmt.gc_worktrees(root)
            warning = next(item for item in gc_payload["warnings"] if item.get("runId") == owner_id)
            self.assertEqual(warning["reason"], "live_attachment")
            self.assertIn(attached_alias, {item.get("alias") for item in warning["attachedRuns"]})
            self.assertTrue(Path(worktree_path).exists())

            # Make the owner old enough that the only reason it is skipped is
            # the live attachment lease; never delete the records in this test.
            owner_state = self.delegate.run_registry.load_run_state(root, owner_id)
            owner_state["lastActivityAt"] = "2000-01-01T00:00:00Z"
            self.delegate.run_registry.write_json_atomic(
                self.delegate.run_registry.run_directory(root, owner_id) / "state.json",
                owner_state,
            )
            prune_payload = self.delegate.run_registry.prune_runs(root, older_than_days=0)
            skipped = next(item for item in prune_payload["skipped"] if item["runId"] == owner_id)
            self.assertEqual(skipped["reason"], "live_attachment")
            self.assertIn(attached_alias, json.dumps(skipped))
            self.assertNotEqual(attached_id, owner_id)

    def test_forbid_commit_is_inherited_and_enforced_on_attached_run(self):
        _repo, repo_path = self._make_repo()
        with (
            tempfile.TemporaryDirectory() as fake_home,
            self._fake_agent(commit=True) as fake_agent,
        ):
            owner_id, owner_alias, worktree_path, _branch = self._owner(
                repo_path, fake_home, forbid_commit=True
            )
            with mock.patch.dict(
                os.environ,
                {"PATH": fake_agent + os.pathsep + os.environ.get("PATH", "")},
            ):
                code, payload, stderr = self._resume(repo_path, fake_home, owner_alias, "continue")
            self.assertEqual(code, 1, stderr)
            self.assertEqual(payload["error"], "commit_policy_violated")
            _child_id, manifest, state = self._child_manifest(repo_path, owner_id)
            self.assertEqual(manifest["commitPolicy"], {"forbidCommit": True})
            self.assertTrue(state["commitPolicy"]["forbidCommit"])
            self.assertTrue(state["commitPolicy"]["violated"])
            self.assertTrue(Path(worktree_path, "resumed-child.txt").exists())

    def test_attachment_start_head_is_used_for_commit_attribution(self):
        _repo, repo_path = self._make_repo()
        with (
            tempfile.TemporaryDirectory() as fake_home,
            self._fake_agent(commit=True) as fake_agent,
        ):
            owner_id, owner_alias, worktree_path, _branch = self._owner(repo_path, fake_home)
            (Path(worktree_path) / "owner-change.txt").write_text("owner\n", encoding="utf-8")
            git("add", "owner-change.txt", cwd=worktree_path)
            git("commit", "-m", "owner change", cwd=worktree_path)
            attach_start = git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip()
            with mock.patch.dict(
                os.environ,
                {"PATH": fake_agent + os.pathsep + os.environ.get("PATH", "")},
            ):
                code, _payload, stderr = self._resume(repo_path, fake_home, owner_alias, "continue")
            self.assertEqual(code, 0, stderr)
            _child_id, manifest, state = self._child_manifest(repo_path, owner_id)
            self.assertEqual(manifest["worktreeAttachment"]["startHeadOid"], attach_start)
            summary = state["workSummary"]
            self.assertEqual(summary["baseCommit"], attach_start)
            self.assertEqual(summary["commitsCreatedCount"], 1)

    def test_include_dirty_is_dropped_with_note_and_explicit_attach_is_rejected(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home, self._fake_agent() as fake_agent:
            owner_id, owner_alias, _worktree_path, _branch = self._owner(
                repo_path, fake_home, include_dirty=True
            )
            with mock.patch.dict(
                os.environ,
                {"PATH": fake_agent + os.pathsep + os.environ.get("PATH", "")},
            ):
                code, _payload, stderr = self._resume(repo_path, fake_home, owner_alias, "continue")
            self.assertEqual(code, 0, stderr)
            self.assertIn("includeDirty is creation-only and was dropped", stderr)
            self.assertEqual(stderr.count("includeDirty is creation-only and was dropped"), 1)
            _child_id, manifest, state = self._child_manifest(repo_path, owner_id)
            self.assertNotIn("includeDirty", manifest)
            self.assertNotIn("includeDirty", state)

            code, payload, _stderr = self._resume(
                repo_path, fake_home, owner_alias, "continue", include_dirty=True
            )
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(payload["error"], "invalid_option_combination")

    def test_reentry_validation_refuses_missing_symlink_unregistered_and_branch_mismatch(self):
        cases = ("missing", "symlink", "unregistered", "branch")
        for case in cases:
            with self.subTest(case=case):
                _repo, repo_path = self._make_repo()
                with tempfile.TemporaryDirectory() as fake_home:
                    owner_id, owner_alias, worktree_path, _branch = self._owner(
                        repo_path, fake_home
                    )
                    if case == "missing":
                        shutil.rmtree(worktree_path)
                        expected = "worktree_missing"
                    elif case == "symlink":
                        real_path = str(Path(fake_home) / "worktree" / "real")
                        git("worktree", "move", worktree_path, real_path, cwd=repo_path)
                        Path(worktree_path).symlink_to(real_path, target_is_directory=True)
                        manifest = self.delegate.run_registry.load_run_manifest(
                            self._registry_root(repo_path), owner_id
                        )
                        manifest["executionCwd"] = worktree_path
                        self.delegate.run_registry.write_json_atomic(
                            self.delegate.run_registry.run_directory(
                                self._registry_root(repo_path), owner_id
                            )
                            / "manifest.json",
                            manifest,
                        )
                        expected = "worktree_path_changed"
                    elif case == "unregistered":
                        moved = str(Path(fake_home) / "worktree" / "moved")
                        git("worktree", "move", worktree_path, moved, cwd=repo_path)
                        Path(worktree_path).mkdir(parents=True)
                        expected = "worktree_unregistered"
                    else:
                        git("checkout", "-b", "delegate/other-branch", cwd=worktree_path)
                        expected = "worktree_branch_mismatch"
                    code, payload, _stderr = self._resume(
                        repo_path, fake_home, owner_alias, "continue"
                    )
                    self.assertEqual(code, self.delegate.EXIT_USAGE)
                    self.assertEqual(payload["error"], expected)

    def test_attachment_race_after_registration_terminalizes_without_spawning_child(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            owner_id, owner_alias, worktree_path, _branch = self._owner(repo_path, fake_home)
            original_register = self.delegate.run_registry.register_run

            def register_then_remove(*args, **kwargs):
                result = original_register(*args, **kwargs)
                git("worktree", "remove", "--force", worktree_path, cwd=repo_path)
                return result

            with (
                mock.patch.object(
                    self.delegate.run_registry, "register_run", side_effect=register_then_remove
                ),
                mock.patch.object(
                    self.delegate.delegate_runner, "execute_tracked"
                ) as execute_tracked,
            ):
                code, payload, _stderr = self._resume(repo_path, fake_home, owner_alias, "continue")
            self.assertEqual(code, self.delegate.EXIT_USAGE)
            self.assertEqual(payload["error"], "worktree_missing")
            execute_tracked.assert_not_called()
            child_id, manifest, state = self._child_manifest(repo_path, owner_id)
            self.assertNotEqual(child_id, owner_id)
            self.assertEqual(manifest["worktreeAttachment"]["sourceRunId"], owner_id)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failureReason"], "worktree_missing")


if __name__ == "__main__":
    unittest.main()
