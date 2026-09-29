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
        self.assertIsNone(result.previous)
        self.assertFalse(workflow_pinning.repin_backup_path(WF_ID, home=self.home).exists())
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

    def pin_directory_files(self) -> list[str]:
        return sorted(
            path.name for path in workflow_pinning.pin_directory(WF_ID, home=self.home).iterdir()
        )

    def test_a_replacement_pin_is_validated_before_it_can_become_the_pin(self) -> None:
        self.create_pin(older=True)
        before = self.pin_bytes()
        live = workflow_pinning.live_runtime_digest()
        real_digest = workflow_pinning._runtime_directory_digest
        pin_when_validated: list[bytes] = []

        def reject_the_live_runtime(root: Path) -> str:
            if root.name == live:
                pin_when_validated.append(self.pin_bytes())
                return "0" * 64
            return real_digest(root)

        with (
            mock.patch.object(
                workflow_pinning, "_runtime_directory_digest", reject_the_live_runtime
            ),
            self.assertRaises(workflow_pinning.WorkflowPinError),
        ):
            workflow_pinning.repin_to_live(WF_ID, home=self.home)

        # The replacement was judged while pin.json still held the old pin, and
        # the failed repin left neither the replacement nor a backup behind.
        self.assertEqual(pin_when_validated, [before])
        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual(self.pin_directory_files(), ["config.json", "pin.json"])
        self.assertEqual(
            workflow_pinning.pin_directory(WF_ID, home=self.home).stat().st_mode & 0o777, 0o500
        )

    def test_an_interruption_while_the_replacement_is_validated_leaves_the_old_pin(self) -> None:
        class Interrupted(BaseException):
            pass

        self.create_pin(older=True)
        before = self.pin_bytes()
        live = workflow_pinning.live_runtime_digest()
        real_digest = workflow_pinning._runtime_directory_digest

        def die_validating_the_live_runtime(root: Path) -> str:
            if root.name == live:
                raise Interrupted
            return real_digest(root)

        with (
            mock.patch.object(
                workflow_pinning, "_runtime_directory_digest", die_validating_the_live_runtime
            ),
            self.assertRaises(Interrupted),
        ):
            workflow_pinning.repin_to_live(WF_ID, home=self.home)

        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual(self.pin_directory_files(), ["config.json", "pin.json"])
        loaded = workflow_pinning.load_pin(WF_ID, home=self.home)
        assert loaded is not None
        self.assertNotEqual(loaded.runtime_digest, live)

    def test_the_old_pin_is_on_disk_before_the_new_one_is_published(self) -> None:
        old = self.create_pin(older=True)
        before = self.pin_bytes()
        backups: list[bytes] = []
        real_publish = workflow_pinning._publish_staged_pin

        def look_first(staged: Path, path: Path) -> None:
            backups.append(workflow_pinning.repin_backup_path(WF_ID, home=self.home).read_bytes())
            real_publish(staged, path)

        with mock.patch.object(workflow_pinning, "_publish_staged_pin", look_first):
            result = workflow_pinning.repin_to_live(WF_ID, home=self.home)

        self.assertEqual(backups, [before])
        # It stays until the resume has launched, byte for byte, sealed.
        backup = workflow_pinning.repin_backup_path(WF_ID, home=self.home)
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o400)
        self.assertNotEqual(self.pin_bytes(), before)
        assert result.previous is not None
        self.assertEqual(result.previous["digest"], old.runtime_digest)

    def test_rollback_puts_the_pin_back_from_disk_without_the_repin_result(self) -> None:
        """An exception after publication but before repin_to_live returns leaves no result.

        Everything rollback needs is then the backup on disk.
        """
        old = self.create_pin(older=True)
        before = self.pin_bytes()
        with (
            mock.patch.object(
                workflow_pinning, "replace", side_effect=RuntimeError("died after the rename")
            ),
            self.assertRaisesRegex(RuntimeError, "died after the rename"),
        ):
            workflow_pinning.repin_to_live(WF_ID, home=self.home)
        live = workflow_pinning.live_runtime_digest()
        # The move is on disk and the way back is beside it.
        self.assertEqual(workflow_pinning.load_pin(WF_ID, home=self.home).runtime_digest, live)
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID, home=self.home).exists())

        undone = workflow_pinning.rollback_repin(WF_ID, home=self.home)

        self.assertEqual(undone, {"abandonedDigest": live, "restoredDigest": old.runtime_digest})
        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual(self.pin_directory_files(), ["config.json", "pin.json"])
        self.assertEqual(
            workflow_pinning.pin_directory(WF_ID, home=self.home).stat().st_mode & 0o777, 0o500
        )
        self.assertEqual(
            workflow_pinning.pin_path(WF_ID, home=self.home).stat().st_mode & 0o777, 0o400
        )
        self.assertIsNone(workflow_pinning.rollback_repin(WF_ID, home=self.home))

    def test_an_interruption_right_after_the_rename_keeps_the_backup(self) -> None:
        class Interrupted(BaseException):
            pass

        old = self.create_pin(older=True)
        before = self.pin_bytes()
        live = workflow_pinning.live_runtime_digest()
        real_publish = workflow_pinning._publish_staged_pin

        def die_after_the_rename(staged: Path, path: Path) -> None:
            real_publish(staged, path)
            raise Interrupted

        with (
            mock.patch.object(workflow_pinning, "_publish_staged_pin", die_after_the_rename),
            self.assertRaises(Interrupted),
        ):
            workflow_pinning.repin_to_live(WF_ID, home=self.home)

        # The move happened, so the copy of the old pin is what undoes it.
        self.assertEqual(json.loads(self.pin_bytes())["runtime"]["digest"], live)
        self.assertEqual(
            workflow_pinning.repin_backup_path(WF_ID, home=self.home).read_bytes(), before
        )
        undone = workflow_pinning.rollback_repin(WF_ID, home=self.home)
        assert undone is not None
        self.assertEqual(undone["restoredDigest"], old.runtime_digest)
        self.assertEqual(self.pin_bytes(), before)

    def test_rollback_refuses_a_backup_that_is_not_this_workflows_pin(self) -> None:
        self.create_pin(older=True)
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        after = self.pin_bytes()
        backup = workflow_pinning.repin_backup_path(WF_ID, home=self.home)
        someone_elses = json.loads(backup.read_text(encoding="utf-8"))
        someone_elses["workflowId"] = "wf_aaaaaaaaaaaa"
        for label, text in (
            ("not a pin", '{"not": "a pin"}'),
            ("not json", "half a pi"),
            ("another workflow's pin", json.dumps(someone_elses)),
        ):
            with self.subTest(label):
                backup.parent.chmod(0o700)
                backup.chmod(0o600)
                backup.write_text(text, encoding="utf-8")
                backup.chmod(0o400)
                backup.parent.chmod(0o500)

                with self.assertRaises(workflow_pinning.WorkflowPinError) as raised:
                    workflow_pinning.rollback_repin(WF_ID, home=self.home)

                self.assertEqual(raised.exception.error, "invalid_pin")
                self.assertEqual(self.pin_bytes(), after)
                self.assertTrue(backup.exists())

    def test_a_second_repin_will_not_overwrite_an_unfinished_ones_backup(self) -> None:
        self.create_pin(older=True)
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        backup = workflow_pinning.repin_backup_path(WF_ID, home=self.home)
        kept = backup.read_bytes()
        after = self.pin_bytes()

        with (
            mock.patch.object(workflow_pinning, "live_runtime_digest", return_value="1" * 64),
            self.assertRaises(workflow_pinning.WorkflowPinError) as raised,
        ):
            workflow_pinning.repin_to_live(WF_ID, home=self.home)

        self.assertEqual(raised.exception.error, "repin_incomplete")
        self.assertEqual(backup.read_bytes(), kept)
        self.assertEqual(self.pin_bytes(), after)

    def test_commit_removes_the_backup_and_never_raises(self) -> None:
        self.create_pin(older=True)
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        backup = workflow_pinning.repin_backup_path(WF_ID, home=self.home)
        after = self.pin_bytes()

        workflow_pinning.commit_repin(WF_ID, home=self.home)
        workflow_pinning.commit_repin(WF_ID, home=self.home)

        self.assertFalse(backup.exists())
        self.assertEqual(self.pin_bytes(), after)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o500)

    def test_a_backup_that_cannot_be_removed_raises_and_reseals(self) -> None:
        # The supervisor calls commit before any step; a backup it cannot retire
        # must stop it, or a later resume would roll the pin back under work
        # that ran on the new runtime.
        self.create_pin(older=True)
        workflow_pinning.repin_to_live(WF_ID, home=self.home)

        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("sealed")),
            self.assertRaises(PermissionError),
        ):
            workflow_pinning.commit_repin(WF_ID, home=self.home)

        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID, home=self.home).exists())
        self.assertEqual(
            workflow_pinning.pin_directory(WF_ID, home=self.home).stat().st_mode & 0o777, 0o500
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
        # As late as the parent can write: once the supervisor starts it owns the
        # journal, so the event follows attempt_config and precedes the launch.
        self.assertLess(events.index("attempt_config"), events.index("runtime_repinned"))
        self.assertNotIn("runtime_repin_rolled_back", events)
        # A launch is not proof that anything ran on the new pin: the way back
        # stays until the supervisor retires it before its first step.
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID, home=self.home).exists())

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

    def emit_resume_with_repin(self) -> None:
        workflow_commands.emit_run(
            workflow_commands.WorkflowCommand("run", resume=WF_ID, repin=True, json_mode=True),
            workspace=self.workspace,
            config={},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    def test_the_repin_is_journaled_before_the_supervisor_launches(self) -> None:
        old = self.create_pin(older=True)
        root = self.seed_workflow()
        seen: list[list[str]] = []

        def launch(*_args: object, **_kwargs: object) -> None:
            # Once the supervisor is running it owns the journal; the parent's
            # record of the move has to be written by now.
            seen.append(
                [
                    event["type"]
                    for event in workflow_registry.iter_journal(
                        root / workflow_registry.JOURNAL_FILE
                    )
                ]
            )

        with mock.patch.object(workflow_runtime, "detach_supervisor", side_effect=launch):
            self.emit_resume_with_repin()

        (events,) = seen
        self.assertIn("runtime_repinned", events)
        (repinned,) = self.journal(root, "runtime_repinned")
        self.assertEqual(repinned["fromDigest"], old.runtime_digest)
        # The supervisor, not the launching resume, retires the backup.
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID).exists())

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
            self.emit_resume_with_repin()

        self.assertEqual(self.pin_bytes(), before)
        self.assertEqual((root / workflow_registry.STATUS_FILE).read_bytes(), status_before)
        self.assertFalse(workflow_pinning.repin_backup_path(WF_ID).exists())
        self.assertEqual(self.pin_directory_files(), ["config.json", "pin.json"])

    def test_a_failed_launch_does_not_leave_the_journal_claiming_the_move(self) -> None:
        old = self.create_pin(older=True)
        root = self.seed_workflow()
        live = workflow_pinning.live_runtime_digest()

        with (
            mock.patch.object(
                workflow_runtime, "detach_supervisor", side_effect=RuntimeError("launch failed")
            ),
            self.assertRaisesRegex(RuntimeError, "launch failed"),
        ):
            self.emit_resume_with_repin()

        events = [
            event
            for event in workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
            if str(event.get("type")).startswith("runtime_repin")
        ]
        # The journal may say the workflow moved only if it also says it did not stay moved.
        self.assertEqual(
            [event["type"] for event in events], ["runtime_repinned", "runtime_repin_rolled_back"]
        )
        moved, undone = events
        self.assertEqual(moved["toDigest"], live)
        self.assertEqual(undone["reason"], "launch_failed")
        self.assertEqual(undone["abandonedDigest"], live)
        self.assertEqual(undone["restoredDigest"], old.runtime_digest)
        self.assertGreater(undone["seq"], moved["seq"])

    def test_a_resume_that_fails_before_launch_restores_the_old_pin(self) -> None:
        self.create_pin(older=True)
        root = self.seed_workflow()
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
            self.emit_resume_with_repin()

        self.assertEqual(raised.exception.error, "workflow_children_unsealed")
        self.assertEqual(self.pin_bytes(), before)
        self.assertFalse(workflow_pinning.repin_backup_path(WF_ID).exists())
        # Nothing was journaled about a move that never got as far as a launch.
        self.assertEqual(self.journal(root, "runtime_repinned"), [])
        self.assertEqual(self.journal(root, "runtime_repin_rolled_back"), [])

    def leave_an_interrupted_repin(self, root: Path, *, journaled: bool) -> str:
        """The state a process death leaves: new pin published, backup kept, no launch."""
        old = self.create_pin(older=True)
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        live = workflow_pinning.live_runtime_digest()
        if journaled:
            workflow_commands._append_command_event(
                root,
                "runtime_repinned",
                fromDigest=old.runtime_digest,
                fromVersion=OLD_VERSION,
                toDigest=live,
            )
        return old.runtime_digest

    def test_a_resume_after_an_interrupted_repin_puts_the_old_pin_back(self) -> None:
        root = self.seed_workflow()
        old_digest = self.leave_an_interrupted_repin(root, journaled=True)
        live = workflow_pinning.live_runtime_digest()
        moved_pin = self.pin_bytes()

        code, stdout, _stderr, detach = self.resume()

        self.assertEqual(code, 0)
        pinned = workflow_pinning.load_pin(WF_ID)
        assert pinned is not None
        self.assertEqual(pinned.runtime_digest, old_digest)
        self.assertNotEqual(self.pin_bytes(), moved_pin)
        self.assertFalse(workflow_pinning.repin_backup_path(WF_ID).exists())
        self.assertEqual(self.pin_directory_files(), ["config.json", "pin.json"])
        # The resume went on to run on the runtime the pin actually names.
        self.assertEqual(json.loads(stdout)["attemptConfig"]["baseRuntimeDigest"], old_digest)
        self.assertIn(old_digest, detach.call_args.args[0][1])
        # The journal said the workflow moved; it now says it did not stay moved.
        (moved,) = self.journal(root, "runtime_repinned")
        (undone,) = self.journal(root, "runtime_repin_rolled_back")
        self.assertEqual(undone["reason"], "interrupted")
        self.assertEqual(undone["abandonedDigest"], live)
        self.assertEqual(undone["restoredDigest"], old_digest)
        self.assertGreater(undone["seq"], moved["seq"])

    def test_recovering_a_repin_that_never_reached_the_journal_adds_no_record(self) -> None:
        root = self.seed_workflow()
        old_digest = self.leave_an_interrupted_repin(root, journaled=False)

        code, _stdout, _stderr, _detach = self.resume()

        self.assertEqual(code, 0)
        pinned = workflow_pinning.load_pin(WF_ID)
        assert pinned is not None
        self.assertEqual(pinned.runtime_digest, old_digest)
        self.assertFalse(workflow_pinning.repin_backup_path(WF_ID).exists())
        self.assertEqual(self.journal(root, "runtime_repinned"), [])
        self.assertEqual(self.journal(root, "runtime_repin_rolled_back"), [])

    def test_a_repin_after_an_interrupted_one_starts_from_the_recovered_pin(self) -> None:
        root = self.seed_workflow()
        old_digest = self.leave_an_interrupted_repin(root, journaled=True)
        live = workflow_pinning.live_runtime_digest()

        code, stdout, _stderr, _detach = self.resume(repin=True)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["runtimePin"]["previous"]["digest"], old_digest)
        # The new repin's backup is the recovered pin, kept until its supervisor runs.
        backup_text = workflow_pinning.repin_backup_path(WF_ID).read_text(encoding="utf-8")
        self.assertEqual(workflow_pinning._document_runtime_digest(backup_text), old_digest)
        records = [
            event["type"]
            for event in workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
            if str(event.get("type")).startswith("runtime_repin")
        ]
        self.assertEqual(
            records, ["runtime_repinned", "runtime_repin_rolled_back", "runtime_repinned"]
        )
        # The workflow's runtime history records the one real move, not the undone one.
        history = json.loads(self.pin_bytes())["runtimeHistory"]
        self.assertEqual([entry["digest"] for entry in history], [old_digest])
        self.assertEqual(json.loads(self.pin_bytes())["runtime"]["digest"], live)

    def test_a_dry_run_resume_leaves_an_interrupted_repin_alone(self) -> None:
        root = self.seed_workflow()
        self.leave_an_interrupted_repin(root, journaled=True)
        moved_pin = self.pin_bytes()

        with mock.patch.object(workflow_runtime, "detach_supervisor"):
            workflow_commands.emit_run(
                workflow_commands.WorkflowCommand(
                    "run", resume=WF_ID, dry_run=True, json_mode=True
                ),
                workspace=self.workspace,
                config={},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(self.pin_bytes(), moved_pin)
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID).exists())
        self.assertEqual(self.journal(root, "runtime_repin_rolled_back"), [])

    def test_recovery_waits_for_a_live_supervisor_instead_of_moving_its_pin(self) -> None:
        root = self.seed_workflow()
        self.leave_an_interrupted_repin(root, journaled=True)
        moved_pin = self.pin_bytes()
        lock_fd = workflow_registry.acquire_workflow_lock(root)
        self.addCleanup(os.close, lock_fd)

        with self.assertRaises(DelegateError) as raised:
            self.resume()

        self.assertEqual(raised.exception.error, "workflow_locked")
        self.assertEqual(self.pin_bytes(), moved_pin)
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID).exists())

    def test_a_failed_rollback_leaves_the_backup_for_the_next_resume(self) -> None:
        old = self.create_pin(older=True)
        root = self.seed_workflow()
        live = workflow_pinning.live_runtime_digest()

        with (
            mock.patch.object(
                workflow_runtime, "detach_supervisor", side_effect=RuntimeError("launch failed")
            ),
            mock.patch.object(
                workflow_pinning, "rollback_repin", side_effect=OSError("disk went away")
            ),
            self.assertRaisesRegex(RuntimeError, "launch failed"),
        ):
            self.emit_resume_with_repin()

        # The launch error is what the operator sees, and the way back is still there.
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID).exists())
        self.assertEqual(self.journal(root, "runtime_repin_rolled_back"), [])

        code, _stdout, _stderr, _detach = self.resume()

        self.assertEqual(code, 0)
        pinned = workflow_pinning.load_pin(WF_ID)
        assert pinned is not None
        self.assertEqual(pinned.runtime_digest, old.runtime_digest)
        (undone,) = self.journal(root, "runtime_repin_rolled_back")
        self.assertEqual(undone["reason"], "interrupted")
        self.assertEqual(undone["abandonedDigest"], live)

    def test_a_failure_after_the_supervisor_launched_does_not_undo_the_repin(self) -> None:
        self.create_pin(older=True)
        root = self.seed_workflow()
        live = workflow_pinning.live_runtime_digest()

        with (
            mock.patch.object(workflow_runtime, "detach_supervisor"),
            mock.patch.object(
                workflow_pinning,
                "temporarily_apply_environment",
                return_value={"DELEGATE_REPIN_TEST_MARKER": None},
            ),
            mock.patch.object(
                workflow_pinning,
                "restore_environment",
                side_effect=RuntimeError("after the launch"),
            ),
            self.assertRaisesRegex(RuntimeError, "after the launch"),
        ):
            self.emit_resume_with_repin()

        # The supervisor is running on the new pin; putting the old one back
        # would make its children fail their attempt check.
        pinned = workflow_pinning.load_pin(WF_ID)
        assert pinned is not None
        self.assertEqual(pinned.runtime_digest, live)
        self.assertEqual(self.journal(root, "runtime_repin_rolled_back"), [])

    def test_recovery_records_only_a_move_the_journal_actually_names(self) -> None:
        root = self.seed_workflow()
        old = self.create_pin(older=True)
        live = workflow_pinning.live_runtime_digest()
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        # The journal names a move to some other runtime, not the one on the pin.
        workflow_commands._append_command_event(
            root, "runtime_repinned", fromDigest=old.runtime_digest, toDigest="f" * 64
        )

        self.resume()

        self.assertEqual(self.journal(root, "runtime_repin_rolled_back"), [])
        pinned = workflow_pinning.load_pin(WF_ID)
        assert pinned is not None
        self.assertEqual(pinned.runtime_digest, old.runtime_digest)

        # A move that was journaled and already taken back, then a later
        # interrupted repin that never reached the journal: the old record is
        # not a record of this one.
        workflow_commands._append_command_event(
            root, "runtime_repinned", fromDigest=old.runtime_digest, toDigest=live
        )
        workflow_commands._append_command_event(
            root,
            "runtime_repin_rolled_back",
            reason="launch_failed",
            abandonedDigest=live,
            restoredDigest=old.runtime_digest,
        )
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        self.seed_workflow()

        self.resume()

        (undone,) = self.journal(root, "runtime_repin_rolled_back")
        self.assertEqual(undone["reason"], "launch_failed")
        self.assertFalse(workflow_pinning.repin_backup_path(WF_ID).exists())

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

    def register_child(self, pid: int | None, *, status: str = "running", **extra: object) -> str:
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
        state.update(extra)
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

    # Round-three fixes: only a supervisor that runs on the new pin retires the
    # backup; the removal is durable; a child that has not published its pid
    # yet still blocks a repin.

    def seed_supervisable_workflow(self) -> Path:
        root = self.seed_workflow(status="running")
        status = workflow_registry.read_json(root / workflow_registry.STATUS_FILE) or {}
        status["workflowKeyVersion"] = workflow_runtime.WORKFLOW_KEY_VERSION
        workflow_registry.write_status(root, status)
        return root

    def run_supervisor_with(self, execute: mock.MagicMock) -> int:
        with (
            mock.patch.object(workflow_runtime, "execute_workflow", execute),
            mock.patch.object(workflow_runtime._SupervisorWatchdog, "start"),
            mock.patch.object(workflow_runtime._SignalRelay, "start"),
        ):
            return workflow_runtime.run_supervisor(
                workspace=self.workspace, wf_id=WF_ID, cli_argv=[], config={}
            )

    def test_the_resume_leaves_the_backup_for_the_supervisor_to_retire(self) -> None:
        self.create_pin(older=True)
        self.seed_workflow()

        code, _stdout, _stderr, detach = self.resume(repin=True)

        self.assertEqual(code, 0)
        detach.assert_called_once()
        # The launch alone proves nothing ran on the new pin.
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID, home=self.home).exists())

    def test_the_supervisor_retires_the_backup_before_its_first_step(self) -> None:
        self.create_pin(older=True)
        self.seed_supervisable_workflow()
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        backup = workflow_pinning.repin_backup_path(WF_ID, home=self.home)
        seen: list[bool] = []

        def execute(_state: object) -> object:
            seen.append(backup.exists())
            raise RuntimeError("stop after the first step")

        self.run_supervisor_with(mock.MagicMock(side_effect=execute))

        self.assertEqual(seen, [False])
        self.assertFalse(backup.exists())

    def test_a_supervisor_that_cannot_retire_the_backup_runs_nothing(self) -> None:
        self.create_pin(older=True)
        root = self.seed_supervisable_workflow()
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        execute = mock.MagicMock(return_value=None)

        with mock.patch.object(
            workflow_pinning, "commit_repin", side_effect=PermissionError("sealed")
        ):
            code = self.run_supervisor_with(execute)

        self.assertNotEqual(code, 0)
        execute.assert_not_called()
        self.assertTrue(workflow_pinning.repin_backup_path(WF_ID, home=self.home).exists())
        self.assertTrue(self.journal(root, "workflow_failed"))

    def test_commit_syncs_the_directory_after_the_backup_is_gone(self) -> None:
        self.create_pin(older=True)
        workflow_pinning.repin_to_live(WF_ID, home=self.home)
        backup = workflow_pinning.repin_backup_path(WF_ID, home=self.home)
        synced: list[tuple[Path, bool]] = []
        real_sync = workflow_pinning._sync_directory

        def sync(directory: Path) -> None:
            synced.append((directory, backup.exists()))
            real_sync(directory)

        with mock.patch.object(workflow_pinning, "_sync_directory", side_effect=sync):
            workflow_pinning.commit_repin(WF_ID, home=self.home)

        self.assertEqual(synced, [(backup.parent, False)])

    def test_directory_sync_raises_real_errors_and_tolerates_unsupported(self) -> None:
        import errno

        for code, raises in ((errno.EIO, True), (errno.EINVAL, False), (errno.ENOTSUP, False)):
            with (
                self.subTest(errno=errno.errorcode[code]),
                mock.patch.object(os, "fsync", side_effect=OSError(code, "sync")),
            ):
                if raises:
                    with self.assertRaises(OSError):
                        workflow_pinning._sync_directory(self.home)
                else:
                    workflow_pinning._sync_directory(self.home)

    def test_repin_refuses_a_child_that_has_not_published_its_pid_yet(self) -> None:
        now = run_registry.utc_now_iso()
        old = "2026-01-01T00:00:00Z"
        dead = subprocess.Popen(["true"])
        dead.wait()
        cases = (
            ("recent row, no launcher", {"lastActivityAt": now}, True),
            ("old row, live launcher", {"lastActivityAt": old, "launcherPid": os.getpid()}, True),
            ("recent row, dead launcher", {"lastActivityAt": now, "launcherPid": dead.pid}, False),
            ("old row, no launcher", {"lastActivityAt": old}, False),
        )
        for label, extra, refused in cases:
            with self.subTest(label):
                other = tempfile.TemporaryDirectory()
                self.addCleanup(other.cleanup)
                base = Path(other.name)
                self.home = base / "home"
                (self.home / ".delegate" / "personas").mkdir(parents=True)
                self.workspace = base / "workspace"
                self.workspace.mkdir()
                with (
                    mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                    mock.patch.object(
                        workflow_runtime, "cancel_workflow_children", return_value=[]
                    ),
                ):
                    self.create_pin(older=True)
                    self.seed_workflow()
                    before = self.pin_bytes()
                    alias = self.register_child(None, **extra)
                    if refused:
                        with self.assertRaises(DelegateError) as raised:
                            self.resume(repin=True)
                        self.assertEqual(raised.exception.error, "repin_children_running")
                        self.assertIn(alias, str(raised.exception))
                        self.assertEqual(self.pin_bytes(), before)
                    else:
                        code, _stdout, _stderr, _detach = self.resume(repin=True)
                        self.assertEqual(code, 0)


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
