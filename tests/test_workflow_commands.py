from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from delegate_agent import (  # noqa: E402
    harness_discovery,
    request_build,
    run_registry,
    safe_workspace,
    sandbox_bwrap,
    workflow_pinning,
)
from delegate_agent.isolation import build_isolation_context  # noqa: E402
from delegate_agent.request_models import (  # noqa: E402
    GlobalOptions,
    ParsedCommand,
    ResolvedWorkspace,
    RunJsonOptions,
)
from delegate_agent.workflows import commands as workflow_commands  # noqa: E402
from delegate_agent.workflows import registry as workflow_registry  # noqa: E402
from delegate_agent.workflows import runtime as workflow_runtime  # noqa: E402
from delegate_agent.workflows import schema as workflow_schema  # noqa: E402
from tests import proc_harness  # noqa: E402

CLI = ROOT / "bin" / "delegate.py"


def _argv_pairs(argv: list[str]) -> list[list[str]]:
    pairs: list[list[str]] = []
    for index, flag in enumerate(argv):
        width = 3 if flag == "--ro-bind" else 2 if flag == "--tmpfs" else 0
        if width:
            pairs.append(argv[index : index + width])
    return pairs


class WorkflowCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.home = self.workspace / "home"
        self.home.mkdir()
        self.codex_home = self.home / "codex-work"
        self.codex_home.mkdir()
        (self.codex_home / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
        (self.home / ".delegate").mkdir()
        (self.home / ".delegate" / "personas").mkdir()
        (self.home / ".delegate" / "config.work.json").write_text("{}\n", encoding="utf-8")
        self.bin_dir = self.workspace / "bin"
        self.bin_dir.mkdir()
        self._write_fake_codex()
        self.config_path = self.workspace / ".delegate" / "config.json"
        self.config_path.parent.mkdir()
        self.config_path.write_text(
            json.dumps(
                {
                    "codex": {"binary": str(self.bin_dir / "codex")},
                    "cursor": {
                        "argvPrefix": [str(self.bin_dir / "agent")],
                        "defaultModel": "composer-2.5",
                    },
                    "droid": {
                        "binary": str(self.bin_dir / "droid"),
                        "models": {"gemini": "fake-gemini"},
                    },
                    "devin": {"binary": str(self.bin_dir / "devin")},
                    "profiles": {
                        "detectFrom": ["DELEGATE_PROFILE", "AI_PROFILE"],
                        "default": None,
                        "definitions": {"work": {"env": {"CODEX_HOME": str(self.codex_home)}}},
                    },
                    "workflows": {"itemThreads": 4, "structuredOutputRetries": 1},
                }
            ),
            encoding="utf-8",
        )
        devin = self.bin_dir / "devin"
        devin.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print('devin 3000.4.25')\n"
            "    raise SystemExit(0)\n"
            "# Read-only lanes must materialize a real config file.\n"
            "if '--config' in sys.argv:\n"
            "    cfg = sys.argv[sys.argv.index('--config') + 1]\n"
            "    if not os.path.isfile(cfg):\n"
            "        sys.stderr.write(f'missing agent config: {cfg}\\n')\n"
            "        sys.exit(1)\n"
            "prompt = ''\n"
            "if '--prompt-file' in sys.argv:\n"
            "    prompt = open(sys.argv[sys.argv.index('--prompt-file') + 1], encoding='utf-8').read()\n"
            "print('fake devin completion')\n",
            encoding="utf-8",
        )
        devin.chmod(0o755)
        for name in ("agent", "droid"):
            path = self.bin_dir / name
            path.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys, time\n"
                "prompt = (sys.stdin.read() if not sys.stdin.closed else '')\n"
                "if '--file' in sys.argv:\n"
                "    prompt += open(sys.argv[sys.argv.index('--file') + 1], encoding='utf-8').read()\n"
                "prompt += ' '.join(sys.argv[1:])\n"
                "if 'slow' in prompt:\n"
                "    time.sleep(1)\n"
                "prompt_log = os.environ.get('FAKE_GENERIC_PROMPT_LOG')\n"
                "if prompt_log:\n"
                "    open(prompt_log, 'a', encoding='utf-8').write(prompt + '\\n---\\n')\n"
                "workspace_log = os.environ.get('FAKE_GENERIC_WORKSPACE_LOG')\n"
                "if workspace_log:\n"
                "    open(workspace_log, 'a', encoding='utf-8').write(os.getcwd() + '\\n')\n"
                "attempt_file = os.environ.get('FAKE_GENERIC_ATTEMPT_FILE')\n"
                "attempt = 0\n"
                "if attempt_file:\n"
                "    try:\n"
                "        attempt = int(open(attempt_file, encoding='utf-8').read() or '0') + 1\n"
                "    except FileNotFoundError:\n"
                "        attempt = 1\n"
                "    open(attempt_file, 'w', encoding='utf-8').write(str(attempt))\n"
                "if 'Return ONLY' in prompt and attempt_file and attempt == 1:\n"
                "    text = 'not json'\n"
                "else:\n"
                "    text = '{\"ok\": true, \"value\": \"structured\"}' if 'Return ONLY' in prompt else 'fake completion'\n"
                "print(json.dumps({'type':'message','role':'assistant','content':text}))\n"
                "print(json.dumps({'type':'completion','finalText':text}))\n",
                encoding="utf-8",
            )
            path.chmod(0o755)

    def _write_fake_codex(self) -> None:
        path = self.bin_dir / "codex"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "prompt = sys.stdin.read()\n"
            "session_id = os.environ.get('FAKE_CODEX_SESSION_ID')\n"
            "if session_id and os.environ.get('FAKE_CODEX_SESSION_BEFORE_SLEEP'):\n"
            "    print(json.dumps({'type': 'thread.started', 'thread_id': session_id}), flush=True)\n"
            "sleep = os.environ.get('FAKE_CODEX_SLEEP_SECONDS')\n"
            "retry_sleep = os.environ.get('FAKE_CODEX_SLEEP_RETRY_SECONDS')\n"
            "retry_attempt_file = os.environ.get('FAKE_CODEX_ATTEMPT_FILE')\n"
            "if retry_sleep and retry_attempt_file:\n"
            "    try:\n"
            "        retry_attempt = int(open(retry_attempt_file, encoding='utf-8').read() or '0')\n"
            "    except FileNotFoundError:\n"
            "        retry_attempt = 0\n"
            "    if retry_attempt >= 1:\n"
            "        time.sleep(float(retry_sleep))\n"
            "elif sleep:\n"
            "    time.sleep(float(sleep))\n"
            "elif 'very slow' in prompt:\n"
            "    time.sleep(10)\n"
            "elif 'slow' in prompt:\n"
            "    time.sleep(1)\n"
            "log = os.environ.get('FAKE_PROMPT_LOG')\n"
            "if log:\n"
            "    open(log, 'a', encoding='utf-8').write(prompt + '\\n---\\n')\n"
            "argv_log = os.environ.get('FAKE_CODEX_ARGV_LOG')\n"
            "if argv_log:\n"
            "    with open(argv_log, 'a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if session_id and not os.environ.get('FAKE_CODEX_SESSION_BEFORE_SLEEP'):\n"
            "    print(json.dumps({'type': 'thread.started', 'thread_id': session_id}))\n"
            "thread_id = os.environ.get('FAKE_CODEX_THREAD_ID')\n"
            "if thread_id:\n"
            "    print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}))\n"
            "if os.environ.get('FAKE_CODEX_REGISTERED_FAILURE'):\n"
            "    print(json.dumps({'ok': False, 'runId': os.environ['FAKE_CODEX_REGISTERED_FAILURE']}))\n"
            "    sys.exit(1)\n"
            "if os.environ.get('FAKE_CODEX_PREAMBLE_ONLY'):\n"
            '    preamble = \'{"ok": true, "value": "preamble"}\'\n'
            "    print(json.dumps({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': preamble}]}))\n"
            "    sys.exit(0)\n"
            "if os.environ.get('FAKE_CODEX_REAL_SHAPE'):\n"
            '    preamble = \'{"ok": true, "value": "preamble"}\'\n'
            '    final = \'{"ok": true, "value": "final"}\'\n'
            "    print(json.dumps({'type': 'turn.started'}))\n"
            "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': preamble}}))\n"
            "    command = {'type': 'command_execution', 'command': 'python3 -m unittest', 'status': 'in_progress'}\n"
            "    print(json.dumps({'type': 'item.started', 'item': command}))\n"
            "    command['status'] = 'completed'\n"
            "    print(json.dumps({'type': 'item.completed', 'item': command}))\n"
            "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': final}}))\n"
            "    print(json.dumps({'type': 'turn.completed'}))\n"
            "    sys.exit(0)\n"
            "structured = '--output-schema' in sys.argv or 'Return ONLY' in prompt\n"
            "attempt_file = os.environ.get('FAKE_CODEX_ATTEMPT_FILE')\n"
            "if structured and os.environ.get('FAKE_CODEX_NULL'):\n"
            "    text = 'null'\n"
            "elif structured and attempt_file:\n"
            "    try:\n"
            "        attempt = int(open(attempt_file, encoding='utf-8').read() or '0') + 1\n"
            "    except FileNotFoundError:\n"
            "        attempt = 1\n"
            "    open(attempt_file, 'w', encoding='utf-8').write(str(attempt))\n"
            "    short_kind = os.environ.get('FAKE_CODEX_SHORT_KIND')\n"
            "    if attempt == 1 and short_kind == 'minLength':\n"
            '        text = \'{"ok": true, "value": ""}\'\n'
            "    elif attempt == 1 and short_kind == 'minItems':\n"
            '        text = \'{"ok": true, "value": []}\'\n'
            "    elif attempt == 1:\n"
            '        text = \'{"ok": "wrong"}\'\n'
            "    elif short_kind == 'minItems':\n"
            '        text = \'{"ok": true, "value": ["structured"]}\'\n'
            "    else:\n"
            '        text = \'{"ok": true, "value": "structured"}\'\n'
            "else:\n"
            "    if 'round 2 findings' in prompt:\n"
            "        text = 'round 2 output'\n"
            "    elif structured:\n"
            '        text = \'{"ok": true, "value": "structured"}\'\n'
            "    else:\n"
            "        text = 'fake completion'\n"
            "print(json.dumps({'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}))\n"
            "print(json.dumps({'type':'completion','finalText':text}))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def run_delegate(
        self,
        args: list[str],
        *,
        env_extra: dict[str, str] | None = None,
        workspace_option: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DELEGATE_CONFIG"] = str(self.config_path)
        env["DELEGATE_WORKFLOW_NO_DAEMON"] = "1"
        env["HOME"] = str(self.home)
        env.update(env_extra or {})
        return subprocess.run(
            [
                sys.executable,
                str(CLI),
                *(["--cwd", str(self.workspace)] if workspace_option else []),
                *args,
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=40,
        )

    def write_workflow(self, body: str) -> Path:
        path = self.workspace / f"wf_{time.time_ns()}.py"
        path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
        return path

    def write_saved_workflow(self, name: str, body: str) -> str:
        root = self.home / ".delegate" / "workflows"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{name}.py"
        path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
        return name

    def wait_for_group_runs(self, wf_id: str, count: int = 1) -> list[dict[str, object]]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.run_delegate(["--json", "runs", "--group", wf_id])
            if result.returncode == 0:
                runs = json.loads(result.stdout)["runs"]
                if len(runs) >= count:
                    return runs
            time.sleep(0.1)
        self.fail(f"timed out waiting for {count} child runs in {wf_id}")

    # A detached supervisor had no way to ring anyone: it parks at a gate, dies,
    # or finishes with nobody watching, and --notify was rejected outright for
    # `workflow` while every plain launch accepted it.

    def test_notify_survives_the_supervisors_status_rebuild(self) -> None:
        post_started = self.workspace / "post-started"
        post_release = self.workspace / "post-release"
        post_call = self.home / "post-call.json"
        post = self.bin_dir / "post"
        post.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "started = Path(os.environ['FAKE_POST_STARTED'])\n"
            "release = Path(os.environ['FAKE_POST_RELEASE'])\n"
            "started.write_text('started\\n', encoding='utf-8')\n"
            "deadline = time.monotonic() + 5\n"
            "while not release.exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise SystemExit('notification barrier release timed out')\n"
            "    time.sleep(0.01)\n"
            "Path(os.environ['FAKE_POST_CALL']).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
            "print('message: pst_test_notification')\n",
            encoding="utf-8",
        )
        post.chmod(0o755)
        script = self.write_workflow(
            """
            meta = {"name": "notify-target"}
            return {"ok": True}
            """
        )
        result = self.run_delegate(
            ["--notify", "channel:somewhere", "workflow", "run", str(script)],
            env_extra={
                "PATH": str(self.bin_dir),
                "FAKE_POST_STARTED": str(post_started),
                "FAKE_POST_RELEASE": str(post_release),
                "FAKE_POST_CALL": str(post_call),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        wf_id = next(
            line.split(": ", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("wfId:")
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        self.addCleanup(proc_harness.reap_workflow_now, self.workspace, wf_id)
        deadline = time.monotonic() + 20
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
            if status.get("status") in {"succeeded", "failed"} and post_started.exists():
                break
            time.sleep(0.1)
        self.assertEqual(status.get("status"), "succeeded", status)
        # _write_status_locked rebuilds status.json from scratch rather than
        # merging it, so a key written only at create time is erased by the
        # supervisor's first write. That is exactly what happened to the first
        # version of this feature, and only a live run revealed it.
        self.assertEqual(status.get("notify"), "channel:somewhere")
        self.assertTrue(
            workflow_registry.supervisor_alive(root),
            "the notification barrier must keep the terminal supervisor alive",
        )
        self.assertFalse(post_call.exists(), "notification escaped its release barrier")

        post_release.write_text("release\n", encoding="utf-8")
        self.assertTrue(
            workflow_runtime.wait_for_workflow_lock(root, timeout_seconds=5),
            "notification writer did not finish and release the workflow lock",
        )
        self.assertFalse(
            workflow_registry.supervisor_alive(root),
            "workflow lock was released while its supervisor still appeared live",
        )
        self.assertTrue(post_call.exists(), "notifier exited without recording its call")
        self.assertEqual(
            json.loads(post_call.read_text(encoding="utf-8")),
            [
                "chat",
                "somewhere",
                "--send",
                "--anyway",
                "--body",
                f"delegate workflow {wf_id} succeeded",
            ],
        )

    def test_workflow_without_notify_records_none_and_sends_nothing(self) -> None:
        post_call = self.home / "unexpected-post-call"
        post = self.bin_dir / "post"
        post.write_text(
            f"#!{sys.executable}\n"
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['FAKE_POST_CALL']).write_text('called\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        post.chmod(0o755)
        script = self.write_workflow(
            """
            meta = {"name": "no-notify"}
            return {"ok": True}
            """
        )
        result = self.run_delegate(
            ["workflow", "run", str(script)],
            env_extra={"PATH": str(self.bin_dir), "FAKE_POST_CALL": str(post_call)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        wf_id = next(
            line.split(": ", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("wfId:")
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        self.addCleanup(proc_harness.reap_workflow_now, self.workspace, wf_id)
        deadline = time.monotonic() + 20
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
            if status.get("status") in {"succeeded", "failed"}:
                break
            time.sleep(0.1)
        self.assertEqual(status.get("status"), "succeeded", status)
        self.assertIsNone(status.get("notify"))
        self.assertTrue(
            workflow_runtime.wait_for_workflow_lock(root, timeout_seconds=5),
            "no-notify workflow supervisor did not exit",
        )
        self.assertFalse(post_call.exists(), "workflow without --notify invoked post")

    def test_a_valid_notify_target_is_accepted_for_workflow_run(self) -> None:
        """The defect was a VALID target being refused, so pin acceptance.

        The first version of this test passed a garbage target and asserted a
        nonzero exit. That was decoration: `parse_notify_target` runs in the
        global-option loop before the subcommand allow-list is consulted, so a
        garbage target already failed closed on the parent that refused
        `--notify` for `workflow` outright — the test would have been green
        through the entire defect and through a fix that changed nothing.
        """
        from delegate_agent import cli_parser

        parsed = cli_parser.parse_cli(
            ["--cwd", str(self.workspace), "--notify", "channel:x", "workflow", "run", "s.py"]
        )
        self.assertEqual(parsed.subcommand, "workflow")
        self.assertEqual(parsed.payload.notify, "channel:x")

        # And it is still refused where it genuinely does not apply.
        with self.assertRaises(Exception) as ctx:
            cli_parser.parse_cli(["--notify", "channel:x", "runs"])
        self.assertIn("notify", str(ctx.exception).lower())

    def test_a_failed_notification_never_changes_the_workflow_result(self) -> None:
        """Telemetry that can fail a workflow is worse than no telemetry."""
        script = self.write_workflow(
            """
            meta = {"name": "notify-degrades"}
            return {"ok": True}
            """
        )
        # `post` is absent from the sandboxed PATH, so every send degrades.
        result = self.run_delegate(
            ["--notify", "channel:nowhere", "workflow", "run", str(script)],
            env_extra={"PATH": str(self.bin_dir)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        wf_id = next(
            line.split(": ", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("wfId:")
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        self.addCleanup(proc_harness.reap_workflow_now, self.workspace, wf_id)
        deadline = time.monotonic() + 20
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
            if status.get("status") in {"succeeded", "failed"}:
                break
            time.sleep(0.1)
        self.assertEqual(status.get("status"), "succeeded", status)
        self.assertTrue(
            workflow_runtime.wait_for_workflow_lock(root, timeout_seconds=5),
            "failed-notification workflow supervisor did not exit",
        )

    def test_a_degraded_notification_is_recorded_and_does_not_un_finish_the_run(self) -> None:
        """Telemetry about a finished workflow must not reset it to running.

        `append_event` writes status="running" beside every journal line, which
        is correct for work events and catastrophic after a terminal write. The
        first version of the degradation record used it and reset succeeded
        workflows back to running; both notify tests caught it.
        """
        script = self.write_workflow(
            """
            meta = {"name": "notify-degraded-record"}
            return {"ok": True}
            """
        )
        result = self.run_delegate(
            ["--notify", "channel:nowhere", "workflow", "run", str(script)],
            env_extra={"PATH": str(self.bin_dir)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        wf_id = next(
            line.split(": ", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("wfId:")
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        self.addCleanup(proc_harness.reap_workflow_now, self.workspace, wf_id)
        deadline = time.monotonic() + 20
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
            if status.get("status") in {"succeeded", "failed"}:
                break
            time.sleep(0.1)
        self.assertEqual(status.get("status"), "succeeded", status)
        self.assertTrue(
            workflow_runtime.wait_for_workflow_lock(root, timeout_seconds=5),
            "degraded-notification workflow supervisor did not exit",
        )

        # A silent degradation is indistinguishable from a delivered
        # notification, which is the failure this whole feature exists to stop.
        journal = (root / workflow_registry.JOURNAL_FILE).read_text(encoding="utf-8")
        degraded = [
            json.loads(line)
            for line in journal.splitlines()
            if line.strip() and json.loads(line).get("type") == "notify_degraded"
        ]
        self.assertTrue(degraded, f"an undelivered notification must be recorded: {journal}")
        self.assertTrue(degraded[0].get("reason"), degraded[0])

    def test_check_accepts_top_level_return_and_warns_on_determinism(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "check"}
            import time
            log(time.time())
            return {"ok": True}
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(script)])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["meta"]["name"], "check")
        self.assertTrue(any("determinism warning" in item for item in payload["warnings"]))

    def test_dry_run_stubs_agents_and_schema_placeholders(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False}
            value = agent("structured", schema=SCHEMA)
            return value
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["result"], {"ok": False})
        self.assertEqual(payload["runTree"]["counts"], {"codex:safe": 1})
        self.assertTrue(any("agent calls" in warning for warning in payload["warnings"]))
        self.assertTrue(
            any("filesystem writes are live" in warning for warning in payload["warnings"])
        )

    def test_dry_run_text_warns_that_script_writes_are_live(self) -> None:
        script = self.write_workflow(
            'meta = {"name": "dry-warning"}\nimport random\nreturn random.random()'
        )
        result = self.run_delegate(["workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("warning:", result.stderr)
        self.assertIn("determinism warning", result.stderr)
        self.assertIn("script filesystem writes are live", result.stderr)
        self.assertIn("scriptPath:", result.stderr)
        self.assertIn("sourceScript:", result.stderr)
        self.assertIn("scriptSha256:", result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"], {})

    def test_dry_run_exposes_dry_run_flag_to_the_script(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-flag"}
            return {"sawDryRun": dry_run}
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["result"], {"sawDryRun": True})

    def test_dry_run_abandons_a_script_that_waits_forever(self) -> None:
        # Reproduces dlg-8oc: a generated plan whose human gate polls for a
        # decision no dry run can supply. Before the deadline this hung until
        # the process was killed by hand.
        script = self.write_workflow(
            """
            import threading

            meta = {"name": "dry-hang"}

            def block(prev, item=None, index=None):
                threading.Event().wait(600)
                return prev

            return pipeline([1, 2, 3], block)
            """
        )
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config.setdefault("workflows", {})["dryRunTimeoutSeconds"] = 2
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout or result.stderr)
        self.assertEqual(payload["error"], "dry_run_timeout")

    def test_dry_run_schema_placeholders_honor_recursive_minimums(self) -> None:
        schema = {
            "type": "object",
            "required": ["name", "groups"],
            "properties": {
                "name": {"minLength": 3},
                "groups": {
                    "minItems": 2,
                    "items": {
                        "type": "object",
                        "required": ["labels"],
                        "properties": {
                            "labels": {
                                "type": "array",
                                "minItems": 2,
                                "items": {"type": "string", "minLength": 2},
                            }
                        },
                    },
                },
            },
        }
        script = self.write_workflow(
            f'meta = {{"name": "dry-minimums"}}\nreturn agent("structured", schema={schema!r})'
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)["result"]
        workflow_schema.validate_value(value, schema)
        self.assertEqual(value["name"], "xxx")
        self.assertEqual(len(value["groups"]), 2)
        self.assertEqual(value["groups"][0]["labels"], ["xx", "xx"])

    def test_dry_run_reports_explicit_agent_routing_and_utf8_prompt_bytes(self) -> None:
        prompt = "Terra says: é🌍"
        script = self.write_workflow(
            f"""
            meta = {{"name": "dry-routing"}}
            return agent(
                {prompt!r},
                engine="codex",
                mode="work",
                model="terra",
                effort="high",
                fast=True,
                isolation="worktree",
            )
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)["runTree"]["calls"][0]
        self.assertEqual(
            {key: call[key] for key in ("model", "effort", "fast", "isolation")},
            {
                "model": "terra",
                "effort": "high",
                "fast": True,
                "isolation": "worktree",
            },
        )
        self.assertEqual(call["promptBytes"], len(prompt.encode("utf-8")))
        self.assertNotEqual(call["promptBytes"], len(prompt))

    def test_dry_run_reports_inherited_and_empty_routing_defaults(self) -> None:
        inherited = self.write_workflow(
            """
            meta = {"name": "dry-defaults", "defaults": {"model": "luna", "effort": "medium", "fast": False, "isolation": "none"}}
            return agent("inherited")
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(inherited), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)["runTree"]["calls"][0]
        self.assertEqual(call["engine"], ["codex"])
        self.assertEqual(call["mode"], "safe")
        self.assertEqual(
            {key: call[key] for key in ("model", "effort", "fast", "isolation")},
            {"model": "luna", "effort": "medium", "fast": False, "isolation": "none"},
        )

        empty = self.write_workflow('meta = {"name": "dry-empty"}\nreturn agent("plain")')
        result = self.run_delegate(["--json", "workflow", "run", str(empty), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)["runTree"]["calls"][0]
        self.assertEqual(
            {key: call[key] for key in ("model", "effort", "fast", "isolation")},
            {"model": None, "effort": None, "fast": None, "isolation": None},
        )

    def test_dry_run_reports_judges_effort_and_overrides(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-judges-effort"}
            SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            return judges(
                "grade this",
                SCHEMA,
                engines=[
                    "codex",
                    {"engine": "codex", "effort": "low", "model": "o3-mini"},
                ],
                effort="high",
            )
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stdout)["runTree"]["calls"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["effort"], "high")
        self.assertIsNone(calls[0]["model"])
        self.assertEqual(calls[1]["effort"], "low")
        self.assertEqual(calls[1]["model"], "o3-mini")

    def test_dry_run_warns_before_argv_transport_prompt_limit(self) -> None:
        from delegate_agent.workflows.runtime import PROMPT_ARGV_GUARD_BYTES

        prompt = "x" * (PROMPT_ARGV_GUARD_BYTES + 1)
        script = self.write_workflow(
            f"""
            meta = {{"name": "dry-argv-limit", "defaults": {{"engine": "kimi"}}}}
            return agent({prompt!r})
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        call = payload["runTree"]["calls"][0]
        self.assertEqual(call["promptBytes"], PROMPT_ARGV_GUARD_BYTES + 1)
        self.assertIn(f"{PROMPT_ARGV_GUARD_BYTES}-byte", call["warnings"][0])
        self.assertIn("kimi", call["warnings"][0])
        runs = self.run_delegate(["--json", "runs", "--group", payload["wfId"]])
        self.assertEqual(runs.returncode, 0, runs.stderr)
        self.assertEqual(json.loads(runs.stdout)["runs"], [])

    def test_agent_fast_parameter_preserves_legacy_positional_schema(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "positional", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            return agent("structured", "codex", "safe", None, None, SCHEMA)
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": False})

    def test_dry_run_stubs_nested_workflow_without_executing_body(self) -> None:
        child = self.write_saved_workflow(
            "dry-child",
            """
            meta = {"name": "child"}
            raise RuntimeError("child body should be stubbed")
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent"}}
            return workflow({child!r}, gate=True)
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(parent), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["result"])
        self.assertEqual(payload["runTree"]["calls"][0]["mode"], "workflow")

    def test_check_rejects_literal_invalid_engine_and_judges_schema(self) -> None:
        invalid_engine = self.write_workflow(
            """
            meta = {"name": "bad"}
            return agent("x", engine="not-an-engine")
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(invalid_engine)])
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid_workflow_script")
        self.assertIn("agent engine must be a real delegate engine", payload["message"])

        invalid_schema = self.write_workflow(
            """
            meta = {"name": "bad-schema"}
            return judges("x", {"type": "object", "patternProperties": {}})
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(invalid_schema)])
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("invalid schema literal", payload["message"])

    def test_check_rejects_invalid_judge_effort(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "bad-effort"}
            SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            return judges(
                "grade this",
                SCHEMA,
                engines=[{"engine": "codex", "effort": "turbo"}],
            )
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(script)])
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid_workflow_script")
        self.assertIn("effort", payload["message"])
        self.assertIn("low", payload["message"])

    def test_check_accepts_auto_effort_only_for_omp(self) -> None:
        """`auto` is omp's alone; pi and claude fail in the child, not at parse."""
        accepted = self.write_workflow(
            """
            meta = {"name": "omp-auto"}
            return agent("do it", engine="omp", effort="auto")
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(accepted)])
        self.assertEqual(result.returncode, 0, msg=result.stdout)

        for engine in ("pi", "claude"):
            with self.subTest(engine=engine):
                rejected = self.write_workflow(
                    f"""
                    meta = {{"name": "{engine}-auto"}}
                    return agent("do it", engine="{engine}", effort="auto")
                    """
                )
                result = self.run_delegate(["--json", "workflow", "check", str(rejected)])
                self.assertNotEqual(result.returncode, 0, msg=result.stdout)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"], "invalid_workflow_script")
                self.assertIn("effort", payload["message"])

    def test_check_keeps_each_engines_own_effort_vocabulary(self) -> None:
        """The planted negative: an effort each engine really accepts must parse."""
        for engine, effort in (("pi", "off"), ("claude", "max"), ("omp", "minimal")):
            with self.subTest(engine=engine, effort=effort):
                script = self.write_workflow(
                    f"""
                    meta = {{"name": "{engine}-{effort}"}}
                    return agent("do it", engine="{engine}", effort="{effort}")
                    """
                )
                result = self.run_delegate(["--json", "workflow", "check", str(script)])
                self.assertEqual(result.returncode, 0, msg=result.stdout)

        # grok has no `off`; claude has no `minimal`.
        for engine, effort in (("grok", "off"), ("claude", "minimal")):
            with self.subTest(engine=engine, effort=effort):
                script = self.write_workflow(
                    f"""
                    meta = {{"name": "{engine}-{effort}"}}
                    return agent("do it", engine="{engine}", effort="{effort}")
                    """
                )
                result = self.run_delegate(["--json", "workflow", "check", str(script)])
                self.assertNotEqual(result.returncode, 0, msg=result.stdout)

    def test_script_size_limit_admits_planc_scale_scripts(self) -> None:
        # planc-compiled workflows embed their engine and measure ~530 KiB at
        # near-limit plan scale; the cap must admit them and still refuse
        # unbounded input.
        from delegate_agent.workflows import script as workflow_script

        self.assertEqual(workflow_script.SCRIPT_SIZE_LIMIT, 1024 * 1024)
        padding = "# " + "x" * 76 + "\n"
        body = 'meta = {"name": "big"}\n' + padding * 7000
        big = self.workspace / "big_wf.py"
        big.write_text(body, encoding="utf-8")
        self.assertGreater(big.stat().st_size, 512 * 1024)
        self.assertEqual(workflow_script.read_script(big), body)

        over = self.workspace / "over_wf.py"
        over.write_bytes(body.encode() + b"#" * (1024 * 1024))
        with self.assertRaises(workflow_script.WorkflowScriptError) as ctx:
            workflow_script.read_script(over)
        self.assertIn("1 MiB", str(ctx.exception))

    def test_row_children_drop_lock_fd_env_but_keep_pin(self) -> None:
        # wp-ptw: rows spawned under a live supervisor inherit env with
        # close_fds, so the lock-fd number is dead; the pin must survive so
        # nested delegate invocations stay on the pinned runtime.
        import sys
        from unittest import mock

        from delegate_agent.workflows import runtime as workflow_runtime

        seeded = {
            workflow_runtime.WORKFLOW_LOCK_FD_ENV: "7",
            "DELEGATE_WORKFLOW_PIN": "/tmp/pin",
            "DELEGATE_TEST_UNRELATED": "kept",
        }
        probe = (
            "import json, os; print(json.dumps({k: os.environ.get(k) for k in "
            f"{sorted(seeded)!r}}}))"
        )
        with mock.patch.dict(os.environ, seeded):
            completed = workflow_runtime._run_child_command(
                [sys.executable, "-c", probe], cwd=str(self.workspace), timeout=30
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        child = json.loads(completed.stdout)
        self.assertIsNone(child[workflow_runtime.WORKFLOW_LOCK_FD_ENV])
        self.assertEqual(child["DELEGATE_WORKFLOW_PIN"], "/tmp/pin")
        self.assertEqual(child["DELEGATE_TEST_UNRELATED"], "kept")

    def test_run_journal_result_group_and_resume_cache(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "real", "defaults": {"engine": "codex", "mode": "safe"}}
            first = agent("one", label="first")
            second = pipeline(["two"], lambda prev, item, index: agent(item, label="pipe"))[0]
            return {"values": [first, second]}
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(
            json.loads(result.stdout)["result"], {"values": ["fake completion", "fake completion"]}
        )
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs.stdout)["runs"]), 2)

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        runs_after = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs_after.stdout)["runs"]), 2)
        events = self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"])
        event_types = {event["type"] for event in json.loads(events.stdout)["events"]}
        self.assertIn("agent_cache_hit", event_types)

    def test_resume_reports_source_drift_and_runs_frozen_copy(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "source-provenance"}
            return "frozen result"
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        launch_payload = json.loads(launch.stdout)
        wf_id = launch_payload["wfId"]
        self.assertEqual(launch_payload["sourceScript"], str(script.resolve()))
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        self.assertEqual(status["sourceScript"], str(script.resolve()))
        self.assertEqual(status["scriptSha256"], launch_payload["scriptSha256"])
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )

        script.write_text(
            'meta = {"name": "source-provenance"}\nreturn "edited source"\n', encoding="utf-8"
        )
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resumed_payload = json.loads(resumed.stdout)
        self.assertEqual(resumed_payload["scriptPath"], str(root / workflow_registry.SCRIPT_FILE))
        self.assertEqual(resumed_payload["scriptSha256"], launch_payload["scriptSha256"])
        self.assertTrue(
            any("source script differs" in warning for warning in resumed_payload["warnings"])
        )
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "frozen result")

    def test_resume_warns_when_frozen_copy_differs_from_recorded_hash(self) -> None:
        script = self.write_workflow(
            'meta = {"name": "frozen-provenance"}\nreturn "original frozen"'
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        launch_payload = json.loads(launch.stdout)
        wf_id = launch_payload["wfId"]
        recorded_hash = launch_payload["scriptSha256"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )

        frozen_path = Path(launch_payload["scriptPath"])
        frozen_path.write_text(
            'meta = {"name": "frozen-provenance"}\nreturn "edited frozen"\n',
            encoding="utf-8",
        )
        current_hash = workflow_registry.script_sha256(frozen_path.read_bytes())
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resumed_payload = json.loads(resumed.stdout)
        self.assertEqual(resumed_payload["scriptSha256"], recorded_hash)
        self.assertTrue(
            any(
                "frozen script differs from recorded hash" in warning
                and current_hash in warning
                and recorded_hash in warning
                for warning in resumed_payload["warnings"]
            )
        )
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "edited frozen")

    def test_resume_warns_when_recorded_source_is_unreadable(self) -> None:
        script = self.write_workflow('meta = {"name": "source-missing"}\nreturn "frozen"')
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        script.unlink()
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(
            any(
                "unable to compare" in warning for warning in json.loads(resumed.stdout)["warnings"]
            )
        )
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )

    def test_new_workflow_pins_supervisor_and_child_argv(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "pin-argv", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("pinned")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        pin = workflow_pinning.load_pin(wf_id, home=self.home)
        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertEqual(pin.cli_argv[0], sys.executable)
        self.assertTrue(pin.entrypoint.is_file())
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        runs = self.wait_for_group_runs(wf_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["group"], wf_id)
        self.assertIn(str(pin.import_root), pin.environment["PYTHONPATH"])

    def test_workflow_child_uses_persona_bytes_from_launch_pin(self) -> None:
        persona = self.home / ".delegate" / "personas" / "reviewer.md"
        persona.write_text("PINNED PERSONA\n", encoding="utf-8")
        prompt_log = self.workspace / "prompt.log"
        script = self.write_workflow(
            """
            meta = {"name": "pin-persona", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("check", persona="reviewer")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_PROMPT_LOG": str(prompt_log)},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        persona.write_text("LIVE PERSONA\n", encoding="utf-8")
        waited = self.run_delegate(
            ["--json", "workflow", "wait", wf_id, "--timeout", "10"],
            env_extra={"FAKE_PROMPT_LOG": str(prompt_log)},
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("PINNED PERSONA", prompt_log.read_text(encoding="utf-8"))
        self.assertNotIn("LIVE PERSONA", prompt_log.read_text(encoding="utf-8"))

    def test_agent_child_events_bind_run_id_to_key_and_label(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "event-identity", "defaults": {"engine": "codex", "mode": "safe"}}
            return parallel([
                lambda: agent("one", label="alpha"),
                lambda: agent("two", label="beta"),
            ])
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)

        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        children = [event for event in events if event["type"] == "agent_child"]
        self.assertEqual(len(children), 2)
        self.assertEqual({event["label"] for event in children}, {"alpha", "beta"})
        self.assertEqual(len({event["runId"] for event in children}), 2)
        self.assertEqual(len({event["key"] for event in children}), 2)
        for event in children:
            self.assertEqual(event["workflowAgentKey"], event["key"])
            self.assertEqual(event["engine"], "codex")
            snapshot = self.run_delegate(["--json", "snapshot", event["runId"]])
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            payload = json.loads(snapshot.stdout)
            self.assertEqual(payload["runId"], event["runId"])
            self.assertEqual(payload["group"], wf_id)

    def test_failed_registered_child_emits_agent_child_identity(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "event-failed", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("fail", label="bad-lane")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_REGISTERED_FAILURE": "1"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)

        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 1)
        run_id = runs[0]["runId"]
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        children = [event for event in events if event["type"] == "agent_child"]
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child["runId"], run_id)
        self.assertEqual(child["label"], "bad-lane")
        self.assertEqual(child["workflowAgentKey"], child["key"])
        snapshot = self.run_delegate(["--json", "snapshot", run_id])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertEqual(json.loads(snapshot.stdout)["runId"], run_id)

    def test_resume_backfills_missing_agent_child_identity_for_adopted_run(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "event-adopt", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one", label="adopt-me")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        run_id = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"][
            0
        ]["runId"]
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        key = next(event["key"] for event in events if event["type"] == "agent_started")
        journal.write_text(
            "\n".join(
                json.dumps(event)
                for event in events
                if event["type"] not in {"agent_child", "agent_finished", "workflow_finished"}
            )
            + "\n",
            encoding="utf-8",
        )

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        after = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        children = [event for event in after if event["type"] == "agent_child"]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["runId"], run_id)
        self.assertEqual(children[0]["key"], key)
        self.assertEqual(children[0]["workflowAgentKey"], key)
        self.assertEqual(children[0]["engine"], "codex")
        self.assertEqual(children[0]["label"], "adopt-me")
        self.assertEqual(
            json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)["result"],
            "fake completion",
        )

    def test_resume_does_not_duplicate_existing_agent_child_identity(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "event-adopt-dedup", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one", label="keep-me")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        original_child = next(event for event in events if event["type"] == "agent_child")
        journal.write_text(
            "\n".join(
                json.dumps(event)
                for event in events
                if event["type"] not in {"agent_finished", "workflow_finished"}
            )
            + "\n",
            encoding="utf-8",
        )

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        after = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        children = [event for event in after if event["type"] == "agent_child"]
        self.assertEqual(children, [original_child])

    def test_resume_preserves_spent_budget_for_new_cache_misses(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "budget-resume", "defaults": {"engine": "codex", "mode": "safe"}}
            return pipeline(["one"], lambda prev, item, index: agent(item))
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script), "--budget", "1"])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        launch_payload = json.loads(launch.stdout)
        wf_id = launch_payload["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        Path(launch_payload["scriptPath"]).write_text(
            textwrap.dedent(
                """
                meta = {"name": "budget-resume", "defaults": {"engine": "codex", "mode": "safe"}}
                return pipeline(["one", "two"], lambda prev, item, index: agent(item))
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], ["fake completion", None])
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs.stdout)["runs"]), 1)

    def test_resume_budget_override_allows_new_cache_misses(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "budget-override", "defaults": {"engine": "codex", "mode": "safe"}}
            return pipeline(["one"], lambda prev, item, index: agent(item))
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script), "--budget", "1"])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        launch_payload = json.loads(launch.stdout)
        wf_id = launch_payload["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        Path(launch_payload["scriptPath"]).write_text(
            textwrap.dedent(
                """
                meta = {"name": "budget-override", "defaults": {"engine": "codex", "mode": "safe"}}
                return pipeline(["one", "two"], lambda prev, item, index: agent(item))
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        resumed = self.run_delegate(
            ["--json", "workflow", "run", "--resume", wf_id, "--budget", "2"]
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(
            json.loads(result.stdout)["result"], ["fake completion", "fake completion"]
        )
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs.stdout)["runs"]), 2)

    def test_terminal_none_agent_result_replays_without_respawn(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "none-cache", "defaults": {"engine": "codex", "mode": "safe"}}
            schema = {"type": "object", "required": ["missing"], "properties": {"missing": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=schema, retries=0)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertIsNone(json.loads(result.stdout)["result"])
        self.assertEqual(
            len(json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]),
            1,
        )

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(
            len(json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]),
            1,
        )
        events = self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"])
        self.assertIn(
            "agent_cache_hit", {event["type"] for event in json.loads(events.stdout)["events"]}
        )

    def test_schema_allowed_null_agent_is_success_and_replays(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "null-success", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("structured null", schema={"type": "null"}, retries=0)
            """
        )
        env = {"FAKE_CODEX_NULL": "1"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        events = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE).read_text().splitlines()
        ]
        finished = [event for event in events if event.get("type") == "agent_finished"]
        self.assertEqual(len(finished), 1)
        self.assertIsNone(finished[0]["result"])
        self.assertNotIn("exhausted", finished[0])
        self.assertNotIn("agent_structured_exhausted", {event["type"] for event in events})
        self.assertIsNone(
            json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)["result"]
        )

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id], env_extra=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        events_after = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE).read_text().splitlines()
        ]
        self.assertIn("agent_cache_hit", {event["type"] for event in events_after})
        self.assertEqual(
            len(json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]),
            1,
        )

    def test_schema_allowed_null_stops_engine_chain(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "null-chain", "defaults": {"mode": "safe"}}
            return agent("structured null", engine=["codex", "droid"], schema={"type": "null"}, retries=0)
            """
        )
        env = {"FAKE_CODEX_NULL": "1"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["harness"], "codex")

    def test_schema_allowed_null_followup_is_success_and_cached(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "null-followup", "defaults": {"engine": "codex", "mode": "work"}}
            agent("prior", label="prior", resumable=True)
            return followup("prior", "structured null", schema={"type": "null"}, retries=0)
            """
        )
        env = {"FAKE_CODEX_NULL": "1", "FAKE_CODEX_THREAD_ID": "th_null_followup"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr or launch.stdout)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        events = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE).read_text().splitlines()
        ]
        followup_finished = [
            event
            for event in events
            if event.get("type") == "agent_finished" and event.get("scope", "").endswith("seq#1")
        ]
        self.assertTrue(followup_finished)
        self.assertIsNone(followup_finished[-1]["result"])
        self.assertNotIn("exhausted", followup_finished[-1], events)
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id], env_extra=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        events_after = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE).read_text().splitlines()
        ]
        self.assertGreaterEqual(
            sum(event.get("type") == "agent_cache_hit" for event in events_after), 2
        )

    def test_resume_preserves_labeled_schema_null_prior_for_followup(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "null-prior-followup", "defaults": {"engine": "codex", "mode": "work"}}
            agent("prior null", label="prior", schema={"type": "null"}, resumable=True, retries=0)
            return followup("prior", "followup null", schema={"type": "null"}, retries=0)
            """
        )
        env = {"FAKE_CODEX_NULL": "1", "FAKE_CODEX_THREAD_ID": "th_null_prior"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr or launch.stdout)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        initial_events = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE).read_text().splitlines()
        ]
        initial_started = [
            event for event in initial_events if event.get("type") == "agent_started"
        ]
        self.assertEqual(len(initial_started), 2)
        self.assertIsNone(
            json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)["result"]
        )

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id], env_extra=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr or resumed.stdout)
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        events_after = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE).read_text().splitlines()
        ]
        self.assertEqual(
            len([event for event in events_after if event.get("type") == "agent_started"]),
            2,
        )
        self.assertGreaterEqual(
            sum(event.get("type") == "agent_cache_hit" for event in events_after), 2
        )

    def test_resume_adopts_schema_allowed_null_without_exhaustion(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "null-adopt", "defaults": {"engine": "codex", "mode": "work"}}
            return agent("structured null", schema={"type": "null"}, resumable=True)
            """
        )
        env = {"FAKE_CODEX_NULL": "1"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr or launch.stdout)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / workflow_registry.JOURNAL_FILE
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        journal.write_text(
            "".join(
                json.dumps(event, sort_keys=True) + "\n"
                for event in events
                if event["type"] not in {"agent_finished", "workflow_finished"}
            ),
            encoding="utf-8",
        )
        (root / workflow_registry.RESULT_FILE).unlink()
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id], env_extra=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        adopted = [
            json.loads(line)
            for line in journal.read_text().splitlines()
            if json.loads(line).get("type") == "agent_adopted"
        ]
        self.assertTrue(adopted)
        self.assertIsNone(adopted[-1]["result"])
        self.assertIsNone(
            json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)["result"]
        )
        self.assertEqual(
            len(json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]),
            1,
        )

    def test_resume_adopts_authoritative_null_for_exhausted_cached_key(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "null-exhausted-adopt", "defaults": {"engine": "codex", "mode": "work"}}
            agent("prior null", label="prior", schema={"type": "null"}, resumable=True, retries=0)
            return followup("prior", "followup null", schema={"type": "null"}, retries=0)
            """
        )
        env = {"FAKE_CODEX_NULL": "1", "FAKE_CODEX_THREAD_ID": "th_null_exhausted"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr or launch.stdout)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / workflow_registry.JOURNAL_FILE
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        prior_key = next(
            event["key"]
            for event in events
            if event.get("type") == "agent_finished" and event.get("scope", "").endswith("seq#0")
        )
        for event in events:
            if event.get("type") == "agent_finished" and event.get("key") == prior_key:
                event["exhausted"] = True
        journal.write_text(
            "".join(
                json.dumps(event, sort_keys=True) + "\n"
                for event in events
                if event.get("type") != "workflow_finished"
            ),
            encoding="utf-8",
        )
        (root / workflow_registry.RESULT_FILE).unlink()

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id], env_extra=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr or resumed.stdout)
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        final_events = [json.loads(line) for line in journal.read_text().splitlines()]
        adopted = [event for event in final_events if event.get("type") == "agent_adopted"]
        self.assertTrue(adopted)
        self.assertIsNone(adopted[-1]["result"])
        prior_settlements = [
            event
            for event in final_events
            if event.get("type") == "agent_finished" and event.get("key") == prior_key
        ]
        self.assertNotIn("exhausted", prior_settlements[-1])
        self.assertEqual(
            len(json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]),
            2,
        )

    def test_nested_workflow_events_keep_unique_monotonic_sequences(self) -> None:
        child = self.write_saved_workflow(
            "event-child",
            """
            meta = {"name": "child", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("child")
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent", "defaults": {{"engine": "codex", "mode": "safe"}}}}
            first = workflow({child!r})
            second = agent("parent")
            return [first, second]
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        events = self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"])
        seqs = [event["seq"] for event in json.loads(events.stdout)["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_parallel_nested_resume_keeps_child_replay_keys_byte_identical(self) -> None:
        child = self.write_saved_workflow(
            "parallel-replay-child",
            """
            meta = {"name": "child", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent(args["prompt"])
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent"}}
            return parallel([
                lambda: workflow({child!r}, args={{"prompt": "left"}}),
                lambda: workflow({child!r}, args={{"prompt": "right"}}),
            ])
            """
        )
        prompt_log = self.home / "parallel-prompts.log"
        env = {"FAKE_PROMPT_LOG": str(prompt_log)}
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / workflow_registry.JOURNAL_FILE
        first_events = workflow_registry.iter_journal(journal)
        first_starts = [
            event
            for event in first_events
            if event.get("type") == "agent_started" and event.get("simulated") is not True
        ]
        first_keys = {event["key"] for event in first_starts}
        self.assertEqual(len(first_keys), 2)
        self.assertEqual(
            {event["scope"] for event in first_starts},
            {
                "root/parallel@0/thunk#0/wf:parallel-replay-child@0/seq#0",
                "root/parallel@0/thunk#1/wf:parallel-replay-child@0/seq#0",
            },
        )
        (root / workflow_registry.RESULT_FILE).unlink()

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id], env_extra=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        final_events = workflow_registry.iter_journal(journal)
        replay_keys = {
            event["key"] for event in final_events if event.get("type") == "agent_cache_hit"
        }
        self.assertEqual(replay_keys, first_keys)
        self.assertEqual(
            len([event for event in final_events if event.get("type") == "agent_child"]),
            2,
        )
        self.assertEqual(prompt_log.read_text(encoding="utf-8").count("\n---\n"), 2)

    def test_nested_pipeline_does_not_deadlock_with_item_thread_cap_one(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["workflows"]["itemThreads"] = 1
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        child = self.write_saved_workflow(
            "pipeline-child",
            """
            meta = {"name": "child", "defaults": {"engine": "codex", "mode": "safe"}}
            return pipeline(["child"], lambda prev, item, index: agent(item))
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent"}}
            return pipeline(["outer"], lambda prev, item, index: workflow({child!r}))
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], [["fake completion"]])

    def test_soft_park_releases_slot_and_replays_named_scope(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["workflows"]["itemThreads"] = 1
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        script = self.write_workflow(
            """
            meta = {"name": "soft-park", "defaults": {"engine": "codex", "mode": "safe"}}

            def parked_item():
                if not parked("parked"):
                    park_item("parked", {"reason": "operator"})
                return agent("parked")

            def unrelated_item():
                return agent("unrelated")

            return soft_park({"parked": parked_item, "unrelated": unrelated_item})
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        parked_event = next(event for event in events if event["type"] == "item_parked")
        unrelated_finished = next(
            event
            for event in events
            if event["type"] == "agent_finished"
            and event.get("scope", "").startswith("root/soft-park/unrelated/")
        )
        self.assertEqual(parked_event["name"], "parked")
        self.assertEqual(parked_event["scope"], "root/soft-park/parked")
        self.assertLess(parked_event["seq"], unrelated_finished["seq"])
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "paused")
        self.assertEqual(status["parkedItems"], ["parked"])

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(
            json.loads(result.stdout)["result"], ["fake completion", "fake completion"]
        )
        after = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        parked_scopes = [
            event["scope"]
            for event in after
            if event["type"] == "agent_started"
            and event.get("scope", "").startswith("root/soft-park/parked/")
        ]
        self.assertEqual(parked_scopes, ["root/soft-park/parked/seq#0"])
        self.assertIn("item_unparked", {event["type"] for event in after})

    def test_soft_park_rejects_duplicate_stable_names(self) -> None:
        wf_id = "wf_444444444444"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {"wfId": wf_id, "status": "created", "budget": {"total": None, "spent": 0}},
        )
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        dsl = workflow_runtime.WorkflowDsl(state, {})
        with self.assertRaisesRegex(ValueError, "unique"):
            dsl.soft_park([("same", lambda: None), ("same", lambda: None)])
        with self.assertRaisesRegex(ValueError, "must not contain"):
            dsl.soft_park({"nested/name": lambda: None})
        too_many = [(f"item-{index}", lambda: None) for index in range(4097)]
        with self.assertRaisesRegex(ValueError, "item limit"):
            dsl.soft_park(too_many)

    def test_nested_pipeline_soft_park_pauses_and_resumes(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "nested-soft-park", "defaults": {"engine": "codex", "mode": "safe"}}

            def stage(previous, item, index):
                def parked_child():
                    if not parked("nested"):
                        park_item("nested", {"reason": "operator"})
                    return agent("nested")
                return soft_park({"nested": parked_child})

            return pipeline(["outer"], stage)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "paused")
        self.assertEqual(status["parkedItems"], ["nested"])

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertEqual(result["result"], [["fake completion"]])

    def test_pipeline_scope_strings_remain_stable_for_current_replay(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "pipeline-scope", "defaults": {"engine": "codex", "mode": "safe"}}
            def first(previous, item, index):
                return agent(f"first-{item}")
            def second(previous, item, index):
                return agent(f"second-{item}")
            return pipeline(["a", "b"], first, second)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        scopes = sorted(event["scope"] for event in events if event["type"] == "agent_started")
        self.assertEqual(
            scopes,
            [
                "root/pipeline@0/item#0/stage#0/seq#0",
                "root/pipeline@0/item#0/stage#1/seq#0",
                "root/pipeline@0/item#1/stage#0/seq#0",
                "root/pipeline@0/item#1/stage#1/seq#0",
            ],
        )

    def test_nested_gate_waits_for_sibling_parent_agents(self) -> None:
        grandchild = self.write_saved_workflow(
            "gate-grandchild",
            """
            meta = {"name": "grandchild"}
            import time
            time.sleep(0.2)
            return {"ok": False}
            """,
        )
        child = self.write_saved_workflow(
            "gate-child",
            f"""
            meta = {{"name": "child"}}
            return workflow({grandchild!r}, gate="on-failure")
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent", "defaults": {{"engine": "codex", "mode": "safe"}}}}
            return parallel([lambda: agent("slow"), lambda: workflow({child!r})])
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        gate_seq = next(event["seq"] for event in events if event["type"] == "gate")
        slow_finish_seq = next(
            event["seq"]
            for event in events
            if event["type"] == "agent_finished" and event.get("result") == "fake completion"
        )
        # The journal gate is authoritative and is fsynced before admission
        # closes/drain begins; the sibling may therefore finish after the gate
        # event while the projection is still being parked.
        self.assertLess(gate_seq, slow_finish_seq)
        status = self.run_delegate(["--json", "workflow", "status", wf_id])
        self.assertEqual(json.loads(status.stdout)["status"], "paused")
        self.assertFalse(
            (self.workspace / ".delegate" / "workflows" / wf_id / "result.json").exists()
        )
        self.assertTrue(
            any(
                event["type"] == "agent_finished" and event.get("result") == "fake completion"
                for event in events
            )
        )

    def test_park_gate_fsyncs_journal_before_drain_and_is_idempotent(self) -> None:
        wf_id = "wf_111111111111"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {
                "wfId": wf_id,
                "status": "created",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        observed: list[tuple[str, str]] = []
        ordering: list[str] = []
        real_fsync = workflow_registry.os.fsync

        def observe_fsync(fd: int) -> None:
            ordering.append("fsync")
            real_fsync(fd)

        def drain() -> None:
            ordering.append("drain")
            events = workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
            observed.append(
                (
                    events[-1].get("type", "") if events else "",
                    (workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}).get(
                        "status", ""
                    ),
                )
            )

        with (
            mock.patch.object(workflow_registry.os, "fsync", side_effect=observe_fsync),
            mock.patch.object(state, "close_gate_and_wait", side_effect=drain),
        ):
            first = state.park_gate("gate-fixture", child="child", result={"ok": False})
            second = state.park_gate("gate-fixture", child="child", result={"ok": False})

        self.assertEqual(first.gate_key, "gate-fixture")
        self.assertEqual(second.gate_key, "gate-fixture")
        self.assertGreater(ordering.index("drain"), ordering.index("fsync"))
        self.assertEqual(observed, [("gate", "created"), ("gate", "paused")])
        events = workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
        self.assertEqual([event["type"] for event in events], ["gate"])
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        self.assertEqual(status.get("status"), "paused")
        self.assertEqual(status.get("gateKey"), "gate-fixture")

    def test_metadata_less_gate_exit_fails_supervisor(self) -> None:
        wf_id = "wf_222222222222"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        (root / workflow_registry.SCRIPT_FILE).write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {
                "wfId": wf_id,
                "status": "created",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        with mock.patch.object(
            workflow_runtime,
            "execute_workflow",
            side_effect=workflow_runtime.GateExit("missing metadata"),
        ):
            rc = workflow_runtime.run_supervisor(
                workspace=self.workspace,
                wf_id=wf_id,
                cli_argv=[str(CLI)],
                config={},
            )
        self.assertEqual(rc, 1)
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        self.assertEqual(status.get("status"), "failed")
        events = workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
        self.assertTrue(any(event.get("type") == "workflow_failed" for event in events), events)

    def test_approve_recovers_missing_gate_key_from_journal_fixture(self) -> None:
        child = self.write_saved_workflow(
            "wf-b43032f7fee5-child",
            """
            meta = {"name": "fixture-child"}
            return {"ok": False}
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "fixture-parent"}}
            return workflow({child!r}, gate="on-failure")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        self.assertEqual(status.get("status"), "paused")
        gate_events = [
            event
            for event in workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
            if event.get("type") == "gate"
        ]
        self.assertTrue(gate_events)
        gate_key = gate_events[-1]["key"]
        status.update({"status": "running"})
        status.pop("gateKey", None)
        status.pop("gateResult", None)
        workflow_registry.write_json(root / workflow_registry.STATUS_FILE, status)

        approved = self.run_delegate(["--json", "workflow", "approve", wf_id])
        self.assertEqual(approved.returncode, 0, approved.stderr)
        final = self.run_delegate(["--json", "workflow", "status", wf_id])
        self.assertEqual(json.loads(final.stdout).get("status"), "succeeded")
        approval = workflow_registry.read_json(root / workflow_registry.APPROVAL_FILE) or {}
        self.assertIn(
            {"key": gate_key, "resultHash": gate_events[-1]["gateResultHash"]},
            approval.get("approvedResults", []),
        )

    def test_approve_refuses_legacy_wf_b43032f7fee5_before_launch(self) -> None:
        wf_id = "wf_b43032f7fee5"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text(
            "meta = {'name': 'b43032-fixture'}\nreturn {'ok': True}\n", encoding="utf-8"
        )
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {
                "wfId": wf_id,
                "status": "running",
                "workspace": str(self.workspace),
                "scriptPath": str(script_path),
                "journalPath": str(root / workflow_registry.JOURNAL_FILE),
                "resultPath": str(root / workflow_registry.RESULT_FILE),
                "budget": {"total": None, "spent": 4, "remaining": None},
            },
        )
        approved = self.run_delegate(["--json", "workflow", "approve", wf_id])
        self.assertEqual(approved.returncode, 2, approved.stderr)
        self.assertEqual(json.loads(approved.stdout)["error"], "unsupported_workflow_version")
        self.assertFalse((root / workflow_registry.APPROVAL_FILE).exists())

    def test_reject_is_durable_idempotent_and_sequence_ordered(self) -> None:
        wf_id = "wf_333333333333"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {
                "wfId": wf_id,
                "status": "created",
                "workflowKeyVersion": 2,
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.append_event("agent_started", key="agent-key", label="draft")
        state.append_event("agent_finished", key="agent-key", result={"ok": False})
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertEqual(state.reject_agent("draft", "emitter policy"), "agent-key")
        single = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertEqual(state.reject_agent("agent-key", "same policy"), "agent-key")
        self.assertIn("agent-key", state.tombstoned_keys)
        self.assertNotIn("agent-key", state.replay_keys)
        double = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertEqual(double.replay, single.replay)
        self.assertEqual(double.replay_keys, single.replay_keys)
        self.assertEqual(double.tombstoned_keys, single.tombstoned_keys)
        self.assertEqual(double.started_without_result, single.started_without_result)
        events = workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
        self.assertEqual(
            [event["type"] for event in events],
            ["agent_started", "agent_finished", "agent_rejected", "agent_rejected"],
        )

        # A result recorded after the tombstone is a fresh settlement and
        # becomes replayable on the next supervisor start.
        state.append_event("agent_finished", key="agent-key", result={"ok": True})
        resumed = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertNotIn("agent-key", resumed.tombstoned_keys)
        self.assertEqual(resumed.replay.get("agent-key"), {"ok": True})

    def test_reject_unknown_key_or_label_fails_script_and_cli(self) -> None:
        wf_id = "wf_666666666666"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {
                "wfId": wf_id,
                "status": "created",
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.append_event("agent_started", key="known-key", label="known")
        self.assertEqual(state.reject_agent("known-key", "no cached result"), "known-key")
        self.assertIn("known-key", state.tombstoned_keys)
        replayed = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertIn("known-key", replayed.tombstoned_keys)
        self.assertIn("known-key", replayed.started_without_result)
        with self.assertRaisesRegex(ValueError, "missing-key"):
            state.reject_agent("missing-key", "not found")
        with self.assertRaisesRegex(ValueError, "missing-label"):
            state.reject_agent("missing-label", "not found")

        rejected = self.run_delegate(
            ["--json", "workflow", "reject", wf_id, "missing-key", "--reason", "not found"]
        )
        self.assertNotEqual(rejected.returncode, 0)
        payload = json.loads(rejected.stdout)
        self.assertEqual(payload["error"], "workflow_reject_unresolved")
        self.assertIn("missing-key", payload["message"])
        rejected_label = self.run_delegate(
            [
                "--json",
                "workflow",
                "reject",
                wf_id,
                "missing-label",
                "--reason",
                "not found",
            ]
        )
        self.assertNotEqual(rejected_label.returncode, 0)
        label_payload = json.loads(rejected_label.stdout)
        self.assertEqual(label_payload["error"], "workflow_reject_unresolved")
        self.assertIn("missing-label", label_payload["message"])

        script = self.write_workflow(
            """
            meta = {"name": "reject-unknown-script"}
            reject("missing-key", "not found")
            return True
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        script_wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", script_wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        status = self.run_delegate(["--json", "workflow", "status", script_wf_id])
        self.assertIn("missing-key", json.loads(status.stdout).get("error", ""))

    def test_reject_label_ignores_non_settlement_events(self) -> None:
        wf_id = "wf_777777777777"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {"wfId": wf_id, "status": "created", "budget": {"total": None, "spent": 0}},
        )
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.append_event("agent_started", key="settled-key", label="draft")
        state.append_event("agent_finished", key="settled-key", label="draft", result="old")
        state.append_event("agent_cache_hit", key="wrong-key", label="draft", result="old")
        state.append_event("agent_rejected", key="wrong-key", label="draft", reason="other")
        replayed = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertEqual(replayed.resolve_agent_key("draft"), ("settled-key", "draft"))

    def test_start_after_reject_remains_adoptable(self) -> None:
        wf_id = "wf_888888888888"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {"wfId": wf_id, "status": "created", "budget": {"total": None, "spent": 0}},
        )
        for event in (
            {"seq": 1, "type": "agent_started", "key": "retry-key"},
            {"seq": 2, "type": "agent_rejected", "key": "retry-key", "reason": "stale"},
            {"seq": 3, "type": "agent_started", "key": "retry-key"},
        ):
            workflow_registry.append_jsonl(root / workflow_registry.JOURNAL_FILE, event)
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertIn("retry-key", state.tombstoned_keys)
        self.assertIn("retry-key", state.started_after_tombstone)

    def test_cli_reject_parked_workflow_tombstones_and_resume_respawns(self) -> None:
        child = self.write_saved_workflow(
            "reject-cli-child",
            """
            meta = {"name": "reject-cli-child"}
            return agent("draft", label="draft")
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "reject-cli-parent"}}
            return workflow({child!r}, gate=True)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["workflow"]["status"], "paused")

        events = json.loads(self.run_delegate(["--json", "workflow", "events", wf_id]).stdout)[
            "events"
        ]
        first_key = next(event["key"] for event in events if event.get("label") == "draft")
        rejected = self.run_delegate(
            ["--json", "workflow", "reject", wf_id, "draft", "--reason", "stale result"]
        )
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        reject_payload = json.loads(rejected.stdout)
        self.assertEqual(reject_payload["key"], first_key)
        self.assertEqual(reject_payload["label"], "draft")
        self.assertEqual(reject_payload["reason"], "stale result")

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resumed_wait = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(resumed_wait.returncode, 0, resumed_wait.stderr)
        events = json.loads(self.run_delegate(["--json", "workflow", "events", wf_id]).stdout)[
            "events"
        ]
        started = [event for event in events if event.get("type") == "agent_started"]
        self.assertEqual(len(started), 2)
        self.assertNotEqual(started[0]["key"], started[1]["key"])

        # Exercise the real resume path a second time.  Setting
        # WorkflowState.replay_attempt directly cannot prove the supervisor
        # restored and re-emitted status.json's counter.
        rejected = self.run_delegate(
            ["--json", "workflow", "reject", wf_id, "draft", "--reason", "still stale"]
        )
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resumed_wait = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(resumed_wait.returncode, 0, resumed_wait.stderr)
        events = json.loads(self.run_delegate(["--json", "workflow", "events", wf_id]).stdout)[
            "events"
        ]
        started = [event for event in events if event.get("type") == "agent_started"]
        self.assertEqual(len(started), 3)
        self.assertEqual(len({event["key"] for event in started}), 3)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status.get("replayAttempt"), 2)

    def test_cli_reject_refuses_live_running_workflow(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "reject-live"}
            return agent("slow", label="draft")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "2"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        rejected = self.run_delegate(
            ["--json", "workflow", "reject", wf_id, "draft", "--reason", "race"]
        )
        self.assertNotEqual(rejected.returncode, 0)
        payload = json.loads(rejected.stdout)
        self.assertEqual(payload["error"], "workflow_running")
        self.assertIn("reject() from the workflow script", payload["message"])
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)

    def test_current_agent_key_includes_timeout(self) -> None:
        opts = {
            "engine": "codex",
            "mode": "safe",
            "model": None,
            "effort": None,
            "fast": None,
            "schema": None,
            "isolation": None,
            "personaDigest": None,
        }
        short = workflow_runtime._agent_key("root/seq#0", "parity prompt", {**opts, "timeout": 1})
        long = workflow_runtime._agent_key("root/seq#0", "parity prompt", {**opts, "timeout": 3601})
        self.assertNotEqual(short, long)

    def test_new_workflow_launch_stamps_v2_key_version(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "v2-stamp"}
            return True
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        status = self.run_delegate(["--json", "workflow", "status", wf_id])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout).get("workflowKeyVersion"), 2)

    def test_current_tombstone_retry_uses_attempt_key(self) -> None:
        wf_id = "wf_555555555555"
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        script_path = root / workflow_registry.SCRIPT_FILE
        script_path.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {
                "wfId": wf_id,
                "status": "created",
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        base_opts = {
            "engine": "codex",
            "mode": "safe",
            "model": None,
            "effort": None,
            "fast": None,
            "schema": None,
            "isolation": None,
            "personaDigest": None,
            "timeout": 1,
        }
        base_key = workflow_runtime._agent_key("root/seq#0", "retry prompt", base_opts)
        workflow_registry.append_jsonl(
            root / workflow_registry.JOURNAL_FILE,
            {"seq": 1, "type": "agent_rejected", "key": base_key, "reason": "retry"},
        )
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script_path,
            config={},
            cli_argv=[str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
            replay_attempt=2,
            dry_run=True,
        )
        workflow_runtime.WorkflowDsl(state, {"defaults": {"engine": "codex"}}).agent(
            "retry prompt", timeout=1
        )
        started = [
            event
            for event in workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
            if event.get("type") == "agent_started"
        ]
        self.assertEqual(len(started), 1)
        self.assertNotEqual(started[0]["key"], base_key)
        retry_opts = dict(base_opts)
        retry_opts["retryAttempt"] = 2
        self.assertEqual(
            started[0]["key"],
            workflow_runtime._agent_key("root/seq#0", "retry prompt", retry_opts),
        )

    def test_resume_releases_paused_gate(self) -> None:
        child = self.write_saved_workflow(
            "resume-gate-child",
            """
            meta = {"name": "gate-child"}
            return {"ok": False}
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "gate-parent"}}
            return workflow({child!r}, gate="on-failure")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["workflow"]["status"], "paused")

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["workflow"]["status"], "succeeded")
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": False})

    def test_resume_clears_watchdog_markers_from_prior_attempt(self) -> None:
        """Fire markers must not survive a resume into a clean, successful run.

        Without the clear, a workflow that fired its watchdog, was resumed, and
        then succeeded reports status succeeded with watchdogCancelRequested
        still true and fire diagnostics from the earlier attempt (WDB-R4). The
        companion assertion pins the other half of the contract: a same-attempt
        status rebuild that omits the marker keys still preserves them, which
        is why the resume clear must write explicit None rather than pop.
        """
        child = self.write_saved_workflow(
            "resume-watchdog-child",
            """
            meta = {"name": "watchdog-child"}
            return {"ok": False}
            """,
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "watchdog-parent"}}
            return workflow({child!r}, gate="on-failure")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["workflow"]["status"], "paused")

        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        status.update(
            {
                "watchdogFiredAt": "2026-08-31T00:00:00Z",
                "watchdogReason": "state_missing",
                "watchdogCancelRequested": True,
            }
        )
        workflow_registry.write_status(root, status)

        # Same-attempt rebuilds omit the marker keys and must preserve them.
        rebuilt = {
            key: value
            for key, value in status.items()
            if key not in ("watchdogFiredAt", "watchdogReason", "watchdogCancelRequested")
        }
        workflow_registry.write_status(root, rebuilt)
        preserved = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        self.assertEqual(preserved.get("watchdogReason"), "state_missing")
        self.assertIs(preserved.get("watchdogCancelRequested"), True)

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["workflow"]["status"], "succeeded")
        final = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        self.assertIsNone(final.get("watchdogFiredAt"), final)
        self.assertIsNone(final.get("watchdogReason"), final)
        self.assertFalse(final.get("watchdogCancelRequested"), final)

    def test_run_rejects_conflicting_targets(self) -> None:
        script = self.write_workflow('meta = {"name": "target"}\nreturn None')
        result = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--resume", "wf_0123abcdef45"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one", result.stdout)

    def test_budget_exceeded_maps_pipeline_slot_to_none(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "budget", "defaults": {"engine": "codex", "mode": "safe"}}
            return pipeline(["one", "two"], lambda prev, item, index: agent(item))
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script), "--budget", "1"])
        wf_id = json.loads(launch.stdout)["wfId"]
        self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        values = json.loads(result.stdout)["result"]
        self.assertEqual(values.count("fake completion"), 1)
        self.assertEqual(values.count(None), 1)

    def test_dry_run_budget_does_not_consume_real_slots(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-budget", "defaults": {"engine": "codex", "mode": "safe"}}
            return [agent("one"), agent("two")]
            """
        )
        result = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--dry-run", "--budget", "1"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["result"], ["", ""])
        self.assertEqual(payload["runTree"]["counts"], {"codex:safe": 2})

    def test_resume_from_dry_run_launches_live_agents_on_same_workflow(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-resume", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one")
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        dry_status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        dry_last_seq = dry_status["lastSeq"]

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["wfId"], wf_id)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "fake completion")
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs.stdout)["runs"]), 1)

        journal = workflow_registry.workflow_dir(self.workspace, wf_id) / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event.get("simulated") for event in events))
        self.assertTrue(
            any(event["type"] == "budget" and not event.get("simulated") for event in events)
        )
        self.assertTrue(
            any(
                event["type"] == "agent_finished" and event.get("result") == "fake completion"
                for event in events
            )
        )
        live_events = json.loads(
            self.run_delegate(
                ["--json", "workflow", "events", wf_id, "--since", str(dry_last_seq)]
            ).stdout
        )["events"]
        self.assertGreater(min(event["seq"] for event in live_events), dry_last_seq)
        self.assertTrue(
            any(event["type"] == "budget" and not event.get("simulated") for event in live_events)
        )
        self.assertIn("agent_started", {event["type"] for event in live_events})
        self.assertTrue(
            any(
                event["type"] == "agent_finished" and event.get("result") == "fake completion"
                for event in live_events
            )
        )
        self.assertNotIn("agent_cache_hit", {event["type"] for event in live_events})
        terminal_status = json.loads(
            self.run_delegate(["--json", "workflow", "status", wf_id]).stdout
        )
        self.assertEqual(terminal_status["scriptSha256"], dry_status["scriptSha256"])
        self.assertEqual(terminal_status["args"], dry_status["args"])

    def test_resume_from_dry_run_repeated_after_pre_supervisor_failure_goes_live(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-resume-retry", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one")
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        status.update({"status": "starting", "replayJournal": False})
        workflow_registry.write_status(root, status)

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "fake completion")
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs.stdout)["runs"]), 1)

    def test_resume_from_dry_run_running_before_live_event_goes_live(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-resume-running", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one")
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        dry_last_seq = status["lastSeq"]
        status.update({"status": "running", "replayJournal": False})
        workflow_registry.write_status(root, status)

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "fake completion")
        events = json.loads(
            self.run_delegate(
                ["--json", "workflow", "events", wf_id, "--since", str(dry_last_seq)]
            ).stdout
        )["events"]
        self.assertNotIn("agent_cache_hit", {event["type"] for event in events})

    def test_resume_from_dry_run_replays_real_progress_not_simulated_placeholders(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-resume-partial", "defaults": {"engine": "codex", "mode": "safe"}}
            return [agent("one"), agent("two")]
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / "journal.jsonl"
        dry_events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        first_key = next(event["key"] for event in dry_events if event["type"] == "agent_started")
        seq = max(event["seq"] for event in dry_events)
        workflow_registry.append_jsonl(
            journal, {"seq": seq + 1, "type": "budget", "key": first_key}
        )
        workflow_registry.append_jsonl(
            journal, {"seq": seq + 2, "type": "agent_started", "key": first_key}
        )
        workflow_registry.append_jsonl(
            journal,
            {"seq": seq + 3, "type": "agent_finished", "key": first_key, "result": "real one"},
        )
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        status.update({"status": "running", "replayJournal": False, "lastSeq": seq + 3})
        workflow_registry.write_status(root, status)

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], ["real one", "fake completion"])
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs.stdout)["runs"]), 1)

    def test_call_mode_agent_runs_without_workspace_cwd(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "call-agent", "defaults": {"engine": "codex"}}
            return agent("one-hop", mode="call")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertIn("fake completion", json.loads(result.stdout)["result"])

    def test_workflow_agent_threads_explicit_fast_false_to_codex(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "fast-off", "defaults": {"engine": "codex", "mode": "call", "fast": True}}
            return agent("one-hop", fast=False)
            """
        )
        argv_log = self.workspace / "codex-argv.json"
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_ARGV_LOG": str(argv_log)},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn('service_tier="default"', json.loads(argv_log.read_text()))
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        snapshot = self.run_delegate(["--json", "snapshot", runs[0]["runId"]])
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertIs(json.loads(snapshot.stdout)["requestedFast"], False)

    def test_check_rejects_passthrough_call_and_schema(self) -> None:
        passthrough_call = self.write_workflow(
            """
            meta = {"name": "bad-call"}
            return agent("x", mode="call", passthrough=True)
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(passthrough_call)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("slash pass-through needs", json.loads(result.stdout)["message"])

        passthrough_schema = self.write_workflow(
            """
            meta = {"name": "bad-schema"}
            return agent("x", passthrough=True, schema={"type": "object"})
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(passthrough_schema)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutually exclusive", json.loads(result.stdout)["message"])

    def test_run_rejects_invalid_agent_timeout(self) -> None:
        for bad_timeout in ("True", "'30'", "0", "-5", "float('inf')"):
            with self.subTest(timeout=bad_timeout):
                script = self.write_workflow(
                    f"""
                    meta = {{"name": "bad-timeout"}}
                    return agent("x", timeout={bad_timeout})
                    """
                )
                result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"], "workflow_execution_failed")
                self.assertIn("timeout must be a positive number of seconds", payload["message"])

    def test_run_surfaces_check_warnings_at_launch(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "warning", "defaults": {"engine": "codex", "mode": "safe"}}
            import random
            log(random.random())
            return None
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        self.assertTrue(
            any("determinism warning" in item for item in json.loads(launch.stdout)["warnings"])
        )

    def test_parallel_item_threads_bound_started_threads(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["workflows"]["itemThreads"] = 2
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        script = self.write_workflow(
            """
            meta = {"name": "threads"}
            import threading, time
            base = threading.active_count()
            seen = {"max": 0}
            lock = threading.Lock()
            def task(i):
                with lock:
                    seen["max"] = max(seen["max"], threading.active_count() - base)
                time.sleep(0.1)
                return i
            values = parallel([lambda i=i: task(i) for i in range(12)])
            return {"values": values, "maxThreads": seen["max"]}
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)[
            "result"
        ]
        self.assertEqual(result["values"], list(range(12)))
        self.assertLessEqual(result["maxThreads"], 3)

    def test_running_workflow_resume_fails_fast_on_lock(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "lock", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("very slow")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id)
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertNotEqual(resumed.returncode, 0)
        self.assertEqual(json.loads(resumed.stdout)["error"], "workflow_locked")
        self.run_delegate(["--json", "workflow", "kill", wf_id])

    def test_resume_hides_prior_terminal_status_and_result_before_supervisor_finishes(
        self,
    ) -> None:
        resume_marker = self.workspace / "resume-started"
        release_marker = self.workspace / "resume-release"
        script = self.write_workflow(
            f"""
            meta = {{"name": "resume-barrier"}}
            import time
            from pathlib import Path
            if Path({str(resume_marker)!r}).exists():
                while not Path({str(release_marker)!r}).exists():
                    time.sleep(0.05)
                return "new"
            return "old"
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        prior = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(prior.stdout)["result"], "old")

        resume_marker.touch()
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        try:
            immediate_result = self.run_delegate(["--json", "workflow", "result", wf_id])
            self.assertNotEqual(immediate_result.returncode, 0)
            self.assertEqual(
                json.loads(immediate_result.stdout)["error"], "workflow_result_missing"
            )

            immediate_wait = self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "1"]
            )
            self.assertEqual(immediate_wait.returncode, 124, immediate_wait.stderr)
            wait_payload = json.loads(immediate_wait.stdout)
            self.assertTrue(wait_payload["timedOut"])
            self.assertIn(wait_payload["workflow"]["status"], {"starting", "running"})
        finally:
            release_marker.touch()

        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "new")

    def test_resume_launch_failure_restores_prior_status_and_result(self) -> None:
        wf_id = "wf_123456789abc"
        root = self._seed_completed_workflow(
            wf_id,
            created_at="2026-01-01T00:00:00Z",
            result={"value": "old"},
        )
        (root / workflow_registry.SCRIPT_FILE).write_text(
            'meta = {"name": "restore"}\nreturn "new"\n', encoding="utf-8"
        )
        status_path = root / workflow_registry.STATUS_FILE
        result_path = root / workflow_registry.RESULT_FILE
        status = workflow_registry.read_json(status_path) or {}
        status["budget"] = {"total": 1, "spent": 1, "remaining": 0}
        workflow_registry.write_status(root, status)
        prior_status = workflow_registry.read_json(status_path)
        prior_result = result_path.read_bytes()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            workflow_pinning.create_pin(wf_id, workspace=self.workspace, config={}, home=self.home)

        def fail_detach(*_args, **_kwargs) -> None:
            self.assertEqual(workflow_registry.read_json(status_path)["status"], "starting")
            self.assertFalse(result_path.exists())
            self.assertTrue(workflow_registry.supervisor_alive(root))
            raise RuntimeError("launch failed")

        with (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(
                workflow_runtime,
                "detach_supervisor",
                side_effect=fail_detach,
            ),
            self.assertRaisesRegex(RuntimeError, "launch failed"),
        ):
            workflow_commands.emit_run(
                workflow_commands.WorkflowCommand("run", resume=wf_id, budget=99, json_mode=True),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(workflow_registry.read_json(status_path), prior_status)
        self.assertEqual(result_path.read_bytes(), prior_result)
        self.assertEqual(result_path.stat().st_mode & 0o777, 0o600)

    def test_unsupported_workflow_versions_refuse_before_launch(self) -> None:
        wf_id = "wf_123456789abc"
        root = self._seed_completed_workflow(
            wf_id,
            created_at="2026-01-01T00:00:00Z",
            result={"value": "old"},
        )
        (root / workflow_registry.SCRIPT_FILE).write_text("return 'legacy'\n", encoding="utf-8")
        for version in (None, 1, 3):
            with self.subTest(version=version):
                status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
                if version is None:
                    status.pop("workflowKeyVersion", None)
                else:
                    status["workflowKeyVersion"] = version
                workflow_registry.write_json(root / workflow_registry.STATUS_FILE, status)
                with (
                    mock.patch.object(workflow_runtime, "detach_supervisor") as detach,
                    self.assertRaises(workflow_commands.DelegateError) as raised,
                ):
                    workflow_commands.emit_run(
                        workflow_commands.WorkflowCommand("run", resume=wf_id, json_mode=True),
                        workspace=self.workspace,
                        config={},
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )
                self.assertEqual(raised.exception.error, "unsupported_workflow_version")
                detach.assert_not_called()
        self.assertFalse(workflow_pinning.pin_path(wf_id, home=self.home).exists())

    def test_resume_snapshot_failure_releases_workflow_lock(self) -> None:
        wf_id = "wf_123456789abc"
        root = self._seed_completed_workflow(
            wf_id,
            created_at="2026-01-01T00:00:00Z",
            result={"value": "old"},
        )
        prior_result = (root / workflow_registry.RESULT_FILE).read_bytes()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            workflow_pinning.create_pin(wf_id, workspace=self.workspace, config={}, home=self.home)

        with (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(Path, "read_bytes", side_effect=RuntimeError("snapshot failed")),
            self.assertRaisesRegex(RuntimeError, "snapshot failed"),
        ):
            workflow_commands.emit_run(
                workflow_commands.WorkflowCommand("run", resume=wf_id, json_mode=True),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertFalse(workflow_registry.supervisor_alive(root))
        self.assertEqual((root / workflow_registry.RESULT_FILE).read_bytes(), prior_result)
        lock_fd = workflow_registry.acquire_workflow_lock(root)
        os.close(lock_fd)

    def test_resume_adopts_completed_child_run_with_missing_result_event(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "adopt", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one", label="adopt-me")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        payload = json.loads(launch.stdout)
        wf_id = payload["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        root = self.workspace / ".delegate" / "workflows" / wf_id
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        kept = [
            event
            for event in events
            if event["type"] not in {"agent_finished", "workflow_finished"}
        ]
        journal.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in kept),
            encoding="utf-8",
        )
        (root / "result.json").unlink()

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "fake completion")
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])
        self.assertEqual(len(json.loads(runs.stdout)["runs"]), 1)
        events_after = self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"])
        self.assertIn(
            "agent_adopted", {event["type"] for event in json.loads(events_after.stdout)["events"]}
        )

    def test_nested_workflow_rejects_external_absolute_path(self) -> None:
        child = self.write_workflow(
            """
            meta = {"name": "external-child"}
            return None
            """
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "reject-external"}}
            return workflow({str(child)!r})
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        status = self.run_delegate(["--json", "workflow", "status", wf_id])
        self.assertEqual(json.loads(status.stdout)["status"], "failed")
        self.assertIn("nested workflow paths", json.loads(status.stdout)["error"])

    def test_nested_workflow_depth_cap_rejects_fourth_level(self) -> None:
        self.write_saved_workflow("depth-d", 'meta = {"name": "d"}\nreturn None')
        self.write_saved_workflow(
            "depth-c",
            """
            meta = {"name": "c"}
            return workflow("depth-d")
            """,
        )
        self.write_saved_workflow(
            "depth-b",
            """
            meta = {"name": "b"}
            return workflow("depth-c")
            """,
        )
        self.write_saved_workflow(
            "depth-a",
            """
            meta = {"name": "a"}
            return workflow("depth-b")
            """,
        )
        parent = self.write_workflow(
            """
            meta = {"name": "depth-root"}
            return workflow("depth-a")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(parent)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "failed")
        self.assertIn("workflow nesting depth exceeded 3", status["error"])

    def test_lifetime_agent_cap_rejects_thousand_first_agent(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "lifetime", "defaults": {"engine": "codex", "mode": "safe"}}
            return [agent(str(i)) for i in range(1001)]
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"], "workflow_execution_failed")
        self.assertIn("1000 lifetime agent() calls", payload["message"])

    def test_iter_journal_ignores_truncated_final_line(self) -> None:
        journal = self.workspace / "journal.jsonl"
        workflow_registry.append_jsonl(journal, {"seq": 1, "type": "agent_finished"})
        with journal.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 2, "type": ')
        with self.assertWarns(RuntimeWarning):
            events = workflow_registry.iter_journal(journal)
        self.assertEqual(events, [{"seq": 1, "type": "agent_finished"}])

    def test_workflow_kill_cancels_children_and_preserves_status_fields(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "kill", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("very slow")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--budget", "3"],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "10"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id)
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        self.assertEqual(killed.returncode, 0, killed.stderr)
        payload = json.loads(killed.stdout)
        self.assertTrue(payload["cancelled"])
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "killed")
        self.assertEqual(status["budget"]["total"], 3)
        self.assertIn("scriptPath", status)
        self.assertIn("journalPath", status)

    def test_workflow_kill_unsafe_child_returns_typed_json_error(self) -> None:
        wf_id = "wf_111122223333"
        workflow_root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        workflow_registry.write_status(
            workflow_root,
            {
                "wfId": wf_id,
                "status": "running",
                "budget": {"total": None, "spent": 0},
            },
        )
        registry_root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")
        run_id, alias = run_registry.register_run(
            registry_root,
            harness="codex",
            metadata={"group": wf_id},
        )
        run_registry.write_json_atomic(
            run_registry.run_directory(registry_root, run_id) / run_registry.STATE_FILE,
            {
                "schema": run_registry.STATE_SCHEMA,
                "runId": run_id,
                "alias": alias,
                "status": run_registry.STATUS_RUNNING,
                "pid": 1,
                "pgid": 1,
            },
        )

        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])

        self.assertEqual(killed.returncode, 2)
        self.assertNotIn("Traceback", killed.stderr)
        payload = json.loads(killed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["schema"], "delegate.error.v1")
        self.assertEqual(payload["error"], "unsafe_signal_target")
        self.assertIn("pid/pgid <= 1", payload["message"])
        self.assertEqual(payload["failureCount"], 1)
        self.assertEqual(payload["failures"][0]["runId"], run_id)
        self.assertNotIn(payload.get("status"), {"killed", "paused"})
        status = workflow_registry.read_json(workflow_root / workflow_registry.STATUS_FILE) or {}
        self.assertEqual(status.get("status"), "running")

    def test_structured_output_with_codex_schema(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "schema", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        wf_id = json.loads(launch.stdout)["wfId"]
        self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})

    def test_codex_workflow_schema_preflight_warns_and_auto_injects(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "schema-preflight", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}}
            return agent("structured", schema=SCHEMA)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        wf_id = json.loads(launch.stdout)["wfId"]
        self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])

        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        runs = self.run_delegate(["--json", "runs", "--group", wf_id])

        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})
        warnings = json.loads(runs.stdout)["runs"][0]["warnings"]
        self.assertTrue(any("auto-injected" in warning for warning in warnings))

    def test_codex_structured_output_uses_exact_final_completion(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "schema-final", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA)
            """
        )
        env = {"FAKE_CODEX_REAL_SHAPE": "1"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "final"})

    def test_codex_structured_output_rejects_valid_preamble_without_child_report(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "schema-no-report", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA, retries=1)
            """
        )
        env = {"FAKE_CODEX_PREAMBLE_ONLY": "1"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertIsNone(json.loads(result.stdout)["result"])
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 2)

    def test_codex_structured_output_rejects_deleted_child_report(self) -> None:
        state = type(
            "State",
            (),
            {
                "cli_argv": ["delegate"],
                "wf_id": "wf_deleted_report",
                "attempt_environment": None,
                "cancel_event": None,
                "workspace": self.workspace,
            },
        )()
        dsl = object.__new__(workflow_runtime.WorkflowDsl)
        dsl.state = state
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "assistantText": '{"ok": true, "value": "preamble"}',
                    "completionReportSource": "child",
                    "completionReportPath": str(self.workspace / "deleted.md"),
                }
            ).encode(),
            stderr=b"",
        )
        with mock.patch.object(workflow_runtime, "_run_child_command", return_value=completed):
            result = dsl._run_delegate(
                "codex",
                "structured",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                isolation=None,
                passthrough=False,
                timeout=None,
                output_schema="schema.json",
                prefer_assistant=True,
                workflow_agent_key="key",
            )
        self.assertIsNone(result)

    def test_resume_adopts_exact_codex_structured_completion(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "schema-final-adopt", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA)
            """
        )
        env = {"FAKE_CODEX_REAL_SHAPE": "1"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = self.workspace / ".delegate" / "workflows" / wf_id
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        journal.write_text(
            "".join(
                json.dumps(event, sort_keys=True) + "\n"
                for event in events
                if event["type"] not in {"agent_finished", "workflow_finished"}
            ),
            encoding="utf-8",
        )
        (root / "result.json").unlink()

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "final"})

    def test_resume_respawns_codex_structured_child_without_authoritative_report(self) -> None:
        for tamper in ("deleted", "missing-source"):
            with self.subTest(tamper=tamper):
                self._assert_resume_respawns_codex_structured_child(tamper)

    def _assert_resume_respawns_codex_structured_child(self, tamper: str) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "schema-report-adopt", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA)
            """
        )
        env = {"FAKE_CODEX_REAL_SHAPE": "1"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        root = self.workspace / ".delegate" / "workflows" / wf_id
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        journal.write_text(
            "".join(
                json.dumps(event, sort_keys=True) + "\n"
                for event in events
                if event["type"] not in {"agent_finished", "workflow_finished"}
            ),
            encoding="utf-8",
        )
        (root / "result.json").unlink()
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 1)
        run_id = runs[0]["runId"]
        snapshot_path = self.workspace / ".delegate" / "runs" / run_id / "state.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if tamper == "deleted":
            report = Path(snapshot["completionReport"]["path"])
            if not report.is_absolute():
                report = self.workspace / report
            report.unlink()
        else:
            snapshot.pop("completionReportSource", None)
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id], env_extra=env)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "final"})
        runs_after = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)[
            "runs"
        ]
        self.assertEqual(len(runs_after), 2)

    def test_schema_min_length_and_min_items_validation(self) -> None:
        for schema, message in (
            ({"type": "string", "minLength": -1}, "non-negative integer"),
            ({"type": "array", "minItems": True}, "non-negative integer"),
            ({"type": "array", "minLength": 1}, "only applies to string"),
            ({"type": ["string", "null"], "minItems": 1}, "only applies to array"),
        ):
            with (
                self.subTest(schema=schema),
                self.assertRaisesRegex(workflow_schema.SchemaError, message),
            ):
                workflow_schema.validate_schema_subset(schema)
        workflow_schema.validate_schema_subset({"minLength": 1})
        workflow_schema.validate_schema_subset({"type": ["string", "null"], "minLength": 1})
        workflow_schema.validate_schema_subset({"type": ["array", "null"], "minItems": 1})

    def test_structured_retry_enforces_min_length_recursively(self) -> None:
        self._assert_structured_bound_retry(
            keyword="minLength", schema='{"type": "string", "minLength": 1}'
        )

    def test_structured_retry_enforces_min_items_recursively(self) -> None:
        self._assert_structured_bound_retry(
            keyword="minItems",
            schema='{"type": "array", "items": {"type": "string"}, "minItems": 1}',
        )

    def _assert_structured_bound_retry(self, *, keyword: str, schema: str) -> None:
        prompt_log = self.workspace / f"{keyword}-prompts.log"
        attempt_file = self.workspace / f"{keyword}-attempts.txt"
        script = self.write_workflow(
            f"""\
            meta = {{"name": "schema-{keyword}", "defaults": {{"engine": "codex", "mode": "safe"}}}}
            SCHEMA = {{"type": "object", "required": ["ok", "value"], "properties": {{"ok": {{"type": "boolean"}}, "value": {schema}}}, "additionalProperties": False}}
            return agent("structured", schema=SCHEMA, retries=1)
            """
        )
        env = {
            "FAKE_PROMPT_LOG": str(prompt_log),
            "FAKE_CODEX_ATTEMPT_FILE": str(attempt_file),
            "FAKE_CODEX_SHORT_KIND": keyword,
        }
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
            ).returncode,
            0,
        )
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)[
            "result"
        ]
        self.assertTrue(result["value"])
        prompts = prompt_log.read_text(encoding="utf-8").split("\n---\n")
        self.assertIn("Validation error:", prompts[1])
        self.assertIn(keyword, prompts[1])

    def test_codex_structured_retry_prompt_includes_correction_context(self) -> None:
        prompt_log = self.workspace / "prompts.log"
        attempt_file = self.workspace / "attempts.txt"
        script = self.write_workflow(
            """
            meta = {"name": "schema-retry", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA, retries=1)
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={
                "FAKE_PROMPT_LOG": str(prompt_log),
                "FAKE_CODEX_ATTEMPT_FILE": str(attempt_file),
            },
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(
                ["--json", "workflow", "wait", wf_id, "--timeout", "10"],
                env_extra={
                    "FAKE_PROMPT_LOG": str(prompt_log),
                    "FAKE_CODEX_ATTEMPT_FILE": str(attempt_file),
                },
            ).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})
        prompts = prompt_log.read_text(encoding="utf-8").split("\n---\n")
        self.assertIn('{"ok": "wrong"}', prompts[1])
        self.assertIn("Validation error:", prompts[1])

    def test_codex_structured_retry_resumes_session_in_same_worktree(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        prompt_log = self.workspace / "resume-prompts.log"
        argv_log = self.workspace / "resume-argv.json"
        attempt_file = self.workspace / "resume-attempts.txt"
        script = self.write_workflow(
            """
            meta = {"name": "schema-native-resume", "defaults": {"engine": "codex", "mode": "work"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("review the entire repository", schema=SCHEMA, retries=1, isolation="worktree")
            """
        )
        env = {
            "FAKE_PROMPT_LOG": str(prompt_log),
            "FAKE_CODEX_ARGV_LOG": str(argv_log),
            "FAKE_CODEX_ATTEMPT_FILE": str(attempt_file),
            "FAKE_CODEX_SESSION_ID": "thread-structured-1",
        }
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        launched = json.loads(launch.stdout)
        wf_id = launched["wfId"]
        waited = self.run_delegate(
            ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})

        prompts = prompt_log.read_text(encoding="utf-8").split("\n---\n")
        self.assertIn("review the entire repository", prompts[0])
        self.assertIn("Re-emit the StructuredOutput now", prompts[1])
        self.assertNotIn("review the entire repository", prompts[1])
        self.assertNotIn('{"ok": "wrong"}', prompts[1])
        retry_argv = json.loads(argv_log.read_text(encoding="utf-8").strip().splitlines()[-1])
        exec_index = retry_argv.index("exec")
        # Sandbox/config flags may sit between `exec` and the `resume`
        # subcommand; a structured resume binds the session id right after it.
        resume_index = retry_argv.index("resume")
        self.assertGreater(resume_index, exec_index)
        self.assertEqual(
            retry_argv[resume_index : resume_index + 2],
            ["resume", "thread-structured-1"],
        )
        self.assertNotIn("--ephemeral", retry_argv)
        self.assertNotIn("--cd", retry_argv)

        runs = self.wait_for_group_runs(wf_id, count=2)
        self.assertEqual(len({run["executionCwd"] for run in runs}), 1)
        journal_path = Path(launched["journalPath"])
        events = [
            json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        retry = next(event for event in events if event["type"] == "agent_structured_retry")
        self.assertEqual(retry["strategy"], "resume")
        self.assertEqual(retry["sessionId"], "thread-structured-1")

    def test_structured_retry_relaunches_unsupported_engine_in_same_worktree(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        prompt_log = self.workspace / "relaunch-prompts.log"
        workspace_log = self.workspace / "relaunch-workspaces.log"
        attempt_file = self.workspace / "relaunch-attempts.txt"
        script = self.write_workflow(
            """
            meta = {"name": "schema-relaunch", "defaults": {"engine": "droid", "model": "gemini", "mode": "work"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("perform the implementation", schema=SCHEMA, retries=1, isolation="worktree")
            """
        )
        env = {
            "FAKE_GENERIC_PROMPT_LOG": str(prompt_log),
            "FAKE_GENERIC_WORKSPACE_LOG": str(workspace_log),
            "FAKE_GENERIC_ATTEMPT_FILE": str(attempt_file),
        }
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        launched = json.loads(launch.stdout)
        wf_id = launched["wfId"]
        waited = self.run_delegate(
            ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})

        workspaces = workspace_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(workspaces), 2)
        self.assertEqual(workspaces[0], workspaces[1])
        self.assertNotEqual(workspaces[0], str(self.workspace))
        prompts = prompt_log.read_text(encoding="utf-8").split("\n---\n")
        self.assertIn("perform the implementation", prompts[1])
        self.assertIn("not json", prompts[1])
        journal_path = Path(launched["journalPath"])
        events = [
            json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        retry = next(event for event in events if event["type"] == "agent_structured_retry")
        self.assertEqual(retry["strategy"], "relaunch")
        self.assertNotIn("sessionId", retry)

    def test_structured_retry_reuses_then_cleans_safe_temporary_workspace(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        workspace_log = self.workspace / "safe-relaunch-workspaces.log"
        attempt_file = self.workspace / "safe-relaunch-attempts.txt"
        script = self.write_workflow(
            """
            meta = {"name": "schema-safe-relaunch", "defaults": {"engine": "droid", "model": "gemini", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("review safely", schema=SCHEMA, retries=1, isolation="worktree")
            """
        )
        env = {
            "FAKE_GENERIC_WORKSPACE_LOG": str(workspace_log),
            "FAKE_GENERIC_ATTEMPT_FILE": str(attempt_file),
        }
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(
            ["--json", "workflow", "wait", wf_id, "--timeout", "10"], env_extra=env
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})

        workspaces = workspace_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(workspaces), 2)
        self.assertEqual(workspaces[0], workspaces[1])
        self.assertFalse(Path(workspaces[0]).exists())
        index = json.loads(
            (self.workspace / ".delegate" / "index.json").read_text(encoding="utf-8")
        )
        owners = []
        for run in json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)[
            "runs"
        ]:
            run_id = run["runId"]
            manifest = json.loads(
                (self.workspace / ".delegate" / "runs" / run_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            descriptor = manifest.get("temporaryWorkspaceCleanup")
            if descriptor is None:
                self.assertNotIn("temporaryWorkspaceCleanup", index["runs"][run_id])
                continue
            owners.append(run_id)
            self.assertEqual(index["runs"][run_id]["temporaryWorkspaceCleanup"], descriptor)
            self.assertEqual(descriptor["isolatedWorkspace"], workspaces[0])
        self.assertEqual(len(owners), 1)

    def test_structured_retry_recovery_reads_manifest_before_first_snapshot(self) -> None:
        root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")
        isolated_workspace = self.workspace / "isolated"
        temp_base = self.workspace / "temp-base"
        descriptor = {
            "gitRoot": None,
            "isolatedWorkspace": str(isolated_workspace),
            "tempBase": str(temp_base),
            "sourceRoot": str(self.workspace),
        }
        run_id, _alias = run_registry.register_run(
            root,
            harness="droid",
            metadata={
                "group": "wf-manifest-only",
                "workflowAgentKey": "agent",
                "executionCwd": str(isolated_workspace),
            },
        )
        run_path = run_registry.run_directory(root, run_id)
        run_registry.write_json_atomic(
            run_path / run_registry.MANIFEST_FILE,
            {
                "runId": run_id,
                "group": "wf-manifest-only",
                "workflowAgentKey": "agent",
                "executionCwd": str(isolated_workspace),
                "temporaryWorkspaceCleanup": descriptor,
                "isolationBackend": "copy",
            },
        )

        recovered = workflow_runtime._workflow_agent_run_result_metadata(
            self.workspace,
            "wf-manifest-only",
            "agent",
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.workspace_cleanup, descriptor)
        self.assertEqual(recovered.execution_cwd, str(isolated_workspace))

    def test_invalid_run_id_cleanup_is_skipped_but_valid_manifest_run_is_cleaned(self) -> None:
        root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")
        descriptor = {
            "gitRoot": None,
            "isolatedWorkspace": str(self.workspace / "isolated"),
            "tempBase": str(self.workspace / "temp-base"),
            "sourceRoot": str(self.workspace),
        }
        run_id, _alias = run_registry.register_run(
            root,
            harness="droid",
            metadata={"group": "wf-cleanup", "workflowAgentKey": "agent"},
        )
        run_path = run_registry.run_directory(root, run_id)
        run_registry.write_json_atomic(
            run_path / run_registry.MANIFEST_FILE,
            {"temporaryWorkspaceCleanup": descriptor},
        )

        with mock.patch.object(workflow_runtime, "_cleanup_structured_retry_workspace") as cleanup:
            workflow_runtime._cleanup_workflow_agent_run_workspace(self.workspace, "malformed")
            cleanup.assert_not_called()
            workflow_runtime._cleanup_workflow_agent_run_workspace(self.workspace, run_id)
            cleanup.assert_called_once_with(descriptor)

    def test_structured_retry_timeout_exhaustion_reaps_every_workspace(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        # A workflow pin changes the child process context.  Keep a legacy/base
        # Codex capability record while its context-keyed cache has only a
        # different context, exercising the request-build selection boundary.
        discovery = harness_discovery.empty_snapshot()
        discovery["harnesses"] = {
            "codex": {
                "installed": True,
                "selector": [str(self.bin_dir / "codex")],
                "version": "codex-cli 1.0.0",
                "probeStatus": "ok",
                "modelScope": "account",
                "defaultModel": None,
                "models": {},
                "harnessReasoning": None,
                "warnings": [],
            }
        }
        harness_discovery.write_discovery_cache(None, discovery, home=self.home)
        harness_discovery.write_discovery_cache(
            None,
            discovery,
            home=self.home,
            env={"PATH": "/unrelated-context", "TMPDIR": "/unrelated-context"},
        )
        script = self.write_workflow(
            """
            meta = {"name": "schema-timeout", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False}
            return agent("timeout structured", schema=SCHEMA, retries=1, timeout=1.5, isolation="worktree")
            """
        )
        # The resume-vs-relaunch assertion needs the fake child's thread.started
        # line captured BEFORE the timeout kill; interpreter startup on a loaded
        # macOS box blows through a 0.2s window, so give it real margin.
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={
                "FAKE_CODEX_SLEEP_SECONDS": "5",
                "FAKE_CODEX_SESSION_ID": "timeout-thread",
                "FAKE_CODEX_SESSION_BEFORE_SLEEP": "1",
            },
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(
            ["--json", "workflow", "wait", wf_id, "--timeout", "15"],
            env_extra={
                "FAKE_CODEX_SLEEP_SECONDS": "5",
                "FAKE_CODEX_SESSION_ID": "timeout-thread",
                "FAKE_CODEX_SESSION_BEFORE_SLEEP": "1",
            },
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertIsNone(result["result"])
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        retry_events = [event for event in events if event["type"] == "agent_structured_retry"]
        self.assertTrue(retry_events)
        self.assertEqual(retry_events[0]["strategy"], "resume")
        self.assertEqual(retry_events[0]["sessionId"], "timeout-thread")
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 2)
        worktree_list = subprocess.run(
            ["git", "-C", str(self.workspace), "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(worktree_list.count("worktree "), 1, worktree_list)
        self.assertTrue(
            all(
                not Path(run["executionCwd"]).exists()
                for run in runs
                if isinstance(run.get("executionCwd"), str)
            )
        )

    def test_structured_retry_workflow_kill_reaps_workspace(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        script = self.write_workflow(
            """
            meta = {"name": "schema-kill", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False}
            return agent("hold for workflow kill", schema=SCHEMA, retries=1, timeout=30, isolation="worktree")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={
                "FAKE_CODEX_ATTEMPT_FILE": str(self.workspace / "kill-attempts.txt"),
                "FAKE_CODEX_SLEEP_RETRY_SECONDS": "30",
            },
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        runs = self.wait_for_group_runs(wf_id, count=2)
        self.assertEqual(len(runs), 2)
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.assertTrue(json.loads(killed.stdout)["cancelled"])
        worktree_list = subprocess.run(
            ["git", "-C", str(self.workspace), "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(worktree_list.count("worktree "), 1, worktree_list)
        self.assertFalse(Path(runs[0]["executionCwd"]).exists())

    def test_structured_retry_supervisor_resume_reaps_stale_workspace(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        script = self.write_workflow(
            """
            meta = {"name": "schema-supervisor-crash", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False}
            return agent("supervisor crash", schema=SCHEMA, retries=0, timeout=30, isolation="worktree")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "30"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        launched = json.loads(launch.stdout)
        wf_id = launched["wfId"]
        runs = self.wait_for_group_runs(wf_id)
        old_workspace = Path(runs[0]["executionCwd"])
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        os.kill(int(status["supervisorPid"]), 9)
        workflow_root = self.workspace / ".delegate" / "workflows" / wf_id
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                fd = workflow_registry.acquire_workflow_lock(workflow_root)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            os.close(fd)
            break
        else:
            self.fail("supervisor did not release workflow lock")
        Path(status["scriptPath"]).write_text(
            textwrap.dedent(
                """
                meta = {"name": "schema-supervisor-crash", "defaults": {"engine": "codex", "mode": "safe"}}
                SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False}
                return agent("supervisor crash", schema=SCHEMA, retries=0, timeout=1.5, isolation="worktree")
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        if (workflow_root / "result.json").exists():
            (workflow_root / "result.json").unlink()
        resumed = self.run_delegate(
            ["--json", "workflow", "run", "--resume", wf_id],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "5"},
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(
            ["--json", "workflow", "wait", wf_id, "--timeout", "15"],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "5"},
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertIsNone(result["result"])
        self.assertFalse(old_workspace.exists())
        worktree_list = subprocess.run(
            ["git", "-C", str(self.workspace), "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(worktree_list.count("worktree "), 1, worktree_list)

    def test_structured_output_with_non_codex_schema_uses_assistant_text(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "schema", "defaults": {"engine": "droid", "model": "gemini", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})

    def test_describe_and_help_include_workflows(self) -> None:
        describe = self.run_delegate(["--json", "describe", "--full"])
        self.assertEqual(describe.returncode, 0, describe.stderr)
        payload = json.loads(describe.stdout)
        self.assertIn("workflows", payload)
        self.assertTrue(
            payload["workflows"]["dsl"]["agent"]["signature"].endswith("allow_repo_persona=False)")
        )
        help_result = self.run_delegate(["--json", "help", "workflow"], workspace_option=False)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertEqual(json.loads(help_result.stdout)["command"], "workflow")

    def test_resume_ignores_synthesized_report_and_respawns_for_invalid_assistant(self) -> None:
        # A synthesized report is diagnostics, never the structured child result.
        script = self.write_workflow(
            """
            meta = {"name": "adopt-schema", "defaults": {"engine": "codex", "mode": "safe"}}
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            return agent("structured", schema=SCHEMA, label="schema-adopt")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        root = self.workspace / ".delegate" / "workflows" / wf_id
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        kept = [
            event
            for event in events
            if event["type"] not in {"agent_finished", "workflow_finished"}
        ]
        journal.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in kept),
            encoding="utf-8",
        )
        (root / "result.json").unlink()
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 1)
        run_id = runs[0]["runId"]
        snapshot_path = self.workspace / ".delegate" / "runs" / run_id / "state.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["assistantText"] = '{"ok": "wrong"}'
        snapshot["completionReportSource"] = "delegate_synthesized"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], {"ok": True, "value": "structured"})
        runs_after = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)[
            "runs"
        ]
        self.assertGreaterEqual(len(runs_after), 2)
        events_after = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        self.assertNotIn("agent_adopted", {event["type"] for event in events_after})

    def test_call_mode_workflow_child_registers_in_workspace_group(self) -> None:
        # R2: grouped call-mode children appear in the workspace registry.
        script = self.write_workflow(
            """
            meta = {"name": "call-group", "defaults": {"engine": "codex"}}
            return agent("one-hop", mode="call")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["group"], wf_id)
        snapshot = json.loads(self.run_delegate(["--json", "snapshot", runs[0]["alias"]]).stdout)
        self.assertEqual(snapshot["mode"], "call")
        self.assertIn("delegate-call-", snapshot["executionCwd"])
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertIn("fake completion", json.loads(result.stdout)["result"])

    def test_grouped_call_child_materializes_devin_agent_config(self) -> None:
        # Merge-interaction regression: the grouped-call execute_tracked branch
        # must pass agent_config_text through, or devin read-only children get
        # a literal placeholder path and die at launch.
        script = self.write_workflow(
            """
            meta = {"name": "devin-call", "defaults": {"engine": "devin"}}
            return agent("one-hop", mode="call")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertIn("fake devin completion", json.loads(result.stdout)["result"])
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(runs[0]["status"], "succeeded")

    def test_workflow_kill_cancels_in_flight_call_child(self) -> None:
        # R2: workflow kill fans out to in-flight call-mode children.
        script = self.write_workflow(
            """
            meta = {"name": "call-kill", "defaults": {"engine": "codex"}}
            return agent("very slow", mode="call")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "10"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id)
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.assertTrue(json.loads(killed.stdout)["cancelled"])
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "killed")

    def test_resume_after_kill_respawns_failed_child(self) -> None:
        # R3: failed/cancelled children are not definitive — resume respawns.
        script = self.write_workflow(
            """
            meta = {"name": "kill-resume", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("hold for cancel", label="respawn-me")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "10"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id)
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        self.assertEqual(killed.returncode, 0, killed.stderr)
        root = self.workspace / ".delegate" / "workflows" / wf_id
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        # Keep agent_started so resume treats the key as started-without-result.
        kept = [
            event
            for event in events
            if event["type"]
            not in {"agent_finished", "workflow_finished", "workflow_killed", "workflow_failed"}
        ]
        journal.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in kept),
            encoding="utf-8",
        )
        if (root / "result.json").exists():
            (root / "result.json").unlink()
        runs_before = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)[
            "runs"
        ]
        self.assertGreaterEqual(len(runs_before), 1)

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"]).returncode,
            0,
        )
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(json.loads(result.stdout)["result"], "fake completion")
        runs_after = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)[
            "runs"
        ]
        self.assertGreater(len(runs_after), len(runs_before))

    def test_durable_workflow_events_are_fsynced(self) -> None:
        # R4 / F5: durable adoption, audit, and budget-claim events are fsynced.
        self.assertIn("agent_started", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_adopted", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_adopt_rejected", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_timeout", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_retry", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_structured_retry", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_structured_exhausted", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("budget", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("item_parked", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("item_unparked", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertNotIn("agent_result", workflow_registry.DURABLE_EVENT_TYPES)
        journal = self.workspace / "fsync-journal.jsonl"
        fsynced: list[int] = []
        original_fsync = os.fsync

        def tracking_fsync(fd: int) -> None:
            fsynced.append(fd)
            original_fsync(fd)

        original = os.fsync
        os.fsync = tracking_fsync  # type: ignore[assignment]
        events = [
            {"seq": 1, "type": "agent_started", "key": "k"},
            {"seq": 2, "type": "log", "message": "x"},
            {"seq": 3, "type": "agent_finished", "key": "k", "result": "ok"},
            {"seq": 4, "type": "agent_adopted", "key": "k"},
            {"seq": 5, "type": "agent_adopt_rejected", "key": "k"},
            {"seq": 6, "type": "agent_timeout", "key": "k"},
            {"seq": 7, "type": "budget", "key": "k", "spent": 1},
            {"seq": 8, "type": "agent_retry", "key": "k"},
            {"seq": 9, "type": "agent_structured_retry", "key": "k"},
            {"seq": 10, "type": "agent_structured_exhausted", "key": "k"},
            {"seq": 11, "type": "item_parked", "name": "parked"},
            {"seq": 12, "type": "item_unparked", "name": "parked"},
        ]
        try:
            for event in events:
                workflow_registry.append_jsonl(journal, event)
        finally:
            os.fsync = original  # type: ignore[assignment]
        expected = sum(event["type"] in workflow_registry.DURABLE_EVENT_TYPES for event in events)
        self.assertEqual(len(fsynced), expected)

    def test_workflow_replay_ignores_simulated_dry_run_events(self) -> None:
        from delegate_agent.workflows import runtime as workflow_runtime

        root = self.workspace / "simulated-replay"
        root.mkdir()
        events = [
            {"seq": 1, "type": "budget", "key": "simulated", "simulated": True},
            {"seq": 2, "type": "agent_started", "key": "simulated", "simulated": True},
            {
                "seq": 3,
                "type": "agent_finished",
                "key": "simulated",
                "result": "",
                "simulated": True,
            },
            {"seq": 4, "type": "budget", "key": "live"},
            {"seq": 5, "type": "agent_started", "key": "live"},
            {"seq": 6, "type": "agent_finished", "key": "live", "result": "done"},
        ]
        for event in events:
            workflow_registry.append_jsonl(root / workflow_registry.JOURNAL_FILE, event)

        state = workflow_runtime.WorkflowState(
            wf_id="simulated-replay",
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(2),
        )

        self.assertEqual(state.sequence, 6)
        self.assertEqual(state.claimed_keys, {"live"})
        self.assertEqual(state.replay_keys, {"live"})
        self.assertEqual(state.replay, {"live": "done"})
        self.assertEqual(state.budget.spent(), 1)

    def test_legacy_dry_run_marker_variants_cannot_create_replay_authority(self) -> None:
        root = self.workspace / "legacy-marker-replay"
        root.mkdir()
        events = [
            {"seq": 1, "type": "budget", "key": "dry-budget", "dryRun": True},
            {
                "seq": 2,
                "type": "agent_started",
                "key": "dry-start",
                "label": "dry-label",
                "dryRun": True,
            },
            {
                "seq": 3,
                "type": "agent_child",
                "key": "dry-child",
                "workflowAgentKey": "dry-child",
                "runId": "dry-run",
                "engine": "codex",
                "label": "dry-child-label",
                "dryRun": True,
            },
            {
                "seq": 4,
                "type": "item_parked",
                "name": "dry-item",
                "scope": "root/soft-park/dry-item",
                "dryRun": True,
            },
            {
                "seq": 5,
                "type": "agent_finished",
                "key": "dry-finish",
                "label": "dry-finish-label",
                "result": "stub",
                "simulated": True,
            },
            {"seq": 6, "type": "agent_rejected", "key": "dry-finish", "reason": "legacy"},
            {"seq": 7, "type": "budget", "key": "live"},
            {
                "seq": 8,
                "type": "agent_child",
                "key": "live",
                "workflowAgentKey": "live",
                "runId": "live-run",
                "engine": "codex",
                "label": "live-label",
            },
            {
                "seq": 9,
                "type": "agent_started",
                "key": "live",
                "label": "live-label",
                "scope": "root/live",
            },
            {"seq": 10, "type": "agent_finished", "key": "live", "result": "real"},
            {
                "seq": 11,
                "type": "agent_child",
                "workflowAgentKey": "live-child-only",
                "runId": "live-child-run",
                "engine": "codex",
                "label": "live-child-label",
            },
            {
                "seq": 12,
                "type": "agent_rejected",
                "key": "live-child-only",
                "reason": "retry",
            },
            {
                "seq": 13,
                "type": "agent_child",
                "workflowAgentKey": "dry-child-only",
                "runId": "dry-child-only-run",
                "engine": "codex",
                "dryRun": True,
            },
            {"seq": 14, "type": "agent_rejected", "key": "dry-child-only", "reason": "legacy"},
        ]
        for event in events:
            workflow_registry.append_jsonl(root / workflow_registry.JOURNAL_FILE, event)

        state = workflow_runtime.WorkflowState(
            wf_id="legacy-marker-replay",
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )

        self.assertEqual(state.claimed_keys, {"live"})
        self.assertEqual(state.replay, {"live": "real"})
        self.assertEqual(state.label_keys, {"live-label": "live"})
        self.assertEqual(state.started_scopes, {"live": "root/live"})
        self.assertEqual(state.tombstoned_keys, {"live-child-only"})
        self.assertEqual(state.soft_parked_items, {})

    def test_live_rejection_survives_simulated_history_in_both_replay_modes(self) -> None:
        for replay_journal in (True, False):
            with self.subTest(replay_journal=replay_journal):
                root = self.workspace / f"mixed-live-reject-{replay_journal}"
                root.mkdir()
                events = [
                    {"seq": 1, "type": "agent_finished", "key": "live", "result": "real"},
                    {
                        "seq": 2,
                        "type": "agent_finished",
                        "key": "live",
                        "result": "stub",
                        "simulated": True,
                    },
                    {"seq": 3, "type": "agent_rejected", "key": "live", "reason": "retry"},
                ]
                for event in events:
                    workflow_registry.append_jsonl(root / workflow_registry.JOURNAL_FILE, event)
                state = workflow_runtime.WorkflowState(
                    wf_id=f"mixed-live-reject-{replay_journal}",
                    workspace=self.workspace,
                    root=root,
                    script_path=root / workflow_registry.SCRIPT_FILE,
                    config=json.loads(self.config_path.read_text(encoding="utf-8")),
                    cli_argv=[sys.executable, str(CLI)],
                    args=None,
                    budget=workflow_runtime.Budget(None),
                    replay_journal=replay_journal,
                )
                self.assertEqual(state.replay, {})
                self.assertEqual(state.tombstoned_keys, {"live"})

    def test_dry_run_rejection_and_custom_events_are_simulated_for_live_resume(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-reject", "defaults": {"engine": "codex", "mode": "safe"}}
            value = agent("one", label="one")
            reject("one", "retry")
            log("ordinary custom event")
            return value
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        events = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        rejection = next(event for event in events if event["type"] == "agent_rejected")
        self.assertTrue(rejection["simulated"])
        self.assertTrue(next(event for event in events if event["type"] == "log")["simulated"])

        dry_state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
            dry_run=True,
        )
        ordinary = dry_state.append_event("custom", key="custom", simulated=False)
        durable = dry_state.append_durable_event("custom_durable", key="durable", simulated=False)
        dry_state.append_journal_only("custom_journal", simulated=False)
        dry_state.append_event("gate", key="gate", result={"ok": True}, simulated=False)
        self.assertTrue(ordinary["simulated"])
        self.assertTrue(durable["simulated"])

        live_state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.assertEqual(live_state.replay, {})
        self.assertEqual(live_state.replay_keys, set())
        self.assertEqual(live_state.claimed_keys, set())
        self.assertEqual(live_state.started_without_result, set())
        self.assertEqual(live_state.tombstoned_keys, set())
        self.assertEqual(live_state.budget.spent(), 0)
        self.assertIsNone(live_state.latest_gate_event())
        self.assertIsNone(workflow_commands._latest_unapproved_gate_event(root))
        live_event = live_state.append_event("live_custom", simulated=False)
        self.assertFalse(live_event["simulated"])
        all_events = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        journal_event = next(event for event in all_events if event["type"] == "custom_journal")
        self.assertTrue(journal_event["simulated"])

    def test_cli_reject_refuses_dry_only_key_but_allows_live_key(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-reject-cli", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one")
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / workflow_registry.JOURNAL_FILE
        before = journal.read_bytes()
        dry_key = next(
            json.loads(line)["key"]
            for line in before.decode(encoding="utf-8").splitlines()
            if json.loads(line).get("type") == "agent_started"
        )

        refused = self.run_delegate(
            ["--json", "workflow", "reject", wf_id, dry_key, "--reason", "retry"]
        )
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(json.loads(refused.stdout)["error"], "workflow_reject_unresolved")
        self.assertEqual(journal.read_bytes(), before)

        # Old dry runs left an unmarked rejection after simulated agent rows.
        # That diagnostic residue is not a real agent the operator can reject.
        sequence = max(
            json.loads(line)["seq"] for line in before.decode(encoding="utf-8").splitlines()
        )
        workflow_registry.append_jsonl(
            journal, {"seq": sequence + 1, "type": "agent_rejected", "key": dry_key}
        )
        before = journal.read_bytes()
        legacy_refused = self.run_delegate(
            ["--json", "workflow", "reject", wf_id, dry_key, "--reason", "retry"]
        )
        self.assertEqual(legacy_refused.returncode, 2)
        self.assertEqual(json.loads(legacy_refused.stdout)["error"], "workflow_reject_unresolved")
        self.assertEqual(journal.read_bytes(), before)

        live_key = "live-control"
        sequence = max(
            json.loads(line)["seq"] for line in before.decode(encoding="utf-8").splitlines()
        )
        for event in (
            {"type": "budget", "key": live_key},
            {"type": "agent_started", "key": live_key, "label": "live-control"},
            {"type": "agent_finished", "key": live_key, "result": "real"},
        ):
            sequence += 1
            workflow_registry.append_jsonl(journal, {"seq": sequence, **event})
        accepted = self.run_delegate(
            ["--json", "workflow", "reject", wf_id, live_key, "--reason", "retry"]
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        rejection = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rejection["type"], "agent_rejected")
        self.assertEqual(rejection["key"], live_key)
        self.assertNotIn("simulated", rejection)

    def test_dry_run_kill_then_resume_does_not_replay_simulated_authority(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "dry-kill-resume", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one")
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        self.assertEqual(killed.returncode, 0, killed.stderr)
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)

        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        events = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        live_budgets = [event for event in events if event["type"] == "budget"]
        self.assertEqual(len(live_budgets), 2)
        self.assertEqual(sum(event.get("simulated") is True for event in live_budgets), 1)
        self.assertEqual(sum(event.get("simulated") is not True for event in live_budgets), 1)
        self.assertEqual(
            sum(event["type"] == "agent_retry" for event in events),
            0,
        )
        self.assertEqual(sum(event["type"] == "gate" for event in events), 0)
        self.assertFalse((root / workflow_registry.APPROVAL_FILE).exists())

    def test_workflow_stubbed_preserves_requested_gate_and_marks_dry_run(self) -> None:
        self.write_saved_workflow(
            "stub-child",
            """
            meta = {"name": "child", "defaults": {"engine": "codex", "mode": "safe"}}
            return "child"
            """,
        )
        script = self.write_workflow(
            """
            meta = {"name": "stub", "defaults": {"engine": "codex", "mode": "safe"}}
            return workflow("stub-child", gate=True)
            """
        )
        dry = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wf_id = json.loads(dry.stdout)["wfId"]
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        events = [
            json.loads(line)
            for line in (root / workflow_registry.JOURNAL_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        stub = next(event for event in events if event["type"] == "workflow_stubbed")
        self.assertTrue(stub["simulated"])
        self.assertTrue(stub["dryRun"])
        self.assertIs(stub["gate"], True)

    def test_resume_budget_override_does_not_mutate_while_locked(self) -> None:
        # R5: lock before budget mutation on resume.
        script = self.write_workflow(
            """
            meta = {"name": "lock-budget", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("very slow")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script), "--budget", "3"])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id)
        before = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        resumed = self.run_delegate(
            ["--json", "workflow", "run", "--resume", wf_id, "--budget", "99"]
        )
        self.assertNotEqual(resumed.returncode, 0)
        self.assertEqual(json.loads(resumed.stdout)["error"], "workflow_locked")
        after = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(after["budget"]["total"], before["budget"]["total"])
        self.assertEqual(after["budget"]["total"], 3)
        self.run_delegate(["--json", "workflow", "kill", wf_id])

    def test_workflow_kill_waits_for_held_lock_before_merging_status(self) -> None:
        # R6 / F3: kill must wait on the workflow flock (or escalate) before
        # merging status=killed; supervisorExited must be explicit, not defaulted.
        script = self.write_workflow(
            """
            meta = {"name": "kill-status", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("very slow")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "30"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        # Kill the live supervisor so its flock drops, then re-hold the lock
        # from the test to simulate a slow-dying supervisor during kill.
        os.kill(int(status["supervisorPid"]), 9)
        root = self.workspace / ".delegate" / "workflows" / wf_id
        deadline = time.monotonic() + 5
        lock_fd = -1
        while time.monotonic() < deadline:
            try:
                lock_fd = os.open(root / "workflow.lock", os.O_CREAT | os.O_RDWR)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if lock_fd >= 0:
                    os.close(lock_fd)
                    lock_fd = -1
                time.sleep(0.05)
        else:
            self.fail("could not acquire workflow lock after supervisor death")
        self.addCleanup(lambda: os.close(lock_fd) if lock_fd >= 0 else None)
        started = time.monotonic()
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        elapsed = time.monotonic() - started
        self.assertEqual(killed.returncode, 0, killed.stderr)
        # Bounded wait (5s) + force wait (2s) before merge while lock is held.
        self.assertGreaterEqual(elapsed, 5.0)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "killed")
        self.assertIn("supervisorExited", status)
        self.assertIs(status["supervisorExited"], False)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        time.sleep(0.3)
        status_later = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status_later["status"], "killed")

    def test_gate_pause_does_not_spawn_pipeline_tail(self) -> None:
        # R7 / F4: after GateExit, unspawned pipeline items must not claim budget.
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["workflows"]["itemThreads"] = 1
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        child = self.write_saved_workflow(
            "gate-early-child",
            """
            meta = {"name": "gate-early-child"}
            return None
            """,
        )
        script = self.write_workflow(
            f"""
            meta = {{"name": "gate-early", "defaults": {{"engine": "codex", "mode": "safe"}}}}
            def stage(value, item, i):
                if i == 0:
                    return workflow({child!r}, gate=True)
                return agent(f"tail-{{i}}")
            return pipeline(list(range(12)), stage)
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "paused")
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        started = [event for event in events if event["type"] == "agent_started"]
        self.assertEqual(len(started), 0)
        budget_events = [event for event in events if event["type"] == "budget"]
        self.assertEqual(len(budget_events), 0)
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 0)

    def test_resume_does_not_double_claim_budget_for_respawned_keys(self) -> None:
        # F1: journaled budget claims are idempotent per structural key on resume.
        script = self.write_workflow(
            """
            meta = {"name": "budget-idempotent", "defaults": {"engine": "codex", "mode": "safe"}}
            return parallel([
                lambda: agent("very slow a"),
                lambda: agent("very slow b"),
            ])
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--budget", "2"],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "30"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id, count=2)
        status_mid = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status_mid["budget"]["spent"], 2)
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        self.assertEqual(killed.returncode, 0, killed.stderr)
        root = self.workspace / ".delegate" / "workflows" / wf_id
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        kept = [
            event
            for event in events
            if event["type"]
            not in {"agent_finished", "workflow_finished", "workflow_killed", "workflow_failed"}
        ]
        journal.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in kept),
            encoding="utf-8",
        )
        if (root / "result.json").exists():
            (root / "result.json").unlink()
        # Budget covers each key once; a double-claim on resume would exceed it.
        resumed = self.run_delegate(
            ["--json", "workflow", "run", "--resume", wf_id, "--budget", "2"]
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "30"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["budget"]["spent"], 2)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(
            json.loads(result.stdout)["result"],
            ["fake completion", "fake completion"],
        )

    def test_resume_reseeds_spent_budget_from_durable_journal(self) -> None:
        # Round-4: budget events are fsynced but status.json is not, so after a
        # hard crash status can lag the journal; resume must trust the journal.
        script = self.write_workflow(
            """
            meta = {"name": "budget-reseed", "defaults": {"engine": "codex", "mode": "safe"}}
            return parallel([
                lambda: agent("very slow a"),
                lambda: agent("very slow b"),
            ])
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--budget", "2"],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "30"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id, count=2)
        killed = self.run_delegate(["--json", "workflow", "kill", wf_id])
        self.assertEqual(killed.returncode, 0, killed.stderr)
        root = self.workspace / ".delegate" / "workflows" / wf_id
        journal = root / "journal.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        kept = [
            event
            for event in events
            if event["type"]
            not in {"agent_finished", "workflow_finished", "workflow_killed", "workflow_failed"}
        ]
        journal.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in kept),
            encoding="utf-8",
        )
        # Simulate the non-durable status write being lost in the crash while
        # the fsynced journal (two budget claims) survived.
        status_path = root / workflow_registry.STATUS_FILE
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["budget"] = {"total": 2, "spent": 0}
        status_path.write_text(json.dumps(status, sort_keys=True), encoding="utf-8")
        if (root / "result.json").exists():
            (root / "result.json").unlink()
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status_after = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status_after["status"], "succeeded")
        self.assertEqual(status_after["budget"]["spent"], 2)

    def test_adoption_timeout_recheck_adopts_child_completed_during_wait(self) -> None:
        # Round-4: a child that finishes between the adoption-wait deadline and
        # the cancel must be adopted, not discarded as a timeout None.
        script = self.write_workflow(
            """
            meta = {"name": "adopt-race", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("one", label="race-me")
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        root = self.workspace / ".delegate" / "workflows" / wf_id
        from unittest import mock

        from delegate_agent.workflows import runtime as workflow_runtime

        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {}})
        key = next(iter(state.replay_keys))
        # Simulate: run looked non-terminal at entry, the wait timed out, and
        # the run turned terminal in the race window before the cancel.
        with (
            mock.patch.object(
                workflow_runtime, "_workflow_run_terminal", side_effect=[False, True]
            ),
            mock.patch.object(workflow_runtime, "_wait_for_workflow_agent_run", return_value=False),
            mock.patch.object(workflow_runtime, "cancel_workflow_agent_child") as cancel_mock,
        ):
            adopted = dsl._adopt_existing_agent_run(
                key,
                scope="root/seq#0",
                label="race-me",
                phase=None,
                schema=None,
                prefer_assistant=False,
                timeout=1,
            )
        self.assertEqual(adopted, "fake completion")
        cancel_mock.assert_not_called()
        events = [
            json.loads(line)
            for line in (root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertNotIn("agent_timeout", {event["type"] for event in events})

    def test_schema_output_recursion_error_degrades_to_rejection_and_retry_events(self) -> None:
        """Child parse failures outside schema errors stay inside the supervisor boundary."""
        from delegate_agent.workflows import runtime as workflow_runtime

        root = self.workspace / "workflow-boundary"
        root.mkdir()
        state = workflow_runtime.WorkflowState(
            wf_id="workflow-boundary",
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.write_status("running")
        dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {}})
        schema = {"type": "object"}

        with (
            mock.patch.object(
                workflow_runtime.workflow_schema,
                "parse_json_tolerant",
                side_effect=RecursionError("pathological child JSON"),
            ),
            mock.patch.object(workflow_runtime, "_find_workflow_agent_run", return_value="child"),
            mock.patch.object(workflow_runtime, "_workflow_run_terminal", return_value=True),
            mock.patch.object(
                workflow_runtime, "_workflow_agent_run_result", return_value="[" * 5000
            ),
            mock.patch.object(dsl, "_run_delegate", return_value="[" * 5000),
        ):
            adopted = dsl._adopt_existing_agent_run(
                "root/seq#0",
                scope="root/seq#0",
                label=None,
                phase=None,
                schema=schema,
                prefer_assistant=False,
                timeout=None,
            )
            retry_result = dsl._run_structured_or_text(
                "codex",
                "pathological child JSON",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation=None,
                passthrough=False,
                timeout=None,
                retries=0,
                key="root/seq#1",
            )

        self.assertIs(adopted, workflow_runtime._MISSING)
        self.assertIsNone(retry_result)
        event_types = {
            event["type"]
            for event in (
                json.loads(line)
                for line in state.journal_path.read_text(encoding="utf-8").splitlines()
            )
        }
        self.assertTrue({"agent_adopt_rejected", "agent_structured_retry"} <= event_types)

    def test_structured_retry_resumes_same_native_session(self) -> None:
        root = self.workspace / "workflow-native-resume"
        root.mkdir()
        state = workflow_runtime.WorkflowState(
            wf_id="workflow-native-resume",
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.write_status("running")
        dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {}})
        schema = {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        }
        first = workflow_runtime._DelegateChildResult(
            text='{"ok": "wrong"}',
            run_id="child-1",
            execution_cwd="/worktrees/child-1",
            session_id="thread-1",
        )
        second = workflow_runtime._DelegateChildResult(
            text='{"ok": "still wrong"}',
            run_id="child-2",
            execution_cwd="/worktrees/child-1",
            session_id="thread-1",
        )
        third = workflow_runtime._DelegateChildResult(
            text='{"ok": true}',
            run_id="child-3",
            execution_cwd="/worktrees/child-1",
            session_id="thread-1",
        )
        with mock.patch.object(
            dsl, "_run_delegate", side_effect=[first, second, third]
        ) as run_mock:
            result = dsl._run_structured_or_text(
                "codex",
                "review the entire repository",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation="worktree",
                passthrough=False,
                timeout=None,
                retries=2,
                key="stable-agent-key",
            )

        self.assertEqual(result, {"ok": True})
        self.assertTrue(run_mock.call_args_list[0].kwargs["preserve_retry_workspace"])
        self.assertTrue(run_mock.call_args_list[1].kwargs["preserve_retry_workspace"])
        retry = run_mock.call_args_list[1]
        self.assertEqual(retry.kwargs["structured_retry_run_id"], "child-1")
        self.assertEqual(retry.kwargs["resume_session_id"], "thread-1")
        self.assertEqual(retry.kwargs["workflow_agent_key"], "stable-agent-key")
        self.assertIn("Re-emit the StructuredOutput now", retry.args[1])
        self.assertNotIn("review the entire repository", retry.args[1])
        self.assertNotIn('{"ok": "wrong"}', retry.args[1])
        final_retry = run_mock.call_args_list[2]
        self.assertEqual(final_retry.kwargs["structured_retry_run_id"], "child-1")
        self.assertEqual(final_retry.kwargs["resume_session_id"], "thread-1")
        events = [json.loads(line) for line in state.journal_path.read_text().splitlines()]
        journal = next(event for event in events if event["type"] == "agent_structured_retry")
        self.assertEqual(journal["strategy"], "resume")
        self.assertEqual(journal["sessionId"], "thread-1")

    def test_unstructured_retry_attaches_to_failed_worktree(self) -> None:
        """A schema-less timeout must retain the prior worktree for retry."""
        root = self.workspace / "workflow-text-retry"
        root.mkdir()
        state = workflow_runtime.WorkflowState(
            wf_id="workflow-text-retry",
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config={},
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.write_status("running")
        dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {}})
        timed_out = workflow_runtime._DelegateChildResult(
            text=None,
            run_id="del_20260827T000000Z_abcdef",
            execution_cwd="/worktrees/dirty-child",
            session_id=None,
            outcome=workflow_runtime.ChildAttemptOutcome(
                run_id="del_20260827T000000Z_abcdef",
                failure_reason="timeout",
                worktree="/worktrees/dirty-child",
                execution_cwd="/worktrees/dirty-child",
            ),
        )
        retried = workflow_runtime._DelegateChildResult(
            text="kept dirty worktree",
            run_id="del_20260827T000001Z_abcdef",
            execution_cwd="/worktrees/dirty-child",
            session_id=None,
        )
        with mock.patch.object(dsl, "_run_delegate", side_effect=[timed_out, retried]) as run_mock:
            result = dsl._run_structured_or_text(
                "codex",
                "implement in the worktree",
                mode="work",
                model=None,
                effort=None,
                fast=None,
                schema=None,
                isolation="worktree",
                passthrough=False,
                timeout=1,
                retries=1,
                key="stable-agent-key",
            )

        self.assertEqual(result, "kept dirty worktree")
        self.assertTrue(run_mock.call_args_list[0].kwargs["preserve_retry_workspace"])
        self.assertEqual(
            run_mock.call_args_list[1].kwargs["structured_retry_run_id"],
            "del_20260827T000000Z_abcdef",
        )
        self.assertEqual(run_mock.call_args_list[1].kwargs["isolation"], "worktree")

    def test_structured_retry_preserves_bwrap_backend_on_retry(self) -> None:
        root = self.workspace / "workflow-bwrap-retry"
        root.mkdir()
        state = workflow_runtime.WorkflowState(
            wf_id="workflow-bwrap-retry",
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.write_status("running")
        dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {}})
        schema = {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        }
        first = workflow_runtime._DelegateChildResult(
            text='{"ok": "wrong"}',
            run_id="del_20260826T165700Z_bwrap1",
            execution_cwd=str(self.workspace),
            session_id=None,
            isolation_backend="bwrap",
        )
        second = workflow_runtime._DelegateChildResult(
            text='{"ok": true}',
            run_id="del_20260826T165701Z_bwrap2",
            execution_cwd=str(self.workspace),
            session_id=None,
            isolation_backend="bwrap",
        )
        with mock.patch.object(dsl, "_run_delegate", side_effect=[first, second]) as run_mock:
            result = dsl._run_structured_or_text(
                "codex",
                "review safely",
                mode="safe",
                model=None,
                effort=None,
                fast=None,
                schema=schema,
                isolation="worktree",
                passthrough=False,
                timeout=None,
                retries=1,
                key="bwrap-agent",
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(run_mock.call_args_list[1].kwargs["structured_retry_backend"], "bwrap")

        source = self.workspace / "bwrap-source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / ".gitignore").write_text("secret.env\nignored-dir/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", ".gitignore"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        (source / "secret.env").write_text("hidden\n", encoding="utf-8")
        (source / "ignored-dir").mkdir()
        (source / "ignored-dir" / "nested.txt").write_text("hidden\n", encoding="utf-8")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["isolation"] = {
            "safeBackend": "bwrap",
            "bwrapBinds": [{"path": str(self.home), "mode": "ro"}],
        }
        workspace = ResolvedWorkspace(str(source), "git")
        initial_context = build_isolation_context(
            source_workspace=str(source),
            resolved_isolation="worktree",
            engine="codex",
            mode="safe",
            source_git_root=str(source),
            config=config,
        )
        initial_request = request_build.build_request(
            "codex",
            "safe",
            None,
            workspace,
            "review safely",
            config,
            False,
            isolation_context=initial_context,
        )
        run_registry_root = run_registry.ensure_registry(source, workspace_kind="git")
        source_run_id, _source_alias = run_registry.register_run(
            run_registry_root,
            harness="codex",
            metadata={
                "engine": "codex",
                "group": "wf-bwrap-context",
                "workflowAgentKey": "bwrap-context-agent",
                "executionCwd": str(source),
            },
        )
        source_run_path = run_registry.run_directory(run_registry_root, source_run_id)
        run_registry.write_json_atomic(
            source_run_path / run_registry.MANIFEST_FILE,
            {
                "runId": source_run_id,
                "engine": "codex",
                "group": "wf-bwrap-context",
                "workflowAgentKey": "bwrap-context-agent",
                "executionCwd": str(source),
                "workspaceKind": "git",
                "isolationLifecycle": "temporary",
                "isolationBackend": "bwrap",
            },
        )
        run_registry.write_json_atomic(
            source_run_path / run_registry.SNAPSHOT_FILE,
            {"runId": source_run_id, "status": "succeeded"},
        )
        input_path = self.workspace / "bwrap-retry.json"
        input_path.write_text(
            json.dumps(
                {
                    "engine": "codex",
                    "mode": "safe",
                    "prompt": "retry safely",
                    "cwd": str(source),
                    "isolation": "worktree",
                    "workflowAgentKey": "bwrap-context-agent",
                    "structuredRetryWorkspace": True,
                    "structuredRetryRunId": source_run_id,
                    "structuredRetryBackend": "bwrap",
                }
            ),
            encoding="utf-8",
        )
        parsed = ParsedCommand(
            "run",
            global_options=GlobalOptions(json_mode=True, group="wf-bwrap-context"),
            payload=RunJsonOptions(str(input_path)),
        )
        retry_request = request_build.request_from_input_json(
            parsed,
            config,
            workspace=workspace,
        )
        with (
            mock.patch.object(
                safe_workspace, "ensure_bwrap_backend", return_value="/usr/bin/bwrap"
            ),
            mock.patch.object(safe_workspace, "_ensure_no_bwrap_symlink_leaks"),
            safe_workspace.safe_isolated_request(
                initial_request,
                config=config,
                env={"DELEGATE_SAFE_BACKEND": "bwrap"},
            ) as initial_isolated,
            safe_workspace.safe_isolated_request(
                retry_request,
                config=config,
                env={"DELEGATE_SAFE_BACKEND": "bwrap"},
            ) as retry_isolated,
        ):
            initial_sandbox = initial_isolated.isolation_context.sandbox
            retry_sandbox = retry_isolated.isolation_context.sandbox

        self.assertEqual(retry_sandbox, initial_sandbox)
        self.assertIsNotNone(retry_sandbox)
        masks = retry_sandbox.masks
        ro_binds = [bind.path for bind in retry_sandbox.binds if bind.mode == "ro"]
        sandbox_argv = sandbox_bwrap.wrap_engine_argv(
            engine_argv=["/bin/true"],
            cwd=str(source),
            env={"HOME": str(self.home), "PATH": os.environ.get("PATH", "")},
            engine="codex",
            scratch_dir=str(self.workspace / "scratch"),
            masks=masks,
            extra_ro_roots=ro_binds,
            bwrap_path="/usr/bin/bwrap",
        )
        self.assertIn(["--ro-bind", str(source), str(source)], _argv_pairs(sandbox_argv))
        self.assertIn(["--tmpfs", "/tmp"], _argv_pairs(sandbox_argv))
        self.assertIn(["--tmpfs", str(self.home)], _argv_pairs(sandbox_argv))
        self.assertIn(
            ["--ro-bind", "/dev/null", str(source / "secret.env")],
            _argv_pairs(sandbox_argv),
        )
        self.assertIn(["--tmpfs", str(source / "ignored-dir")], _argv_pairs(sandbox_argv))

    def test_structured_retry_relaunches_in_previous_workspace_without_handle(self) -> None:
        root = self.workspace / "workflow-relaunch"
        root.mkdir()
        state = workflow_runtime.WorkflowState(
            wf_id="workflow-relaunch",
            workspace=self.workspace,
            root=root,
            script_path=root / workflow_registry.SCRIPT_FILE,
            config=json.loads(self.config_path.read_text(encoding="utf-8")),
            cli_argv=[sys.executable, str(CLI)],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        state.write_status("running")
        dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {}})
        schema = {"type": "object"}
        first = workflow_runtime._DelegateChildResult(
            text="not json",
            run_id="child-1",
            execution_cwd="/worktrees/child-1",
            session_id=None,
        )
        second = workflow_runtime._DelegateChildResult(
            text="{}",
            run_id="child-2",
            execution_cwd="/worktrees/child-1",
            session_id=None,
        )
        with mock.patch.object(dsl, "_run_delegate", side_effect=[first, second]) as run_mock:
            result = dsl._run_structured_or_text(
                "droid",
                "do the work",
                mode="work",
                model="gemini",
                effort=None,
                fast=None,
                schema=schema,
                isolation="worktree",
                passthrough=False,
                timeout=None,
                retries=1,
                key="stable-agent-key",
            )

        self.assertEqual(result, {})
        retry = run_mock.call_args_list[1]
        self.assertEqual(retry.kwargs["structured_retry_run_id"], "child-1")
        self.assertIsNone(retry.kwargs["resume_session_id"])
        self.assertEqual(retry.kwargs["workflow_agent_key"], "stable-agent-key")
        self.assertIn("do the work", retry.args[1])
        self.assertIn("not json", retry.args[1])
        events = [json.loads(line) for line in state.journal_path.read_text().splitlines()]
        journal = next(event for event in events if event["type"] == "agent_structured_retry")
        self.assertEqual(journal["strategy"], "relaunch")
        self.assertNotIn("sessionId", journal)

    def test_structured_retry_in_persistent_worktree_carries_attachment(self) -> None:
        root = self.workspace / "workflow-attachment"
        root.mkdir()
        source = self.workspace / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Delegate Tests",
                "-c",
                "user.email=delegate-tests@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        registry_root = run_registry.ensure_registry(source, workspace_kind="git")
        worktree = self.workspace / "prior-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "-q",
                str(worktree),
                "-b",
                "delegate/codex-prior",
            ],
            check=True,
        )
        source_run_id, source_alias = run_registry.register_run(
            registry_root,
            harness="codex",
            metadata={
                "engine": "codex",
                "group": "workflow-attachment",
                "workflowAgentKey": "stable-agent-key",
            },
        )
        source_run_path = run_registry.run_directory(registry_root, source_run_id)
        run_registry.write_json_atomic(
            source_run_path / run_registry.MANIFEST_FILE,
            {
                "runId": source_run_id,
                "alias": source_alias,
                "engine": "codex",
                "group": "workflow-attachment",
                "workflowAgentKey": "stable-agent-key",
                "executionCwd": str(worktree),
                "workspaceKind": "git",
                "isolationLifecycle": "persistent",
                "sourceGitRoot": str(source),
                "branch": "delegate/codex-prior",
            },
        )
        run_registry.write_json_atomic(
            source_run_path / run_registry.SNAPSHOT_FILE,
            {"runId": source_run_id, "status": "succeeded"},
        )
        input_path = root / "retry.json"
        input_path.write_text(
            json.dumps(
                {
                    "engine": "codex",
                    "mode": "work",
                    "prompt": "retry",
                    "cwd": str(source),
                    "isolation": "worktree",
                    "workflowAgentKey": "stable-agent-key",
                    "structuredRetryWorkspace": True,
                    "structuredRetryRunId": source_run_id,
                }
            ),
            encoding="utf-8",
        )
        parsed = ParsedCommand(
            "run",
            global_options=GlobalOptions(json_mode=True, group="workflow-attachment"),
            payload=RunJsonOptions(str(input_path)),
        )
        request = request_build.request_from_input_json(
            parsed,
            {
                **json.loads(self.config_path.read_text(encoding="utf-8")),
                "codex": {
                    **request_build.delegate_config.DEFAULT_CONFIG["codex"],
                    "binary": str(self.bin_dir / "codex"),
                },
            },
            workspace=ResolvedWorkspace(str(source), "git"),
        )

        self.assertEqual(request.workspace, str(worktree.resolve()))
        self.assertIsNotNone(request.isolation_context)
        assert request.isolation_context is not None
        self.assertEqual(request.isolation_context.isolation_lifecycle, "attached")
        self.assertEqual(request.isolation_context.attachment["sourceRunId"], source_run_id)
        self.assertEqual(request.isolation_context.attachment["path"], str(worktree))

    def test_adoption_wait_timeout_cancels_child_without_duplicate(self) -> None:
        # F2: adoption wait timeout cancels the adopted run and returns None.
        script = self.write_workflow(
            """
            meta = {"name": "adopt-timeout", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("very slow", label="hold")
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "30"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        runs = self.wait_for_group_runs(wf_id)
        self.assertEqual(len(runs), 1)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        supervisor_pid = status["supervisorPid"]
        os.kill(int(supervisor_pid), 9)
        root = self.workspace / ".delegate" / "workflows" / wf_id
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                fd = workflow_registry.acquire_workflow_lock(root)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            os.close(fd)
            break
        else:
            self.fail("supervisor did not release workflow lock")
        # v2 timeout changes intentionally resolve a fresh key; the stale child
        # is cancelled and its temporary workspace is reaped before relaunch.
        Path(status["scriptPath"]).write_text(
            textwrap.dedent(
                """
                meta = {"name": "adopt-timeout", "defaults": {"engine": "codex", "mode": "safe"}}
                return agent("very slow", label="hold", timeout=1)
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        if (root / "result.json").exists():
            (root / "result.json").unlink()
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertIsNone(json.loads(result.stdout)["result"])
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        self.assertIn("agent_timeout", {event["type"] for event in events})
        runs_after = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)[
            "runs"
        ]
        self.assertEqual(len(runs_after), 2)
        snap = json.loads(self.run_delegate(["--json", "snapshot", runs_after[-1]["alias"]]).stdout)
        self.assertIn(snap.get("effectiveStatus") or snap.get("status"), {"cancelled", "failed"})

    def test_argv_transport_prompt_size_guard(self) -> None:
        # §2.4: kimi argv transport rejects oversized agent prompts (cursor and
        # omp moved to stdin and no longer carry the ARG_MAX ceiling).
        from delegate_agent.workflows.runtime import PROMPT_ARGV_GUARD_BYTES

        oversized = "x" * (PROMPT_ARGV_GUARD_BYTES + 1)
        script = self.write_workflow(
            f"""
            meta = {{"name": "argv-guard", "defaults": {{"engine": "kimi", "mode": "safe"}}}}
            return agent({oversized!r})
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertIsNone(json.loads(result.stdout)["result"])
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        failed = [event for event in events if event["type"] == "agent_failed"]
        self.assertTrue(failed)
        self.assertIn("stage output too large for kimi argv transport", failed[0]["error"])
        self.assertIn("route this stage to codex/claude/droid/opencode", failed[0]["error"])

    def test_judges_spawns_call_read_only_children_per_engine(self) -> None:
        # §2.4: judges() is one call --read-only child per engine; votes are parsed.
        script = self.write_workflow(
            """
            meta = {"name": "judges-panel"}
            SCHEMA = {
                "type": "object",
                "required": ["ok", "value"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "value": {"type": "string"},
                },
            }
            return judges("grade this", SCHEMA, engines=["codex", "cursor"])
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)], env_extra={"AI_PROFILE": "work"}
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"]).returncode,
            0,
        )
        votes = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)[
            "result"
        ]
        self.assertEqual(votes, [{"ok": True, "value": "structured"}] * 2)
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 2)
        harnesses = sorted(run["harness"] for run in runs)
        self.assertEqual(harnesses, ["codex", "cursor"])
        for run in runs:
            snap = json.loads(self.run_delegate(["--json", "snapshot", run["alias"]]).stdout)
            self.assertEqual(snap["mode"], "call")
            self.assertEqual(snap["authProfile"], "work")
            self.assertEqual(snap["promptInstructionMode"], "wrapped")
            manifest = json.loads(
                (self.workspace / ".delegate" / "runs" / run["runId"] / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsInstance(snap["workflowAgentKey"], str)
            self.assertEqual(manifest["workflowAgentKey"], snap["workflowAgentKey"])
            argv = manifest.get("argv") or []
            if run["harness"] == "codex":
                self.assertIn("--sandbox", argv)
                self.assertIn("read-only", argv)
            else:
                self.assertNotIn("--force", argv)
                self.assertNotIn("--approve-mcps", argv)
            self.assertNotIn("requestedReasoningEffort", manifest)

    def test_judges_plumbs_effort_and_per_judge_override(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "judges-effort"}
            SCHEMA = {
                "type": "object",
                "required": ["ok", "value"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "value": {"type": "string"},
                },
            }
            return judges(
                "grade this",
                SCHEMA,
                engines=[
                    "codex",
                    {"engine": "codex", "effort": "low"},
                ],
                effort="high",
            )
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)], env_extra={"AI_PROFILE": "work"}
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"]).returncode,
            0,
        )
        votes = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)[
            "result"
        ]
        self.assertEqual(votes, [{"ok": True, "value": "structured"}] * 2)
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        self.assertEqual(len(runs), 2)
        efforts = set()
        for run in runs:
            manifest = json.loads(
                (self.workspace / ".delegate" / "runs" / run["runId"] / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["reasoningEffortSource"], "input-json")
            efforts.add(manifest["requestedReasoningEffort"])
        self.assertEqual(efforts, {"high", "low"})

    def test_engine_caps_bound_concurrent_child_runs(self) -> None:
        # §2.5: workflows.engineCaps limits concurrent children for a fake engine.
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["workflows"]["engineCaps"] = {"codex": 1}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        overlap_log = self.workspace / "spawn-overlap.jsonl"
        script = self.write_workflow(
            """
            meta = {"name": "engine-caps", "defaults": {"engine": "codex", "mode": "safe"}}
            return parallel([
                lambda: agent("slow a"),
                lambda: agent("slow b"),
                lambda: agent("slow c"),
            ])
            """
        )
        # Patch fake codex to record concurrent occupancy while sleeping.
        path = self.bin_dir / "codex"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "prompt = sys.stdin.read()\n"
            "log = os.environ.get('FAKE_SPAWN_OVERLAP_LOG')\n"
            "sleep = float(os.environ.get('FAKE_CODEX_SLEEP_SECONDS') or '0.4')\n"
            "if log:\n"
            "    import fcntl\n"
            "    with open(log + '.lock', 'a+', encoding='utf-8') as lock:\n"
            "        fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "        active_path = log + '.active'\n"
            "        try:\n"
            "            active = int(open(active_path, encoding='utf-8').read() or '0')\n"
            "        except FileNotFoundError:\n"
            "            active = 0\n"
            "        active += 1\n"
            "        open(active_path, 'w', encoding='utf-8').write(str(active))\n"
            "        open(log, 'a', encoding='utf-8').write(json.dumps({'active': active}) + '\\n')\n"
            "        fcntl.flock(lock, fcntl.LOCK_UN)\n"
            "    time.sleep(sleep)\n"
            "    with open(log + '.lock', 'a+', encoding='utf-8') as lock:\n"
            "        fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "        active = int(open(active_path, encoding='utf-8').read()) - 1\n"
            "        open(active_path, 'w', encoding='utf-8').write(str(active))\n"
            "        fcntl.flock(lock, fcntl.LOCK_UN)\n"
            "else:\n"
            "    time.sleep(sleep)\n"
            "text = 'fake completion'\n"
            "print(json.dumps({'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}))\n"
            "print(json.dumps({'type':'completion','finalText':text}))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={
                "FAKE_CODEX_SLEEP_SECONDS": "0.5",
                "FAKE_SPAWN_OVERLAP_LOG": str(overlap_log),
            },
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"]).returncode,
            0,
        )
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)[
            "result"
        ]
        self.assertEqual(result, ["fake completion"] * 3)
        peaks = [
            json.loads(line)["active"]
            for line in overlap_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(peaks), 3)
        self.assertLessEqual(max(peaks), 1)

    def test_opencode_workflow_engine_and_engine_cap(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "opencode-dry", "defaults": {"engine": "opencode", "mode": "safe"}}
            return agent("review")
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["runTree"]["counts"], {"opencode:safe": 1})

        from delegate_agent.workflows import runtime as workflow_runtime

        semaphores = workflow_runtime._engine_semaphores(
            {"workflows": {"engineCaps": {"opencode": 1, "not-real": 1}}}
        )
        self.assertIn("opencode", semaphores)
        self.assertNotIn("not-real", semaphores)

    def test_opencode_engine_caps_bound_concurrent_child_runs(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["opencode"] = {"binary": str(self.bin_dir / "opencode")}
        config["workflows"]["engineCaps"] = {"opencode": 1}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        overlap_log = self.workspace / "opencode-spawn-overlap.jsonl"
        script = self.write_workflow(
            """
            meta = {"name": "opencode-engine-caps", "defaults": {"engine": "opencode", "mode": "safe"}}
            return parallel([
                lambda: agent("slow a"),
                lambda: agent("slow b"),
                lambda: agent("slow c"),
            ])
            """
        )
        path = self.bin_dir / "opencode"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "prompt = sys.stdin.read()\n"
            "log = os.environ.get('FAKE_SPAWN_OVERLAP_LOG')\n"
            "sleep = float(os.environ.get('FAKE_OPENCODE_SLEEP_SECONDS') or '0.4')\n"
            "if log:\n"
            "    import fcntl\n"
            "    with open(log + '.lock', 'a+', encoding='utf-8') as lock:\n"
            "        fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "        active_path = log + '.active'\n"
            "        try:\n"
            "            active = int(open(active_path, encoding='utf-8').read() or '0')\n"
            "        except FileNotFoundError:\n"
            "            active = 0\n"
            "        active += 1\n"
            "        open(active_path, 'w', encoding='utf-8').write(str(active))\n"
            "        open(log, 'a', encoding='utf-8').write(json.dumps({'active': active}) + '\\n')\n"
            "        fcntl.flock(lock, fcntl.LOCK_UN)\n"
            "    time.sleep(sleep)\n"
            "    with open(log + '.lock', 'a+', encoding='utf-8') as lock:\n"
            "        fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "        active = int(open(active_path, encoding='utf-8').read()) - 1\n"
            "        open(active_path, 'w', encoding='utf-8').write(str(active))\n"
            "        fcntl.flock(lock, fcntl.LOCK_UN)\n"
            "else:\n"
            "    time.sleep(sleep)\n"
            "text = 'fake completion'\n"
            "print(json.dumps({'type':'step_start','part':{'type':'step-start'}}))\n"
            "print(json.dumps({'type':'text','part':{'type':'text','text':text}}))\n"
            "print(json.dumps({'type':'step_finish','part':{'type':'step-finish','reason':'stop'}}))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script)],
            env_extra={
                "FAKE_OPENCODE_SLEEP_SECONDS": "0.5",
                "FAKE_SPAWN_OVERLAP_LOG": str(overlap_log),
            },
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"]).returncode,
            0,
        )
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)[
            "result"
        ]
        self.assertEqual(result, ["fake completion"] * 3)
        peaks = [
            json.loads(line)["active"]
            for line in overlap_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(peaks), 3)
        self.assertLessEqual(max(peaks), 1)

    def test_workflow_save_list_watch_and_approve_not_gated(self) -> None:
        # CLI verbs: save → run --name; list shows saved + workspace; watch --json;
        # approve on a non-gated workflow → workflow_not_gated.
        source = self.write_workflow(
            """
            meta = {"name": "saved-cli", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("from-saved")
            """
        )
        saved = self.run_delegate(
            ["--json", "workflow", "save", str(source), "--name", "saved-cli"]
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        saved_payload = json.loads(saved.stdout)
        self.assertEqual(saved_payload["name"], "saved-cli")
        self.assertTrue(Path(saved_payload["path"]).is_file())

        launch = self.run_delegate(["--json", "workflow", "run", "--name", "saved-cli"])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        listed = self.run_delegate(["--json", "workflow", "list"])
        self.assertEqual(listed.returncode, 0, listed.stderr)
        list_payload = json.loads(listed.stdout)
        self.assertIn("saved-cli", list_payload["saved"])
        self.assertTrue(any(item["wfId"] == wf_id for item in list_payload["workflows"]))

        watched = self.run_delegate(["--json", "workflow", "watch", wf_id])
        self.assertEqual(watched.returncode, 0, watched.stderr)
        watch_payload = json.loads(watched.stdout)
        self.assertTrue(watch_payload["ok"])
        self.assertGreaterEqual(len(watch_payload["events"]), 1)

        approve = self.run_delegate(["--json", "workflow", "approve", wf_id])
        self.assertNotEqual(approve.returncode, 0)
        self.assertEqual(json.loads(approve.stdout)["error"], "workflow_not_gated")

    def _seed_running_workflow(self, wf_id: str = "wf_aaaaaaaaaaaa") -> Path:
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        workflow_registry.write_status(
            root,
            {
                "ok": True,
                "wfId": wf_id,
                "status": "running",
                "workspace": str(self.workspace),
                "scriptPath": str(root / "script.py"),
                "journalPath": str(root / "journal.jsonl"),
                "resultPath": str(root / "result.json"),
            },
        )
        (root / "script.py").write_text(
            'meta = {"name": "seeded"}\nreturn None\n', encoding="utf-8"
        )
        # Real post-crash state: the lock file exists but nobody holds the flock.
        (root / workflow_registry.LOCK_FILE).touch()
        return root

    def _seed_completed_workflow(
        self,
        wf_id: str,
        *,
        created_at: str,
        result: object = None,
        with_result: bool = True,
    ) -> Path:
        root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
        workflow_registry.register_workflow(
            self.workspace,
            root,
            {
                "ok": True,
                "wfId": wf_id,
                "status": "succeeded",
                "createdAt": created_at,
                "workspace": str(self.workspace),
                "scriptPath": str(root / "script.py"),
                "journalPath": str(root / "journal.jsonl"),
                "resultPath": str(root / "result.json"),
            },
        )
        if with_result:
            workflow_registry.write_result(root, {"ok": True, "wfId": wf_id, "result": result})
        return root

    def test_creation_ordinals_ignore_same_second_ids_and_survive_status_writes(self) -> None:
        first_id = "wf_ffffffffffff"
        second_id = "wf_000000000000"
        with mock.patch.object(
            workflow_registry.run_registry,
            "utc_now_iso",
            return_value="2026-01-01T00:00:00Z",
        ):
            first = workflow_registry.ensure_workflow_dir(self.workspace, first_id)
            workflow_registry.register_workflow(
                self.workspace,
                first,
                {"wfId": first_id, "status": "created"},
            )
            second = workflow_registry.ensure_workflow_dir(self.workspace, second_id)
            workflow_registry.register_workflow(
                self.workspace,
                second,
                {"wfId": second_id, "status": "created"},
            )

        first_status = workflow_registry.read_json(first / workflow_registry.STATUS_FILE) or {}
        second_status = workflow_registry.read_json(second / workflow_registry.STATUS_FILE) or {}
        self.assertEqual(first_status["createdOrdinal"], 1)
        self.assertEqual(second_status["createdOrdinal"], 2)

        first_status.update({"status": "running", "createdOrdinal": 99})
        workflow_registry.write_status(first, first_status)
        self.assertEqual(
            workflow_registry.read_json(first / workflow_registry.STATUS_FILE)["createdOrdinal"],
            1,
        )
        second_status["status"] = "succeeded"
        workflow_registry.write_status(second, second_status)
        workflow_registry.write_result(second, {"ok": True, "wfId": second_id, "result": "new"})
        latest = self.run_delegate(["--json", "workflow", "result"])
        self.assertEqual(latest.returncode, 0, latest.stderr)
        self.assertEqual(json.loads(latest.stdout)["wfId"], second_id)

        unregistered = workflow_registry.ensure_workflow_dir(self.workspace, "wf_111111111111")
        workflow_registry.write_status(unregistered, {"status": "running", "createdOrdinal": 100})
        self.assertNotIn(
            "createdOrdinal",
            workflow_registry.read_json(unregistered / workflow_registry.STATUS_FILE),
        )

    def test_creation_ordinal_survives_resume(self) -> None:
        script = self.write_workflow('meta = {"name": "ordinal-resume"}\nreturn None')
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        status_path = (
            workflow_registry.workflow_dir(self.workspace, wf_id) / workflow_registry.STATUS_FILE
        )
        ordinal = workflow_registry.read_json(status_path)["createdOrdinal"]

        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        status = workflow_registry.read_json(status_path)
        self.assertEqual(status["createdOrdinal"], ordinal)

    def test_latest_wait_excludes_dry_runs_only(self) -> None:
        old_id = "wf_111111111111"
        latest_id = "wf_222222222222"
        self._seed_completed_workflow(old_id, created_at="2026-01-01T00:00:00Z", result="old")
        self._seed_completed_workflow(latest_id, created_at="2026-01-02T00:00:00Z", result="latest")
        dry_run = self.run_delegate(
            [
                "--json",
                "workflow",
                "run",
                str(self.write_workflow('meta = {"name": "dry-latest"}\nreturn agent("dry")')),
                "--dry-run",
            ]
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        dry_id = json.loads(dry_run.stdout)["wfId"]
        dry_status = workflow_registry.read_json(
            workflow_registry.workflow_dir(self.workspace, dry_id) / workflow_registry.STATUS_FILE
        )
        self.assertIsInstance(dry_status.get("createdAt"), str)
        self.assertIsInstance(dry_status.get("createdOrdinal"), int)
        self.assertEqual(dry_status["status"], "dry_run")

        latest = self.run_delegate(["--json", "workflow", "wait", "--timeout", "1"])
        self.assertEqual(latest.returncode, 0, latest.stderr)
        latest_payload = json.loads(latest.stdout)
        self.assertEqual(latest_payload["wfId"], latest_id)
        self.assertEqual(latest_payload["resolutionKind"], "latest")
        self.assertEqual(latest_payload["workflow"]["status"], "succeeded")

        explicit = self.run_delegate(["--json", "workflow", "wait", latest_id, "--timeout", "1"])
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        explicit_payload = json.loads(explicit.stdout)
        self.assertNotIn("wfId", explicit_payload)
        self.assertNotIn("resolutionKind", explicit_payload)

        explicit_dry = self.run_delegate(["--json", "workflow", "wait", dry_id, "--timeout", "5"])
        self.assertEqual(explicit_dry.returncode, 1, explicit_dry.stderr)
        explicit_dry_payload = json.loads(explicit_dry.stdout)
        self.assertFalse(explicit_dry_payload["timedOut"])
        self.assertEqual(explicit_dry_payload["workflow"]["status"], "dry_run")

    def test_latest_wait_surfaces_orphaned_newest_created_workflow_as_stalled(self) -> None:
        completed_id = "wf_222222222222"
        self._seed_completed_workflow(
            completed_id,
            created_at="2099-01-01T00:00:00Z",
            result="completed",
        )
        created_id = "wf_111111111111"
        created = workflow_registry.ensure_workflow_dir(self.workspace, created_id)
        workflow_registry.register_workflow(
            self.workspace,
            created,
            {
                "ok": True,
                "wfId": created_id,
                "status": "created",
                "workspace": str(self.workspace),
            },
        )

        latest = self.run_delegate(["--json", "workflow", "wait", "--timeout", "5"])
        self.assertEqual(latest.returncode, 1, latest.stderr)
        payload = json.loads(latest.stdout)
        self.assertEqual(payload["wfId"], created_id)
        self.assertEqual(payload["workflow"]["status"], "stalled")
        self.assertEqual(payload["workflow"]["statusOnDisk"], "created")

    def test_latest_workflow_ignores_legacy_unversioned_status(self) -> None:
        legacy = workflow_registry.ensure_workflow_dir(self.workspace, "wf_aaaaaaaaaaaa")
        workflow_registry.write_status(
            legacy,
            {"wfId": legacy.name, "status": "succeeded", "createdAt": "2099-01-01"},
        )
        current = workflow_registry.ensure_workflow_dir(self.workspace, "wf_bbbbbbbbbbbb")
        workflow_registry.register_workflow(
            self.workspace,
            current,
            {"wfId": current.name, "status": "succeeded", "createdAt": "2026-01-01"},
        )
        latest = workflow_registry.latest_workflow_dir(self.workspace)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.name, current.name)

    def test_latest_result_uses_immutable_created_at_and_requires_result_file(self) -> None:
        old_id = "wf_444444444444"
        latest_id = "wf_555555555555"
        no_result_id = "wf_666666666666"
        old = self._seed_completed_workflow(
            old_id, created_at="2026-01-01T00:00:00Z", result={"value": "old"}
        )
        self._seed_completed_workflow(
            latest_id, created_at="2026-01-02T00:00:00Z", result={"value": "latest"}
        )
        self._seed_completed_workflow(
            no_result_id,
            created_at="2026-01-03T00:00:00Z",
            with_result=False,
        )
        resumed = workflow_registry.read_json(old / workflow_registry.STATUS_FILE) or {}
        resumed.update({"status": "running", "createdAt": "2099-01-01T00:00:00Z"})
        workflow_registry.write_status(old, resumed)
        self.assertEqual(
            workflow_registry.read_json(old / workflow_registry.STATUS_FILE)["createdAt"],
            "2026-01-01T00:00:00Z",
        )

        latest = self.run_delegate(["--json", "workflow", "result"])
        self.assertEqual(latest.returncode, 0, latest.stderr)
        latest_payload = json.loads(latest.stdout)
        self.assertEqual(latest_payload["wfId"], latest_id)
        self.assertEqual(latest_payload["resolutionKind"], "latest")
        self.assertEqual(latest_payload["result"], {"value": "latest"})

        explicit = self.run_delegate(["--json", "workflow", "result", latest_id])
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        explicit_payload = json.loads(explicit.stdout)
        self.assertNotIn("resolutionKind", explicit_payload)
        self.assertEqual(explicit_payload["result"], {"value": "latest"})

    def test_workflow_result_field_outputs_raw_text_and_json_envelope(self) -> None:
        wf_id = "wf_777777777777"
        self._seed_completed_workflow(
            wf_id,
            created_at="2026-01-01T00:00:00Z",
            result={"message": "hello", "count": 2},
        )

        text_result = self.run_delegate(["workflow", "result", wf_id, "--field", "message"])
        self.assertEqual(text_result.returncode, 0, text_result.stderr)
        self.assertEqual(text_result.stdout, "hello\n")

        json_result = self.run_delegate(["--json", "workflow", "result", wf_id, "--field", "count"])
        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        self.assertEqual(
            json.loads(json_result.stdout),
            {
                "ok": True,
                "schema": "delegate.workflow-command.v1",
                "wfId": wf_id,
                "field": "count",
                "value": 2,
            },
        )

        latest = self.run_delegate(["--json", "workflow", "result", "--field", "message"])
        self.assertEqual(latest.returncode, 0, latest.stderr)
        self.assertEqual(json.loads(latest.stdout)["resolutionKind"], "latest")

        missing = self.run_delegate(["--json", "workflow", "result", wf_id, "--field", "nope"])
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(json.loads(missing.stdout)["error"], "workflow_result_field_missing")

        scalar_id = "wf_888888888888"
        self._seed_completed_workflow(scalar_id, created_at="2026-01-02T00:00:00Z", result="scalar")
        non_object = self.run_delegate(
            ["--json", "workflow", "result", scalar_id, "--field", "message"]
        )
        self.assertNotEqual(non_object.returncode, 0)
        self.assertEqual(json.loads(non_object.stdout)["error"], "workflow_result_not_object")

    def test_workflow_result_field_rejects_missing_empty_or_option_shaped_keys(self) -> None:
        for args in (
            ["--json", "workflow", "result", "--field"],
            ["--json", "workflow", "result", "--field", ""],
            ["--json", "workflow", "result", "--field", "--timeout"],
        ):
            with self.subTest(args=args):
                result = self.run_delegate(args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    json.loads(result.stdout)["error"], "missing_workflow_result_field"
                )

    def test_status_reports_stalled_when_running_without_lock(self) -> None:
        wf_id = "wf_bbbbbbbbbbbb"
        self._seed_running_workflow(wf_id)
        status = self.run_delegate(["--json", "workflow", "status", wf_id])
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "stalled")
        self.assertEqual(payload["statusOnDisk"], "running")
        on_disk = json.loads(
            (self.workspace / ".delegate" / "workflows" / wf_id / "status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(on_disk["status"], "running")
        listed = json.loads(self.run_delegate(["--json", "workflow", "list"]).stdout)
        entry = next(item for item in listed["workflows"] if item["wfId"] == wf_id)
        self.assertEqual(entry["status"], "stalled")
        self.assertEqual(entry["statusOnDisk"], "running")

    def test_status_reports_running_when_lock_held(self) -> None:
        wf_id = "wf_cccccccccccc"
        root = self._seed_running_workflow(wf_id)
        lock_fd = workflow_registry.acquire_workflow_lock(root)
        self.addCleanup(lambda: os.close(lock_fd))
        status = self.run_delegate(["--json", "workflow", "status", wf_id])
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "running")
        self.assertNotIn("statusOnDisk", payload)

    def test_wait_exits_promptly_on_stalled_supervisor(self) -> None:
        wf_id = "wf_dddddddddddd"
        self._seed_running_workflow(wf_id)
        started = time.monotonic()
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "5"])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, f"wait hung for {elapsed:.2f}s on stalled workflow")
        self.assertNotEqual(waited.returncode, 0)
        self.assertNotEqual(waited.returncode, 124)
        payload = json.loads(waited.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload.get("timedOut", False))
        self.assertEqual(payload["workflow"]["status"], "stalled")
        self.assertEqual(payload["workflow"]["statusOnDisk"], "running")
        human = self.run_delegate(["workflow", "wait", wf_id, "--timeout", "5"])
        self.assertIn("stalled", human.stdout.lower())
        self.assertIn("resume", human.stdout.lower())

    def test_workflow_failure_includes_traceback(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "boom-traceback"}
            missing = {}
            return missing["files"]
            """
        )
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        failed = [event for event in events if event["type"] == "workflow_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("traceback", failed[0])
        self.assertIn("KeyError", failed[0]["traceback"])
        self.assertIn("files", failed[0]["traceback"])
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertIn("traceback", result)
        self.assertIn("KeyError", result["traceback"])
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "failed")
        self.assertIn("traceback", status)
        self.assertIn("KeyError", status["traceback"])

    def test_current_agent_replay_key_identity(self) -> None:
        opts_without_resumable = {
            "engine": "codex",
            "mode": "work",
            "model": None,
            "effort": None,
            "fast": None,
            "schema": None,
            "isolation": None,
            "personaDigest": None,
        }
        key_plain = workflow_runtime._agent_key("root/seq#0", "do it", opts_without_resumable)
        # Current-format replay keys must remain byte-identical.
        self.assertEqual(
            key_plain, "5ffe349954477cad66dacd8d7dd3d3980149fef64755786db058a6e18eaad4a8"
        )

        opts_with_resumable = dict(opts_without_resumable)
        opts_with_resumable["resumable"] = True
        key_resumable = workflow_runtime._agent_key("root/seq#0", "do it", opts_with_resumable)
        self.assertNotEqual(key_plain, key_resumable)

        followup_key = workflow_runtime._followup_key("root/seq#1", "fix-r1", "do it", {})
        self.assertTrue(followup_key.isalnum())
        self.assertNotEqual(followup_key, key_plain)
        self.assertNotEqual(followup_key, key_resumable)

    def test_followup_happy_path_with_resumable_agent(self) -> None:
        argv_log = self.home / "codex_argv.jsonl"
        script = self.write_workflow(
            """
            meta = {"name": "followup-happy", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("do it", label="fix-r1", resumable=True)
            r2 = followup("fix-r1", "round 2 findings", label="fix-r2")
            return {"r1": r1, "r2": r2}
            """
        )
        env_extra = {
            "FAKE_CODEX_ARGV_LOG": str(argv_log),
            "FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345",
        }
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env_extra)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "succeeded")
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertEqual(result["result"], {"r1": "fake completion", "r2": "round 2 output"})

        lines = [
            json.loads(line) for line in argv_log.read_text(encoding="utf-8").strip().splitlines()
        ]
        self.assertEqual(len(lines), 2)
        r1_argv, r2_argv = lines[0], lines[1]
        self.assertNotIn("--ephemeral", r1_argv)
        self.assertNotIn("resume", r1_argv)

        self.assertNotIn("--ephemeral", r2_argv)
        self.assertIn("exec", r2_argv)
        exec_idx = r2_argv.index("exec")
        # Sandbox/config flags may sit between `exec` and the `resume`
        # subcommand; the real codex parser accepts that (probe-verified).
        self.assertGreater(r2_argv.index("resume"), exec_idx)
        self.assertIn("th_wf_fixed_12345", r2_argv)

    def test_followup_timeout_rounds_up_for_cli(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "followup-timeout", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("do it", label="fix-r1", resumable=True)
            r2 = followup("fix-r1", "round 2 findings", timeout=0.9)
            return {"r1": r1, "r2": r2}
            """
        )
        env_extra = {"FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env_extra)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        runs = json.loads(self.run_delegate(["--json", "runs", "--group", wf_id]).stdout)["runs"]
        manifests = []
        for run in runs:
            manifest_path = self.workspace / ".delegate" / "runs" / run["runId"] / "manifest.json"
            if manifest_path.is_file():
                manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
        manifest = next(item for item in manifests if item.get("timeoutSeconds"))
        self.assertEqual(manifest["timeoutSeconds"], 1)

    def test_resume_replays_followup_after_first_resumable_agent(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "followup-replay", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("do it", label="fix-r1", resumable=True)
            r2 = followup("fix-r1", "round 2 findings", label="fix-r2")
            return {"r1": r1, "r2": r2}
            """
        )
        env_extra = {"FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env_extra)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / workflow_registry.JOURNAL_FILE
        events = workflow_registry.iter_journal(journal)
        first_key = next(
            index
            for index, event in enumerate(events)
            if event.get("type") == "agent_child" and event.get("label") == "fix-r1"
        )
        first_key = events[first_key]["key"]
        first_finished = next(
            index
            for index, event in enumerate(events)
            if event.get("type") == "agent_finished" and event.get("key") == first_key
        )
        journal.write_text(
            "".join(json.dumps(event) + "\n" for event in events[: first_finished + 1]),
            encoding="utf-8",
        )
        (root / workflow_registry.RESULT_FILE).unlink()

        resumed = self.run_delegate(
            ["--json", "workflow", "run", "--resume", wf_id], env_extra=env_extra
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        events_after = workflow_registry.iter_journal(journal)
        children = [event for event in events_after if event.get("type") == "agent_child"]
        self.assertEqual([event.get("label") for event in children], ["fix-r1", "fix-r2"])
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(
            json.loads(result.stdout)["result"],
            {"r1": "fake completion", "r2": "round 2 output"},
        )

    def test_resume_replays_labeled_followup_chain_without_duplicate_children(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "followup-chain-replay", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("step 1", label="turn-1", resumable=True)
            r2 = followup("turn-1", "step 2", label="turn-2")
            r3 = followup("turn-2", "step 3", label="turn-3")
            return {"r1": r1, "r2": r2, "r3": r3}
            """
        )
        env_extra = {"FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env_extra)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        root = workflow_registry.workflow_dir(self.workspace, wf_id)
        journal = root / workflow_registry.JOURNAL_FILE
        events = workflow_registry.iter_journal(journal)
        second_key = next(
            index
            for index, event in enumerate(events)
            if event.get("type") == "agent_child" and event.get("label") == "turn-2"
        )
        second_key = events[second_key]["key"]
        second_finished = next(
            index
            for index, event in enumerate(events)
            if event.get("type") == "agent_finished" and event.get("key") == second_key
        )
        journal.write_text(
            "".join(json.dumps(event) + "\n" for event in events[: second_finished + 1]),
            encoding="utf-8",
        )
        (root / workflow_registry.RESULT_FILE).unlink()

        resumed = self.run_delegate(
            ["--json", "workflow", "run", "--resume", wf_id], env_extra=env_extra
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        events_after = workflow_registry.iter_journal(journal)
        children = [event for event in events_after if event.get("type") == "agent_child"]
        labels = [event.get("label") for event in children]
        self.assertEqual(labels, ["turn-1", "turn-2", "turn-3"])
        self.assertEqual(len(labels), len(set(labels)))

    def test_followup_fails_on_unknown_prior_label(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "followup-unknown", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("do it", label="fix-r1", resumable=True)
            r2 = followup("nope", "round 2 findings")
            return {"r1": r1, "r2": r2}
            """
        )
        env_extra = {"FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env_extra)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "failed")
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertIn("unknown or incomplete prior child label 'nope'", result["traceback"])
        self.assertIn("fix-r1", result["traceback"])

    def test_followup_fails_when_prior_child_not_resumable(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "followup-not-resumable", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("do it", label="fix-r1", resumable=False)
            r2 = followup("fix-r1", "round 2 findings")
            return {"r1": r1, "r2": r2}
            """
        )
        env_extra = {"FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345"}
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env_extra)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "failed")
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertIn(
            "prior child with label 'fix-r1' was not launched with resumable=True",
            result["traceback"],
        )
        self.assertIn("add resumable=True to the prior agent() call", result["traceback"])

    def test_followup_dry_run_tree_and_simulated_budget(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "followup-dry-run", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("do it", label="fix-r1", resumable=True)
            r2 = followup("fix-r1", "round 2 findings", label="fix-r2")
            return {"r1": r1, "r2": r2}
            """
        )
        dry_run = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--dry-run", "--budget", "5"]
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        payload = json.loads(dry_run.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dryRun"])
        run_tree = payload["runTree"]
        calls = run_tree["calls"]
        self.assertEqual(len(calls), 2)

        # Call 0: agent with resumable=True
        self.assertEqual(calls[0]["scope"], "root/seq#0")
        self.assertEqual(calls[0]["label"], "fix-r1")
        self.assertTrue(calls[0].get("resumable"))

        # Call 1: followup primitive
        self.assertEqual(calls[1]["scope"], "root/seq#1")
        self.assertEqual(calls[1]["primitive"], "followup")
        self.assertEqual(calls[1]["priorLabel"], "fix-r1")
        self.assertEqual(calls[1]["label"], "fix-r2")

        # Check simulated budget events
        wf_id = payload["wfId"]
        events = json.loads(
            self.run_delegate(["--json", "workflow", "events", wf_id, "--since", "0"]).stdout
        )["events"]
        budget_events = [e for e in events if e.get("type") == "budget"]
        self.assertEqual(len(budget_events), 2)
        self.assertEqual(budget_events[-1]["spent"], 2)

    def test_followup_budget_exceeded_fails_workflow(self) -> None:
        script = self.write_workflow(
            """
            meta = {"name": "followup-budget", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("do it", label="fix-r1", resumable=True)
            r2 = followup("fix-r1", "round 2 findings", label="fix-r2")
            return {"r1": r1, "r2": r2}
            """
        )
        env_extra = {"FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345"}
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--budget", "1"],
            env_extra=env_extra,
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "failed")
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertIn("BudgetExceeded", result["traceback"])

    def test_resumable_and_followup_validation_checks(self) -> None:
        # Mode call with resumable=True
        script_call = self.write_workflow(
            """
            meta = {"name": "invalid-call-resumable"}
            return agent("do it", mode="call", resumable=True)
            """
        )
        check_call = self.run_delegate(["--json", "workflow", "check", str(script_call)])
        self.assertNotEqual(check_call.returncode, 0)
        self.assertIn("call mode", json.loads(check_call.stdout)["message"])

        # Unsupported engine with resumable=True
        script_droid = self.write_workflow(
            """
            meta = {"name": "invalid-droid-resumable"}
            return agent("do it", engine="droid", resumable=True)
            """
        )
        check_droid = self.run_delegate(["--json", "workflow", "check", str(script_droid)])
        self.assertNotEqual(check_droid.returncode, 0)
        self.assertIn(
            "only supported by codex and claude", json.loads(check_droid.stdout)["message"]
        )

        script_safe = self.write_workflow(
            """
            meta = {"name": "invalid-safe-resumable"}
            return agent("do it", engine="codex", mode="safe", resumable=True)
            """
        )
        check_safe = self.run_delegate(["--json", "workflow", "check", str(script_safe)])
        self.assertNotEqual(check_safe.returncode, 0)
        self.assertIn("safe workspaces are temporary", json.loads(check_safe.stdout)["message"])

        # Duplicate label ambiguity
        script_dup = self.write_workflow(
            """
            meta = {"name": "dup-label", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("one", label="dup", resumable=True)
            r2 = agent("two", label="dup", resumable=True)
            r3 = followup("dup", "three")
            return {"r1": r1, "r2": r2, "r3": r3}
            """
        )
        env_extra = {"FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345"}
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script_dup)], env_extra=env_extra
        )
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertNotEqual(waited.returncode, 0)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "failed")
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertIn("duplicate completed children with label 'dup'", result["traceback"])

    def test_followup_structured_output_and_chaining(self) -> None:
        argv_log = self.home / "codex_chain_argv.jsonl"
        script = self.write_workflow(
            """
            meta = {"name": "followup-chain", "defaults": {"engine": "codex", "mode": "work"}}
            r1 = agent("step 1", label="turn-1", resumable=True)
            r2 = followup("turn-1", "step 2", label="turn-2")
            SCHEMA = {"type": "object", "required": ["ok", "value"], "properties": {"ok": {"type": "boolean"}, "value": {"type": "string"}}, "additionalProperties": False}
            r3 = followup("turn-2", "Return ONLY JSON", label="turn-3", schema=SCHEMA)
            return {"r1": r1, "r2": r2, "r3": r3}
            """
        )
        env_extra = {
            "FAKE_CODEX_ARGV_LOG": str(argv_log),
            "FAKE_CODEX_THREAD_ID": "th_wf_fixed_12345",
        }
        launch = self.run_delegate(["--json", "workflow", "run", str(script)], env_extra=env_extra)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "succeeded")
        result = json.loads(self.run_delegate(["--json", "workflow", "result", wf_id]).stdout)
        self.assertEqual(result["result"]["r3"], {"ok": True, "value": "structured"})

        lines = [
            json.loads(line) for line in argv_log.read_text(encoding="utf-8").strip().splitlines()
        ]
        self.assertEqual(len(lines), 3)
        self.assertIn("resume", lines[1])
        self.assertIn("resume", lines[2])


if __name__ == "__main__":
    unittest.main()
