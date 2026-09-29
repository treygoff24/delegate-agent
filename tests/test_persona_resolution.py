import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, request_build, run_registry, runner
from delegate_agent import config as config_api
from delegate_agent.errors import DelegateError
from delegate_agent.isolation import IsolationContext
from delegate_agent.request_models import Request, ResolvedWorkspace


class PersonaResolutionTests(unittest.TestCase):
    def _persona(self, directory: Path, name: str, text: str) -> Path:
        path = directory / ".delegate" / "personas" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _context(self, source: Path, lifecycle: str) -> IsolationContext:
        effective = "worktree" if lifecycle in {"temporary", "persistent"} else "none"
        return IsolationContext(
            source_workspace=str(source),
            effective_isolation=effective,
            isolation_mode=effective,
            isolation_lifecycle=lifecycle,
            preserved_workspace=lifecycle == "persistent",
            planned_execution_cwd=str(source / "isolated-copy"),
        )

    def test_workspace_persona_shadows_global_persona(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            source = Path(tmp) / "source"
            global_dir = Path(tmp) / ".delegate" / "personas"
            self._persona(source, "editor", "workspace persona")
            global_dir.mkdir(parents=True)
            (global_dir / "editor.md").write_text("global persona", encoding="utf-8")

            resolved = request_build.personas.resolve_persona(source, "editor")
            self.assertEqual(resolved.source, "workspace")
            self.assertEqual(resolved.text, "workspace persona")

    def test_symlinked_persona_root_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "target"
            target.mkdir(parents=True)
            (target / "editor.md").write_text("persona", encoding="utf-8")
            (source / ".delegate").mkdir(parents=True)
            os.symlink(target, source / ".delegate" / "personas")

            with self.assertRaises(DelegateError) as caught:
                request_build.personas.resolve_persona(source, "editor")
            self.assertEqual(caught.exception.error, "invalid_persona")

    def test_symlinked_delegate_parent_is_refused_for_workspace_and_global(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp)
            target = root / "target"
            (target / "personas").mkdir(parents=True)
            (target / "personas" / "editor.md").write_text("escaped", encoding="utf-8")
            source = root / "source"
            source.mkdir()
            os.symlink(target, source / ".delegate")

            with self.assertRaises(DelegateError) as workspace_error:
                request_build.personas.resolve_persona(source, "editor")
            self.assertEqual(workspace_error.exception.error, "invalid_persona")

            (root / ".delegate").symlink_to(target)
            with self.assertRaises(DelegateError) as global_error:
                request_build.personas.resolve_persona(root / "missing-workspace", "editor")
            self.assertEqual(global_error.exception.error, "invalid_persona")

    def test_persona_leaf_validation_uses_the_opened_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            self._persona(source, "editor", "descriptor checked")
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("path read")):
                resolved = request_build.personas.resolve_persona(source, "editor")
            self.assertEqual(resolved.text, "descriptor checked")

    def test_resolution_uses_source_workspace_under_each_isolation_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            isolated = root / "isolated"
            self._persona(source, "editor", "source persona")
            self._persona(isolated, "editor", "isolated persona")

            for lifecycle in ("temporary", "persistent", "none"):
                captured = {}
                sentinel = Request("cursor", "work", str(source), "prompt", [], None)

                def capture(*args, _captured=captured, _sentinel=sentinel, **kwargs):
                    _captured.update(kwargs)
                    return _sentinel

                with (
                    self.subTest(lifecycle=lifecycle),
                    mock.patch.object(
                        request_build, "_build_request_for_workspace", side_effect=capture
                    ),
                    mock.patch.object(
                        request_build, "_runtime_discovery_for_engine", return_value=(None, ())
                    ),
                ):
                    result = request_build.build_request(
                        "cursor",
                        "safe" if lifecycle == "temporary" else "work",
                        None,
                        ResolvedWorkspace(str(source), "directory"),
                        "prompt",
                        config_api.embedded_default_config(),
                        dry_run=True,
                        isolation_context=self._context(source, lifecycle),
                        persona="editor",
                        allow_repo_persona=lifecycle == "temporary",
                    )

                self.assertIs(result, sentinel)
                resolved = captured["persona_resolution"]
                self.assertEqual(
                    resolved.path,
                    (source / ".delegate" / "personas" / "editor.md").resolve(),
                )
                self.assertEqual(resolved.text, "source persona")

    def test_safe_mode_refuses_workspace_persona_unless_opted_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            self._persona(source, "editor", "local")
            context = self._context(source, "temporary")
            with (
                mock.patch.object(
                    request_build, "_runtime_discovery_for_engine", return_value=(None, ())
                ),
                self.assertRaises(DelegateError) as caught,
            ):
                request_build.build_request(
                    "cursor",
                    "safe",
                    None,
                    ResolvedWorkspace(str(source), "directory"),
                    "prompt",
                    config_api.embedded_default_config(),
                    dry_run=True,
                    isolation_context=context,
                    persona="editor",
                )
            self.assertEqual(caught.exception.error, "workspace_persona_refused")

            with (
                mock.patch.object(request_build, "_build_request_for_workspace", return_value=None),
                mock.patch.object(
                    request_build, "_runtime_discovery_for_engine", return_value=(None, ())
                ),
            ):
                request_build.build_request(
                    "cursor",
                    "safe",
                    None,
                    ResolvedWorkspace(str(source), "directory"),
                    "prompt",
                    config_api.embedded_default_config(),
                    dry_run=True,
                    isolation_context=context,
                    persona="editor",
                    allow_repo_persona=True,
                )

    def _tracked_manifest_for(self, source: Path, persona: str, stderr: io.StringIO) -> dict:
        with mock.patch.object(
            request_build, "_runtime_discovery_for_engine", return_value=(None, ())
        ):
            request = request_build.build_request(
                "cursor",
                "work",
                None,
                ResolvedWorkspace(str(source), "directory"),
                "prompt",
                config_api.embedded_default_config(),
                dry_run=True,
                persona=persona,
                stderr=stderr,
            )
        registry_root = run_registry.ensure_registry(source, workspace_kind="directory")
        run_id, alias = run_registry.register_run(
            registry_root,
            harness="cursor",
            metadata={"mode": "work", "cwd": str(source)},
        )
        ctx = cli.make_run_context(
            registry_root,
            request,
            run_id=run_id,
            alias=alias,
            source_workspace=ResolvedWorkspace(str(source), "directory"),
        )
        files = runner._prepare_tracked_run(["agent", "prompt"], ctx, manifest_argv=["agent"])
        manifest_path = files.run_path / run_registry.MANIFEST_FILE
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def test_source_is_logged_and_persisted_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            source = Path(tmp) / "source"
            persona_path = self._persona(source, "editor", "log me")
            global_path = Path(tmp) / ".delegate" / "personas" / "reviewer.md"
            global_path.parent.mkdir(parents=True, exist_ok=True)
            global_path.write_text("global log me", encoding="utf-8")

            stderr = io.StringIO()
            manifest = self._tracked_manifest_for(source, "editor", stderr)
            self.assertEqual(stderr.getvalue(), "persona: editor (workspace)\n")
            self.assertEqual(manifest["personaName"], "editor")
            self.assertEqual(manifest["personaSource"], "workspace")
            self.assertEqual(manifest["personaFile"], "persona.txt")
            self.assertEqual(manifest["personaDigest"], hashlib.sha256(b"log me").hexdigest())
            self.assertEqual(persona_path.read_text(encoding="utf-8"), "log me")

            stderr = io.StringIO()
            manifest = self._tracked_manifest_for(source, "reviewer", stderr)
            self.assertEqual(stderr.getvalue(), "persona: reviewer (global)\n")
            self.assertEqual(manifest["personaName"], "reviewer")
            self.assertEqual(manifest["personaSource"], "global")


if __name__ == "__main__":
    unittest.main()
