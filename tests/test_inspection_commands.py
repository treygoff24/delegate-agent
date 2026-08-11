import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.delegate_fixtures import seed_persistent_worktree_run, write_snapshot_run

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import (  # noqa: E402
    cli,
    command_errors,
    inspection_commands,
    rendering,
    run_registry,
)


class InspectionCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def write_run(
        self,
        *,
        harness: str = "cursor",
        status: str = "running",
        assistant_text: str = "planning the change",
        pid: int | None = os.getpid(),
        started_at: str | None = None,
        group: str | None = None,
    ) -> tuple[str, str]:
        run_id, alias = write_snapshot_run(
            run_registry,
            self.registry_root,
            self.workspace,
            harness=harness,
            status=status,
            assistant_text=assistant_text,
            pid=pid,
            started_at=started_at,
        )
        if group is not None:
            index = run_registry.load_index(self.registry_root)
            entry = index["runs"][run_id]
            if isinstance(entry, dict):
                entry["group"] = group
            run_registry.save_index(self.registry_root, index)
        return run_id, alias

    def seed_old_persistent_worktree_run(self, *, worktree_status: str) -> tuple[str, str]:
        subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
        (self.workspace / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=self.workspace, check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Delegate Test",
                "-c",
                "user.email=delegate-test@example.com",
                "commit",
                "-m",
                "init",
            ],
            cwd=self.workspace,
            check=True,
            capture_output=True,
        )

        def git_runner(*args: str, cwd: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
            )

        return seed_persistent_worktree_run(
            cli,
            str(self.workspace),
            git_runner=git_runner,
            worktree_status=worktree_status,
            last_activity_at="2020-01-01T00:00:00Z",
        )

    def test_resolve_latest_harness_returns_latest_alias(self):
        self.write_run(
            harness="codex",
            assistant_text="first",
            started_at="2026-05-20T12:00:00Z",
        )
        latest_run_id, latest_alias = self.write_run(
            harness="codex",
            assistant_text="second",
            started_at="2026-05-20T12:05:00Z",
        )

        run_id, alias = command_errors.resolve_run_target(
            self.registry_root,
            handle=None,
            latest_harness="codex",
            error_cls=inspection_commands.InspectionError,
        )

        self.assertEqual(run_id, latest_run_id)
        self.assertEqual(alias, latest_alias)

    def test_emit_snapshot_bare_harness_exposes_resolution_fields_in_json(self):
        self.write_run(
            harness="codex",
            assistant_text="first",
            started_at="2026-05-20T12:00:00Z",
        )
        latest_run_id, latest_alias = self.write_run(
            harness="codex",
            assistant_text="second",
            started_at="2026-05-20T12:05:00Z",
        )
        stdout = io.StringIO()

        code = inspection_commands.emit_snapshot(
            inspection_commands.SnapshotCommand(handle="codex", json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["runId"], latest_run_id)
        self.assertEqual(payload["alias"], latest_alias)
        self.assertEqual(payload["requestedHandle"], "codex")
        self.assertEqual(payload["resolvedHandle"], latest_alias)
        self.assertEqual(payload["resolutionKind"], "latest")

    def test_emit_snapshot_bare_harness_renders_resolution_banner_in_text(self):
        self.write_run(
            harness="codex",
            assistant_text="first",
            started_at="2026-05-20T12:00:00Z",
        )
        _, latest_alias = self.write_run(
            harness="codex",
            assistant_text="second",
            started_at="2026-05-20T12:05:00Z",
        )
        stdout = io.StringIO()

        code = inspection_commands.emit_snapshot(
            inspection_commands.SnapshotCommand(handle="codex"),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn(f"resolved handle: codex -> {latest_alias} (latest)", output)

    def test_emit_runs_group_filter(self):
        self.write_run(harness="codex", group="wave4", assistant_text="included")
        self.write_run(harness="codex", group="other", assistant_text="excluded")
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="wave4", json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual(payload["runs"][0]["group"], "wave4")

    def test_emit_runs_projects_initiator_root_from_registry_metadata(self):
        run_id, _alias = self.write_run(harness="codex", assistant_text="included")
        index = run_registry.load_index(self.registry_root)
        index["runs"][run_id]["initiatorRoot"] = "codex:root-thread"
        run_registry.save_index(self.registry_root, index)
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["runs"][0]["initiatorRoot"], "codex:root-thread")

    def test_emit_runs_structural_projection_omits_content_fields(self):
        run_id, _alias = self.write_run(harness="codex", assistant_text="included")
        index = run_registry.load_index(self.registry_root)
        index["runs"][run_id]["initiatorRoot"] = "codex:root-thread"
        run_registry.save_index(self.registry_root, index)
        state = run_registry.load_run_state(self.registry_root, run_id)
        state.update(
            {"current": "secret current", "message": "secret message", "error": "secret error"}
        )
        run_registry.write_json_atomic(
            run_registry.run_directory(self.registry_root, run_id) / "state.json", state
        )
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(structural=True, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        summary = payload["runs"][0]
        self.assertEqual(summary["initiatorRoot"], "codex:root-thread")
        self.assertEqual(summary["runId"], run_id)
        self.assertFalse({"current", "message", "error", "nextActions"} & summary.keys())
        self.assertLessEqual(set(summary), set(inspection_commands.STRUCTURAL_RUN_KEYS))

    def test_emit_runs_structural_omits_terminal_event_content_and_redacts_projection(self):
        run_id, _alias = self.write_run(harness="codex", assistant_text="included")
        index = run_registry.load_index(self.registry_root)
        index["runs"][run_id]["initiatorRoot"] = "codex:root-thread"
        run_registry.save_index(self.registry_root, index)
        state = run_registry.load_run_state(self.registry_root, run_id)
        state.update(
            {
                "status": "failed",
                "terminalStatus": "failed",
                "terminalEvent": {
                    "event": "turn.failed",
                    "status": "failed",
                    "reason": (
                        "provider rejected Authorization: Bearer raw-provider-token-1234567890"
                    ),
                },
            }
        )
        run_path = run_registry.run_directory(self.registry_root, run_id)
        run_registry.write_json_atomic(run_path / "state.json", state)
        manifest = run_registry.load_run_manifest(self.registry_root, run_id)
        manifest["modelResolved"] = "model with token=structural-secret-value"
        run_registry.write_json_atomic(run_path / "manifest.json", manifest)
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(structural=True, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        output = stdout.getvalue()
        payload = json.loads(output)
        self.assertEqual(code, 0)
        summary = payload["runs"][0]
        self.assertEqual(summary["initiatorRoot"], "codex:root-thread")
        self.assertEqual(summary["rawStatus"], "failed")
        self.assertEqual(summary["effectiveStatus"], "failed")
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["terminalStatus"], "failed")
        self.assertNotIn("terminalEvent", summary)
        self.assertNotIn("provider rejected", output)
        self.assertNotIn("raw-provider-token-1234567890", output)
        self.assertNotIn("structural-secret-value", output)

    def test_emit_snapshot_literal_handle_omits_resolution_fields(self):
        run_id, alias = self.write_run(harness="cursor", assistant_text="literal run")
        stdout = io.StringIO()

        code = inspection_commands.emit_snapshot(
            inspection_commands.SnapshotCommand(handle=alias, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["runId"], run_id)
        self.assertNotIn("requestedHandle", payload)
        self.assertNotIn("resolvedHandle", payload)
        self.assertNotIn("resolutionKind", payload)

    def test_emit_snapshot_json_uses_registry_view(self):
        run_id, alias = self.write_run(assistant_text="adapter output")
        stdout = io.StringIO()

        code = inspection_commands.emit_snapshot(
            inspection_commands.SnapshotCommand(handle=alias, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], run_registry.SNAPSHOT_SCHEMA)
        self.assertEqual(payload["runId"], run_id)
        self.assertEqual(payload["alias"], alias)
        self.assertEqual(payload["assistantText"], "adapter output")

    def test_emit_runs_json_filters_and_redacts_summaries(self):
        _, alias = self.write_run(assistant_text="adapter output")
        index = run_registry.load_index(self.registry_root)
        run_id = index["aliases"][alias]
        run_path = run_registry.run_directory(self.registry_root, run_id)
        state = run_registry.load_run_state(self.registry_root, run_id)
        state["current"] = "checking token=super-secret-value"
        run_registry.write_json_atomic(run_path / "state.json", state)
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(running=True, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "running")
        self.assertEqual(payload["runs"][0]["alias"], alias)
        self.assertIn("token=", payload["runs"][0]["current"])
        self.assertNotIn("super-secret-value", payload["runs"][0]["current"])

    def test_prune_uses_effective_status_and_respects_age_boundary(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

        def stamp(moment):
            return moment.strftime(run_registry.UTC_TIMESTAMP_FORMAT)

        old_id, old_alias = self.write_run(
            status="succeeded", pid=None, started_at=stamp(now - timedelta(days=31))
        )
        boundary_id, boundary_alias = self.write_run(
            status="succeeded", pid=None, started_at=stamp(now - timedelta(days=30))
        )
        live_id, live_alias = self.write_run(
            status="running", pid=os.getpid(), started_at=stamp(now - timedelta(days=31))
        )
        stale_id, stale_alias = self.write_run(
            status="running", pid=999999999, started_at=stamp(now - timedelta(days=31))
        )
        archive = self.registry_root / "archive"
        archive.mkdir()
        (archive / f"{old_id}.tar.gz").write_bytes(b"archive")

        payload = run_registry.prune_runs(self.registry_root, older_than_days=30, now=now)

        self.assertEqual({entry["alias"] for entry in payload["planned"]}, {old_alias, stale_alias})
        self.assertEqual({entry["alias"] for entry in payload["removed"]}, {old_alias, stale_alias})
        stale_removed = next(entry for entry in payload["removed"] if entry["alias"] == stale_alias)
        self.assertEqual(stale_removed["effectiveStatus"], "stale")
        skipped = {entry["alias"]: entry["reason"] for entry in payload["skipped"]}
        self.assertEqual(skipped[boundary_alias], "not_yet_old_enough")
        self.assertEqual(skipped[live_alias], "running")
        index = run_registry.load_index(self.registry_root)
        self.assertNotIn(old_id, index["runs"])
        self.assertNotIn(stale_id, index["runs"])
        self.assertIn(boundary_id, index["runs"])
        self.assertIn(live_id, index["runs"])
        self.assertFalse(run_registry.run_directory(self.registry_root, old_id).exists())
        self.assertFalse((archive / f"{old_id}.tar.gz").exists())

    def test_prune_dry_run_reports_candidates_without_mutation(self):
        run_id, alias = self.write_run(
            status="succeeded", pid=None, started_at="2020-01-01T00:00:00Z"
        )
        run_path = run_registry.run_directory(self.registry_root, run_id)
        (run_path / "stdout.log").write_text("keep", encoding="utf-8")
        archive = self.registry_root / "archive"
        archive.mkdir()
        archive_path = archive / f"{run_id}.tar.gz"
        archive_path.write_bytes(b"keep")

        stdout = io.StringIO()
        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(
                action="prune", older_than_days=30, dry_run=True, json_mode=True
            ),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], run_registry.RUNS_PRUNE_SCHEMA)
        self.assertTrue(payload["dryRun"])
        self.assertEqual([entry["alias"] for entry in payload["planned"]], [alias])
        self.assertEqual(payload["removed"], [])
        self.assertTrue(run_path.exists())
        self.assertTrue(archive_path.exists())

        text = io.StringIO()
        inspection_commands.emit_runs(
            inspection_commands.RunsCommand(action="prune", older_than_days=30, dry_run=True),
            workspace_path=str(self.workspace),
            stdout=text,
        )
        self.assertIn("dry run: Registry, logs, and artifacts unchanged", text.getvalue())
        self.assertIn(alias, text.getvalue())

    def test_prune_skips_present_persistent_worktree(self):
        run_id, alias = self.seed_old_persistent_worktree_run(worktree_status="present")

        payload = run_registry.prune_runs(
            self.registry_root,
            older_than_days=30,
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )

        self.assertEqual(payload["planned"], [])
        self.assertEqual(payload["removed"], [])
        self.assertEqual(
            payload["skipped"],
            [
                {
                    "alias": alias,
                    "runId": run_id,
                    "effectiveStatus": "succeeded",
                    "reason": "persistent_worktree",
                }
            ],
        )
        self.assertTrue(run_registry.run_directory(self.registry_root, run_id).exists())
        text = io.StringIO()
        rendering.render_runs_prune_text(payload, text)
        self.assertIn(f"{alias} persistent_worktree", text.getvalue())

    def test_prune_skips_unknown_status_persistent_worktree(self):
        run_id, _alias = self.seed_old_persistent_worktree_run(worktree_status="unknown")

        payload = run_registry.prune_runs(
            self.registry_root,
            older_than_days=30,
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )

        self.assertEqual(payload["removed"], [])
        self.assertEqual(payload["skipped"][0]["reason"], "persistent_worktree")
        self.assertTrue(run_registry.run_directory(self.registry_root, run_id).exists())

    def test_prune_removes_persistent_worktree_record_after_worktree_removed(self):
        run_id, alias = self.seed_old_persistent_worktree_run(worktree_status="removed")

        payload = run_registry.prune_runs(
            self.registry_root,
            older_than_days=30,
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )

        self.assertEqual([entry["alias"] for entry in payload["removed"]], [alias])
        self.assertNotIn(run_id, run_registry.load_index(self.registry_root)["runs"])
        self.assertFalse(run_registry.run_directory(self.registry_root, run_id).exists())

    def test_missing_registry_latest_snapshot_raises_inspection_error(self):
        isolated_dir = tempfile.TemporaryDirectory()
        self.addCleanup(isolated_dir.cleanup)
        isolated = Path(isolated_dir.name)

        with self.assertRaises(inspection_commands.InspectionError) as caught:
            inspection_commands.emit_snapshot(
                inspection_commands.SnapshotCommand(handle=None, latest_harness="droid"),
                workspace_path=str(isolated),
                stdout=io.StringIO(),
            )

        self.assertEqual(caught.exception.error, "no_matching_runs")
        self.assertEqual(caught.exception.message, "No runs found for harness: droid")

    def test_missing_registry_snapshot_unknown_handle_message_comes_from_resolver(self):
        isolated_dir = tempfile.TemporaryDirectory()
        self.addCleanup(isolated_dir.cleanup)
        isolated = Path(isolated_dir.name)

        with self.assertRaises(inspection_commands.InspectionError) as caught:
            inspection_commands.emit_snapshot(
                inspection_commands.SnapshotCommand(handle="missing"),
                workspace_path=str(isolated),
                stdout=io.StringIO(),
            )

        self.assertEqual(caught.exception.error, "unknown_handle")
        self.assertEqual(
            caught.exception.message,
            "Unknown run handle: missing. Suggestions: (none). Runs are recorded "
            "per-workspace under <workspace>/.delegate; if this run was launched "
            "elsewhere, pass --cwd <that workspace>.",
        )

    def test_missing_registry_snapshot_missing_handle_keeps_snapshot_message(self):
        isolated_dir = tempfile.TemporaryDirectory()
        self.addCleanup(isolated_dir.cleanup)
        isolated = Path(isolated_dir.name)

        with self.assertRaises(inspection_commands.InspectionError) as caught:
            inspection_commands.emit_snapshot(
                inspection_commands.SnapshotCommand(handle=None),
                workspace_path=str(isolated),
                stdout=io.StringIO(),
            )

        self.assertEqual(caught.exception.error, "missing_handle")
        self.assertEqual(
            caught.exception.message,
            "snapshot requires a run handle or --latest.",
        )

    def test_emit_runs_json_reports_total_and_truncated_when_limit_slices(self):
        for index in range(3):
            self.write_run(
                harness="codex",
                group="wave4",
                started_at=f"2026-05-20T12:0{index}:00Z",
                assistant_text=f"run-{index}",
            )
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="wave4", limit=2, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(payload["total"], 3)
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["runs"]), 2)
        self.assertNotIn("warnings", payload)

        text = io.StringIO()
        inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="wave4", limit=2),
            workspace_path=str(self.workspace),
            stdout=text,
        )
        self.assertIn("showing 2 of 3 runs (raise --limit to see more)", text.getvalue())

    def test_emit_runs_json_truncated_false_when_under_limit(self):
        self.write_run(harness="codex", group="wave4", assistant_text="only")
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="wave4", limit=5, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["total"], 1)
        self.assertFalse(payload["truncated"])
        self.assertEqual(len(payload["runs"]), 1)

        text = io.StringIO()
        inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="wave4", limit=5),
            workspace_path=str(self.workspace),
            stdout=text,
        )
        self.assertNotIn("showing", text.getvalue())

    def test_emit_runs_zero_row_group_warns_workspace_scoped(self):
        self.write_run(harness="codex", group="other", assistant_text="elsewhere")
        stdout = io.StringIO()

        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="missing-wave", json_mode=True),
            workspace_path=str(self.workspace),
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["total"], 0)
        self.assertFalse(payload["truncated"])
        self.assertEqual(len(payload["warnings"]), 1)
        warning = payload["warnings"][0]
        self.assertIn("workspace-scoped", warning)
        self.assertIn("--cwd PATH", warning)
        self.assertNotIn("exists", warning.lower())

        text = io.StringIO()
        inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="missing-wave"),
            workspace_path=str(self.workspace),
            stdout=text,
        )
        output = text.getvalue()
        self.assertIn("warning:", output)
        self.assertIn("workspace-scoped", output)
        self.assertIn("--cwd PATH", output)
        self.assertIn("Registry", warning)

    def test_emit_runs_zero_row_status_filter_warns_drop_flag(self):
        self.write_run(harness="codex", group="pc-w3", status="succeeded", pid=None)
        self.write_run(harness="codex", group="pc-w3", status="failed", pid=None)

        for flag_name, kwargs in (
            ("running", {"running": True}),
            ("stale", {"stale": True}),
            ("active", {"active": True}),
        ):
            with self.subTest(flag=flag_name):
                stdout = io.StringIO()
                code = inspection_commands.emit_runs(
                    inspection_commands.RunsCommand(group="pc-w3", json_mode=True, **kwargs),
                    workspace_path=str(self.workspace),
                    stdout=stdout,
                )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(payload["runs"], [])
                self.assertEqual(len(payload["warnings"]), 1)
                warning = payload["warnings"][0]
                self.assertIn(f"No {flag_name} runs matched", warning)
                self.assertIn(f"Drop --{flag_name}", warning)
                self.assertNotIn("workspace-scoped", warning)
                self.assertNotIn("--cwd", warning)

                text = io.StringIO()
                inspection_commands.emit_runs(
                    inspection_commands.RunsCommand(group="pc-w3", **kwargs),
                    workspace_path=str(self.workspace),
                    stdout=text,
                )
                output = text.getvalue()
                self.assertIn("warning:", output)
                self.assertIn(f"Drop --{flag_name}", output)
                self.assertNotIn("workspace-scoped", output)

        populated = io.StringIO()
        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(group="pc-w3", json_mode=True),
            workspace_path=str(self.workspace),
            stdout=populated,
        )
        populated_payload = json.loads(populated.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(len(populated_payload["runs"]), 2)
        self.assertNotIn("warnings", populated_payload)

        # scope_total semantics: group matches N runs, status filter excludes all
        index = run_registry.load_index(self.registry_root)
        summaries, total, scope_total = run_registry.list_run_summaries(
            self.registry_root,
            index,
            active=True,
            group="pc-w3",
        )
        self.assertEqual(summaries, [])
        self.assertEqual(total, 0)
        self.assertEqual(scope_total, 2)

    def test_emit_runs_absent_group_with_status_filter_warns_workspace_scoped(self):
        self.write_run(harness="codex", group="other", assistant_text="elsewhere")

        for flag_name, kwargs in (
            ("running", {"running": True}),
            ("stale", {"stale": True}),
            ("active", {"active": True}),
        ):
            with self.subTest(flag=flag_name, command="runs"):
                stdout = io.StringIO()
                code = inspection_commands.emit_runs(
                    inspection_commands.RunsCommand(
                        group="definitely-absent", json_mode=True, **kwargs
                    ),
                    workspace_path=str(self.workspace),
                    stdout=stdout,
                )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(payload["runs"], [])
                self.assertEqual(payload["total"], 0)
                self.assertEqual(len(payload["warnings"]), 1)
                warning = payload["warnings"][0]
                self.assertIn("workspace-scoped", warning)
                self.assertIn("--cwd PATH", warning)
                self.assertNotIn(f"No {flag_name} runs matched", warning)
                self.assertNotIn(f"Drop --{flag_name}", warning)

        # ps parity: absent group + --active → workspace-scope warning
        empty = io.StringIO()
        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(active=True, group="definitely-absent", json_mode=True),
            workspace_path=str(self.workspace),
            stdout=empty,
        )
        empty_payload = json.loads(empty.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(empty_payload["mode"], "active")
        self.assertEqual(empty_payload["runs"], [])
        self.assertEqual(len(empty_payload["warnings"]), 1)
        self.assertIn("workspace-scoped", empty_payload["warnings"][0])
        self.assertNotIn("No active runs matched", empty_payload["warnings"][0])

        index = run_registry.load_index(self.registry_root)
        _summaries, _total, scope_total = run_registry.list_run_summaries(
            self.registry_root,
            index,
            active=True,
            group="definitely-absent",
        )
        self.assertEqual(scope_total, 0)

    def test_emit_ps_parity_for_total_truncated_and_empty_filter_warning(self):
        for index in range(3):
            self.write_run(
                harness="codex",
                group="ps-wave",
                started_at=f"2026-05-20T13:0{index}:00Z",
                assistant_text=f"ps-{index}",
            )
        truncated = io.StringIO()
        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(active=True, group="ps-wave", limit=2, json_mode=True),
            workspace_path=str(self.workspace),
            stdout=truncated,
        )
        payload = json.loads(truncated.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "active")
        self.assertEqual(payload["total"], 3)
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["runs"]), 2)

        # Absent group under --active (ps) must use workspace-scope warning, not status.
        empty = io.StringIO()
        code = inspection_commands.emit_runs(
            inspection_commands.RunsCommand(
                active=True, harness="cursor", group="absent", json_mode=True
            ),
            workspace_path=str(self.workspace),
            stdout=empty,
        )
        empty_payload = json.loads(empty.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(empty_payload["mode"], "active")
        self.assertEqual(empty_payload["runs"], [])
        self.assertEqual(empty_payload["total"], 0)
        self.assertEqual(len(empty_payload["warnings"]), 1)
        self.assertIn("workspace-scoped", empty_payload["warnings"][0])
        self.assertIn("--cwd PATH", empty_payload["warnings"][0])
        self.assertNotIn("No active runs matched", empty_payload["warnings"][0])
        self.assertNotIn("Drop --active", empty_payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
