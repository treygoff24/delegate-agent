"""Tracked scratch placement and argv contracts; the native probe owns actual ACL proof."""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delegate_agent import run_registry, run_scratch, runner


class CodexScratchTests(unittest.TestCase):
    def test_post_open_foreign_owner_error_survives_and_closes_directory_fd(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "owned"
            target.mkdir()
            opened: list[int] = []
            real_open = run_registry.private_io.os.open

            def capture_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            foreign = SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=stat.S_IFDIR | 0o700)
            with (
                mock.patch.object(run_registry.private_io.os, "open", side_effect=capture_open),
                mock.patch.object(run_registry.private_io.os, "fstat", return_value=foreign),
                self.assertRaises(PermissionError) as caught,
            ):
                run_registry.private_io.ensure_owned_dir(target)

            self.assertEqual(caught.exception.errno, 1)
            with self.assertRaises(OSError):
                os.fstat(opened[-1])

    def test_post_open_fchmod_error_survives_and_closes_directory_fd(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "new-owned"
            opened: list[int] = []
            real_open = run_registry.private_io.os.open
            failure = OSError(5, "injected fchmod failure")

            def capture_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            with (
                mock.patch.object(run_registry.private_io.os, "open", side_effect=capture_open),
                mock.patch.object(run_registry.private_io.os, "fchmod", side_effect=failure),
                self.assertRaises(OSError) as caught,
            ):
                run_registry.private_io.ensure_owned_dir(target)

            self.assertIs(caught.exception, failure)
            with self.assertRaises(OSError):
                os.fstat(opened[-1])

    def _init_repo(self, path: Path) -> None:
        path.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
        (path / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "seed.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=Delegate Test",
                "-c",
                "user.email=delegate-test@example.invalid",
                "commit",
                "-m",
                "seed",
            ],
            check=True,
            capture_output=True,
        )

    def _tracked_git_run(
        self,
        root: Path,
        *,
        source: Path,
        execution: Path,
        mode: str,
        sandbox: str,
    ) -> tuple[dict, dict, Path]:
        registry = run_registry.ensure_registry(source, workspace_kind="git")
        run_id, alias = run_registry.register_run(registry, harness="codex")
        # This fake observes tracked-run routing and argv without claiming to
        # enforce Codex's native sandbox. tests.codex_scratch_probe owns that.
        child = root / f"fake-codex-{mode}"
        child.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "target = pathlib.Path.cwd() / 'healthy-write.txt'\n"
            "try:\n"
            "    target.write_text('ok\\n', encoding='utf-8')\n"
            "    workspace_write = True\n"
            "except OSError:\n"
            "    workspace_write = False\n"
            "payload = {'argv': sys.argv[1:], 'TMPDIR': os.environ.get('TMPDIR'), "
            "'TMP': os.environ.get('TMP'), 'TEMP': os.environ.get('TEMP'), "
            "'workspaceWrite': workspace_write}\n"
            "print(json.dumps({'type': 'item.completed', 'item': "
            "{'type': 'agent_message', 'text': json.dumps(payload)}}))\n"
            "print(json.dumps({'type': 'turn.completed'}))\n",
            encoding="utf-8",
        )
        child.chmod(0o700)
        ctx = runner.RunContext(
            registry_root=registry,
            run_id=run_id,
            alias=alias,
            harness="codex",
            engine="codex",
            mode=mode,
            model="fixture",
            source_cwd=str(source),
            execution_cwd=str(execution),
            workspace_kind="git",
            isolated_workspace=True,
            started_at=run_registry.utc_now_iso(),
            source_git_root=str(source),
            isolation_mode="worktree",
            effective_isolation="worktree",
            isolation_lifecycle="persistent" if mode == "work" else "temporary",
        )
        code, result = runner.execute_tracked(
            [str(child), "exec", "--sandbox", sandbox, "task"],
            str(execution),
            ctx,
            json_mode=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(code, 0)
        manifest = run_registry.load_run_manifest(registry, run_id)
        observed = json.loads(result["assistantText"])
        return manifest, observed, registry

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode bits only")
    def test_tracked_worktree_and_safe_runs_use_private_non_git_scratch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir(mode=0o700)

            work_source = root / "work-source"
            self._init_repo(work_source)
            work_execution = root / "work-execution"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(work_source),
                    "worktree",
                    "add",
                    "-b",
                    "fixture-work",
                    str(work_execution),
                ],
                check=True,
                capture_output=True,
            )

            safe_main = root / "safe-main"
            self._init_repo(safe_main)
            safe_source = root / "safe-source"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(safe_main),
                    "worktree",
                    "add",
                    "-b",
                    "fixture-safe",
                    str(safe_source),
                ],
                check=True,
                capture_output=True,
            )
            safe_execution = root / "safe-copy"
            safe_execution.mkdir()
            (safe_execution / "seed.txt").write_text("copy\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                work_manifest, work_observed, work_registry = self._tracked_git_run(
                    root,
                    source=work_source,
                    execution=work_execution,
                    mode="work",
                    sandbox="workspace-write",
                )
                safe_manifest, safe_observed, safe_registry = self._tracked_git_run(
                    root,
                    source=safe_source,
                    execution=safe_execution,
                    mode="safe",
                    sandbox="read-only",
                )

            for manifest, observed, registry in (
                (work_manifest, work_observed, work_registry),
                (safe_manifest, safe_observed, safe_registry),
            ):
                scratch = Path(observed["TMPDIR"])
                self.assertEqual(observed["TMP"], str(scratch))
                self.assertEqual(observed["TEMP"], str(scratch))
                self.assertEqual(manifest["scratchPath"], str(scratch))
                self.assertNotEqual(
                    scratch, run_registry.run_directory(registry, manifest["runId"]) / "scratch"
                )
                probe_env = {
                    key: value for key, value in os.environ.items() if not key.startswith("GIT_")
                }
                probe = subprocess.run(
                    ["git", "-C", str(scratch), "rev-parse", "--show-toplevel"],
                    env=probe_env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(probe.returncode, 0, probe.stdout)
                info = scratch.stat()
                self.assertEqual(info.st_uid, os.geteuid())
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)

            self.assertTrue(work_observed["workspaceWrite"])
            self.assertIn("--sandbox", work_observed["argv"])
            self.assertEqual(
                work_observed["argv"][work_observed["argv"].index("--sandbox") + 1],
                "workspace-write",
            )
            self.assertFalse((safe_source / "healthy-write.txt").exists())
            self.assertEqual(safe_manifest["scratchPermissions"]["base"], ":read-only")
            self.assertEqual(
                safe_manifest["scratchPermissions"]["writableRoots"],
                [safe_observed["TMPDIR"]],
            )

    def test_failed_and_cancelled_runs_retain_recoverable_scratch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir(mode=0o700)
            source = root / "source"
            self._init_repo(source)
            registry = run_registry.ensure_registry(source, workspace_kind="git")

            for terminal_event, expected_status, child_exit in (
                ("turn.failed", "failed", 1),
                ("turn.cancelled", "cancelled", 0),
            ):
                with self.subTest(status=expected_status):
                    run_id, alias = run_registry.register_run(registry, harness="codex")
                    child = root / f"fake-codex-{expected_status}"
                    child.write_text(
                        f"#!{sys.executable}\n"
                        "import json, os, pathlib, sys\n"
                        "scratch = pathlib.Path(os.environ['TMPDIR'])\n"
                        "(scratch / 'recoverable.txt').write_text('artifact\\n')\n"
                        f"print(json.dumps({{'type': {terminal_event!r}}}))\n"
                        f"sys.exit({child_exit})\n",
                        encoding="utf-8",
                    )
                    child.chmod(0o700)
                    ctx = runner.RunContext(
                        registry,
                        run_id,
                        alias,
                        "codex",
                        "codex",
                        "safe",
                        None,
                        str(source),
                        str(source),
                        "git",
                        True,
                        run_registry.utc_now_iso(),
                    )
                    with mock.patch.dict(os.environ, {"HOME": str(home)}):
                        code, payload = runner.execute_tracked(
                            [str(child), "exec", "--sandbox", "read-only", "task"],
                            str(source),
                            ctx,
                            json_mode=True,
                            stdout=io.StringIO(),
                            stderr=io.StringIO(),
                        )
                    manifest = run_registry.load_run_manifest(registry, run_id)
                    scratch = Path(manifest["scratchPath"])
                    self.assertEqual(payload["status"], expected_status)
                    self.assertEqual(code, 1)
                    self.assertEqual(
                        (scratch / "recoverable.txt").read_text(encoding="utf-8"),
                        "artifact\n",
                    )

    def test_allocator_refuses_symlinked_delegate_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            self._init_repo(home)
            registry = root / "registry"
            registry.mkdir()
            target = root / "target"
            target.mkdir()
            (home / ".delegate").symlink_to(target, target_is_directory=True)
            with (
                mock.patch.dict(os.environ, {"HOME": str(home)}),
                self.assertRaises(run_scratch.ScratchSafetyError),
            ):
                run_scratch.allocate(registry, "del_20260906T000000Z_abcdef")

    def test_allocator_refuses_foreign_owned_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir(mode=0o700)
            registry = root / "registry"
            registry.mkdir()
            with (
                mock.patch.dict(os.environ, {"HOME": str(home)}),
                mock.patch.object(run_scratch.os, "geteuid", return_value=os.geteuid() + 1),
                self.assertRaisesRegex(run_scratch.ScratchSafetyError, "foreign owner"),
            ):
                run_scratch.allocate(registry, "del_20260906T000000Z_abcdef")

    def test_allocator_preserves_shared_delegate_home_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir(mode=0o700)
            delegate_home = home / ".delegate"
            delegate_home.mkdir(mode=0o755)
            registry = root / "registry"
            registry.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                scratch = run_scratch.allocate(registry, "del_20260906T000000Z_abcdef")

            self.assertEqual(stat.S_IMODE(delegate_home.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(scratch.parent.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(scratch.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)

    def test_git_home_uses_fixed_private_neutral_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            self._init_repo(home)
            registry = root / "registry"
            registry.mkdir()
            persistent_temp = root / "persistent-temp"
            persistent_temp.mkdir()
            with (
                mock.patch.dict(os.environ, {"HOME": str(home)}),
                mock.patch.object(run_scratch, "PERSISTENT_TEMP_ROOT", persistent_temp),
            ):
                planned = run_scratch.expected_path(registry, "del_20260906T000000Z_abcdef")
                self.assertFalse((home / ".delegate").exists())
                scratch = run_scratch.allocate(registry, "del_20260906T000000Z_abcdef")

            self.assertEqual(scratch, planned)
            self.assertTrue(scratch.is_relative_to(persistent_temp))
            self.assertEqual(stat.S_IMODE(scratch.parent.parent.stat().st_mode), 0o700)

    def test_invalid_run_id_is_rejected_before_scratch_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            registry = Path(temp) / "registry"
            registry.mkdir()
            with (
                mock.patch.dict(os.environ, {"HOME": str(home)}),
                self.assertRaisesRegex(run_scratch.ScratchSafetyError, "invalid run id"),
            ):
                run_scratch.allocate(registry, "../../escape")
            self.assertFalse((home / ".delegate").exists())

    def test_allocation_failure_is_recorded_as_terminal_without_launching_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir(mode=0o700)
            source = root / "source"
            self._init_repo(source)
            registry = run_registry.ensure_registry(source, workspace_kind="git")
            run_id, alias = run_registry.register_run(registry, harness="codex")
            ctx = runner.RunContext(
                registry,
                run_id,
                alias,
                "codex",
                "codex",
                "safe",
                None,
                str(source),
                str(source),
                "git",
                True,
                run_registry.utc_now_iso(),
            )
            child = root / "must-not-launch"
            child.write_text(f"#!{sys.executable}\nraise SystemExit(99)\n", encoding="utf-8")
            child.chmod(0o700)

            real_allocate = run_scratch.allocate_plan

            def allocate_then_refuse(scratch_plan):
                scratch = real_allocate(scratch_plan)
                (scratch / "partial.txt").write_text("recoverable\n", encoding="utf-8")
                raise run_scratch.ScratchSafetyError("injected refusal")

            with (
                mock.patch.dict(os.environ, {"HOME": str(home)}),
                mock.patch.object(
                    run_scratch,
                    "allocate_plan",
                    side_effect=allocate_then_refuse,
                ),
                self.assertRaises(runner.RunnerLaunchError),
            ):
                runner.execute_tracked(
                    [str(child), "exec", "--sandbox", "read-only", "task"],
                    str(source),
                    ctx,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            state = run_registry.load_run_state(registry, run_id)
            manifest = run_registry.load_run_manifest(registry, run_id)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error"], "unsafe_scratch_directory")
            self.assertEqual(manifest["scratchAllocation"]["status"], "failed")
            self.assertIn("injected refusal", manifest["scratchAllocation"]["message"])
            self.assertEqual(
                (Path(manifest["scratchPath"]) / "partial.txt").read_text(encoding="utf-8"),
                "recoverable\n",
            )
            state["finishedAt"] = "2000-01-01T00:00:00Z"
            state["lastActivityAt"] = "2000-01-01T00:00:00Z"
            run_registry.write_json_atomic(
                run_registry.run_directory(registry, run_id) / "state.json", state
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                pruned = run_registry.prune_runs(
                    registry,
                    older_than_days=0,
                    now=datetime(2026, 9, 6, tzinfo=UTC),
                )
            self.assertEqual([item["runId"] for item in pruned["removed"]], [run_id])
            self.assertFalse(Path(manifest["scratchPath"]).exists())

    def test_profile_is_read_only_plus_one_root_and_keeps_launch_context(self):
        original = [
            "codex",
            "--ask-for-approval",
            "never",
            "--model",
            "model-a",
            "exec",
            "--cd",
            "/review",
            "--sandbox",
            "read-only",
            "--json",
            "-",
        ]
        with tempfile.TemporaryDirectory() as temp:
            updated = runner._codex_argv_with_scratch(original, Path(temp))
            self.assertNotIn("--sandbox", updated)
            self.assertIn("--strict-config", updated)
            self.assertEqual(updated[updated.index("--cd") + 1], "/review")
            self.assertEqual(updated[updated.index("--model") + 1], "model-a")
            self.assertEqual(updated[-1], "-")
            settings = [
                updated[index + 1] for index, value in enumerate(updated[:-1]) if value == "-c"
            ]
            config = tomllib.loads("\n".join(settings))
            name = config["default_permissions"]
            self.assertEqual(
                config["permissions"][name],
                {"extends": ":read-only", "filesystem": {str(Path(temp).resolve()): "write"}},
            )
            second = runner._codex_argv_with_scratch(original, Path(temp))
            self.assertNotEqual(updated, second)

    def test_other_sandbox_modes_and_absent_scratch_are_unchanged(self):
        for argv in (
            ["codex", "exec", "--sandbox", "workspace-write", "task"],
            ["codex", "exec", "task"],
        ):
            self.assertEqual(runner._codex_argv_with_scratch(argv, Path("/unused")), argv)
        argv = ["codex", "exec", "--sandbox", "read-only", "task"]
        self.assertEqual(runner._codex_argv_with_scratch(argv, None), argv)

    def test_symlink_scratch_is_refused_before_changing_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source").mkdir()
            (root / "scratch").symlink_to(root / "source", target_is_directory=True)
            with self.assertRaises(runner.RunnerLaunchError):
                runner._codex_argv_with_scratch(
                    ["codex", "exec", "--sandbox", "read-only", "task"], root / "scratch"
                )

    def test_old_cli_refusal_is_actionable_without_permissive_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = run_registry.ensure_registry(Path(temp), workspace_kind="directory")
            run_id, alias = run_registry.register_run(root, harness="codex")
            ctx = runner.RunContext(
                root,
                run_id,
                alias,
                "codex",
                "codex",
                "safe",
                None,
                temp,
                temp,
                "directory",
                False,
                run_registry.utc_now_iso(),
            )
            # A local old-CLI fixture rejects the enforcement flag before a turn.
            executable = Path(temp) / "old-codex"
            executable.write_text(
                f"#!{sys.executable}\nimport sys\nprint(\"error: unexpected argument '--strict-config' found\",file=sys.stderr)\nsys.exit(2)\n"
            )
            executable.chmod(0o700)
            code, payload = runner.execute_tracked(
                [str(executable), "exec", "--sandbox", "read-only", "task"],
                temp,
                ctx,
                json_mode=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "codex_scratch_permissions_unavailable")
            self.assertIn("no permissive fallback", payload["message"])
            self.assertEqual(payload["scratchPermissions"]["base"], ":read-only")
