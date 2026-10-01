"""A Claude Run that ends its turn mid-job is `succeeded` but marked `degraded`.

Each test drives a scripted fake `claude` through the real tracked launch, so
the finalizer, the run record, the launch envelope, `wait`, `snapshot`, `runs`,
and `run-output` all see what production sees. The stream shapes are the real
Claude Code events: a `background_tasks_changed` snapshot, then `result`, then
the harness's own post-`result` clean-up snapshot.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import (  # noqa: E402
    argv_builders,
    cli,
    prompt_instructions,
    request_build,
)
from delegate_agent import config as delegate_config  # noqa: E402
from delegate_agent.constants import PROMPT_INSTRUCTION_MODE_SLASH  # noqa: E402
from delegate_agent.workflows import runtime as workflow_runtime  # noqa: E402

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys

stdin_text = sys.stdin.read()
record = {
    "argv": sys.argv[1:],
    "disableBackgroundTasks": os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
    "bashDefaultTimeoutMs": os.environ.get("BASH_DEFAULT_TIMEOUT_MS"),
    "bashMaxTimeoutMs": os.environ.get("BASH_MAX_TIMEOUT_MS"),
    "stdin": stdin_text,
}
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")
with open(os.environ["FAKE_CLAUDE_STREAM"], encoding="utf-8") as handle:
    sys.stdout.write(handle.read())
"""

SESSION = "550e8400-e29b-41d4-a716-446655440000"
GATE_COMMAND = "pytest -q > /tmp/full-gate.log 2>&1; echo EXIT=$?"


def system_event(subtype: str, **fields: object) -> dict:
    return {"type": "system", "subtype": subtype, "session_id": SESSION, **fields}


def tasks_changed(*descriptions: str) -> dict:
    return system_event(
        "background_tasks_changed",
        tasks=[
            {"task_id": f"b{index}", "task_type": "local_bash", "description": description}
            for index, description in enumerate(descriptions)
        ],
    )


def assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def result_event(text: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": SESSION,
    }


def mid_job_stream(final: str = "Waiting on the gate.") -> list[dict]:
    """The real shape: gate started, turn ends, harness then kills the leftovers."""
    return [
        system_event("init"),
        tasks_changed(GATE_COMMAND),
        assistant_text(final),
        result_event(final),
        tasks_changed(),
    ]


def finished_stream(final: str) -> list[dict]:
    """The gate ran to completion inside the turn: the snapshot is empty at `result`."""
    return [
        system_event("init"),
        tasks_changed(GATE_COMMAND),
        tasks_changed(),
        assistant_text(final),
        result_event(final),
    ]


FINISHED_REPORT = (
    "## Delegate completion report\n\n"
    "- **Status:** completed.\n"
    "- **What:** fixed the parser and added a regression test.\n"
    "- **Verification:** full gate 412 passed.\n"
)


class DegradedRunsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.workspace = Path(self.temp.name) / "workspace"
        self.bin_dir = Path(self.temp.name) / "bin"
        for path in (self.home, self.workspace, self.bin_dir):
            path.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=self.workspace, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=self.workspace, check=True
        )
        (self.workspace / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=self.workspace, check=True, capture_output=True
        )
        claude = self.bin_dir / "claude"
        claude.write_text(FAKE_CLAUDE, encoding="utf-8")
        claude.chmod(0o755)
        self.claude_config: dict = {
            "binary": str(claude),
            "defaultModel": "claude-3-7-sonnet",
            "models": {"claude-3-7-sonnet": "claude-3-7-sonnet"},
            "workPermissionMode": "auto",
            "noSessionPersistence": True,
        }
        self.extra_config: dict = {}
        self.config_path = self.home / "delegate_config.json"
        self.stream_path = self.home / "stream.jsonl"
        self.log_path = self.home / "claude_log.jsonl"
        self.write_config()

    def write_config(self) -> None:
        self.config_path.write_text(
            json.dumps({"version": 1, "claude": self.claude_config, **self.extra_config}),
            encoding="utf-8",
        )

    def script(self, events: list[dict]) -> None:
        self.stream_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

    def child_launches(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").strip().splitlines()
        ]

    def run_delegate(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = {
            "HOME": str(self.home),
            "DELEGATE_CONFIG": str(self.config_path),
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_CLAUDE_LOG": str(self.log_path),
            "FAKE_CLAUDE_STREAM": str(self.stream_path),
        }
        old_env = os.environ.copy()
        try:
            os.environ.update(env)
            exit_code = cli.main(
                ["--cwd", str(self.workspace), *args], stdout=stdout, stderr=stderr
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def launch(self, *args: str) -> dict:
        exit_code, stdout, stderr = self.run_delegate(["--json", "claude", *args])
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r} stderr={stderr!r}")
        return json.loads(stdout)

    def run_state(self, payload: dict) -> dict:
        matches = list(self.workspace.glob(f".delegate/runs/{payload['runId']}/state.json"))
        self.assertEqual(len(matches), 1, msg=f"no state.json for {payload['runId']}")
        return json.loads(matches[0].read_text(encoding="utf-8"))


class DegradedDetectionTests(DegradedRunsBase):
    def test_work_run_ending_mid_job_is_succeeded_and_marked_degraded_everywhere(self):
        self.script(mid_job_stream())
        payload = self.launch("work", "run the gate")

        # Status is not changed: the work is still adoptable.
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["resultQuality"], "ok")
        # The launch envelope says so.
        self.assertIs(payload["degraded"], True)
        self.assertEqual(payload["degradedReason"], "ended_waiting_on_background_work")
        self.assertTrue(any("degraded=" in warning for warning in payload["warnings"]))
        evidence = " ".join(payload["degradedEvidence"])
        self.assertIn("Waiting on the gate.", evidence)
        self.assertIn("pytest -q", evidence)
        # So does the persisted record.
        state = self.run_state(payload)
        self.assertEqual(state["status"], "succeeded")
        self.assertIs(state["degraded"], True)
        self.assertEqual(state["degradedReason"], "ended_waiting_on_background_work")

        alias = payload["alias"]
        # wait: still exit 0 and ok, with the flag on the run.
        exit_code, stdout, _ = self.run_delegate(["--json", "wait", alias])
        self.assertEqual(exit_code, 0)
        waited = json.loads(stdout)
        self.assertTrue(waited["ok"])
        self.assertIs(waited["runs"][0]["degraded"], True)
        self.assertEqual(waited["runs"][0]["degradedReason"], "ended_waiting_on_background_work")
        # wait --structural keeps it too.
        exit_code, stdout, _ = self.run_delegate(["--json", "wait", "--structural", alias])
        self.assertEqual(exit_code, 0)
        structural = json.loads(stdout)["runs"][0]
        self.assertIs(structural["degraded"], True)
        self.assertEqual(structural["degradedReason"], "ended_waiting_on_background_work")
        # wait text table names it.
        exit_code, stdout, _ = self.run_delegate(["wait", alias])
        self.assertEqual(exit_code, 0)
        self.assertIn("degraded: ended_waiting_on_background_work", stdout)
        # snapshot, json and text.
        exit_code, stdout, _ = self.run_delegate(["--json", "snapshot", alias])
        self.assertEqual(exit_code, 0)
        snapshot = json.loads(stdout)
        self.assertIs(snapshot["degraded"], True)
        self.assertEqual(snapshot["degradedReason"], "ended_waiting_on_background_work")
        exit_code, stdout, _ = self.run_delegate(["snapshot", alias])
        self.assertIn("degraded: ended_waiting_on_background_work", stdout)
        # runs listing, json and text.
        exit_code, stdout, _ = self.run_delegate(["--json", "runs"])
        listed = json.loads(stdout)["runs"][0]
        self.assertIs(listed["degraded"], True)
        exit_code, stdout, _ = self.run_delegate(["runs"])
        self.assertIn("degraded", stdout)
        # The completion-report view.
        exit_code, stdout, _ = self.run_delegate(
            ["--json", "run-output", alias, "--completion-report"]
        )
        self.assertEqual(exit_code, 0)
        view = json.loads(stdout)
        section = view["sections"]["completionReport"]
        self.assertIs(section["degraded"], True)
        self.assertEqual(section["degradedReason"], "ended_waiting_on_background_work")
        self.assertTrue(any("degraded=" in warning for warning in view["warnings"]))

    def test_the_post_result_cleanup_snapshot_does_not_hide_the_unfinished_task(self):
        # Real order: the harness kills leftovers AFTER `result` and reports an
        # empty task list. A last-snapshot reader would see no tasks and miss it.
        self.script(mid_job_stream(final=FINISHED_REPORT))
        payload = self.launch("work", "run the gate")
        self.assertTrue(payload["ok"])
        self.assertIs(payload["degraded"], True)
        self.assertEqual(payload["degradedReason"], "background_work_unfinished_at_exit")

    def test_text_evidence_alone_marks_a_run_with_no_background_task_in_the_stream(self):
        # An older Claude Code emits no background_tasks_changed events.
        self.script(
            [
                system_event("init"),
                assistant_text("Monitor armed"),
                result_event("Monitor armed; waiting for both suites to finish."),
            ]
        )
        payload = self.launch("work", "run the gate")
        self.assertIs(payload["degraded"], True)
        self.assertEqual(payload["degradedReason"], "ended_waiting_on_background_work")

    def test_a_run_whose_background_task_finished_before_the_turn_ended_is_clean(self):
        self.script(finished_stream(FINISHED_REPORT))
        payload = self.launch("work", "run the gate")
        self.assertTrue(payload["ok"])
        self.assertNotIn("degraded", payload)
        self.assertNotIn("degradedReason", payload)
        self.assertNotIn("degradedEvidence", payload)
        self.assertFalse(any("degraded=" in warning for warning in payload.get("warnings", [])))
        state = self.run_state(payload)
        self.assertNotIn("degraded", state)
        _code, stdout, _ = self.run_delegate(["wait", payload["alias"]])
        self.assertNotIn("degraded", stdout)
        _code, stdout, _ = self.run_delegate(["snapshot", payload["alias"]])
        self.assertNotIn("degraded", stdout)

    def test_a_blocked_report_that_waits_on_the_requester_is_clean(self):
        self.script(
            [
                system_event("init"),
                assistant_text("x"),
                result_event(
                    "**Status: blocked at the base check.**\n\n"
                    "- **Remaining:** all of round eight is waiting on your answer."
                ),
            ]
        )
        payload = self.launch("work", "do the round")
        self.assertNotIn("degraded", payload)

    def test_work_run_ending_on_an_announced_step_is_degraded(self):
        final = "I have read every file. Now let me write my report."
        self.script([system_event("init"), assistant_text(final), result_event(final)])
        payload = self.launch("work", "review the code")
        self.assertEqual(payload["status"], "succeeded")
        self.assertIs(payload["degraded"], True)
        self.assertEqual(payload["degradedReason"], "ended_announcing_next_step")

    def test_work_run_parked_on_approval_with_no_changes_is_degraded(self):
        final = "Awaiting approval of the bounded implementation design"
        self.script([system_event("init"), assistant_text(final), result_event(final)])
        payload = self.launch("work", "implement the design")
        self.assertEqual(payload["status"], "succeeded")
        self.assertIs(payload["degraded"], True)
        self.assertEqual(payload["degradedReason"], "ended_awaiting_input")

    def test_safe_run_asking_for_approval_is_not_degraded(self):
        final = "Awaiting approval of the bounded implementation design"
        self.script([system_event("init"), assistant_text(final), result_event(final)])
        payload = self.launch("safe", "review the design")
        self.assertNotIn("degraded", payload)

    def test_a_failed_run_is_not_marked_degraded(self):
        # Degraded means "succeeded, but ended mid-job". A failed run is just failed.
        self.script(
            [
                system_event("init"),
                tasks_changed(GATE_COMMAND),
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "Waiting on the gate.",
                    "session_id": SESSION,
                },
                tasks_changed(),
            ]
        )
        _code, stdout, _stderr = self.run_delegate(["--json", "claude", "work", "run the gate"])
        payload = json.loads(stdout)
        self.assertNotEqual(payload["status"], "succeeded")
        self.assertNotIn("degraded", payload)
        self.assertNotIn("degraded", self.run_state(payload))

    def test_the_completion_report_view_recovered_from_stdout_is_marked_too(self):
        self.script(mid_job_stream())
        payload = self.launch("work", "run the gate")
        report = next(self.workspace.glob(f".delegate/runs/{payload['runId']}/*report*.md"))
        report.unlink()
        exit_code, stdout, _ = self.run_delegate(
            ["--json", "run-output", payload["alias"], "--completion-report"]
        )
        self.assertEqual(exit_code, 0)
        section = json.loads(stdout)["sections"]["completionReport"]
        self.assertIs(section["synthetic"], True)
        self.assertIs(section["degraded"], True)
        self.assertEqual(section["degradedReason"], "ended_waiting_on_background_work")

    def test_a_structured_output_run_is_not_text_checked(self):
        schema = self.home / "schema.json"
        schema.write_text(
            json.dumps({"type": "object", "properties": {"note": {"type": "string"}}}),
            encoding="utf-8",
        )
        self.script(
            [
                system_event("init"),
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "",
                    "structured_output": {"note": "Waiting on the gate."},
                    "session_id": SESSION,
                },
            ]
        )
        exit_code, stdout, stderr = self.run_delegate(
            ["--json", "claude", "work", "--output-schema", str(schema), "run the gate"]
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r} stderr={stderr!r}")
        self.assertNotIn("degraded", json.loads(stdout))


