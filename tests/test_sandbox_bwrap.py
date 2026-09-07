import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import config as config_api
from delegate_agent import isolation as isolation_api
from delegate_agent import request_build as request_api
from delegate_agent import request_models as request_types
from tests.delegate_commands_test_base import CommandTestBase, make_git_repo

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Imported after the base (which bootstraps sys.path).
from delegate_agent import config as delegate_config  # noqa: E402
from delegate_agent import (  # noqa: E402
    mail_push,
    request_build,
    run_registry,
    run_scratch,
    runner,
    safe_workspace,
    sandbox_bwrap,
)
from delegate_agent.errors import DelegateError  # noqa: E402


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Delegate Test",
            "-c",
            "user.email=delegate-test@example.com",
            *args,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _make_committed_repo() -> tempfile.TemporaryDirectory:
    repo = make_git_repo()
    _git(repo.name, "commit", "--allow-empty", "-m", "init")
    return repo


class BuildBwrapArgvTests(unittest.TestCase):
    def test_emission_order_and_engine_argv_untouched(self):
        argv = sandbox_bwrap.build_bwrap_argv(
            workspace="/ws",
            engine_argv=["codex", "exec"],
            env={"CODEX_HOME": "/eng/codex-home"},
            home="/home/fake",
            rw_roots=["/scratch"],
            ro_roots=["/opt"],
            engine="codex",
            masks=(
                sandbox_bwrap.Mask(path="node_modules", kind=sandbox_bwrap.MASK_KIND_TMPFS),
                sandbox_bwrap.Mask(path="secret.env", kind=sandbox_bwrap.MASK_KIND_DEVNULL),
            ),
        )
        self.assertEqual(
            argv,
            [
                "bwrap",
                "--unshare-user",
                "--unshare-ipc",
                "--unshare-uts",
                "--unshare-cgroup-try",
                "--die-with-parent",
                "--new-session",
                "--ro-bind",
                "/usr",
                "/usr",
                "--ro-bind",
                "/etc",
                "/etc",
                "--symlink",
                "usr/lib",
                "lib",
                "--symlink",
                "usr/lib64",
                "lib64",
                "--symlink",
                "usr/bin",
                "bin",
                "--symlink",
                "usr/sbin",
                "sbin",
                "--dev",
                "/dev",
                "--bind",
                "/proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/home/fake",
                "--ro-bind",
                "/opt",
                "/opt",
                "--ro-bind",
                "/ws",
                "/ws",
                "--tmpfs",
                "/ws/node_modules",
                "--ro-bind",
                "/dev/null",
                "/ws/secret.env",
                "--bind",
                "/scratch",
                "/scratch",
                "--bind",
                "/eng/codex-home",
                "/eng/codex-home",
                "--chdir",
                "/ws",
                "--",
                "codex",
                "exec",
            ],
        )

    def test_never_unshares_pid_or_net_and_never_uses_proc_flag(self):
        argv = sandbox_bwrap.build_bwrap_argv(
            workspace="/ws",
            engine_argv=["engine"],
            env={},
            home="/home/fake",
            rw_roots=[],
            ro_roots=[],
        )
        self.assertNotIn("--unshare-net", argv)
        self.assertNotIn("--proc", argv)

    def test_mount_targets_dedupe_keep_first(self):
        argv = sandbox_bwrap.build_bwrap_argv(
            workspace="/ws",
            engine_argv=["engine"],
            env={"CODEX_HOME": "/shared"},
            home="/home/fake",
            rw_roots=["/shared"],
            ro_roots=["/dup", "/dup"],
            masks=(),
        )
        # The read-only bind wins (declared first); neither the duplicate
        # ro-root nor the identical engine home may re-declare /dup or /shared.
        self.assertEqual(argv.count("/dup"), 2)  # one --ro-bind pair
        self.assertEqual(argv.count("/shared"), 2)  # one --bind pair; home deduped
        self.assertEqual(argv[argv.index("/dup") - 1], "--ro-bind")
        self.assertEqual(argv[argv.index("/shared") - 1], "--bind")

    def test_only_the_selected_engine_home_is_rw_bound(self):
        env = {"CODEX_HOME": "/eng/codex", "CLAUDE_CONFIG_DIR": "/eng/claude"}
        for engine, expected, hidden in (
            ("codex", "/eng/codex", "/eng/claude"),
            ("claude", "/eng/claude", "/eng/codex"),
            ("omp", None, "/eng/codex"),
        ):
            argv = sandbox_bwrap.build_bwrap_argv(
                workspace="/ws",
                engine_argv=["engine"],
                env=env,
                home="/home/fake",
                rw_roots=["/scratch"],
                ro_roots=[],
                engine=engine,
            )
            self.assertNotIn(hidden, argv, engine)
            chdir = argv.index("--chdir")
            if expected is None:
                self.assertEqual(argv[chdir - 3 : chdir], ["--bind", "/scratch", "/scratch"])
            else:
                self.assertEqual(argv[chdir - 3 : chdir], ["--bind", expected, expected])

    def test_exact_bwrap_path_is_argv0(self):
        argv = sandbox_bwrap.build_bwrap_argv(
            workspace="/ws",
            engine_argv=["engine"],
            env={},
            home="/home/fake",
            rw_roots=[],
            ro_roots=[],
            bwrap_path="/usr/bin/bwrap",
        )
        self.assertEqual(argv[0], "/usr/bin/bwrap")

    def test_display_argv_is_truncated_prefix(self):
        display = sandbox_bwrap.bwrap_display_argv(["codex", "exec"])
        self.assertEqual(display, ["bwrap", "…", "--", "codex", "exec"])


