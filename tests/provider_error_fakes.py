"""A scripted fake `codex` for the provider-error CLI tests.

The fake walks a plan (one step per invocation, the last step repeating) and logs
each invocation's argv. Steps emit the JSON event shapes the Codex `--json` stream
uses; they are fixtures, not provider integration probes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "bin" / "delegate.py"

FAKE_CODEX = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

state = pathlib.Path(os.environ["FAKE_CODEX_STATE"])
state.mkdir(parents=True, exist_ok=True)
plan = json.loads(pathlib.Path(os.environ["FAKE_CODEX_PLAN"]).read_text())
counter = state / "count"
attempt = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(attempt + 1))
with (state / "argv.log").open("a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
step = plan[min(attempt, len(plan) - 1)]


def emit(event):
    print(json.dumps(event), flush=True)


def fail(message, error_message=None):
    emit({"type": "error", "message": message})
    emit({"type": "turn.failed", "error": {"message": error_message or message}})
    raise SystemExit(1)


emit({"type": "thread.started", "thread_id": "thr_fake_session"})
if step == "ok":
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "fake codex done"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    raise SystemExit(0)
if step == "auth401":
    fail("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header")
if step == "ws_drop":
    fail("stream disconnected before completion: websocket closed by server before response.completed")
if step == "image_limit":
    fail("400 Bad Request: Too many images in request: 8 > 4")
if step == "novel":
    fail("something the table has never seen")
raise SystemExit(f"unknown fake step {step!r}")
"""


class FakeCodexCase(unittest.TestCase):
    """A hermetic workspace, HOME, and fake `codex` on PATH; runs the real CLI."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "codex-home").mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.state_dir = self.root / "fake-state"
        self.plan_path = self.root / "plan.json"
        codex = self.bin_dir / "codex"
        codex.write_text(FAKE_CODEX, encoding="utf-8")
        codex.chmod(0o755)
        self.config: dict = {"codex": {"binary": "codex"}}
        self.set_plan("ok")

    def set_plan(self, *steps: str) -> None:
        self.plan_path.write_text(json.dumps(list(steps)), encoding="utf-8")

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        env.update(
            HOME=str(self.home),
            CODEX_HOME=str(self.root / "codex-home"),
            PATH=str(self.bin_dir) + os.pathsep + env.get("PATH", ""),
            DELEGATE_CONFIG=str(config_path),
            FAKE_CODEX_STATE=str(self.state_dir),
            FAKE_CODEX_PLAN=str(self.plan_path),
        )
        return env

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), "--cwd", str(self.workspace), *args],
            text=True,
            capture_output=True,
            env=self.env(),
            check=False,
            timeout=120,
        )

    def bare_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """No --cwd (call mode and doctor refuse it): run from the workspace directory."""
        return subprocess.run(
            [sys.executable, str(CLI_PATH), *args],
            text=True,
            capture_output=True,
            env=self.env(),
            cwd=self.workspace,
            check=False,
            timeout=120,
        )

    def json_cli(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        completed = self.cli("--json", *args)
        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            self.fail(f"no JSON on stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
        return completed, payload

    def invocation_count(self) -> int:
        counter = self.state_dir / "count"
        return int(counter.read_text()) if counter.exists() else 0

    def invocations(self) -> list[list[str]]:
        log = self.state_dir / "argv.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line]

    def marker_files(self) -> list[Path]:
        directory = self.home / ".delegate" / "state" / "lane-health"
        return sorted(directory.glob("*.json")) if directory.is_dir() else []
