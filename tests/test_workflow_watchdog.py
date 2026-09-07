from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from delegate_agent import run_registry, run_status
from delegate_agent.workflows import registry, runtime
from tests import proc_harness

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "delegate.py"


class WorkflowWatchdogProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.addClassCleanup(proc_harness.assert_no_live_process_groups)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.launched_workflows: list[str] = []
        self.captured_workflow_identities: dict[str, tuple[int, int]] = {}
        self.captured_workflow_pgids: dict[str, set[int]] = {}
        self.addCleanup(self._cleanup_workflows)
        self.workspace = Path(self.temp.name)
        self.home = self.workspace / "home"
        self.home.mkdir()
        (self.home / ".delegate").mkdir()
        self.bin_dir = self.workspace / "bin"
        self.bin_dir.mkdir()
        self.codex = self.bin_dir / "codex"
        self.codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "time.sleep(float(os.environ.get('FAKE_CODEX_SLEEP', '0')))\n"
            "print(json.dumps({'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}))\n"
            "print(json.dumps({'type':'completion','finalText':'done'}))\n",
            encoding="utf-8",
        )
        self.codex.chmod(0o755)
        self.config = self.workspace / ".delegate" / "config.json"
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            json.dumps({"codex": {"binary": str(self.codex)}}),
            encoding="utf-8",
        )

    def _env(self, sleep: float) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("DELEGATE_WORKFLOW_LOCK_FD", None)
        env.update(
            {
                "HOME": str(self.home),
                "DELEGATE_CONFIG": str(self.config),
                "DELEGATE_WORKFLOW_NO_DAEMON": "1",
                "FAKE_CODEX_SLEEP": str(sleep),
            }
        )
        return env

    def _cleanup_workflows(self) -> None:
        for wf_id in reversed(self.launched_workflows):
            with proc_harness.workflow_reap(self.workspace, wf_id):
                pass
            identity = self.captured_workflow_identities.get(wf_id)
            if identity is not None:
                proc_harness.reap_process_tree(*identity)
            for pgid in self.captured_workflow_pgids.get(wf_id, ()):
                proc_harness.reap_recorded_group_matching(pgid, str(self.workspace))

    def _launch(
        self,
        sleep: float,
        source: str | None = None,
        *,
        notify_target: str | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[str, Path]:
        script = self.workspace / f"workflow-{time.time_ns()}.py"
        script.write_text(
            source or "meta = {'name': 'watchdog'}\nreturn agent('long child')\n",
            encoding="utf-8",
        )
        argv = [
            sys.executable,
            str(CLI),
            "--cwd",
            str(self.workspace),
            "--json",
        ]
        if notify_target is not None:
            argv.extend(["--notify", notify_target])
        argv.extend(["workflow", "run", str(script)])
        env = self._env(sleep)
        if env_updates:
            env.update(env_updates)
        launched = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=10,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        wf_id = json.loads(launched.stdout)["wfId"]
        self.launched_workflows.append(wf_id)
        root = registry.workflow_dir(self.workspace, wf_id)
        supervisor_pid_value = self._wait_for(
            lambda: (registry.read_json(root / registry.STATUS_FILE) or {}).get("supervisorPid")
        )
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        self.assertIsInstance(supervisor_pid_value, int)
        supervisor_pid = status.get("supervisorPid")
        supervisor_pgid = status.get("supervisorPgid")
        self.assertIsInstance(supervisor_pid, int)
        self.assertIsInstance(supervisor_pgid, int)
        self.assertEqual(supervisor_pid, supervisor_pid_value)
        try:
            captured_pgids = proc_harness.register_process_tree(supervisor_pid, supervisor_pgid)
        except (RuntimeError, ValueError) as exc:
            self.fail(f"failed to capture supervisor process group: {exc}")
        self.assertIn(supervisor_pgid, captured_pgids)
        self.captured_workflow_identities[wf_id] = (supervisor_pid, supervisor_pgid)
        self.captured_workflow_pgids[wf_id] = captured_pgids
        return wf_id, root

    def _wait_for(self, predicate, timeout: float = 6.0) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("watchdog process condition timed out")

    def _wait_process_gone(self, pid: int, timeout: float = 6.0) -> None:
        # ps rather than /proc: the supervisor is not our child (no waitpid),
        # and /proc does not exist on macOS. A zombie counts as gone.
        def gone() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            state = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip()
            return not state or state.startswith("Z")

        self._wait_for(gone, timeout)

    def _workflow_child_states(self, wf_id: str) -> dict[str, dict[str, object]]:
        registry_root = run_registry.registry_root(self.workspace)
        index = run_registry.load_index(registry_root)
        states: dict[str, dict[str, object]] = {}
        for run_id, entry in run_registry.index_run_entries(index):
            if entry.get("group") != wf_id:
                continue
            state = run_registry.load_run_state_or_none(registry_root, run_id)
            if isinstance(state, dict):
                states[run_id] = state
        return states

    @staticmethod
    def _group_gone(pgid: int) -> bool:
        result = subprocess.run(
            ["ps", "-axo", "pgid=,stat="],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == str(pgid) and not fields[1].startswith("Z"):
                return False
        return True

    def test_state_file_deletion_cancels_real_supervisor_and_releases_lock(self) -> None:
        _, root = self._launch(10)
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        pid = int(status["supervisorPid"])
        (root / registry.STATUS_FILE).unlink()
        self._wait_process_gone(pid)
        self.assertFalse(registry.supervisor_alive(root))
        self.assertFalse((root / registry.STATUS_FILE).exists())

    def test_registry_entry_deletion_cancels_real_supervisor(self) -> None:
        _, root = self._launch(10)
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        pid = int(status["supervisorPid"])
        shutil.rmtree(root)
        self._wait_process_gone(pid)

    def test_state_deletion_reaps_only_owned_parallel_children(self) -> None:
        wf_id, root = self._launch(
            30,
            "meta = {'name': 'watchdog parallel cleanup'}\n"
            "return parallel([\n"
            "    lambda: agent('owned one'),\n"
            "    lambda: agent('owned two'),\n"
            "    lambda: agent('owned three'),\n"
            "])\n",
        )
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        supervisor_pid = int(status["supervisorPid"])
        supervisor_pgid = int(status["supervisorPgid"])

        # Four-core CI runners deliberately admit only two concurrent agents.
        # On a 4-core runner this is 2, so the reap assertion covers two of the three children.
        expected_children = min(3, runtime._global_agent_cap())

        def running_children() -> dict[str, dict[str, object]] | None:
            states = self._workflow_child_states(wf_id)
            if len(states) != expected_children:
                return None
            if any(
                run_status.raw_status(state) != run_status.STATUS_RUNNING
                for state in states.values()
            ):
                return None
            if any(not isinstance(state.get("pgid"), int) for state in states.values()):
                return None
            return states

        running = self._wait_for(running_children, timeout=12)
        self.assertIsInstance(running, dict)
        owned_pgids = {run_id: int(state["pgid"]) for run_id, state in running.items()}
        self.assertNotIn(supervisor_pgid, owned_pgids.values())
        self.captured_workflow_pgids[wf_id].update(owned_pgids.values())

        registry_root = run_registry.registry_root(self.workspace)
        retained_source = self.workspace / "retained-source"
        retained_source.mkdir()
        subprocess.run(["git", "init", "-q", str(retained_source)], check=True)
        (retained_source / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(retained_source), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(retained_source),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        retained_worktree = self.workspace / "retained-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(retained_source),
                "worktree",
                "add",
                "-q",
                "-b",
                "retained",
                str(retained_worktree),
            ],
            check=True,
        )
        sentinel = retained_worktree / "SENTINEL"
        sentinel.write_text("preserve\n", encoding="utf-8")
        retained_run_id, retained_alias = run_registry.register_run(
            registry_root,
            harness="codex",
            metadata={
                "group": wf_id,
                "mode": "work",
                "executionCwd": str(retained_worktree),
            },
        )
        retained_run_dir = run_registry.run_directory(registry_root, retained_run_id)
        run_registry.write_json_atomic(
            retained_run_dir / run_registry.MANIFEST_FILE,
            {
                "schema": run_registry.MANIFEST_SCHEMA,
                "runId": retained_run_id,
                "alias": retained_alias,
                "harness": "codex",
                "group": wf_id,
                "mode": "work",
                "executionCwd": str(retained_worktree),
                "sourceGitRoot": str(retained_source),
                "isolationMode": "worktree",
                "isolationLifecycle": "persistent",
                "preservedWorkspace": True,
            },
        )
        run_registry.write_json_atomic(
            retained_run_dir / run_registry.STATE_FILE,
            {
                "schema": run_registry.STATE_SCHEMA,
                "runId": retained_run_id,
                "alias": retained_alias,
                "status": run_status.STATUS_SUCCEEDED,
            },
        )

        with proc_harness.spawn_process(
            [sys.executable, "-c", "import time; time.sleep(30)", str(self.workspace)]
        ) as canary:
            canary_pgid = os.getpgid(canary.pid)
            canary_run_id, canary_alias = run_registry.register_run(
                registry_root,
                harness="codex",
                metadata={"group": "unrelated-canary", "mode": "work"},
            )
            canary_run_dir = run_registry.run_directory(registry_root, canary_run_id)
            run_registry.write_json_atomic(
                canary_run_dir / run_registry.STATE_FILE,
                {
                    "schema": run_registry.STATE_SCHEMA,
                    "runId": canary_run_id,
                    "alias": canary_alias,
                    "status": run_status.STATUS_RUNNING,
                    "pid": canary.pid,
                    "pgid": canary_pgid,
                },
            )

            (root / registry.STATUS_FILE).unlink()
            self._wait_process_gone(supervisor_pid, timeout=12)

            for pgid in owned_pgids.values():
                self._wait_for(lambda pgid=pgid: self._group_gone(pgid), timeout=8)
            settled = self._workflow_child_states(wf_id)
            for run_id in owned_pgids:
                self.assertEqual(
                    run_status.raw_status(settled[run_id]), run_status.STATUS_CANCELLED
                )
            self.assertIsNone(canary.poll())
            canary_state = run_registry.load_run_state(registry_root, canary_run_id)
            self.assertEqual(run_status.raw_status(canary_state), run_status.STATUS_RUNNING)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")

    def test_healthy_long_child_is_not_killed(self) -> None:
        _, root = self._launch(2)
        self._wait_for(
            lambda: (
                (registry.read_json(root / registry.STATUS_FILE) or {}).get("status") == "succeeded"
            ),
            timeout=8,
        )
        events = registry.iter_journal(root / registry.JOURNAL_FILE)
        self.assertNotIn("workflow_watchdog", {event.get("type") for event in events})
        self.assertFalse(registry.supervisor_alive(root))

    def test_journal_silent_stretch_is_not_killed(self) -> None:
        _, root = self._launch(
            0,
            "import time\n"
            "meta = {'name': 'parked stretch'}\n"
            "time.sleep(3.5)\n"
            "return agent('after parked stretch')\n",
        )
        initial_journal = registry.iter_journal(root / registry.JOURNAL_FILE)
        self.assertEqual([event.get("type") for event in initial_journal], ["attempt_config"])
        time.sleep(2.2)

        status = registry.read_json(root / registry.STATUS_FILE) or {}
        self.assertEqual(status.get("status"), "running")
        self.assertTrue(registry.supervisor_alive(root))
        self.assertEqual(registry.iter_journal(root / registry.JOURNAL_FILE), initial_journal)
        self._wait_for(
            lambda: (
                (registry.read_json(root / registry.STATUS_FILE) or {}).get("status") == "succeeded"
            ),
            timeout=8,
        )

    def test_slow_gate_and_soft_park_notifications_do_not_break_pause(self) -> None:
        post = self.bin_dir / "post"
        post.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "if ' paused' in ' '.join(sys.argv):\n"
            "    time.sleep(float(os.environ.get('FAKE_POST_SLEEP', '2.5')))\n",
            encoding="utf-8",
        )
        post.chmod(0o755)
        env_updates = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_POST_SLEEP": "2.5",
        }
        saved_workflows = self.home / ".delegate" / "workflows"
        saved_workflows.mkdir()
        child = saved_workflows / "gated-child.py"
        child.write_text("meta = {'name': 'child'}\nreturn True\n", encoding="utf-8")
        cases = {
            "gate": (
                "meta = {'name': 'slow gate notify'}\n"
                f"return workflow({str(child)!r}, gate=True)\n"
            ),
            "soft-park": (
                "meta = {'name': 'slow soft park notify'}\n"
                "def parked_item():\n"
                "    park_item('waiting')\n"
                "return soft_park({'waiting': parked_item})\n"
            ),
        }

        for name, source in cases.items():
            with self.subTest(name=name):
                _, root = self._launch(
                    0,
                    source,
                    notify_target=f"channel:{name}",
                    env_updates=env_updates,
                )
                self._wait_for(
                    lambda root=root: (
                        (registry.read_json(root / registry.STATUS_FILE) or {}).get("status")
                        == "paused"
                    )
                )
                time.sleep(1.2)

                self.assertTrue(registry.supervisor_alive(root))
                status = registry.read_json(root / registry.STATUS_FILE) or {}
                self._wait_process_gone(int(status["supervisorPid"]), timeout=8)
                self.assertEqual(
                    (registry.read_json(root / registry.STATUS_FILE) or {}).get("status"),
                    "paused",
                )


