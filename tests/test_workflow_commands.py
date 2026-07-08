from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from delegate_agent.workflows import registry as workflow_registry  # noqa: E402

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
                    "workflows": {"itemThreads": 4, "structuredOutputRetries": 1},
                }
            ),
            encoding="utf-8",
        )
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
            "structured = '--output-schema' in sys.argv or 'Return ONLY' in prompt\n"
            "attempt_file = os.environ.get('FAKE_CODEX_ATTEMPT_FILE')\n"
            "if structured and attempt_file:\n"
            "    try:\n"
            "        attempt = int(open(attempt_file, encoding='utf-8').read() or '0') + 1\n"
            "    except FileNotFoundError:\n"
            "        attempt = 1\n"
            "    open(attempt_file, 'w', encoding='utf-8').write(str(attempt))\n"
            '    text = \'{"ok": "wrong"}\' if attempt == 1 else \'{"ok": true, "value": "structured"}\'\n'
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
            timeout=20,
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
        self.assertIn("workflows", json.loads(describe.stdout))
        help_result = self.run_delegate(["--json", "help", "workflow"])
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertEqual(json.loads(help_result.stdout)["command"], "workflow")

    def test_resume_rejects_schema_invalid_adopted_result_and_respawns(self) -> None:
        # R1: adopted raw text that fails schema must not be cached; respawn.
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
        self.assertIn("agent_adopt_rejected", {event["type"] for event in events_after})

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
        self.assertIn("agent_adopt_rejected", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("agent_timeout", workflow_registry.DURABLE_EVENT_TYPES)
        self.assertIn("budget", workflow_registry.DURABLE_EVENT_TYPES)
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
            workflow_registry.append_jsonl(
                journal, {"seq": 4, "type": "agent_adopt_rejected", "key": "k"}
            )
            workflow_registry.append_jsonl(journal, {"seq": 5, "type": "agent_timeout", "key": "k"})
            workflow_registry.append_jsonl(
                journal, {"seq": 6, "type": "budget", "key": "k", "spent": 1}
            )
        finally:
            os.fsync = original  # type: ignore[assignment]
        self.assertEqual(len(fsynced), 5)

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
                lambda: agent("very slow c"),
            ])
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--budget", "3"],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "30"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id, count=3)
        status_mid = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status_mid["budget"]["spent"], 3)
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
            ["--json", "workflow", "run", "--resume", wf_id, "--budget", "3"]
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["budget"]["spent"], 3)
        result = self.run_delegate(["--json", "workflow", "result", wf_id])
        self.assertEqual(
            json.loads(result.stdout)["result"],
            ["fake completion", "fake completion", "fake completion"],
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
                lambda: agent("very slow c"),
            ])
            """
        )
        launch = self.run_delegate(
            ["--json", "workflow", "run", str(script), "--budget", "3"],
            env_extra={"FAKE_CODEX_SLEEP_SECONDS": "30"},
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        wf_id = json.loads(launch.stdout)["wfId"]
        self.wait_for_group_runs(wf_id, count=3)
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
        # the fsynced journal (three budget claims) survived.
        status_path = root / workflow_registry.STATUS_FILE
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["budget"] = {"total": 3, "spent": 0}
        status_path.write_text(json.dumps(status, sort_keys=True), encoding="utf-8")
        if (root / "result.json").exists():
            (root / "result.json").unlink()
        resumed = self.run_delegate(["--json", "workflow", "run", "--resume", wf_id])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        waited = self.run_delegate(["--json", "workflow", "wait", wf_id, "--timeout", "15"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        status_after = json.loads(self.run_delegate(["--json", "workflow", "status", wf_id]).stdout)
        self.assertEqual(status_after["status"], "succeeded")
        self.assertEqual(status_after["budget"]["spent"], 3)

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


if __name__ == "__main__":
    unittest.main()
