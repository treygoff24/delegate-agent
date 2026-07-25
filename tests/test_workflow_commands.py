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

from delegate_agent.workflows import commands as workflow_commands  # noqa: E402
from delegate_agent.workflows import registry as workflow_registry  # noqa: E402
from delegate_agent.workflows import runtime as workflow_runtime  # noqa: E402
from delegate_agent.workflows import schema as workflow_schema  # noqa: E402

CLI = ROOT / "bin" / "delegate.py"


class WorkflowCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.home = self.workspace / "home"
        self.home.mkdir()
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
                    "workflows": {"itemThreads": 4, "structuredOutputRetries": 1},
                }
            ),
            encoding="utf-8",
        )
        devin = self.bin_dir / "devin"
        devin.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "# Read-only lanes must materialize a real agent-config file.\n"
            "if '--agent-config' in sys.argv:\n"
            "    cfg = sys.argv[sys.argv.index('--agent-config') + 1]\n"
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
                "import json, sys, time\n"
                "prompt = (sys.stdin.read() if not sys.stdin.closed else '')\n"
                "if '--file' in sys.argv:\n"
                "    prompt += open(sys.argv[sys.argv.index('--file') + 1], encoding='utf-8').read()\n"
                "prompt += ' '.join(sys.argv[1:])\n"
                "if 'slow' in prompt:\n"
                "    time.sleep(1)\n"
                "text = '{\"ok\": true, \"value\": \"structured\"}' if 'Return ONLY' in prompt else 'fake completion'\n"
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
            "sleep = os.environ.get('FAKE_CODEX_SLEEP_SECONDS')\n"
            "if sleep:\n"
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
            "    open(argv_log, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
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
            "if structured and attempt_file:\n"
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
            '    text = \'{"ok": true, "value": "structured"}\' if structured else \'fake completion\'\n'
            "print(json.dumps({'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}))\n"
            "print(json.dumps({'type':'completion','finalText':text}))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def run_delegate(
        self, args: list[str], *, env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DELEGATE_CONFIG"] = str(self.config_path)
        env["DELEGATE_WORKFLOW_NO_DAEMON"] = "1"
        env["HOME"] = str(self.home)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(CLI), "--cwd", str(self.workspace), *args],
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

    def test_dry_run_warns_before_argv_transport_prompt_limit(self) -> None:
        from delegate_agent.workflows.runtime import PROMPT_ARGV_GUARD_BYTES

        prompt = "x" * (PROMPT_ARGV_GUARD_BYTES + 1)
        script = self.write_workflow(
            f"""
            meta = {{"name": "dry-argv-limit", "defaults": {{"engine": "cursor"}}}}
            return agent({prompt!r})
            """
        )
        result = self.run_delegate(["--json", "workflow", "run", str(script), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        call = payload["runTree"]["calls"][0]
        self.assertEqual(call["promptBytes"], PROMPT_ARGV_GUARD_BYTES + 1)
        self.assertIn(f"{PROMPT_ARGV_GUARD_BYTES}-byte", call["warnings"][0])
        self.assertIn("cursor", call["warnings"][0])
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
        self.assertLess(slow_finish_seq, gate_seq)
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

        def fail_detach(*_args, **_kwargs) -> None:
            self.assertEqual(workflow_registry.read_json(status_path)["status"], "starting")
            self.assertFalse(result_path.exists())
            self.assertTrue(workflow_registry.supervisor_alive(root))
            raise RuntimeError("launch failed")

        with (
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

    def test_resume_snapshot_failure_releases_workflow_lock(self) -> None:
        wf_id = "wf_123456789abc"
        root = self._seed_completed_workflow(
            wf_id,
            created_at="2026-01-01T00:00:00Z",
            result={"value": "old"},
        )
        prior_result = (root / workflow_registry.RESULT_FILE).read_bytes()

        with (
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
        snapshot_path = self.workspace / ".delegate" / "runs" / run_id / "snapshot.json"
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
        describe = self.run_delegate(["--json", "describe"])
        self.assertEqual(describe.returncode, 0, describe.stderr)
        payload = json.loads(describe.stdout)
        self.assertIn("workflows", payload)
        self.assertTrue(payload["workflows"]["dsl"]["agent"]["signature"].endswith("fast=None)"))
        help_result = self.run_delegate(["--json", "help", "workflow"])
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
        snapshot_path = self.workspace / ".delegate" / "runs" / run_id / "snapshot.json"
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

    def test_agent_started_events_are_fsynced(self) -> None:
        # R4 / F5: durable adoption, audit, and budget-claim events are fsynced.
        self.assertIn("agent_started", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_adopted", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_adopt_rejected", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_timeout", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("budget", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertNotIn("agent_result", workflow_registry.DURABLE_EVENT_TYPES)
        journal = self.workspace / "fsync-journal.jsonl"
        fsynced: list[int] = []
        original_fsync = os.fsync

        def tracking_fsync(fd: int) -> None:
            fsynced.append(fd)
            original_fsync(fd)

        original = os.fsync
        os.fsync = tracking_fsync  # type: ignore[assignment]
        try:
            workflow_registry.append_jsonl(journal, {"seq": 1, "type": "agent_started", "key": "k"})
            workflow_registry.append_jsonl(journal, {"seq": 2, "type": "log", "message": "x"})
            workflow_registry.append_jsonl(
                journal, {"seq": 3, "type": "agent_finished", "key": "k", "result": "ok"}
            )
            workflow_registry.append_jsonl(journal, {"seq": 4, "type": "agent_adopted", "key": "k"})
            workflow_registry.append_jsonl(
                journal, {"seq": 5, "type": "agent_adopt_rejected", "key": "k"}
            )
            workflow_registry.append_jsonl(journal, {"seq": 6, "type": "agent_timeout", "key": "k"})
            workflow_registry.append_jsonl(
                journal, {"seq": 7, "type": "budget", "key": "k", "spent": 1}
            )
        finally:
            os.fsync = original  # type: ignore[assignment]
        self.assertEqual(len(fsynced), 6)

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
        # timeout is not part of the structural key; shorten adoption wait only.
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
        self.assertEqual(len(runs_after), 1)
        snap = json.loads(self.run_delegate(["--json", "snapshot", runs_after[0]["alias"]]).stdout)
        self.assertIn(snap.get("effectiveStatus") or snap.get("status"), {"cancelled", "failed"})

    def test_argv_transport_prompt_size_guard(self) -> None:
        # §2.4: cursor/kimi argv transport rejects oversized agent prompts.
        from delegate_agent.workflows.runtime import PROMPT_ARGV_GUARD_BYTES

        oversized = "x" * (PROMPT_ARGV_GUARD_BYTES + 1)
        script = self.write_workflow(
            f"""
            meta = {{"name": "argv-guard", "defaults": {{"engine": "cursor", "mode": "safe"}}}}
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
        self.assertIn("stage output too large for cursor/kimi argv transport", failed[0]["error"])
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
        launch = self.run_delegate(["--json", "workflow", "run", str(script)])
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
            manifest = json.loads(
                (self.workspace / ".delegate" / "runs" / run["runId"] / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            argv = manifest.get("argv") or []
            if run["harness"] == "codex":
                self.assertIn("--sandbox", argv)
                self.assertIn("read-only", argv)
            else:
                self.assertNotIn("--force", argv)
                self.assertNotIn("--approve-mcps", argv)

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
        workflow_registry.write_status(
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

        legacy = self._seed_completed_workflow("wf_111111111111", created_at="2025-01-01T00:00:00Z")
        legacy_status = workflow_registry.read_json(legacy / workflow_registry.STATUS_FILE) or {}
        legacy_status.update({"status": "running", "createdOrdinal": 100})
        workflow_registry.write_status(legacy, legacy_status)
        self.assertNotIn(
            "createdOrdinal",
            workflow_registry.read_json(legacy / workflow_registry.STATUS_FILE),
        )

    def test_creation_ordinal_survives_resume_and_legacy_resume_stays_legacy(self) -> None:
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

        status.pop("createdOrdinal")
        workflow_registry.write_json(status_path, status)
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "10"]).returncode,
            0,
        )
        self.assertNotIn("createdOrdinal", workflow_registry.read_json(status_path))

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

    def test_latest_legacy_workflow_has_deterministic_timestamp_and_name_fallback(self) -> None:
        statuses = (
            ("wf_aaaaaaaaaaaa", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
            ("wf_bbbbbbbbbbbb", "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z"),
            ("wf_cccccccccccc", "2026-01-02T00:00:00Z", "2026-01-02T00:00:02Z"),
            ("wf_dddddddddddd", "2026-01-02T00:00:00Z", "2026-01-02T00:00:02Z"),
        )
        for wf_id, created_at, updated_at in statuses:
            root = workflow_registry.ensure_workflow_dir(self.workspace, wf_id)
            workflow_registry.write_status(
                root,
                {
                    "wfId": wf_id,
                    "status": "succeeded",
                    "createdAt": created_at,
                    "updatedAt": updated_at,
                },
            )
        latest = workflow_registry.latest_workflow_dir(self.workspace)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.name, "wf_dddddddddddd")

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


if __name__ == "__main__":
    unittest.main()