class WrapEngineArgvTests(unittest.TestCase):
    def test_optional_ro_roots_bound_only_when_present(self):
        with (
            tempfile.TemporaryDirectory() as home_tmp,
            tempfile.TemporaryDirectory() as scratch_tmp,
        ):
            fake_home = Path(home_tmp)
            (fake_home / ".local").mkdir()
            (fake_home / ".codex").mkdir()
            scratch = Path(scratch_tmp)
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=["engine"],
                cwd="/ws",
                env={"HOME": str(fake_home)},
                engine="codex",
                scratch_dir=str(scratch),
            )
            self.assertIn("--tmpfs", argv)
            self.assertIn(str(fake_home), argv)
            self.assertIn(str(fake_home / ".local"), argv)
            self.assertIn(str(fake_home / ".codex"), argv)
            self.assertNotIn(str(fake_home / ".cargo"), argv)
            self.assertIn(str(scratch), argv)
            for system_root in ("/opt", "/run/systemd/resolve"):
                if os.path.isdir(system_root):
                    self.assertIn(system_root, argv)
                else:
                    self.assertNotIn(system_root, argv)

    def test_other_engine_dot_dirs_not_bound_for_codex(self):
        with tempfile.TemporaryDirectory() as home_tmp:
            fake_home = Path(home_tmp)
            (fake_home / ".claude").mkdir()
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=["engine"],
                cwd="/ws",
                env={"HOME": str(fake_home)},
                engine="codex",
                scratch_dir=None,
            )
            self.assertNotIn(str(fake_home / ".claude"), argv)

    def test_missing_optional_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as home_tmp:
            fake_home = Path(home_tmp)
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=["engine"],
                cwd="/ws",
                env={"HOME": str(fake_home)},
                engine="codex",
                scratch_dir=None,
            )
            self.assertNotIn(str(fake_home / ".local"), argv)
            self.assertNotIn(str(fake_home / ".codex"), argv)


class ProbeTests(unittest.TestCase):
    def test_probe_is_the_production_boundary_with_the_exact_binary(self):
        argv = sandbox_bwrap.probe_argv("/opt/bin/bwrap", home="/home/fake")
        production = sandbox_bwrap.build_bwrap_argv(
            workspace="/usr",
            engine_argv=["/bin/true"],
            env={},
            home="/home/fake",
            rw_roots=[],
            ro_roots=[],
            bwrap_path="/opt/bin/bwrap",
        )
        self.assertEqual(argv, production)
        self.assertEqual(argv[0], "/opt/bin/bwrap")
        self.assertEqual(argv[-1], "/bin/true")

    def test_availability_is_never_cached(self):
        calls = []

        def probe(path):
            calls.append(path)
            return len(calls) == 1

        with (
            mock.patch.object(sandbox_bwrap.sys, "platform", "linux"),
            mock.patch.object(sandbox_bwrap, "_run_probe", probe),
        ):
            self.assertTrue(sandbox_bwrap.bwrap_available("/fake/bwrap"))
            self.assertFalse(sandbox_bwrap.bwrap_available("/fake/bwrap"))
        self.assertEqual(calls, ["/fake/bwrap", "/fake/bwrap"])

    def test_ensure_returns_the_resolved_binary(self):
        with (
            mock.patch.object(sandbox_bwrap.sys, "platform", "linux"),
            mock.patch.object(sandbox_bwrap.shutil, "which", return_value="/usr/bin/bwrap"),
            mock.patch.object(sandbox_bwrap, "bwrap_available", return_value=True),
        ):
            self.assertEqual(sandbox_bwrap.ensure_bwrap_backend(), "/usr/bin/bwrap")


class RequestedSafeBackendTests(unittest.TestCase):
    def test_default_is_copy(self):
        self.assertEqual(sandbox_bwrap.requested_safe_backend({}), "copy")

    def test_config_selects_bwrap(self):
        config = {"isolation": {"safeBackend": "bwrap"}}
        self.assertEqual(sandbox_bwrap.requested_safe_backend(config), "bwrap")

    def test_env_beats_config(self):
        config = {"isolation": {"safeBackend": "bwrap"}}
        env = {"DELEGATE_SAFE_BACKEND": "copy"}
        self.assertEqual(sandbox_bwrap.requested_safe_backend(config, env), "copy")

    def test_invalid_env_fails_closed(self):
        with self.assertRaises(DelegateError) as caught:
            sandbox_bwrap.requested_safe_backend({}, {"DELEGATE_SAFE_BACKEND": "yolo"})
        self.assertEqual(caught.exception.error, "invalid_safe_backend")

    def test_invalid_config_fails_closed(self):
        config = {"isolation": {"safeBackend": "yolo"}}
        with self.assertRaises(DelegateError) as caught:
            sandbox_bwrap.requested_safe_backend(config)
        self.assertEqual(caught.exception.error, "invalid_safe_backend")


