"""Shared setup for tests that run a real child process through ``execute_tracked``."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

from delegate_agent import run_registry, runner


def make_context(
    workspace: Path, harness: str, *, tracked_stream_max_bytes: int | None = None
) -> runner.RunContext:
    """A tracked-run context; ``tracked_stream_max_bytes=None`` resolves the real default."""
    root = run_registry.ensure_registry(workspace, workspace_kind="directory")
    run_id, alias = run_registry.register_run(root, harness=harness)
    return runner.RunContext(
        registry_root=root,
        run_id=run_id,
        alias=alias,
        harness=harness,
        engine=harness,
        mode="work",
        model=None,
        source_cwd=str(workspace),
        execution_cwd=str(workspace),
        workspace_kind="directory",
        isolated_workspace=False,
        started_at=run_registry.utc_now_iso(),
        tracked_stream_max_bytes=tracked_stream_max_bytes,
    )


def run_tracked(
    workspace: Path,
    script: str,
    *,
    harness: str,
    timeout: float | None = 30,
    tracked_stream_max_bytes: int | None = None,
) -> tuple[int, dict, Path, runner.RunContext]:
    """Run ``script`` as the child; returns (exit code, payload, run dir, context).

    Raises ``RunnerLaunchError`` when the runner does (for example an output cap).
    """
    ctx = make_context(workspace, harness, tracked_stream_max_bytes=tracked_stream_max_bytes)
    # The script goes in a file, not ``python -c``: Linux caps one argv string
    # at 128 KiB (E2BIG), and the enormous-draft tests exceed it.
    with tempfile.TemporaryDirectory() as script_dir:
        script_path = Path(script_dir) / "child.py"
        script_path.write_text(script, encoding="utf-8")
        code, payload = runner.execute_tracked(
            [sys.executable, str(script_path)],
            str(workspace),
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            timeout=timeout,
        )
    return code, payload, run_registry.run_directory(ctx.registry_root, ctx.run_id), ctx


def load_state(ctx: runner.RunContext) -> dict:
    return run_registry.load_run_state(ctx.registry_root, ctx.run_id)


def read_report(ctx: runner.RunContext) -> str:
    path = run_registry.run_directory(ctx.registry_root, ctx.run_id) / runner.COMPLETION_REPORT_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


def jsonl(*records: dict) -> str:
    """A child-script fragment that writes each record as one stdout line."""
    return "".join(f"os.write(1, {(json.dumps(r) + chr(10)).encode()!r})\n" for r in records)
