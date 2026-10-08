"""Tracked lanes run inside the host's capped test slice when it is installed.

The live test needs Linux with ``agent-tests.slice`` installed as a user unit
file (the devbox cell installs it); elsewhere it skips and the gating tests
still run.
"""

import contextlib
import fcntl
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import lane_slice, runner


def _probe(stdout: str, returncode: int = 0):
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return run


def _which(name: str) -> str:
    return f"/usr/bin/{name}"


INSTALLED = "LoadState=loaded\nFragmentPath=/home/u/.config/systemd/user/agent-tests.slice\n"


class LaneSliceGatingTests(unittest.TestCase):
    def test_wraps_only_when_the_parent_slice_is_an_installed_unit_file(self):
        cases = [
            # (label, platform, env, probe stdout, probe rc, wraps)
            ("installed slice", "linux", {}, INSTALLED, 0, True),
            ("macOS", "darwin", {}, INSTALLED, 0, False),
            ("operator opt-out", "linux", {"DELEGATE_LANE_SLICE": "off"}, INSTALLED, 0, False),
            ("slice not installed", "linux", {}, "LoadState=not-found\nFragmentPath=\n", 0, False),
            # systemd creates a parent implicitly for a child slice: loaded, no cap.
            ("implicit slice", "linux", {}, "LoadState=loaded\nFragmentPath=\n", 0, False),
            ("no user bus", "linux", {}, "", 1, False),
        ]
        for label, platform, env, stdout, rc, wraps in cases:
            with self.subTest(label):
                scope = lane_slice.plan(
                    "run-1",
                    "/src/delegate-agent",
                    platform=platform,
                    environ=env,
                    which=_which,
                    run=_probe(stdout, rc),
                )
                self.assertEqual(scope is not None, wraps)

    def test_missing_systemd_binaries_launch_unwrapped(self):
        scope = lane_slice.plan(
            "run-1",
            "/src/x",
            platform="linux",
            environ={},
            which=lambda _n: None,
            run=_probe(INSTALLED),
        )
        self.assertIsNone(scope)

    def test_repo_slice_names_never_nest_under_another_repo(self):
        # A dash in a slice name is nesting: delegate-agent must not land
        # inside a repo named delegate and share its weight.
        for path, slug in [
            ("/home/u/Code/delegate-agent", "delegate_agent"),
            ("/home/u/Code/Loom", "loom"),
            ("/home/u/Code/linux-devbox/", "linux_devbox"),
            ("", "workspace"),
        ]:
            with self.subTest(path):
                self.assertEqual(lane_slice.repo_slug(path), slug)
                self.assertNotIn("-", lane_slice.repo_slug(path))


def _slice_installed() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    env = {k: v for k, v in os.environ.items() if k != "DELEGATE_LANE_SLICE"}
    return lane_slice.plan("probe", "/x", environ=env) is not None


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


@unittest.skipUnless(_slice_installed(), "needs Linux with agent-tests.slice installed")
class LaneSliceLiveTests(unittest.TestCase):
    def test_lane_runs_in_its_repo_slice_and_a_detached_grandchild_dies_at_lane_end(self):
        env = {k: v for k, v in os.environ.items() if k != "DELEGATE_LANE_SLICE"}
        scope = lane_slice.plan("live-test", "/src/delegate-agent", environ=env)
        self.assertIsNotNone(scope)
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild.pid"
            # The grandchild leaves the lane's process group with setsid, the
            # way a detached test runner or daemon escapes group cleanup.
            script = (
                f"setsid sh -c 'echo $$ > {marker}; exec sleep 300' </dev/null >/dev/null 2>&1 & "
                "cat /proc/self/cgroup; sleep 300"
            )
            process = runner._launch_tracked_process(
                ["sh", "-c", script], tmp, stdin_text=None, lane_scope=scope
            )
            try:
                cgroup = process.stdout.readline().decode()
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                grandchild = int(marker.read_text().strip())
                self.assertTrue(_alive(grandchild))
                self.assertIn(
                    f"/agent-tests.slice/agent-tests-delegate_agent.slice/{scope.unit}", cgroup
                )
                # The pgid the runner records is still the lane's own pid.
                self.assertEqual(os.getpgid(process.pid), process.pid)
                self.assertNotEqual(os.getpgid(grandchild), process.pid)
            finally:
                runner._terminate_call_process(process, grace_seconds=1.0)
                lane_slice.stop(scope)
                process.stdout.close()
                process.stderr.close()
            deadline = time.monotonic() + 5
            while _alive(grandchild) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(grandchild), "detached grandchild outlived the lane")