class DegradedPreventionTests(DegradedRunsBase):
    def test_work_run_disables_background_tasks_and_the_monitor_tool(self):
        self.script(finished_stream(FINISHED_REPORT))
        self.launch("work", "run the gate")
        (launch,) = self.child_launches()
        self.assertEqual(launch["disableBackgroundTasks"], "1")
        argv = launch["argv"]
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Monitor")
        # Claude Code cuts a foreground command off at 2 minutes (default) and 10
        # minutes (maximum); with background tasks off a 15-minute gate needs more.
        self.assertEqual(launch["bashDefaultTimeoutMs"], "7200000")
        self.assertEqual(launch["bashMaxTimeoutMs"], "7200000")

    def test_followup_of_a_resumable_work_run_keeps_the_prevention(self):
        self.script(finished_stream(FINISHED_REPORT))
        first = self.launch("work", "--resumable", "run the gate")
        exit_code, stdout, stderr = self.run_delegate(
            ["--json", "followup", first["alias"], "keep going"]
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r} stderr={stderr!r}")
        _initial, followup = self.child_launches()
        self.assertIn("--resume", followup["argv"])
        self.assertEqual(followup["disableBackgroundTasks"], "1")
        self.assertEqual(followup["bashDefaultTimeoutMs"], "7200000")
        self.assertEqual(followup["bashMaxTimeoutMs"], "7200000")
        self.assertIn("--disallowedTools", followup["argv"])

    def test_config_key_turns_the_prevention_off(self):
        self.claude_config["disableBackgroundTasks"] = False
        self.write_config()
        self.script(finished_stream(FINISHED_REPORT))
        self.launch("work", "run the gate")
        (launch,) = self.child_launches()
        self.assertIsNone(launch["disableBackgroundTasks"])
        self.assertIsNone(launch["bashDefaultTimeoutMs"])
        self.assertIsNone(launch["bashMaxTimeoutMs"])
        self.assertNotIn("--disallowedTools", launch["argv"])

    def test_safe_run_argv_and_env_are_unchanged(self):
        self.script(finished_stream(FINISHED_REPORT))
        self.launch("safe", "review the diff")
        (launch,) = self.child_launches()
        self.assertIsNone(launch["disableBackgroundTasks"])
        self.assertIsNone(launch["bashDefaultTimeoutMs"])
        self.assertIsNone(launch["bashMaxTimeoutMs"])
        self.assertNotIn("--disallowedTools", launch["argv"])

    def test_bash_timeouts_follow_a_run_timeout_longer_than_the_floor(self):
        self.script(finished_stream(FINISHED_REPORT))
        self.launch("work", "--timeout", "10800", "run the gate")
        (launch,) = self.child_launches()
        self.assertEqual(launch["bashDefaultTimeoutMs"], "10800000")
        self.assertEqual(launch["bashMaxTimeoutMs"], "10800000")

    def test_a_short_run_timeout_does_not_shrink_the_bash_timeouts_below_the_floor(self):
        self.script(finished_stream(FINISHED_REPORT))
        self.launch("work", "--timeout", "60", "run the gate")
        (launch,) = self.child_launches()
        self.assertEqual(launch["bashDefaultTimeoutMs"], "7200000")
        self.assertEqual(launch["bashMaxTimeoutMs"], "7200000")

    def test_a_profile_cannot_undo_the_prevention_environment(self):
        # The auth profile's env is applied after Delegate builds the child env.
        self.extra_config["profiles"] = {
            "detectFrom": [],
            "default": None,
            "definitions": {
                "loose": {
                    "env": {
                        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0",
                        "BASH_DEFAULT_TIMEOUT_MS": "1000",
                        "BASH_MAX_TIMEOUT_MS": "2000",
                    }
                }
            },
        }
        self.write_config()
        self.script(finished_stream(FINISHED_REPORT))
        exit_code, stdout, stderr = self.run_delegate(
            ["--json", "--auth-profile", "loose", "claude", "work", "run the gate"]
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r} stderr={stderr!r}")
        (launch,) = self.child_launches()
        self.assertEqual(launch["disableBackgroundTasks"], "1")
        self.assertEqual(launch["bashDefaultTimeoutMs"], "7200000")
        self.assertEqual(launch["bashMaxTimeoutMs"], "7200000")
        warnings = json.loads(stdout)["warnings"]
        for name in (
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
            "BASH_DEFAULT_TIMEOUT_MS",
            "BASH_MAX_TIMEOUT_MS",
        ):
            self.assertTrue(
                any(f"profile env {name} ignored" in warning for warning in warnings), warnings
            )

    def test_workspace_env_for_an_owned_name_is_overridden_with_a_warning(self):
        from types import SimpleNamespace

        from delegate_agent import request_build

        owned = {
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "BASH_DEFAULT_TIMEOUT_MS": "7200000",
            "BASH_MAX_TIMEOUT_MS": "7200000",
        }
        launch = SimpleNamespace(
            workspace_base=None,
            workspace_setup=None,
            workspace_env={
                "BASH_MAX_TIMEOUT_MS": "1000",
                "BASH_DEFAULT_TIMEOUT_MS": "7200000",
                "MY_SETTING": "x",
            },
            workspace_env_files=(),
        )
        for engine, warned in (("claude", True), ("codex", False)):
            with self.subTest(engine):
                request = SimpleNamespace(
                    engine=engine, env_overrides=dict(owned), warnings=("earlier",)
                )
                request_build._apply_workspace_spec(request, launch)
                self.assertEqual(request.env_overrides["BASH_MAX_TIMEOUT_MS"], "7200000")
                self.assertEqual(request.env_overrides["MY_SETTING"], "x")
                self.assertEqual(request.warnings[0], "earlier")
                ignored = [w for w in request.warnings if "workspace env" in w]
                if warned:
                    # Only the name whose value differs draws a warning.
                    self.assertEqual(len(ignored), 1, request.warnings)
                    self.assertIn("workspace env BASH_MAX_TIMEOUT_MS ignored", ignored[0])
                else:
                    self.assertEqual(ignored, [])

    def test_a_profile_that_agrees_with_the_prevention_environment_draws_no_warning(self):
        self.extra_config["profiles"] = {
            "detectFrom": [],
            "default": None,
            "definitions": {"same": {"env": {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}}},
        }
        self.write_config()
        self.script(finished_stream(FINISHED_REPORT))
        exit_code, stdout, stderr = self.run_delegate(
            ["--json", "--auth-profile", "same", "claude", "work", "run the gate"]
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r} stderr={stderr!r}")
        self.assertFalse(
            any("profile env" in warning for warning in json.loads(stdout).get("warnings", []))
        )

    def test_with_the_prevention_off_the_profile_decides(self):
        self.claude_config["disableBackgroundTasks"] = False
        self.extra_config["profiles"] = {
            "detectFrom": [],
            "default": None,
            "definitions": {"loose": {"env": {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"}}},
        }
        self.write_config()
        self.script(finished_stream(FINISHED_REPORT))
        exit_code, stdout, stderr = self.run_delegate(
            ["--json", "--auth-profile", "loose", "claude", "work", "run the gate"]
        )
        self.assertEqual(exit_code, 0, msg=f"stdout={stdout!r} stderr={stderr!r}")
        (launch,) = self.child_launches()
        self.assertEqual(launch["disableBackgroundTasks"], "0")
        self.assertIsNone(launch["bashMaxTimeoutMs"])

    def test_every_tracked_run_prompt_says_ending_the_turn_ends_the_run(self):
        self.script(finished_stream(FINISHED_REPORT))
        self.launch("work", "run the gate")
        self.launch("safe", "review the diff")
        for launch in self.child_launches():
            self.assertIn("Ending your turn ends this Run", launch["stdin"])
            self.assertIn("in the foreground", launch["stdin"])


class TurnEndClauseTests(unittest.TestCase):
    def frame(self, prompt="fix it", **kwargs):
        options = {"engine": "codex", "mode": "work", "completion_report_mode": "none"}
        options.update(kwargs)
        return request_build.effective_prompt(prompt, **options)

    def test_tracked_work_and_safe_prompts_carry_the_clause_once_for_every_engine(self):
        for engine in ("claude", "codex", "cursor", "omp", "grok", "kimi", "droid", "pi"):
            for mode in ("work", "safe"):
                with self.subTest(engine=engine, mode=mode):
                    framed = self.frame(engine=engine, mode=mode)
                    self.assertEqual(framed.count(prompt_instructions.TURN_END_INSTRUCTION), 1)

    def test_work_prompt_says_nobody_can_approve_and_safe_prompt_does_not(self):
        for engine in ("claude", "codex", "cursor", "omp", "grok", "kimi", "droid", "pi"):
            with self.subTest(engine=engine):
                work = self.frame(engine=engine, mode="work")
                self.assertIn("Nobody can answer a question or approve a step", work)
                self.assertIn("as the approval to carry it out", work)
                self.assertIn("plan, a review, or a read-only answer", work)
                self.assertIn("says exactly what blocks you", work)
                self.assertEqual(work.count(prompt_instructions.WORK_NO_APPROVAL_INSTRUCTION), 1)
                safe = self.frame(engine=engine, mode="safe")
                self.assertNotIn("approve a step", safe)
        self.assertNotIn("approve a step", self.frame(mode="call"))
        self.assertNotIn("approve a step", self.frame(mode=""))

    def test_reframing_a_work_prompt_does_not_repeat_the_approval_sentence(self):
        once = self.frame(mode="work")
        self.assertEqual(self.frame(prompt=once, mode="work"), once)

    def test_a_safe_framed_prompt_reframed_as_work_gains_the_approval_sentences(self):
        safe = self.frame(mode="safe")
        self.assertNotIn(prompt_instructions.WORK_NO_APPROVAL_INSTRUCTION, safe)
        work = self.frame(prompt=safe, mode="work")
        self.assertEqual(work.count(prompt_instructions.WORK_NO_APPROVAL_INSTRUCTION), 1)
        self.assertEqual(work.count(prompt_instructions.TURN_END_INSTRUCTION), 1)
        self.assertEqual(self.frame(prompt=work, mode="work"), work)

    def test_the_clause_says_what_matters(self):
        body = prompt_instructions.TURN_END_INSTRUCTION.split("\n\n", 1)[1].strip()
        self.assertIn("Ending your turn ends this Run", body)
        self.assertIn("nothing will wake you", body)
        self.assertIn("foreground", body)
        self.assertIn("before your final message", body)

    def test_the_clause_follows_the_task_and_precedes_the_completion_report(self):
        framed = self.frame(prompt="the task", completion_report_mode="markdown")
        clause = framed.index(prompt_instructions.TURN_END_INSTRUCTION)
        self.assertGreater(clause, framed.index("the task"))
        self.assertLess(clause, framed.index("Delegate completion report requirement"))

    def test_reframing_an_already_framed_prompt_does_not_repeat_it(self):
        once = self.frame(mode="safe")
        self.assertEqual(self.frame(prompt=once, mode="safe"), once)

    def test_call_mode_slash_passthrough_and_unmoded_prompts_are_untouched(self):
        self.assertEqual(self.frame(mode="call"), "fix it")
        self.assertNotIn(prompt_instructions.TURN_END_INSTRUCTION, self.frame(mode=""))
        slash = self.frame(prompt="/goal fix it", instruction_mode=PROMPT_INSTRUCTION_MODE_SLASH)
        self.assertEqual(slash, "/goal fix it")


class LaneTestsClauseTests(unittest.TestCase):
    def frame(self, prompt="fix it", **kwargs):
        options = {"engine": "codex", "mode": "work", "completion_report_mode": "none"}
        options.update(kwargs)
        return request_build.effective_prompt(prompt, **options)

    def test_tracked_work_and_safe_prompts_carry_the_lane_rule_once_for_every_engine(self):
        for engine in ("claude", "codex", "cursor", "omp", "grok", "kimi", "droid", "pi"):
            for mode in ("work", "safe"):
                with self.subTest(engine=engine, mode=mode):
                    framed = self.frame(engine=engine, mode=mode)
                    self.assertEqual(framed.count(prompt_instructions.LANE_TESTS_INSTRUCTION), 1)

    def test_the_rule_names_targeted_tests_and_keeps_the_gate_for_the_coordinator(self):
        body = prompt_instructions.LANE_TESTS_INSTRUCTION
        self.assertIn("Lanes run targeted tests only", body)
        self.assertIn("testrun <repo> <kind> -- <cmd>", body)
        self.assertIn("a lane never runs them", body)

    def test_the_rule_follows_the_task_and_precedes_the_turn_end_clause(self):
        framed = self.frame(prompt="the task")
        rule = framed.index(prompt_instructions.LANE_TESTS_INSTRUCTION)
        self.assertGreater(rule, framed.index("the task"))
        self.assertLess(rule, framed.index(prompt_instructions.TURN_END_INSTRUCTION))

    def test_reframing_does_not_repeat_the_rule(self):
        once = self.frame(mode="safe")
        self.assertEqual(self.frame(prompt=once, mode="safe"), once)
        work = self.frame(prompt=once, mode="work")
        self.assertEqual(work.count(prompt_instructions.LANE_TESTS_INSTRUCTION), 1)

    def test_call_mode_slash_passthrough_and_unmoded_prompts_do_not_get_it(self):
        self.assertNotIn(prompt_instructions.LANE_TESTS_INSTRUCTION, self.frame(mode="call"))
        self.assertNotIn(prompt_instructions.LANE_TESTS_INSTRUCTION, self.frame(mode=""))
        slash = self.frame(prompt="/goal fix it", instruction_mode=PROMPT_INSTRUCTION_MODE_SLASH)
        self.assertEqual(slash, "/goal fix it")


class ClaudeWorkEnvOverridesTests(unittest.TestCase):
    def env(self, timeout_seconds, **claude):
        return argv_builders.claude_work_env_overrides(
            {"binary": "claude", **claude}, timeout_seconds=timeout_seconds
        )

    def test_default_run_gets_the_floor_and_a_longer_run_timeout_wins(self):
        for timeout, expected in (
            (None, "7200000"),
            (60, "7200000"),
            (7200, "7200000"),
            (14400, "14400000"),
        ):
            with self.subTest(timeout=timeout):
                env = self.env(timeout)
                self.assertEqual(env["BASH_DEFAULT_TIMEOUT_MS"], expected)
                self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], expected)
                self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")

    def test_a_huge_run_timeout_is_capped_below_the_javascript_timer_limit(self):
        env = self.env(10**9)
        self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], str(2**31 - 1))
        self.assertEqual(env["BASH_DEFAULT_TIMEOUT_MS"], str(2**31 - 1))

    def test_every_variable_delegate_sets_is_in_the_owned_set(self):
        self.assertEqual(set(self.env(None)), set(argv_builders.CLAUDE_WORK_OWNED_ENV))


