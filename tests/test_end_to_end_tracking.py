"""End-to-end CLI tests for run tracking using fake harness binaries."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
CLI_PATH = ROOT / "bin" / "delegate.py"
MODULE_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"
RETENTION_PATH = ROOT / "src" / "delegate_agent" / "retention.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)

RUN_ID_RE = re.compile(r"^del_\d{8}T\d{6}Z_[0-9a-f]{6}$")
ASSISTANT_MARKER = "E2E assistant summary"
COMPLETION_MARKER = "E2E completion report body"
STDERR_MARKER = "E2E_STDERR_LINE"
SECRET_TOKEN = "***************************************"
CODEX_ASSISTANT_MARKER = "Codex completed"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_alias_from_bounded_stdout(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("alias:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"alias not found in bounded output:\n{stdout}")


def read_fifo_line(path: Path, *, timeout: float) -> str:
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            raise AssertionError(f"timed out waiting for FIFO signal: {path}")
        return os.read(fd, 4096).decode("utf-8").strip()
    finally:
        os.close(fd)


def make_streaming_harness_script(*, include_completion: bool = True) -> str:
    completion_line = (
        f'printf \'{{"type":"completion","finalText":"{COMPLETION_MARKER}"}}\\n\'\n'
        if include_completion
        else ""
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'printf \'{{"type":"message","role":"assistant","content":"{ASSISTANT_MARKER}"}}\\n\'\n'
        f"{completion_line}"
        f'printf "{STDERR_MARKER}\\n" >&2\n'
        'exit "${FAKE_EXIT:-0}"\n'
    )


def make_sleeping_harness_script() -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ -n "${FAKE_STARTED_FIFO:-}" ]; then printf "started\\n" > "$FAKE_STARTED_FIFO"; fi\n'
        'if [ -n "${FAKE_RELEASE_FIFO:-}" ]; then\n'
        '  IFS= read -r _ < "$FAKE_RELEASE_FIFO"\n'
        "else\n"
        "  python3 -c 'import select; select.select([], [], [], 4)'\n"
        "fi\n"
        f'printf \'{{"type":"message","role":"assistant","content":"{ASSISTANT_MARKER}"}}\\n\'\n'
        f'printf \'{{"type":"completion","finalText":"{COMPLETION_MARKER}"}}\\n\'\n'
        'exit "${FAKE_EXIT:-0}"\n'
    )


def make_pass_through_script() -> str:
    return (
        "#!/usr/bin/env bash\n"
        "printf 'OUT:pass-through\\n'\n"
        "printf 'ERR:pass-through\\n' >&2\n"
        'exit "${FAKE_EXIT:-0}"\n'
    )


def make_codex_streaming_script(*, include_completion: bool = True) -> str:
    completion_line = (
        f'printf \'{{"type":"completion","finalText":"{COMPLETION_MARKER}"}}\\n\'\n'
        if include_completion
        else ""
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'printf \'{{"type":"message","role":"assistant","content":[{{"type":"text","text":"{CODEX_ASSISTANT_MARKER}"}}]}}\\n\'\n'
        f'printf \'{{"type":"tool_call","tool":"shell","args":{{"command":"python3 -m unittest"}}}}\\n\'\n'
        f"{completion_line}"
        f'printf "{STDERR_MARKER}\\n" >&2\n'
        'exit "${FAKE_EXIT:-0}"\n'
    )


def make_grok_streaming_script(*, include_completion: bool = True) -> str:
    fixture = ROOT / "tests" / "fixtures" / "grok_streaming_json_smoke.jsonl"
    stream_command = f"cat {fixture}\n" if include_completion else f"sed '$d' {fixture}\n"
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "%s\\n" "$@" > "${GROK_ARGV_LOG:-/dev/null}"\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "--prompt-file" ]; then\n'
        "    shift\n"
        '    grep -q "Delegate Grok safe mode" "$1" || exit 11\n'
        "    break\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        f"{stream_command}"
        f'printf "{STDERR_MARKER}\\n" >&2\n'
        'exit "${FAKE_EXIT:-0}"\n'
    )


class EndToEndTrackingTests(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(MODULE_PATH, "delegate_cli_e2e_test")
        self.registry = load_module(REGISTRY_PATH, "delegate_registry_e2e_test")
        self.retention = load_module(RETENTION_PATH, "delegate_retention_e2e_test")
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.workspace_temp.name)
        self.bin_temp = tempfile.TemporaryDirectory()
        self.bin_dir = Path(self.bin_temp.name)
        self.registry_root = self.registry.delegate_root(self.workspace)
        self.config_path = self.write_workspace_config()

    def tearDown(self):
        self.bin_temp.cleanup()
        self.workspace_temp.cleanup()

    def write_fake_binaries(
        self,
        *,
        passthrough: bool = False,
        include_completion: bool = True,
        sleeping: bool = False,
    ) -> None:
        if sleeping:
            script_body = make_sleeping_harness_script()
            codex_script = make_sleeping_harness_script()
        elif passthrough:
            script_body = make_pass_through_script()
            codex_script = make_pass_through_script()
        else:
            script_body = make_streaming_harness_script(include_completion=include_completion)
            codex_script = make_codex_streaming_script(include_completion=include_completion)
        for name in ("droid", "agent"):
            path = self.bin_dir / name
            path.write_text(script_body, encoding="utf-8")
            path.chmod(0o755)
        codex_path = self.bin_dir / "codex"
        codex_path.write_text(codex_script, encoding="utf-8")
        codex_path.chmod(0o755)
        grok_path = self.bin_dir / "grok"
        grok_path.write_text(
            make_grok_streaming_script(include_completion=include_completion), encoding="utf-8"
        )
        grok_path.chmod(0o755)

    def write_workspace_config(self, *, raw_log_days: int = 7) -> Path:
        config_path = self.registry.delegate_root(self.workspace) / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "tracking": {
                        "completionReport": {"defaultMode": "markdown"},
                        "retention": {"enabled": True, "rawLogDays": raw_log_days},
                    },
                    "cursor": {
                        "argvPrefix": ["agent"],
                        "defaultModel": "composer-2.5",
                    },
                    "droid": {
                        "binary": "droid",
                        "models": {"minimax": "e2e-model-id"},
                    },
                    "grok": {
                        "binary": "grok",
                    },
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def delegate_env(
        self,
        config_path: Path | None = None,
        *,
        fake_exit: str | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        home = self.workspace / "home"
        codex_home = self.workspace / "codex-home"
        home.mkdir(exist_ok=True)
        codex_home.mkdir(exist_ok=True)
        env["HOME"] = str(home)
        env["CODEX_HOME"] = str(codex_home)
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config_path or self.config_path)
        if fake_exit is not None:
            env["FAKE_EXIT"] = fake_exit
        return env

    def run_cli(
        self,
        args: list[str],
        *,
        config_path: Path | None = None,
        fake_exit: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), "--cwd", str(self.workspace), *args],
            text=True,
            capture_output=True,
            env=self.delegate_env(config_path, fake_exit=fake_exit),
            check=False,
        )

    def run_tracked_droid(
        self,
        prompt: str,
        *,
        global_args: list[str] | None = None,
        fake_exit: str | None = None,
        include_completion: bool = True,
        config_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.write_fake_binaries(include_completion=include_completion)
        args = [*(global_args or []), "droid", "minimax", "safe", prompt]
        return self.run_cli(args, config_path=config_path, fake_exit=fake_exit)

    def lookup_run(self, alias: str) -> tuple[str, Path]:
        run_id = self.registry.lookup_run_id(self.registry.load_index(self.registry_root), alias)
        self.assertIsNotNone(run_id)
        return run_id, self.registry.run_directory(self.registry_root, run_id)

    def assert_registry_files(self, run_id: str, *, expect_completion_report: bool = True) -> Path:
        self.assertTrue(RUN_ID_RE.match(run_id))
        run_path = self.registry.run_directory(self.registry_root, run_id)
        for name in (
            "manifest.json",
            "state.json",
            "snapshot.json",
            "stdout.log",
            "stderr.log",
            "events.jsonl",
        ):
            with self.subTest(file=name):
                self.assertTrue((run_path / name).exists(), f"missing {name}")
        if expect_completion_report:
            self.assertTrue((run_path / "completion-report.md").exists())
        index = self.registry.load_index(self.registry_root)
        self.assertIn(run_id, index["runs"])
        return run_path

    def test_droid_tracked_run_end_to_end(self):
        completed = self.run_tracked_droid("e2e task")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("delegate run droid completed", completed.stdout)
        self.assertIn("alias:", completed.stdout)
        self.assertIn("snapshot:", completed.stdout)
        self.assertIn("completion report:", completed.stdout)
        self.assertNotIn("OUT:", completed.stdout)
        self.assertNotIn("ERR:", completed.stdout)
        self.assertEqual(completed.stderr, "")

        alias = parse_alias_from_bounded_stdout(completed.stdout)
        run_id, run_path = self.lookup_run(alias)
        self.assert_registry_files(run_id)

        stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
        self.assertIn(ASSISTANT_MARKER, stdout_text)
        self.assertNotIn("OUT:", stdout_text)
        self.assertIn(STDERR_MARKER, (run_path / "stderr.log").read_text(encoding="utf-8"))
        self.assertIn(
            COMPLETION_MARKER, (run_path / "completion-report.md").read_text(encoding="utf-8")
        )
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        index_entry = self.registry.load_index(self.registry_root)["runs"][run_id]
        self.assertEqual(manifest["modelAlias"], "minimax")
        self.assertEqual(index_entry["modelAlias"], "minimax")
        self.assertEqual(manifest["modelResolved"], "e2e-model-id")
        self.assertEqual(index_entry["modelResolved"], "e2e-model-id")

        snapshot = self.run_cli(["snapshot", alias])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertIn(alias, snapshot.stdout)
        self.assertIn(ASSISTANT_MARKER, snapshot.stdout)
        self.assertNotIn("OUT:", snapshot.stdout)

        runs = self.run_cli(["runs", "--recent", "--limit", "5"])
        self.assertEqual(runs.returncode, 0, runs.stderr)
        self.assertIn(alias, runs.stdout)
        self.assertIn("succeeded", runs.stdout.lower())

        report = self.run_cli(["run-output", alias, "--completion-report"])
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn(COMPLETION_MARKER, report.stdout)

        stdout_out = self.run_cli(["run-output", alias, "--stdout", "--tail", "3"])
        self.assertEqual(stdout_out.returncode, 0, stdout_out.stderr)
        self.assertIn(ASSISTANT_MARKER, stdout_out.stdout)

        stderr_out = self.run_cli(["run-output", alias, "--stderr", "--tail", "1"])
        self.assertEqual(stderr_out.returncode, 0, stderr_out.stderr)
        self.assertIn(STDERR_MARKER, stderr_out.stdout)

        self.write_fake_binaries()
        json_run = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "droid",
                "minimax",
                "safe",
                "json e2e",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_run.returncode, 0, json_run.stderr)
        payload = json.loads(json_run.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("alias", payload)
        self.assertIn("runId", payload)
        self.assertIn("snapshotCommand", payload)
        self.assertIn("completionReportCommand", payload)
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)

    def test_failed_run_propagates_exit_code_and_inspection_still_works(self):
        completed = self.run_tracked_droid("fail me", fake_exit="7")
        self.assertEqual(completed.returncode, 7, completed.stderr)
        self.assertIn("status: failed", completed.stdout)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        run_id, run_path = self.lookup_run(alias)
        self.assert_registry_files(run_id)
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exitCode"], 7)

        self.write_fake_binaries()
        json_run = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "droid",
                "minimax",
                "safe",
                "json fail",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(fake_exit="7"),
            check=False,
        )
        self.assertEqual(json_run.returncode, 7, json_run.stderr)
        payload = json.loads(json_run.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "child_failed")
        self.assertEqual(payload["exitCode"], 7)
        self.assertEqual(payload["status"], "failed")

        snapshot = self.run_cli(["snapshot", alias])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertIn(alias, snapshot.stdout)

        runs = self.run_cli(["runs", "--recent", "--limit", "5"])
        self.assertEqual(runs.returncode, 0, runs.stderr)
        self.assertIn(alias, runs.stdout)
        self.assertIn("failed", runs.stdout.lower())

        stdout_out = self.run_cli(["run-output", alias, "--stdout", "--tail", "2"])
        self.assertEqual(stdout_out.returncode, 0, stdout_out.stderr)
        self.assertIn(ASSISTANT_MARKER, stdout_out.stdout)

    def test_silent_running_harness_snapshots_as_running(self):
        self.write_fake_binaries(sleeping=True)
        started_fifo = self.workspace / "fake-started.fifo"
        release_fifo = self.workspace / "fake-release.fifo"
        os.mkfifo(started_fifo)
        os.mkfifo(release_fifo)
        env = self.delegate_env()
        env["FAKE_STARTED_FIFO"] = str(started_fifo)
        env["FAKE_RELEASE_FIFO"] = str(release_fifo)
        process = subprocess.Popen(
            [
                sys.executable,
                str(CLI_PATH),
                "--cwd",
                str(self.workspace),
                "droid",
                "minimax",
                "safe",
                "silent work",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            self.assertEqual(read_fifo_line(started_fifo, timeout=5), "started")
            index = self.registry.load_index(self.registry_root)
            run_id = index["aliases"].get("droid-1")
            self.assertIsNotNone(run_id)

            snapshot = self.run_cli(["snapshot", "droid"])
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            self.assertIn("droid-1 · running", snapshot.stdout)
            self.assertNotIn("stale", snapshot.stdout)

            with release_fifo.open("w", encoding="utf-8") as release:
                release.write("go\n")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("alias: droid", stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_progress_emits_stderr_heartbeats_and_preserves_json_stdout(self):
        self.write_fake_binaries(sleeping=True)
        started_fifo = self.workspace / "progress-started.fifo"
        release_fifo = self.workspace / "progress-release.fifo"
        os.mkfifo(started_fifo)
        os.mkfifo(release_fifo)
        env = self.delegate_env()
        env["FAKE_STARTED_FIFO"] = str(started_fifo)
        env["FAKE_RELEASE_FIFO"] = str(release_fifo)
        env["DELEGATE_PROGRESS_INITIAL_DELAY_SEC"] = "0.05"
        env["DELEGATE_PROGRESS_INTERVAL_SEC"] = "0.05"
        process = subprocess.Popen(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "droid",
                "minimax",
                "safe",
                "--progress",
                "silent work",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            self.assertEqual(read_fifo_line(started_fifo, timeout=5), "started")
            time.sleep(0.2)
            with release_fifo.open("w", encoding="utf-8") as release:
                release.write("go\n")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            payload = json.loads(stdout)
            self.assertTrue(payload["ok"])
            self.assertIn("delegate: run started", stderr)
            self.assertIn("delegate: still running", stderr)
            self.assertNotIn(ASSISTANT_MARKER, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_run_output_raw_and_json_truncated_metadata(self):
        completed = self.run_tracked_droid("output metadata")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        _run_id, run_path = self.lookup_run(alias)
        (run_path / "stdout.log").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        raw_text = self.run_cli(["run-output", alias, "--raw"])
        self.assertEqual(raw_text.returncode, 0, raw_text.stderr)
        self.assertIn("alpha", raw_text.stdout)
        self.assertIn("gamma", raw_text.stdout)

        json_tail = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--stdout",
                "--tail",
                "2",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_tail.returncode, 0, json_tail.stderr)
        tail_payload = json.loads(json_tail.stdout)
        stdout_section = tail_payload["sections"]["stdout"]
        self.assertTrue(stdout_section["truncated"])
        self.assertEqual(stdout_section["content"], "beta\ngamma\n")
        self.assertGreater(stdout_section["bytes"], 0)

        text_capped = self.run_cli(["run-output", alias, "--stdout", "--max-chars", "5"])
        self.assertEqual(text_capped.returncode, 0, text_capped.stderr)
        self.assertIn("last 5 chars", text_capped.stdout)
        self.assertIn("omitted", text_capped.stdout)

        json_raw = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--raw",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_raw.returncode, 0, json_raw.stderr)
        raw_payload = json.loads(json_raw.stdout)
        raw_stdout = raw_payload["sections"]["stdout"]
        self.assertFalse(raw_stdout["truncated"])
        self.assertEqual(raw_stdout["content"], "alpha\nbeta\ngamma\n")

        json_report = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--completion-report",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_report.returncode, 0, json_report.stderr)
        report_payload = json.loads(json_report.stdout)
        report_section = report_payload["sections"]["completionReport"]
        self.assertIn("bytes", report_section)
        self.assertIn(COMPLETION_MARKER, report_section["content"])
        self.assertNotIn("truncated", report_section)

    def test_json_pass_through_rejected_end_to_end(self):
        self.write_fake_binaries(passthrough=True)
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--pass-through",
                "--cwd",
                str(self.workspace),
                "droid",
                "minimax",
                "safe",
                "legacy",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("invalid_option_combination", completed.stderr + completed.stdout)

    def test_second_droid_run_uses_droid_2_alias(self):
        first = self.run_tracked_droid("first droid")
        self.assertEqual(first.returncode, 0, first.stderr)
        alias1 = parse_alias_from_bounded_stdout(first.stdout)
        self.assertEqual(alias1, "droid-1")

        second = self.run_tracked_droid("second droid")
        self.assertEqual(second.returncode, 0, second.stderr)
        alias2 = parse_alias_from_bounded_stdout(second.stdout)
        self.assertEqual(alias2, "droid-2")

        runs = self.run_cli(["runs", "--recent", "--limit", "10"])
        self.assertEqual(runs.returncode, 0, runs.stderr)
        self.assertIn("droid-1", runs.stdout)
        self.assertIn("droid-2", runs.stdout)

        snapshot2 = self.run_cli(["snapshot", "droid-2"])
        self.assertEqual(snapshot2.returncode, 0, snapshot2.stderr)
        self.assertIn("droid-2", snapshot2.stdout)
        self.assertIn(ASSISTANT_MARKER, snapshot2.stdout)

        json_latest = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "snapshot",
                "--latest",
                "droid",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_latest.returncode, 0, json_latest.stderr)
        latest_payload = json.loads(json_latest.stdout)
        self.assertEqual(latest_payload["alias"], "droid-2")

    def test_runs_harness_filter_and_json_schema(self):
        self.run_tracked_droid("droid one")
        self.write_fake_binaries()
        self.run_cli(["cursor", "work", "cursor one"])

        droid_runs = self.run_cli(["runs", "--harness", "droid", "--limit", "5"])
        self.assertEqual(droid_runs.returncode, 0, droid_runs.stderr)
        self.assertIn("droid", droid_runs.stdout)
        self.assertNotIn("cursor", droid_runs.stdout)

        json_runs = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "runs",
                "--harness",
                "droid",
                "--limit",
                "3",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_runs.returncode, 0, json_runs.stderr)
        payload = json.loads(json_runs.stdout)
        self.assertEqual(payload["schema"], "delegate.runs.v1")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "recent")
        self.assertEqual(payload["limit"], 3)
        self.assertTrue(payload["runs"])
        self.assertTrue(all(run["harness"] == "droid" for run in payload["runs"]))

    def test_no_completion_report_skips_file_without_harness_completion_event(self):
        completed = self.run_tracked_droid(
            "no harness completion",
            global_args=["--completion-report", "none"],
            include_completion=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        run_id, run_path = self.lookup_run(alias)
        self.assert_registry_files(run_id, expect_completion_report=False)
        self.assertFalse((run_path / "completion-report.md").exists())

    def test_completion_fallback_uses_assistant_text_without_completion_event(self):
        completed = self.run_tracked_droid(
            "assistant fallback",
            include_completion=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        _run_id, run_path = self.lookup_run(alias)
        report = (run_path / "completion-report.md").read_text(encoding="utf-8")
        self.assertIn(ASSISTANT_MARKER, report)
        self.assertNotIn(COMPLETION_MARKER, report)

    def test_snapshot_and_run_output_no_redact_preserve_secrets(self):
        secret_script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'printf \'{{"type":"message","role":"assistant","content":"token {SECRET_TOKEN}"}}\\n\'\n'
            f'printf \'{{"type":"completion","finalText":"{COMPLETION_MARKER}"}}\\n\'\n'
            'exit "${FAKE_EXIT:-0}"\n'
        )
        for name in ("droid", "agent"):
            path = self.bin_dir / name
            path.write_text(secret_script, encoding="utf-8")
            path.chmod(0o755)

        completed = self.run_cli(["droid", "minimax", "safe", "secret run"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)

        snapshot = self.run_cli(["snapshot", "--no-redact", alias])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertIn(SECRET_TOKEN, snapshot.stdout)

        stdout_out = self.run_cli(["run-output", alias, "--stdout", "--raw", "--no-redact"])
        self.assertEqual(stdout_out.returncode, 0, stdout_out.stderr)
        self.assertIn(SECRET_TOKEN, stdout_out.stdout)

    def test_snapshot_and_run_output_by_run_id(self):
        completed = self.run_tracked_droid("run id lookup")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        run_id, _run_path = self.lookup_run(alias)

        snapshot = self.run_cli(["snapshot", run_id])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertIn(ASSISTANT_MARKER, snapshot.stdout)

        json_snapshot = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "snapshot",
                run_id,
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_snapshot.returncode, 0, json_snapshot.stderr)
        self.assertEqual(json.loads(json_snapshot.stdout)["runId"], run_id)

        stdout_out = self.run_cli(["run-output", run_id, "--stdout", "--tail", "5"])
        self.assertEqual(stdout_out.returncode, 0, stdout_out.stderr)
        self.assertIn(ASSISTANT_MARKER, stdout_out.stdout)

    def test_missing_handle_errors_for_snapshot_and_run_output(self):
        completed = self.run_tracked_droid("handle errors")
        self.assertEqual(completed.returncode, 0, completed.stderr)

        snapshot = self.run_cli(["snapshot", "definitely-missing-alias"])
        self.assertNotEqual(snapshot.returncode, 0)
        self.assertIn("unknown_handle", snapshot.stderr + snapshot.stdout)

        run_output = self.run_cli(
            ["run-output", "definitely-missing-alias", "--stdout", "--tail", "1"]
        )
        self.assertNotEqual(run_output.returncode, 0)
        self.assertIn("unknown_handle", run_output.stderr + run_output.stdout)

    def test_cursor_work_tracked_run_bounded_json(self):
        self.write_fake_binaries()
        completed = self.run_cli(["cursor", "work", "cursor e2e"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        self.assertEqual(alias, "cursor-1")
        run_id, _run_path = self.lookup_run(alias)
        self.assert_registry_files(run_id)

        json_snapshot = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "snapshot",
                alias,
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_snapshot.returncode, 0, json_snapshot.stderr)
        payload = json.loads(json_snapshot.stdout)
        self.assertEqual(payload["schema"], "delegate.snapshot.v1")
        self.assertEqual(payload["alias"], alias)
        self.assertIn("assistantText", payload)

    def test_codex_work_tracked_run_bounded_json(self):
        self.write_fake_binaries()
        completed = self.run_cli(["codex", "work", "codex e2e"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("delegate run codex completed", completed.stdout)
        self.assertIn(
            f"snapshot: delegate --cwd {self.workspace.resolve()} snapshot codex-1",
            completed.stdout,
        )

        snapshot = self.run_cli(["snapshot", "codex"])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertIn(CODEX_ASSISTANT_MARKER, snapshot.stdout)

        json_snapshot = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "snapshot",
                "codex",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(),
            check=False,
        )
        self.assertEqual(json_snapshot.returncode, 0, json_snapshot.stderr)
        payload = json.loads(json_snapshot.stdout)
        self.assertIn("assistantText", payload)
        self.assertIn(CODEX_ASSISTANT_MARKER, payload["assistantText"])
        self.assertIsNone(payload.get("model"))

    def test_codex_safe_fast_metadata_survives_temporary_isolation(self):
        self.write_fake_binaries()
        completed = self.run_cli(["codex", "safe", "--fast", "codex fast e2e"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        snapshot = self.run_cli(["--json", "snapshot", "codex"])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        payload = json.loads(snapshot.stdout)
        self.assertIs(payload["requestedFast"], True)
        run_id, run_path = self.lookup_run("codex-1")
        self.assertIsNotNone(run_id)
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn('service_tier="fast"', manifest["argv"])

    def test_pass_through_preserves_raw_output_without_tracked_run(self):
        self.write_fake_binaries(passthrough=True)
        completed = self.run_cli(["--pass-through", "droid", "minimax", "safe", "legacy"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OUT:pass-through", completed.stdout)
        self.assertIn("ERR:pass-through", completed.stderr)
        self.assertNotIn("alias:", completed.stdout)
        self.assertNotIn("delegate run droid completed", completed.stdout)
        if self.registry.index_path(self.registry_root).exists():
            index = self.registry.load_index(self.registry_root)
            self.assertEqual(index["aliases"], {})
            self.assertEqual(index["runs"], {})
        run_entries = list(self.registry.runs_dir(self.registry_root).glob("*"))
        self.assertEqual(run_entries, [])

    def test_archived_stdout_retrieval_via_run_output(self):
        config_path = self.write_workspace_config(raw_log_days=0)
        completed = self.run_tracked_droid("archive me", config_path=config_path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        run_id, run_path = self.lookup_run(alias)
        self.assertTrue((run_path / "stdout.log").exists())

        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        state["status"] = "succeeded"
        state["finishedAt"] = old
        state["lastActivityAt"] = old
        self.registry.write_json_atomic(run_path / "state.json", state)

        snapshot = self.run_cli(["snapshot", alias], config_path=config_path)
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertFalse((run_path / "stdout.log").exists())
        self.assertTrue(self.retention.archive_path(self.registry_root, run_id).exists())

        stdout_out = self.run_cli(
            ["run-output", alias, "--stdout", "--tail", "2"],
            config_path=config_path,
        )
        self.assertEqual(stdout_out.returncode, 0, stdout_out.stderr)
        self.assertIn(ASSISTANT_MARKER, stdout_out.stdout)

        json_tail = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "run-output",
                alias,
                "--stdout",
                "--tail",
                "2",
            ],
            text=True,
            capture_output=True,
            env=self.delegate_env(config_path),
            check=False,
        )
        self.assertEqual(json_tail.returncode, 0, json_tail.stderr)
        payload = json.loads(json_tail.stdout)
        self.assertFalse(payload["sections"]["stdout"]["truncated"])

    def test_tracked_grok_safe_run_records_prompt_file_and_snapshot(self):
        argv_log = self.workspace / "grok-argv.log"
        self.write_fake_binaries()
        env = self.delegate_env()
        env["GROK_ARGV_LOG"] = str(argv_log)
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--json",
                "--cwd",
                str(self.workspace),
                "grok",
                "safe",
                "Review this workspace. Do not edit files.",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        payload = json.loads(completed.stdout)
        alias = payload["alias"]
        run_id, run_path = self.lookup_run(alias)
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["promptTransport"], "file")
        self.assertNotIn("Review this workspace", json.dumps(manifest.get("argv", [])))
        self.assertTrue(argv_log.exists())
        recorded = argv_log.read_text(encoding="utf-8")
        self.assertIn("--prompt-file", recorded)
        self.assertIn("streaming-json", recorded)
        self.assert_registry_files(run_id)
        snapshot_payload = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot_payload["harness"], "grok")
        self.assertIn("delegate grok fixture ok", snapshot_payload.get("assistantText", ""))
        report = self.run_cli(["run-output", alias, "--completion-report"])
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("delegate grok fixture ok", report.stdout)

    def test_tracked_grok_without_end_recovers_completion_report_from_stream(self):
        self.write_fake_binaries(include_completion=False)
        completed = self.run_cli(["grok", "safe", "Recover streamed Grok text."])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = parse_alias_from_bounded_stdout(completed.stdout)
        run_id, run_path = self.lookup_run(alias)
        self.assert_registry_files(run_id)

        stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
        self.assertIn('"type":"text","data":"delegate"', stdout_text)
        self.assertIn('"type":"text","data":" ok"', stdout_text)
        self.assertNotIn('"type":"end"', stdout_text)
        report = (run_path / "completion-report.md").read_text(encoding="utf-8")
        self.assertIn("delegate grok fixture ok", report)


if __name__ == "__main__":
    unittest.main()
