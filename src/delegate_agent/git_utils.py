from __future__ import annotations

import subprocess
from pathlib import Path

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


def run_git(
    cwd: str,
    args: list[str],
    *,
    timeout_seconds: int = GIT_MUTATION_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", cwd, *args]
    try:
        return subprocess.run(  # nosec B603 - fixed git argv is executed with shell=False and a timeout.
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return timeout_completed_process(
            command,
            exc,
            timeout_seconds=timeout_seconds,
        )


def timeout_completed_process_bytes(
    command: list[str],
    exc: subprocess.TimeoutExpired,
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    stderr = exc.stderr or f"git command timed out after {timeout_seconds}s".encode()
    if isinstance(stderr, str):
        stderr = stderr.encode()
    stdout = exc.stdout or b""
    if isinstance(stdout, str):
        stdout = stdout.encode()
    return subprocess.CompletedProcess(
        command,
        GIT_TIMEOUT_RETURN_CODE,
        stdout,
        stderr,
    )


def run_git_bytes(
    cwd: str,
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: int = GIT_MUTATION_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    command = ["git", "-C", cwd, *args]
    try:
        return subprocess.run(  # nosec B603 - fixed git argv is executed with shell=False and a timeout.
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return timeout_completed_process_bytes(
            command,
            exc,
            timeout_seconds=timeout_seconds,
        )


def capture_git_metadata(
    workspace_path: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Capture read-only Git metadata used for isolation planning.

    Returns ``(git_root, git_common_dir, head_oid, head_ref, branch_name)``.
    All fields are ``None`` when the workspace is not a Git repo or Git cannot
    answer the probes.
    """
    try:
        root_result = run_git(
            workspace_path,
            ["rev-parse", "--show-toplevel"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
        if root_result.returncode != 0:
            return None, None, None, None, None
        git_root = root_result.stdout.strip()

        common_result = run_git(
            workspace_path,
            ["rev-parse", "--git-common-dir"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
        git_common_dir = common_result.stdout.strip() if common_result.returncode == 0 else None
        if git_common_dir and not git_common_dir.startswith("/"):
            git_common_dir = str(Path(git_root) / git_common_dir)

        oid_result = run_git(
            workspace_path,
            ["rev-parse", "HEAD"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
        head_oid = oid_result.stdout.strip() if oid_result.returncode == 0 else None

        ref_result = run_git(
            workspace_path,
            ["symbolic-ref", "--quiet", "HEAD"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
        head_ref = ref_result.stdout.strip() if ref_result.returncode == 0 else None

        branch_name = None
        if head_ref and head_ref.startswith("refs/heads/"):
            branch_name = head_ref[11:]

        return git_root, git_common_dir, head_oid, head_ref, branch_name
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None, None, None, None, None