class HeldWorkflowLockTests(unittest.TestCase):
    """The inherited lock fd is only trusted when it maps to the lock file.

    A process spawned with close_fds inherits DELEGATE_WORKFLOW_LOCK_FD
    without the fd itself, so the number may be closed or reused for an
    unrelated file; both must fall back to fresh acquisition.
    """

    def setUp(self) -> None:
        from delegate_agent.workflows import runtime

        self.runtime = runtime
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "wfroot"
        self.root.mkdir()

    def _with_lock_fd_env(self, value: str) -> None:
        os.environ[self.runtime.WORKFLOW_LOCK_FD_ENV] = value
        self.addCleanup(os.environ.pop, self.runtime.WORKFLOW_LOCK_FD_ENV, None)

    def test_closed_fd_number_falls_back_to_fresh_acquisition(self) -> None:
        probe = os.open(os.devnull, os.O_RDONLY)
        os.close(probe)
        self._with_lock_fd_env(str(probe))
        with self.runtime._held_workflow_lock(self.root):
            self.assertTrue((self.root / registry.LOCK_FILE).exists())

    def test_reused_fd_for_unrelated_file_falls_back(self) -> None:
        stranger = self.root / "stranger.txt"
        stranger.write_text("not the lock", encoding="utf-8")
        fd = os.open(stranger, os.O_RDWR)

        def _close() -> None:
            with contextlib.suppress(OSError):
                os.close(fd)

        self.addCleanup(_close)
        self._with_lock_fd_env(str(fd))
        with self.runtime._held_workflow_lock(self.root):
            self.assertTrue((self.root / registry.LOCK_FILE).exists())
        # The stranger fd was neither flocked nor closed by the context manager.
        os.fstat(fd)

    def test_inherited_fd_matching_lock_file_is_used(self) -> None:
        fd = registry.acquire_workflow_lock(self.root)
        self._with_lock_fd_env(str(fd))
        with self.runtime._held_workflow_lock(self.root):
            pass
        # The context manager closed the fd it adopted.
        with self.assertRaises(OSError):
            os.fstat(fd)


if __name__ == "__main__":
    unittest.main()
