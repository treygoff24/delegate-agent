from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_missing_audit_tool_reports_optional_checkout(tmp_path: Path) -> None:
    shim = Path(__file__).resolve().parents[1] / "bin/delegate-audit"
    result = subprocess.run(
        [str(shim)], env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True
    )
    assert result.returncode == 127
    assert result.stdout == ""
    assert "optional writing-plans checkout" in result.stderr
    assert str(tmp_path / "Code/writing-plans/bin/delegate-audit") in result.stderr


def test_present_audit_tool_receives_arguments_and_preserves_exit(tmp_path: Path) -> None:
    shim = Path(__file__).resolve().parents[1] / "bin/delegate-audit"
    tool = tmp_path / "Code/writing-plans/bin/delegate-audit"
    tool.parent.mkdir(parents=True)
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n')
    tool.chmod(0o700)
    result = subprocess.run(
        [str(shim), "argument with spaces", "--check"],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 23
    assert result.stdout == "argument with spaces\n--check\n"
    assert result.stderr == ""
