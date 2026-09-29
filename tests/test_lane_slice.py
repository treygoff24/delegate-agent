"""Tracked lanes run inside the host's capped test slice when it is installed.

The live test needs Linux with ``agent-tests.slice`` installed as a user unit
file (the devbox cell installs it); elsewhere it skips and the gating tests
still run.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
