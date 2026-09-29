"""Work write guard: plan, settings, backends' argv, fallbacks, and --forbid-commit hooks.

The live confinement proofs (a real child refused writes) live in
``tests/test_write_guard_live.py``; this file covers everything that needs no
sandbox to run.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli_parser as parser_api
from delegate_agent import config as delegate_config
from delegate_agent import errors as error_types
from delegate_agent import request_build, runner, sandbox_bwrap, seatbelt, write_guard
from delegate_agent import write_guard_launch as launch
from delegate_agent.write_guard import GuardFacts, Reopen, WriteGuardSettings
from tests.execution_test_base import load_delegate

GIT_IDENTITY = ("-c", "user.name=Delegate Test", "-c", "user.email=delegate-test@example.com")


def run_git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


class HomeTestCase(unittest.TestCase):
    """A throwaway HOME laid out like the estate: credentials, profiles, the runtime, code."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        for rel in (
            ".ssh",
            ".gnupg",
            ".ai-profiles/accounts/claude/work/work-d",
            ".ai-profiles/accounts/claude/personal",
            ".config/gh",
            ".delegate/src",
            ".delegate/releases",
            ".delegate/bin",
            ".delegate/worktrees/run-1",
            ".delegate/registry",
            ".cache",
            "Code/repo",
            "Code/sibling",
        ):
            (self.home / rel).mkdir(parents=True)
        (self.home / ".delegate" / "config.json").write_text("{}", encoding="utf-8")
        (self.home / ".delegate" / "config.local.json").write_text("{}", encoding="utf-8")

    def real(self, rel: str) -> str:
        return os.path.realpath(self.home / rel)

    def plan(self, settings=None, **facts):
        facts.setdefault("exec_root", str(self.home / "Code" / "repo"))
        return write_guard.plan_guard(
            settings or WriteGuardSettings(),
            GuardFacts(home=str(self.home), **facts),
            backend=write_guard.BACKEND_BWRAP,
        )


class PlanTests(HomeTestCase):
    def test_protects_the_named_paths_and_leaves_run_state_writable(self):
        plan = self.plan()
        for rel in (
            ".ssh",
            ".gnupg",
            ".ai-profiles",
            ".config/gh",
            ".delegate/src",
            ".delegate/releases",
            ".delegate/bin",
            ".delegate/config.json",
            ".delegate/config.local.json",
            "Code",
        ):
            with self.subTest(path=rel):
                self.assertIn(self.real(rel), plan.protected)
        for rel in (".delegate/worktrees", ".delegate/registry", ".cache", "."):
            with self.subTest(path=rel):
                self.assertNotIn(self.real(rel), plan.protected)

    def test_paths_that_do_not_exist_are_dropped(self):
        plan = self.plan()
        self.assertNotIn(self.real(".aws"), plan.protected)
        self.assertTrue(all(os.path.exists(path) for path in plan.protected))

    def test_parents_come_before_children_and_the_exec_root_is_reopened(self):
        mounts = self.plan().mounts()
        code = ("ro", self.real("Code"))
        repo = ("rw", self.real("Code/repo"))
        self.assertIn(code, mounts)
        self.assertIn(repo, mounts)
        self.assertLess(mounts.index(code), mounts.index(repo))
        self.assertNotIn(("rw", self.real("Code/sibling")), mounts)

    def test_git_common_dir_of_a_worktree_is_reopened_under_the_protected_source(self):
        (self.home / "Code/repo/.git").mkdir()
        plan = self.plan(
            exec_root=str(self.home / ".delegate/worktrees/run-1"),
            git_common_dir=str(self.home / "Code/repo/.git"),
        )
        mounts = plan.mounts()
        self.assertIn(("rw", self.real("Code/repo/.git")), mounts)
        self.assertLess(
            mounts.index(("ro", self.real("Code"))),
            mounts.index(("rw", self.real("Code/repo/.git"))),
        )
        # The worktree itself is outside every protected path, so it needs no mount.
        self.assertNotIn(self.real(".delegate/worktrees/run-1"), [path for _, path in mounts])

    def test_engine_home_is_reopened_inside_ai_profiles_and_siblings_stay_protected(self):
        engine_home = str(self.home / ".ai-profiles/accounts/claude/work/work-d")
        plan = self.plan(run_roots=(Reopen(engine_home, "engine home"),))
        mounts = plan.mounts()
        self.assertIn(("rw", self.real(".ai-profiles/accounts/claude/work/work-d")), mounts)
        self.assertNotIn(("rw", self.real(".ai-profiles/accounts/claude/personal")), mounts)
        self.assertIn(("ro", self.real(".ai-profiles")), mounts)

    def test_only_profile_directories_named_by_the_environment_are_reopened(self):
        profile = self.home / ".ai-profiles/accounts/claude/work/work-d"
        env = {
            "CLAUDE_CONFIG_DIR": str(profile),
            "GNUPGHOME": str(self.home / ".gnupg"),
            "PROFILES_ROOT": str(self.home / ".ai-profiles"),
            "NOT_A_DIR": str(self.home / "missing"),
            "RELATIVE": "accounts/claude",
            "EMPTY": "",
        }
        roots = launch.profile_home_roots(env, str(self.home))
        self.assertEqual([root.path for root in roots], [os.path.realpath(profile)])
        self.assertEqual(roots[0].reason, "profile home")

    def test_remove_drops_a_default_and_add_protects_an_extra_path(self):
        (self.home / "vault").mkdir()
        plan = self.plan(
            WriteGuardSettings(remove=("~/.gnupg",), add=("~/vault",)),
        )
        self.assertNotIn(self.real(".gnupg"), plan.protected)
        self.assertIn(self.real("vault"), plan.protected)
        self.assertIn(self.real(".ssh"), plan.protected)

    def test_config_writable_and_run_writable_lift_a_protection(self):
        plan = self.plan(
            WriteGuardSettings(writable=("~/.ssh",), run_writable=(self.real("Code/sibling"),))
        )
        self.assertNotIn(self.real(".ssh"), plan.protected)
        mounts = plan.mounts()
        self.assertIn(("rw", self.real("Code/sibling")), mounts)
        reasons = {entry.path: entry.reason for entry in plan.writable}
        self.assertEqual(reasons[self.real("Code/sibling")], "--writable")
        self.assertEqual(reasons[self.real(".ssh")], "isolation.writeGuard.writable")

    def test_a_reopen_that_is_already_writable_needs_no_mount(self):
        plan = self.plan(run_roots=(Reopen(str(self.home / ".cache"), "cache"),))
        self.assertNotIn(("rw", self.real(".cache")), plan.mounts())

    def test_a_null_code_root_leaves_checkouts_unprotected(self):
        plan = self.plan(WriteGuardSettings(code_root=None))
        self.assertNotIn(self.real("Code"), plan.protected)

    def test_payload_names_backend_protected_and_reopens_with_reasons(self):
        payload = self.plan().payload()
        self.assertEqual(payload["backend"], "bwrap")
        self.assertIn(self.real(".ssh"), payload["protected"])
        self.assertIn(
            {"path": self.real("Code/repo"), "reason": "execution root"}, payload["writable"]
        )


