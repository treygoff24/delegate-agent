from __future__ import annotations

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
CLI = ROOT / "bin" / "delegate.py"


class WorkflowCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
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
            "if 'slow' in prompt:\n"
            "    time.sleep(1)\n"
            "log = os.environ.get('FAKE_PROMPT_LOG')\n"
            "if log:\n"
            "    open(log, 'a', encoding='utf-8').write(prompt + '\\n---\\n')\n"
            "structured = '--output-schema' in sys.argv or 'Return ONLY' in prompt\n"
            'text = \'{"ok": true, "value": "structured"}\' if structured else \'fake completion\'\n'
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
        child = self.write_workflow(
            """
            meta = {"name": "child"}
            raise RuntimeError("child body should be stubbed")
            """
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent"}}
            return workflow({str(child)!r}, gate=True)
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
        self.assertIn("agent engine must be a real delegate engine", result.stderr)

        invalid_schema = self.write_workflow(
            """
            meta = {"name": "bad-schema"}
            return judges("x", {"type": "object", "patternProperties": {}})
            """
        )
        result = self.run_delegate(["--json", "workflow", "check", str(invalid_schema)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid schema literal", result.stderr)

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
        child = self.write_workflow(
            """
            meta = {"name": "child", "defaults": {"engine": "codex", "mode": "safe"}}
            return agent("child")
            """
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent", "defaults": {{"engine": "codex", "mode": "safe"}}}}
            first = workflow({str(child)!r})
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
        child = self.write_workflow(
            """
            meta = {"name": "child", "defaults": {"engine": "codex", "mode": "safe"}}
            return pipeline(["child"], lambda prev, item, index: agent(item))
            """
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent"}}
            return pipeline(["outer"], lambda prev, item, index: workflow({str(child)!r}))
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
        grandchild = self.write_workflow(
            """
            meta = {"name": "grandchild"}
            import time
            time.sleep(0.2)
            return {"ok": False}
            """
        )
        child = self.write_workflow(
            f"""
            meta = {{"name": "child"}}
            return workflow({str(grandchild)!r}, gate="on-failure")
            """
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "parent", "defaults": {{"engine": "codex", "mode": "safe"}}}}
            return parallel([lambda: agent("slow"), lambda: workflow({str(child)!r})])
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

    def test_resume_releases_paused_gate(self) -> None:
        child = self.write_workflow(
            """
            meta = {"name": "gate-child"}
            return {"ok": False}
            """
        )
        parent = self.write_workflow(
            f"""
            meta = {{"name": "gate-parent"}}
            return workflow({str(child)!r}, gate="on-failure")
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


if __name__ == "__main__":
    unittest.main()
