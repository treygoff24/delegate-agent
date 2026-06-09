import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
CLI_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
RUNNER_PATH = ROOT / "src" / "delegate_agent" / "runner.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_streaming_fake_bin():
    temp = tempfile.TemporaryDirectory()
    script = Path(temp.name) / "droid"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' \'{"type":"message","role":"assistant","content":"HELLO"}\'\n'
        'printf \'%s\\n\' \'{"type":"completion","finalText":"DONE"}\' >&1\n'
        "printf 'ERR:noise\\n' >&2\n"
        'exit "${FAKE_EXIT:-0}"\n'
    )
    script.chmod(0o755)
    return temp, script.parent


@contextlib.contextmanager
def open_pipe_as_process_stdin():
    saved_stdin = os.dup(0)
    read_fd, write_fd = os.pipe()
    try:
        os.dup2(read_fd, 0)
        os.close(read_fd)
        yield
    finally:
        os.dup2(saved_stdin, 0)
        for fd in (saved_stdin, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def write_stdin_probe_script(path: Path) -> None:
    path.write_text(
        "import select\n"
        "import sys\n"
        "from pathlib import Path\n"
        "ready, _, _ = select.select([sys.stdin], [], [], 0)\n"
        "if ready:\n"
        "    data = sys.stdin.read(1)\n"
        "    status = 'stdin:eof' if data == '' else 'stdin:data'\n"
        "else:\n"
        "    status = 'stdin:blocked'\n"
        "if len(sys.argv) > 1:\n"
        "    Path(sys.argv[1]).write_text(status, encoding='utf-8')\n"
        "else:\n"
        "    print(status)\n",
        encoding="utf-8",
    )


class RunnerCaptureTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module(RUNNER_PATH, "delegate_runner_under_test")
        self.registry = load_module(REGISTRY_PATH, "delegate_registry_runner_test")

    def test_write_stdin_records_delivery_failure(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        pipe = os.fdopen(write_fd, "wb")
        failures: list[str] = []
        self.runner._write_stdin(pipe, "x" * 65536, failures)
        self.assertEqual(len(failures), 1)
        self.assertIn("stdin prompt delivery", failures[0])

    def test_tracked_run_surfaces_stdin_delivery_failure(self):
        # Child closes stdin without reading the prompt: the run must warn
        # instead of silently proceeding as if delivery succeeded.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "closer"
        script.write_text(
            "#!/usr/bin/env bash\nexec 0<&-\nsleep 0.2\nexit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            stderr = io.StringIO()
            code, _payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=stderr,
                stdin_text="x" * (1 << 20),
            )
            self.assertEqual(code, 0)
            self.assertIn("stdin prompt delivery", stderr.getvalue())
            snapshot = json.loads(
                (root / "runs" / run_id / "snapshot.json").read_text(encoding="utf-8")
            )
            warnings = snapshot.get("warnings", [])
            self.assertTrue(any("stdin prompt delivery" in w for w in warnings))

    def test_tracked_run_writes_logs_and_bounded_json(self):
        temp, bin_dir = make_streaming_fake_bin()
        self.addCleanup(temp.cleanup)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            argv = [str(bin_dir / "droid"), "exec", "hello"]
            env_path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": env_path}):
                code, payload = self.runner.execute_tracked(
                    argv,
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["alias"], alias)
            self.assertEqual(payload["runId"], run_id)
            self.assertIn("snapshotCommand", payload)
            self.assertIn("completionReportCommand", payload)
            self.assertEqual(payload["exitCode"], 0)
            self.assertNotIn("stdout", payload)
            self.assertNotIn("stderr", payload)
            run_path = self.registry.run_directory(root, run_id)
            self.assertTrue((run_path / "stdout.log").exists())
            self.assertTrue((run_path / "stderr.log").exists())
            stdout_text = (run_path / "stdout.log").read_text()
            self.assertIn("HELLO", stdout_text)
            self.assertNotIn("OUT:", stdout_text)

    def test_tracked_run_gives_child_eof_stdin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_probe.py"
        write_stdin_probe_script(script)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            with open_pipe_as_process_stdin():
                code, _payload = self.runner.execute_tracked(
                    [sys.executable, str(script)],
                    workspace,
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
            self.assertIn("stdin:eof", stdout_text)
            self.assertNotIn("stdin:blocked", stdout_text)

    def test_tracked_run_can_deliver_prompt_via_stdin_without_manifest_leak(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_echo.py"
        script.write_text(
            "import sys\ndata = sys.stdin.read()\nprint('STDIN:' + data)\n",
            encoding="utf-8",
        )
        secret_prompt = "TOP-SECRET-STDIN-PROMPT"
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                prompt_transport="stdin",
            )
            code, _payload = self.runner.execute_tracked(
                [sys.executable, str(script), "-"],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                stdin_text=secret_prompt,
                manifest_argv=[sys.executable, str(script), "<prompt via stdin>"],
            )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
            self.assertIn(secret_prompt, stdout_text)
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["promptTransport"], "stdin")
            self.assertNotIn(secret_prompt, json.dumps(manifest["argv"]))

    def test_tracked_run_can_deliver_prompt_via_private_file_without_manifest_leak(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "prompt_file_reader.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "prompt_path = Path(sys.argv[sys.argv.index('--file') + 1])\n"
            "print('PROMPT_FILE:' + str(prompt_path))\n"
            "print('PROMPT:' + prompt_path.read_text(encoding='utf-8'))\n",
            encoding="utf-8",
        )
        secret_prompt = "TOP-SECRET-FILE-PROMPT"
        placeholder = "<delegate-prompt-file>"
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
                prompt_transport="file",
            )
            code, _payload = self.runner.execute_tracked(
                [sys.executable, str(script), "--file", placeholder],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                prompt_file_text=secret_prompt,
                prompt_file_placeholder=placeholder,
                manifest_argv=[sys.executable, str(script), "--file", "<prompt file>"],
            )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            stdout_text = (run_path / "stdout.log").read_text(encoding="utf-8")
            self.assertIn(secret_prompt, stdout_text)
            prompt_file_line = next(
                line.removeprefix("PROMPT_FILE:")
                for line in stdout_text.splitlines()
                if line.startswith("PROMPT_FILE:")
            )
            self.assertFalse(Path(prompt_file_line).exists())
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["promptTransport"], "file")
            self.assertNotIn(secret_prompt, json.dumps(manifest["argv"]))

    def test_tracked_codex_item_completed_writes_completion_report(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"I am working"}}\'\n'
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"reasoning","text":"hidden reasoning"}}\'\n'
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"Status: completed\\\\n- final from codex"}}\'\n'
            "printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, _payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            run_path = self.registry.run_directory(root, run_id)
            report = (run_path / "completion-report.md").read_text(encoding="utf-8")
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertIn("final from codex", report)
            self.assertNotIn("I am working", report)
            self.assertIn("I am working", snapshot["assistantText"])
            self.assertNotIn("hidden reasoning", snapshot["assistantText"])

    def test_tracked_codex_progress_message_before_command_does_not_write_report(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "codex"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\n' '{\"type\":\"turn.started\"}'\n"
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"I will inspect the repo first"}}\'\n'
            'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"command_execution","command":"pwd","status":"completed"}}\'\n'
            "printf '%s\n' '{\"type\":\"turn.completed\"}'\n"
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="codex")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="safe",
                model=None,
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            code, payload = self.runner.execute_tracked(
                [str(script)],
                workspace,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertNotIn("completionReportCommand", payload)
            self.assertNotIn("completionReportPath", payload)
            run_path = self.registry.run_directory(root, run_id)
            self.assertFalse((run_path / "completion-report.md").exists())
            snapshot = json.loads((run_path / "snapshot.json").read_text(encoding="utf-8"))
            self.assertNotIn("completionReport", snapshot)
            self.assertIn("I will inspect the repo first", snapshot["assistantText"])

    def test_tracked_text_output_omits_raw_streams(self):
        temp, bin_dir = make_streaming_fake_bin()
        self.addCleanup(temp.cleanup)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            [str(bin_dir / "droid"), "exec", "hello"]
            env_path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": env_path}):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(CLI_PATH),
                        "--cwd",
                        workspace,
                        "droid",
                        "minimax",
                        "safe",
                        "hello",
                    ],
                    text=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "PATH": env_path,
                        "DELEGATE_CONFIG": str(self._config_path(workspace)),
                    },
                    check=False,
                )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("alias:", completed.stdout)
            self.assertNotIn("OUT:", completed.stdout)
            self.assertNotIn("ERR:", completed.stdout)
            self.assertEqual(completed.stderr, "")

    def _config_path(self, workspace: str) -> str:
        path = Path(workspace) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "droid": {
                        "binary": "droid",
                        "models": {"minimax": "model-id"},
                    }
                }
            )
        )
        return str(path)

    def test_pass_through_cli_preserves_raw_output(self):
        repo_temp = tempfile.TemporaryDirectory()
        subprocess.run(
            ["git", "-C", repo_temp.name, "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(repo_temp.cleanup)
        temp, bin_dir = make_streaming_fake_bin()
        self.addCleanup(temp.cleanup)
        fake = bin_dir / "droid"
        fake.write_text(
            "#!/usr/bin/env bash\nprintf 'OUT:raw\\n'\nprintf 'ERR:raw\\n' >&2\nexit 0\n"
        )
        fake.chmod(0o755)
        config = Path(repo_temp.name) / "config.json"
        config.write_text(
            json.dumps(
                {
                    "droid": {
                        "binary": "droid",
                        "models": {"minimax": "model-id"},
                    }
                }
            )
        )
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--pass-through",
                "--cwd",
                repo_temp.name,
                "droid",
                "minimax",
                "safe",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("OUT:raw", completed.stdout)
        self.assertIn("ERR:raw", completed.stderr)

    def test_passthrough_run_gives_child_eof_stdin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_probe.py"
        write_stdin_probe_script(script)
        with tempfile.TemporaryDirectory() as workspace:
            marker = Path(workspace) / "stdin-state.txt"
            with open_pipe_as_process_stdin():
                code = self.runner.execute_passthrough(
                    [sys.executable, str(script), str(marker)],
                    workspace,
                )
            self.assertEqual(code, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "stdin:eof")

    def test_passthrough_run_can_deliver_prompt_via_stdin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "stdin_writer.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as workspace:
            marker = Path(workspace) / "stdin-prompt.txt"
            code = self.runner.execute_passthrough(
                [sys.executable, str(script), str(marker)],
                workspace,
                stdin_text="PROMPT-VIA-STDIN",
            )
            self.assertEqual(code, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "PROMPT-VIA-STDIN")

    def test_completion_json_payload_always_includes_exit_code(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="alias-1",
            harness="droid",
            engine="droid",
            mode="safe",
            model="model-id",
            source_cwd="/tmp",
            execution_cwd="/tmp",
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-05-20T21:42:33Z",
        )
        payload = self.runner.completion_json_payload(
            ctx,
            ok=True,
            status="succeeded",
            exit_code=0,
            duration_ms=100,
            stdout_bytes=10,
            stderr_bytes=0,
        )
        self.assertEqual(payload["exitCode"], 0)

    def test_persistent_worktree_completion_payload_includes_force_cleanup_command(self):
        ctx = self.runner.RunContext(
            registry_root=Path("/tmp"),
            run_id="run-1",
            alias="cursor-1",
            harness="cursor",
            engine="cursor",
            mode="work",
            model="composer-2.5",
            source_cwd="/repo",
            execution_cwd="/wt",
            workspace_kind="git",
            isolated_workspace=True,
            started_at="2026-05-20T21:42:33Z",
            source_git_root="/repo",
            isolation_mode="worktree",
            effective_isolation="worktree",
            isolation_lifecycle="persistent",
            preserved_workspace=True,
            branch="delegate/cursor-1",
            worktree_status="present",
        )
        payload = self.runner.completion_json_payload(
            ctx,
            ok=True,
            status="succeeded",
            exit_code=0,
            duration_ms=100,
            stdout_bytes=0,
            stderr_bytes=0,
        )
        cleanup = payload["worktreeCleanupCommands"]
        self.assertEqual(cleanup["force"], "delegate worktree remove cursor-1 --force")

    def test_join_drain_thread_completes_after_pipe_drained(self):
        reader, writer = os.pipe()
        os.write(writer, b"line\n")
        os.close(writer)
        read_fd = os.fdopen(reader, "rb", closefd=True)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "stdout.log"
            byte_counter = [0]
            thread = threading.Thread(
                target=self.runner._drain_stream,
                args=(read_fd, log_path, byte_counter),
                kwargs={"on_line": None},
                daemon=True,
            )
            thread.start()
            self.runner._join_drain_thread(thread, read_fd)
            read_fd.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(byte_counter[0], len(b"line\n"))

    def test_tracked_run_batches_progress_persistence(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        script = Path(temp.name) / "droid"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "for i in $(seq 0 24); do\n"
            '  printf \'%s\\n\' "{\\"type\\":\\"message\\",\\"role\\":\\"assistant\\",\\"content\\":\\"line${i}\\"}"\n'
            "done\n"
            "exit 0\n"
        )
        script.chmod(0o755)
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="directory")
            run_id, alias = self.registry.register_run(root, harness="droid")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="droid",
                engine="droid",
                mode="safe",
                model="model-id",
                source_cwd=workspace,
                execution_cwd=workspace,
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-05-20T21:42:33Z",
            )
            with mock.patch.object(self.runner, "persist_progress") as persist_mock:
                self.runner.execute_tracked(
                    [str(script)],
                    workspace,
                    ctx,
                    json_mode=False,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            running_persists = [
                call
                for call in persist_mock.call_args_list
                if call.kwargs.get("status") == "running"
            ]
            self.assertGreater(len(running_persists), 0)
            self.assertLess(len(running_persists), 25)

    def test_running_persist_skips_when_no_new_lines_between_ticks(self):
        progress_dirty = True
        outcomes: list[str] = []

        def maybe_persist_running() -> None:
            nonlocal progress_dirty
            if not progress_dirty:
                outcomes.append("skipped")
                return
            progress_dirty = False
            outcomes.append("persisted")

        maybe_persist_running()
        maybe_persist_running()
        self.assertEqual(outcomes, ["persisted", "skipped"])

    def test_emit_bounded_text_summary_uses_shared_cleanup_renderer(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = self.registry.ensure_registry(Path(workspace), workspace_kind="git")
            run_id, alias = self.registry.register_run(root, harness="cursor")
            ctx = self.runner.RunContext(
                registry_root=root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="work",
                model="composer-2.5",
                source_cwd=workspace,
                execution_cwd=str(Path(workspace) / "wt"),
                workspace_kind="git",
                isolated_workspace=True,
                started_at="2026-05-20T21:42:33Z",
                source_git_root=workspace,
                isolation_lifecycle="persistent",
                branch="delegate/cursor-demo",
            )
            stdout = io.StringIO()
            self.runner.emit_bounded_text_summary(
                ctx,
                status="completed",
                duration_ms=1200,
                stdout=stdout,
            )
            output = stdout.getvalue()
            self.assertIn(
                f"cleanup (refuses dirty / unmerged):       delegate worktree remove {alias}",
                output,
            )
            self.assertIn(
                f"cleanup (DISCARD uncommitted edits):      delegate worktree remove {alias} --discard-uncommitted",
                output,
            )
            self.assertIn("raw git equivalent:", output)
            self.assertIn(f"git -C {workspace} worktree remove {Path(workspace) / 'wt'}", output)
            self.assertIn("git -C", output)
            self.assertIn("branch -d delegate/cursor-demo", output)
            self.assertNotIn("worktree remove --force", output)
            self.assertNotIn("branch -D", output)
