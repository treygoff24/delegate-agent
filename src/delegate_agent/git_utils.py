from __future__ import annotations

import subprocess

GIT_QUICK_TIMEOUT_SECONDS = 10
GIT_MUTATION_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_RETURN_CODE = 124


def timeout_completed_process(
    command: list[str],
    exc: subprocess.TimeoutExpired,
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        GIT_TIMEOUT_RETURN_CODE,
        exc.stdout or "",
        f"git command timed out after {timeout_seconds}s",
    )