class DisableBackgroundTasksConfigTests(unittest.TestCase):
    def test_default_is_on(self):
        config = delegate_config.embedded_default_config()
        self.assertIs(config["claude"]["disableBackgroundTasks"], True)
        delegate_config.validate_config(config)

    def test_false_is_accepted(self):
        config = delegate_config.embedded_default_config()
        config["claude"]["disableBackgroundTasks"] = False
        delegate_config.validate_config(config)

    def test_a_non_boolean_is_rejected(self):
        config = delegate_config.embedded_default_config()
        config["claude"]["disableBackgroundTasks"] = "no"
        with self.assertRaises(delegate_config.ConfigError) as ctx:
            delegate_config.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_claude_config")
        self.assertIn("claude.disableBackgroundTasks", ctx.exception.message)


class WorkflowDegradedSurfaceTests(unittest.TestCase):
    """`agent()` still returns a degraded child's text; `agent_meta()` says it is degraded."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        root = self.workspace / ".delegate" / "workflows" / "wf_de9ade9ade9a"
        workflow_runtime.run_registry.ensure_private_dir(root)
        self.state = workflow_runtime.WorkflowState(
            wf_id="wf_de9ade9ade9a",
            workspace=self.workspace,
            root=root,
            script_path=self.workspace / "workflow.py",
            config=delegate_config.embedded_default_config(),
            cli_argv=["delegate"],
            args=None,
            budget=workflow_runtime.Budget(None),
        )
        self.dsl = workflow_runtime.WorkflowDsl(
            self.state, {"defaults": {"engine": "claude", "mode": "safe"}}
        )

    def _agent(self, envelope_fields: dict) -> object:
        envelope = {
            "ok": True,
            "status": "succeeded",
            "exitCode": 0,
            "runId": "del_20260928T000000Z_degrade",
            "failureKind": None,
            "text": "Waiting on the gate.",
            **envelope_fields,
        }

        def child(argv, *, cwd, timeout, environment=None, cancel_event=None):
            return CompletedProcess(argv, 0, json.dumps(envelope).encode(), b"")

        with mock.patch.object(workflow_runtime, "_run_child_command", side_effect=child):
            return self.dsl.agent("run the gate", label="gate")

    def test_a_degraded_child_is_returned_not_failed_and_agent_meta_flags_it(self) -> None:
        result = self._agent(
            {"degraded": True, "degradedReason": "ended_waiting_on_background_work"}
        )
        self.assertEqual(result, "Waiting on the gate.")
        meta = self.dsl.agent_meta("gate")
        assert meta is not None
        self.assertIs(meta["degraded"], True)
        self.assertEqual(meta["degradedReason"], "ended_waiting_on_background_work")
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["status"], "succeeded")

    def test_a_clean_child_has_no_degraded_flag_in_agent_meta(self) -> None:
        self._agent({"text": "Status: completed. All checks passed."})
        meta = self.dsl.agent_meta("gate")
        assert meta is not None
        self.assertIsNone(meta["degraded"])
        self.assertIsNone(meta["degradedReason"])


if __name__ == "__main__":
    unittest.main()
