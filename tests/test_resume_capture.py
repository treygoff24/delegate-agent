import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, run_registry, runner, safe_workspace, worktree_execution
from delegate_agent.isolation import IsolationContext
from delegate_agent.request_models import Request, ResolvedWorkspace


class ResumeCaptureTests(unittest.TestCase):
    def _git_repo(self, root: Path) -> None:
        subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
            capture_output=True,
        )

    def test_runner_capture_writes_verbatim_prompt_and_manifest_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, alias = run_registry.register_run(registry_root, harness="cursor")
            source_prompt = "literal prompt\nwith\tcontrol-free bytes\n"
            context = runner.RunContext(
                registry_root=registry_root,
                run_id=run_id,
                alias=alias,
                harness="cursor",
                engine="cursor",
                mode="work",
                model="composer-2.5",
                source_cwd=str(workspace),
                execution_cwd=str(workspace),
                workspace_kind="directory",
                isolated_workspace=False,
                started_at="2026-07-31T12:00:00Z",
                source_prompt=source_prompt,
            )

            events: list[str] = []
            original_prompt_write = run_registry.write_private_text
            original_manifest_write = runner.write_manifest

            def write_prompt(path, text, **kwargs):
                if path.name == "prompt.txt":
                    events.append("prompt")
                return original_prompt_write(path, text, **kwargs)

            def write_manifest(path, manifest):
                events.append("manifest")
                return original_manifest_write(path, manifest)

            with (
                mock.patch.object(run_registry, "write_private_text", side_effect=write_prompt),
                mock.patch.object(runner, "write_manifest", side_effect=write_manifest),
            ):
                code, _payload = runner.execute_tracked(
                    [sys.executable, "-c", "pass"],
                    str(workspace),
                    context,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            run_path = registry_root / "runs" / run_id
            self.assertEqual((run_path / "prompt.txt").read_text(), source_prompt)
            self.assertEqual((run_path / "prompt.txt").stat().st_mode & 0o777, 0o600)
            manifest = json.loads((run_path / "manifest.json").read_text())
            self.assertEqual(manifest["promptFile"], "prompt.txt")
            self.assertNotIn("Delegate sub-agent skill review requirement", source_prompt)
            self.assertLess(events.index("prompt"), events.index("manifest"))

    def test_persistent_worktree_registration_captures_prompt_at_second_seam(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._git_repo(workspace)
            registry_root = run_registry.ensure_registry(workspace, workspace_kind="git")
            source_prompt = "persistent seam prompt\nno framing\n"
            isolation = IsolationContext(
                source_workspace=str(workspace),
                effective_isolation="worktree",
                isolation_mode="worktree",
                isolation_lifecycle="persistent",
                preserved_workspace=True,
                source_git_root=str(workspace),
                source_git_common_dir=str(workspace / ".git"),
            )
            request = Request(
                engine="cursor",
                mode="work",
                workspace=str(workspace),
                prompt="framed prompt that must not be persisted",
                argv=["agent", "framed prompt that must not be persisted"],
                model="composer-2.5",
                workspace_kind="git",
                isolation_context=isolation,
                source_prompt=source_prompt,
            )
            execution = worktree_execution.PersistentWorktreeExecution(
                request=request,
                json_mode=True,
                config=cli.DEFAULT_CONFIG,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=ResolvedWorkspace(str(workspace), "git"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                binary_validator=lambda _argv, _engine: None,
            )
            preflight = worktree_execution.PersistentWorktreePreflight(
                iso_ctx=isolation,
                source_git_root=str(workspace),
                base_oid="HEAD",
                source_git_common_dir=str(workspace / ".git"),
                source_head_oid="HEAD",
                source_head_ref="refs/heads/master",
                source_branch="master",
                registry_root=registry_root,
                tracked_dirty_files=0,
                untracked_files=0,
                dirty_example_paths=(),
                dirty_snapshot=safe_workspace.DirtySyncSnapshot((), ()),
            )

            events: list[str] = []
            original_prompt_write = run_registry.write_private_text
            original_manifest_write = runner.write_manifest

            def write_prompt(path, text, **kwargs):
                if path.name == "prompt.txt":
                    events.append("prompt")
                return original_prompt_write(path, text, **kwargs)

            def write_manifest(path, manifest):
                events.append("manifest")
                return original_manifest_write(path, manifest)

            with (
                mock.patch.object(run_registry, "write_private_text", side_effect=write_prompt),
                mock.patch.object(runner, "write_manifest", side_effect=write_manifest),
            ):
                registration = worktree_execution._register_persistent_worktree_run(
                    execution, preflight
                )

            prompt_path = registration.run_path / "prompt.txt"
            self.assertEqual(prompt_path.read_text(), source_prompt)
            self.assertEqual(prompt_path.stat().st_mode & 0o777, 0o600)
            manifest = json.loads((registration.run_path / "manifest.json").read_text())
            self.assertEqual(manifest["promptFile"], "prompt.txt")
            self.assertNotIn("framed prompt", prompt_path.read_text())
            self.assertLess(events.index("prompt"), events.index("manifest"))

    def test_dry_run_does_not_create_registry_or_prompt_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = cli.main(
                ["--json", "--cwd", tmp, "dry-run", "cursor", "work", "dry-run prompt"],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())["dryRun"])
            self.assertFalse((Path(tmp) / ".delegate").exists())
