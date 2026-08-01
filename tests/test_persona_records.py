from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from delegate_agent import (
    config,
    describe_payload,
    resume_command,
    retention,
    run_registry,
    run_status,
    runner,
    snapshot_view,
    worktree_execution,
)
from delegate_agent.cli import main, parse_cli, request_from_parsed
from delegate_agent.constants import MODE_WORK
from delegate_agent.isolation import IsolationContext
from delegate_agent.request_models import Request, ResolvedWorkspace


class PersonaRecordTests(unittest.TestCase):
    persona_text = "Recorded persona bytes: é🌍\n"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = run_registry.ensure_registry(self.workspace, workspace_kind="directory")
        persona_dir = Path.home() / ".delegate" / "personas"
        persona_dir.mkdir(parents=True, exist_ok=True)
        (persona_dir / "editor.md").write_text(self.persona_text, encoding="utf-8")

    def _context(self, run_id: str, alias: str, *, persona: bool = True) -> runner.RunContext:
        return runner.RunContext(
            registry_root=self.root,
            run_id=run_id,
            alias=alias,
            harness="cursor",
            engine="cursor",
            mode=MODE_WORK,
            model="composer-2.5",
            source_cwd=str(self.workspace),
            execution_cwd=str(self.workspace),
            workspace_kind="directory",
            isolated_workspace=False,
            started_at="2026-07-01T00:00:00Z",
            source_prompt="the user prompt\n",
            persona_name="editor" if persona else None,
            persona_source="global" if persona else None,
            persona_transport="prepend" if persona else None,
            persona_digest=(
                hashlib.sha256(self.persona_text.encode("utf-8")).hexdigest() if persona else None
            ),
            persona_file="persona.txt" if persona else None,
            persona_text=self.persona_text if persona else None,
        )

    def _seed_resume_run(self) -> tuple[str, Path]:
        run_id, alias = run_registry.register_run(
            self.root,
            harness="cursor",
            metadata={"mode": MODE_WORK, "cwd": str(self.workspace)},
        )
        run_path = run_registry.run_directory(self.root, run_id)
        digest = hashlib.sha256(self.persona_text.encode("utf-8")).hexdigest()
        manifest = {
            "schema": run_registry.MANIFEST_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "harness": "cursor",
            "engine": "cursor",
            "mode": MODE_WORK,
            "model": "composer-2.5",
            "cwd": str(self.workspace),
            "isolationMode": "none",
            "startedAt": "2026-07-01T00:00:00Z",
            "personaName": "editor",
            "personaSource": "global",
            "personaTransport": "prepend",
            "personaDigest": digest,
            "personaFile": run_registry.PERSONA_TXT_FILE,
        }
        state = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": run_registry.STATUS_FAILED,
            "lastActivityAt": "2026-07-01T00:00:00Z",
        }
        snapshot = {
            "schema": run_registry.SNAPSHOT_SCHEMA,
            "runId": run_id,
            "alias": alias,
            "status": run_registry.STATUS_FAILED,
            "assistantText": "previous output",
            "recentEvents": [],
        }
        run_registry.write_json_atomic(run_path / run_registry.MANIFEST_FILE, manifest)
        run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, state)
        run_registry.write_json_atomic(run_path / run_registry.SNAPSHOT_FILE, snapshot)
        run_registry.write_private_text(run_path / run_registry.PROMPT_TXT_FILE, "original task\n")
        run_registry.write_private_text(run_path / run_registry.PERSONA_TXT_FILE, self.persona_text)
        return alias, run_path

    def test_persona_artifact_precedes_first_manifest_on_normal_tracked_seam(self) -> None:
        run_id, alias = run_registry.register_run(
            self.root,
            harness="cursor",
            metadata={"mode": MODE_WORK, "cwd": str(self.workspace)},
        )
        ctx = self._context(run_id, alias)
        files = runner._prepare_tracked_run(["agent", "prompt"], ctx, manifest_argv=["agent"])

        persona_path = files.run_path / run_registry.PERSONA_TXT_FILE
        manifest_path = files.run_path / run_registry.MANIFEST_FILE
        self.assertTrue(persona_path.is_file())
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(persona_path.read_text(encoding="utf-8"), self.persona_text)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                key: manifest[key]
                for key in (
                    "personaName",
                    "personaSource",
                    "personaTransport",
                    "personaDigest",
                    "personaFile",
                )
            },
            {
                "personaName": "editor",
                "personaSource": "global",
                "personaTransport": "prepend",
                "personaDigest": hashlib.sha256(self.persona_text.encode("utf-8")).hexdigest(),
                "personaFile": "persona.txt",
            },
        )

    def test_manifest_records_post_fallback_persona_transport(self) -> None:
        parsed = parse_cli(
            [
                "--cwd",
                str(self.workspace),
                "claude",
                "work",
                "--persona",
                "editor",
                "review",
            ]
        )
        request = request_from_parsed(
            parsed,
            config.embedded_default_config(),
            io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(request.persona_transport, "prepend")
        self.assertTrue(any("not proven by discovery" in warning for warning in request.warnings))
        self.assertEqual(request.persona_file, "persona.txt")

    def test_persistent_worktree_first_manifest_has_persona_artifact(self) -> None:
        run_id, alias = run_registry.register_run(
            self.root,
            harness="cursor",
            metadata={"mode": MODE_WORK, "cwd": str(self.workspace)},
        )
        run_path = run_registry.run_directory(self.root, run_id)
        execution_workspace = self.workspace / "worktree"
        execution_workspace.mkdir()
        iso = IsolationContext(
            source_workspace=str(self.workspace),
            effective_isolation="worktree",
            isolation_mode="worktree",
            isolation_lifecycle="persistent",
            preserved_workspace=True,
            source_git_root=str(self.workspace),
        )
        request = Request(
            engine="cursor",
            mode=MODE_WORK,
            workspace=str(execution_workspace),
            prompt="prompt",
            argv=["agent", "prompt"],
            model="composer-2.5",
            source_prompt="the user prompt\n",
            persona_name="editor",
            persona_source="global",
            persona_transport="prepend",
            persona_digest=hashlib.sha256(self.persona_text.encode("utf-8")).hexdigest(),
            persona_file="persona.txt",
            persona_text=self.persona_text,
            isolation_context=iso,
        )
        execution = worktree_execution.PersistentWorktreeExecution(
            request=request,
            json_mode=True,
            config=config.embedded_default_config(),
            pass_through=False,
            completion_report_mode="none",
            source_workspace=ResolvedWorkspace(str(self.workspace), "directory"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            binary_validator=lambda _argv, _engine: None,
        )
        preflight = worktree_execution.PersistentWorktreePreflight(
            iso_ctx=iso,
            source_git_root=str(self.workspace),
            base_oid="base",
            source_git_common_dir=str(self.workspace),
            source_head_oid="head",
            source_head_ref=None,
            source_branch=None,
            registry_root=self.root,
            tracked_dirty_files=0,
            untracked_files=0,
            dirty_example_paths=(),
            dirty_snapshot=None,
        )
        registration = worktree_execution.PersistentWorktreeRegistration(
            run_id=run_id,
            alias=alias,
            run_path=run_path,
            pre_ctx=self._context(run_id, alias),
            branch="delegate/cursor-test",
            worktree_path=str(execution_workspace),
            creation_context={},
        )
        seen_persona_at_manifest = []
        original_manifest = worktree_execution.delegate_runner.write_manifest

        def observe_manifest(path: Path, manifest: dict[str, object]) -> None:
            seen_persona_at_manifest.append((path / run_registry.PERSONA_TXT_FILE).is_file())
            original_manifest(path, manifest)

        with (
            mock.patch.object(
                worktree_execution.delegate_runner,
                "write_manifest",
                side_effect=observe_manifest,
            ),
            mock.patch.object(
                worktree_execution.delegate_runner,
                "execute_tracked",
                return_value=(0, {"ok": True}),
            ),
            mock.patch.object(worktree_execution.run_registry, "set_worktree_status"),
        ):
            worktree_execution._launch_child_in_persistent_worktree(
                execution, preflight, registration
            )
        self.assertEqual(seen_persona_at_manifest, [True])

    def test_persona_is_not_an_archive_member_and_prune_removes_it(self) -> None:
        self.assertNotIn(run_registry.PERSONA_TXT_FILE, retention.ARCHIVE_MEMBER_NAMES)
        alias, run_path = self._seed_resume_run()
        result = run_registry.prune_runs(
            self.root,
            older_than_days=30,
            now=datetime(2026, 8, 15, tzinfo=UTC),
        )
        self.assertEqual([entry["alias"] for entry in result["removed"]], [alias])
        self.assertFalse(run_path.exists())

    def test_prune_dry_run_writes_nothing_for_persona_artifact(self) -> None:
        _alias, run_path = self._seed_resume_run()
        before = (run_path / run_registry.PERSONA_TXT_FILE).read_bytes()
        result = run_registry.prune_runs(
            self.root,
            older_than_days=30,
            dry_run=True,
            now=datetime(2026, 8, 15, tzinfo=UTC),
        )
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["planned"][0]["runId"], run_path.name)
        self.assertEqual((run_path / run_registry.PERSONA_TXT_FILE).read_bytes(), before)

    def test_snapshot_and_runs_projection_include_persona_only_when_used(self) -> None:
        alias, run_path = self._seed_resume_run()
        run_id = run_path.name
        manifest = run_registry.load_run_manifest(self.root, run_id)
        view = snapshot_view.merge_snapshot_view(
            self.root,
            run_id,
            run_registry.read_json_object(run_path / run_registry.SNAPSHOT_FILE),
            redact=False,
        )
        summary = run_status.build_run_summary(
            self.root, run_id, run_registry.load_index(self.root)["runs"][run_id]
        )
        for payload in (manifest, view, summary):
            self.assertEqual(payload["personaName"], "editor")
            self.assertEqual(payload["personaSource"], "global")
            self.assertEqual(payload["personaTransport"], "prepend")
            self.assertEqual(payload["personaFile"], "persona.txt")
        self.assertEqual(view["alias"], alias)

        unused_id, unused_alias = run_registry.register_run(
            self.root,
            harness="cursor",
            metadata={"mode": MODE_WORK, "cwd": str(self.workspace)},
        )
        unused_path = run_registry.run_directory(self.root, unused_id)
        unused_manifest = {
            "schema": run_registry.MANIFEST_SCHEMA,
            "runId": unused_id,
            "alias": unused_alias,
            "harness": "cursor",
            "engine": "cursor",
            "mode": MODE_WORK,
            "cwd": str(self.workspace),
            "startedAt": "2026-07-01T00:00:00Z",
        }
        unused_state = {
            "schema": run_registry.STATE_SCHEMA,
            "runId": unused_id,
            "alias": unused_alias,
            "status": run_registry.STATUS_FAILED,
            "lastActivityAt": "2026-07-01T00:00:00Z",
        }
        run_registry.write_json_atomic(unused_path / run_registry.MANIFEST_FILE, unused_manifest)
        run_registry.write_json_atomic(unused_path / run_registry.STATE_FILE, unused_state)
        run_registry.write_json_atomic(
            unused_path / run_registry.SNAPSHOT_FILE,
            {"schema": run_registry.SNAPSHOT_SCHEMA, "runId": unused_id, "alias": unused_alias},
        )
        unused_view = snapshot_view.merge_snapshot_view(
            self.root,
            unused_id,
            run_registry.read_json_object(unused_path / run_registry.SNAPSHOT_FILE),
            redact=False,
        )
        unused_summary = run_status.build_run_summary(
            self.root, unused_id, run_registry.load_index(self.root)["runs"][unused_id]
        )
        for payload in (unused_manifest, unused_view, unused_summary):
            for key in (
                "personaName",
                "personaSource",
                "personaTransport",
                "personaDigest",
                "personaFile",
            ):
                self.assertNotIn(key, payload)

    def test_dry_run_does_not_create_persona_or_run_artifacts(self) -> None:
        dry_workspace = self.workspace / "dry-run"
        dry_workspace.mkdir()
        stdout = io.StringIO()
        code = main(
            [
                "--json",
                "--cwd",
                str(dry_workspace),
                "dry-run",
                "cursor",
                "work",
                "--persona",
                "editor",
                "review",
            ],
            stdout=stdout,
            stderr=io.StringIO(),
        )
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertFalse((dry_workspace / ".delegate").exists())

    def test_resume_replays_recorded_bytes_or_resolves_override_or_drops(self) -> None:
        alias, _run_path = self._seed_resume_run()
        old_text = self.persona_text
        new_text = "Edited persona bytes\n"
        global_path = Path.home() / ".delegate" / "personas" / "editor.md"
        global_path.parent.mkdir(parents=True, exist_ok=True)
        global_path.write_text(new_text, encoding="utf-8")

        default_plan = resume_command.build_resume_plan(
            parse_cli(["resume", alias]),
            ResolvedWorkspace(str(self.workspace), "directory"),
            config.embedded_default_config(),
            stderr=io.StringIO(),
        )
        default_request = request_from_parsed(
            default_plan.parsed,
            config.embedded_default_config(),
            io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(default_request.persona_text, old_text)
        self.assertEqual(
            default_request.persona_digest, hashlib.sha256(old_text.encode()).hexdigest()
        )

        override_plan = resume_command.build_resume_plan(
            parse_cli(["resume", "--persona", "editor", alias]),
            ResolvedWorkspace(str(self.workspace), "directory"),
            config.embedded_default_config(),
            stderr=io.StringIO(),
        )
        override_request = request_from_parsed(
            override_plan.parsed,
            config.embedded_default_config(),
            io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(override_request.persona_text, new_text)

        drop_plan = resume_command.build_resume_plan(
            parse_cli(["resume", "--no-persona", alias]),
            ResolvedWorkspace(str(self.workspace), "directory"),
            config.embedded_default_config(),
            stderr=io.StringIO(),
        )
        drop_request = request_from_parsed(
            drop_plan.parsed,
            config.embedded_default_config(),
            io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertIsNone(drop_request.persona_text)
        self.assertIsNone(drop_request.persona_name)
        self.assertNotIn(old_text, drop_request.prompt)
        self.assertNotIn(new_text, drop_request.prompt)

    def test_describe_persona_surface_is_allowlisted_and_exact(self) -> None:
        payload = describe_payload.describe_payload(
            config.embedded_default_config(), "embedded-default", self.workspace
        )
        self.assertEqual(
            payload["personaTransports"]["safe"],
            {
                engine: "prepend"
                for engine in (
                    "cursor",
                    "droid",
                    "codex",
                    "kimi",
                    "claude",
                    "grok",
                    "devin",
                    "opencode",
                    "pi",
                    "omp",
                )
            },
        )
        self.assertEqual(payload["personaTransports"]["work"]["cursor"], "prepend")
        self.assertIn("native-file", payload["personaTransports"]["work"]["claude"])
        self.assertIn("agent-config", payload["personaTransports"]["work"]["opencode"])
        self.assertEqual(
            len(payload["personaTransports"]["notes"]),
            3,
        )
        launch_options = payload["launchOptions"]
        persona_options = ("--persona", "--no-persona", "--allow-repo-persona")
        self.assertEqual(tuple(launch_options[9:12]), persona_options)
        command_names = {command["name"] for command in payload["commands"]}
        self.assertIn("personas", command_names)
        for command in payload["commands"]:
            if command["name"] in {
                "cursor",
                "droid",
                "codex",
                "claude",
                "grok",
                "devin",
                "opencode",
                "pi",
                "omp",
                "kimi",
            }:
                self.assertEqual(
                    tuple(command["launchOptions"][-3:]),
                    ("--persona", "--no-persona", "--allow-repo-persona"),
                )
        self.assertEqual(
            payload["promptTransforms"][1:3],
            [
                "Resolves one named persona from the source workspace, with workspace-local files shadowing global files; persona text is prepended in safe mode and uses the documented work-mode transport when available.",
                "Final framed order is work: skill, safe, persona, worktree note, user, completion suffix, dirty note; safe: skill, persona, safe, worktree note, user, completion suffix, dirty note.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
