"""Pinned-versus-live runtime reporting and the opt-in ``--repin`` for workflow resume."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from delegate_agent import cli_parser, run_registry, workflow_pinning
from delegate_agent.errors import DelegateError
from delegate_agent.workflows import commands as workflow_commands
from delegate_agent.workflows import registry as workflow_registry
from delegate_agent.workflows import runtime as workflow_runtime

WF_ID = "wf_123456789abc"
OLD_VERSION = "0.0.1"


@contextlib.contextmanager
def older_runtime(version: str = OLD_VERSION) -> Iterator[None]:
    """Make ``create_pin`` snapshot a runtime that differs from the live one.

    The extra file changes the content digest; the rewritten VERSION line is
    what ``pinned_runtime_summary`` later reads back out of the snapshot.
    """
    real = workflow_pinning._runtime_source_files

    def older(source_root: Path | None = None) -> list[tuple[str, bytes]]:
        files = []
        for name, content in real(source_root):
            if name == "src/delegate_agent/__init__.py":
                content = re.sub(rb'VERSION = "[^"]+"', f'VERSION = "{version}"'.encode(), content)
            files.append((name, content))
        files.append(
            ("src/delegate_agent/_older_runtime_marker.py", b"# stands in for older code\n")
        )
        return files

    with mock.patch.object(workflow_pinning, "_runtime_source_files", older):
        yield


class RepinTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.home = base / "home"
        (self.home / ".delegate" / "personas").mkdir(parents=True)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def create_pin(self, *, older: bool) -> workflow_pinning.WorkflowPin:
        with older_runtime() if older else contextlib.nullcontext():
            return workflow_pinning.create_pin(
                WF_ID, workspace=self.workspace, config={}, home=self.home
            )

    def seed_workflow(self, status: str = "failed") -> Path:
        root = workflow_registry.ensure_workflow_dir(self.workspace, WF_ID)
        workflow_registry.register_workflow(
            self.workspace,
            root,
            {
                "ok": True,
                "wfId": WF_ID,
                "status": status,
                "createdAt": "2026-09-01T00:00:00Z",
                "workspace": str(self.workspace),
                "scriptPath": str(root / "script.py"),
                "journalPath": str(root / "journal.jsonl"),
                "resultPath": str(root / "result.json"),
            },
        )
        (root / workflow_registry.SCRIPT_FILE).write_text(
            'meta = {"name": "repin"}\nreturn "done"\n', encoding="utf-8"
        )
        return root

    def resume(
        self, *, repin: bool = False, json_mode: bool = True
    ) -> tuple[int, str, str, mock.MagicMock]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(workflow_runtime, "detach_supervisor") as detach:
            code = workflow_commands.emit_run(
                workflow_commands.WorkflowCommand(
                    "run", resume=WF_ID, repin=repin, json_mode=json_mode
                ),
                workspace=self.workspace,
                config={},
                stdout=stdout,
                stderr=stderr,
            )
        return code, stdout.getvalue(), stderr.getvalue(), detach

    def journal(self, root: Path, event_type: str) -> list[dict[str, object]]:
        return [
            event
            for event in workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
            if event.get("type") == event_type
        ]

    def pin_bytes(self) -> bytes:
        return workflow_pinning.pin_path(WF_ID, home=self.home).read_bytes()


class RuntimeDriftTests(RepinTestCase):
    def test_summary_and_drift_name_the_pinned_and_live_runtime(self) -> None:
        pin = self.create_pin(older=True)

        summary = workflow_pinning.pinned_runtime_summary(WF_ID, home=self.home)
        drift = workflow_pinning.runtime_drift(summary, home=self.home)

        self.assertEqual(summary["digest"], pin.runtime_digest)
        self.assertEqual(summary["version"], OLD_VERSION)
        self.assertEqual(summary["pinnedAt"], pin.created_at)
        self.assertIs(drift["differs"], True)
        self.assertEqual(drift["live"]["digest"], workflow_pinning.live_runtime_digest())
        self.assertNotEqual(drift["live"]["digest"], summary["digest"])
        notice = workflow_pinning.runtime_drift_notice(
            drift, workflow_id=WF_ID, resume_hint="HINT."
        )
        assert notice is not None
        self.assertIn(pin.runtime_digest[:12], notice)
        self.assertIn(drift["live"]["digest"][:12], notice)
        self.assertIn(f"delegate {OLD_VERSION}", notice)
        self.assertIn(f"pinned {pin.created_at}", notice)
        self.assertTrue(notice.endswith("HINT."))

    def test_a_pin_on_the_live_runtime_has_no_drift(self) -> None:
        self.create_pin(older=False)

        drift = workflow_pinning.runtime_drift(
            workflow_pinning.pinned_runtime_summary(WF_ID, home=self.home), home=self.home
        )

        self.assertIs(drift["differs"], False)
        self.assertIsNone(
            workflow_pinning.runtime_drift_notice(drift, workflow_id=WF_ID, resume_hint="x")
        )

    def test_live_promotion_date_is_reported_only_for_the_live_digest(self) -> None:
        self.create_pin(older=True)
        summary = workflow_pinning.pinned_runtime_summary(WF_ID, home=self.home)
        live = workflow_pinning.live_runtime_digest()
        stamp = {"runtimeDigest": live, "promotedAt": "2026-09-25T18:00:00Z"}
        with mock.patch.object(workflow_pinning, "_read_promotion", return_value=stamp):
            stamped = workflow_pinning.runtime_drift(summary, home=self.home)
        with mock.patch.object(
            workflow_pinning, "_read_promotion", return_value={**stamp, "runtimeDigest": "0" * 64}
        ):
            other = workflow_pinning.runtime_drift(summary, home=self.home)

        self.assertEqual(stamped["live"]["promotedAt"], "2026-09-25T18:00:00Z")
        self.assertIsNone(other["live"]["promotedAt"])

    def test_resume_without_repin_warns_and_keeps_the_pin(self) -> None:
        pin = self.create_pin(older=True)
        self.seed_workflow()
        before = self.pin_bytes()

        code, stdout, _stderr, detach = self.resume()

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        (notice,) = [w for w in payload["warnings"] if "pinned to runtime" in w]
        self.assertIn(pin.runtime_digest[:12], notice)
        self.assertIn(workflow_pinning.live_runtime_digest()[:12], notice)
        self.assertIn("--repin", notice)
        self.assertIs(payload["runtimePin"]["differs"], True)
        self.assertNotIn("repinned", payload["runtimePin"])
        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual(payload["attemptConfig"]["baseRuntimeDigest"], pin.runtime_digest)
        detach.assert_called_once()
        self.assertEqual(detach.call_args.args[0][0:2], pin.cli_argv)

    def test_resume_text_mode_prints_the_notice_as_a_warning(self) -> None:
        self.create_pin(older=True)
        self.seed_workflow()

        _code, stdout, stderr, _detach = self.resume(json_mode=False)

        self.assertIn("warning: workflow " + WF_ID + " is pinned to runtime", stderr)
        self.assertIn("differs from the live runtime", stderr)
        self.assertIn(f"wfId: {WF_ID}", stdout)

    def test_resume_on_the_live_runtime_is_quiet(self) -> None:
        self.create_pin(older=False)
        self.seed_workflow()

        _code, stdout, _stderr, _detach = self.resume()

        payload = json.loads(stdout)
        self.assertIs(payload["runtimePin"]["differs"], False)
        self.assertEqual([w for w in payload.get("warnings", []) if "pinned to runtime" in w], [])

    def test_a_pin_that_cannot_be_described_never_blocks_the_resume(self) -> None:
        """The notice is advisory: load_pin vouched for the pin, so resume goes on unchecked."""
        self.create_pin(older=True)
        self.seed_workflow()
        broken = workflow_pinning.WorkflowPinError("invalid_pin", "cannot describe this pin")

        with mock.patch.object(workflow_pinning, "pinned_runtime_summary", side_effect=broken):
            code, stdout, _stderr, detach = self.resume()

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertIs(payload["runtimePin"]["checked"], False)
        self.assertEqual(payload["runtimePin"]["error"], "invalid_pin")
        self.assertEqual([w for w in payload.get("warnings", []) if "pinned to runtime" in w], [])
        detach.assert_called_once()

    def test_status_reports_the_comparison_and_a_plain_line_when_it_differs(self) -> None:
        pin = self.create_pin(older=True)
        self.seed_workflow(status="failed")
        command = workflow_commands.WorkflowCommand("status", wf_id=WF_ID)

        text = io.StringIO()
        workflow_commands.emit_status(command, workspace=self.workspace, stdout=text)
        as_json = io.StringIO()
        workflow_commands.emit_status(
            workflow_commands.WorkflowCommand("status", wf_id=WF_ID, json_mode=True),
            workspace=self.workspace,
            stdout=as_json,
        )

        self.assertIn(f"is pinned to runtime {pin.runtime_digest[:12]}", text.getvalue())
        self.assertIn(f"workflow resume {WF_ID} --repin", text.getvalue())
        view = json.loads(as_json.getvalue())
        self.assertIs(view["runtimePin"]["checked"], True)
        self.assertIs(view["runtimePin"]["differs"], True)
        self.assertEqual(view["runtimePin"]["pinned"]["digest"], pin.runtime_digest)

    def test_status_is_silent_for_a_finished_workflow_but_still_reports_json(self) -> None:
        self.create_pin(older=True)
        self.seed_workflow(status="succeeded")

        text = io.StringIO()
        workflow_commands.emit_status(
            workflow_commands.WorkflowCommand("status", wf_id=WF_ID),
            workspace=self.workspace,
            stdout=text,
        )
        as_json = io.StringIO()
        workflow_commands.emit_status(
            workflow_commands.WorkflowCommand("status", wf_id=WF_ID, json_mode=True),
            workspace=self.workspace,
            stdout=as_json,
        )

        self.assertNotIn("pinned to runtime", text.getvalue())
        self.assertIs(json.loads(as_json.getvalue())["runtimePin"]["differs"], True)

    def test_status_survives_a_missing_pin(self) -> None:
        self.seed_workflow(status="failed")
        as_json = io.StringIO()

        workflow_commands.emit_status(
            workflow_commands.WorkflowCommand("status", wf_id=WF_ID, json_mode=True),
            workspace=self.workspace,
            stdout=as_json,
        )

        pin_view = json.loads(as_json.getvalue())["runtimePin"]
        self.assertIs(pin_view["checked"], False)
        self.assertEqual(pin_view["error"], "invalid_pin")


class RepinTests(RepinTestCase):
    def test_repin_moves_the_runtime_and_keeps_every_frozen_input(self) -> None:
        persona = self.home / ".delegate" / "personas" / "reviewer.md"
        persona.write_text("Review only.\n", encoding="utf-8")
        old = self.create_pin(older=True)
        before = json.loads(self.pin_bytes())
        self.assertEqual(before["personas"]["reviewer"]["text"], "Review only.\n")
        # Edited after launch: a repin must keep the bytes the workflow launched with.
        persona.write_text("Review and rewrite.\n", encoding="utf-8")

        result = workflow_pinning.repin_to_live(WF_ID, home=self.home)

        live = workflow_pinning.live_runtime_digest()
        self.assertTrue(result.changed)
        self.assertEqual(result.pin.runtime_digest, live)
        self.assertEqual(result.previous["digest"], old.runtime_digest)
        self.assertEqual(result.previous["version"], OLD_VERSION)
        after = json.loads(self.pin_bytes())
        for frozen in (
            "config",
            "configDigest",
            "configPath",
            "personas",
            "profileIdentity",
            "profileIdentityDigest",
            "createdAt",
            "workflowId",
        ):
            self.assertEqual(after[frozen], before[frozen], frozen)
        self.assertEqual(after["runtime"]["digest"], live)
        (superseded,) = after["runtimeHistory"]
        self.assertEqual(superseded["digest"], old.runtime_digest)
        self.assertEqual(superseded["pinnedAt"], old.created_at)
        self.assertEqual(superseded["supersededAt"], after["runtime"]["pinnedAt"])
        # The old snapshot is left where it was for anything still pointing at it.
        self.assertTrue(old.runtime_root.is_dir())
        # The pin is sealed again, and the result loads like any other pin.
        self.assertEqual(
            workflow_pinning.pin_path(WF_ID, home=self.home).stat().st_mode & 0o777, 0o400
        )
        self.assertEqual(result.pin.path.parent.stat().st_mode & 0o777, 0o500)
        reloaded = workflow_pinning.load_pin(WF_ID, home=self.home)
        assert reloaded is not None
        self.assertEqual(reloaded.runtime_digest, live)

    def test_repin_on_the_live_runtime_changes_nothing(self) -> None:
        self.create_pin(older=False)
        before = self.pin_bytes()

        result = workflow_pinning.repin_to_live(WF_ID, home=self.home)

        self.assertFalse(result.changed)
        self.assertIsNone(result.previous_payload)
        self.assertEqual(self.pin_bytes(), before)

    def test_repin_refuses_credential_namespace_drift_and_changes_nothing(self) -> None:
        self.create_pin(older=True)
        before = self.pin_bytes()
        elsewhere = Path(self.temp.name) / "other-codex-home"

        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(elsewhere)}),
            self.assertRaises(workflow_pinning.WorkflowPinError) as raised,
        ):
            workflow_pinning.repin_to_live(WF_ID, home=self.home)

        self.assertEqual(raised.exception.error, "workflow_profile_drift")
        self.assertEqual(self.pin_bytes(), before)

    def test_repin_restores_the_pin_when_the_new_pin_does_not_validate(self) -> None:
        self.create_pin(older=True)
        before = self.pin_bytes()
        real_load = workflow_pinning.load_pin
        calls: list[int] = []

        def fail_the_second_load(*args: object, **kwargs: object) -> object:
            calls.append(1)
            if len(calls) == 2:
                raise workflow_pinning.WorkflowPinError("invalid_pin", "new pin rejected")
            return real_load(*args, **kwargs)

        with (
            mock.patch.object(workflow_pinning, "load_pin", fail_the_second_load),
            self.assertRaises(workflow_pinning.WorkflowPinError),
        ):
            workflow_pinning.repin_to_live(WF_ID, home=self.home)

        self.assertEqual(len(calls), 2)
        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual(
            workflow_pinning.pin_path(WF_ID, home=self.home).parent.stat().st_mode & 0o777, 0o500
        )

    def test_resume_with_repin_runs_the_supervisor_on_the_live_runtime(self) -> None:
        old = self.create_pin(older=True)
        root = self.seed_workflow()

        code, stdout, _stderr, detach = self.resume(repin=True)

        live = workflow_pinning.live_runtime_digest()
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertIs(payload["runtimePin"]["repinned"], True)
        self.assertEqual(payload["runtimePin"]["previous"]["digest"], old.runtime_digest)
        self.assertEqual(payload["runtimePin"]["pinned"]["digest"], live)
        self.assertIs(payload["runtimePin"]["differs"], False)
        self.assertEqual(payload["attemptConfig"]["baseRuntimeDigest"], live)
        self.assertNotIn("pinned to runtime", " ".join(payload.get("warnings", [])))
        new_pin = workflow_pinning.load_pin(WF_ID, home=self.home)
        assert new_pin is not None
        self.assertEqual(new_pin.runtime_digest, live)
        launched_argv = detach.call_args.args[0]
        self.assertEqual(launched_argv[0:2], new_pin.cli_argv)
        self.assertIn(live, launched_argv[1])
        (repinned,) = self.journal(root, "runtime_repinned")
        self.assertEqual(repinned["fromDigest"], old.runtime_digest)
        self.assertEqual(repinned["fromVersion"], OLD_VERSION)
        self.assertEqual(repinned["toDigest"], live)
        events = [
            event["type"]
            for event in workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
        ]
        self.assertLess(events.index("runtime_repinned"), events.index("attempt_config"))

    def test_resume_with_repin_prints_the_move_in_text_mode(self) -> None:
        old = self.create_pin(older=True)
        self.seed_workflow()

        _code, stdout, _stderr, _detach = self.resume(repin=True, json_mode=False)

        self.assertIn(f"runtimeRepinned: {old.runtime_digest[:12]}", stdout)
        self.assertIn(f"-> {workflow_pinning.live_runtime_digest()[:12]}", stdout)

    def test_resume_with_repin_on_the_live_runtime_says_so(self) -> None:
        self.create_pin(older=False)
        self.seed_workflow()
        before = self.pin_bytes()

        _code, stdout, _stderr, _detach = self.resume(repin=True, json_mode=False)

        self.assertIn("runtimeRepinned: no change, already on the live runtime", stdout)
        self.assertEqual(self.pin_bytes(), before)

    def test_a_resume_that_fails_after_the_repin_restores_the_old_pin(self) -> None:
        self.create_pin(older=True)
        root = self.seed_workflow()
        before = self.pin_bytes()
        status_before = (root / workflow_registry.STATUS_FILE).read_bytes()

        with (
            mock.patch.object(
                workflow_runtime, "detach_supervisor", side_effect=RuntimeError("launch failed")
            ),
            self.assertRaisesRegex(RuntimeError, "launch failed"),
        ):
            workflow_commands.emit_run(
                workflow_commands.WorkflowCommand("run", resume=WF_ID, repin=True, json_mode=True),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual((root / workflow_registry.STATUS_FILE).read_bytes(), status_before)

    def test_a_resume_that_fails_before_launch_restores_the_old_pin(self) -> None:
        self.create_pin(older=True)
        self.seed_workflow()
        before = self.pin_bytes()

        with (
            mock.patch.object(
                workflow_runtime,
                "cancel_workflow_children",
                side_effect=workflow_runtime.WorkflowChildCancellationError(
                    [{"runId": "x", "error": "boom"}]
                ),
            ),
            self.assertRaises(DelegateError) as raised,
        ):
            workflow_commands.emit_run(
                workflow_commands.WorkflowCommand("run", resume=WF_ID, repin=True, json_mode=True),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(raised.exception.error, "workflow_children_unsealed")
        self.assertEqual(self.pin_bytes(), before)

    def test_repin_is_refused_while_a_supervisor_holds_the_workflow(self) -> None:
        self.create_pin(older=True)
        root = self.seed_workflow()
        before = self.pin_bytes()
        lock_fd = workflow_registry.acquire_workflow_lock(root)
        self.addCleanup(os.close, lock_fd)

        with self.assertRaises(DelegateError) as raised:
            self.resume(repin=True)

        self.assertEqual(raised.exception.error, "workflow_locked")
        self.assertEqual(self.pin_bytes(), before)

    def test_repin_is_refused_while_a_child_run_is_still_running(self) -> None:
        self.create_pin(older=True)
        root = self.seed_workflow()
        before = self.pin_bytes()
        status_before = (root / workflow_registry.STATUS_FILE).read_bytes()
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        alias = self.register_child(sleeper.pid)

        with (
            mock.patch.object(workflow_runtime, "cancel_workflow_children") as cancel,
            self.assertRaises(DelegateError) as raised,
        ):
            self.resume(repin=True)

        self.assertEqual(raised.exception.error, "repin_children_running")
        self.assertIn(alias, str(raised.exception))
        self.assertIn("without --repin", str(raised.exception))
        cancel.assert_not_called()
        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual((root / workflow_registry.STATUS_FILE).read_bytes(), status_before)
        # The refusal released the lock: nothing is left holding the workflow.
        os.close(workflow_registry.acquire_workflow_lock(root))

    def test_repin_ignores_children_that_are_not_running(self) -> None:
        self.create_pin(older=True)
        self.seed_workflow()
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.register_child(dead.pid)
        self.register_child(None, status="succeeded")

        with mock.patch.object(workflow_runtime, "cancel_workflow_children", return_value=[]):
            code, stdout, _stderr, _detach = self.resume(repin=True)

        self.assertEqual(code, 0)
        self.assertIs(json.loads(stdout)["runtimePin"]["repinned"], True)

    def register_child(self, pid: int | None, *, status: str = "running") -> str:
        registry_root = run_registry.ensure_registry(self.workspace, workspace_kind="plain")
        run_id, alias = run_registry.register_run(
            registry_root, harness="codex", metadata={"group": WF_ID}
        )
        state: dict[str, object] = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": status,
        }
        if pid is not None:
            state["pid"] = pid
        run_registry.write_json_atomic(
            run_registry.run_directory(registry_root, run_id) / "state.json", state
        )
        return alias

    def test_live_workflow_children_lists_only_live_processes_of_this_workflow(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        live_alias = self.register_child(sleeper.pid)
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.register_child(dead.pid)
        self.register_child(None)
        self.register_child(None, status="succeeded")
        registry_root = run_registry.ensure_registry(self.workspace, workspace_kind="plain")
        other_id, _other_alias = run_registry.register_run(
            registry_root, harness="codex", metadata={"group": "wf_aaaaaaaaaaaa"}
        )
        run_registry.write_json_atomic(
            run_registry.run_directory(registry_root, other_id) / "state.json",
            {
                "schema": run_registry.STATE_SCHEMA,
                "runId": other_id,
                "status": "running",
                "pid": sleeper.pid,
            },
        )

        children = workflow_runtime.live_workflow_children(self.workspace, WF_ID)

        self.assertEqual([child["alias"] for child in children], [live_alias])
        self.assertEqual(children[0]["pid"], sleeper.pid)
        self.assertEqual(
            workflow_runtime.live_workflow_children(self.workspace / "nowhere", WF_ID), []
        )

    def test_repin_is_refused_for_a_pin_that_cannot_be_verified(self) -> None:
        self.create_pin(older=True)
        self.seed_workflow()
        path = workflow_pinning.pin_path(WF_ID, home=self.home)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["configDigest"] = "0" * 64
        path.parent.chmod(0o700)
        path.chmod(0o600)
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o400)
        path.parent.chmod(0o500)
        before = self.pin_bytes()

        with self.assertRaises(DelegateError) as raised:
            self.resume(repin=True)

        self.assertEqual(raised.exception.error, "invalid_pin")
        self.assertEqual(self.pin_bytes(), before)

    def test_repin_cannot_be_combined_with_a_new_run_or_a_dry_run(self) -> None:
        self.create_pin(older=True)
        self.seed_workflow()
        with self.assertRaises(DelegateError) as fresh:
            workflow_commands.emit_run(
                workflow_commands.WorkflowCommand("run", script="x.py", repin=True),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        with self.assertRaises(DelegateError) as dry:
            workflow_commands.emit_run(
                workflow_commands.WorkflowCommand("run", resume=WF_ID, repin=True, dry_run=True),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(fresh.exception.error, "invalid_option_combination")
        self.assertEqual(dry.exception.error, "invalid_option_combination")
        self.assertIn("--dry-run", str(dry.exception))


class RepinParserTests(unittest.TestCase):
    def test_repin_is_accepted_by_resume_run_resume_and_approve_only(self) -> None:
        for argv in (
            ["workflow", "resume", WF_ID, "--repin"],
            ["workflow", "run", "--resume", WF_ID, "--repin"],
            ["workflow", "approve", WF_ID, "--repin"],
        ):
            with self.subTest(argv=argv):
                self.assertIs(cli_parser.parse_cli(argv).payload.repin, True)
        self.assertIs(cli_parser.parse_cli(["workflow", "resume", WF_ID]).payload.repin, False)
        for action in ("status", "events", "kill", "wait", "result"):
            with self.subTest(action=action), self.assertRaises(DelegateError):
                cli_parser.parse_cli(["workflow", action, WF_ID, "--repin"])

    def test_approve_passes_repin_to_the_resume_it_performs(self) -> None:
        seen: list[workflow_commands.WorkflowCommand] = []

        def fake_run(command: workflow_commands.WorkflowCommand, **_kwargs: object) -> int:
            seen.append(command)
            return 0

        with tempfile.TemporaryDirectory() as workspace:
            workflow_registry.ensure_workflow_dir(Path(workspace), WF_ID)
            with (
                mock.patch.object(workflow_commands, "emit_run", fake_run),
                mock.patch.object(workflow_commands.time, "sleep"),
            ):
                for repin in (True, False):
                    workflow_commands.emit_approve(
                        workflow_commands.WorkflowCommand("approve", wf_id=WF_ID, repin=repin),
                        workspace=Path(workspace),
                        config={},
                        stdout=io.StringIO(),
                    )

        self.assertEqual([command.repin for command in seen], [True, False])


if __name__ == "__main__":
    unittest.main()