class ParityMasksTests(unittest.TestCase):
    def setUp(self):
        repo = _make_committed_repo()
        self.addCleanup(repo.cleanup)
        self.repo = Path(repo.name)
        (self.repo / ".gitignore").write_text(
            "secret.env\nnode_modules/\n.delegate/\n", encoding="utf-8"
        )
        _git(str(self.repo), "add", ".gitignore")
        _git(str(self.repo), "commit", "-m", "ignore rules")
        (self.repo / "secret.env").write_text("s3cret\n", encoding="utf-8")
        (self.repo / "node_modules" / "pkg").mkdir(parents=True)
        (self.repo / "node_modules" / "pkg" / "index.js").write_text(
            "module.exports = 1;\n", encoding="utf-8"
        )
        (self.repo / ".delegate" / "run").mkdir(parents=True)
        (self.repo / ".delegate" / "run" / "state.json").write_text("{}\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("local-only\n", encoding="utf-8")

    def test_masks_collapse_directories_and_skip_delegate(self):
        masks = sandbox_bwrap.parity_masks(str(self.repo))
        self.assertEqual(
            masks,
            (
                sandbox_bwrap.Mask(path="node_modules", kind=sandbox_bwrap.MASK_KIND_TMPFS),
                sandbox_bwrap.Mask(path="secret.env", kind=sandbox_bwrap.MASK_KIND_DEVNULL),
            ),
        )

    def test_overflow_raises(self):
        with (
            mock.patch.object(sandbox_bwrap, "MASK_OVERFLOW_LIMIT", 1),
            self.assertRaises(sandbox_bwrap.BwrapMaskOverflow),
        ):
            sandbox_bwrap.parity_masks(str(self.repo))


class SafeIsolatedRequestBwrapTests(CommandTestBase):
    def setUp(self):
        super().setUp()
        repo = _make_committed_repo()
        self.addCleanup(repo.cleanup)
        self.repo = Path(repo.name)
        (self.repo / ".gitignore").write_text("secret.env\n", encoding="utf-8")
        _git(str(self.repo), "add", ".gitignore")
        _git(str(self.repo), "commit", "-m", "ignore rules")
        (self.repo / "secret.env").write_text("s3cret\n", encoding="utf-8")

    def _request(self, engine: str = "codex"):
        iso_ctx = isolation_api.build_isolation_context(
            source_workspace=str(self.repo),
            resolved_isolation="auto",
            engine=engine,
            mode="safe",
            source_git_root=str(self.repo),
        )
        return request_api.build_request(
            engine,
            "safe",
            None,
            request_types.ResolvedWorkspace(str(self.repo), "git"),
            "review",
            config_api.embedded_default_config(),
            dry_run=True,
            isolation_context=iso_ctx,
        )

    def test_copy_is_the_default_backend(self):
        request = self._request()
        with safe_workspace.safe_isolated_request(request, config={}) as isolated:
            ctx = isolated.isolation_context
            self.assertEqual(ctx.safe_workspace_method, "git-worktree")
            self.assertNotEqual(isolated.workspace, str(self.repo))

    def test_cursor_stays_on_copy_with_warning(self):
        request = self._request(engine="cursor")
        with safe_workspace.safe_isolated_request(
            request,
            config={},
            env={"DELEGATE_SAFE_BACKEND": "bwrap"},
        ) as isolated:
            ctx = isolated.isolation_context
            self.assertEqual(ctx.safe_workspace_method, "git-worktree")
            self.assertTrue(any("Cursor safe mode always uses" in w for w in ctx.warnings))

    def test_unavailable_bwrap_fails_closed(self):
        request = self._request()
        failure = DelegateError("bwrap_unavailable", "The bubblewrap probe failed on this host.")
        with (
            mock.patch.object(safe_workspace, "ensure_bwrap_backend", side_effect=failure),
            self.assertRaises(DelegateError) as caught,
            safe_workspace.safe_isolated_request(
                request,
                config={},
                env={"DELEGATE_SAFE_BACKEND": "bwrap"},
            ),
        ):
            pass
        self.assertEqual(caught.exception.error, "bwrap_unavailable")

    def test_symlink_leak_fails_closed(self):
        request = self._request()
        os.symlink(str(self.repo / "secret.env"), self.repo / "leak-link")
        with (
            mock.patch.object(safe_workspace, "ensure_bwrap_backend", lambda: None),
            self.assertRaises(DelegateError) as caught,
            safe_workspace.safe_isolated_request(
                request,
                config={},
                env={"DELEGATE_SAFE_BACKEND": "bwrap"},
            ),
        ):
            pass
        self.assertEqual(caught.exception.error, "bwrap_symlink_leak")

    def test_mask_overflow_fails_closed(self):
        request = self._request()
        overflow = sandbox_bwrap.BwrapMaskOverflow("too many masks")
        with (
            mock.patch.object(safe_workspace, "ensure_bwrap_backend", lambda: None),
            mock.patch.object(safe_workspace, "parity_masks", side_effect=overflow),
            self.assertRaises(DelegateError) as caught,
            safe_workspace.safe_isolated_request(
                request,
                config={},
                env={"DELEGATE_SAFE_BACKEND": "bwrap"},
            ),
        ):
            pass
        self.assertEqual(caught.exception.error, "bwrap_mask_overflow")

    def test_success_yields_workspace_in_place_with_sandbox_payload(self):
        request = self._request()
        with (
            mock.patch.object(safe_workspace, "ensure_bwrap_backend", lambda: None),
            safe_workspace.safe_isolated_request(
                request,
                config={},
                env={"DELEGATE_SAFE_BACKEND": "bwrap"},
            ) as isolated,
        ):
            ctx = isolated.isolation_context
            self.assertEqual(isolated.workspace, str(self.repo))
            self.assertEqual(isolated.argv, request.argv)
            self.assertEqual(ctx.safe_workspace_method, sandbox_bwrap.BWRAP_METHOD)
            self.assertIsInstance(ctx.sandbox, sandbox_bwrap.SandboxPlan)
            self.assertEqual(ctx.sandbox.backend, "bwrap")
            self.assertEqual(
                ctx.sandbox.masks,
                (sandbox_bwrap.Mask("secret.env", sandbox_bwrap.MASK_KIND_DEVNULL),),
            )

    def test_structured_retry_accepts_bwrap_source_without_cleanup_descriptor(self):
        root = run_registry.ensure_registry(self.repo, workspace_kind="git")
        run_id, _alias = run_registry.register_run(
            root,
            harness="codex",
            metadata={
                "engine": "codex",
                "group": "wf-test",
                "workflowAgentKey": "agent",
                "executionCwd": str(self.repo),
                "workspaceKind": "git",
                "isolationLifecycle": "temporary",
                "isolationBackend": "bwrap",
            },
        )
        run_path = run_registry.run_directory(root, run_id)
        run_registry.write_json_atomic(
            run_path / run_registry.MANIFEST_FILE,
            {
                "engine": "codex",
                "group": "wf-test",
                "workflowAgentKey": "agent",
                "executionCwd": str(self.repo),
                "isolationLifecycle": "temporary",
                "isolationBackend": "bwrap",
                "workspaceKind": "git",
            },
        )
        run_registry.write_json_atomic(
            run_path / run_registry.SNAPSHOT_FILE,
            {"runId": run_id, "status": "succeeded"},
        )
        resolved = request_build._structured_retry_workspace(
            request_types.ResolvedWorkspace(str(self.repo), "git"),
            engine="codex",
            group="wf-test",
            workflow_agent_key="agent",
            run_id=run_id,
            session_id=None,
        )
        self.assertEqual(resolved.path, str(self.repo.resolve()))


class RunnerTempBaseTests(unittest.TestCase):
    def test_prompt_temp_files_land_under_scratch_when_sandboxed(self):
        from delegate_agent import runner

        with tempfile.TemporaryDirectory() as scratch:
            updated, temp_dir = runner._materialize_prompt_file_argv(
                ["engine", "{PROMPT_FILE}"],
                prompt_file_text="review",
                prompt_file_placeholder="{PROMPT_FILE}",
                temp_base=Path(scratch),
            )
            self.assertTrue(updated[1].endswith("prompt.txt"))
            self.assertTrue(Path(updated[1]).is_relative_to(Path(scratch)))
            self.assertTrue(temp_dir.is_relative_to(Path(scratch)))
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_schema_temp_files_land_under_scratch_when_sandboxed(self):
        from delegate_agent import runner

        with tempfile.TemporaryDirectory() as scratch:
            updated, _temp_dir = runner._materialize_output_schema_argv(
                ["engine", "{SCHEMA_PATH}"],
                output_schema_text="{}",
                output_schema_path="{SCHEMA_PATH}",
                temp_base=Path(scratch),
            )
            self.assertTrue(updated[1].endswith("schema.json"))
            self.assertTrue(Path(updated[1]).is_relative_to(Path(scratch)))

    def test_without_sandbox_temp_files_use_default_tempdir(self):
        from delegate_agent import runner

        _updated, temp_dir = runner._materialize_prompt_file_argv(
            ["engine", "{PROMPT_FILE}"],
            prompt_file_text="review",
            prompt_file_placeholder="{PROMPT_FILE}",
            temp_base=None,
        )
        try:
            self.assertTrue(str(temp_dir).startswith(tempfile.gettempdir()))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


@unittest.skipUnless(sys.platform.startswith("linux"), "bwrap boundary is Linux-only")
class LiveBwrapBoundaryTests(unittest.TestCase):
    def test_boundary_hides_home_secrets_and_gitignored_state(self):
        if not sandbox_bwrap.bwrap_available():
            self.skipTest("working bubblewrap unavailable on this host")

        with (
            tempfile.TemporaryDirectory() as home_tmp,
        ):
            home = Path(home_tmp)
            profile_dir = home / ".ai-profiles" / "personal"
            profile_dir.mkdir(parents=True)
            canary = profile_dir / "keys.zsh"
            canary.write_text("# CANARY never-leak\n", encoding="utf-8")

            scratch_root = home / ".delegate" / "run-scratch" / "bucket"
            scratch = scratch_root / "del_current"
            sibling = scratch_root / "del_sibling"
            scratch.mkdir(parents=True)
            sibling.mkdir()
            sibling_canary = sibling / "keep.txt"
            sibling_canary.write_text("keep\n", encoding="utf-8")
            repo = _make_committed_repo()
            self.addCleanup(repo.cleanup)
            workspace = Path(repo.name)
            (workspace / ".gitignore").write_text("secret.env\n", encoding="utf-8")
            _git(str(workspace), "add", ".gitignore")
            _git(str(workspace), "commit", "-m", "ignore rules")
            (workspace / "secret.env").write_text("s3cret\n", encoding="utf-8")

            masks = sandbox_bwrap.parity_masks(str(workspace))
            self.assertIn(
                sandbox_bwrap.Mask(path="secret.env", kind=sandbox_bwrap.MASK_KIND_DEVNULL),
                masks,
            )
            script = "\n".join(
                [
                    f'if [ -e "{home}/.ai-profiles/personal/keys.zsh" ]; then echo canary=visible; else echo canary=hidden; fi',
                    "if touch write-attempt 2>/dev/null; then echo workspace-write=yes; else echo workspace-write=no; fi",
                    'echo "secret-bytes=$(dd if=secret.env bs=64 count=1 2>/dev/null | wc -c)"',
                    f'if touch "{scratch}/scratch-ok" 2>/dev/null; then echo scratch-write=yes; else echo scratch-write=no; fi',
                    f'if touch "{sibling}/denied" 2>/dev/null; then echo sibling-write=yes; else echo sibling-write=no; fi',
                ]
            )
            argv = sandbox_bwrap.build_bwrap_argv(
                workspace=str(workspace),
                engine_argv=["/bin/sh", "-c", script],
                env={"HOME": str(home)},
                home=str(home),
                rw_roots=[str(scratch)],
                ro_roots=[],
                masks=masks,
            )
            child_env = dict(os.environ, HOME=str(home))
            result = subprocess.run(  # nosec B603 - fixed boundary argv built by the function under test.
                argv,
                capture_output=True,
                text=True,
                timeout=120,
                env=child_env,
                check=False,
                cwd=str(workspace),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = dict(
                line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
            )
            self.assertEqual(observed.get("canary"), "hidden")
            self.assertEqual(observed.get("workspace-write"), "no")
            self.assertEqual(observed.get("secret-bytes"), "0")
            self.assertEqual(observed.get("scratch-write"), "yes")
            self.assertEqual(observed.get("sibling-write"), "no")
            self.assertEqual(sibling_canary.read_text(encoding="utf-8"), "keep\n")


class DryRunBwrapTests(CommandTestBase):
    def setUp(self):
        super().setUp()
        repo = _make_committed_repo()
        self.addCleanup(repo.cleanup)
        self.repo = Path(repo.name)

    def _run_dry_run(self, *flags: str) -> tuple[int, str, str]:
        with mock.patch.dict(os.environ, {"DELEGATE_SAFE_BACKEND": "bwrap"}, clear=False):
            return self.run_main(["--cwd", str(self.repo), *flags])

    def test_json_dry_run_reports_bwrap_method_and_truncated_argv(self):
        code, out, _err = self._run_dry_run("--json", "dry-run", "codex", "safe", "review")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["safeWorkspaceMethod"], sandbox_bwrap.BWRAP_METHOD)
        self.assertEqual(payload["argv"][0], sandbox_bwrap.BWRAP_BINARY)
        self.assertEqual(payload["argv"][1], "…")
        self.assertIn("--", payload["argv"])

    def test_human_dry_run_prints_method_and_truncated_argv_line(self):
        code, out, _err = self._run_dry_run("dry-run", "codex", "safe", "review")
        self.assertEqual(code, 0)
        self.assertIn("safe workspace method: bwrap-ro-bind", out)
        self.assertIn("argv: bwrap '…' -- codex", out)


class HomeRelativeRoBindOrderTests(unittest.TestCase):
    def test_home_ro_binds_follow_the_home_tmpfs(self):
        # bwrap mounts in argv order: a HOME tmpfs emitted after ~/.local would
        # shadow it (live failure: execvp of a ~/.local/bin wrapper returned ENOENT).
        argv = sandbox_bwrap.build_bwrap_argv(
            workspace="/ws",
            engine_argv=["true"],
            env={},
            home="/home/fake",
            rw_roots=[],
            ro_roots=["/home/fake/.local", "/opt"],
        )
        tmpfs_home = argv.index("/home/fake")
        self.assertEqual(argv[tmpfs_home - 1], "--tmpfs")
        self.assertGreater(argv.index("/home/fake/.local"), tmpfs_home)

    def test_wrap_engine_argv_extra_ro_roots_follow_the_home_tmpfs(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ws:
            extra = os.path.join(home, ".ai-profiles", "contract")
            os.makedirs(extra)
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=["true"],
                cwd=ws,
                env={},
                engine="codex",
                home=home,
                extra_ro_roots=[extra],
            )
            self.assertGreater(argv.index(extra), argv.index(home))
            self.assertEqual(argv[argv.index(extra) - 1], "--ro-bind")


class ConfiguredBwrapBindsTests(unittest.TestCase):
    def test_absent_section_yields_nothing(self):
        self.assertEqual(sandbox_bwrap.configured_bwrap_binds({}, workspace="/ws"), ())
        config = {"isolation": {"safeBackend": "bwrap"}}
        self.assertEqual(sandbox_bwrap.configured_bwrap_binds(config, workspace="/ws"), ())

    def test_expands_home_dedupes_and_keeps_modes(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ws:
            sock = os.path.join(home, "broker.sock")
            Path(sock).write_text("", encoding="utf-8")
            os.makedirs(os.path.join(home, ".local", "bin"))
            config = {
                "isolation": {
                    "bwrapBinds": [
                        {"path": "~/.local/bin", "mode": "ro"},
                        {"path": "~/broker.sock", "mode": "rw"},
                        {"path": "~/.local/bin", "mode": "ro"},
                    ]
                }
            }
            with mock.patch.dict(os.environ, {"HOME": home}):
                binds = sandbox_bwrap.configured_bwrap_binds(config, workspace=ws)
            self.assertEqual(
                binds,
                (
                    sandbox_bwrap.Bind(
                        path=os.path.realpath(os.path.join(home, ".local", "bin")), mode="ro"
                    ),
                    sandbox_bwrap.Bind(path=os.path.realpath(sock), mode="rw"),
                ),
            )

    def test_missing_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as ws:
            config = {"isolation": {"bwrapBinds": [{"path": "/nonexistent/xyz", "mode": "ro"}]}}
            with self.assertRaises(DelegateError) as caught:
                sandbox_bwrap.configured_bwrap_binds(config, workspace=ws)
            self.assertEqual(caught.exception.error, "bwrap_bind_missing")

    def test_rw_bind_covering_workspace_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            ws = os.path.join(root, "repo")
            os.makedirs(ws)
            for covering in (root, ws):
                config = {"isolation": {"bwrapBinds": [{"path": covering, "mode": "rw"}]}}
                with self.assertRaises(DelegateError) as caught:
                    sandbox_bwrap.configured_bwrap_binds(config, workspace=ws)
                self.assertEqual(caught.exception.error, "bwrap_bind_conflict")
            ro = {"isolation": {"bwrapBinds": [{"path": root, "mode": "ro"}]}}
            self.assertEqual(
                sandbox_bwrap.configured_bwrap_binds(ro, workspace=ws),
                (sandbox_bwrap.Bind(path=os.path.realpath(root), mode="ro"),),
            )

    def test_rw_bind_inside_workspace_and_relative_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as ws:
            inside = Path(ws) / "vendor"
            inside.mkdir()
            config = {"isolation": {"bwrapBinds": [{"path": str(inside), "mode": "rw"}]}}
            with self.assertRaises(DelegateError) as caught:
                sandbox_bwrap.configured_bwrap_binds(config, workspace=ws)
            self.assertEqual(caught.exception.error, "bwrap_bind_conflict")
            ro = {"isolation": {"bwrapBinds": [{"path": str(inside), "mode": "ro"}]}}
            self.assertEqual(len(sandbox_bwrap.configured_bwrap_binds(ro, workspace=ws)), 1)
            rel = {"isolation": {"bwrapBinds": [{"path": "vendor", "mode": "ro"}]}}
            with self.assertRaises(DelegateError) as caught:
                sandbox_bwrap.configured_bwrap_binds(rel, workspace=ws)
            self.assertEqual(caught.exception.error, "invalid_isolation_config")

    def test_config_validation_rejects_malformed_entries(self):
        for bad in (
            {"bwrapBinds": "nope"},
            {"bwrapBinds": [{"path": "", "mode": "ro"}]},
            {"bwrapBinds": [{"path": "/x", "mode": "rwx"}]},
            {"bwrapBinds": [{"mode": "ro"}]},
            {"bwrapBinds": ["/x"]},
        ):
            with self.assertRaises(delegate_config.ConfigError) as caught:
                delegate_config._validate_isolation_section(bad)
            self.assertEqual(caught.exception.error, "invalid_isolation_config")
        delegate_config._validate_isolation_section(
            {"bwrapBinds": [{"path": "/x", "mode": "ro"}, {"path": "~/y", "mode": "rw"}]}
        )

    def test_typed_plan_keeps_bind_modes_and_refuses_malformed_entries(self):
        plan = sandbox_bwrap.SandboxPlan(
            None, binds=(sandbox_bwrap.Bind("/a", "ro"), sandbox_bwrap.Bind("/b", "rw"))
        )
        self.assertEqual([bind.path for bind in plan.binds if bind.mode == "ro"], ["/a"])
        self.assertEqual([bind.path for bind in plan.binds if bind.mode == "rw"], ["/b"])
        with self.assertRaises(DelegateError):
            sandbox_bwrap.SandboxPlan(None, binds=({"x": 1},))

    def test_terminal_metadata_reports_effective_isolation_backend(self):
        for backend, sandbox in (("copy", None), ("bwrap", sandbox_bwrap.SandboxPlan(None))):
            with self.subTest(backend=backend):
                ctx = runner.RunContext(
                    registry_root=Path("/tmp"),
                    run_id="run-1",
                    alias="omp-1",
                    harness="omp",
                    engine="omp",
                    mode="safe",
                    model=None,
                    source_cwd="/repo",
                    execution_cwd="/repo",
                    workspace_kind="git",
                    isolated_workspace=True,
                    started_at="2026-08-22T20:00:00Z",
                    isolation_mode="auto",
                    effective_isolation="worktree",
                    isolation_lifecycle="temporary",
                    safe_workspace_method=(
                        "git-worktree" if backend == "copy" else "bwrap-ro-bind"
                    ),
                    sandbox=sandbox,
                )
                payload = runner.completion_json_payload(
                    ctx,
                    ok=True,
                    status="succeeded",
                    exit_code=0,
                    duration_ms=1,
                    stdout_bytes=0,
                    stderr_bytes=0,
                )

                self.assertEqual(payload["isolationBackend"], backend)
                self.assertEqual(runner.build_manifest(ctx, ["omp"])["isolationBackend"], backend)


class RegistryMaskAndContainmentTests(unittest.TestCase):
    def test_workspace_registry_is_masked_then_scratch_rebound(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as scratch_tmp:
            registry = Path(ws) / ".delegate"
            registry.mkdir()
            scratch = Path(scratch_tmp) / "run-scratch" / "bucket" / "del_new"
            scratch.mkdir(parents=True)
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=["true"], cwd=ws, env={}, engine="codex", scratch_dir=str(scratch)
            )
            mask_at = argv.index(str(registry))
            self.assertEqual(argv[mask_at - 1], "--tmpfs")
            self.assertGreater(mask_at, argv.index(ws))
            self.assertGreater(argv.index(str(scratch)), mask_at)
            self.assertEqual(argv[argv.index(str(scratch)) - 1], "--bind")

    def test_engine_binary_outside_core_roots_is_ro_bound_as_a_file(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as bin_dir:
            engine = Path(bin_dir) / "fake-engine"
            engine.write_text("#!/bin/sh\n", encoding="utf-8")
            engine.chmod(0o755)
            (Path(bin_dir) / "sibling-secret").write_text("x", encoding="utf-8")
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=[str(engine)], cwd=ws, env={}, engine="omp"
            )
            self.assertEqual(argv[argv.index(str(engine)) - 1], "--ro-bind")
            self.assertNotIn(bin_dir, argv)  # the directory itself is never bound
            self.assertGreater(argv.index(str(engine)), argv.index("/tmp"))
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=["/usr/bin/env"], cwd=ws, env={}, engine="omp"
            )
            self.assertNotIn("/usr/bin/env", argv[: argv.index("--")])

    def test_all_rw_roots_inside_workspace_are_refused(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as scratch_tmp:
            run_dir = Path(ws) / ".delegate" / "runs" / "del_x"
            run_dir.mkdir(parents=True)
            scratch = Path(scratch_tmp) / "run-scratch" / "bucket" / "del_x"
            scratch.mkdir(parents=True)
            mail_home = run_dir / "mail-push-home"
            mail_home.mkdir()
            (Path(ws) / "src").mkdir()
            for inside in (
                str(Path(ws) / "src"),
                str(Path(ws) / ".delegate"),
                str(mail_home),
            ):
                with self.assertRaises(DelegateError) as caught:
                    sandbox_bwrap.wrap_engine_argv(
                        engine_argv=["true"],
                        cwd=ws,
                        env={},
                        engine="omp",
                        scratch_dir=str(scratch),
                        extra_rw_roots=[inside],
                    )
                self.assertEqual(caught.exception.error, "bwrap_bind_conflict", inside)
            legacy_scratch = run_dir / "scratch"
            legacy_scratch.mkdir()
            with self.assertRaises(DelegateError) as caught:
                sandbox_bwrap.wrap_engine_argv(
                    engine_argv=["true"],
                    cwd=ws,
                    env={},
                    engine="omp",
                    scratch_dir=str(legacy_scratch),
                )
            self.assertEqual(caught.exception.error, "bwrap_bind_conflict")
            with self.assertRaises(DelegateError):
                sandbox_bwrap.wrap_engine_argv(
                    engine_argv=["true"],
                    cwd=ws,
                    env={"CODEX_HOME": str(Path(ws) / "src")},
                    engine="codex",
                )

    def test_preflight_runs_the_final_plan(self):
        true_path = shutil.which("true")
        false_path = shutil.which("false")
        self.assertIsNotNone(true_path)
        self.assertIsNotNone(false_path)
        assert true_path is not None and false_path is not None
        argv = [false_path, "--x", "--", "engine"]
        with self.assertRaises(DelegateError) as caught:
            sandbox_bwrap.preflight_plan(argv)
        self.assertEqual(caught.exception.error, "bwrap_launch_failed")
        sandbox_bwrap.preflight_plan([true_path, "--", "engine"])
        with self.assertRaises(DelegateError):
            sandbox_bwrap.preflight_plan([true_path])

    def test_missing_registry_is_not_masked(self):
        with tempfile.TemporaryDirectory() as ws:
            argv = sandbox_bwrap.wrap_engine_argv(
                engine_argv=["true"], cwd=ws, env={}, engine="omp"
            )
            self.assertNotIn(os.path.join(ws, ".delegate"), argv)

    def test_rw_root_covering_workspace_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            ws = os.path.join(root, "repo")
            os.makedirs(ws)
            for covering in (root, ws):
                with self.assertRaises(DelegateError) as caught:
                    sandbox_bwrap.wrap_engine_argv(
                        engine_argv=["true"],
                        cwd=ws,
                        env={},
                        engine="omp",
                        extra_rw_roots=[covering],
                    )
                self.assertEqual(caught.exception.error, "bwrap_bind_conflict")
            with self.assertRaises(DelegateError) as caught:
                sandbox_bwrap.wrap_engine_argv(
                    engine_argv=["true"], cwd=ws, env={"CODEX_HOME": root}, engine="codex"
                )
            self.assertEqual(caught.exception.error, "bwrap_bind_conflict")


class LinkedWorktreeAndSubmoduleTests(unittest.TestCase):
    def _request(self, workspace: str):
        from delegate_agent.request_models import Request

        return Request(
            engine="codex",
            mode="safe",
            model_alias=None,
            workspace=workspace,
            workspace_kind="git",
            prompt="review",
            argv=["codex", "exec"],
            env_overrides={},
        )

    def test_linked_worktree_binds_common_dir_read_only(self):
        repo = _make_committed_repo()
        self.addCleanup(repo.cleanup)
        linked = tempfile.mkdtemp(prefix="delegate-linked-")
        self.addCleanup(shutil.rmtree, linked, True)
        linked_ws = os.path.join(linked, "wt")
        _git(repo.name, "worktree", "add", "--detach", linked_ws)
        common = safe_workspace._bwrap_git_common_dir(linked_ws)
        self.assertEqual(common, str((Path(repo.name) / ".git").resolve()))
        self.assertIsNone(safe_workspace._bwrap_git_common_dir(repo.name))

    def test_initialized_submodule_is_refused(self):
        outer = _make_committed_repo()
        self.addCleanup(outer.cleanup)
        inner = _make_committed_repo()
        self.addCleanup(inner.cleanup)
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", inner.name, "sub"],
            cwd=outer.name,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with self.assertRaises(DelegateError) as caught:
            safe_workspace._refuse_bwrap_initialized_submodules(outer.name)
        self.assertEqual(caught.exception.error, "bwrap_submodules_unsupported")
        # no submodules: silently fine
        plain = _make_committed_repo()
        self.addCleanup(plain.cleanup)
        safe_workspace._refuse_bwrap_initialized_submodules(plain.name)

    def test_failed_inspection_fails_closed(self):
        with tempfile.TemporaryDirectory() as not_a_repo:
            with self.assertRaises(DelegateError) as caught:
                safe_workspace._refuse_bwrap_initialized_submodules(not_a_repo)
            self.assertEqual(caught.exception.error, "bwrap_submodules_unsupported")
            with self.assertRaises(DelegateError) as caught:
                safe_workspace._bwrap_git_common_dir(not_a_repo)
            self.assertEqual(caught.exception.error, "bwrap_unavailable")


class PassThroughBwrapTests(CommandTestBase):
    def test_pass_through_is_refused_under_bwrap(self):
        repo = _make_committed_repo()
        self.addCleanup(repo.cleanup)
        fake_bin = self.write_fake_executable("codex")
        with (
            mock.patch.dict(os.environ, {"DELEGATE_SAFE_BACKEND": "bwrap"}, clear=False),
            mock.patch.object(
                safe_workspace, "ensure_bwrap_backend", return_value="/usr/bin/bwrap"
            ),
        ):
            code, _out, err = self.run_main(
                ["--cwd", repo.name, "--pass-through", "codex", "safe", "review"],
                path_prefix=fake_bin,
            )
        self.assertNotEqual(code, 0)
        self.assertIn("--pass-through cannot run inside the bwrap safe backend", err)


@unittest.skipUnless(sys.platform.startswith("linux"), "bwrap boundary is Linux-only")
class EndToEndBwrapRunTests(CommandTestBase):
    """A tracked safe run through bin/delegate.py inside the real boundary."""

    def test_tracked_safe_run_hides_registry_and_denies_writes(self):
        if not sandbox_bwrap.bwrap_available():
            self.skipTest("working bubblewrap unavailable on this host")
        repo = _make_committed_repo()
        self.addCleanup(repo.cleanup)
        workspace = Path(repo.name)
        old_prompt = workspace / ".delegate" / "runs" / "del_old" / "prompt.txt"
        old_prompt.parent.mkdir(parents=True)
        old_prompt.write_text("OLD-PROMPT-CANARY\n", encoding="utf-8")
        fake_dir = tempfile.TemporaryDirectory()
        self.addCleanup(fake_dir.cleanup)
        fake = Path(fake_dir.name) / "omp"
        fake.write_text(
            "#!/bin/sh\n"
            "if touch leak.txt 2>/dev/null; then echo write=allowed; else echo write=denied; fi\n"
            'if touch "$TMPDIR/bwrap-temp" 2>/dev/null; then echo temp=allowed; else echo temp=denied; fi\n'
            "if [ -e .delegate/runs/del_old/prompt.txt ]; then echo registry=visible; "
            "else echo registry=hidden; fi\n"
            "if grep -q OLD-PROMPT-CANARY .delegate/runs/*/prompt.txt 2>/dev/null; then echo canary=leaked; "
            "else echo canary=clean; fi\n"
            "git status --short >/dev/null 2>&1 && echo git=ok || echo git=error\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        config_path = Path(self._config_env["DELEGATE_CONFIG"])
        config_path.write_text(
            json.dumps({"omp": {"binary": str(fake)}, "isolation": {"safeBackend": "bwrap"}}),
            encoding="utf-8",
        )
        code, out, err = self.run_main(
            ["--json", "--cwd", str(workspace), "--no-completion-report", "omp", "safe", "review"]
        )
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "succeeded", payload)
        self.assertEqual(payload["safeWorkspaceMethod"], sandbox_bwrap.BWRAP_METHOD)
        # The fake engine's raw stdout is captured verbatim in the run registry
        # (omp's assistant-text extraction expects its own event stream).
        raw = (workspace / ".delegate" / "runs" / payload["runId"] / "stdout.log").read_text(
            encoding="utf-8"
        )
        observed = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        self.assertEqual(observed.get("write"), "denied", raw)
        self.assertEqual(observed.get("temp"), "allowed", raw)
        self.assertEqual(observed.get("registry"), "hidden")
        self.assertEqual(observed.get("canary"), "clean")
        self.assertEqual(observed.get("git"), "ok")
        self.assertFalse((workspace / "leak.txt").exists())
        manifest = run_registry.load_run_manifest(workspace / ".delegate", payload["runId"])
        self.assertTrue((Path(manifest["scratchPath"]) / "bwrap-temp").is_file())

    def test_boundary_construction_error_is_a_recorded_launch_failure(self):
        if not sandbox_bwrap.bwrap_available():
            self.skipTest("working bubblewrap unavailable on this host")
        repo = _make_committed_repo()
        self.addCleanup(repo.cleanup)
        workspace = Path(repo.name)
        fake_dir = tempfile.TemporaryDirectory()
        self.addCleanup(fake_dir.cleanup)
        fake = Path(fake_dir.name) / "omp"
        fake.write_text("#!/bin/sh\necho ran\n", encoding="utf-8")
        fake.chmod(0o755)
        config_path = Path(self._config_env["DELEGATE_CONFIG"])
        config_path.write_text(
            json.dumps({"omp": {"binary": str(fake)}, "isolation": {"safeBackend": "bwrap"}}),
            encoding="utf-8",
        )
        failure = DelegateError("bwrap_launch_failed", "bwrap preflight failed: synthetic")
        with mock.patch.object(sandbox_bwrap, "preflight_plan", side_effect=failure):
            code, out, _err = self.run_main(
                ["--json", "--cwd", str(workspace), "--no-completion-report", "omp", "safe", "x"]
            )
        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload.get("error"), "bwrap_launch_failed", out)
        runs = [d for d in (workspace / ".delegate" / "runs").iterdir() if d.is_dir()]
        self.assertEqual(len(runs), 1, runs)
        state = json.loads((runs[0] / "state.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state.get("error"), "bwrap_launch_failed")


class MailPushSandboxBoundaryTests(unittest.TestCase):
    """The mail-push private home must be bindable inside a bwrap boundary."""

    def test_mail_push_home_is_a_writable_root_outside_the_read_only_workspace(self):
        with tempfile.TemporaryDirectory(prefix="delegate-mail-push-bwrap-") as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            source_codex = root / "codex"
            source_codex.mkdir()
            (source_codex / "auth.json").write_text('{"token":"x"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
                run_id, alias = run_registry.register_run(registry_root, harness="codex")
                env: dict[str, str] = {"CODEX_HOME": str(source_codex)}
                provision = mail_push.provision_mail_push(
                    "codex",
                    ["codex", "exec", "prompt"],
                    None,
                    registry_root,
                    run_id,
                    env,
                )
                self.assertIsNone(provision.warning)
                codex_home = provision.codex_home
                self.assertIsNotNone(codex_home)
                assert codex_home is not None
                ctx = runner.RunContext(
                    registry_root=registry_root,
                    run_id=run_id,
                    alias=alias,
                    harness="codex",
                    engine="codex",
                    mode="safe",
                    model=None,
                    source_cwd=str(workspace),
                    execution_cwd=str(workspace),
                    workspace_kind="directory",
                    isolated_workspace=False,
                    started_at=run_registry.utc_now_iso(),
                    mail_push=True,
                )
                rw_roots = runner._bwrap_mail_push_rw_roots(ctx)
                scratch = run_scratch.allocate(registry_root, run_id)
                self.assertEqual(len(rw_roots), 1, rw_roots)
                argv = sandbox_bwrap.wrap_engine_argv(
                    engine_argv=["true"],
                    cwd=str(workspace),
                    env={"HOME": str(home), "CODEX_HOME": codex_home},
                    engine="codex",
                    scratch_dir=str(scratch),
                    extra_rw_roots=rw_roots,
                )
                resolved_workspace = workspace.resolve()
                self.assertTrue(Path(codex_home).is_relative_to(Path(rw_roots[0])))
                for rw_root in (*rw_roots, codex_home):
                    self.assertFalse(
                        Path(rw_root).resolve().is_relative_to(resolved_workspace), rw_root
                    )
                self.assertEqual(argv[argv.index(rw_roots[0]) - 1], "--bind")
                mail_push.cleanup_mail_push_private_homes(registry_root, run_id)
                self.assertFalse(Path(codex_home).exists())


if __name__ == "__main__":
    unittest.main()
