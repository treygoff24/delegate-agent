#!/usr/bin/env python3
"""Bounded, serialized pytest runs. Gates and focused checks share this entry point."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str]) -> int:
    child = subprocess.Popen(argv, cwd=ROOT, start_new_session=True)
    try:
        return child.wait()
    except KeyboardInterrupt:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        return 130


def interrupted(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def test_scope() -> str | None:
    if sys.platform != "linux":
        return None
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        match = re.search(r"/agent-tests-[^/]+\.slice/(delegate-agent-[^/]+\.scope)(?:/|$)", line)
        if match:
            return match.group(1)
    return None


def contain(cpu_limit: int, *, gate: bool) -> None:
    scope = test_scope()
    wrapper = shutil.which("testrun")
    if not scope and wrapper and os.environ.get("DELEGATE_TEST_WRAPPED") != "1":
        os.environ["DELEGATE_TEST_WRAPPED"] = "1"
        os.execv(
            wrapper,
            [
                wrapper,
                "delegate-agent",
                "gate" if gate else "tests",
                "--max",
                "1800" if gate else "600",
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
        )
    if sys.platform == "linux" and wrapper:
        if scope is None:
            raise RuntimeError("testrun did not place this run in its own test scope")
        subprocess.run(
            [
                "systemctl",
                "--user",
                "set-property",
                "--runtime",
                scope,
                f"CPUQuota={cpu_limit * 100}%",
                "CPUWeight=10",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"tests: CPU budget {cpu_limit} cores, low priority", flush=True)
    else:
        print("tests: low priority; hard CPU quotas require Linux and testrun", flush=True)
    os.nice(10)


def lock_path() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    directory = ROOT / result.stdout.strip() if result.returncode == 0 else ROOT
    return directory.resolve() / ".delegate-tests.lock"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    parser.add_argument(
        "--scheduler", choices=("loadfile", "worksteal", "load"), default="loadfile"
    )
    parser.add_argument("--cpu-limit", type=int, choices=range(1, 5), default=2)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pytest_args = args.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if any(
        arg.startswith(("-n", "--numprocesses", "--dist", "--tx")) or arg == "-d"
        for arg in pytest_args
    ):
        parser.error("use --workers and --scheduler before -- to keep the worker count bounded")
    contain(args.cpu_limit, gate=False)
    with lock_path().open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                "tests: another run owns this repository; refusing to stack tests", file=sys.stderr
            )
            return 75
        print(f"tests: {args.workers} workers, {args.scheduler}", flush=True)
        return run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n",
                str(args.workers),
                "--dist",
                args.scheduler,
                *pytest_args,
            ]
        )


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
