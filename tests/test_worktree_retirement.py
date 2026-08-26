from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.execution_test_base import ExecutionTestBase


class WorktreeRetirementTests(ExecutionTestBase):
    def _clean_agent(self) -> Path:
        temp = tempfile.TemporaryDirectory(prefix="delegate-clean-agent-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        agent = root / "agent"
        agent.write_text("#!/usr/bin/env bash\nprintf 'done\\n'\n", encoding="utf-8")
        agent.chmod(0o755)
        return agent

    def _run_cursor(self, repo: str, config: dict, *, agent: Path, env: dict[str, str]):
        workspace = self.delegate.resolve_workspace(repo)
        request = self._make_persistent_worktree_request("cursor", "work", repo, config)
        request = self.delegate.Request(
            request.engine,
            request.mode,
            request.workspace,
            request.prompt,
            [str(agent), *request.argv[1:]],
            request.model,
            dry_run=False,
            workspace_kind=request.workspace_kind,
            isolation_context=request.isolation_context,
        )
        with mock.patch.dict(os.environ, env, clear=False):
            return self.delegate.execute_request(
                request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

    def _worktree_paths(self, home: str) -> list[Path]:
        return [
            path for path in (Path(home) / ".delegate" / "worktrees").glob("*/*") if path.is_dir()
        ]

    def test_clean_completion_retires_worktree_and_preserves_branch(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            agent = self._clean_agent()
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertIsNotNone(payload)
            self.assertTrue(payload["worktreeRetired"])
            self.assertFalse(self._worktree_paths(fake_home))
            pool = Path(fake_home) / ".delegate" / "worktrees"
            self.assertFalse(list(pool.iterdir()))
            run_path = Path(repo.name) / ".delegate" / "runs" / payload["runId"]
            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(state["worktreeStatus"], "removed")
            self.assertEqual(snapshot["worktreeStatus"], "removed")
            branches = subprocess.run(
                [
                    "git",
                    "-C",
                    repo.name,
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/heads/delegate/",
                ],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.splitlines()
            self.assertEqual(len(branches), 1)

    def test_true_child_dirt_is_retained_and_reported(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            agent = self._clean_agent()
            agent.write_text(
                "#!/usr/bin/env bash\nprintf 'child dirt\\n' > child-created.txt\nprintf 'done\\n'\n",
                encoding="utf-8",
            )
            agent.chmod(0o755)
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["worktreeRetained"], "dirty")
            self.assertTrue(self._worktree_paths(fake_home))
            run_path = Path(repo.name) / ".delegate" / "runs" / payload["runId"]
            state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["worktreeRetained"], "dirty")

    def test_seeded_dirt_unchanged_since_sync_is_clean(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            seeded = Path(repo.name) / "seeded.txt"
            seeded.write_text("source dirt\n", encoding="utf-8")
            agent = self._clean_agent()
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertTrue(payload["worktreeRetired"])
            self.assertTrue(payload["workSummary"]["seededOnlyChanges"])
            self.assertFalse(self._worktree_paths(fake_home))
            self.assertEqual(seeded.read_text(encoding="utf-8"), "source dirt\n")

    def test_seeded_file_edited_by_child_is_retained(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            seeded = Path(repo.name) / "seeded.txt"
            seeded.write_text("source dirt\n", encoding="utf-8")
            agent = self._clean_agent()
            agent.write_text(
                "#!/usr/bin/env bash\nprintf 'child edit\\n' > seeded.txt\nprintf 'done\\n'\n",
                encoding="utf-8",
            )
            agent.chmod(0o755)
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["worktreeRetained"], "dirty")
            self.assertTrue(self._worktree_paths(fake_home))

    def test_more_than_50_seeded_files_unchanged_are_still_retired(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            for idx in range(51):
                (Path(repo.name) / f"seeded-{idx:02d}.txt").write_text(
                    f"seeded {idx}\n", encoding="utf-8"
                )
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=self._clean_agent(),
                env={
                    "HOME": fake_home,
                    "PATH": str(Path(repo.name).parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertTrue(payload["worktreeRetired"])
            self.assertEqual(payload["workSummary"]["changedFilesCount"], 0)
            self.assertTrue(payload["workSummary"]["seededOnlyChanges"])
            self.assertFalse(self._worktree_paths(fake_home))

    def test_50_seeded_files_plus_child_file_are_retained_and_name_child(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            for idx in range(50):
                (Path(repo.name) / f"seeded-{idx:02d}.txt").write_text(
                    f"seeded {idx}\n", encoding="utf-8"
                )
            agent = self._clean_agent()
            agent.write_text(
                "#!/usr/bin/env bash\nprintf 'child dirt\\n' > child-created.txt\nprintf 'done\\n'\n",
                encoding="utf-8",
            )
            agent.chmod(0o755)
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["worktreeRetained"], "dirty")
            self.assertIn("child-created.txt", payload["worktreeRetentionPaths"])
            self.assertTrue(self._worktree_paths(fake_home))

    def test_failed_run_retains_worktree_as_not_succeeded(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            agent = self._clean_agent()
            agent.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
            agent.chmod(0o755)
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertNotEqual(code, 0)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["worktreeRetained"], "run_not_succeeded")
            self.assertTrue(self._worktree_paths(fake_home))

    def test_cancelled_run_retains_worktree_as_not_succeeded(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            agent = self._clean_agent()
            agent.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' '{\"type\":\"turn.cancelled\"}'\n",
                encoding="utf-8",
            )
            agent.chmod(0o755)
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "cancelled")
            self.assertEqual(payload["worktreeRetained"], "run_not_succeeded")
            self.assertTrue(self._worktree_paths(fake_home))

    def test_process_group_survivor_skips_retirement_with_distinct_reason(self):
        with tempfile.TemporaryDirectory() as registry:
            ctx = SimpleNamespace(
                mode="work",
                isolation_lifecycle="persistent",
                retire_worktree_on_completion=True,
                registry_root=Path(registry),
                run_id="del_survivor",
            )
            extra = {"processGroupSurvived": True}
            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "_completion_record",
                    return_value={"runId": "del_survivor"},
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt.run_registry,
                    "load_run_state_or_none",
                    return_value={"status": "succeeded"},
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "_persist_completion_worktree_fields",
                ) as persist,
                mock.patch.object(self.delegate.worktree_mgmt, "detect_worktree_status") as detect,
            ):
                self.delegate.worktree_mgmt.retire_worktree_on_completion(ctx, extra)

            self.assertEqual(extra["worktreeRetained"], "process_group_survived")
            persist.assert_called_once()
            detect.assert_not_called()

    def test_structured_retry_pending_skips_retirement(self):
        with tempfile.TemporaryDirectory() as registry:
            ctx = SimpleNamespace(
                mode="work",
                isolation_lifecycle="persistent",
                structured_retry=True,
                retire_worktree_on_completion=True,
                registry_root=Path(registry),
                run_id="del_structured",
            )
            extra = {}
            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "_completion_record",
                    return_value={"runId": "del_structured"},
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt.run_registry,
                    "load_run_state_or_none",
                    return_value={"status": "succeeded"},
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt, "_persist_completion_worktree_fields"
                ),
                mock.patch.object(self.delegate.worktree_mgmt, "detect_worktree_status") as detect,
            ):
                self.delegate.worktree_mgmt.retire_worktree_on_completion(ctx, extra)

            self.assertEqual(extra["worktreeRetained"], "structured_retry_pending")
            detect.assert_not_called()

    def test_resumable_clean_completion_is_retained_for_followup(self):
        with tempfile.TemporaryDirectory() as registry:
            ctx = SimpleNamespace(
                mode="work",
                isolation_lifecycle="persistent",
                resumable=True,
                retire_worktree_on_completion=True,
                registry_root=Path(registry),
                run_id="del_resumable",
            )
            extra = {}
            with (
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "_completion_record",
                    return_value={"runId": "del_resumable"},
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt.run_registry,
                    "load_run_state_or_none",
                    return_value={"status": "succeeded"},
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt, "_persist_completion_worktree_fields"
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "detect_worktree_status",
                    return_value=("present", []),
                ),
                mock.patch.object(
                    self.delegate.worktree_mgmt,
                    "_effective_dirty_for_retirement",
                    return_value=(False, [], []),
                ),
            ):
                self.delegate.worktree_mgmt.retire_worktree_on_completion(ctx, extra)

            self.assertEqual(extra["worktreeRetained"], "resumable_session")
            self.assertNotIn("worktreeRetired", extra)

    def test_seeded_tracked_deletion_unchanged_since_sync_is_clean(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            tracked = Path(repo.name) / "tracked.txt"
            tracked.write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo.name, "add", "tracked.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo.name, "commit", "-m", "tracked"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            tracked.unlink()
            agent = self._clean_agent()
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertTrue(payload["worktreeRetired"])
            self.assertFalse(self._worktree_paths(fake_home))

    def test_config_off_preserves_clean_worktree(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _ = self._make_git_repo_with_commit()
            agent = self._clean_agent()
            config = json.loads(json.dumps(self.delegate.DEFAULT_CONFIG))
            config["worktrees"]["retireWorktreeOnCompletion"] = False
            code, payload = self._run_cursor(
                repo.name,
                config,
                agent=agent,
                env={
                    "HOME": fake_home,
                    "PATH": str(agent.parent) + os.pathsep + os.environ["PATH"],
                },
            )

            self.assertEqual(code, 0)
            self.assertNotIn("worktreeRetired", payload)
            self.assertNotIn("worktreeRetained", payload)
            self.assertTrue(self._worktree_paths(fake_home))

    def test_auto_prune_runs_from_completion_hook(self):
        with tempfile.TemporaryDirectory() as registry:
            ctx = SimpleNamespace(
                mode="work",
                isolation_lifecycle="persistent",
                retire_worktree_on_completion=False,
                worktree_auto_prune_on_completion=True,
                worktree_auto_prune_merged_older_than_days=3,
                registry_root=Path(registry),
            )
            extra = {}
            result = {"ok": True, "removed": []}
            with mock.patch.object(
                self.delegate.worktree_mgmt, "maybe_auto_prune", return_value=result
            ) as prune:
                self.delegate.worktree_mgmt.retire_worktree_on_completion(ctx, extra)

            prune.assert_called_once()
            self.assertEqual(extra["autoPrune"], result)


if __name__ == "__main__":
    unittest.main()