class SettingsTests(unittest.TestCase):
    def test_defaults_are_on_and_warn(self):
        settings = write_guard.settings_from_config({}, env={})
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.on_unavailable, "warn")
        self.assertFalse(settings.macos_seatbelt)
        self.assertEqual(settings.code_root, "~/Code")

    def test_config_section_is_read(self):
        config = {
            "isolation": {
                "writeGuard": {
                    "enabled": False,
                    "onUnavailable": "refuse",
                    "macosSeatbelt": True,
                    "codeRoot": "/srv/code",
                    "add": ["~/vault"],
                    "remove": ["~/.gnupg"],
                    "writable": ["~/.cache/tool"],
                }
            }
        }
        settings = write_guard.settings_from_config(config, env={})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.on_unavailable, "refuse")
        self.assertTrue(settings.macos_seatbelt)
        self.assertEqual(settings.code_root, "/srv/code")
        self.assertEqual(settings.add, ("~/vault",))
        self.assertEqual(settings.remove, ("~/.gnupg",))
        self.assertEqual(settings.writable, ("~/.cache/tool",))

    def test_environment_override_wins_over_config_both_ways(self):
        on = {"isolation": {"writeGuard": {"enabled": True}}}
        off = {"isolation": {"writeGuard": {"enabled": False}}}
        self.assertFalse(
            write_guard.settings_from_config(on, env={write_guard.ENV_OVERRIDE: "off"}).enabled
        )
        self.assertTrue(
            write_guard.settings_from_config(off, env={write_guard.ENV_OVERRIDE: "1"}).enabled
        )

    def test_validation_names_the_bad_key(self):
        bad = {
            "enabled": "yes",
            "onUnavailable": "shrug",
            "macosSeatbelt": 1,
            "codeRoot": "relative/path",
            "add": "~/x",
            "remove": ["relative"],
            "unknownKey": True,
        }
        for key, value in bad.items():
            with self.subTest(key=key):
                message = write_guard.validate_config_section({key: value})
                self.assertIsNotNone(message)
                self.assertIn(key, message)
        self.assertIsNone(write_guard.validate_config_section(None))
        self.assertIsNone(
            write_guard.validate_config_section({"codeRoot": None, "add": ["~/vault", "/abs"]})
        )

    def test_config_validation_rejects_a_bad_section(self):
        config = delegate_config.embedded_default_config()
        config["isolation"] = {**config["isolation"], "writeGuard": {"onUnavailable": "shrug"}}
        with self.assertRaises(delegate_config.ConfigError) as ctx:
            delegate_config.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_isolation_config")
        self.assertIn("onUnavailable", ctx.exception.message)


