"""Workspace spec: worktree base ref, recorded env, setup step, and the lease.

Launch-level behavior runs the real CLI against a fake cursor harness; resume,
prune, and registry behavior uses seeded registry records like their neighbors.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry as registry_api
from delegate_agent import workspace_spec
from delegate_agent import worktree_gc as worktree_gc_api
from delegate_agent.workflows import runtime as workflow_runtime
from tests import test_resume_attachment as resume_tests
from tests import test_wave4_launch_features as wave4_tests
from tests.execution_test_base import ExecutionTestBase
from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git

RESULT_EVENT = 'printf \'{"type":"result","result":"Status: completed. ok"}\\n\'\n'


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])  # fixed argv, reaped at once
    process.wait()
    return process.pid


class WorkspaceSpecLaunchTests(ExecutionTestBase):
    write_executable = wave4_tests.Wave4LaunchFeatureTests.write_executable
    write_config = wave4_tests.Wave4LaunchFeatureTests.write_config
    config_with_cursor = wave4_tests.Wave4LaunchFeatureTests.config_with_cursor

    def _repo_with_side_branch(self) -> str:
        repo, _ = self._make_git_repo_with_commit()
        path = repo.name
        git("checkout", "-q", "-b", "spine", cwd=path)
        Path(path, "spine.txt").write_text("spine\n", encoding="utf-8")
        git("add", "spine.txt", cwd=path)
        git("commit", "-q", "-m", "spine", cwd=path)
        git("checkout", "-q", "-", cwd=path)
        return path

    def _launch(self, repo: str, fake_home: str, agent: Path, *args: str) -> tuple[int, dict, str]:
        config = self.write_config(
            self.config_with_cursor(agent, data_home=str(Path(fake_home) / "worktrees"))
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": fake_home, "DELEGATE_CONFIG": str(config)}):
            code = self.delegate.main(
                ["--cwd", repo, "--json", "--isolation", "worktree", "cursor", "work", *args],
                stdout=stdout,
                stderr=stderr,
            )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_worktree_is_cut_from_base_and_setup_runs_before_the_child_with_env(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            probe = Path(fake_home) / "probe.txt"
            agent = self.write_executable(
                "agent",
                f"[ -d .git ] || [ -f .git ] || exit 0; {{ test -f spine.txt && echo base=spine; echo setup=$(cat setup-marker); "
                f'echo "env=$SPEC_VAR"; }} > "{probe}"\n' + RESULT_EVENT,
            )
            code, payload, stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--base",
                "spine",
                "--env",
                "SPEC_VAR=from-launch",
                "--setup",
                'printf "%s" "$SPEC_VAR" > setup-marker',
                "do the task",
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                probe.read_text(encoding="utf-8").split(),
                ["base=spine", "setup=from-launch", "env=from-launch"],
            )
            run_path = registry_api.run_directory(Path(repo) / ".delegate", payload["runId"])
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            spine_oid = git("rev-parse", "spine", cwd=repo).stdout.strip()
            spec = manifest["workspaceSpec"]
            self.assertEqual(spec["base"], "spine")
            self.assertEqual(spec["baseOid"], spine_oid)
            self.assertEqual(spec["envKeys"], ["SPEC_VAR"])
            self.assertEqual(spec["setupResult"]["exitCode"], 0)
            self.assertEqual(manifest["creationContext"]["sourceHeadOid"], spine_oid)
            # Values live only in the private env record, never the manifest.
            self.assertNotIn("from-launch", json.dumps(manifest))
            self.assertEqual(workspace_spec.read_run_env(run_path), {"SPEC_VAR": "from-launch"})
            self.assertEqual((run_path / "workspace-env.json").stat().st_mode & 0o777, 0o600)

    def test_setup_failure_is_typed_and_launches_no_child(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            marker = Path(fake_home) / "child-ran"
            agent = self.write_executable("agent", f'touch "{marker}"\n' + RESULT_EVENT)
            code, payload, _stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--setup",
                "echo installing; exit 7",
                "do the task",
            )
            self.assertNotEqual(code, 0)
            self.assertFalse(marker.exists(), "no child may launch after a failed setup")
            self.assertEqual(payload["error"], "workspace_setup_failed")
            self.assertEqual(payload["failureKind"], "workspace_setup")
            self.assertEqual(payload["workspaceSetup"]["exitCode"], 7)
            state = registry_api.load_run_state(Path(repo) / ".delegate", payload["runId"])
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failureReason"], "workspace_setup_failed")
            self.assertEqual(state["failureKind"], "workspace_setup")
            log = Path(payload["workspaceSetup"]["logPath"])
            self.assertIn("installing", log.read_text(encoding="utf-8"))

    def test_spec_flags_are_refused_outside_worktree_work_runs(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            agent = self.write_executable("agent", RESULT_EVENT)
            config = self.write_config(self.config_with_cursor(agent))
            cases = (
                (["cursor", "safe", "--env", "A=1", "x"], "invalid_option_combination"),
                (["cursor", "work", "--base", "spine", "x"], "invalid_option_combination"),
                (["cursor", "work", "--env", "DELEGATE_RUN_ID=x", "x"], "invalid_workspace_env"),
                (["cursor", "work", "--env", "A=1", "--env", "A=2", "x"], "invalid_workspace_env"),
            )
            for argv, error in cases:
                with self.subTest(argv=argv):
                    stdout = io.StringIO()
                    with mock.patch.dict(
                        os.environ, {"HOME": fake_home, "DELEGATE_CONFIG": str(config)}
                    ):
                        code = self.delegate.main(
                            ["--cwd", repo, "--json", *argv], stdout=stdout, stderr=io.StringIO()
                        )
                    self.assertNotEqual(code, 0)
                    self.assertEqual(json.loads(stdout.getvalue())["error"], error)


class WorkspaceEnvResumeTests(WorktreeMgmtTestBase):
    _owner = resume_tests.ResumeAttachmentTests._owner
    _source_prompt = resume_tests.ResumeAttachmentTests._source_prompt
    _resume = resume_tests.ResumeAttachmentTests._resume

    def test_resume_reapplies_recorded_env_even_when_the_resuming_shell_differs(self):
        _repo, repo_path = self._make_repo()
        with (
            tempfile.TemporaryDirectory() as fake_home,
            tempfile.TemporaryDirectory() as bin_dir,
        ):
            agent = Path(bin_dir) / "agent"
            agent.write_text(
                '#!/bin/sh\nprintf "%s\\n" "${SPEC_VAR:-unset}" > "$SPEC_PROBE"\n' + RESULT_EVENT,
                encoding="utf-8",
            )
            agent.chmod(0o755)
            probe = Path(fake_home) / "probe.txt"
            owner_id, owner_alias, _worktree, _branch = self._owner(repo_path, fake_home)
            owner_path = registry_api.run_directory(self._registry_root(repo_path), owner_id)
            workspace_spec.write_run_env(owner_path, {"SPEC_VAR": "from-launch"})
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
                    "SPEC_VAR": "from-resuming-shell",
                    "SPEC_PROBE": str(probe),
                },
            ):
                code, payload, stderr = self._resume(repo_path, fake_home, owner_alias, "continue")
            self.assertEqual(code, 0, stderr)
            self.assertEqual(probe.read_text(encoding="utf-8").strip(), "from-launch")
            child_path = registry_api.run_directory(
                self._registry_root(repo_path), payload["runId"]
            )
            # The attached run records the env too, so a resume of it replays it.
            self.assertEqual(workspace_spec.read_run_env(child_path), {"SPEC_VAR": "from-launch"})


class WorktreeLeaseTests(WorktreeMgmtTestBase):
    def _leased_tree(self, repo_path: str, fake_home: str, alias: str, **state: object) -> str:
        branch = f"delegate/{alias}"
        wt_path = str(Path(fake_home) / "wt" / alias)
        old = (datetime.now(UTC) - timedelta(days=3)).strftime(registry_api.UTC_TIMESTAMP_FORMAT)
        run_id, _ = self._seed_persistent_run(
            repo_path, alias=alias, branch=branch, execution_cwd=wt_path, last_activity_at=old
        )
        self._create_worktree_at(repo_path, branch, wt_path)
        root = self._registry_root(repo_path)
        state_path = registry_api.run_directory(root, run_id) / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        dead = _dead_pid()
        payload.update({"status": "running", "pid": dead, "pgid": dead, **state})
        registry_api.write_json_atomic(state_path, payload)
        return wt_path

    def test_live_launcher_lease_survives_auto_prune_and_prune_merged_with_dead_pid(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            wt_path = self._leased_tree(
                repo_path, fake_home, "cursor-leased", launcherPid=os.getpid()
            )
            global_config = Path(fake_home) / ".delegate" / "config.json"
            global_config.parent.mkdir(parents=True, exist_ok=True)
            global_config.write_text(
                json.dumps(
                    {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
                ),
                encoding="utf-8",
            )
            code, out, err = self._run_cli(
                ["--cwd", repo_path, "--json", "worktree", "list"], home=fake_home
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["summary"]["autoPruneMode"], "attempted")
            self.assertTrue(Path(wt_path).exists(), "auto-prune reaped a leased worktree")

            code, out, err = self._run_cli(
                ["--cwd", repo_path, "--json", "worktree", "prune", "--merged"], home=fake_home
            )
            payload = json.loads(out)
            self.assertTrue(Path(wt_path).exists(), "prune --merged reaped a leased worktree")
            reasons = {entry["alias"]: entry["reason"] for entry in payload["skipped"]}
            self.assertEqual(reasons["cursor-leased"], "worktree_leased")

    def test_lease_ends_with_a_dead_launcher_or_a_terminal_record(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._leased_tree(repo_path, fake_home, "cursor-gone", launcherPid=_dead_pid())
            self._leased_tree(
                repo_path, fake_home, "cursor-done", status="succeeded", launcherPid=os.getpid()
            )
            result = worktree_gc_api.prune_worktrees(
                self._registry_root(repo_path), merged=True, dry_run=True
            )
            planned = {entry["alias"] for entry in result["planned"]}
            self.assertEqual(planned, {"cursor-gone", "cursor-done"})


class LinkedWorktreeRegistryTests(WorktreeMgmtTestBase):
    def test_run_launched_from_a_linked_worktree_is_listed_from_the_parent_repo(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            linked = str(Path(fake_home) / "lane")
            git("worktree", "add", "-q", "-b", "lane", linked, "HEAD", cwd=repo_path)
            parent_run, _ = self._seed_plain_run(repo_path)
            linked_run, _ = self._seed_plain_run(linked)

            code, out, err = self._run_cli(["--cwd", repo_path, "--json", "runs"], home=fake_home)
            self.assertEqual(code, 0, err)
            runs = {item["runId"]: item for item in json.loads(out)["runs"]}
            self.assertIn(parent_run, runs)
            self.assertIn(linked_run, runs)
            self.assertEqual(runs[linked_run]["registryWorkspace"], linked)
            self.assertNotIn("registryWorkspace", runs[parent_run])

            # The linked worktree's own listing stays its own.
            code, out, _err = self._run_cli(["--cwd", linked, "--json", "runs"], home=fake_home)
            self.assertEqual([item["runId"] for item in json.loads(out)["runs"]], [linked_run])


class WorkflowWorkspaceSpecTests(WorktreeMgmtTestBase):
    def test_capability_is_advertised(self):
        self.assertEqual(workflow_runtime.WORKFLOW_CAPABILITIES["workspaceSpec"], 1)

    def test_agent_spec_requires_worktree_work_lane_and_valid_values(self):
        spec = workflow_runtime._agent_workspace_spec(
            base="main", env={"A": "1"}, setup="make", mode="work", isolation="worktree"
        )
        self.assertEqual(spec, {"base": "main", "env": {"A": "1"}, "setup": "make"})
        for kwargs in (
            {"mode": "safe", "isolation": "worktree"},
            {"mode": "work", "isolation": None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                workflow_runtime._agent_workspace_spec(
                    base=None, env={"A": "1"}, setup=None, **kwargs
                )
        with self.assertRaises(ValueError):
            workflow_runtime._agent_workspace_spec(
                base=None, env={"TMPDIR": "/x"}, setup=None, mode="work", isolation="worktree"
            )

    def test_workflow_launch_env_is_recorded_and_resume_refuses_new_env(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            script = Path(fake_home) / "wf.py"
            script.write_text('meta = {"name": "noop"}\nreturn {"ok": True}\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"DELEGATE_WORKFLOW_NO_DAEMON": "1"}):
                code, out, err = self._run_cli(
                    [
                        "--cwd",
                        repo_path,
                        "--json",
                        "workflow",
                        "run",
                        str(script),
                        "--dry-run",
                        "--env",
                        "SPEC_VAR=from-launch",
                    ],
                    home=fake_home,
                )
            self.assertEqual(code, 0, err)
            wf_root = Path(repo_path) / ".delegate" / "workflows" / json.loads(out)["wfId"]
            self.assertEqual(workspace_spec.read_run_env(wf_root), {"SPEC_VAR": "from-launch"})

            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    repo_path,
                    "--json",
                    "workflow",
                    "resume",
                    json.loads(out)["wfId"],
                    "--env",
                    "SPEC_VAR=other",
                ],
                home=fake_home,
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out)["error"], "invalid_option_combination")

    def test_workflow_children_see_the_launch_env_not_the_resuming_shell(self):
        with tempfile.TemporaryDirectory() as wf_root:
            workspace_spec.write_run_env(Path(wf_root), {"SPEC_VAR": "from-launch"})
            environment = workflow_runtime.with_launch_environment(
                Path(wf_root), {"DELEGATE_WORKFLOW_ATTEMPT": "attempt"}
            )
            with mock.patch.dict(os.environ, {"SPEC_VAR": "from-resuming-shell"}):
                child = workflow_runtime._run_child_command(
                    ["/bin/sh", "-c", 'printf "%s" "$SPEC_VAR"'],
                    cwd=wf_root,
                    timeout=10,
                    environment=environment,
                )
            self.assertEqual(child.stdout.decode(), "from-launch")
