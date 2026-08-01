import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, request_build, runner
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
                        cli.DEFAULT_CONFIG,
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
                    cli.DEFAULT_CONFIG,
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
                    cli.DEFAULT_CONFIG,
                    dry_run=True,
                    isolation_context=context,
                    persona="editor",
                    allow_repo_persona=True,
                )

    def test_source_is_logged_and_persisted_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            source = Path(tmp) / "source"
            persona_path = self._persona(source, "editor", "log me")
            global_path = Path(tmp) / ".delegate" / "personas" / "reviewer.md"
            global_path.parent.mkdir(parents=True, exist_ok=True)
            global_path.write_text("global log me", encoding="utf-8")
            stderr = io.StringIO()
            captured = {}
            sentinel = Request("cursor", "work", str(source), "prompt", [], None)

            def capture(*args, **kwargs):
                captured.update(kwargs)
                return sentinel

            with (
                mock.patch.object(
                    request_build, "_build_request_for_workspace", side_effect=capture
                ),
                mock.patch.object(
                    request_build, "_runtime_discovery_for_engine", return_value=(None, ())
                ),
            ):
                request_build.build_request(
                    "cursor",
                    "work",
                    None,
                    ResolvedWorkspace(str(source), "directory"),
                    "prompt",
                    cli.DEFAULT_CONFIG,
                    dry_run=True,
                    persona="editor",
                    stderr=stderr,
                )

            resolved = captured["persona_resolution"]
            self.assertEqual(stderr.getvalue(), "persona: editor (workspace)\n")
            context = runner.RunContext(
                registry_root=source,
                run_id="run-1",
                alias="run-1",
                harness="cursor",
                engine="cursor",
                mode="work",
                model=None,
                source_cwd=str(source),
                execution_cwd=str(source),
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-31T00:00:00Z",
                persona_name=resolved.name,
                persona_source=resolved.source,
                persona_transport="prepend",
                persona_digest=resolved.digest,
                persona_file="persona.txt",
                persona_text=resolved.text,
            )
            manifest = runner.build_manifest(context, [])
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
                    "personaSource": "workspace",
                    "personaTransport": "prepend",
                    "personaDigest": resolved.digest,
                    "personaFile": "persona.txt",
                },
            )
            self.assertEqual(persona_path.read_text(encoding="utf-8"), "log me")

            captured.clear()
            stderr.seek(0)
            stderr.truncate(0)
            with (
                mock.patch.object(
                    request_build, "_build_request_for_workspace", side_effect=capture
                ),
                mock.patch.object(
                    request_build, "_runtime_discovery_for_engine", return_value=(None, ())
                ),
            ):
                request_build.build_request(
                    "cursor",
                    "work",
                    None,
                    ResolvedWorkspace(str(source), "directory"),
                    "prompt",
                    cli.DEFAULT_CONFIG,
                    dry_run=True,
                    persona="reviewer",
                    stderr=stderr,
                )
            global_resolved = captured["persona_resolution"]
            self.assertEqual(stderr.getvalue(), "persona: reviewer (global)\n")
            global_context = runner.RunContext(
                registry_root=source,
                run_id="run-2",
                alias="run-2",
                harness="cursor",
                engine="cursor",
                mode="work",
                model=None,
                source_cwd=str(source),
                execution_cwd=str(source),
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-31T00:00:00Z",
                persona_name=global_resolved.name,
                persona_source=global_resolved.source,
                persona_transport="prepend",
                persona_digest=global_resolved.digest,
                persona_file="persona.txt",
                persona_text=global_resolved.text,
            )
            self.assertEqual(runner.build_manifest(global_context, [])["personaSource"], "global")


if __name__ == "__main__":
    unittest.main()