class BackendArgvTests(unittest.TestCase):
    def test_bwrap_argv_binds_root_writable_then_each_mount_in_order(self):
        argv = sandbox_bwrap.build_work_guard_argv(
            mounts=[("ro", "/home/u/Code"), ("rw", "/home/u/Code/repo")],
            engine_argv=["engine", "--flag"],
            cwd="/home/u/Code/repo",
            bwrap_path="/usr/bin/bwrap",
        )
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        root = argv.index("--dev-bind")
        self.assertEqual(argv[root : root + 3], ["--dev-bind", "/", "/"])
        ro = argv.index("--ro-bind")
        rw = argv.index("--bind")
        self.assertLess(root, ro)
        self.assertLess(ro, rw)
        self.assertEqual(argv[ro : ro + 3], ["--ro-bind", "/home/u/Code", "/home/u/Code"])
        self.assertEqual(argv[rw : rw + 3], ["--bind", "/home/u/Code/repo", "/home/u/Code/repo"])
        self.assertEqual(argv[-5:], ["--chdir", "/home/u/Code/repo", "--", "engine", "--flag"][-5:])
        self.assertIn("--die-with-parent", argv)
        self.assertNotIn("--tmpfs", argv)

    def test_bwrap_argv_refuses_a_relative_mount_path(self):
        with self.assertRaises(error_types.DelegateError):
            sandbox_bwrap.build_work_guard_argv(
                mounts=[("ro", "relative")], engine_argv=["e"], cwd="/w"
            )

    def test_seatbelt_profile_allows_by_default_then_denies_and_reallows_in_order(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            (base / "code" / "repo").mkdir(parents=True)
            profile = seatbelt.build_work_guard_profile(
                [("ro", str(base / "code")), ("rw", str(base / "code" / "repo"))],
                no_unlink=[str(base / "code" / "repo")],
            )
        lines = profile.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(allow default)"])
        deny = next(i for i, line in enumerate(lines) if line.startswith("(deny file-write*"))
        allow = next(i for i, line in enumerate(lines) if line.startswith("(allow file-write*"))
        unlink = next(i for i, line in enumerate(lines) if "file-write-unlink" in line)
        self.assertLess(deny, allow)
        self.assertLess(allow, unlink)
        self.assertIn("(literal ", lines[unlink])

    def test_seatbelt_wrap_prefixes_sandbox_exec(self):
        self.assertEqual(
            seatbelt.work_guard_argv("(version 1)", ["engine", "x"]),
            ["sandbox-exec", "-p", "(version 1)", "engine", "x"],
        )


class FallbackTests(HomeTestCase):
    def apply(self, settings, *, platform, which, argv=None, engine="cursor"):
        env = {"HOME": str(self.home)}
        with (
            mock.patch.object(sys, "platform", platform),
            mock.patch.object(shutil, "which", return_value=which),
        ):
            return launch.apply_write_guard(
                settings,
                argv=argv or ["engine"],
                cwd=str(self.home / "Code" / "repo"),
                env=env,
                engine=engine,
            )

    def test_missing_bwrap_warns_and_runs_unguarded_by_default(self):
        result = self.apply(WriteGuardSettings(), platform="linux", which=None)
        self.assertEqual(result.argv, ["engine"])
        self.assertEqual(result.record["status"], "unavailable")
        self.assertIn("bubblewrap", result.record["reason"])
        self.assertIn("not guarded", result.warning)

    def test_missing_bwrap_refuses_when_configured(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            self.apply(WriteGuardSettings(on_unavailable="refuse"), platform="linux", which=None)
        self.assertEqual(ctx.exception.error, "write_guard_unavailable")
        self.assertIn("onUnavailable", ctx.exception.message)

    def test_a_failed_bwrap_preflight_counts_as_unavailable(self):
        failure = error_types.DelegateError("bwrap_preflight_failed", "user namespaces disabled")
        with mock.patch.object(sandbox_bwrap, "preflight_plan", side_effect=failure):
            result = self.apply(WriteGuardSettings(), platform="linux", which="/usr/bin/bwrap")
        self.assertEqual(result.argv, ["engine"])
        self.assertEqual(result.record["status"], "unavailable")
        self.assertIn("user namespaces", result.record["reason"])
        self.assertIsNotNone(result.warning)

    def apply_with_seatbelt_probe(self, settings, *, returncode, stderr=b""):
        real_run = subprocess.run

        def run(argv, *args, **kwargs):
            if argv and argv[0] == "sandbox-exec":
                return subprocess.CompletedProcess(argv, returncode, b"", stderr)
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(launch.subprocess, "run", side_effect=run):
            return self.apply(
                settings, platform="darwin", which="/usr/bin/sandbox-exec", argv=["engine", "x"]
            )

    def test_seatbelt_wraps_when_the_probe_passes(self):
        result = self.apply_with_seatbelt_probe(
            WriteGuardSettings(macos_seatbelt=True), returncode=0
        )
        self.assertEqual(result.argv[:2], ["sandbox-exec", "-p"])
        self.assertEqual(result.argv[-2:], ["engine", "x"])
        self.assertEqual(result.record["status"], "enforced")
        self.assertEqual(result.record["backend"], "seatbelt")
        self.assertIsNone(result.warning)

    def test_a_failed_seatbelt_probe_counts_as_unavailable(self):
        # A nested sandbox_apply fails inside an already sandboxed process; the run
        # must not be wrapped in a profile that cannot start.
        result = self.apply_with_seatbelt_probe(
            WriteGuardSettings(macos_seatbelt=True),
            returncode=71,
            stderr=b"sandbox_apply: Operation not permitted\n",
        )
        self.assertEqual(result.argv, ["engine", "x"])
        self.assertEqual(result.record["status"], "unavailable")
        self.assertIn("preflight failed", result.record["reason"])
        self.assertIn("Operation not permitted", result.record["reason"])
        self.assertIn("not guarded", result.warning)

    def test_a_failed_seatbelt_probe_refuses_when_configured(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            self.apply_with_seatbelt_probe(
                WriteGuardSettings(macos_seatbelt=True, on_unavailable="refuse"), returncode=71
            )
        self.assertEqual(ctx.exception.error, "write_guard_unavailable")

    def test_macos_seatbelt_off_is_off_not_unavailable(self):
        # Opt-in on the Mac: nothing is missing, so no warning and no refusal.
        for on_unavailable in ("warn", "refuse"):
            with self.subTest(on_unavailable=on_unavailable):
                result = self.apply(
                    WriteGuardSettings(on_unavailable=on_unavailable),
                    platform="darwin",
                    which="/usr/bin/sandbox-exec",
                )
                self.assertEqual(result.argv, ["engine"])
                self.assertEqual(result.record["status"], "off")
                self.assertIsNone(result.warning)

    def test_codex_with_its_own_sandbox_is_never_wrapped(self):
        argv = ["codex", "exec", "--sandbox", "workspace-write", "task"]
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                result = self.apply(
                    WriteGuardSettings(macos_seatbelt=True),
                    platform=platform,
                    which="/usr/bin/tool",
                    argv=argv,
                    engine="codex",
                )
                self.assertEqual(result.argv, argv)
                self.assertEqual(result.record["backend"], "codex-native-sandbox")

    def test_codex_bypass_is_wrapped_like_any_other_engine_on_linux(self):
        argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "task"]
        self.assertFalse(write_guard.codex_native_sandbox_on(argv))
        with mock.patch.object(sandbox_bwrap, "preflight_plan"):
            result = self.apply(
                WriteGuardSettings(),
                platform="linux",
                which="/usr/bin/bwrap",
                argv=argv,
                engine="codex",
            )
        self.assertEqual(result.record["backend"], "bwrap")
        self.assertEqual(result.argv[0], "/usr/bin/bwrap")
        self.assertEqual(result.argv[-len(argv) :], argv)


class LaunchSeamGuardTests(HomeTestCase):
    def test_native_codex_record_survives_the_launch_and_the_child_is_not_wrapped(self):
        # Seatbelt is switched on so a wrongly wrapped Codex would show: the record
        # captured with the argv's --add-dir roots must not be replaced at launch.
        repo = self.home / "Code" / "repo"
        out = self.home / "argv.txt"
        script = self.home / "codex"
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$0" > "{out}"\n', encoding="utf-8")
        script.chmod(0o755)
        record = {"backend": "codex-native-sandbox", "status": "recorded-earlier"}
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            process = runner._launch_tracked_process(
                [str(script), "exec", "--sandbox", "workspace-write", "task"],
                str(repo),
                stdin_text=None,
                engine="codex",
                run_path=self.home / ".delegate" / "registry",
                guard_settings=WriteGuardSettings(macos_seatbelt=True),
                guard_record=record,
            )
            process.communicate(timeout=30)
        self.assertEqual(record, {"backend": "codex-native-sandbox", "status": "recorded-earlier"})
        self.assertEqual(out.read_text(encoding="utf-8").strip(), str(script))
        self.assertEqual(process.args[0], str(script))


class CodexNativeRootsTests(HomeTestCase):
    def test_sandbox_detection_reads_the_argv(self):
        self.assertTrue(
            write_guard.codex_native_sandbox_on(["codex", "exec", "--sandbox", "workspace-write"])
        )
        self.assertFalse(
            write_guard.codex_native_sandbox_on(["codex", "exec", "--sandbox", "read-only"])
        )
        self.assertFalse(
            write_guard.codex_native_sandbox_on(
                ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
            )
        )

    def test_roots_include_git_common_dir_even_inside_the_exec_root(self):
        (self.home / "Code/repo/.git").mkdir()
        (self.home / ".cache/tool").mkdir()
        facts = GuardFacts(
            home=str(self.home),
            exec_root=str(self.home / "Code/repo"),
            git_common_dir=str(self.home / "Code/repo/.git"),
            registry_root=str(self.home / ".delegate/registry"),
            run_roots=(Reopen(str(self.home / "Code/repo/scratch"), "inside"),),
        )
        roots = {
            r.path: r.reason for r in write_guard.native_writable_roots(WriteGuardSettings(), facts)
        }
        # Codex protects .git under a writable root unless it is its own root.
        self.assertEqual(roots[self.real("Code/repo/.git")], "git common dir")
        self.assertEqual(roots[self.real(".delegate/registry")], "run registry")
        self.assertEqual(roots[self.real(".cache")], "home cache")
        self.assertNotIn(self.real("Code/repo"), roots)

    def test_add_dir_roots_go_right_after_exec_and_survive_resume(self):
        roots = (Reopen("/a", "x"), Reopen("/b", "y"))
        fresh = launch.codex_argv_with_work_roots(
            ["codex", "-c", "k=v", "exec", "--cd", "/w", "--sandbox", "workspace-write", "task"],
            roots,
        )
        self.assertEqual(
            fresh[:8], ["codex", "-c", "k=v", "exec", "--add-dir", "/a", "--add-dir", "/b"]
        )
        resume = launch.codex_argv_with_work_roots(
            ["codex", "exec", "--sandbox", "workspace-write", "resume", "sid"], roots
        )
        self.assertLess(resume.index("--add-dir"), resume.index("resume"))

    def test_add_dir_is_not_added_when_codex_bypasses_its_sandbox(self):
        argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "task"]
        self.assertEqual(launch.codex_argv_with_work_roots(argv, (Reopen("/a", "x"),)), argv)

    def test_native_launch_is_none_without_the_sandbox(self):
        self.assertIsNone(
            launch.codex_native_launch(
                WriteGuardSettings(),
                argv=["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
                cwd=str(self.home),
                registry_root=None,
            )
        )


class PreviewTests(HomeTestCase):
    def test_preview_lists_the_plan_for_the_predicted_backend(self):
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
        ):
            payload = launch.preview_payload(
                WriteGuardSettings(),
                engine="cursor",
                argv=["agent"],
                exec_root=str(self.home / "Code/repo"),
                registry_root=None,
                home=str(self.home),
            )
        self.assertEqual(payload["backend"], "bwrap")
        self.assertEqual(payload["status"], "planned")
        self.assertIn(self.real(".ssh"), payload["protected"])

    def test_preview_says_why_there_is_no_guard(self):
        with mock.patch.object(sys, "platform", "darwin"):
            payload = launch.preview_payload(
                WriteGuardSettings(),
                engine="cursor",
                argv=["agent"],
                exec_root=str(self.home),
                registry_root=None,
                home=str(self.home),
            )
        self.assertIsNone(payload["backend"])
        self.assertEqual(payload["status"], "off")
        self.assertIn("macosSeatbelt", payload["reason"])


class PromptNoteTests(unittest.TestCase):
    def test_note_names_the_protected_paths_for_a_protect_list_backend(self):
        note = write_guard.prompt_note(WriteGuardSettings(), write_guard.BACKEND_BWRAP, home="/h")
        self.assertIn("SSH", note)
        self.assertIn("/h/Code", note)
        self.assertIn("read-only or permission error", note)

    def test_note_for_codex_native_says_where_it_may_write(self):
        note = write_guard.prompt_note(WriteGuardSettings(), write_guard.BACKEND_CODEX_NATIVE)
        self.assertIn("Codex", note)
        self.assertIn("git directory", note)

    def test_no_backend_means_no_note(self):
        self.assertIsNone(write_guard.prompt_note(WriteGuardSettings(), None))


class RequestWiringTests(HomeTestCase):
    """The guard settings, --writable and the note travel from the CLI into the request."""

    def setUp(self):
        super().setUp()
        self.delegate = load_delegate()
        patcher = mock.patch.dict(
            os.environ, {"HOME": str(self.home), write_guard.ENV_OVERRIDE: "on"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.home / "Code" / "repo"
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)

    def build(self, *args, config=None, dry_run=True):
        command = ["--cwd", str(self.repo), "--json"]
        if dry_run:
            command.append("dry-run")
        parsed = parser_api.parse_cli([*command, *args])
        return request_build.request_from_parsed(
            parsed, config or delegate_config.embedded_default_config(), io.StringIO("")
        )

    def with_backend(self):
        return (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
        )

    def test_work_request_carries_settings_and_the_prompt_note(self):
        patches = self.with_backend()
        with patches[0], patches[1]:
            request = self.build("cursor", "work", "fix it")
        self.assertIsNotNone(request.write_guard)
        self.assertTrue(request.write_guard.enabled)
        self.assertIn("Delegate write guard", request.prompt)

    def test_safe_and_call_requests_carry_no_guard(self):
        patches = self.with_backend()
        with patches[0], patches[1]:
            safe = self.build("cursor", "safe", "look")
        self.assertIsNone(safe.write_guard)
        self.assertNotIn("Delegate write guard", safe.prompt)

    def test_disabled_guard_adds_no_note(self):
        with mock.patch.dict(os.environ, {write_guard.ENV_OVERRIDE: "off"}):
            request = self.build("cursor", "work", "fix it")
        self.assertFalse(request.write_guard.enabled)
        self.assertNotIn("Delegate write guard", request.prompt)

    def test_codex_note_says_native_sandbox_when_the_bypass_is_off(self):
        config = delegate_config.embedded_default_config()
        with mock.patch.object(sys, "platform", "darwin"):
            request = self.build("codex", "work", "fix it", config=config)
        self.assertIn("sandboxed by Codex", request.prompt)

    def test_writable_flag_resolves_into_run_writable(self):
        extra = self.home / "Code" / "sibling"
        request = self.build("cursor", "work", "--writable", str(extra), "fix it")
        self.assertEqual(request.write_guard.run_writable, (os.path.realpath(extra),))

    def test_writable_flag_resolves_relative_paths_against_the_cwd(self):
        request = self.build("cursor", "work", "--writable", "../sibling", "fix it")
        self.assertEqual(
            request.write_guard.run_writable, (os.path.realpath(self.home / "Code/sibling"),)
        )

    def test_writable_flag_repeats(self):
        request = self.build(
            "cursor",
            "work",
            "--writable",
            str(self.home / ".ssh"),
            "--writable",
            str(self.home / ".gnupg"),
            "fix it",
        )
        self.assertEqual(len(request.write_guard.run_writable), 2)

    def test_writable_path_must_exist(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            self.build("cursor", "work", "--writable", str(self.home / "missing"), "fix it")
        self.assertEqual(ctx.exception.error, "invalid_writable_path")

    def test_writable_needs_a_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["cursor", "work", "--writable"])
        self.assertEqual(ctx.exception.error, "missing_writable_path")
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["cursor", "work", "--writable", "", "fix it"])
        self.assertEqual(ctx.exception.error, "missing_writable_path")

    def test_writable_is_refused_outside_work_mode_and_with_pass_through(self):
        for location, args in (
            (["--cwd", str(self.repo)], ("cursor", "safe", "--writable", str(self.home), "x")),
            ([], ("codex", "call", "--writable", str(self.home), "x")),
        ):
            with self.subTest(args=args), self.assertRaises(error_types.DelegateError) as ctx:
                parsed = parser_api.parse_cli([*location, "--json", "dry-run", *args])
                request_build.request_from_parsed(
                    parsed, delegate_config.embedded_default_config(), io.StringIO("")
                )
            self.assertEqual(ctx.exception.error, "invalid_option_combination")
            self.assertIn("work mode", ctx.exception.message)
        with self.assertRaises(error_types.DelegateError) as ctx:
            parsed = parser_api.parse_cli(
                [
                    "--cwd",
                    str(self.repo),
                    "--pass-through",
                    "cursor",
                    "work",
                    "--writable",
                    str(self.home),
                    "x",
                ]
            )
            request_build.request_from_parsed(
                parsed, delegate_config.embedded_default_config(), io.StringIO("")
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("pass-through", ctx.exception.message)

    def test_dry_run_payload_shows_the_guard_plan(self):
        (self.home / ".ssh" / "id").write_text("k", encoding="utf-8")
        patches = self.with_backend()
        with patches[0], patches[1]:
            request = self.build("cursor", "work", "--writable", str(self.home / ".gnupg"), "x")
            payload = self.delegate.dry_run_payload(request)
        guard = payload["writeGuard"]
        self.assertEqual(guard["backend"], "bwrap")
        self.assertEqual(guard["status"], "planned")
        self.assertIn(self.real(".ssh"), guard["protected"])
        self.assertNotIn(self.real(".gnupg"), guard["protected"])
        self.assertIn({"path": self.real(".gnupg"), "reason": "--writable"}, guard["writable"])

    def test_safe_dry_run_payload_has_no_guard_section(self):
        request = self.build("cursor", "safe", "look")
        self.assertNotIn("writeGuard", self.delegate.dry_run_payload(request))


class ForbidCommitHookTests(unittest.TestCase):
    """--forbid-commit is refused by git itself, in every isolation mode."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        run_git(self.repo, "add", "seed.txt")
        run_git(self.repo, "commit", "-q", "-m", "seed")
        self.hooks = write_guard.install_forbid_commit_hooks(self.root / "hooks")

    def env(self, base=None):
        env = {k: v for k, v in (base or os.environ).items() if not k.startswith("GIT_CONFIG")}
        env.update(write_guard.forbid_commit_env(str(self.hooks), env))
        return env

    def commit(self, *args, env=None):
        (self.repo / "change.txt").write_text(str(os.urandom(4)), encoding="utf-8")
        run_git(self.repo, "add", "change.txt", env=env)
        return run_git(self.repo, "commit", "-q", "-m", "child", *args, env=env, check=False)

    def head(self):
        return run_git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_every_commit_path_has_its_own_refusing_hook(self):
        # Names are spelled out on purpose: iterating the module constant would pass
        # with any hook silently missing from it. Git runs pre-merge-commit only for
        # a merge that creates a commit, and the other three also fire on that path,
        # so only running each hook itself proves each one refuses.
        for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "pre-merge-commit"):
            with self.subTest(hook=name):
                hook = self.hooks / name
                self.assertTrue(os.access(hook, os.X_OK))
                result = subprocess.run(
                    [str(hook)], capture_output=True, text=True, check=False, cwd=self.repo
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--forbid-commit", result.stderr)

    def test_commit_is_refused_with_the_reason(self):
        before = self.head()
        result = self.commit(env=self.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--forbid-commit", result.stderr)
        self.assertEqual(self.head(), before)

    def test_no_verify_does_not_get_around_it(self):
        before = self.head()
        result = self.commit("--no-verify", env=self.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(), before)

    def test_merge_commit_is_refused(self):
        run_git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "side.txt").write_text("s", encoding="utf-8")
        run_git(self.repo, "add", "side.txt")
        run_git(self.repo, "commit", "-q", "-m", "side")
        run_git(self.repo, "checkout", "-q", "main")
        (self.repo / "main.txt").write_text("m", encoding="utf-8")
        run_git(self.repo, "add", "main.txt")
        run_git(self.repo, "commit", "-q", "-m", "main")
        before = self.head()
        result = run_git(
            self.repo, "merge", "--no-ff", "-m", "merge", "side", env=self.env(), check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(), before)

    def test_without_the_env_the_same_commit_succeeds(self):
        before = self.head()
        result = self.commit(env=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.head(), before)

    def test_existing_git_config_env_is_preserved(self):
        base = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "delegate.carried",
            "GIT_CONFIG_VALUE_0": "Carried Over",
        }
        env = write_guard.forbid_commit_env(str(self.hooks), base)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "core.hooksPath")
        self.assertNotIn("GIT_CONFIG_KEY_0", env)
        merged = {**base, **env}
        name = run_git(self.repo, "config", "--get", "delegate.carried", env=merged).stdout.strip()
        hooks = run_git(self.repo, "config", "--get", "core.hooksPath", env=merged).stdout.strip()
        self.assertEqual(name, "Carried Over")
        self.assertEqual(hooks, str(self.hooks))

    def test_the_parameters_form_alone_still_refuses(self):
        # Codex's default shell policy drops variables whose names contain KEY, so the
        # indexed GIT_CONFIG_KEY_n form may never arrive; the parameters form must
        # carry the setting by itself.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
        updates = write_guard.forbid_commit_env(str(self.hooks), env)
        env["GIT_CONFIG_PARAMETERS"] = updates["GIT_CONFIG_PARAMETERS"]
        before = self.head()
        result = self.commit(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(), before)

    def test_a_path_with_a_quote_survives_the_parameters_form(self):
        odd = self.root / "it's hooks"
        write_guard.install_forbid_commit_hooks(odd)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
        env["GIT_CONFIG_PARAMETERS"] = write_guard.forbid_commit_env(str(odd), env)[
            "GIT_CONFIG_PARAMETERS"
        ]
        value = run_git(self.repo, "config", "--get", "core.hooksPath", env=env).stdout.strip()
        self.assertEqual(value, str(odd))

    def test_launch_seam_puts_the_hooks_into_the_child_environment(self):
        run_path = self.root / "run"
        run_path.mkdir()
        out = self.root / "env.txt"
        script = self.root / "child.sh"
        script.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$GIT_CONFIG_COUNT" "$GIT_CONFIG_KEY_0" "$GIT_CONFIG_VALUE_0" '
            f'> "{out}"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        process = runner._launch_tracked_process(
            [str(script)],
            str(self.repo),
            stdin_text=None,
            env_overrides={"GIT_CONFIG_COUNT": "0"},
            run_path=run_path,
            forbid_commit=True,
        )
        process.communicate(timeout=30)
        count, key, value = out.read_text(encoding="utf-8").split("\n")[:3]
        self.assertEqual(count, "1")
        self.assertEqual(key, "core.hooksPath")
        self.assertEqual(Path(value), run_path / write_guard.FORBID_COMMIT_HOOKS_DIRNAME)
        self.assertTrue((Path(value) / "pre-commit").exists())

    def test_launch_seam_without_forbid_commit_leaves_git_config_alone(self):
        run_path = self.root / "run"
        run_path.mkdir()
        out = self.root / "env.txt"
        script = self.root / "child.sh"
        script.write_text(f'#!/bin/sh\nprintf "%s" "${{GIT_CONFIG_COUNT:-unset}}" > "{out}"\n')
        script.chmod(0o755)
        with mock.patch.dict(os.environ):
            os.environ.pop("GIT_CONFIG_COUNT", None)
            process = runner._launch_tracked_process(
                [str(script)], str(self.repo), stdin_text=None, run_path=run_path
            )
            process.communicate(timeout=30)
        self.assertEqual(out.read_text(encoding="utf-8"), "unset")
        self.assertFalse((run_path / write_guard.FORBID_COMMIT_HOOKS_DIRNAME).exists())


class InPlaceCommitCountTests(unittest.TestCase):
    """The post-exit check that backs the hooks up when there is no worktree."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve()
        run_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        run_git(self.repo, "add", "a.txt")
        run_git(self.repo, "commit", "-q", "-m", "a")
        self.base = run_git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def ctx(self, **overrides):
        ctx = mock.Mock()
        ctx.isolation_lifecycle = "none"
        ctx.forbid_commit = True
        ctx.worktree_attachment = None
        ctx.creation_context = {"sourceHeadOid": self.base}
        ctx.execution_cwd = str(self.repo)
        ctx.workspace_kind = "git"
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    def commit(self):
        (self.repo / "b.txt").write_text("b", encoding="utf-8")
        run_git(self.repo, "add", "b.txt")
        run_git(self.repo, "commit", "-q", "-m", "b")

    def test_a_clean_in_place_run_verifies_zero_commits_and_keeps_its_exit_code(self):
        code, extra = runner._final_extra(self.ctx(), 0)
        self.assertEqual(code, 0)
        self.assertEqual(extra["commitPolicy"]["commitsCreatedCount"], 0)
        self.assertTrue(extra["commitPolicy"]["verified"])
        self.assertFalse(extra["commitPolicy"]["violated"])

    def test_a_commit_after_launch_fails_the_run(self):
        self.commit()
        code, extra = runner._final_extra(self.ctx(), 0)
        self.assertEqual(code, 1)
        self.assertEqual(extra["error"], "commit_policy_violated")
        self.assertEqual(extra["commitPolicy"]["commitsCreatedCount"], 1)

    def test_a_commit_that_skipped_the_hooks_is_still_caught(self):
        # core.hooksPath override is the one bypass the hooks cannot stop.
        (self.repo / "c.txt").write_text("c", encoding="utf-8")
        run_git(self.repo, "add", "c.txt")
        run_git(self.repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "sneaky")
        _, extra = runner._final_extra(self.ctx(), 0)
        self.assertTrue(extra["commitPolicyViolated"])

    def test_an_unverifiable_run_is_reported_not_passed(self):
        code, extra = runner._final_extra(self.ctx(workspace_kind="directory"), 0)
        self.assertEqual(code, 1)
        self.assertEqual(extra["error"], "commit_policy_unverified")

    def test_a_repo_without_commits_at_launch_counts_a_new_one(self):
        with tempfile.TemporaryDirectory() as raw:
            fresh = Path(raw).resolve()
            run_git(fresh, "init", "-q", "-b", "main")
            ctx = self.ctx(creation_context=None, execution_cwd=str(fresh))
            self.assertEqual(runner._in_place_commits_created(ctx), 0)
            (fresh / "f.txt").write_text("f", encoding="utf-8")
            run_git(fresh, "add", "f.txt")
            run_git(fresh, "commit", "-q", "-m", "first")
            self.assertEqual(runner._in_place_commits_created(ctx), 1)


if __name__ == "__main__":
    unittest.main()
