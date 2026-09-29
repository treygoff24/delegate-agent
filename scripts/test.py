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
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAST_FILES = (
    "tests/test_structured_output.py",
    "tests/test_provider_errors.py",
    "tests/test_workflow_schema.py",
    "tests/test_utility_modules.py",
    "tests/test_model_selection_wave1a.py",
    "tests/test_command_help.py",
    "tests/test_degraded.py",
    "tests/test_reasoning_capabilities.py",
    "tests/test_mail_gating.py",
    "tests/test_provider_error_records.py",
    "tests/test_delegate_help_cli.py",
    "tests/test_omp_provider_pin.py",
    "tests/test_c5_validation.py",
    "tests/test_stall_watchdog.py",
    "tests/test_model_selection_wave1b.py",
    "tests/test_delegate_parser.py",
    "tests/test_delegate_validation.py",
    "tests/test_test_runner.py",
)


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


def common_git_dir() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ROOT
    directory = ROOT / result.stdout.strip() if result.returncode == 0 else ROOT
    return directory.resolve()


def lock_path() -> Path:
    return common_git_dir() / ".delegate-tests.lock"


def tool(name: str, *, version: str | None = None) -> str | None:
    candidates = (
        ROOT / ".venv/bin" / name,
        common_git_dir().parent / ".venv/bin" / name,
        Path(shutil.which(name) or "/nonexistent"),
    )
    for candidate in candidates:
        if not os.access(candidate, os.X_OK) or not candidate.is_file():
            continue
        if version is not None:
            result = subprocess.run(
                [str(candidate), "--version"], capture_output=True, text=True, check=False
            )
            if result.returncode != 0 or result.stdout.strip() != version:
                continue
        return str(candidate)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers", type=int, choices=range(1, 9), default=2 if sys.platform == "darwin" else 4
    )
    parser.add_argument(
        "--scheduler", choices=("loadfile", "worksteal", "load"), default="worksteal"
    )
    parser.add_argument("--cpu-limit", type=int, choices=range(1, 5), default=2)
    parser.add_argument(
        "--gate", action="store_true", help="also run compile, lint, and format checks"
    )
    parser.add_argument(
        "--fast", action="store_true", help="quick checks, plus any supplied test paths"
    )
    parser.add_argument(
        "--durations", type=int, default=25, help="report this many slow test phases"
    )
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
    if args.durations < 0:
        parser.error("--durations must be non-negative")
    if args.gate and not args.fast and pytest_args:
        parser.error("the full gate cannot filter tests; use --fast or a focused test run")
    # Ambient filters must not silently turn a full gate into a subset.
    os.environ.pop("PYTEST_ADDOPTS", None)
    contain(args.cpu_limit, gate=args.gate)
    pytest_command = [sys.executable, "-m", "pytest"]
    ruff = None
    if args.gate:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        pinned = next(
            x for x in project["project"]["optional-dependencies"]["dev"] if x.startswith("ruff==")
        )
        version = "ruff " + pinned.removeprefix("ruff==")
        ruff = tool("ruff", version=version)
        if ruff is None:
            print(f"gate: requires {version} from the project dev extra", file=sys.stderr)
            return 1
        pytest = tool("pytest")
        if pytest is None:
            print("gate: requires pytest from the project dev extra", file=sys.stderr)
            return 1
        pytest_command = [pytest]
        print(f"python: {sys.executable}\npytest: {pytest}\n{version}", flush=True)
        if run([sys.executable, "--version"]):
            return 1
    with lock_path().open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                "tests: another run owns this repository; refusing to stack tests", file=sys.stderr
            )
            return 75
        print(f"tests: {args.workers} workers, {args.scheduler}", flush=True)
        if args.fast:
            print(
                "tests: FAST checks; full regression suite is still required before merge",
                flush=True,
            )
        code = run(
            [
                *pytest_command,
                "-q",
                "-n",
                str(args.workers),
                "--dist",
                args.scheduler,
                f"--durations={args.durations}",
                *(FAST_FILES if args.fast else ()),
                *pytest_args,
            ]
        )
        if code or not args.gate:
            return code
        assert ruff is not None
        for command in (
            [sys.executable, "-m", "compileall", "-q", "src", "tests", "bin", "scripts", "bench"],
            [ruff, "check", "."],
            [ruff, "format", "--check", "."],
        ):
            code = run(command)
            if code:
                return code
        print("FAST GATE PASS" if args.fast else "GATE PASS", flush=True)
        return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
