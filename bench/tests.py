#!/usr/bin/env python3
"""Fixed test-speed workload and collection guard for the hillclimb harness."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "tests/test_workflow_commands.py::WorkflowCommandTests::test_engine_caps_bound_concurrent_child_runs",
    "tests/test_workflow_commands.py::WorkflowCommandTests::test_opencode_engine_caps_bound_concurrent_child_runs",
    "tests/test_wait_cancel_commands.py::WaitCancelCommandTests::test_cancel_process_group_marks_cancelled",
    "tests/test_wait_cancel_commands.py::WaitCancelCommandTests::test_cancelled_run_drops_the_live_pending_tool",
)


def main() -> int:
    python = ROOT / ".venv/bin/python"
    if sys.argv[1:] == ["--collect"]:
        result = subprocess.run(
            [str(python), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        actual = {line for line in result.stdout.splitlines() if "::" in line}
        expected = set(Path(os.environ["DELEGATE_BENCH_COLLECTION"]).read_text().splitlines())
        if not expected or not expected <= actual:
            print("collection guard: missing tests", sorted(expected - actual), file=sys.stderr)
            return 1
        print(json.dumps({"collected": len(actual), "baseline_collected": len(expected)}))
        return 0
    full = sys.argv[1:] == ["--full"]
    if sys.argv[1:] not in ([], ["--full"]):
        raise SystemExit("expected --full, --collect, or no arguments")
    with tempfile.TemporaryDirectory(prefix="delegate-test-bench-") as directory:
        report = Path(directory) / "result.xml"
        start = time.perf_counter()
        result = subprocess.run(
            [
                str(python),
                "scripts/test.py",
                "--",
                "-p",
                "no:cacheprovider",
                "--durations=50",
                f"--junitxml={report}",
                *([] if full else CASES),
            ],
            cwd=ROOT,
            check=False,
        )
        wall = time.perf_counter() - start
        suites = list(ET.parse(report).getroot().iter("testsuite")) if report.is_file() else []
        cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(
            json.dumps(
                {
                    "elapsed_s": wall,
                    "cpu_s": cpu.ru_utime + cpu.ru_stime,
                    "tests": sum(int(s.get("tests", 0)) for s in suites),
                    "skipped": sum(int(s.get("skipped", 0)) for s in suites),
                    "failures": sum(int(s.get("failures", 0)) for s in suites),
                    "errors": sum(int(s.get("errors", 0)) for s in suites),
                }
            ),
            flush=True,
        )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
