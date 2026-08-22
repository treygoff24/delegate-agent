import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase, make_git_repo

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Imported after the base (which bootstraps sys.path).
from delegate_agent import config as delegate_config  # noqa: E402
from delegate_agent import (  # noqa: E402
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

    def test_engine_homes_from_env_appended_rw_after_rw_roots(self):
        argv = sandbox_bwrap.build_bwrap_argv(
            workspace="/ws",
            engine_argv=["engine"],
            env={"CODEX_HOME": "/eng/codex", "CLAUDE_CONFIG_DIR": "/eng/claude"},
            home="/home/fake",
            rw_roots=["/scratch"],
            ro_roots=[],
            masks=(),
        )
        homes = ["--bind", "/eng/codex", "/eng/codex", "--bind", "/eng/claude", "/eng/claude"]
        chdir = argv.index("--chdir")
        self.assertEqual(argv[chdir - len(homes) : chdir], homes)

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


class ProbeCacheTests(unittest.TestCase):
    def setUp(self):
        cache_root = tempfile.TemporaryDirectory()
        self.addCleanup(cache_root.cleanup)
        self.cache_dir = Path(cache_root.name)
        self.cache_file = self.cache_dir / "delegate" / "bwrap-probe.json"
        self.boot = {"id": "boot-1"}
        # A stable fake bwrap path so the cache key's mtime component never moves.
        self.bwrap_path = str(self.cache_dir / "fake-bwrap")
        Path(self.bwrap_path).write_text("", encoding="utf-8")

    def available(self, probe):
        with (
            mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache_dir)}, clear=False),
            mock.patch.object(sandbox_bwrap, "_boot_id", lambda: self.boot["id"]),
            mock.patch.object(sandbox_bwrap, "_run_probe", probe),
        ):
            return sandbox_bwrap.bwrap_available(self.bwrap_path)

    def test_miss_then_hit_then_boot_id_invalidation(self):
        probe = mock.MagicMock(return_value=True)
        self.assertTrue(self.available(probe))
        self.assertEqual(probe.call_count, 1)
        cached = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertTrue(cached["available"])
        self.assertTrue(self.available(probe))
        self.assertEqual(probe.call_count, 1)
        self.boot["id"] = "boot-2"
        self.assertTrue(self.available(probe))
        self.assertEqual(probe.call_count, 2)

    def test_negative_result_is_cached_too(self):
        probe = mock.MagicMock(return_value=False)
        self.assertFalse(self.available(probe))
        self.assertFalse(self.available(probe))
        self.assertEqual(probe.call_count, 1)

    def test_corrupt_cache_fails_open_to_probe(self):
        probe = mock.MagicMock(return_value=True)
        self.assertTrue(self.available(probe))
        self.cache_file.write_text("{not json", encoding="utf-8")
        self.assertTrue(self.available(probe))
        self.assertEqual(probe.call_count, 2)
        json.loads(self.cache_file.read_text(encoding="utf-8"))  # the rewrite healed it
        self.assertTrue(self.available(probe))
        self.assertEqual(probe.call_count, 2)


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
        iso_ctx = self.delegate.build_isolation_context(
            source_workspace=str(self.repo),
            resolved_isolation="auto",
            engine=engine,
            mode="safe",
            source_git_root=str(self.repo),
        )
        return self.delegate.build_request(
            engine,
            "safe",
            None,
            self.delegate.ResolvedWorkspace(str(self.repo), "git"),
            "review",
            self.delegate.DEFAULT_CONFIG,
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
            self.assertEqual(ctx.sandbox["backend"], "bwrap")
            self.assertEqual(
                ctx.sandbox["masks"],
                [{"path": "secret.env", "kind": sandbox_bwrap.MASK_KIND_DEVNULL}],
            )


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
            tempfile.TemporaryDirectory() as scratch_tmp,
        ):
            home = Path(home_tmp)
            profile_dir = home / ".ai-profiles" / "personal"
            profile_dir.mkdir(parents=True)
            canary = profile_dir / "keys.zsh"
            canary.write_text("# CANARY never-leak\n", encoding="utf-8")

            scratch = Path(scratch_tmp)
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
        # shadow it (live failure: execvp estate-codex ENOENT).
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
                    sandbox_bwrap.Bind(path=os.path.join(home, ".local", "bin"), mode="ro"),
                    sandbox_bwrap.Bind(path=sock, mode="rw"),
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
                (sandbox_bwrap.Bind(path=root, mode="ro"),),
            )

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

    def test_runner_splits_binds_by_mode(self):
        payload = {
            "backend": "bwrap",
            "masks": [],
            "binds": [{"path": "/a", "mode": "ro"}, {"path": "/b", "mode": "rw"}, {"x": 1}],
        }
        self.assertEqual(runner._binds_from_sandbox(payload, "ro"), ["/a"])
        self.assertEqual(runner._binds_from_sandbox(payload, "rw"), ["/b"])
        self.assertEqual(runner._binds_from_sandbox(None, "rw"), [])


if __name__ == "__main__":
    unittest.main()