SLICE = "agent-tests-delegate_agent.slice"
CGROUP = f"/user.slice/user-1001.slice/user@1001.service/agent.slice/agent-tests.slice/{SLICE}"


def _scope() -> lane_slice.LaneScope:
    return lane_slice.LaneScope(
        unit="delegate-x-abc123.scope",
        slice=SLICE,
        systemd_run="/usr/bin/systemd-run",
        systemctl="/usr/bin/systemctl",
    )


class FakeSystemctl:
    """Stands in for systemctl: records argv, answers ``show -p ControlGroup``."""

    def __init__(self, cgroup: str = CGROUP, show_rc: int = 0, raises: Exception | None = None):
        self.calls: list[list[str]] = []
        self.cgroup = cgroup
        self.show_rc = show_rc
        self.raises = raises

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        if "show" in argv:
            return subprocess.CompletedProcess(
                argv, self.show_rc, stdout=self.cgroup + "\n", stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def slice_stops(self) -> list[list[str]]:
        return [c for c in self.calls if "stop" in c and SLICE in c]


class SliceLockTests(unittest.TestCase):
    """The slice release contract shared with linux-devbox testrun."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {"XDG_RUNTIME_DIR": str(self.root / "run")}
        self.cgroot = self.root / "cgroup"
        (self.cgroot / CGROUP.lstrip("/")).mkdir(parents=True)
        self.lock_file = self.root / "run" / "testrun" / f"{SLICE}.lock"

    def _other_fd(self) -> int:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.close(fd) if _fd_open(fd) else None)
        return fd

    def _release(self, scope, systemctl, **kw):
        lane_slice.release(scope, run=systemctl, cgroup_root=str(self.cgroot), environ=None, **kw)

    def test_lock_path_is_the_shared_contract_path_with_tmp_fallback(self):
        self.assertEqual(
            lane_slice.lock_path(SLICE, {"XDG_RUNTIME_DIR": "/run/user/1001"}),
            Path(f"/run/user/1001/testrun/{SLICE}.lock"),
        )
        # testrun's rule when XDG_RUNTIME_DIR is unset: /run/user/<uid> if it exists, else /tmp.
        with mock.patch.object(lane_slice.os, "getuid", return_value=4242):
            with mock.patch.object(Path, "is_dir", return_value=True):
                self.assertEqual(
                    lane_slice.lock_path(SLICE, {}), Path(f"/run/user/4242/testrun/{SLICE}.lock")
                )
            with mock.patch.object(Path, "is_dir", return_value=False):
                self.assertEqual(
                    lane_slice.lock_path(SLICE, {}), Path(f"/tmp/testrun/{SLICE}.lock")
                )

    def test_acquire_holds_a_shared_lock_that_blocks_exclusive_but_not_shared(self):
        scope = _scope()
        self.assertTrue(lane_slice.acquire(scope, environ=self.env))
        self.assertIsNotNone(scope.lock.fd)
        self.assertTrue(self.lock_file.exists())
        other = self._other_fd()
        fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)  # sharing is fine
        with self.assertRaises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_UN)
        lane_slice.release(scope, run=FakeSystemctl(), cgroup_root=str(self.cgroot))
        self.assertIsNone(scope.lock.fd)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # lock gone with the fd

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir(),
        "reads /proc/self/fd",
    )
    def test_the_lock_fd_does_not_leak_into_the_lane_command(self):
        scope = _scope()
        lane_slice.acquire(scope, environ=self.env)
        self.addCleanup(lane_slice.release, scope, run=FakeSystemctl())
        self.assertFalse(os.get_inheritable(scope.lock.fd))
        out = subprocess.run(
            ["sh", "-c", "ls -l /proc/self/fd"],
            capture_output=True,
            text=True,
            # Even a launcher that kept fds open would not hand this one over.
            close_fds=False,
            check=True,
        ).stdout
        self.assertNotIn(f"{SLICE}.lock", out)

    def test_acquire_and_release_are_noops_without_a_scope(self):
        self.assertFalse(lane_slice.acquire(None, environ=self.env))
        lane_slice.release(None, run=FakeSystemctl())
        self.assertFalse(self.lock_file.exists())

    def test_acquire_fails_open_when_the_lock_dir_is_unusable(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file where a directory must go")
        scope = _scope()
        logged: list[str] = []
        got = lane_slice.acquire(
            scope, environ={"XDG_RUNTIME_DIR": str(blocker)}, log=logged.append
        )
        self.assertFalse(got)  # the caller must then launch unwrapped
        self.assertEqual(len(logged), 1)
        self.assertIn(SLICE, logged[0])
        self.assertIsNone(scope.lock.fd)
        # Releasing a scope that never got the lock must not stop anything.
        fake = FakeSystemctl()
        self._release(scope, fake)
        self.assertEqual(fake.calls, [])

    def test_acquire_gives_up_within_its_bound_when_an_exclusive_holder_persists(self):
        other = self._other_fd()
        fcntl.flock(other, fcntl.LOCK_EX)
        scope = _scope()
        start = time.monotonic()
        logged: list[str] = []
        got = lane_slice.acquire(scope, environ=self.env, wait_sec=0.1, log=logged.append)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertFalse(got)
        self.assertEqual(len(logged), 1)
        self.assertIsNone(scope.lock.fd)

    def test_last_user_with_a_childless_slice_stops_it_with_job_mode_fail(self):
        scope = _scope()
        lane_slice.acquire(scope, environ=self.env)
        fake = FakeSystemctl()
        self._release(scope, fake)
        self.assertEqual(
            fake.slice_stops(), [["/usr/bin/systemctl", "--user", "stop", "--job-mode=fail", SLICE]]
        )
        self.assertIn(
            ["/usr/bin/systemctl", "--user", "show", "-p", "ControlGroup", "--value", SLICE],
            fake.calls,
        )

    def test_another_shared_holder_keeps_the_slice(self):
        scope = _scope()
        lane_slice.acquire(scope, environ=self.env)
        other = self._other_fd()
        fcntl.flock(other, fcntl.LOCK_SH)  # e.g. a testrun still in the slice
        fake = FakeSystemctl()
        self._release(scope, fake)
        self.assertEqual(fake.calls, [])

    def test_a_cgroup_child_keeps_the_slice(self):
        (self.cgroot / CGROUP.lstrip("/") / "some-other.scope").mkdir()
        scope = _scope()
        lane_slice.acquire(scope, environ=self.env)
        fake = FakeSystemctl()
        self._release(scope, fake)
        self.assertEqual(fake.slice_stops(), [])
        self.assertTrue(any("show" in c for c in fake.calls))  # the check did run

    def test_an_unreadable_cgroup_never_stops_the_slice_and_never_raises(self):
        for label, fake in [
            ("show fails", FakeSystemctl(show_rc=1)),
            ("empty cgroup", FakeSystemctl(cgroup="")),
            ("cgroup dir missing", FakeSystemctl(cgroup="/not/there")),
            ("systemctl missing", FakeSystemctl(raises=FileNotFoundError())),
            ("systemctl hangs", FakeSystemctl(raises=subprocess.TimeoutExpired("systemctl", 1))),
        ]:
            with self.subTest(label):
                scope = _scope()
                lane_slice.acquire(scope, environ=self.env)
                self._release(scope, fake)
                self.assertEqual(fake.slice_stops(), [])
                self.assertIsNone(scope.lock.fd)

    def test_stop_ends_the_scope_before_it_considers_the_slice(self):
        scope = _scope()
        lane_slice.acquire(scope, environ=self.env)
        fake = FakeSystemctl()
        with mock.patch.object(lane_slice, "CGROUP_ROOT", str(self.cgroot)):
            lane_slice.stop(scope, run=fake)
        stops = [c for c in fake.calls if "stop" in c]
        self.assertEqual(
            stops,
            [
                ["/usr/bin/systemctl", "--user", "stop", scope.unit],
                ["/usr/bin/systemctl", "--user", "stop", "--job-mode=fail", SLICE],
            ],
        )

    def test_stop_for_a_lane_that_never_launched_skips_the_scope_stop_but_releases(self):
        scope = _scope()
        lane_slice.acquire(scope, environ=self.env)
        fake = FakeSystemctl()
        with mock.patch.object(lane_slice, "CGROUP_ROOT", str(self.cgroot)):
            lane_slice.stop(scope, run=fake, launched=False)
        self.assertNotIn(["/usr/bin/systemctl", "--user", "stop", scope.unit], fake.calls)
        self.assertIsNone(scope.lock.fd)

    def test_plan_none_means_no_lock_traffic_at_all(self):
        # macOS / opt-out: plan() is None and every hook is inert.
        scope = lane_slice.plan(
            "r", "/x", platform="darwin", environ=self.env, which=_which, run=_probe(INSTALLED)
        )
        self.assertIsNone(scope)
        lane_slice.acquire(scope, environ=self.env)
        lane_slice.stop(scope, run=FakeSystemctl())
        self.assertFalse((self.root / "run").exists())


def _fd_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


class RunnerWiringTests(unittest.TestCase):
    """The tracked-attempt path takes the lock before launch and always releases it."""

    def _ctx(self, workspace: str):
        run_registry = runner.run_registry
        root = run_registry.ensure_registry(Path(workspace), workspace_kind="directory")
        run_id, alias = run_registry.register_run(root, harness="codex")
        return runner.RunContext(
            registry_root=root,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode="safe",
            model=None,
            source_cwd=workspace,
            execution_cwd=workspace,
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-07-16T12:00:00Z",
        )

    def _attempt(self, argv, workspace, *, lock_ok=True, registry_lock=None):
        events: list[str] = []
        launch_kwargs: list[dict] = []
        scope = _scope()
        real_launch = runner._launch_tracked_process

        def launch(*a, **kw):
            events.append("launch")
            launch_kwargs.append(kw)
            return real_launch(*a, **kw)

        with (
            mock.patch.object(lane_slice, "plan", return_value=scope),
            mock.patch.object(lane_slice, "wrap_argv", side_effect=lambda a, _s: a),
            mock.patch.object(
                lane_slice,
                "acquire",
                side_effect=lambda s, **k: events.append("acquire") or lock_ok,
            ),
            mock.patch.object(
                lane_slice,
                "stop",
                side_effect=lambda s, **k: events.append(
                    f"stop scope={'yes' if s is not None else 'none'} "
                    f"launched={k.get('launched', True)}"
                ),
            ),
            mock.patch.object(runner, "_launch_tracked_process", side_effect=launch),
            contextlib.ExitStack() as extra,
        ):
            if registry_lock is not None:
                extra.enter_context(
                    mock.patch.object(runner, "_launch_registry_lock", registry_lock)
                )
            try:
                runner.execute_tracked(
                    argv,
                    workspace,
                    self._ctx(workspace),
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            except Exception as exc:
                events.append(f"raised {type(exc).__name__}: {exc}")
        self.launch_kwargs = launch_kwargs
        return events

    def test_lock_is_taken_before_launch_and_released_after_the_lane(self):
        with tempfile.TemporaryDirectory() as workspace:
            events = self._attempt([sys.executable, "-c", "pass"], workspace)
        self.assertEqual(events[0], "acquire")
        self.assertEqual(events[1], "launch")
        self.assertEqual(events[-1], "stop scope=yes launched=True")

    def test_a_failed_launch_still_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as workspace:
            events = self._attempt([str(Path(workspace) / "no-such-binary")], workspace)
        self.assertEqual(events[0], "acquire")
        self.assertIn("stop scope=yes launched=False", events)

    def test_a_lock_that_cannot_be_taken_launches_the_lane_unwrapped(self):
        with tempfile.TemporaryDirectory() as workspace:
            events = self._attempt([sys.executable, "-c", "pass"], workspace, lock_ok=False)
        self.assertEqual(events[:2], ["acquire", "launch"])
        self.assertNotIn("lane_scope", self.launch_kwargs[0])
        self.assertEqual(events[-1], "stop scope=none launched=True")

    def test_a_failure_before_the_launch_still_releases_the_lock(self):
        # Registry admission timing out used to sit outside the cleanup boundary.
        @contextlib.contextmanager
        def refused(*_a, **_k):
            raise TimeoutError("registry busy")
            yield  # pragma: no cover

        with tempfile.TemporaryDirectory() as workspace:
            events = self._attempt([sys.executable, "-c", "pass"], workspace, registry_lock=refused)
        self.assertEqual(events[0], "acquire")
        self.assertNotIn("launch", events)
        self.assertIn("stop scope=yes launched=False", events)
        self.assertTrue(any(e.startswith("raised TimeoutError") for e in events))


if __name__ == "__main__":
    unittest.main()
